import asyncio
import json

import httpx
import pytest

from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.store import InboxItem
from test_transport import rig as _transport_rig

rig = _transport_rig


def test_unbound_host_negotiates_facts_and_queries_current_state(monkeypatch):
    monkeypatch.setenv("EMAIL_USER", "bot@example.org")
    async def run():
        state = ["silent"]
        submitted = []
        def handler(request):
            assert request.headers["x-skill-token"] == "test"
            if request.url.path == "/identity":
                return httpx.Response(200, json={"places_version": 1})
            if request.url.path == "/presence/place":
                assert request.url.params["provider"] == "email"
                return httpx.Response(200, json={"state": state[0], "label": "Email"})
            if request.url.path == "/presence/work/retained":
                assert "binding_id" not in request.url.params
                return httpx.Response(200, json={"status": "completed", "outcome": "silent"})
            submitted.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "completed", "outcome": "silent"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with httpx.Client(transport=httpx.MockTransport(handler)) as sync:
                host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="test", http_client=http, place_http_client=sync)
                item = InboxItem(1, "lease", "INBOX", 4, "<m>", "", (), "a@example.org", "Subject", "Text", (), "", 1)
                await host.submit(item)
                assert "binding_id" not in submitted[0] and submitted[0]["event"]["facts"] == ["any_message"]
                assert "silent" in host.place_hold("bot@example.org", "<m>")
                state[0] = "active"
                assert host.place_hold("bot@example.org", "<m>") == ""
                assert (await host._poll("retained"))["status"] == "completed"
    asyncio.run(run())


@pytest.mark.parametrize("state", ["silent", "paused"])
def test_hold_keeps_message_id_bytes_and_retry_budget(rig, state):
    clock, _, client, store, host, runtime = rig
    held = [True]
    def check(account, conversation):
        assert account == "bot@example.org" and conversation == "<root>"
        return f"Held: Email is {state}" if held[0] else ""
    host.place_hold = check
    store.enqueue_outbox(request_id="stable", recipients=["a@example.org"], subject="subject", body="body", references=["<root>"])
    original = store.receipt("stable")["provider_message_id"]
    assert runtime.process_outbox()
    assert not client.sent
    receipt = store.receipt("stable")
    assert receipt["attempts"] == 0 and receipt["last_error"].startswith("Held:")
    assert store.status()["outbox_held"] == 1
    held[0] = False
    clock[0] += 6
    assert runtime.process_outbox()
    assert len(client.sent) == 1 and client.sent[0]["Message-ID"] == original
    assert "body" in client.sent[0].get_content()
    assert not runtime.process_outbox()
