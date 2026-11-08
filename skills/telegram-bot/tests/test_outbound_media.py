"""Outgoing video, audio, voice, animation and albums use telegram_send and the durable outbox (0.12.0)."""
import asyncio
import importlib.util
import json
import pathlib
import sys

import pytest

from telegram_bot.api import TelegramApiError
from telegram_bot.runtime import TelegramTransportRuntime

from fake_bot_api import Clock, FakeBotApi, use_clock
from test_plugin import FakeApi
from test_runtime import AcceptingSubmitter, Logger

SKILL = pathlib.Path(__file__).resolve().parents[1]
CHAT = "-1001234"


class ReportingSubmitter(AcceptingSubmitter):
    """A Host that takes /presence/delivery reports (presence_delivery_version 1)."""
    delivery_reporting_status = "supported"
    presence_explicit_completion = 0

    async def discover_delivery_support(self):
        return 1


def _plugin(tmp_path):
    spec = importlib.util.spec_from_file_location("telegram_bot_media_test", SKILL / "plugin.py",
                                                  submodule_search_locations=[str(SKILL)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    tools = {name: handler for name, handler, _ in api.tools}
    schemas = {name: metadata for name, _, metadata in api.tools}
    return tools, schemas


def _runtime(tmp_path, telegram):
    return TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                    submitter=ReportingSubmitter(), client_factory=telegram.client)


def _file(tmp_path, name, size=64):
    path = tmp_path / "out" / name
    path.parent.mkdir(exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)  # sparse: a size check needs no real bytes
    return str(path)


@pytest.mark.parametrize("kind,name,method", [
    ("video", "clip.mp4", "sendVideo"),
    ("audio", "song.mp3", "sendAudio"),
    ("voice", "note.ogg", "sendVoice"),
    ("animation", "cat.mp4", "sendAnimation"),
])
def test_each_media_kind_is_queued_sent_once_and_reported(tmp_path, kind, name, method):
    tools, schemas = _plugin(tmp_path)
    assert kind in schemas["telegram_send"]["schema"]["properties"]["kind"]["enum"]
    queued = tools["telegram_send"](chat_id=CHAT, kind=kind, file_path=_file(tmp_path, name), caption="**look**",
                                    reply_to_message_id="41", request_id=f"r-{kind}")
    assert queued == {"ok": True, "state": "queued", "request_id": f"r-{kind}", "chat_id": CHAT, "kind": kind}
    again = tools["telegram_send"](chat_id=CHAT, kind=kind, file_path=_file(tmp_path, name), request_id=f"r-{kind}")
    assert again["state"] == "already_queued"  # request_id deduplicates the same logical send
    telegram = FakeBotApi()
    runtime = _runtime(tmp_path, telegram)
    assert asyncio.run(runtime.process_one_outbox())
    [fields] = telegram.methods(method)
    assert fields["_files"] == {kind: (name, 64)} and fields["caption"] == "<b>look</b>"
    assert json.loads(fields["reply_parameters"]) == {"message_id": 41}
    receipt = runtime.store.delivery_receipt(f"telegram-send:r-{kind}")
    assert receipt["state"] == "delivered" and receipt["provider_receipt"]["kind"] == kind
    [report] = [entry["payload"] for entry in receipt["history_reports"]]
    assert (report["state"], report["part_id"], report["text"]) == ("delivered", "0", "<b>look</b>")
    assert (report["message"]["kind"], report["message"]["file_name"]) == (kind, name)
    assert kind in report["message"]  # Telegram's own media object of the sent message
    assert not asyncio.run(runtime.process_one_outbox())  # nothing is sent twice


def test_an_album_is_one_send_with_a_receipt_part_and_report_per_message(tmp_path):
    tools, schemas = _plugin(tmp_path)
    items_schema = schemas["telegram_send"]["schema"]["properties"]["items"]
    assert (items_schema["minItems"], items_schema["maxItems"]) == (2, 10)
    items = [{"kind": "photo", "file_path": _file(tmp_path, "a.jpg"), "caption": "first _look_"},
             {"kind": "video", "file_path": _file(tmp_path, "b.mp4")},
             {"kind": "photo", "file_path": _file(tmp_path, "c.jpg")}]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, topic_id="5", request_id="album-1")["ok"]
    telegram = FakeBotApi()
    runtime = _runtime(tmp_path, telegram)
    assert asyncio.run(runtime.process_one_outbox())
    [fields] = telegram.methods("sendMediaGroup")
    assert fields["message_thread_id"] == "5"
    assert json.loads(fields["media"]) == [
        {"type": "photo", "media": "attach://file0", "caption": "first <i>look</i>", "parse_mode": "HTML"},
        {"type": "video", "media": "attach://file1"}, {"type": "photo", "media": "attach://file2"}]
    assert fields["_files"] == {"file0": ("a.jpg", 64), "file1": ("b.mp4", 64), "file2": ("c.jpg", 64)}
    receipt = runtime.store.delivery_receipt("telegram-send:album-1")
    messages = receipt["provider_receipt"]["messages"]
    assert receipt["state"] == "delivered" and receipt["provider_receipt"]["kind"] == "album" and len(messages) == 3
    reports = [entry["payload"] for entry in receipt["history_reports"]]
    assert [(r["part_id"], r["state"], r["message"]["kind"], r["message"]["file_name"]) for r in reports] == [
        ("0", "delivered", "photo", "a.jpg"), ("1", "delivered", "video", "b.mp4"), ("2", "delivered", "photo", "c.jpg")]
    assert [r["message"]["message_id"] for r in reports] == [m["message_id"] for m in messages]
    assert reports[0]["text"] == "first <i>look</i>" and reports[1]["text"] == ""
    assert len({r["message"]["media_group_id"] for r in reports}) == 1
    for message in messages:  # each sent member is this bot's own retained message, for later reactions
        assert runtime.store.reaction_target("9", CHAT, message["message_id"])["retained"] is True


def test_an_album_caption_falls_back_to_plain_text_only_on_telegrams_400(tmp_path):
    tools, _ = _plugin(tmp_path)
    items = [{"kind": "document", "file_path": _file(tmp_path, "a.pdf"), "caption": "**report**"},
             {"kind": "document", "file_path": _file(tmp_path, "b.pdf")}]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, request_id="docs")["ok"]
    telegram = FakeBotApi()
    telegram.answers["sendMediaGroup"] = [{"ok": False, "error_code": 400,
                                           "description": "Bad Request: can't parse entities"}]
    runtime = _runtime(tmp_path, telegram)
    assert asyncio.run(runtime.process_one_outbox())
    first, second = telegram.methods("sendMediaGroup")
    assert json.loads(first["media"])[0]["parse_mode"] == "HTML"
    assert json.loads(second["media"])[0] == {"type": "document", "media": "attach://file0", "caption": "report"}
    assert runtime.store.delivery_receipt("telegram-send:docs")["state"] == "delivered"


class Crash(BaseException):
    """The process ends here: nothing in the runtime catches it."""


@pytest.mark.parametrize("crash", [False, True], ids=["finished", "crashed_before_finishing"])
@pytest.mark.parametrize("answer", ["one_of_two", "not_a_list", "three_for_two"])
def test_an_accepted_album_is_sent_once_across_restarts_and_ends_uncertain(tmp_path, monkeypatch, answer, crash):
    clock = Clock()
    use_clock(monkeypatch, clock)
    tools, _ = _plugin(tmp_path)
    items = [{"kind": "photo", "file_path": _file(tmp_path, "a.jpg"), "caption": "first"},
             {"kind": "photo", "file_path": _file(tmp_path, "b.jpg")}]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, request_id="short")["ok"]
    telegram = FakeBotApi()
    one = {"message_id": 777, "date": 1_800_000_000, "chat": {"id": int(CHAT), "type": "supergroup"},
           "media_group_id": "g1", "photo": [{"file_id": "p"}], "caption": "first"}
    returned_result = {"one_of_two": [one], "not_a_list": True,
                       "three_for_two": [one, {**one, "message_id": 778}, {**one, "message_id": 779}]}[answer]
    telegram.answers["sendMediaGroup"] = [{"ok": True, "result": returned_result}]
    first = _runtime(tmp_path, telegram)
    if crash:  # the process ends after Telegram's answer was kept and before the row was finished
        first.store.mark_outbox_failed = lambda *_args, **_kwargs: (_ for _ in ()).throw(Crash())
        with pytest.raises(Crash):
            asyncio.run(first.process_one_outbox())
        del first.store.mark_outbox_failed
    else:
        assert asyncio.run(first.process_one_outbox())
    for _ in range(3):  # restarts, well after any retry delay; Telegram would now answer with both messages
        clock.advance(600)
        later = _runtime(tmp_path, telegram)
        later.store.release_own_leases()  # as run() does first
        while asyncio.run(later.process_one_outbox()):
            pass
    assert len(telegram.methods("sendMediaGroup")) == 1  # Telegram's yes is never asked for again
    receipt = later.store.delivery_receipt("telegram-send:short")
    returned = {"one_of_two": 1, "not_a_list": 0, "three_for_two": 3}[answer]
    assert receipt["state"] == "uncertain" and f"returned {returned} messages for 2 items" in receipt["error"]
    reports = [entry["payload"] for entry in receipt["history_reports"]]
    assert [(r["part_id"], r["state"]) for r in reports] == [
        (str(index), "delivered") for index in range(returned)] + [("status", "uncertain")]
    assert reports[-1]["message"]["confirmed_parts"] == returned and "2 items" in reports[-1]["message"]["error"]
    if returned:  # the message Telegram did return is this bot's own, with its own receipt part
        assert reports[0]["message"]["message_id"] == 777
        assert later.store.reaction_target("9", CHAT, 777)["retained"] is True
    if returned == 3:
        assert reports[2]["message"]["message_id"] == 779
        assert "file_name" not in reports[2]["message"]  # no invented third source file
        assert later.store.reaction_target("9", CHAT, 779)["retained"] is True


@pytest.mark.parametrize("answer", ["one_of_two", "three_for_two", "not_a_list"])
def test_without_host_history_the_local_receipt_names_what_an_accepted_album_returned(tmp_path, monkeypatch, answer):
    """The local receipt is all a Host without delivery history has: it lists the messages Telegram returned while the
    row stays uncertain, read from the row it already loads. A complete album's receipt is unchanged."""
    use_clock(monkeypatch, Clock())
    tools, _ = _plugin(tmp_path)
    items = [{"kind": "photo", "file_path": _file(tmp_path, "a.jpg"), "caption": "first"},
             {"kind": "photo", "file_path": _file(tmp_path, "b.jpg")}]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, request_id="short")["ok"]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, request_id="whole")["ok"]
    telegram = FakeBotApi()
    one = {"message_id": 777, "date": 1_800_000_000, "chat": {"id": int(CHAT), "type": "supergroup"},
           "media_group_id": "g1", "photo": [{"file_id": "p"}], "caption": "first"}
    returned = {"one_of_two": [one], "not_a_list": True,
                "three_for_two": [one, {**one, "message_id": 778}, {**one, "message_id": 779}]}[answer]
    telegram.answers["sendMediaGroup"] = [{"ok": True, "result": returned}]
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                       submitter=AcceptingSubmitter(), client_factory=telegram.client)
    assert asyncio.run(runtime.process_one_outbox()) and asyncio.run(runtime.process_one_outbox())
    statements = []
    with runtime.store._connect() as conn:
        conn.set_trace_callback(statements.append)
    receipt = runtime.store.delivery_receipt("telegram-send:short")
    with runtime.store._connect() as conn:
        conn.set_trace_callback(None)
    assert len(statements) == 1  # the row it reads anyway; nothing else is scanned
    assert receipt["state"] == "uncertain" and receipt["history_reports"] == [] and receipt["provider_receipt"] == {}
    assert receipt["confirmed_messages"] == (returned if isinstance(returned, list) else [])
    whole = runtime.store.delivery_receipt("telegram-send:whole")
    assert whole["state"] == "delivered" and len(whole["provider_receipt"]["messages"]) == 2
    assert "confirmed_messages" not in whole


def test_an_album_telegram_did_not_accept_is_still_retried(tmp_path, monkeypatch):
    clock = Clock()
    use_clock(monkeypatch, clock)
    tools, _ = _plugin(tmp_path)
    items = [{"kind": "photo", "file_path": _file(tmp_path, "a.jpg")}, {"kind": "photo", "file_path": _file(tmp_path, "b.jpg")}]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, request_id="retry")["ok"]
    telegram = FakeBotApi()
    telegram.answers["sendMediaGroup"] = [{"ok": False, "error_code": 502, "description": "Bad Gateway"},
                                          TelegramApiError("sendMediaGroup", "ConnectionResetError")]
    runtime = _runtime(tmp_path, telegram)
    for _ in range(3):
        asyncio.run(runtime.process_one_outbox())
        clock.advance(600)
    assert len(telegram.methods("sendMediaGroup")) == 3  # a refusal and a lost answer retry as before; the third passed
    assert runtime.store.delivery_receipt("telegram-send:retry")["state"] == "delivered"


class TopicPolicy(ReportingSubmitter):
    """Places as Ouroboros answers ``place_hold``: the chat is Active and its forum topic 5 has its own Paused place."""

    def __init__(self):
        super().__init__()
        self.asked = []

    async def place_hold(self, account_id, conversation_id, thread_id=""):
        self.asked.append(thread_id)
        return "Held: Topic 5 is paused" if thread_id == "5" else ""


@pytest.mark.parametrize("kind", ["message", "photo", "album", "edit", "reply"])
def test_a_row_queued_for_a_paused_topic_is_held_while_other_topics_and_the_chat_send(tmp_path, kind):
    tools, _ = _plugin(tmp_path)
    send = tools["telegram_send"]
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                       submitter=(host := TopicPolicy()), client_factory=(telegram := FakeBotApi()).client)
    held = "telegram-send:five"
    if kind == "message":
        assert send(chat_id=CHAT, text="for five", topic_id="5", request_id="five")["ok"]
    elif kind == "photo":
        assert send(chat_id=CHAT, kind="photo", file_path=_file(tmp_path, "a.jpg"), topic_id="5", request_id="five")["ok"]
    elif kind == "album":
        items = [{"kind": "photo", "file_path": _file(tmp_path, "a.jpg")},
                 {"kind": "video", "file_path": _file(tmp_path, "b.mp4")}]
        assert send(chat_id=CHAT, kind="album", items=items, topic_id="5", request_id="five")["ok"]
    elif kind == "edit":  # an edit is words people read (0.11.2 sends); the tool keeps the topic as an integer
        assert tools["telegram_operation"](method="editMessageText", chat_id=CHAT, message_id=41, text="fixed",
                                           topic_id=5, request_id="five")["ok"]
        held = "telegram-operation:five"
    else:  # the turn's own reply to a message in topic 5
        from telegram_bot.events import parse_telegram_update
        event = parse_telegram_update({"update_id": 3, "message": {
            "message_id": 30, "date": 1_700_000_000, "message_thread_id": 5, "is_topic_message": True, "text": "hi",
            "chat": {"id": int(CHAT), "type": "supergroup", "title": "Lab", "is_forum": True},
            "from": {"id": 3, "is_bot": False, "first_name": "Ann"}}}, bot_account_id="9", places=True)
        runtime.store.commit_update(3, event)
        lease = runtime.store.claim_inbox()
        runtime.store.record_submission(lease.event_id, binding_id="", turn_ref="t", outcome="message",
                                        text="for five", work_ref="")
        held = f"presence:{lease.event_id}:turn"
    assert send(chat_id=CHAT, text="for seven", topic_id="7", request_id="seven")["ok"]
    assert send(chat_id=CHAT, text="for the chat", request_id="chat")["ok"]
    while asyncio.run(runtime.process_one_outbox()):
        pass
    # Only the two rows outside topic 5 reached Telegram; the held row made no Bot API call and spent no attempt.
    assert [(m, f.get("message_thread_id")) for m, f in telegram.calls if m != "getMe" and m != "deleteWebhook"] == [
        ("sendMessage", 7), ("sendMessage", None)]
    receipt = runtime.store.delivery_receipt(held)
    assert (receipt["state"], receipt["error"], receipt["attempts"]) == ("held", "Held: Topic 5 is paused", 0)
    assert [runtime.store.delivery_receipt(f"telegram-send:{name}")["state"] for name in ("seven", "chat")] == [
        "delivered", "delivered"]
    assert host.asked == ["5", "7", ""]  # each row asked about its own topic


MiB = 1024 * 1024


@pytest.mark.parametrize("arguments,code", [
    ({"kind": "video", "file": ("big.mp4", 50 * MiB + 1)}, "file_too_large"),
    ({"kind": "photo", "file": ("big.jpg", 10 * MiB + 1)}, "file_too_large"),
    ({"kind": "voice", "file": ("memo.wav", 100)}, "voice_format"),
    ({"kind": "audio", "file": ("song.mp3", 100), "caption": "x" * 1100}, "caption_too_long"),
    ({"kind": "album", "items": [("photo", "a.jpg")]}, "album_size"),
    ({"kind": "album", "items": [("photo", f"{n}.jpg") for n in range(11)]}, "album_size"),
    ({"kind": "album", "items": [("audio", "a.mp3"), ("photo", "b.jpg")]}, "album_mixed"),
    ({"kind": "album", "items": [("document", "a.pdf"), ("audio", "b.mp3")]}, "album_mixed"),
    ({"kind": "album", "items": [("photo", "a.jpg"), ("animation", "b.mp4")]}, "album_kind"),
    ({"kind": "album", "items": [("photo", "a.jpg", "one"), ("photo", "b.jpg")], "caption": "two"}, "album_caption"),
    ({"kind": "album", "items": [("photo", "a.jpg"), ("video", None)]}, "file_missing"),
])
def test_telegram_limits_are_typed_refusals_and_nothing_is_queued(tmp_path, arguments, code):
    tools, _ = _plugin(tmp_path)
    call = {"chat_id": CHAT, "kind": arguments["kind"], "request_id": "refused", "caption": arguments.get("caption", "")}
    if "file" in arguments:
        call["file_path"] = _file(tmp_path, *arguments["file"])
    if "items" in arguments:
        call["items"] = [{"kind": item[0], "file_path": _file(tmp_path, item[1]) if item[1] else str(tmp_path / "gone"),
                          **({"caption": item[2]} if len(item) > 2 else {})} for item in arguments["items"]]
    answer = tools["telegram_send"](**call)
    assert answer["ok"] is False and answer["code"] == code and answer["error"]
    if code == "file_too_large":
        assert answer["size_bytes"] > answer["limit_bytes"] in (10 * MiB, 50 * MiB)
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=Logger(),
                                       submitter=AcceptingSubmitter())
    assert runtime.store.status_snapshot()["outbox_waiting"] == 0
    assert runtime.store.delivery_receipt("telegram-send:refused") == {"state": "not_found"}
