"""Reactions through the actual parser, custody, runtime workers, Bot API client and Host client.

Telegram and Host are fakes; nothing leaves the process. When an ``ouroboros`` core carrying the
reaction validators is importable, produced facts also pass core's own ingress and delivery
validators; otherwise those checks are skipped, never counted.
"""
import asyncio
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from telegram_bot.api import TelegramApiError, TelegramClient
from telegram_bot.custody import REACTION_WITHHELD, CustodyStore
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostClient, PresenceHostError, PresenceSubmission, PresenceWorkResult
from telegram_bot.runtime import TelegramTransportRuntime
from telegram_bot.tools import make_operation_tool
from test_formatted_delivery import Provider, ready_again, runtime

CAPABLE = {"presence_delivery_version": 1, "places_version": 1, "presence_reaction_context": 1,
           "presence_explicit_completion": 1}
OLD_HOST = {"presence_delivery_version": 1, "places_version": 1}
BOT = "9"
FORUM = {"id": -100, "type": "supergroup", "title": "Forum", "is_forum": True}
GROUP = {"id": -200, "type": "supergroup", "title": "Plain"}
PRIVATE = {"id": 55, "type": "private", "first_name": "Ann"}
ANN = {"id": 55, "first_name": "Ann", "last_name": "Lee", "username": "ann"}
THUMB, HEART = {"type": "emoji", "emoji": "👍"}, {"type": "emoji", "emoji": "❤"}


def reacted(update_id=30, *, chat=GROUP, message_id=7, user=ANN, actor_chat=None, old=(), new=(THUMB,), date=1700000000):
    body = {"chat": chat, "message_id": message_id, "date": date,
            "old_reaction": list(old), "new_reaction": list(new)}
    if user is not None:
        body["user"] = user
    if actor_chat is not None:
        body["actor_chat"] = actor_chat
    return {"update_id": update_id, "message_reaction": body}


def counted(update_id=31, *, chat=GROUP, message_id=7, reactions=None, date=1700000001):
    return {"update_id": update_id, "message_reaction_count": {
        "chat": chat, "message_id": message_id, "date": date,
        "reactions": [{"type": THUMB, "total_count": 3}] if reactions is None else reactions}}


def said(update_id=20, *, chat=GROUP, message_id=7, text="the words", topic=None, sender=ANN):
    return {"update_id": update_id, "message": {
        "message_id": message_id, "date": 1, "chat": chat, "from": sender, "text": text,
        **({"message_thread_id": topic, "is_topic_message": True} if topic else {})}}


def parse(update, *, account=BOT, places=True):
    return parse_telegram_update(update, bot_account_id=account, places=places)


def commit(store, update, *, account=BOT):
    event = parse(update, account=account)
    store.commit_update(update["update_id"], event)
    return event


def stored(store, update_id):
    with sqlite3.connect(store.path) as db:
        return json.loads(db.execute("SELECT payload_json FROM inbox WHERE update_id=?", (update_id,)).fetchone()[0])


def valid_ingress(event):
    try:
        from ouroboros.place_intake import reaction_refusal
    except (ImportError, AttributeError):
        return
    assert reaction_refusal(event, []) == ""


def valid_receipt(report):
    try:
        from ouroboros.presence_delivery import validate_delivery
    except ImportError:
        return
    assert validate_delivery(report)


class Host:
    delivery_reporting_status = "unknown"

    def __init__(self, identity=CAPABLE):
        self.identity, self.turns, self.reports = identity, [], []
        self.delivery_reporting_version = 0
        self.presence_reaction_context = self.presence_explicit_completion = 0
        self.places_version = 1
        self.place_checks, self.thread_holds = [], {}

    async def discover_delivery_support(self):
        if self.identity is None:
            self.delivery_reporting_status = "unavailable"
            self.presence_reaction_context = self.presence_explicit_completion = 0
            return 0
        self.delivery_reporting_version = int(self.identity.get("presence_delivery_version") == 1)
        self.delivery_reporting_status = "supported" if self.delivery_reporting_version else "unsupported"
        for name in ("presence_reaction_context", "presence_explicit_completion"):
            value = self.identity.get(name)
            setattr(self, name, int(type(value) is int and value == 1))
        return self.delivery_reporting_version

    async def submit(self, event, staged):
        self.turns.append(json.loads(json.dumps(event)))
        return PresenceSubmission(status="completed", outcome="silent", text="", turn_ref="t", work_ref="", binding_id="")

    async def poll(self, work_ref, binding_id):
        return PresenceWorkResult(status="completed", outcome="silent", work_ref=work_ref)

    async def report_delivery(self, payload):
        self.reports.append(json.loads(json.dumps(payload)))

    async def place_hold(self, account_id, conversation_id, thread_id=""):
        self.place_checks.append((account_id, conversation_id, thread_id))
        return self.thread_holds.get(thread_id, getattr(self, "hold", ""))


def world(path, provider=None, identity=CAPABLE):
    value = runtime(path, provider or Provider())
    value.submitter, value._bot_id = Host(identity), BOT
    return value


def tool(path, account=BOT):
    return make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(path)), lambda: account)


async def drain_reports(value):
    while await value.process_one_delivery_report():
        pass


# -- Parsing ---------------------------------------------------------------------------------

@pytest.mark.parametrize("old,new,change,fields", [
    ((), (THUMB,), "add", {"new": [{"type": "emoji", "value": "👍"}]}),
    ((THUMB,), (), "remove", {"old": [{"type": "emoji", "value": "👍"}]}),
    ((THUMB,), (HEART,), "replace", {"old": [{"type": "emoji", "value": "👍"}], "new": [{"type": "emoji", "value": "❤"}]}),
    ((), ({"type": "custom_emoji", "custom_emoji_id": "cu5"},), "add", {"new": [{"type": "custom_emoji", "value": "cu5"}]}),
    ((), ({"type": "paid"},), "add", {"new": [{"type": "paid", "value": ""}]}),
])
def test_individual_change_keeps_the_raw_snapshots_and_invents_no_words(old, new, change, fields):
    event = parse(reacted(old=old, new=new))
    fact = event.message["reaction"]
    assert fact["change"] == change and fact["schema_version"] == 1
    assert {key: fact[key] for key in ("old", "new") if key in fact} == fields
    assert event.message["old_reaction"] == list(old) and event.message["new_reaction"] == list(new)
    assert event.text == "" and "attachments" not in event.message
    assert event.actor == {"kind": "user", "platform": "telegram", "platform_actor_id": "55", "username": "ann",
                           "first_name": "Ann", "last_name": "Lee", "is_bot": False}
    assert event.message["sent_at"] == "2023-11-14T22:13:20+00:00" and event.message["sent_at_epoch"] == 1700000000
    assert event.facts == []
    valid_ingress(event.to_dict())


def test_unchanged_snapshot_is_not_an_event():
    assert parse(reacted(old=(THUMB, HEART), new=(HEART, THUMB))) is None
    assert parse(reacted(old=(), new=())) is None


def test_chat_acting_as_itself_is_no_person_and_no_owner_candidate():
    event = parse(reacted(user=None, actor_chat={"id": -77, "title": "News", "username": "news", "type": "channel"}))
    assert event.actor == {"kind": "chat", "platform": "telegram", "platform_actor_id": "-77", "display_name": "News",
                           "username": "news", "authenticated": False}
    valid_ingress(event.to_dict())


def test_anonymous_counts_name_nobody():
    event = parse(counted())
    assert event.actor == {"kind": "anonymous", "platform": "telegram", "authenticated": False}
    assert event.message["reaction"]["change"] == "count"
    assert event.message["reaction"]["counts"] == [{"type": "emoji", "value": "👍", "count": 3}]
    assert "old" not in event.message["reaction"] and "new" not in event.message["reaction"]
    assert event.message["event_kind"] == "message_reaction_count"
    assert event.message["reaction_counts"][0]["total_count"] == 3 and event.facts == []
    valid_ingress(event.to_dict())


def test_empty_or_malformed_counts_stay_honest():
    assert parse(counted(reactions=[])).message["reaction"]["counts"] == []
    bad = parse(counted(reactions=[{"type": THUMB, "total_count": -1}, {"type": THUMB, "total_count": True}, "x"]))
    assert bad.message["reaction"]["counts"] == [] and len(bad.message["reaction_counts"]) == 3
    assert parse(counted()) is not None and parse({"update_id": 1, "message_reaction_count": {"chat": GROUP}}) is None


def test_other_bot_keeps_from_bot_and_own_id_is_dropped():
    event = parse(reacted(user={"id": 66, "is_bot": True, "first_name": "B"}))
    assert event.facts == ["from_bot"] and event.actor["is_bot"] is True
    assert parse(reacted(user={"id": 9, "is_bot": True})) is None


@pytest.mark.parametrize("chat,resolution", [
    (PRIVATE, "resolved"), (GROUP, "resolved"), ({"id": -3, "type": "group"}, "resolved"),
    ({"id": -4, "type": "channel"}, "resolved"), (FORUM, "unresolved"),
])
def test_only_a_forum_leaves_the_conversation_unproven_without_a_record(chat, resolution):
    event = parse(reacted(chat=chat))
    assert event.message["reaction"]["target"] == {"message_id": "7", "resolution": resolution}
    assert event.thread_id == "" and event.conversation_key == f"telegram:9:{chat['id']}:0"
    assert event.conversation.get("is_forum", False) is (chat is FORUM)


def test_count_update_shows_the_group_like_any_other_update():
    from telegram_bot.discovery import group_sightings
    [seen] = group_sightings(counted(), BOT)
    assert seen.chat_id == "-200" and seen.membership == "present"


def test_old_host_parse_path_still_filters_bots():
    assert parse(reacted(user={"id": 66, "is_bot": True}), places=False) is None
    assert "is_bot" not in parse(reacted(), places=False).actor


# -- Exact targets ---------------------------------------------------------------------------

def test_retained_target_adds_author_and_excerpt_and_labels_a_cut(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    words = "слово " * 200
    commit(store, said(text=words))
    commit(store, reacted())
    event = stored(store, 30)
    target = event["message"]["reaction"]["target"]
    assert target["resolution"] == "resolved"
    assert target["author"] == {"platform_actor_id": "55", "display_name": "Ann Lee", "is_self": False}
    assert target["excerpt"] == {"text": words[:500], "truncated": True}
    assert event["message"]["target_lookup"]["text"] == words
    assert event["text"] == "" and event["thread_id"] == ""
    valid_ingress(event)


def test_forum_topic_comes_only_from_the_exact_retained_message(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=FORUM, topic=5, message_id=7))
    commit(store, said(21, chat=FORUM, message_id=8, text="general"))
    commit(store, reacted(30, chat=FORUM, message_id=7))
    commit(store, reacted(31, chat=FORUM, message_id=8))
    commit(store, reacted(32, chat=FORUM, message_id=999))
    known, general, unknown = stored(store, 30), stored(store, 31), stored(store, 32)
    assert known["thread_id"] == "5" and known["conversation_key"] == "telegram:9:-100:5"
    assert known["conversation"]["topic_id"] == 5
    assert general["thread_id"] == "" and general["message"]["reaction"]["target"]["resolution"] == "resolved"
    assert unknown["thread_id"] == "" and unknown["message"]["reaction"]["target"] == {
        "message_id": "999", "resolution": "unresolved"}
    assert unknown["conversation_key"] == "telegram:9:-100:0"


@pytest.mark.parametrize("other_account,other_chat", [("8", GROUP), (BOT, {**GROUP, "id": -201})])
def test_same_message_id_under_another_bot_or_chat_does_not_match(tmp_path, other_account, other_chat):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=other_chat), account=other_account)
    commit(store, reacted())
    target = stored(store, 30)["message"]["reaction"]["target"]
    assert "author" not in target and "excerpt" not in target


def test_private_chat_that_has_shown_topics_is_not_called_proven(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=PRIVATE, topic=5, message_id=7))
    commit(store, reacted(chat=PRIVATE, message_id=8))
    assert stored(store, 30)["message"]["reaction"]["target"]["resolution"] == "unresolved"


def test_own_message_is_retained_from_its_exact_send_receipt(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": {
            "message_id": 77, "text": "my words", "chat": {"id": -100, "type": "supergroup", "is_forum": True}}}])
        value = world(tmp_path, provider)
        value.store.enqueue_outbox("telegram-send:a", {"kind": "message", "chat_id": "-100", "topic_id": "5",
                                                       "text": "my words", "markdown": False})
        assert await value.process_one_outbox()
        value.store.commit_update(30, parse(reacted(chat=FORUM, message_id=77)))
        event = stored(value.store, 30)
        assert event["thread_id"] == "5"
        assert event["message"]["reaction"]["target"]["author"] == {"platform_actor_id": "9", "is_self": True}
        assert event["message"]["reaction"]["target"]["excerpt"]["text"] == "my words"
        # Same words sent under an unconfirmed identity prove nothing.
        assert value.store.reaction_target("8", "-100", 77) == {"retained": False, "forum": None}
    asyncio.run(run())


def test_lookup_is_addressed_by_the_primary_key_not_a_scan(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    with sqlite3.connect(store.path) as db:
        plan = " ".join(str(row[3]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM telegram_messages WHERE account_id=? AND chat_id=? AND message_id=?", ("9", "1", 1)))
        newest = " ".join(str(row[3]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT chat_forum FROM telegram_messages WHERE account_id=? AND chat_id=? "
            "AND chat_forum IS NOT NULL ORDER BY message_id DESC LIMIT 1", ("9", "1")))
    assert "SEARCH" in plan and "SEARCH" in newest and "SCAN" not in plan and "TEMP B-TREE" not in newest
    assert "telegram_messages_forum" in newest


def test_edit_keeps_the_topic_and_newest_words(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=FORUM, topic=5))
    edit = {"update_id": 21, "edited_message": said(chat=FORUM, topic=5, text="corrected")["message"]}
    commit(store, edit)
    commit(store, reacted(chat=FORUM))
    event = stored(store, 30)
    assert event["thread_id"] == "5" and event["message"]["reaction"]["target"]["excerpt"]["text"] == "corrected"


def test_ordinary_events_gain_no_reaction_shape(tmp_path):
    event = parse(said())
    assert "reaction" not in event.message and "kind" not in event.actor and "sent_at" not in event.message


# -- Worker to Host --------------------------------------------------------------------------

def test_capable_host_receives_the_exact_stored_fact_and_no_files(tmp_path):
    async def run():
        value = world(tmp_path)
        commit(value.store, said())
        commit(value.store, reacted())
        assert await value.process_one_inbox() and await value.process_one_inbox()
        event = value.submitter.turns[-1]
        assert event["message"]["reaction"]["change"] == "add" and event["text"] == ""
        valid_ingress(event)
        assert value.store.status_snapshot()["reactions_withheld"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("identity", [OLD_HOST, {**OLD_HOST, "presence_reaction_context": 0},
                                      {**OLD_HOST, "presence_reaction_context": 2}, None])
def test_unsupported_host_withholds_the_reaction_and_keeps_it_locally(tmp_path, identity):
    async def run():
        value = world(tmp_path, identity=identity)
        commit(value.store, reacted())
        commit(value.store, counted())
        assert await value.process_one_inbox() and await value.process_one_inbox()
        assert value.submitter.turns == []
        counts = value.store.status_snapshot()
        assert counts["reactions_withheld"] == 2 and counts["inbox_failed"] == 0 and counts["inbox_waiting"] == 0
        assert value.status_snapshot()["reaction_context"] == "update_required"
        with sqlite3.connect(value.store.path) as db:
            assert {row[0] for row in db.execute("SELECT last_error FROM inbox")} == {REACTION_WITHHELD}
        assert stored(value.store, 30)["message"]["reaction"]["change"] == "add"
    asyncio.run(run())


def test_withheld_observation_is_not_replayed_after_the_host_gains_the_capability(tmp_path):
    async def run():
        value = world(tmp_path, identity=OLD_HOST)
        commit(value.store, reacted())
        assert await value.process_one_inbox()
        assert value.submitter.turns == []
        later = world(tmp_path, identity=CAPABLE)
        commit(later.store, reacted(40))
        assert await later.process_one_inbox()
        assert not await later.process_one_inbox()
        assert [event["source_event_id"] for event in later.submitter.turns] == ["telegram:9:40"]
    asyncio.run(run())


def test_row_queued_before_the_typed_fact_is_not_delivered_even_to_a_capable_host(tmp_path):
    async def run():
        value = world(tmp_path)
        event = parse(reacted()).to_dict()
        del event["message"]["reaction"]
        with sqlite3.connect(value.store.path) as db:
            db.execute("INSERT INTO inbox(event_id,update_id,conversation_key,payload_json,state,available_at,received_at)"
                       " VALUES (?,?,?,?, 'pending',0,0)", (event["source_event_id"], 30, event["conversation_key"], json.dumps(event)))
        assert await value.process_one_inbox()
        assert value.submitter.turns == [] and value.store.status_snapshot()["reactions_withheld"] == 1
    asyncio.run(run())


def test_normal_messages_continue_on_old_host_in_the_same_lane(tmp_path):
    async def run():
        value = world(tmp_path, identity=OLD_HOST)
        commit(value.store, said())
        commit(value.store, reacted())
        commit(value.store, said(40, message_id=8, text="next"))
        for _ in range(3):
            assert await value.process_one_inbox()
        assert [event["text"] for event in value.submitter.turns] == ["the words", "next"]
    asyncio.run(run())


def test_real_host_client_reads_the_capability_strictly_and_resets_on_failure(tmp_path):
    class Transport:
        def __init__(self, answer):
            self.answer = answer

        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            if isinstance(self.answer, BaseException):
                raise self.answer
            return self.answer

    async def run():
        for answer, expected in (({"presence_delivery_version": 1, "presence_reaction_context": 1,
                                   "presence_explicit_completion": 1}, (1, 1)),
                                 ({"presence_delivery_version": 1, "presence_reaction_context": True,
                                   "presence_explicit_completion": "1"}, (0, 0)),
                                 ({"presence_delivery_version": 1}, (0, 0)),
                                 (PresenceHostError("down"), (0, 0))):
            client = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "t", host_base="http://127.0.0.1:1",
                                        transport=Transport(answer))
            await client.discover_delivery_support()
            assert (client.presence_reaction_context, client.presence_explicit_completion) == expected
    asyncio.run(run())


def test_subscription_asks_for_counts_beside_individual_reactions():
    class Transport:
        calls = []

        async def post_json(self, url, payload, timeout_sec):
            self.calls.append(payload)
            return {"ok": True, "result": []}

    async def run():
        await TelegramClient("fake", transport=Transport()).get_updates(offset=0, timeout_sec=1)
        allowed = Transport.calls[0]["allowed_updates"]
        assert {"message_reaction", "message_reaction_count", "message", "my_chat_member"} <= set(allowed)
    asyncio.run(run())


# -- Tool ------------------------------------------------------------------------------------

@pytest.mark.parametrize("reaction,message", [
    ([THUMB, HEART], "at most one"),
    ([{"type": "paid"}], "paid"),
    ([{"type": "emoji"}], "custom_emoji_id"),
    ([{"type": "custom_emoji"}], "custom_emoji_id"),
    ("👍", "list"),
    (None, "list"),
])
def test_provider_documented_limits_refuse_before_the_queue(tmp_path, reaction, message):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    result = tool(tmp_path)(method="setMessageReaction", chat_id="55", message_id=7, reaction=reaction, request_id="r")
    assert result["ok"] is False and message in result["error"]
    assert store.delivery_receipt("telegram-operation:r") == {"state": "not_found"}


@pytest.mark.parametrize("reaction", [[THUMB], [{"type": "custom_emoji", "custom_emoji_id": "any"}], []])
def test_clear_custom_and_unlisted_values_are_left_to_telegram(tmp_path, reaction):
    result = tool(tmp_path)(method="setMessageReaction", chat_id="55", message_id=7, reaction=reaction, request_id="r")
    assert result["state"] == "queued"


def test_forum_reaction_without_a_known_topic_is_refused_with_its_cause(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=FORUM, topic=5, message_id=7))
    miss = tool(tmp_path)(method="setMessageReaction", chat_id="-100", message_id=999, reaction=[THUMB], request_id="a")
    assert miss["ok"] is False and miss["error"].startswith("forum_topic_unknown") and "is a forum" in miss["error"]
    assert "Not sent" in miss["error"]
    assert store.delivery_receipt("telegram-operation:a") == {"state": "not_found"}


def test_retained_target_must_name_its_own_topic_for_the_guard(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=FORUM, topic=5, message_id=7))
    commit(store, said(21, chat=FORUM, message_id=8))
    operate = tool(tmp_path)
    omitted = operate(method="setMessageReaction", chat_id="-100", message_id=7, reaction=[THUMB], request_id="a")
    assert omitted["ok"] is False and "forum_topic_mismatch" in omitted["error"] and "topic_id=5" in omitted["error"]
    wrong = operate(method="setMessageReaction", chat_id="-100", message_id=7, topic_id=6, reaction=[THUMB], request_id="b")
    assert wrong["ok"] is False and "topic 5" in wrong["error"]
    general = operate(method="setMessageReaction", chat_id="-100", message_id=8, reaction=[THUMB], request_id="c")
    assert general["state"] == "queued"
    right = operate(method="setMessageReaction", chat_id="-100", message_id=7, topic_id=5, reaction=[THUMB], request_id="d")
    assert right["state"] == "queued"
    payload = store.outbox_payload("telegram-operation:d")
    assert payload["topic_id"] == "5" and payload["_reaction_target"] == {"message_id": "7", "resolution": "resolved"}
    assert "topic_id" not in payload["parameters"] and "message_thread_id" not in payload["parameters"]


def test_explicit_topic_on_an_unretained_forum_message_is_kept_unproven(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=FORUM, topic=5, message_id=7))
    result = tool(tmp_path)(method="setMessageReaction", chat_id="-100", message_id=999, topic_id=6,
                            reaction=[THUMB], request_id="a")
    assert result["state"] == "queued"
    payload = store.outbox_payload("telegram-operation:a")
    assert payload["topic_id"] == "6" and payload["_reaction_target"]["resolution"] == "unresolved"


def test_never_observed_group_is_not_assumed_to_have_no_topics(tmp_path):
    refused = tool(tmp_path)(method="setMessageReaction", chat_id="-300", message_id=7, reaction=[THUMB], request_id="a")
    assert refused["ok"] is False and "may be a forum" in refused["error"]
    # Without a confirmed bot nothing retained can be read as this bot's.
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said())
    unconfirmed = tool(tmp_path, account="")(method="setMessageReaction", chat_id="-200", message_id=7,
                                             reaction=[THUMB], request_id="b")
    assert unconfirmed["ok"] is False and "identity is unconfirmed" in unconfirmed["error"]
    assert "not retained" not in unconfirmed["error"] and "not observed" not in unconfirmed["error"]


def test_known_non_forum_and_private_targets_keep_working_without_a_topic(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(message_id=7))
    operate = tool(tmp_path)
    assert operate(method="setMessageReaction", chat_id="-200", message_id=7, reaction=[THUMB], request_id="a")["state"] == "queued"
    # A message this bot never saw, in a chat known to have no topics.
    assert operate(method="setMessageReaction", chat_id="-200", message_id=999, reaction=[THUMB], request_id="b")["state"] == "queued"
    assert operate(method="setMessageReaction", chat_id="55", message_id=1, reaction=[THUMB], request_id="c")["state"] == "queued"
    wrong = operate(method="setMessageReaction", chat_id="-200", message_id=999, topic_id=3, reaction=[THUMB], request_id="d")
    assert wrong["ok"] is False and "no topics" in wrong["error"]
    assert store.outbox_payload("telegram-operation:b")["_reaction_target"]["resolution"] == "resolved"


def test_edit_keeps_its_separate_path_and_needs_no_topic(tmp_path):
    result = tool(tmp_path)(method="editMessageText", chat_id="-300", message_id=7, text="fixed", request_id="e")
    assert result["state"] == "queued"
    store = CustodyStore(tmp_path / "custody.sqlite3")
    assert "_reaction_target" not in store.outbox_payload("telegram-operation:e")


def test_registered_tool_reads_records_of_the_bot_getme_confirmed(tmp_path):
    from test_plugin import FakeApi
    skill = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("telegram_reactions_registered", skill / "plugin.py",
                                                  submodule_search_locations=[str(skill)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    api.get_settings = lambda _keys: {"TELEGRAM_PUBLIC_BOT_TOKEN": "fake-token"}
    module.register(api)
    [(_, operate, metadata)] = [row for row in api.tools if row[0] == "telegram_operation"]
    assert "own-message" not in metadata["description"] and "any message" in metadata["description"]
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=FORUM, topic=5, message_id=7))
    arguments = dict(method="setMessageReaction", chat_id="-100", message_id=7, reaction=[THUMB])
    module._RUNTIME.store.record_confirmed_bot("fake-token", BOT)
    assert "topic_id=5" in operate(**arguments, request_id="a")["error"]
    # A retained record of another bot's message is not this bot's: it is simply unknown here.
    module._RUNTIME.store.record_confirmed_bot("fake-token", "8")
    refused = operate(**arguments, request_id="b")["error"]
    assert "forum_topic_unknown" in refused and "identity" not in refused
    api.unload()


# -- Tool to outbox to provider to receipt ---------------------------------------------------

def queue_reaction(value, *, request_id="r", reaction=(THUMB,), chat_id="55", message_id=7, **extra):
    result = tool(value.state_dir)(method="setMessageReaction", chat_id=chat_id, message_id=message_id,
                                   reaction=list(reaction), request_id=request_id, **extra)
    assert result["state"] == "queued"
    return f"telegram-operation:{request_id}"


def provider_reactions(provider):
    return [payload for endpoint, payload in provider.calls if endpoint == "setMessageReaction"]


def test_applied_set_reports_what_was_asked_for_and_no_invented_prior(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        queued = value.store.delivery_receipt(delivery)
        assert queued["state"] == "pending" and "history_limitation" not in queued and "provider_receipt" in queued
        assert await value.process_one_outbox()
        assert provider_reactions(provider) == [{"chat_id": "55", "message_id": 7, "reaction": [THUMB], "is_big": False}]
        receipt = value.store.delivery_receipt(delivery)
        assert receipt["state"] == "delivered" and receipt["provider_receipt"]["effect"] == "applied"
        await drain_reports(value)
        [report] = value.submitter.reports
        valid_receipt(report)
        assert report["text"] == "" and report["format"] == "" and report["part_id"] == "0" and report["thread_id"] == ""
        assert report["message"]["kind"] == "operation" and report["message"]["method"] == "setMessageReaction"
        assert report["message"]["reaction"] == {
            "schema_version": 1, "operation": "set", "new": [{"type": "emoji", "value": "👍"}],
            "target": {"message_id": "7", "resolution": "resolved"}, "effect": "applied"}
    asyncio.run(run())


def test_clear_is_a_set_of_nothing(tmp_path):
    async def run():
        value = world(tmp_path, Provider([{"ok": True, "result": True}]))
        queue_reaction(value, reaction=())
        assert await value.process_one_outbox()
        await drain_reports(value)
        assert value.submitter.reports[0]["message"]["reaction"]["new"] == []
        assert "old" not in value.submitter.reports[0]["message"]["reaction"]
    asyncio.run(run())


def test_old_host_gets_no_reaction_report_but_keeps_edit_reports_and_the_local_receipt(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}, {"ok": True, "result": {"message_id": 7, "text": "fixed"}}])
        value = world(tmp_path, provider, identity=OLD_HOST)
        delivery = queue_reaction(value)
        edit = tool(tmp_path)(method="editMessageText", chat_id="55", message_id=7, text="fixed", request_id="e")
        assert edit["state"] == "queued"
        assert await value.process_one_outbox() and await value.process_one_outbox()
        await drain_reports(value)
        receipt = value.store.delivery_receipt(delivery)
        assert receipt["state"] == "delivered" and receipt["history_reports"] == []
        assert "unavailable" in receipt["history_limitation"]
        assert [row["message"]["method"] for row in value.submitter.reports] == ["editMessageText"]
        assert "history_limitation" not in value.store.delivery_receipt("telegram-operation:e")
    asyncio.run(run())


def test_explicit_topic_stays_local_through_lane_guard_and_report(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        commit(value.store, said(chat=FORUM, topic=5, message_id=7))
        queue_reaction(value, chat_id="-100", topic_id=5)
        assert await value.process_one_outbox()
        [call] = provider_reactions(provider)
        assert "topic_id" not in call and "message_thread_id" not in call
        assert value.submitter.place_checks == [(BOT, "-100", "5")]
        await drain_reports(value)
        assert value.submitter.reports[0]["thread_id"] == "5"
    asyncio.run(run())


def test_a_topic_paused_after_queueing_holds_its_reaction_while_general_goes(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}, {"ok": True, "result": True}])
        value = world(tmp_path, provider)
        commit(value.store, said(chat=FORUM, topic=5, message_id=7))
        commit(value.store, said(21, chat=FORUM, message_id=8))
        in_topic = queue_reaction(value, request_id="topic", chat_id="-100", topic_id=5)
        in_general = queue_reaction(value, request_id="general", chat_id="-100", message_id=8)
        value.submitter.thread_holds["5"] = "Held: Telegram is paused"  # that topic's own place, after queueing
        assert await value.process_one_outbox() and await value.process_one_outbox()
        held = value.store.delivery_receipt(in_topic)
        assert held["state"] == "held" and held["error"] == "Held: Telegram is paused" and held["attempts"] == 0
        assert "_provider_call_started" not in value.store.outbox_payload(in_topic)
        assert value.store.delivery_receipt(in_general)["state"] == "delivered"
        assert [call["message_id"] for call in provider_reactions(provider)] == [8]
        # A reaction without a topic is checked as the chat's, as before; the paused topic is named only for its own.
        assert value.submitter.place_checks == [(BOT, "-100", "5"), (BOT, "-100", "")]
        del value.submitter.thread_holds["5"]
        ready_again(value.store)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(in_topic)["state"] == "delivered"
        assert [call["message_id"] for call in provider_reactions(provider)] == [8, 7]
    asyncio.run(run())


@pytest.mark.parametrize("answer", [
    {"ok": False, "error_code": 400, "description": "Bad Request: REACTION_INVALID"},
    {"ok": False, "error_code": 400, "description": "Bad Request: message to react not found"},
    {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"},
])
def test_refusal_settles_at_once_and_does_not_hold_the_next_job(tmp_path, answer):
    async def run():
        provider = Provider([answer, {"ok": True, "result": True}])
        value = world(tmp_path, provider)
        first = queue_reaction(value, request_id="a")
        second = queue_reaction(value, request_id="b")
        assert await value.process_one_outbox() and await value.process_one_outbox()
        failed = value.store.delivery_receipt(first)
        assert failed["state"] == "failed" and failed["attempts"] == 1 and answer["description"] in failed["error"]
        assert value.store.delivery_receipt(second)["state"] == "delivered"
        assert len(provider_reactions(provider)) == 2
        await drain_reports(value)
        report = next(row for row in value.submitter.reports if row["delivery_id"] == first)
        valid_receipt(report)
        assert report["state"] == "failed" and "effect" not in report["message"]["reaction"]
    asyncio.run(run())


@pytest.mark.parametrize("answer", [OSError("response lost"), {"ok": False, "error_code": 502, "description": "Bad Gateway"},
                                    {"ok": False, "description": "no code"}])
def test_unknown_acceptance_is_uncertain_and_never_sent_again(tmp_path, answer):
    async def run():
        provider = Provider([answer])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        assert await value.process_one_outbox()
        assert not await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "uncertain"
        assert len(provider_reactions(provider)) == 1
        await drain_reports(value)
        [report] = value.submitter.reports
        valid_receipt(report)
        assert report["state"] == "uncertain" and "effect" not in report["message"]["reaction"]
    asyncio.run(run())


def test_restart_after_the_marker_settles_uncertain_without_a_second_call(tmp_path):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        lease = value.store.claim_outbox(lease_sec=120)
        value.store.checkpoint_outbox(delivery, {**lease.payload, "_provider_call_started": True})
        assert value.store.release_own_leases()["outbox"] == 1  # the worker died mid-call
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "uncertain" and provider_reactions(provider) == []
    asyncio.run(run())


def test_confirmed_result_already_checkpointed_finishes_without_a_second_call(tmp_path):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        lease = value.store.claim_outbox(lease_sec=120)
        value.store.checkpoint_outbox(delivery, {**lease.payload, "_provider_call_started": True,
                                                 "_operation_result": {"ok": True}})
        value.store.release_own_leases()
        assert await value.process_one_outbox()
        receipt = value.store.delivery_receipt(delivery)
        assert receipt["state"] == "delivered" and receipt["provider_receipt"]["effect"] == "applied"
        assert provider_reactions(provider) == []
    asyncio.run(run())


def test_flood_refusal_retries_and_is_not_mistaken_for_an_unknown_effect(tmp_path):
    async def run():
        provider = Provider([{"ok": False, "error_code": 429, "description": "Too Many Requests"},
                             {"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "pending"
        assert "_provider_call_started" not in value.store.outbox_payload(delivery)
        ready_again(value.store)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "delivered"
        assert len(provider_reactions(provider)) == 2
    asyncio.run(run())


def test_silent_place_holds_the_reaction_before_the_marker_or_any_call(tmp_path):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        value.submitter.hold = "Held: Telegram is silent"
        delivery = queue_reaction(value)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "held" and provider_reactions(provider) == []
        assert "_provider_call_started" not in value.store.outbox_payload(delivery)
    asyncio.run(run())


def test_status_discloses_the_gap_on_an_old_host(tmp_path):
    async def run():
        value = world(tmp_path, identity=OLD_HOST)
        await value.submitter.discover_delivery_support()
        assert value.status_snapshot()["reaction_context"] == "update_required"
        assert value.status_snapshot()["reaction_history"] == "update_required"
        capable = world(tmp_path / "other", identity=CAPABLE)
        await capable.submitter.discover_delivery_support()
        assert capable.status_snapshot()["reaction_context"] == "available"
        assert capable.status_snapshot()["reaction_history"] == "available"
    asyncio.run(run())


# -- Reports already frozen, at the moment they would be sent ---------------------------------

def legacy_report(delivery_id="telegram-operation:old", *, state="delivered", part="0", **message):
    """Exactly what 0.9.0's delivery_report froze for a confirmed reaction."""
    return {"schema_version": 1, "delivery_id": delivery_id, "part_id": part, "state": state, "provider": "telegram",
            "account_id": BOT, "conversation_id": "-200", "thread_id": "", "text": "", "format": "",
            "message": {"kind": "operation", "method": "setMessageReaction", "target_message_id": 7,
                        "reaction": [dict(THUMB)], **message},
            "origin": {"kind": "tool", "task_id": "t1"}}


def freeze(store, report):
    """A delivered 0.9.0 row with its unacknowledged report, as an upgrade finds it."""
    payload = {"kind": "operation", "chat_id": "-200", "method": "setMessageReaction",
               "parameters": {"chat_id": "-200", "message_id": 7, "reaction": [dict(THUMB)], "is_big": False},
               "text": "", "_reporting": {"version": 1, "account_id": BOT, "origin": {"kind": "tool", "task_id": "t1"}},
               "_operation_result": {"ok": True}}
    delivery_id = report["delivery_id"]
    store.enqueue_outbox(delivery_id, payload)
    store.claim_outbox(lease_sec=120)
    store.mark_delivered(delivery_id, provider_receipt={"kind": "operation", "method": "setMessageReaction",
                                                        "result": {"ok": True}}, report=report)


def reports_due(store):
    with sqlite3.connect(store.path) as db:
        for delivery_id, raw in db.execute("SELECT delivery_id,reports_json FROM outbox").fetchall():
            entries = json.loads(raw)
            for entry in entries:
                entry["available_at"] = 1
            db.execute("UPDATE outbox SET reports_json=?,report_due_at=? WHERE delivery_id=?",
                       (json.dumps(entries), 1 if any(not e["acked"] for e in entries) else 0, delivery_id))


def stored_entry(store, delivery_id="telegram-operation:old"):
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT reports_json,provider_receipt_json FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone()
    return json.loads(row[0])[0], row[1]


@pytest.mark.parametrize("identity", [OLD_HOST, {**OLD_HOST, "presence_explicit_completion": 2},
                                      {**OLD_HOST, "presence_explicit_completion": True}, None])
def test_frozen_list_report_stays_local_on_a_host_that_could_count_it_as_speech(tmp_path, identity):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider, identity=identity)
        freeze(value.store, legacy_report())
        _, receipt_before = stored_entry(value.store)
        assert await value.process_one_delivery_report()
        assert value.submitter.reports == [] and provider.calls == []
        entry, receipt_after = stored_entry(value.store)
        assert entry["payload"] == legacy_report() and receipt_after == receipt_before
        assert entry["acked"] is False and entry["error"].startswith("Held: Host history for reactions is unavailable")
        receipt = value.store.delivery_receipt("telegram-operation:old")
        assert receipt["state"] == "delivered" and "unavailable" in receipt["history_limitation"]
        assert value.store.status_snapshot()["delivery_reports_held"] == 1
        # Retried when due: held again, still no provider call and no ACK.
        reports_due(value.store)
        assert await value.process_one_delivery_report()
        assert value.submitter.reports == [] and provider.calls == [] and stored_entry(value.store)[0]["acked"] is False
    asyncio.run(run())


def test_never_asked_host_is_asked_before_the_decision(tmp_path):
    async def run():
        value = world(tmp_path, identity=CAPABLE)
        freeze(value.store, legacy_report())
        assert value.submitter.presence_explicit_completion == 0  # no discovery has happened yet
        assert await value.process_one_delivery_report()
        assert len(value.submitter.reports) == 1
    asyncio.run(run())


def test_read_failure_then_recovery_through_the_real_host_client(tmp_path):
    class Transport:
        def __init__(self):
            self.identity, self.posts = None, []

        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            if url.endswith("/identity"):
                if self.identity is None:
                    raise PresenceHostError("down")
                return self.identity
            self.posts.append(payload)
            return {"ok": True, "recorded": True}

    async def run():
        transport = Transport()
        value = world(tmp_path)
        value.submitter = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "t",
                                             host_base="http://127.0.0.1:1", transport=transport)
        freeze(value.store, legacy_report())
        assert await value.process_one_delivery_report()
        assert transport.posts == [] and stored_entry(value.store)[0]["error"].startswith("Held:")
        transport.identity = CAPABLE
        reports_due(value.store)
        assert await value.process_one_delivery_report()
        assert [post["message"]["reaction"]["operation"] for post in transport.posts] == ["set"]
        assert stored_entry(value.store)[0]["acked"] is True
    asyncio.run(run())


def test_old_host_then_reopened_on_a_capable_host_sends_the_typed_fact_once_and_keeps_the_original(tmp_path):
    async def run():
        provider = Provider()
        before = world(tmp_path, provider, identity=OLD_HOST)
        freeze(before.store, legacy_report())
        assert await before.process_one_delivery_report()
        assert before.submitter.reports == []

        after = world(tmp_path, provider, identity=CAPABLE)  # a new process over the same custody
        reports_due(after.store)
        assert await after.process_one_delivery_report()
        [report] = after.submitter.reports
        valid_receipt(report)
        assert report["message"]["reaction"] == {
            "schema_version": 1, "operation": "set", "new": [{"type": "emoji", "value": "👍"}],
            "target": {"message_id": "7", "resolution": "unresolved"}, "effect": "applied"}
        assert "old" not in report["message"]["reaction"]
        assert (report["delivery_id"], report["part_id"], report["state"], report["text"]) == (
            "telegram-operation:old", "0", "delivered", "")
        entry, _ = stored_entry(after.store)
        assert entry["payload"] == legacy_report() and entry["acked"] is True and entry["error"] == ""
        assert "history_limitation" not in after.store.delivery_receipt("telegram-operation:old")
        assert after.store.status_snapshot()["delivery_reports_held"] == 0
        assert not await after.process_one_delivery_report() and len(after.submitter.reports) == 1
        assert provider.calls == []
    asyncio.run(run())


def test_frozen_failed_and_uncertain_reports_claim_no_effect_when_typed(tmp_path):
    async def run():
        value = world(tmp_path)
        for state, key in (("failed", "a"), ("uncertain", "b")):
            freeze(value.store, legacy_report(f"telegram-operation:{key}", state=state, part="status",
                                              error="boom", confirmed_parts=0))
        assert await value.process_one_delivery_report() and await value.process_one_delivery_report()
        assert [row["state"] for row in value.submitter.reports] == ["failed", "uncertain"]
        for row in value.submitter.reports:
            valid_receipt(row)
            assert "effect" not in row["message"]["reaction"] and row["message"]["error"] == "boom"
    asyncio.run(run())


@pytest.mark.parametrize("damage", [{"target_message_id": None}, {"target_message_id": True}, {"reaction": "👍"},
                                    {"reaction": [{"emoji": "👍"}]}, {"reaction": {"schema_version": 9}}])
def test_report_that_does_not_state_its_facts_is_disclosed_not_guessed(tmp_path, damage):
    async def run():
        value = world(tmp_path)
        freeze(value.store, legacy_report(**damage))
        assert await value.process_one_delivery_report()
        assert value.submitter.reports == []
        entry, _ = stored_entry(value.store)
        assert entry["acked"] is False and "does not state its message and values" in entry["error"]
    asyncio.run(run())


def test_typed_report_frozen_on_a_capable_host_is_held_after_the_host_changes_back(tmp_path):
    async def run():
        value = world(tmp_path, Provider([{"ok": True, "result": True}]))
        queue_reaction(value)
        assert await value.process_one_outbox()
        later = world(tmp_path, identity=OLD_HOST)
        assert await later.process_one_delivery_report()
        assert later.submitter.reports == []
        entry, _ = stored_entry(later.store, "telegram-operation:r")
        assert entry["payload"]["message"]["reaction"]["effect"] == "applied" and entry["error"].startswith("Held:")
    asyncio.run(run())


def test_held_reaction_report_does_not_hold_edit_or_send_reports(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": {"message_id": 7, "text": "fixed"}},
                             {"ok": True, "result": {"message_id": 8, "text": "hello"}}])
        value = world(tmp_path, provider, identity=OLD_HOST)
        freeze(value.store, legacy_report())
        edit = tool(tmp_path)(method="editMessageText", chat_id="55", message_id=7, text="fixed", request_id="e")
        assert edit["state"] == "queued"
        value.store.enqueue_outbox("telegram-send:s", {
            "kind": "message", "chat_id": "55", "text": "hello", "markdown": False,
            "_reporting": {"version": 1, "account_id": BOT, "origin": {"kind": "tool"}}})
        assert await value.process_one_outbox() and await value.process_one_outbox()
        for _ in range(4):
            await value.process_one_delivery_report()
        reported = {row["delivery_id"]: row["message"].get("method") for row in value.submitter.reports}
        assert reported == {"telegram-operation:e": "editMessageText", "telegram-send:s": None}
        assert stored_entry(value.store)[0]["acked"] is False
    asyncio.run(run())


def test_lost_ack_after_an_old_host_recorded_the_list_stays_visible_and_never_touches_telegram(tmp_path):
    from telegram_bot.host import PresenceHostHTTPError

    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        freeze(value.store, legacy_report())

        async def conflict(_payload):
            raise PresenceHostHTTPError(409, "presence delivery identity already has different facts",
                                        {"ok": False, "code": "presence_receipt_conflict", "disposition": "rejected"})
        value.submitter.report_delivery = conflict
        assert await value.process_one_delivery_report()
        entry, _ = stored_entry(value.store)
        assert entry["acked"] is False and "409" in entry["error"] and provider.calls == []
    asyncio.run(run())


def test_outbox_row_retried_after_upgrade_asks_the_host_before_deciding_on_a_report(tmp_path):
    async def run():
        value = world(tmp_path, Provider([{"ok": True, "result": True}]))
        value.store.enqueue_outbox("telegram-operation:p", {
            "kind": "operation", "chat_id": "55", "method": "setMessageReaction", "text": "",
            "parameters": {"chat_id": "55", "message_id": 7, "reaction": [THUMB], "is_big": False},
            "_reporting": {"version": 1, "account_id": BOT, "origin": {"kind": "tool"}}})
        assert value.submitter.presence_explicit_completion == 0
        assert await value.process_one_outbox()
        await drain_reports(value)
        assert value.submitter.reports[0]["message"]["reaction"]["effect"] == "applied"
    asyncio.run(run())


# -- Rows an earlier attempt left without an answer ------------------------------------------

def old_reaction_row(store, *, delivery_id="telegram-operation:stale", chat_id="-200"):
    """A reaction row as 0.9.0 left it: no send marker, and nothing says whether Telegram was called."""
    payload = {"kind": "operation", "chat_id": chat_id, "method": "setMessageReaction", "text": "",
               "parameters": {"chat_id": chat_id, "message_id": 7, "reaction": [dict(THUMB)], "is_big": False},
               "_reporting": {"version": 1, "account_id": BOT, "status": "supported",
                              "origin": {"kind": "tool", "task_id": "t1"}}}
    store.enqueue_outbox(delivery_id, payload)
    return payload


def follower(store, chat_id="-200"):
    store.enqueue_outbox("telegram-send:next", {
        "kind": "message", "chat_id": chat_id, "text": "next", "markdown": False,
        "_reporting": {"version": 1, "account_id": BOT, "origin": {"kind": "tool"}}})


def leave_leased(store):
    lease = store.claim_outbox(lease_sec=120)
    assert lease.delivery_id == "telegram-operation:stale"
    return lease


def expire_leases(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET lease_until=1 WHERE state='leased'")


REOPENED = {
    "released_at_start": lambda store: store.release_own_leases(),
    "lease_expired": expire_leases,
}


@pytest.mark.parametrize("reopen", list(REOPENED))
def test_row_leased_when_the_runtime_stopped_is_never_called_again_and_its_follower_proceeds(tmp_path, reopen):
    async def run():
        provider = Provider([{"ok": True, "result": {"message_id": 8, "text": "next"}}])
        value = world(tmp_path, provider)
        frozen = old_reaction_row(value.store)
        follower(value.store)
        leave_leased(value.store)
        REOPENED[reopen](value.store)

        assert await value.process_one_outbox() and await value.process_one_outbox()
        assert provider_reactions(provider) == []
        assert [endpoint for endpoint, _ in provider.calls] == ["sendMessage"]
        receipt = value.store.delivery_receipt("telegram-operation:stale")
        assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}
        assert "send_result_unrecorded" in receipt["error"]
        stored = value.store.outbox_payload("telegram-operation:stale")
        assert {key: stored[key] for key in ("kind", "chat_id", "method", "parameters", "text")} == {
            key: frozen[key] for key in ("kind", "chat_id", "method", "parameters", "text")}
        assert value.store.delivery_receipt("telegram-send:next")["state"] == "delivered"
        counts = value.store.status_snapshot()
        assert counts["outbox_uncertain"] == 1 and counts["outbox_failed"] == 0
        await drain_reports(value)
        report = next(row for row in value.submitter.reports if row["delivery_id"] == "telegram-operation:stale")
        valid_receipt(report)
        assert report["state"] == "uncertain" and report["part_id"] == "status" and report["text"] == ""
        assert report["message"]["reaction"]["operation"] == "set" and "effect" not in report["message"]["reaction"]
        assert "send_result_unrecorded" in report["message"]["error"]
        # Later passes and another restart change nothing.
        value.store.release_own_leases()
        ready_again(value.store)
        assert not await value.process_one_outbox() and provider_reactions(provider) == []
    asyncio.run(run())


def test_old_host_keeps_the_unknown_outcome_local(tmp_path):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider, identity=OLD_HOST)
        old_reaction_row(value.store)
        leave_leased(value.store)
        value.store.release_own_leases()
        assert await value.process_one_outbox()
        await drain_reports(value)
        receipt = value.store.delivery_receipt("telegram-operation:stale")
        assert receipt["state"] == "uncertain" and provider.calls == [] and value.submitter.reports == []
        assert "unavailable" in receipt["history_limitation"]
    asyncio.run(run())


def test_row_0_9_released_or_cancelled_before_the_upgrade_is_equally_unknown(tmp_path):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        for delivery_id, reason in (("telegram-operation:a", "released_at_runtime_start"),
                                    ("telegram-operation:b", "worker_cancelled")):
            old_reaction_row(value.store, delivery_id=delivery_id)
            with sqlite3.connect(value.store.path) as db:
                db.execute("UPDATE outbox SET attempts=1,last_error=? WHERE delivery_id=?", (reason, delivery_id))
        assert await value.process_one_outbox() and await value.process_one_outbox()
        assert provider.calls == []
        assert {value.store.delivery_receipt(key)["state"] for key in ("telegram-operation:a", "telegram-operation:b")} == {"uncertain"}
    asyncio.run(run())


def test_unknown_row_settles_without_a_token_so_the_lane_is_not_held(tmp_path):
    async def run():
        value = world(tmp_path)
        value.token_provider = lambda: ""
        old_reaction_row(value.store)
        leave_leased(value.store)
        value.store.release_own_leases()
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == "uncertain"
    asyncio.run(run())


def test_cancellation_before_the_call_settles_uncertain_rather_than_risk_a_repeat(tmp_path):
    """A marker-less cancel looks exactly like 0.9.0's, so it is treated the same way."""
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        queue_reaction(value)
        value.submitter.place_hold = lambda *_: asyncio.sleep(3600)
        task = asyncio.create_task(value.process_one_outbox())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        value.submitter.place_hold = lambda *_: asyncio.sleep(0, "")
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:r")["state"] == "uncertain"
        assert provider_reactions(provider) == []
    asyncio.run(run())


def test_fresh_row_still_sends_once(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        old_reaction_row(value.store)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == "delivered"
        assert len(provider_reactions(provider)) == 1
    asyncio.run(run())


# What 0.9.0's own client and runtime leave in last_error, word for word, when its source is run.
OLD_429 = "TelegramApiError: Telegram setMessageReaction failed (429): Too Many Requests: retry after 1"
OLD_TOKEN = "RuntimeError: telegram token is unavailable"


@pytest.mark.parametrize("attempts,last_error", [
    (1, OLD_429),
    (1, "TelegramApiError: Telegram setMessageReaction failed (401): Unauthorized"),
    (1, "TelegramApiError: Telegram setMessageReaction failed (404): Not Found"),
    (1, "TelegramApiError: Telegram setMessageReaction failed (400): Bad Request: REACTION_INVALID"),
    (1, "TelegramApiError: Telegram setMessageReaction failed (403): Forbidden: bot was kicked from the group chat"),
    (1, OLD_TOKEN),
    (1, "TelegramApiError: Telegram getMe failed (401): Unauthorized"),
    (0, "Held: Telegram is silent"),  # a hold counts no attempt
])
def test_known_no_effect_retry_of_an_old_row_still_sends(tmp_path, attempts, last_error):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        old_reaction_row(value.store)
        with sqlite3.connect(value.store.path) as db:
            db.execute("UPDATE outbox SET attempts=?,last_error=?", (attempts, last_error))
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == "delivered"
        assert len(provider_reactions(provider)) == 1
        # The stamp is the count this version's claim left.
        assert value.store.outbox_payload("telegram-operation:stale")["_attempts_marked"] == attempts + 1
    asyncio.run(run())


@pytest.mark.parametrize("attempts,last_error", [
    (1, "TelegramApiError: Telegram setMessageReaction failed (408): HTTP error"),
    (1, "TelegramApiError: Telegram setMessageReaction failed (502): HTTP error"),
    (1, "TelegramApiError: Telegram setMessageReaction failed (409): Conflict"),
    (1, "ValueError: invalid literal for int() with base 10: 'abc'"),  # 0.9.0 read a malformed error_code
    (1, "TypeError: int() argument must be a string, a bytes-like object or a real number, not 'list'"),
    (1, "OperationalError: database is locked"),  # its store failed, before the call or after it
    (1, "Held: Telegram is silent"),  # the hold replaced the counted attempt's error
    (2, OLD_429),  # 0.9.0 keeps only the newest answer
    (2, OLD_TOKEN),
])
def test_row_0_9_released_without_proof_of_no_effect_is_never_sent_again(tmp_path, attempts, last_error):
    async def run():
        provider = Provider([{"ok": True, "result": {"message_id": 8, "text": "next"}}])
        value = world(tmp_path, provider)
        frozen = old_reaction_row(value.store)
        follower(value.store)
        with sqlite3.connect(value.store.path) as db:
            db.execute("UPDATE outbox SET attempts=?,last_error=? WHERE delivery_id='telegram-operation:stale'",
                       (attempts, last_error))
        assert await value.process_one_outbox() and await value.process_one_outbox()
        assert [endpoint for endpoint, _ in provider.calls] == ["sendMessage"]
        receipt = value.store.delivery_receipt("telegram-operation:stale")
        assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}
        assert "send_result_unrecorded" in receipt["error"] and receipt["attempts"] == attempts + 1
        stored = value.store.outbox_payload("telegram-operation:stale")
        assert "_attempts_marked" not in stored and stored["parameters"] == frozen["parameters"]
        assert value.store.delivery_receipt("telegram-send:next")["state"] == "delivered"
        assert value.store.status_snapshot()["outbox_uncertain"] == 1
        await drain_reports(value)
        report = next(row for row in value.submitter.reports if row["delivery_id"] == "telegram-operation:stale")
        valid_receipt(report)
        assert report["state"] == "uncertain" and "effect" not in report["message"]["reaction"]
        value.store.release_own_leases()
        ready_again(value.store)
        assert not await value.process_one_outbox() and provider_reactions(provider) == []
    asyncio.run(run())


def test_old_row_is_judged_before_a_failure_of_this_version_can_replace_its_record(tmp_path):
    """The claim reads 0.9.0's error before clearing it, so no token, Host or place read can change the verdict."""
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        old_reaction_row(value.store)
        with sqlite3.connect(value.store.path) as db:
            db.execute("UPDATE outbox SET attempts=1,last_error='TelegramApiError: Telegram setMessageReaction "
                       "failed (408): HTTP error'")
        ready, hold = value._ready_client, value.submitter.place_hold

        async def no_token():
            return None

        async def no_place(*_args):
            raise PresenceHostError("down")
        value._ready_client, value.submitter.place_hold = no_token, no_place
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == "uncertain"
        value._ready_client, value.submitter.place_hold = ready, hold
        ready_again(value.store)
        assert not await value.process_one_outbox() and provider.calls == []
    asyncio.run(run())


def test_old_row_cleared_once_keeps_this_versions_proof_through_a_failure_before_the_call(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        old_reaction_row(value.store)
        with sqlite3.connect(value.store.path) as db:
            db.execute("UPDATE outbox SET attempts=1,last_error=?", (OLD_429,))
        ready = value._ready_client

        async def no_token():
            return None
        value._ready_client = no_token
        assert await value.process_one_outbox()
        pending = value.store.delivery_receipt("telegram-operation:stale")
        assert pending["state"] == "pending" and pending["attempts"] == 2 and provider.calls == []
        assert value.store.outbox_payload("telegram-operation:stale")["_attempts_marked"] == 2
        value._ready_client = ready
        ready_again(value.store)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == "delivered"
        assert len(provider_reactions(provider)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("reopen", list(REOPENED))
def test_stale_row_with_a_recorded_result_finishes_delivered_without_a_call(tmp_path, reopen):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        payload = old_reaction_row(value.store)
        lease = leave_leased(value.store)
        value.store.checkpoint_outbox(lease.delivery_id, {**payload, "_operation_result": {"ok": True}})
        REOPENED[reopen](value.store)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == "delivered"
        assert provider.calls == []
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["edit", "moderation"])
def test_stale_edit_and_moderation_rows_keep_their_existing_handling(tmp_path, kind):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        if kind == "edit":
            value.store.enqueue_outbox("telegram-operation:x", {
                "kind": "operation", "chat_id": "-200", "method": "editMessageText", "text": "fixed",
                "parameters": {"chat_id": "-200", "message_id": 7, "text": "fixed"}})
        else:
            value.store.enqueue_outbox("telegram-moderate:x", {
                "kind": "moderation", "chat_id": "-200", "action": "delete_message",
                "parameters": {"chat_id": "-200", "message_id": 7}})
        value.store.claim_outbox(lease_sec=120)
        value.store.release_own_leases()
        assert await value.process_one_outbox()
        assert len(provider.calls) == 1
    asyncio.run(run())


# -- A rollback to 0.9.0 between this version's attempts --------------------------------------

# What 0.9.0's run left in last_error after these answers, word for word, besides OLD_429 and OLD_TOKEN.
OLD_408 = "TelegramApiError: Telegram setMessageReaction failed (408): HTTP error"
OLD_502 = "TelegramApiError: Telegram setMessageReaction failed (502): HTTP error"
OLD_MALFORMED = "ValueError: invalid literal for int() with base 10: 'abc'"
OLD_GETME = "TelegramApiError: Telegram getMe failed (401): Unauthorized"
HELD = "Held: Telegram is silent"
# How this version's first attempt ends without an effect, with Telegram's answers to it.
FIRST = {"no_token": [], "held": [],
         "429": [{"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1"}],
         "401": [{"ok": False, "error_code": 401, "description": "Unauthorized"}]}


async def first_turn(value, first, delivery="telegram-operation:r"):
    ready = value._ready_client

    async def no_token():
        return None
    value._ready_client = no_token if first == "no_token" else ready
    value.submitter.hold = HELD if first == "held" else ""
    assert await value.process_one_outbox()
    value._ready_client, value.submitter.hold = ready, ""
    ready_again(value.store)
    assert value.store.delivery_receipt(delivery)["state"] in {"pending", "held"}


def old_turn(store, last_error, delivery="telegram-operation:r"):
    """One claim of 0.9.0 and how it ended, in the statements of 0.9.0's own store, due again at once.

    The claim counts an attempt; a hold gives it back, anything else is released with 0.9.0's error. 0.9.0's
    checkpoint keeps every payload key it does not know, so this version's stamp stays as it was.
    """
    now = time.time()
    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "UPDATE outbox SET state='leased', lease_until=?, attempts=attempts+1, last_error='' WHERE delivery_id=? "
            "AND ((state='pending' AND available_at<=?) OR (state='leased' AND lease_until<=?))",
            (now + 120, delivery, now, now)).rowcount == 1
        if last_error.startswith("Held:"):
            db.execute("UPDATE outbox SET state='pending',available_at=?,lease_until=0,last_error=?,"
                       "attempts=MAX(0,attempts-1) WHERE delivery_id=? AND state='leased'", (0, last_error, delivery))
        else:
            db.execute("UPDATE outbox SET state='pending', available_at=?, lease_until=0, last_error=? "
                       "WHERE delivery_id=? AND state='leased'", (0, last_error, delivery))


@pytest.mark.parametrize("first", list(FIRST))
@pytest.mark.parametrize("old_error", [OLD_408, OLD_502, OLD_MALFORMED])
def test_attempt_0_9_made_after_a_rollback_is_never_followed_by_another_call(tmp_path, first, old_error):
    """This version stamped the row, 0.9.0 then asked Telegram and kept no answer, and this version is back."""
    async def run():
        provider = Provider([*FIRST[first], {"ok": True, "result": {"message_id": 8, "text": "next"}}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        follower(value.store, chat_id="55")
        await first_turn(value, first)
        old_turn(value.store, old_error)
        assert await value.process_one_outbox() and await value.process_one_outbox()
        assert len(provider_reactions(provider)) == len(FIRST[first])
        receipt = value.store.delivery_receipt(delivery)
        assert receipt["state"] == "uncertain" and "send_result_unrecorded" in receipt["error"]
        assert value.store.delivery_receipt("telegram-send:next")["state"] == "delivered"
        value.store.release_own_leases()
        ready_again(value.store)
        assert not await value.process_one_outbox() and len(provider_reactions(provider)) == len(FIRST[first])
    asyncio.run(run())


@pytest.mark.parametrize("first", list(FIRST))
@pytest.mark.parametrize("old_error", [OLD_TOKEN, OLD_GETME, OLD_429, HELD])
def test_rollback_turn_that_proves_no_call_leaves_the_reaction_to_send_once(tmp_path, first, old_error):
    async def run():
        provider = Provider([*FIRST[first], {"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        await first_turn(value, first)
        old_turn(value.store, old_error)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "delivered"
        assert len(provider_reactions(provider)) == len(FIRST[first]) + 1
    asyncio.run(run())


@pytest.mark.parametrize("turns", [(OLD_408, OLD_TOKEN), (OLD_502, HELD), (OLD_408, HELD, HELD), (OLD_429, OLD_429)])
def test_several_0_9_turns_past_the_stamp_are_unknown_whatever_the_newest_error(tmp_path, turns):
    """0.9.0 keeps only its newest error, so past the stamp it is judged as on a direct upgrade."""
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        await first_turn(value, "no_token")
        for error in turns:
            old_turn(value.store, error)
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "uncertain" and provider.calls == []
    asyncio.run(run())


def test_hold_gives_its_attempt_back_and_the_stamp_follows_the_count(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        value.submitter.hold = HELD
        for _ in range(3):
            assert await value.process_one_outbox()
            ready_again(value.store)
            assert value.store.delivery_receipt(delivery)["attempts"] == 0
            assert value.store.outbox_payload(delivery)["_attempts_marked"] == 0
        value.submitter.hold = ""
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt(delivery)["state"] == "delivered" and len(provider_reactions(provider)) == 1
        # Rows without a stamp keep their payload byte for byte through a hold.
        value.store.enqueue_outbox("telegram-send:x", {"kind": "message", "chat_id": "56", "text": "x"})
        lease = value.store.claim_outbox(lease_sec=120)
        with sqlite3.connect(value.store.path) as db:
            before = db.execute("SELECT payload_json FROM outbox WHERE delivery_id=?", (lease.delivery_id,)).fetchone()
            value.store.hold_outbox(lease.delivery_id, HELD)
            after = db.execute("SELECT payload_json, attempts FROM outbox WHERE delivery_id=?", (lease.delivery_id,)).fetchone()
        assert after == (before[0], 0)
    asyncio.run(run())


@pytest.mark.parametrize("old_error,calls,state", [(OLD_408, 0, "uncertain"), (OLD_TOKEN, 1, "delivered")])
def test_row_0_9_left_then_this_version_stamped_is_judged_from_the_stamp_after_0_9_again(tmp_path, old_error,
                                                                                         calls, state):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        old_reaction_row(value.store)
        with sqlite3.connect(value.store.path) as db:
            db.execute("UPDATE outbox SET attempts=1,last_error=?", (OLD_429,))
        await first_turn(value, "no_token", "telegram-operation:stale")
        assert value.store.outbox_payload("telegram-operation:stale")["_attempts_marked"] == 2
        old_turn(value.store, old_error, "telegram-operation:stale")
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == state
        assert len(provider_reactions(provider)) == calls
    asyncio.run(run())


@pytest.mark.parametrize("stamp,attempts", [(True, 1), ("2", 2), (2.0, 2)])
def test_stamp_that_is_not_a_count_proves_nothing(tmp_path, stamp, attempts):
    """An unreleased build stamped rows true. A stamp that is no count is judged as if 0.9.0 left the row."""
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        payload = old_reaction_row(value.store)
        with sqlite3.connect(value.store.path) as db:
            db.execute("UPDATE outbox SET attempts=?,last_error=?,payload_json=?",
                       (attempts, OLD_408, json.dumps({**payload, "_attempts_marked": stamp})))
        assert await value.process_one_outbox()
        assert value.store.delivery_receipt("telegram-operation:stale")["state"] == "uncertain" and provider.calls == []
    asyncio.run(run())


# -- What one attempt learned survives its own record -----------------------------------------

class Faults:
    """Store methods that fail while a predicate on their call holds; each call is otherwise real."""

    def __init__(self, store):
        self.store, self.originals = store, {}

    def fail(self, name, *, times=1, when=lambda *args, **kwargs: True):
        original = self.originals.setdefault(name, getattr(self.store, name))
        left = [times]

        def failing(*args, **kwargs):
            if left[0] and when(*args, **kwargs):
                left[0] -= 1
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)
        setattr(self.store, name, failing)

    def heal(self):
        for name, original in self.originals.items():
            setattr(self.store, name, original)


def holding(key):
    return lambda delivery_id, payload, **kwargs: key in payload


def outcome(value, delivery):
    return value.store.delivery_receipt(delivery)


@pytest.mark.parametrize("name,when", [
    ("checkpoint_outbox", holding("_operation_result")),
    ("mark_delivered", lambda *args, **kwargs: True),
])
def test_recording_failure_after_telegram_said_yes_finishes_without_a_second_call(tmp_path, name, when):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        faults = Faults(value.store)
        faults.fail(name, when=when)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "pending"
        assert value.store.outbox_payload(delivery)["_operation_result"] == {"ok": True}
        ready_again(value.store)
        assert await value.process_one_outbox()
        receipt = outcome(value, delivery)
        assert receipt["state"] == "delivered" and receipt["provider_receipt"]["effect"] == "applied"
        assert len(provider_reactions(provider)) == 1
        await drain_reports(value)
        [report] = value.submitter.reports
        valid_receipt(report)
        assert report["state"] == "delivered" and report["message"]["reaction"]["effect"] == "applied"
    asyncio.run(run())


def test_success_the_store_cannot_take_yet_is_kept_by_this_process(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        faults = Faults(value.store)
        faults.fail("checkpoint_outbox", times=3, when=holding("_operation_result"))
        faults.fail("release_outbox", times=3)
        faults.fail("mark_delivered", times=3)
        assert await value.process_one_outbox()
        assert "_operation_result" not in value.store.outbox_payload(delivery)
        assert not await value.process_one_outbox()  # still leased
        expire_leases(value.store)
        faults.heal()
        assert await value.process_one_outbox()
        receipt = outcome(value, delivery)
        assert receipt["state"] == "delivered" and receipt["provider_receipt"]["effect"] == "applied"
        assert len(provider_reactions(provider)) == 1
        await drain_reports(value)
        assert [row["state"] for row in value.submitter.reports] == ["delivered"]
    asyncio.run(run())


def test_success_lost_with_its_process_is_uncertain_and_still_not_sent_again(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        faults = Faults(value.store)
        faults.fail("checkpoint_outbox", times=3, when=holding("_operation_result"))
        faults.fail("release_outbox", times=3)
        assert await value.process_one_outbox()
        faults.heal()
        reopened = world(tmp_path, provider)
        assert reopened.store.release_own_leases()["outbox"] == 1
        assert await reopened.process_one_outbox()
        assert outcome(reopened, delivery)["state"] == "uncertain"
        assert len(provider_reactions(provider)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["no_token", "client_fails", "host_fails", "hold_fails"])
def test_recorded_result_finishes_without_the_token_the_host_or_telegram(tmp_path, damage):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        lease = value.store.claim_outbox(lease_sec=120)
        recorded = {**lease.payload, "_provider_call_started": True, "_operation_result": {"ok": True},
                    "_reporting": {"version": 1, "account_id": BOT, "explicit_completion": 1, "origin": {"kind": "tool"}}}
        value.store.checkpoint_outbox(delivery, recorded)
        value.store.release_outbox(delivery, reason="TelegramApiError: recording failed", retry_after_sec=0)

        async def broken(*_args):
            raise PresenceHostError("down")
        if damage == "no_token":
            value.token_provider = lambda: ""
        elif damage == "client_fails":
            value._ready_client = broken
        elif damage == "host_fails":
            value.submitter.discover_delivery_support = broken
        else:
            value.submitter.place_hold = broken
        assert await value.process_one_outbox()
        receipt = outcome(value, delivery)
        assert receipt["state"] == "delivered" and receipt["provider_receipt"]["effect"] == "applied"
        assert provider.calls == []
        await drain_reports(value)
    asyncio.run(run())


def test_marker_that_cannot_be_recorded_proves_no_call_and_the_retry_is_fresh(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        Faults(value.store).fail("checkpoint_outbox", when=holding("_provider_call_started"))
        assert await value.process_one_outbox()
        assert provider.calls == [] and outcome(value, delivery)["state"] == "pending"
        assert "_provider_call_started" not in value.store.outbox_payload(delivery)
        ready_again(value.store)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "delivered" and len(provider_reactions(provider)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("answer", [
    {"ok": False, "error_code": 408, "description": "Request Timeout"},
    {"ok": False, "error_code": 409, "description": "Conflict"},
    {"ok": False, "error_code": 302, "description": "Found"},
    {"ok": False, "error_code": "abc", "description": "malformed code"},
    {"ok": False, "error_code": ["x"], "description": "malformed code"},
    {"ok": False, "error_code": float("inf"), "description": "malformed code"},
    {"ok": True, "result": False},
    ["not", "an", "object"],
])
def test_any_answer_that_does_not_prove_no_effect_is_uncertain_and_never_sent_again(tmp_path, answer):
    async def run():
        provider = Provider([answer])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "uncertain"
        ready_again(value.store)
        assert not await value.process_one_outbox()
        assert len(provider_reactions(provider)) == 1
        await drain_reports(value)
        [report] = value.submitter.reports
        valid_receipt(report)
        assert report["state"] == "uncertain" and "effect" not in report["message"]["reaction"]
    asyncio.run(run())


@pytest.mark.parametrize("code", ["abc", ["x"], float("inf"), None])
def test_client_turns_a_malformed_error_code_into_an_unknown_one(code):
    with pytest.raises(TelegramApiError) as caught:
        TelegramClient._result("setMessageReaction", {"ok": False, "error_code": code, "description": "bad"})
    assert caught.value.error_code == 0


@pytest.mark.parametrize("code", [401, 404, 429])
def test_answers_that_prove_no_effect_retry_and_clear_the_marker(tmp_path, code):
    async def run():
        provider = Provider([{"ok": False, "error_code": code, "description": "not done"}, {"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "pending"
        assert "_provider_call_started" not in value.store.outbox_payload(delivery)
        ready_again(value.store)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "delivered" and len(provider_reactions(provider)) == 2
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["transport", "bad_gateway", "hold_read"])
def test_failure_before_any_call_is_not_an_unknown_outcome(tmp_path, failure):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        ready, hold = value._ready_client, value.submitter.place_hold

        async def no_identity():
            raise TelegramApiError("getMe", "ConnectError", 502 if failure == "bad_gateway" else 0)

        async def no_place(*_args):
            raise PresenceHostError("down")
        if failure == "hold_read":
            value.submitter.place_hold = no_place
        else:
            value._ready_client = no_identity
        assert await value.process_one_outbox()
        assert provider.calls == [] and outcome(value, delivery)["state"] == "pending"
        value._ready_client, value.submitter.place_hold = ready, hold
        ready_again(value.store)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "delivered" and len(provider_reactions(provider)) == 1
    asyncio.run(run())


def test_attempts_used_up_before_any_call_settle_failed_not_uncertain(tmp_path):
    async def run():
        provider = Provider()
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        value.token_provider = lambda: ""
        for _ in range(5):
            assert await value.process_one_outbox()
            ready_again(value.store)
        receipt = outcome(value, delivery)
        assert receipt["state"] == "failed" and provider.calls == []
        assert "token is unavailable" in receipt["error"]
    asyncio.run(run())


def test_no_effect_answer_whose_record_fails_stays_uncertain_rather_than_risk_a_repeat(tmp_path):
    async def run():
        provider = Provider([{"ok": False, "error_code": 429, "description": "Too Many Requests"}])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        Faults(value.store).fail("checkpoint_outbox", when=lambda _id, payload, **_: "_provider_call_started" not in payload
                                 and "explicit_completion" in payload["_reporting"] and provider.calls)
        assert await value.process_one_outbox()
        assert value.store.outbox_payload(delivery)["_provider_call_started"] is True
        expire_leases(value.store)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "uncertain" and len(provider_reactions(provider)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("answer", [{"ok": False, "error_code": 502, "description": "Bad Gateway"},
                                    {"ok": False, "error_code": 400, "description": "Bad Request"}])
def test_terminal_answer_whose_settlement_fails_leaves_the_worker_running_and_no_repeat(tmp_path, answer):
    async def run():
        provider = Provider([answer])
        value = world(tmp_path, provider)
        delivery = queue_reaction(value)
        Faults(value.store).fail("mark_outbox_failed")
        assert await value.process_one_outbox()
        assert value.store.outbox_payload(delivery)["_provider_call_started"] is True
        expire_leases(value.store)
        assert await value.process_one_outbox()
        assert outcome(value, delivery)["state"] == "uncertain" and len(provider_reactions(provider)) == 1
    asyncio.run(run())


def test_edit_keeps_its_own_failure_handling(tmp_path):
    async def run():
        provider = Provider([OSError("response lost")])
        value = world(tmp_path, provider)
        result = tool(tmp_path)(method="editMessageText", chat_id="55", message_id=7, text="fixed", request_id="e")
        assert result["state"] == "queued"
        assert await value.process_one_outbox()
        assert outcome(value, "telegram-operation:e")["state"] == "uncertain"
        assert "_provider_call_started" not in value.store.outbox_payload("telegram-operation:e")
    asyncio.run(run())


# -- The target a reaction names --------------------------------------------------------------

def album_member(message_id, *, update_id=20):
    update = said(update_id=update_id, message_id=message_id)
    update["message"]["media_group_id"] = "album-1"
    return update


def test_album_member_keeps_the_requested_target_and_leaves_the_effective_one_unknown(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}])
        value = world(tmp_path, provider)
        commit(value.store, album_member(7))
        commit(value.store, album_member(8, update_id=21))
        delivery = queue_reaction(value, chat_id="-200", message_id=8)
        assert await value.process_one_outbox()
        [call] = provider_reactions(provider)
        assert call["message_id"] == 8
        receipt = outcome(value, delivery)
        assert receipt["provider_receipt"]["album"] == {"media_group_id": "album-1", "effective_message_id": "unknown"}
        await drain_reports(value)
        [report] = value.submitter.reports
        valid_receipt(report)
        fact = report["message"]["reaction"]
        assert fact["target"] == {"message_id": "8", "resolution": "resolved"} and fact["effect"] == "applied"
        assert report["message"]["album"] == {"media_group_id": "album-1", "effective_message_id": "unknown"}
        assert "7" not in json.dumps(report["message"]["album"])
    asyncio.run(run())


def test_album_fact_is_not_invented_for_a_single_or_unknown_message(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": True}, {"ok": True, "result": True}])
        value = world(tmp_path, provider)
        commit(value.store, said(message_id=7))
        retained = queue_reaction(value, request_id="a", chat_id="-200", message_id=7)
        unknown = queue_reaction(value, request_id="b", chat_id="-200", message_id=999)
        assert await value.process_one_outbox() and await value.process_one_outbox()
        await drain_reports(value)
        assert all("album" not in outcome(value, key)["provider_receipt"] for key in (retained, unknown))
        assert all("album" not in row["message"] for row in value.submitter.reports)
    asyncio.run(run())


def test_explicit_topic_of_an_unknown_private_chat_is_kept_unproven(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    operate = tool(tmp_path)
    arguments = dict(method="setMessageReaction", reaction=[THUMB])
    assert operate(chat_id="55", message_id=1, topic_id=9, request_id="a", **arguments)["state"] == "queued"
    queued = store.outbox_payload("telegram-operation:a")
    assert queued["_reaction_target"]["resolution"] == "unresolved" and queued["topic_id"] == "9"
    # Without a topic it stays the one ordinary conversation.
    assert operate(chat_id="55", message_id=2, request_id="b", **arguments)["state"] == "queued"
    assert store.outbox_payload("telegram-operation:b")["_reaction_target"]["resolution"] == "resolved"
    # A private chat's newest retained message having no topic says nothing about an older one.
    commit(store, said(chat=PRIVATE, message_id=3))
    assert operate(chat_id="55", message_id=4, topic_id=9, request_id="c", **arguments)["state"] == "queued"
    assert store.outbox_payload("telegram-operation:c")["_reaction_target"]["resolution"] == "unresolved"
    # An exact record still decides, and a group known to have no topics still refuses one.
    wrong = operate(chat_id="55", message_id=3, topic_id=9, request_id="d", **arguments)
    assert wrong["ok"] is False and "forum_topic_mismatch" in wrong["error"]
    commit(store, said(update_id=30, message_id=7))
    group = operate(chat_id="-200", message_id=999, topic_id=9, request_id="e", **arguments)
    assert group["ok"] is False and "no topics" in group["error"]


def test_later_message_outside_topics_does_not_undo_a_private_chats_topics(tmp_path):
    async def run():
        provider = Provider([{"ok": True, "result": {"message_id": 9, "text": "mine", "chat": PRIVATE}}])
        value = world(tmp_path, provider)
        store, operate, arguments = value.store, tool(tmp_path), dict(method="setMessageReaction", reaction=[THUMB])
        commit(store, said(chat=PRIVATE, topic=5, message_id=7))
        commit(store, said(21, chat=PRIVATE, message_id=8))
        # This bot's own message outside a topic says no more than anyone else's.
        store.enqueue_outbox("telegram-send:mine", {"kind": "message", "chat_id": "55", "text": "mine", "markdown": False})
        assert await value.process_one_outbox()
        assert store.reaction_target(BOT, "55", 999) == {"retained": False, "forum": 1}
        commit(store, reacted(30, chat=PRIVATE, message_id=999))
        unknown = stored(store, 30)
        assert unknown["thread_id"] == "" and unknown["message"]["reaction"]["target"]["resolution"] == "unresolved"
        refused = operate(chat_id="55", message_id=999, request_id="a", **arguments)
        assert refused["ok"] is False and refused["error"].startswith("forum_topic_unknown")
        assert "has shown topics" in refused["error"] and "Not sent" in refused["error"]
        assert store.delivery_receipt("telegram-operation:a") == {"state": "not_found"}
        assert operate(chat_id="55", message_id=999, topic_id=5, request_id="b", **arguments)["state"] == "queued"
        explicit = store.outbox_payload("telegram-operation:b")
        assert explicit["topic_id"] == "5" and explicit["_reaction_target"]["resolution"] == "unresolved"
        # Exact records still decide, for the ordinary message, this bot's own and the topic message.
        for message_id, request_id in ((8, "c"), (9, "d")):
            assert operate(chat_id="55", message_id=message_id, request_id=request_id, **arguments)["state"] == "queued"
            assert store.outbox_payload(f"telegram-operation:{request_id}")["_reaction_target"]["resolution"] == "resolved"
        assert "forum_topic_mismatch" in operate(chat_id="55", message_id=7, request_id="e", **arguments)["error"]
        commit(store, reacted(31, chat=PRIVATE, message_id=8))
        assert stored(store, 31)["message"]["reaction"]["target"]["resolution"] == "resolved"
        # A private chat that has shown no topic stays one conversation.
        commit(store, said(22, chat={**PRIVATE, "id": 56}, message_id=8))
        assert operate(chat_id="56", message_id=999, request_id="f", **arguments)["state"] == "queued"
        assert store.outbox_payload("telegram-operation:f")["_reaction_target"]["resolution"] == "resolved"
    asyncio.run(run())


# -- Status cost and capability lifetime ------------------------------------------------------

def test_held_report_count_reads_only_rows_with_reports_due(tmp_path):
    from telegram_bot.custody import _HELD_REPORTS
    store = CustodyStore(tmp_path / "custody.sqlite3")
    with sqlite3.connect(store.path) as db:
        plan = " ".join(str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + _HELD_REPORTS))
    assert "outbox_reports_due" in plan and "SCAN outbox" not in plan
    # A row whose reports were acknowledged long ago is not read, whatever its old entry says.
    entries = [{"payload": {"part_id": "status", "state": "uncertain"}, "acked": True, "attempts": 1,
                "available_at": 1, "error": "Held: an old error"}]
    store.enqueue_outbox("telegram-operation:old", {"kind": "operation", "chat_id": "1"})
    store.enqueue_outbox("telegram-operation:held", {"kind": "operation", "chat_id": "2"})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET reports_json=?,report_due_at=0 WHERE delivery_id='telegram-operation:old'",
                   (json.dumps(entries),))
        db.execute("UPDATE outbox SET reports_json=?,report_due_at=5 WHERE delivery_id='telegram-operation:held'",
                   (json.dumps([{**entries[0], "acked": False}]),))
    assert store.status_snapshot()["delivery_reports_held"] == 1


class Counted(CustodyStore):
    """The same store; its connections also count SQLite VM steps and keep the statements they ran."""
    steps, statements = 0, []

    @contextmanager
    def _connect(self):
        with super()._connect() as conn:
            conn.set_progress_handler(Counted.step, 1)
            conn.set_trace_callback(Counted.statements.append)
            yield conn

    @staticmethod
    def step():
        Counted.steps += 1
        return 0


def test_private_topic_lookup_reads_no_message_outside_topics(tmp_path):
    """However long a private chat's history outside topics, finding that it has shown topics reads none of it."""
    from telegram_bot.custody import _REMEMBER_MESSAGE
    costs = {}
    for size in (10, 2000):
        store = Counted(tmp_path / str(size) / "custody.sqlite3")
        commit(store, said(chat=PRIVATE, topic=5, message_id=1))
        commit(store, said(21, chat=PRIVATE, message_id=2))
        with sqlite3.connect(store.path) as db:
            ordinary = db.execute("SELECT * FROM telegram_messages WHERE message_id=2").fetchone()
            db.executemany(_REMEMBER_MESSAGE, [(*ordinary[:2], message_id, *ordinary[3:])
                                               for message_id in range(3, size)])
        Counted.steps, Counted.statements = 0, []
        # The tool's lookup, then the poll path's inside its write transaction.
        assert store.reaction_target(BOT, "55", 10 ** 6) == {"retained": False, "forum": 1}
        commit(store, reacted(30, chat=PRIVATE, message_id=10 ** 6))
        assert stored(store, 30)["message"]["reaction"]["target"]["resolution"] == "unresolved"
        costs[size] = Counted.steps
        lookups = [text for text in Counted.statements if "chat_forum IS NOT NULL" in text]
        with sqlite3.connect(store.path) as db:
            plans = [" ".join(str(row[3]) for row in db.execute(
                "EXPLAIN QUERY PLAN " + text, (BOT, "55") if "?" in text else ())) for text in lookups]
        assert len(plans) == 2 and all("telegram_messages_forum" in plan for plan in plans), plans
    assert costs[2000] - costs[10] < 200, costs


def test_store_whose_messages_predate_the_topic_index_gains_it_when_opened(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(chat=PRIVATE, topic=5, message_id=7))
    commit(store, said(21, chat=PRIVATE, message_id=8))
    with sqlite3.connect(store.path) as db:
        db.execute("DROP INDEX telegram_messages_forum")  # the table as it was before this index
    before = [store.reaction_target(BOT, "55", message_id) for message_id in (7, 8, 999)]
    for _ in range(2):  # the first open builds it from the rows already there; the next finds it
        reopened = CustodyStore(store.path)
        with sqlite3.connect(store.path) as db:
            indexes = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='telegram_messages'")]
        assert indexes.count("telegram_messages_forum") == 1
        assert [reopened.reaction_target(BOT, "55", message_id) for message_id in (7, 8, 999)] == before
    assert before[2] == {"retained": False, "forum": 1}


def test_status_route_reads_the_store_once(tmp_path, monkeypatch):
    from test_plugin import FakeApi
    skill = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("telegram_reactions_status", skill / "plugin.py",
                                                  submodule_search_locations=[str(skill)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    real, reads = module._RUNTIME.store.status_snapshot, []
    monkeypatch.setattr(module._RUNTIME.store, "status_snapshot", lambda: reads.append(1) or real())
    module._status_payload()
    assert len(reads) == 1
    api.unload()


def test_capability_lives_as_long_as_the_connector_that_read_it(tmp_path):
    """SKILL.md's restart boundary: a supported or unsupported answer is kept; only failure asks again."""
    class Transport:
        def __init__(self, identity):
            self.identity, self.reads = identity, 0

        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            self.reads += 1
            return self.identity

    def client(transport):
        return PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "t", host_base="http://127.0.0.1:1",
                                  transport=transport)

    async def run():
        transport = Transport(OLD_HOST)
        running = client(transport)
        await running.discover_delivery_support()
        transport.identity = CAPABLE
        await running.discover_delivery_support()
        assert running.presence_explicit_completion == 0 and transport.reads == 1
        restarted = client(transport)
        await restarted.discover_delivery_support()
        assert restarted.presence_explicit_completion == 1
    asyncio.run(run())
