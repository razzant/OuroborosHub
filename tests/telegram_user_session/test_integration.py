"""The actual plugin tool produces an intent the companion consumes; no Telegram login."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from test_plugin import setup as plugin_setup, verified_account
from userbot.presence import PresenceResult
from userbot.runtime import UserSessionBridge
from userbot.store import DurableStore
from userbot.telegram import SendResult


class OfflineAdapter:
    def __init__(self):
        self.client = SimpleNamespace(get_entity=self.get_entity, get_messages=self.get_messages)
        self.requests = []

    async def get_entity(self, peer):
        return SimpleNamespace(forum=True, monoforum=False)

    async def get_messages(self, peer, *, ids):
        return SimpleNamespace(id=ids, sender_id=654321,
                               reply_to=SimpleNamespace(reply_to_top_id=42,
                                                        reply_to_msg_id=42,
                                                        forum_topic=True), action=None)

    async def topic_ids(self, peer):
        return {1, 42}

    async def send_text(self, peer, text, *, random_id, topic_id, reply_id):
        self.requests.append((peer, text, random_id, topic_id, reply_id))
        return SendResult(random_id, 808)


class OfflineHost:
    def report_delivered(self, **kwargs):
        raise AssertionError("Manual tool sends do not manufacture Presence delivery")


def test_plugin_send_intent_crosses_store_into_companion_and_rechecks_binding(tmp_path):
    api = plugin_setup(tmp_path)
    verified_account(api)
    send = api.tools["send_text"][0]
    adapter = OfflineAdapter()
    with DurableStore(api.state_dir / "transport.sqlite3", recover_attempted=True) as store:
        cfg = SimpleNamespace(account_id=654321, binding_id="b" * 32,
                              allowed_peer_ids=(-1001234567890, 200))
        bridge = UserSessionBridge(api.state_dir, cfg, adapter, store, OfflineHost())
        send("tool-01", -1001234567890, "Inside topic", 42, 45)
        job = store.get("654321", "tool-01")
        assert job.status == "queued"
        assert asyncio.run(bridge.send_one()) is True
        assert store.get("654321", "tool-01").status == "sent"
        assert adapter.requests == [(-1001234567890, "Inside topic", job.random_id, 42, 45)]
        # After revocation, an already-queued intent must never leak to old peer.
        send("tool-02", 200, "Do not send")
        bridge.config = SimpleNamespace(account_id=654321, binding_id="c" * 32,
                                        allowed_peer_ids=(-1001234567890, 200))
        assert asyncio.run(bridge.send_one()) is True
        assert store.get("654321", "tool-02").status == "failed"
        assert len(adapter.requests) == 1
