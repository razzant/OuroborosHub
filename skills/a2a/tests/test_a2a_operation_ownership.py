"""Current-message races and real SDK durable recovery (SDK optional in CI)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading
import uuid
import urllib.parse

import httpx
import pytest
from starlette.testclient import TestClient


def load_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("A2A_TOOLS_REFRESHER", "0")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=(
        {"name": "fixture", "description": "fixture"} if url.endswith("/identity")
        else {"tools": [{"function": {"name": "read_file", "description": "read"}}]} if url.endswith("/tools/schemas")
        else {"ok": True, "status": "pending", "operation_ref": url.rsplit("/", 1)[-1]}
    ), request=httpx.Request("GET", url)))
    path = Path(__file__).resolve().parents[1] / "scripts/a2a_daemon.py"
    spec = importlib.util.spec_from_file_location("a2a_owned_" + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atomic_binding_and_old_message_cannot_reset_current(tmp_path, monkeypatch):
    daemon = load_daemon(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        bindings = list(pool.map(lambda _: daemon._bind_inbound_message_sync("task", "ctx", "one"), range(2)))
    assert bindings[0] == bindings[1]
    first = bindings[0][1]
    daemon._update_task_record("task", state="completed", expected_message_id=first)
    _, second = daemon._bind_inbound_message_sync("task", "ctx", "two")
    daemon._bind_inbound_message_sync("task", "ctx", "one")
    daemon._update_task_record("task", state="completed", expected_message_id=first)
    record = daemon._load_task("task")
    assert record["ouroboros"]["client_message_id"] == second
    assert record["status"]["state"] == "working"
    assert daemon._client_message_id("a:b", "c") != daemon._client_message_id("a", "b:c")
    assert len(daemon._client_message_id("long" * 100, "id" * 100)) <= 128


@pytest.mark.parametrize("method,status", [("tasks/get", "completed"), ("tasks/get", "failed"), ("message/send", "completed")])
def test_late_host_outcome_cannot_overwrite_current_message(tmp_path, monkeypatch, method, status):
    daemon = load_daemon(tmp_path, monkeypatch)
    daemon._A2A_SDK_AVAILABLE = False
    _, first = daemon._bind_inbound_message_sync("task", "ctx", "one")
    entered, release = threading.Event(), threading.Event()
    def late_read(ref):
        assert ref == first
        entered.set()
        assert release.wait(5)
        return {"status": status, "text": "old answer", "operation_ref": ref}
    def late_dispatch(*args):
        late_read(first)
        return "old answer"
    monkeypatch.setattr(daemon, "_read_operation_sync", late_read)
    monkeypatch.setattr(daemon, "_dispatch_after_allocate_sync", late_dispatch)
    params = {"id": "task"} if method == "tasks/get" else {"message": {
        "taskId": "task", "contextId": "ctx", "messageId": "one", "parts": [{"kind": "text", "text": "first"}]}}
    with TestClient(daemon._build_app()) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, "/", json={"method": method, "params": params})
        try:
            assert entered.wait(5)
            _, second = daemon._bind_inbound_message_sync("task", "ctx", "two")
            before = daemon._task_path("task").read_bytes()
        finally:
            release.set()
        result = pending.result(timeout=5).json()["result"]
    assert result["ouroboros"]["client_message_id"] == second
    assert result["status"]["state"] == "working" and not result.get("artifacts")
    assert daemon._task_path("task").read_bytes() == before
    daemon._update_task_record("task", state="completed", expected_message_id=second,
                               artifacts=[{"parts": [{"kind": "text", "text": "new answer"}]}])
    assert daemon._load_task("task")["status"]["state"] == "completed"


def test_restart_polls_retained_presence_ref_without_resubmission(tmp_path, monkeypatch):
    daemon = load_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: httpx.Response(200,
        json={"status": "unresolved", "turn_ref": "turn/one"}, request=httpx.Request("POST", url)))
    conversation, source = daemon._bind_inbound_message_sync("task", "ctx", "one")
    with pytest.raises(daemon._HostWaitExpired):
        daemon._inject_sync(conversation, "hello", source)
    restarted = load_daemon(tmp_path, monkeypatch)
    requests = []
    def get(url, **kw):
        requests.append(url)
        return httpx.Response(200, json={"status": "completed", "outcome": "message", "text": "late answer"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(restarted.httpx, "get", get)
    monkeypatch.setattr(restarted.httpx, "post", lambda *a, **kw: pytest.fail("no resubmission"))
    assert restarted._refresh_task_record("task")["artifacts"][0]["parts"][0]["text"] == "late answer"
    assert requests == [restarted.HOST_SERVICE_URL + "/presence/work/turn%2Fone"]


@pytest.mark.parametrize("sdk", [False, True])
def test_wait_expiry_and_cancel_refusal_keep_task_working(tmp_path, monkeypatch, sdk):
    if sdk:
        pytest.importorskip("a2a")
    daemon = load_daemon(tmp_path, monkeypatch)
    if not sdk:
        daemon._A2A_SDK_AVAILABLE = False
    def expire(*args):
        raise daemon._HostWaitExpired("Host work may still be running")
    monkeypatch.setattr(daemon, "_dispatch_after_allocate_sync", expire)
    with TestClient(daemon._build_app()) as client:
        result = client.post("/", json={"method": "message/send", "params": {"message": {
            "role": "user", "messageId": "m", "parts": [{"kind": "text", "text": "hello"}]}}}).json()["result"]
        assert result["status"]["state"] == "working"
        refused = client.post("/", json={"method": "tasks/cancel", "params": {"id": result["id"]}}).json()
        assert "error" in refused and "result" not in refused
        assert daemon._load_task(result["id"])["status"]["state"] == "working"


def _stored_sdk_tasks(tmp_path, monkeypatch):
    pytest.importorskip("a2a", reason="actual SDK controls use the isolated acceptance environment")
    from a2a.server.context import ServerCallContext
    from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

    daemon = load_daemon(tmp_path, monkeypatch)
    assert daemon._A2A_SDK_AVAILABLE
    store, context, refs = daemon._sdk_task_store(), ServerCallContext(), {}
    for index in range(2):
        task_id = f"stored-{index}"
        client_id = daemon._client_message_id(task_id, "m")
        refs[task_id] = daemon._operation_ref(-61 - index, client_id)
        daemon._update_task_record(task_id, "ctx", binding={
            "conversation_id": "ctx", "protocol": "presence", "client_message_id": client_id, "operation_ref": refs[task_id],
        })
        asyncio.run(store.save(Task(id=task_id, context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            history=[Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="request")])]), context))
    return daemon, store, context, refs


@pytest.mark.parametrize("sdk", [False, True])
def test_lost_host_operation_reconciles_to_failed_with_its_reason(tmp_path, monkeypatch, sdk):
    reason = "the host restarted before answering; the message was not carried over"
    if sdk:
        daemon, _, _, refs = _stored_sdk_tasks(tmp_path, monkeypatch)
        task_id, ref = "stored-0", refs["stored-0"]
        record = daemon._load_task(task_id)
        record["sdk_task"]["status"]["message"] = {
            "role": "ROLE_AGENT", "messageId": "old-note", "parts": [{"text": "still waiting"}],
        }
        daemon._save_task(record)
    else:
        daemon = load_daemon(tmp_path, monkeypatch)
        daemon._A2A_SDK_AVAILABLE = False
        _, message_id = daemon._bind_inbound_message_sync("stored-0", "ctx", "m")
        task_id, ref = "stored-0", daemon._operation_ref(-61, message_id)
    app = daemon.app if sdk else daemon._build_app()
    requests, dispatched = [], []

    def host_get(url, **kwargs):
        requests.append(url)
        assert urllib.parse.unquote(url).endswith(ref)
        return httpx.Response(200, json={"ok": True, "operation_ref": ref, "status": "lost",
            "reason": "host_restarted_before_answer"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(daemon, "_read_operation_sync", lambda ref: host_get(
        daemon.HOST_SERVICE_URL + "/presence/work/" + urllib.parse.quote(ref, safe="")).json())
    monkeypatch.setattr(daemon, "_dispatch_after_allocate_sync", lambda *a: dispatched.append(a) or "unexpected")
    with TestClient(app) as client:
        response = client.post("/", json={"jsonrpc": "2.0", "id": "get-lost", "method": "tasks/get",
                                         "params": {"id": task_id}}).json()
        assert response["id"] == "get-lost"
        task = response["result"]
        assert task["status"]["state"] == "failed"
        assert task["status"]["message"]["parts"][0]["text"] == reason
        assert not task.get("artifacts") and len(requests) == 1
        saved = daemon._task_path(task_id).read_bytes()
        if sdk:
            listed = client.post("/", headers={"A2A-Version": "1.0"}, json={
                "jsonrpc": "2.0", "id": "list-lost", "method": "ListTasks", "params": {},
            }).json()
            lost = next(item for item in listed["result"]["tasks"] if item["id"] == task_id)
            assert lost["status"]["state"] == "TASK_STATE_FAILED"
            assert lost["status"]["message"]["parts"][0]["text"] == reason
            assert len(requests) == 1 and daemon._task_path(task_id).read_bytes() == saved
            replay = client.post("/", json={"jsonrpc": "2.0", "id": "replay-lost", "method": "message/send", "params": {
                "message": {"role": "user", "taskId": task_id, "messageId": "m",
                            "parts": [{"kind": "text", "text": "request"}]},
            }}).json()["result"]
            assert replay["status"]["state"] == "failed" and not replay.get("artifacts")
            assert replay["status"]["message"]["parts"][0]["text"] == reason
        assert dispatched == []
    with pytest.raises(RuntimeError, match=reason):
        daemon._wait_final_after_expiry_sync(-61, daemon.time.monotonic() + 1, ref)


def test_real_sdk_lost_old_message_replay_does_not_change_current_message(tmp_path, monkeypatch):
    daemon, store, context, refs = _stored_sdk_tasks(tmp_path, monkeypatch)
    from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

    task_id, old_ref = "stored-0", refs["stored-0"]
    _, current_id = daemon._bind_inbound_message_sync(task_id, "ctx", "m2")
    asyncio.run(store.save(Task(id=task_id, context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        history=[Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="request")]),
                 Message(message_id="m2", role=Role.ROLE_USER, parts=[Part(text="next request")])]), context))
    before = daemon._task_path(task_id).read_bytes()
    requests, dispatched, complete = [], [], False

    def host_get(url, **kwargs):
        requests.append(url)
        ref = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        if ref == old_ref:
            view = {"status": "lost", "reason": "host_restarted_before_answer"}
        else:
            assert ref == daemon._operation_ref(-61, current_id)
            view = {"status": "completed", "outcome": "message", "text": "answer to M2"} if complete else {"status": "pending"}
        return httpx.Response(200, json={"ok": True, "operation_ref": ref, **view}, request=httpx.Request("GET", url))

    monkeypatch.setattr(daemon, "_read_operation_sync", lambda ref: host_get(
        daemon.HOST_SERVICE_URL + "/presence/work/" + urllib.parse.quote(ref, safe="")).json())
    monkeypatch.setattr(daemon, "_dispatch_after_allocate_sync", lambda *a: dispatched.append(a) or "unexpected")
    with TestClient(daemon.app) as client:
        old = client.post("/", json={"jsonrpc": "2.0", "id": "old", "method": "message/send", "params": {
            "message": {"role": "user", "taskId": task_id, "messageId": "m",
                        "parts": [{"kind": "text", "text": "request"}]},
        }}).json()["result"]
        assert old["status"]["state"] == "failed" and not old.get("artifacts")
        assert old["status"]["message"]["parts"][0]["text"] == (
            "the host restarted before answering; the message was not carried over"
        )
        assert [message["messageId"] for message in old["history"]] == ["m"]
        assert daemon._task_path(task_id).read_bytes() == before
        assert len(requests) == 2 and dispatched == []
        complete = True
        current = client.post("/", json={"jsonrpc": "2.0", "id": "current", "method": "tasks/get",
                                        "params": {"id": task_id}}).json()["result"]
        assert current["status"]["state"] == "completed"
        assert current["artifacts"][0]["parts"][0]["text"] == "answer to M2"
        record = daemon._load_task(task_id)
        assert record["ouroboros"]["client_message_id"] == current_id
        assert record["status"]["state"] == "completed" and dispatched == []


def test_sdk_list_reads_local_snapshots_off_loop_and_get_refreshes_one_task(tmp_path, monkeypatch):
    daemon, store, context, refs = _stored_sdk_tasks(tmp_path, monkeypatch)
    from a2a.types import ListTasksRequest, TaskState

    paths = list(daemon._tasks_dir().glob("*.json"))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    requests, read_threads = [], []
    read_text = Path.read_text

    def observe_read(path, *args, **kwargs):
        if path in before:
            read_threads.append(threading.get_ident())
        return read_text(path, *args, **kwargs)

    def host_get(url, **kwargs):
        requests.append(url)
        assert urllib.parse.unquote(url).endswith(refs["stored-0"])
        return httpx.Response(200, json={"ok": True, "operation_ref": refs["stored-0"],
            "status": "completed", "outcome": "message", "text": "one refreshed answer"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(Path, "read_text", observe_read)
    monkeypatch.setattr(daemon, "_read_operation_sync", lambda ref: host_get(
        daemon.HOST_SERVICE_URL + "/presence/work/" + urllib.parse.quote(ref, safe="")).json())

    async def exercise():
        loop_thread = threading.get_ident()
        first = await store.list(ListTasksRequest(page_size=1), context)
        assert first.total_size == 2 and len(first.tasks) == 1 and first.next_page_token
        assert len(read_threads) == 2 and all(t != loop_thread for t in read_threads)
        second = await store.list(ListTasksRequest(page_size=1, page_token=first.next_page_token), context)
        assert {first.tasks[0].id, second.tasks[0].id} == set(refs)
        assert all(task.status.state == TaskState.TASK_STATE_WORKING for task in [*first.tasks, *second.tasks])
        assert requests == []
        assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
        refreshed = await store.get("stored-0", context)
        assert refreshed.status.state == TaskState.TASK_STATE_COMPLETED
        assert refreshed.artifacts[0].parts[0].text == "one refreshed answer"
        assert requests == [daemon.HOST_SERVICE_URL + "/presence/work/" + urllib.parse.quote(refs["stored-0"], safe="")]

    asyncio.run(exercise())
    assert daemon._load_task("stored-0")["status"]["state"] == "completed"
    assert daemon._task_path("stored-1").read_bytes() == before[daemon._task_path("stored-1")][0]


def test_sdk_local_list_keeps_owner_filter_without_remote_reads(tmp_path, monkeypatch):
    daemon, store, context, _ = _stored_sdk_tasks(tmp_path, monkeypatch)
    from a2a.types import ListTasksRequest

    foreign = daemon._load_task("stored-1")
    foreign["sdk_owner"] = "another-owner"
    daemon._save_task(foreign)
    requests = []
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **kw: requests.append(a) or None)
    listed = asyncio.run(store.list(ListTasksRequest(), context))
    assert [task.id for task in listed.tasks] == ["stored-0"] and listed.total_size == 1
    assert requests == []
    assert daemon._load_task("stored-1") == foreign


@pytest.mark.parametrize("raw", ["{broken json", "[]", '{"id":"other"}'])
def test_sdk_list_and_get_report_corrupt_state_without_dropping_records(tmp_path, monkeypatch, raw):
    daemon, store, context, _ = _stored_sdk_tasks(tmp_path, monkeypatch)
    from a2a.types import ListTasksRequest

    path = daemon._task_path("bad")
    path.write_text(raw, encoding="utf-8")
    before = {p: p.read_bytes() for p in daemon._tasks_dir().glob("*.json")}
    assert daemon._load_task("missing") is None
    for call in [lambda: daemon._load_task("bad"), lambda: asyncio.run(store.get("bad", context)),
                 lambda: asyncio.run(store.list(ListTasksRequest(), context))]:
        with pytest.raises(daemon._TaskStateCorrupt):
            call()
    requests = []
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **kw: requests.append(a) or None)
    with TestClient(daemon.app, headers={"A2A-Version": "1.0"}) as client:
        for method, params in [("ListTasks", {}), ("GetTask", {"id": "bad"})]:
            response = client.post("/", json={"jsonrpc": "2.0", "id": method, "method": method, "params": params}).json()
            assert response["id"] == method and response["error"]["code"] == -32603
            assert "task state" in response["error"]["message"] and "result" not in response
    assert requests == []
    assert {p: p.read_bytes() for p in daemon._tasks_dir().glob("*.json")} == before
