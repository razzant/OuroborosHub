"""getMe identity, negotiated at the actual submit and status boundaries."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from telegram_bot.host import PresenceHostClient
from telegram_bot.events import parse_telegram_update
from telegram_bot.runtime import TelegramTransportRuntime
from test_host import _event
from test_runtime import AcceptingSubmitter, Logger, RuntimeClient


@pytest.mark.parametrize("advertised", [None, 0, 1])
def test_bot_identity_is_sent_only_when_advertised(tmp_path, advertised):
    (tmp_path / "settings.json").write_text(json.dumps({"binding_id": "a" * 32}))
    sent = []
    class Transport:
        async def request_json(self, method, url, **kwargs):
            if url.endswith("/identity"):
                return {"places_version": 1, **({"presence_identity": advertised} if advertised is not None else {})}
            sent.append(kwargs["payload"]["event"])
            return {"status": "completed", "outcome": "silent"}
    async def run():
        host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "fixture",
                                  host_base="http://127.0.0.1:8767", transport=Transport())
        runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "telegram-fixture",
                                           logger=Logger(), submitter=host, client_factory=RuntimeClient)
        await runtime._ready_client()  # RuntimeClient.get_me returns id 9 / presence_bot.
        await host.submit(_event(), [])
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("telegram_identity_fixture", root / "plugin.py",
                                                      submodule_search_locations=[str(root)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        plugin._RUNTIME, plugin._STATE_DIR = runtime, tmp_path
        status = json.loads((await plugin._status_route(None)).body)
        if advertised == 1:
            assert status["identity"] == sent[0]["identity"] == {
                "sends_as": {"id": "9", "label": "@presence_bot", "kind": "bot"}}
        else:
            assert "identity" not in status and "identity" not in sent[0]
        assert "authenticated" not in sent[0]["actor"]
    asyncio.run(run())


@pytest.mark.parametrize("sender_id", [9, 10])
def test_sending_identity_matches_its_message_actor_id(tmp_path, sender_id):
    async def run():
        host = AcceptingSubmitter()
        runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "telegram-fixture",
                                           logger=Logger(), submitter=host, client_factory=RuntimeClient)
        await runtime._ready_client()  # Real runtime identity from the mocked getMe response.
        update = {"update_id": 42, "message": {
            "message_id": 7, "date": 1_700_000_000, "text": "Sent text",
            "from": {"id": sender_id, "is_bot": True, "username": "presence_bot" if sender_id == 9 else "other_bot"},
            "chat": {"id": -456, "type": "supergroup", "title": "Community"}}}
        # Observe the sender as another connector would; our own echo is ignored.
        event = parse_telegram_update(update, bot_account_id="99", places=True)
        assert event is not None
        assert (host.connector_identity["sends_as"]["id"] == event.actor["platform_actor_id"]) is (sender_id == 9)
        own_view = parse_telegram_update(update, bot_account_id="9", places=True)
        assert (own_view is None) is (sender_id == 9)
    asyncio.run(run())
