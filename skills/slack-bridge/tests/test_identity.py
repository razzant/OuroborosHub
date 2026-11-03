"""The authenticated bot identity uses the same gate on status and event wire."""
import asyncio
import json

import httpx
import pytest

from lib.host_adapter import create_host_adapter
from lib.events import parse_socket_envelope
from lib.presence import slack_identity, slack_presence_event
from lib.runtime import _discover_reporting
from lib.store import BridgeStore
from test_host_adapter import _item
from test_workers_plugin import _Api, _load_plugin


@pytest.mark.parametrize("advertised", [None, 0, 1])
def test_bot_identity_is_sent_only_when_advertised(tmp_path, monkeypatch, advertised):
    monkeypatch.setenv("HOST_SERVICE_TOKEN", "fixture")
    sent = []
    async def run():
        def handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"places_version": 1,
                    **({"presence_identity": advertised} if advertised is not None else {})})
            sent.append(json.loads(request.content)["event"])
            return httpx.Response(200, json={"status": "completed", "outcome": "silent"})
        # These are Slack's auth.test field names, not the event sender or app id.
        identity = slack_identity({"user_id": "U-BOT", "user": "ouroboros", "bot_id": "B-APP", "team_id": "T1"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = create_host_adapter("", http_client=http, identity=identity)
            await _discover_reporting(host, BridgeStore(tmp_path))
            await host.submit(_item())
        plugin, api = _load_plugin(), _Api(tmp_path)
        plugin.register(api)
        status = json.loads((await api.routes["status"][0]()).body)
        assert "connector_identity" not in status
        if advertised == 1:
            assert status["identity"] == sent[0]["identity"] == {
                "sends_as": {"id": "U-BOT", "label": "ouroboros", "kind": "bot"}}
        else:
            assert "identity" not in status and "identity" not in sent[0]
        assert "authenticated" not in sent[0]["actor"]
    asyncio.run(run())


@pytest.mark.parametrize("sender_id", ["U-BOT", "U-OTHER"])
def test_sending_identity_matches_its_message_actor_id(tmp_path, sender_id):
    auth = {"user_id": "U-BOT", "user": "ouroboros", "bot_id": "B-APP", "team_id": "T1"}
    identity = slack_identity(auth)
    payload = {"type": "events_api", "envelope_id": "env-1", "payload": {
        "team_id": "T1", "event_id": "Ev-1", "event": {
            "type": "message", "subtype": "bot_message", "user": sender_id,
            "bot_id": "B-APP" if sender_id == "U-BOT" else "B-OTHER",
            "channel": "C1", "channel_type": "channel", "ts": "1710000000.001", "text": "Sent text"}}}
    # Observe the sender as another connector would; our own echo is ignored.
    parsed = parse_socket_envelope(payload, bot_user_id="U-OBSERVER")
    assert parsed.accepted
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parsed)
    event = slack_presence_event(store.claim_inbox(), places=True)
    assert (identity["sends_as"]["id"] == event["actor"]["platform_actor_id"]) is (sender_id == "U-BOT")
    own_view = parse_socket_envelope(payload, bot_user_id=auth["user_id"], bot_id=auth["bot_id"])
    assert own_view.accepted is (sender_id != "U-BOT")
