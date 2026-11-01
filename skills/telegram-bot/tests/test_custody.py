import json
import sqlite3

from telegram_bot.custody import CustodyStore
from telegram_bot.delivery import delivery_report
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
        work.work_ref, status="completed", text="The follow-up is ready."
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


def test_outbox_fifo_survives_retry_without_blocking_other_rooms(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    for key, chat in (("first", "1"), ("second", "1"), ("other", "2")):
        store.enqueue_outbox(key, {"kind": "message", "chat_id": chat, "text": key})
    assert store.claim_outbox().delivery_id == "first"
    store.release_outbox("first", reason="temporary", retry_after_sec=60)
    assert store.claim_outbox().delivery_id == "other"
    assert store.claim_outbox() is None
    store.mark_delivered("other", provider_receipt={"message_id": 3})
    # Terminal failure releases the next message in this same room.
    with store._connect() as conn:
        conn.execute("UPDATE outbox SET state='leased' WHERE delivery_id='first'")
    store.mark_outbox_failed("first", reason="retry exhausted")
    assert store.claim_outbox().delivery_id == "second"


def test_stale_leases_from_a_stopped_runtime_are_released_at_once(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    store.commit_update(40, _event(40))
    store.commit_update(41, _event(41))
    deferred = store.claim_inbox(lease_sec=1860)
    store.record_submission(
        deferred.event_id, binding_id="b" * 32, turn_ref="turn-40",
        outcome="deferred", text="", work_ref="work-40",
    )
    inbox = store.claim_inbox(lease_sec=1860)
    work = store.claim_work(lease_sec=120)
    assert store.enqueue_outbox("send-1", {"kind": "message", "chat_id": "4", "text": "a"})
    outbox = store.claim_outbox(lease_sec=120)
    assert inbox is not None and work is not None and outbox is not None
    assert store.enqueue_outbox("send-2", {"kind": "message", "chat_id": "5", "text": "b"})
    backoff = store.claim_outbox()
    assert backoff is not None and backoff.delivery_id == "send-2"
    store.release_outbox("send-2", reason="backoff", retry_after_sec=600)

    # The dead runtime's leases hold every queue until they expire.
    assert store.claim_inbox() is None
    assert store.claim_work() is None
    assert store.claim_outbox() is None

    assert store.release_own_leases() == {"inbox": 1, "outbox": 1, "presence_work": 1}
    again_inbox = store.claim_inbox()
    again_work = store.claim_work()
    again_outbox = store.claim_outbox()
    # Attempts are not reset: the reclaim counts exactly one more attempt.
    assert again_inbox.event_id == inbox.event_id and again_inbox.attempts == inbox.attempts + 1
    assert again_work.event_id == work.event_id and again_work.attempts == work.attempts + 1
    assert again_outbox.delivery_id == "send-1" and again_outbox.attempts == outbox.attempts + 1
    # The payload survives the release untouched; send checkpoints, reports
    # and receipts are pinned by the next test.
    assert again_outbox.payload == outbox.payload and again_work.event == work.event
    # Rows that were not leased keep their state and their backoff.
    assert store.claim_outbox() is None
    snapshot = store.status_snapshot()
    assert snapshot["inbox_submitted"] == 1 and snapshot["outbox_waiting"] == 1


def test_releasing_stale_leases_keeps_send_checkpoints_reports_and_receipts(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    reporting = {"version": 1, "account_id": "9", "origin": {"kind": "tool", "task_id": "task-1"}}
    # A finished delivery: provider receipt stored, its report acknowledged.
    assert store.enqueue_outbox("done-1", {
        "kind": "message", "chat_id": "4", "text": "done", "_reporting": reporting,
        "_rendered_chunks": [{"text": "done", "parse_mode": ""}],
    })
    done = store.claim_outbox()
    assert done is not None and done.delivery_id == "done-1"
    done_sent = [{"message_id": 70, "date": 1}]
    store.checkpoint_outbox("done-1", {**done.payload, "_sent_messages": done_sent}, report=delivery_report(
        "done-1", done.payload, part_id=0, state="delivered", receipt=done_sent[0], text="done", fmt="plain"))
    store.mark_delivered("done-1", provider_receipt={"kind": "message", "messages": done_sent})
    done_report = store.next_delivery_report()
    assert done_report is not None and done_report[0] == "done-1"
    store.finish_delivery_report("done-1", done_report[1])
    # The stopped runtime left this one mid-send: chunk 0 is confirmed and
    # checkpointed, its report is not yet acknowledged, chunk 1 was never sent.
    assert store.enqueue_outbox("send-1", {
        "kind": "message", "chat_id": "4", "text": "first second", "_reporting": reporting,
        "_rendered_chunks": [{"text": "first", "parse_mode": ""}, {"text": "second", "parse_mode": ""}],
    })
    lease = store.claim_outbox(lease_sec=120)
    assert lease is not None and lease.delivery_id == "send-1"
    sent = [{"message_id": 71, "date": 2}]
    report = delivery_report(
        "send-1", lease.payload, part_id=0, state="delivered", receipt=sent[0], text="first", fmt="plain")
    store.checkpoint_outbox("send-1", {**lease.payload, "_sent_messages": sent}, report=report)
    checkpoint = store.outbox_payload("send-1")
    pending_report = store.next_delivery_report()
    send_reports = store.delivery_receipt("send-1")["history_reports"]
    done_receipt = store.delivery_receipt("done-1")
    done_payload = store.outbox_payload("done-1")
    assert checkpoint["_sent_messages"] == sent
    assert pending_report == ("send-1", 0, report) and report["state"] == "delivered"
    assert [entry["acked"] for entry in send_reports] == [False]
    assert done_receipt["state"] == "delivered"
    assert done_receipt["provider_receipt"] == {"kind": "message", "messages": done_sent}
    assert [entry["acked"] for entry in done_receipt["history_reports"]] == [True]
    assert store.claim_outbox() is None

    assert store.release_own_leases() == {"inbox": 0, "outbox": 1, "presence_work": 0}

    after = store.delivery_receipt("send-1")
    assert after["state"] == "pending"
    assert store.outbox_payload("send-1") == checkpoint
    assert store.outbox_payload("send-1")["_sent_messages"] == sent
    assert store.next_delivery_report() == pending_report
    assert after["history_reports"] == send_reports
    assert store.delivery_receipt("done-1") == done_receipt
    assert store.outbox_payload("done-1") == done_payload and done_payload["_sent_messages"] == done_sent
    # The reclaim resumes after the confirmed chunk instead of resending it.
    again = store.claim_outbox()
    assert again is not None and again.delivery_id == "send-1"
    assert again.payload["_sent_messages"] == sent and again.attempts == lease.attempts + 1


def test_opening_the_store_for_status_or_tools_keeps_live_leases(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    store.commit_update(50, _event(50))
    lease = store.claim_inbox(lease_sec=1860)
    assert store.enqueue_outbox("send-1", {"kind": "message", "chat_id": "4", "text": "a"})
    assert store.claim_outbox(lease_sec=120) is not None
    assert lease is not None

    # A second handle (status view, telegram_send tool) must not steal leases.
    reader = CustodyStore(tmp_path / "custody.sqlite3")
    reader.status_snapshot()
    assert reader.enqueue_outbox("send-2", {"kind": "message", "chat_id": "4", "text": "b"})
    assert reader.claim_inbox() is None
    assert reader.claim_outbox() is None
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
        states = dict(conn.execute("SELECT delivery_id, state FROM outbox"))
        inbox_state = conn.execute("SELECT state FROM inbox").fetchone()[0]
    assert states == {"send-1": "leased", "send-2": "pending"}
    assert inbox_state == "leased"
