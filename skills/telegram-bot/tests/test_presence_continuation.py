"""Presence continuation version 2 through the real connector.

A loopback HTTP Host speaks the version-2 wire of the Host contract
(``presence_continuation_versions``, the early custody answer, the immediate busy
``presence_capacity_full`` retry and ``reply_anchor``) and keeps one author per
conversation the way that contract describes. ``PresenceHostClient`` with its urllib
transport, ``CustodyStore``, the runtime and its workers, and ``TelegramClient`` run
unchanged; only Telegram's HTTP side is a recorder that never leaves the process.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import json
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from telegram_bot.api import TelegramClient
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostClient, PresenceHostError, PresenceHostHTTPError
from telegram_bot.runtime import TelegramTransportRuntime
from test_runtime import Logger

TOKEN = "skill-token"
BOT = "presence_bot"
GROUP = -1001
WAKES = {"direct_message", "mention", "reply_to_me"}
V2_IDENTITY = {"ok": True, "presence_delivery_version": 1, "places_version": 1,
               "presence_continuation_version": 1, "presence_continuation_versions": [1, 2]}


def source(update_id):
    return f"telegram:9:{update_id}"


def message(update_id, text, *, chat=GROUP, user=3, mention=False, topic=None):
    """A Telegram update; a mention or a private chat is a wake fact, other group text background."""
    body = f"@{BOT} {text}" if mention else text
    item = {"message_id": 100 + update_id, "date": 1_700_000_000 + update_id, "text": body,
            "from": {"id": user, "is_bot": False, "first_name": f"User{user}"},
            "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup",
                     **({} if chat > 0 else {"title": "Room"})}}
    if mention:
        item["entities"] = [{"type": "mention", "offset": 0, "length": len(BOT) + 1}]
    if topic is not None:
        item.update(message_thread_id=topic, is_topic_message=True)
    return {"update_id": update_id, "message": item}


class Author:
    """A running author as the Host contract describes it, or a promoted child of one."""

    def __init__(self, ref, event, *, anchored=True):
        self.ref, self.event, self.anchored = ref, event, anchored
        self.intake = anchored    # a child takes no conversation mail
        self.delivered = []       # arrivals handed to it as context, in delivery order
        self.covered = 0          # how many of them its latest model call saw
        self.outputs = []         # released {output_ref, outcome, text[, reply_anchor]}
        self.terminal = None
        self.child = ""


class Host:
    """Loopback Presence Host: one stored answer per source event, replayed on retry."""

    def __init__(self, identity=None, *, slots=1):
        self.identity = dict(V2_IDENTITY if identity is None else identity)
        self.slots, self.conversation_hint, self.capacity_hint = slots, 1, 5
        self.lock = threading.RLock()
        self.answers, self.authors, self.live = {}, {}, {}
        self.legacy, self.override, self.inline = {}, {}, {}
        self.posts, self.busy, self.polls, self.reports = [], [], [], []
        self.losses, self.report_failures = [], 0
        self._refs, self._outputs = itertools.count(1), itertools.count(1)
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _answer(self, path, status, body):
                if host.lose(path):  # processed, but the answer never reaches the transport
                    self.close_connection = True
                    return
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                assert self.headers["X-Skill-Token"] == TOKEN
                path = self.path.partition("?")[0]
                if path == "/identity":
                    return self._answer(path, 200, host.identity)
                if path == "/presence/place":
                    return self._answer(path, 200, {"ok": True, "place_id": "place-1", "label": "Telegram",
                                                    "state": "active"})
                assert path.startswith("/presence/work/"), path
                ref = urllib.parse.unquote(path.removeprefix("/presence/work/"))
                self._answer("/presence/work", *host.work_view(ref))

            def do_POST(self):
                assert self.headers["X-Skill-Token"] == TOKEN
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/presence/delivery":
                    return self._answer(self.path, *host.report(body))
                assert self.path == "/presence/turn", self.path
                self._answer(self.path, *host.turn(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        with self.lock:
            for item in self.legacy.values():
                if item.get("gate"):
                    item["gate"].set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def lose(self, path):
        with self.lock:
            if path in self.losses:
                self.losses.remove(path)
                return True
            return False

    # ``/presence/turn`` in the contract's order: replay, current author, heard, busy, start.
    def turn(self, body):
        event = body["event"]
        event_id = event["source_event_id"]
        with self.lock:
            self.posts.append((event_id, copy.deepcopy(body)))
            if event_id in self.answers:
                return self.answers[event_id]
            if event_id in self.override:
                return self._store(event_id, self.override[event_id])
            if body.get("continuation_version") != 2:
                legacy = self.legacy.setdefault(event_id, {"text": "Legacy answer"})
            else:
                return self._v2(event_id, event)
        if legacy.get("gate") is not None:  # version 0 waits for the terminal
            assert legacy["gate"].wait(30), "the test never released the legacy turn"
        with self.lock:
            return self._store(event_id, (200, {
                "ok": True, "status": "completed", "outcome": "message", "text": legacy["text"],
                "turn_ref": f"legacy-{event_id}", "work_ref": "", "delivery_reporting_version": 1}))

    def _v2(self, event_id, event):
        key = (event["conversation_id"], event["thread_id"])
        author = self.live.get(key)
        if author is not None and author.intake:
            author.delivered.append(event)
            return self._store(event_id, self._completed(f"heard-{event_id}"))
        if not WAKES & set(event.get("facts") or []):
            return self._store(event_id, self._completed(f"heard-{event_id}"))
        if author is not None or len(self.live) >= self.slots:
            field = "conversation" if author is not None else "capacity"
            self.busy.append((event_id, field))
            return 429, {"ok": False, "error": f"presence_capacity_full: {field}", "code": "presence_capacity_full",
                         "disposition": "retry", "field": field,
                         "retry_after": self.conversation_hint if field == "conversation" else self.capacity_hint,
                         "attachment_manifest": []}
        ref = f"presence-turn-{next(self._refs)}"
        if event_id in self.inline:  # the author ended before it opened intake
            return self._store(event_id, self._completed(
                ref, outcome="message", text=self.inline[event_id], output_ref=self._output_ref(),
                reply_anchor=self._anchor(Author(ref, event))))
        self.authors[ref] = self.live[key] = Author(ref, event)
        return self._store(event_id, (200, {
            "ok": True, "status": "continuing", "outcome": "deferred", "text": "", "turn_ref": ref,
            "work_ref": "", "delivery_reporting_version": 1, "continuation_version": 2,
            "continuation_ref": ref, "output_ref": ""}))

    @staticmethod
    def _completed(ref, *, outcome="silent", text="", output_ref="", reply_anchor=None):
        return 200, {"ok": True, "status": "completed", "outcome": outcome, "text": text, "turn_ref": ref,
                     "work_ref": "", "delivery_reporting_version": 1, "continuation_version": 2,
                     "continuation_ref": "", "output_ref": output_ref,
                     **({"reply_anchor": reply_anchor} if reply_anchor else {})}

    def _store(self, event_id, answer):
        self.answers[event_id] = answer
        return answer

    def _output_ref(self):
        return f"presence-output-{next(self._outputs):024x}"

    @staticmethod
    def _anchor(author):
        covered = [author.event, *author.delivered[:author.covered]]
        return {"source_event_id": covered[-1]["source_event_id"], "message": copy.deepcopy(covered[-1]["message"])}

    def work_view(self, ref):
        with self.lock:
            self.polls.append(ref)
            author = self.authors.get(ref)
            if author is None:
                return 404, {"ok": False, "error": "unknown work"}
            common = {"ok": True, "work_ref": ref, "task_id": ref, "child_work_ref": author.child,
                      "outputs": copy.deepcopy(author.outputs), "delivery_reporting_version": 1}
            if author.anchored:
                common.update(continuation_ref=ref, continuation_version=2)
            if author.terminal is None:
                return 202, {**common, "status": "pending"}
            return 200, {**common, **copy.deepcopy(author.terminal)}

    def report(self, body):
        with self.lock:
            if self.report_failures:
                self.report_failures -= 1
                return 503, {"ok": False, "error": "history unreadable", "code": "presence_history_unreadable"}
            identity = (body["delivery_id"], body["part_id"], body["state"])
            duplicate = any((item["delivery_id"], item["part_id"], item["state"]) == identity
                            for item in self.reports)
            if not duplicate:
                self.reports.append(copy.deepcopy(body))
            return 200, {"ok": True, "recorded": True, "duplicate": duplicate}

    # Test controls for the author's own life.
    def ref_of(self, event_id):
        with self.lock:
            return self.answers[event_id][1]["turn_ref"]

    def see(self, ref):
        """The author's next model call sees every arrival delivered so far."""
        with self.lock:
            self.authors[ref].covered = len(self.authors[ref].delivered)

    def release(self, ref, text):
        with self.lock:
            author = self.authors[ref]
            output = {"output_ref": self._output_ref(), "outcome": "message", "text": text}
            if author.anchored:
                output["reply_anchor"] = self._anchor(author)
            author.outputs.append(output)
            return output

    def close_intake(self, ref):
        """Final closure: no more mail, the conversation and slot stay held until the terminal."""
        with self.lock:
            self.authors[ref].intake = False

    def finish(self, ref, text="", *, status="completed", outcome="", code="", anchor=None):
        with self.lock:
            author = self.authors[ref]
            author.intake = False
            terminal = {"status": status, "outcome": outcome or ("message" if text else "silent"), "text": text,
                        "output_ref": self._output_ref() if text else "", **({"code": code} if code else {})}
            if author.anchored and text:
                terminal["reply_anchor"] = anchor or self._anchor(author)
            author.terminal = terminal
            key = (author.event["conversation_id"], author.event["thread_id"])
            if self.live.get(key) is author:
                del self.live[key]
            return terminal

    def promote(self, ref):
        with self.lock:
            child = Author(f"presence-child-{next(self._refs)}", self.authors[ref].event, anchored=False)
            self.authors[child.ref] = child
            self.authors[ref].child = child.ref
            return child.ref

    def delivered(self, ref):
        with self.lock:
            return [event["source_event_id"] for event in self.authors[ref].delivered]

    def posted(self):
        with self.lock:
            return [event_id for event_id, _body in self.posts]


class Telegram:
    """Telegram's HTTP side under the real ``TelegramClient``: every request is recorded."""

    def __init__(self):
        self.requests, self.accepted, self.updates = [], [], []
        self.lose_before_accept = {}  # text -> sendMessage attempts that fail before Telegram accepts
        self._message_ids = itertools.count(9001)

    def client(self, token):
        return TelegramClient(token, transport=self, api_server="http://telegram.invalid")

    async def post_json(self, url, payload, timeout_sec):
        del timeout_sec
        method = url.rsplit("/", 1)[-1]
        self.requests.append((method, copy.deepcopy(payload)))
        if method == "getMe":
            return {"ok": True, "result": {"id": 9, "is_bot": True, "username": BOT}}
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        if method == "getUpdates":
            await asyncio.sleep(0.02)
            return {"ok": True, "result": [item for item in self.updates if item["update_id"] >= payload["offset"]]}
        assert method == "sendMessage", method
        if self.lose_before_accept.get(payload["text"], 0):
            self.lose_before_accept[payload["text"]] -= 1
            raise ConnectionResetError("connection reset before Telegram answered")
        self.accepted.append(copy.deepcopy(payload))
        return {"ok": True, "result": {"message_id": next(self._message_ids), "date": 1_700_001_000,
                                       "chat": {"id": int(payload["chat_id"])}, "text": payload["text"]}}

    async def post_multipart(self, *_args):
        raise AssertionError("no media in these scenarios")

    async def get_bytes(self, *_args):
        raise AssertionError("no media in these scenarios")

    def replies(self):
        """(chat, text, answered message id) of every message Telegram accepted, in order."""
        return [(item["chat_id"], item["text"], (item.get("reply_parameters") or {}).get("message_id"))
                for item in self.accepted]


class Bridge:
    """The production runtime over one state directory, rebuilt to model a restart."""

    def __init__(self, root, host, telegram):
        self.root, self.host, self.telegram = root, host, telegram
        self.restart()

    def restart(self):
        self.client = PresenceHostClient(state_dir=self.root, token_provider=lambda: TOKEN, host_base=self.host.base)
        self.runtime = TelegramTransportRuntime(state_dir=self.root, token_provider=lambda: "telegram-token",
                                                logger=Logger(), submitter=self.client,
                                                client_factory=self.telegram.client)
        self.store = self.runtime.store
        self.store.release_own_leases()

    def receive(self, *updates):
        for item in updates:
            event = parse_telegram_update(item, bot_account_id="9", places=True, bot_username=BOT)
            assert event is not None
            self.store.commit_update(item["update_id"], event)

    def due(self, *tables):
        with sqlite3.connect(self.store.path) as conn:
            for table in tables or ("inbox", "presence_work", "outbox"):
                conn.execute(f"UPDATE {table} SET available_at=0 WHERE state='pending'")

    async def inbox(self):
        """Every inbox row that is due now, once each, in the runtime's claim order."""
        worked = 0
        while await self.runtime.process_one_inbox():
            worked += 1
        return worked

    async def poll(self):
        self.due("presence_work")
        worked = 0
        while await self.runtime.process_one_work():
            worked += 1
        return worked

    async def send(self):
        while True:
            self.due("outbox")
            if not await self.runtime.process_one_outbox():
                break
        while await self.runtime.process_one_delivery_report():
            # Report backoff has passed. A due time of 0 means "nothing pending" to the store,
            # so a past moment stands for "now".
            with sqlite3.connect(self.store.path) as conn:
                for delivery_id, raw in conn.execute("SELECT delivery_id, reports_json FROM outbox").fetchall():
                    entries = [{**entry, "available_at": 1} for entry in json.loads(raw)]
                    conn.execute("UPDATE outbox SET reports_json=?, report_due_at=CASE WHEN report_due_at>0 "
                                 "THEN 1 ELSE 0 END WHERE delivery_id=?", (json.dumps(entries), delivery_id))

    def rows(self, sql, *args):
        with sqlite3.connect(self.store.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, args)]

    def inbox_rows(self):
        return {row["event_id"]: row for row in self.rows("SELECT * FROM inbox ORDER BY update_id")}

    def work(self):
        return {row["work_ref"]: (row["role"], row["state"]) for row in self.rows(
            "SELECT work_ref, role, state FROM presence_work ORDER BY rowid")}

    def outbox(self):
        return [(row["delivery_id"], json.loads(row["payload_json"])) for row in self.rows(
            "SELECT delivery_id, payload_json FROM outbox ORDER BY rowid")]


@pytest.fixture
def host():
    value = Host()
    yield value
    value.close()


@pytest.fixture
def telegram():
    return Telegram()


def bodies(host, event_id):
    with host.lock:
        return [body for posted, body in host.posts if posted == event_id]


@pytest.mark.parametrize("identity,requested", [
    (V2_IDENTITY, 2),
    ({**V2_IDENTITY, "presence_continuation_versions": [1]}, None),
    ({key: value for key, value in V2_IDENTITY.items() if key != "presence_continuation_versions"}, None),
    ({**V2_IDENTITY, "presence_continuation_versions": ["2", 2.0, True]}, None),
    ({**V2_IDENTITY, "presence_continuation_version": 2, "presence_continuation_versions": None}, None),
])
def test_version_2_is_requested_only_when_the_host_lists_it(tmp_path, telegram, identity, requested):
    async def run():
        host = Host(identity)
        try:
            bridge = Bridge(tmp_path, host, telegram)
            bridge.receive(message(1, "hello", mention=True))
            assert await bridge.inbox() == 1
            body = bodies(host, source(1))[0]
            assert set(body) == {"event", "staged_files", "delivery_reporting_version",
                                 *({"continuation_version"} if requested else ())}
            assert body.get("continuation_version") == requested
            assert bridge.runtime.status_snapshot()["presence_continuation_version"] == (requested or 0)
            expected = "continuing" if requested else "completed"
            assert host.answers[source(1)][1]["status"] == expected
        finally:
            host.close()

    asyncio.run(run())


def test_a_startup_burst_and_a_later_correction_reach_one_author_and_its_reply_answers_the_correction(
        tmp_path, host, telegram):
    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        # Three inputs are queued before the transport submits any of them.
        bridge.receive(message(1, "please summarize the thread", mention=True),
                       message(2, "context: the numbers are in the doc", user=4),
                       message(3, "and add the totals", mention=True, user=5))
        assert await bridge.inbox() == 3
        ref = host.ref_of(source(1))
        # The first started the conversation's author; its early answer is custody, never speech.
        assert host.answers[source(1)][1]["status"] == "continuing"
        assert [host.answers[source(n)][1]["outcome"] for n in (2, 3)] == ["silent", "silent"]
        assert host.delivered(ref) == [source(2), source(3)]
        assert host.posted() == [source(1), source(2), source(3)]
        assert bridge.work() == {ref: ("deferred", "pending")}
        assert {row["state"] for row in bridge.inbox_rows().values()} == {"submitted"}
        assert bridge.inbox_rows()[source(1)]["submission_ref"] == ref
        host.see(ref)
        assert await bridge.poll() == 1
        await bridge.send()
        assert bridge.outbox() == [] and telegram.replies() == []
        # A correction while the author is still reasoning joins it as context.
        bridge.receive(message(4, "correction: only last week", mention=True, user=4))
        assert await bridge.inbox() == 1
        assert host.answers[source(4)][1]["outcome"] == "silent" and host.delivered(ref)[-1] == source(4)
        host.see(ref)
        host.finish(ref, "Totals for last week: 42.")
        assert await bridge.poll() == 1
        await bridge.send()
        assert telegram.replies() == [(str(GROUP), "Totals for last week: 42.", 104)]
        assert len(host.authors) == 1 and host.posted() == [source(n) for n in (1, 2, 3, 4)]
        assert [(report["origin"]["task_id"], report["message"]["reply_to_message_id"])
                for report in host.reports] == [(ref, 104)]
        # Later polls, a restart and Telegram redelivering the burst send and submit nothing more.
        bridge.restart()
        bridge.receive(message(1, "please summarize the thread", mention=True))
        assert await bridge.inbox() == 0 and await bridge.poll() == 0
        await bridge.send()
        assert len(telegram.accepted) == 1 and len(host.posts) == 4

    asyncio.run(run())


def test_a_busy_conversation_keeps_its_event_first_honours_the_hint_and_serves_other_chats(
        tmp_path, host, telegram):
    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "start", mention=True))
        assert await bridge.inbox() == 1
        first = host.ref_of(source(1))
        host.close_intake(first)  # its final closure: the conversation stays held until its terminal
        bridge.receive(message(2, "one more thing", mention=True),
                       message(3, "background after it", user=4),
                       message(4, "hello from a private chat", chat=501, user=501))
        before = time.time()
        assert await bridge.inbox() == 2  # 2 is refused busy, 3 waits behind it, 4 is served meanwhile
        assert host.posted() == [source(1), source(2), source(4)]
        assert host.busy == [(source(2), "conversation"), (source(4), "capacity")]
        rows = bridge.inbox_rows()
        assert (rows[source(2)]["state"], rows[source(3)]["state"], rows[source(4)]["state"]) == (
            "pending", "pending", "pending")
        # Not custody and not a failure: no attempt is counted and the delay is the Host's hint.
        assert rows[source(2)]["attempts"] == 0 and rows[source(4)]["attempts"] == 0
        assert 0.9 <= rows[source(2)]["available_at"] - before <= 1.5
        assert 4.9 <= rows[source(4)]["available_at"] - before <= 5.5
        assert "presence_capacity_full" in rows[source(2)]["last_error"]
        assert bridge.work() == {first: ("deferred", "pending")} and bridge.outbox() == []
        for _ in range(5):  # repeated refusals never grow into an exponential delay
            bridge.due("inbox")
            before = time.time()
            assert await bridge.inbox() == 2
            rows = bridge.inbox_rows()
            assert 0.9 <= rows[source(2)]["available_at"] - before <= 1.5
            assert 4.9 <= rows[source(4)]["available_at"] - before <= 5.5
            assert rows[source(2)]["attempts"] == rows[source(4)]["attempts"] == 0
        assert host.posted().count(source(3)) == 0
        # Each retry is the same event: identical request bytes, never a new one.
        assert len({json.dumps(body, sort_keys=True) for body in bodies(host, source(2))}) == 1
        host.finish(first, "First answer.")
        bridge.due("inbox")
        assert await bridge.inbox() == 3
        second = host.ref_of(source(2))
        assert host.answers[source(2)][1]["status"] == "continuing" and host.delivered(second) == [source(3)]
        assert host.busy[-1] == (source(4), "capacity")  # the second author holds the only slot
        host.see(second)
        host.finish(second, "Second answer.")
        bridge.due("inbox")
        assert await bridge.inbox() == 1
        assert await bridge.poll() == 3
        await bridge.send()
        third = host.ref_of(source(4))
        host.finish(third, "Private answer.")
        assert await bridge.poll() == 1
        await bridge.send()
        assert telegram.replies() == [(str(GROUP), "First answer.", 101), (str(GROUP), "Second answer.", 103),
                                      ("501", "Private answer.", 104)]
        rows = bridge.inbox_rows()
        assert {row["state"] for row in rows.values()} == {"submitted"}
        assert [rows[source(n)]["attempts"] for n in (1, 2, 3, 4)] == [1, 1, 1, 1]

    asyncio.run(run())


@pytest.mark.parametrize("payload,expected", [
    ({"field": "conversation", "retry_after": 1}, 1.0),
    ({"field": "capacity", "retry_after": 5}, 5.0),
    ({"field": "capacity", "retry_after": 2.5}, 2.5),
    ({"field": "capacity", "retry_after": 900}, 900.0),
    ({"field": "conversation", "retry_after": 0}, 1.0),
    ({"field": "conversation"}, 1.0),
    ({"field": "capacity", "retry_after": "5"}, 5.0),
    ({"field": "capacity", "retry_after": True}, 5.0),
    ({"field": "capacity", "retry_after": float("nan")}, 5.0),
    ({}, None),  # an in-flight refusal without a field keeps the ordinary retry backoff
    ({"field": "slot", "retry_after": 1}, None),
    ({"field": "conversation", "retry_after": 1, "disposition": "blocked"}, None),
    ({"field": "conversation", "retry_after": 1, "code": "presence_rate_limited"}, None),
    ({"field": "conversation", "retry_after": 1, "ok": True}, None),
])
def test_only_a_typed_busy_refusal_is_retried_after_its_host_hint(payload, expected):
    body = {"ok": False, "code": "presence_capacity_full", "disposition": "retry", **payload}
    assert PresenceHostHTTPError(429, "busy", body).busy_retry_after == expected


def test_a_busy_wait_does_not_escalate_a_later_ordinary_retry(tmp_path, host, telegram):
    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "start", mention=True))
        assert await bridge.inbox() == 1
        host.close_intake(host.ref_of(source(1)))
        bridge.receive(message(2, "again", mention=True))
        for _ in range(6):
            bridge.due("inbox")
            assert await bridge.inbox() == 1
        host.finish(host.ref_of(source(1)))
        host.losses.append("/presence/turn")  # an ordinary failure: the Host's answer is lost
        bridge.due("inbox")
        before = time.time()
        assert await bridge.inbox() == 1
        row = bridge.inbox_rows()[source(2)]
        assert row["state"] == "pending" and row["attempts"] == 1
        assert 1.9 <= row["available_at"] - before <= 2.5  # the first ordinary backoff, not the eighth
        bridge.due("inbox")
        assert await bridge.inbox() == 1
        assert host.answers[source(2)][1]["status"] == "continuing"
        assert len(host.authors) == 2 and bridge.inbox_rows()[source(2)]["state"] == "submitted"

    asyncio.run(run())


def test_more_waiting_conversations_than_workers_never_hold_back_a_correction(tmp_path, host, telegram):
    """Real workers: two inbox workers, one slot, three conversations refused for capacity."""

    async def run():
        host.capacity_hint = 30  # long enough that a worker waiting it out would starve the correction
        for update_id, chat in ((2, 601), (3, 602), (4, 603)):
            host.inline[source(update_id)] = f"Private answer {chat}."
        telegram.updates = [message(1, "please summarize", mention=True)]
        bridge = Bridge(tmp_path, host, telegram)
        assert bridge.runtime.inbox_workers == 2
        task = asyncio.create_task(bridge.runtime.run())

        async def until(predicate, what, timeout=20):
            deadline = time.monotonic() + timeout
            while not predicate():
                assert not task.done(), f"the runtime stopped before {what}"
                assert time.monotonic() < deadline, f"timed out waiting for {what}"
                await asyncio.sleep(0.02)

        try:
            await until(lambda: bridge.work(), "the first author's custody")
            ref = host.ref_of(source(1))
            # Three other conversations wait for the only slot, ahead of a correction to the first.
            telegram.updates += [message(2, "hello", chat=601, user=601), message(3, "hello", chat=602, user=602),
                                 message(4, "hello", chat=603, user=603),
                                 message(5, "correction: only last week", mention=True, user=4)]
            await until(lambda: source(5) in host.answers and len({item for item, _field in host.busy}) == 3,
                        "the correction's answer and every capacity refusal", timeout=10)
            with host.lock:
                # The correction reached the running author while every other conversation was
                # still refused: no busy event held a worker or was admitted.
                assert host.answers[source(5)][1]["outcome"] == "silent" and host.delivered(ref) == [source(5)]
                assert sorted(host.busy) == [(source(n), "capacity") for n in (2, 3, 4)]
                assert all(source(n) not in host.answers for n in (2, 3, 4))
                assert host.authors[ref].terminal is None
            # Each waits out the Host's hint as its conversation's head, without being retried early.
            rows = bridge.inbox_rows()
            assert all(rows[source(n)]["state"] == "pending" and rows[source(n)]["available_at"] > time.time() + 25
                       for n in (2, 3, 4))
            host.see(ref)
            host.finish(ref, "Totals for last week: 42.")
            bridge.due("inbox", "presence_work")  # the hint and the poll interval have passed
            await until(lambda: len(telegram.accepted) == 4, "every reply")
            await until(lambda: len(host.reports) == 4, "every delivery report")
        finally:
            bridge.runtime.stop()
            await asyncio.gather(task, return_exceptions=True)
        assert sorted(telegram.replies(), key=lambda item: item[2]) == [
            ("601", "Private answer 601.", 102), ("602", "Private answer 602.", 103),
            ("603", "Private answer 603.", 104), (str(GROUP), "Totals for last week: 42.", 105)]
        assert all(host.posted().count(source(n)) == 1 for n in (1, 5))
        rows = bridge.inbox_rows()
        assert {row["state"] for row in rows.values()} == {"submitted"}
        assert all(rows[source(n)]["attempts"] == 1 for n in range(1, 6))
        assert len(host.authors) == 1

    asyncio.run(run())


@pytest.mark.parametrize("loss", ["answer_lost", "crash_before_record"])
def test_a_lost_early_answer_is_replayed_after_a_restart_without_a_second_author(
        tmp_path, host, telegram, monkeypatch, loss):
    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "please summarize", mention=True), message(2, "fyi: numbers are final", user=4))
        if loss == "answer_lost":
            host.losses.append("/presence/turn")
        else:
            original = bridge.store.record_submission

            def crash(*_args, **_kwargs):
                monkeypatch.setattr(bridge.store, "record_submission", original)
                raise OSError("disk unavailable before the custody commit")

            monkeypatch.setattr(bridge.store, "record_submission", crash)
        assert await bridge.inbox() == 1
        ref = host.ref_of(source(1))
        # The Host started the author, but this transport holds no custody: the event stays first.
        assert bridge.inbox_rows()[source(1)]["state"] == "pending" and bridge.work() == {}
        assert host.posted() == [source(1)]
        bridge.restart()
        bridge.due("inbox")
        assert await bridge.inbox() == 2
        assert host.posted() == [source(1), source(1), source(2)]
        assert bodies(host, source(1))[0] == bodies(host, source(1))[1]
        assert len(host.authors) == 1 and host.delivered(ref) == [source(2)]
        assert bridge.work() == {ref: ("deferred", "pending")}
        host.see(ref)
        host.release(ref, "Working on the summary.")
        host.losses.append("/presence/work")  # a lost poll answer: polled again, said once
        assert await bridge.poll() == 1
        assert bridge.outbox() == []
        assert await bridge.poll() == 1
        host.finish(ref, "Summary with the final numbers.")
        bridge.restart()
        assert await bridge.poll() == 1
        await bridge.send()
        assert telegram.replies() == [(str(GROUP), "Working on the summary.", 102),
                                      (str(GROUP), "Summary with the final numbers.", 102)]

    asyncio.run(run())


def test_late_outputs_children_and_retained_references_speak_each_output_once_with_its_anchor(
        tmp_path, host, telegram):
    async def run():
        retained_child = Author("retained-child-1", None, anchored=False)
        retained_turn = Author("retained-turn-1", None, anchored=False)
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "plan the release", mention=True))
        assert await bridge.inbox() == 1
        ref = host.ref_of(source(1))
        bridge.receive(message(2, "the deadline moved to friday", user=4))
        assert await bridge.inbox() == 1
        host.see(ref)
        draft = host.release(ref, "Draft plan for friday.")
        bridge.receive(message(3, "include the totals", mention=True, user=5))
        assert await bridge.inbox() == 1
        assert await bridge.poll() == 1
        host.see(ref)
        totals = host.release(ref, "Totals added.")
        child = host.promote(ref)
        child_note = host.release(child, "Long task started.")
        # The same selected output can be named by more than one reference.
        with host.lock:
            host.authors[ref].outputs.append(copy.deepcopy(child_note))
        assert await bridge.poll() == 2
        assert bridge.work() == {ref: ("deferred", "pending"), child: ("child", "pending")}
        bridge.restart()
        assert await bridge.poll() == 2
        host.finish(child, "Long task finished.")
        final = host.finish(ref, "Final plan.")
        assert await bridge.poll() == 2
        for _ in range(3):
            assert await bridge.poll() == 0
        # A retained refusal of another chat's event: its original and child name one output.
        host.override[source(4)] = (409, {
            "ok": False, "error": "presence_attempt_outcome_unknown: source_event_id",
            "code": "presence_attempt_outcome_unknown", "disposition": "retained", "field": "source_event_id",
            "turn_ref": retained_turn.ref, "work_ref": retained_child.ref, "delivery_reporting_version": 1})
        bridge.receive(message(4, "status?", chat=701, user=701))
        with host.lock:
            for author in (retained_turn, retained_child):
                host.authors[author.ref] = author
            shared = {"output_ref": host._output_ref(), "outcome": "message", "text": "Recovered answer."}
            retained_turn.outputs.append(dict(shared))
            retained_turn.terminal = {"status": "failed", "outcome": "silent", "text": "", "output_ref": "",
                                      "code": "presence_attempt_outcome_unknown"}
            retained_child.terminal = {"status": "completed", "outcome": "message", "text": shared["text"],
                                       "output_ref": shared["output_ref"]}
        assert await bridge.inbox() == 1
        assert await bridge.poll() == 2
        await bridge.send()
        # The author's outputs answer the message their model call covered; the child's and the
        # retained references', which carry no anchor, answer their event's own message.
        assert telegram.replies() == [
            (str(GROUP), "Draft plan for friday.", 102), (str(GROUP), "Totals added.", 103),
            (str(GROUP), "Long task started.", 101), (str(GROUP), "Final plan.", 103),
            (str(GROUP), "Long task finished.", 101), ("701", "Recovered answer.", 104)]
        assert draft["reply_anchor"]["source_event_id"] == source(2)
        assert (totals["reply_anchor"]["source_event_id"], final["reply_anchor"]["source_event_id"]) == (
            source(3), source(3))
        identities = [delivery_id for delivery_id, _payload in bridge.outbox()]
        assert len(identities) == len(set(identities)) == 6
        # One row per output identity, under whichever reference named it first.
        assert [item for item in identities if item.endswith(child_note["output_ref"])] == [
            f"presence:{source(1)}:output:{ref}:{child_note['output_ref']}"]
        assert [item for item in identities if item.endswith(shared["output_ref"])] == [
            f"presence:{source(4)}:output:{retained_turn.ref}:{shared['output_ref']}"]
        assert bridge.work() == {ref: ("deferred", "completed"), child: ("child", "completed"),
                                 retained_turn.ref: ("turn", "failed"), retained_child.ref: ("child", "completed")}

    asyncio.run(run())


@pytest.mark.parametrize("anchor", ["another_chat", "unknown_event", "different_message", "malformed"])
def test_an_anchor_this_chat_never_captured_falls_back_to_the_original_message(tmp_path, host, telegram, anchor):
    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "question", mention=True), message(2, "elsewhere", chat=801, user=801))
        assert await bridge.inbox() == 2
        ref = host.ref_of(source(1))
        own = host.authors[ref].event
        other = json.loads(bridge.inbox_rows()[source(2)]["payload_json"])
        value = {
            "another_chat": {"source_event_id": source(2), "message": other["message"]},
            "unknown_event": {"source_event_id": source(77), "message": {**own["message"], "message_id": 177}},
            "different_message": {"source_event_id": source(1), "message": {**own["message"], "message_id": 555}},
            "malformed": {"source_event_id": source(1), "message": {"message_id": True}},
        }[anchor]
        host.finish(ref, "Answer.", anchor=value)
        assert await bridge.poll() == 1
        await bridge.send()
        assert telegram.replies()[-1] == (str(GROUP), "Answer.", 101)

    asyncio.run(run())


def test_a_topic_reply_stays_in_its_topic_and_answers_the_message_it_covered(tmp_path, host, telegram):
    async def run():
        host.slots = 2
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "plan for this topic", mention=True, topic=77),
                       message(2, "a question in the general chat", mention=True, user=5),
                       message(3, "detail for the topic", user=4, topic=77))
        assert await bridge.inbox() == 3
        topic_turn, general_turn = host.ref_of(source(1)), host.ref_of(source(2))
        assert topic_turn != general_turn and host.delivered(topic_turn) == [source(3)]
        host.see(topic_turn)
        host.finish(topic_turn, "Topic plan.")
        # An anchor in the same chat's other conversation (the topic) is not this turn's to answer.
        general = json.loads(bridge.inbox_rows()[source(2)]["payload_json"])
        host.finish(general_turn, "General answer.",
                    anchor={"source_event_id": source(3), "message": host.authors[topic_turn].delivered[0]["message"]})
        assert general["thread_id"] == "" and await bridge.poll() == 2
        await bridge.send()
        sent = [(item["text"], item.get("message_thread_id"), item["reply_parameters"]["message_id"])
                for item in telegram.accepted]
        assert sent == [("Topic plan.", 77, 103), ("General answer.", None, 102)]

    asyncio.run(run())


def test_an_author_that_ended_before_intake_answers_in_the_request_once(tmp_path, host, telegram):
    async def run():
        host.inline[source(1)] = "Quick answer."
        host.losses.append("/presence/turn")
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "quick one", chat=901, user=901), message(2, "thanks", chat=901, user=901))
        assert await bridge.inbox() == 1
        answer = host.answers[source(1)][1]
        assert (answer["status"], answer["reply_anchor"]["source_event_id"]) == ("completed", source(1))
        bridge.restart()
        bridge.due("inbox")
        assert await bridge.inbox() == 2  # the replayed answer, then the chat's next event
        await bridge.send()
        assert telegram.replies() == [("901", "Quick answer.", 101)]
        [(delivery_id, _payload)] = bridge.outbox()
        assert delivery_id == f"presence:{source(1)}:output:{answer['turn_ref']}:{answer['output_ref']}"
        assert host.posted() == [source(1), source(1), source(2)]
        assert bridge.work() == {host.ref_of(source(2)): ("deferred", "pending")}

    asyncio.run(run())


def test_send_and_report_unknowns_keep_custody_without_a_second_speech(tmp_path, host, telegram):
    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "summarize", mention=True))
        assert await bridge.inbox() == 1
        ref = host.ref_of(source(1))
        long_text = "Part one. " * 450 + "\n\nPart two."
        host.release(ref, long_text)
        host.finish(ref, "Done.")
        assert await bridge.poll() == 1
        telegram.lose_before_accept = {"Done.": 1}
        sent = []
        # The long output's earlier chunks are accepted; its last chunk's connection resets once.
        original = telegram.post_json

        async def lose_last_chunk(url, payload, timeout_sec):
            if url.endswith("/sendMessage") and payload["text"].endswith("Part two.") and not sent:
                sent.append(payload["text"])
                raise ConnectionResetError("connection reset before Telegram answered")
            return await original(url, payload, timeout_sec)

        telegram.post_json = lose_last_chunk
        host.report_failures = 1
        host.losses.append("/presence/delivery")  # one report is recorded but its answer is lost
        await bridge.send()
        chunks = [item for item in telegram.replies() if item[1] != "Done."]
        # Three frozen chunks, each accepted once; only the first replies to the anchored message.
        assert [item[2] for item in chunks] == [101, None, None] and chunks[-1][1].endswith("Part two.")
        assert sent and sent[0] == chunks[-1][1]
        assert [item for item in telegram.replies() if item[1] == "Done."] == [(str(GROUP), "Done.", 101)]
        assert len(telegram.accepted) == 4
        for _ in range(3):
            await bridge.send()
        assert len(telegram.accepted) == 4
        long_id, done_id = (delivery_id for delivery_id, _payload in bridge.outbox())
        assert sorted((report["delivery_id"], report["part_id"]) for report in host.reports) == sorted(
            [(long_id, "0"), (long_id, "1"), (long_id, "2"), (done_id, "0")])
        assert all(report["origin"] == {"kind": "automatic", "task_id": ref, "source_event_id": source(1)}
                   for report in host.reports)
        assert bridge.store.status_snapshot()["delivery_reports_pending"] == 0
        assert {row["state"] for row in bridge.rows("SELECT state FROM outbox")} == {"delivered"}

    asyncio.run(run())


def test_stop_speaks_no_cancelled_answer_and_never_reposts_its_source(tmp_path, host, telegram):
    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "research this", mention=True), message(2, "and that", mention=True, user=4))
        assert await bridge.inbox() == 2
        ref = host.ref_of(source(1))
        host.release(ref, "Looking into it.")
        assert await bridge.poll() == 1
        await bridge.send()
        host.finish(ref, status="cancelled", outcome="silent", code="presence_turn_cancelled")
        assert await bridge.poll() == 1
        await bridge.send()
        assert telegram.replies() == [(str(GROUP), "Looking into it.", 101)]
        assert bridge.work() == {ref: ("deferred", "cancelled")}
        bridge.restart()
        bridge.receive(message(1, "research this", mention=True), message(2, "and that", mention=True, user=4))
        bridge.due()
        assert await bridge.inbox() == 0 and await bridge.poll() == 0
        await bridge.send()
        assert host.posted() == [source(1), source(2)] and len(telegram.accepted) == 1
        # A later event is new work of its own, not a revival of the stopped turn.
        bridge.receive(message(3, "new question", mention=True))
        assert await bridge.inbox() == 1
        assert host.answers[source(3)][1]["status"] == "continuing" and host.ref_of(source(3)) != ref
        assert host.posted() == [source(1), source(2), source(3)]

    asyncio.run(run())


def test_an_older_host_keeps_the_whole_answer_in_the_conversation_queue(tmp_path, telegram):
    async def run():
        host = Host({key: value for key, value in V2_IDENTITY.items() if key != "presence_continuation_versions"})
        try:
            gate = threading.Event()
            host.legacy[source(1)] = {"text": "First answer.", "gate": gate}
            host.legacy[source(2)] = {"text": "Second answer."}
            bridge = Bridge(tmp_path, host, telegram)
            bridge.receive(message(1, "first", mention=True), message(2, "second", mention=True, user=4))
            first = asyncio.create_task(bridge.runtime.process_one_inbox())
            for _ in range(200):
                if host.posted():
                    break
                await asyncio.sleep(0.01)
            # The second event waits behind the first's whole answer, as before version 2.
            assert not await bridge.runtime.process_one_inbox()
            assert host.posted() == [source(1)] and "continuation_version" not in bodies(host, source(1))[0]
            gate.set()
            assert await first
            assert await bridge.inbox() == 1
            await bridge.send()
            assert telegram.replies() == [(str(GROUP), "First answer.", 101), (str(GROUP), "Second answer.", 102)]
            assert bridge.work() == {}
        finally:
            host.close()

    asyncio.run(run())


def test_a_replayed_initial_envelope_with_selected_speech_and_a_child_speaks_once(tmp_path, host, telegram):
    """An initial envelope first written at a review park can already carry speech and a child."""

    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "review this", mention=True))
        event = parse_telegram_update(message(1, "review this", mention=True), bot_account_id="9", places=True,
                                      bot_username=BOT).to_dict()
        with host.lock:
            author = host.authors["presence-turn-r"] = Author("presence-turn-r", event)
            child = host.authors["presence-child-r"] = Author("presence-child-r", event, anchored=False)
            early = {"output_ref": host._output_ref(), "outcome": "message", "text": "Reviewed: one fix needed.",
                     "reply_anchor": host._anchor(author)}
            author.outputs.append(early)
            author.child = child.ref
        host.override[source(1)] = (200, {
            "ok": True, "status": "continuing", "outcome": "message", "text": early["text"],
            "turn_ref": author.ref, "work_ref": child.ref, "delivery_reporting_version": 1,
            "continuation_version": 2, "continuation_ref": author.ref, "output_ref": early["output_ref"]})
        assert await bridge.inbox() == 1
        assert bridge.work() == {author.ref: ("deferred", "pending"), child.ref: ("child", "pending")}
        assert await bridge.poll() == 2
        host.finish(child.ref, "Fix applied.")
        host.finish(author.ref)
        assert await bridge.poll() == 2
        await bridge.send()
        assert telegram.replies() == [(str(GROUP), "Reviewed: one fix needed.", 101),
                                      (str(GROUP), "Fix applied.", 101)]
        assert bridge.work() == {author.ref: ("deferred", "completed"), child.ref: ("child", "completed")}

    asyncio.run(run())


@pytest.mark.parametrize("answer", [
    {"continuation_ref": ""},
    {"continuation_ref": "presence-turn-x", "turn_ref": "presence-turn-y"},
    {"continuation_version": 1},
    {"outcome": "unknown"},
])
def test_a_continuing_answer_without_its_real_version_2_reference_is_not_custody(tmp_path, host, telegram, answer):
    async def run():
        envelope = {"ok": True, "status": "continuing", "outcome": "deferred", "text": "", "turn_ref": "presence-turn-x",
                    "work_ref": "", "delivery_reporting_version": 1, "continuation_version": 2,
                    "continuation_ref": "presence-turn-x", "output_ref": "", **answer}
        host.override[source(1)] = (200, envelope)
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(message(1, "hello", mention=True), message(2, "later", mention=True))
        assert await bridge.inbox() == 1
        rows = bridge.inbox_rows()
        assert rows[source(1)]["state"] == "pending" and "PresenceHostError" in rows[source(1)]["last_error"]
        assert bridge.work() == {} and host.posted() == [source(1)]

    asyncio.run(run())


def test_a_continuing_answer_to_a_request_without_version_2_is_refused(tmp_path):
    class Transport:
        async def request_json(self, method, url, **kwargs):
            if url.endswith("/identity"):
                return {"ok": True, "places_version": 1, "presence_continuation_version": 1}
            assert "continuation_version" not in kwargs["payload"]
            return {"ok": True, "status": "continuing", "outcome": "deferred", "turn_ref": "t", "continuation_ref": "t",
                    "continuation_version": 2}

    client = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:1",
                                transport=Transport())
    event = parse_telegram_update(message(1, "hi", mention=True), bot_account_id="9", places=True, bot_username=BOT)
    with pytest.raises(PresenceHostError, match="did not complete"):
        asyncio.run(client.submit(event.to_dict(), []))


def test_observation_serves_due_work_first_so_two_slow_pending_authors_never_starve_a_finished_one(
        tmp_path, telegram, monkeypatch):
    """Each read takes longer than the pending pace: older references must not take every turn."""
    from types import SimpleNamespace

    from telegram_bot import custody

    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(custody, "time", SimpleNamespace(time=lambda: clock.now, gmtime=time.gmtime,
                                                         strftime=time.strftime))
    host = Host(slots=3)
    read = host.work_view

    def slow_read(ref):
        clock.now += 6.0  # one successful read outlasts the 5 second pending pace
        return read(ref)

    host.work_view = slow_read

    async def run():
        bridge = Bridge(tmp_path, host, telegram)
        bridge.receive(*(message(n, f"question {n}", chat=500 + n, user=500 + n) for n in (1, 2, 3)))
        for _ in range(3):  # one author per chat, captured in arrival order
            assert await bridge.runtime.process_one_inbox()
            clock.now += 1.0
        first, second, third = (host.ref_of(source(n)) for n in (1, 2, 3))
        host.finish(third, "Third answer.")
        for _ in range(6):  # the single observation worker, one claim at a time
            if not await bridge.runtime.process_one_work():
                clock.now += 0.5
        await bridge.send()
        return first, second, third

    try:
        first, second, third = asyncio.run(run())
    finally:
        host.close()
    # Older pending work keeps being observed, but due order gives the finished one its turn.
    assert host.polls[:5] == [first, second, third, first, second]
    assert telegram.replies() == [("503", "Third answer.", 103)]
