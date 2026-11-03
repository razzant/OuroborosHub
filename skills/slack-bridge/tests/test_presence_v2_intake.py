"""Presence continuation version 2 (timely intake) through the production bridge path.

Real BridgeStore, SocketModeClient, intake/observation InboundWorkers, BridgeRuntime
loops, OutboundWorker, SlackClient and LoopbackPresenceHostAdapter run against local
httpx transports. The Host answers are scripted here in the shapes of the v2 wire
contract (early custody ACK, heard/context completion, typed 429 capacity retry,
``reply_anchor`` on author outputs); they are not produced by a real Host process.
Nothing leaves the process.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from lib import store as store_module
from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import BridgeRuntime, InboundWorker
from lib.store import BridgeStore
from test_presence_continuation import (
    BINDING, TOKEN, Bridge, FakeHost, FakeSlack, _origin, child_done, child_pending, envelope, out,
)


# ---- the v2 wire, produced locally ---------------------------------------------------------

def ack(task: str, request: dict) -> dict:
    """A: early custody acknowledgement once the author's intake is open."""
    return {"ok": True, "status": "continuing", "outcome": "deferred", "text": "", "output_ref": "",
            "turn_ref": task, "continuation_ref": task, "work_ref": "", "continuation_version": 2,
            "delivery_reporting_version": request.get("delivery_reporting_version", 0)}


def heard(request: dict, task: str = "") -> dict:
    """B: heard, or delivered as context to the conversation's current author."""
    return {"ok": True, "status": "completed", "outcome": "silent", "text": "", "turn_ref": task,
            "work_ref": "", "output_ref": "", "continuation_ref": "", "continuation_version": 2,
            "delivery_reporting_version": request.get("delivery_reporting_version", 0)}


def answered(task: str, request: dict, text: str, output_ref: str, anchor: dict) -> dict:
    """C: the author ended before opening intake."""
    return {**heard(request, task), "outcome": "message", "text": text, "output_ref": output_ref,
            "reply_anchor": anchor}


def busy(field: str) -> tuple[int, dict]:
    """D: one non-blocking admission attempt found a resource held; nothing ran."""
    return 429, {"ok": False, "error": f"presence_capacity_full: {field}", "code": "presence_capacity_full",
                 "disposition": "retry", "field": field, "retry_after": 1 if field == "conversation" else 5,
                 "attachment_manifest": []}


def anchored(output_ref: str, text: str, anchor: dict, outcome: str = "message") -> dict:
    return {**out(output_ref, text, outcome), "reply_anchor": anchor}


def _author(ref: str, outputs: list, mode: int, version: int) -> dict:
    return {"ok": True, "work_ref": ref, "continuation_ref": ref, "task_id": ref, "continuation_version": version,
            "outputs": outputs, "delivery_reporting_version": mode}


def pending(ref: str, outputs: list, *, child: str = "", mode: int = 1, version: int = 2) -> tuple[int, dict]:
    return 202, {**_author(ref, outputs, mode, version), "status": "pending", "child_work_ref": child}


def terminal(ref: str, outputs: list, *, status: str = "completed", outcome: str = "silent", text: str = "",
             output_ref: str = "", anchor: dict | None = None, child: str = "", mode: int = 1,
             version: int = 2, **facts) -> tuple[int, dict]:
    body = {**_author(ref, outputs, mode, version), "status": status, "outcome": outcome, "text": text,
            "output_ref": output_ref, "child_work_ref": child, **facts}
    if anchor is not None:
        body["reply_anchor"] = anchor
    return 200, body


class V2Host(FakeHost):
    """The scripted Host advertising ``presence_continuation_versions``.

    ``busy[source]`` answers typed 429s before the event's real answer, writing no
    durable answer (no task row). ``turn_status[source]`` sends the stored answer
    with another HTTP status (a typed refusal). A gated work GET is held, before
    it is recorded in ``polls``, until released.
    """

    def __init__(self, *, versions=(1, 2), scalar: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.versions, self.scalar = versions, scalar
        self.busy: dict[str, list[tuple[int, dict]]] = {}
        self.turn_status: dict[str, int] = {}
        self.turn_order: list[str] = []
        self.poll_gates: dict[str, asyncio.Event] = {}
        self.poll_entered: dict[str, asyncio.Event] = {}

    def gate_poll(self, ref: str) -> None:
        self.poll_gates[ref], self.poll_entered[ref] = asyncio.Event(), asyncio.Event()

    def anchor(self, source: str) -> dict:
        """The exact captured provider message object of that event."""
        return {"source_event_id": source, "message": self.body(source)["event"]["message"]}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/identity":
            facts = {"ok": True, "name": "fixture"}
            if self.reporting:
                facts["presence_delivery_version"] = 1
            if self.scalar:
                facts["presence_continuation_version"] = 1
            if self.versions is not None:
                facts["presence_continuation_versions"] = list(self.versions)
            return httpx.Response(200, json=facts)
        if path == "/presence/turn":
            body = json.loads(request.content)
            source = body["event"]["source_event_id"]
            self.turn_order.append(source)
            if self.busy.get(source):
                self.turn_bodies.append(body)
                status, payload = self.busy[source].pop(0)
                return httpx.Response(status, json=payload)
            response = await super().handle(request)
            return httpx.Response(self.turn_status.get(source, response.status_code), json=response.json())
        if path.startswith("/presence/work/") and request.method == "GET":
            ref = path.removeprefix("/presence/work/")
            if ref in self.poll_gates:
                self.poll_entered[ref].set()
                await self.poll_gates[ref].wait()
        return await super().handle(request)


class SplitBridge(Bridge):
    """The production bridge with the companion's separate intake and observation workers."""

    def __init__(self, root, host, slack) -> None:
        super().__init__(root, host, slack)
        self.intake, self.observe = self.worker("intake"), self.worker("observe")

    def worker(self, role: str) -> InboundWorker:
        return InboundWorker(self.store, self.slack, self.adapter, staged_root=self.store.state_dir / "staged",
                             role=role)

    def row(self, event_id: str) -> dict:
        with sqlite3.connect(self.store.path) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM inbox WHERE event_id=?", (event_id,)).fetchone())


@pytest.fixture
def clock(monkeypatch):
    """Store time only: claims, leases and retry availability use this logical clock."""
    value = SimpleNamespace(now=50000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: value.now))
    return value


# ---- negotiation ----------------------------------------------------------------------------

@pytest.mark.parametrize("identity,expected", [
    ({"presence_continuation_version": 1, "presence_continuation_versions": [1, 2]}, 2),
    ({"presence_continuation_versions": [2]}, 2),
    ({"presence_continuation_version": 1}, 1),
    ({"presence_continuation_version": 1, "presence_continuation_versions": [1]}, 1),
    ({"presence_continuation_versions": [1]}, 1),
    ({"presence_continuation_version": 1, "presence_continuation_versions": ["2", True, 2.0]}, 1),
    ({"presence_continuation_version": 2}, 0),
    ({"presence_continuation_versions": "1,2"}, 0),
    ({}, 0),
])
def test_version_two_is_requested_only_when_listed(tmp_path, identity, expected):
    async def run():
        seen = []

        def handle(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"ok": True, **identity})
            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(200, json={"ok": True, "status": "completed", "outcome": "silent", "text": "",
                                             "turn_ref": "t", "work_ref": "",
                                             **({"continuation_version": expected, "continuation_ref": "",
                                                 "output_ref": ""} if expected else {})})

        store = BridgeStore(tmp_path)
        payload = envelope("Ev-1", ts="1.0", text="Hello")
        store.ingest_envelope(payload, parse_socket_envelope(payload))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            adapter = LoopbackPresenceHostAdapter(binding_id=BINDING, host_service_url="http://127.0.0.1:8767",
                                                  skill_token=TOKEN, http_client=http)
            worker = InboundWorker(store, _NoSlack(), adapter, staged_root=tmp_path / "staged", role="intake")
            assert await worker.process_once()
            assert adapter.continuation_version == expected
        assert seen[0].get("continuation_version") == (expected or None)
        # The transport queue observation travels exactly when continuation is negotiated.
        assert ("transport_queue" in seen[0]["event"]["conversation"]) is bool(expected)
        assert store.runtime_value("presence_continuation_version") == expected
        assert store.status()["inbox_delivered"] == 1

    asyncio.run(run())


class _NoSlack:
    async def user_info(self, user_id):
        return {"id": user_id}

    async def conversation_info(self, channel_id):
        return {"id": channel_id}

    async def read_messages(self, **kwargs):
        return {"messages": [], "complete": True, "next_cursor": None}


# ---- startup, active input and anchors -------------------------------------------------------

@pytest.mark.parametrize("reporting", [True, False])
def test_early_ack_releases_thread_and_frees_input_worker_while_author_runs(tmp_path, reporting):
    async def run():
        host, slack = V2Host(reporting=reporting), FakeSlack()
        host.gate("Ev-1")  # the author is starting: the ACK follows once its intake is open
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        host.scripts["Ev-2"] = lambda body: heard(body, "turn-1")  # mail to the live author
        host.scripts["Ev-9"] = lambda body: answered("turn-9", body, "Other thread", "out-9", host.anchor("Ev-9"))
        bridge = SplitBridge(tmp_path, host, slack)
        other = bridge.worker("intake")
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Write the report"))
            first = asyncio.create_task(bridge.intake.process_once())
            await asyncio.wait_for(host.arrived["Ev-1"].wait(), 5)
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Also cover Q3"))
            await bridge.ingest(envelope("Ev-9", ts="9.0", channel="C2", text="Unrelated"))
            assert await other.process_once()  # another conversation is served meanwhile
            # Startup order stays FIFO: Ev-2 waits only for Ev-1's custody ACK.
            assert await other.process_once() is False
            assert await bridge.observe.process_once() is False  # nothing admitted to observe yet
            host.release("Ev-1")
            assert await asyncio.wait_for(first, 5)
            # The input worker stored the reference and released the row without any
            # poll or queue report: observation belongs to another worker.
            assert host.polls == [] and host.queue_reports == []
            assert bridge.row("Ev-1")["state"] == "pending" and bridge.row("Ev-1")["last_error"] == ""
            assert bridge.row("Ev-1")["host_reference"].startswith("continuing:")
            assert [row["output_ref"] for row in bridge.outbox()] == ["out-9"]
            assert await other.process_once()  # Ev-2 reaches the live author now
            assert bridge.inbox() == {"Ev-1": "pending", "Ev-2": "delivered", "Ev-9": "delivered"}
            assert host.turn_order == ["Ev-1", "Ev-9", "Ev-2"]

            # The author's selection covers Ev-2: its anchor is the last covered input.
            host.work["turn-1"] = [
                pending("turn-1", [anchored("out-1", "Report, now with Q3", host.anchor("Ev-2"))], mode=int(reporting)),
                terminal("turn-1", [anchored("out-1", "Report, now with Q3", host.anchor("Ev-2"))],
                         mode=int(reporting)),
            ]
            assert await bridge.observe.process_once()
            await bridge.send_all()
            assert bridge.inbox()["Ev-1"] == "pending"
            bridge.due()
            assert await bridge.observe.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"Ev-1": "delivered", "Ev-2": "delivered", "Ev-9": "delivered"}
        assert slack.texts() == ["Other thread", "Report, now with Q3"]
        # The anchor names a covered input; the Slack destination stays the turn's thread.
        assert [post["thread_ts"] for post in slack.posts] == ["9.0", "1.0"]
        assert [post["channel"] for post in slack.posts] == ["C2", "C1"]
        assert host.body("Ev-1")["continuation_version"] == 2
        assert "transport_queue" in host.body("Ev-1")["event"]["conversation"]
        # Ev-1 arrived first; its ACK was produced once its author's intake opened.
        assert host.generations == ["Ev-9", "Ev-1", "Ev-2"] and len(host.turn_bodies) == 3
        assert host.polls == ["turn-1", "turn-1"]
        rows = bridge.outbox()
        assert [row["output_ref"] for row in rows] == ["out-9", "out-1"]
        assert [row["origin"] for row in rows] == [_origin("Ev-9", "turn-9"), _origin("Ev-1", "turn-1")]
        assert {row["mode"] for row in rows} == {int(reporting)}
        assert [report["message"].get("output_ref") for report in host.reports] == (
            ["out-9", "out-1"] if reporting else [])

    asyncio.run(run())


def test_slow_output_poll_never_holds_input_of_its_own_or_other_conversations(tmp_path):
    async def run():
        host, slack = V2Host(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        for source in ("Ev-2", "Ev-3"):
            host.scripts[source] = lambda body: heard(body, "turn-1")
        host.scripts["Ev-8"] = lambda body: heard(body)
        host.gate_poll("turn-1")
        host.work["turn-1"] = [terminal("turn-1", [], outcome="message", text="Done", output_ref="out-done",
                                        anchor={"source_event_id": "Ev-3", "message": {"message_id": "3.0"}})]
        bridge = SplitBridge(tmp_path, host, slack)
        poll = None
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Start"))
            assert await bridge.intake.process_once()
            poll = asyncio.create_task(bridge.observe.process_once())
            await asyncio.wait_for(host.poll_entered["turn-1"].wait(), 5)
            for number, (source, channel, thread) in enumerate(
                    [("Ev-2", "C1", "1.0"), ("Ev-8", "C8", ""), ("Ev-3", "C1", "1.0")], 2):
                await bridge.ingest(envelope(source, ts=f"{number}.0", channel=channel, thread_ts=thread,
                                             text=f"input {source}"))
            for _ in range(3):
                assert await bridge.intake.process_once()
            # The observation GET is still in flight (not yet answered or recorded).
            assert not poll.done() and host.polls == [] and len(host.queue_reports) == 1
            assert bridge.inbox() == {"Ev-1": "leased", "Ev-2": "delivered", "Ev-8": "delivered", "Ev-3": "delivered"}
            host.poll_gates["turn-1"].set()
            assert await asyncio.wait_for(poll, 5)
            await bridge.send_all()
        finally:
            if poll is not None:
                host.poll_gates["turn-1"].set()
                await asyncio.gather(poll, return_exceptions=True)
            await bridge.close()
        assert host.turn_order == ["Ev-1", "Ev-2", "Ev-8", "Ev-3"]
        assert bridge.inbox()["Ev-1"] == "delivered" and slack.texts() == ["Done"]
        assert slack.posts[0]["thread_ts"] == "1.0"

    asyncio.run(run())


# ---- saturation, busy hints, order and floods -----------------------------------------------

@pytest.mark.parametrize("field,delays", [
    ("capacity", [5, 5, 5, 8, 16, 30, 30]),
    ("conversation", [1, 2, 4, 8, 16, 30, 30]),
])
def test_capacity_retry_keeps_the_same_head_and_serves_other_conversations(tmp_path, clock, field, delays):
    async def run():
        host, slack = V2Host(), FakeSlack()
        host.busy["Ev-1"] = [busy(field) for _ in delays]
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        host.scripts["Ev-2"] = lambda body: heard(body, "turn-1")
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Head of thread"))
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Next in thread"))
            for index, delay in enumerate(delays):
                other = f"Other-{index}"
                host.scripts[other] = lambda body: heard(body)
                await bridge.ingest(envelope(other, ts=f"{10 + index}.0", channel="C9", text="Elsewhere"))
                assert await bridge.intake.process_once()  # the head is refused, the worker is free
                row = bridge.row("Ev-1")
                assert (row["state"], row["host_reference"]) == ("pending", "")
                assert row["available_at"] == clock.now + delay
                assert "presence_capacity_full" in row["last_error"] and field in row["last_error"]
                assert await bridge.intake.process_once()  # another conversation is served
                assert bridge.inbox()[other] == "delivered"
                assert await bridge.intake.process_once() is False  # Ev-2 never overtakes its head
                assert bridge.row("Ev-2")["state"] == "pending" and bridge.row("Ev-2")["attempts"] == 0
                clock.now += delay
            assert await bridge.intake.process_once()  # admitted: custody ACK
            assert await bridge.intake.process_once()  # then the next event of that thread
        finally:
            await bridge.close()
        assert bridge.row("Ev-1")["host_reference"].startswith("continuing:")
        assert bridge.row("Ev-2")["state"] == "delivered"
        heads = [body for body in host.turn_bodies if body["event"]["source_event_id"] == "Ev-1"]
        # Every retry resubmitted the same event, byte for byte; no attempt cap applies.
        assert len(heads) == len(delays) + 1 and all(body == heads[0] for body in heads)
        order = [source for source in host.turn_order if not source.startswith("Other")]
        assert order == ["Ev-1"] * (len(delays) + 1) + ["Ev-2"]
        assert host.generations[-2:] == ["Ev-1", "Ev-2"] and host.polls == [] and slack.posts == []

    asyncio.run(run())


def test_capacity_refusal_without_retry_hint_still_backs_off_and_untyped_busy_is_generic(tmp_path, clock):
    async def run():
        host, slack = V2Host(), FakeSlack()
        status, body = busy("capacity")
        host.busy["Ev-1"] = [(status, {key: value for key, value in body.items() if key != "retry_after"}),
                             (429, {"ok": False, "error": "busy", "code": "presence_capacity_full",
                                    "disposition": "retry", "retry_after": "soon"}),
                             (503, {"ok": False, "error": "unavailable", "code": "presence_store_busy",
                                    "disposition": "retry"})]
        host.scripts["Ev-1"] = lambda request: ack("turn-1", request)
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Head"))
            observed = []
            for _ in range(3):
                assert await bridge.intake.process_once()
                row = bridge.row("Ev-1")
                assert row["host_reference"] == ""
                observed.append(row["available_at"] - clock.now)
                clock.now = row["available_at"]
            assert await bridge.intake.process_once()
        finally:
            await bridge.close()
        # Missing or malformed hints keep the capacity schedule; other typed retries
        # keep the existing exponential backoff.
        assert observed == [1, 2, 8]
        assert bridge.row("Ev-1")["host_reference"].startswith("continuing:")

    asyncio.run(run())


@pytest.mark.parametrize("hint,delays", [
    (45, [45, 45, 45, 45, 45, 45, 45]),  # a Host hint above the backoff cap is never cut
    (12.5, [12.5, 12.5, 12.5, 12.5, 16, 30, 30]),
    (0, [1, 2, 4, 8, 16, 30, 30]),  # a zero hint leaves the bridge's own backoff
])
def test_capacity_hint_is_a_floor_and_only_the_own_backoff_is_capped(tmp_path, clock, hint, delays):
    async def run():
        host, slack = V2Host(), FakeSlack()
        status, body = busy("capacity")
        host.busy["Ev-1"] = [(status, {**body, "retry_after": hint}) for _ in delays]
        host.scripts["Ev-1"] = lambda request: ack("turn-1", request)
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Head"))
            observed = []
            for _ in delays:
                assert await bridge.intake.process_once()
                observed.append(bridge.row("Ev-1")["available_at"] - clock.now)
                clock.now = bridge.row("Ev-1")["available_at"]
            assert await bridge.intake.process_once()
        finally:
            await bridge.close()
        assert observed == delays and bridge.row("Ev-1")["host_reference"].startswith("continuing:")

    asyncio.run(run())


def test_continuous_inflow_never_starves_output_observation(tmp_path):
    """BridgeRuntime with one configured inbound worker: the reply goes out while input is queued."""

    async def run():
        done = asyncio.Event()

        class BusyHost(V2Host):
            async def handle(self, request):
                if (request.url.path == "/presence/turn" and not done.is_set()
                        and json.loads(request.content)["event"]["source_event_id"] != "Ev-1"):
                    # While the inflow runs, events arrive faster than they can be
                    # submitted, so input stays queued throughout.
                    await asyncio.sleep(0.1)
                return await super().handle(request)

        host, slack = BusyHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        host.work["turn-1"] = [terminal("turn-1", [], outcome="message", text="Answer", output_ref="out-1")]
        backlog_at_post, received, rigs = [], [], []
        handle = slack.handle

        def recording_handle(request):
            if request.url.path.endswith("chat.postMessage"):
                backlog_at_post.append(rigs[0].unsubmitted())
            return handle(request)

        slack.handle = recording_handle  # installed before the runtime builds its Slack transport
        rig = _Runtime(tmp_path, host, slack)
        rigs.append(rig)
        feeder = None

        def arrive(number):
            source = f"In-{number}"
            host.scripts[source] = lambda body: heard(body)
            rig.ingest(envelope(source, ts=f"{number}.0", channel=f"C{number}", text="more input"))
            received.append(source)

        async def inflow():
            number = 130
            while not done.is_set():
                arrive(number)
                number += 1
                await asyncio.sleep(0.005)

        try:
            rig.ingest(envelope("Ev-1", ts="1.0", text="Start"))
            for number in range(100, 130):  # a backlog already waits behind the author's event
                arrive(number)
            rig.start()
            feeder = asyncio.create_task(inflow())
            await rig.until(lambda: len(slack.posts) == 1)
            posted_after = len(received)
            await asyncio.sleep(0.2)
            assert len(received) > posted_after  # the inflow kept running after the reply left
            done.set()
            await feeder
            await rig.until(lambda: rig.unsubmitted() == 0, timeout=30)
        finally:
            done.set()
            if feeder is not None:
                await asyncio.gather(feeder, return_exceptions=True)
            await rig.finish()
        # Observation progressed while input stayed queued; every arrival reached Host
        # once, in arrival order, with nothing dropped or merged.
        assert backlog_at_post[0] > 0 and slack.texts() == ["Answer"] and host.polls == ["turn-1"]
        assert host.turn_order == ["Ev-1", *received]

    asyncio.run(run())


def test_flood_into_a_live_author_is_submitted_in_order_while_its_poll_is_blocked(tmp_path, monkeypatch):
    """BridgeRuntime with one configured inbound worker: one intake loop, one observation loop."""

    async def run():
        host, slack = V2Host(), FakeSlack()
        flood = [f"Ev-{number}" for number in range(2, 14)]
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        for source in flood:
            host.scripts[source] = lambda body: heard(body, "turn-1")
        host.gate_poll("turn-1")
        rig = _Runtime(tmp_path, host, slack)
        try:
            rig.ingest(envelope("Ev-1", ts="1.0", text="Start"))
            rig.start()
            await asyncio.wait_for(host.poll_entered["turn-1"].wait(), 5)
            for number, source in enumerate(flood, 2):
                rig.ingest(envelope(source, ts=f"{number}.0", thread_ts="1.0", text=f"words {number}"))
            await rig.until(lambda: all(rig.state(source) == "delivered" for source in flood))
            # Every arrival is its own submission, in arrival order, while the only
            # observation loop is still inside its first GET. Nothing was dropped or merged.
            assert host.turn_order == ["Ev-1", *flood] and host.polls == []
            assert rig.state("Ev-1") == "leased" and slack.posts == []
            last = host.anchor(flood[-1])
            host.work["turn-1"] = [terminal("turn-1", [anchored("out-1", "One answer to all of it", last)],
                                            anchor=last)]
            host.poll_gates["turn-1"].set()
            await rig.until(lambda: rig.state("Ev-1") == "delivered" and len(slack.posts) == 1)
        finally:
            await rig.finish()
        assert slack.texts() == ["One answer to all of it"] and slack.posts[0]["thread_ts"] == "1.0"
        assert len(host.turn_bodies) == 1 + len(flood) and host.generations == ["Ev-1", *flood]
        assert host.polls == ["turn-1"]

    asyncio.run(run())


class _Runtime:
    """The companion's BridgeRuntime loops with a stand-in Socket Mode client."""

    def __init__(self, root, host, slack) -> None:
        self.store = BridgeStore(root)
        self.store.set_runtime(workspace_id="T1")
        self.host_http = httpx.AsyncClient(transport=httpx.MockTransport(host.handle))
        self.slack_http = httpx.AsyncClient(transport=httpx.MockTransport(slack.handle))
        from lib.slack_api import SlackClient

        adapter = LoopbackPresenceHostAdapter(binding_id=BINDING, host_service_url="http://127.0.0.1:8767",
                                              skill_token=TOKEN, http_client=self.host_http)
        client = SlackClient("xoxb-fixture", "xapp-fixture", http_client=self.slack_http)
        self.runtime = BridgeRuntime(store=self.store, slack=client, host=adapter, bot_user_id="U_BOT",
                                     inbound_workers=1, outbound_workers=1)
        self.runtime.socket = self
        self._finish = asyncio.Event()
        self.task = None

    async def run(self) -> None:
        await self._finish.wait()

    async def close(self) -> None:
        self._finish.set()

    def ingest(self, payload: dict) -> None:
        self.store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))

    def state(self, event_id: str) -> str:
        with sqlite3.connect(self.store.path) as db:
            return db.execute("SELECT state FROM inbox WHERE event_id=?", (event_id,)).fetchone()[0]

    def unsubmitted(self) -> int:
        with sqlite3.connect(self.store.path) as db:
            return db.execute("SELECT COUNT(*) FROM inbox WHERE host_reference='' "
                              "AND state IN ('pending','leased')").fetchone()[0]

    def start(self) -> None:
        self.task = asyncio.create_task(self.runtime.run())

    async def until(self, predicate, timeout: float = 10.0) -> None:
        async def wait():
            while not predicate():
                if self.task is not None and self.task.done():
                    self.task.result()
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout)

    async def finish(self) -> None:
        self.runtime._stop.set()
        self._finish.set()
        try:
            if self.task is not None:
                await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)
        finally:
            await self.host_http.aclose()
            await self.slack_http.aclose()


# ---- deduplication, unknown sends and restart -----------------------------------------------

@pytest.mark.parametrize("reporting", [True, False])
def test_output_ref_is_sent_once_across_polls_terminal_child_and_restart(tmp_path, reporting):
    async def run():
        host, slack = V2Host(reporting=reporting), FakeSlack()
        mode = int(reporting)
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.intake.process_once()
            await bridge.close()
            # A restart between the ACK and the first poll keeps the stored reference.
            bridge = SplitBridge(tmp_path, host, slack)
            first = anchored("out-1", "First part", host.anchor("Ev-1"))
            second = anchored("out-2", "Second part", host.anchor("Ev-1"))
            host.work["turn-1"] = [
                pending("turn-1", [first], mode=mode),
                pending("turn-1", [first, second], child="child-1", mode=mode),
                # The terminal repeats a released selection under the same output_ref.
                terminal("turn-1", [first, second], outcome="message", text="Second part", output_ref="out-2",
                         anchor=host.anchor("Ev-1"), child="child-1", mode=mode),
            ]
            # A promoted child carries no anchor and may repeat an output identity.
            host.work["child-1"] = [child_pending("child-1", mode),
                                    child_done("child-1", "First part", mode, output_ref="out-1")]
            for _ in range(2):
                bridge.due()
                assert await bridge.observe.process_once()
                await bridge.send_all()
            await bridge.close()
            # After a restart the terminal repeats both selections and the child
            # repeats the first identity: nothing is queued twice.
            bridge = SplitBridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.observe.process_once()
            await bridge.send_all()
            bridge.due()
            assert await bridge.observe.process_once() is False
            assert await bridge.intake.process_once() is False
        finally:
            await bridge.close()
        assert slack.texts() == ["First part", "Second part"]
        assert [row["output_ref"] for row in bridge.outbox()] == ["out-1", "out-2"]
        assert bridge.inbox() == {"Ev-1": "delivered"}
        assert host.generations == ["Ev-1"] and len(host.turn_bodies) == 1
        assert [report["message"]["output_ref"] for report in host.reports] == (["out-1", "out-2"] if reporting else [])

    asyncio.run(run())


def test_lost_ack_replays_stored_envelope_and_unknown_send_is_never_repeated(tmp_path):
    async def run():
        host, slack = V2Host(), FakeSlack(lose_posts=True)
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        host.lose_reply.add("Ev-1")
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            payload = envelope("Ev-1", ts="1.0", text="Quick question")
            await bridge.ingest(payload)
            assert await bridge.intake.process_once()  # Host recorded the ACK; its reply was lost
            assert bridge.row("Ev-1")["host_reference"] == ""
            bridge.due()
            assert await bridge.intake.process_once()  # the retry replays the identical envelope
            assert bridge.row("Ev-1")["host_reference"].startswith("continuing:")
            assert host.turn_bodies[0] == host.turn_bodies[1] and host.generations == ["Ev-1"]
            selection = anchored("out-1", "Answer", host.anchor("Ev-1"))
            host.work["turn-1"] = [pending("turn-1", [selection]), pending("turn-1", [selection]),
                                   terminal("turn-1", [selection])]
            bridge.due()
            assert await bridge.observe.process_once()
            await bridge.send_all()  # Slack's response is lost: unknown, never resent
            await bridge.ingest(payload)  # Slack redelivers the envelope: deduplicated
            for _ in range(2):
                bridge.due()
                assert await bridge.observe.process_once()
                await bridge.send_all()
        finally:
            await bridge.close()
        assert slack.attempts == 1 and [row["state"] for row in bridge.outbox()] == ["uncertain"]
        assert bridge.inbox() == {"Ev-1": "delivered"} and len(host.turn_bodies) == 2

    asyncio.run(run())


@pytest.mark.parametrize("stopped_as", ["cancelled", "retained"])
def test_stopped_author_speech_is_never_sent_and_the_event_never_reposted(tmp_path, stopped_as):
    async def run():
        host, slack = V2Host(), FakeSlack()
        released = anchored("out-1", "Working on it", {"source_event_id": "Ev-1", "message": {"message_id": "1.0"}})
        if stopped_as == "cancelled":
            host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
            host.work["turn-1"] = [
                pending("turn-1", [released]),
                # Stop: no canonical answer; any held draft is gone. The released
                # output was already sent; the terminal names no new speech.
                terminal("turn-1", [released], status="cancelled", outcome="silent",
                         code="presence_turn_cancelled", recorded_status="cancelled"),
            ]
        else:
            host.turn_status["Ev-1"] = 409
            host.scripts["Ev-1"] = lambda body: {
                "ok": False, "error": "presence_turn_cancelled: source_event_id", "code": "presence_turn_cancelled",
                "disposition": "retained", "field": "source_event_id", "turn_ref": "turn-1",
                "delivery_reporting_version": 1}
            host.work["turn-1"] = [(200, {"ok": True, "status": "cancelled", "outcome": "silent", "text": "",
                                          "output_ref": "", "work_ref": "turn-1", "code": "presence_turn_cancelled",
                                          "delivery_reporting_version": 1})]
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            payload = envelope("Ev-1", ts="1.0", text="Do the thing")
            await bridge.ingest(payload)
            assert await bridge.intake.process_once()
            for _ in range(2 if stopped_as == "cancelled" else 1):
                bridge.due()
                assert await bridge.observe.process_once()
                await bridge.send_all()
            await bridge.ingest(payload)
            await bridge.close()
            bridge = SplitBridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.intake.process_once() is False
            assert await bridge.observe.process_once() is False
        finally:
            await bridge.close()
        assert bridge.inbox() == {"Ev-1": "failed"}
        assert "cancelled" in bridge.inbox_error("Ev-1")
        assert slack.texts() == (["Working on it"] if stopped_as == "cancelled" else [])
        # Only the speech released before Stop was sent and reported.
        assert [report["message"].get("output_ref") for report in host.reports] == (
            ["out-1"] if stopped_as == "cancelled" else [])
        assert len(host.turn_bodies) == 1 and host.generations == ["Ev-1"]

    asyncio.run(run())


# ---- late child and anchors ------------------------------------------------------------------

def test_late_child_without_anchor_and_anchored_author_both_reply_in_the_thread(tmp_path):
    async def run():
        host, slack = V2Host(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        host.scripts["Ev-2"] = lambda body: heard(body, "turn-1")
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", thread_ts="0.5", text="Research this"))
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="0.5", text="And that"))
            for _ in range(2):
                assert await bridge.intake.process_once()
            update = anchored("out-a", "Started both", host.anchor("Ev-2"))
            host.work["turn-1"] = [pending("turn-1", [update]), pending("turn-1", [update], child="child-late"),
                                   pending("turn-1", [update], child="child-late")]
            host.work["child-late"] = [child_pending("child-late"),
                                       child_done("child-late", "Child report", output_ref="out-child")]
            bridge.due()
            assert await bridge.observe.process_once()
            bridge.due()
            assert await bridge.observe.process_once()  # discovers the child and persists it first
            await bridge.close()
            bridge = SplitBridge(tmp_path, host, slack)  # the child comes back from SQLite
            bridge.due()
            assert await bridge.observe.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert slack.texts() == ["Started both", "Child report"]
        assert {post["thread_ts"] for post in slack.posts} == {"0.5"}
        assert [row["origin"] for row in bridge.outbox()] == [_origin("Ev-1", "turn-1"), _origin("Ev-1", "child-late")]
        assert host.polls.count("child-late") == 2 and len(host.turn_bodies) == 2

    asyncio.run(run())


# ---- older Hosts and mixed versions ------------------------------------------------------------

def test_legacy_host_keeps_v1_while_the_companion_still_separates_polling(tmp_path):
    async def run():
        host, slack = V2Host(versions=None), FakeSlack()
        host.gate("Ev-1")  # a v1 Host holds the request until its author parks
        host.scripts["Ev-1"] = lambda body: {
            "ok": True, "status": "continuing", "outcome": "message", "text": "Draft", "output_ref": "out-d",
            "turn_ref": "turn-1", "continuation_ref": "turn-1", "work_ref": "", "continuation_version": 1,
            "delivery_reporting_version": 1}
        host.work["turn-1"] = [pending("turn-1", [out("out-d", "Draft")], version=1),
                               terminal("turn-1", [out("out-d", "Draft")], outcome="message", text="Final",
                                        output_ref="out-f", version=1)]
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="More"))
            host.scripts["Ev-2"] = lambda body: {**heard(body), "continuation_version": 1}
            first = asyncio.create_task(bridge.intake.process_once())
            await asyncio.wait_for(host.arrived["Ev-1"].wait(), 5)
            assert await bridge.worker("intake").process_once() is False  # v1: FIFO until the park
            host.release("Ev-1")
            assert await asyncio.wait_for(first, 5)
            # The envelope's own speech is queued by the input claim, before any poll.
            assert [row["output_ref"] for row in bridge.outbox()] == ["out-d"] and host.polls == []
            assert await bridge.intake.process_once()
            for _ in range(2):
                bridge.due()
                assert await bridge.observe.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert host.body("Ev-1")["continuation_version"] == 1
        assert slack.texts() == ["Draft", "Final"] and bridge.inbox() == {"Ev-1": "delivered", "Ev-2": "delivered"}

    asyncio.run(run())


@pytest.mark.parametrize("negotiated,echoed,accepted", [(2, 1, True), (1, 2, False), (2, 3, False)])
def test_continuing_envelope_must_echo_a_negotiated_version(tmp_path, negotiated, echoed, accepted):
    async def run():
        host, slack = V2Host(versions=(1, 2) if negotiated == 2 else None), FakeSlack()
        host.scripts["Ev-1"] = lambda body: {**ack("turn-1", body), "continuation_version": echoed}
        host.work["turn-1"] = [terminal("turn-1", [], outcome="message", text="Done", output_ref="out-1",
                                        version=echoed)]
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.intake.process_once()
            bridge.due()
            assert await bridge.observe.process_once() is accepted
            await bridge.send_all()
        finally:
            await bridge.close()
        assert host.body("Ev-1")["continuation_version"] == negotiated
        if accepted:  # a replayed v1 envelope keeps its version for every poll
            assert bridge.inbox() == {"Ev-1": "delivered"} and slack.texts() == ["Done"]
        else:
            assert bridge.inbox() == {"Ev-1": "pending"} and bridge.row("Ev-1")["host_reference"] == ""
            assert "continuation reference" in bridge.inbox_error("Ev-1") and host.polls == []

    asyncio.run(run())


def test_v2_poll_that_echoes_another_version_keeps_custody(tmp_path):
    async def run():
        host, slack = V2Host(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: ack("turn-1", body)
        host.work["turn-1"] = [pending("turn-1", [out("out-1", "Early")], version=1)]
        bridge = SplitBridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.intake.process_once()
            bridge.due()
            assert await bridge.observe.process_once()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"Ev-1": "pending"} and bridge.outbox() == []
        assert "without continuation_version 2" in bridge.inbox_error("Ev-1")
        assert bridge.row("Ev-1")["host_reference"].startswith("continuing:")

    asyncio.run(run())


# ---- observation order ----------------------------------------------------------------------

@pytest.mark.parametrize("role", ["observe", None])
def test_observation_serves_due_work_first_so_two_slow_pending_authors_never_starve_a_finished_one(
        tmp_path, clock, role):
    """The supported single observer: each read outlasts the 5 second pending pace."""
    class SlowReadHost(V2Host):
        async def handle(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.startswith("/presence/work/"):
                clock.now += 6
            return await super().handle(request)

    async def run():
        host, slack = SlowReadHost(), FakeSlack()
        for n in (1, 2, 3):
            host.scripts[f"Ev-{n}"] = lambda body, n=n: ack(f"turn-{n}", body)
        host.work["turn-1"] = [pending("turn-1", [])]
        host.work["turn-2"] = [pending("turn-2", [])]
        host.work["turn-3"] = [terminal("turn-3", [], outcome="message", text="Third answer", output_ref="out-3")]
        bridge = SplitBridge(tmp_path, host, slack)
        observer = bridge.worker(role)
        try:
            for n in (1, 2, 3):
                await bridge.ingest(envelope(f"Ev-{n}", ts=f"{n}.0", channel=f"C{n}", text=f"Question {n}"))
            for _ in range(3):  # each conversation's author takes custody, in arrival order
                assert await bridge.intake.process_once()
                clock.now += 1
            for _ in range(6):  # one claim at a time
                if not await observer.process_once():
                    clock.now += 0.5
            await bridge.send_all()
        finally:
            await bridge.close()
        # Older pending work keeps being observed, but due order gives the finished one its turn.
        assert host.polls[:5] == ["turn-1", "turn-2", "turn-3", "turn-1", "turn-2"]
        assert bridge.inbox()["Ev-3"] == "delivered"
        assert slack.texts() == ["Third answer"] and [post["channel"] for post in slack.posts] == ["C3"]

    asyncio.run(run())


@pytest.mark.parametrize("submitted,expected", [
    (False, ["Ev-1", "Ev-2"]),  # new input keeps capture order, whatever its retry time
    (True, ["Ev-4", "Ev-3"]),  # admitted work: earliest due first
    (None, ["Ev-1", "Ev-4", "Ev-2", "Ev-3"]),  # one worker: capture time and due time, interleaved
])
def test_each_claim_role_keeps_its_own_order(tmp_path, clock, submitted, expected):
    store = BridgeStore(tmp_path)
    start = clock.now
    for n in (1, 2, 3, 4):  # captured one second apart, one conversation each
        payload = envelope(f"Ev-{n}", ts=f"{n}.0", channel=f"C{n}", text=f"Event {n}")
        store.ingest_envelope(payload, parse_socket_envelope(payload))
        clock.now += 1
    with sqlite3.connect(store.path) as db:
        db.executemany("UPDATE inbox SET host_reference=?, available_at=? WHERE event_id=?", [
            ("", start + 9, "Ev-1"), ("", start + 2, "Ev-2"),  # Ev-1 was retried, later due
            ("continuing:a", start + 9, "Ev-3"), ("continuing:b", start + 0.5, "Ev-4")])
    clock.now = start + 10
    claimed = []
    while (item := store.claim_inbox(submitted=submitted)) is not None:
        claimed.append(item.event_id)
    assert claimed == expected
