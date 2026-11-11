"""Upload recovery through the public tool, the outbox, the outbound worker and the real client.

Bytes travel over a real local socket to ``slack_upload_host.UploadHost``; the Slack
Web API is ``FakeSlack`` behind the same client. ``published`` is the provider effect
a duplicate would repeat, so "exactly one" is checked there and against the bytes
the host received. Store clocks may run faster than real time to reach lease
boundaries quickly; the sockets and their backpressure stay real.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import pathlib
import random
import sqlite3
import subprocess
import sys
import time
import types

import httpx
import pytest

import lib.runtime as runtime_module
import lib.store as store_module
from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient
from lib.store import BridgeStore, OutboxLeaseLost
from slack_upload_host import FakeSlack, UploadHost, http_client

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS_ROOT = pathlib.Path(__file__).resolve().parent
MiB = 1024 * 1024


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
    def __init__(self, state_dir: pathlib.Path) -> None:
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


class _Bridge:
    """The registered tools and their state directory."""

    def __init__(self, state: pathlib.Path) -> None:
        self.state = state
        self.api = _Api(state)
        _load_plugin().register(self.api)
        self.store = BridgeStore(state)

    def upload(self, source: pathlib.Path, request_id: str, **arguments) -> dict:
        result = json.loads(self.api.tools["slack_file_upload"][0](
            file_path=str(source), channel_id="C1", request_id=request_id, **arguments))
        assert result["ok"] and result["state"] == "queued", result
        return result

    def receipt(self, request_id: str) -> dict:
        return json.loads(self.api.tools["slack_receipt"][0](request_id=request_id))

    def staged(self) -> list[pathlib.Path]:
        root = self.state / "outbound" / "files"
        return sorted(root.iterdir()) if root.exists() else []


def _source(tmp_path: pathlib.Path, size: int, name: str = "report.bin") -> tuple[pathlib.Path, str]:
    data = random.Random(size).randbytes(size)
    path = tmp_path / "originals" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def _release_backoff(store: BridgeStore) -> None:
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")


def _expire_leases(store: BridgeStore) -> None:
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET lease_until=0,available_at=0")


def _row(store: BridgeStore) -> dict:
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone())


async def _process(store: BridgeStore, fake: FakeSlack, *, host=None, timeout: float = 30.0, **attributes) -> bool:
    async with http_client(fake, timeout=timeout) as http:
        worker = OutboundWorker(store, SlackClient("xoxb-test", "xapp-test", http_client=http), host)
        for name, value in attributes.items():
            setattr(worker, name, value)
        try:
            return await worker.process_once()
        finally:
            await worker.aclose()


def _drain(store: BridgeStore, fake: FakeSlack, *, rounds: int = 6, **attributes) -> int:
    """Fresh workers, as after restarts, until nothing is claimable; backoffs are released."""
    worked = 0
    for _ in range(rounds):
        if not asyncio.run(_process(store, fake, **attributes)):
            break
        worked += 1
        _release_backoff(store)
    return worked


def _no_upload_url(text: str, host: UploadHost) -> None:
    """Neither the upload path nor the host's port: what makes the URL a capability."""
    assert "/upload/" not in text and f":{host.port}" not in text


class _FastStoreClock:
    """``time`` for the store module only, ``factor`` times faster than real time."""

    def __init__(self, factor: float) -> None:
        self.factor, self.origin, self.start = factor, time.time(), time.monotonic()

    def time(self) -> float:
        return self.origin + (time.monotonic() - self.start) * self.factor


@pytest.fixture
def host():
    upload_host = UploadHost()
    yield upload_host
    upload_host.close()


@pytest.mark.parametrize("size", [5, 300 * 1024, 48 * MiB], ids=["5B", "300KiB", "48MiB"])
def test_public_tool_upload_shares_exact_bytes_once(tmp_path, host, caplog, size):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, size)
    bridge.upload(source, "up-1", title="Quarterly", thread_ts="1.5", initial_comment="See attached")
    fake = FakeSlack(host)
    caplog.set_level("DEBUG")
    assert _drain(bridge.store, fake) == 1

    assert fake.slots == ["F1"] and fake.published == ["F1"] and fake.declared == {"F1": size}
    assert fake.completion_requests == [{"files": [{"id": "F1", "title": "Quarterly"}], "channel_id": "C1",
                                         "thread_ts": "1.5", "initial_comment": "See attached"}]
    record = host.records["F1"]
    assert (record.content_length, record.transfer_encoding, record.received) == (size, None, size)
    assert record.sha256 == digest and record.complete
    receipt = bridge.receipt("up-1")
    [part] = receipt["parts"]
    assert part["state"] == "delivered" and part["attempts"] == 1
    assert part["provider_result"]["files"] == [{"id": "F1", "title": "Quarterly"}]
    [attempt] = part["upload_attempts"]
    assert {key: attempt[key] for key in ("attempt", "file_id", "declared_length", "bytes", "bytes_sent",
                                          "acknowledged_length", "completion", "outcome", "phase")} == {
        "attempt": 1, "file_id": "F1", "declared_length": size, "bytes": "accepted", "bytes_sent": size,
        "acknowledged_length": size, "completion": "completed", "outcome": "delivered", "phase": "completion"}
    assert attempt["started_at"] and attempt["recorded_at"]
    _no_upload_url(json.dumps(receipt), host)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "HTTP Request: POST http://127.0.0.1/[redacted]" in logged and "slack.com/api/files.getUploadURLExternal" in logged
    _no_upload_url(logged, host)
    assert bridge.staged() == [] and source.read_bytes() == random.Random(size).randbytes(size)


class _RecordingInput(runtime_module.UploadInput):
    """Times each chunk handed to HTTP, to compare with what the host had received."""

    handed: list[tuple[float, int]] = []

    async def chunks(self):
        async for chunk in super().chunks():
            yield chunk
            _RecordingInput.handed.append((time.monotonic(), self.sent))


def _max_in_flight(handed: list[tuple[float, int]], received: list[tuple[float, int]]) -> int:
    """The most bytes the client had handed over beyond what the host had read at that time."""
    worst, index, seen = 0, 0, 0
    for moment, sent in handed:
        while index < len(received) and received[index][0] <= moment:
            seen = received[index][1]
            index += 1
        worst = max(worst, sent - seen)
    return worst


def _two_workers(monkeypatch, tmp_path, host, *, stale_renewal: float):
    """Worker 1 uploads 6 MiB at 2 MiB/s, then waits for a slow answer and a slow completion;
    worker 2 tries to claim every 20 ms. Store time runs 60x: a 120 s lease is 2 s real."""
    monkeypatch.setattr(store_module, "time", _FastStoreClock(60.0))
    monkeypatch.setattr(runtime_module, "UploadInput", _RecordingInput)
    _RecordingInput.handed = []
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 6 * MiB)
    bridge.upload(source, "slow-1")
    host.plan("F1", read_rate=2 * MiB, answer_delay=2.5, trickle=0.05)
    host.plan("F2", read_rate=2 * MiB, answer_delay=2.5, trickle=0.05)
    fake = FakeSlack(host, complete_delay=2.5)
    renewals = []
    renew = BridgeStore.renew_outbox_lease

    def counted(self, item, **kwargs):
        renewals.append(time.monotonic())
        return renew(self, item, **kwargs)

    monkeypatch.setattr(BridgeStore, "renew_outbox_lease", counted)

    async def run():
        async with http_client(fake) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            first = OutboundWorker(bridge.store, slack)
            first.lease_renew_seconds = stale_renewal
            second = OutboundWorker(BridgeStore(tmp_path / "state"), slack)
            second.lease_renew_seconds = 0.5
            started = time.monotonic()
            task = asyncio.create_task(first.process_once())
            claims = []
            second_task = None
            while not task.done():
                if second_task is None:
                    second_task = asyncio.create_task(second.process_once())
                elif second_task.done():
                    if second_task.result():
                        claims.append(time.monotonic() - started)
                    second_task = None
                await asyncio.sleep(0.02)
            if second_task is not None:
                if await second_task:
                    claims.append(time.monotonic() - started)
            first_result = await task
            await first.aclose()
            await second.aclose()
            return first_result, claims, time.monotonic() - started

    return bridge, fake, digest, renewals, asyncio.run(run())


def test_slow_upload_keeps_its_claim_through_body_answer_and_completion(monkeypatch, tmp_path, host):
    bridge, fake, digest, renewals, (first, claims, elapsed) = _two_workers(
        monkeypatch, tmp_path, host, stale_renewal=0.5)
    assert first is True and claims == []
    assert fake.slots == ["F1"] and fake.published == ["F1"] and len(fake.completion_requests) == 1
    record = host.records["F1"]
    assert record.sha256 == digest and record.received == 6 * MiB and record.content_length == 6 * MiB
    # Body alone took longer than one lease (2 s real); answer and completion each outlast it.
    body_seconds = record.timeline[-1][0] - record.timeline[0][0]
    assert body_seconds > 2.4 and elapsed > body_seconds + 2.5 + 2.5
    # Real backpressure: the client never ran more than ~1.5 MiB ahead of what the host read.
    assert _max_in_flight(_RecordingInput.handed, record.timeline) < 1.5 * MiB
    assert len(renewals) >= int(elapsed / 0.5) - 2
    [part] = bridge.receipt("slow-1")["parts"]
    assert part["state"] == "delivered" and part["attempts"] == 1
    assert [attempt["outcome"] for attempt in part["upload_attempts"]] == ["delivered"]


def test_stale_writer_whose_claim_lapsed_never_begins_completion(monkeypatch, tmp_path, host):
    # Negative control: the first worker does not extend its lease. The second claims
    # the row once it lapses and uploads with its own slot; the first finishes sending,
    # finds its claim gone and ends without completing or writing.
    bridge, fake, digest, _renewals, (first, claims, _elapsed) = _two_workers(
        monkeypatch, tmp_path, host, stale_renewal=1e6)
    assert first is True and len(claims) == 1 and claims[0] > 1.5
    assert fake.slots == ["F1", "F2"] and fake.published == ["F2"]
    assert [request["files"][0]["id"] for request in fake.completion_requests] == ["F2"]
    assert host.records["F1"].sha256 == host.records["F2"].sha256 == digest
    [part] = bridge.receipt("slow-1")["parts"]
    assert part["state"] == "delivered" and part["attempts"] == 2
    stale, fresh = part["upload_attempts"]
    assert (stale["attempt"], stale["file_id"], stale["phase"], stale["bytes"]) == (1, "F1", "bytes", "sending")
    assert "outcome" not in stale and stale["completion"] == "not_sent"
    assert (fresh["attempt"], fresh["file_id"], fresh["outcome"]) == (2, "F2", "delivered")
    assert bridge.staged() == []


def test_chunked_body_is_bounded_per_write_not_per_file(tmp_path, host):
    # The client's timeout is 1 s; the healthy body takes over 3 s at 2 MiB/s.
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 6 * MiB)
    bridge.upload(source, "stall-bound")
    host.plan("F1", read_rate=2 * MiB)
    fake = FakeSlack(host)
    assert asyncio.run(_process(bridge.store, fake, timeout=1.0))
    assert fake.published == ["F1"] and host.records["F1"].sha256 == digest
    assert bridge.receipt("stall-bound")["parts"][0]["state"] == "delivered"


def _raise(error):
    def answer(request):
        raise error("controlled transport failure", request=request)
    return answer


_PRE_COMPLETION = {
    # name: (fake keyword arguments, host plan for F1, failure class, slot failed before an ID)
    "slot_answer_lost": ({"slot_answers": [_raise(httpx.ReadTimeout)]}, None, "transport_unknown"),
    "slot_unavailable": ({"slot_answers": [httpx.Response(503)]}, None, "provider_unavailable"),
    "slot_internal_error": ({"slot_answers": [httpx.Response(200, json={"ok": False, "error": "internal_error"})]},
                            None, "provider_unavailable"),
    "slot_malformed": ({"slot_answers": [httpx.Response(200, text="<html>")]}, None, "malformed_response"),
    "slot_rate_limited": ({"slot_answers": [httpx.Response(429, headers={"Retry-After": "7"},
                                                           json={"ok": False, "error": "ratelimited"})]},
                          None, "rate_limited"),
    "bytes_reset_mid_body": ({}, {"close_after": 64 * 1024}, "transport_unknown"),
    "bytes_dropped_after_body": ({}, {"close_before_answer": True}, "transport_unknown"),
    "bytes_unavailable": ({}, {"status": 503, "body": "busy"}, "provider_unavailable"),
    "bytes_rate_limited": ({}, {"status": 429, "body": "slow down", "headers": {"Retry-After": "5"}}, "rate_limited"),
    "bytes_length_mismatch": ({}, {"body": "OK - 3"}, "length_mismatch"),
}


@pytest.mark.parametrize("case", list(_PRE_COMPLETION))
def test_failure_before_any_share_retries_with_a_fresh_slot(tmp_path, host, case):
    fake_arguments, plan, failure_class = _PRE_COMPLETION[case]
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 1 * MiB)
    bridge.upload(source, "retry-1")
    if plan:
        host.plan("F1", **plan)
    fake = FakeSlack(host, **fake_arguments)
    assert asyncio.run(_process(bridge.store, fake))
    first = _row(bridge.store)
    assert (first["state"], first["send_started"], first["attempts"]) == ("pending", 0, 1)
    if case.endswith("rate_limited"):
        expected = {"slot_rate_limited": 7, "bytes_rate_limited": 5}[case]
        assert first["available_at"] - first["updated_at"] == pytest.approx(expected, abs=0.5)
    assert len(bridge.staged()) == 1
    _release_backoff(bridge.store)
    assert _drain(bridge.store, fake) == 1

    final = fake.slots[-1]
    assert fake.published == [final] and host.records[final].sha256 == digest
    assert [request["files"][0]["id"] for request in fake.completion_requests][-1] == final
    receipt = bridge.receipt("retry-1")
    [part] = receipt["parts"]
    assert part["state"] == "delivered" and part["attempts"] == 2
    failed, delivered = part["upload_attempts"]
    assert failed["outcome"] == "retry" and failed["failure"]["class"] == failure_class
    assert delivered["outcome"] == "delivered" and delivered["file_id"] == final
    if case.startswith("slot"):
        assert failed["file_id"] == "" and failed["phase"] == "slot" and fake.slots == ["F1"]
    else:
        assert failed["file_id"] == "F1" and fake.slots == ["F1", "F2"]
    if case.startswith("bytes"):
        assert failed["phase"] == "bytes" and failed["completion"] == "not_sent"
        assert failed["bytes"] == {"bytes_reset_mid_body": "unknown", "bytes_dropped_after_body": "unknown",
                                   "bytes_unavailable": "rejected", "bytes_rate_limited": "rejected",
                                   "bytes_length_mismatch": "length_mismatch"}[case]
        assert "F1" not in fake.published
    if case == "bytes_dropped_after_body":
        assert failed["bytes_sent"] == 1 * MiB and host.records["F1"].complete
    if case == "bytes_length_mismatch":
        assert failed["acknowledged_length"] == 3
    _no_upload_url(json.dumps(receipt), host)
    assert bridge.staged() == []


@pytest.mark.parametrize("case", ["malformed", "internal_error", "fatal_error"])
def test_unknown_completion_stays_uncertain_and_is_never_repeated(tmp_path, host, case):
    bridge = _Bridge(tmp_path / "state")
    source, _digest = _source(tmp_path, 200 * 1024)
    bridge.upload(source, "unknown-1")
    fake = FakeSlack(host)
    fake.complete_answers = [{
        "malformed": httpx.Response(200, text="<html>"),
        "internal_error": httpx.Response(200, json={"ok": False, "error": "internal_error"}),
        "fatal_error": httpx.Response(200, json={"ok": False, "error": "fatal_error"}),
    }[case]]
    assert _drain(bridge.store, fake) == 1
    _expire_leases(bridge.store)
    assert _drain(bridge.store, fake) == 0
    assert fake.slots == ["F1"] and len(fake.completion_requests) == 1
    assert fake.published == []
    [part] = bridge.receipt("unknown-1")["parts"]
    assert (part["state"], part["provider_result"]) == ("uncertain", {"uncertain": True})
    [attempt] = part["upload_attempts"]
    assert (attempt["file_id"], attempt["bytes"], attempt["completion"], attempt["outcome"]) == (
        "F1", "accepted", "unknown", "uncertain")
    assert attempt["failure"]["class"] == "unknown" and attempt["failure"]["phase"] == "completion"
    assert bridge.staged() == []


@pytest.mark.parametrize("case", ["slot_refused", "bytes_too_large", "bytes_redirect"])
def test_explicit_refusal_is_final_without_another_upload(tmp_path, host, case):
    bridge = _Bridge(tmp_path / "state")
    source, _digest = _source(tmp_path, 100 * 1024)
    bridge.upload(source, "refused-1")
    fake = FakeSlack(host)
    if case == "slot_refused":
        fake.slot_answers = [httpx.Response(200, json={"ok": False, "error": "invalid_auth"})]
    elif case == "bytes_too_large":
        host.plan("F1", status=413, body="too large")
    elif case == "bytes_redirect":
        host.plan("F1", status=302, body="", headers={"Location": host.url("elsewhere")})
    assert _drain(bridge.store, fake) == 1
    [part] = bridge.receipt("refused-1")["parts"]
    assert (part["state"], part["attempts"], part["provider_result"]) == ("failed", 1, {"uncertain": False})
    [attempt] = part["upload_attempts"]
    assert attempt["failure"]["class"] == "refused" and attempt["outcome"] == "failed"
    assert fake.slot_requests == 1 and fake.published == []
    assert "elsewhere" not in host.records
    assert fake.completion_requests == []
    if case in {"bytes_too_large", "bytes_redirect"}:
        assert attempt["bytes"] == "rejected" and attempt["failure"]["http_status"] in {413, 302}
    assert bridge.staged() == []


def test_slot_identity_survives_an_allocation_without_upload_url(tmp_path, host):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 64 * 1024)
    bridge.upload(source, "missing-url")
    fake = FakeSlack(host, slot_answers=[httpx.Response(200, json={"ok": True, "file_id": "F_ORPHAN"})])
    assert _drain(bridge.store, fake) == 2
    [part] = bridge.receipt("missing-url")["parts"]
    earlier, final = part["upload_attempts"]
    assert earlier["file_id"] == "F_ORPHAN" and earlier["phase"] == "slot"
    assert earlier["bytes"] == earlier["completion"] == "not_sent"
    assert earlier["failure"]["error"] == "missing_upload_url"
    assert part["state"] == "delivered" and fake.published == ["F1"]
    assert final["file_id"] == "F1" and host.records["F1"].sha256 == digest


def test_private_completion_does_not_claim_a_conversation_share(tmp_path, host):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 32)
    result = json.loads(bridge.api.tools["slack_file_upload"][0](
        file_path=str(source), request_id="private-upload"))
    assert result["ok"] and result["state"] == "queued"
    fake = FakeSlack(host)
    assert _drain(bridge.store, fake) == 1
    [request] = fake.completion_requests
    assert "channel_id" not in request
    assert host.records["F1"].sha256 == digest
    [part] = bridge.receipt("private-upload")["parts"]
    assert part["state"] == "delivered"
    assert part["upload_attempts"][0]["completion"] == "completed"


def test_three_physical_uploads_at_most(tmp_path, host):
    bridge = _Bridge(tmp_path / "state")
    source, _digest = _source(tmp_path, 64 * 1024)
    bridge.upload(source, "exhausted-1")
    for slot in ("F1", "F2", "F3", "F4"):
        host.plan(slot, status=503, body="busy")
    fake = FakeSlack(host)
    assert _drain(bridge.store, fake) == 3
    assert fake.slots == ["F1", "F2", "F3"] and fake.completion_requests == []
    [part] = bridge.receipt("exhausted-1")["parts"]
    assert (part["state"], part["attempts"]) == ("failed", 3)
    assert part["error"] == "upload_attempts_exhausted: upload_bytes_http_503"
    assert [(attempt["attempt"], attempt["file_id"], attempt["outcome"]) for attempt in part["upload_attempts"]] == [
        (1, "F1", "retry"), (2, "F2", "retry"), (3, "F3", "failed")]
    assert bridge.staged() == []


class _PlaceScript:
    """place_hold answers in order; once the script ends, the place is Active."""

    def __init__(self, *answers: str) -> None:
        self.answers, self.calls = list(answers), 0

    async def place_hold(self, account, conversation):
        self.calls += 1
        return self.answers.pop(0) if self.answers else ""


def test_hold_spends_an_upload_only_once_a_slot_exists(tmp_path, host):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 64 * 1024)
    bridge.upload(source, "held-1")
    held = "Held: Slack is silent"
    # Claim 1 holds before bytes, claim 2 before completion, claim 3 before anything.
    place = _PlaceScript("", held, "", "", held, held)
    fake = FakeSlack(host)
    for expected in ((1, "held"), (2, "held"), (2, "held")):
        assert asyncio.run(_process(bridge.store, fake, host=place))
        part = bridge.receipt("held-1")["parts"][0]
        assert (part["attempts"], part["state"]) == expected
        _release_backoff(bridge.store)
    assert asyncio.run(_process(bridge.store, fake, host=place))
    [part] = bridge.receipt("held-1")["parts"]
    assert (part["state"], part["attempts"]) == ("delivered", 3)
    assert fake.slots == ["F1", "F2", "F3"] and fake.published == ["F3"] and host.records["F3"].sha256 == digest
    assert "F1" not in host.records
    first, second, third = part["upload_attempts"]
    assert (first["phase"], first["bytes"], first["outcome"], first["failure"]["class"]) == (
        "bytes", "not_sent", "held", "held")
    assert (second["phase"], second["bytes"], second["completion"], second["outcome"]) == (
        "completion", "accepted", "not_sent", "held")
    assert third["outcome"] == "delivered"


def test_holds_after_a_slot_cannot_allocate_without_bound(tmp_path, host):
    bridge = _Bridge(tmp_path / "state")
    source, _digest = _source(tmp_path, 64 * 1024)
    bridge.upload(source, "held-2")
    held = "Held: Slack is paused"
    place = _PlaceScript("", held, "", held, "", held)
    fake = FakeSlack(host)
    for _ in range(4):
        assert asyncio.run(_process(bridge.store, fake, host=place))
        _release_backoff(bridge.store)
    [part] = bridge.receipt("held-2")["parts"]
    assert (part["state"], part["error"]) == ("failed", "upload_attempts_exhausted")
    assert fake.slots == ["F1", "F2", "F3"] and fake.completion_requests == []
    assert [attempt["outcome"] for attempt in part["upload_attempts"]] == ["held", "held", "held"]
    assert bridge.staged() == []


def test_failed_facts_checkpoint_stops_before_any_byte(tmp_path, host, monkeypatch):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 64 * 1024)
    bridge.upload(source, "checkpoint-1")
    record = BridgeStore.record_upload_attempt
    calls = []

    def failing_once(self, item, index, facts):
        calls.append(facts["phase"])
        if len(calls) == 2:  # the slot's identity, before any byte
            raise sqlite3.OperationalError("disk I/O error")
        return record(self, item, index, facts)

    monkeypatch.setattr(BridgeStore, "record_upload_attempt", failing_once)
    fake = FakeSlack(host)
    assert _drain(bridge.store, fake) == 2
    assert "F1" not in host.records and fake.published == ["F2"] and host.records["F2"].sha256 == digest
    [part] = bridge.receipt("checkpoint-1")["parts"]
    assert (part["state"], part["attempts"]) == ("delivered", 2)
    first, second = part["upload_attempts"]
    assert first["failure"]["class"] == "internal" and first["outcome"] == "retry"
    assert second["file_id"] == "F2"


def test_failed_marker_write_never_sends_completion(tmp_path, host, monkeypatch):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 64 * 1024)
    bridge.upload(source, "marker-1")
    begin = BridgeStore.begin_send
    failures = []

    def failing_once(self, item, **kwargs):
        if not failures:
            failures.append(kwargs["upload_attempt"][1]["completion"])
            raise sqlite3.OperationalError("disk I/O error")
        return begin(self, item, **kwargs)

    monkeypatch.setattr(BridgeStore, "begin_send", failing_once)
    fake = FakeSlack(host)
    assert _drain(bridge.store, fake) == 2
    assert failures == ["started"] and [request["files"][0]["id"] for request in fake.completion_requests] == ["F2"]
    assert fake.published == ["F2"] and host.records["F2"].sha256 == digest
    first, second = bridge.receipt("marker-1")["parts"][0]["upload_attempts"]
    assert (first["file_id"], first["bytes"], first["completion"], first["outcome"]) == ("F1", "accepted", "not_sent", "retry")
    assert first["failure"] == {"phase": "completion", "class": "internal", "error": "OperationalError"}
    assert second["outcome"] == "delivered"


def test_replaced_claim_stops_its_transfer_at_the_next_renewal(tmp_path, host):
    # As after this process was suspended past its lease: another claim takes the row over.
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 4 * MiB)
    bridge.upload(source, "replaced-1")
    host.plan("F1", read_rate=1 * MiB)
    fake = FakeSlack(host)

    async def run():
        async with http_client(fake) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            first = OutboundWorker(bridge.store, slack)
            first.lease_renew_seconds = 0.2
            task = asyncio.create_task(first.process_once())
            while "F1" not in host.records or host.records["F1"].received < 256 * 1024:
                await asyncio.sleep(0.02)
            with sqlite3.connect(bridge.store.path) as db:
                db.execute("UPDATE outbox SET lease_until=0")
            second = OutboundWorker(BridgeStore(tmp_path / "state"), slack)
            claimed = asyncio.create_task(second.process_once())
            assert await task is True
            assert await claimed is True
            await first.aclose()
            await second.aclose()

    asyncio.run(run())
    assert not host.records["F1"].complete and host.records["F1"].received < 3 * MiB
    assert fake.published == ["F2"] and host.records["F2"].sha256 == digest
    [part] = bridge.receipt("replaced-1")["parts"]
    assert (part["state"], part["attempts"]) == ("delivered", 2)
    stale, fresh = part["upload_attempts"]
    assert (stale["file_id"], stale["bytes"], stale["completion"]) == ("F1", "sending", "not_sent")
    assert "outcome" not in stale and fresh["outcome"] == "delivered"


def test_unrecorded_share_is_left_to_recovery_and_never_repeated(tmp_path, host, monkeypatch, caplog):
    bridge = _Bridge(tmp_path / "state")
    source, _digest = _source(tmp_path, 64 * 1024)
    bridge.upload(source, "checkpoint-2")

    def lost(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    fake = FakeSlack(host)
    with monkeypatch.context() as patch:
        patch.setattr(BridgeStore, "complete_outbox", lost)
        assert asyncio.run(_process(bridge.store, fake))
    row = _row(bridge.store)
    assert (row["state"], row["send_started"]) == ("leased", 1) and fake.published == ["F1"]
    assert "shared file F1 but its receipt could not be recorded" in caplog.text
    _expire_leases(bridge.store)
    assert _drain(bridge.store, fake) == 0
    assert len(fake.completion_requests) == 1
    [part] = bridge.receipt("checkpoint-2")["parts"]
    assert (part["state"], part["error"]) == ("uncertain", "send_result_unrecorded")
    [attempt] = part["upload_attempts"]
    assert (attempt["file_id"], attempt["completion"]) == ("F1", "started")


def test_cancel_while_sending_keeps_the_staged_copy_for_a_fresh_upload(tmp_path, host):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 4 * MiB)
    bridge.upload(source, "cancel-1")
    host.plan("F1", read_rate=1 * MiB)
    fake = FakeSlack(host)

    async def run():
        async with http_client(fake) as http:
            worker = OutboundWorker(bridge.store, SlackClient("xoxb-test", "xapp-test", http_client=http))
            task = asyncio.create_task(worker.process_once())
            while "F1" not in host.records or host.records["F1"].received < 512 * 1024:
                await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await worker.aclose()

    asyncio.run(run())
    row = _row(bridge.store)
    assert (row["state"], row["send_started"]) == ("leased", 0) and len(bridge.staged()) == 1
    [attempt] = bridge.receipt("cancel-1")["parts"][0]["upload_attempts"]
    assert (attempt["file_id"], attempt["bytes"], attempt["outcome"]) == ("F1", "unknown", "cancelled")
    _expire_leases(bridge.store)
    assert _drain(bridge.store, fake) == 1
    assert fake.published == ["F2"] and host.records["F2"].sha256 == digest
    assert bridge.receipt("cancel-1")["parts"][0]["state"] == "delivered" and bridge.staged() == []


_CRASH_CHILD = r"""
import asyncio, json, os, pathlib, sys
skill, tests, state, port, point = sys.argv[1:6]
sys.path[:0] = [skill, tests]
import lib.runtime as runtime
from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient
from lib.store import BridgeStore
from slack_upload_host import FakeSlack, http_client

class Host:  # the parent's real upload host, over TCP
    def url(self, slot):
        return f"http://127.0.0.1:{port}/upload/{slot}"

store = BridgeStore(pathlib.Path(state))
fake = FakeSlack(Host())
if point == "mid_bytes":
    original = runtime.UploadInput.chunks
    async def crashing(self):
        async for chunk in original(self):
            yield chunk
            if self.sent >= 512 * 1024:
                print("CRASH_MID_BYTES", flush=True)
                os._exit(71)
    runtime.UploadInput.chunks = crashing
elif point == "before_marker":
    def crash(*args, **kwargs):
        print("CRASH_BEFORE_MARKER", flush=True)
        os._exit(72)
    store.begin_send = crash
else:
    def shared_then_crash(request):
        pathlib.Path(state, "published.json").write_text(json.dumps([json.loads(request.content)["files"][0]["id"]]))
        print("CRASH_AFTER_SHARE", flush=True)
        os._exit(73)
    fake.complete_answers = [shared_then_crash]

async def run():
    async with http_client(fake) as http:
        await OutboundWorker(store, SlackClient("xoxb-test", "xapp-test", http_client=http)).process_once()
asyncio.run(run())
print("NO_CRASH", flush=True)
"""


@pytest.mark.serial
@pytest.mark.parametrize("point,code", [("mid_bytes", 71), ("before_marker", 72), ("after_share", 73)])
def test_process_crash_then_reopen_recovers_without_a_second_share(tmp_path, host, point, code):
    bridge = _Bridge(tmp_path / "state")
    source, digest = _source(tmp_path, 2 * MiB)
    bridge.upload(source, "crash-1")
    host.plan("F1", read_rate=4 * MiB)
    process = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, str(SKILL_ROOT), str(TESTS_ROOT), str(tmp_path / "state"),
         str(host.port), point],
        capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == code, process.stdout + process.stderr
    row = _row(bridge.store)
    assert (row["state"], row["send_started"]) == ("leased", 1 if point == "after_share" else 0)
    reopened = BridgeStore(tmp_path / "state")
    assert reopened.claim_outbox() is None  # the dead process's lease still holds
    _expire_leases(reopened)
    fake = FakeSlack(host, prefix="G")
    _drain(reopened, fake)
    [part] = bridge.receipt("crash-1")["parts"]
    crashed = part["upload_attempts"][0]
    assert (crashed["attempt"], crashed["file_id"]) == (1, "F1") and "outcome" not in crashed
    if point == "after_share":
        assert json.loads((tmp_path / "state" / "published.json").read_text()) == ["F1"]
        assert fake.slot_requests == 0 and fake.completion_requests == []
        assert (part["state"], part["error"]) == ("uncertain", "send_result_unrecorded")
        assert crashed["completion"] == "started"
        return
    assert fake.published == ["G1"] and host.records["G1"].sha256 == digest
    assert (part["state"], part["attempts"]) == ("delivered", 2)
    assert crashed["phase"] == ("bytes" if point == "mid_bytes" else "completion")
    assert crashed["bytes"] == ("sending" if point == "mid_bytes" else "accepted")
    assert not host.records["F1"].complete if point == "mid_bytes" else host.records["F1"].complete
    assert bridge.staged() == []


def test_old_rows_keep_their_outcomes(tmp_path, host):
    store = BridgeStore(tmp_path)
    staged = tmp_path / "outbound" / "files"
    staged.mkdir(parents=True)
    for request_id in ("old-uncertain", "old-marked", "old-pending", "old-spent"):
        (staged / request_id).write_bytes(b"old bytes")
        store.enqueue_mutation(request_id=request_id, operation="upload_file",
                               payload={"path": str(staged / request_id), "filename": "old.txt", "channel": "C1"})
    with sqlite3.connect(store.path) as db:
        db.execute("""UPDATE outbox SET state='uncertain',attempts=1,last_error='upload_bytes_transport_uncertain',
                      result_json='{"uncertain":true}' WHERE request_id='old-uncertain'""")
        # A 1.12.1 claim marked its whole upload before allocating a slot.
        db.execute("""UPDATE outbox SET state='leased',lease_token='old',lease_until=1,send_started=1,attempts=1
                      WHERE request_id='old-marked'""")
        db.execute("UPDATE outbox SET attempts=1,last_error='ratelimited' WHERE request_id='old-pending'")
        db.execute("UPDATE outbox SET attempts=3,last_error='missing_upload_url' WHERE request_id='old-spent'")
        db.execute("ALTER TABLE outbox DROP COLUMN upload_attempts_json")
    store.close()
    reopened = BridgeStore(tmp_path)
    fake = FakeSlack(host)
    _drain(reopened, fake)
    parts = {request_id: reopened.delivery_receipt(request_id)["parts"][0]
             for request_id in ("old-uncertain", "old-marked", "old-pending", "old-spent")}
    assert (parts["old-uncertain"]["state"], parts["old-uncertain"]["upload_attempts"]) == ("uncertain", [])
    assert (parts["old-marked"]["state"], parts["old-marked"]["error"]) == ("uncertain", "send_result_unrecorded")
    assert (parts["old-spent"]["state"], parts["old-spent"]["error"]) == ("failed", "upload_attempts_exhausted")
    assert parts["old-pending"]["state"] == "delivered"
    assert [(attempt["attempt"], attempt["file_id"]) for attempt in parts["old-pending"]["upload_attempts"]] == [(2, "F1")]
    assert fake.slots == ["F1"] and fake.published == ["F1"]
    # Recovery never removes another holder's bytes; the settled old rows keep theirs.
    assert sorted(path.name for path in staged.iterdir()) == ["old-marked", "old-uncertain"]


def test_only_the_current_claim_can_mark_completion(tmp_path):
    store = BridgeStore(tmp_path)
    artifact = tmp_path / "staged"
    artifact.write_bytes(b"bytes")
    store.enqueue_mutation(request_id="cas-1", operation="upload_file", payload={"path": str(artifact), "channel": "C1"})
    old = store.claim_outbox(lease_seconds=60)
    index = store.record_upload_attempt(old, None, {"attempt": 1, "file_id": "F1", "phase": "bytes"})
    _expire_leases(store)
    with pytest.raises(OutboxLeaseLost):
        store.renew_outbox_lease(old, lease_seconds=60)
    with pytest.raises(OutboxLeaseLost):
        store.record_upload_attempt(old, index, {"attempt": 1, "file_id": "F1", "phase": "completion"})
    current = store.claim_outbox(lease_seconds=60)
    for write in (lambda: store.begin_send(old, upload_attempt=(index, {"attempt": 1})),
                  lambda: store.renew_outbox_lease(old, lease_seconds=60),
                  lambda: store.complete_outbox(old.row_id, old.lease_token),
                  lambda: store.fail_outbox(old.row_id, old.lease_token, "stale"),
                  lambda: store.retry_outbox(old.row_id, old.lease_token, "stale", delay_seconds=0),
                  lambda: store.hold_outbox(old, "Held: stale")):
        with pytest.raises(OutboxLeaseLost):
            write()
    store.renew_outbox_lease(current, lease_seconds=60)
    new_index = store.record_upload_attempt(current, None, {"attempt": 2, "file_id": "F2", "phase": "slot"})
    assert new_index == 1
    attempts = store.delivery_receipt("cas-1")["parts"][0]["upload_attempts"]
    assert [(attempt["attempt"], attempt["file_id"], attempt["phase"]) for attempt in attempts] == [
        (1, "F1", "bytes"), (2, "F2", "slot")]
