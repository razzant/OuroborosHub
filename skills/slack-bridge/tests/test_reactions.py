"""Reactions through the actual parser, store, workers, Slack client and loopback adapter.

Slack and Host are scripted over httpx.MockTransport; nothing leaves the process. When an
``ouroboros`` core carrying the reaction validators is importable, every produced fact is also
checked against core's own ingress and delivery validators; otherwise those checks are skipped,
never counted. This does not exercise core's attention, sleep or final-selection paths.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import time

import httpx
import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.provider_context import capture_reaction_target
from lib.runtime import InboundWorker, OutboundWorker, _discover_reporting
from lib.slack_api import SlackClient
from lib.store import REACTION_WITHHELD, BridgeStore
from test_workers_plugin import _Api, _load_plugin

CAPABLE = {"ok": True, "places_version": 1, "presence_delivery_version": 1, "presence_reaction_context": 1,
           "presence_continuation_version": 1, "presence_explicit_completion": 1}
OLD_HOST = {"ok": True, "places_version": 1, "presence_delivery_version": 1, "presence_continuation_version": 1}


def reaction(event_id="Ev-r", *, kind="reaction_added", ts="100.000001", channel="C1", team="T1", name="eyes",
             item_user="U_AUTHOR", user="U_ACTOR", event_ts="200.5", item_type="message"):
    return {"type": "events_api", "envelope_id": "env-" + event_id, "payload": {
        "event_id": event_id, "team_id": team, "event_time": 200, "event": {
            "type": kind, "user": user, "reaction": name, "event_ts": event_ts,
            **({"item_user": item_user} if item_user else {}),
            "item": {"type": item_type, "channel": channel, "ts": ts}}}}


def message(event_id="Ev-m", *, ts="100.000001", thread_ts="", channel="C1", team="T1", text="the words", user="U_AUTHOR"):
    return {"type": "events_api", "envelope_id": "env-" + event_id, "payload": {
        "event_id": event_id, "team_id": team, "event": {
            "type": "message", "user": user, "channel": channel, "channel_type": "channel", "ts": ts, "text": text,
            **({"thread_ts": thread_ts} if thread_ts else {})}}}


def ingest(store, *values, **kwargs):
    for value in values:
        store.ingest_envelope(value, parse_socket_envelope(value, bot_user_id="U_BOT", **kwargs))


class World:
    """Scripted Slack and Host, recording every request."""

    def __init__(self, store, tmp_path, identity=CAPABLE, *, replies=None, slack_error=None):
        self.store, self.tmp_path, self.identity = store, tmp_path, identity
        self.replies, self.slack_error = replies, slack_error
        self.slack_requests, self.turns, self.reports = [], [], []
        self.reaction_answers = []
        self.place_queries, self.thread_states = [], {}

    def slack(self, request):
        path = request.url.path.rsplit("/", 1)[-1]
        self.slack_requests.append((path, dict(request.url.params), request.content))
        if path == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"id": request.url.params["user"], "name": "n"}})
        if path == "conversations.info":
            return httpx.Response(200, json={"ok": True, "channel": {"id": request.url.params["channel"]}})
        if path == "conversations.replies":
            if isinstance(self.replies, BaseException):
                raise self.replies
            return httpx.Response(200, json=self.replies if isinstance(self.replies, dict)
                                  else {"ok": True, "messages": self.replies or [], "has_more": False})
        if path in {"reactions.add", "reactions.remove"}:
            answer = self.reaction_answers.pop(0) if self.reaction_answers else {"ok": True}
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)
        if path == "chat.postMessage":
            body = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": f"{len(self.slack_requests)}.5"})
        raise AssertionError(f"unexpected Slack method {path}")

    def host(self, request):
        if request.url.path == "/identity":
            if self.identity is None:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=self.identity)
        if request.url.path == "/presence/turn":
            self.turns.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "completed", "outcome": "silent", "text": "",
                                             "turn_ref": "t", "work_ref": ""})
        if request.url.path == "/presence/delivery":
            self.reports.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "recorded": True})
        if request.url.path == "/presence/place":
            self.place_queries.append(dict(request.url.params))
            state = self.thread_states.get(request.url.params.get("thread_id", ""), getattr(self, "place_state", "active"))
            return httpx.Response(200, json={"state": state, "label": "Slack"})
        raise AssertionError(f"unexpected Host path {request.url.path}")

    def clients(self):
        self._slack_http = httpx.AsyncClient(transport=httpx.MockTransport(self.slack))
        self._host_http = httpx.AsyncClient(transport=httpx.MockTransport(self.host))
        slack = SlackClient("xoxb-fixture", "xapp-fixture", http_client=self._slack_http)
        adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                              skill_token="fixture", http_client=self._host_http)
        return slack, adapter

    def inbound(self):
        slack, adapter = self.clients()
        return InboundWorker(self.store, slack, adapter, staged_root=self.tmp_path / "staged")

    def outbound(self):
        slack, adapter = self.clients()
        return OutboundWorker(self.store, slack, adapter)


def valid_ingress(payload):
    """Core's own validator on the exact wire body, when this core carries it."""
    try:
        from ouroboros.place_intake import reaction_refusal
    except (ImportError, AttributeError):
        return
    assert reaction_refusal(payload["event"], payload.get("staged_files")) == ""


def valid_receipt(report):
    try:
        from ouroboros.presence_delivery import validate_delivery
    except ImportError:
        return
    assert validate_delivery(report)


async def drive(world, *, passes=8):
    inbound = world.inbound()
    for _ in range(passes):
        if not await inbound.process_once():
            break
    await inbound.host.aclose()
    return inbound


def reads(world, method):
    return [row for row in world.slack_requests if row[0] == method]


def target_reads(world):
    """The reaction's own lookup, not an ordinary message's existing thread observation."""
    return [row for row in reads(world, "conversations.replies") if "oldest" in row[1]]


def provider_turn(world, index=-1):
    payload = world.turns[index]
    valid_ingress(payload)
    return payload["event"]


# -- Inbound: facts and exact targets ---------------------------------------------------------

def test_add_on_retained_top_level_message_reads_nothing_more_and_keeps_raw_facts(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(), reaction())
        world = World(store, tmp_path)
        await drive(world)
        event = provider_turn(world)
        fact = event["message"]["reaction"]
        assert fact == {"schema_version": 1, "change": "add", "new": [{"type": "name", "value": "eyes"}],
                        "target": {"message_id": "100.000001", "resolution": "resolved",
                                   "author": {"platform_actor_id": "U_AUTHOR", "is_self": False},
                                   "excerpt": {"text": "the words", "truncated": False}}}
        assert "old" not in fact and "counts" not in fact
        assert event["thread_id"] == "100.000001" and event["text"] == "" and event["message"]["attachments"] == []
        assert event["conversation"]["thread_ts"] == "100.000001"
        assert event["actor"]["kind"] == "user" and event["actor"]["platform_actor_id"] == "U_ACTOR"
        assert event["message"]["sent_at"] == "1970-01-01T00:03:20.500000+00:00"
        raw = event["message"]["provider_facts"]["reaction"]
        assert raw["item_user"] == "U_AUTHOR" and raw["name"] == "eyes"
        assert raw["target_lookup"]["source"] == "slack-bridge inbox"
        assert world.turns[-1]["staged_files"] == []
        assert not target_reads(world) and not reads(world, "conversations.history")
        # The original message's lane was not reinterpreted, and a reaction is not a wake fact.
        assert event["facts"] == []
    asyncio.run(run())


def test_remove_names_only_the_value_this_event_observed(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(), reaction(kind="reaction_removed", name="thumbsup::skin-tone-2"))
        world = World(store, tmp_path)
        await drive(world)
        fact = provider_turn(world)["message"]["reaction"]
        assert fact["change"] == "remove" and fact["old"] == [{"type": "name", "value": "thumbsup::skin-tone-2"}]
        assert "new" not in fact
    asyncio.run(run())


def test_retained_reply_uses_its_proved_root(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(ts="101.5", thread_ts="100.0"), reaction(ts="101.5"))
        world = World(store, tmp_path)
        await drive(world)
        event = provider_turn(world)
        assert event["thread_id"] == "100.0" and event["conversation_key"] == "slack:T1:C1:100.0"
        assert event["message"]["reaction"]["target"]["message_id"] == "101.5"
        assert not target_reads(world)
    asyncio.run(run())


def test_edited_message_excerpt_is_the_newest_retained_words(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        edit = {"type": "events_api", "envelope_id": "e", "payload": {"event_id": "Ev-e", "team_id": "T1", "event": {
            "type": "message", "subtype": "message_changed", "channel": "C1", "channel_type": "channel",
            "event_ts": "150.0", "ts": "150.0",
            "message": {"type": "message", "user": "U_AUTHOR", "ts": "100.000001", "text": "corrected"},
            "previous_message": {"type": "message", "user": "U_AUTHOR", "ts": "100.000001", "text": "the words"}}}}
        ingest(store, message(), edit, reaction())
        world = World(store, tmp_path)
        await drive(world)
        assert provider_turn(world)["message"]["reaction"]["target"]["excerpt"]["text"] == "corrected"
    asyncio.run(run())


def test_own_message_is_proved_only_by_its_exact_creation_receipt(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        store.enqueue_outbox(request_id="say", target="C1", thread_ts="90.0", chunks=["my own words"])
        item = store.claim_outbox()
        store.set_resolved_target(item, "C1", "T1")
        store.complete_outbox(item.row_id, item.lease_token, provider_message_ts="100.000001")
        # A mutation that merely returned this ts proves nothing about authorship.
        store.enqueue_mutation(request_id="edit", operation="update_message",
                               payload={"channel": "C1", "ts": "555.0", "text": "x"})
        other = store.claim_outbox()
        store.set_resolved_target(other, "C1", "T1")
        store.complete_outbox(other.row_id, other.lease_token, provider_message_ts="555.0")
        ingest(store, reaction(item_user=""), reaction("Ev-other", ts="555.0", item_user=""))
        world = World(store, tmp_path, replies={"ok": False, "error": "thread_not_found"})
        await drive(world)
        own, unknown = provider_turn(world, 0), provider_turn(world, 1)
        assert own["thread_id"] == "90.0"
        assert own["message"]["reaction"]["target"]["author"] == {"platform_actor_id": "U_BOT", "is_self": True}
        assert own["message"]["reaction"]["target"]["excerpt"]["text"] == "my own words"
        assert unknown["thread_id"] == "" and "author" not in unknown["message"]["reaction"]["target"]
        assert unknown["message"]["reaction"]["target"]["resolution"] == "unresolved"
    asyncio.run(run())


def test_item_user_equal_to_the_bot_is_the_only_other_self_proof(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction(item_user="U_BOT"))
        world = World(store, tmp_path, replies=[{"ts": "100.000001", "user": "U_BOT", "text": "mine"}])
        await drive(world)
        assert provider_turn(world)["message"]["reaction"]["target"]["author"] == {
            "platform_actor_id": "U_BOT", "is_self": True}
    asyncio.run(run())


@pytest.mark.parametrize("target_ts,page,thread", [
    ("100.000001", [{"ts": "100.000001", "user": "U2", "text": "top"}], "100.000001"),
    ("100.000001", [{"ts": "100.000001", "thread_ts": "100.000001", "user": "U2", "text": "parent"}], "100.000001"),
    ("101.5", [{"ts": "100.0", "thread_ts": "100.0", "user": "U9", "text": "root"},
               {"ts": "101.5", "thread_ts": "100.0", "user": "U2", "text": "a reply"}], "100.0"),
])
def test_miss_costs_one_narrow_read_and_proves_top_level_or_root(tmp_path, target_ts, page, thread):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction(ts=target_ts, item_user=""))
        world = World(store, tmp_path, replies=page)
        await drive(world)
        event = provider_turn(world)
        assert event["thread_id"] == thread
        target = event["message"]["reaction"]["target"]
        assert target["resolution"] == "resolved" and target["author"]["platform_actor_id"] == "U2"
        [(_, params, _)] = reads(world, "conversations.replies")
        assert params == {"channel": "C1", "ts": target_ts, "oldest": target_ts, "latest": target_ts,
                          "inclusive": "true", "limit": "2"}
        assert not reads(world, "conversations.history")
    asyncio.run(run())


@pytest.mark.parametrize("answer,code", [
    ({"ok": False, "error": "thread_not_found"}, "thread_not_found"),
    ({"ok": False, "error": "missing_scope", "needed": "channels:history"}, "missing_scope"),
    ({"ok": False, "error": "not_in_channel"}, "not_in_channel"),
    ({"ok": False, "error": "ratelimited"}, "ratelimited"),
    ([], "reacted_message_not_returned"),
    ([{"ts": "100.0", "thread_ts": "100.0", "user": "U9", "text": "root only"}], "reacted_message_not_returned"),
    (httpx.ReadTimeout("slow"), "transport_error"),
])
def test_unprovable_target_is_channel_level_never_a_phantom_root(tmp_path, answer, code):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction(ts="101.5"))
        world = World(store, tmp_path, replies=answer)
        await drive(world)
        event = provider_turn(world)
        target = event["message"]["reaction"]["target"]
        assert event["thread_id"] == "" and event["conversation"]["thread_ts"] == ""
        assert event["conversation_key"] == "slack:T1:C1:0"
        assert target["resolution"] == "unresolved" and target["message_id"] == "101.5"
        # item_user is the provider's own statement even when the conversation is unproven.
        assert target["author"]["platform_actor_id"] == "U_AUTHOR" and "excerpt" not in target
        lookup = event["message"]["provider_facts"]["reaction"]["target_lookup"]
        assert lookup["status"] == "unavailable" and lookup["error"]["code"] == code
        assert len(reads(world, "conversations.replies")) == 1 and store.status()["inbox_delivered"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("ingested", [
    message(channel="C2"), message(team="T2"), message(ts="100.000002"),
])
def test_same_timestamp_elsewhere_does_not_resolve(tmp_path, ingested):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, ingested, reaction())
        assert store.retained_message("T1", "C1", "100.000001") is None
        world = World(store, tmp_path, replies=[])
        await drive(world)
        assert provider_turn(world)["message"]["reaction"]["target"]["resolution"] == "unresolved"
    asyncio.run(run())


def test_row_captured_before_this_version_stays_unresolved_and_reads_nothing_more(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction())
        item = store.claim_inbox()
        store.set_provider_context(item.row_id, item.lease_token, {"user": {"status": "available", "data": {"id": "U_ACTOR"}},
                                                                   "conversation": {"status": "available", "data": {"id": "C1"}},
                                                                   "workspace": {"name": "W", "source": "auth.test"}})
        store.retry_inbox(item.row_id, item.lease_token, "", delay_seconds=0)
        world = World(store, tmp_path)
        await drive(world)
        event = provider_turn(world)
        assert event["thread_id"] == "" and event["message"]["reaction"]["target"]["resolution"] == "unresolved"
        assert world.slack_requests == []
    asyncio.run(run())


def test_lookups_are_addressed_by_an_index_not_a_scan(tmp_path):
    store = BridgeStore(tmp_path)
    with sqlite3.connect(store.path) as db:
        own = " ".join(str(row[3]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT thread_ts,text FROM outbox WHERE resolved_channel=? AND provider_message_ts=? "
            "AND provider_account_id=? AND kind='text' AND state='delivered' ORDER BY id DESC LIMIT 1", ("C", "1", "T")))
        seen = " ".join(str(row[3]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT actor_user_id,thread_ts,text FROM inbox WHERE team_id=? AND channel_id=? AND message_ts=? "
            "AND event_type IN ('message','app_mention') AND subtype!='message_deleted' ORDER BY id DESC LIMIT 1", ("T", "C", "1")))
    assert "outbox_message" in own and "inbox_message" in seen


def test_deleted_message_row_is_not_a_target_record(tmp_path):
    store = BridgeStore(tmp_path)
    ingest(store, {"type": "events_api", "envelope_id": "d", "payload": {"event_id": "Ev-d", "team_id": "T1", "event": {
        "type": "message", "subtype": "message_deleted", "channel": "C1", "deleted_ts": "100.000001", "ts": "150.0",
        "previous_message": {"ts": "100.000001", "user": "U_AUTHOR", "text": "gone"}}}})
    assert store.retained_message("T1", "C1", "100.000001") is None


def test_long_target_is_labelled_partial_and_stays_whole_in_raw_facts(tmp_path):
    async def run():
        words = "слово " * 200
        store = BridgeStore(tmp_path)
        ingest(store, reaction(item_user=""))
        world = World(store, tmp_path, replies=[{
            "ts": "100.000001", "user": "U2", "text": words,
            "files": [{"id": "F1", "name": "a.pdf", "url_private": "https://files.slack.com/secret"}]}])
        await drive(world)
        event = provider_turn(world)
        excerpt = event["message"]["reaction"]["target"]["excerpt"]
        assert excerpt == {"text": words[:500], "truncated": True}
        raw = event["message"]["provider_facts"]["reaction"]["target_lookup"]["message"]
        assert raw["text"] == words
        assert "secret" not in json.dumps(event)
        with sqlite3.connect(store.path) as db:  # the full provider answer stays in connector state
            assert "secret" in db.execute("SELECT provider_context_json FROM inbox").fetchone()[0]
    asyncio.run(run())


def test_exactly_500_characters_is_not_called_partial(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(text="x" * 500), reaction())
        world = World(store, tmp_path)
        await drive(world)
        assert provider_turn(world)["message"]["reaction"]["target"]["excerpt"] == {"text": "x" * 500, "truncated": False}
    asyncio.run(run())


def test_retry_after_lost_host_answer_reuses_the_one_read(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction(ts="101.5"))
        world = World(store, tmp_path, replies=[{"ts": "101.5", "thread_ts": "100.0", "user": "U2", "text": "r"}])
        real = world.host
        lost = {"n": 0}

        def flaky(request):
            if request.url.path == "/presence/turn" and not lost["n"]:
                lost["n"] = 1
                world.turns.append(json.loads(request.content))
                raise httpx.ReadTimeout("Host answer lost", request=request)
            return real(request)
        world.host = flaky
        inbound = world.inbound()
        assert await inbound.process_once()
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE inbox SET available_at=0")
        assert await inbound.process_once()
        assert world.turns[0] == world.turns[1]
        assert len(reads(world, "conversations.replies")) == 1
        assert store.status()["inbox_delivered"] == 1
    asyncio.run(run())


def test_non_message_item_is_not_a_reaction_fact(tmp_path):
    store = BridgeStore(tmp_path)
    file_item = {"type": "events_api", "envelope_id": "f", "payload": {"event_id": "Ev-f", "team_id": "T1", "event": {
        "type": "reaction_added", "user": "U1", "reaction": "eyes", "event_ts": "1.0",
        "item": {"type": "file", "file": "F1"}}}}
    parsed = parse_socket_envelope(file_item, bot_user_id="U_BOT")
    assert not parsed.accepted and parsed.reason == "missing_message_provenance"


def test_own_reaction_echo_never_reaches_host(tmp_path):
    parsed = parse_socket_envelope(reaction(user="U_BOT"), bot_user_id="U_BOT")
    assert not parsed.accepted and parsed.reason == "self_message"


def test_other_bots_reaction_keeps_its_existing_from_bot_fact_and_individual_shape(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(), reaction(user="U_OTHERBOT"))
        world = World(store, tmp_path)
        real = world.slack

        def slack(request):
            if request.url.path.endswith("users.info"):
                return httpx.Response(200, json={"ok": True, "user": {"id": "U_OTHERBOT", "is_bot": True}})
            return real(request)
        world.slack = slack
        await drive(world)
        event = provider_turn(world)
        assert event["facts"] == ["from_bot"] and event["actor"]["is_bot"] is True and event["actor"]["kind"] == "user"
    asyncio.run(run())


def test_ordinary_message_events_gain_no_reaction_shape(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message())
        world = World(store, tmp_path, replies=[{"ts": "100.000001", "user": "U_AUTHOR", "text": "the words"}])
        await drive(world)
        event = provider_turn(world)
        assert "reaction" not in event["message"] and "kind" not in event["actor"] and "sent_at" not in event["message"]
        assert event["thread_id"] == "100.000001" and "any_message" in event["facts"]
    asyncio.run(run())


# -- Inbound: unsupported Host ----------------------------------------------------------------

@pytest.mark.parametrize("identity", [
    OLD_HOST, {**OLD_HOST, "presence_reaction_context": 2}, {**OLD_HOST, "presence_reaction_context": True},
    {**OLD_HOST, "presence_reaction_context": "1"}, {**OLD_HOST, "presence_reaction_context": 0}, None,
])
def test_reaction_is_retained_locally_and_not_delivered_or_read_on_unsupported_host(tmp_path, identity):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction())
        world = World(store, tmp_path, identity=identity)
        await drive(world)
        assert world.turns == [] and world.slack_requests == []
        status = store.status()
        assert status["reactions_withheld"] == 1 and status["inbox_pending"] == 0 and status["inbox_ignored"] == 1
        assert status["reaction_context_state"] == "update_required"
        assert "update" in status["reaction_context_limitation"]
        with sqlite3.connect(store.path) as db:
            state, reason, raw = db.execute("SELECT state,ignored_reason,raw_json FROM inbox").fetchone()
        assert (state, reason) == ("ignored", REACTION_WITHHELD) and json.loads(raw)["payload"]["event"]["reaction"] == "eyes"
    asyncio.run(run())


def test_withheld_observation_is_not_replayed_after_host_gains_the_capability(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction("Ev-old"))
        await drive(World(store, tmp_path, identity=OLD_HOST))
        ingest(store, reaction("Ev-new", ts="100.000002", item_user=""))
        world = World(BridgeStore(tmp_path), tmp_path, identity=CAPABLE, replies=[])
        await drive(world)
        assert [turn["event"]["source_event_id"] for turn in world.turns] == ["Ev-new"]
        assert world.store.status()["reactions_withheld"] == 1
        assert world.store.status()["reaction_context_state"] == "available"
    asyncio.run(run())


def test_normal_messages_continue_on_old_host_and_queue_omits_withheld_reaction(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(), reaction(), message("Ev-2", ts="100.000003", thread_ts="100.000001", text="next"))
        world = World(store, tmp_path, identity=OLD_HOST, replies=[{"ts": "100.000001", "user": "U1", "text": "m"}])
        await drive(world)
        assert [turn["event"]["source_event_id"] for turn in world.turns] == ["Ev-m", "Ev-2"]
        queue = world.turns[0]["event"]["conversation"]["transport_queue"]
        assert [row["source_event_id"] for row in queue["events"]] == ["Ev-2"] and queue["pending_count"] == 1
        assert store.status()["reactions_withheld"] == 1
    asyncio.run(run())


def test_reaction_event_carries_no_queue_of_its_target_lane(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(ts="101.5", thread_ts="100.0"), reaction(ts="101.5"), reaction("Ev-2", ts="101.5", name="wave"))
        world = World(store, tmp_path, identity=CAPABLE)
        await drive(world)
        events = [provider_turn(world, index) for index in (-2, -1)]
        assert [event["message"]["reaction"]["new"][0]["value"] for event in events] == ["eyes", "wave"]
        assert all("transport_queue" not in event["conversation"] for event in events)
    asyncio.run(run())


def test_capable_host_sees_queued_reaction_as_an_observation(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(), reaction())
        world = World(store, tmp_path, identity=CAPABLE, replies=[{"ts": "100.000001", "user": "U1", "text": "m"}])
        await drive(world)
        queue = world.turns[0]["event"]["conversation"]["transport_queue"]
        assert [row["source_event_id"] for row in queue["events"]] == ["Ev-r"]
    asyncio.run(run())


def test_row_already_submitted_before_the_update_keeps_observing(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, reaction())
        item = store.claim_inbox()
        store.set_host_reference(item.row_id, item.lease_token, "completed:eyJzdGF0dXMiOiJjb21wbGV0ZWQiLCJvdXRjb21lIjoic2lsZW50In0")
        store.retry_inbox(item.row_id, item.lease_token, "", delay_seconds=0)
        world = World(store, tmp_path, identity=OLD_HOST)
        await drive(world)
        assert store.status()["reactions_withheld"] == 0 and store.status()["inbox_delivered"] == 1
    asyncio.run(run())


# -- Outgoing ---------------------------------------------------------------------------------

def queue(store, operation="reaction_add", *, ts="100.000001", name="eyes", channel="C1", request_id="react-1"):
    store.set_runtime(workspace_id="T1")
    store.enqueue_mutation(request_id=request_id, operation=operation,
                           payload={"channel": channel, "ts": ts, "name": name},
                           origin={"kind": "tool", "task_id": "task-1"}, delivery_reporting_version=1)


async def outbound_pass(world, *, passes=3):
    worker = world.outbound()
    await _discover_reporting(worker.host, world.store)
    for _ in range(passes):
        await worker.process_once()
        await asyncio.sleep(0)
    await worker.aclose()
    return worker


def provider_writes(world):
    return [row for row in world.slack_requests if row[0].startswith("reactions.")]


def part(store, request_id="react-1"):
    return store.delivery_receipt(request_id)["parts"][0]


def test_applied_add_is_a_nonspeech_receipt_on_a_host_that_shows_actions(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(ts="101.5", thread_ts="100.0"))
        queue(store, ts="101.5")
        world = World(store, tmp_path)
        await outbound_pass(world)
        assert json.loads(provider_writes(world)[0][2]) == {"channel": "C1", "timestamp": "101.5", "name": "eyes"}
        assert part(store)["state"] == "delivered" and part(store)["provider_result"]["effect"] == "applied"
        [report] = world.reports
        valid_receipt(report)
        assert report["text"] == "" and report["format"] == "" and report["part_id"] == "0"
        assert report["thread_id"] == "100.0" and report["state"] == "delivered"
        assert report["message"]["kind"] == "operation" and report["origin"] == {"kind": "tool", "task_id": "task-1"}
        assert report["message"]["reaction"] == {
            "schema_version": 1, "operation": "add", "new": [{"type": "name", "value": "eyes"}],
            "target": {"message_id": "101.5", "resolution": "resolved"}, "effect": "applied"}
    asyncio.run(run())


def test_remove_names_the_removed_value_and_unknown_target_stays_unresolved(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store, "reaction_remove")
        world = World(store, tmp_path)
        await outbound_pass(world)
        [report] = world.reports
        assert report["thread_id"] == "" and report["message"]["reaction"]["operation"] == "remove"
        assert report["message"]["reaction"]["old"] == [{"type": "name", "value": "eyes"}]
        assert "new" not in report["message"]["reaction"]
        assert report["message"]["reaction"]["target"]["resolution"] == "unresolved"
    asyncio.run(run())


def test_old_host_keeps_the_local_receipt_and_gets_no_report(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store)
        world = World(store, tmp_path, identity=OLD_HOST)
        await outbound_pass(world)
        assert part(store)["provider_result"]["effect"] == "applied"
        assert part(store)["history_report_state"] == "" and world.reports == []
        assert "update" in store.status()["reaction_history_limitation"]
    asyncio.run(run())


@pytest.mark.parametrize("operation,error,effect,field", [
    ("reaction_add", "already_reacted", "already_present", "new"),
    ("reaction_remove", "no_reaction", "already_absent", "old"),
])
def test_no_op_is_an_observation_distinct_from_applied_and_the_lane_moves_on(tmp_path, operation, error, effect, field):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store, operation)
        queue(store, "reaction_add", name="wave", request_id="next")
        world = World(store, tmp_path)
        world.reaction_answers = [{"ok": False, "error": error}, {"ok": True}]
        await outbound_pass(world)
        first = part(store)
        assert first["state"] == "delivered" and first["attempts"] == 1
        assert first["provider_result"] == {"effect": effect, "provider_error": error, "uncertain": False}
        assert part(store, "next")["provider_result"]["effect"] == "applied"
        report = next(row for row in world.reports if row["delivery_id"] == "react-1")
        valid_receipt(report)
        assert report["message"]["reaction"]["effect"] == effect and report["state"] == "delivered"
        assert report["message"]["reaction"][field][0]["value"] == "eyes"
        assert len(provider_writes(world)) == 2
    asyncio.run(run())


@pytest.mark.parametrize("error", ["message_not_found", "not_reactable", "invalid_name", "thread_locked",
                                   "too_many_reactions", "missing_scope", "is_archived", "no_permission"])
def test_documented_refusal_settles_at_once_and_does_not_hold_the_next_job(tmp_path, error):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store)
        queue(store, name="wave", request_id="next")
        world = World(store, tmp_path)
        world.reaction_answers = [{"ok": False, "error": error}, {"ok": True}]
        await outbound_pass(world)
        first = part(store)
        assert (first["state"], first["attempts"], first["error"]) == ("failed", 1, error)
        assert part(store, "next")["state"] == "delivered"
        report = next(row for row in world.reports if row["delivery_id"] == "react-1")
        valid_receipt(report)
        assert report["state"] == "failed" and "effect" not in report["message"]["reaction"]
        assert report["message"]["error"] == error
    asyncio.run(run())


@pytest.mark.parametrize("answer", [
    httpx.ReadTimeout("response lost"),
    httpx.Response(500, json={}),
    httpx.Response(408, json={}),
    {"ok": False, "error": "internal_error"},
    {"ok": False, "error": "fatal_error"},
])
def test_unknown_acceptance_is_uncertain_and_never_sent_again(tmp_path, answer):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store)
        world = World(store, tmp_path)
        world.reaction_answers = [answer]
        await outbound_pass(world, passes=4)
        assert part(store)["state"] == "uncertain" and part(store)["provider_result"] == {"uncertain": True}
        assert len(provider_writes(world)) == 1
        [report] = world.reports
        valid_receipt(report)
        assert report["state"] == "uncertain" and "effect" not in report["message"]["reaction"]
    asyncio.run(run())


def test_crash_after_marker_settles_uncertain_with_its_prepared_report_and_no_resend(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store)
        world = World(store, tmp_path)
        worker = world.outbound()
        await _discover_reporting(worker.host, store)
        item = store.claim_outbox(lease_seconds=120.0)
        import dataclasses
        item = dataclasses.replace(item, provider_account_id="T1")
        store.begin_send(item, uncertain_report=worker._reaction_report(item, "uncertain", error="send_result_unrecorded"))
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET lease_until=?", (time.time() - 1,))
        reopened = World(BridgeStore(tmp_path), tmp_path)
        await outbound_pass(reopened)
        assert part(store)["state"] == "uncertain" and provider_writes(reopened) == []
        [report] = reopened.reports
        valid_receipt(report)
        assert report["state"] == "uncertain" and report["message"]["error"] == "send_result_unrecorded"
    asyncio.run(run())


def test_rate_limit_retries_without_inventing_an_effect(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store)
        world = World(store, tmp_path)
        world.reaction_answers = [httpx.Response(429, json={"ok": False, "error": "ratelimited"},
                                                 headers={"retry-after": "0"}), {"ok": True}]
        await outbound_pass(world, passes=1)
        assert part(store)["state"] == "pending" and world.reports == []
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET available_at=0")
        await outbound_pass(world, passes=2)
        assert part(store)["provider_result"]["effect"] == "applied" and len(provider_writes(world)) == 2
    asyncio.run(run())


@pytest.mark.parametrize("place", ["silent", "paused"])
def test_silent_or_paused_place_holds_the_reaction_before_any_provider_write(tmp_path, place):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store)
        world = World(store, tmp_path)
        world.place_state = place
        await outbound_pass(world, passes=1)
        assert provider_writes(world) == [] and part(store)["state"] == "held"
    asyncio.run(run())


def test_a_thread_paused_after_queueing_holds_its_reaction_and_an_unretained_target_stays_the_channels(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(ts="100.000001", thread_ts="99.0"), message("Ev-top", ts="200.000001"))
        queue(store, request_id="in-thread")
        world = World(store, tmp_path)
        world.thread_states["99.0"] = "paused"  # that thread's own place, after queueing; the channel stays Active
        await outbound_pass(world, passes=1)
        assert provider_writes(world) == [] and part(store, "in-thread")["state"] == "held"
        assert part(store, "in-thread")["error"] == "Held: Slack is paused"
        assert world.place_queries == [{"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "99.0"}]
        del world.thread_states["99.0"]
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET available_at=0")
        # Retained at top level, its own timestamp is its thread; unretained, nothing is inferred and the channel decides.
        queue(store, request_id="unretained", ts="300.000001")
        queue(store, request_id="top", ts="200.000001")
        world.thread_states.update({"200.000001": "paused", "300.000001": "paused"})
        await outbound_pass(world, passes=4)
        assert part(store, "in-thread")["state"] == "delivered" and part(store, "unretained")["state"] == "delivered"
        assert part(store, "top")["state"] == "held"
        assert [json.loads(row[2])["timestamp"] for row in provider_writes(world)] == ["100.000001", "300.000001"]
        assert world.place_queries[1:] == [
            {"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "99.0"},
            {"provider": "slack", "account_id": "T1", "conversation_id": "C1"},
            {"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "200.000001"}]
    asyncio.run(run())


@pytest.mark.parametrize("later,reported", [(OLD_HOST, False), (None, False), (CAPABLE, True)])
def test_report_frozen_on_a_capable_host_goes_only_to_a_capable_one(tmp_path, later, reported):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store)
        first = World(store, tmp_path)
        worker = first.outbound()
        await _discover_reporting(worker.host, store)
        original_report = worker._report
        worker._report = lambda report: asyncio.sleep(0)  # the worker stops before its report goes out
        await worker.process_once()
        await asyncio.sleep(0)
        worker._report = original_report
        await worker.aclose()
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET report_state='pending',report_lease_until=0,report_available_at=0")
            frozen = db.execute("SELECT report_payload_json FROM outbox").fetchone()[0]
        reopened = World(BridgeStore(tmp_path), tmp_path, identity=later)
        await outbound_pass(reopened, passes=2)
        assert provider_writes(reopened) == [] and part(store)["provider_result"]["effect"] == "applied"
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT report_payload_json FROM outbox").fetchone()[0] == frozen
        if reported:
            assert part(store)["history_report_state"] == "acked" and len(reopened.reports) == 1
        else:
            assert reopened.reports == [] and part(store)["history_report_state"] == "pending"
            assert part(store)["history_report_error"].startswith("Held: Host history for reactions is unavailable")
    asyncio.run(run())


def test_other_mutations_gain_no_reaction_report(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        store.enqueue_mutation(request_id="pin", operation="pin_add", payload={"channel": "C1", "ts": "1.0"},
                               origin={"kind": "tool"}, delivery_reporting_version=1)
        world = World(store, tmp_path)
        real = world.slack

        def slack(request):
            if request.url.path.endswith("pins.add"):
                return httpx.Response(200, json={"ok": True})
            return real(request)
        world.slack = slack
        await outbound_pass(world)
        assert part(store, "pin")["state"] == "delivered" and world.reports == []
    asyncio.run(run())


def test_generic_api_reaction_stays_an_undeclared_operation(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        store.enqueue_mutation(request_id="generic", operation="generic_api", payload={
            "method": "POST", "path": "reactions.add", "effect": "write", "result_kind": "auto",
            "params": {}, "body": {"channel": "C1", "timestamp": "1.0", "name": "eyes"}},
            origin={"kind": "tool"}, delivery_reporting_version=1)
        world = World(store, tmp_path)
        world.reaction_answers = [{"ok": False, "error": "already_reacted"}]
        await outbound_pass(world)
        assert part(store, "generic")["state"] == "failed" and world.reports == []
    asyncio.run(run())


# -- One lane per reacted message -------------------------------------------------------------

def send(store, request_id, *, thread_ts=""):
    """A slack_send to C1 as the tool queues it, top-level unless a thread is named; its words are its id."""
    store.enqueue_outbox(request_id=request_id, target="C1", thread_ts=thread_ts, chunks=[request_id],
                         origin={"kind": "tool"}, delivery_reporting_version=1)


def generic(store, request_id, path, *, method="POST", **arguments):
    """A slack_api write as the tool queues it, its arguments untouched."""
    store.enqueue_mutation(request_id=request_id, operation="generic_api", payload={
        "method": method, "path": path, "effect": "write", "result_kind": "auto",
        "params": arguments if method == "GET" else {}, "body": arguments if method == "POST" else {}},
        origin={"kind": "tool"}, delivery_reporting_version=1)


def earlier_row(store, request_id, operation="reaction_add", *, channel="C1", ts="100.000001", lane="C1:",
                payload=None, **columns):
    """A mutation row exactly as an earlier version left it: its channel lane, payload bytes and any state."""
    body = payload if isinstance(payload, str) else json.dumps(payload or {"channel": channel, "ts": ts, "name": "eyes"})
    with sqlite3.connect(store.path) as db:
        db.execute(
            """INSERT INTO outbox(request_id,chunk_index,chunk_count,target,thread_ts,text,ordering_key,kind,operation,
                                  payload_json,origin_json,delivery_reporting_version,created_at,updated_at)
               VALUES(?,0,1,?,'','',?,'mutation',?,?,'{"kind":"tool"}',1,1,1)""",
            (request_id, channel, lane, operation, body))
        for name, value in columns.items():
            db.execute(f"UPDATE outbox SET {name}=? WHERE request_id=?", (value, request_id))


def lanes(store):
    with sqlite3.connect(store.path) as db:
        return dict(db.execute("SELECT request_id,ordering_key FROM outbox"))


def states(store, *request_ids):
    return {request_id: part(store, request_id)["state"] for request_id in request_ids}


def due(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")


def sent(world):
    """Every provider write in order: a reaction by method and message, a post by its words (GET: its query)."""
    writes = []
    for path, params, body in world.slack_requests:
        if path in {"reactions.add", "reactions.remove", "chat.postMessage"}:
            fields = json.loads(body) if body else params
            writes.append((path, fields.get("timestamp") or fields.get("markdown_text")))
    return writes


def test_a_reaction_held_for_its_thread_holds_back_only_later_writes_to_its_own_message(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(ts="100.000001", thread_ts="99.0"), message("Ev-top", ts="200.000001"))
        queue(store, request_id="held")
        send(store, "top-level")
        queue(store, request_id="other-message", ts="200.000001")
        queue(store, "reaction_remove", request_id="same-message", channel=" C1 ", ts=" 100.000001 ")
        generic(store, "same-message-api", "reactions.add", channel="C1", timestamp="100.000001", name="wave")
        world = World(store, tmp_path)
        world.thread_states["99.0"] = "paused"
        await outbound_pass(world, passes=6)
        assert states(store, "held", "top-level", "other-message", "same-message", "same-message-api") == {
            "held": "held", "top-level": "delivered", "other-message": "delivered",
            "same-message": "pending", "same-message-api": "pending"}
        assert part(store, "same-message")["attempts"] == 0 == part(store, "same-message-api")["attempts"]
        assert part(store, "top-level")["provider_message_ts"]
        assert sent(world) == [("chat.postMessage", "top-level"), ("reactions.add", "200.000001")]
        del world.thread_states["99.0"]
        due(store)
        await outbound_pass(world, passes=4)
        assert set(states(store, "held", "same-message", "same-message-api").values()) == {"delivered"}
        assert sent(world)[2:] == [("reactions.add", "100.000001"), ("reactions.remove", "100.000001"),
                                   ("reactions.add", "100.000001")]
    asyncio.run(run())


def test_each_reaction_write_is_laned_by_its_one_message_and_every_other_write_keeps_its_lane(tmp_path):
    store = BridgeStore(tmp_path)
    queue(store, request_id="add", channel=" C1 ", ts=" 100.000001 ")
    queue(store, "reaction_remove", request_id="remove")
    queue(store, request_id="names-no-message", channel=" ", ts="100.000001")
    generic(store, "api-post", "reactions.add", channel="C1", timestamp=" 100.000001 ", name="eyes")
    generic(store, "api-get", "reactions.remove", method="GET", channel="C1", timestamp="100.000001", name="eyes")
    generic(store, "api-file", "reactions.remove", file="F1", name="eyes")
    generic(store, "api-speech", "chat.postMessage", channel="C1", text="hi")
    store.enqueue_mutation(request_id="pin", operation="pin_add", payload={"channel": "C1", "ts": "100.000001"})
    send(store, "top-level")
    send(store, "reply", thread_ts="100.000001")
    assert lanes(store) == {
        "add": "reaction:C1:100.000001", "remove": "reaction:C1:100.000001", "names-no-message": "mutation:",
        "api-post": "reaction:C1:100.000001", "api-get": "reaction:C1:100.000001", "api-file": "mutation:",
        "api-speech": "C1:", "pin": "C1:", "top-level": "C1:", "reply": "C1:100.000001"}
    with sqlite3.connect(store.path) as db:
        bodies = dict(db.execute("SELECT request_id,payload_json FROM outbox WHERE request_id LIKE 'api-%'"))
    assert json.loads(bodies["api-post"])["body"]["timestamp"] == " 100.000001 "  # slack_api bytes stay as given


def test_live_rows_an_earlier_version_laned_by_channel_move_to_their_message_when_the_store_opens(tmp_path):
    store = BridgeStore(tmp_path)
    later = time.time() + 600
    earlier_row(store, "held", channel=" C1 ", ts=" 100.000001 ", lane=" C1 :", last_error="Held: Slack is paused",
                available_at=later)
    earlier_row(store, "leased", "reaction_remove", ts="200.000001", state="leased", lease_token="old-worker",
                lease_until=later, attempts=1)
    earlier_row(store, "started", ts="300.000001", state="leased", lease_token="old-sender", lease_until=later,
                send_started=1, attempts=1)
    earlier_row(store, "api", "generic_api", payload={"method": "POST", "path": "reactions.add", "params": {},
                                                      "body": {"channel": "C1", "timestamp": "400.000001", "name": "eyes"}})
    for state in ("delivered", "failed", "uncertain"):
        earlier_row(store, state, state=state)
    earlier_row(store, "unreadable", payload="{not json")
    earlier_row(store, "names-no-message", channel=" ", ts="")
    earlier_row(store, "api-speech", "generic_api", payload={"method": "POST", "path": "chat.postMessage", "params": {},
                                                             "body": {"channel": "C1", "text": "hi"}})
    send(store, "top-level")

    def everything_but_lanes():
        with sqlite3.connect(store.path) as db:
            db.row_factory = sqlite3.Row
            return [{key: row[key] for key in row.keys() if key != "ordering_key"}
                    for row in db.execute("SELECT * FROM outbox ORDER BY id")]
    before = everything_but_lanes()
    BridgeStore(tmp_path)  # the next process to open it
    assert lanes(store) == {
        "held": "reaction:C1:100.000001", "leased": "reaction:C1:200.000001", "started": "reaction:C1:300.000001",
        "api": "reaction:C1:400.000001", "delivered": "C1:", "failed": "C1:", "uncertain": "C1:",
        "unreadable": "C1:", "names-no-message": "C1:", "api-speech": "C1:", "top-level": "C1:"}
    assert everything_but_lanes() == before  # leases, send markers, attempts, holds and payload bytes as they were


def test_a_lease_an_older_worker_took_in_the_channel_lane_still_owns_its_row_and_holds_back_only_its_message(tmp_path):
    store = BridgeStore(tmp_path)
    earlier_row(store, "in-flight")
    earlier_row(store, "same-message", "reaction_remove")
    send(store, "top-level")
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        db.execute("UPDATE outbox SET state='leased',lease_token='stale',lease_until=?,attempts=1 "
                   "WHERE request_id='in-flight'", (time.time() + 600,))
        stale = BridgeStore._outbox_item(db.execute("SELECT * FROM outbox WHERE request_id='in-flight'").fetchone())
    assert stale.ordering_key == "C1:" and stale.lease_token == "stale"  # that worker's own copy, taken before the move
    reopened = BridgeStore(tmp_path)
    assert lanes(reopened)["in-flight"] == lanes(reopened)["same-message"] == "reaction:C1:100.000001"
    independent = reopened.claim_outbox(lease_seconds=60)
    assert independent.request_id == "top-level" and reopened.claim_outbox(lease_seconds=60) is None
    reopened.hold_outbox(stale, "Held: Slack is paused")  # its token still owns the row
    assert part(reopened, "in-flight")["state"] == "held" and reopened.claim_outbox(lease_seconds=60) is None
    due(reopened)
    again = reopened.claim_outbox(lease_seconds=60)
    assert again.request_id == "in-flight" and reopened.claim_outbox(lease_seconds=60) is None
    reopened.complete_outbox(again.row_id, again.lease_token)
    assert reopened.claim_outbox(lease_seconds=60).request_id == "same-message"


def test_a_channel_laned_row_an_older_process_queues_after_the_store_opened_keeps_its_message_order(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(ts="100.000001", thread_ts="99.0"))
        queue(store, request_id="held")
        legacy_row(store, request_id="older-writer", operation="reaction_remove")  # " C1 :", after this store opened
        send(store, "top-level")
        world = World(store, tmp_path)
        world.thread_states["99.0"] = "paused"
        await outbound_pass(world, passes=5)
        assert states(store, "held", "older-writer", "top-level") == {
            "held": "held", "older-writer": "pending", "top-level": "delivered"}
        assert lanes(store)["older-writer"] == "reaction:C1:100.000001"
        assert sent(world) == [("chat.postMessage", "top-level")]
        del world.thread_states["99.0"]
        due(store)
        await outbound_pass(world, passes=3)
        assert sent(world)[1:] == [("reactions.add", "100.000001"), ("reactions.remove", "100.000001")]
    asyncio.run(run())


def test_an_add_queued_before_its_target_was_known_still_precedes_a_remove_queued_after(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store, request_id="add-unknown-target")
        ingest(store, message(ts="100.000001", thread_ts="99.0"))  # the target's record arrives after the add
        queue(store, "reaction_remove", request_id="remove-known-target")
        send(store, "top-level")
        world = World(store, tmp_path)
        world.thread_states["99.0"] = "paused"
        await outbound_pass(world, passes=4)
        assert lanes(store)["add-unknown-target"] == lanes(store)["remove-known-target"]
        assert states(store, "add-unknown-target", "remove-known-target", "top-level") == {
            "add-unknown-target": "held", "remove-known-target": "pending", "top-level": "delivered"}
        assert world.place_queries[0]["thread_id"] == "99.0"  # checked in the thread it now knows
        del world.thread_states["99.0"]
        due(store)
        await outbound_pass(world, passes=3)
        assert sent(world) == [("chat.postMessage", "top-level"), ("reactions.add", "100.000001"),
                               ("reactions.remove", "100.000001")]
    asyncio.run(run())


def test_an_expired_attempt_in_the_channel_lane_settles_unknown_unsent_and_its_message_moves_on(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        gone = time.time() - 1
        earlier_row(store, "crashed", state="leased", lease_token="gone", lease_until=gone, send_started=1, attempts=1)
        earlier_row(store, "never-started", ts="200.000001", state="leased", lease_token="gone-too", lease_until=gone,
                    attempts=1)
        earlier_row(store, "after-crash", "reaction_remove")
        earlier_row(store, "after-reclaim", "reaction_remove", ts="200.000001")
        send(store, "top-level")
        world = World(BridgeStore(tmp_path), tmp_path)
        await outbound_pass(world, passes=6)
        assert states(store, "crashed", "never-started", "after-crash", "after-reclaim", "top-level") == {
            "crashed": "uncertain", "never-started": "delivered", "after-crash": "delivered",
            "after-reclaim": "delivered", "top-level": "delivered"}
        assert part(store, "crashed")["error"] == "send_result_unrecorded"
        assert sent(world) == [("reactions.add", "200.000001"), ("reactions.remove", "100.000001"),
                               ("reactions.remove", "200.000001"), ("chat.postMessage", "top-level")]
    asyncio.run(run())


def test_a_cancelled_reaction_write_stays_unknown_and_its_message_moves_on_as_before(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        queue(store, request_id="cancelled")
        queue(store, "reaction_remove", request_id="same-message")
        world = World(store, tmp_path)
        world.reaction_answers = [asyncio.CancelledError()]
        worker = world.outbound()
        await _discover_reporting(worker.host, store)
        with pytest.raises(asyncio.CancelledError):
            await worker.process_once()
        await worker.aclose()
        await outbound_pass(world, passes=3)
        assert states(store, "cancelled", "same-message") == {"cancelled": "uncertain", "same-message": "delivered"}
        assert part(store, "cancelled")["error"] == "send_cancelled_after_dispatch"
        assert sent(world) == [("reactions.add", "100.000001"), ("reactions.remove", "100.000001")]
    asyncio.run(run())


def api_payload(method, path, **arguments):
    """A slack_api write's payload as the tool stores it, arguments under params (GET) or body (POST)."""
    return {"method": method, "path": path, "effect": "write", "result_kind": "auto",
            "params": arguments if method == "GET" else {}, "body": arguments if method == "POST" else {}}


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_a_slack_api_reaction_an_older_process_queues_after_the_store_opened_waits_behind_its_held_message(tmp_path,
                                                                                                          method):
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message(ts="100.000001", thread_ts="99.0"))
        queue(store, request_id="held")
        earlier_row(store, "older-api-remove", "generic_api",  # in its channel's lane, after this store opened
                    payload=api_payload(method, "reactions.remove", channel="C1", timestamp="100.000001", name="eyes"))
        send(store, "top-level")
        world = World(store, tmp_path)
        world.thread_states["99.0"] = "paused"
        await outbound_pass(world, passes=5)
        assert states(store, "held", "older-api-remove", "top-level") == {
            "held": "held", "older-api-remove": "pending", "top-level": "delivered"}
        assert part(store, "older-api-remove")["attempts"] == 0
        assert lanes(store)["older-api-remove"] == "reaction:C1:100.000001"
        assert sent(world) == [("chat.postMessage", "top-level")]
        del world.thread_states["99.0"]
        due(store)
        await outbound_pass(world, passes=3)
        assert sent(world)[1:] == [("reactions.add", "100.000001"), ("reactions.remove", "100.000001")]
    asyncio.run(run())


def test_a_live_slack_api_lease_an_older_worker_holds_keeps_back_only_its_message(tmp_path):
    store = BridgeStore(tmp_path)
    earlier_row(store, "older-api-add", "generic_api",  # mid-send in an older worker, queued after this store opened
                payload=api_payload("POST", "reactions.add", channel="C1", timestamp="100.000001", name="eyes"),
                state="leased", lease_token="older-worker", lease_until=time.time() + 600, send_started=1, attempts=1)
    queue(store, "reaction_remove", request_id="same-message")
    send(store, "top-level")
    assert store.claim_outbox(lease_seconds=60).request_id == "top-level"
    assert store.claim_outbox(lease_seconds=60) is None
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT id,ordering_key,lease_token,send_started,state FROM outbox "
                         "WHERE request_id='older-api-add'").fetchone()
    assert row[1:] == ("reaction:C1:100.000001", "older-worker", 1, "leased")
    store.complete_outbox(row[0], "older-worker", result={"ok": True})  # its own token still ends it
    assert store.claim_outbox(lease_seconds=60).request_id == "same-message"


def test_registered_slack_api_reactions_share_each_message_lane_with_the_dedicated_tools(tmp_path, tool_json):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        ingest(store, message(ts="100.000001", thread_ts="99.0"), message("Ev-top", ts="200.000001"))
        api = _Api(tmp_path)
        _load_plugin().register(api)

        async def call(tool_name, /, **arguments):
            result = api.tools[tool_name][0](**arguments)
            answer = tool_json(await result if asyncio.iscoroutine(result) else result)
            assert answer["state"] == "queued", answer
        await call("slack_reaction_add", channel="C1", ts="100.000001", name="eyes", request_id="dedicated-add")
        await call("slack_api", method="POST", path="reactions.remove", request_id="api-remove",
                   body={"channel": "C1", "timestamp": "100.000001", "name": "eyes"})
        await call("slack_api", method="GET", path="reactions.add", request_id="api-add",
                   params={"channel": "C1", "timestamp": "200.000001", "name": "wave"})
        await call("slack_reaction_remove", channel="C1", ts="200.000001", name="wave", request_id="dedicated-remove")
        await call("slack_send", channel_or_user="C1", text="independent", request_id="top-level")
        assert lanes(store) == {"dedicated-add": "reaction:C1:100.000001", "api-remove": "reaction:C1:100.000001",
                                "api-add": "reaction:C1:200.000001", "dedicated-remove": "reaction:C1:200.000001",
                                "top-level": "C1:"}
        world = World(store, tmp_path)
        world.thread_states["99.0"] = "paused"
        await outbound_pass(world, passes=6)
        assert states(store, "dedicated-add", "api-remove", "api-add", "dedicated-remove", "top-level") == {
            "dedicated-add": "held", "api-remove": "pending", "api-add": "delivered", "dedicated-remove": "delivered",
            "top-level": "delivered"}
        assert sent(world) == [("reactions.add", "200.000001"), ("reactions.remove", "200.000001"),
                               ("chat.postMessage", "independent")]
        assert [row[1] for row in world.slack_requests if row[0] == "reactions.add"] == [
            {"channel": "C1", "timestamp": "200.000001", "name": "wave"}]  # the GET carried its query as given
        del world.thread_states["99.0"]
        due(store)
        await outbound_pass(world, passes=3)
        assert sent(world)[3:] == [("reactions.add", "100.000001"), ("reactions.remove", "100.000001")]
    asyncio.run(run())


def test_concurrent_claims_never_take_two_writes_to_one_message(tmp_path):
    first, second = BridgeStore(tmp_path), BridgeStore(tmp_path)
    queue(first, request_id="add")
    queue(first, "reaction_remove", request_id="remove")
    legacy_row(first, request_id="older-writer")  # the same message, queued in its channel lane after both opened
    queue(first, request_id="other", ts="200.000001")
    send(first, "top-level")
    taken = [first.claim_outbox(lease_seconds=60), second.claim_outbox(lease_seconds=60),
             second.claim_outbox(lease_seconds=60)]
    assert [item.request_id for item in taken] == ["add", "other", "top-level"]
    assert first.claim_outbox(lease_seconds=60) is None and second.claim_outbox(lease_seconds=60) is None
    second.complete_outbox(taken[0].row_id, taken[0].lease_token)
    remove = second.claim_outbox(lease_seconds=60)
    assert remove.request_id == "remove" and first.claim_outbox(lease_seconds=60) is None
    first.complete_outbox(remove.row_id, remove.lease_token)
    assert first.claim_outbox(lease_seconds=60).request_id == "older-writer"


def test_racing_claimers_keep_each_message_in_queue_order(tmp_path):
    import threading

    store = BridgeStore(tmp_path)
    targets = [f"{100 + index}.000001" for index in range(4)]
    for round_ in range(3):
        for ts in targets:
            queue(store, ("reaction_add", "reaction_remove")[round_ % 2], ts=ts, request_id=f"{ts}/{round_}")
        send(store, f"send/{round_}")
    events = []

    def claimer():
        own, idle = BridgeStore(tmp_path), 0
        while idle < 100:
            item = own.claim_outbox(lease_seconds=60)
            if item is None:
                idle += 1
                time.sleep(0.002)
                continue
            idle = 0
            events.append(("start", item.request_id))
            time.sleep(0.005)
            events.append(("end", item.request_id))
            own.complete_outbox(item.row_id, item.lease_token)
    threads = [threading.Thread(target=claimer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert store.status()["outbox_delivered"] == 15
    for ts in targets:
        mine = [event for event in events if event[1].startswith(ts)]
        # One at a time, in queue order: each write to a message starts only after the previous one ended.
        assert mine == [(edge, f"{ts}/{round_}") for round_ in range(3) for edge in ("start", "end")]


# -- Tools ------------------------------------------------------------------------------------

def test_reaction_tool_only_queues_and_makes_no_claim(tmp_path, tool_json):
    async def run():
        api = _Api(tmp_path)
        _load_plugin().register(api)
        tool = api.tools["slack_reaction_add"][0]
        result = tool_json(tool(channel="C1", ts="1.0", name="eyes", request_id="r"))
        assert result["ok"] is True and result["state"] == "queued" and result["operation"] == "reaction_add"
        again = tool_json(tool(channel="C1", ts="1.0", name="eyes", request_id="r"))
        assert again["deduplicated"] is True
    asyncio.run(run())


def declared_tools():
    import yaml
    text = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    return {row["name"]: row for row in yaml.safe_load(text.split("---")[1])["tools"]}


def test_thread_reader_takes_a_reply_timestamp_but_cannot_promise_reactions(monkeypatch, tool_json):
    """Target context is proved here. Reactions are not: the same documented call may omit them."""
    async def run():
        from test_read_tools import with_tools
        answers = [
            [{"ts": "100.0", "thread_ts": "100.0", "user": "U9", "text": "root"},
             {"ts": "101.5", "thread_ts": "100.0", "user": "U2", "text": "reply",
              "reactions": [{"name": "eyes", "count": 2, "users": ["U1"]}]}],
            [{"ts": "101.5", "thread_ts": "100.0", "user": "U2", "text": "reply"}],
        ]
        seen = []

        def provider(request):
            seen.append((request.url.path, dict(request.url.params)))
            return httpx.Response(200, json={"ok": True, "messages": answers.pop(0), "has_more": False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            handler, _ = with_tools(monkeypatch, http)["slack_thread"]
            arguments = dict(channel_id="C1", thread_ts="101.5", oldest="101.5", latest="101.5", inclusive=True, limit=2)
            first, second = [tool_json(await handler(**arguments)) for _ in range(2)]
        assert seen[0] == ("/api/conversations.replies", {
            "channel": "C1", "ts": "101.5", "oldest": "101.5", "latest": "101.5", "inclusive": "true", "limit": "2"})
        reply = next(row for row in first["messages"] if row["ts"] == "101.5")
        assert reply["thread_ts"] == "100.0" and reply["user"] == "U2" and reply["text"] == "reply"
        assert reply["reactions"] == [{"name": "eyes", "count": 2, "users": ["U1"]}]
        # Same call, no array: this says nothing about the message's reactions.
        assert "reactions" not in second["messages"][0] and second["complete"] is True
    asyncio.run(run())


def test_reaction_reader_keeps_count_and_partial_users_apart(monkeypatch, tool_json):
    async def run():
        from test_read_tools import with_tools
        seen = []

        def provider(request):
            seen.append((request.method, request.url.path, dict(request.url.params)))
            return httpx.Response(200, json={"ok": True, "type": "message", "channel": "C1", "message": {
                "type": "message", "ts": "101.5", "thread_ts": "100.0", "user": "U2", "text": "reply",
                "files": [{"id": "F1", "name": "a.pdf", "url_private": "https://files.slack.com/secret"}],
                "reactions": [{"name": "eyes", "count": 5, "users": ["U_BOT", "U1"]},
                              {"name": "thumbsup::skin-tone-2", "count": 1, "users": ["U3"]}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            handler, metadata = with_tools(monkeypatch, http)["slack_reactions"]
            result = tool_json(await handler(channel_id="C1", ts="101.5"))
        assert seen == [("GET", "/api/reactions.get", {"channel": "C1", "timestamp": "101.5", "full": "true"})]
        assert result["ok"] is True and result["source"] == "reactions.get" and result["ts"] == "101.5"
        assert result["reactions"] == [{"name": "eyes", "count": 5, "users": ["U_BOT", "U1"]},
                                       {"name": "thumbsup::skin-tone-2", "count": 1, "users": ["U3"]}]
        assert "may list only some" in result["users_note"] and result["observed_at"]
        assert result["message"]["thread_ts"] == "100.0" and "secret" not in json.dumps(result)
        assert metadata["schema"]["required"] == ["channel_id", "ts"]
        assert metadata["schema"]["additionalProperties"] is False
    asyncio.run(run())


def test_message_without_reactions_is_an_empty_list_from_a_real_answer(monkeypatch, tool_json):
    async def run():
        from test_read_tools import with_tools
        answer = {"ok": True, "type": "message", "channel": "C1", "message": {"type": "message", "ts": "1.0", "text": "x"}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=answer))) as http:
            handler, _ = with_tools(monkeypatch, http)["slack_reactions"]
            assert tool_json(await handler(channel_id="C1", ts="1.0"))["reactions"] == []
    asyncio.run(run())


@pytest.mark.parametrize("answer,code", [
    (httpx.Response(200, json={"ok": False, "error": "message_not_found"}), "message_not_found"),
    (httpx.Response(200, json={"ok": False, "error": "channel_not_found"}), "channel_not_found"),
    (httpx.Response(200, json={"ok": False, "error": "missing_scope", "needed": "reactions:read"}), "missing_scope"),
    (httpx.Response(429, json={"ok": False, "error": "ratelimited"}, headers={"retry-after": "7"}), "ratelimited"),
    (httpx.Response(200, json={"ok": True, "type": "file", "file": {"id": "F1"}}), "missing_message"),
    (httpx.ConnectError("down"), "transport_error"),
])
def test_unknown_deleted_or_refused_message_returns_normally_and_is_never_empty(monkeypatch, tool_json, answer, code):
    async def run():
        from test_read_tools import with_tools
        calls = []

        def provider(request):
            calls.append(request.url.path)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            handler, _ = with_tools(monkeypatch, http)["slack_reactions"]
            result = tool_json(await handler(channel_id="C1", ts="1.0"))
        assert result["ok"] is False and result["error"]["code"] == code and "reactions" not in result
        assert len(calls) == 1
        if code == "ratelimited":
            assert result["error"]["retry_after"] == 7.0
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [{"channel_id": "C1", "ts": " "}, {"channel_id": " ", "ts": "1.0"}])
def test_reaction_reader_refuses_a_blank_address_before_any_request(monkeypatch, tool_json, arguments):
    async def run():
        from test_read_tools import with_tools

        def provider(_request):
            raise AssertionError("no provider call")
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            handler, _ = with_tools(monkeypatch, http)["slack_reactions"]
            assert tool_json(await handler(**arguments))["error"]["code"] == "configuration_or_argument"
    asyncio.run(run())


def test_read_surfaces_are_declared_reads_and_the_scope_is_already_requested():
    declared = declared_tools()
    for name in ("slack_reactions", "slack_thread"):
        assert declared[name]["effect"] == "read" and declared[name]["conversation"] == "channel_id"
    assert declared["slack_reactions"]["target"] == "channel_id"
    # The generic method stays a write whatever effect its caller selects, so it is no read surface.
    assert declared["slack_api"]["effect"] == "write"
    manifest = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text(encoding="utf-8"))
    assert "reactions:read" in manifest["oauth_config"]["scopes"]["bot"]


def test_capture_helper_does_not_read_when_a_record_exists(tmp_path):
    async def run():
        class NoReads:
            async def read_messages(self, *_a, **_k):
                raise AssertionError("a retained record costs no request")
        store = BridgeStore(tmp_path)
        ingest(store, message())
        retained = store.retained_message("T1", "C1", "100.000001")
        item = store.claim_inbox()
        observation = await capture_reaction_target(NoReads(), item, retained)
        assert observation["status"] == "available" and observation["thread_ts"] == "100.000001"
    asyncio.run(run())


# -- One address through dispatch, lookup and receipt -----------------------------------------

def legacy_row(store, *, request_id="react-1", operation="reaction_add", channel=" C1 ", ts=" 100.000001 ", name=" eyes "):
    """A row as the previous tool queued it: raw arguments in its payload, target and lane."""
    with sqlite3.connect(store.path) as db:
        db.execute(
            """INSERT INTO outbox(request_id,chunk_index,chunk_count,target,thread_ts,text,ordering_key,kind,operation,
                                  payload_json,origin_json,delivery_reporting_version,created_at,updated_at)
               VALUES(?,0,1,?,'','',?,'mutation',?,?,?,1,1,1)""",
            (request_id, channel, f"{channel}:", operation, json.dumps({"channel": channel, "ts": ts, "name": name}),
             json.dumps({"kind": "tool", "task_id": "task-1"})))


@pytest.mark.parametrize("source", ["tool", "queued_earlier"])
def test_padded_address_names_one_message_through_dispatch_lookup_hold_and_report(tmp_path, tool_json, source):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        ingest(store, message(thread_ts="99.0"))
        if source == "tool":
            api = _Api(tmp_path)
            _load_plugin().register(api)
            queued = tool_json(api.tools["slack_reaction_add"][0](channel=" C1 ", ts=" 100.000001 ", name=" eyes ",
                                                                  request_id="react-1"))
            assert queued["state"] == "queued"
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE outbox SET delivery_reporting_version=1")
                row = db.execute("SELECT target,ordering_key,payload_json FROM outbox").fetchone()
            assert row[0] == "C1" and row[1] == "reaction:C1:100.000001"
            assert json.loads(row[2]) == {"channel": "C1", "ts": "100.000001", "name": "eyes"}
        else:
            legacy_row(store)
        world = World(store, tmp_path)
        holds = []
        worker = world.outbound()
        await _discover_reporting(worker.host, store)
        real_hold = worker.host.place_hold

        async def recording(account, conversation, **thread):
            holds.append((conversation, thread))
            return await real_hold(account, conversation, **thread)
        worker.host.place_hold = recording
        for _ in range(3):
            await worker.process_once()
            await asyncio.sleep(0)
        await worker.aclose()
        assert holds == [("C1", {"thread_id": "99.0"})]  # the thread its receipt names, below
        assert json.loads(provider_writes(world)[0][2]) == {"channel": "C1", "timestamp": "100.000001", "name": "eyes"}
        assert part(store)["state"] == "delivered" and part(store)["target"] == "C1"
        [report] = world.reports
        valid_receipt(report)
        assert report["conversation_id"] == "C1" and report["thread_id"] == "99.0"
        assert report["message"]["reaction"] == {
            "schema_version": 1, "operation": "add", "new": [{"type": "name", "value": "eyes"}],
            "target": {"message_id": "100.000001", "resolution": "resolved"}, "effect": "applied"}
    asyncio.run(run())


def test_padded_earlier_row_that_ends_uncertain_reports_the_same_address(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        legacy_row(store, operation="reaction_remove")
        world = World(store, tmp_path)
        world.reaction_answers = [httpx.ReadTimeout("lost")]
        await outbound_pass(world)
        assert part(store)["state"] == "uncertain" and len(provider_writes(world)) == 1
        [report] = world.reports
        assert report["conversation_id"] == "C1"
        assert report["message"]["reaction"]["old"] == [{"type": "name", "value": "eyes"}]
        assert report["message"]["reaction"]["target"]["message_id"] == "100.000001"
    asyncio.run(run())


def test_other_mutations_keep_their_arguments_as_given(tmp_path):
    store = BridgeStore(tmp_path)
    store.enqueue_mutation(request_id="pin", operation="pin_add", payload={"channel": " C1 ", "ts": " 1.0 "})
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT target,payload_json FROM outbox").fetchone()
    assert row[0] == " C1 " and json.loads(row[1]) == {"channel": " C1 ", "ts": " 1.0 "}
    assert store.claim_outbox(lease_seconds=60).payload == {"channel": " C1 ", "ts": " 1.0 "}


def test_reaction_reader_answers_with_the_address_it_read(monkeypatch, tool_json):
    async def run():
        from test_read_tools import with_tools
        seen = []

        def provider(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"ok": True, "message": {"ts": "101.5", "reactions": []}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            handler, _ = with_tools(monkeypatch, http)["slack_reactions"]
            result = tool_json(await handler(channel_id=" C1 ", ts=" 101.5 "))
        assert seen == [{"channel": "C1", "timestamp": "101.5", "full": "true"}]
        assert result["channel_id"] == "C1" and result["ts"] == "101.5"
    asyncio.run(run())


# -- Documented limits of an earlier snapshot and of a running bridge -------------------------

def test_snapshot_saved_earlier_is_resubmitted_unchanged_on_any_host(tmp_path):
    """SKILL.md's exception: a retry keeps its observation, so a saved reaction row is not removed from it."""
    async def run():
        store = BridgeStore(tmp_path)
        ingest(store, message())
        saved = {"events": [{"source_event_id": "Ev-r", "event_type": "reaction_added"}], "pending_count": 1,
                 "complete": True}
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE inbox SET transport_queue_json=?", (json.dumps(saved),))
        world = World(store, tmp_path, identity=OLD_HOST, replies=[])
        await drive(world)
        assert world.turns[0]["event"]["conversation"]["transport_queue"] == saved
    asyncio.run(run())


def test_capability_lives_as_long_as_the_bridge_that_read_it(tmp_path):
    """SKILL.md's restart boundary: a supported or unsupported answer is kept; a restarted bridge asks again."""
    async def run():
        store = BridgeStore(tmp_path)
        world = World(store, tmp_path, identity=OLD_HOST)
        _, running = world.clients()
        await running.discover_delivery_support()
        world.identity = CAPABLE
        await running.discover_delivery_support()
        assert running.explicit_completion == 0 and running.reaction_context == 0
        _, restarted = world.clients()
        await restarted.discover_delivery_support()
        assert restarted.explicit_completion == 1 and restarted.reaction_context == 1
    asyncio.run(run())
