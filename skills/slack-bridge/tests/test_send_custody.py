"""Durable physical-send custody using production store, worker and Slack client.

The Slack boundary is a deterministic HTTP transport. The checkpoint-loss test
actually exits a separate worker process after provider acceptance, before the
delivery checkpoint; remaining tests control cancellation and lease expiry.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import subprocess
import sys

import httpx
import pytest

from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient
from lib.store import BridgeStore


def _enqueue(store, reporting, *, request_id="selection", operation="text", path=None):
    store.set_runtime(workspace_id="T1")
    if operation == "text":
        store.enqueue_outbox(
            request_id=request_id, target="C1", thread_ts="1.0", chunks=["Selected words"],
            origin={"kind": "automatic", "task_id": "author", "source_event_id": "event"},
            output_ref="author:selection:2", delivery_reporting_version=reporting,
        )
    elif operation == "generic":
        store.enqueue_mutation(
            request_id=request_id, operation="generic_api", delivery_reporting_version=reporting,
            payload={"method": "GET", "path": "chat.postMessage", "params": {"channel": "C1", "text": "Selected words"}},
        )
    else:
        path.write_bytes(b"immutable upload")
        store.enqueue_mutation(
            request_id=request_id, operation="upload_file", delivery_reporting_version=reporting,
            payload={"path": str(path), "filename": path.name, "channel": "C1"},
        )


def _expire(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET lease_until=0,available_at=0")


def _row(store):
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone())


def _report(store, reporting, *, error):
    report = store.claim_report()
    if not reporting:
        assert report is None
        return
    payload = report["payload"]
    assert payload["state"] == "uncertain"
    assert payload["delivery_id"] == "selection" and payload["part_id"] == "0"
    assert payload["text"] == "Selected words" and payload["format"] == "markdown"
    assert payload["conversation_id"] == "C1" and payload["account_id"] == "T1"
    assert payload["origin"]["task_id"] == "author"
    assert payload["message"]["output_ref"] == "author:selection:2"
    assert payload["message"]["provider_message_id"] == ""
    assert payload["message"]["error"] == error
    store.finish_report(report)
    assert store.claim_report() is None


_CRASH_WORKER = r"""
import asyncio, json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import httpx
from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient
from lib.store import BridgeStore
root = pathlib.Path(sys.argv[2])
store = BridgeStore(root)
def provider(request):
    # Synthetic provider accepted exactly one physical write.
    (root / 'provider-effects.json').write_text(json.dumps([str(request.url)]))
    return httpx.Response(200, json={'ok': True, 'channel': 'C1', 'ts': '2.0'})
def lose_checkpoint(*args, **kwargs):
    print('SEND_APPLIED_BEFORE_CHECKPOINT', flush=True)
    os._exit(73)
store.complete_outbox = lose_checkpoint
async def run():
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        slack = SlackClient('xoxb-test', 'xapp-test', http_client=http)
        await OutboundWorker(store, slack).process_once()
asyncio.run(run())
"""


@pytest.mark.parametrize("reporting", [0, 1])
@pytest.mark.serial
def test_process_loss_after_provider_acceptance_never_reclaims_the_send(tmp_path, reporting):
    store = BridgeStore(tmp_path)
    _enqueue(store, reporting)
    process = subprocess.run(
        [sys.executable, "-c", _CRASH_WORKER, str(pathlib.Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True, text=True, timeout=20,
    )
    assert process.returncode == 73, process.stderr
    assert process.stdout.strip() == "SEND_APPLIED_BEFORE_CHECKPOINT"
    assert len(json.loads((tmp_path / "provider-effects.json").read_text())) == 1
    assert (_row(store)["state"], _row(store)["send_started"]) == ("leased", 1)
    assert store.claim_outbox() is None and store.claim_report() is None

    # Expiry is only a lost result, not proof that Slack did nothing.
    _expire(store)
    reopened = BridgeStore(tmp_path)
    assert reopened.claim_outbox() is None
    part = reopened.delivery_receipt("selection")["parts"][0]
    assert (part["state"], part["attempts"]) == ("uncertain", 1)
    assert part["provider_result"] == {"uncertain": True}
    _report(reopened, reporting, error="send_result_unrecorded")

    # Replaying the same Host selection cannot replace or revive its attempt.
    _enqueue(reopened, reporting)
    assert reopened.claim_outbox() is None
    assert len(json.loads((tmp_path / "provider-effects.json").read_text())) == 1


@pytest.mark.parametrize("reporting", [0, 1])
@pytest.mark.parametrize("operation", ["text", "generic", "upload"])
def test_cancel_after_dispatch_records_uncertainty_without_resend(tmp_path, reporting, operation):
    async def run():
        store = BridgeStore(tmp_path)
        artifact = tmp_path / "upload.txt"
        _enqueue(store, reporting, operation=operation, path=artifact)
        accepted = asyncio.Event()
        calls = []

        async def provider(request):
            if request.url.path.endswith("files.getUploadURLExternal"):
                return httpx.Response(200, json={"ok": True, "file_id": "F1", "upload_url": "https://uploads.example/bytes"})
            if request.url.host == "uploads.example":
                return httpx.Response(200, text="uploaded")
            calls.append((request.method, request.url.path))
            accepted.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            worker = OutboundWorker(store, slack)
            task = asyncio.create_task(worker.process_once())
            try:
                await asyncio.wait_for(accepted.wait(), timeout=3)
                assert _row(store)["send_started"] == 1 and store.claim_report() is None
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                _expire(store)
                restarted = OutboundWorker(BridgeStore(tmp_path), slack)
                assert not await restarted.process_once()
                part = store.delivery_receipt("selection")["parts"][0]
                assert (part["state"], part["attempts"]) == ("uncertain", 1)
                assert part["provider_result"] == {"uncertain": True}
                assert len(calls) == 1
                if operation == "generic":
                    assert calls == [("GET", "/api/chat.postMessage")]
                if operation == "text":
                    _report(store, reporting, error="send_cancelled_after_dispatch")
                else:
                    # No provider-returned speech exists for this generic/upload
                    # result, so uncertainty stays an operation receipt.
                    assert store.claim_report() is None
                await restarted.aclose()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await worker.aclose()
        if operation == "upload":
            assert not artifact.exists()

    asyncio.run(run())


@pytest.mark.parametrize("reporting", [0, 1])
def test_cancel_before_message_dispatch_can_reclaim_the_unsent_lease(tmp_path, reporting):
    async def run():
        store = BridgeStore(tmp_path)
        _enqueue(store, reporting)
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET target='U1'")
        resolving = asyncio.Event()
        calls = []
        paused = True

        async def provider(request):
            if request.url.path.endswith("conversations.open"):
                resolving.set()
                if paused:
                    await asyncio.Event().wait()
                return httpx.Response(200, json={"ok": True, "channel": {"id": "C1"}})
            calls.append(request)
            return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "2.0"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            worker = OutboundWorker(store, slack)
            task = asyncio.create_task(worker.process_once())
            try:
                await asyncio.wait_for(resolving.wait(), timeout=3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert _row(store)["send_started"] == 0 and not calls
                assert store.claim_report() is None
                _expire(store)
                paused = False
                restarted = OutboundWorker(BridgeStore(tmp_path), slack)
                assert await restarted.process_once()
                assert len(calls) == 1
                assert store.delivery_receipt("selection")["parts"][0]["state"] == "delivered"
                await restarted.aclose()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await worker.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("refusal", ["ratelimited", "connect"])
@pytest.mark.parametrize("operation", ["text", "generic"])
def test_known_no_effect_clears_attempt_marker_before_retry(tmp_path, refusal, operation):
    async def run():
        store = BridgeStore(tmp_path)
        _enqueue(store, 1, operation=operation)
        calls = []

        def provider(request):
            calls.append(request)
            if len(calls) == 1:
                if refusal == "connect":
                    raise httpx.ConnectError("request not sent", request=request)
                return httpx.Response(429, headers={"Retry-After": "1"}, json={"ok": False, "error": "ratelimited"})
            return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "2.0"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            worker = OutboundWorker(store, slack)
            assert await worker.process_once()
            row = _row(store)
            assert (row["state"], row["send_started"], row["report_payload_json"]) == ("pending", 0, "")
            assert store.claim_report() is None
            _expire(store)
            reopened = BridgeStore(tmp_path)
            restarted = OutboundWorker(reopened, slack)
            assert await restarted.process_once()
            part = reopened.delivery_receipt("selection")["parts"][0]
            assert (part["state"], part["attempts"], len(calls)) == ("delivered", 2, 2)
            assert _row(store)["send_started"] == 0
            await worker.aclose()
            await restarted.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("operation,payload,error", [
    ("update_message", {"channel": " ", "ts": "1.0"}, "channel and ts are required"),
    ("update_message", {"channel": "C1", "ts": "1.0", "text_format": "html"}, "text_format must be"),
    ("delete_message", {"channel": "C1", "ts": ""}, "channel and ts are required"),
    ("reaction_add", {"channel": "C1", "ts": "1.0", "name": " "}, "channel, ts and name are required"),
    ("reaction_remove", {"channel": "C1", "ts": "", "name": "eyes"}, "channel, ts and name are required"),
    ("pin_add", {"channel": "", "ts": "1.0"}, "channel and ts are required"),
    ("pin_remove", {"channel": "C1", "ts": ""}, "channel and ts are required"),
    ("bookmark_add", {"channel": ""}, "channel is required"),
    ("bookmark_add", {"channel": "C1", "link": "https://example.com"}, "title and link are required"),
    ("bookmark_add", {"channel": "C1", "title": "Example"}, "title and link are required"),
    ("bookmark_remove", {"channel": "C1", "bookmark_id": " "}, "bookmark_id is required"),
    ("join_conversation", {"channel_id": " "}, "channel_id is required"),
    ("generic_api", {"method": "PUT", "path": "chat.delete"}, "method must be GET or POST"),
    ("generic_api", {"path": "https://example.com/api/chat.delete"}, "path must stay on slack.com/api"),
    ("generic_api", {"path": "../chat.delete"}, "path is invalid"),
    ("generic_api", {"path": "chat.delete?channel=C1"}, "path must be one Slack Web API method"),
    ("generic_api", {"path": "chat.delete", "body": {"token": "not-a-credential"}}, "payload must not include token"),
    ("generic_api", {"method": "GET", "path": "chat.delete", "params": {"token": "not-a-credential"}}, "payload must not include token"),
])
def test_local_mutation_argument_refusal_is_failed_without_provider_request(tmp_path, operation, payload, error):
    asyncio.run(_local_refusal(tmp_path, operation, payload, error))


@pytest.mark.parametrize("input_state", ["missing", "directory", "empty", "permission", "read_error"])
def test_local_upload_input_refusal_is_failed_without_provider_request(tmp_path, monkeypatch, input_state):
    artifact = tmp_path / "upload.txt"
    if input_state == "directory":
        artifact.mkdir()
    elif input_state != "missing":
        artifact.write_bytes(b"" if input_state == "empty" else b"immutable upload")
    if input_state in {"permission", "read_error"}:
        original = pathlib.Path.read_bytes

        def unreadable(path):
            if path == artifact:
                error = PermissionError if input_state == "permission" else OSError
                raise error("controlled local read failure")
            return original(path)

        monkeypatch.setattr(pathlib.Path, "read_bytes", unreadable)
    asyncio.run(_local_refusal(
        tmp_path, "upload_file", {"path": str(artifact), "filename": "upload.txt", "channel": "C1"},
        "file must not be empty" if input_state == "empty" else "could not read upload input",
    ))
    # Terminal cleanup removes staged files, but does not recursively delete a directory.
    assert artifact.is_dir() if input_state == "directory" else not artifact.exists()


async def _local_refusal(tmp_path, operation, payload, error):
    store = BridgeStore(tmp_path)
    store.set_runtime(workspace_id="T1")
    arguments = dict(request_id="local-refusal", operation=operation, payload=payload,
                     delivery_reporting_version=1)
    assert store.enqueue_mutation(**arguments)
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
        worker = OutboundWorker(store, slack)
        assert await worker.process_once()
        assert calls == []  # Even upload URL allocation must not have happened.
        [part] = store.delivery_receipt("local-refusal")["parts"]
        assert (part["state"], part["attempts"]) == ("failed", 1)
        assert part["provider_result"] == {"uncertain": False}
        assert error in part["error"]
        assert _row(store)["send_started"] == 0
        assert store.claim_report() is None  # No provider-returned speech to report.
        await worker.aclose()

        _expire(store)
        reopened = BridgeStore(tmp_path)
        assert not reopened.enqueue_mutation(**arguments)
        restarted = OutboundWorker(reopened, slack)
        assert not await restarted.process_once()
        assert reopened.delivery_receipt("local-refusal")["parts"] == [part]
        assert calls == []
        await restarted.aclose()


@pytest.mark.parametrize("failure", [OSError, httpx.ReadTimeout])
@pytest.mark.parametrize("phase", ["generic", "upload_url", "upload_bytes", "upload_complete"])
def test_dispatched_failure_stays_uncertain_without_resend(tmp_path, failure, phase):
    async def run():
        store = BridgeStore(tmp_path)
        artifact = tmp_path / "upload.txt"
        _enqueue(store, 1, operation="generic" if phase == "generic" else "upload", path=artifact)
        calls = []

        def provider(request):
            calls.append(request.url.path)
            current = {
                "/api/chat.postMessage": "generic",
                "/api/files.getUploadURLExternal": "upload_url",
                "/bytes": "upload_bytes",
                "/api/files.completeUploadExternal": "upload_complete",
            }[request.url.path]
            if current == phase:
                raise failure("provider dispatched; outcome unknown")
            if current == "upload_url":
                return httpx.Response(200, json={"ok": True, "file_id": "F1", "upload_url": "https://uploads.example/bytes"})
            assert current == "upload_bytes"
            return httpx.Response(200, text="uploaded")

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            worker = OutboundWorker(store, slack)
            assert await worker.process_once()
            expected_calls = {
                "generic": ["/api/chat.postMessage"],
                "upload_url": ["/api/files.getUploadURLExternal"],
                "upload_bytes": ["/api/files.getUploadURLExternal", "/bytes"],
                "upload_complete": ["/api/files.getUploadURLExternal", "/bytes", "/api/files.completeUploadExternal"],
            }[phase]
            assert calls == expected_calls
            [part] = store.delivery_receipt("selection")["parts"]
            assert (part["state"], part["attempts"]) == ("uncertain", 1)
            assert part["provider_result"] == {"uncertain": True}
            assert _row(store)["send_started"] == 0
            await worker.aclose()
            _expire(store)
            reopened = BridgeStore(tmp_path)
            restarted = OutboundWorker(reopened, slack)
            assert not await restarted.process_once()
            assert reopened.delivery_receipt("selection")["parts"] == [part]
            assert calls == expected_calls
            assert store.claim_report() is None
            await restarted.aclose()
        assert not artifact.exists()

    asyncio.run(run())


def test_send_requires_the_current_unexpired_and_unstarted_lease(tmp_path):
    store = BridgeStore(tmp_path)
    _enqueue(store, 1)
    expired = store.claim_outbox()
    _expire(store)
    with pytest.raises(RuntimeError, match="unexpired"):
        store.begin_send(expired)
    current = store.claim_outbox()
    with pytest.raises(RuntimeError, match="unexpired"):
        store.begin_send(expired)
    store.begin_send(current)
    with pytest.raises(RuntimeError, match="unstarted"):
        store.begin_send(current)
    assert store.claim_outbox() is None and store.claim_report() is None


@pytest.mark.parametrize("reporting", [0, 1])
def test_pre_marker_leased_rows_migrate_conservatively(tmp_path, reporting):
    store = BridgeStore(tmp_path)
    _enqueue(store, reporting)
    old = store.claim_outbox()
    store.set_resolved_target(old, "C1", "T1")
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE outbox DROP COLUMN send_started")
    reopened = BridgeStore(tmp_path)
    assert _row(reopened)["send_started"] == 1
    assert reopened.claim_outbox() is None
    _expire(reopened)
    assert reopened.claim_outbox() is None
    assert reopened.delivery_receipt("selection")["parts"][0]["state"] == "uncertain"
    _report(reopened, reporting, error="send_result_unrecorded")
