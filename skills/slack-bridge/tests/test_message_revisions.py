from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest

from lib.events import parse_socket_envelope
from lib.socket_mode import SocketModeClient
from lib.store import BridgeStore


def _message():
    return {
        "type": "message", "user": "U_ACTOR", "ts": "123.456",
        "text": "Please check this document.", "blocks": [],
        "language": {"locale": "en", "is_reliable": True},
    }


def _envelope(event_id, message, *, previous=None):
    event = {**deepcopy(message), "channel": "C_ROOM", "channel_type": "channel"}
    if previous is not None:
        event = {
            "type": "message", "subtype": "message_changed",
            "channel": "C_ROOM", "channel_type": "channel",
            "message": deepcopy(message), "previous_message": deepcopy(previous),
        }
    return {
        "type": "events_api", "envelope_id": f"env-{event_id}",
        "payload": {"team_id": "T_WORKSPACE", "event_id": event_id, "event": event},
    }


@pytest.mark.parametrize("change", ["language", "edited", "both", "identical"])
def test_bookkeeping_only_revision_does_not_create_another_turn(change):
    previous = _message()
    current = deepcopy(previous)
    if change in {"language", "both"}:
        current["language"]["locale"] = "ru"
    if change in {"edited", "both"}:
        current["edited"] = {"user": "U_ACTOR", "ts": "124.0"}

    original = parse_socket_envelope(_envelope("Ev-original", previous))
    update = _envelope("Ev-classification", current, previous=previous)
    before = deepcopy(update)
    parsed = parse_socket_envelope(update)

    assert original.accepted
    assert not parsed.accepted and parsed.event is None
    assert parsed.reason == "message_content_unchanged"
    assert parsed.event_id == "Ev-classification"
    assert update == before


@pytest.mark.parametrize("field,old,new", [
    ("text", "first question", "corrected question"),
    ("text", "removed text", ""),
    ("blocks", [{"type": "section", "text": {"type": "plain_text", "text": "old"}}],
     [{"type": "section", "text": {"type": "plain_text", "text": "new"}}]),
    ("blocks", [{"type": "divider"}], []),
    ("attachments", [{"text": "old attachment"}], [{"text": "new attachment"}]),
    ("attachments", [{"text": "attachment"}], []),
    ("files", [{"id": "F1", "url_private": "https://files.slack.com/first"}],
     [{"id": "F2", "url_private": "https://files.slack.com/second"}]),
    ("files", [{"id": "F1", "url_private": "https://files.slack.com/first"}], []),
    ("future_content", {"value": "old"}, {"value": "new"}),
    ("user", "U_ACTOR", "U_OTHER"),
])
def test_real_changes_with_same_timestamp_still_reach_the_model(field, old, new):
    previous = _message()
    previous[field] = old
    current = deepcopy(previous)
    current[field] = new
    current["language"]["locale"] = "ru"
    current["edited"] = {"user": "U_ACTOR", "ts": "124.0"}

    parsed = parse_socket_envelope(_envelope("Ev-edit", current, previous=previous))

    assert parsed.accepted and parsed.event is not None
    assert parsed.event.message_ts == previous["ts"]
    assert parsed.event.structured["message"][field] == new
    assert parsed.event.structured["previous_message"][field] == old


@pytest.mark.parametrize("field", ["future_content", "metadata", "blocks", "attachments", "files"])
@pytest.mark.parametrize("old,new", [(False, 0), (True, 1), (1, 1.0)])
def test_json_type_changes_remain_claimable(tmp_path, field, old, new):
    previous = _message()
    previous[field] = [{"nested": {"value": old}}]
    current = deepcopy(previous)
    current[field] = [{"nested": {"value": new}}]
    current["language"]["locale"] = "ru"
    envelope = _envelope("Ev-type-edit", current, previous=previous)
    parsed = parse_socket_envelope(envelope)
    assert parsed.accepted
    store = BridgeStore(tmp_path)
    store.ingest_envelope(envelope, parsed)
    assert store.claim_inbox().event_id == "Ev-type-edit"


@pytest.mark.parametrize("previous", [None, {}, {"ts": "123.456", "user": "U_ACTOR"}])
def test_missing_comparison_content_is_not_treated_as_unchanged(previous):
    envelope = _envelope("Ev-edit", _message(), previous={})
    envelope["payload"]["event"]["previous_message"] = previous
    assert parse_socket_envelope(envelope).accepted


def test_different_message_timestamp_and_blocks_only_content():
    previous = _message()
    current = {**previous, "ts": "125.0"}
    assert parse_socket_envelope(_envelope("Ev-other", current, previous=previous)).accepted

    previous.pop("text")
    previous["blocks"] = [{"type": "section", "text": {"type": "plain_text", "text": "visible"}}]
    current = deepcopy(previous)
    current["language"]["locale"] = "ru"
    assert parse_socket_envelope(_envelope("Ev-blocks", current, previous=previous)).reason == "message_content_unchanged"

    partial = {"user": "U_ACTOR", "ts": "123.456"}
    assert parse_socket_envelope(_envelope("Ev-partial", partial, previous=partial)).accepted


@pytest.mark.parametrize("revision_first", [False, True])
def test_language_update_is_durable_before_ack_but_only_original_is_claimable(tmp_path, revision_first):
    store = BridgeStore(tmp_path)
    previous = _message()
    current = deepcopy(previous)
    current["language"]["locale"] = "ru"
    original = _envelope("Ev-original", previous)
    update = _envelope("Ev-language", current, previous=previous)
    envelopes = [update, original] if revision_first else [original, update]
    retry = deepcopy(update)
    retry["envelope_id"] = "env-language-retry"
    envelopes.append(retry)
    acknowledged = []

    class WebSocket:
        async def send(self, raw):
            ack = json.loads(raw)
            sent = next(e for e in envelopes if e["envelope_id"] == ack["envelope_id"])
            event_id = sent["payload"]["event_id"]
            with sqlite3.connect(store.path) as db:
                row = db.execute("SELECT state,ignored_reason,raw_json FROM inbox WHERE event_id=?", (event_id,)).fetchone()
            assert row is not None
            if event_id == "Ev-language":
                assert row[:2] == ("ignored", "message_content_unchanged")
                assert json.loads(row[2]) == update
            else:
                assert row[0] == "pending"
            acknowledged.append(ack)

    async def receive():
        socket = SocketModeClient(object(), store, bot_user_id="U_BOT")
        for envelope in envelopes:
            await socket.handle_raw_message(WebSocket(), json.dumps(envelope))

    asyncio.run(receive())
    assert len(acknowledged) == 3
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
    claimed = store.claim_inbox()
    assert claimed.event_id == "Ev-original"
    store.complete_inbox(claimed.row_id, claimed.lease_token)
    assert store.claim_inbox() is None


def _thread_root(**fields):
    """A thread root as Slack re-sends it: the 06.11 10:31:38Z shape, after a reply under it was deleted."""
    return {
        "type": "message", "user": "U_ROOT", "ts": "1793232132.554429", "thread_ts": "1793232132.554429",
        "client_msg_id": "528a21fd", "team": "T_WORKSPACE",
        "text": "Media is not being delivered in the chat.",
        "reply_count": 27, "reply_users_count": 9, "latest_reply": "1793931322.589289",
        "reply_users": ["U_ROOT", "U_BOT", "U_OTHER"], "is_locked": False,
        "blocks": [{"type": "rich_text", "block_id": "PkhCE", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "text", "text": "Media is not being delivered in the chat."}]}]}],
        "language": {"locale": "en", "is_reliable": True}, **fields,
    }


def _regenerated(message, block_id):
    return {**message, "blocks": [{**message["blocks"][0], "block_id": block_id}]}


_ROOT = _thread_root()
_BOOKKEEPING_REVISIONS = {
    # Deleting a reply under the root: only this bot's read and follow marks on the root changed.
    "reply deleted": ({**_ROOT, "last_read": "1793931991.992449", "subscribed": True}, _ROOT),
    # Slack regenerates every block_id when it re-renders an unchanged message.
    "follow mark, regenerated blocks": ({**_ROOT, "subscribed": False}, _regenerated(_ROOT, "34EJ0")),
    # The last reply went away: the thread's own state, the root's own thread_ts included, is gone.
    "thread emptied": ({**_ROOT, "reply_count": 0, "reply_users_count": 0, "reply_users": [],
                        "latest_reply": "0000000000.000000", "subscribed": False},
                       _regenerated({key: value for key, value in _ROOT.items() if key not in {
                           "thread_ts", "reply_count", "reply_users_count", "reply_users", "latest_reply", "is_locked"}},
                           "OYy0")),
    "language detector, regenerated blocks": (_ROOT, _regenerated({**_ROOT, "language": {"locale": "ru"}}, "1Pqij")),
    "re-saved without new words": (_ROOT, _regenerated({**_ROOT, "edited": {"user": "U_ROOT", "ts": "1793891194.0"},
                                                        "source_team": "T_WORKSPACE", "user_team": "T_WORKSPACE"}, "Yg7d")),
}


@pytest.mark.parametrize("case", _BOOKKEEPING_REVISIONS)
def test_thread_bookkeeping_and_regenerated_blocks_are_not_an_edit(tmp_path, case):
    previous, current = _BOOKKEEPING_REVISIONS[case]
    envelope = _envelope("Ev-root-revision", current, previous=previous)
    parsed = parse_socket_envelope(envelope, bot_user_id="U_BOT")
    assert not parsed.accepted and parsed.reason == "message_content_unchanged"
    store = BridgeStore(tmp_path)
    store.ingest_envelope(envelope, parsed)
    assert store.claim_inbox() is None and store.status()["inbox_ignored"] == 1


@pytest.mark.parametrize("current", [
    # The same bookkeeping beside new words, an unfurl, a deleted root (a tombstone) or a reply's own thread.
    _regenerated({**_ROOT, "text": "Media is delivered again.", "edited": {"user": "U_ROOT", "ts": "1793891194.0"}}, "Yg7d"),
    {**_ROOT, "attachments": [{"id": 1, "fallback": "joi.com", "from_url": "https://joi.com/chats/1"}]},
    {**_ROOT, "subtype": "tombstone", "hidden": True, "text": "This message was deleted.", "user": "USLACKBOT"},
    {**_ROOT, "thread_ts": "1793232000.000001"},
])
def test_a_root_revision_with_new_content_still_reaches_the_model(current):
    previous = {**_ROOT, "last_read": "1793931991.992449", "subscribed": True}
    parsed = parse_socket_envelope(_envelope("Ev-root-edit", current, previous=previous), bot_user_id="U_BOT")
    assert parsed.accepted and parsed.event.structured["message"] == current
