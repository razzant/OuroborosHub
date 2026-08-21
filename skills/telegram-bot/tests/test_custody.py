import json
import sqlite3

from telegram_bot.custody import CustodyStore
from telegram_bot.events import parse_telegram_update


def _event(update_id=10):
    return parse_telegram_update(
        {
            "update_id": update_id,
            "message": {
                "message_id": 2,
                "text": "hello",
                "from": {"id": 3, "is_bot": False},
                "chat": {"id": 4, "type": "private"},
            },
        },
        bot_account_id="9",
    )


def test_update_commit_deduplicates_and_advances_offset_atomically(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    event = _event()
    assert event is not None
    assert store.commit_update(10, event) is True
    assert store.commit_update(10, event) is False
    assert store.telegram_offset() == 11

    lease = store.claim_inbox()
    assert lease is not None
    assert lease.event_id == "telegram:9:10"
    assert lease.payload["actor"]["platform_actor_id"] == "3"
    store.release_inbox(lease.event_id, reason="later", retry_after_sec=0)
    lease = store.claim_inbox()
    assert lease is not None and lease.attempts == 2
    store.record_submission(
        lease.event_id,
        binding_id="a" * 32,
        turn_ref="turn-1",
        outcome="silent",
        text="",
        work_ref="",
    )
    assert store.status_snapshot()["inbox_submitted"] == 1


def test_ignored_update_still_advances_offset(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    assert store.commit_update(15, None) is False
    assert store.telegram_offset() == 16
    assert store.claim_inbox() is None


def test_imports_legacy_offset_once(tmp_path):
    (tmp_path / "offsets.json").write_text(
        json.dumps({"last_offset": 44}), encoding="utf-8"
    )
    store = CustodyStore(tmp_path / "custody.sqlite3")
    assert store.telegram_offset() == 44
    (tmp_path / "offsets.json").write_text(
        json.dumps({"last_offset": 99}), encoding="utf-8"
    )
    assert CustodyStore(tmp_path / "custody.sqlite3").telegram_offset() == 44


def test_expired_lease_is_reclaimed_and_outbox_receipt_is_durable(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    event = _event(20)
    assert event is not None
    store.commit_update(20, event)
    first = store.claim_inbox(lease_sec=60)
    assert first is not None
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE inbox SET lease_until=0 WHERE event_id=?", (first.event_id,)
        )
    second = store.claim_inbox()
    assert second is not None and second.attempts == 2

    assert store.enqueue_outbox(
        "delivery-1", {"kind": "message", "chat_id": "4", "text": "ok"}
    )
    assert not store.enqueue_outbox("delivery-1", {"kind": "message"})
    delivery = store.claim_outbox()
    assert delivery is not None
    store.mark_delivered(delivery.delivery_id, provider_receipt={"message_id": 7})
    snapshot = store.status_snapshot()
    assert snapshot["outbox_delivered"] == 1


def test_terminal_failure_states_are_counted_and_leave_later_work_claimable(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    first = _event(21)
    second = _event(22)
    assert first is not None and second is not None
    store.commit_update(21, first)
    store.commit_update(22, second)
    inbox = store.claim_inbox()
    assert inbox is not None and inbox.event_id == "telegram:9:21"
    store.mark_inbox_failed(inbox.event_id, reason="binding mismatch")
    assert store.claim_inbox() is not None

    store.enqueue_outbox("failed-delivery", {"kind": "message", "chat_id": "4"})
    outbox = store.claim_outbox()
    assert outbox is not None
    store.mark_outbox_failed(outbox.delivery_id, reason="provider rejected delivery")
    store.enqueue_outbox("later-delivery", {"kind": "message", "chat_id": "4"})
    assert store.claim_outbox().delivery_id == "later-delivery"

    snapshot = store.status_snapshot()
    assert snapshot["inbox_failed"] == 1
    assert snapshot["outbox_failed"] == 1


def test_inbox_claims_different_conversations_in_parallel_but_preserves_fifo(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    first = _event(30)
    second = _event(31)
    assert first is not None and second is not None
    store.commit_update(30, first)
    store.commit_update(31, second)
    other = parse_telegram_update(
        {
            "update_id": 32,
            "message": {
                "message_id": 1,
                "text": "other",
                "from": {"id": 8, "is_bot": False},
                "chat": {"id": 9, "type": "private"},
            },
        },
        bot_account_id="9",
    )
    assert other is not None
    store.commit_update(32, other)

    lease_one = store.claim_inbox()
    lease_two = store.claim_inbox()
    assert lease_one is not None and lease_one.event_id == "telegram:9:30"
    assert lease_two is not None and lease_two.event_id == "telegram:9:32"
    store.record_submission(
        lease_one.event_id,
        binding_id="a" * 32,
        turn_ref="turn-30",
        outcome="silent",
        text="",
        work_ref="",
    )
    lease_three = store.claim_inbox()
    assert lease_three is not None and lease_three.event_id == "telegram:9:31"


def test_submission_and_deferred_result_enqueue_each_text_once(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    event = _event(40)
    assert event is not None
    store.commit_update(40, event)
    lease = store.claim_inbox()
    assert lease is not None

    store.record_submission(
        lease.event_id,
        binding_id="b" * 32,
        turn_ref="turn-40",
        outcome="deferred",
        text="I will follow up.",
        work_ref="work-40",
    )
    work = store.claim_work()
    assert work is not None
    assert work.binding_id == "b" * 32
    assert work.work_ref == "work-40"
    store.complete_work(
        work.event_id, status="completed", text="The follow-up is ready."
    )

    deliveries = []
    for _index in range(2):
        outbox = store.claim_outbox()
        assert outbox is not None
        deliveries.append(outbox.payload)
        store.mark_delivered(
            outbox.delivery_id, provider_receipt={"message_id": len(deliveries)}
        )
    assert store.claim_outbox() is None
    assert {item["text"] for item in deliveries} == {
        "I will follow up.",
        "The follow-up is ready.",
    }
    assert all(item["chat_id"] == "4" for item in deliveries)
    assert store.status_snapshot()["work_terminal"] == 1
