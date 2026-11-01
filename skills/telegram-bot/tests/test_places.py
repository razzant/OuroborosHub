import asyncio

import pytest

from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostClient
from telegram_bot.runtime import TelegramTransportRuntime
from test_host import _event
from test_runtime import AcceptingSubmitter, Logger, RuntimeClient


def test_private_chats_groups_channels_and_explicit_addressing_only():
    message = {"message_id": 1, "chat": {"id": -1, "type": "supergroup"}, "from": {"id": 3},
               "text": "😀 @presence_bot", "entities": [{"type": "mention", "offset": 3, "length": 13}],
               "reply_to_message": {"message_id": 2, "from": {"id": 9}}}
    def parse(kind="message", places=True):
        return parse_telegram_update({"update_id": 1, kind: message}, bot_account_id="9", bot_username="presence_bot", places=places)
    assert set(parse().facts) == {"any_message", "mention", "reply_to_me"}
    message.pop("entities"); message.pop("reply_to_message")
    assert parse().facts == ["any_message"]
    message["from"]["is_bot"] = True
    assert parse().facts == ["any_message", "from_bot"]
    assert parse(places=False) is None
    message["chat"]["type"] = "channel"
    assert parse("channel_post") is not None
    message["chat"]["type"] = "private"
    message["chat"]["id"] = 3
    message["from"]["is_bot"] = False
    private = parse()
    assert private is not None and private.conversation_id == "3"
    assert private.facts == ["any_message", "direct_message"]
    assert private.actor["platform_actor_id"] == "3"
    assert "facts" not in parse(places=False).to_dict()
    message["from"]["id"] = 9  # the bot's own echo in its private chat
    assert parse() is None
    message["chat"]["type"] = "unknown"
    message["from"]["id"] = 3
    assert parse() is None


def test_private_reply_from_bot_place_is_checked_not_refused(tmp_path):
    async def run():
        class Transport:
            lookups = []
            async def request_json(self, method, url, **kwargs):
                if url.endswith("/identity"):
                    return {"places_version": 1}
                assert "/presence/place?" in url
                self.lookups.append(url)
                return {"state": "active", "label": "Telegram"}
        transport = Transport()
        host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "token",
                                  host_base="http://127.0.0.1:8767", transport=transport)
        assert await host.place_hold("9", "3") == ""
        assert "conversation_id=3" in transport.lookups[-1]
    asyncio.run(run())


def test_place_lookup_names_a_thread_only_when_one_is_given(tmp_path):
    async def run():
        class Transport:
            lookups = []
            async def request_json(self, method, url, **kwargs):
                if url.endswith("/identity"):
                    return {"places_version": 1}
                self.lookups.append(url.split("?", 1)[1])
                return {"state": "active", "label": "Telegram"}
        transport = Transport()
        host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "token",
                                  host_base="http://127.0.0.1:8767", transport=transport)
        assert await host.place_hold("9", "-100", thread_id="5") == ""
        assert await host.place_hold("9", "-100") == ""
        assert transport.lookups == ["provider=telegram&account_id=9&conversation_id=-100&thread_id=5",
                                     "provider=telegram&account_id=9&conversation_id=-100"]
    asyncio.run(run())


def test_unbound_submission_negotiates_and_live_checks_do_not_cache_state(tmp_path):
    async def run():
        class Transport:
            state = "silent"
            calls = []
            async def request_json(self, method, url, **kwargs):
                if url.endswith("/identity"):
                    return {"places_version": 1}
                if "/presence/place?" in url:
                    assert "provider=telegram" in url and "account_id=9" in url
                    return {"state": self.state, "label": "Telegram"}
                if "/presence/work/retained" in url:
                    assert "binding_id" not in url
                    return {"status": "completed", "outcome": "silent"}
                self.calls.append(kwargs["payload"])
                return {"status": "completed", "outcome": "silent", "turn_ref": "t"}
        transport = Transport()
        host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "token", host_base="http://127.0.0.1:8767", transport=transport)
        event = {**_event(), "facts": ["mention"]}
        assert (await host.submit(event, [])).binding_id == ""
        assert transport.calls == [{"event": event, "staged_files": []}]
        assert "silent" in await host.place_hold("9", "-1")
        transport.state = "active"
        assert await host.place_hold("9", "-1") == ""
        assert (await host.poll("retained", "")).status == "completed"
        await host.submit(_event(), [])
        assert "facts" not in transport.calls[-1]["event"]  # pre-upgrade queued event
    asyncio.run(run())


@pytest.mark.parametrize("state", ["silent", "paused"])
def test_live_hold_preserves_parts_and_resumes_once(tmp_path, monkeypatch, state):
    async def run():
        clock = [1000.0]
        monkeypatch.setattr("time.time", lambda: clock[0])
        class Host(AcceptingSubmitter):
            held = True
            async def place_hold(self, account, conversation):
                assert (account, conversation) == ("9", "-1")
                return f"Held: Telegram is {state}" if self.held else ""
        client, host = RuntimeClient("token"), Host()
        runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(), submitter=host, client_factory=lambda token: client)
        runtime.store.enqueue_outbox("stable", {"kind": "message", "chat_id": "-1", "text": "unchanged"})
        assert await runtime.process_one_outbox()
        assert not client.sent
        receipt = runtime.store.delivery_receipt("stable")
        assert receipt["attempts"] == 0 and receipt["error"].startswith("Held:")
        assert runtime.store.status_snapshot()["outbox_held"] == 1
        host.held = False
        clock[0] += 6
        assert await runtime.process_one_outbox()
        assert len(client.sent) == 1
        assert runtime.store.delivery_receipt("stable")["state"] == "delivered"
        assert not await runtime.process_one_outbox()
    asyncio.run(run())
