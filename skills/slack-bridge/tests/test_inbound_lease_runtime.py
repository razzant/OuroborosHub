"""Finite continuation lease budgets through the actual companion consumers.

BridgeRuntime's ordinary four inbound/two outbound loops, the SQLite store,
SlackClient and LoopbackPresenceHostAdapter are real. Only Socket Mode and HTTP
providers are synthetic; store time is a logical clock. These schedules do not
claim a total HTTP deadline or protection from arbitrary suspension.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lib import store as store_module
from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import BridgeRuntime, _INBOUND_LEASE_SECONDS
from lib.slack_api import SlackClient
from lib.store import BridgeStore


class _Clock:
    now = 10000.0

    def time(self):
        return self.now


def _envelope(event_id="Ev1", *, file=False):
    event = {"type": "message", "channel_type": "im", "user": "U1", "channel": "D1",
             "ts": "1.1" if event_id == "Ev1" else "2.1", "text": "lease fixture"}
    if file:
        event["files"] = [{"id": "F1", "name": "fixture.txt", "mimetype": "text/plain",
                           "size": 11, "url_private": "https://files.slack.com/fixture"}]
    return {"type": "events_api", "envelope_id": "env-" + event_id,
            "payload": {"event_id": event_id, "team_id": "T1", "event": event}}


async def _until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 5)


class _Consumer:
    def __init__(self, root, monkeypatch, *, clock_step=False, reporting=1):
        self.clock = _Clock()
        monkeypatch.setattr(store_module, "time", self.clock)
        self.store = BridgeStore(root)
        self.clock_step, self.reporting = clock_step, reporting
        self.claims, self.retries, self.submits, self.posts = [], [], [], []
        self.queue_reports, self.delivery_reports = [], []
        self.polls, self.file_requests = 0, 0
        self.poll_started, self.takeover = asyncio.Event(), asyncio.Event()
        self.release_first, self.release_second = asyncio.Event(), asyncio.Event()
        self.stale_checkpoint = asyncio.Event()
        self.socket_closed = self.socket_cancelled = False
        self.socket_finish = asyncio.Event()
        claim = self.store.claim_inbox
        retry = self.store.retry_inbox
        checkpoint = self.store.set_transport_queue_report

        def observed_claim(*, lease_seconds):
            item = claim(lease_seconds=lease_seconds)
            if item is not None:
                self.claims.append({"event_id": item.event_id, "token": item.lease_token,
                                    "at": self.clock.now, "lease_seconds": lease_seconds,
                                    "task": asyncio.current_task().get_name()})
            return item

        def observed_retry(row_id, token, *args, **kwargs):
            self.retries.append(token)
            return retry(row_id, token, *args, **kwargs)

        def observed_checkpoint(row_id, token, snapshot):
            try:
                return checkpoint(row_id, token, snapshot)
            finally:
                if (self.clock_step and snapshot is None and len(self.claims) >= 2
                        and token == self.claims[0]["token"]):
                    self.stale_checkpoint.set()

        monkeypatch.setattr(self.store, "claim_inbox", observed_claim)
        monkeypatch.setattr(self.store, "retry_inbox", observed_retry)
        monkeypatch.setattr(self.store, "set_transport_queue_report", observed_checkpoint)
        self.host_http = httpx.AsyncClient(transport=httpx.MockTransport(self.host), trust_env=False)
        self.slack_http = httpx.AsyncClient(transport=httpx.MockTransport(self.slack), trust_env=False)
        adapter = LoopbackPresenceHostAdapter(binding_id="b" * 32, host_service_url="http://127.0.0.1:1",
                                              skill_token="synthetic", http_client=self.host_http)
        slack = SlackClient("xoxb-synthetic", "xapp-synthetic", http_client=self.slack_http)
        self.runtime = BridgeRuntime(store=self.store, slack=slack, host=adapter, bot_user_id="BOT")
        self.runtime.socket = self
        self.task = None

    def ingest(self, event_id="Ev1", *, file=False):
        envelope = _envelope(event_id, file=file)
        self.store.ingest_envelope(envelope, parse_socket_envelope(envelope))

    def row(self, event_id="Ev1"):
        with self.store._connect() as db:
            return dict(db.execute("SELECT * FROM inbox WHERE event_id=?", (event_id,)).fetchone())

    def outbox(self):
        with self.store._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY id")]

    async def run(self):
        try:
            await self.socket_finish.wait()
        except asyncio.CancelledError:
            self.socket_cancelled = True
            raise

    async def close(self):
        self.socket_closed = True

    def start(self):
        self.task = asyncio.create_task(self.runtime.run())

    async def finish(self):
        self.release_first.set()
        self.release_second.set()
        self.runtime._stop.set()
        self.socket_finish.set()
        try:
            if self.task is not None:
                # Cleanup also collects the original runtime error on a red run.
                await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)
        finally:
            await self.host_http.aclose()
            await self.slack_http.aclose()

    async def host(self, request):
        path = request.url.path
        if path == "/identity":
            return httpx.Response(200, json={"ok": True, "presence_delivery_version": self.reporting,
                                           "presence_continuation_version": 1})
        if path == "/presence/delivery":
            self.delivery_reports.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "recorded": True})
        if path == "/presence/turn":
            body = json.loads(request.content)
            self.submits.append(body)
            if body["event"]["source_event_id"] == "Ev2":
                return httpx.Response(200, json={"ok": True, "status": "completed", "outcome": "silent",
                                               "turn_ref": "other", "text": "", "work_ref": ""})
            if not self.clock_step:
                self.clock.now += 1790
            return httpx.Response(200, json={"ok": True, "status": "continuing", "outcome": "message",
                                           "text": "Early output", "turn_ref": "author", "work_ref": "",
                                           "continuation_version": 1, "continuation_ref": "author",
                                           "output_ref": "released-output", "delivery_reporting_version": self.reporting})
        assert path == "/presence/work/author"
        if request.method == "POST":
            self.queue_reports.append(json.loads(request.content))
            if not self.clock_step:
                self.clock.now += 9
            return httpx.Response(200, json={"ok": True, "status": "recorded"})
        self.polls += 1
        if self.polls == 1:
            self.clock.now += 2101 if self.clock_step else 5
            self.poll_started.set()
            await self.release_first.wait()
        else:
            self.takeover.set()
            await self.release_second.wait()
        return httpx.Response(202, json={"ok": True, "status": "pending", "work_ref": "author",
                                       "continuation_version": 1, "continuation_ref": "author",
                                       "delivery_reporting_version": self.reporting,
                                       "outputs": [{"outcome": "message", "output_ref": "released-output",
                                                    "text": "Early output"}]})

    async def slack(self, request):
        if request.url.host == "files.slack.com":
            self.file_requests += 1
            clock = self.clock

            class FiniteBytes(httpx.AsyncByteStream):
                async def __aiter__(self):
                    for _ in range(11):
                        clock.now += 27
                        yield b"x"
                        await asyncio.sleep(0)

            return httpx.Response(200, stream=FiniteBytes())
        if request.url.path.endswith("users.info"):
            return httpx.Response(200, json={"ok": True, "user": {"id": "U1"}})
        if request.url.path.endswith("conversations.info"):
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D1"}})
        assert request.url.path.endswith("chat.postMessage")
        self.posts.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": "D1", "ts": "3.1"})


@pytest.mark.parametrize("reporting", [0, 1])
def test_finite_overhead_refreshes_owned_budget_and_delivers_early_output(tmp_path, monkeypatch, reporting):
    async def run():
        bridge = _Consumer(tmp_path, monkeypatch, reporting=reporting)
        bridge.ingest(file=True)
        bridge.start()
        try:
            await asyncio.wait_for(bridge.poll_started.wait(), 5)
            # All real idle peers get a normal 250ms worker-loop claim opportunity.
            await asyncio.sleep(0.35)
            assert len(bridge.claims) == 1, bridge.claims
            assert bridge.clock.now - 10000 == 297 + 1790 + 9 + 5 == 2101
            assert _INBOUND_LEASE_SECONDS == 2100
            assert bridge.row()["lease_until"] == 10000 + 297 + 1790 + 2100
            assert not bridge.task.done() and not bridge.socket_closed
            await _until(lambda: len(bridge.posts) == 1)
            assert bridge.posts[0]["markdown_text"] == "Early output"
            durable = bridge.row()
            assert durable["host_reference"].startswith("continuing:")
            assert json.loads(durable["staged_files_json"])
            bridge.release_first.set()
            await _until(lambda: bridge.row()["state"] == "pending")
            final = bridge.row()
            assert final["lease_token"] == "" and final["lease_until"] == 0
            assert final["host_reference"] == durable["host_reference"]
            assert final["staged_files_json"] == durable["staged_files_json"]
            assert final["attempts"] == 1 and bridge.polls == 1
            assert bridge.file_requests == len(bridge.submits) == len(bridge.queue_reports) == 1
            assert len(bridge.outbox()) == 1
            assert bridge.outbox()[0]["output_ref"] == "released-output"
            assert bridge.outbox()[0]["state"] == "delivered"
            if reporting:
                await _until(lambda: bool(bridge.delivery_reports))
                assert bridge.delivery_reports[0]["message"]["output_ref"] == "released-output"
            else:
                assert bridge.delivery_reports == []
        finally:
            await bridge.finish()

    asyncio.run(run())


def test_post_renewal_takeover_preserves_new_owner_and_companion_workers(tmp_path, monkeypatch):
    async def run():
        bridge = _Consumer(tmp_path, monkeypatch, clock_step=True)
        bridge.ingest()
        bridge.start()
        try:
            await asyncio.wait_for(bridge.takeover.wait(), 5)
            old, new = bridge.claims
            assert new["at"] - old["at"] == 2101
            assert new["token"] != old["token"] and new["task"] != old["task"]
            owned = bridge.row()
            assert owned["lease_token"] == new["token"] and owned["attempts"] == 2
            bridge.release_first.set()
            await asyncio.wait_for(bridge.stale_checkpoint.wait(), 5)
            await asyncio.sleep(0)
            assert old["token"] not in bridge.retries
            assert not bridge.task.done() and not bridge.socket_closed and not bridge.runtime._stop.is_set()
            # A real companion inbound loop must process a new conversation turn
            # while the new owner is still blocked inside its one finite GET.
            bridge.ingest("Ev2")
            await _until(lambda: bridge.row("Ev2")["state"] == "delivered")
            assert bridge.row() == owned
            assert not bridge.task.done() and not bridge.socket_closed
            assert [item["event"]["source_event_id"] for item in bridge.submits] == ["Ev1", "Ev2"]
            assert bridge.polls == 2
            bridge.release_second.set()
            await _until(lambda: bridge.row()["state"] == "pending")
            assert bridge.retries == [new["token"]]
            await _until(lambda: len(bridge.posts) == 1)
            assert len(bridge.outbox()) == 1
        finally:
            await bridge.finish()

    asyncio.run(run())
