"""Uploads through the real SlackClient, OutboundWorker and BridgeStore.

Slack traffic is ``httpx.MockTransport`` following the External Upload API phases
(https://docs.slack.dev/messaging/working-with-files/#upload): files.getUploadURLExternal,
the byte POST, then files.completeUploadExternal, which is the call that shares the file.
"""
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient
from lib.store import BridgeStore

UPLOAD_URL = "https://files.slack.com/upload/v1/F_UP"
COMPLETE = "files.completeUploadExternal"


class _Slack:
    """Slack Web API plus upload host; ``complete`` answers each completion call in turn."""

    def __init__(self, *complete) -> None:
        self.complete = list(complete)
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        phase = request.url.path.rsplit("/", 1)[-1] if request.url.host == "slack.com" else "bytes"
        self.calls.append(phase)
        if phase == "files.getUploadURLExternal":
            return httpx.Response(200, json={"ok": True, "upload_url": UPLOAD_URL, "file_id": "F_UP"})
        if phase == "bytes":
            return httpx.Response(200, text="OK - 5")
        answer = self.complete.pop(0)
        return answer(request) if callable(answer) else answer

    def uploads(self) -> int:
        return self.calls.count("files.getUploadURLExternal")


def _accepted_then_lost(error: type[httpx.TransportError]):
    def answer(request: httpx.Request) -> httpx.Response:
        # Slack shares the file; only the response to this worker is lost.
        raise error("completion response lost", request=request)
    return answer


def _shared() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "files": [{"id": "F_UP", "title": "report.txt"}]})


def _enqueue(store: BridgeStore, tmp_path, request_id: str):
    artifact = tmp_path / f"{request_id}.txt"
    artifact.write_bytes(b"bytes")
    store.enqueue_mutation(request_id=request_id, operation="upload_file", payload={
        "path": str(artifact), "filename": "report.txt", "channel": "C1", "thread_ts": "1.0"})
    return artifact


def _release(store: BridgeStore) -> None:
    """Let every lease and retry backoff elapse."""
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET available_at=0, lease_until=0")


async def _drain(store: BridgeStore, provider: _Slack, rounds: int = 3) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
        for _ in range(rounds):
            # A fresh worker each round, as after a restart.
            worker = OutboundWorker(store, slack)
            await worker.process_once()
            await worker.aclose()
            _release(store)


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteTimeout, httpx.WriteError,
])
def test_lost_completion_response_is_uncertain_and_never_uploads_again(tmp_path, error):
    store = BridgeStore(tmp_path)
    artifact = _enqueue(store, tmp_path, "upload-lost")
    provider = _Slack(_accepted_then_lost(error))
    asyncio.run(_drain(store, provider))

    assert provider.calls == ["files.getUploadURLExternal", "bytes", COMPLETE]
    [part] = store.delivery_receipt("upload-lost")["parts"]
    assert part["state"] == "uncertain" and part["attempts"] == 1
    assert part["provider_result"] == {"uncertain": True} and part["error"] == error.__name__
    status = store.status()
    assert status["mutations_uncertain"] == 1 and status["mutations_pending"] == 0
    assert not artifact.exists()


def test_completion_that_never_left_the_worker_retries_the_whole_upload(tmp_path):
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    store = BridgeStore(tmp_path)
    artifact = _enqueue(store, tmp_path, "upload-unsent")
    provider = _Slack(refused, _shared())
    asyncio.run(_drain(store, provider))

    assert provider.calls == ["files.getUploadURLExternal", "bytes", COMPLETE] * 2
    [part] = store.delivery_receipt("upload-unsent")["parts"]
    assert part["state"] == "delivered" and part["attempts"] == 2
    assert not artifact.exists()


def test_explicit_completion_answers_keep_their_outcomes(tmp_path):
    rejected = httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    limited = httpx.Response(429, headers={"Retry-After": "1"}, json={"ok": False, "error": "ratelimited"})
    cases = {
        "upload-shared": ([_shared()], "delivered", 1, False),
        "upload-rejected": ([rejected], "failed", 1, False),
        "upload-limited": ([limited, _shared()], "delivered", 2, False),
        "upload-unavailable": ([httpx.Response(503)], "uncertain", 1, True),
    }
    for request_id, (answers, state, uploads, uncertain) in cases.items():
        store = BridgeStore(tmp_path / request_id)
        artifact = _enqueue(store, tmp_path, request_id)
        provider = _Slack(*answers)
        asyncio.run(_drain(store, provider))

        [part] = store.delivery_receipt(request_id)["parts"]
        assert (part["state"], provider.uploads()) == (state, uploads), request_id
        if state == "delivered":
            assert part["provider_result"]["files"] == [{"id": "F_UP", "title": "report.txt"}]
        else:
            assert part["provider_result"] == {"uncertain": uncertain}, request_id
        assert not artifact.exists()
