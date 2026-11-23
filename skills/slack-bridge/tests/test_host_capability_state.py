"""What the bridge knows about the Host, and why it is not confirmed (1.13.0 round 8).

Only a Host answer changes the capabilities; a failed or unanswered request keeps the last confirmed answer. What the
adapter knows is recorded once per change, and a write that failed is retried at the next check without a new Host
request. A missing service key (no request is ever made) is told apart from a request that got no answer.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import os
import sqlite3

import httpx
import pytest

from card_text import card_items
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import BridgeRuntime, _discover_reporting
from lib.status_summary import attention_items
from lib.store import BridgeStore
from test_status_contract import _read_status_route, _TokensApi
from test_workers_plugin import _Slack

BINDING = "9f4c2a7e1b8053d6c4e2a9187b06f5d3"
ANSWER = {"ok": True, "presence_delivery_version": 1, "presence_reaction_context": 1, "presence_explicit_completion": 1}


def _adapter(handler, *, token: str = "skill-token", binding: str = BINDING) -> tuple[LoopbackPresenceHostAdapter, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LoopbackPresenceHostAdapter(binding_id=binding, host_service_url="http://127.0.0.1:8767",
                                       skill_token=token, http_client=http), http


def test_a_failed_capability_write_is_retried_at_the_next_check_without_a_new_request(tmp_path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=ANSWER)

    async def run() -> tuple[str | None, str | None, str]:
        adapter, http = _adapter(handler)
        store = BridgeStore(tmp_path)
        try:
            BridgeRuntime(store=store, slack=_Slack(), host=adapter, bot_user_id="U09OUROBOT1")
            original, locked = store.set_runtime, [True]

            def set_runtime(**values):
                if locked and "history_reporting_state" in values:
                    locked.clear()
                    raise sqlite3.OperationalError("database is locked")  # one transient lock
                return original(**values)

            store.set_runtime = set_runtime
            assert await adapter.discover_delivery_support() == 1  # the Host answered; the write failed
            before = store.runtime_value("history_reporting_state")
            assert await adapter.discover_delivery_support() == 1  # cached: no request, the write is retried
            after = store.runtime_value("history_reporting_state")
            return before, after, store.runtime_value("reaction_context_state")
        finally:
            store.close()
            await http.aclose()

    before, after, reactions = asyncio.run(run())
    assert requests == ["/identity"]
    assert before is None and after == "supported" and reactions == "available"


@pytest.mark.parametrize("first", ["200", "503"])
def test_a_failed_check_never_erases_a_confirmed_answer(tmp_path, first) -> None:
    """Two checks in flight after a failed start check, one answered and one not, finishing in either order."""
    gates = {"200": asyncio.Event(), "503": asyncio.Event()}
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if not calls:
            calls.append("start")
            return httpx.Response(503, json={"ok": False})
        answer = "200" if len(calls) == 1 else "503"
        calls.append(answer)
        await gates[answer].wait()
        return httpx.Response(200, json=ANSWER) if answer == "200" else httpx.Response(503, json={"ok": False})

    async def run() -> tuple:
        adapter, http = _adapter(handler)
        store = BridgeStore(tmp_path)
        try:
            BridgeRuntime(store=store, slack=_Slack(), host=adapter, bot_user_id="U09OUROBOT1")
            await adapter.discover_delivery_support()
            assert (adapter.delivery_reporting_status, adapter.delivery_reporting_cause) == ("unavailable", "request_failed")
            ok = asyncio.create_task(adapter.discover_delivery_support())
            await asyncio.sleep(0)
            failed = asyncio.create_task(adapter.discover_delivery_support())
            await asyncio.sleep(0)
            gates[first].set()
            await (ok if first == "200" else failed)
            gates["503" if first == "200" else "200"].set()
            await asyncio.gather(ok, failed)
            status = store.status()
            return (adapter.delivery_reporting_status, adapter.reaction_context, adapter.delivery_reporting_cause,
                    status["history_reporting_state"], status["reaction_context_state"], status["host_check_cause"])
        finally:
            store.close()
            await http.aclose()

    assert asyncio.run(run()) == ("supported", 1, "", "supported", "available", "")


def _host_payload(tmp_path, handler, *, token: str) -> dict:
    """The start check a companion makes (BridgeRuntime.run's first steps), then the real status route."""
    async def run() -> None:
        adapter, http = _adapter(handler, token=token, binding="")
        store = BridgeStore(tmp_path)
        try:
            BridgeRuntime(store=store, slack=_Slack(), host=adapter, bot_user_id="U09OUROBOT1")
            store.set_runtime(socket_state="connected", companion_pid=os.getpid())
            await _discover_reporting(adapter, store)
            store.set_runtime(host_adapter_state="active" if adapter.available else "missing_binding_id")
        finally:
            store.close()
            await http.aclose()

    asyncio.run(run())
    return _read_status_route(_TokensApi(tmp_path))


def test_a_missing_service_key_is_not_an_unanswered_request(tmp_path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=ANSWER)

    payload = _host_payload(tmp_path, handler, token="")
    assert requests == []  # without HOST_SERVICE_TOKEN nothing is asked
    assert payload["host_check_cause"] == "credential_missing"
    link = payload["attention"][0]
    assert link["detail"] == "host_adapter_inactive" and link.get("host_key_missing") is True
    assert "host_unanswered" not in link
    text = "\n".join(line for item in card_items([payload])[0] for line in item["lines"])
    assert "the bridge has no Ouroboros service key (HOST_SERVICE_TOKEN)" in text
    assert "In Extensions, turn Slack Bridge off and on; Ouroboros gives the bridge its key when it starts it." in text
    for words in ("checks again by itself", "asked again", "did not answer"):
        assert words not in text, (words, text)


def test_a_request_without_an_answer_keeps_the_automatic_retry_wording(tmp_path) -> None:
    payload = _host_payload(tmp_path, lambda _request: httpx.Response(503, json={"ok": False}), token="skill-token")
    assert payload["host_check_cause"] == "request_failed"
    link = payload["attention"][0]
    assert link.get("host_unanswered") is True and "host_key_missing" not in link
    text = "\n".join(line for item in card_items([payload])[0] for line in item["lines"])
    assert "The bridge checks again by itself with the next Slack event or send." in text
    assert "service key" not in text


@pytest.mark.parametrize("cause,gap", [("credential_missing", "host_key_missing"), ("request_failed", "host_unanswered"),
                                       (None, "host_unanswered")])
def test_the_summary_names_the_cause_of_an_unconfirmed_host(cause, gap) -> None:
    payload = {"link": {"state": "connected", "detail": "host_adapter_inactive", "companion_alive": True},
               "history_reporting_state": "unavailable", "reaction_context_state": "update_required",
               "recent_problems": {"inbound": {}, "outbound_text": {}, "mutations": {}},
               **({"host_check_cause": cause} if cause else {})}  # an older payload has no cause
    items = attention_items(payload)
    link = items[0]
    assert link.get("host_key_missing", False) is (cause == "credential_missing")
    assert link.get("host_unanswered", False) is (cause != "credential_missing")
    assert items[-1]["kind"] == "partial" and items[-1]["gaps"] == [gap]
