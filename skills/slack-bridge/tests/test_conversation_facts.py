from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import slack_presence_event
from lib.provider_context import capture_context
from lib.store import BridgeStore


def _message(**fields):
    return {"type": "message", "user": "U_AUTHOR", "ts": "123.456",
            "text": "A shared update", **fields}


def _envelope(message, *, previous=None):
    event = {**message, "channel": "C_ROOM", "channel_type": "channel"}
    if previous is not None:
        event = {"type": "message", "subtype": "message_changed", "channel": "C_ROOM",
                 "message": message, "previous_message": previous}
    return {"type": "events_api", "envelope_id": "env-1",
            "payload": {"team_id": "T_WORKSPACE", "event_id": "Ev-1", "event": event}}


@pytest.mark.parametrize("self_id", ["U_SELF", ""])
@pytest.mark.parametrize("text,mentions", [
    ("<@U_OTHER> please check this", ["U_OTHER"]),
    ("<@U_SELF> please check this", ["U_SELF"]),
    ("A shared update without mentions", []),
])
def test_conversation_facts_survive_custody_without_filtering_speech(tmp_path, self_id, text, mentions):
    payload = _envelope(_message(text=text, thread_ts="100.0", parent_user_id="U_PARENT"))
    original = deepcopy(payload)
    parsed = parse_socket_envelope(payload, bot_user_id=self_id)
    assert parsed.accepted and payload == original
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parsed)
    item = BridgeStore(tmp_path).claim_inbox()
    event = slack_presence_event(item)
    assert set(event) == {"source_event_id", "provider", "account_id", "conversation_id",
                          "thread_id", "conversation_key", "actor", "conversation", "message", "text"}
    assert event["text"] == text and event["thread_id"] == "100.0"
    assert event["actor"]["platform_actor_id"] == "U_AUTHOR"
    assert event["message"]["provider_facts"] == {
        **({"self_user_id": self_id} if self_id else {}),
        **({"mentioned_user_ids": mentions} if mentions else {}),
        "parent_user_id": "U_PARENT",
    }


def test_rich_text_mentions_and_text_tokens_are_occurrences_not_name_matching():
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_quote", "elements": [
        {"type": "user", "user_id": "U_BLOCK"},
        {"type": "user", "user_id": "U_TEXT"},
        {"type": "text", "text": "U_NAME and @DisplayName"},
    ]}]}]
    payload = _envelope(_message(text="Quoted `<@U_TEXT>`; <@U_TEXT|old label>", blocks=blocks))
    original = deepcopy(payload)
    parsed = parse_socket_envelope(payload)
    assert parsed.accepted and payload == original
    assert parsed.event.structured == {"blocks": blocks, "mentioned_user_ids": ["U_BLOCK", "U_TEXT"]}


@pytest.mark.parametrize("current,expected", [
    (_message(text="<@U_NEW> revised", parent_user_id="U_PARENT_NEW"),
     {"mentioned_user_ids": ["U_NEW"], "parent_user_id": "U_PARENT_NEW"}),
    (_message(text="Revised without mention or parent"), {}),
])
def test_edit_facts_describe_current_message_without_borrowing_previous(current, expected):
    previous = _message(text="<@U_OLD> first", parent_user_id="U_PARENT_OLD")
    payload = _envelope(current, previous=previous)
    # Outer update metadata must not masquerade as the current nested message.
    payload["payload"]["event"].update(text="<@U_WRAPPER>", parent_user_id="U_WRAPPER_PARENT")
    original = deepcopy(payload)
    parsed = parse_socket_envelope(payload, bot_user_id="U_SELF")
    assert parsed.accepted and payload == original
    assert parsed.event.structured == {
        "change": "edited", "message": current, "previous_message": previous,
        "self_user_id": "U_SELF", **expected,
    }


def test_self_facts_do_not_change_empty_self_or_noop_admission():
    empty = parse_socket_envelope(_envelope(_message(text="")), bot_user_id="U_SELF")
    assert not empty.accepted and empty.reason == "empty_message"
    own = parse_socket_envelope(_envelope(_message(user="U_SELF")), bot_user_id="U_SELF")
    assert not own.accepted and own.reason == "self_message"
    previous = _message(text="<@U_SELF> check this")
    current = {**previous, "language": {"locale": "en"}}
    noop = parse_socket_envelope(_envelope(current, previous=previous), bot_user_id="U_SELF")
    assert not noop.accepted and noop.reason == "message_content_unchanged"
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
        {"type": "user", "user_id": "U_OTHER"},
    ]}]}]
    visible = parse_socket_envelope(_envelope(_message(text="", blocks=blocks)), bot_user_id="U_SELF")
    assert visible.accepted
    assert visible.event.structured == {
        "blocks": blocks, "self_user_id": "U_SELF", "mentioned_user_ids": ["U_OTHER"],
    }


@pytest.mark.parametrize("kind", ["message_deleted", "reaction_added", "reaction_removed"])
def test_updates_do_not_invent_current_message_mentions_or_parent(kind):
    if kind == "message_deleted":
        message = {"type": "message", "subtype": kind, "deleted_ts": "123.456",
                   "previous_message": _message(text="<@U_PREVIOUS>", parent_user_id="U_PARENT")}
        expected = {"change": "deleted", "deleted_ts": "123.456",
                    "previous_message": message["previous_message"], "self_user_id": "U_SELF"}
    else:
        message = {"type": kind, "user": "U_AUTHOR", "reaction": "wave",
                   "item": {"type": "message", "channel": "C_ROOM", "ts": "123.456"}}
        expected = {"reaction": {"kind": "added" if kind == "reaction_added" else "removed",
                                 "name": "wave", "user_id": "U_AUTHOR", "item": message["item"]},
                    "self_user_id": "U_SELF"}
    parsed = parse_socket_envelope(_envelope(message), bot_user_id="U_SELF")
    assert parsed.accepted and parsed.event.structured == expected


def test_old_persisted_events_keep_unknown_facts(tmp_path):
    payload = _envelope(_message())
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parse_socket_envelope(payload))
    event = slack_presence_event(BridgeStore(tmp_path).claim_inbox())
    assert event["message"]["provider_facts"] == {}
    assert event["text"] == "A shared update"


def _spoken(ts, *, channel="D1", channel_type="im", thread_ts=None, event_id=None, kind="message"):
    event = {"type": kind, "user": "U1", "channel": channel, "ts": ts, "text": "words",
             **({"channel_type": channel_type} if channel_type else {}), **({"thread_ts": thread_ts} if thread_ts else {})}
    return {"type": "events_api", "envelope_id": "env-" + ts,
            "payload": {"team_id": "T1", "event_id": event_id or "Ev-" + ts, "event": event}}


def _received(tmp_path, payload):
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parse_socket_envelope(payload))
    return store.claim_inbox()


@pytest.mark.parametrize("payload,thread", [
    # The flow of a direct or group message is one conversation; it names no thread.
    (_spoken("1.0"), ""),
    (_spoken("1.0", channel_type="mpim", channel="G1"), ""),
    # An explicit thread stays its own conversation, in a direct message too.
    (_spoken("2.0", thread_ts="1.0"), "1.0"),
    # A thread's parent stays in the flow it was posted in; in a channel it is its own thread.
    (_spoken("1.0", thread_ts="1.0"), ""),
    (_spoken("1.0", channel="C1", channel_type="channel", thread_ts="1.0"), "1.0"),
    # A top-level channel message opens its own conversation, wherever the channel's ID came from.
    (_spoken("1.0", channel="C1", channel_type="channel"), "1.0"),
    (_spoken("1.0", channel="C1", channel_type="group"), "1.0"),
    (_spoken("1.0", channel="C1", channel_type="", kind="app_mention"), "1.0"),
])
def test_one_stored_conversation_names_the_event_the_lane_and_the_reply(tmp_path, payload, thread):
    item = _received(tmp_path, payload)
    channel = payload["payload"]["event"]["channel"]
    assert item.ordering_key == f"T1:{channel}:{thread or '0'}" and item.conversation_thread == thread
    event = slack_presence_event(item)
    assert event["thread_id"] == event["conversation"]["thread_ts"] == thread
    assert event["conversation_key"] == f"slack:T1:{channel}:{thread or '0'}"


def test_direct_messages_share_the_room_a_proactive_send_lands_in(tmp_path):
    store = BridgeStore(tmp_path)
    keys = set()
    for ts in ("1.0", "2.0", "3.0"):
        store.ingest_envelope(payload := _spoken(ts), parse_socket_envelope(payload))
        item = store.claim_inbox()
        keys.add(slack_presence_event(item)["conversation_key"])
        store.complete_inbox(item.row_id, item.lease_token)
    assert keys == {"slack:T1:D1:0"}


def test_a_message_received_under_its_own_key_keeps_it_through_custody(tmp_path):
    """What an earlier version stored stays: its room, its reply thread, and with them its identities."""
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload := _spoken("1.0"), parse_socket_envelope(payload))
    with store._connect() as db:
        db.execute("UPDATE inbox SET ordering_key='T1:D1:1.0'")
    item = store.claim_inbox()
    assert item.conversation_thread == "1.0"
    assert slack_presence_event(item)["conversation_key"] == "slack:T1:D1:1.0"


@pytest.mark.parametrize("payload,reads", [(_spoken("1.0"), False), (_spoken("2.0", thread_ts="1.0"), True),
                                           (_spoken("1.0", channel="C1", channel_type="channel"), True)])
def test_a_thread_is_read_only_where_the_conversation_is_one(tmp_path, payload, reads):
    item = _received(tmp_path, payload)
    calls = []

    class Slack:
        async def user_info(self, _user): return {}
        async def conversation_info(self, _channel): return {}
        async def read_messages(self, **arguments):
            calls.append(arguments)
            return {"messages": [], "complete": True, "next_cursor": None}

    snapshot = asyncio.run(capture_context(Slack(), item, "Workspace"))
    assert ("thread" in snapshot) is reads and len(calls) == int(reads)
    if reads:
        assert calls[0]["thread_ts"] == item.conversation_thread


@pytest.mark.parametrize("info,filed", [({"is_im": True}, ""), ({"is_mpim": True}, ""), ({"is_channel": True}, "100.0")])
def test_a_reaction_on_a_top_level_message_joins_the_conversation_it_was_posted_in(tmp_path, info, filed):
    store = BridgeStore(tmp_path)
    payload = {"type": "events_api", "envelope_id": "env-r", "payload": {"team_id": "T1", "event_id": "Ev-r", "event": {
        "type": "reaction_added", "user": "U1", "reaction": "eyes", "item_user": "U_BOT",
        "item": {"type": "message", "channel": "D1", "ts": "100.0"}}}}
    store.ingest_envelope(payload, parse_socket_envelope(payload))
    item = store.claim_inbox()

    class Slack:
        async def user_info(self, _user): return {}
        async def conversation_info(self, _channel): return info
        async def read_messages(self, *_channel, **_arguments):
            return {"messages": [{"ts": "100.0", "user": "U_BOT", "text": "words"}], "complete": True, "next_cursor": None}

    snapshot = asyncio.run(capture_context(Slack(), item, "Workspace"))
    assert snapshot["reaction_target"]["thread_ts"] == filed


@pytest.mark.parametrize("stated,channel,sent_in,parts,filed", [
    # A top-level post in a channel opens the thread its replies continue: the receipt names that thread.
    ("channel", "C1", "", 1, "9.9"), ("group", "C1", "", 1, "9.9"),
    # A post split into parts keeps the thread it was sent in, so its parts stay together.
    ("channel", "C1", "", 2, ""),
    # A send into a thread stays in that thread; a send into a direct or group chat stays in its flow.
    ("channel", "C1", "5.0", 1, "5.0"), ("im", "D1", "", 1, ""), ("mpim", "G1", "", 1, ""), ("im", "D1", "5.0", 1, "5.0"),
    # A conversation Slack never described here keeps the thread the message was sent in.
    ("", "C1", "", 1, ""), ("", "D1", "", 1, ""),
])
def test_a_send_is_filed_in_the_conversation_its_replies_continue(tmp_path, stated, channel, sent_in, parts, filed):
    from lib.runtime import OutboundWorker

    store = BridgeStore(tmp_path)
    store.set_runtime(workspace_id="T1")
    if stated:
        store.ingest_envelope(heard := _spoken("1.0", channel=channel, channel_type=stated), parse_socket_envelope(heard))
    store.enqueue_outbox(request_id="send-1", target=channel, thread_ts=sent_in, chunks=["words"] * parts,
                         origin={"kind": "tool"}, delivery_reporting_version=1)

    class Slack:
        async def resolve_target(self, target): return target
        async def post_message(self, **sent): return {"ok": True, "channel": sent["channel"], "ts": "9.9"}

    assert asyncio.run(OutboundWorker(store, Slack()).process_once()) is True
    with store._connect() as db:
        report = db.execute("SELECT report_payload_json FROM outbox WHERE report_payload_json IS NOT NULL ORDER BY id").fetchone()[0]
    assert __import__("json").loads(report)["thread_id"] == filed
