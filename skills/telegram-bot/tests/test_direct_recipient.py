"""Private destinations through tool registration, persisted replies and the synthetic Bot API."""

import asyncio
import importlib.util
import io
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from fake_bot_api import Clock, FakeBotApi
from telegram_bot.custody import CALL_STARTED
from telegram_bot.events import parse_telegram_update
from test_delivery_reports import ReportingHost
from test_formatted_delivery import ready_again
from test_group_discovery import said
from test_unknown_outcomes import Wire
from test_worker_identity import instance


@pytest.fixture
def connector(tmp_path):
    settings = {"token": "first"}
    processes = []

    def register():
        parts = instance(tmp_path, settings)
        processes.append(parts)
        return parts

    server = register()
    first, second = FakeBotApi(bot_id=9), FakeBotApi(bot_id=77)
    for bot in (first, second):
        bot.chats["55"] = {"id": 55, "type": "private"}
    providers = {"first": first, "renewed": first, "second": second}
    client = server.runtime.client_factory
    server.runtime.client_factory = lambda token: client(token, transport=providers[token])
    server.runtime.submitter = ReportingHost()

    def send_tool(parts):
        _, send, metadata = next(tool for tool in parts.api.tools if tool[0] == "telegram_send")
        return send, metadata["schema"]

    yield SimpleNamespace(settings=settings, runtime=server.runtime, plugin=server.plugin,
                          first=first, second=second, initial_tool=send_tool(server),
                          worker_tool=lambda: send_tool(register()))
    for parts in processes:
        parts.api.unload()


@pytest.fixture
def confirmed_route():
    address = {"connector": "telegram-bot", "provider": "telegram", "account_id": "9",
               "conversation_id": "55", "thread_id": ""}
    return {**address, "tool": "ext_14_r_telegram-bot_telegram_send", "place_id": "confirming-place",
            "connected_revision": 1, "confirmed_at": "2025-01-01T00:00:00Z", "observed_revision": 3,
            "delivery_place": {**address, "version": 1,
                               "place_id": "outbound-place", "connected_revision": 2}}


def test_declared_recipient_doors_stay_stable_across_start_and_rotation(connector):
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "SKILL.md").read_text().split("---", 2)[1])
    route = {"provider": "telegram", "confirmation": "owner_confirmation"}
    assert manifest["place_reply"] == {"direct_recipient": route}
    assert {t["name"]: t["direct_recipient"] for t in manifest["tools"] if "direct_recipient" in t} == {
        "telegram_send": {**route, "account": "direct_account_id"}}
    send, schema = connector.initial_tool
    assert "direct_account_id" not in schema["required"]
    assert "default" not in schema["properties"]["direct_account_id"]
    assert send(chat_id="55", text="a", direct_account_id="9", request_id="cold")["state"] == "queued"
    assert not connector.first.calls
    assert asyncio.run(connector.runtime.process_one_outbox())
    assert connector.first.methods("getChat") == [{"chat_id": "55"}]
    assert connector.runtime.store.delivery_receipt("telegram-send:cold")["state"] == "delivered"
    for invalid in ("77", "", None, False, 9):
        assert not send(chat_id="55", text="a", direct_account_id=invalid)["ok"]
    for chat in ("-55", "0", "055", " 55", "55,56", str(1 << 40), 55):
        assert not send(chat_id=chat, text="a", direct_account_id="9")["ok"]
    connector.settings["token"] = "second"
    assert send(chat_id="55", text="b", direct_account_id="9", request_id="stale")["state"] == "queued"
    assert asyncio.run(connector.runtime.process_one_outbox())
    assert connector.runtime.store.delivery_receipt("telegram-send:stale")["state"] == "held"
    assert not connector.second.methods("sendMessage")
    assert not send(chat_id="55", text="a", direct_account_id="9")["ok"]
    connector.settings["token"] = "renewed"
    ready_again(connector.runtime.store)
    assert asyncio.run(connector.runtime.process_one_outbox())
    assert connector.runtime.store.delivery_receipt("telegram-send:stale")["state"] == "delivered"
    connector.settings["token"] = "second"
    assert send(chat_id="55", text="c", direct_account_id="77", request_id="new")["state"] == "queued"
    assert asyncio.run(connector.runtime.process_one_outbox())
    assert connector.second.methods("sendMessage")[0]["chat_id"] == "55"


@pytest.mark.parametrize("update_kind", ["message", "message_reaction"])
@pytest.mark.parametrize("chat, account, recipient", [
    ({"id": 55, "type": "private"}, "9", "55"),
    ({"id": (1 << 40) - 1, "type": "private"}, "9", str((1 << 40) - 1)),
    ({"id": -55, "type": "private"}, "9", None),
    ({"id": 1 << 40, "type": "private"}, "9", None),
    ({"id": 55.1, "type": "private"}, "9", None),
    ({"id": 55, "type": "private", "is_direct_messages": True}, "9", None),
    ({"id": 55, "type": "private"}, "unknown", None),
    ({"id": 55, "type": "group", "title": "Private"}, "9", None),
    ({"id": -55, "type": "supergroup"}, "9", None),
    ({"id": -55, "type": "channel"}, "9", None),
])
def test_recipient_is_the_private_chat_in_the_update(update_kind, chat, account, recipient):
    if update_kind == "message":
        update = said(1, chat)
    else:
        update = {"update_id": 1, "message_reaction": {"message_id": 1, "chat": chat, "user": {"id": 3},
                  "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}}
    event = parse_telegram_update(update, bot_account_id=account, places=True)
    assert event.actor["platform_actor_id"] == "3"
    assert event.conversation.get("direct_reply_user_id") == recipient
    assert ("direct_reply_user_id" in event.conversation) == (recipient is not None)
    assert event.message.get("reply_targets") == ([] if recipient else None)


@pytest.mark.parametrize("pinned", [False, True])
def test_reusing_a_request_id_preserves_its_first_destination(tmp_path, connector, pinned):
    asyncio.run(connector.runtime._ready_client())
    send, _ = connector.worker_tool()
    common = {"chat_id": "55", "text": "a", "request_id": "once"}
    pin = {"direct_account_id": "9"}
    args = {**common, **(pin if pinned else {})}
    assert send(**args)["state"] == "queued"
    store = connector.runtime.store
    first = store.outbox_payload("telegram-send:once")
    assert send(**args) == {"ok": True, "state": "already_queued", "request_id": "once",
                            "chat_id": "55", "kind": "message"}
    source = tmp_path / "fixture.txt"
    source.write_text("fixture")
    for change in ({"chat_id": "56"}, {"topic_id": "4"}, {"reply_to_message_id": "3"},
                   {"kind": "document", "file_path": str(source)}):
        assert send(**{**args, **change})["ok"] is False
    assert send(**{**common, **({} if pinned else pin)})["ok"] is False
    assert store.outbox_payload("telegram-send:once") == first


def test_unpinned_repeat_preserves_the_established_sending_bot(connector):
    async def run():
        rt = connector.runtime
        send, _ = connector.worker_tool()
        args = {"chat_id": "-55", "text": "fixture", "request_id": "retained-bot"}
        assert send(**args)["state"] == "queued"
        assert await rt.process_one_outbox()
        first = rt.store.outbox_payload("telegram-send:retained-bot")
        assert first["_sending_account_id"] == "9"
        connector.settings["token"] = "renewed"
        assert (await rt._ready_client()).account_id == "9"
        assert send(**args)["state"] == "already_queued"
        connector.settings["token"] = "second"
        assert (await rt._ready_client()).account_id == "77"
        assert send(**args)["ok"] is False
        assert rt.store.outbox_payload("telegram-send:retained-bot") == first
        assert len(connector.first.methods("sendMessage")) == 1 and not connector.second.methods("sendMessage")
    asyncio.run(run())


def test_conditional_repeat_does_not_replace_the_original_proof(connector, confirmed_route):
    async def run():
        rt = connector.runtime
        send, _ = connector.worker_tool()
        args = {"chat_id": "55", "direct_account_id": "9", "text": "fixture", "request_id": "proof"}
        assert send(**args, owner_confirmation=confirmed_route)["state"] == "queued"
        first = rt.store.outbox_payload("telegram-send:proof")
        fresh = {**confirmed_route, "observed_revision": confirmed_route["observed_revision"] + 1}
        assert send(**args, owner_confirmation=fresh)["state"] == "already_queued"
        for change in ({"tool": "place_reply"}, {"connector": "other"}, {"place_id": "different"},
                       {"delivery_place": {**fresh["delivery_place"], "place_id": "different"}}):
            assert send(**args, owner_confirmation={**fresh, **change})["ok"] is False
        assert send(**args)["ok"] is False
        assert rt.store.outbox_payload("telegram-send:proof") == first

        async def place(account, chat, *, owner_confirmation):
            assert (account, chat, owner_confirmation) == ("9", "55", confirmed_route)
            return ""

        rt.submitter.place_hold = place
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt("telegram-send:proof")["state"] == "delivered"
    asyncio.run(run())


def test_a_private_dialog_needs_telegram_chat_type_before_sending(connector):
    async def run():
        rt = connector.runtime
        send, _ = connector.initial_tool
        assert send(chat_id="55", text="fixture", direct_account_id="9", request_id="chat-type")["ok"]
        delivery = "telegram-send:chat-type"
        connector.first.answers["getChat"] = [{"ok": True, "result": {"id": 55}}]
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt(delivery)["state"] == "held"
        assert not connector.first.methods("sendMessage")
        ready_again(rt.store)  # Next getChat returns the fixture's typed private dialog.
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt(delivery)["state"] == "delivered"
    asyncio.run(run())


@pytest.fixture
def direct_wire(connector, monkeypatch):
    wire, clock = Wire(), Clock()
    monkeypatch.setattr(urllib.request, "urlopen", wire)
    rt = connector.runtime
    runtime_module = sys.modules[type(rt).__module__]
    custody_module = sys.modules[type(rt.store).__module__]
    fake_time = SimpleNamespace(time=clock, strftime=time.strftime, gmtime=time.gmtime)
    monkeypatch.setattr(runtime_module, "time", fake_time)
    monkeypatch.setattr(custody_module, "time", fake_time)
    rt.client_factory = lambda token: runtime_module.TelegramClient(token, api_server="http://telegram.invalid")
    return wire, clock


@pytest.mark.parametrize("refused_once", [False, True], ids=["accepted-first-post", "refused-first-post"])
def test_pinned_send_recovers_after_six_transient_getchat_failures(connector, direct_wire, refused_once):
    async def run():
        rt, (wire, clock) = connector.runtime, direct_wire
        send, _ = connector.initial_tool
        assert send(chat_id="55", text="fixture", markdown=False, direct_account_id="9",
                    request_id="recover-chat")["state"] == "queued"
        delivery = "telegram-send:recover-chat"
        flood = urllib.error.HTTPError("http://telegram.invalid", 429, "Too Many Requests", {}, io.BytesIO(
            b'{"ok":false,"error_code":429,"description":"Too Many Requests","parameters":{"retry_after":40}}'))
        chat = {"id": 55, "type": "private"}
        reply = {"message_id": 501, "chat": chat, "text": "fixture"}
        wire.answer("getChat", "no_dns", "lost", 502, "reset", 500, flood, {"result": chat}, {"result": chat})
        if refused_once:
            wire.answer("sendMessage", 429)
        wire.answer("sendMessage", {"result": reply})

        for reads, delay in enumerate((5, 5, 5, 5, 5, 40), 1):
            assert await rt.process_one_outbox()
            receipt = rt.store.delivery_receipt(delivery)
            assert receipt["state"] == "pending" and receipt["attempts"] == 0
            assert not receipt["provider_receipt"] and not receipt["history_reports"]
            assert CALL_STARTED not in rt.store.outbox_payload(delivery)
            assert wire.attempted.count("getChat") == reads and not wire.sent("sendMessage")
            clock.advance(delay - 1)
            assert not await rt.process_one_outbox()
            clock.advance(1)

        assert await rt.process_one_outbox()
        if refused_once:
            receipt = rt.store.delivery_receipt(delivery)
            assert receipt["state"] == "pending" and receipt["attempts"] == 1
            assert CALL_STARTED not in rt.store.outbox_payload(delivery)
            assert len(wire.sent("sendMessage")) == 1
            assert not await rt.process_one_outbox()
            clock.advance(2)
            assert await rt.process_one_outbox()

        receipt = rt.store.delivery_receipt(delivery)
        assert receipt["state"] == "delivered" and receipt["attempts"] == 1 + int(refused_once)
        assert receipt["provider_receipt"] == {"kind": "message", "messages": [reply]}
        [report] = receipt["history_reports"]
        assert report["payload"]["state"] == "delivered"
        assert report["payload"]["message"]["message_id"] == 501
        assert CALL_STARTED not in rt.store.outbox_payload(delivery)
        assert not await rt.process_one_outbox()
        assert wire.sent("sendMessage") == [({"chat_id": "55", "text": "fixture"}, {})] * (1 + int(refused_once))
    asyncio.run(run())


@pytest.mark.parametrize("status, body", [
    (400, b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'),
    (404, b'Not Found'),
], ids=["bot-refusal", "http-only-refusal"])
def test_pinned_send_keeps_the_attempt_limit_for_getchat_refusals(connector, direct_wire, status, body):
    async def run():
        rt, (wire, clock) = connector.runtime, direct_wire
        send, _ = connector.initial_tool
        assert send(chat_id="55", text="fixture", direct_account_id="9",
                    request_id="refused-chat")["state"] == "queued"
        delivery = "telegram-send:refused-chat"
        wire.answer("getChat", *(urllib.error.HTTPError("http://telegram.invalid", status, "Refused", {},
                                                        io.BytesIO(body)) for _ in range(6)))
        for attempts in range(1, 6):
            assert await rt.process_one_outbox()
            receipt = rt.store.delivery_receipt(delivery)
            assert receipt["attempts"] == attempts
            assert receipt["state"] == ("pending" if attempts < 5 else "failed")
            clock.advance(32)
        assert not await rt.process_one_outbox()
        assert wire.attempted.count("getChat") == 5 and not wire.sent("sendMessage")
    asyncio.run(run())


@pytest.mark.parametrize("deferred", [False, True])
def test_captured_reply_uses_its_own_bot_after_rotation(tmp_path, connector, deferred):
    wire_events = []

    class HostWire:
        async def request_json(self, method, url, *, payload, **kwargs):
            if url.endswith("/identity"):
                return {"places_version": 1, "presence_identity": 1}
            if "/presence/place?" in url:
                return {"state": "active"}
            if "/presence/work/" in url:
                return {"status": "completed", "outcome": "message", "text": "reply"}
            assert method == "POST" and url.endswith("/presence/turn")
            wire_events.append(payload["event"])
            return {"status": "completed", "turn_ref": "turn", **(
                {"outcome": "deferred", "work_ref": "work"} if deferred else {"outcome": "message", "text": "reply"})}

    async def run():
        rt = connector.runtime
        rt.submitter = connector.plugin.PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "fixture",
                                                            host_base="http://host.invalid", transport=HostWire())
        bot = await rt._ready_client()
        event = parse_telegram_update(said(1, {"id": 55, "type": "private"}, is_topic_message=True,
                                           message_thread_id=4), bot_account_id=bot.account_id, places=True)
        rt.store.commit_update(1, event)
        connector.settings["token"] = "second"
        await rt._ready_client()
        assert await rt.process_one_inbox()
        if deferred:
            assert await rt.process_one_work()
        captured, = wire_events
        assert captured["identity"]["sends_as"]["id"] == captured["account_id"] == "9"
        assert captured["conversation"]["direct_reply_user_id"] == "55"
        delivery = f"presence:{event.source_event_id}:" + ("work" if deferred else "turn")
        row = rt.store.outbox_payload(delivery)
        assert (row["kind"], row["direct_account_id"], row["chat_id"], row["direct_reply_user_id"]) == (
            "direct_send", "9", "55", "55")
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt(delivery)["state"] == "held"
        assert not connector.second.methods("sendMessage")
        connector.settings["token"] = "renewed"  # another token verified as the original bot
        ready_again(rt.store)
        assert await rt.process_one_outbox()
        message, = connector.first.methods("sendMessage")
        assert (message["chat_id"], message["message_thread_id"], message["reply_parameters"]) == (
            "55", 4, {"message_id": 1})
        assert rt.store.delivery_receipt(delivery)["state"] == "delivered"
    asyncio.run(run())


@pytest.mark.parametrize("source, expected_kind", [("old", "message"), ("inconsistent", "direct_send")])
def test_private_reply_with_old_or_inconsistent_evidence(connector, source, expected_kind):
    async def run():
        rt = connector.runtime
        event = parse_telegram_update(said(1, {"id": 55, "type": "private"}), bot_account_id="9", places=True)
        conversation = dict(event.conversation)
        if source == "old":
            del conversation["direct_reply_user_id"]  # 0.12.1 retained the receiving bot
        else:
            conversation["direct_reply_user_id"] = "56"
        rt.store.commit_update(1, replace(event, conversation=conversation))
        assert rt.store.claim_inbox()
        rt.store.record_submission(event.source_event_id, binding_id="", turn_ref="turn",
                                   outcome="message", text="reply", work_ref="")
        delivery = f"presence:{event.source_event_id}:turn"
        assert rt.store.outbox_payload(delivery)["kind"] == expected_kind
        connector.settings["token"] = "second"
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt(delivery)["state"] == "held"
        assert not connector.second.methods("sendMessage")
        connector.settings["token"] = "first"
        ready_again(rt.store)
        assert await rt.process_one_outbox()
        if source == "old":
            assert rt.store.delivery_receipt(delivery)["state"] == "delivered"
            assert "direct_reply_user_id" not in rt.store.outbox_payload(delivery)
        else:
            assert rt.store.delivery_receipt(delivery)["state"] == "held"
            assert not connector.first.methods("sendMessage")
    asyncio.run(run())


@pytest.mark.parametrize("kind, method", [("document", "sendDocument"), ("album", "sendMediaGroup")])
def test_direct_uploads_keep_the_checked_chat_and_bot(tmp_path, connector, kind, method):
    async def run():
        rt = connector.runtime
        await rt._ready_client()
        send, _ = connector.worker_tool()
        file = tmp_path / "fixture.txt"
        file.write_bytes(b"fixture")
        part = {"kind": "document", "file_path": str(file)}
        content = {"file_path": str(file)} if kind == "document" else {"items": [part, part]}
        assert send(chat_id="55", direct_account_id="9", kind=kind, request_id=kind, **content)["ok"]
        connector.settings["token"] = "second"
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt("telegram-send:" + kind)["state"] == "held"
        assert not connector.second.methods(method)
        connector.settings["token"] = "renewed"
        ready_again(rt.store)
        assert await rt.process_one_outbox()
        physical, = connector.first.methods(method)
        assert physical["chat_id"] == "55"
        receipt = rt.store.delivery_receipt("telegram-send:" + kind)
        assert receipt["state"] == "delivered" and receipt["provider_receipt"]["kind"] == kind
    asyncio.run(run())


@pytest.mark.parametrize("pinned", [False, True])
def test_inflight_token_change_keeps_the_client_place_and_reports_together(connector, pinned):
    async def run():
        rt = connector.runtime
        await rt._ready_client()
        send, _ = connector.worker_tool()
        pin = {"direct_account_id": "9"} if pinned else {}
        assert send(chat_id="55", text="**Hello**", topic_id="4", request_id="rotate", **pin)["ok"]
        delivery = "telegram-send:rotate"
        route, steps = ("9", "55", "4"), []

        async def discover():
            connector.settings["token"] = "second"
            await rt._ready_client()  # a different client is now the current one
            return 1

        async def guard(account_id, chat_id, thread_id):
            assert CALL_STARTED not in rt.store.outbox_payload(delivery)
            steps.append((account_id, chat_id, thread_id))
            return ""

        scripted = connector.first._scripted

        def provider_answer(method):
            if method == "sendMessage":
                row = rt.store.outbox_payload(delivery)
                assert row[CALL_STARTED] == {"part": 0, "method": method}
                assert row["_sending_account_id"] == "9"
                steps.append("post")
            return scripted(method)

        rt.submitter.discover_delivery_support = discover
        rt.submitter.place_hold = guard
        connector.first._scripted = provider_answer
        connector.first.answers["sendMessage"] = [{"ok": False, "error_code": 400, "description": "format"}]
        assert await rt.process_one_outbox()
        assert steps == ([route] if pinned else []) + [route, "post"] * 2
        formatted, plain = connector.first.methods("sendMessage")
        assert formatted["chat_id"] == plain["chat_id"] == "55"
        assert formatted["message_thread_id"] == plain["message_thread_id"] == 4
        assert formatted["parse_mode"] == "HTML" and "parse_mode" not in plain
        assert not connector.second.methods("sendMessage")
        receipt = rt.store.delivery_receipt(delivery)
        assert receipt["state"] == "delivered"
        report = receipt["history_reports"][0]["payload"]
        assert (report["account_id"], report["conversation_id"], report["message"]["kind"]) == ("9", "55", "message")
    asyncio.run(run())


def test_disconnected_outbound_place_releases_fifo_with_confirmed_parts(connector, confirmed_route, tmp_path):
    async def run():
        rt = connector.runtime
        client_type = connector.plugin.PresenceHostClient
        host_error = sys.modules[client_type.__module__].PresenceHostHTTPError

        class HostWire:
            async def request_json(self, method, url, **kwargs):
                if url.endswith("/identity"):
                    return {"ok": True, "places_version": 1, "presence_delivery_version": 1}
                assert method == "GET" and "/presence/place?" in url
                place = {"state": "active", "place_id": "outbound-place"}
                if "owner_confirmation=" not in url:
                    return place
                if connector.first.methods("sendMessage"):
                    raise host_error(409, "disconnected", {"ok": False, "code": "place_disconnected", "disposition": "blocked"})
                return {**place, "owner_confirmation": confirmed_route}

        rt.submitter = client_type(state_dir=tmp_path, token_provider=lambda: "fixture",
                                   host_base="http://host.invalid", transport=HostWire())
        send, _ = connector.initial_tool
        assert send(chat_id="55", text="a" * 4100, markdown=False, direct_account_id="9",
                    owner_confirmation=confirmed_route, request_id="split")["state"] == "queued"
        assert send(chat_id="55", text="next", request_id="next")["state"] == "queued"
        assert await rt.process_one_outbox()
        receipt = rt.store.delivery_receipt("telegram-send:split")
        assert receipt["state"] == "failed" and len(receipt["confirmed_messages"]) == 1
        assert "original outbound Place was disconnected" in receipt["error"]
        assert [r["payload"]["state"] for r in receipt["history_reports"] if r["payload"]["part_id"] == "status"] == ["failed"]
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt("telegram-send:next")["state"] == "delivered"
        calls = connector.first.methods("sendMessage")
        assert len(calls) == 2 and calls[-1]["text"] == "next"
        assert not await rt.process_one_outbox()
    asyncio.run(run())


@pytest.mark.parametrize("protected", [False, True])
def test_partial_direct_send_stays_held_and_an_unknown_last_part_is_not_repeated(connector, confirmed_route, protected):
    async def run():
        rt = connector.runtime
        await rt._ready_client()
        send, _ = connector.worker_tool()
        binding = confirmed_route if protected else None
        assert send(chat_id="55", text="a" * 4100, markdown=False, direct_account_id="9", request_id="parts",
                    owner_confirmation=binding)["ok"]
        delivery = "telegram-send:parts"
        assert len(rt.store.outbox_payload(delivery)["_rendered_chunks"]) == 2
        async def place(account, chat, *, owner_confirmation=None):
            assert owner_confirmation == binding
            return "Held: private place paused" if connector.first.methods("sendMessage") else ""
        rt.submitter.place_hold = place
        assert await rt.process_one_outbox()
        receipt = rt.store.delivery_receipt(delivery)
        assert receipt["state"] == "held" and len(receipt["confirmed_messages"]) == 1
        connector.settings["token"] = "second"
        ready_again(rt.store)
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt(delivery)["state"] == "held"
        assert not connector.second.methods("sendMessage")
        async def active(account, chat, *, owner_confirmation=None):
            assert owner_confirmation == binding
            return ""
        rt.submitter.place_hold = active
        connector.settings["token"] = "first"
        connector.first.answers["sendMessage"] = [{"ok": False, "error_code": 502, "description": "unknown"}]
        ready_again(rt.store)
        assert await rt.process_one_outbox()
        receipt = rt.store.delivery_receipt(delivery)
        assert receipt["state"] == "uncertain" and len(receipt["confirmed_messages"]) == 1
        assert not await rt.process_one_outbox()
        assert len(connector.first.methods("sendMessage")) == 2
    asyncio.run(run())


@pytest.mark.parametrize("kind, method", [("message", "sendMessage"), ("document", "sendDocument"), ("album", "sendMediaGroup")])
def test_each_physical_send_requires_the_same_owner_confirmation(tmp_path, connector, confirmed_route, kind, method):
    async def run():
        rt = connector.runtime
        send, _ = connector.initial_tool
        file = tmp_path / "example.txt"
        file.write_text("Example")
        item = {"kind": "document", "file_path": str(file)}
        content = {"text": "**example**"} if kind == "message" else (
            {"caption": "**example**", "items": [item, item]} if kind == "album" else
            {"caption": "**example**", "file_path": str(file)})
        checks = []

        delivery = "telegram-send:" + kind

        async def place(account, chat, *, owner_confirmation=None):
            assert CALL_STARTED not in rt.store.outbox_payload(delivery)
            checks.append((account, chat, owner_confirmation))
            return "Held: private policy check unavailable" if connector.first.methods(method) else ""

        scripted = connector.first._scripted

        def provider_answer(endpoint):
            if endpoint == method:
                row = rt.store.outbox_payload(delivery)
                assert row[CALL_STARTED] == {"part": 0, "method": method}
                assert row["_sending_account_id"] == "9"
            return scripted(endpoint)

        rt.submitter.place_hold = place
        connector.first._scripted = provider_answer
        connector.first.answers[method] = [{"ok": False, "error_code": 400, "description": "format"}]
        assert send(chat_id="55", kind=kind, direct_account_id="9", owner_confirmation=confirmed_route,
                    request_id=kind, **content)["ok"]
        assert await rt.process_one_outbox()
        # One check before getChat/file validation, then one before each attempted POST.
        assert checks == [("9", "55", confirmed_route)] * 3
        assert rt.store.delivery_receipt("telegram-send:" + kind)["state"] == "held"
        ready_again(rt.store)
        assert await rt.process_one_outbox()
        assert rt.store.delivery_receipt("telegram-send:" + kind)["state"] == "held"
        assert len(connector.first.methods(method)) == 1 and not connector.second.methods(method)
    asyncio.run(run())


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("case", ["queued", "file", "partial", "unanswered"])
def test_address_bound_queue_survives_a_0_12_1_rollback(tmp_path, monkeypatch, confirmed_route, protected, case):
    previous = os.environ.get("TELEGRAM_BOT_0121_ROOT")
    if not previous:
        pytest.skip("Set TELEGRAM_BOT_0121_ROOT to the 0.12.1 payload to exercise rollback")
    root = Path(previous).resolve()
    assert yaml.safe_load((root / "SKILL.md").read_text().split("---", 2)[1])["version"] == "0.12.1"
    settings = {"token": "first"}
    first, second = FakeBotApi(bot_id=9), FakeBotApi(bot_id=77)
    for bot in (first, second):
        bot.chats["55"] = {"id": 55, "type": "private"}
    bots = {"first": first, "renewed": first, "second": second}
    confirmation = confirmed_route if protected else None
    delivery = "telegram-send:pinned"
    multipart = case in {"partial", "unanswered"}
    text = "a" * 4100 if multipart else "pinned"

    class Interrupted(BaseException):
        """Lose the answer and stop without releasing the lease, on the fake transport only."""

    async def active(*args, **kwargs):
        return ""

    def start(legacy=""):
        plugin = None
        if legacy:
            # A separate package per incarnation: exercise the old store initializer again on rollback.
            spec = importlib.util.spec_from_file_location(f"telegram_bot_0121_{legacy}", root / "plugin.py",
                                                          submodule_search_locations=[str(root)])
            plugin = importlib.util.module_from_spec(spec)
            monkeypatch.setitem(sys.modules, spec.name, plugin)
            spec.loader.exec_module(plugin)
        parts = instance(tmp_path, settings, plugin=plugin)
        client = parts.runtime.client_factory
        parts.runtime.client_factory = lambda token: client(token, transport=bots[token])
        parts.runtime.submitter = ReportingHost()
        parts.runtime.submitter.place_hold = active
        return parts

    def row(runtime, key):
        with runtime.store._connect() as db:
            return tuple(db.execute("SELECT rowid, * FROM outbox WHERE delivery_id=?", (key,)).fetchone())

    async def run():
        seed = start("seed")
        try:
            assert seed.runtime.store.enqueue_outbox("legacy-before", {"kind": "message", "chat_id": "-41", "text": "legacy"})
            assert await seed.runtime.process_one_outbox()
            legacy_row = row(seed.runtime, "legacy-before")
        finally:
            seed.api.unload()
        start_count = len(first.methods("sendMessage"))

        current = start()
        try:
            rt = current.runtime
            assert row(rt, "legacy-before") == legacy_row
            with rt.store._connect() as db:
                assert {"outbox_ready_idx", "outbox_reports_due"} <= {entry[1] for entry in db.execute("PRAGMA index_list(outbox)")}
            send = next(handler for name, handler, _ in current.api.tools if name == "telegram_send")
            content = {"text": text}
            if case == "file":
                source = tmp_path / "example.txt"
                source.write_bytes(b"queued bytes")
                content = {"kind": "document", "file_path": str(source)}
            assert send(chat_id="55", direct_account_id="9", owner_confirmation=confirmation,
                        request_id="pinned", **content)["state"] == "queued"
            if case == "file":
                source.write_bytes(b"changed source")
            if case == "partial":
                async def pause_after_first(*args, **kwargs):
                    return "Held: fixture pause" if len(first.methods("sendMessage")) > start_count else ""
                rt.submitter.place_hold = pause_after_first
                assert await rt.process_one_outbox()
            elif case == "unanswered":
                first.answers["sendMessage"] = [None, Interrupted()]
                with pytest.raises(Interrupted):
                    await rt.process_one_outbox()
                assert rt.store.outbox_payload(delivery)[CALL_STARTED]["part"] == 1
            receipt = rt.store.delivery_receipt(delivery)
            assert receipt["state"] == {"partial": "held", "unanswered": "leased"}.get(case, "pending")
            assert len(receipt.get("confirmed_messages", [])) == int(multipart)
            assert bool(receipt["history_reports"]) == multipart
            assert send(chat_id="55", text="shared", request_id="shared")["state"] == "queued"
            if multipart:
                assert rt.store.claim_outbox() is None  # same chat cannot overtake a held or leased pin in 0.12.2
            kept = row(rt, delivery)
        finally:
            current.api.unload()

        settings["token"] = "second"
        old = start("rollback")
        try:
            assert old.runtime.store.release_own_leases()["outbox"] == 0
            assert await old.runtime.process_one_outbox()  # only the row both versions understand
            assert not await old.runtime.process_one_outbox()
            assert old.runtime.store.delivery_receipt("telegram-send:shared")["state"] == "delivered"
            assert row(old.runtime, delivery) == kept
            assert [call["text"] for call in second.methods("sendMessage")] == ["shared"]
            assert not second.methods("sendDocument")
        finally:
            old.api.unload()

        restored = start()
        try:
            rt = restored.runtime
            assert row(rt, delivery) == kept
            assert rt.store.release_own_leases()["outbox"] == int(case == "unanswered")
            ready_again(rt.store)
            previous_calls = len(second.calls)
            assert await rt.process_one_outbox()
            if case == "unanswered":
                settled = rt.store.delivery_receipt(delivery)
                assert settled["state"] == "uncertain"
                assert settled["confirmed_messages"] == receipt["confirmed_messages"]
                assert len(second.calls) == previous_calls  # settle the open marker before any provider read
                assert len(first.methods("sendMessage")) == start_count + 2
            else:
                held = rt.store.delivery_receipt(delivery)
                assert held["state"] == "held" and held["attempts"] == receipt["attempts"]
                assert not second.methods("getChat")
                assert [call["text"] for call in second.methods("sendMessage")] == ["shared"]
                settings["token"] = "renewed"
                ready_again(rt.store)
                assert await rt.process_one_outbox()
                sent = rt.store.delivery_receipt(delivery)
                assert sent["state"] == "delivered"
                assert first.methods("getChat")[-1] == {"chat_id": "55"}
                if case == "file":
                    assert Path(sent["files"][0]["copy_path"]).read_bytes() == b"queued bytes"
                    [upload] = first.methods("sendDocument")
                    assert upload["chat_id"] == "55" and upload["_files"]["document"] == ("example.txt", len(b"queued bytes"))
                else:
                    calls = first.methods("sendMessage")[start_count:]
                    assert len(calls) == (2 if multipart else 1)
                    assert all(call["chat_id"] == "55" for call in calls)
                    assert "".join(call["text"] for call in calls) == text
            assert not await rt.process_one_outbox()
        finally:
            restored.api.unload()

    asyncio.run(run())
