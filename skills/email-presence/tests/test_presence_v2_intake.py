"""Presence continuation version 2 (timely intake) through the real adapter, runtime and store.

The loopback Host is scripted over httpx in the shapes of the v2 wire contract: an
early custody ACK, heard/context completions, a typed 429 capacity retry and
``reply_anchor`` on author outputs. SMTP is the transport tests' fake client. These
Host answers are not produced by a real Host process.
"""
import asyncio
import base64
import contextlib
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from lib import store as store_module
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_transport import FakeClient, FakeIMAP

BINDING = "a" * 32
FIRST, SECOND, THIRD, NINTH = "INBOX:0:1:<1>", "INBOX:0:2:<2>", "INBOX:0:3:<3>", "INBOX:0:9:<9>"


def ack(turn, body):
    return 200, {"ok": True, "status": "continuing", "outcome": "deferred", "text": "", "output_ref": "",
                 "turn_ref": turn, "continuation_ref": turn, "work_ref": "", "continuation_version": 2,
                 "delivery_reporting_version": body.get("delivery_reporting_version", 0)}


def heard(body, turn=""):
    return 200, {"ok": True, "status": "completed", "outcome": "silent", "text": "", "turn_ref": turn, "work_ref": "",
                 "output_ref": "", "continuation_ref": "", "continuation_version": 2,
                 "delivery_reporting_version": body.get("delivery_reporting_version", 0)}


def answered(turn, body, text, output_ref, anchor):
    status, payload = heard(body, turn)
    return status, {**payload, "outcome": "message", "text": text, "output_ref": output_ref, "reply_anchor": anchor}


def busy(field):
    return 429, {"ok": False, "error": f"presence_capacity_full: {field}", "code": "presence_capacity_full",
                 "disposition": "retry", "field": field, "retry_after": 1 if field == "conversation" else 5,
                 "attachment_manifest": []}


def output(output_ref, text, anchor=None, outcome="message"):
    row = {"output_ref": output_ref, "outcome": outcome, "text": text}
    return {**row, "reply_anchor": anchor} if anchor else row


def view(ref, outputs, *, status="pending", child="", version=2, **terminal):
    body = {"ok": True, "status": status, "work_ref": ref, "continuation_ref": ref, "task_id": ref,
            "continuation_version": version, "outputs": outputs, "child_work_ref": child,
            "delivery_reporting_version": 1, **terminal}
    return (202 if status == "pending" else 200), body


def child_view(ref, text="", *, status="completed", output_ref=""):
    if status == "pending":
        return 202, {"ok": True, "status": "pending", "work_ref": ref, "delivery_reporting_version": 1}
    return 200, {"ok": True, "status": status, "outcome": "message", "text": text, "work_ref": ref,
                 "output_ref": output_ref, "delivery_reporting_version": 1}


class V2Host:
    """One durable answer per source event; typed 429s first; scripted polls keep their last answer."""

    def __init__(self, *, identity=None, reporting=True):
        self.identity = {"presence_continuation_version": 1, "presence_continuation_versions": [1, 2]} \
            if identity is None else identity
        self.reporting = reporting
        self.turns, self.answers, self.busy, self.work = {}, {}, {}, {}
        self.posts, self.bodies, self.generations, self.polls, self.reports = [], [], [], [], []

    def body(self, source):
        return [body for body in self.bodies if body["event"]["source_event_id"] == source][-1]

    def anchor(self, source):
        return {"source_event_id": source, "message": self.body(source)["event"]["message"]}

    def handle(self, request):
        assert request.headers["X-Skill-Token"] == "token"
        path = request.url.path
        if path == "/identity":
            return httpx.Response(200, json={"ok": True, **self.identity,
                                             **({"presence_delivery_version": 1} if self.reporting else {})})
        if path == "/presence/delivery":
            self.reports.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "recorded": True, "duplicate": False})
        if path == "/presence/turn":
            body = json.loads(request.content)
            source = body["event"]["source_event_id"]
            self.posts.append(source)
            self.bodies.append(body)
            if self.busy.get(source):
                status, payload = self.busy[source].pop(0)
                return httpx.Response(status, json=payload)
            if source not in self.answers:  # a retry replays; it never generates again
                self.generations.append(source)
                self.answers[source] = self.turns[source](body)
            status, payload = self.answers[source]
            return httpx.Response(status, json=payload)
        assert request.method == "GET", "email reports no transport queue"
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

    def receive(self, uid, *, sender="alice@example.org", thread=(), reply_to=(), subject="Question"):
        message = {"uid": uid, "message_id": f"<{uid}>", "sender": sender, "subject": subject,
                   "body": f"Body {uid}", "references": list(thread), "recipients": ["bot@example.org"]}
        if reply_to:
            message["reply_to"] = list(reply_to)
        return self.store.ingest(message)[1]

    def due(self):
        with sqlite3.connect(self.store.path) as db:
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

    def outbox(self):
        with sqlite3.connect(self.store.path) as db:
            return [row[0] for row in db.execute("SELECT request_id FROM outbox ORDER BY id")]

    async def send(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE outbox SET available_at=0")
        while self.runtime.process_outbox():
            with sqlite3.connect(self.store.path) as db:
                db.execute("UPDATE outbox SET available_at=0")
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE outbox SET report_due_at=CASE WHEN report_due_at>0 THEN 1 ELSE 0 END")
        while await self.runtime.process_delivery_report():
            pass

    def mail(self):
        return [{"to": str(message["To"]), "in_reply_to": str(message["In-Reply-To"] or ""),
                 "references": str(message["References"] or ""), "subject": str(message["Subject"]),
                 "body": message.get_content().strip()} for message in self.client.sent]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_USER", "bot@example.org")
    return tmp_path, FakeClient(FakeIMAP(1800000000))


@pytest.fixture
def clock(monkeypatch):
    """Store time only: claims, leases and retry availability use this logical clock."""
    value = SimpleNamespace(now=1800000000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: value.now))
    return value


# ---- negotiation ----------------------------------------------------------------------------

@pytest.mark.parametrize("identity,requested", [
    ({"presence_continuation_version": 1, "presence_continuation_versions": [1, 2]}, True),
    ({"presence_continuation_versions": [2]}, True),
    ({"presence_continuation_version": 1}, False),  # this transport never spoke version 1
    ({"presence_continuation_versions": [1]}, False),
    ({"presence_continuation_versions": ["2", True, 2.0]}, False),
    ({}, False),
])
def test_version_two_is_requested_only_when_listed(rig, identity, requested):
    async def run():
        root, client = rig
        host = V2Host(identity=identity)
        host.turns[FIRST] = lambda body: heard(body) if requested else (200, {
            "status": "completed", "outcome": "message", "text": "Plain answer", "turn_ref": "t"})
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            await bridge.send()
        finally:
            await bridge.close()
        assert host.body(FIRST).get("continuation_version") == (2 if requested else None)
        assert bridge.inbox() == {"<1>": "completed"}
        assert bridge.store.get("delivery_support")["presence_continuation_version"] == (2 if requested else 0)
        # An older Host keeps the plain turn and its legacy reply identity.
        assert bridge.outbox() == ([] if requested else [f"presence:{FIRST}:final"])

    asyncio.run(run())


# ---- startup, active input and anchors -------------------------------------------------------

@pytest.mark.parametrize("reporting", [True, False])
def test_custody_ack_frees_the_thread_and_replies_anchor_on_the_last_covered_mail(rig, reporting):
    async def run():
        root, client = rig
        host = V2Host(reporting=reporting)
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.turns[SECOND] = lambda body: heard(body, "turn-1")  # mail to the live author
        host.turns[NINTH] = lambda body: answered("turn-9", body, "Separate answer", "out-9", host.anchor(NINTH))
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1, sender="alice@example.org")
            bridge.receive(2, sender="bob@example.org", thread=("<1>",), reply_to=("Bob <bob+list@example.org>",),
                           subject="Re: Question")
            bridge.receive(9, sender="carol@example.org", subject="Other")
            assert await bridge.runtime.process_inbox()
            # Custody, not a reply: the submitting claim stored the reference and polled nothing.
            assert host.polls == [] and bridge.receipt()[0] == "continuing"
            assert bridge.receipt()[1]["continuation_version"] == 2
            assert (bridge.row()["state"], bridge.row()["last_error"]) == ("retry", "")
            assert await bridge.runtime.process_inbox()  # the thread's next mail reaches the author now
            assert await bridge.runtime.process_inbox()  # another thread
            assert host.posts == [FIRST, SECOND, NINTH] and host.polls == []
            host.work["turn-1"] = [view("turn-1", [output("out-1", "One answer for both", host.anchor(SECOND))]),
                                   view("turn-1", [output("out-1", "One answer for both", host.anchor(SECOND))],
                                        status="completed", outcome="silent", text="", output_ref="")]
            assert await bridge.runtime.process_inbox()  # the author's first poll
            bridge.due()
            assert await bridge.runtime.process_inbox()  # its terminal repeats the selection
            await bridge.send()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"<1>": "completed", "<2>": "completed", "<9>": "completed"}
        assert host.polls == ["turn-1", "turn-1"] and host.generations == [FIRST, SECOND, NINTH]
        assert all(body["continuation_version"] == 2 for body in host.bodies)
        assert bridge.outbox() == [f"presence:{NINTH}:output:out-9", f"presence:{FIRST}:output:out-1"]
        assert bridge.mail() == [
            {"to": "carol@example.org", "in_reply_to": "<9>", "references": "<9>", "subject": "Re: Other",
             "body": "Separate answer"},
            # Anchored on the covered later mail: its Reply-To, its Message-ID, same thread.
            {"to": "Bob <bob+list@example.org>", "in_reply_to": "<2>", "references": "<1> <2>",
             "subject": "Re: Question", "body": "One answer for both"},
        ]
        origins = [report["origin"] for report in host.reports]
        assert origins == ([{"kind": "automatic", "task_id": "turn-9", "source_event_id": NINTH},
                            {"kind": "automatic", "task_id": "turn-1", "source_event_id": FIRST}] if reporting else [])

    asyncio.run(run())


def test_new_mail_is_submitted_before_an_admitted_turn_is_polled_again(rig):
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        for source in (SECOND, THIRD, "INBOX:0:4:<4>"):
            host.turns[source] = lambda body: heard(body, "turn-1")
        host.work["turn-1"] = [view("turn-1", [])]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            bridge.receive(2, thread=("<1>",))
            bridge.receive(3, sender="dave@example.org")
            bridge.receive(4, thread=("<1>",))
            for _ in range(3):
                assert await bridge.runtime.process_inbox()
            assert host.polls == []  # the due author poll waited behind every arrival
            assert await bridge.runtime.process_inbox()
            assert not await bridge.runtime.process_inbox()  # pending author: next pass after 5 seconds
        finally:
            await bridge.close()
        assert host.posts == [FIRST, SECOND, THIRD, "INBOX:0:4:<4>"] and host.polls == ["turn-1"]
        assert bridge.inbox() == {"<1>": "retry", "<2>": "completed", "<3>": "completed", "<4>": "completed"}

    asyncio.run(run())


def test_flood_into_a_live_author_is_each_submitted_once_in_order_and_answered_once(rig):
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        flood = list(range(2, 14))
        for uid in flood:
            host.turns[f"INBOX:0:{uid}:<{uid}>"] = lambda body: heard(body, "turn-1")
        host.work["turn-1"] = [view("turn-1", [])]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            for uid in flood:
                bridge.receive(uid, sender=f"writer{uid}@example.org", thread=("<1>",), subject="Re: Question")
            for _ in flood:
                assert await bridge.runtime.process_inbox()
            assert host.polls == []  # every arrival went first; nothing was merged or dropped
            last = host.anchor(f"INBOX:0:{flood[-1]}:<{flood[-1]}>")
            host.work["turn-1"] = [view("turn-1", [], status="completed", outcome="message", text="One reply",
                                        output_ref="out-1", reply_anchor=last)]
            assert await bridge.runtime.process_inbox()
            await bridge.send()
        finally:
            await bridge.close()
        assert host.posts == [FIRST, *[f"INBOX:0:{uid}:<{uid}>" for uid in flood]]
        assert host.generations == host.posts and host.polls == ["turn-1"]
        assert set(bridge.inbox().values()) == {"completed"}
        assert [(mail["body"], mail["to"], mail["in_reply_to"]) for mail in bridge.mail()] == [
            ("One reply", f"writer{flood[-1]}@example.org", f"<{flood[-1]}>")]

    asyncio.run(run())


# ---- saturation, busy hints and order --------------------------------------------------------

@pytest.mark.parametrize("field,delays", [
    ("capacity", [5, 5, 5, 8, 16, 30, 30]),
    ("conversation", [1, 2, 4, 8, 16, 30, 30]),
])
def test_capacity_retry_keeps_the_same_head_and_serves_other_threads(rig, clock, field, delays):
    async def run():
        root, client = rig
        host = V2Host()
        host.busy[FIRST] = [busy(field) for _ in delays]
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.turns[SECOND] = lambda body: heard(body, "turn-1")
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            bridge.receive(2, thread=("<1>",))
            for index, delay in enumerate(delays):
                uid = 100 + index
                host.turns[f"INBOX:0:{uid}:<{uid}>"] = lambda body: heard(body)
                bridge.receive(uid, sender="other@example.org")
                assert await bridge.runtime.process_inbox()  # the head is refused; the worker is free
                row = bridge.row()
                assert (row["state"], row["host_reference"]) == ("retry", "")
                assert row["available_at"] == clock.now + delay
                assert "presence_capacity_full" in row["last_error"] and field in row["last_error"]
                assert await bridge.runtime.process_inbox()  # another thread is served
                assert bridge.inbox()[f"<{uid}>"] == "completed"
                assert not await bridge.runtime.process_inbox()  # <2> never overtakes its head
                assert bridge.row("<2>")["attempts"] == 0
                clock.now += delay
            assert await bridge.runtime.process_inbox()  # admitted: custody ACK
            assert await bridge.runtime.process_inbox()  # then the thread's next mail
        finally:
            await bridge.close()
        assert bridge.receipt()[0] == "continuing" and bridge.inbox()["<2>"] == "completed"
        heads = [body for body in host.bodies if body["event"]["source_event_id"] == FIRST]
        assert len(heads) == len(delays) + 1 and all(body == heads[0] for body in heads)
        assert [source for source in host.posts if source in (FIRST, SECOND)] == [FIRST] * (len(delays) + 1) + [SECOND]
        assert host.polls == [] and client.sent == []

    asyncio.run(run())


@pytest.mark.parametrize("hint,delays", [
    (45, [45, 45, 45, 45, 45, 45, 45]),  # a Host hint above the backoff cap is never cut
    (12.5, [12.5, 12.5, 12.5, 12.5, 16, 30, 30]),
    (0, [1, 2, 4, 8, 16, 30, 30]),  # a zero hint leaves the transport's own backoff
])
def test_capacity_hint_is_a_floor_and_only_the_own_backoff_is_capped(rig, clock, hint, delays):
    async def run():
        root, client = rig
        host = V2Host()
        status, body = busy("capacity")
        host.busy[FIRST] = [(status, {**body, "retry_after": hint}) for _ in delays]
        host.turns[FIRST] = lambda request: ack("turn-1", request)
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            observed = []
            for _ in delays:
                assert await bridge.runtime.process_inbox()
                observed.append(bridge.row()["available_at"] - clock.now)
                clock.now = bridge.row()["available_at"]
            assert await bridge.runtime.process_inbox()
        finally:
            await bridge.close()
        assert observed == delays and bridge.receipt()[0] == "continuing"

    asyncio.run(run())


# ---- the running companion: input and observation loops ------------------------------------

async def _until(predicate, task, timeout=10.0):
    async def wait():
        while not predicate():
            if task.done():
                task.result()  # surface a crashed runtime instead of timing out
                raise AssertionError("EmailRuntime.run ended early")
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


def _unsubmitted(store):
    with sqlite3.connect(store.path) as db:
        return db.execute("SELECT COUNT(*) FROM inbox WHERE host_reference='' "
                          "AND state IN ('pending','retry','processing')").fetchone()[0]


def test_slow_author_poll_never_holds_new_mail_in_the_running_companion(rig):
    """EmailRuntime.run: the author's GET is held; both threads' new mail still reaches Host."""
    async def run():
        root, client = rig
        gate, entered = asyncio.Event(), asyncio.Event()

        class SlowPollHost(V2Host):
            async def handle(self, request):
                if request.method == "GET" and request.url.path == "/presence/work/turn-1":
                    entered.set()
                    await gate.wait()
                return super().handle(request)

        host = SlowPollHost()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.turns[SECOND] = lambda body: heard(body, "turn-1")
        host.turns[THIRD] = lambda body: answered("turn-3", body, "Other thread answer", "out-3", host.anchor(THIRD))
        bridge = Bridge(root, host, client)
        bridge.runtime.poll = lambda: 0
        stop, task = asyncio.Event(), None
        try:
            bridge.receive(1, sender="alice@example.org")
            task = asyncio.create_task(bridge.runtime.run(stop))
            await asyncio.wait_for(entered.wait(), 10)  # the observation loop waits inside the author's GET
            bridge.receive(2, sender="bob@example.org", thread=("<1>",), subject="Re: Question")
            bridge.receive(3, sender="carol@example.org", subject="Other")
            await _until(lambda: bridge.inbox() == {"<1>": "processing", "<2>": "completed", "<3>": "completed"}
                         and len(client.sent) == 1, task)
            # Both threads' mail reached Host while the poll was still held; the poll
            # keeps its claim and lease on <1>, and nothing of <1>'s was sent yet.
            assert not gate.is_set() and host.posts == [FIRST, SECOND, THIRD] and host.polls == []
            assert bridge.row()["lease_token"] and bridge.row()["host_reference"].startswith("continuing:")
            host.work["turn-1"] = [view("turn-1", [], status="completed", outcome="message", text="Answer for both",
                                        output_ref="out-1", reply_anchor=host.anchor(SECOND))]
            gate.set()
            await _until(lambda: bridge.inbox()["<1>"] == "completed" and len(client.sent) == 2, task)
            await asyncio.sleep(1.2)  # one more pass of every loop: nothing is sent twice
        finally:
            stop.set()
            gate.set()
            if task is not None:
                await asyncio.wait_for(task, 10)  # all four loops end on stop
            await bridge.close()
        assert [(mail["body"], mail["to"], mail["in_reply_to"]) for mail in bridge.mail()] == [
            ("Other thread answer", "carol@example.org", "<3>"), ("Answer for both", "bob@example.org", "<2>")]
        assert host.polls == ["turn-1"] and host.posts == [FIRST, SECOND, THIRD]
        assert bridge.outbox() == [f"presence:{THIRD}:output:out-3", f"presence:{FIRST}:output:out-1"]

    asyncio.run(run())


def test_continuous_inflow_never_starves_output_observation(rig):
    """EmailRuntime.run: the author's reply goes out while newer mail is still queued."""
    async def run():
        root, client = rig
        stop, done, task, feeder = asyncio.Event(), asyncio.Event(), None, None

        class BusyHost(V2Host):
            async def handle(self, request):
                if (request.url.path == "/presence/turn" and not done.is_set()
                        and json.loads(request.content)["event"]["source_event_id"] != FIRST):
                    # While the inflow runs, mail arrives faster than it can be
                    # submitted, so input stays queued throughout.
                    await asyncio.sleep(0.1)
                return super().handle(request)

        host = BusyHost()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        bridge = Bridge(root, host, client)
        bridge.runtime.poll = lambda: 0
        backlog_at_send, received = [], []
        send = client.send_message

        def recording_send(message, **kwargs):
            backlog_at_send.append(_unsubmitted(bridge.store))
            return send(message, **kwargs)

        client.send_message = recording_send

        def arrive(uid):
            host.turns[f"INBOX:0:{uid}:<{uid}>"] = lambda body: heard(body)
            bridge.receive(uid, sender=f"writer{uid}@example.org", subject=f"Topic {uid}")
            received.append(uid)

        async def inflow():
            uid = 130
            while not done.is_set():
                arrive(uid)
                uid += 1
                await asyncio.sleep(0.005)

        try:
            bridge.receive(1)
            for uid in range(100, 130):  # a backlog already waits behind the author's mail
                arrive(uid)
            host.work["turn-1"] = [view("turn-1", [], status="completed", outcome="message", text="Answer",
                                        output_ref="out-1")]
            task = asyncio.create_task(bridge.runtime.run(stop))
            feeder = asyncio.create_task(inflow())
            await _until(lambda: len(client.sent) == 1, task)
            sent_after = len(received)
            await asyncio.sleep(0.2)
            assert len(received) > sent_after  # the inflow was still running after the reply left
            done.set()
            await feeder
            await _until(lambda: _unsubmitted(bridge.store) == 0, task, timeout=30)
        finally:
            done.set()
            stop.set()
            if feeder is not None:
                await asyncio.gather(feeder, return_exceptions=True)
            if task is not None:
                await asyncio.wait_for(task, 10)
            await bridge.close()
        # Observation progressed while input stayed queued, and no mail was dropped,
        # merged or submitted twice: each went to Host once, in arrival order.
        assert backlog_at_send[0] > 0
        assert [mail["body"] for mail in bridge.mail()] == ["Answer"] and host.polls == ["turn-1"]
        assert host.posts == [FIRST, *[f"INBOX:0:{uid}:<{uid}>" for uid in received]]
        assert set(bridge.inbox().values()) == {"completed"}

    asyncio.run(run())


# ---- deduplication, unknown sends and restart -----------------------------------------------

def test_output_ref_is_sent_once_across_polls_terminal_child_restart_and_unknown_smtp(rig):
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            await bridge.close()
            bridge.restart()  # a restart between the ACK and the first poll keeps the reference
            first, second = output("out-1", "First part", host.anchor(FIRST)), output("out-2", "Second part",
                                                                                     host.anchor(FIRST))
            host.work["turn-1"] = [
                view("turn-1", [first]),
                view("turn-1", [first, second], child="child-1"),
                view("turn-1", [first, second], status="completed", outcome="message", text="Second part",
                     output_ref="out-2", reply_anchor=host.anchor(FIRST), child_work_ref="child-1"),
            ]
            # A promoted child carries no anchor and here repeats an output identity.
            host.work["child-1"] = [child_view("child-1", status="pending"),
                                    child_view("child-1", "First part", output_ref="out-1")]
            bridge.due()
            assert await bridge.runtime.process_inbox()
            client.fail_data = True  # SMTP acceptance becomes unknown: never resent
            await bridge.send()
            client.fail_data = False
            bridge.due()
            assert await bridge.runtime.process_inbox()  # names the child: persisted before its poll
            assert bridge.receipt()[1]["work_refs"] == ["child-1"] and "child-1" not in host.polls
            await bridge.close()
            bridge.restart()
            for _ in range(2):
                bridge.due()
                assert await bridge.runtime.process_inbox()
            await bridge.send()
            bridge.due()
            assert not await bridge.runtime.process_inbox()
        finally:
            await bridge.close()
        assert [mail["body"] for mail in bridge.mail()] == ["First part", "Second part"]
        assert bridge.outbox() == [f"presence:{FIRST}:output:out-1", f"presence:{FIRST}:output:out-2"]
        assert bridge.store.receipt(f"presence:{FIRST}:output:out-1")["state"] == "uncertain"
        assert bridge.inbox() == {"<1>": "completed"} and host.posts == [FIRST]
        assert host.polls.count("child-1") == 2

    asyncio.run(run())


def test_lost_ack_reply_is_replayed_without_a_second_generation(rig):
    async def run():
        root, client = rig

        class LossyHost(V2Host):
            lost = False

            def handle(self, request):
                response = super().handle(request)
                if request.url.path == "/presence/turn" and not self.lost:
                    self.lost = True
                    raise httpx.ReadTimeout("ACK recorded, reply lost", request=request)
                return response

        host = LossyHost()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.work["turn-1"] = [view("turn-1", [], status="completed", outcome="message", text="Done",
                                    output_ref="out-1")]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            assert bridge.row()["host_reference"] == ""
            for _ in range(2):
                bridge.due()
                assert await bridge.runtime.process_inbox()
            await bridge.send()
        finally:
            await bridge.close()
        assert host.posts == [FIRST, FIRST] and host.generations == [FIRST] and host.bodies[0] == host.bodies[1]
        assert [mail["body"] for mail in bridge.mail()] == ["Done"] and bridge.inbox() == {"<1>": "completed"}
        assert bridge.mail()[0]["in_reply_to"] == "<1>"

    asyncio.run(run())


@pytest.mark.parametrize("stopped_as", ["cancelled", "retained"])
def test_stopped_author_speech_is_never_sent_and_the_mail_never_reposted(rig, stopped_as):
    async def run():
        root, client = rig
        host = V2Host()
        released = output("out-1", "Working on it")
        if stopped_as == "cancelled":
            host.turns[FIRST] = lambda body: ack("turn-1", body)
            host.work["turn-1"] = [view("turn-1", [released]),
                                   view("turn-1", [released], status="cancelled", outcome="silent", text="",
                                        output_ref="", code="presence_turn_cancelled")]
        else:
            host.turns[FIRST] = lambda body: (409, {
                "ok": False, "error": "presence_turn_cancelled: source_event_id", "code": "presence_turn_cancelled",
                "disposition": "retained", "field": "source_event_id", "turn_ref": "turn-1",
                "delivery_reporting_version": 1})
            host.work["turn-1"] = [(200, {"ok": True, "status": "cancelled", "outcome": "silent", "text": "",
                                          "output_ref": "", "work_ref": "turn-1", "code": "presence_turn_cancelled",
                                          "delivery_reporting_version": 1})]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            for _ in range(3 if stopped_as == "cancelled" else 1):
                bridge.due()
                assert await bridge.runtime.process_inbox()
                await bridge.send()
            assert not bridge.receive(1)  # IMAP redelivery is deduplicated
            await bridge.close()
            bridge.restart()
            bridge.due()
            assert not await bridge.runtime.process_inbox()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"<1>": "failed"} and "cancelled" in bridge.row()["last_error"]
        assert [mail["body"] for mail in bridge.mail()] == (["Working on it"] if stopped_as == "cancelled" else [])
        assert host.posts == [FIRST] and host.generations == [FIRST]

    asyncio.run(run())


# ---- late child and anchors ------------------------------------------------------------------

def test_late_child_replies_to_the_polled_mail_and_unknown_anchor_falls_back(rig):
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.turns[SECOND] = lambda body: heard(body, "turn-1")
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1, sender="alice@example.org")
            bridge.receive(2, sender="bob@example.org", thread=("<1>",))
            for _ in range(2):
                assert await bridge.runtime.process_inbox()
            stray = {"source_event_id": "INBOX:0:77:<never-received>", "message": {"message_id": "<never-received>"}}
            host.work["turn-1"] = [
                view("turn-1", [output("out-a", "To Bob", host.anchor(SECOND)), output("out-b", "Fallback", stray)],
                     child="child-late"),
                view("turn-1", [output("out-a", "To Bob", host.anchor(SECOND)), output("out-b", "Fallback", stray)],
                     child="child-late"),
            ]
            host.work["child-late"] = [child_view("child-late", "Child report", output_ref="out-child")]
            bridge.due()
            assert await bridge.runtime.process_inbox()
            await bridge.close()
            bridge.restart()  # the child comes back from SQLite
            bridge.due()
            assert await bridge.runtime.process_inbox()
            await bridge.send()
        finally:
            await bridge.close()
        assert [(mail["body"], mail["to"], mail["in_reply_to"]) for mail in bridge.mail()] == [
            ("To Bob", "bob@example.org", "<2>"),
            ("Fallback", "alice@example.org", "<1>"),
            ("Child report", "alice@example.org", "<1>"),
        ]
        assert bridge.inbox()["<1>"] == "retry" and host.posts == [FIRST, SECOND]

    asyncio.run(run())


def test_terminal_only_final_replies_to_the_mail_its_anchor_names(rig):
    """The current Host's v2 shape: no released rows, one anchored terminal output."""
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.turns[SECOND] = lambda body: heard(body, "turn-1")
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1, sender="alice@example.org")
            bridge.receive(2, sender="bob@example.org", thread=("<1>",), subject="Re: Question")
            for _ in range(2):
                assert await bridge.runtime.process_inbox()
            host.work["turn-1"] = [view("turn-1", []),
                                   view("turn-1", [], status="completed", outcome="message", text="Final answer",
                                        output_ref="out-final", reply_anchor=host.anchor(SECOND))]
            for _ in range(2):
                bridge.due()
                assert await bridge.runtime.process_inbox()
            await bridge.send()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"<1>": "completed", "<2>": "completed"}
        assert [(mail["body"], mail["to"], mail["in_reply_to"], mail["references"]) for mail in bridge.mail()] == [
            ("Final answer", "bob@example.org", "<2>", "<1> <2>")]
        assert bridge.outbox() == [f"presence:{FIRST}:output:out-final"]

    asyncio.run(run())


# A completed (synchronous) answer cannot cover <2>: FIFO captures it only after <1> is answered.
@pytest.mark.parametrize("channel,anchored", [
    *[(channel, anchored) for channel in ("released", "terminal")
      for anchored in ("other_thread", "forged_message", "other_message_id", "same_thread")],
    ("completed", "other_thread"), ("completed", "forged_message")])
def test_an_anchor_binds_only_the_exact_captured_mail_of_the_same_thread(rig, channel, anchored):
    """Carol's <3> is captured in another thread. An anchor naming it, or one whose message is
    not the captured mail its source names, falls back to the polled mail (alice's <1>)."""
    async def run():
        root, client = rig
        host = V2Host()

        def anchor():
            if anchored == "other_thread":
                return host.anchor(THIRD)
            if anchored == "forged_message":  # the message is a captured mail, but not <3>
                captured = FIRST if channel == "completed" else SECOND
                return {"source_event_id": THIRD, "message": host.anchor(captured)["message"]}
            if anchored == "other_message_id":
                return {"source_event_id": SECOND, "message": {**host.anchor(SECOND)["message"], "message_id": "<3>"}}
            return host.anchor(SECOND)

        host.turns[THIRD] = lambda body: heard(body, "turn-3")
        host.turns[FIRST] = (lambda body: answered("turn-1", body, "Answer", "out-1", anchor())) \
            if channel == "completed" else (lambda body: ack("turn-1", body))
        host.turns[SECOND] = lambda body: heard(body, "turn-1" if channel != "completed" else "turn-2")
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(3, sender="carol@example.org", subject="Other")
            bridge.receive(1, sender="alice@example.org")
            bridge.receive(2, sender="bob@example.org", thread=("<1>",), subject="Re: Question")
            for _ in range(3):
                assert await bridge.runtime.process_inbox()
            assert host.posts == [THIRD, FIRST, SECOND]
            if channel == "released":
                host.work["turn-1"] = [view("turn-1", [output("out-1", "Answer", anchor())], status="completed",
                                            outcome="silent", text="", output_ref="")]
            elif channel == "terminal":
                host.work["turn-1"] = [view("turn-1", [], status="completed", outcome="message", text="Answer",
                                            output_ref="out-1", reply_anchor=anchor())]
            bridge.due()
            while await bridge.runtime.process_inbox():
                bridge.due()
            await bridge.send()
        finally:
            await bridge.close()
        assert set(bridge.inbox().values()) == {"completed"}
        assert bridge.outbox() == [f"presence:{FIRST}:output:out-1"]
        expected = ("bob@example.org", "<2>", "<1> <2>") if anchored == "same_thread" \
            else ("alice@example.org", "<1>", "<1>")
        assert [(mail["body"], mail["to"], mail["in_reply_to"], mail["references"]) for mail in bridge.mail()] == [
            ("Answer", *expected)]

    asyncio.run(run())


def test_the_anchor_lookup_searches_the_unique_folder_uidvalidity_uid_index(rig, monkeypatch):
    """The anchor's own folder/uidvalidity/imap_uid name its captured mail; no inbox scan."""
    statements = []
    connect = EmailStore._connect

    @contextlib.contextmanager
    def traced(self):
        with connect(self) as db:
            db.set_trace_callback(statements.append)
            yield db

    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.turns[SECOND] = lambda body: heard(body, "turn-1")
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            bridge.receive(2, sender="bob@example.org", thread=("<1>",))
            for _ in range(2):
                assert await bridge.runtime.process_inbox()
            host.work["turn-1"] = [view("turn-1", [], status="completed", outcome="message", text="Answer",
                                        output_ref="out-1", reply_anchor=host.anchor(SECOND))]
            bridge.due()
            monkeypatch.setattr(EmailStore, "_connect", traced)
            assert await bridge.runtime.process_inbox()
            monkeypatch.setattr(EmailStore, "_connect", connect)
            await bridge.send()
        finally:
            await bridge.close()
        assert [(mail["to"], mail["in_reply_to"]) for mail in bridge.mail()] == [("bob@example.org", "<2>")]
        return bridge.store.path

    path = asyncio.run(run())
    lookups = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT * FROM INBOX WHERE")
               and "available_at" not in sql]  # the claim's own queue query is not the lookup
    assert lookups
    with sqlite3.connect(path) as db:
        plans = [" ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + sql, [None] * sql.count("?")))
                 for sql in lookups]
    assert all("USING INDEX" in plan and "SCAN" not in plan for plan in plans), plans


@pytest.mark.parametrize("shape", ["continuing", "completed_continuation", "completed"])
def test_an_initial_or_completed_answer_keeps_its_exact_reply_anchor(rig, shape):
    """Adapter boundary: the anchor the /presence/turn answer carries is the one every output reports,
    through the persisted reference, the network-free initial status and a later poll."""
    async def run():
        root, client = rig
        host = V2Host()

        def answer(body):
            exact = {"source_event_id": FIRST, "message": {**body["event"]["message"], "source_order": 7}}
            if shape == "completed":
                return answered("turn-1", body, "Answer", "out-1", exact)
            status, payload = ack("turn-1", body)
            return status, {**payload, "outcome": "message", "text": "Answer", "output_ref": "out-1",
                            "reply_anchor": exact, **({"status": "completed"} if shape != "continuing" else {})}

        host.turns[FIRST] = answer
        host.work["turn-1"] = [view("turn-1", [])]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            item = bridge.store.claim_inbox()
            ref = await bridge.adapter.submit(item)
            exact = host.answers[FIRST][1]["reply_anchor"]
            prefix, receipt = ref.split(":", 1)[0], json.loads(base64.urlsafe_b64decode(
                ref.split(":", 1)[1] + "=" * (-len(ref.split(":", 1)[1]) % 4)))
            assert prefix == ("completed" if shape == "completed" else "continuing") and receipt["anchor"] == exact
            assert exact["message"]["imap_uid"] == 1 and exact["message"]["source_order"] == 7
            views = [bridge.adapter.initial_status(ref), await bridge.adapter.status(ref)]
        finally:
            await bridge.close()
        for status in views:
            assert [(output.identity, output.anchor) for output in status.outputs] == [("output:out-1", exact)]
        assert host.polls == ([] if shape == "completed" else ["turn-1"])

    asyncio.run(run())


def test_a_repeated_output_with_other_headers_keeps_the_first_and_progresses(rig):
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.turns[SECOND] = lambda body: heard(body, "turn-1")
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            bridge.receive(2, sender="bob@example.org", thread=("<1>",))
            for _ in range(2):
                assert await bridge.runtime.process_inbox()
            host.work["turn-1"] = [view("turn-1", [output("out-1", "Answer", host.anchor(FIRST))]),
                                   view("turn-1", [output("out-1", "Answer", host.anchor(SECOND))],
                                        status="completed", outcome="silent", text="", output_ref="")]
            for _ in range(2):
                bridge.due()
                assert await bridge.runtime.process_inbox()
            await bridge.send()
        finally:
            await bridge.close()
        assert bridge.inbox()["<1>"] == "completed"
        assert [(mail["body"], mail["in_reply_to"]) for mail in bridge.mail()] == [("Answer", "<1>")]

    asyncio.run(run())


# ---- version echoes ----------------------------------------------------------------------------

def test_an_ack_without_version_two_or_reference_is_not_custody(rig):
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: (200, {**ack("turn-1", body)[1], "continuation_version": 1})
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            bridge.receive(2, thread=("<1>",))
            assert await bridge.runtime.process_inbox()
            host.answers[FIRST] = (200, {**ack("turn-1", {})[1], "continuation_ref": ""})
            bridge.due()
            assert await bridge.runtime.process_inbox()
            bridge.due()
            assert await bridge.runtime.process_inbox()  # still the same head; <2> waits
        finally:
            await bridge.close()
        assert bridge.row()["host_reference"] == "" and bridge.inbox() == {"<1>": "retry", "<2>": "pending"}
        assert host.posts == [FIRST, FIRST, FIRST] and host.polls == []

    asyncio.run(run())


def test_a_v2_poll_answered_in_another_version_keeps_custody(rig):
    async def run():
        root, client = rig
        host = V2Host()
        host.turns[FIRST] = lambda body: ack("turn-1", body)
        host.work["turn-1"] = [view("turn-1", [output("out-1", "Early")], version=1)]
        bridge = Bridge(root, host, client)
        try:
            bridge.receive(1)
            assert await bridge.runtime.process_inbox()
            assert await bridge.runtime.process_inbox()
        finally:
            await bridge.close()
        assert bridge.inbox() == {"<1>": "retry"} and bridge.outbox() == []
        assert "without continuation_version 2" in bridge.row()["last_error"]
        assert bridge.receipt()[0] == "continuing"

    asyncio.run(run())


# ---- observation order ----------------------------------------------------------------------

@pytest.mark.parametrize("role", ["observe", None])
def test_observation_serves_due_work_first_so_two_slow_pending_authors_never_starve_a_finished_one(
        rig, clock, role):
    """Each read outlasts the 5 second pending pace: older references must not take every turn."""
    async def run():
        root, client = rig

        class SlowReadHost(V2Host):
            def handle(self, request):
                if request.method == "GET" and request.url.path.startswith("/presence/work/"):
                    clock.now += 6
                return super().handle(request)

        host = SlowReadHost()
        for uid in (1, 2, 3):
            host.turns[f"INBOX:0:{uid}:<{uid}>"] = lambda body, uid=uid: ack(f"turn-{uid}", body)
        host.work["turn-1"] = [view("turn-1", [])]
        host.work["turn-2"] = [view("turn-2", [])]
        host.work["turn-3"] = [view("turn-3", [], status="completed", outcome="message", text="Third answer",
                                    output_ref="out-3")]
        bridge = Bridge(root, host, client)
        try:
            for uid in (1, 2, 3):
                bridge.receive(uid, sender=f"writer{uid}@example.org", subject=f"Topic {uid}")
            for _ in range(3):  # each thread's author takes custody, in arrival order
                assert await bridge.runtime.process_inbox("intake")
                clock.now += 1
            for _ in range(6):  # the single observation loop, one claim at a time
                if not await bridge.runtime.process_inbox(role):
                    clock.now += 0.5
            await bridge.send()
        finally:
            await bridge.close()
        # Older pending work keeps being observed, but due order gives the finished one its turn.
        assert host.polls[:5] == ["turn-1", "turn-2", "turn-3", "turn-1", "turn-2"]
        assert bridge.inbox()["<3>"] == "completed"
        assert [(mail["body"], mail["in_reply_to"]) for mail in bridge.mail()] == [("Third answer", "<3>")]

    asyncio.run(run())


def _queued(root, clock):
    """Four threads: <1>/<2> new mail, <3>/<4> admitted work; each pair's older row is due later."""
    store = EmailStore(root)
    for uid in (1, 2, 3, 4):
        store.ingest({"uid": uid, "message_id": f"<{uid}>", "sender": f"w{uid}@example.org", "subject": "s",
                      "body": "b", "references": [], "recipients": ["bot@example.org"]})
    with sqlite3.connect(store.path) as db:
        db.executemany("UPDATE inbox SET host_reference=?, state=?, available_at=? WHERE message_id=?", [
            ("", "retry", clock.now - 1, "<1>"), ("", "pending", clock.now - 5, "<2>"),
            ("continuing:a", "retry", clock.now - 1, "<3>"), ("continuing:b", "retry", clock.now - 5, "<4>")])
    return store


@pytest.mark.parametrize("submitted,expected", [
    (False, ["<1>", "<2>"]),  # new mail keeps capture order, whatever its retry time
    (True, ["<4>", "<3>"]),  # admitted work: earliest due first
    (None, ["<1>", "<2>", "<4>", "<3>"]),  # no role: new mail first, then admitted work by due time
])
def test_each_claim_role_keeps_its_own_order(tmp_path, clock, submitted, expected):
    store = _queued(tmp_path, clock)
    claimed = []
    while (item := store.claim_inbox(submitted=submitted)) is not None:
        claimed.append(item.message_id)
    assert claimed == expected


def test_a_new_mail_claim_walks_capture_order_without_sorting_the_queue(tmp_path, clock, monkeypatch):
    """Intake needs no sort: thousands of waiting threads must not be ordered for one claim."""
    statements = []
    connect = EmailStore._connect

    @contextlib.contextmanager
    def traced(self):
        with connect(self) as db:
            db.set_trace_callback(statements.append)
            yield db

    store = _queued(tmp_path, clock)
    monkeypatch.setattr(EmailStore, "_connect", traced)
    assert store.claim_inbox(submitted=False).message_id == "<1>"
    monkeypatch.setattr(EmailStore, "_connect", connect)
    claims = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT * FROM INBOX WHERE")]
    assert len(claims) == 1
    with sqlite3.connect(store.path) as db:
        plan = " ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + claims[0]))
    assert "TEMP B-TREE" not in plan, plan
