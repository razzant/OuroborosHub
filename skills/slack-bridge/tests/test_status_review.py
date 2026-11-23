"""Status facts added after the Telegram card's review wave, where the Slack half had the same gap.

F3: events the Host refused for now come from the queue rows, not a process-wide note.
F8: every hold reason, the owner's move first. F10: partly delivered messages. F11: an auth refusal is told apart
from any other connection error. F13: counters and recent problems are one read.
"""
from __future__ import annotations

import sqlite3

from lib.events import parse_socket_envelope
from lib.socket_mode import _FATAL_AUTH_ERRORS
from lib.status_summary import AUTH_REFUSALS, SKILL_ACCESS_REFUSED, attention_items, auth_refused, summarize
from lib.store import BridgeStore
from test_status_contract import _envelope


def _quiet(**overrides):
    payload = {
        "link": {"state": "connected", "detail": "ok", "companion_alive": True, "since": None, "error": ""},
        "recent_problems": {"window_seconds": 86400, "inbound": {}, "outbound_text": {}, "mutations": {}},
        "outbox_held": 0, "outbound_text_chunks": {"held": 0}, "outbound_messages": {"held": 0},
        "reaction_context_state": "available", "history_reporting_state": "supported", "last_delivery_error": "",
    }
    payload.update(overrides)
    return payload


def _sql(store: BridgeStore, sql: str, params: tuple = ()) -> None:
    with sqlite3.connect(store.path) as conn:
        conn.execute(sql, params)


# ---------- F13 ----------

def test_status_reads_every_counter_in_one_transaction(tmp_path):
    store = BridgeStore(tmp_path)
    try:
        store.status()  # opens the kept connection
        seen: list[str] = []
        store._db.set_trace_callback(seen.append)
        store.status()
        store._db.set_trace_callback(None)
        statements = [line.strip().split()[0].upper() for line in seen if line.strip()]
        assert statements[0] == "BEGIN" and statements[-1] == "COMMIT", statements
        assert set(statements[1:-1]) == {"SELECT"}, statements
    finally:
        store.close()


# ---------- F3 ----------

def test_events_the_host_refused_for_now_are_counted_from_their_rows(tmp_path):
    store = BridgeStore(tmp_path)
    try:
        for index in (1, 2, 3):
            raw = _envelope(index, channel=f"C09SYNTH0{index}")
            store.ingest_envelope(raw, parse_socket_envelope(raw, bot_user_id="U_BOT"))
        first, second = store.claim_inbox(), store.claim_inbox()
        # A refusal the worker will retry, and a turn that is simply running (its routine note).
        store.retry_inbox(first.row_id, first.lease_token, "HostRetryError: capacity full", delay_seconds=30.0)
        store.retry_inbox(second.row_id, second.lease_token, "Host turn state: pending", delay_seconds=5.0)
        status = store.status()
        assert status["inbox_retrying"]["count"] == 1  # the third row (attempts=0) is an ordinary wait
        assert status["inbox_retrying"]["last_error"]["text"] == "HostRetryError: capacity full"
        assert status["inbox_retrying"]["last_error"]["at"].endswith("Z")
    finally:
        store.close()


def test_waiting_hand_offs_are_said_in_neutral_ink_and_quiet_rows_stay_clear():
    waiting = summarize(_quiet(inbox_retrying={"count": 2, "last_error": {"at": "2026-11-22T22:00:00Z",
                                                                          "text": "HostRetryError: capacity full"}}))
    assert (waiting["text"], waiting["tone"]) == ("Connected · 2 Slack events wait to be passed to Ouroboros", "muted")
    assert waiting["attention"][0]["error"]["text"] == "HostRetryError: capacity full"
    assert summarize(_quiet(inbox_retrying={"count": 0, "last_error": None}))["tone"] == "ok"
    # When no place was confirmed, that item already explains the wait; no second item.
    inactive = _quiet(link={"state": "connected", "detail": "host_adapter_inactive"},
                      inbox_retrying={"count": 2, "last_error": None})
    assert [item["kind"] for item in attention_items(inactive)] == ["link"]


# ---------- F8 ----------

def test_every_hold_reason_is_read_and_the_owners_move_comes_first(tmp_path):
    store = BridgeStore(tmp_path)
    try:
        for index in range(4):
            store.enqueue_outbox(request_id=f"held-{index}", target=f"C09HOLD{index}", thread_ts="", chunks=("hold",))
        for index in range(4):
            item = store.claim_outbox()
            note = SKILL_ACCESS_REFUSED if index == 0 else "Held: Ops room is paused"
            store.hold_outbox(item, note)
        reasons = store.status()["held_reasons"]
        assert reasons == [
            {"reason": "Ops room is paused", "note": "Held: Ops room is paused", "count": 3},
            {"reason": "Check this skill's access in Skills.", "note": SKILL_ACCESS_REFUSED, "count": 1},
        ]
    finally:
        store.close()
    payload = _quiet(outbox_held=4, outbound_text_chunks={"held": 4}, outbound_messages={"held": 4},
                     held_reasons=reasons, last_delivery_error="Held: Ops room is paused")
    held = attention_items(payload)[0]
    # The newest note is a pause, but one row waits for the owner: amber, and that reason first.
    assert held["tone"] == "warn"
    assert [reason["reason"] for reason in held["reasons"]] == ["Check this skill's access in Skills.", "Ops room is paused"]


# ---------- F10 ----------

def test_partly_delivered_messages_are_counted(tmp_path):
    store = BridgeStore(tmp_path)
    try:
        store.enqueue_outbox(request_id="long", target="C09LONG", thread_ts="", chunks=("part one", "part two"))
        first = store.claim_outbox()
        store.complete_outbox(first.row_id, first.lease_token, provider_message_ts="1.1")
        second = store.claim_outbox()
        store.fail_outbox(second.row_id, second.lease_token, "msg_too_long", state="failed")
        store.enqueue_outbox(request_id="short", target="C09SHORT", thread_ts="", chunks=("only part",))
        only = store.claim_outbox()
        store.fail_outbox(only.row_id, only.lease_token, "channel_not_found", state="failed")
        text = store.status()["recent_problems"]["outbound_text"]
        assert text["failed_messages"] == 2
        assert text["failed_messages_partly_delivered"] == 1
        assert text["uncertain_messages_partly_delivered"] == 0
    finally:
        store.close()
    payload = _quiet()
    payload["recent_problems"]["outbound_text"] = text
    item = attention_items(payload)[0]
    assert item["kind"] == "not_delivered" and item["partly_delivered"] == 1


# ---------- F11 ----------

def test_auth_codes_match_the_socket_modes_own_list():
    assert AUTH_REFUSALS == _FATAL_AUTH_ERRORS


def test_only_an_auth_refusal_reads_as_a_token_problem():
    assert auth_refused("invalid_auth") and auth_refused("Slack API error: token_revoked")
    assert not auth_refused("ConnectionResetError: [Errno 54] Connection reset by peer")
    assert not auth_refused("")
    refused = attention_items(_quiet(link={"state": "error", "detail": "socket_error", "error": "invalid_auth"}))[0]
    assert refused["auth"] is True and refused["headline"] == "Slack refused the bridge's tokens"
    other = attention_items(_quiet(link={"state": "error", "detail": "companion_exited_error",
                                         "error": "OSError: disk full"}))[0]
    assert other["auth"] is False and other["headline"] == "The bridge stopped after an error"
    assert summarize(_quiet(link={"state": "error", "detail": "socket_error", "error": "timed out"}))["text"] == (
        "Connection error · the Slack connection failed")
