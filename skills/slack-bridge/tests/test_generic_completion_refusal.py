"""slack_api refuses files.completeUploadExternal; every other method behaves as before.

This method is reserved for the dedicated tool: it shares
already uploaded files, with a comment, into conversations, while the tool's declaration
did not present that as a message. Files are shared through slack_file_upload. These
cases run the registered tool and the outbound worker with the real SlackClient, whose
HTTP is recorded, so "refused" also means no provider request.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import httpx
import pytest

from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient, SlackMethodReserved, refuse_reserved_method
from lib.store import BridgeStore

SKILL_ROOT = Path(__file__).resolve().parents[1]
_SPELLINGS = ["files.completeUploadExternal", "/api/files.completeUploadExternal", "files.completeUploadExternal/",
              "FILES.COMPLETEUPLOADEXTERNAL", "files.CompleteUploadExternal"]


def _load_plugin():
    package = types.ModuleType("slack_bridge_test")
    package.__path__ = [str(SKILL_ROOT)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location("slack_bridge_test.plugin", SKILL_ROOT / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Api:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir, self.tools = state_dir, {}

    def get_state_dir(self):
        return str(self.state_dir)

    def get_settings(self, keys):
        return {key: "" for key in keys}

    def register_tool(self, name, handler, **metadata):
        self.tools[name] = (handler, metadata)

    def __getattr__(self, name):
        if name.startswith("register_"):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


def _provider(calls):
    def answer(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True, "file": {"id": "F1"}, "file_id": "F9",
                                         "upload_url": "https://files.slack.com/upload/v1/x"})
    return answer


def _registered(tmp_path: Path, calls: list) -> _Api:
    module = _load_plugin()

    class _Recording(module.SlackClient):
        """The real client with recorded HTTP in place of the network."""

        def __init__(self, *_args, **kwargs):
            kwargs.pop("http_client", None)
            super().__init__("xoxb-test", "xapp-test",
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(_provider(calls))), **kwargs)

        async def __aexit__(self, *_exc):
            await self._http.aclose()

    module.SlackClient = _Recording
    api = _Api(tmp_path)
    module.register(api)
    return api


def _outbox_rows(tmp_path: Path) -> int:
    """Queued rows; a refusal before the store was ever opened leaves no database at all."""
    path = tmp_path / "slack_bridge.sqlite3"
    if not path.exists():
        return 0
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


@pytest.mark.parametrize("path", _SPELLINGS)
@pytest.mark.parametrize("method,effect", [("POST", "write"), ("GET", "write"), ("GET", "read"), ("POST", "read")])
def test_generic_tool_refuses_upload_completion_before_any_request(tool_json, tmp_path, path, method, effect):
    calls = []
    handler = _registered(tmp_path, calls).tools["slack_api"][0]
    result = tool_json(asyncio.run(handler(
        method=method, effect=effect, path=path, request_id="complete-1",
        body={"files": [{"id": "F1"}, {"id": "F2"}], "channel_id": "C1", "initial_comment": "hello"},
        params={"files": "F1", "channel_id": "C1"},
    )))
    assert result["ok"] is False and result["error"]["code"] == "reserved_for_slack_file_upload"
    message = result["error"]["message"]
    assert "slack_file_upload" in message and "slack_receipt" in message
    # The refusal points at the dedicated tool, not at assembling the upload by hand.
    assert "getUploadURLExternal" not in message and "upload_url" not in message
    assert calls == [] and _outbox_rows(tmp_path) == 0


@pytest.mark.parametrize("path,method,effect", [
    ("files.getUploadURLExternal", "POST", "write"),  # Slot allocation alone shares nothing.
    ("files.info", "GET", "read"),
    ("chat.postMessage", "POST", "write"),
    ("files.completeUploadExternalV2", "POST", "write"),  # Only the exact method is closed.
])
def test_other_generic_methods_are_unchanged(tool_json, tmp_path, path, method, effect):
    calls = []
    api = _registered(tmp_path, calls)
    result = tool_json(asyncio.run(api.tools["slack_api"][0](
        method=method, effect=effect, path=path, request_id="other-1",
        params={"file": "F1"}, body={"channel": "C1", "text": "hi", "filename": "a.txt", "length": 1})))
    assert result["ok"] is True
    if effect == "read":
        assert result["state"] == "read" and calls == [("GET", f"/api/{path}")]
        return
    assert result["state"] == "queued" and calls == []

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_provider(calls))) as http:
            worker = OutboundWorker(BridgeStore(tmp_path), SlackClient("xoxb-test", "xapp-test", http_client=http))
            assert await worker.process_once()
            await worker.aclose()

    asyncio.run(deliver())
    assert calls == [(method, f"/api/{path}")]
    [part] = BridgeStore(tmp_path).delivery_receipt("other-1")["parts"]
    assert part["state"] == "delivered"


@pytest.mark.parametrize("path", ["files.completeUploadExternal", "/api/FILES.completeUploadExternal"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_queued_generic_completion_is_refused_locally_when_it_runs(tmp_path, path, method):
    # A row an earlier version queued: refused before any request, as a definite local failure.
    store = BridgeStore(tmp_path)
    store.set_runtime(workspace_id="T1")
    arguments = {"files": [{"id": "F1"}], "channel_id": "C1", "initial_comment": "hello"}
    assert store.enqueue_mutation(request_id="old-complete", operation="generic_api", delivery_reporting_version=1,
                                  payload={"method": method, "path": path, "effect": "write", "result_kind": "auto",
                                           "body" if method == "POST" else "params": arguments})
    calls = []

    async def run(worker_store):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_provider(calls))) as http:
            worker = OutboundWorker(worker_store, SlackClient("xoxb-test", "xapp-test", http_client=http))
            try:
                return await worker.process_once()
            finally:
                await worker.aclose()

    assert asyncio.run(run(store))
    [part] = store.delivery_receipt("old-complete")["parts"]
    assert (part["state"], part["attempts"], part["provider_result"]) == ("failed", 1, {"uncertain": False})
    assert "not available through slack_api" in part["error"] and "slack_file_upload" in part["error"]
    assert calls == [] and store.claim_report() is None
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET lease_until=0,available_at=0")
    reopened = BridgeStore(tmp_path)
    assert not asyncio.run(run(reopened))
    assert reopened.delivery_receipt("old-complete")["parts"] == [part] and calls == []


def test_reserved_method_check_is_exact_and_case_insensitive():
    for endpoint in ("files.completeUploadExternal", "FILES.COMPLETEUPLOADEXTERNAL", "Files.CompleteUploadExternal"):
        with pytest.raises(SlackMethodReserved):
            refuse_reserved_method(endpoint)
    for endpoint in ("files.getUploadURLExternal", "files.info", "files.upload", "files.completeUploadExternalV2",
                     "chat.postMessage", ""):
        refuse_reserved_method(endpoint)
    assert json.dumps({"code": SlackMethodReserved.error}) == '{"code": "reserved_for_slack_file_upload"}'
