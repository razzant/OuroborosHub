"""Finite continuation leases: renew current ownership, contain obsolete work."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from lib import store as store_module
from lib.events import parse_socket_envelope
from lib.host_adapter import HostBindingTerminalError, HostDelivery, HostTurnStatus
from lib.runtime import InboundWorker
from lib.store import BridgeStore, InboxLeaseLost


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    # Do not replace stdlib time.time globally or advance any real processes.
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))
    return clock


def _ingest(store, *, event_id="Ev-1", channel="D1"):
    payload = {
        "type": "events_api", "envelope_id": f"env-{event_id}",
        "payload": {"event_id": event_id, "team_id": "T1", "event": {
            "type": "message", "channel_type": "im", "user": "U1",
            "channel": channel, "ts": "1.0", "text": "preserve this event",
        }},
    }
    row_id, inserted = store.ingest_envelope(payload, parse_socket_envelope(payload))
    assert inserted
    return row_id


def _row(store, row_id, *, table="inbox"):
    assert table in {"inbox", "outbox"}
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())


def _claimed(store, *, lease_seconds=60):
    _ingest(store)
    item = store.claim_inbox(lease_seconds=lease_seconds)
    assert item is not None
    return item


@pytest.mark.parametrize("elapsed", [10, 61], ids=["unexpired", "expired-unclaimed"])
def test_renewal_preserves_checkpoints_token_attempt_and_finite_recovery(tmp_path, clock, elapsed):
    store = BridgeStore(tmp_path)
    item = _claimed(store)
    store.set_staged_files(item.row_id, item.lease_token, [{"path": "original/staged.pdf"}])
    store.set_provider_context(item.row_id, item.lease_token, {"reader": "original"})
    store.set_transport_queue(item.row_id, item.lease_token, {"events": ["initial"]})
    store.set_transport_queue_report(item.row_id, item.lease_token, {"events": ["refresh"]})
    store.set_host_reference(item.row_id, item.lease_token, "continuing:original-author")
    before = _row(store, item.row_id)
    clock.now += elapsed

    store.renew_inbox_lease(item.row_id, item.lease_token, lease_seconds=2100)

    after = _row(store, item.row_id)
    assert after == {**before, "lease_until": clock.now + 2100, "updated_at": clock.now}
    contender = BridgeStore(tmp_path)
    clock.now = after["lease_until"] - 1
    assert contender.claim_inbox() is None
    clock.now = after["lease_until"]
    replacement = contender.claim_inbox()
    assert replacement is not None
    assert replacement.row_id == item.row_id
    assert replacement.lease_token != item.lease_token
    assert replacement.attempts == item.attempts + 1


@pytest.mark.parametrize("ownership", ["obsolete", "missing", "pending", "delivered", "failed"])
def test_renewal_cannot_revive_or_replace_ownership(tmp_path, clock, ownership):
    store = BridgeStore(tmp_path)
    item = _claimed(store)
    target_id = item.row_id
    if ownership == "obsolete":
        clock.now += 61
        replacement = BridgeStore(tmp_path).claim_inbox()
        assert replacement is not None and replacement.lease_token != item.lease_token
        store.set_host_reference(replacement.row_id, replacement.lease_token, "continuing:new-owner")
    elif ownership == "missing":
        target_id += 100
    elif ownership == "pending":
        store.retry_inbox(item.row_id, item.lease_token, "waiting", delay_seconds=5)
    elif ownership == "delivered":
        store.complete_inbox(item.row_id, item.lease_token)
    else:
        store.fail_inbox(item.row_id, item.lease_token, "terminal")
    before = _row(store, item.row_id)
    clock.now += 1

    with pytest.raises(InboxLeaseLost):
        store.renew_inbox_lease(target_id, item.lease_token, lease_seconds=2100)

    assert _row(store, item.row_id) == before


_INBOX_MUTATIONS = [
    "set_staged_files", "set_provider_context", "set_transport_queue", "set_transport_queue_report",
    "set_host_reference", "renew_inbox_lease", "complete_inbox", "fail_inbox", "retry_inbox",
]


def _mutate_inbox(store, item, method):
    extra_args, kwargs = {
        "set_staged_files": (([{"path": "obsolete.pdf"}],), {}),
        "set_provider_context": (({"owner": "obsolete"},), {}),
        "set_transport_queue": (({"events": ["obsolete"]},), {}),
        "set_transport_queue_report": ((None,), {}),
        "set_host_reference": (("continuing:obsolete",), {}),
        "renew_inbox_lease": ((), {"lease_seconds": 2100}),
        "complete_inbox": ((), {}),
        "fail_inbox": (("obsolete failure",), {}),
        "retry_inbox": (("obsolete retry",), {"delay_seconds": 0}),
    }[method]
    getattr(store, method)(item.row_id, item.lease_token, *extra_args, **kwargs)


@pytest.mark.parametrize("method", _INBOX_MUTATIONS)
def test_every_inbox_checkpoint_reports_typed_loss_without_touching_new_owner(tmp_path, clock, method):
    store = BridgeStore(tmp_path)
    obsolete = _claimed(store)
    clock.now += 61
    contender = BridgeStore(tmp_path)
    replacement = contender.claim_inbox()
    assert replacement is not None and replacement.lease_token != obsolete.lease_token
    contender.set_host_reference(replacement.row_id, replacement.lease_token, "continuing:new-owner")
    contender.set_transport_queue_report(replacement.row_id, replacement.lease_token, {"owner": "new"})
    before = _row(store, obsolete.row_id)
    clock.now += 1

    with pytest.raises(InboxLeaseLost) as caught:
        _mutate_inbox(store, obsolete, method)

    assert isinstance(caught.value, RuntimeError)
    assert _row(store, obsolete.row_id) == before


@pytest.mark.parametrize("method", ["begin_send", "set_resolved_target", "complete_outbox", "fail_outbox", "retry_outbox"])
def test_outbound_lease_errors_remain_outside_inbox_loss_type(tmp_path, clock, method):
    store = BridgeStore(tmp_path)
    store.enqueue_outbox(request_id="reply", target="D1", thread_ts="1.0", chunks=("reply",))
    obsolete = store.claim_outbox(lease_seconds=60)
    assert obsolete is not None
    clock.now += 61
    replacement = BridgeStore(tmp_path).claim_outbox()
    assert replacement is not None and replacement.lease_token != obsolete.lease_token
    before = _row(store, obsolete.row_id, table="outbox")

    with pytest.raises(RuntimeError) as caught:
        if method == "begin_send":
            store.begin_send(obsolete)
        elif method == "set_resolved_target":
            store.set_resolved_target(obsolete, "C_OBSOLETE", "obsolete-account")
        elif method == "complete_outbox":
            store.complete_outbox(obsolete.row_id, obsolete.lease_token)
        elif method == "fail_outbox":
            store.fail_outbox(obsolete.row_id, obsolete.lease_token, "obsolete")
        else:
            store.retry_outbox(obsolete.row_id, obsolete.lease_token, "obsolete", delay_seconds=0)

    assert not isinstance(caught.value, InboxLeaseLost)
    assert _row(store, obsolete.row_id, table="outbox") == before


class _Slack:
    async def user_info(self, user_id):
        return {"id": user_id, "name": "reader"}

    async def conversation_info(self, channel_id):
        return {"id": channel_id, "is_im": True}


class _Host:
    def __init__(self, *, reference="continuing:author", state="ready", error=None):
        self.reference, self.state, self.error = reference, state, error
        self.submissions = []

    async def submit(self, item):
        self.submissions.append(item)
        if self.error is not None:
            raise self.error
        return self.reference

    async def refresh_transport_queue(self, reference, snapshot):
        raise AssertionError("the synthetic iterator records the queue itself")

    async def status_updates(self, reference, **kwargs):
        yield HostTurnStatus(self.state, transport_queue_recorded=True)

    async def deliver(self, reference):
        return HostDelivery(("synthetic reply",))


def _takeover(store, clock):
    clock.now += 2101
    contender = BridgeStore(store.state_dir)
    replacement = contender.claim_inbox(lease_seconds=2100)
    assert replacement is not None
    contender.set_provider_context(replacement.row_id, replacement.lease_token, {"owner": "new"})
    contender.set_host_reference(replacement.row_id, replacement.lease_token, "continuing:new-owner")
    contender.set_transport_queue_report(replacement.row_id, replacement.lease_token, {"owner": "new"})
    return replacement, _row(store, replacement.row_id)


@pytest.mark.parametrize("checkpoint,state", [
    ("set_provider_context", "ready"), ("set_transport_queue", "ready"),
    ("set_host_reference", "ready"), ("renew_inbox_lease", "ready"),
    ("set_transport_queue_report", "ready"), ("complete_inbox", "ready"),
    ("fail_inbox", "failed"), ("retry_inbox", "pending"),
])
def test_stale_main_checkpoint_does_not_retry_obsolete_token_and_worker_survives(
    tmp_path, clock, monkeypatch, checkpoint, state,
):
    store = BridgeStore(tmp_path)
    row_id = _ingest(store)
    original_checkpoint = getattr(store, checkpoint)
    original_retry = store.retry_inbox
    takeover, retries = [], []

    def stale_checkpoint(*args, **kwargs):
        if not takeover:
            takeover.append(_takeover(store, clock))
        return original_checkpoint(*args, **kwargs)

    def tracked_retry(*args, **kwargs):
        retries.append((args, kwargs))
        if checkpoint == "retry_inbox":
            return stale_checkpoint(*args, **kwargs)
        return original_retry(*args, **kwargs)

    monkeypatch.setattr(store, checkpoint, stale_checkpoint)
    monkeypatch.setattr(store, "retry_inbox", tracked_retry)
    host = _Host(state=state)
    worker = InboundWorker(store, _Slack(), host, staged_root=tmp_path / "staged")

    assert asyncio.run(worker.process_once()) is True
    replacement, expected_row = takeover[0]
    assert replacement.row_id == row_id
    assert _row(store, row_id) == expected_row
    assert len(retries) == (1 if checkpoint == "retry_inbox" else 0)

    # Loss ends one attempt; the same worker can process a different conversation.
    monkeypatch.setattr(store, checkpoint, original_checkpoint)
    monkeypatch.setattr(store, "retry_inbox", original_retry)
    host.state = "ready"
    later_id = _ingest(store, event_id="Ev-later", channel="D2")
    assert asyncio.run(worker.process_once()) is True
    assert _row(store, later_id)["state"] == "delivered"
    assert _row(store, row_id) == expected_row


@pytest.mark.parametrize("handler", ["fail_inbox", "retry_inbox"])
def test_loss_inside_exception_handler_preserves_new_owner(tmp_path, clock, monkeypatch, handler):
    store = BridgeStore(tmp_path)
    row_id = _ingest(store)
    failure = HostBindingTerminalError("rejected") if handler == "fail_inbox" else RuntimeError("offline")
    host = _Host(error=failure)
    original_handler = getattr(store, handler)
    original_retry = store.retry_inbox
    calls, takeover, retries = [], [], []

    def stale_handler(*args, **kwargs):
        calls.append((args, kwargs))
        takeover.append(_takeover(store, clock))
        return original_handler(*args, **kwargs)

    def tracked_retry(*args, **kwargs):
        retries.append((args, kwargs))
        if handler == "retry_inbox":
            return stale_handler(*args, **kwargs)
        return original_retry(*args, **kwargs)

    monkeypatch.setattr(store, handler, stale_handler)
    monkeypatch.setattr(store, "retry_inbox", tracked_retry)
    worker = InboundWorker(store, _Slack(), host, staged_root=tmp_path / "staged")

    assert asyncio.run(worker.process_once()) is True
    assert len(calls) == 1
    assert len(retries) == (1 if handler == "retry_inbox" else 0)
    assert _row(store, row_id) == takeover[0][1]


@pytest.mark.parametrize("handler", ["fail_inbox", "retry_inbox"])
@pytest.mark.parametrize("failure_type", [RuntimeError, sqlite3.OperationalError])
def test_genuine_exception_handler_failures_propagate(tmp_path, monkeypatch, handler, failure_type):
    store = BridgeStore(tmp_path)
    _ingest(store)
    host_error = HostBindingTerminalError("rejected") if handler == "fail_inbox" else RuntimeError("offline")
    host = _Host(error=host_error)
    # Identical text is still a programming/storage error, not typed lease loss.
    failure = failure_type("Slack inbox lease no longer belongs to this worker")

    def broken_handler(*args, **kwargs):
        raise failure

    monkeypatch.setattr(store, handler, broken_handler)
    worker = InboundWorker(store, _Slack(), host, staged_root=tmp_path / "staged")
    with pytest.raises(failure_type) as caught:
        asyncio.run(worker.process_once())
    assert caught.value is failure


def test_ordinary_host_failure_still_retries(tmp_path, clock):
    store = BridgeStore(tmp_path)
    row_id = _ingest(store)
    worker = InboundWorker(store, _Slack(), _Host(error=RuntimeError("offline")), staged_root=tmp_path / "staged")

    assert asyncio.run(worker.process_once()) is True

    row = _row(store, row_id)
    assert row["state"] == "pending"
    assert row["last_error"] == "offline"
    assert row["lease_token"] == ""
    assert row["attempts"] == 1
    assert row["available_at"] == clock.now + 2


def test_cancellation_propagates_without_releasing_or_retrying_inbox(tmp_path):
    store = BridgeStore(tmp_path)
    row_id = _ingest(store)
    worker = InboundWorker(store, _Slack(), _Host(error=asyncio.CancelledError()), staged_root=tmp_path / "staged")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.process_once())

    row = _row(store, row_id)
    assert row["state"] == "leased"
    assert row["lease_token"]
    assert row["last_error"] == ""


@pytest.mark.parametrize("reference", ["continuing:author", "deferred:child", "completed:turn"])
def test_refresh_happens_only_at_first_continuation_boundary(tmp_path, clock, monkeypatch, reference):
    store = BridgeStore(tmp_path)
    row_id = _ingest(store)
    host = _Host(reference=reference, state="pending")
    original_renew = store.renew_inbox_lease
    original_submit = host.submit
    original_updates = host.status_updates
    renewals, polls = [], []

    async def slow_submit(item):
        clock.now += 1790
        return await original_submit(item)

    def renew(row_id, token, *, lease_seconds):
        # The accepted Host reference is durable before this phase starts.
        assert _row(store, row_id)["host_reference"] == reference
        renewals.append((row_id, token, lease_seconds))
        original_renew(row_id, token, lease_seconds=lease_seconds)

    async def observe_updates(reference, **kwargs):
        polls.append(_row(store, row_id))
        async for status in original_updates(reference, **kwargs):
            yield status

    monkeypatch.setattr(host, "submit", slow_submit)
    monkeypatch.setattr(host, "status_updates", observe_updates)
    monkeypatch.setattr(store, "renew_inbox_lease", renew)
    worker = InboundWorker(store, _Slack(), host, staged_root=tmp_path / "staged")

    assert asyncio.run(worker.process_once()) is True
    clock.now += 5
    assert asyncio.run(worker.process_once()) is True

    assert len(host.submissions) == 1
    assert len(renewals) == (1 if reference.startswith("continuing:") else 0)
    assert polls[0]["lease_until"] == 1000 + 2100 + (1790 if reference.startswith("continuing:") else 0)
    assert polls[1]["lease_until"] == 1000 + 1790 + 5 + 2100
    assert polls[0]["lease_token"] != polls[1]["lease_token"]
    assert _row(store, row_id)["attempts"] == 2
