from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.provider_context import observe
from lib.runtime import InboundWorker
from lib.slack_api import SlackClient
from lib.socket_mode import SocketModeClient
from lib.store import BridgeStore


def payload(event_id="event-1"):
    return {"type": "events_api", "envelope_id": "env-" + event_id,
            "payload": {"event_id": event_id, "team_id": "T_TEST",
                        "event": {"type": "message", "user": "U_TEST", "channel": "D_TEST",
                                  "channel_type": "im", "ts": "123.456", "text": "Hello"}}}


def user(name="Example Reader"):
    return {"id": "U_TEST", "name": "reader", "real_name": name, "tz": "Etc/UTC",
            "profile": {"display_name": name, "email": "reader@example.org", "title": "Contributor",
                        "image_72": "https://example.org/avatar.png"}}


def conversation():
    return {"id": "D_TEST", "name": "example-room", "is_im": True,
            "topic": {"value": "Current work"}, "purpose": {"value": "Discussion"}}


def host_response():
    return httpx.Response(200, json={"status": "completed", "outcome": "silent", "text": "",
                                    "turn_ref": "turn-1", "work_ref": ""})


def context_row(store):
    with sqlite3.connect(store.path) as db:
        return json.loads(db.execute("SELECT provider_context_json FROM inbox ORDER BY id LIMIT 1").fetchone()[0])


def test_provider_mock_through_worker_persists_context_before_host(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_name="Example workspace")
        acked = []
        calls = []
        class Socket:
            async def send(self, message):
                assert store.status()["inbox_pending"] == 1
                assert not calls
                acked.append(json.loads(message))
        def slack_handler(request):
            assert acked, "Provider enrichment belongs outside the Socket ACK path"
            calls.append(request.url.path)
            assert request.method == "GET" and request.content == b""
            if request.url.path.endswith("users.info"):
                assert request.url.params["user"] == "U_TEST"
                return httpx.Response(200, json={"ok": True, "user": user()})
            assert request.url.params["channel"] == "D_TEST"
            if request.url.path.endswith("conversations.replies"):
                return httpx.Response(200, json={"ok": True, "messages": [payload()["payload"]["event"]]})
            return httpx.Response(200, json={"ok": True, "channel": conversation()})
        events = []
        def host_handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"ok": True})
            event = json.loads(request.content)["event"]
            stored = context_row(store)
            assert stored["user"]["data"] == user()
            assert stored["conversation"]["data"] == conversation()
            assert event["actor"]["profile_lookup"]["observed_at"] == stored["user"]["observed_at"]
            events.append(event)
            return host_response()
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack_handler)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(host_handler)) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            host = LoopbackPresenceHostAdapter(binding_id="a" * 32, host_service_url="http://127.0.0.1:8877",
                                               skill_token="local-test", http_client=host_http)
            await SocketModeClient(slack, store, bot_user_id="U_BOT").handle_raw_message(Socket(), json.dumps(payload()))
            assert await InboundWorker(store, slack, host, staged_root=tmp_path / "staged").process_once()
        event = events[0]
        assert event["actor"]["platform_actor_id"] == "U_TEST"
        assert event["message"]["provider_facts"] == {"self_user_id": "U_BOT"}
        assert event["actor"]["actor_team_id"] == "T_TEST"
        assert event["actor"]["display_name"] == "Example Reader"
        assert event["actor"]["profile"]["email"] == "reader@example.org"
        assert event["actor"]["profile"]["title"] == "Contributor"
        assert event["actor"]["tz"] == "Etc/UTC"
        assert event["conversation"]["workspace_name"] == "Example workspace"
        assert event["conversation"]["channel_id"] == "D_TEST"
        assert event["conversation"]["topic"] == {"value": "Current work"}
        assert event["conversation"]["is_im"] is True
        assert event["thread_id"] == "123.456" and event["text"] == "Hello"
        assert sorted(calls) == ["/api/conversations.info", "/api/conversations.replies", "/api/users.info"]
        assert store.status()["inbox_delivered"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing_scope", "rate_limited", "network", "identity_mismatch"])
def test_failed_lookup_still_delivers_message_with_typed_gap(tmp_path, failure):
    async def run():
        store = BridgeStore(tmp_path)
        value = payload()
        store.ingest_envelope(value, parse_socket_envelope(value))
        def slack_handler(request):
            if request.url.path.endswith("conversations.replies"):
                return httpx.Response(200, json={"ok": True, "messages": [payload()["payload"]["event"]]})
            if request.url.path.endswith("conversations.info"):
                return httpx.Response(200, json={"ok": True, "channel": conversation()})
            if failure == "network":
                raise httpx.ReadError("unreachable", request=request)
            if failure == "rate_limited":
                return httpx.Response(429, headers={"Retry-After": "30"}, json={"ok": False, "error": "ratelimited"})
            if failure == "identity_mismatch":
                return httpx.Response(200, json={"ok": True, "user": {**user(), "id": "U_OTHER"}})
            return httpx.Response(200, json={"ok": False, "error": "missing_scope", "needed": "users:read"})
        events = []
        def host_handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"ok": True})
            events.append(json.loads(request.content)["event"])
            return host_response()
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack_handler)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(host_handler)) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            host = LoopbackPresenceHostAdapter(binding_id="a" * 32, host_service_url="http://127.0.0.1:8877",
                                               skill_token="local-test", http_client=host_http)
            await InboundWorker(store, slack, host, staged_root=tmp_path / "staged").process_once()
        actor = events[0]["actor"]
        assert actor["platform_actor_id"] == "U_TEST"
        assert actor["profile_lookup"]["status"] == "unavailable"
        assert actor["profile_lookup"]["error"]["code"]
        assert "display_name" not in actor and "profile" not in actor
        assert events[0]["conversation"]["name"] == "example-room"
        assert store.status()["inbox_delivered"] == 1
        if failure == "rate_limited":
            assert actor["profile_lookup"]["error"]["retry_after"] == 30
    asyncio.run(run())


def test_retry_after_restart_reuses_exact_snapshot_but_next_event_refreshes(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        value = payload()
        store.ingest_envelope(value, parse_socket_envelope(value))
        calls, events = [], []
        name = "First name"
        def slack_handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"ok": True, "user": user(name), "channel": conversation(),
                                             "messages": [payload()["payload"]["event"]]})
        def host_handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"ok": True})
            events.append(json.loads(request.content)["event"])
            if len(events) == 1:
                raise httpx.ReadTimeout("Host reply lost", request=request)
            return host_response()
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack_handler)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(host_handler)) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            host = LoopbackPresenceHostAdapter(binding_id="a" * 32, host_service_url="http://127.0.0.1:8877",
                                               skill_token="local-test", http_client=host_http)
            await InboundWorker(store, slack, host, staged_root=tmp_path / "staged").process_once()
            saved = context_row(store)
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE inbox SET available_at=0")
            name = "Changed name"
            reopened = BridgeStore(tmp_path)
            worker = InboundWorker(reopened, slack, host, staged_root=tmp_path / "staged")
            await worker.process_once()
            assert events[0] == events[1]
            assert context_row(reopened) == saved and len(calls) == 3
            next_value = payload("event-2")
            reopened.ingest_envelope(next_value, parse_socket_envelope(next_value))
            await worker.process_once()
        assert events[2]["actor"]["display_name"] == "Changed name" and len(calls) == 6
    asyncio.run(run())


def test_snapshot_write_failure_prevents_host_submission(tmp_path, monkeypatch):
    async def run():
        store = BridgeStore(tmp_path)
        value = payload()
        store.ingest_envelope(value, parse_socket_envelope(value))
        class Slack:
            async def user_info(self, _value): return user()
            async def conversation_info(self, _value): return conversation()
            async def read_messages(self, **_kwargs):
                return {"messages": [payload()["payload"]["event"]], "complete": True, "next_cursor": None}
        class Host:
            async def submit(self, _item): raise AssertionError("Must persist context first")
        def fail(*_args): raise sqlite3.OperationalError("disk unavailable")
        monkeypatch.setattr(store, "set_provider_context", fail)
        await InboundWorker(store, Slack(), Host(), staged_root=tmp_path / "staged").process_once()
        assert store.status()["inbox_pending"] == 1
        assert "disk unavailable" in store.status()["last_delivery_error"]
    asyncio.run(run())


def test_existing_database_migration_preserves_rows_and_pending_status(tmp_path):
    store = BridgeStore(tmp_path)
    value = payload()
    store.ingest_envelope(value, parse_socket_envelope(value))
    store.enqueue_outbox(request_id="queued", target="D_TEST", thread_ts="123.456", chunks=["queued text"])
    # Reproduce the pre-1.1 inbox schema without a context column.
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE inbox DROP COLUMN provider_context_json")
    reopened = BridgeStore(tmp_path)
    item = reopened.claim_inbox()
    assert item.provider_context is None
    assert item.text == "Hello" and item.actor_user_id == "U_TEST"
    assert reopened.status()["outbox_pending"] == 1
    assert reopened.claim_outbox().text == "queued text"
    BridgeStore(tmp_path)  # Migration is idempotent.


def test_lookup_timeout_and_cancellation_have_distinct_outcomes():
    async def timeout(): raise TimeoutError()
    async def cancel(): raise asyncio.CancelledError()
    result = asyncio.run(observe("users.info", timeout))
    assert result["status"] == "unavailable" and result["error"]["code"] == "lookup_timeout"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(observe("users.info", cancel))


@pytest.mark.parametrize("slow_method", ["users.info", "conversations.info"])
def test_real_lookup_wait_expiry_still_submits_original_message(tmp_path, monkeypatch, slow_method):
    # Python 3.10 raises asyncio.TimeoutError here, not built-in TimeoutError.
    # Shorten the existing wait in this test, but exercise its real expiry.
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.01 if timeout == 10.0 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_wait_for)

    async def run():
        store = BridgeStore(tmp_path)
        value = payload()
        store.ingest_envelope(value, parse_socket_envelope(value))
        events = []

        async def slack_handler(request):
            if request.url.path.endswith(slow_method):
                await asyncio.sleep(1)
            return httpx.Response(200, json={"ok": True, "user": user(), "channel": conversation(),
                                             "messages": [payload()["payload"]["event"]]})

        def host_handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"ok": True})
            events.append(json.loads(request.content)["event"])
            return host_response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(slack_handler)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(host_handler)) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            host = LoopbackPresenceHostAdapter(binding_id="a" * 32, host_service_url="http://127.0.0.1:8877",
                                               skill_token="local-test", http_client=host_http)
            assert await InboundWorker(store, slack, host, staged_root=tmp_path / "staged").process_once()

        assert len(events) == 1
        event = events[0]
        assert event["text"] == "Hello"
        assert event["actor"]["platform_actor_id"] == "U_TEST"
        assert event["account_id"] == "T_TEST" and event["conversation_id"] == "D_TEST"
        assert event["thread_id"] == "123.456"
        section, marker = (("actor", "profile_lookup") if slow_method == "users.info"
                           else ("conversation", "info_lookup"))
        observation = event[section][marker]
        assert observation["status"] == "unavailable"
        assert observation["error"]["code"] == "lookup_timeout"
        stored_section = "user" if slow_method == "users.info" else "conversation"
        assert context_row(store)[stored_section] == observation
        assert store.status()["inbox_delivered"] == 1
        assert store.status()["inbox_pending"] == 0

    asyncio.run(run())
