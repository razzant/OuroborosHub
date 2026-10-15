"""Retained Host refusals through the real client, urllib transport, runtime and store.

A scripted loopback HTTP Host speaks the Presence wire (typed refusal envelopes and
``/presence/work`` views). It stores one answer per source event and replays it on
retry, so ``generations`` counts real model work. Telegram itself is a fake client.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from telegram_bot.custody import CustodyStore
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostClient
from telegram_bot.runtime import TelegramTransportRuntime
from test_runtime import Logger, RuntimeClient

BINDING = "b" * 32
TOKEN = "skill-token"


def retained(turn, *, child="", code="presence_attempt_outcome_unknown", mode=1):
    return 409, {"ok": False, "error": f"{code}: source_event_id", "code": code, "disposition": "retained",
                 "field": "source_event_id", "turn_ref": turn, "delivery_reporting_version": mode,
                 **({"work_ref": child} if child else {})}


def unresolved(turn, *, child="", code="presence_attempt_outcome_unknown"):
    # The draft core names the admitted child as work_ref in this view.
    return 200, {"ok": True, "status": "unresolved", "disposition": "retained", "code": code,
                 "turn_ref": turn, "delivery_reporting_version": 1, **({"work_ref": child} if child else {})}


def done(ref, text="", *, status="completed", outcome="message"):
    return 200, {"ok": True, "status": status, "outcome": outcome, "text": text, "work_ref": ref,
                 "delivery_reporting_version": 1}


def pending(ref):
    return 202, {"ok": True, "status": "pending", "work_ref": ref, "delivery_reporting_version": 1}


def answered(turn, text, *, outcome="message", work_ref=""):
    return 200, {"ok": True, "status": "completed", "outcome": outcome, "text": text, "turn_ref": turn,
                 "work_ref": work_ref, "delivery_reporting_version": 1}


class Host:
    """One durable answer per source event; scripted polls keep their last answer."""

    def __init__(self):
        self.turns, self.answers, self.work = {}, {}, {}
        self.posts, self.generations, self.polls, self.reports = [], [], [], []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                assert self.headers["X-Skill-Token"] == TOKEN
                path, _, query = self.path.partition("?")
                if path == "/identity":
                    return self._reply(200, {"ok": True, "presence_delivery_version": 1})
                ref = urllib.parse.unquote(path.removeprefix("/presence/work/"))
                assert urllib.parse.parse_qs(query)["binding_id"] == [BINDING]
                host.polls.append(ref)
                queue = host.work[ref]
                self._reply(*(queue.pop(0) if len(queue) > 1 else queue[0]))

            def do_POST(self):
                assert self.headers["X-Skill-Token"] == TOKEN
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/presence/delivery":
                    host.reports.append(body)
                    return self._reply(200, {"ok": True, "recorded": True, "duplicate": False})
                assert self.path == "/presence/turn" and body["binding_id"] == BINDING
                source = body["event"]["source_event_id"]
                host.posts.append(source)
                if source not in host.answers:  # a retry replays; it never generates again
                    host.generations.append(source)
                    host.answers[source] = host.turns[source]
                self._reply(*host.answers[source])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def host():
    value = Host()
    yield value
    value.close()


class Bridge:
    """The production runtime over one state directory, rebuilt to model a restart."""

    def __init__(self, root, host):
        (root / "settings.json").write_text(json.dumps({"binding_id": BINDING}), encoding="utf-8")
        self.root, self.host, self.provider = root, host, RuntimeClient("token")
        self.restart()

    def restart(self):
        client = PresenceHostClient(state_dir=self.root, token_provider=lambda: TOKEN, host_base=self.host.base)
        self.runtime = TelegramTransportRuntime(state_dir=self.root, token_provider=lambda: "token", logger=Logger(),
                                                submitter=client, client_factory=lambda _token: self.provider)
        self.store = self.runtime.store
        self.store.release_own_leases()

    def receive(self, update_id, text="hello", chat=4):
        event = parse_telegram_update({"update_id": update_id, "message": {
            "message_id": 100 + update_id, "text": text, "from": {"id": 3}, "chat": {"id": chat, "type": "private"},
        }}, bot_account_id="9")
        return self.store.commit_update(update_id, event)

    def due(self):
        with sqlite3.connect(self.store.path) as conn:
            for table in ("inbox", "presence_work", "outbox"):
                conn.execute(f"UPDATE {table} SET available_at=0")

    async def inbox(self):
        self.due()
        # One physical submission per queued fixture event. A retained-refusal
        # regression must fail here, not spin by making its retry due forever.
        for _ in self.rows("SELECT event_id FROM inbox WHERE state='pending'"):
            assert await self.runtime.process_one_inbox()
        assert not await self.runtime.process_one_inbox()

    async def poll(self):
        self.due()
        worked = await self.runtime.process_one_work()
        while await self.runtime.process_one_work():
            pass
        return worked

    async def send(self):
        while True:
            self.due()
            if not await self.runtime.process_one_outbox():
                break
        while await self.runtime.process_one_delivery_report():
            self.reports_due()

    def reports_due(self):
        with sqlite3.connect(self.store.path) as conn:
            for delivery_id, raw in conn.execute("SELECT delivery_id, reports_json FROM outbox").fetchall():
                entries = json.loads(raw)
                for entry in entries:
                    entry["available_at"] = 0
                conn.execute("UPDATE outbox SET reports_json=?, report_due_at=CASE WHEN report_due_at>0 THEN 1 "
                             "ELSE 0 END WHERE delivery_id=?", (json.dumps(entries), delivery_id))

    def rows(self, sql, *args):
        with sqlite3.connect(self.store.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, args)]

    def work(self):
        return {row["work_ref"]: (row["role"], row["state"]) for row in self.rows(
            "SELECT work_ref, role, state FROM presence_work ORDER BY rowid")}

    def receipt(self, event_id):
        raw = self.rows("SELECT host_receipt_json FROM inbox WHERE event_id=?", event_id)[0]["host_receipt_json"]
        return json.loads(raw) if raw else None

    def outbox(self):
        return [(row["delivery_id"], json.loads(row["payload_json"])) for row in self.rows(
            "SELECT delivery_id, payload_json FROM outbox ORDER BY rowid")]

    def sent(self):
        return [item[2] for item in self.provider.sent]


@pytest.mark.parametrize("late_status,late_outcome", [("completed", "message"), ("failed", "deferred")])
def test_retained_event_releases_its_chat_and_late_child_and_parent_each_speak_once(tmp_path, host, late_status, late_outcome):
    async def run():
        host.turns["telegram:9:1"] = retained("turn-1", child="child-1")
        host.turns["telegram:9:2"] = answered("turn-2", "Second answer")
        host.work["turn-1"] = [unresolved("turn-1", child="child-1")]
        host.work["child-1"] = [pending("child-1"), done("child-1", "Child answer")]
        bridge = Bridge(tmp_path, host)
        assert bridge.receive(1, "What is the weather?") and bridge.receive(2, "Still there?")
        await bridge.inbox()
        # The retained event left the FIFO without a second generation; the next one ran.
        assert host.posts == host.generations == ["telegram:9:1", "telegram:9:2"]
        assert bridge.store.status_snapshot()["inbox_submitted"] == 2
        assert bridge.store.status_snapshot()["inbox_retained"] == 1
        receipt = {"http_status": 409, "response": host.answers["telegram:9:1"][1]}
        assert bridge.receipt("telegram:9:1") == receipt and bridge.receipt("telegram:9:2") is None
        assert bridge.work() == {"turn-1": ("turn", "pending"), "child-1": ("child", "pending")}
        await bridge.send()
        assert bridge.sent() == ["Second answer"]

        assert await bridge.poll()
        assert bridge.work() == {"turn-1": ("turn", "pending"), "child-1": ("child", "pending")}
        unresolved_row = bridge.rows("SELECT last_error FROM presence_work WHERE work_ref='turn-1'")[0]
        assert unresolved_row["last_error"] == "presence_work_unresolved: presence_attempt_outcome_unknown"
        bridge.restart()
        assert await bridge.poll()
        await bridge.send()
        # The child finished; the parent stays unresolved and its receipt is intact.
        assert bridge.sent() == ["Second answer", "Child answer"]
        assert bridge.work() == {"turn-1": ("turn", "pending"), "child-1": ("child", "completed")}
        assert bridge.receipt("telegram:9:1") == receipt

        host.work["turn-1"] = [done("turn-1", "Late original answer", status=late_status, outcome=late_outcome)]
        bridge.restart()
        assert await bridge.poll()
        for _ in range(3):  # later polls and a provider redelivery change nothing
            assert not await bridge.poll()
        assert not bridge.receive(1, "What is the weather?")
        await bridge.inbox()
        await bridge.send()
        assert bridge.sent() == ["Second answer", "Child answer", "Late original answer"]
        assert bridge.store.status_snapshot()["inbox_retained"] == 0
        assert host.posts == ["telegram:9:1", "telegram:9:2"]
        assert host.polls.count("child-1") == 2 and host.polls.count("turn-1") == 3
        assert bridge.receipt("telegram:9:1") == receipt
        outbox = dict(bridge.outbox())
        assert set(outbox) == {"presence:telegram:9:2:turn", "presence:telegram:9:1:child:child-1",
                               "presence:telegram:9:1:turn:turn-1"}
        for delivery_id, task in (("presence:telegram:9:1:child:child-1", "child-1"),
                                  ("presence:telegram:9:1:turn:turn-1", "turn-1")):
            # Source-bound: the original chat and message, attributed to the producing task.
            assert outbox[delivery_id]["chat_id"] == "4" and outbox[delivery_id]["reply_to_message_id"] == 101
            assert outbox[delivery_id]["_reporting"]["origin"] == {
                "kind": "automatic", "task_id": task, "source_event_id": "telegram:9:1"}
        assert [(report["origin"].get("task_id"), report["text"]) for report in host.reports] == [
            ("turn-2", "Second answer"), ("child-1", "Child answer"), ("turn-1", "Late original answer")]

    asyncio.run(run())


def test_unresolved_original_names_its_child_later_and_failed_original_never_speaks(tmp_path, host):
    async def run():
        host.turns["telegram:9:1"] = retained("turn-1", code="presence_turn_cancelled")
        host.work["turn-1"] = [unresolved("turn-1", code="presence_turn_cancelled"),
                               unresolved("turn-1", child="child-late", code="presence_turn_cancelled"),
                               done("turn-1", status="cancelled", outcome="silent")]
        host.work["child-late"] = [done("child-late", "Work that was already admitted")]
        bridge = Bridge(tmp_path, host)
        bridge.receive(1)
        await bridge.inbox()
        assert bridge.work() == {"turn-1": ("turn", "pending")}
        assert await bridge.poll()
        assert bridge.work() == {"turn-1": ("turn", "pending")}
        assert await bridge.poll()
        assert bridge.work() == {"turn-1": ("turn", "pending"), "child-late": ("child", "completed")}
        assert await bridge.poll()
        await bridge.send()
        assert bridge.work() == {"turn-1": ("turn", "cancelled"), "child-late": ("child", "completed")}
        assert bridge.sent() == ["Work that was already admitted"]
        assert bridge.receipt("telegram:9:1")["response"]["code"] == "presence_turn_cancelled"
        assert host.posts == ["telegram:9:1"] and host.polls.count("child-late") == 1

    asyncio.run(run())


def test_crash_before_receipt_resubmits_same_event_and_host_replays_without_generation(tmp_path, host, monkeypatch):
    async def run():
        host.turns["telegram:9:1"] = retained("turn-1", child="child-1")
        host.work["turn-1"] = [unresolved("turn-1", child="child-1")]
        host.work["child-1"] = [pending("child-1")]
        bridge = Bridge(tmp_path, host)
        bridge.receive(1)
        original = bridge.store.record_retention

        def crash(*_args, **_kwargs):
            raise OSError("disk unavailable before the receipt commit")

        monkeypatch.setattr(bridge.store, "record_retention", crash)
        assert await bridge.runtime.process_one_inbox()
        assert bridge.store.status_snapshot()["inbox_waiting"] == 1 and bridge.work() == {}
        monkeypatch.setattr(bridge.store, "record_retention", original)
        bridge.restart()
        await bridge.inbox()
        await bridge.poll()
        assert host.posts == ["telegram:9:1", "telegram:9:1"] and host.generations == ["telegram:9:1"]
        assert bridge.work() == {"turn-1": ("turn", "pending"), "child-1": ("child", "pending")}
        assert bridge.receipt("telegram:9:1")["response"]["turn_ref"] == "turn-1"

    asyncio.run(run())


@pytest.mark.parametrize("answer,terminal", [
    ((409, {"ok": False, "code": "presence_event_identity_conflict", "disposition": "rejected",
            "turn_ref": "turn-1", "error": "conflict"}), True),
    ((403, {"ok": False, "code": "presence_auth_blocked", "disposition": "blocked", "error": "stale"}), False),
    ((409, {"ok": False, "code": "presence_start_unwritable", "disposition": "retry", "turn_ref": "turn-1"}), False),
    ((404, {"detail": "Not Found"}), False),
    ((409, {"ok": False, "code": "presence_attempt_outcome_unknown", "disposition": "retained"}), False),
])
def test_typed_dispositions_over_real_http(tmp_path, host, answer, terminal):
    async def run():
        host.turns["telegram:9:1"] = answer
        host.turns["telegram:9:2"] = answered("turn-2", "Second answer")
        bridge = Bridge(tmp_path, host)
        bridge.receive(1)
        bridge.receive(2)
        for _ in range(2):
            bridge.due()
            assert await bridge.runtime.process_one_inbox()
        if terminal:  # poison input: failed with its receipt, the chat moves on
            assert bridge.store.status_snapshot()["inbox_failed"] == 1
            assert bridge.receipt("telegram:9:1") == {"http_status": answer[0], "response": answer[1]}
            assert host.posts == ["telegram:9:1", "telegram:9:2"]
        else:  # repair or retry first: the event stays first in its chat, never dropped
            assert bridge.store.status_snapshot()["inbox_waiting"] == 2
            assert host.posts == ["telegram:9:1", "telegram:9:1"] and bridge.receipt("telegram:9:1") is None
            host.answers["telegram:9:1"] = answered("turn-1", "After repair")
            await bridge.inbox()
            await bridge.send()
            assert bridge.sent() == ["After repair", "Second answer"]
        assert bridge.work() == {}

    asyncio.run(run())


def test_normal_message_silent_and_deferred_outcomes_over_real_http(tmp_path, host):
    async def run():
        host.turns["telegram:9:1"] = answered("turn-1", "Immediate")
        host.turns["telegram:9:2"] = answered("turn-2", "", outcome="silent")
        host.turns["telegram:9:3"] = answered("turn-3", "Working on it", outcome="deferred", work_ref="work-3")
        host.work["work-3"] = [pending("work-3"), done("work-3", "Deferred result")]
        bridge = Bridge(tmp_path, host)
        for update_id in (1, 2, 3):
            bridge.receive(update_id)
        await bridge.inbox()
        assert await bridge.poll() and await bridge.poll()
        await bridge.send()
        assert bridge.sent() == ["Immediate", "Working on it", "Deferred result"]
        assert bridge.work() == {"work-3": ("deferred", "completed")}
        assert "presence:telegram:9:3:work" in dict(bridge.outbox())
        assert bridge.store.status_snapshot()["inbox_retained"] == 0

    asyncio.run(run())


def test_old_work_table_moves_to_reference_keys_without_losing_custody(tmp_path):
    path = tmp_path / "custody.sqlite3"
    event = parse_telegram_update({"update_id": 7, "message": {
        "message_id": 8, "text": "hi", "from": {"id": 3}, "chat": {"id": 4, "type": "private"}}}, bot_account_id="9")
    payload = json.dumps({**event.to_dict(), "_reporting": {"version": 1, "account_id": "9",
                          "origin": {"kind": "automatic", "task_id": "turn-7", "source_event_id": event.source_event_id}}})
    with sqlite3.connect(path) as conn:  # the 0.4.3 layout, with an in-flight deferred handoff
        conn.executescript("""
            CREATE TABLE inbox(event_id TEXT PRIMARY KEY, update_id INTEGER NOT NULL UNIQUE,
                conversation_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','leased','submitted','failed')),
                attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0, received_at REAL NOT NULL, submitted_at REAL,
                failed_at REAL, submission_ref TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '');
            CREATE TABLE presence_work(event_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL,
                work_ref TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','leased','completed','failed','cancelled')),
                attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, completed_at REAL,
                last_error TEXT NOT NULL DEFAULT '', FOREIGN KEY(event_id) REFERENCES inbox(event_id));
            CREATE INDEX presence_work_ready_idx ON presence_work(state, available_at, lease_until, created_at);
        """)
        conn.execute("INSERT INTO inbox VALUES(?, 7, ?, ?, 'submitted', 1, 0, 0, 1, 1, NULL, 'turn-7', '')",
                     (event.source_event_id, event.conversation_key, json.dumps(event.to_dict())))
        conn.execute("INSERT INTO presence_work VALUES(?, ?, 'work-7', ?, 'pending', 3, 0, 0, 1, NULL, 'pending')",
                     (event.source_event_id, BINDING, payload))
    store = CustodyStore(path)
    CustodyStore(path)  # a second opening is a no-op
    work = store.claim_work()
    assert (work.work_ref, work.event_id, work.role, work.attempts) == ("work-7", event.source_event_id, "deferred", 4)
    store.complete_work(work.work_ref, status="completed", text="Finished after upgrade")
    delivery = store.claim_outbox()
    assert delivery.delivery_id == f"presence:{event.source_event_id}:work"
    assert delivery.payload["_reporting"]["origin"]["task_id"] == "turn-7"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT host_receipt_json FROM inbox").fetchone()[0] == ""
        columns = [row[1] for row in conn.execute("PRAGMA table_info(presence_work)")]
        primary = [row[1] for row in conn.execute("PRAGMA table_info(presence_work)") if row[5]]
    assert "role" in columns and primary == ["work_ref"]


def test_selected_output_survives_unresolved_author_and_repeated_terminal(tmp_path, host):
    async def run():
        host.turns["telegram:9:1"] = retained("turn-1")
        selected = {"output_ref": "selection-1", "outcome": "message", "text": "Selected answer"}
        status, body = unresolved("turn-1")
        host.work["turn-1"] = [(status, {**body, "outputs": [selected]})]
        bridge = Bridge(tmp_path, host)
        bridge.receive(1)
        await bridge.inbox()
        assert await bridge.poll()
        await bridge.send()
        assert bridge.sent() == ["Selected answer"]
        bridge.restart()
        assert await bridge.poll()
        await bridge.send()
        status, body = done("turn-1", "Selected answer", status="failed", outcome="deferred")
        host.work["turn-1"] = [(status, {**body, "output_ref": "selection-1"})]
        assert await bridge.poll()
        await bridge.send()
        assert bridge.sent() == ["Selected answer"]
        assert bridge.work()["turn-1"] == ("turn", "failed")
        assert len(host.reports) == 1 and host.reports[0]["origin"]["task_id"] == "turn-1"
    asyncio.run(run())
