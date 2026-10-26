import asyncio
import importlib.util
import sys
import pytest
from pathlib import Path


def _load_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOST_SERVICE_TOKEN", "token")
    # Keep the module-level app build from spawning background tool pollers
    # (they would outlive the test and poke whatever occupies the host port).
    monkeypatch.setenv("A2A_TOOLS_REFRESHER", "0")
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "a2a_daemon.py"
    spec = importlib.util.spec_from_file_location(f"a2a_daemon_test_{id(tmp_path)}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_dispatch_sends_peer_text_to_presence(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp(200, {"status": "completed", "outcome": "message", "text": "ok"})
    monkeypatch.setattr(daemon.httpx, "post", post)
    assert asyncio.run(daemon._dispatch_to_host("/panic", task_id="task", context_id="ctx", message_id="m")) == "ok"
    assert len(calls) == 1 and calls[0][0].endswith("/presence/turn")
    event = calls[0][1]["json"]["event"]
    assert event["text"] == "/panic" and event["conversation_id"] == "ctx"
    assert event["facts"] == ["direct_message", "from_bot"]
    assert calls[0][1]["timeout"] == daemon.A2A_RESPONSE_TIMEOUT_SEC + 15


def test_dispatch_applies_backpressure(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)

    async def run():
        daemon._A2A_SEMAPHORE = asyncio.Semaphore(1)
        await daemon._A2A_SEMAPHORE.acquire()
        try:
            try:
                await daemon._dispatch_to_host("hello")
            except RuntimeError as exc:
                assert "busy" in str(exc)
            else:
                raise AssertionError("busy dispatch was not rejected")
        finally:
            daemon._A2A_SEMAPHORE.release()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8767",
        "http://localhost:8767",
        "http://[::1]:8767",
    ],
)
def test_host_service_loopback_urls_are_allowed(tmp_path, monkeypatch, url):
    monkeypatch.setenv("HOST_SERVICE_URL", url)
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon._is_loopback(daemon._host_service_hostname(url)) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8767@evil.example",
        "http://evil.example:8767",
    ],
)
def test_host_service_url_rejects_non_loopback_and_userinfo(tmp_path, monkeypatch, url):
    monkeypatch.setenv("HOST_SERVICE_URL", url)
    with pytest.raises(RuntimeError):
        _load_daemon(tmp_path, monkeypatch)


def test_task_ids_that_sanitize_alike_do_not_share_a_record(tmp_path, monkeypatch):
    """The id -> filename mapping must be injective.

    Stripping non-alphanumerics mapped "job/a" and "joba" onto one state file, so
    one peer's task could overwrite or return another's record.
    """
    daemon = _load_daemon(tmp_path, monkeypatch)

    assert daemon._task_path("job/a") != daemon._task_path("joba")

    daemon._save_task({"id": "job/a", "status": {"state": "completed"}})
    daemon._save_task({"id": "joba", "status": {"state": "failed"}})

    first = daemon._load_task("job/a")
    second = daemon._load_task("joba")
    assert first["status"]["state"] == "completed"
    assert second["status"]["state"] == "failed"
    # The untouched original id survives inside the record.
    assert first["id"] == "job/a"
    assert second["id"] == "joba"


def test_empty_task_id_maps_deterministically(tmp_path, monkeypatch):
    """A random uuid fallback made _load_task("") unable to find its own write."""
    daemon = _load_daemon(tmp_path, monkeypatch)

    assert daemon._task_path("") == daemon._task_path("")
    daemon._save_task({"id": "", "status": {"state": "completed"}})
    assert daemon._load_task("")["status"]["state"] == "completed"


def test_corrupt_task_record_raises_typed_error(tmp_path, monkeypatch):
    """A present-but-unreadable record is not "task not found"."""
    daemon = _load_daemon(tmp_path, monkeypatch)

    path = daemon._task_path("broken")
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(daemon._TaskStateCorrupt):
        daemon._load_task("broken")

    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(daemon._TaskStateCorrupt):
        daemon._load_task("broken")


def test_malformed_port_setting_does_not_break_import(tmp_path, monkeypatch):
    """A bare int() at module scope crash-looped the companion before `app` existed."""
    monkeypatch.setenv("A2A_PORT", "not-a-port")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon.A2A_PORT == 18800


def test_out_of_range_port_setting_is_clamped(tmp_path, monkeypatch):
    """An out-of-range port reached uvicorn and prevented startup."""
    monkeypatch.setenv("A2A_PORT", "99999")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert 1 <= daemon.A2A_PORT <= 65535


def test_non_dict_settings_document_falls_back_to_defaults(tmp_path, monkeypatch):
    """A wrong-shaped settings file must behave like an unreadable one, not like
    "never configured"."""
    (tmp_path / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon._SETTINGS == {}
    assert daemon.A2A_PORT == 18800


def test_response_timeout_is_clamped_below_the_stream_deadline(tmp_path, monkeypatch):
    """A response wait longer than the total deadline blocked for the full socket
    wait and then gave up immediately."""
    monkeypatch.setenv("A2A_RESPONSE_TIMEOUT_SEC", "1740")
    monkeypatch.setenv("A2A_STREAM_DEADLINE_SEC", "60")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon.A2A_RESPONSE_TIMEOUT_SEC + 15 <= daemon.A2A_STREAM_DEADLINE_SEC


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_message_send_binds_presence_and_replay_keeps_source(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return _Resp(200, {"status": "completed", "outcome": "message", "text": "ok"})
    monkeypatch.setattr(daemon.httpx, "post", post)
    def send(message_id):
        return asyncio.run(daemon._dispatch_to_host("hello", task_id="task", context_id="ctx", message_id=message_id))
    assert send("one") == send("one") == "ok" and len(calls) == 1
    assert send("two") == "ok" and len(calls) == 2
    bound = daemon._load_task("task")["ouroboros"]
    assert bound["protocol"] == "presence" and bound["conversation_id"] == "ctx"
    assert "chat_id" not in bound
    assert calls[0]["event"]["source_event_id"] != calls[1]["event"]["source_event_id"]


def test_cancellation_is_honestly_unsupported_on_presence(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    daemon = _load_daemon(tmp_path, monkeypatch)
    daemon._A2A_SDK_AVAILABLE = False
    monkeypatch.setattr(daemon.httpx, "post", lambda *a, **kw: pytest.fail("no owner-chat cancel endpoint"))
    with pytest.raises(daemon._HostCancelRefused, match="no host operation"):
        daemon._cancel_host_operation_sync("unknown")
    daemon._bind_inbound_message_sync("task", "ctx", "m")
    with TestClient(daemon._build_app()) as client:
        missing = client.post("/", json={"method": "tasks/cancel", "params": {"id": "unknown"}}).json()
        assert missing["error"]["code"] == -32001
        refused = client.post("/", json={"method": "tasks/cancel", "params": {"id": "task"}}).json()
        assert refused["error"]["code"] == -32002 and "no cancellation endpoint" in refused["error"]["message"]
        assert daemon._load_task("task")["status"]["state"] == "working"
        unknown = client.post("/", json={"method": "tasks/nope", "params": {}}).json()
        assert unknown["error"]["code"] == -32601


def test_sdk_cancellation_refusal_does_not_publish_canceled(tmp_path, monkeypatch):
    from types import SimpleNamespace
    daemon = _load_daemon(tmp_path, monkeypatch)
    daemon._bind_inbound_message_sync("task", "ctx", "m")
    with pytest.raises(daemon.TaskNotCancelableError, match="no cancellation endpoint"):
        asyncio.run(daemon.OuroborosExecutor().cancel(SimpleNamespace(task_id="task", context_id="ctx"), []))
    assert daemon._load_task("task")["status"]["state"] == "working"


def test_legacy_message_is_not_resubmitted_under_place_authority(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)
    old = daemon._client_message_id("task", "old")
    daemon._update_task_record("task", "ctx", binding={"chat_id": -1, "client_message_id": old, "operation_ref": "old"})
    before = daemon._task_path("task").read_bytes()
    with pytest.raises(daemon._HostMessageConflict, match="Legacy"):
        daemon._bind_inbound_message_sync("task", "ctx", "old")
    assert daemon._task_path("task").read_bytes() == before
    assert daemon._bind_inbound_message_sync("task", "ctx", "new")[0] == "ctx"
