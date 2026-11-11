"""Completion outcomes through the real SlackClient, OutboundWorker and BridgeStore.

Slack traffic is MockTransport following the External Upload API. Byte transport,
backpressure and lease competition belong to test_upload_recovery; this module owns
known-unsent completion, explicit refusal, and lost-response invariants.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient
from lib.store import BridgeStore

UPLOAD_URL = "https://files.slack.com/upload/v1"
COMPLETE = "files.completeUploadExternal"


class _Slack:
    """Fresh slot per upload; completion answers are consumed in order."""

    def __init__(self, *complete) -> None:
        self.complete = list(complete)
        self.calls: list[str] = []
        self.completed_ids: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        phase = request.url.path.rsplit("/", 1)[-1] if request.url.host == "slack.com" else "bytes"
        self.calls.append(phase)
        if phase == "files.getUploadURLExternal":
            slot = f"F_UP_{self.uploads()}"
            return httpx.Response(200, json={"ok": True, "upload_url": f"{UPLOAD_URL}/{slot}", "file_id": slot})
        if phase == "bytes":
            assert request.content == b"bytes"
            return httpx.Response(200, text="OK - 5")
        self.completed_ids.append(json.loads(request.content)["files"][0]["id"])
        answer = self.complete.pop(0)
        return answer(request) if callable(answer) else answer

    def uploads(self) -> int:
        return self.calls.count("files.getUploadURLExternal")


def _accepted_then_lost(error: type[httpx.TransportError]):
    def answer(request: httpx.Request) -> httpx.Response:
        # The completion may have applied; the transport cannot prove otherwise.
        raise error("completion response lost", request=request)
    return answer


def _shared():
    def answer(request: httpx.Request) -> httpx.Response:
        slot = json.loads(request.content)["files"][0]["id"]
        return httpx.Response(200, json={"ok": True, "files": [{"id": slot, "title": "report.txt"}]})
    return answer


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


async def _drain(store: BridgeStore, provider: _Slack, rounds: int = 3, observations=None) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
        for _ in range(rounds):
            worker = OutboundWorker(store, slack)  # fresh worker, as after restart
            await worker.process_once()
            await worker.aclose()
            if observations is not None:
                with sqlite3.connect(store.path) as db:
                    db.row_factory = sqlite3.Row
                    observations.append(dict(db.execute("SELECT * FROM outbox").fetchone()))
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
    [attempt] = part["upload_attempts"]
    assert (attempt["file_id"], attempt["bytes"], attempt["completion"], attempt["outcome"]) == (
        "F_UP_1", "accepted", "unknown", "uncertain")
    assert attempt["failure"] == {"phase": "completion", "class": "unknown", "error": error.__name__}
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
    assert provider.completed_ids == ["F_UP_1", "F_UP_2"]
    [part] = store.delivery_receipt("upload-unsent")["parts"]
    assert part["state"] == "delivered" and part["attempts"] == 2
    first, final = part["upload_attempts"]
    assert (first["file_id"], first["bytes"], first["completion"], first["outcome"]) == (
        "F_UP_1", "accepted", "not_applied", "retry")
    assert first["failure"]["class"] == "not_sent"
    assert (final["file_id"], final["completion"], final["outcome"]) == ("F_UP_2", "completed", "delivered")
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
        observations = []
        asyncio.run(_drain(store, provider, observations=observations))

        [part] = store.delivery_receipt(request_id)["parts"]
        assert (part["state"], provider.uploads()) == (state, uploads), request_id
        assert provider.completed_ids == [f"F_UP_{i}" for i in range(1, uploads + 1)]
        attempts = part["upload_attempts"]
        assert [a["file_id"] for a in attempts] == provider.completed_ids
        assert all(a["bytes"] == "accepted" for a in attempts)
        if state == "delivered":
            assert part["provider_result"]["files"] == [{"id": f"F_UP_{uploads}", "title": "report.txt"}]
            assert (attempts[-1]["completion"], attempts[-1]["outcome"]) == ("completed", "delivered")
        else:
            assert part["provider_result"] == {"uncertain": uncertain}, request_id
            assert attempts[0]["completion"] == ("unknown" if uncertain else "refused")
            assert attempts[0]["outcome"] == state
        if request_id == "upload-limited":
            assert attempts[0]["completion"] == "not_applied" and attempts[0]["failure"]["class"] == "rate_limited"
            first = observations[0]
            assert first["state"] == "pending" and first["send_started"] == 0
            assert first["available_at"] - first["updated_at"] == pytest.approx(1.0, abs=0.5)
        assert not artifact.exists()
