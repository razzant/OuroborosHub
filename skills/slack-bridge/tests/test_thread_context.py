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


def envelope(event_id="event-1", *, ts="100.000000", thread_ts="1.000000"):
    return {"type": "events_api", "envelope_id": "env-" + event_id,
            "payload": {"event_id": event_id, "team_id": "T1", "event": {
                "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel",
                "ts": ts, "thread_ts": thread_ts, "text": "Yes, that one."}}}


def ingest(store, value):
    store.ingest_envelope(value, parse_socket_envelope(value, bot_user_id="U_BOT"))


def snapshot(store, event_id="event-1"):
    with sqlite3.connect(store.path) as db:
        raw = db.execute("SELECT provider_context_json FROM inbox WHERE event_id=?", (event_id,)).fetchone()[0]
    return json.loads(raw)


def metadata_response(request):
    if request.url.path.endswith("users.info"):
        return httpx.Response(200, json={"ok": True, "user": {"id": "U1", "name": "reader"}})
    if request.url.path.endswith("conversations.info"):
        return httpx.Response(200, json={"ok": True, "channel": {"id": "C1", "is_private": True}})
    return None


def page(messages, cursor=""):
    return httpx.Response(200, json={"ok": True, "messages": messages, "has_more": bool(cursor),
                                    "response_metadata": {"next_cursor": cursor}})


def host_client(store, events, *, lose_first=False):
    def handler(request):
        if request.url.path == "/identity":
            return httpx.Response(200, json={"ok": True})
        event = json.loads(request.content)["event"]
        # The exact observation must already be durable before Host sees it.
        stored = snapshot(store, event["source_event_id"])
        if event["message"]["thread_id"]:
            assert stored["thread"]["observed_at"] == event["conversation"]["thread_history"]["observed_at"]
        events.append(event)
        if lose_first and len(events) == 1:
            raise httpx.ReadTimeout("Host reply lost", request=request)
        return httpx.Response(200, json={"status": "completed", "outcome": "silent", "text": "",
                                        "turn_ref": "turn-1", "work_ref": ""})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def worker(store, slack, host_http, tmp_path):
    host = LoopbackPresenceHostAdapter(binding_id="a" * 32, host_service_url="http://127.0.0.1:8877",
                                      skill_token="local-test", http_client=host_http)
    return InboundWorker(store, slack, host, staged_root=tmp_path / "staged")


def test_paginated_thread_recovers_unreceived_question_without_replaying_history(tmp_path):
    async def run():
        store, events, reads = BridgeStore(tmp_path), [], []
        ingest(store, envelope())
        question = "<@U_BOT> Which project would you choose? " + "Полный текст. " * 1500
        history = [{"ts": f"1.{i:06d}", "user": "U2", "text": f"Earlier reply {i}"} for i in range(25)]
        history[23]["text"] = question
        # A delayed event must see the current reply, including a later resolution.
        history[24] = {"ts": "101.000000", "user": "U2", "text": "Already resolved, thanks.",
                       "edited": {"user": "U2", "ts": "102.000000"}}
        history[23]["files"] = [{"id": "F1", "name": "brief.pdf", "mimetype": "application/pdf",
                                  "url_private": "https://files.slack.com/private-only",
                                  "url_private_download": "https://files.slack.com/private-download",
                                  "permalink_public": "https://slack-files.com/private-link",
                                  "preview": "private preview"}]

        def provider(request):
            metadata = metadata_response(request)
            if metadata is not None:
                return metadata
            assert request.url.path == "/api/conversations.replies" and request.method == "GET"
            params = dict(request.url.params)
            reads.append(params)
            assert params["channel"] == "C1" and params["ts"] == "1.000000"
            assert "latest" not in params and "oldest" not in params
            assert params["limit"] == "200"
            return page(history[22:]) if params.get("cursor") == "next" else page(history[:22], "next")

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                host_client(store, events) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            assert await worker(store, slack, host_http, tmp_path).process_once()
        thread = events[0]["conversation"]["thread_history"]
        assert thread["status"] == "available" and thread["source"] == "conversations.replies"
        assert thread["data"]["complete"] is True and thread["data"]["pages_read"] == 2
        assert thread["data"]["next_cursor"] is None and "continuation" not in thread
        assert len(thread["data"]["messages"]) == 25
        assert thread["data"]["messages"][23]["text"] == question
        assert thread["data"]["messages"][24] == history[24]
        assert thread["scope"] == "current_accessible_thread"
        assert thread["observed_at"] <= thread["completed_at"]
        assert thread["data"]["messages"][23]["files"] == [
            {"id": "F1", "name": "brief.pdf", "mimetype": "application/pdf"}]
        assert "private-only" not in json.dumps(events[0])
        assert "private-download" not in json.dumps(events[0])
        assert "private-link" not in json.dumps(events[0]) and "private preview" not in json.dumps(events[0])
        assert snapshot(store)["thread"]["data"]["messages"] == history
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT event_id,state FROM inbox").fetchall() == [("event-1", "delivered")]
            assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        assert len(reads) == 2 and len(events) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing_scope", "rate_limited", "network", "timeout"])
def test_later_page_failure_retains_received_history_and_precise_continuation(tmp_path, monkeypatch, failure):
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.02 if timeout == 10.0 else timeout)

    if failure == "timeout":
        monkeypatch.setattr(asyncio, "wait_for", short_wait_for)

    async def run():
        store, events = BridgeStore(tmp_path), []
        ingest(store, envelope())
        first = [{"ts": "1.000000", "user": "U2", "text": "Earlier question"}]

        async def provider(request):
            metadata = metadata_response(request)
            if metadata is not None:
                return metadata
            if not request.url.params.get("cursor"):
                return page(first, "next")
            if failure == "timeout":
                await asyncio.sleep(1)
            if failure == "network":
                raise httpx.ReadError("unreachable", request=request)
            status = 429 if failure == "rate_limited" else 200
            code = "ratelimited" if failure == "rate_limited" else "missing_scope"
            return httpx.Response(status, headers={"Retry-After": "30"},
                                  json={"ok": False, "error": code, "needed": "groups:history"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                host_client(store, events) as host_http:
            assert await worker(store, SlackClient("xoxb-test", "xapp-test", http_client=slack_http),
                                host_http, tmp_path).process_once()
        thread = events[0]["conversation"]["thread_history"]
        assert thread["status"] == "partial" and thread["data"]["messages"] == first
        assert thread["data"]["complete"] is False and thread["data"]["pages_read"] == 1
        assert thread["continuation"] == {"tool": "slack_thread", "arguments": {
            "channel_id": "C1", "thread_ts": "1.000000", "limit": 200, "cursor": "next"}}
        expected = {"missing_scope": "missing_scope", "rate_limited": "ratelimited",
                    "network": "transport_error", "timeout": "lookup_timeout"}[failure]
        assert thread["error"]["code"] == expected
        assert events[0]["text"] == "Yes, that one." and store.status()["inbox_delivered"] == 1
        assert snapshot(store)["thread"] == thread
    asyncio.run(run())


def test_first_page_scope_refusal_does_not_block_original_event(tmp_path):
    async def run():
        store, events = BridgeStore(tmp_path), []
        ingest(store, envelope())

        def provider(request):
            metadata = metadata_response(request)
            return metadata if metadata is not None else httpx.Response(
                200, json={"ok": False, "error": "missing_scope", "needed": "groups:history"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                host_client(store, events) as host_http:
            assert await worker(store, SlackClient("xoxb-test", "xapp-test", http_client=slack_http),
                                host_http, tmp_path).process_once()
        thread = events[0]["conversation"]["thread_history"]
        assert thread["status"] == "unavailable" and thread["error"]["code"] == "missing_scope"
        assert "data" not in thread and thread["continuation"]["arguments"]["cursor"] == ""
        assert events[0]["text"] == "Yes, that one." and store.status()["inbox_delivered"] == 1
    asyncio.run(run())


def test_cursorless_partial_names_range_continuation_instead_of_repeating_first_page(tmp_path):
    async def run():
        store, events = BridgeStore(tmp_path), []
        ingest(store, envelope())

        def provider(request):
            metadata = metadata_response(request)
            if metadata is not None:
                return metadata
            return httpx.Response(200, json={"ok": True, "has_more": True, "messages": [
                {"ts": "1.000000", "user": "U2", "text": "Earlier question"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                host_client(store, events) as host_http:
            assert await worker(store, SlackClient("xoxb-test", "xapp-test", http_client=slack_http),
                                host_http, tmp_path).process_once()
        thread = events[0]["conversation"]["thread_history"]
        assert thread["status"] == "partial" and thread["data"]["complete"] is False
        assert thread["data"]["messages"][0]["text"] == "Earlier question"
        assert "without a cursor" in thread["data"]["continuation_note"]
        assert thread["continuation"]["requires"] == "explicit_timestamp_range"
        assert thread["continuation"]["filters"]["thread_ts"] == "1.000000"
        assert "arguments" not in thread["continuation"]
        assert store.status()["inbox_delivered"] == 1
    asyncio.run(run())


def test_reaction_on_unretained_message_reads_only_that_message_never_channel_history(tmp_path):
    async def run():
        store, events, reads = BridgeStore(tmp_path), [], []
        value = envelope(thread_ts="")
        value["payload"]["event"].update(type="reaction_added", reaction="eyes",
                                           item={"type": "message", "channel": "C1", "ts": "100.000000"})
        ingest(store, value)

        def provider(request):
            reads.append(request.url.path)
            response = metadata_response(request)
            if response is None:
                assert request.url.path.endswith("conversations.replies"), "never a history read"
                response = page([{"ts": "100.000000", "user": "U2", "text": "Yes, that one."}])
            return response

        def identity_aware(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"ok": True, "presence_reaction_context": 1})
            events.append(json.loads(request.content)["event"])
            return httpx.Response(200, json={"status": "completed", "outcome": "silent", "text": "",
                                             "turn_ref": "turn-1", "work_ref": ""})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(identity_aware)) as host_http:
            assert await worker(store, SlackClient("xoxb-test", "xapp-test", http_client=slack_http),
                                host_http, tmp_path).process_once()
        assert "thread" not in snapshot(store) and "thread_history" not in events[0]["conversation"]
        assert sorted(reads) == ["/api/conversations.info", "/api/conversations.replies", "/api/users.info"]
        assert events[0]["thread_id"] == "100.000000"
        assert store.status()["inbox_delivered"] == 1
    asyncio.run(run())


def test_retry_after_restart_reuses_thread_snapshot_and_next_event_refreshes(tmp_path):
    async def run():
        store, events, reads = BridgeStore(tmp_path), [], []
        ingest(store, envelope())
        current_text = "Question at first capture"

        def provider(request):
            metadata = metadata_response(request)
            if metadata is not None:
                return metadata
            reads.append(dict(request.url.params))
            return page([{"ts": "1.000000", "user": "U2", "text": current_text}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                host_client(store, events, lose_first=True) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            assert await worker(store, slack, host_http, tmp_path).process_once()
            saved = snapshot(store)
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE inbox SET available_at=0")
            current_text = "Question after edit"
            reopened = BridgeStore(tmp_path)
            assert await worker(reopened, slack, host_http, tmp_path).process_once()
            assert snapshot(reopened) == saved and events[0] == events[1] and len(reads) == 1
            ingest(reopened, envelope("event-2", ts="101.000000"))
            assert await worker(reopened, slack, host_http, tmp_path).process_once()
        assert len(events) == 3 and len(reads) == 2
        assert events[2]["conversation"]["thread_history"]["data"]["messages"][0]["text"] == current_text
        assert "latest" not in reads[1] and "oldest" not in reads[1]
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
    asyncio.run(run())


@pytest.mark.parametrize("event_type", ["message", "app_mention"])
@pytest.mark.parametrize("has_reply", [False, True])
def test_root_message_reads_current_thread_or_singleton_before_submission(tmp_path, event_type, has_reply):
    async def run():
        store, events, reads = BridgeStore(tmp_path), [], []
        value = envelope(thread_ts="")
        original = value["payload"]["event"]
        original.update(type=event_type, text="<@U_BOT> Which approach should we use?")
        original.pop("thread_ts")  # Slack root events have no thread yet at receipt.
        ingest(store, value)
        messages = [{"ts": original["ts"], "user": "U1", "text": original["text"]}]
        if has_reply:
            messages.append({"ts": "101.000000", "thread_ts": original["ts"], "user": "U2",
                             "text": "Already resolved; please do not send anything."})

        def provider(request):
            metadata = metadata_response(request)
            if metadata is not None:
                return metadata
            assert request.url.path == "/api/conversations.replies"
            parameters = dict(request.url.params)
            assert parameters["ts"] == original["ts"]
            assert "latest" not in parameters and "oldest" not in parameters
            reads.append(parameters)
            return page(messages)

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                host_client(store, events) as host_http:
            assert await worker(store, SlackClient("xoxb-test", "xapp-test", http_client=slack_http),
                                host_http, tmp_path).process_once()
        thread = events[0]["conversation"]["thread_history"]
        assert thread["status"] == "available" and thread["data"]["complete"] is True
        assert thread["data"]["thread_ts"] == original["ts"]
        assert thread["data"]["messages"] == messages
        assert snapshot(store)["thread"] == thread
        assert events[0]["text"] == original["text"] and len(reads) == 1
        # History is context, including a later cancellation, never more inbox work.
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT event_id,state FROM inbox").fetchall() == [("event-1", "delivered")]
            assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    asyncio.run(run())
