"""Typed Host refusal custody through the real adapter, workers and SQLite store.

Host and Slack HTTP are scripted in process. These cases certify bridge consumer
behavior; the separate process fixture owns evidence for core-produced replies.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3

import httpx
import pytest

from test_presence_continuation import (
    Bridge, FakeHost, FakeSlack, _Crash, _origin, child_done, child_pending, completed, envelope,
)


class RefusalHost(FakeHost):
    """Use the ordinary scripted Host with per-event HTTP response statuses."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.turn_status: dict[str, int] = {}

    async def handle(self, request):
        response = await super().handle(request)
        if request.url.path == "/presence/turn":
            source = json.loads(request.content)["event"]["source_event_id"]
            return httpx.Response(self.turn_status.get(source, 200), json=response.json())
        return response


def refusal(_body, *, disposition="retry", work_ref="", error="author_dispatch_failed",
            code="presence_attempt_outcome_unknown", field="source_event_id"):
    # Host _presence_error does not echo negotiated protocol versions. Keep
    # its typed error shape exact and persist delivery mode outside this body.
    return {"ok": False, "error": error, "code": code, "disposition": disposition, "field": field,
            **({"work_ref": work_ref} if work_ref else {})}


def inbox_row(bridge, source="Ev-1"):
    with sqlite3.connect(bridge.store.path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM inbox WHERE event_id=?", (source,)).fetchone())


def assert_refusal_receipt(bridge, expected, *, source="Ev-1", http_status=409, mode=1):
    reference = inbox_row(bridge, source)["host_reference"]
    prefix, encoded = reference.split(":", 1)
    assert prefix == "refused"
    receipt = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert receipt["http_status"] == http_status and receipt["response"] == expected
    assert receipt["delivery_reporting_version"] == mode
    assert "status" not in receipt["response"] and "delivery_reporting_version" not in receipt["response"]
    return reference


@pytest.mark.parametrize("reporting", [True, False])
@pytest.mark.parametrize("disposition", ["rejected", "blocked", "retry"])
def test_409_admitted_child_survives_restart_and_releases_the_thread(tmp_path, reporting, disposition):
    async def run():
        host, slack = RefusalHost(reporting=reporting), FakeSlack()
        host.turn_status["Ev-1"] = 409
        codes = {"retry": "presence_attempt_outcome_unknown", "rejected": "presence_event_identity_conflict",
                 "blocked": "fixture_author_dispatch_failed"}
        host.scripts["Ev-1"] = lambda body: refusal(
            body, disposition=disposition, code=codes[disposition], work_ref="child-1")
        host.scripts["Ev-2"] = lambda body: completed("turn-2", body, text="New facts handled", output_ref="out-next")
        host.work["child-1"] = [child_pending("child-1"),
                                child_done("child-1", "Child result", output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            event = envelope("Ev-1", ts="1.0", text="Research this")
            await bridge.ingest(event)
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "pending"} and bridge.outbox() == []
            saved = assert_refusal_receipt(bridge, host.answers["Ev-1"], mode=int(reporting))
            assert disposition in bridge.inbox_error("Ev-1") and "author_dispatch_failed" in bridge.inbox_error("Ev-1")
            assert host.answers["Ev-1"]["code"] in bridge.inbox_error("Ev-1")
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="New facts"))
            # The original event now owns only child polling, so it must not
            # prevent another event in its conversation from reaching Host.
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "pending", "Ev-2": "delivered"}
            await bridge.send_all()
            await bridge.close()

            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["New facts handled", "Child result"]
            assert bridge.inbox() == {"Ev-1": "failed", "Ev-2": "delivered"}
            assert "author_dispatch_failed" in bridge.inbox_error("Ev-1")
            assert "HTTP 409" in bridge.inbox_error("Ev-1")
            assert inbox_row(bridge)["host_reference"] == saved
            assert_refusal_receipt(bridge, host.answers["Ev-1"], mode=int(reporting))
            await bridge.ingest(event)  # Slack redelivery also cannot resubmit the original turn.
            bridge.due()
            assert await bridge.inbound.process_once() is False
            assert host.generations == ["Ev-1", "Ev-2"] and len(host.turn_bodies) == 2
            assert host.polls == ["child-1", "child-1"]
            assert [row["output_ref"] for row in bridge.outbox()] == ["out-next", "out-child"]
            assert bridge.outbox()[1]["origin"] == _origin("Ev-1", "child-1")
            assert {post["thread_ts"] for post in slack.posts} == {"1.0"}
            assert {row["mode"] for row in bridge.outbox()} == {int(reporting)}
            assert [report["message"]["output_ref"] for report in host.reports] == (
                ["out-next", "out-child"] if reporting else [])
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("output_ref", ["out-child", ""])
def test_409_already_completed_child_has_one_durable_output_identity(tmp_path, output_ref):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        for source in ("Ev-1", "Ev-2"):
            host.turn_status[source] = 409
            host.scripts[source] = lambda body: refusal(body, work_ref="child-1")
        host.work["child-1"] = [child_done("child-1", "Child result", output_ref=output_ref)]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Child result"] and bridge.inbox() == {"Ev-1": "failed"}
            first = bridge.outbox()[0]
            assert first["request_id"].startswith("presence-output:")
            assert first["output_ref"] == output_ref
            await bridge.close()

            bridge = Bridge(tmp_path, host, slack)
            # Host may expose the same admitted work to another source event.
            # Its identity and destination still select the existing outbox row.
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Where is that result?"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Child result"] and len(bridge.outbox()) == 1
            assert bridge.outbox()[0]["request_id"] == first["request_id"]
            assert bridge.inbox() == {"Ev-1": "failed", "Ev-2": "failed"}
            for source in ("Ev-1", "Ev-2"):
                assert_refusal_receipt(bridge, host.answers[source], source=source)
                assert "author_dispatch_failed" in bridge.inbox_error(source)
            assert len(host.turn_bodies) == 2 and host.polls == ["child-1", "child-1"]
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("state,outcome", [
    ("completed", "silent"), ("completed", "tool_delivered"), ("failed", "silent"), ("cancelled", "silent"),
])
def test_409_child_terminal_outcome_never_erases_original_refusal(tmp_path, state, outcome):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda body: refusal(body, work_ref="child-1")
        host.work["child-1"] = [(200, {"status": state, "outcome": outcome, "work_ref": "child-1",
                                      "text": "Not owed by the bridge", "error": "child_ended"})]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert bridge.inbox() == {"Ev-1": "failed"} and bridge.outbox() == [] and slack.attempts == 0
            assert "author_dispatch_failed" in bridge.inbox_error("Ev-1")
            if state != "completed":
                assert "child_ended" in bridge.inbox_error("Ev-1")
            saved = assert_refusal_receipt(bridge, host.answers["Ev-1"])
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once() is False
            assert inbox_row(bridge)["host_reference"] == saved and len(host.turn_bodies) == 1
        finally:
            await bridge.close()

    asyncio.run(run())


def test_409_poll_errors_keep_exact_receipt_and_event_through_restarts(tmp_path):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda body: refusal(body, work_ref="child-1")
        host.work["child-1"] = [(500, {"ok": False}), child_pending("child-1"),
                                child_done("child-1", "Child result", output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            event = envelope("Ev-1", ts="1.0", text="Original event, kept in full")
            await bridge.ingest(event)
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "pending"}
            assert "HTTP 500" in bridge.inbox_error("Ev-1")
            before = inbox_row(bridge)
            saved = assert_refusal_receipt(bridge, host.answers["Ev-1"])
            for expected_state in ("pending", "failed"):
                await bridge.close()
                bridge = Bridge(tmp_path, host, slack)
                bridge.due()
                assert await bridge.inbound.process_once()
                assert bridge.inbox() == {"Ev-1": expected_state}
                assert "author_dispatch_failed" in bridge.inbox_error("Ev-1")
                after = inbox_row(bridge)
                assert after["host_reference"] == saved
                for key in ("raw_json", "provider_context_json", "transport_queue_json"):
                    assert after[key] == before[key]
            await bridge.send_all()
            assert slack.texts() == ["Child result"] and len(host.turn_bodies) == 1
            assert host.polls == ["child-1"] * 3
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("reporting", [True, False])
def test_409_child_unknown_send_is_not_repeated_after_checkpoint_crash(tmp_path, monkeypatch, reporting):
    async def run():
        host, slack = RefusalHost(reporting=reporting), FakeSlack(lose_posts=True)
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda body: refusal(body, work_ref="child-1")
        host.work["child-1"] = [child_done("child-1", "Child result", output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            def crash(*_args, **_kwargs):
                raise _Crash()

            monkeypatch.setattr(bridge.store, "fail_inbox", crash)
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            with pytest.raises(_Crash):
                await bridge.inbound.process_once()
            # The child output was durably queued before the original event's
            # terminal checkpoint. A lost provider reply must not undo custody.
            saved = assert_refusal_receipt(bridge, host.answers["Ev-1"], mode=int(reporting))
            await bridge.send_all()
            assert slack.attempts == 1 and [row["state"] for row in bridge.outbox()] == ["uncertain"]
            request_id = bridge.outbox()[0]["request_id"]
            await bridge.close()
            with sqlite3.connect(bridge.store.path) as db:
                db.execute("UPDATE inbox SET lease_until=0, available_at=0")
            bridge = Bridge(tmp_path, host, slack)
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert bridge.inbox() == {"Ev-1": "failed"}
            assert inbox_row(bridge)["host_reference"] == saved
            assert "author_dispatch_failed" in bridge.inbox_error("Ev-1")
            assert slack.attempts == 1 and [row["request_id"] for row in bridge.outbox()] == [request_id]
            assert host.polls == ["child-1", "child-1"] and len(host.turn_bodies) == 1
            assert [(report["state"], report["message"]["output_ref"]) for report in host.reports] == (
                [("uncertain", "out-child")] if reporting else [])
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("http_status", [400, 403, 404, 409, 429, 500, 503])
@pytest.mark.parametrize("disposition", ["rejected", "blocked"])
def test_typed_terminal_refusal_without_work_is_durable_and_not_retried(tmp_path, http_status, disposition):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = http_status
        host.scripts["Ev-1"] = lambda body: refusal(body, disposition=disposition, code="fixture_terminal_refusal")
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "failed"}
            assert disposition in bridge.inbox_error("Ev-1") and "author_dispatch_failed" in bridge.inbox_error("Ev-1")
            assert host.answers["Ev-1"]["code"] in bridge.inbox_error("Ev-1")
            saved = assert_refusal_receipt(bridge, host.answers["Ev-1"], http_status=http_status)
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once() is False
            assert inbox_row(bridge)["host_reference"] == saved
            assert host.polls == [] and len(host.turn_bodies) == 1 and bridge.outbox() == []
        finally:
            await bridge.close()

    asyncio.run(run())


def test_400_validation_refusal_without_optional_field_is_terminal(tmp_path):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 400
        # Exact direct _presence_error validation response; it has neither the
        # exception-only field nor any continuation/reporting negotiation data.
        payload = {"ok": False, "error": "invalid presence event", "code": "presence_event_invalid",
                   "disposition": "rejected"}
        host.scripts["Ev-1"] = lambda _body: payload
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "failed"}
            assert "presence_event_invalid" in bridge.inbox_error("Ev-1")
            assert "invalid presence event" in bridge.inbox_error("Ev-1")
            saved = assert_refusal_receipt(bridge, payload, http_status=400)
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once() is False
            assert inbox_row(bridge)["host_reference"] == saved
            assert len(host.turn_bodies) == 1 and host.polls == [] and bridge.outbox() == []
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("http_status", [400, 403, 404, 409, 429, 500, 503])
def test_typed_retry_without_work_has_no_attempt_cap_and_preserves_submission(tmp_path, http_status):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = http_status
        host.scripts["Ev-1"] = lambda body: refusal(body, disposition="retry", error="temporarily_unavailable",
                                                  code="presence_resources_unavailable")
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            for _ in range(6):
                bridge.due()
                assert await bridge.inbound.process_once()
                assert bridge.inbox() == {"Ev-1": "pending"}
                assert inbox_row(bridge)["host_reference"] == ""
                assert "retry" in bridge.inbox_error("Ev-1")
                assert "presence_resources_unavailable" in bridge.inbox_error("Ev-1")
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            with sqlite3.connect(bridge.store.path) as db:
                db.execute("UPDATE inbox SET attempts=1000, available_at=0")
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "pending"} and inbox_row(bridge)["attempts"] == 1001
            host.turn_status["Ev-1"] = 200
            host.answers["Ev-1"] = completed("turn-1", host.body("Ev-1"), text="Now ready", output_ref="out-ready")
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert bridge.inbox() == {"Ev-1": "delivered"} and slack.texts() == ["Now ready"]
            assert len(host.turn_bodies) == 8 and all(body == host.turn_bodies[0] for body in host.turn_bodies)
            assert host.polls == []
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"work_ref": "child-1"},
    {"status": "pending", "work_ref": "child-1"},
    {"status": "rejected", "error": "Invented status is not the Host refusal protocol", "work_ref": "child-1"},
    {"status": "completed", "outcome": "message", "text": "Not a successful response", "work_ref": "child-1"},
    ["not a response object"],
])
def test_malformed_409_is_not_accepted_as_success_or_admitted_work(tmp_path, payload):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda _body: payload
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "pending"}
            assert inbox_row(bridge)["host_reference"] == ""
            assert "Host" in bridge.inbox_error("Ev-1")
            assert host.polls == [] and bridge.outbox() == [] and slack.attempts == 0
        finally:
            await bridge.close()

    asyncio.run(run())
