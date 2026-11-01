"""AgentRQ Presence intake under continuation version 2, against loopback fakes.

The actual PresenceInbox (urllib to the Host), the listener's intake loop and the plugin's
AgentRQClient run against two servers on 127.0.0.1: a Host that answers the Presence wire
as continuation version 2 describes it, and an AgentRQ MCP endpoint that records task
replies. No AgentRQ workspace, credentials, live network or real message.
"""
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SKILL / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


presence = load("agentrq_presence_v2", "presence.py")
WORKSPACE = "https://workspace.example/mcp"


def serve(route):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _call(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"null") if method == "POST" else None
            status, answer, headers = route(method, self.path, body)
            data = b"" if answer is None else json.dumps(answer).encode()
            self.send_response(status)
            for name, value in {**headers, "Content-Type": "application/json", "Content-Length": str(len(data))}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._call("GET")

        def do_POST(self):
            self._call("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server


class Host:
    """The consumer-visible Host: live authors take their task's later events as context."""

    def __init__(self, versions=(1, 2), *, auto_intake=True, capacity=4):
        self.versions, self.auto_intake, self.capacity = versions, auto_intake, capacity
        self.cv = threading.Condition()
        self.requests, self.authors, self.live, self.answers = [], {}, {}, {}
        self.busy, self.work, self.counter = set(), {}, 0
        self.hold = {}  # source_event_id -> threading.Event its POST waits for
        self.server = serve(self.route)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        for gate in self.hold.values():
            gate.set()
        for author in self.authors.values():
            author["intake"].set()
            author["ended"].set()
        self.server.shutdown()
        self.server.server_close()

    def posts(self, task=None):
        with self.cv:
            return [b["event"]["source_event_id"] for m, p, b in self.requests if p == "/presence/turn"
                    and (task is None or b["event"]["conversation"]["task_id"] == task)]

    def bodies(self):
        with self.cv:
            return [b for m, p, b in self.requests if p == "/presence/turn"]

    def polls(self, ref):
        with self.cv:
            return [p for m, p, b in self.requests if p == "/presence/work/" + ref]

    def wait(self, predicate, timeout=10):
        with self.cv:
            assert self.cv.wait_for(predicate, timeout), [p for m, p, b in self.requests]

    def author(self, task):
        with self.cv:
            return self.live.get(task)

    def finish(self, ref, *, text="", output_ref="", anchor=None, status="completed"):
        with self.cv:
            author = self.authors[ref]
            body = {"ok": True, "status": status, "outcome": "message" if text else "silent", "text": text,
                    "output_ref": output_ref, "work_ref": ref, "task_id": ref, "continuation_ref": ref,
                    "continuation_version": author["version"] or 1, "child_work_ref": author["child"], "outputs": []}
            if anchor is not None and author["version"] == 2:
                body["reply_anchor"] = {"source_event_id": anchor["source_event_id"], "message": anchor["message"]}
            author["terminal"] = body
            self.live.pop(author["task"], None)
            author["intake"].set()
            author["ended"].set()
            self.cv.notify_all()

    def route(self, method, path, body):
        url = urlsplit(path)
        with self.cv:
            self.requests.append((method, url.path, body))
            self.cv.notify_all()
        if url.path == "/identity":
            facts = {"ok": True, "places_version": 1, "presence_continuation_version": 1}
            if self.versions is not None:
                facts["presence_continuation_versions"] = list(self.versions)
            return 200, facts, {}
        if url.path == "/presence/place":
            return 200, {"place_id": "place", "label": "AgentRQ", "state": "active"}, {}
        if url.path.startswith("/presence/work/"):
            return (*self.view(unquote(url.path.rsplit("/", 1)[-1])), {})
        return (*self.turn(body), {})

    def view(self, ref):
        with self.cv:
            author = self.authors.get(ref)
            if author is None:
                answers = self.work.get(ref)
                if not answers:
                    return 404, {"ok": False, "code": "presence_work_not_found", "disposition": "rejected"}
                return answers.pop(0) if len(answers) > 1 else answers[0]
            if author["terminal"] is not None:
                return 200, author["terminal"]
            return 202, {"ok": True, "status": "pending", "work_ref": ref, "task_id": ref, "continuation_ref": ref,
                         "continuation_version": author["version"] or 1, "child_work_ref": author["child"], "outputs": []}

    def turn(self, body):
        event, version = body["event"], body.get("continuation_version", 0)
        sid, task = event["source_event_id"], event["conversation"]["task_id"]
        gate = self.hold.get(sid)
        if gate is not None:
            gate.wait(30)
        with self.cv:
            if sid in self.answers:
                return self.answers[sid]
            author = self.live.get(task)
            if author is not None and author["intake"].is_set() and author["terminal"] is None:
                author["context"].append(event)
                return self.store(sid, {"ok": True, "status": "completed", "outcome": "silent", "text": ""})
            if "direct_message" not in event["facts"]:
                return self.store(sid, {"ok": True, "status": "completed", "outcome": "silent", "text": ""})
            if version == 2 and (author is not None or task in self.busy):
                return 429, {"ok": False, "error": "presence_capacity_full: conversation", "code": "presence_capacity_full",
                             "disposition": "retry", "field": "conversation", "retry_after": 1, "attachment_manifest": []}
            if version == 2 and len(self.live) >= self.capacity:
                return 429, {"ok": False, "error": "presence_capacity_full: capacity", "code": "presence_capacity_full",
                             "disposition": "retry", "field": "capacity", "retry_after": 5, "attachment_manifest": []}
            if author is not None:
                self.cv.wait_for(lambda: author["terminal"] is not None, 10)
            self.counter += 1
            ref = "turn-%d" % self.counter
            author = self.authors[ref] = {"ref": ref, "task": task, "event": event, "version": version, "context": [],
                                          "child": "", "terminal": None, "intake": threading.Event(),
                                          "ended": threading.Event()}
            self.live[task] = author
            if self.auto_intake:
                author["intake"].set()
            self.cv.notify_all()
        if version == 2:
            author["intake"].wait(10)
            return self.store(sid, {"ok": True, "status": "continuing", "outcome": "deferred", "text": "",
                                    "output_ref": "", "turn_ref": ref, "continuation_ref": ref, "work_ref": "",
                                    "continuation_version": 2, "delivery_reporting_version": 0})
        author["ended"].wait(10)
        terminal = author["terminal"] or {}
        return self.store(sid, {**{k: terminal[k] for k in ("ok", "status", "outcome", "text", "output_ref") if k in terminal},
                                "turn_ref": ref, "work_ref": author["child"]})

    def store(self, sid, body):
        self.answers[sid] = (200, body)
        return 200, body


class AgentRQ:
    """A Streamable HTTP MCP endpoint that records task replies."""

    def __init__(self):
        self.replies, self.lock = [], threading.Lock()
        self.hold = None  # a threading.Event a reply waits for, after it is recorded
        self.server = serve(self.route)
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]

    def close(self):
        if self.hold is not None:
            self.hold.set()
        self.server.shutdown()
        self.server.server_close()

    def route(self, method, path, body):
        if body.get("method") == "initialize":
            return 200, {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}}, {"Mcp-Session-Id": "session"}
        if "id" not in body:
            return 202, None, {}
        arguments = body["params"]["arguments"]
        with self.lock:
            self.replies.append((arguments["chatId"], arguments["text"]))
            number = len(self.replies)
        if self.hold is not None:
            self.hold.wait(30)
        return 200, {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [
            {"type": "text", "text": json.dumps({"ok": True, "messageId": "m-%d" % number})}]}}, {}


@pytest.fixture
def make_world(tmp_path):
    """The actual inbox and AgentRQ client, wired as listener.py wires them, over loopback fakes."""
    opened = []
    def make(**host_options):
        host, agentrq = Host(**host_options), AgentRQ()
        plugin = load("agentrq_plugin_v2", "plugin.py")
        client = plugin.AgentRQClient(agentrq.url, "synthetic-token")
        inbox = presence.PresenceInbox(tmp_path, host.url, "synthetic",
                                       lambda task, text: client.call_tool("reply", {"chatId": task, "text": text}))
        opened.append((host, agentrq, inbox))
        return host, agentrq, inbox
    yield make
    for host, agentrq, inbox in opened:
        inbox.close()
        host.close()
        agentrq.close()


@pytest.fixture
def world(make_world):
    return make_world()


def capture(inbox, task, text, sender="human", **extra):
    capture.n = getattr(capture, "n", 0) + 1
    params = {"type": "message.created", "taskId": task, "sender": sender, "text": text,
              "messageId": "msg-%d" % capture.n, **extra}
    event = presence.provider_event(params, WORKSPACE)
    return inbox.enqueue(event), event


def record(path):
    return json.loads(path.read_text())


def elapse(inbox, *paths):
    for path in paths:
        inbox._not_before.pop(path, None)


def test_the_busy_wait_is_the_hosts_own_hint_with_no_growth():
    assert [presence.busy_delay("conversation", 1.0), presence.busy_delay("capacity", 5.0)] == [1.0, 5.0]
    # No hint: the documented 1 s / 5 s; the older in-flight refusal names no field and waits like capacity.
    assert [presence.busy_delay("conversation", None), presence.busy_delay("", None)] == [1.0, 5.0]


def test_version_2_only_when_listed(world):
    host, _agentrq, inbox = world
    path, event = capture(inbox, "task-a", "hello")
    inbox.process_pending()
    assert host.bodies() == [{"event": event, "continuation_version": 2}]
    assert record(path)["refs"] == ["turn-1"]


def test_an_older_host_keeps_the_version_0_request_and_its_terminal_wait(make_world):
    host, agentrq, inbox = make_world(versions=None)
    path, event = capture(inbox, "task-a", "hello")
    worker = threading.Thread(target=inbox.process_pending)
    worker.start()
    host.wait(lambda: host.live.get("task-a") is not None)
    time.sleep(0.1)
    assert worker.is_alive(), "version 0 still waits for the terminal"
    host.finish("turn-1", text="Done", output_ref="out-1")
    worker.join(5)
    inbox.process_pending()
    assert host.bodies() == [{"event": event}]
    assert record(path)["state"] == "completed" and agentrq.replies == [("task-a", "Done")]


def test_capture_order_not_file_names_orders_a_task(world):
    host, _agentrq, inbox = world
    host.busy.add("task-a")
    paths = [capture(inbox, "task-a", "m%d" % n)[0] for n in range(6)]
    assert [record(p)["seq"] for p in paths] == list(range(1, 7))
    assert sorted(paths) != paths  # hash names would reorder them
    host.busy.discard("task-a")
    for _ in range(6):
        elapse(inbox, *paths)
        inbox.process_pending()
    assert host.posts("task-a") == [record(p)["event"]["source_event_id"] for p in paths]


def test_a_task_waits_for_its_early_answer_while_another_task_goes(world):
    host, _agentrq, inbox = world
    host.auto_intake = False
    listener = load("agentrq_listener_v2", "listener.py")
    listener._inbox = inbox
    (a1, e1), (a2, e2), (b1, f1) = capture(inbox, "task-a", "first"), capture(inbox, "task-a", "second"), capture(inbox, "task-b", "other")
    stop = threading.Event()
    loop = threading.Thread(target=listener._intake_loop, args=(stop,), daemon=True)
    loop.start()
    try:
        host.wait(lambda: {e1["source_event_id"], f1["source_event_id"]} <= set(host.posts()))
        time.sleep(0.3)
        assert host.posts("task-a") == [e1["source_event_id"]], "the second event waits for the first one's answer"
        host.authors[host.author("task-a")["ref"]]["intake"].set()
        host.wait(lambda: e2["source_event_id"] in host.posts())
        deadline = time.monotonic() + 5
        while record(a2)["state"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert [e["source_event_id"] for e in host.author("task-a")["context"]] == [e2["source_event_id"]]
        assert record(a1)["refs"] == [host.author("task-a")["ref"]]
        host.authors[host.author("task-b")["ref"]]["intake"].set()
    finally:
        stop.set()
        inbox.wake.set()
        loop.join(5)
    assert not loop.is_alive()


def test_a_busy_task_keeps_its_head_and_identity_while_another_advances(world):
    host, _agentrq, inbox = world
    host.busy.add("task-a")
    (a1, e1), (a2, e2), (b1, _f1) = capture(inbox, "task-a", "first"), capture(inbox, "task-a", "second"), capture(inbox, "task-b", "other")
    inbox.process_pending()
    assert record(b1)["refs"], "another task advanced meanwhile"
    assert inbox._not_before[a1] - time.time() == pytest.approx(1, abs=0.5)
    inbox.process_pending()
    assert host.posts("task-a") == [e1["source_event_id"]]
    for _ in range(3):  # still busy: the same 1 s hint every time, never a growing wait
        elapse(inbox, a1)
        inbox.process_pending()
        assert inbox._not_before[a1] - time.time() == pytest.approx(1, abs=0.5)
    host.busy.discard("task-a")
    elapse(inbox, a1)
    inbox.process_pending()
    inbox.process_pending()
    assert host.posts("task-a") == [e1["source_event_id"]] * 5 + [e2["source_event_id"]]
    assert len({json.dumps(b, sort_keys=True) for b in host.bodies() if b["event"] == e1}) == 1
    assert record(a2)["state"] == "completed" and a1 not in inbox._not_before


def test_full_capacity_waits_while_the_live_author_takes_its_task(make_world):
    host, _agentrq, inbox = make_world(capacity=1)
    a1, _ = capture(inbox, "task-a", "first")
    inbox.process_pending()
    (b1, _f1), (a2, _e2) = capture(inbox, "task-b", "new task"), capture(inbox, "task-a", "later")
    inbox.process_pending()
    assert inbox._not_before[b1] - time.time() == pytest.approx(5, abs=0.5)
    assert record(a2)["state"] == "completed"  # context for the live author: no slot needed
    host.finish(record(a1)["refs"][0])
    elapse(inbox, b1)
    inbox.process_pending()
    assert record(b1)["refs"]


def test_a_flood_reaches_the_live_author_in_order_without_drops(world):
    host, _agentrq, inbox = world
    capture(inbox, "task-a", "start")
    inbox.process_pending()
    flood = [capture(inbox, "task-a", "m%d" % n, sender="human" if n % 2 else "agent")[1] for n in range(15)]
    for _ in range(15):
        inbox.process_pending()
    assert host.posts("task-a")[1:] == [e["source_event_id"] for e in flood]
    assert [e["source_event_id"] for e in host.author("task-a")["context"]] == [e["source_event_id"] for e in flood]
    assert len(host.authors) == 1


def test_a_late_child_and_its_parent_reply_once_each_and_the_anchor_stays_in_the_task(world):
    host, agentrq, inbox = world
    a1, e1 = capture(inbox, "task-a", "question")
    inbox.process_pending()
    a2, e2 = capture(inbox, "task-a", "detail")
    inbox.process_pending()  # context; the author still runs
    ref = record(a1)["refs"][0]
    host.authors[ref]["child"] = "child-1"
    child = {"ok": True, "work_ref": "child-1", "task_id": "child-1", "child_work_ref": "", "outputs": []}
    host.work["child-1"] = [(202, {**child, "status": "pending"}),
                            (200, {**child, "status": "completed", "outcome": "message", "text": "From the child",
                                   "output_ref": "out-child"})]
    inbox.process_pending()
    assert record(a1)["refs"] == [ref, "child-1"]
    inbox.process_pending()
    host.finish(ref, text="From the author", output_ref="out-author", anchor=e2)
    for _ in range(3):
        inbox.process_pending()
    done = record(a1)
    assert done["state"] == "completed" and done["refs"] == [] and sorted(done["ended_refs"]) == sorted([ref, "child-1"])
    assert sorted(agentrq.replies) == [("task-a", "From the author"), ("task-a", "From the child")]
    assert done["outputs"]["out-author"]["anchor"] == e2["source_event_id"]
    assert "anchor" not in done["outputs"]["out-child"]  # a promoted child's output carries none
    assert len(host.polls("child-1")) == 2, "an ended child is not read again"


def test_an_anchor_naming_another_task_is_not_kept_and_the_reply_stays_in_the_task(world):
    host, agentrq, inbox = world
    a1, _e1 = capture(inbox, "task-a", "question")
    inbox.process_pending()
    _b, foreign = capture(inbox, "task-b", "elsewhere")
    host.finish(record(a1)["refs"][0], text="Answer", output_ref="out-1", anchor=foreign)
    inbox.process_pending()
    assert ("task-a", "Answer") in agentrq.replies
    assert "anchor" not in record(a1)["outputs"]["out-1"]


def test_a_stop_sends_nothing_and_never_submits_the_event_again(world):
    host, agentrq, inbox = world
    a1, e1 = capture(inbox, "task-a", "question")
    inbox.process_pending()
    host.finish(record(a1)["refs"][0], status="cancelled")
    for _ in range(3):
        inbox.process_pending()
    assert record(a1)["state"] == "completed" and record(a1)["outputs"] == {}
    assert host.posts() == [e1["source_event_id"]] and agentrq.replies == []


def test_an_unreadable_identity_decides_nothing_and_is_asked_again(world):
    host, _agentrq, inbox = world
    path, event = capture(inbox, "task-a", "hello")
    refusals = [{"ok": False, "code": "presence_auth_blocked", "disposition": "blocked"}]
    real = inbox._request
    def request(method, route, payload=None):
        if route == "/identity" and refusals:
            return refusals.pop()  # the first identity read is refused
        return real(method, route, payload)
    inbox._request = request
    inbox.process_pending()
    assert inbox.continuation is None and host.posts() == []
    elapse(inbox, path)
    inbox.process_pending()
    assert inbox.continuation == 2 and host.bodies() == [{"event": event, "continuation_version": 2}]


def test_a_lost_answer_is_offered_again_unchanged_and_custody_is_taken_once(world):
    host, _agentrq, inbox = world
    (a1, e1), (a2, e2) = capture(inbox, "task-a", "first"), capture(inbox, "task-a", "second")
    real, lost = inbox._request, []
    def request(method, route, payload=None):
        answer = real(method, route, payload)
        if route == "/presence/turn" and not lost:
            lost.append(answer)
            raise ConnectionResetError("the answer was lost after the Host stored it")
        return answer
    inbox._request = request
    inbox.process_pending()
    assert not inbox._admitted(record(a1)) and host.posts() == [e1["source_event_id"]]
    assert inbox._not_before[a1] - time.time() == pytest.approx(5, abs=0.5)
    elapse(inbox, a1)
    inbox.process_pending()
    assert record(a1)["refs"] == [lost[0]["turn_ref"]] and len(host.authors) == 1
    inbox.process_pending()
    assert host.posts() == [e1["source_event_id"], e1["source_event_id"], e2["source_event_id"]]
    assert host.bodies()[0] == host.bodies()[1] and record(a2)["state"] == "completed"


def test_a_failed_terminal_speaks_only_with_an_output_identity(world):
    host, agentrq, inbox = world
    a1, _ = capture(inbox, "task-a", "question")
    inbox.process_pending()
    ref = record(a1)["refs"][0]
    host.authors[ref]["terminal"] = {"ok": True, "status": "failed", "outcome": "message", "text": "Task failed: provider error",
                                     "output_ref": "", "work_ref": ref, "task_id": ref, "child_work_ref": "", "outputs": []}
    inbox.process_pending()
    assert agentrq.replies == [] and record(a1)["state"] == "completed"


def until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def listener_running(monkeypatch, tmp_path, host, agentrq, notifications):
    """The actual listener.main(), its SSE loop replaced: notifications arrive, then a stop.

    Returns (main thread, stop trigger, listener module, outcome). The stand-in loop exits the
    way the SIGTERM handler makes it exit (SystemExit), so main's own shutdown path runs.
    """
    listener = load("agentrq_listener_lifecycle_%d" % len(notifications), "listener.py")
    for name, value in {"MCP_URL": agentrq.url, "AUTH_TOKEN": "synthetic-token", "SKILL_TOKEN": "synthetic",
                        "HOST_BASE": host.url, "_inbox": None}.items():
        monkeypatch.setattr(listener, name, value)
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path / "listener-state"))
    stop = threading.Event()

    def listen():
        for params in notifications:
            listener._handle_event({"method": "notifications/message", "params": params})
        assert stop.wait(60), "the test never stopped the listener"
        raise SystemExit(0)

    monkeypatch.setattr(listener, "listen_loop", listen)
    outcome = {}

    def run():
        try:
            listener.main()
        except SystemExit as exc:
            outcome["exit"] = exc.code
    thread = threading.Thread(target=run, name="listener-main", daemon=True)
    thread.start()
    assert until(lambda: listener._inbox is not None, 10)
    return thread, stop, listener, outcome


def notification(task, n, text):
    return {"type": "message.created", "taskId": task, "sender": "human", "text": text, "messageId": "lc-%s-%d" % (task, n)}


def stored(tmp_path):
    return {record(path)["event"]["text"]: record(path) for path in (tmp_path / "listener-state" / "presence").glob("*.json")}


def test_a_stop_joins_the_held_submission_and_starts_no_new_work(make_world, tmp_path, monkeypatch):
    host, agentrq, _inbox = make_world()
    held = presence.provider_event(notification("task-a", 1, "held at the Host"), agentrq.url)["source_event_id"]
    host.hold[held] = gate = threading.Event()
    thread, stop, listener, outcome = listener_running(monkeypatch, tmp_path, host, agentrq, [
        notification("task-a", 1, "held at the Host"), notification("task-b", 1, "answered at once")])
    inbox = listener._inbox
    assert until(lambda: held in host.posts() and len(host.posts()) == 2, 15)
    stop.set()  # the companion is told to stop while one submission is in flight
    time.sleep(0.3)
    inbox.enqueue(presence.provider_event(notification("task-c", 1, "arrived during the stop"), listener.MCP_URL))
    time.sleep(0.5)
    assert thread.is_alive(), "main returned before joining its intake submission"
    gate.set()  # the held answer arrives
    thread.join(30)
    assert not thread.is_alive() and outcome == {"exit": 0}
    assert [t.name for t in threading.enumerate() if t.name.startswith("agentrq-presence")] == [], \
        "no intake or observation thread outlives the stop"
    records = stored(tmp_path)
    assert records["held at the Host"]["refs"], "the held submission's answer is kept"
    assert not inbox._admitted(records["arrived during the stop"]), "a later event stays for the next start"
    time.sleep(0.3)
    assert sorted(host.posts()) == sorted([held, records["answered at once"]["event"]["source_event_id"]]), \
        "nothing new was submitted after the stop"


def test_a_stop_during_a_reply_keeps_it_sending_and_starts_no_other_reply(make_world, tmp_path, monkeypatch):
    host, agentrq, _inbox = make_world()
    agentrq.hold = threading.Event()
    thread, stop, listener, outcome = listener_running(monkeypatch, tmp_path, host, agentrq, [
        notification("task-a", 1, "first"), notification("task-b", 1, "second")])
    assert until(lambda: len(host.posts()) == 2 and len(host.authors) == 2, 15)
    for author in list(host.authors.values()):
        host.finish(author["ref"], text="Answer for " + author["task"], output_ref="out-" + author["task"])
    assert until(lambda: len(agentrq.replies) == 1, 20), "the observation loop sends the first reply"
    stop.set()
    thread.join(20)
    assert not thread.is_alive() and outcome == {"exit": 0}
    states = sorted(r["state"] for r in stored(tmp_path).values())
    assert "sending" in states and states.count("sending") == 1, states  # the unknown outcome stays on disk
    agentrq.hold.set()  # the in-flight reply's answer arrives after the stop
    assert until(lambda: "sending" not in [r["state"] for r in stored(tmp_path).values()], 10)
    time.sleep(0.5)
    assert len(agentrq.replies) == 1, "no other reply started after the stop"
    assert [t.name for t in threading.enumerate() if t.name.startswith("agentrq-presence")] == []


def stopping_inbox(tmp_path, hold, answer):
    """An inbox over a scripted Host whose read of ``hold`` blocks until released.

    Every call records whether the stop had already arrived when the call started.
    """
    arrived, release = threading.Event(), threading.Event()
    calls, effects = [], []
    inbox = presence.PresenceInbox(tmp_path, "http://unused.invalid", "synthetic",
                                   lambda task, text: effects.append((task, text)) or {"ok": True})

    def request(method, route, payload=None):
        calls.append((method, route.split("?")[0], inbox.closed.is_set()))
        if route.startswith(hold):
            arrived.set()
            assert release.wait(10)
        return answer(route)
    inbox._request = request
    return inbox, calls, effects, arrived, release


def scripted(route):
    if route == "/identity":
        return {"ok": True, "presence_continuation_versions": [1, 2]}
    if route == "/presence/turn":
        return {"ok": True, "status": "completed", "outcome": "message", "text": "answer", "output_ref": "out-1"}
    return {"state": "active"}


def test_a_stop_while_the_hosts_identity_is_read_submits_nothing(tmp_path):
    inbox, calls, effects, arrived, release = stopping_inbox(tmp_path, "/identity", scripted)
    path, _event = capture(inbox, "task-a", "hello")
    inbox.intake_pending()
    assert arrived.wait(5)
    closing = threading.Thread(target=inbox.close)
    closing.start()
    assert inbox.closed.wait(5)
    release.set()  # the identity answer arrives after the stop
    closing.join(10)
    assert not closing.is_alive()
    assert [call for call in calls if call[1] == "/presence/turn"] == [], calls
    assert not inbox._admitted(record(path)), "the event stays, unchanged, for the next start"


def test_a_stop_while_the_place_is_read_sends_no_reply_and_the_next_start_replies_once(tmp_path):
    inbox, calls, effects, arrived, release = stopping_inbox(tmp_path, "/presence/place", scripted)
    path, _event = capture(inbox, "task-a", "hello")
    running = threading.Thread(target=inbox.process, args=(path,))
    running.start()
    assert arrived.wait(5)
    inbox.close()
    release.set()  # the place answers active after the stop
    running.join(10)
    assert not running.is_alive() and effects == []
    kept = record(path)
    assert kept["outputs"]["out-1"]["state"] == "queued" and kept["state"] == "pending", kept
    fresh = presence.PresenceInbox(tmp_path, "http://unused.invalid", "synthetic",
                                   lambda task, text: effects.append((task, text)) or {"ok": True})
    fresh._request = lambda method, route, payload=None: scripted(route)
    fresh.process(path)
    fresh.process(path)
    assert effects == [("task-a", "answer")] and record(path)["state"] == "completed"


def test_a_stop_during_one_reference_read_reads_no_later_reference(tmp_path):
    def answer(route):
        if route == "/presence/turn":  # retained: the turn and a real child to read
            return {"ok": False, "disposition": "retained", "turn_ref": "turn-1", "work_ref": "child-1"}
        if route == "/presence/work/turn-1":
            return {"ok": True, "status": "pending", "work_ref": "turn-1", "child_work_ref": "child-1",
                    "outputs": [{"output_ref": "released-1", "outcome": "deferred", "text": "Working on it."}]}
        if route == "/presence/work/child-1":
            return {"ok": True, "status": "completed", "outcome": "message", "text": "From the child",
                    "output_ref": "out-child"}
        return scripted(route)
    inbox, calls, effects, arrived, release = stopping_inbox(tmp_path, "/presence/work/turn-1", answer)
    path, _event = capture(inbox, "task-a", "hello")
    inbox.submit(path)
    assert record(path)["refs"] == ["turn-1", "child-1"]
    running = threading.Thread(target=inbox.observe, args=(path,))
    running.start()
    assert arrived.wait(5)
    inbox.close()
    release.set()  # the first reference answers after the stop
    running.join(10)
    assert not running.is_alive()
    reads = [route for _method, route, _closed in calls if route.startswith("/presence/work/")]
    assert reads == ["/presence/work/turn-1"], "no later reference is read after the stop"
    kept = record(path)
    assert kept["refs"] == ["turn-1", "child-1"], "nothing is dropped: the next start reads both again"
    assert kept["outputs"]["released-1"]["state"] == "queued", "the answer already read is kept"
    assert effects == [] and not [route for _m, route, _c in calls if route.startswith("/presence/place")]
