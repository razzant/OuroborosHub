"""telegram_typing: "typing…" the model asks for in its turn's own chat; never speech, custody or a receipt (0.12.0).

The Host here advertises nothing beyond completed turns, like an older core: no capability is needed.
"""
import asyncio
import contextlib
import sqlite3
from types import SimpleNamespace

import pytest

from telegram_bot.events import parse_telegram_update
from telegram_bot.runtime import TelegramTransportRuntime
from telegram_bot.tools import make_typing_tool

from fake_bot_api import Clock, FakeBotApi, use_clock
from test_runtime import AcceptingSubmitter, Logger

PRIVATE = {"id": 4001, "type": "private", "first_name": "Ann"}
FORUM = {"id": -100500, "type": "supergroup", "title": "Lab", "is_forum": True}


def _setup(tmp_path, monkeypatch, submitter=None, api=None):
    clock = Clock()
    use_clock(monkeypatch, clock)
    api = api or FakeBotApi()
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                       submitter=submitter or AcceptingSubmitter(), client_factory=api.client)
    tool = make_typing_tool(SimpleNamespace(get_state_dir=lambda: str(tmp_path)))
    return clock, api, runtime, tool


def _turn(runtime, update_id=7, chat=PRIVATE, **message):
    """A turn whose /presence/turn request is open: its event is leased, as while the Host answers."""
    update = {"update_id": update_id, "message": {"message_id": 70 + update_id, "date": 1_700_000_000, "chat": dict(chat),
                                                  "from": {"id": 3, "is_bot": False, "first_name": "Ann"},
                                                  "text": "hello", **message}}
    event = parse_telegram_update(update, bot_account_id="9", places=True)
    runtime.store.commit_update(update_id, event)
    return SimpleNamespace(task_id=f"turn-{update_id}", task_metadata={"presence": {"event": event.to_dict()}}), event


def _typing(api):
    return api.methods("sendChatAction")


def _show(runtime):
    return asyncio.run(runtime.process_typing())


class PlaceOnRecord(AcceptingSubmitter):
    """Ouroboros's current place policy as ``PresenceHostClient.place_hold`` answers it; ``meanwhile`` runs once
    while it is being read, as anything else may while the runtime awaits the Host."""

    def __init__(self):
        super().__init__()
        self.state, self.reads, self.meanwhile = "active", 0, None

    async def place_hold(self, account_id, conversation_id, thread_id=""):
        self.reads += 1
        act, self.meanwhile = self.meanwhile, None
        if act is not None:
            await act()
        return "" if self.state == "active" else f"Held: Telegram is {self.state}"


def test_typing_is_requested_then_shown_renewed_and_ended_at_the_first_delivered_message(tmp_path, monkeypatch):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    ctx, _ = _turn(runtime)
    assert runtime.store.claim_inbox() is not None
    assert _show(runtime) == 0 and _typing(api) == []  # a turn that does not ask shows nothing
    answer = tool(ctx, chat_id="4001")
    # A request, not a showing: Telegram has not been asked yet, and nothing reads as a queued or delivered send.
    assert (answer["ok"], answer["typing"], answer["chat_id"]) == (True, "requested", "4001") and _typing(api) == []
    assert set(answer) == {"ok", "typing", "chat_id", "note"} and "nothing was sent" in answer["note"].lower()
    assert _show(runtime) == 1 and _typing(api) == [{"chat_id": "4001", "action": "typing"}]
    clock.advance(2)
    assert _show(runtime) == 0
    clock.advance(3)
    assert _show(runtime) == 1 and len(_typing(api)) == 2  # renewed before Telegram's 5 s run out
    runtime.store.enqueue_outbox("telegram-send:ack", {"kind": "message", "chat_id": "4001", "text": "On it"})
    assert asyncio.run(runtime.process_one_outbox())
    clock.advance(5)
    assert _show(runtime) == 0 and runtime.store.typing_requests() == []  # the first message ended it
    # Not speech: no outbox row, receipt or report of its own; only the real message was delivered.
    snapshot = runtime.store.status_snapshot()
    assert (snapshot["outbox_delivered"], snapshot["outbox_waiting"], snapshot["delivery_reports_pending"]) == (1, 0, 0)
    assert tool(ctx, chat_id="4001")["ok"] and _show(runtime) == 1  # asked again after an intermediate message


def test_a_message_queued_before_the_request_ends_it_when_delivered_as_the_note_says(tmp_path, monkeypatch):
    """The first message of this bot delivered after the request ends it, even an ack queued in the same step before
    it: then no "typing…" shows, and the note says so. Asked after the ack was delivered, it shows."""
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    ctx, _ = _turn(runtime)
    runtime.store.claim_inbox()
    runtime.store.enqueue_outbox("telegram-send:ack", {"kind": "message", "chat_id": "4001", "text": "On it"})
    answer = tool(ctx, chat_id="4001")
    assert answer["ok"] and "queued" in answer["note"]
    clock.advance(0.5)
    assert asyncio.run(runtime.process_one_outbox())  # the outbox delivers the ack queued before the request
    assert _show(runtime) == 0 and _typing(api) == [] and runtime.store.typing_requests() == []
    clock.advance(1)
    assert tool(ctx, chat_id="4001")["ok"] and _show(runtime) == 1 and len(_typing(api)) == 1


def test_typing_ends_with_its_turn(tmp_path, monkeypatch):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    ctx, event = _turn(runtime)
    lease = runtime.store.claim_inbox()
    assert tool(ctx, chat_id="4001")["ok"] and _show(runtime) == 1
    runtime.store.record_submission(lease.event_id, binding_id="", turn_ref="turn-7", outcome="silent", text="",
                                    work_ref="")  # the turn finished silently
    clock.advance(5)
    assert _show(runtime) == 0 and runtime.store.typing_requests() == [] and len(_typing(api)) == 1
    refused = tool(ctx, chat_id="4001")
    assert refused["ok"] is False and refused["code"] == "turn_ended"


def test_a_version_2_continuation_keeps_typing_until_its_work_settles(tmp_path, monkeypatch):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    ctx, _ = _turn(runtime)
    lease = runtime.store.claim_inbox()
    runtime.store.record_submission(lease.event_id, binding_id="", turn_ref="turn-7", outcome="silent", text="",
                                    work_ref="", continuation_ref="turn-7")  # custody answer: the author runs on
    assert tool(ctx, chat_id="4001")["ok"] and _show(runtime) == 1
    work = runtime.store.claim_work()
    runtime.store.complete_work(work.work_ref, status="completed", text="")
    clock.advance(5)
    assert _show(runtime) == 0 and runtime.store.typing_requests() == []


def test_typing_stops_after_two_minutes(tmp_path, monkeypatch):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    ctx, _ = _turn(runtime)
    runtime.store.claim_inbox()
    assert tool(ctx, chat_id="4001")["ok"]
    for _ in range(40):  # 200 s of a long turn
        _show(runtime)
        clock.advance(5)
    assert len(_typing(api)) == 25 and runtime.store.typing_requests() == []  # 0..120 s, then nothing


def test_a_429_ends_typing_and_is_never_retried(tmp_path, monkeypatch):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    api.answers["sendChatAction"] = [{"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 9",
                                      "parameters": {"retry_after": 9}}]
    ctx, _ = _turn(runtime)
    runtime.store.claim_inbox()
    assert tool(ctx, chat_id="4001")["ok"]
    assert _show(runtime) == 0
    for _ in range(5):
        clock.advance(10)
        assert _show(runtime) == 0
    assert len(_typing(api)) == 1 and runtime.store.typing_requests() == []


def test_several_turns_of_one_chat_share_one_renewal(tmp_path, monkeypatch):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    first, _ = _turn(runtime, 7)
    second, _ = _turn(runtime, 8)
    runtime.store.claim_inbox()
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:  # turn-8 runs as a continuation still observed
        conn.execute("INSERT INTO presence_work(work_ref, event_id, binding_id, role, event_json, state, attempts, "
                     "available_at, created_at) VALUES ('turn-8', 'telegram:9:8', '', 'deferred', '{}', 'pending', 0, 0, 0)")
    assert tool(first, chat_id="4001")["ok"] and tool(second, chat_id="4001")["ok"]
    assert _show(runtime) == 1
    clock.advance(5)
    assert _show(runtime) == 1 and len(_typing(api)) == 2 and len(runtime.store.typing_requests()) == 2


def test_only_a_forum_topic_is_a_thread_never_a_reply_chain(tmp_path, monkeypatch):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    topic, _ = _turn(runtime, 7, FORUM, message_thread_id=77, is_topic_message=True)
    runtime.store.claim_inbox()
    assert tool(topic, chat_id="-100500", topic_id=77)["ok"] and _show(runtime) == 1
    assert _typing(api)[-1] == {"chat_id": "-100500", "action": "typing", "message_thread_id": 77}

    group = {"id": -100600, "type": "supergroup", "title": "Plain"}
    reply, event = _turn(runtime, 8, group, message_thread_id=55)  # a reply chain, not a topic
    assert event.thread_id == "" and event.message["reply_chain_id"] == 55
    runtime.store.claim_inbox()
    refused = tool(reply, chat_id="-100600", topic_id=55)
    assert refused["ok"] is False and refused["code"] == "other_conversation"
    assert tool(reply, chat_id="-100600")["ok"] and _show(runtime) == 1
    assert _typing(api)[-1] == {"chat_id": "-100600", "action": "typing"}


@pytest.mark.parametrize("ctx,chat,code", [
    (None, "4001", "no_turn"),
    (SimpleNamespace(task_id="t", task_metadata={}), "4001", "no_turn"),
    ("own", "4002", "other_conversation"),
])
def test_typing_shows_only_in_a_live_telegram_turns_own_chat(tmp_path, monkeypatch, ctx, chat, code):
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch)
    own, _ = _turn(runtime)
    runtime.store.claim_inbox()
    answer = tool(own if ctx == "own" else ctx, chat_id=chat)
    assert answer["ok"] is False and answer["code"] == code and "Nothing was shown" in answer["error"]
    assert runtime.store.typing_requests() == [] and _show(runtime) == 0


def test_a_place_that_may_not_act_shows_nothing(tmp_path, monkeypatch):
    class SilentPlace(AcceptingSubmitter):
        async def place_hold(self, account_id, conversation_id, thread_id=""):
            return "Held: Room is silent"

    clock, api, runtime, tool = _setup(tmp_path, monkeypatch, SilentPlace())
    ctx, _ = _turn(runtime)
    runtime.store.claim_inbox()
    assert tool(ctx, chat_id="4001")["ok"]
    assert _show(runtime) == 0 and _typing(api) == [] and runtime.store.typing_requests() == []


@pytest.mark.parametrize("state", ["silent", "paused"])
def test_the_place_is_read_before_every_showing_so_a_later_change_stops_it(tmp_path, monkeypatch, state):
    host = PlaceOnRecord()
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch, host)
    ctx, _ = _turn(runtime)
    runtime.store.claim_inbox()
    assert tool(ctx, chat_id="4001")["ok"] and _show(runtime) == 1
    clock.advance(5)
    assert _show(runtime) == 1 and host.reads == 2  # the renewal read the place again
    host.state = state  # made Silent or Paused while the turn still prepares its reply
    clock.advance(5)
    assert _show(runtime) == 0 and host.reads == 3 and len(_typing(api)) == 2
    assert runtime.store.typing_requests() == []  # ended, not waiting for the place to return
    host.state = "active"
    clock.advance(5)
    assert _show(runtime) == 0 and len(_typing(api)) == 2


@pytest.mark.parametrize("at", ["first", "renewal"])
@pytest.mark.parametrize("meanwhile", ["reply", "turn_end", "cap"])
def test_an_end_met_while_the_place_is_read_sends_nothing(tmp_path, monkeypatch, meanwhile, at):
    host = PlaceOnRecord()
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch, host)
    ctx, _ = _turn(runtime)
    lease = runtime.store.claim_inbox()
    assert tool(ctx, chat_id="4001")["ok"]
    if at == "renewal":
        assert _show(runtime) == 1
        clock.advance(5)

    async def reply():  # this bot's message reaches the chat through the outbox
        runtime.store.enqueue_outbox("telegram-send:meanwhile", {"kind": "message", "chat_id": "4001", "text": "Done"})
        assert await runtime.process_one_outbox()

    async def turn_end():
        runtime.store.record_submission(lease.event_id, binding_id="", turn_ref="turn-7", outcome="silent", text="",
                                        work_ref="")

    async def cap():
        clock.advance(121)

    host.meanwhile = {"reply": reply, "turn_end": turn_end, "cap": cap}[meanwhile]
    assert _show(runtime) == 0
    assert len(_typing(api)) == (1 if at == "renewal" else 0) and runtime.store.typing_requests() == []
    assert host.meanwhile is None  # it did run during the read


def test_a_restart_never_replays_typing(tmp_path, monkeypatch):
    class StopAtFirstPoll(FakeBotApi):
        async def post_json(self, url, payload, timeout_sec):
            if url.endswith("/getUpdates"):
                runtime.stop()
            return await super().post_json(url, payload, timeout_sec)

    api = StopAtFirstPoll()
    clock, api, runtime, tool = _setup(tmp_path, monkeypatch, api=api)
    ctx, _ = _turn(runtime)
    runtime.store.claim_inbox()
    assert tool(ctx, chat_id="4001")["ok"] and len(runtime.store.typing_requests()) == 1
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(runtime.run())  # a new runtime starts: earlier requests are gone before any worker runs
    assert runtime.store.typing_requests() == [] and _typing(api) == []
