"""Resend advice for messages sent in several parts (1.13.0 round 8).

A message with more than one part and a refused or unknown-outcome part gets one per-part advice: wait for its queued
or held parts, check each part with slack_receipt, check unknown parts in Slack itself, send only the parts confirmed
missing. It never gets whole-message wording ("repeat it", "send again if needed"), whatever the age of the problem
part and the state of the others. Each case below is one message in a real store, read through the summary and the
real card script.
"""
from __future__ import annotations

import sqlite3

import pytest

from card_text import card_items
from lib.status_summary import attention_items
from lib.store import BridgeStore
from test_status_contract import _quiet_route_payload

NOW = 1795384800.0
RECENT, OLD = NOW - 600, NOW - 2 * 86400
# Settled outcomes are claimed and written first; held and waiting parts come last, in that order (FIFO claims).
_SETTLED = {"delivered", "failed", "uncertain", "failed_old", "uncertain_old"}
PER_PART = ("check parts with an unknown outcome in Slack itself", "send again only the parts confirmed missing",
            "ask Ouroboros to check slack_receipt for each part")
WHOLE = ("repeat it", "repeat them", "send again if needed", "send only the refused parts", "send only what is missing",
         "Before sending that part again")


def _seed(store: BridgeStore, request_id: str, parts: tuple[str, ...]) -> None:
    order = sorted(parts, key=lambda state: (state not in _SETTLED, state == "waiting"))
    store.enqueue_outbox(request_id=request_id, target="C09SYNTH01", thread_ts="",
                         chunks=tuple(f"part {index}" for index in range(len(order))))
    for state in order:
        if state == "waiting":
            continue
        part = store.claim_outbox()
        if state == "held":
            store.hold_outbox(part, "Held: Ops room is paused")
            continue
        if state == "delivered":
            store.complete_outbox(part.row_id, part.lease_token, provider_message_ts="1700000000.000100")
            continue
        outcome = state.removesuffix("_old")
        store.fail_outbox(part.row_id, part.lease_token, "ReadTimeout" if outcome == "uncertain" else "msg_blocked",
                          state=outcome)
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE outbox SET updated_at=? WHERE id=?", (OLD if state.endswith("_old") else RECENT, part.row_id))


PROBLEMS = ("failed", "uncertain", "failed_old", "uncertain_old")
OTHERS = (("waiting",), ("held",), ("delivered",), ("uncertain",), ("failed",), ("uncertain_old",),
          ("delivered", "held"), ("delivered", "uncertain", "held"), ("delivered", "waiting"), ("failed", "held"))
CASES = [(problem, others) for problem in PROBLEMS for others in OTHERS]


def _card(tmp_path, parts) -> tuple[list[dict], list[dict]]:
    store = BridgeStore(tmp_path)
    try:
        _seed(store, "synthetic-message", parts)
        status = store.status(now=NOW, pid_alive=lambda _pid: True)
    finally:
        store.close()
    payload = {**status, **_quiet_route_payload()}
    payload["attention"] = attention_items(payload)
    return payload["attention"], card_items([payload])[0]


@pytest.mark.parametrize("problem,others", CASES, ids=["-".join((p, *o)) for p, o in CASES])
def test_a_message_in_parts_gets_one_per_part_advice_and_never_a_whole_resend(tmp_path, problem, others) -> None:
    parts = (problem, *others)
    attention, items = _card(tmp_path, parts)
    text = "\n".join(line for item in items for line in item["lines"])
    recent = any(state in ("failed", "uncertain") for state in parts)
    held = "held" in parts
    if not recent and not held:
        # A problem older than a day leaves the attention list; waiting parts go out by themselves.
        assert not [item for item in items if item["kind"] in ("not_delivered", "outcome_unknown", "held")], items
        return
    assert text.lower().count("part by part:") == 1, text
    for words in PER_PART:
        assert words in text, (words, text)
    for words in WHOLE:
        assert words not in text, (words, text)
    queued = any(state in ("waiting", "held") for state in parts)
    assert ("wait until the queued or held parts are sent" in text) == queued, text
    problem_items = [item for item in attention if item["kind"] in ("not_delivered", "outcome_unknown")]
    if recent:
        assert len(problem_items) == 1 and problem_items[0]["in_parts"] == 1, problem_items
    if "uncertain" in parts and "failed" in parts:
        # The unknown-outcome item counts the message; its refused part is said, not hidden.
        assert "Another of its parts was refused." in text, text


@pytest.mark.parametrize("state,simple", [("failed", "Fix the cause, then ask Ouroboros to send again if needed."),
                                          ("uncertain", "before asking Ouroboros to repeat it.")])
def test_a_single_part_message_keeps_the_simple_advice(tmp_path, state, simple) -> None:
    attention, items = _card(tmp_path, (state,))
    text = "\n".join(line for item in items for line in item["lines"])
    assert simple in text and "part by part" not in text.lower(), text
    assert attention[0]["in_parts"] == 0


def test_single_and_multi_part_messages_in_one_item_each_get_their_own_advice(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    try:
        _seed(store, "one-part", ("failed",))
        _seed(store, "two-parts", ("failed", "delivered"))
        _seed(store, "unknown-one", ("uncertain",))
        _seed(store, "unknown-two", ("uncertain", "delivered"))
        status = store.status(now=NOW, pid_alive=lambda _pid: True)
    finally:
        store.close()
    payload = {**status, **_quiet_route_payload()}
    payload["attention"] = attention_items(payload)
    items = {item["kind"]: "\n".join(item["lines"]) for item in card_items([payload])[0]}
    refused, unknown = items["not_delivered"], items["outcome_unknown"]
    assert "For the messages sent in parts, fix the cause, then part by part:" in refused
    assert "For the others, fix the cause, then ask Ouroboros to send again if needed." in refused
    assert "For the messages sent in parts, part by part:" in unknown
    assert "For the others, check in Slack whether it took effect" in unknown


def test_an_older_payload_without_the_parts_count_never_advises_a_whole_resend() -> None:
    # 1.13.0 before round 8: no in_parts; any message may have parts, so only per-part advice is safe.
    items = card_items([{"attention": [
        {"kind": "not_delivered", "tone": "error", "headline": "1 message not delivered", "messages": 1, "actions": 0},
        {"kind": "outcome_unknown", "tone": "warn", "headline": "Outcome unknown for 1 message", "messages": 1,
         "actions": 0, "events": 0, "partly_delivered": 1}]}])[0]
    text = "\n".join(line for item in items for line in item["lines"])
    assert text.lower().count("part by part:") == 2 and "repeat it" not in text and "send again if needed" not in text


def test_last_failed_error_comes_from_the_messages_the_item_counts(tmp_path) -> None:
    """R8-8: a message with a recent refusal and a recent unknown part counts as unknown; the refused-part count
    still includes its part, and last_failed_error is the newest error among counted refused messages (none here)."""
    store = BridgeStore(tmp_path)
    try:
        _seed(store, "both", ("failed", "uncertain"))
        status = store.status(now=NOW, pid_alive=lambda _pid: True)
    finally:
        store.close()
    text = status["recent_problems"]["outbound_text"]
    assert (text["failed"], text["failed_messages"], text["uncertain_messages"]) == (1, 0, 1)
    assert text["last_failed_error"] is None and text["uncertain_messages_with_refused_part"] == 1
