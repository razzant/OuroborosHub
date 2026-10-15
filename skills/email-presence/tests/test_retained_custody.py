"""Retained Host refusals through the real adapter, runtime, SQLite store and SMTP worker.

The loopback Host is scripted over httpx in the shapes core produces: a typed HTTP
error envelope with ``disposition: retained`` and ``/presence/work`` views whose
``status: unresolved`` carries no authored outcome or text. SMTP is a fake client.
"""
import asyncio
import base64
import json
import sqlite3

import httpx
import pytest

from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_transport import FakeClient, FakeIMAP

BINDING = "a" * 32
FIRST, SECOND = "INBOX:0:1:<1>", "INBOX:0:2:<2>"


def retained(*, turn="turn-1", child="", code="presence_attempt_outcome_unknown", mode=1):
    return 409, {"ok": False, "error": f"{code}: source_event_id", "code": code, "disposition": "retained",
                 "field": "source_event_id", "turn_ref": turn, "delivery_reporting_version": mode,
                 **({"work_ref": child} if child else {})}


def unresolved(turn, *, child="", code="presence_attempt_outcome_unknown"):
    return 200, {"ok": True, "status": "unresolved", "disposition": "retained", "code": code, "turn_ref": turn,
                 "delivery_reporting_version": 1, **({"work_ref": child} if child else {})}


def terminal(ref, text="", *, status="completed", outcome="message"):
    return 200, {"ok": True, "status": status, "outcome": outcome, "text": text, "work_ref": ref,
                 "delivery_reporting_version": 1}


def answered(turn, text, mode=1):
    return 200, {"ok": True, "status": "completed", "outcome": "message", "text": text, "turn_ref": turn,
                 "work_ref": "", "delivery_reporting_version": mode}


class Host:
    """One durable answer per source event; scripted polls keep their last answer."""

    def __init__(self, reporting=True):
        self.reporting, self.turns, self.answers, self.work = reporting, {}, {}, {}
        self.posts, self.generations, self.polls, self.reports = [], [], [], []

    def handle(self, request):
        assert request.headers["X-Skill-Token"] == "token"
        path = request.url.path
        if path == "/identity":
            return httpx.Response(200, json={"ok": True, **({"presence_delivery_version": 1} if self.reporting else {})})
        if path == "/presence/delivery":
            self.reports.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "recorded": True, "duplicate": False})
        if path == "/presence/turn":
            source = json.loads(request.content)["event"]["source_event_id"]
            self.posts.append(source)
            if source not in self.answers:  # a retry replays; it never generates again
                self.generations.append(source)
                self.answers[source] = self.turns[source]
            return httpx.Response(self.answers[source][0], json=self.answers[source][1])
        assert request.url.params["binding_id"] == BINDING
        ref = path.removeprefix("/presence/work/")
        self.polls.append(ref)
        queue = self.work[ref]
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=body)


class Bridge:
    def __init__(self, root, host, client):
        self.root, self.host, self.client = root, host, client
        self.restart()

    def restart(self):
        self.store = EmailStore(self.root)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.host.handle))
        self.adapter = LoopbackPresenceHostAdapter(BINDING, host_service_url="http://127.0.0.1:8767",
                                                   skill_token="token", http_client=self.http)
        self.runtime = EmailRuntime(self.store, self.client, self.adapter, account="bot@example.org")

    async def close(self):
        await self.http.aclose()

    def receive(self, uid, *, thread=""):
        return self.store.ingest({"uid": uid, "message_id": f"<{uid}>", "sender": "reader@example.org",
                                  "subject": "Question", "body": f"Body {uid}",
                                  "references": [thread] if thread else []})[1]

    def due(self, *, inbox=True):
        with sqlite3.connect(self.store.path) as db:
            if inbox:
                db.execute("UPDATE inbox SET available_at=0")
            db.execute("UPDATE outbox SET available_at=0")

    def inbox(self):
        with sqlite3.connect(self.store.path) as db:
            return {row[0]: row[1] for row in db.execute("SELECT message_id, state FROM inbox ORDER BY id")}

    def row(self, message_id="<1>"):
        with sqlite3.connect(self.store.path) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM inbox WHERE message_id=?", (message_id,)).fetchone())

    def receipt(self, message_id="<1>"):
        prefix, encoded = self.row(message_id)["host_reference"].split(":", 1)
        return prefix, json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    async def send(self):
        self.due(inbox=False)
        while self.runtime.process_outbox():
            self.due(inbox=False)
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE outbox SET report_due_at=CASE WHEN report_due_at>0 THEN 1 ELSE 0 END")
        while await self.runtime.process_delivery_report():
            pass

    def bodies(self):
        return [message.get_content().strip() for message in self.client.sent]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_USER", "bot@example.org")
    return tmp_path, FakeClient(FakeIMAP(1800000000))


@pytest.mark.parametrize("reporting", [True, False])
@pytest.mark.parametrize("late_status,late_outcome", [("completed", "message"), ("failed", "deferred")])
def test_retained_event_frees_thread_and_child_then_late_original_each_reply_once(rig, reporting, late_status, late_outcome):
    async def run():
        root, client = rig
        host, mode = Host(reporting), int(reporting)
        host.turns[FIRST] = retained(child="child-1", mode=mode)
        host.turns[SECOND] = answered("turn-2", "Second answer", mode)
        host.work["turn-1"] = [unresolved("turn-1", child="child-1")]
        host.work["child-1"] = [(202, {"ok": True, "status": "pending", "work_ref": "child-1"}),
                                terminal("child-1", "Child answer")]
        bridge = Bridge(root, host, client)
        try:
            assert bridge.receive(1) and bridge.receive(2, thread="<1>")
            assert await bridge.runtime.process_inbox()
            prefix, saved = bridge.receipt()
            assert prefix == "retained" and saved["response"] == host.answers[FIRST][1]
            assert saved["http_status"] == 409 and saved["delivery_reporting_version"] == mode
            assert bridge.inbox() == {"<1>": "retry", "<2>": "pending"}
            assert "retained" in bridge.row()["last_error"] and "unresolved" in bridge.row()["last_error"]
            assert bridge.store.status()["inbox_retained"] == 1
            # The retained event is not due yet, and no longer holds its thread.
            assert await bridge.runtime.process_inbox()
            assert bridge.inbox() == {"<1>": "retry", "<2>": "completed"}
            await bridge.send()
            await bridge.close()

            bridge.restart()
            bridge.due()
            assert await bridge.runtime.process_inbox()
            await bridge.send()
            assert bridge.bodies() == ["Second answer", "Child answer"]
            assert bridge.inbox()["<1>"] == "retry" and bridge.receipt() == (prefix, saved)
            await bridge.close()

            host.work["turn-1"] = [terminal("turn-1", "Late original answer", status=late_status, outcome=late_outcome)]
            bridge.restart()
            bridge.due()
            assert await bridge.runtime.process_inbox()
            assert not bridge.receive(1)  # IMAP redelivery is deduplicated, never resubmitted
            bridge.due()
            assert not await bridge.runtime.process_inbox()
            await bridge.send()
            assert bridge.bodies() == ["Second answer", "Child answer", "Late original answer"]
            assert bridge.inbox() == {"<1>": "completed" if late_status == "completed" else "failed", "<2>": "completed"}
            assert bridge.receipt() == (prefix, saved) and bridge.store.status()["inbox_retained"] == 0
            assert host.posts == host.generations == [FIRST, SECOND]
            assert all(message["In-Reply-To"] == "<1>" for message in client.sent[1:])
            receipts = [bridge.store.receipt(f"presence:{FIRST}:work:{ref}") for ref in ("child-1", "turn-1")]
            assert [item["delivery_reporting"]["origin"] for item in receipts] == [
                {"kind": "automatic", "task_id": ref, "source_event_id": FIRST} for ref in ("child-1", "turn-1")]
            assert [(report["origin"].get("task_id"), report["text"].strip()) for report in host.reports] == (
                [("turn-2", "Second answer"), ("child-1", "Child answer"), ("turn-1", "Late original answer")]
                if reporting else [])
        finally:
            await bridge.close()

    asyncio.run(run())


def test_child_named_later_is_kept_before_polled_and_cancelled_original_keeps_receipt(rig):
    async def run():
        root, client = rig
        host = Host()
        host.turns[FIRST] = retained(code="presence_turn_cancelled")
        host.work["turn-1"] = [unresolved("turn-1", code="presence_turn_cancelled"),
                               unresolved("turn-1", child="child-late", code="presence_turn_cancelled"),
                               terminal("turn-1", status="cancelled", outcome="silent")]
        host.work["child-late"] = [terminal("child-late", "Admitted work result")]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            bridge.due()
            assert await bridge.runtime.process_inbox()
            # Persisted with the receipt before any child poll.
            assert bridge.receipt()[1]["work_refs"] == ["child-late"] and "child-late" not in host.polls
            await bridge.close()
            bridge.restart()
            bridge.due()
            assert await bridge.runtime.process_inbox()
            await bridge.send()
            assert bridge.bodies() == ["Admitted work result"] and bridge.inbox() == {"<1>": "failed"}
            error = bridge.row()["last_error"]
            assert "retained" in error and "presence_turn_cancelled" in error and "original turn-1: cancelled" in error
            assert bridge.receipt()[1]["response"] == host.answers[FIRST][1] and host.posts == [FIRST]
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("answer,terminal_state", [
    ((409, {"ok": False, "code": "presence_event_identity_conflict", "disposition": "rejected", "error": "x"}), True),
    ((403, {"ok": False, "code": "presence_auth_blocked", "disposition": "blocked"}), False),
    ((409, {"ok": False, "code": "presence_start_unwritable", "disposition": "retry"}), False),
    ((403, {"error": "binding"}), False),
    ((404, {"detail": "Not Found"}), False),
    ((409, {"ok": False, "code": "presence_attempt_outcome_unknown", "disposition": "retained"}), False),
])
def test_typed_dispositions_and_untyped_errors(rig, answer, terminal_state):
    async def run():
        root, client = rig
        host = Host()
        host.turns[FIRST], host.turns[SECOND] = answer, answered("turn-2", "Second answer")
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            bridge.receive(2, thread="<1>")
            for _ in range(2):
                bridge.due()
                assert await bridge.runtime.process_inbox()
            if terminal_state:  # poison input: failed with its receipt; the thread moves on
                assert bridge.inbox() == {"<1>": "failed", "<2>": "completed"}
                assert bridge.receipt() == ("rejected", {"http_status": answer[0], "response": answer[1]})
                assert host.posts == [FIRST, SECOND]
            else:  # repair or retry first: the event stays first in its thread, never dropped
                assert bridge.inbox() == {"<1>": "retry", "<2>": "pending"} and bridge.row()["host_reference"] == ""
                assert host.posts == [FIRST, FIRST]
                host.answers[FIRST] = answered("turn-1", "After repair")
                for _ in range(2):
                    bridge.due()
                    assert await bridge.runtime.process_inbox()
                await bridge.send()
                assert bridge.bodies() == ["After repair", "Second answer"]
            assert host.polls == []
        finally:
            await bridge.close()

    asyncio.run(run())


def test_retained_poll_errors_including_403_404_never_drop_custody(rig):
    async def run():
        root, client = rig
        host = Host()
        host.turns[FIRST] = retained(child="child-1")
        host.work["turn-1"] = [(404, {"ok": False, "code": "presence_work_not_found", "disposition": "rejected"}),
                               terminal("turn-1", status="failed", outcome="silent")]
        host.work["child-1"] = [(403, {"ok": False}), terminal("child-1", "Child answer")]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            assert bridge.inbox() == {"<1>": "retry"}
            assert "HTTP 404" in bridge.row()["last_error"] and "HTTP 403" in bridge.row()["last_error"]
            bridge.due()
            assert await bridge.runtime.process_inbox()
            await bridge.send()
            assert bridge.bodies() == ["Child answer"] and bridge.inbox() == {"<1>": "failed"}
        finally:
            await bridge.close()

    asyncio.run(run())


def test_selected_output_survives_unresolved_author_and_repeated_terminal(rig):
    async def run():
        root, client = rig
        host = Host()
        host.turns[FIRST] = retained()
        selected = {"output_ref": "selection-1", "outcome": "message", "text": "Selected answer"}
        status, body = unresolved("turn-1")
        host.work["turn-1"] = [(status, {**body, "outputs": [selected]})]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            await bridge.send()
            assert bridge.bodies() == ["Selected answer"]
            await bridge.close()
            bridge.restart()
            bridge.due()
            assert await bridge.runtime.process_inbox()
            await bridge.send()
            status, body = terminal("turn-1", "Selected answer", status="failed", outcome="deferred")
            host.work["turn-1"] = [(status, {**body, "output_ref": "selection-1"})]
            bridge.due()
            assert await bridge.runtime.process_inbox()
            await bridge.send()
            assert bridge.bodies() == ["Selected answer"]
            assert bridge.inbox()["<1>"] == "failed"
        finally:
            await bridge.close()
    asyncio.run(run())
