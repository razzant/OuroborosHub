from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from lib.events import parse_socket_envelope
from lib.host_adapter import HostDelivery, HostTurnStatus, slack_presence_event
from lib.runtime import InboundWorker
from lib.socket_mode import SocketModeClient
from lib.store import BridgeStore


def _payload(kind, event_id, *, team="T1", channel="C1", ts="2.0", text="<@U_BOT> question"):
    return {
        "type": "events_api", "envelope_id": f"env-{event_id}",
        "payload": {"team_id": team, "event_id": event_id, "event": {
            "type": kind, "user": "U1", "channel": channel, "ts": ts,
            "thread_ts": "1.0", "text": text,
        }},
    }


def _ingest(store, payload):
    return store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))


def _edited_payload(kind, event_id, revision="3.0", *, text="<@U_BOT> question"):
    payload = _payload(kind, event_id, text=text)
    event = payload["payload"]["event"]
    message = {"user": "U1", "ts": "2.0", "thread_ts": "1.0", "text": text,
               "edited": {"user": "U1", "ts": revision}}
    if kind == "message":
        event.update(subtype="message_changed", message=message, event_ts=revision,
                     previous_message={**message, "text": "previous text"})
    else:
        event.update(message)
    return payload


@pytest.mark.parametrize("first_type", ["message", "app_mention"])
@pytest.mark.parametrize("edited", [False, True])
def test_both_socket_surfaces_have_custody_but_submit_one_presence_turn(tmp_path, monkeypatch, first_type, edited):
    async def context(*_args):
        return {}

    monkeypatch.setattr("lib.runtime.capture_context", context)
    store = BridgeStore(tmp_path)
    payload = _edited_payload if edited else _payload
    payloads = [payload(first_type, "Ev-first"),
                payload("app_mention" if first_type == "message" else "message", "Ev-second")]
    acknowledgements = []
    submissions = []

    class WebSocket:
        async def send(self, value):
            # Each distinct provider event is durable before its own ACK.
            with sqlite3.connect(store.path) as db:
                assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == len(acknowledgements) + 1
            acknowledgements.append(json.loads(value))

    class Host:
        available = True

        async def submit(self, item):
            submissions.append(slack_presence_event(item))
            return "turn-1"

        async def status(self, _reference):
            return HostTurnStatus("ready")

        async def deliver(self, _reference):
            return HostDelivery()

    async def run():
        socket = SocketModeClient(object(), store, bot_user_id="U_BOT")
        for payload in payloads:
            await socket.handle_raw_message(WebSocket(), json.dumps(payload))
        worker = InboundWorker(store, object(), Host(), staged_root=tmp_path / "staged")
        assert await worker.process_once()
        assert not await worker.process_once()

    asyncio.run(run())
    assert acknowledgements == [{"envelope_id": payload["envelope_id"]} for payload in payloads]
    assert len(submissions) == 1
    assert submissions[0]["text"] == "<@U_BOT> question"
    assert submissions[0]["source_event_id"] == "Ev-first"
    assert submissions[0]["message"]["event_type"] == first_type
    assert submissions[0]["message"]["provider_facts"]["mentioned_user_ids"] == ["U_BOT"]
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT raw_json, state, ignored_reason FROM inbox ORDER BY id").fetchall()
    assert [json.loads(row[0]) for row in rows] == payloads
    assert rows[0][1:] == ("delivered", "")
    assert rows[1][1:] == ("ignored", "duplicate_message_surface")


@pytest.mark.parametrize("first_type", ["message", "app_mention"])
def test_duplicate_after_delivery_and_restart_retains_old_row_and_retry_identity(tmp_path, first_type):
    store = BridgeStore(tmp_path)
    first = _payload(first_type, "Ev-first")
    first_id, _ = _ingest(store, first)
    item = store.claim_inbox()
    store.complete_inbox(item.row_id, item.lease_token)
    # Older databases have the ordinary inbox records but no new lookup index.
    with sqlite3.connect(store.path) as db:
        db.execute("DROP INDEX inbox_message")
    store = BridgeStore(tmp_path)
    second = _payload("app_mention" if first_type == "message" else "message", "Ev-second")
    second_id, inserted = _ingest(store, second)
    assert inserted and second_id != first_id
    second["envelope_id"] = "env-retry"
    assert _ingest(BridgeStore(tmp_path), second) == (second_id, False)
    assert store.claim_inbox() is None
    assert store.status()["inbox_delivered"] == 1
    assert store.status()["inbox_ignored"] == 1


def test_standalone_edited_mention_is_admitted_with_its_provider_revision(tmp_path):
    store = BridgeStore(tmp_path)
    payload = _edited_payload("app_mention", "Ev-mention")
    _ingest(store, payload)
    item = store.claim_inbox()
    assert item is not None and item.text == "<@U_BOT> question"
    assert item.structured["edited"] == {"user": "U1", "ts": "3.0"}
    assert store.claim_inbox() is None


@pytest.mark.parametrize("first_type", ["message", "app_mention"])
def test_later_edit_and_revert_are_new_revisions_even_when_text_matches(tmp_path, first_type):
    store = BridgeStore(tmp_path)
    other_type = "app_mention" if first_type == "message" else "message"
    _ingest(store, _payload(first_type, "Ev-original"))
    _ingest(store, _payload(other_type, "Ev-original-duplicate"))
    for revision, text in [("3.0", "<@U_BOT> changed question"), ("4.0", "<@U_BOT> question")]:
        _ingest(store, _edited_payload(first_type, f"Ev-{revision}", revision, text=text))
        _ingest(store, _edited_payload(other_type, f"Ev-{revision}-duplicate", revision, text=text))
    assert store.status()["inbox_pending"] == 3
    assert store.status()["inbox_ignored"] == 3
    events = []
    while item := store.claim_inbox():
        events.append(item.event_id)
        store.complete_inbox(item.row_id, item.lease_token)
    assert events == ["Ev-original", "Ev-3.0", "Ev-4.0"]


def test_edit_without_native_revision_uses_event_timestamp(tmp_path):
    store = BridgeStore(tmp_path)
    _ingest(store, _payload("app_mention", "Ev-original"))
    edit = _edited_payload("message", "Ev-edit", "3.0")
    del edit["payload"]["event"]["message"]["edited"]
    _ingest(store, edit)
    _ingest(store, _edited_payload("app_mention", "Ev-edit-mention", "3.0"))
    assert store.status()["inbox_pending"] == 2
    assert store.status()["inbox_ignored"] == 1


@pytest.mark.parametrize("difference", [
    {"team": "T2"}, {"channel": "C2"}, {"ts": "3.0"}, {"text": "<@U_BOT> edited question"},
])
def test_distinct_identity_or_changed_text_remains_an_independent_message(tmp_path, difference):
    store = BridgeStore(tmp_path)
    _ingest(store, _payload("message", "Ev-first"))
    _ingest(store, _payload("app_mention", "Ev-second", **difference))
    assert store.status()["inbox_pending"] == 2
    assert store.status()["inbox_ignored"] == 0


@pytest.mark.parametrize("kind", ["message_changed", "message_deleted", "reaction_added", "reaction_removed"])
@pytest.mark.parametrize("update_first", [False, True])
def test_message_updates_and_reactions_are_not_duplicate_mention_surfaces(tmp_path, kind, update_first):
    store = BridgeStore(tmp_path)
    mention = _payload("app_mention", "Ev-mention")
    update = _payload("message", "Ev-update")
    event = update["payload"]["event"]
    original = {"user": "U1", "ts": "2.0", "thread_ts": "1.0", "text": "<@U_BOT> question"}
    if kind == "message_changed":
        event.update(subtype=kind, message=original, previous_message={**original, "text": "previous"})
    elif kind == "message_deleted":
        event.update(subtype=kind, deleted_ts="2.0", previous_message=original)
    else:
        event.update(type=kind, reaction="eyes", item={"type": "message", "channel": "C1", "ts": "2.0"})
    for payload in ([update, mention] if update_first else [mention, update]):
        assert parse_socket_envelope(payload).accepted
        _ingest(store, payload)
    assert store.status()["inbox_pending"] == 2


def test_self_mention_remains_ignored_and_unaddressed_messages_remain_accepted(tmp_path):
    store = BridgeStore(tmp_path)
    own = _payload("app_mention", "Ev-own")
    own["payload"]["event"]["user"] = "U_BOT"
    _ingest(store, own)
    _ingest(store, _payload("message", "Ev-ordinary", text="colleagues talking"))
    item = store.claim_inbox()
    assert item is not None and item.event_id == "Ev-ordinary"
    assert store.status()["inbox_ignored"] == 1


@pytest.mark.parametrize("first_type", ["message", "app_mention"])
def test_opposite_surface_while_first_is_leased_does_not_start_another_turn(tmp_path, first_type):
    store = BridgeStore(tmp_path)
    _ingest(store, _payload(first_type, "Ev-first"))
    first = store.claim_inbox()
    assert first is not None
    _ingest(store, _payload("app_mention" if first_type == "message" else "message", "Ev-second"))
    store.complete_inbox(first.row_id, first.lease_token)
    assert store.claim_inbox() is None
    assert store.status()["inbox_ignored"] == 1


def test_metadata_update_with_an_old_text_edit_stamp_remains_new_work(tmp_path):
    store = BridgeStore(tmp_path)
    _ingest(store, _edited_payload("app_mention", "Ev-edit-mention", "3.0"))
    update = _edited_payload("message", "Ev-unfurl", "3.0")
    event = update["payload"]["event"]
    event["event_ts"] = "4.0"
    event["previous_message"] = dict(event["message"])
    event["message"]["attachments"] = [{"text": "New provider attachment"}]
    _ingest(store, update)
    assert store.status()["inbox_pending"] == 2
    assert store.status()["inbox_ignored"] == 0
