"""Who an event names: mentions and thread authors carry names, observed once per person and kept dated."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import httpx
import pytest

from lib import provider_context
from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter, slack_presence_event
from lib.people import NAME_MAX_AGE_SEC, directory_key
from lib.provider_context import capture_context
from lib.runtime import InboundWorker
from lib.slack_api import SlackApiError, SlackClient
from lib.store import BridgeStore

TOKEN = "xoxb-test"
PEOPLE = {
    "U_OWNER": {"id": "U_OWNER", "name": "anton", "profile": {"display_name": "Anton", "real_name": "Anton R."}},
    "U_SASHA": {"id": "U_SASHA", "name": "a.ochkanov", "profile": {"display_name": "", "real_name": "Alexander Ochkanov"}},
    "U_BOT": {"id": "U_BOT", "name": "ouroboros2", "is_bot": True, "profile": {"display_name": "Ouroboros"}},
}
I1_TEXT = "<@U_SASHA> привет! Давай обновим мой эксперимент?"


def _envelope(event_id, ts, text, *, thread_ts="10.0", parent="U_BOT", user="U_OWNER", channel_type="channel"):
    event = {"type": "message", "user": user, "channel": "C1", "channel_type": channel_type, "ts": ts, "text": text}
    if thread_ts:
        event.update(thread_ts=thread_ts, parent_user_id=parent)
    return {"type": "events_api", "envelope_id": "env-" + event_id,
            "payload": {"team_id": "T1", "event_id": event_id, "event": event}}


def _item(store, payload):
    store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))
    return store.claim_inbox()


class _Slack:
    """users.info answers from PEOPLE; Slack limits the rate for an id in ``refuse`` and knows no other id."""
    bot_token = TOKEN

    def __init__(self, thread=(), refuse=(), delay=0.0):
        self.thread, self.refuse, self.delay = list(thread), set(refuse), delay
        self.reads, self.in_flight, self.most = [], 0, 0

    async def user_info(self, user_id):
        self.reads.append(user_id)
        self.in_flight += 1
        self.most = max(self.most, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if user_id in self.refuse:
                raise SlackApiError("ratelimited", status_code=429, retry_after=30)
            if user_id not in PEOPLE:
                raise SlackApiError("user_not_found")
            return PEOPLE[user_id]
        finally:
            self.in_flight -= 1

    async def conversation_info(self, channel_id):
        return {"id": channel_id, "is_channel": True}

    async def read_messages(self, **_arguments):
        return {"messages": list(self.thread), "complete": True, "next_cursor": None}


def _event(item, snapshot):
    return slack_presence_event(dataclasses.replace(item, provider_context=snapshot), places=True)


I1_THREAD = [{"ts": "10.0", "user": "U_BOT", "text": "JEML-2574 [ML] Nova v1"},
             {"ts": "11.0", "user": "U_BOT", "text": "<@U_OWNER> MR: !495"},
             {"ts": "12.0", "user": "U_OWNER", "text": I1_TEXT}]


def test_the_i1_reply_names_the_colleague_it_mentions_and_who_wrote_the_thread(tmp_path):
    """06.11 I1 through the real worker: Anton wrote to Alexander in a thread Ouroboros started."""
    async def run():
        store, events, reads = BridgeStore(tmp_path), [], []
        item_payload = _envelope("Ev-1", "12.0", I1_TEXT)
        store.ingest_envelope(item_payload, parse_socket_envelope(item_payload, bot_user_id="U_BOT"))

        def provider(request):
            method, params = request.url.path.rsplit("/", 1)[-1], request.url.params
            if method == "users.info":
                reads.append(params["user"])
                return httpx.Response(200, json={"ok": True, "user": PEOPLE[params["user"]]})
            if method == "conversations.info":
                return httpx.Response(200, json={"ok": True, "channel": {"id": "C1", "is_private": True}})
            return httpx.Response(200, json={"ok": True, "messages": I1_THREAD, "has_more": False})

        def host(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"places_version": 1})
            events.append(json.loads(request.content)["event"])
            return httpx.Response(200, json={"status": "completed", "outcome": "silent", "turn_ref": "t"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(host)) as host_http:
            slack = SlackClient(TOKEN, "xapp-test", http_client=slack_http)
            adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:8767",
                                                  skill_token="test", http_client=host_http)
            worker = InboundWorker(store, slack, adapter, staged_root=tmp_path / "staged")
            assert await worker.process_once()
            first = list(reads)
            # The next message in the thread names the same people: only its author's own lookup reads Slack.
            later = _envelope("Ev-2", "13.0", "<@U_SASHA> ещё одно")
            store.ingest_envelope(later, parse_socket_envelope(later, bot_user_id="U_BOT"))
            assert await worker.process_once()
        event = events[0]
        assert event["facts"] == ["any_message", "my_thread"]
        assert event["message"]["mentions"] == [{"id": "U_SASHA", "name": "Alexander Ochkanov", "is_self": False}]
        assert event["conversation"]["thread_history"]["authors"] == [
            {"id": "U_BOT", "name": "Ouroboros", "is_self": True}, {"id": "U_OWNER", "name": "Anton", "is_self": False}]
        # The provider's own facts are untouched: the occurrence and the root's author, never an addressee.
        assert event["message"]["provider_facts"]["mentioned_user_ids"] == ["U_SASHA"]
        assert event["message"]["provider_facts"]["parent_user_id"] == "U_BOT"
        assert sorted(first) == ["U_BOT", "U_OWNER", "U_SASHA"] and reads[len(first):] == ["U_OWNER"]
        assert events[1]["message"]["mentions"] == events[0]["message"]["mentions"]
        assert events[1]["conversation"]["thread_history"]["authors"] == event["conversation"]["thread_history"]["authors"]
    asyncio.run(run())


def test_each_name_is_dated_and_the_actor_lookup_is_never_repeated(tmp_path):
    store = BridgeStore(tmp_path)
    item = _item(store, _envelope("Ev-1", "12.0", "<@U_OWNER> and <@U_BOT>, look", parent="U_SASHA"))
    slack = _Slack(thread=[{"ts": "10.0", "user": "U_SASHA", "text": "root"}, {"ts": "12.0", "user": "U_OWNER"}])
    snapshot = asyncio.run(capture_context(slack, item, "Workspace", names=store))
    people = snapshot["people"]["users"]
    assert list(people) == ["U_BOT", "U_OWNER", "U_SASHA"]  # mentions in their order, then thread authors
    assert all(entry["source"] == "users.info" and entry["observed_at"] for entry in people.values())
    assert sorted(slack.reads) == ["U_BOT", "U_OWNER", "U_SASHA"]  # the actor U_OWNER once, from its own lookup
    event = _event(item, snapshot)
    assert event["message"]["mentions"] == [{"id": "U_BOT", "name": "Ouroboros", "is_self": True},
                                            {"id": "U_OWNER", "name": "Anton", "is_self": False}]
    assert "mention" in event["facts"] and "my_thread" not in event["facts"]


def test_a_failed_read_keeps_an_older_name_and_otherwise_stays_unnamed(tmp_path):
    store = BridgeStore(tmp_path)
    old = time.time() - NAME_MAX_AGE_SEC - 60
    store.save_directory_snapshot(directory_key("user_name", TOKEN) + ":U_SASHA",
                                  {"observed_at": old, "user_id": "U_SASHA", "name": "Sasha", "source": "users.info"})
    item = _item(store, _envelope("Ev-1", "12.0", "<@U_SASHA> <@U_GONE> hi", thread_ts=""))
    slack = _Slack(refuse={"U_SASHA"})
    snapshot = asyncio.run(capture_context(slack, item, "Workspace", names=store))
    sasha, gone = snapshot["people"]["users"]["U_SASHA"], snapshot["people"]["users"]["U_GONE"]
    assert sasha["name"] == "Sasha" and sasha["refresh_error"]["code"] == "ratelimited"
    assert gone["name"] is None and gone["error"] == {"code": "user_not_found", "http_status": 0, "retry_after": 0.0}
    assert _event(item, snapshot)["message"]["mentions"] == [
        {"id": "U_GONE", "name": None, "is_self": False}, {"id": "U_SASHA", "name": "Sasha", "is_self": False}]
    # A name observed within the window answers without a read; the failed one is not kept as a newer name.
    store.save_directory_snapshot(directory_key("user_name", TOKEN) + ":U_GONE",
                                  {"observed_at": time.time(), "user_id": "U_GONE", "name": "Gone", "source": "users.info"})
    again = _Slack(refuse={"U_SASHA"})
    snapshot = asyncio.run(capture_context(again, item, "Workspace", names=store))
    assert snapshot["people"]["users"]["U_GONE"]["name"] == "Gone" and again.reads == ["U_OWNER", "U_SASHA"]


def test_the_directory_scan_names_a_person_without_a_read_and_a_rotated_credential_does_not(tmp_path):
    store = BridgeStore(tmp_path)
    store.save_directory_snapshot(directory_key("user", TOKEN), {
        "observed_at": time.time(), "complete": True, "from_start": True,
        "entries": [{"id": "U_X", "profile": {"display_name": "Xena"}}]})
    item = _item(store, _envelope("Ev-1", "12.0", "<@U_X> ping", thread_ts=""))
    slack = _Slack()
    snapshot = asyncio.run(capture_context(slack, item, "Workspace", names=store))
    assert snapshot["people"]["users"]["U_X"]["name"] == "Xena" and snapshot["people"]["users"]["U_X"]["source"] == "users.list"
    assert "U_X" not in slack.reads
    other = _Slack()
    other.bot_token = "xoxb-rotated"
    rotated = asyncio.run(capture_context(other, item, "Workspace", names=store))
    assert "U_X" in other.reads and rotated["people"]["users"]["U_X"]["name"] is None


def test_reads_are_bounded_in_number_at_once_and_in_time(tmp_path, monkeypatch):
    store = BridgeStore(tmp_path)
    authors = [f"U_A{index}" for index in range(10)]
    item = _item(store, _envelope("Ev-1", "12.0", "hi", parent="U_ROOT"))
    thread = [{"ts": f"{index}.0", "user": user, "text": "x"} for index, user in enumerate(authors)]
    slack = _Slack(thread=thread, delay=0.01)
    snapshot = asyncio.run(capture_context(slack, item, "Workspace", names=store))
    assert slack.most <= 4 and len(snapshot["people"]["users"]) == 10
    # The reads share one provider-I/O budget; whoever is still unread when it ends keeps no name.
    monkeypatch.setattr(provider_context, "_NAME_BUDGET_SEC", 0.05)
    slow = _Slack(delay=1.0)
    started = time.monotonic()
    people = asyncio.run(provider_context.capture_people(slow, authors))
    assert time.monotonic() - started < 1.0 and slow.in_flight == 0
    assert {entry["error"]["code"] for entry in people["users"].values()} == {"lookup_timeout"}
    late = _event(item, {"thread": {"status": "available", "data": {"messages": thread}}, "people": people})
    assert late["conversation"]["thread_history"]["authors"][0] == {"id": "U_A0", "name": None, "is_self": False}


def test_a_rate_limit_stops_the_reads_still_waiting(tmp_path):
    authors = [f"U_A{index}" for index in range(10)]
    limited = _Slack(delay=0.01, refuse=set(authors))
    people = asyncio.run(provider_context.capture_people(limited, authors))
    # The four reads already in flight finish; the six still waiting carry the refusal and are never sent.
    assert len(limited.reads) == 4
    assert {entry["error"]["code"] for entry in people["users"].values()} == {"ratelimited"}
    # Another refusal does not stop the rest: each id is still read once.
    other = _Slack(delay=0.01)
    people = asyncio.run(provider_context.capture_people(other, authors))
    assert sorted(other.reads) == sorted(authors)
    assert {entry["error"]["code"] for entry in people["users"].values()} == {"user_not_found"}


def test_an_event_captured_before_names_keeps_its_view_and_one_without_people_reads_no_one(tmp_path):
    store = BridgeStore(tmp_path)
    item = _item(store, _envelope("Ev-1", "12.0", "<@U_SASHA> hi"))
    earlier = {"user": {"status": "unavailable"}, "conversation": {"status": "unavailable"},
               "thread": {"status": "available", "data": {"messages": I1_THREAD}}}
    event = _event(item, earlier)
    assert "mentions" not in event["message"] and "authors" not in event["conversation"]["thread_history"]
    plain = _item(BridgeStore(tmp_path / "plain"), _envelope("Ev-2", "5.0", "no one named", thread_ts="", channel_type="im"))
    slack = _Slack()
    snapshot = asyncio.run(capture_context(slack, plain, "Workspace", names=store))
    assert "people" not in snapshot and slack.reads == ["U_OWNER"]


@pytest.mark.parametrize("self_id", ["U_BOT", ""])
def test_is_self_marks_only_the_bots_own_known_id(tmp_path, self_id):
    store = BridgeStore(tmp_path)
    payload = _envelope("Ev-1", "12.0", "<@U_BOT> <@U_SASHA>", thread_ts="")
    store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id=self_id))
    item = store.claim_inbox()
    snapshot = asyncio.run(capture_context(_Slack(), item, "Workspace", names=store))
    marks = {entry["id"]: entry["is_self"] for entry in _event(item, snapshot)["message"]["mentions"]}
    assert marks == {"U_BOT": bool(self_id), "U_SASHA": False}
