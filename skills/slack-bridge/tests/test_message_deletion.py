"""A deletion names the deleted message and its original thread, not its own stamp.

Envelopes follow https://docs.slack.dev/reference/events/message/message_deleted/: the
outer ``ts``/``event_ts`` stamp the deletion itself, ``deleted_ts`` and
``previous_message`` describe the message that is gone (synthetic, not recorded).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import InboundWorker
from lib.slack_api import SlackClient
from lib.store import BridgeStore

REPLY = {"type": "message", "user": "U1", "ts": "1.0", "thread_ts": "0.5", "parent_user_id": "U2", "text": "reply"}
ROOT = {"type": "message", "user": "U1", "ts": "1.0", "text": "root"}


def _envelope(event_id: str, event: dict, *, envelope_id: str = "") -> dict:
    return {"type": "events_api", "envelope_id": envelope_id or f"env-{event_id}",
            "payload": {"event_id": event_id, "team_id": "T1", "event_time": 1600,
                        "event": {"channel": "C1", "channel_type": "channel", **event}}}


def _deletion(previous: dict, **outer) -> dict:
    event = {"type": "message", "subtype": "message_deleted", "hidden": True,
             "ts": "1.6", "event_ts": "1.6", "deleted_ts": previous["ts"], "previous_message": previous}
    return {key: value for key, value in {**event, **outer}.items() if value is not None}


def _parse(envelope: dict):
    parsed = parse_socket_envelope(envelope, bot_user_id="U_BOT")
    assert parsed.accepted, parsed.reason
    return parsed.event


@pytest.mark.parametrize("event,expected", [
    (_deletion(REPLY), ("1.0", "0.5", "1.6", "T1:C1:0.5")),
    (_deletion(ROOT), ("1.0", "", "1.6", "T1:C1:1.0")),
    # previous_message.ts names the target when deleted_ts is absent.
    (_deletion(REPLY, deleted_ts=None), ("1.0", "0.5", "1.6", "T1:C1:0.5")),
    # Without event_ts the deletion's own ts remains its occurrence stamp.
    (_deletion(REPLY, event_ts=None), ("1.0", "0.5", "1.6", "T1:C1:0.5")),
    # Edits and ordinary messages keep their identities.
    ({"type": "message", "subtype": "message_changed", "ts": "1.5", "event_ts": "1.5",
      "message": {**REPLY, "text": "edited"}, "previous_message": REPLY}, ("1.0", "0.5", "1.5", "T1:C1:0.5")),
    ({**REPLY, "event_ts": "1.0"}, ("1.0", "0.5", "1.0", "T1:C1:0.5")),
    ({**ROOT}, ("1.0", "", "1600", "T1:C1:1.0")),
], ids=["threaded-delete", "root-delete", "previous-ts-fallback", "no-event-ts", "edit", "reply", "root"])
def test_message_and_thread_identity(event, expected):
    parsed = _parse(_envelope("Ev-1", event))
    assert (parsed.message_ts, parsed.thread_ts, parsed.event_ts, parsed.ordering_key) == expected


def test_deletion_keeps_provider_facts_and_replay_identity(tmp_path):
    deletion = _deletion(REPLY)
    parsed = _parse(_envelope("Ev-del", deletion))
    assert parsed.text == "" and parsed.files == () and parsed.actor_user_id == "U1"
    assert parsed.structured == {"change": "deleted", "deleted_ts": "1.0", "previous_message": REPLY,
                                 "self_user_id": "U_BOT"}

    store = BridgeStore(tmp_path)
    for envelope in (_envelope("Ev-reply", REPLY), _envelope("Ev-del", deletion),
                     _envelope("Ev-del", deletion, envelope_id="env-retry")):
        store.ingest_envelope(envelope, parse_socket_envelope(envelope, bot_user_id="U_BOT"))
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM inbox ORDER BY id").fetchall()
    # The replay is one row; the deletion queues behind the thread it changes.
    assert [row["dedupe_key"] for row in rows] == ["event:Ev-reply", "event:Ev-del"]
    reply, deleted = rows
    assert deleted["ordering_key"] == reply["ordering_key"] == "T1:C1:0.5"
    assert (deleted["message_ts"], deleted["thread_ts"], deleted["event_ts"]) == ("1.0", "0.5", "1.6")
    assert json.loads(deleted["raw_json"])["payload"]["event"]["ts"] == "1.6"
    assert json.loads(deleted["structured_json"])["previous_message"] == REPLY


class _Host:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity":
            return httpx.Response(200, json={"ok": True})
        event = json.loads(request.content)["event"]
        self.events.append(event)
        return httpx.Response(200, json={"ok": True, "status": "completed", "outcome": "message", "text": "noted",
                                         "turn_ref": event["source_event_id"], "work_ref": ""})


def _provider(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("users.info"):
        return httpx.Response(200, json={"ok": True, "user": {"id": request.url.params["user"]}})
    return httpx.Response(200, json={"ok": True, "channel": {"id": request.url.params["channel"]}})


@pytest.mark.parametrize("previous,thread", [(REPLY, "0.5"), (ROOT, "")], ids=["threaded", "root"])
def test_host_sees_the_deleted_message_in_its_conversation(tmp_path, previous, thread):
    async def run(host: _Host) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_provider)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(host)) as host_http:
            adapter = LoopbackPresenceHostAdapter(binding_id="b" * 32, host_service_url="http://127.0.0.1:8767",
                                                  skill_token="skill-token", http_client=host_http)
            worker = InboundWorker(store, SlackClient("xoxb-test", "xapp-test", http_client=slack_http),
                                   adapter, staged_root=tmp_path / "staged")
            for _ in range(2):
                await worker.process_once()

    store, host = BridgeStore(tmp_path), _Host()
    for envelope in (_envelope("Ev-orig", previous), _envelope("Ev-del", _deletion(previous))):
        store.ingest_envelope(envelope, parse_socket_envelope(envelope, bot_user_id="U_BOT"))
    asyncio.run(run(host))

    original, deleted = host.events
    assert deleted["source_event_id"] == "Ev-del"
    assert deleted["conversation_key"] == original["conversation_key"]
    assert deleted["thread_id"] == deleted["conversation"]["thread_ts"] == original["thread_id"]
    message = deleted["message"]
    assert (message["message_id"], message["thread_id"], message["event_ts"]) == ("1.0", thread, "1.6")
    assert message["subtype"] == "message_deleted" and message["attachments"] == []
    assert message["provider_facts"]["deleted_ts"] == "1.0"
    assert message["provider_facts"]["previous_message"] == previous
    with sqlite3.connect(store.path) as db:
        replies = db.execute("SELECT thread_ts FROM outbox ORDER BY id").fetchall()
    # A reply to either event lands where the deleted message lived.
    assert replies == [(thread or "1.0",)] * 2
