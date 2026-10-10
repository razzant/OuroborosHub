"""Presence continuation (#1536) through the production bridge path.

Real BridgeStore, SocketModeClient, InboundWorker, OutboundWorker, SlackClient and
LoopbackPresenceHostAdapter run against two local httpx transports: a loopback Host
speaking core's continuation wire (``turn_response`` / ``work_view`` shapes, produced
here) and the Slack Web API. Nothing leaves the process.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from lib.events import parse_socket_envelope
from lib.runtime import InboundWorker, OutboundWorker
from lib.slack_api import SlackClient
from lib.socket_mode import SocketModeClient
from lib.store import BridgeStore

BINDING = "b" * 32
TOKEN = "fixture-host-token"


def envelope(event_id: str, *, ts: str, text: str, thread_ts: str = "", channel: str = "C1",
             user: str = "U1", files: tuple = ()) -> dict:
    event = {"type": "message", "user": user, "channel": channel, "channel_type": "channel", "ts": ts,
             "text": text, **({"thread_ts": thread_ts} if thread_ts else {}),
             **({"files": list(files)} if files else {})}
    return {"type": "events_api", "envelope_id": f"env-{event_id}",
            "payload": {"event_id": event_id, "team_id": "T1", "event": event}}


# ---- core's #1536 wire, produced locally -------------------------------------------------

def continuing(task: str, request: dict, *, outcome: str = "deferred", text: str = "",
               output_ref: str = "", work_ref: str = "") -> dict:
    return {"ok": True, "status": "continuing", "outcome": outcome, "text": text, "turn_ref": task,
            "work_ref": work_ref, "delivery_reporting_version": request.get("delivery_reporting_version", 0),
            "continuation_version": 1, "continuation_ref": task, "output_ref": output_ref}


def completed(task: str, request: dict, *, outcome: str = "message", text: str = "",
              output_ref: str = "", work_ref: str = "") -> dict:
    body = {"ok": True, "status": "completed", "outcome": outcome, "text": text, "turn_ref": task,
            "work_ref": work_ref, "delivery_reporting_version": request.get("delivery_reporting_version", 0)}
    if request.get("continuation_version") == 1:
        body.update(continuation_version=1, continuation_ref="", output_ref=output_ref)
    return body


def out(output_ref: str, text: str, outcome: str = "message") -> dict:
    return {"output_ref": output_ref, "outcome": outcome, "text": text}


def _author(ref: str, outputs: list, mode: int) -> dict:
    return {"ok": True, "work_ref": ref, "continuation_ref": ref, "continuation_version": 1,
            "outputs": outputs, "delivery_reporting_version": mode}


def author_pending(ref: str, outputs: list, mode: int = 1, *, child: str = "") -> tuple[int, dict]:
    return 202, {**_author(ref, outputs, mode), "status": "pending", "child_work_ref": child}


def author_terminal(ref: str, outputs: list, *, status: str = "completed", outcome: str = "silent",
                    text: str = "", output_ref: str = "", child: str = "", mode: int = 1) -> tuple[int, dict]:
    return 200, {**_author(ref, outputs, mode), "status": status, "outcome": outcome, "text": text,
                 "output_ref": output_ref, "child_work_ref": child}


def author_interrupted(ref: str, outputs: list, mode: int = 1, *, child: str = "") -> tuple[int, dict]:
    return 200, {**_author(ref, outputs, mode), "status": "interrupted", "outcome": "silent", "text": "",
                 "output_ref": "", "child_work_ref": child}


def child_pending(ref: str, mode: int = 1) -> tuple[int, dict]:
    return 202, {"ok": True, "status": "pending", "work_ref": ref, "delivery_reporting_version": mode}


def child_done(ref: str, text: str, mode: int = 1, *, output_ref: str = "") -> tuple[int, dict]:
    return 200, {"ok": True, "status": "completed", "outcome": "message", "text": text, "work_ref": ref,
                 "delivery_reporting_version": mode, "output_ref": output_ref}


class FakeHost:
    """A loopback Host: one durable answer per source event, scripted work polls."""

    def __init__(self, *, continuation: bool = True, reporting: bool = True) -> None:
        self.continuation, self.reporting = continuation, reporting
        self.scripts: dict[str, Callable[[dict], dict]] = {}
        self.answers: dict[str, dict] = {}
        self.generations: list[str] = []
        self.turn_bodies: list[dict] = []
        self.work: dict[str, list[tuple[int, dict]]] = {}
        self.polls: list[str] = []
        self.reports: list[dict] = []
        self.queue_reports: list[dict] = []
        self.lose_reply: set[str] = set()
        self.arrived: dict[str, asyncio.Event] = {}
        self._gates: dict[str, asyncio.Event] = {}

    def gate(self, source: str) -> None:
        self._gates[source], self.arrived[source] = asyncio.Event(), asyncio.Event()

    def release(self, source: str) -> None:
        self._gates[source].set()

    def body(self, source: str) -> dict:
        return [body for body in self.turn_bodies if body["event"]["source_event_id"] == source][-1]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Skill-Token"] == TOKEN
        path = request.url.path
        if path == "/identity":
            facts = {"ok": True, "name": "fixture"}
            if self.reporting:
                facts["presence_delivery_version"] = 1
            if self.continuation:
                facts["presence_continuation_version"] = 1
            return httpx.Response(200, json=facts)
        if path == "/presence/turn":
            body = json.loads(request.content)
            self.turn_bodies.append(body)
            source = body["event"]["source_event_id"]
            if source in self.arrived:
                self.arrived[source].set()
                await self._gates[source].wait()
            if source not in self.answers:  # a retry replays the stored envelope, never reruns
                self.generations.append(source)
                self.answers[source] = self.scripts[source](body)
            if source in self.lose_reply:
                self.lose_reply.discard(source)
                raise httpx.ReadTimeout("reply lost after Host recorded the turn", request=request)
            return httpx.Response(200, json=self.answers[source])
        if path.startswith("/presence/work/"):
            if request.method == "POST":
                body = json.loads(request.content)
                assert body["binding_id"] == BINDING
                self.queue_reports.append({"ref": path.rsplit("/", 1)[-1], **body})
                return httpx.Response(200, json={"ok": True, "status": "recorded",
                                                 "observed_at": body["transport_queue"]["observed_at"]})
            assert request.url.params["binding_id"] == BINDING
            ref = path.removeprefix("/presence/work/")
            self.polls.append(ref)
            queue = self.work[ref]
            status, body = queue.pop(0) if len(queue) > 1 else queue[0]
            return httpx.Response(status, json=body)
        if path == "/presence/delivery":
            self.reports.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "recorded": True, "duplicate": False})
        return httpx.Response(404, json={"ok": False})


class FakeSlack:
    def __init__(self, *, lose_posts: bool = False) -> None:
        self.lose_posts = lose_posts
        self.posts: list[dict] = []
        self.attempts = 0

    def texts(self) -> list[str]:
        return [post["markdown_text"] for post in self.posts]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.slack.com":
            return httpx.Response(200, content=b"pdf")
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"id": request.url.params["user"], "name": "reader"}})
        if method == "conversations.info":
            return httpx.Response(200, json={"ok": True, "channel": {"id": request.url.params["channel"]}})
        assert method == "chat.postMessage", method
        body = json.loads(request.content)
        self.attempts += 1
        if self.lose_posts:
            raise httpx.ReadTimeout("provider response lost", request=request)
        self.posts.append(body)
        return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": f"9.{len(self.posts)}",
                                         "message": {"text": body["markdown_text"]}})


class _WebSocket:
    def __init__(self) -> None:
        self.acks: list[str] = []

    async def send(self, value: str) -> None:
        self.acks.append(json.loads(value)["envelope_id"])


class Bridge:
    """The production bridge over one state directory, as one companion process runs it."""

    def __init__(self, root: Path, host: FakeHost, slack: FakeSlack) -> None:
        self.store = BridgeStore(root)
        self.store.set_runtime(workspace_id="T1")
        self._host_http = httpx.AsyncClient(transport=httpx.MockTransport(host.handle))
        self._slack_http = httpx.AsyncClient(transport=httpx.MockTransport(slack.handle))
        from lib.host_adapter import LoopbackPresenceHostAdapter

        self.adapter = LoopbackPresenceHostAdapter(binding_id=BINDING, host_service_url="http://127.0.0.1:8767",
                                                   skill_token=TOKEN, http_client=self._host_http)
        self.slack = SlackClient("xoxb-fixture", "xapp-fixture", http_client=self._slack_http)
        self.socket = SocketModeClient(self.slack, self.store, bot_user_id="U_BOT")
        self.websocket = _WebSocket()
        self.inbound = self.inbound_worker()
        self.outbound = OutboundWorker(self.store, self.slack, self.adapter)

    def inbound_worker(self) -> InboundWorker:
        return InboundWorker(self.store, self.slack, self.adapter, staged_root=self.store.state_dir / "staged")

    async def ingest(self, payload: dict) -> None:
        await self.socket.handle_raw_message(self.websocket, json.dumps(payload))

    def due(self) -> None:
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE inbox SET available_at=0")
            db.execute("UPDATE outbox SET available_at=0, report_available_at=0")

    async def send_all(self) -> None:
        """Run the outbound worker until neither a provider send nor a report remains."""
        for _ in range(100):
            self.due()
            worked = await self.outbound.process_once()
            if self.outbound._report_task is not None:
                await asyncio.gather(self.outbound._report_task, return_exceptions=True)
            if not worked:
                return
        raise AssertionError("outbound worker did not settle")

    def inbox(self) -> dict[str, str]:
        with sqlite3.connect(self.store.path) as db:
            return dict(db.execute("SELECT event_id, state FROM inbox WHERE state<>'ignored' ORDER BY id"))

    def inbox_error(self, event_id: str) -> str:
        with sqlite3.connect(self.store.path) as db:
            return db.execute("SELECT last_error FROM inbox WHERE event_id=?", (event_id,)).fetchone()[0]

    def outbox(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.store.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        return [{"request_id": row["request_id"], "text": row["text"], "state": row["state"],
                 "thread_ts": row["thread_ts"], "origin": json.loads(row["origin_json"]),
                 "output_ref": row["output_ref"], "mode": row["delivery_reporting_version"]} for row in rows]

    async def close(self) -> None:
        await self.outbound.aclose()
        await self.adapter.aclose()
        await self.slack.aclose()
        await self._host_http.aclose()
        await self._slack_http.aclose()


def _origin(source: str, task: str) -> dict:
    return {"kind": "automatic", "source_event_id": source, "task_id": task}


@pytest.mark.parametrize("reporting", [True, False])
def test_early_output_frees_the_thread_and_a_late_correction_is_sent_once(tmp_path, reporting):
    async def run():
        host, slack = FakeHost(reporting=reporting), FakeSlack()
        draft = out("out-1", "Draft: about 10 miles")
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text=draft["text"],
                                                       output_ref="out-1")
        host.scripts["Ev-2"] = lambda body: completed("turn-2", body, text="Noted, metric from now on.",
                                                      output_ref="out-2")
        host.work["turn-1"] = [author_pending("turn-1", [draft]),
                               author_terminal("turn-1", [draft], outcome="message", text="Correction: 16 km",
                                               output_ref="out-3")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="How far is it?"))
            assert await bridge.inbound.process_once()
            # The released early output is queued at once and the event keeps only polling.
            assert bridge.inbox() == {"Ev-1": "pending"}
            assert [row["text"] for row in bridge.outbox()] == [draft["text"]]
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Use metric units"))
            assert await bridge.inbound.process_once()  # a new message passes the continuing event
            assert bridge.inbox() == {"Ev-1": "pending", "Ev-2": "delivered"}
            bridge.due()
            assert await bridge.inbound.process_once()  # late poll: the same author's correction
            assert bridge.inbox() == {"Ev-1": "delivered", "Ev-2": "delivered"}
            await bridge.send_all()
        finally:
            await bridge.close()
        assert slack.texts() == [draft["text"], "Noted, metric from now on.", "Correction: 16 km"]
        assert {post["thread_ts"] for post in slack.posts} == {"1.0"}
        first = host.body("Ev-1")
        assert first["continuation_version"] == 1
        assert ("delivery_reporting_version" in first) is reporting
        assert host.polls == ["turn-1", "turn-1"] and host.generations == ["Ev-1", "Ev-2"]
        rows, mode = bridge.outbox(), int(reporting)
        assert [row["output_ref"] for row in rows] == ["out-1", "out-2", "out-3"]
        assert [row["origin"] for row in rows] == [_origin("Ev-1", "turn-1"), _origin("Ev-2", "turn-2"),
                                                   _origin("Ev-1", "turn-1")]
        assert {row["mode"] for row in rows} == {mode} and {row["state"] for row in rows} == {"delivered"}
        assert rows[0]["request_id"].startswith("presence-output:")
        if reporting:
            assert [report["delivery_id"] for report in host.reports] == [row["request_id"] for row in rows]
            assert [report["origin"] for report in host.reports] == [row["origin"] for row in rows]
            assert [report["message"].get("output_ref") for report in host.reports] == ["out-1", "out-2", "out-3"]
            assert {report["state"] for report in host.reports} == {"delivered"}
        else:
            assert host.reports == []

    asyncio.run(run())


def test_delayed_initial_envelope_holds_only_its_thread_and_relays_queued_events(tmp_path):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        host.gate("Ev-1")
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body)  # Blocking: the result stays held
        host.scripts["Ev-2"] = lambda body: completed("turn-2", body, text="Covering Q3 too.", output_ref="out-2")
        host.scripts["Ev-3"] = lambda body: completed("turn-3", body, outcome="silent")
        host.scripts["Ev-9"] = lambda body: completed("turn-9", body, text="Other thread", output_ref="out-9")
        host.work["turn-1"] = [author_pending("turn-1", []),
                               author_terminal("turn-1", [], outcome="message", text="Report, now with Q3",
                                               output_ref="out-r")]
        attachment = {"id": "F1", "name": "q3.pdf", "mimetype": "application/pdf", "size": 3,
                      "url_private": "https://files.slack.com/files-pri/T1-F1/q3.pdf"}
        bridge = Bridge(tmp_path, host, slack)
        other = bridge.inbound_worker()
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Write the report"))
            first = asyncio.create_task(bridge.inbound.process_once())
            await asyncio.wait_for(host.arrived["Ev-1"].wait(), 5)
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Also cover Q3"))
            await bridge.ingest(envelope("Ev-3", ts="3.0", thread_ts="1.0", text="x" * 2500, files=(attachment,)))
            await bridge.ingest(envelope("Ev-9", ts="9.0", channel="C2", text="Unrelated"))
            assert await other.process_once()  # another thread runs meanwhile
            assert await other.process_once() is False  # this thread waits for Ev-1's initial envelope
            host.release("Ev-1")
            assert await asyncio.wait_for(first, 5)
            assert bridge.inbox()["Ev-1"] == "pending"
            assert await other.process_once()  # Ev-2 now passes the continuing Ev-1
            assert await other.process_once()  # Ev-3
            bridge.due()
            assert await bridge.inbound.process_once()  # Ev-1's author ends with its revised result
            await bridge.send_all()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"Ev-1": "delivered", "Ev-2": "delivered", "Ev-3": "delivered", "Ev-9": "delivered"}
        assert slack.texts() == ["Other thread", "Covering Q3 too.", "Report, now with Q3"]
        assert [body["event"]["source_event_id"] for body in host.turn_bodies] == ["Ev-1", "Ev-9", "Ev-2", "Ev-3"]

        # Each submission carries its own conversation's inbox snapshot, taken right before it.
        before = host.body("Ev-1")["event"]["conversation"]["transport_queue"]
        assert (before["pending_count"], before["events"], before["complete"]) == (0, [], True)
        queue = host.body("Ev-2")["event"]["conversation"]["transport_queue"]
        assert queue["schema_version"] == 1 and queue["source"] == "slack-bridge inbox"
        assert queue["conversation_key"] == "slack:T1:C1:1.0" and queue["after_source_event_id"] == "Ev-2"
        assert (queue["pending_count"], queue["omitted_count"], queue["complete"]) == (1, 0, True)
        assert queue["text_limit_chars"] is None
        [queued] = queue["events"]
        assert queued["source_event_id"] == "Ev-3" and queued["inbox_state"] == "pending"
        assert queued["actor"] == {"platform_actor_id": "U1", "actor_team_id": "T1"}
        assert (queued["message_id"], queued["thread_id"]) == ("3.0", "1.0")
        assert queued["text"] == "x" * 2500 and queued["text_chars"] == 2500 and queued["text_truncated"] is False
        assert queued["files"] == [{"file_id": "F1", "file_name": "q3.pdf"}]
        assert queued["received_at"].endswith("+00:00")
        assert "files.slack.com" not in json.dumps(host.body("Ev-2"))
        assert host.body("Ev-3")["event"]["conversation"]["transport_queue"]["pending_count"] == 0
        assert "Ev-3" not in json.dumps(host.body("Ev-9")["event"]["conversation"]["transport_queue"])

    asyncio.run(run())


@pytest.mark.parametrize("tail_child", ["", "child-1", "child-2"])
def test_promoted_child_and_parent_tail_are_polled_independently(tmp_path, tail_child):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, work_ref="child-1")
        host.work["turn-1"] = [author_pending("turn-1", []), author_pending("turn-1", []),
                               author_terminal("turn-1", [], outcome="message", text="Parent tail",
                                               output_ref="out-tail", child=tail_child)]
        host.work["child-1"] = [child_pending("child-1"), child_done("child-1", "Child result")]
        host.work["child-2"] = [child_done("child-2", "Second child result")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            assert bridge.outbox() == []
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            # The child's result is sent while the inline author still lives.
            assert slack.texts() == ["Child result"] and bridge.inbox() == {"Ev-1": "pending"}
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"Ev-1": "delivered"}
        expected = ["Child result", "Parent tail"] + (["Second child result"] if tail_child == "child-2" else [])
        assert slack.texts() == expected
        # The tail neither replaced the child's reference nor made it poll again after its terminal.
        assert host.polls == ["turn-1", "child-1", "turn-1", "child-1", "turn-1"] + (
            ["child-2"] if tail_child == "child-2" else [])
        origins = [row["origin"]["task_id"] for row in bridge.outbox()]
        assert origins == ["child-1", "turn-1"] + (["child-2"] if tail_child == "child-2" else [])

    asyncio.run(run())


@pytest.mark.parametrize("reporting", [True, False])
@pytest.mark.parametrize("author_state", ["pending", "interrupted"])
def test_late_child_discovery_keeps_output_identity_through_restart(tmp_path, reporting, author_state):
    async def run():
        host, slack = FakeHost(reporting=reporting), FakeSlack()
        early = out("out-parent", "Same words")
        host.scripts["Ev-1"] = lambda body: continuing(
            "turn-1", body, outcome="message", text="Same words", output_ref="out-parent")
        discovery = author_pending if author_state == "pending" else author_interrupted
        host.work["turn-1"] = [author_pending("turn-1", [early]),
                               discovery("turn-1", [early], child="child-late")]
        if author_state == "pending":
            host.work["turn-1"] += [author_pending("turn-1", [early], child="child-late"),
                                   author_terminal("turn-1", [early], child="child-late")]
        host.work["child-late"] = [child_done("child-late", "Same words", output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Same words"] and host.polls == ["turn-1"]
            assert host.answers["Ev-1"]["work_ref"] == ""

            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            # Promotion happened on reentry, after the initial envelope was
            # already durable. Pending and interrupted authors both expose it.
            assert slack.texts() == ["Same words", "Same words"]
            assert "child-late" in host.polls
            assert bridge.inbox() == {"Ev-1": "pending" if author_state == "pending" else "failed"}
            assert [row["output_ref"] for row in bridge.outbox()] == ["out-parent", "out-child"]
            assert [row["origin"] for row in bridge.outbox()] == [
                _origin("Ev-1", "turn-1"), _origin("Ev-1", "child-late")]

            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            if author_state == "pending":
                # Replaying both selections after the adapter cache is gone
                # must not repeat either send, even though their text is equal.
                for _ in range(2):
                    bridge.due()
                    assert await bridge.inbound.process_once()
                assert bridge.inbox() == {"Ev-1": "delivered"}
            else:
                assert "interrupted" in bridge.inbox_error("Ev-1")
                assert "not restarted" in bridge.inbox_error("Ev-1")
            bridge.due()
            assert await bridge.inbound.process_once() is False
            await bridge.send_all()
            assert slack.texts() == ["Same words", "Same words"]
            assert len({row["request_id"] for row in bridge.outbox()}) == 2
            assert host.generations == ["Ev-1"] and len(host.turn_bodies) == 1
            assert {post["thread_ts"] for post in slack.posts} == {"1.0"}
            if reporting:
                assert [report["message"]["output_ref"] for report in host.reports] == ["out-parent", "out-child"]
            else:
                assert host.reports == []
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("parent_poll,error,final_state", [
    ((500, {"ok": False}), "HTTP 500", "pending"),
    ((404, {"ok": False}), "HTTP 404", "failed"),
    (author_interrupted("turn-1", []), "interrupted", "failed"),
])
def test_late_child_survives_restart_and_unavailable_parent(tmp_path, parent_poll, error, final_state):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body)
        host.work["turn-1"] = [author_pending("turn-1", []),
                               author_pending("turn-1", [], child="child-late"), parent_poll]
        host.work["child-late"] = [child_pending("child-late"), child_pending("child-late"),
                                   child_done("child-late", "Child result", output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            bridge.due()
            assert await bridge.inbound.process_once()
            assert host.polls.count("child-late") == 1 and bridge.outbox() == []
            await bridge.close()

            # The only available parent answer has no child reference. Child
            # custody must therefore come from SQLite, not adapter memory or a
            # successful rediscovery poll after restart.
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once()
            assert host.polls.count("child-late") == 2
            assert bridge.inbox() == {"Ev-1": "pending"}
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Child result"]
            assert bridge.inbox() == {"Ev-1": final_state}
            assert error in bridge.inbox_error("Ev-1")
            assert bridge.outbox()[0]["origin"] == _origin("Ev-1", "child-late")
            assert bridge.outbox()[0]["output_ref"] == "out-child"
            assert host.generations == ["Ev-1"] and len(host.turn_bodies) == 1
            if final_state == "pending":
                host.work["turn-1"] = [author_terminal("turn-1", [])]
                bridge.due()
                assert await bridge.inbound.process_once()
                assert bridge.inbox() == {"Ev-1": "delivered"}
            bridge.due()
            assert await bridge.inbound.process_once() is False
        finally:
            await bridge.close()

    asyncio.run(run())


def test_late_child_completes_before_slow_parent_poll_after_restart(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class GatedHost(FakeHost):
            block_parent = False

            async def handle(self, request):
                if self.block_parent and request.method == "GET" and request.url.path == "/presence/work/turn-1":
                    entered.set()
                    await release.wait()
                return await super().handle(request)

        host, slack = GatedHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body)
        host.work["turn-1"] = [author_pending("turn-1", []),
                               author_pending("turn-1", [], child="child-late"), author_pending("turn-1", [])]
        host.work["child-late"] = [child_pending("child-late"),
                                   child_done("child-late", "Child result", output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        work = None
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            for _ in range(2):
                bridge.due()
                assert await bridge.inbound.process_once()
            assert host.polls.count("child-late") == 1
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            host.block_parent = True
            work = asyncio.create_task(bridge.inbound.process_once())
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(100):
                await asyncio.sleep(0)
                await bridge.send_all()
                if slack.texts():
                    break
            assert slack.texts() == ["Child result"] and not work.done()
            assert bridge.outbox()[0]["output_ref"] == "out-child"
            assert len(host.turn_bodies) == 1
            release.set()
            assert await asyncio.wait_for(work, 2)
            assert bridge.inbox() == {"Ev-1": "pending"}
        finally:
            release.set()
            if work is not None:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await bridge.close()

    asyncio.run(run())


def test_late_child_custody_survives_cancelled_poll_and_unknown_send(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class GatedHost(FakeHost):
            async def handle(self, request):
                if request.method == "GET" and request.url.path == "/presence/work/child-late":
                    entered.set()
                    await release.wait()
                return await super().handle(request)

        host, slack = GatedHost(), FakeSlack(lose_posts=True)
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body)
        host.work["turn-1"] = [author_pending("turn-1", []),
                               author_pending("turn-1", [], child="child-late")]
        host.work["child-late"] = [child_done("child-late", "Child result", output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        work = None
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            bridge.due()
            work = asyncio.create_task(bridge.inbound.process_once())
            await asyncio.wait_for(entered.wait(), 2)
            # Stop before the discovered child's first HTTP request returns.
            # Its reference must already be durable, not merely checkpointed
            # after all polls complete or the final pending status is emitted.
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            await bridge.close()
            with sqlite3.connect(bridge.store.path) as db:
                db.execute("UPDATE inbox SET lease_until=0, available_at=0")
            host.work["turn-1"] = [(500, {"ok": False})]
            release.set()

            bridge = Bridge(tmp_path, host, slack)
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.attempts == 1
            assert [row["state"] for row in bridge.outbox()] == ["uncertain"]
            assert bridge.outbox()[0]["output_ref"] == "out-child"
            request_id = bridge.outbox()[0]["request_id"]
            await bridge.close()

            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.attempts == 1
            assert [row["request_id"] for row in bridge.outbox()] == [request_id]
            assert bridge.inbox() == {"Ev-1": "pending"}
            assert "HTTP 500" in bridge.inbox_error("Ev-1")
            assert len(host.turn_bodies) == 1 and host.generations == ["Ev-1"]
            assert [(report["state"], report["message"]["output_ref"]) for report in host.reports] == [
                ("uncertain", "out-child")]
        finally:
            release.set()
            if work is not None:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await bridge.close()

    asyncio.run(run())


def test_identical_text_is_new_speech_but_a_repeated_selection_is_sent_once(tmp_path):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        first, again = out("out-A", "Same words"), out("out-B", "Same words")
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Same words",
                                                       output_ref="out-A")
        # The author re-finalizes its released selection: the terminal names nothing new.
        host.work["turn-1"] = [author_pending("turn-1", [first]), author_pending("turn-1", [first, again]),
                               author_terminal("turn-1", [first, again], outcome="silent")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Say it"))
            for _ in range(3):
                bridge.due()
                assert await bridge.inbound.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert slack.texts() == ["Same words", "Same words"]
        assert [row["output_ref"] for row in bridge.outbox()] == ["out-A", "out-B"]
        assert bridge.inbox() == {"Ev-1": "delivered"}

    asyncio.run(run())


class _Crash(BaseException):
    """A process death between two durable writes, not a handled error."""


def test_lost_reply_replay_and_restart_neither_regenerate_nor_duplicate(tmp_path, monkeypatch):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        early = out("out-1", "Early answer")
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early answer",
                                                       output_ref="out-1")
        host.lose_reply.add("Ev-1")
        host.work["turn-1"] = [author_pending("turn-1", [early]),
                               author_terminal("turn-1", [early], outcome="silent")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            payload = envelope("Ev-1", ts="1.0", text="Quick question")
            await bridge.ingest(payload)
            await bridge.ingest(payload)  # Slack retries the same Socket envelope
            assert bridge.websocket.acks == ["env-Ev-1", "env-Ev-1"] and list(bridge.inbox()) == ["Ev-1"]
            assert await bridge.inbound.process_once()  # Host recorded the turn; its reply was lost
            assert bridge.outbox() == []
            bridge.due()

            def crash(*_args, **_kwargs):
                raise _Crash()

            monkeypatch.setattr(bridge.store, "retry_inbox", crash)
            with pytest.raises(_Crash):
                await bridge.inbound.process_once()  # replayed envelope stored and queued, then death
        finally:
            await bridge.close()
        assert [row["text"] for row in bridge.outbox()] == ["Early answer"]
        with sqlite3.connect(bridge.store.path) as db:
            db.execute("UPDATE inbox SET lease_until=0")  # the dead worker's lease expires
        restarted = Bridge(tmp_path, host, slack)
        try:
            assert await restarted.inbound.process_once()  # same reference, polled again: nothing new
            await restarted.send_all()
        finally:
            await restarted.close()
        assert slack.texts() == ["Early answer"] and len(restarted.outbox()) == 1
        assert restarted.inbox() == {"Ev-1": "delivered"}
        assert host.generations == ["Ev-1"] and len(host.turn_bodies) == 2
        # The retry resubmits the identical event, including its persisted queue snapshot.
        assert host.turn_bodies[0] == host.turn_bodies[1]

    asyncio.run(run())


@pytest.mark.parametrize("reporting", [True, False])
def test_unknown_provider_outcome_is_never_resent_by_a_later_poll(tmp_path, reporting):
    async def run():
        host, slack = FakeHost(reporting=reporting), FakeSlack(lose_posts=True)
        early = out("out-1", "Early answer")
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early answer",
                                                       output_ref="out-1")
        host.work["turn-1"] = [author_pending("turn-1", [early]), author_pending("turn-1", [early]),
                               author_terminal("turn-1", [early], outcome="silent")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()  # first Slack response is lost: immediately unknown
            assert slack.attempts == 1 and [row["state"] for row in bridge.outbox()] == ["uncertain"]
            for _ in range(2):
                bridge.due()
                assert await bridge.inbound.process_once()  # the same output is presented again
            await bridge.send_all()
        finally:
            await bridge.close()
        assert slack.attempts == 1 and len(bridge.outbox()) == 1
        assert bridge.inbox() == {"Ev-1": "delivered"}
        receipt = bridge.store.delivery_receipt(bridge.outbox()[0]["request_id"])
        assert [part["state"] for part in receipt["parts"]] == ["uncertain"]
        if reporting:
            assert [(report["state"], report["message"]["output_ref"]) for report in host.reports] == [
                ("uncertain", "out-1")]
        else:
            assert host.reports == []

    asyncio.run(run())


@pytest.mark.parametrize("outcome,text,sent", [
    ("tool_delivered", "I posted it myself", []),  # sent by the author's own Slack tool already
    ("message", "Late result", ["Late result"]),  # no send tool: the bridge ships it once
])
def test_late_terminal_text_is_shipped_only_when_it_is_owed(tmp_path, outcome, text, sent):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body)
        host.work["turn-1"] = [author_pending("turn-1", []),
                               author_terminal("turn-1", [], outcome=outcome, text=text,
                                               output_ref="out-late" if outcome == "message" else "")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Do it"))
            for _ in range(2):
                bridge.due()
                assert await bridge.inbound.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert slack.texts() == sent and bridge.inbox() == {"Ev-1": "delivered"}

    asyncio.run(run())


@pytest.mark.parametrize("with_child", [False, True])
def test_interrupted_author_is_visible_and_never_restarted(tmp_path, with_child):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        early = out("out-1", "Early answer")
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early answer",
                                                       output_ref="out-1", work_ref="child-1" if with_child else "")
        host.work["turn-1"] = [author_interrupted("turn-1", [early])]
        host.work["child-1"] = [child_pending("child-1"), child_done("child-1", "Child result")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.inbound.process_once()
            if with_child:  # the independent child still finishes and is delivered
                assert bridge.inbox() == {"Ev-1": "pending"}
                bridge.due()
                assert await bridge.inbound.process_once()
            bridge.due()
            assert await bridge.inbound.process_once() is False  # nothing left to retry or resubmit
            await bridge.send_all()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"Ev-1": "failed"}
        assert "interrupted" in bridge.inbox_error("Ev-1") and "not restarted" in bridge.inbox_error("Ev-1")
        assert slack.texts() == ["Early answer"] + (["Child result"] if with_child else [])
        assert host.generations == ["Ev-1"] and len(host.turn_bodies) == 1
        assert host.polls == (["turn-1", "child-1", "child-1"] if with_child else ["turn-1"])

    asyncio.run(run())


def test_host_without_continuation_keeps_the_legacy_protocol(tmp_path):
    async def run():
        host, slack = FakeHost(continuation=False), FakeSlack()
        host.scripts["Ev-1"] = lambda body: completed("turn-1", body, outcome="deferred", text="Working",
                                                      work_ref="work-1")
        host.work["work-1"] = [child_pending("work-1"), child_done("work-1", "Done")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            for _ in range(2):
                bridge.due()
                assert await bridge.inbound.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        body = host.body("Ev-1")
        assert "continuation_version" not in body and body["delivery_reporting_version"] == 1
        assert "transport_queue" not in body["event"]["conversation"]
        assert slack.texts() == ["Working", "Done"]
        assert [row["request_id"].rsplit(":", 2)[1:] for row in bridge.outbox()] == [["ack", "0"], ["final", "0"]]
        assert bridge.inbox() == {"Ev-1": "delivered"}

    asyncio.run(run())


def test_turn_that_ends_within_the_request_keeps_the_completed_path(tmp_path):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: completed("turn-1", body, text="Quick answer", output_ref="out-q")
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert host.body("Ev-1")["continuation_version"] == 1 and host.polls == []
        assert slack.texts() == ["Quick answer"] and bridge.inbox() == {"Ev-1": "delivered"}
        assert bridge.outbox()[0]["request_id"].startswith("presence-output:")
        assert bridge.outbox()[0]["output_ref"] == "out-q"

    asyncio.run(run())


@pytest.mark.parametrize("poll,inbox_state,error", [
    ((500, {"ok": False}), "pending", "HTTP 500"),
    ((200, {"ok": True, "status": "pending", "work_ref": "turn-1"}), "pending", "without continuation_version"),
    ((404, {"ok": False}), "failed", "HTTP 404"),
])
def test_an_unreadable_poll_never_holds_back_released_speech(tmp_path, poll, inbox_state, error):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early answer",
                                                       output_ref="out-1")
        host.work["turn-1"] = [poll]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
        finally:
            await bridge.close()
        assert slack.texts() == ["Early answer"]
        assert bridge.inbox() == {"Ev-1": inbox_state} and error in bridge.inbox_error("Ev-1")

    asyncio.run(run())


def test_continuing_answer_without_its_reference_is_not_accepted(tmp_path):
    async def run():
        host, slack = FakeHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: {**continuing("turn-1", body, outcome="message", text="Early",
                                                          output_ref="out-1"), "continuation_ref": ""}
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.inbound.process_once()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"Ev-1": "pending"} and bridge.outbox() == []
        assert "continuation reference" in bridge.inbox_error("Ev-1")
        with sqlite3.connect(bridge.store.path) as db:
            assert db.execute("SELECT host_reference FROM inbox").fetchone()[0] == ""

    asyncio.run(run())


def test_queue_snapshot_counts_every_cut_and_names_only_this_conversation(tmp_path):
    store = BridgeStore(tmp_path)

    def ingest(payload: dict) -> None:
        store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))

    ingest(envelope("Ev-1", ts="1.0", text="first"))
    ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="second message"))
    ingest({"type": "events_api", "envelope_id": "env-Ev-3", "payload": {
        "event_id": "Ev-3", "team_id": "T1", "event": {
            "type": "reaction_added", "user": "U2", "reaction": "eyes",
            "item": {"type": "message", "channel": "C1", "ts": "1.0"}, "event_ts": "3.0"}}})
    ingest(envelope("Ev-4", ts="4.0", thread_ts="1.0", text="fourth"))
    ingest(envelope("Ev-5", ts="5.0", thread_ts="1.0", user="U_BOT", text="own message"))  # ignored
    ingest(envelope("Ev-6", ts="6.0", channel="C2", text="other conversation"))
    item = store.claim_inbox()
    assert item.event_id == "Ev-1"
    snapshot = store.conversation_queue(item, limit=2, text_chars=6)
    assert (snapshot["pending_count"], snapshot["omitted_count"], snapshot["complete"]) == (3, 1, False)
    assert snapshot["text_limit_chars"] == 6 and snapshot["after_source_event_id"] == "Ev-1"
    second, reaction = snapshot["events"]
    assert (second["source_event_id"], second["text"], second["text_chars"], second["text_truncated"]) == (
        "Ev-2", "second", 14, True)
    assert reaction["source_event_id"] == "Ev-3" and reaction["reaction"] == {"kind": "added", "name": "eyes"}
    assert reaction["actor"]["platform_actor_id"] == "U2" and reaction["text"] == "" and not reaction["text_truncated"]
    assert "other conversation" not in json.dumps(snapshot) and "own message" not in json.dumps(snapshot)


@pytest.mark.parametrize("reporting", [True, False])
@pytest.mark.parametrize("blocked_ref", ["turn-1", "child-1"])
def test_speech_and_other_reference_progress_before_a_slow_poll_finishes(tmp_path, reporting, blocked_ref):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class GatedHost(FakeHost):
            async def handle(self, request):
                if request.method == "GET" and request.url.path == f"/presence/work/{blocked_ref}":
                    # The initial selection must already be durable before ANY
                    # poll; waiting for the HTTP timeout would be too late.
                    assert bridge.outbox()[0]["text"] == "Early"
                    entered.set()
                    await release.wait()
                return await super().handle(request)

        host, slack = GatedHost(reporting=reporting), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early",
                                                       output_ref="out-early", work_ref="child-1")
        host.scripts["Ev-2"] = lambda body: completed("turn-2", body, text="Next message", output_ref="out-next")
        host.work["turn-1"] = [author_terminal("turn-1", [], outcome="message", text="Author correction", output_ref="out-tail")]
        host.work["child-1"] = [(200, {"ok": True, "status": "completed", "work_ref": "child-1",
                                      "outcome": "message", "text": "Child result"})]
        bridge = Bridge(tmp_path, host, slack)
        work = None
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="First"))
            work = asyncio.create_task(bridge.inbound.process_once())
            await asyncio.wait_for(entered.wait(), 2)
            assert not work.done()
            await bridge.send_all()
            assert slack.texts()[0] == "Early"
            # The independently completed poll must reach the outbox even while
            # the sibling HTTP call is blocked, without waiting for its timeout.
            expected = "Child result" if blocked_ref == "turn-1" else "Author correction"
            for _ in range(100):
                await asyncio.sleep(0)
                await bridge.send_all()
                if expected in slack.texts():
                    break
            assert expected in slack.texts() and not work.done()
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="New facts"))
            assert await bridge.inbound_worker().process_once()
            assert bridge.inbox()["Ev-2"] == "delivered" and not work.done()
            release.set()
            assert await asyncio.wait_for(work, 2)
            await bridge.send_all()
            assert sorted(slack.texts()) == sorted(["Early", "Author correction", "Child result", "Next message"])
            assert len({row["request_id"] for row in bridge.outbox()}) == 4
        finally:
            release.set()
            if work is not None:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await bridge.close()

    asyncio.run(run())


def test_cancelling_a_worker_reaps_both_independent_polls(tmp_path):
    async def run():
        entered = {ref: asyncio.Event() for ref in ("turn-1", "child-1")}
        stopped = []

        class GatedHost(FakeHost):
            async def handle(self, request):
                if request.method == "GET" and request.url.path.startswith("/presence/work/"):
                    ref = request.url.path.rsplit("/", 1)[-1]
                    entered[ref].set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stopped.append(ref)
                return await super().handle(request)

        host, slack = GatedHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early",
                                                       output_ref="out-early", work_ref="child-1")
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="First"))
            work = asyncio.create_task(bridge.inbound.process_once())
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            assert sorted(stopped) == ["child-1", "turn-1"]
            await bridge.send_all()
            assert slack.texts() == ["Early"]
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("reporting", [True, False])
def test_completed_v1_identity_survives_replay_restart_and_equal_text_selections(tmp_path, reporting):
    async def run():
        host, slack = FakeHost(reporting=reporting), FakeSlack()
        for number in (1, 2):
            host.scripts[f"Ev-{number}"] = lambda body, number=number: completed(
                f"turn-{number}", body, text="Same words", output_ref=f"out-{number}")
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="First"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            with sqlite3.connect(bridge.store.path) as db:
                db.execute("UPDATE inbox SET state='pending', available_at=0")
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            assert await bridge.inbound.process_once()
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Second"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Same words", "Same words"]
            assert [row["output_ref"] for row in bridge.outbox()] == ["out-1", "out-2"]
            assert host.generations == ["Ev-1", "Ev-2"] and host.polls == []
            if reporting:
                assert [report["message"]["output_ref"] for report in host.reports] == ["out-1", "out-2"]
        finally:
            await bridge.close()

    asyncio.run(run())


def test_default_queue_snapshot_keeps_all_events_and_every_character(tmp_path):
    store = BridgeStore(tmp_path)
    first = envelope("Ev-0", ts="1.0", text="first")
    store.ingest_envelope(first, parse_socket_envelope(first))
    texts = {f"Ev-{number}": f"{number}: " + ("界 🛰\n" * 5000) + "END OF FULL MESSAGE" for number in range(1, 14)}
    for number, (event_id, text) in enumerate(texts.items(), 2):
        payload = envelope(event_id, ts=f"{number}.0", thread_ts="1.0", text=text)
        store.ingest_envelope(payload, parse_socket_envelope(payload))
    item = store.claim_inbox()
    full = store.conversation_queue(item)
    assert full["pending_count"] == 13 and full["omitted_count"] == 0 and full["complete"] is True
    assert full["text_limit_chars"] is None
    assert {event["source_event_id"]: event["text"] for event in full["events"]} == texts
    assert all(not event["text_truncated"] and event["text_chars"] == len(event["text"]) for event in full["events"])
    store.set_transport_queue(item.row_id, item.lease_token, full)
    store.retry_inbox(item.row_id, item.lease_token, "lost host reply", delay_seconds=0)
    assert BridgeStore(tmp_path).claim_inbox().transport_queue == full
    # All rows being present is insufficient if even one text was clipped.
    clipped = store.conversation_queue(item, text_chars=1000)
    assert clipped["omitted_count"] == 0 and clipped["complete"] is False


def test_queued_blocks_and_edits_share_the_submission_file_projection(tmp_path):
    store = BridgeStore(tmp_path)
    first = envelope("Ev-0", ts="1.0", text="first")
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": "FULL BLOCK CONTENT"}}]
    block_event = envelope("Ev-1", ts="2.0", thread_ts="1.0", text="")
    block_event["payload"]["event"]["blocks"] = blocks
    edit = {"type": "events_api", "envelope_id": "env-Ev-2", "payload": {
        "event_id": "Ev-2", "team_id": "T1", "event": {
            "type": "message", "subtype": "message_changed", "channel": "C1",
            "message": {"user": "U1", "ts": "3.0", "thread_ts": "1.0", "text": "edited",
                        "files": [{"id": "F1", "name": "report.pdf", "title": "report",
                                   "url_private": "https://files.slack.com/private", "preview": "secret preview"}]},
            "previous_message": {"user": "U1", "ts": "3.0", "thread_ts": "1.0", "text": "old"}}}}
    for payload in (first, block_event, edit):
        store.ingest_envelope(payload, parse_socket_envelope(payload))
    queued = store.conversation_queue(store.claim_inbox())["events"]
    assert queued[0]["provider_facts"]["blocks"] == blocks
    assert queued[1]["provider_facts"]["message"]["files"] == [{"id": "F1", "name": "report.pdf", "title": "report"}]
    assert "private" not in json.dumps(queued) and "secret preview" not in json.dumps(queued)


def test_queue_refresh_retries_exact_observation_after_lost_ack_and_restart(tmp_path):
    async def run():
        class LostAckHost(FakeHost):
            async def handle(self, request):
                response = await super().handle(request)
                if request.method == "POST" and request.url.path.startswith("/presence/work/") and len(self.queue_reports) == 2:
                    raise httpx.ReadTimeout("observation accepted, ACK lost", request=request)
                return response

        host, slack = LostAckHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early", output_ref="early-1")
        host.work["turn-1"] = [author_pending("turn-1", [])]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Start"))
            assert await bridge.inbound.process_once()
            original = host.body("Ev-1")
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="LATER " + "x" * 5000))
            bridge.due()
            assert await bridge.inbound.process_once()  # its fresh observation ACK is lost
            assert host.queue_reports[-1]["transport_queue"]["pending_count"] == 1
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            await bridge.ingest(envelope("Ev-3", ts="3.0", thread_ts="1.0", text="Even later"))
            bridge.due()
            assert await bridge.inbound.process_once()
            assert host.queue_reports[1] == host.queue_reports[2]  # same bytes after restart
            bridge.due()
            assert await bridge.inbound.process_once()  # successful ACK permits a fresh observation
            assert host.queue_reports[-1]["transport_queue"]["pending_count"] == 2
            assert host.queue_reports[-1]["transport_queue"]["after_source_event_id"] == "Ev-1"
            assert host.body("Ev-1") == original and host.generations == ["Ev-1"]
            assert host.queue_reports[-1]["ref"] == "turn-1"
            await bridge.send_all()
            assert slack.texts() == ["Early"]
        finally:
            await bridge.close()

    asyncio.run(run())
