from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import InboundWorker, OutboundWorker, _worker_loop
from lib.slack_api import SlackApiError
from lib.store import BridgeStore
from test_workers_plugin import _Api, _load_plugin


def _enqueue_inbox(store):
    payload = {"type": "events_api", "envelope_id": "envelope-1", "payload": {
        "team_id": "T1", "event_id": "event-1", "event": {
            "type": "message", "user": "U1", "channel": "D1", "channel_type": "im",
            "ts": "1.0", "text": "Hello",
        },
    }}
    store.ingest_envelope(payload, parse_socket_envelope(payload))


def _host(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    host = LoopbackPresenceHostAdapter(binding_id="a" * 32, host_service_url="http://127.0.0.1:8767",
                                       skill_token="fixture-host-token", http_client=http)
    return host, http


class Slack:
    def __init__(self, error=None):
        self.sent = []
        self.resolved = []
        self.error = error

    async def user_info(self, user_id): return {"id": user_id}
    async def conversation_info(self, channel_id): return {"id": channel_id}

    async def resolve_target(self, target):
        self.resolved.append(target)
        return "D_RESOLVED" if target.startswith("U") else target

    async def post_message(self, **kwargs):
        self.sent.append(kwargs)
        if self.error:
            raise self.error
        return {"ok": True, "channel": kwargs["channel"], "ts": f"2.{len(self.sent)}",
                "message": {"text": kwargs["text"], "user": "U_BOT"}}


def _due(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET available_at=0,report_available_at=0,report_lease_until=0")
        db.execute("UPDATE inbox SET available_at=0")


@pytest.mark.parametrize("advertised,echoed,expected", [(None, None, 0), (1, 0, 0), (1, 1, 1)])
def test_identity_opt_in_and_original_turn_echo_own_automatic_mode(tmp_path, advertised, echoed, expected):
    async def run():
        store = BridgeStore(tmp_path)
        _enqueue_inbox(store)
        requests = []
        def handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"presence_delivery_version": advertised} if advertised else {})
            body = json.loads(request.content)
            requests.append(body)
            assert (body.get("delivery_reporting_version") == 1) is (advertised == 1)
            if not advertised: assert "delivery_reporting_version" not in body
            result = {"status": "completed", "outcome": "message", "text": "Answer", "turn_ref": "turn-1"}
            if echoed is not None: result["delivery_reporting_version"] = echoed
            return httpx.Response(200, json=result)
        host, http = _host(handler)
        try:
            assert await InboundWorker(store, Slack(), host, staged_root=tmp_path / "staged").process_once()
            item = store.claim_outbox()
            assert item.delivery_reporting_version == expected
            assert item.origin == {"kind": "automatic", "task_id": "turn-1", "source_event_id": "event-1"}
            assert len(requests) == 1
        finally:
            await http.aclose()
    asyncio.run(run())


def test_deferred_ack_and_final_keep_original_mode_across_restart(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        _enqueue_inbox(store)
        polls = []
        def handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"presence_delivery_version": 1})
            if request.url.path == "/presence/turn":
                return httpx.Response(200, json={"status": "completed", "outcome": "deferred", "text": "Working",
                                                "turn_ref": "turn-1", "work_ref": "work-1", "delivery_reporting_version": 1})
            polls.append(request)
            if len(polls) == 1:
                return httpx.Response(200, json={"status": "pending", "work_ref": "work-1", "delivery_reporting_version": 1})
            return httpx.Response(200, json={"status": "completed", "outcome": "message", "text": "Done",
                                            "work_ref": "work-1", "turn_ref": "work-1", "delivery_reporting_version": 1})
        host, http = _host(handler)
        try:
            await InboundWorker(store, Slack(), host, staged_root=tmp_path / "staged").process_once()
            ack = store.claim_outbox()
            assert ack.text == "Working" and ack.delivery_reporting_version == 1
            store.complete_outbox(ack.row_id, ack.lease_token)
            _due(store)
            # New adapter instance has not discovered any capability. The saved
            # deferred receipt, not this new instance, owns the original mode.
            resumed, resumed_http = _host(handler)
            try:
                await InboundWorker(BridgeStore(tmp_path), Slack(), resumed, staged_root=tmp_path / "staged").process_once()
                final = store.claim_outbox()
                assert final.text == "Done" and final.delivery_reporting_version == 1
                assert final.origin["task_id"] == "work-1"
            finally:
                await resumed_http.aclose()
        finally:
            await http.aclose()
    asyncio.run(run())


def test_legacy_deferred_reference_is_not_upgraded_by_new_host():
    async def run():
        legacy = {"status": "deferred", "text": "Working", "turn_ref": "turn-0", "work_ref": "work-0"}
        reference = "deferred:" + base64.urlsafe_b64encode(json.dumps(legacy).encode()).decode().rstrip("=")
        host, http = _host(lambda _: httpx.Response(200, json={"status": "completed", "outcome": "message", "text": "Done",
                                                             "work_ref": "work-0", "delivery_reporting_version": 0}))
        host.delivery_reporting_version = 1
        try:
            assert (await host.status(reference)).delivery_reporting_version == 0
            assert (await host.deliver(reference)).delivery_reporting_version == 0
        finally: await http.aclose()
    asyncio.run(run())


def test_explicit_send_captures_compact_origin_and_reports_resolved_physical_receipt(tool_json, tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(presence_delivery_version=1, history_reporting_state="supported", workspace_id="T1")
        plugin = _load_plugin()
        api = _Api(tmp_path)
        plugin.register(api)
        context = SimpleNamespace(task_id="turn-1", task_metadata={"presence": {"event": {"source_event_id": "event-1"}}},
                                  unrelated_private_field="must-not-be-captured")
        result = tool_json(api.tools["slack_send"][0](context, channel_or_user="U1", text="**Early reply**",
                                                     thread_ts="1.0", request_id="logical-1"))
        assert result["state"] == "queued"
        assert store.status()["delivery_reports_pending"] == 0
        reports = []
        def handler(request):
            assert request.url.path == "/presence/delivery"
            assert request.headers["X-Skill-Token"] == "fixture-host-token"
            assert store.status()["outbox_delivered"] == 1
            reports.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "recorded": True})
        host, http = _host(handler)
        slack = Slack()
        try:
            worker = OutboundWorker(store, slack, host)
            await worker.process_once()
            assert not reports and store.status()["delivery_reports_pending"] == 1
            await worker.process_once()
            await worker._report_task
            assert len(slack.sent) == 1 and slack.resolved == ["U1"]
            assert store.status()["delivery_reports_acked"] == 1
        finally:
            await worker.aclose()
            await http.aclose()
        report = reports[0]
        assert set(report) == {"schema_version", "delivery_id", "part_id", "state", "provider", "account_id",
                               "conversation_id", "thread_id", "text", "format", "message", "origin"}
        assert report["delivery_id"] == "logical-1" and report["part_id"] == "0"
        assert report["state"] == "delivered" and report["account_id"] == "T1"
        assert report["conversation_id"] == "D_RESOLVED" and report["thread_id"] == "1.0"
        assert report["text"] == "**Early reply**" and report["format"] == "markdown"
        assert report["message"]["provider_message_id"] == "2.1"
        assert "provider_message" not in report["message"]
        assert report["origin"] == {"kind": "tool", "task_id": "turn-1", "source_event_id": "event-1"}
        assert "must-not-be-captured" not in json.dumps(report)
    asyncio.run(run())


def test_report_timeout_does_not_resend_or_block_next_provider_row(tmp_path):
    async def run():
        store, slack = BridgeStore(tmp_path), Slack()
        store.set_runtime(workspace_id="T1")
        for number in (1, 2):
            store.enqueue_outbox(request_id=f"send-{number}", target="D1", thread_ts="1.0", chunks=[f"text-{number}"],
                                 origin={"kind": "tool"}, delivery_reporting_version=1)
        reports, accepted = [], set()
        def handler(request):
            payload = json.loads(request.content)
            reports.append(payload)
            accepted.add((payload["delivery_id"], payload["part_id"], payload["state"]))
            if len(reports) == 1: raise httpx.ReadTimeout("ACK lost", request=request)
            return httpx.Response(200, json={"ok": True, "recorded": True, "duplicate": len(reports) > 1})
        host, http = _host(handler)
        try:
            worker = OutboundWorker(store, slack, host)
            await worker.process_once()  # First provider send, no report yet.
            await worker.process_once()  # First report loses ACK; next send still runs.
            await worker._report_task
            assert len(slack.sent) == 2
            frozen = reports[0]
            store.set_runtime(workspace_id="T_OTHER")
            _due(store)
            restarted = OutboundWorker(BridgeStore(tmp_path), slack, host)
            await restarted.process_once()
            await restarted._report_task
            await restarted.process_once()
            await restarted._report_task
            assert reports[1] == frozen
            assert len(slack.sent) == 2 and len(accepted) == 2
            assert store.status()["delivery_reports_acked"] == 2
        finally:
            await worker.aclose()
            await restarted.aclose()
            await http.aclose()
    asyncio.run(run())


def test_crash_after_host_acceptance_retries_report_without_provider_send(tmp_path):
    async def run():
        store, slack = BridgeStore(tmp_path), Slack()
        store.set_runtime(workspace_id="T1")
        store.enqueue_outbox(request_id="crash", target="D1", thread_ts="", chunks=["Done"],
                             origin={"kind": "tool"}, delivery_reporting_version=1)
        reports = []
        def handler(request):
            reports.append(json.loads(request.content))
            if len(reports) == 1: raise asyncio.CancelledError()
            return httpx.Response(200, json={"ok": True, "recorded": True, "duplicate": True})
        host, http = _host(handler)
        try:
            worker = OutboundWorker(store, slack, host)
            await worker.process_once()
            await worker.process_once()
            with pytest.raises(asyncio.CancelledError): await worker._report_task
            _due(store)
            restarted = OutboundWorker(BridgeStore(tmp_path), slack, host)
            await restarted.process_once()
            await restarted._report_task
            assert reports[0] == reports[1] and len(slack.sent) == 1
            assert store.status()["delivery_reports_acked"] == 1
        finally:
            await worker.aclose()
            await restarted.aclose()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("error,state", [
    (SlackApiError("invalid_arguments"), "failed"),
    (httpx.ReadTimeout("unknown"), "uncertain"),
    (SlackApiError("http_503", status_code=503), "uncertain"),
    (SlackApiError("invalid_json", status_code=200), "uncertain"),
])
def test_terminal_provider_failure_is_not_reported_as_delivered(tmp_path, error, state):
    async def run():
        store, slack = BridgeStore(tmp_path), Slack(error)
        store.set_runtime(workspace_id="T1")
        store.enqueue_outbox(request_id="failed", target="D1", thread_ts="", chunks=["Attempted"],
                             origin={"kind": "tool"}, delivery_reporting_version=1)
        worker = OutboundWorker(store, slack)
        for _ in range(5):
            await worker.process_once()
            _due(store)
        report = store.claim_report()["payload"]
        assert report["state"] == state and report["message"]["error"]
        assert store.status()["outbox_delivered"] == 0
        assert len(slack.sent) == (1 if state == "uncertain" else 5)
    asyncio.run(run())


def test_migrated_automatic_rows_and_old_terminal_rows_do_not_report(tmp_path):
    store = BridgeStore(tmp_path)
    store.enqueue_outbox(request_id="old-terminal", target="D1", thread_ts="", chunks=["Already sent"])
    old_terminal = store.claim_outbox()
    store.complete_outbox(old_terminal.row_id, old_terminal.lease_token, provider_message_ts="old-terminal-ts")
    store.enqueue_outbox(request_id="old", target="D1", thread_ts="", chunks=["Old"])
    # Recreate the published pre-reporting schema by removing the additive columns.
    names = ("origin_json", "delivery_reporting_version", "resolved_channel", "provider_account_id",
             "report_payload_json", "report_state", "report_lease_token", "report_lease_until",
             "report_attempts", "report_available_at", "report_error")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP INDEX outbox_report_work")
        for name in names: db.execute(f"ALTER TABLE outbox DROP COLUMN {name}")
    migrated = BridgeStore(tmp_path)
    item = migrated.claim_outbox()
    assert item.delivery_reporting_version == 0 and item.origin == {}
    migrated.complete_outbox(item.row_id, item.lease_token, provider_message_ts="old-ts")
    assert BridgeStore(tmp_path).claim_report() is None


def test_partial_chunks_report_individual_physical_results(tmp_path):
    async def run():
        class PartialSlack(Slack):
            async def post_message(self, **kwargs):
                if kwargs["text"] == "bad part":
                    self.sent.append(kwargs)
                    raise SlackApiError("invalid_arguments")
                return await super().post_message(**kwargs)
        store, slack = BridgeStore(tmp_path), PartialSlack()
        store.set_runtime(workspace_id="T1")
        store.enqueue_outbox(request_id="three-parts", target="D1", thread_ts="1.0",
                             chunks=["first part", "bad part", "last part"], origin={"kind": "automatic"},
                             delivery_reporting_version=1)
        worker = OutboundWorker(store, slack)
        for _ in range(7):
            await worker.process_once()
            _due(store)
        payloads = []
        while report := store.claim_report():
            payloads.append(report["payload"])
            store.finish_report(report)
        assert [(p["part_id"], p["state"], p["text"]) for p in payloads] == [
            ("0", "delivered", "first part"), ("1", "failed", "bad part"), ("2", "delivered", "last part")]
        assert all(p["message"]["chunk_count"] == 3 for p in payloads)
        assert store.status()["outbox_delivered"] == 2 and store.status()["outbox_failed"] == 1
    asyncio.run(run())


def test_explicit_send_on_legacy_host_keeps_provider_sending_and_discloses_gap(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(presence_delivery_version=0, history_reporting_state="unsupported",
                          history_reporting_limitation="Host delivery reporting unavailable", workspace_id="T1")
        plugin = _load_plugin()
        api = _Api(tmp_path)
        plugin.register(api)
        api.tools["slack_send"][0](channel_or_user="D1", text="Old Host", request_id="old-host")
        slack = Slack()
        await OutboundWorker(store, slack).process_once()
        assert len(slack.sent) == 1 and store.claim_report() is None
        assert store.status()["history_reporting_state"] == "unsupported"
        assert store.status()["history_reporting_limitation"]
    asyncio.run(run())


def test_report_requires_recorded_ack_not_just_http_ok(tmp_path):
    async def run():
        store, slack = BridgeStore(tmp_path), Slack()
        store.set_runtime(workspace_id="T1")
        store.enqueue_outbox(request_id="ack-contract", target="D1", thread_ts="", chunks=["Done"],
                             origin={"kind": "tool"}, delivery_reporting_version=1)
        response = {"ok": True}
        host, http = _host(lambda _: httpx.Response(200, json=response))
        try:
            worker = OutboundWorker(store, slack, host)
            await worker.process_once()
            await worker.process_once()
            await worker._report_task
            assert store.status()["delivery_reports_acked"] == 0
            assert store.status()["delivery_reports_pending"] == 1
            assert "not acknowledged" in store.status()["last_report_error"]
            response["recorded"] = True
            _due(store)
            await worker.process_once()
            await worker._report_task
            assert store.status()["delivery_reports_acked"] == 1 and len(slack.sent) == 1
        finally:
            await worker.aclose()
            await http.aclose()
    asyncio.run(run())


def test_one_worker_sends_two_later_messages_while_host_report_is_blocked(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        entered, release, sent_three, stop = (asyncio.Event() for _ in range(4))
        reports = []
        class ProgressSlack(Slack):
            async def post_message(self, **kwargs):
                result = await super().post_message(**kwargs)
                if len(self.sent) == 3: sent_three.set()
                return result
        async def blocked_host(request):
            reports.append(json.loads(request.content))
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True, "recorded": True})
        def enqueue(number):
            store.enqueue_outbox(request_id=f"slow-host-{number}", target="D1", thread_ts="1.0",
                                 chunks=[f"text-{number}"], origin={"kind": "tool"}, delivery_reporting_version=1)
        slack = ProgressSlack()
        host, http = _host(blocked_host)
        worker = OutboundWorker(store, slack, host)
        enqueue(1)
        running = asyncio.create_task(_worker_loop(worker, stop))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            pending_report = worker._report_task
            assert pending_report is not None and not pending_report.done()
            enqueue(2)
            enqueue(3)
            await asyncio.wait_for(sent_three.wait(), 1)
            assert not release.is_set() and len(reports) == 1
            assert worker._report_task is pending_report
            assert store.status()["outbox_delivered"] == 3
            release.set()
            async def settled():
                while store.status()["delivery_reports_acked"] != 3:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(settled(), 2)
            assert len(slack.sent) == 3 and len(reports) == 3
        finally:
            stop.set()
            await asyncio.wait_for(running, 1)
            await http.aclose()
        assert worker._report_task is None
    asyncio.run(run())


@pytest.mark.parametrize("cancel_worker", [False, True])
def test_worker_shutdown_cancels_and_awaits_inflight_report(tmp_path, cancel_worker):
    async def run():
        store, slack = BridgeStore(tmp_path), Slack()
        store.set_runtime(workspace_id="T1")
        store.enqueue_outbox(request_id="stop-pending", target="D1", thread_ts="", chunks=["Done"],
                             origin={"kind": "tool"}, delivery_reporting_version=1)
        entered, cancelled, stop = (asyncio.Event() for _ in range(3))
        async def blocked_host(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        host, http = _host(blocked_host)
        worker = OutboundWorker(store, slack, host)
        running = asyncio.create_task(_worker_loop(worker, stop))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            report_task = worker._report_task
            if cancel_worker:
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(running, 1)
            else:
                stop.set()
                await asyncio.wait_for(running, 1)
            assert cancelled.is_set() and report_task.cancelled()
            assert worker._report_task is None
            assert not any(t.get_name() == "slack-delivery-report" and not t.done() for t in asyncio.all_tasks())
            assert len(slack.sent) == 1 and store.status()["outbox_delivered"] == 1
            assert store.status()["delivery_reports_pending"] == 1
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await http.aclose()
    asyncio.run(run())
