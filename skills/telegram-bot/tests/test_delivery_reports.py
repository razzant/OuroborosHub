import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from telegram_bot.api import TelegramApiError
from telegram_bot.custody import CustodyStore
from telegram_bot.events import parse_telegram_update
from telegram_bot.delivery import delivery_report
from telegram_bot.tools import make_operation_tool
from telegram_bot.host import PresenceHostClient, PresenceHostError
from test_formatted_delivery import Provider, runtime, send_tool, ready_again


class ReportingHost:
    delivery_reporting_status = "supported"

    def __init__(self, mode=1):
        self.mode, self.calls, self.records = mode, [], {}
        self.lose_ack = False
        self.entered = self.release = None

    async def discover_delivery_support(self):
        self.delivery_reporting_status = "supported" if self.mode else "unsupported"
        return self.mode

    async def report_delivery(self, report):
        self.calls.append(copy.deepcopy(report))
        key = report["delivery_id"], report["part_id"], report["state"]
        self.records.setdefault(key, report)
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.lose_ack:
            self.lose_ack = False
            raise OSError("ACK lost after canonical acceptance")


def worker(path, provider, host):
    value = runtime(path, provider)
    value.submitter, value._bot_id = host, "9"
    return value


def reports_ready(store):
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT delivery_id,reports_json FROM outbox").fetchall()
        for delivery_id, raw in rows:
            entries = json.loads(raw)
            for entry in entries:
                entry["available_at"] = 1
            db.execute("UPDATE outbox SET reports_json=?,report_due_at=1 WHERE delivery_id=?", (json.dumps(entries), delivery_id))


def test_report_ack_loss_restart_never_resends_and_payload_is_frozen(tmp_path):
    async def run():
        provider, host = Provider(), ReportingHost()
        ctx = SimpleNamespace(task_id="task1", task_metadata={"presence": {"event": {"source_event_id": "source1"}}})
        send_tool(tmp_path)(ctx, chat_id="-42", text="**Hello**", request_id="once")
        first = worker(tmp_path, provider, host)
        assert first.store.next_delivery_report() is None
        await first.process_one_outbox()
        assert first.store.delivery_receipt("telegram-send:once")["state"] == "delivered"
        host.lose_ack = True
        await first.process_one_delivery_report()
        assert len(provider.calls) == 1 and len(host.records) == 1
        reports_ready(first.store)
        restarted = worker(tmp_path, provider, host)
        assert not await restarted.process_one_outbox()
        await restarted.process_one_delivery_report()
        assert len(provider.calls) == 1 and len(host.records) == 1
        assert host.calls[0] == host.calls[1]
        report = host.calls[0]
        assert len(report) == 12 and "binding_id" not in report
        assert report["text"] == "<b>Hello</b>" and report["format"] == "html"
        assert report["conversation_id"] == "-42" and report["account_id"] == "9"
        assert report["origin"] == {"kind": "tool", "task_id": "task1", "source_event_id": "source1"}
        assert restarted.store.next_delivery_report() is None
    asyncio.run(run())


def test_operation_report_matches_host_validator_and_keeps_source_in_message(tmp_path):
    validator = pytest.importorskip("ouroboros.presence_delivery").validate_delivery
    payload = {
        "kind": "operation", "chat_id": "-42", "method": "editMessageText",
        "parameters": {"chat_id": "-42", "message_id": 7, "text": "fixed"},
        "text": "fixed", "original_delivery_id": "telegram-send:original",
        "_reporting": {"version": 1, "account_id": "999",
                       "origin": {"kind": "tool", "task_id": "task-1"}},
    }
    report = delivery_report("telegram-operation:edit", payload,
                             part_id="0", state="delivered",
                             receipt={"message_id": 7}, text="fixed", fmt="plain")
    assert validator(report) == report
    assert report["message"]["original_delivery_id"] == "telegram-send:original"
    assert report["message"]["replacement_text"] == "fixed"
    assert report["origin"] == {"kind": "tool", "task_id": "task-1"}
    assert report["text"] == ""  # an edit does not create new spoken text


def test_registered_operation_runtime_report_is_host_valid_and_original_is_unchanged(tmp_path):
    validator = pytest.importorskip("ouroboros.presence_delivery").validate_delivery
    async def run():
        provider, host = Provider(), ReportingHost()
        w = worker(tmp_path, provider, host)
        store = w.store
        store.enqueue_outbox("telegram-send:original", {"kind": "message", "chat_id": "-42", "text": "old"})
        original_before = store.outbox_payload("telegram-send:original")
        operation = make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(tmp_path)))
        queued = operation(method="editMessageText", chat_id="-42", message_id=7,
                           text="fixed", request_id="edit", original_delivery_id="telegram-send:original")
        assert queued["state"] == "queued"
        assert await w.process_one_outbox()  # original send
        assert await w.process_one_outbox()  # edit
        assert store.outbox_payload("telegram-send:original")["text"] == original_before["text"]
        receipt = store.delivery_receipt("telegram-operation:edit")
        assert receipt["state"] == "delivered"
        report = receipt["history_reports"][0]["payload"]
        assert validator(report) == report
        assert report["part_id"] == "0" and report["text"] == ""
        assert report["message"]["original_delivery_id"] == "telegram-send:original"
        assert report["message"]["replacement_text"] == "fixed"
        assert report["origin"] == {"kind": "tool"}
        await w.process_one_delivery_report()
        await w.process_one_delivery_report()
        assert any(row["delivery_id"] == "telegram-operation:edit" for row in host.calls)
    asyncio.run(run())


def test_blocked_report_does_not_block_single_provider_worker(tmp_path):
    async def run():
        host, provider = ReportingHost(), Provider()
        host.entered, host.release = asyncio.Event(), asyncio.Event()
        w = worker(tmp_path, provider, host)
        send_tool(tmp_path)(chat_id="4", text="First", request_id="first")
        await w.process_one_outbox()
        sent = asyncio.Event()
        original = provider.record
        def record(endpoint, payload):
            result = original(endpoint, payload)
            if len(provider.calls) == 2:
                sent.set()
            return result
        provider.record = record
        task = asyncio.create_task(w._outbox_worker())
        try:
            await asyncio.wait_for(host.entered.wait(), 1)
            send_tool(tmp_path)(chat_id="4", text="Second", request_id="second")
            await asyncio.wait_for(sent.wait(), 1)
            assert not host.release.is_set()
            assert len(provider.calls) == 2
        finally:
            w._stopping = True
            host.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_partial_chunks_report_only_success_and_remaining_failure(tmp_path):
    async def run():
        provider = Provider([None] + [TelegramApiError("sendMessage", "rejected", 400)] * 10)
        host = ReportingHost()
        w = worker(tmp_path, provider, host)
        send_tool(tmp_path)(chat_id="4", text="x" * 4096 + "y" * 100, markdown=False, request_id="partial")
        for _ in range(5):
            await w.process_one_outbox()
            ready_again(w.store)
        while await w.process_one_delivery_report():
            pass
        speech = [r for r in host.calls if r["state"] == "delivered"]
        status = [r for r in host.calls if r["state"] == "failed"]
        assert len(speech) == 1 and speech[0]["text"] == "x" * 4096
        assert len(status) == 1 and status[0]["text"] == ""
        assert status[0]["message"]["confirmed_parts"] == 1
    asyncio.run(run())


def test_media_without_caption_and_plain_fallback_keep_actual_wire(tmp_path):
    async def run():
        path = tmp_path / "report.pdf"
        path.write_bytes(b"fixture")
        host, provider = ReportingHost(), Provider()
        w = worker(tmp_path, provider, host)
        send_tool(tmp_path)(chat_id="4", kind="document", file_path=str(path), request_id="file")
        await w.process_one_outbox()
        await w.process_one_delivery_report()
        assert host.calls[0]["text"] == ""
        assert host.calls[0]["message"]["file_name"] == "report.pdf"
        provider.responses = [TelegramApiError("sendMessage", "bad HTML", 400), None]
        send_tool(tmp_path)(chat_id="4", text="**Bold**", request_id="fallback")
        await w.process_one_outbox()
        await w.process_one_delivery_report()
        assert host.calls[-1]["text"] == "Bold" and host.calls[-1]["format"] == "plain"
    asyncio.run(run())


@pytest.mark.parametrize("mode", [0, 1])
def test_automatic_and_deferred_rows_keep_original_echoed_mode(tmp_path, mode):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    event = parse_telegram_update({"update_id": 1, "message": {"message_id": 2, "from": {"id": 3}, "chat": {"id": 4}, "text": "hi"}}, bot_account_id="9")
    store.commit_update(1, event)
    item = store.claim_inbox()
    store.record_submission(item.event_id, binding_id="a" * 32, turn_ref="turn1", outcome="deferred", text="Early", work_ref="work1", delivery_reporting_version=mode)
    early = store.claim_outbox()
    assert early.payload["_reporting"]["version"] == mode
    store.mark_delivered(early.delivery_id, provider_receipt={})
    work = store.claim_work()
    store.complete_work(work.work_ref, status="completed", text="Final")
    final = store.claim_outbox()
    assert final.payload["_reporting"] == early.payload["_reporting"]


def test_old_host_and_legacy_terminal_rows_do_not_report(tmp_path):
    async def run():
        host, provider = ReportingHost(0), Provider()
        w = worker(tmp_path, provider, host)
        w.store.enqueue_outbox("legacy", {"chat_id": "4", "text": "Old"})
        send_tool(tmp_path)(chat_id="4", text="New on old host", request_id="new")
        await w.process_one_outbox()
        await w.process_one_outbox()
        assert not await w.process_one_delivery_report()
        assert len(provider.calls) == 2
        assert w.store.delivery_receipt("telegram-send:new")["delivery_reporting"]["status"] == "unsupported"
    asyncio.run(run())


@pytest.mark.parametrize("advertised,echo", [(0, 0), (1, 1), (1, 0)])
def test_handshake_and_cached_turn_use_actual_echo(tmp_path, advertised, echo):
    class Transport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            if url.endswith("/identity"):
                return {"ok": True, "presence_delivery_version": advertised}
            assert (payload.get("delivery_reporting_version") == 1) is bool(advertised)
            return {"status": "completed", "outcome": "message", "text": "answer", "delivery_reporting_version": echo}
    (tmp_path / "settings.json").write_text(json.dumps({"binding_id": "a" * 32}), encoding="utf-8")
    event = parse_telegram_update({"update_id": 1, "message": {"message_id": 2, "from": {"id": 3}, "chat": {"id": 4}, "text": "hi"}}, bot_account_id="9")
    host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "test", host_base="http://127.0.0.1:8767", transport=Transport())
    assert asyncio.run(host.submit(event.to_dict(), [])).delivery_reporting_version == echo


def test_report_requires_positive_recorded_ack(tmp_path):
    class Transport:
        async def request_json(self, *args, **kwargs):
            return {"ok": True, "recorded": False}
    host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "test", host_base="http://127.0.0.1:8767", transport=Transport())
    with pytest.raises(PresenceHostError, match="acknowledged"):
        asyncio.run(host.report_delivery({}))


def test_migration_does_not_import_terminal_legacy_receipts(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    store.enqueue_outbox("old", {"chat_id": "4", "text": "Old"})
    store.claim_outbox()
    store.mark_delivered("old", provider_receipt={"message_id": 7})
    with sqlite3.connect(store.path) as db:
        db.execute("DROP INDEX outbox_reports_due")
        db.execute("ALTER TABLE outbox DROP COLUMN reports_json")
        db.execute("ALTER TABLE outbox DROP COLUMN report_due_at")
    migrated = CustodyStore(store.path)
    assert migrated.delivery_receipt("old")["provider_receipt"] == {"message_id": 7}
    assert migrated.next_delivery_report() is None
    assert migrated.claim_outbox() is None
