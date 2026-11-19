import asyncio

import pytest

from telegram_bot.events import parse_telegram_update
from telegram_bot.host import (PLACE_POLICY_UNREAD, SKILL_ACCESS_REFUSED, ConditionalDeliveryEnded,
                               PresenceHostClient, PresenceHostError, PresenceHostHTTPError)
from telegram_bot.runtime import TelegramTransportRuntime
from test_host import _event
from test_runtime import AcceptingSubmitter, Logger, RuntimeClient

NOT_CONNECTED = {"ok": False, "error": "Connect this place in Consciousness > Places.",
                 "code": "place_not_connected", "disposition": "blocked"}
# The Host's 403 carries a diagnostic, not words for a person; only the owner can lift it in Skills.
ACCESS_REFUSED = {"ok": False, "error": "skill 'telegram-bot' lacks grant 'presence'",
                  "code": "presence_auth_blocked", "disposition": "blocked"}


def _place_host(tmp_path, place, identity=None):
    """A real client whose Host answers /identity and /presence/place as told; an exception is raised."""
    class Transport:
        async def request_json(self, method, url, **kwargs):
            answer = identity if url.endswith("/identity") else place
            if isinstance(answer, BaseException):
                raise answer
            return answer if answer is not None else {"places_version": 1}
    return PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "token",
                              host_base="http://127.0.0.1:8767", transport=Transport())


@pytest.mark.parametrize("place", [
    OSError("refused"), TimeoutError(), PresenceHostError("presence Host returned invalid JSON"),
    PresenceHostHTTPError(503, "disk", {"code": "places_unavailable", "disposition": "retry"}),
    PresenceHostHTTPError(404, "not found"),  # a route that is not the Host's refusal
    PresenceHostHTTPError(409, "gone", {"ok": False, "code": "place_disconnected", "disposition": "blocked"}),
    {"place_id": "p", "label": "Telegram"},  # an answer that names no state says nothing about sending
])
def test_unread_place_policy_is_held_in_words_that_ask_nothing(tmp_path, place):
    assert asyncio.run(_place_host(tmp_path, place).place_hold("9", "-1")) == PLACE_POLICY_UNREAD


def _owner_condition():
    address = {"provider": "telegram", "account_id": "9", "conversation_id": "55", "thread_id": "4"}
    return {**address, "connector": "telegram-bot", "tool": "place_reply", "place_id": "confirming-place",
            "confirmed_at": "2025-01-01T00:00:00Z", "connected_revision": 1, "observed_revision": 3,
            "delivery_place": {**address, "connector": "telegram-bot", "version": 1,
                               "place_id": "outbound-place", "connected_revision": 2}}


@pytest.mark.parametrize("status,code,disposition,ok", [
    (409, "place_owner_confirmation_changed", "blocked", False),  # earlier Host
    (409, "place_owner_confirmation_changed", "retry", False),
    (503, "place_owner_confirmation_changed", "rejected", False),
    (409, "place_owner_confirmation_changed", "rejected", True),
    (409, "presence_recipient_capability_required", "blocked", False),
    (409, "other", "rejected", False),
    (503, "place_disconnected", "blocked", False),
    (409, "place_disconnected", "retry", False),
    (409, "place_disconnected", "blocked", True),
    (409, "place_scope_changed", "blocked", False),
    (409, "place_not_connected", "blocked", False),
])
def test_inconclusive_owner_check_keeps_the_row_held(tmp_path, status, code, disposition, ok):
    refusal = {"ok": ok, "code": code, "disposition": disposition, "error": "The owner confirmation changed."}
    host = _place_host(tmp_path, PresenceHostHTTPError(status, "fixture", refusal))
    assert asyncio.run(host.place_hold("9", "55", thread_id="4", owner_confirmation=_owner_condition())).startswith("Held:")


def test_conditional_send_uses_the_original_outbound_place_and_exact_echo(tmp_path):
    async def run():
        condition = _owner_condition()
        active = {"state": "active", "place_id": "outbound-place", "owner_confirmation": condition}
        for answer, expected in [(active, ""), ({**active, "place_id": "confirming-place"}, PLACE_POLICY_UNREAD),
                                 ({**active, "owner_confirmation": {**condition, "observed_revision": 4}}, PLACE_POLICY_UNREAD)]:
            host = _place_host(tmp_path, answer)
            assert await host.place_hold("9", "55", "4", owner_confirmation=condition) == expected
        refusal = {"ok": False, "code": "place_owner_confirmation_changed", "disposition": "rejected"}
        host = _place_host(tmp_path, PresenceHostHTTPError(409, "fixture", refusal))
        with pytest.raises(ConditionalDeliveryEnded):
            await host.place_hold("9", "55", "4", owner_confirmation=condition)
    asyncio.run(run())


def test_host_that_cannot_answer_identity_holds_in_the_same_words_and_older_hosts_do_not_hold(tmp_path):
    assert asyncio.run(_place_host(tmp_path, {}, identity=OSError("refused")).place_hold("9", "-1")) == PLACE_POLICY_UNREAD
    assert asyncio.run(_place_host(tmp_path, {}, identity={"ok": True, "places_version": False}).place_hold("9", "-1")) == PLACE_POLICY_UNREAD
    assert asyncio.run(_place_host(tmp_path, {}, identity={"ok": True}).place_hold("9", "-1")) == ""


def test_legacy_policy_exception_is_refreshed_before_the_next_send(tmp_path):
    async def run():
        class Transport:
            identities = iter([{"ok": True}, {"ok": True, "places_version": 1}])

            async def request_json(self, method, url, **kwargs):
                if url.endswith("/identity"):
                    return next(self.identities)
                return {"state": "paused"}

        host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "fixture",
                                  host_base="http://host.invalid", transport=Transport())
        assert await host.place_hold("9", "-1") == ""
        assert await host.place_hold("9", "-1") == "Held: Telegram is paused"
    asyncio.run(run())


def test_place_state_reads_as_before_and_not_connected_keeps_the_hosts_words(tmp_path):
    def hold(place):
        identity = {"ok": True, "places_version": 1, "presence_place_binding_version": 1}
        return asyncio.run(_place_host(tmp_path, place, identity=identity).place_hold("9", "-1"))
    assert hold({"state": "silent", "label": "Team chat"}) == "Held: Team chat is silent"
    assert hold({"state": "paused"}) == "Held: Telegram is paused"
    assert hold({"state": "active"}) == ""
    assert hold(PresenceHostHTTPError(404, "not connected", NOT_CONNECTED)) == "Held: Connect this place in Consciousness > Places."


@pytest.mark.parametrize("place,words", [
    (PresenceHostHTTPError(403, "refused", ACCESS_REFUSED), SKILL_ACCESS_REFUSED),
    (PresenceHostHTTPError(503, "disk", {"code": "places_unavailable", "disposition": "retry"}), PLACE_POLICY_UNREAD),
])
def test_a_refused_skill_sends_its_owner_to_skills_and_an_unread_place_still_retries(tmp_path, place, words):
    assert asyncio.run(_place_host(tmp_path, place).place_hold("9", "-1")) == words


@pytest.mark.parametrize("line,by_itself,in_skills,cause", [
    (PLACE_POLICY_UNREAD, True, False, "did not answer"),
    (SKILL_ACCESS_REFUSED, False, True, "refused"),
])
def test_a_hold_says_whether_its_reader_acts_and_leaves_the_hosts_answer_out(line, by_itself, in_skills, cause):
    assert line.startswith("Held: ")  # the outbox counts a hold by its prefix
    assert ("by itself" in line) is by_itself and ("in Skills" in line) is in_skills
    assert cause not in line  # which answer the Host gave changes nothing the reader does


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


@pytest.mark.parametrize("reason", ["Held: Telegram is silent", "Held: Telegram is paused", PLACE_POLICY_UNREAD])
def test_live_hold_preserves_parts_and_resumes_once(tmp_path, monkeypatch, reason):
    async def run():
        clock = [1000.0]
        monkeypatch.setattr("time.time", lambda: clock[0])
        class Host(AcceptingSubmitter):
            held = True
            async def place_hold(self, account, conversation):
                assert (account, conversation) == ("9", "-1")
                return reason if self.held else ""
        client, host = RuntimeClient("token"), Host()
        runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(), submitter=host, client_factory=lambda token: client)
        runtime.store.enqueue_outbox("stable", {"kind": "message", "chat_id": "-1", "text": "unchanged"})
        assert await runtime.process_one_outbox()
        assert not client.sent
        receipt = runtime.store.delivery_receipt("stable")
        assert receipt["attempts"] == 0 and receipt["error"] == reason and receipt["state"] == "held"
        assert runtime.store.status_snapshot()["outbox_held"] == 1
        host.held = False
        clock[0] += 6
        assert await runtime.process_one_outbox()
        assert len(client.sent) == 1
        assert runtime.store.delivery_receipt("stable")["state"] == "delivered"
        assert not await runtime.process_one_outbox()
    asyncio.run(run())
