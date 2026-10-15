"""Retained Host refusals through the real adapter, inbound/outbound workers and store.

Host and Slack HTTP are scripted in process, in the shapes core produces: a typed
HTTP error envelope with ``disposition: retained`` and ``/presence/work`` views whose
``status: unresolved`` carries no authored outcome or text.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3

import pytest

from test_host_refusals import RefusalHost, inbox_row
from test_presence_continuation import (
    Bridge, FakeSlack, _author, _origin, author_pending, child_done, child_pending, completed, continuing, envelope, out,
)


def retained(_body, *, turn="turn-1", child="", code="presence_attempt_outcome_unknown", mode=1):
    return {"ok": False, "error": f"{code}: source_event_id", "code": code, "disposition": "retained",
            "field": "source_event_id", "turn_ref": turn, "delivery_reporting_version": mode,
            **({"work_ref": child} if child else {})}


def unresolved(turn, *, child="", code="presence_attempt_outcome_unknown", mode=1):
    # retained_work_view: turn_ref is the polled original; work_ref, if any, is the actual child.
    return 200, {"ok": True, "status": "unresolved", "disposition": "retained", "code": code, "turn_ref": turn,
                 "recorded_status": "failed", "reason_code": "provider_unavailable",
                 "delivery_reporting_version": mode, **({"work_ref": child} if child else {})}


def terminal(ref, text="", *, status="completed", outcome="message", output_ref="", mode=1):
    return 200, {"ok": True, "status": status, "outcome": outcome, "text": text, "work_ref": ref,
                 "delivery_reporting_version": mode, "output_ref": output_ref}


def receipt(bridge, source="Ev-1"):
    prefix, encoded = inbox_row(bridge, source)["host_reference"].split(":", 1)
    assert prefix == "retained"
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


@pytest.mark.parametrize("reporting", [True, False])
@pytest.mark.parametrize("late_status,late_outcome", [("completed", "message"), ("failed", "deferred")])
def test_retained_event_frees_thread_and_child_then_late_original_each_speak_once(tmp_path, reporting, late_status, late_outcome):
    async def run():
        host, slack = RefusalHost(reporting=reporting), FakeSlack()
        mode = int(reporting)
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda body: retained(body, child="child-1", mode=mode)
        host.scripts["Ev-2"] = lambda body: completed("turn-2", body, text="New facts handled", output_ref="out-next")
        host.work["turn-1"] = [unresolved("turn-1", child="child-1", mode=mode)]
        host.work["child-1"] = [child_pending("child-1", mode), child_done("child-1", "Child result", mode,
                                                                            output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            event = envelope("Ev-1", ts="1.0", text="What is the weather?")
            await bridge.ingest(event)
            assert await bridge.inbound.process_once()
            saved = receipt(bridge)
            assert saved["response"] == host.answers["Ev-1"] and saved["http_status"] == 409
            assert saved["delivery_reporting_version"] == mode and saved["work_refs"] == []
            assert bridge.inbox() == {"Ev-1": "pending"} and bridge.outbox() == []
            assert "retained" in bridge.inbox_error("Ev-1") and "unresolved" in bridge.inbox_error("Ev-1")
            assert bridge.store.status()["inbox_retained"] == 1
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Still there?"))
            assert await bridge.inbound.process_once()  # the retained event no longer holds its thread
            assert bridge.inbox() == {"Ev-1": "pending", "Ev-2": "delivered"}
            await bridge.send_all()
            await bridge.close()

            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            # The child spoke; the original stays unresolved and its receipt is untouched.
            assert slack.texts() == ["New facts handled", "Child result"]
            assert bridge.inbox() == {"Ev-1": "pending", "Ev-2": "delivered"}
            assert receipt(bridge) == saved
            await bridge.close()

            host.work["turn-1"] = [terminal("turn-1", "Late original answer", output_ref="out-late", mode=mode, status=late_status, outcome=late_outcome)]
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.ingest(event)  # Slack redelivery cannot resubmit the original event
            bridge.due()
            assert await bridge.inbound.process_once() is False
            await bridge.send_all()
            assert slack.texts() == ["New facts handled", "Child result", "Late original answer"]
            assert bridge.inbox() == {"Ev-1": "delivered" if late_status == "completed" else "failed", "Ev-2": "delivered"}
            assert receipt(bridge) == saved and bridge.store.status()["inbox_retained"] == 0
            assert host.generations == ["Ev-1", "Ev-2"] and len(host.turn_bodies) == 2
            # A restarted adapter re-reads the finished child once; its output identity is not queued again.
            assert host.polls.count("child-1") == 3 and host.polls.count("turn-1") == 3
            rows = bridge.outbox()
            assert [row["output_ref"] for row in rows] == ["out-next", "out-child", "out-late"]
            assert [row["origin"] for row in rows[1:]] == [_origin("Ev-1", "child-1"), _origin("Ev-1", "turn-1")]
            assert {post["thread_ts"] for post in slack.posts} == {"1.0"} and {row["mode"] for row in rows} == {mode}
            assert [report["message"]["output_ref"] for report in host.reports] == (
                ["out-next", "out-child", "out-late"] if reporting else [])
        finally:
            await bridge.close()

    asyncio.run(run())


def test_child_named_later_is_checkpointed_and_failed_original_keeps_its_receipt(tmp_path):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda body: retained(body, code="presence_turn_cancelled")
        host.work["turn-1"] = [unresolved("turn-1", code="presence_turn_cancelled"),
                               unresolved("turn-1", child="child-late", code="presence_turn_cancelled"),
                               (500, {"ok": False}),
                               terminal("turn-1", status="cancelled", outcome="silent")]
        host.work["child-late"] = [child_pending("child-late"), child_done("child-late", "Admitted work result")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            assert receipt(bridge)["work_refs"] == []
            bridge.due()
            assert await bridge.inbound.process_once()
            # The newly named child was persisted with the receipt before it was polled.
            assert receipt(bridge)["work_refs"] == ["child-late"]
            assert receipt(bridge)["response"] == host.answers["Ev-1"]
            await bridge.close()

            # After restart the child comes from SQLite; an unreadable parent poll keeps custody.
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Admitted work result"]
            assert bridge.inbox() == {"Ev-1": "pending"} and "HTTP 500" in bridge.inbox_error("Ev-1")
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert bridge.inbox() == {"Ev-1": "failed"}
            error = bridge.inbox_error("Ev-1")
            assert "retained" in error and "presence_turn_cancelled" in error and "original turn-1: cancelled" in error
            assert receipt(bridge)["work_refs"] == ["child-late"] and slack.texts() == ["Admitted work result"]
            assert len(host.turn_bodies) == 1 and host.polls.count("child-late") == 2
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"ok": False, "code": "presence_attempt_outcome_unknown", "disposition": "retained"},
    {"ok": False, "code": "presence_attempt_outcome_unknown", "disposition": "retained", "turn_ref": " "},
    {"ok": True, "code": "presence_attempt_outcome_unknown", "disposition": "retained", "turn_ref": "turn-1"},
    {"ok": False, "code": "presence_attempt_outcome_unknown", "disposition": "settled", "turn_ref": "turn-1"},
])
def test_unusable_retained_receipt_keeps_the_event_queued(tmp_path, payload):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda _body: payload
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            await bridge.ingest(envelope("Ev-2", ts="2.0", thread_ts="1.0", text="Next"))
            for _ in range(2):
                bridge.due()
                assert await bridge.inbound.process_once()
                assert bridge.inbox() == {"Ev-1": "pending", "Ev-2": "pending"}
                assert inbox_row(bridge)["host_reference"] == ""
            assert len(host.turn_bodies) == 2 and host.polls == []
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("child_status,child_outcome", [
    ("completed", "message"), ("failed", "message"), ("failed", "deferred"), ("completed", "deferred"),
])
def test_unresolved_continuing_author_keeps_released_output_and_child_and_stays_observed(tmp_path, child_status, child_outcome):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.scripts["Ev-1"] = lambda body: continuing("turn-1", body, outcome="message", text="Early answer",
                                                       output_ref="out-1")
        released = [out("out-1", "Early answer")]
        # The parked author's stack is gone with an unknown outcome: no canonical answer yet.
        lost = {**_author("turn-1", released, 1), "status": "unresolved", "disposition": "retained",
                "code": "presence_attempt_outcome_unknown", "turn_ref": "turn-1", "work_ref": "child-1",
                "child_work_ref": "child-1"}
        host.work["turn-1"] = [author_pending("turn-1", released), (200, lost)]
        host.work["child-1"] = [terminal("child-1", "Child result", status=child_status, outcome=child_outcome, output_ref="out-child")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Hello"))
            assert await bridge.inbound.process_once()
            for _ in range(2):
                bridge.due()
                assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Early answer", "Child result"]
            # The current Host retains a lost author as unresolved; child lifecycle is separate.
            assert bridge.inbox() == {"Ev-1": "pending"} and "unresolved" in bridge.inbox_error("Ev-1")
            host.work["turn-1"] = [(200, {**_author("turn-1", released, 1), "status": "completed",
                                          "outcome": "message", "text": "Late correction", "output_ref": "out-2",
                                          "child_work_ref": "child-1"})]
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Early answer", "Child result", "Late correction"]
            assert bridge.inbox() == {"Ev-1": "failed" if child_status == "failed" else "delivered"}
            assert len(host.turn_bodies) == 1
        finally:
            await bridge.close()

    asyncio.run(run())


def test_retained_poll_errors_including_404_never_drop_custody(tmp_path):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda body: retained(body, child="child-1")
        host.work["turn-1"] = [(404, {"ok": False, "code": "presence_work_not_found", "disposition": "rejected"}),
                               terminal("turn-1", status="failed", outcome="silent")]
        host.work["child-1"] = [(403, {"ok": False}), child_done("child-1", "Child result")]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Research this"))
            assert await bridge.inbound.process_once()
            assert bridge.inbox() == {"Ev-1": "pending"}
            assert "HTTP 404" in bridge.inbox_error("Ev-1") and "HTTP 403" in bridge.inbox_error("Ev-1")
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Child result"] and bridge.inbox() == {"Ev-1": "failed"}
            with sqlite3.connect(bridge.store.path) as db:
                assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        finally:
            await bridge.close()

    asyncio.run(run())


def test_selected_output_survives_retained_author_and_repeated_terminal(tmp_path):
    async def run():
        host, slack = RefusalHost(), FakeSlack()
        host.turn_status["Ev-1"] = 409
        host.scripts["Ev-1"] = lambda body: retained(body)
        selected = {"output_ref": "selection-1", "outcome": "message", "text": "Selected answer"}
        status, body = unresolved("turn-1")
        host.work["turn-1"] = [(status, {**body, "outputs": [selected]})]
        bridge = Bridge(tmp_path, host, slack)
        try:
            await bridge.ingest(envelope("Ev-1", ts="1.0", text="Question"))
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Selected answer"]
            await bridge.close()
            bridge = Bridge(tmp_path, host, slack)
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            host.work["turn-1"] = [terminal("turn-1", "Selected answer", status="failed", outcome="deferred", output_ref="selection-1")]
            bridge.due()
            assert await bridge.inbound.process_once()
            await bridge.send_all()
            assert slack.texts() == ["Selected answer"]
            assert bridge.inbox()["Ev-1"] == "failed"
            assert bridge.outbox()[0]["output_ref"] == "selection-1"
        finally:
            await bridge.close()
    asyncio.run(run())
