"""A loopback Host answering the Presence wire as continuation version 2 describes it.

It models what a consumer can observe, not the core. A live author takes its conversation's
later events as context (``completed``/``silent``); a background event no author takes is
heard. A wake event without a live author starts one: at version 2 it answers
``continuing`` once the author's intake opens (the test may hold that), and a held
conversation or full capacity answers the typed 429 at once. Versions 0 and 1 wait for the
author's terminal, as an older Host does. Work views report the author's state, a child
and its terminal output with an optional ``reply_anchor``. Every request is recorded with
its exact body; a replayed event gets its stored answer. Real HTTP on 127.0.0.1 only.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import unquote, urlsplit

WAKES = {"direct_message", "mention", "reply_to_me"}
IDENTITY = {"ok": True, "name": "fake", "places_version": 1, "presence_work_output_version": 1,
            "presence_continuation_version": 1, "presence_delivery_version": 1}


class Author:
    def __init__(self, ref, event, version):
        self.ref, self.event, self.version = ref, event, version
        self.context = []             # later events this author took as context, in order
        self.intake = threading.Event()
        self.closing = False          # final closure: a wake arrival is told to retry
        self.terminal = None          # the terminal work view, once ended
        self.ended = threading.Event()
        self.child = ""
        self.outputs = []             # released outputs (rows), as an older author's park would

    def accepting(self):
        return self.intake.is_set() and not self.closing and self.terminal is None


class FakeHost:
    def __init__(self, versions=(1, 2), *, capacity=4, auto_intake=True, delivery=1):
        self.versions = None if versions is None else list(versions)
        self.capacity, self.auto_intake, self.delivery = capacity, auto_intake, delivery
        self.cv = threading.Condition()
        self.requests = []            # (method, path, body)
        self.authors = {}             # turn ref -> Author
        self.live = {}                # conversation_key -> Author still running
        self.answers = {}             # source_event_id -> stored (status, body)
        self.busy = set()             # conversation keys held by something else (preparing, remote)
        self.capacity_full = False
        self.inflight_full = False    # the older in-flight refusal: no field, no retry_after
        self.work = {}                # other refs -> scripted work views; the last repeats
        self.hold = {}                # source_event_id -> threading.Event a POST waits for
        self.hold_work = {}           # work ref -> threading.Event a work read waits for
        self.place_state = "active"
        self.counter = 0
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._answer(*host.route("GET", self.path, None))

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self._answer(*host.route("POST", self.path, json.loads(self.rfile.read(length) or b"null")))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                                       name="fake-host", daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        for gate in (*self.hold.values(), *self.hold_work.values()):
            gate.set()
        for author in self.authors.values():
            author.intake.set()
            author.ended.set()
        self.server.shutdown()
        self.server.server_close()

    # Observation helpers for tests.
    def posts(self, key=None):
        with self.cv:
            return [body["event"]["source_event_id"] for method, path, body in self.requests
                    if method == "POST" and path == "/presence/turn"
                    and (key is None or body["event"]["conversation_key"] == key)]

    def bodies(self, path="/presence/turn"):
        with self.cv:
            return [body for method, p, body in self.requests if method == "POST" and p == path]

    def polls(self, ref=None):
        with self.cv:
            return [p.rsplit("/", 1)[-1] for method, p, _ in self.requests
                    if method == "GET" and p.startswith("/presence/work/") and (ref is None or unquote(p.rsplit("/", 1)[-1].split("?")[0]) == ref)]

    def wait(self, predicate, timeout=30.0):
        with self.cv:
            if not self.cv.wait_for(predicate, timeout):
                raise AssertionError("fake Host condition not reached; requests: %r" % [
                    (m, p) for m, p, _ in self.requests])

    def author(self, key):
        with self.cv:
            return self.live.get(key)

    # Test controls.
    def open(self, ref):
        self.authors[ref].intake.set()

    def finish(self, ref, *, text="", output_ref="", anchor=None, status="completed", child="",
               outcome=None, outputs=()):
        """End an author: its conversation is free; its view becomes terminal."""
        with self.cv:
            author = self.authors[ref]
            author.child = child or author.child
            body = {"ok": True, "status": status, "outcome": outcome or ("message" if text else "silent"),
                    "text": text, "output_ref": output_ref, "work_ref": ref, "task_id": ref,
                    "continuation_ref": ref, "continuation_version": 2 if author.version == 2 else 1,
                    "child_work_ref": author.child, "outputs": list(outputs) or list(author.outputs),
                    "delivery_reporting_version": self.delivery}
            if anchor is not None and author.version == 2:
                body["reply_anchor"] = {"source_event_id": anchor["source_event_id"], "message": anchor["message"]}
            author.terminal = body
            if self.live.get(author.event["conversation_key"]) is author:
                del self.live[author.event["conversation_key"]]
            author.intake.set()
            author.ended.set()
            self.cv.notify_all()

    def close_author(self, ref):
        with self.cv:
            self.authors[ref].closing = True
            self.cv.notify_all()

    # The wire.
    def route(self, method, path, body):
        url = urlsplit(path)
        with self.cv:
            self.requests.append((method, url.path, body))
            self.cv.notify_all()
        if method == "GET" and url.path == "/identity":
            facts = dict(IDENTITY)
            if self.versions is not None:
                facts["presence_continuation_versions"] = self.versions
            return 200, facts
        if method == "GET" and url.path == "/presence/place":
            return 200, {"place_id": "place", "label": "Telegram", "state": self.place_state}
        if method == "POST" and url.path == "/presence/delivery":
            return 200, {"ok": True, "recorded": True, "duplicate": False}
        if method == "GET" and url.path.startswith("/presence/work/"):
            return self.view(unquote(url.path.rsplit("/", 1)[-1]))
        if method == "POST" and url.path == "/presence/turn":
            return self.turn(body)
        return 404, {"ok": False, "code": "not_found", "disposition": "rejected", "error": "no route"}

    def view(self, ref):
        gate = self.hold_work.get(ref)
        if gate is not None:
            gate.wait(30)  # a slow work read; the rest of the Host keeps answering
        with self.cv:
            author = self.authors.get(ref)
            if author is None:
                answers = self.work.get(ref)
                if not answers:
                    return 404, {"ok": False, "code": "presence_work_not_found", "disposition": "rejected",
                                 "error": "presence_work_not_found"}
                return answers.pop(0) if len(answers) > 1 else answers[0]
            if author.terminal is not None:
                return 200, author.terminal
            return 202, {"ok": True, "status": "pending", "work_ref": ref, "task_id": ref, "continuation_ref": ref,
                         "continuation_version": 2 if author.version == 2 else 1, "child_work_ref": author.child,
                         "outputs": list(author.outputs), "delivery_reporting_version": self.delivery}

    def turn(self, body):
        event = body["event"]
        sid, key = event["source_event_id"], event["conversation_key"]
        version = body.get("continuation_version", 0)
        gate = self.hold.get(sid)
        if gate is not None:
            gate.wait(30)
        with self.cv:
            if sid in self.answers:
                return self.answers[sid]  # a retry of the same event: its stored answer
            author = self.live.get(key)
            facts = event.get("facts")
            wake = facts is None or bool(WAKES & set(facts))  # absent facts: legacy_addressed
            if author is not None and author.accepting():
                author.context.append(event)
                return self.store(sid, 200, self.settled("current_author"))
            if not wake:
                return self.store(sid, 200, self.settled("heard"))
            if self.inflight_full:
                return 429, {"ok": False, "error": "too many in-flight presence requests",
                             "code": "presence_capacity_full", "disposition": "retry"}
            if version == 2:
                if author is not None or key in self.busy:
                    return 429, self.capacity_error("conversation", 1)
                running = sum(1 for a in self.live.values() if a.terminal is None)
                if self.capacity_full or running >= self.capacity:
                    return 429, self.capacity_error("capacity", 5)
            elif author is not None:
                # Versions 0 and 1 still wait in the gate for the conversation's author.
                self.cv.wait_for(lambda: author.terminal is not None, 30)
            self.counter += 1
            ref = "turn-%d" % self.counter
            author = self.authors[ref] = Author(ref, event, version)
            self.live[key] = author
            if self.auto_intake:
                author.intake.set()
            self.cv.notify_all()
        if version == 2:
            author.intake.wait(30)
            with self.cv:
                if author.terminal is not None:
                    # The author ended before its intake opened: the terminal answers the request.
                    # A turn answer's work_ref names a promoted child, never the turn itself.
                    terminal = author.terminal
                    return self.store(sid, 200, {
                        **{key: terminal[key] for key in ("ok", "status", "outcome", "text", "output_ref",
                                                          "delivery_reporting_version", "reply_anchor") if key in terminal},
                        "turn_ref": ref, "work_ref": author.child, "continuation_ref": "", "continuation_version": 2})
                return self.store(sid, 200, {
                    "ok": True, "status": "continuing", "outcome": "deferred", "text": "", "output_ref": "",
                    "turn_ref": ref, "continuation_ref": ref, "work_ref": "", "continuation_version": 2,
                    "delivery_reporting_version": self.delivery})
        author.ended.wait(30)
        with self.cv:
            terminal = dict(author.terminal or {})
            answer = {key: terminal[key] for key in ("ok", "status", "outcome", "text", "output_ref",
                                                     "delivery_reporting_version") if key in terminal}
            answer.update(turn_ref=ref, work_ref=author.child)
            if version:
                answer.update(continuation_version=1, continuation_ref="")
            return self.store(sid, 200, answer)

    def store(self, sid, status, body):
        self.answers[sid] = (status, body)
        return status, body

    def settled(self, reason):
        return {"ok": True, "status": "completed", "outcome": "silent", "text": "", "output_ref": "",
                "turn_ref": "", "work_ref": "", "reason": reason, "delivery_reporting_version": self.delivery}

    @staticmethod
    def capacity_error(field, after):
        return {"ok": False, "error": "presence_capacity_full: " + field, "code": "presence_capacity_full",
                "disposition": "retry", "field": field, "retry_after": after, "attachment_manifest": []}
