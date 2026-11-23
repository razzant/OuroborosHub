"""Additive status facts for the Widgets card, and the one rule the Places line and the card's first line share."""
import asyncio
import importlib.util
import json
import pathlib
import sqlite3
import sys
import time

import pytest

from types import SimpleNamespace

from telegram_bot import runtime as runtime_mod
from telegram_bot import api as api_mod
from telegram_bot.api import TelegramApiError
from telegram_bot.custody import CustodyStore, RECENT_PROBLEM_WINDOW_S
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PLACE_POLICY_UNREAD, SKILL_ACCESS_REFUSED, PresenceHostError, PresenceHostHTTPError
from telegram_bot.status_summary import TONE_RANK, attention_items, summarize
from test_plugin import FakeApi


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _event(update_id):
    return parse_telegram_update(
        {"update_id": update_id, "message": {"message_id": update_id, "text": "hello",
                                             "from": {"id": 3, "is_bot": False}, "chat": {"id": 4, "type": "private"}}},
        bot_account_id="9",
    )


def _quiet(**overrides):
    payload = {
        "runtime_state": "polling", "poll_result": "ok", "host_checked": True, "places_version": 1, "binding_state": "missing",
        "reaction_context": "available", "outbox_held": 0, "held_reasons": [], "inbox_waiting": 0,
        "recent_problems": {"window_seconds": 86400,
                            "outbound": {"failed": 0, "uncertain": 0, "last_failed_at": "", "last_uncertain_at": "",
                                         "last_error": None},
                            "inbound": {"failed": 0, "last_failed_at": "", "last_error": None}},
    }
    payload.update(overrides)
    return payload


# ---------- the rule ----------

def test_healthy_polling_is_green_and_says_so():
    assert summarize(_quiet()) == {"text": "Connected · nothing needs attention", "tone": "ok", "attention": []}


@pytest.mark.parametrize(("facts", "text", "tone"), [
    ({"runtime_state": "not_started"}, "Starting…", "muted"),
    ({"runtime_state": "waiting_for_token", "poll_result": ""}, "Not set up · the bot token is missing", "warn"),
    ({"runtime_state": "provider_error", "poll_result": "failed", "poll_failure": "network"},
     "Connection error · new messages are not being received", "error"),
    ({"runtime_state": "provider_error", "poll_result": "failed", "poll_failure": "local"},
     "Error on this computer · new messages are not being processed", "error"),
    ({"runtime_state": "stopped"}, "Stopped · the bot is not running", "error"),
])
def test_every_runtime_state_has_words_and_a_tone(facts, text, tone):
    summary = summarize(_quiet(**facts))
    assert (summary["text"], summary["tone"]) == (text, tone)


def test_delivery_failures_are_no_longer_a_muted_polling_line():
    # The root's fixture 05: two sends refused (403) while polling; the old route said "polling", muted.
    payload = _quiet()
    payload["recent_problems"]["outbound"].update(failed=2, last_failed_at="2026-11-22T17:58:40Z", last_error={
        "at": "2026-11-22T17:58:40Z", "state": "failed", "text": "Forbidden: bot was blocked by the user"})
    summary = summarize(payload)
    # The outbox also holds reactions, moderation and operations: "outgoing items", not "messages".
    assert (summary["text"], summary["tone"]) == ("Connected · 2 outgoing items not delivered", "error")
    assert summary["attention"][0]["error"]["text"] == "Forbidden: bot was blocked by the user"


def test_order_and_worst_tone():
    payload = _quiet(runtime_state="provider_error", poll_result="failed", poll_error="TelegramApiError: x",
                     poll_failure="network", provider_ok_at="2026-11-22T10:00:00Z",
                     outbox_held=2, held_reasons=[{"reason": "Example is paused", "note": "Held: Example is paused", "count": 2}],
                     places_version=0, reaction_context="update_required")
    payload["recent_problems"]["outbound"].update(failed=1, uncertain=1)
    payload["recent_problems"]["inbound"].update(failed=3)
    kinds = [item["kind"] for item in attention_items(payload)]
    # Worst tone first (error), then warn, then muted; the order of consequence within each tone.
    assert kinds == ["link", "not_delivered", "not_handed", "outcome_unknown", "binding", "reactions", "held"]
    link = attention_items(payload)[0]
    assert link["at"] == "2026-11-22T10:00:00Z" and link["error"]["text"] == "TelegramApiError: x"
    assert summarize(payload)["tone"] == "error"
    assert summarize(payload)["text"].endswith("· 6 more need attention")


def test_outcome_unknown_is_amber_and_never_called_failed():
    payload = _quiet()
    payload["recent_problems"]["outbound"].update(uncertain=1)
    summary = summarize(payload)
    assert summary["tone"] == "warn" and "fail" not in summary["text"].lower()


def test_held_is_amber_only_for_the_skills_own_refusal():
    paused = _quiet(outbox_held=1, held_reasons=[{"reason": "Example is paused", "note": "Held: Example is paused", "count": 1}])
    assert summarize(paused)["tone"] == "muted"
    retries = _quiet(outbox_held=1, held_reasons=[{"reason": "It retries by itself.", "note": PLACE_POLICY_UNREAD, "count": 1}])
    assert summarize(retries)["tone"] == "muted"
    refused = _quiet(outbox_held=1, held_reasons=[{"reason": "x", "note": SKILL_ACCESS_REFUSED, "count": 1}])
    assert summarize(refused)["tone"] == "warn"


def test_capabilities_are_not_blamed_before_ouroboros_answered():
    unknown = _quiet(host_checked=False, places_version=0, reaction_context="update_required", inbox_waiting=2)
    # Nothing is blamed, and nothing reads as all clear either: the state is partly known (muted).
    assert attention_items(unknown) == [{"kind": "partial", "tone": "muted", "headline": "State partly known",
                                         "gaps": ["capabilities_unchecked"]}]
    unreachable = summarize(_quiet(host_checked=False, delivery_reporting="unavailable"))
    assert (unreachable["text"], unreachable["tone"]) == ("Connected · state partly known", "muted")
    assert unreachable["attention"][0]["gaps"] == ["host_unanswered"]
    legacy = _quiet()
    del legacy["recent_problems"]
    assert summarize(legacy)["attention"][0]["gaps"] == ["no_recent_window"]
    old_host = _quiet(places_version=0, inbox_waiting=2)
    assert attention_items(old_host)[0] | {} == {"kind": "binding", "tone": "warn", "count": 2,
                                                 "headline": "Incoming messages wait for a place binding"}
    assert attention_items(_quiet(places_version=0, binding_state="configured")) == []


def test_an_unavailable_connect_list_while_polling_stays_visible():
    payload = _quiet(connect=None, connect_unavailable="Observed chats could not be read")
    summary = summarize(payload)
    assert (summary["text"], summary["tone"]) == ("Connected · observed chats could not be read", "warn")
    # Outside polling the runtime's own item already says why; no second item.
    stopped = summarize(_quiet(runtime_state="stopped", connect=None, connect_unavailable="Telegram bot not running"))
    assert [item["kind"] for item in stopped["attention"]] == ["link"]


def test_the_first_line_names_the_item_that_sets_its_tone():
    # Root's fixture 10: an unknown outcome (warn) beside refusals (error); the red line names the refusals.
    payload = _quiet()
    payload["recent_problems"]["outbound"].update(failed=4127, uncertain=318)
    summary = summarize(payload)
    assert summary["tone"] == "error"
    assert summary["text"] == "Connected · 4,127 outgoing items not delivered · 1 more needs attention"
    assert summary["attention"][1]["kind"] == "outcome_unknown" and summary["attention"][1]["tone"] == "warn"


def test_each_outbound_item_carries_its_own_newest_error():
    payload = _quiet()
    payload["recent_problems"]["outbound"].update(
        failed=1, uncertain=1,
        last_error={"at": "2026-11-22T18:06:02Z", "state": "uncertain", "text": "TimeoutError"},
        last_failed_error={"at": "2026-11-22T17:58:40Z", "state": "failed", "text": "Forbidden"},
        last_uncertain_error={"at": "2026-11-22T18:06:02Z", "state": "uncertain", "text": "TimeoutError"})
    errors = {item["kind"]: item["error"]["text"] for item in attention_items(payload)}
    assert errors == {"not_delivered": "Forbidden", "outcome_unknown": "TimeoutError"}


def _retrying(count, error="PresenceHostHTTPError: 409", **stages):
    return {"count": count, "since": "2026-11-22T18:00:00Z", "next_attempt_at": "2026-11-22T18:20:00Z",
            "last_error": error if count else "", "submit": stages.get("submit", count),
            "download": stages.get("download", 0), "last_stage": "submit" if count else ""}


def test_updates_waiting_for_ouroboros_are_said_but_never_amber():
    payload = _quiet(inbox_waiting=6, handoff_retrying=_retrying(6))
    summary = summarize(payload)
    assert (summary["text"], summary["tone"]) == ("Connected · 6 Telegram updates wait to be passed to Ouroboros", "muted")
    item = summary["attention"][0]
    assert item["error"]["text"] == "PresenceHostHTTPError: 409" and item["at"] == "2026-11-22T18:00:00Z"
    # One update: the singular verb.
    one = summarize(_quiet(inbox_waiting=1, handoff_retrying=_retrying(1)))
    assert one["text"] == "Connected · 1 Telegram update waits to be passed to Ouroboros"
    # On a pre-Places Host the binding item explains the wait; no second item.
    old = attention_items(_quiet(places_version=0, inbox_waiting=6, handoff_retrying=_retrying(6)))
    assert [item["kind"] for item in old] == ["binding"]


def test_handoff_waiting_follows_custody_rows_never_the_runtimes_newest_error():
    # Inbox rows back off while an outbound blip overwrote the process-wide error source: still listed.
    blip = _quiet(inbox_waiting=2, handoff_retrying=_retrying(2), last_error_source="outbound", last_error="TimeoutError")
    assert [item["kind"] for item in attention_items(blip)] == ["handoff_waiting"]
    # After recovery the rows are gone; a sticky inbound error of the runtime no longer makes a problem.
    recovered = _quiet(inbox_waiting=0, handoff_retrying=_retrying(0), last_error_source="inbound", last_error="old")
    assert summarize(recovered) == {"text": "Connected · nothing needs attention", "tone": "ok", "attention": []}
    # A busy Host's deferral (nothing tried and failed) is normal flow: green.
    deferred = _quiet(inbox_waiting=3, handoff_retrying=_retrying(0))
    assert summarize(deferred)["tone"] == "ok"


def test_a_place_to_connect_is_the_owners_move_and_leads_four_pauses():
    # Four paused places outnumber the one place the Host says to connect; that one still leads, amber.
    reasons = [{"reason": f"Place {index} is paused", "note": f"Held: Place {index} is paused", "count": 9 - index}
               for index in range(4)] + [{"reason": "Connect this place in Consciousness > Places.",
                                          "note": "Held: Connect this place in Consciousness > Places.", "count": 1,
                                          "kind": "place_not_connected"}]
    held = attention_items(_quiet(outbox_held=31, held_reasons=reasons))[0]
    assert held["tone"] == "warn"
    assert held["reasons"][0] == {"reason": "Connect this place in Consciousness > Places.", "count": 1,
                                  "action": True, "kind": "place_not_connected"}
    # A refused skill held before 0.13.1 kept no kind: its own fixed note still marks it.
    legacy = attention_items(_quiet(outbox_held=1, held_reasons=[
        {"reason": "x", "note": SKILL_ACCESS_REFUSED, "count": 1}]))[0]
    assert legacy["reasons"][0]["kind"] == "skill_access" and legacy["tone"] == "warn"


def test_every_hold_reason_counts_toward_the_owners_move():
    reasons = [{"reason": f"Place {index} is paused", "note": f"Held: Place {index} is paused", "count": 9 - index}
               for index in range(6)] + [{"reason": "x", "note": SKILL_ACCESS_REFUSED, "count": 1}]
    held = attention_items(_quiet(outbox_held=40, held_reasons=reasons))[0]
    assert held["tone"] == "warn" and len(held["reasons"]) == 7
    # The reason that makes it amber leads, though it is the rarest: the card shows the first four.
    assert held["reasons"][0] == {"reason": "x", "count": 1, "action": True, "kind": "skill_access"}
    assert [reason["reason"] for reason in held["reasons"][1:3]] == ["Place 0 is paused", "Place 1 is paused"]
    assert not any(reason.get("action") for reason in held["reasons"][1:])


# ---------- the link: green only after an answer for the token set now ----------

def test_a_request_still_out_never_reads_connected():
    connecting = summarize(_quiet(poll_result=""))
    assert (connecting["text"], connecting["tone"]) == ("Connecting…", "muted")
    # The last completed attempt failed and the next one is out: still the error, never green.
    retry = summarize(_quiet(poll_result="failed", poll_error="TelegramApiError: Telegram getUpdates failed "
                             "(transport): TimeoutError", poll_failure="network", poll_at="2026-11-22T18:00:00Z"))
    assert (retry["text"], retry["tone"]) == ("Connection error · new messages are not being received", "error")
    assert retry["attention"][0]["failure"] == "network"


def test_confirmed_bot_reasons_set_the_tone():
    from telegram_bot.runtime import BOT_TOKEN_MISSING, BOT_UNCONFIRMED
    # A new token getMe has not confirmed yet, while the old request is out: waiting, neutral.
    waiting = summarize(_quiet(poll_result="", connect=None, connect_unavailable=BOT_UNCONFIRMED))
    assert (waiting["text"], waiting["tone"]) == ("Connecting…", "muted")
    # The token was removed while a request was out: the owner's move, as before any token.
    missing = summarize(_quiet(connect=None, connect_unavailable=BOT_TOKEN_MISSING))
    assert (missing["text"], missing["tone"]) == ("Not set up · the bot token is missing", "warn")
    assert [item["kind"] for item in missing["attention"]] == ["link"]


def test_the_link_item_carries_this_attempts_error_not_another_loops():
    payload = _quiet(runtime_state="provider_error", poll_result="failed", poll_error="sqlite3.OperationalError: locked",
                     poll_failure="local", last_error="Forbidden from a send", last_error_source="outbound")
    link = attention_items(payload)[0]
    assert link["failure"] == "local" and link["error"]["text"] == "sqlite3.OperationalError: locked"
    assert link["detail"] == "local_error" and link["headline"] == "New messages are not being processed"
    # No completed attempt for the token set now: no link error at all, whatever another loop or token recorded.
    other = summarize(_quiet(runtime_state="provider_error", poll_result="", last_error="Forbidden",
                             last_error_source="polling"))
    assert (other["text"], other["tone"]) == ("Connecting…", "muted")
    assert not any(item["kind"] == "link" for item in other["attention"])


def test_the_link_is_judged_for_the_token_set_now():
    from telegram_bot.runtime import BOT_TOKEN_MISSING, BOT_UNCONFIRMED
    # Token A was refused (401) and the loop waits out its backoff; the owner already set token B.
    backoff = summarize(_quiet(runtime_state="provider_error", poll_result="", connect=None,
                               connect_unavailable=BOT_UNCONFIRMED, last_error_source="polling",
                               last_error="TelegramApiError: Telegram getMe failed (401): Unauthorized"))
    assert (backoff["text"], backoff["tone"]) == ("Connecting…", "muted")
    assert "401" not in json.dumps(backoff)
    # A token added during the pause while none was set: waiting for its first answer, not "missing".
    added = summarize(_quiet(runtime_state="waiting_for_token", poll_result="", connect=None,
                             connect_unavailable=BOT_UNCONFIRMED))
    assert (added["text"], added["tone"]) == ("Connecting…", "muted")
    # Still no token: the owner's move.
    missing = summarize(_quiet(runtime_state="waiting_for_token", poll_result="", connect=None,
                               connect_unavailable=BOT_TOKEN_MISSING))
    assert (missing["text"], missing["tone"]) == ("Not set up · the bot token is missing", "warn")
    # A completed failure for the token set now keeps its own error and side.
    refused = summarize(_quiet(runtime_state="provider_error", poll_result="failed", poll_failure="token",
                               poll_error="TelegramApiError: Telegram getMe failed (401): Unauthorized",
                               connect=None, connect_unavailable=BOT_UNCONFIRMED))
    assert refused["tone"] == "error" and refused["attention"][0]["failure"] == "token"


class _Client:
    """getUpdates: first a request without an answer, then one held until ``gate`` opens."""
    download_limit = 20 * 1024 * 1024

    def __init__(self, gate, updates=()):
        self.gate, self.calls, self.updates = gate, 0, list(updates)

    async def delete_webhook(self):
        return True

    async def get_me(self):
        return {"id": 9, "username": "card_bot"}

    async def get_updates(self, offset, timeout_sec):
        self.calls += 1
        if self.calls == 1:
            raise TelegramApiError("getUpdates", "TimeoutError")
        await self.gate.wait()
        await asyncio.sleep(0.01)  # a real long poll yields; an open gate alone would spin the loop
        return self.updates


def _link_facts(runtime):
    """The runtime's link facts over a quiet card (this fake Host is never asked about capabilities)."""
    snapshot = runtime.status_snapshot({})
    return {key: snapshot[key] for key in ("runtime_state", "poll_result", "poll_at", "poll_error", "poll_failure",
                                           "poll_stage", "provider_ok_at")}


async def _until(predicate):
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def test_after_a_timeout_the_next_attempt_is_not_a_false_all_clear(tmp_path):
    async def run():
        gate = asyncio.Event()
        client = _Client(gate)
        runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token-a",
                                                       logger=None, submitter=SimpleNamespace(),
                                                       client_factory=lambda token: client)

        async def brief(_seconds):
            await asyncio.sleep(0.01)
        runtime._pause = brief
        loop = asyncio.create_task(runtime._poll_loop())
        try:
            await _until(lambda: client.calls == 2)
            snapshot = runtime.status_snapshot({})
            # runtime_state says "polling" (set before the request), but nothing answered it yet.
            assert snapshot["runtime_state"] == "polling" and snapshot["poll_result"] == "failed"
            assert snapshot["poll_failure"] == "network"
            assert summarize(_quiet(**_link_facts(runtime)))["tone"] == "error"
            gate.set()
            await _until(lambda: runtime.status_snapshot({})["poll_result"] == "ok")
            assert summarize(_quiet(**_link_facts(runtime)))["text"] == "Connected · nothing needs attention"
        finally:
            runtime._stopping = True
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            runtime.store.close()

    asyncio.run(run())


def test_a_local_failure_after_telegrams_answer_is_named_local(tmp_path):
    async def run():
        gate = asyncio.Event()
        gate.set()
        client = _Client(gate, [{"update_id": 1, "message": {"message_id": 1, "text": "hi", "date": 1,
                                                             "from": {"id": 3, "is_bot": False},
                                                             "chat": {"id": 4, "type": "private"}}}])
        client.calls = 1  # skip the first, failing request
        runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token-a",
                                                       logger=None, submitter=SimpleNamespace(),
                                                       client_factory=lambda token: client)

        def broken(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")
        runtime.store.commit_update = broken

        async def brief(_seconds):
            await asyncio.sleep(0.01)
        runtime._pause = brief
        loop = asyncio.create_task(runtime._poll_loop())
        try:
            await _until(lambda: runtime.status_snapshot({})["poll_result"] == "failed")
            snapshot = runtime.status_snapshot({})
            assert snapshot["poll_failure"] == "local" and "database is locked" in snapshot["poll_error"]
            assert snapshot["provider_ok_at"].endswith("Z")  # Telegram did answer
            # The first line (and the Places line) names a failure on this computer, not the connection.
            summary = summarize(_quiet(**_link_facts(runtime)))
            assert (summary["text"], summary["tone"]) == (
                "Error on this computer · new messages are not being processed", "error")
        finally:
            runtime._stopping = True
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            runtime.store.close()

    asyncio.run(run())


class _TokenBot:
    """getMe refuses ``refused``; any other token's getUpdates waits on ``gate``."""
    download_limit = 20 * 1024 * 1024

    def __init__(self, token, refused, gate):
        self.token, self.refused, self.gate = token, refused, gate

    async def delete_webhook(self):
        return True

    async def get_me(self):
        if self.token == self.refused:
            raise TelegramApiError("getMe", "Unauthorized", 401)
        return {"id": 77, "username": "second_bot"}

    async def get_updates(self, offset, timeout_sec):
        await self.gate.wait()
        await asyncio.sleep(0.01)
        return []


def _route_payload(runtime):
    """The facts plugin._status_payload takes from one runtime read (connect left to the summary's reason)."""
    snapshot = runtime.status_snapshot({})
    return _quiet(**{key: snapshot[key] for key in ("runtime_state", "poll_result", "poll_at", "poll_error",
                                                    "poll_failure", "poll_stage", "last_error", "last_error_source")},
                  connect=None, connect_unavailable=snapshot["confirmed_reason"])


def test_a_refusal_is_recorded_for_the_token_the_attempt_actually_used(tmp_path):
    """The loop read token A, then waited for the client lock while the owner set B; getMe refused B. That refusal
    belongs to B: the card says so at once, not "Connecting…" until the next attempt."""
    async def run():
        token, gate = {"value": "token-a"}, asyncio.Event()
        runtime = runtime_mod.TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: token["value"], logger=None, submitter=SimpleNamespace(),
            client_factory=lambda value: _TokenBot(value, "token-b", gate))
        held = asyncio.Event()

        async def hold(_seconds):
            await held.wait()
        runtime._pause = hold
        runtime._client_lock = asyncio.Lock()
        await runtime._client_lock.acquire()  # another worker holds the client while the loop starts its attempt
        loop = asyncio.create_task(runtime._poll_loop())
        runtime._run_task = loop
        try:
            await asyncio.sleep(0.05)  # the loop has read token A and waits for the lock
            token["value"] = "token-b"
            runtime._client_lock.release()
            await _until(lambda: runtime.status_snapshot({})["runtime_state"] == "provider_error")
            snapshot = runtime.status_snapshot({})
            assert (snapshot["poll_result"], snapshot["poll_failure"]) == ("failed", "token")
            summary = summarize(_route_payload(runtime))
            assert summary["tone"] == "error" and summary["text"].startswith("Connection error")
            assert summary["attention"][0]["failure"] == "token"
        finally:
            runtime._stopping = True
            held.set()
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            runtime.store.close()

    asyncio.run(run())


@pytest.mark.parametrize("first", ["token-a", ""])
def test_a_new_token_during_a_backoff_or_the_no_token_pause_reads_connecting(tmp_path, first):
    async def run():
        token, gate, held = {"value": first}, asyncio.Event(), asyncio.Event()
        runtime = runtime_mod.TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: token["value"], logger=None, submitter=SimpleNamespace(),
            client_factory=lambda value: _TokenBot(value, "token-a", gate))

        async def hold(_seconds):
            await held.wait()  # the backoff after A's refusal, or the 5 s wait for a token, lasts
        runtime._pause = hold
        loop = asyncio.create_task(runtime._poll_loop())
        runtime._run_task = loop
        try:
            expected = "provider_error" if first else "waiting_for_token"
            await _until(lambda: runtime.status_snapshot({})["runtime_state"] == expected)
            before = summarize(_route_payload(runtime))["text"]
            assert before.startswith("Connection error" if first else "Not set up")
            token["value"] = "token-b"  # the owner sets a new token while the loop still waits
            after = summarize(_route_payload(runtime))
            assert (after["text"], after["tone"]) == ("Connecting…", "muted")
            assert "401" not in json.dumps(after) and "Unauthorized" not in json.dumps(after)
            held.set()
            gate.set()
            await _until(lambda: runtime.status_snapshot({})["poll_result"] == "ok")
            assert summarize(_route_payload(runtime))["text"].startswith("Connected")
        finally:
            runtime._stopping = True
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            runtime.store.close()

    asyncio.run(run())


@pytest.mark.parametrize(("exc", "side"), [
    (TelegramApiError("getUpdates", "Unauthorized", 401), "token"),
    (TelegramApiError("getUpdates", "Conflict", 409), "telegram"),
    (TelegramApiError("getUpdates", "TimeoutError"), "network"),
    # A token that cannot be written in a request line: no request left, so never "network" (review wave 5, item 5).
    # Built when the test runs (the class is new in 0.13.1), so this module still imports where it does not exist.
    (lambda: api_mod.TelegramTokenMalformed("request", "InvalidURL"), "token_format"),
    (ValueError("x"), ""),
])
def test_poll_failure_sides(exc, side):
    exc = exc() if callable(exc) else exc
    for stage in ("identity", "updates"):
        assert runtime_mod._poll_failure(exc, stage) == side
    assert runtime_mod._poll_failure(exc, "record") == "local"  # Telegram answered: whatever failed is here
    # The custody store failing is this computer's, at any stage.
    for stage in runtime_mod.POLL_STAGES:
        assert runtime_mod._poll_failure(sqlite3.OperationalError("database is locked"), stage) == "local"


def test_telegrams_refusal_stays_the_outcome_when_forgetting_the_bot_meets_a_lock(tmp_path):
    """getMe refused the token (401) and forgetting the bot confirmed for it met a SQLite lock: the poll outcome is
    still Telegram's refusal (token), never "local", and the failed cleanup is recorded apart (the log)."""
    class Lines:
        def __init__(self):
            self.lines = []

        def log(self, level, message):
            self.lines.append((level, message))

    async def run():
        logger = Lines()
        runtime = runtime_mod.TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: "token-a", logger=logger, submitter=SimpleNamespace(),
            client_factory=lambda value: _TokenBot(value, "token-a", asyncio.Event()))

        def locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")
        runtime.store.forget_confirmed_bot = locked
        held = asyncio.Event()

        async def hold(_seconds):
            await held.wait()
        runtime._pause = hold
        loop = asyncio.create_task(runtime._poll_loop())
        runtime._run_task = loop
        try:
            await _until(lambda: runtime.status_snapshot({})["poll_result"] == "failed")
            snapshot = runtime.status_snapshot({})
            assert (snapshot["poll_failure"], snapshot["poll_stage"]) == ("token", "identity")
            assert "(401)" in snapshot["poll_error"] and "locked" not in snapshot["poll_error"]
            summary = summarize(_route_payload(runtime))
            assert summary["text"].startswith("Connection error") and summary["attention"][0]["failure"] == "token"
            assert any("database is locked" in message for _level, message in logger.lines)
        finally:
            runtime._stopping = True
            held.set()
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            runtime.store.close()

    asyncio.run(run())


def test_a_store_failure_after_getme_confirmed_the_bot_is_named_local(tmp_path):
    """getMe answered, then recording the confirmed bot met a SQLite lock: an error on this computer, at the identity
    stage, never "Connection error" (nothing was wrong with the token or the network)."""
    async def run():
        gate = asyncio.Event()
        runtime = runtime_mod.TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: "token-a", logger=None, submitter=SimpleNamespace(),
            client_factory=lambda value: _TokenBot(value, "nobody", gate))

        def locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")
        runtime.store.record_confirmed_bot = locked
        held = asyncio.Event()

        async def hold(_seconds):
            await held.wait()
        runtime._pause = hold
        loop = asyncio.create_task(runtime._poll_loop())
        runtime._run_task = loop
        try:
            await _until(lambda: runtime.status_snapshot({})["poll_result"] == "failed")
            snapshot = runtime.status_snapshot({})
            assert (snapshot["poll_failure"], snapshot["poll_stage"]) == ("local", "identity")
            summary = summarize(_route_payload(runtime))
            assert (summary["text"], summary["tone"]) == (
                "Error on this computer · new messages are not being processed", "error")
            assert summary["attention"][0]["stage"] == "identity"
        finally:
            runtime._stopping = True
            held.set()
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            runtime.store.close()

    asyncio.run(run())


def test_a_poll_result_counts_only_for_the_token_set_now(tmp_path):
    token = {"value": "token-a"}
    runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: token["value"],
                                                   logger=None, submitter=object())
    runtime._poll_done("token-a", "ok")
    assert runtime.status_snapshot({})["poll_result"] == "ok"
    token["value"] = "token-b"
    assert runtime.status_snapshot({})["poll_result"] == ""
    runtime.store.close()


def test_tones_are_the_place_sheet_vocabulary():
    assert set(TONE_RANK) == {"muted", "ok", "warn", "error"}


# ---------- custody: recent problems and hold reasons ----------

def test_card_snapshot_counts_recent_terminal_rows_with_times_and_hold_reasons(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    for update_id in (21, 22, 23):
        store.commit_update(update_id, _event(update_id))
    inbox = store.claim_inbox()
    store.mark_inbox_failed(inbox.event_id, reason="Host refused")
    withheld = store.claim_inbox()
    store.mark_inbox_failed(withheld.event_id, reason="reaction_host_context_unavailable")
    # The outbox is FIFO per chat; one chat per row keeps every row claimable here.
    for index, name in enumerate(("refused", "lost", "old", "held-a", "held-b", "held-c")):
        store.enqueue_outbox(name, {"kind": "message", "chat_id": str(100 + index)})
    store.mark_outbox_failed(store.claim_outbox().delivery_id, reason="Forbidden: bot was blocked by the user")
    store.mark_outbox_failed(store.claim_outbox().delivery_id, reason="TimeoutError", state="uncertain")
    old = store.claim_outbox().delivery_id
    store.mark_outbox_failed(old, reason="an old refusal")
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
        conn.execute("UPDATE outbox SET failed_at=? WHERE delivery_id=?", (time.time() - RECENT_PROBLEM_WINDOW_S - 60, old))
    store.hold_outbox(store.claim_outbox().delivery_id, "Held: Example is paused")
    store.hold_outbox(store.claim_outbox().delivery_id, "Held: Example is paused")
    store.hold_outbox(store.claim_outbox().delivery_id, SKILL_ACCESS_REFUSED)

    card = store.card_snapshot()
    recent = card["recent_problems"]
    assert recent["window_seconds"] == RECENT_PROBLEM_WINDOW_S
    assert recent["outbound"]["failed"] == 1 and recent["outbound"]["uncertain"] == 1
    assert recent["outbound"]["last_failed_at"].endswith("Z") and recent["outbound"]["last_uncertain_at"].endswith("Z")
    assert recent["outbound"]["last_error"]["state"] in {"failed", "uncertain"}
    assert recent["inbound"]["failed"] == 1  # the withheld reaction is not an inbound failure
    assert recent["inbound"]["last_error"]["text"] == "Host refused"
    assert card["held_reasons"] == [
        {"reason": "Example is paused", "note": "Held: Example is paused", "count": 2},
        {"reason": "Check this skill's access in Skills.", "note": SKILL_ACCESS_REFUSED, "count": 1},
    ]
    # Lifetime totals keep their meaning beside the recent window.
    snapshot = store.status_snapshot()
    assert snapshot["outbox_failed"] == 2 and snapshot["outbox_uncertain"] == 1 and snapshot["outbox_held"] == 3


def test_card_snapshot_returns_every_hold_reason(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    for index in range(7):
        store.enqueue_outbox(f"held-{index}", {"kind": "message", "chat_id": str(200 + index)})
        note = SKILL_ACCESS_REFUSED if index == 6 else f"Held: Place {index} is paused"
        store.hold_outbox(store.claim_outbox().delivery_id, note)
    reasons = store.card_snapshot()["held_reasons"]
    assert len(reasons) == 7 and sum(reason["count"] for reason in reasons) == store.status_snapshot()["outbox_held"]
    assert any(reason["note"] == SKILL_ACCESS_REFUSED for reason in reasons)


def test_card_snapshot_of_an_empty_store(tmp_path):
    card = CustodyStore(tmp_path / "custody.sqlite3").card_snapshot()
    assert card["held_reasons"] == []
    assert card["recent_problems"]["outbound"] == {"failed": 0, "uncertain": 0, "failed_partly_confirmed": 0,
                                                   "uncertain_partly_confirmed": 0, "last_failed_at": "",
                                                   "last_uncertain_at": "", "last_error": None,
                                                   "last_failed_error": None, "last_uncertain_error": None}
    assert card["recent_problems"]["inbound"] == {"failed": 0, "last_failed_at": "", "last_error": None}
    assert card["handoff_retrying"] == _NO_WAIT


# Answers as any version may have stored them under _sent_messages / _sent_media: one object or a list, with text
# captions, missing, zero, negative, textual, real or boolean ids, nested lists, bare scalars and nulls.
_LEGACY_ANSWERS = [
    {"message_id": 0, "caption": "a photo kept by 0.12"}, {"message_id": 5, "caption": "x"}, {"caption": "x"}, {},
    "sent", 12, 1.5, True, None, [], [{"message_id": 3}, "plain text"], ["plain text", {"message_id": 3}],
    [7, {"ok": True}], [[{"message_id": 4}]], [{"message_id": True}], [{"message_id": "6"}], [{"message_id": 6.0}],
    [{"message_id": -2}], [None, {"message_id": 8, "caption": {"nested": "x"}}], [{"result": {"message_id": 9}}],
]


def test_no_stored_answer_shape_makes_the_status_read_fail(tmp_path):
    """A legacy receipt (0.12 kept a photo's answer with message_id 0 and a text caption) once made read_status raise
    "malformed JSON": Places and the card lost the status while it stayed in the window. Every shape reads, and is
    counted exactly as delivery.stored_answers reads it."""
    from telegram_bot.delivery import stored_answers
    store = CustodyStore(tmp_path / "custody.sqlite3")
    expected = {"failed": 0, "uncertain": 0}
    rows = [({key: shape}, state) for shape in _LEGACY_ANSWERS for key in ("_sent_messages", "_sent_media")
            for state in ("failed", "uncertain")]
    rows.append(({"_sent_messages": "x", "_sent_media": [{"message_id": 1}]}, "failed"))  # _sent_messages decides
    for index, (extra, state) in enumerate(rows):
        store.enqueue_outbox(f"legacy-{index}", {"kind": "message", "chat_id": str(10_000 + index)})
        lease = store.claim_outbox()
        store.checkpoint_outbox(lease.delivery_id, {**lease.payload, **extra})
        store.mark_outbox_failed(lease.delivery_id, reason="x", state=state)
        expected[state] += bool((stored_answers({**lease.payload, **extra}) or ([], []))[0])
    counts, card = store.read_status()  # must not raise
    outbound = card["recent_problems"]["outbound"]
    assert (outbound["failed_partly_confirmed"], outbound["uncertain_partly_confirmed"]) == (
        expected["failed"], expected["uncertain"])
    assert counts["outbox_failed"] + counts["outbox_uncertain"] == len(rows)
    assert expected["failed"] > 0 and expected["uncertain"] > 0  # the shapes include confirmed ones too
    store.close()


def test_sends_telegram_confirmed_in_part_are_counted_as_stored_answers_read_them(tmp_path):
    """The receipt's confirmed_messages (delivery.stored_answers) decide; an answer naming no message confirms nothing."""
    from telegram_bot.delivery import stored_answers
    store = CustodyStore(tmp_path / "custody.sqlite3")
    payloads = {
        "album-part": {"_sent_media": [{"message_id": 11}, {"ok": True}]},     # an incomplete sendMediaGroup answer
        "chunks-part": {"_sent_messages": [{"message_id": 12}]},              # the first 4096 characters arrived
        "one-file": {"_sent_media": {"message_id": 13}},                       # one answer, not a list
        "no-id": {"_sent_messages": [{"ok": True}], "_sent_media": [{"message_id": 9}]},  # _sent_messages decides
        "nothing": {},
    }
    states = {"album-part": "uncertain", "chunks-part": "failed", "one-file": "uncertain", "no-id": "uncertain",
              "nothing": "failed"}
    for index, name in enumerate(payloads):
        store.enqueue_outbox(name, {"kind": "message", "chat_id": str(300 + index)})
        lease = store.claim_outbox()
        store.checkpoint_outbox(lease.delivery_id, {**lease.payload, **payloads[name]})
        store.mark_outbox_failed(lease.delivery_id, reason="x", state=states[name])
    expected = {name: bool((stored_answers(payloads[name]) or ([], []))[0]) for name in payloads}
    assert expected == {"album-part": True, "chunks-part": True, "one-file": True, "no-id": False, "nothing": False}
    outbound = store.card_snapshot()["recent_problems"]["outbound"]
    assert (outbound["failed"], outbound["failed_partly_confirmed"]) == (2, 1)
    assert (outbound["uncertain"], outbound["uncertain_partly_confirmed"]) == (3, 2)
    store.close()


_NO_WAIT = {"count": 0, "submit": 0, "download": 0, "other": 0, "since": "", "next_attempt_at": "",
            "last_error": "", "last_stage": "", "last_error_at": "", "last_error_recorded": False,
            "last_error_newest": False}


def _hello(store, *update_ids):
    for update_id in update_ids:
        store.commit_update(update_id, parse_telegram_update(
            {"update_id": update_id, "message": {"message_id": update_id, "text": "hi", "date": 1,
                                                 "from": {"id": 3, "is_bot": False},
                                                 "chat": {"id": 40 + update_id, "type": "private"}}},
            bot_account_id="9"))


def test_handoff_retrying_counts_only_failed_attempts_by_where_they_stopped(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    _hello(store, 31, 32, 33, 34, 35)
    assert store.card_snapshot()["handoff_retrying"] == _NO_WAIT
    # A busy Host (presence capacity full) gives the attempt back: waiting, not failing.
    busy = store.claim_inbox()
    store.defer_inbox(busy.event_id, reason="PresenceHostHTTPError: presence_capacity_full", retry_after_sec=60)
    # A lease released at runtime start is no attempt's answer either.
    store.claim_inbox()
    store.release_own_leases()
    assert store.card_snapshot()["handoff_retrying"]["count"] == 0
    # A handoff that was tried and failed is retried by itself, and counted as such.
    failed = store.claim_inbox()
    store.release_inbox(failed.event_id, reason="PresenceHostError: 500", retry_after_sec=30, stage="submit")
    retrying = store.card_snapshot()["handoff_retrying"]
    assert (retrying["count"], retrying["submit"], retrying["download"], retrying["other"]) == (1, 1, 0, 0)
    assert retrying["last_error"] == "PresenceHostError: 500" and retrying["last_stage"] == "submit"
    assert retrying["since"].endswith("Z") and retrying["next_attempt_at"].endswith("Z")
    summary = summarize(_quiet(handoff_retrying=retrying))
    assert summary["text"] == "Connected · 1 Telegram update waits to be passed to Ouroboros"
    assert summary["attention"][0]["stages"] == {"submit": 1, "download": 0, "other": 0}
    # Telegram's getFile refused (429) before any handoff: a download retry, never a failed handoff.
    download = store.claim_inbox()
    store.release_inbox(download.event_id, reason="TelegramApiError: Telegram getFile failed (429): Too Many Requests",
                        retry_after_sec=30, stage="download")
    retrying = store.card_snapshot()["handoff_retrying"]
    assert (retrying["count"], retrying["submit"], retrying["download"]) == (2, 1, 1)
    # A busy deferral after an earlier Host error is waiting again, not a failure.
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
        conn.execute("UPDATE inbox SET available_at=0 WHERE event_id=?", (failed.event_id,))
    again = store.claim_inbox()
    while again is not None and again.event_id != failed.event_id:
        store.release_inbox(again.event_id, reason="", retry_after_sec=3600)
        again = store.claim_inbox()
    store.defer_inbox(failed.event_id, reason="PresenceHostHTTPError: presence_capacity_full", retry_after_sec=0)
    assert store.card_snapshot()["handoff_retrying"]["submit"] == 0
    # Recovery: once Ouroboros takes it, nothing is listed for it (whatever the runtime's sticky error says).
    again = store.claim_inbox()
    while again is not None and again.event_id != failed.event_id:
        store.release_inbox(again.event_id, reason="", retry_after_sec=3600)
        again = store.claim_inbox()
    store.record_submission(failed.event_id, binding_id="", turn_ref="turn-1", outcome="silent", text="", work_ref="")
    assert store.card_snapshot()["handoff_retrying"]["submit"] == 0
    store.close()


def test_the_inbox_worker_records_where_an_attempt_stopped(tmp_path):
    class Host:
        def __init__(self):
            self.calls, self.answer = 0, None

        async def submit(self, event, staged_files):
            self.calls += 1
            raise self.answer

    async def run():
        host = Host()
        runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                       submitter=host)
        _hello(runtime.store, 41)

        async def refused_download(_lease):
            raise TelegramApiError("getFile", "Too Many Requests", 429)
        runtime._stage_attachments = refused_download
        assert await runtime.process_one_inbox()
        waiting = runtime.store.card_snapshot()["handoff_retrying"]
        assert host.calls == 0 and (waiting["download"], waiting["submit"]) == (1, 0)

        async def nothing(_lease):
            return []
        runtime._stage_attachments = nothing
        with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
            conn.execute("UPDATE inbox SET available_at=0")
        host.answer = PresenceHostError("Host answered 500")
        assert await runtime.process_one_inbox()
        waiting = runtime.store.card_snapshot()["handoff_retrying"]
        assert host.calls == 1 and (waiting["download"], waiting["submit"]) == (0, 1)
        with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
            conn.execute("UPDATE inbox SET available_at=0")
        host.answer = PresenceHostHTTPError(429, "busy", {"ok": False, "code": "presence_capacity_full",
                                                           "disposition": "retry", "field": "capacity",
                                                           "retry_after": 1})
        assert host.answer.busy_retry_after == 1.0
        assert await runtime.process_one_inbox()
        assert runtime.store.card_snapshot()["handoff_retrying"]["count"] == 0  # deferred: waiting, not failing
        runtime.store.close()

    asyncio.run(run())


# 0.13.0's own statements (telegram_bot/custody.py at c60c7ad), run against this version's store as a rollback runs
# them: they name their columns, change attempts, last_error and available_at, and never touch the 0.13.1 marks.
_V0130_CLAIM_INBOX = ("UPDATE inbox SET state='leased', lease_until=?, attempts=attempts+1, last_error='' "
                      "WHERE event_id=? AND state='pending'")
_V0130_RELEASE_INBOX = ("UPDATE inbox SET state='pending', available_at=?, lease_until=0, last_error=? "
                        "WHERE event_id=? AND state='leased'")
_V0130_DEFER_INBOX = ("UPDATE inbox SET state='pending', available_at=?, lease_until=0, last_error=?, "
                      "attempts=MAX(0, attempts-1) WHERE event_id=? AND state='leased'")
_V0130_CLAIM_OUTBOX = ("UPDATE outbox SET state='leased', lease_until=?, attempts=attempts+1, last_error='' "
                       "WHERE delivery_id=? AND state='pending'")
_V0130_HOLD_OUTBOX = ("UPDATE outbox SET state='pending', available_at=?, lease_until=0, last_error=?, "
                      "attempts=MAX(0, attempts-1) WHERE delivery_id=? AND state='leased'")


def _as_0130(path, *statements):
    with sqlite3.connect(path) as conn:
        for sql, params in statements:
            assert conn.execute(sql, params).rowcount == 1, sql


def test_the_waiting_updates_error_is_the_latest_failed_attempts(tmp_path, monkeypatch):
    """The newer update's download failed first, then the older update's handoff failed: the card shows the handoff
    error (the latest attempt), with its time, not the newest update's older error."""
    store = CustodyStore(tmp_path / "custody.sqlite3")
    _hello(store, 81, 82)
    older, newer = store.claim_inbox(), store.claim_inbox()
    assert (older.event_id, newer.event_id) == ("telegram:9:81", "telegram:9:82")
    clock = [1_800_000_000.0]
    monkeypatch.setattr("telegram_bot.custody.time.time", lambda: clock[0])
    store.release_inbox(newer.event_id, reason="TelegramApiError: Telegram getFile failed (429)", retry_after_sec=30,
                        stage="download")
    clock[0] += 60
    store.release_inbox(older.event_id, reason="PresenceHostError: presence Host returned HTTP 500", retry_after_sec=30,
                        stage="submit")
    waiting = store.card_snapshot(now=clock[0])["handoff_retrying"]
    assert waiting["last_error"] == "PresenceHostError: presence Host returned HTTP 500"
    assert waiting["last_stage"] == "submit" and waiting["last_error_at"] == _iso(clock[0])
    item = summarize(_quiet(handoff_retrying=waiting))["attention"][0]
    assert item["error"] == {"text": "PresenceHostError: presence Host returned HTTP 500", "at": _iso(clock[0]),
                             "stage": "submit"}
    store.close()


def test_an_update_ouroboros_took_but_this_computer_failed_to_record_is_not_called_unsent(tmp_path):
    """Astra's sequence: the Host answered with a completed turn, then a competing SQLite writer blocked
    record_submission. The update waits again with no recorded stage; the card must not say it waits to be passed
    to Ouroboros: whether it was handled is not confirmed here. A recorded download or handoff failure keeps its words."""
    from telegram_bot.host import PresenceSubmission

    class AcceptingHost:
        async def submit(self, event, staged_files):
            return PresenceSubmission(status="completed", outcome="silent", text="", turn_ref="turn-1", work_ref="",
                                      binding_id="b" * 32)

    async def run():
        runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                       submitter=AcceptingHost())

        def locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")
        runtime.store.record_submission = locked
        _hello(runtime.store, 97)
        assert await runtime.process_one_inbox()
        waiting = runtime.store.card_snapshot()["handoff_retrying"]
        runtime.store.close()
        return waiting

    waiting = asyncio.run(run())
    assert (waiting["count"], waiting["submit"], waiting["download"], waiting["other"]) == (1, 0, 0, 1)
    summary = summarize(_quiet(handoff_retrying=waiting))
    assert summary["text"] == "Connected · 1 Telegram update waits for another attempt"
    assert summary["attention"][0]["stages"] == {"submit": 0, "download": 0, "other": 1}
    # A recorded handoff failure keeps the specific words.
    failed = summarize(_quiet(handoff_retrying=_retrying(1)))
    assert failed["text"] == "Connected · 1 Telegram update waits to be passed to Ouroboros"


def test_a_note_an_earlier_version_left_is_never_called_the_last_error(tmp_path):
    """Astra's sequence with 0.13.0's own statements: a submission failed, then a busy Host deferred the update (which
    gives the attempt back and replaces the error with the capacity notice), then 0.13.1 runs again. The update still
    waits and is counted, but its note is not called the latest error: nothing shows the last attempt failed."""
    path = tmp_path / "custody.sqlite3"
    store = CustodyStore(path)
    _hello(store, 95)
    store.close()
    event_id, later = "telegram:9:95", time.time() + 30
    _as_0130(path, (_V0130_CLAIM_INBOX, (later + 1800, event_id)),
             (_V0130_RELEASE_INBOX, (later, "PresenceHostError: presence Host returned HTTP 500", event_id)),
             (_V0130_CLAIM_INBOX, (later + 1800, event_id)),
             (_V0130_DEFER_INBOX, (later + 5, "PresenceHostHTTPError: presence Host returned HTTP 429: "
                                               "presence_capacity_full", event_id)))
    store = CustodyStore(path)
    waiting = store.card_snapshot()["handoff_retrying"]
    assert waiting["count"] == 1 and waiting["last_error_newest"] is False
    assert waiting["last_error"].endswith("presence_capacity_full") and waiting["last_error_at"] == ""
    item = summarize(_quiet(handoff_retrying=waiting))["attention"][0]
    assert item["events"] == 1 and item["error"]["unrecorded"] is True and "example" not in item["error"]
    store.close()


def test_an_error_whose_recency_is_unknown_is_shown_as_an_example(tmp_path, monkeypatch):
    """0.13.1 stamped a download failure at 08:00:00; after a rollback 0.13.0 released another update at 08:00:23
    without a stamp. Which failure is the latest cannot be told, so the card shows the error as an example."""
    path = tmp_path / "custody.sqlite3"
    store = CustodyStore(path)
    _hello(store, 91, 92)
    stamped, later = store.claim_inbox(), store.claim_inbox()
    clock = [1_800_000_000.0]
    monkeypatch.setattr("telegram_bot.custody.time.time", lambda: clock[0])
    store.release_inbox(stamped.event_id, reason="TelegramApiError: Telegram getFile failed (429)", retry_after_sec=30,
                        stage="download")
    _as_0130(path, (_V0130_RELEASE_INBOX, (clock[0] + 53, "PresenceHostError: HTTP 500", later.event_id)))
    waiting = store.card_snapshot(now=clock[0] + 30)["handoff_retrying"]
    assert waiting["count"] == 2 and waiting["last_error_newest"] is False
    error = summarize(_quiet(handoff_retrying=waiting))["attention"][0]["error"]
    assert error["example"] is True
    # Once every waiting update's last attempt has a recorded end, the latest is known again.
    store.close()
    store = CustodyStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE inbox SET available_at=0 WHERE event_id=?", (later.event_id,))
    again = store.claim_inbox()
    assert again.event_id == later.event_id
    store.release_inbox(again.event_id, reason="PresenceHostError: HTTP 500", retry_after_sec=30, stage="submit")
    waiting = store.card_snapshot(now=clock[0] + 30)["handoff_retrying"]
    assert waiting["last_error_newest"] is True and waiting["last_error"] == "PresenceHostError: HTTP 500"
    assert "example" not in summarize(_quiet(handoff_retrying=waiting))["attention"][0]["error"]
    store.close()


def test_marks_left_by_0131_mean_nothing_once_0130_rewrote_the_row(tmp_path):
    """0.13.1 -> 0.13.0 -> 0.13.1: 0.13.0 claims and settles a row without the 0.13.1 marks. A stale "deferred" must
    not hide its real Host refusal, a stale "skill_access" must not make an ordinary pause the owner's move."""
    path = tmp_path / "custody.sqlite3"
    store = CustodyStore(path)
    _hello(store, 71)
    busy = store.claim_inbox()
    store.defer_inbox(busy.event_id, reason="PresenceHostHTTPError: presence_capacity_full", retry_after_sec=0)
    store.enqueue_outbox("held", {"kind": "message", "chat_id": "700"})
    store.hold_outbox(store.claim_outbox().delivery_id, SKILL_ACCESS_REFUSED, kind="skill_access")
    assert store.card_snapshot()["held_reasons"][0]["kind"] == "skill_access"
    store.close()
    # Rolled back to 0.13.0: the deferred update is retried and refused; the held send is retried and paused.
    later = time.time() + 30
    _as_0130(path, (_V0130_CLAIM_INBOX, (later + 1800, busy.event_id)),
             (_V0130_RELEASE_INBOX, (later, "PresenceHostError: presence Host returned HTTP 500", busy.event_id)),
             (_V0130_CLAIM_OUTBOX, (later + 90, "held")),
             (_V0130_HOLD_OUTBOX, (later + 5, "Held: Example place is paused", "held")))
    # 0.13.0 still adds rows to this store with its own explicit column lists.
    _as_0130(path, ("INSERT OR IGNORE INTO outbox(delivery_id, payload_json, state, attempts, available_at, created_at) "
                    "VALUES (?, ?, 'pending', 0, ?, ?)", ("from-0130", '{"kind": "message", "chat_id": "701"}', later, later)))
    # Back on 0.13.1.
    store = CustodyStore(path)
    card = store.card_snapshot()
    waiting = card["handoff_retrying"]
    assert (waiting["count"], waiting["submit"], waiting["download"], waiting["other"]) == (1, 0, 0, 1)
    assert waiting["last_error"] == "PresenceHostError: presence Host returned HTTP 500" and waiting["last_stage"] == ""
    assert card["held_reasons"] == [{"reason": "Example place is paused", "note": "Held: Example place is paused",
                                     "count": 1}]
    summary = summarize(_quiet(handoff_retrying=waiting, outbox_held=1, held_reasons=card["held_reasons"]))
    held = next(item for item in summary["attention"] if item["kind"] == "held")
    assert held["tone"] == "muted" and not any(reason.get("action") for reason in held["reasons"])
    assert any(item["kind"] == "handoff_waiting" for item in summary["attention"])
    store.close()


def test_an_older_store_gains_the_stage_column_and_its_waiting_rows_read_as_unrecorded(tmp_path):
    path = tmp_path / "custody.sqlite3"
    store = CustodyStore(path)
    _hello(store, 51)
    lease = store.claim_inbox()
    store.release_inbox(lease.event_id, reason="PresenceHostError: 500", retry_after_sec=30, stage="submit")
    store.close()
    with sqlite3.connect(path) as conn:  # the 0.13.0 schema: no last_stage
        for table, column in (("inbox", "last_stage"), ("inbox", "last_stage_at"), ("inbox", "last_stage_due"),
                              ("outbox", "hold_kind"), ("outbox", "hold_kind_due")):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    reopened = CustodyStore(path)
    waiting = reopened.card_snapshot()["handoff_retrying"]
    # The row keeps waiting and its error; where it stopped was not recorded, so it is not called a failed handoff.
    assert (waiting["count"], waiting["submit"], waiting["download"], waiting["other"]) == (1, 0, 0, 1)
    assert waiting["last_error"] == "PresenceHostError: 500" and waiting["last_stage"] == ""
    assert reopened.card_snapshot()["held_reasons"] == []  # the outbox gained hold_kind too
    reopened.close()
    CustodyStore(path).close()  # opening again changes nothing (idempotent)


def test_a_kind_with_no_failed_send_is_not_looked_for(tmp_path):
    """The newest plain failure and the newest unknown outcome are read only when the shared pass counted some: with
    none, the walk of outbox_failed_idx would read every failed row's outcome mark to find nothing."""
    store = CustodyStore(tmp_path / "custody.sqlite3")

    def newest_queries():
        seen = []
        with store._connect() as conn:
            conn.set_trace_callback(seen.append)
        try:
            counts, card = store.read_status()
        finally:
            with store._connect() as conn:
                conn.set_trace_callback(None)
        lookups = [sql for sql in seen if "ORDER BY failed_at DESC, rowid DESC LIMIT 1" in sql]
        return {"plain": sum("AND NOT (" in sql for sql in lookups), "uncertain": sum("AND NOT (" not in sql for sql in lookups)}, card

    assert newest_queries()[0] == {"plain": 0, "uncertain": 0}
    for index, state in enumerate(("failed", "failed")):
        store.enqueue_outbox(f"plain-{index}", {"kind": "message", "chat_id": str(900 + index)})
        store.mark_outbox_failed(store.claim_outbox().delivery_id, reason="Forbidden", state=state)
    queries, card = newest_queries()
    assert queries == {"plain": 1, "uncertain": 0}
    outbound = card["recent_problems"]["outbound"]
    assert outbound["last_failed_error"]["text"] == "Forbidden" and outbound["last_uncertain_error"] is None
    store.enqueue_outbox("unknown", {"kind": "message", "chat_id": "910"})
    store.mark_outbox_failed(store.claim_outbox().delivery_id, reason="TimeoutError", state="uncertain")
    queries, card = newest_queries()
    assert queries == {"plain": 1, "uncertain": 1}
    assert card["recent_problems"]["outbound"]["last_uncertain_error"]["text"] == "TimeoutError"
    store.close()


def test_status_totals_and_card_facts_come_from_one_read_transaction(tmp_path, monkeypatch):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    store.enqueue_outbox("sending", {"kind": "message", "chat_id": "100"})
    lease = store.claim_outbox()
    original = CustodyStore._card_facts.__func__

    def card_after_a_send_finished(cls, conn, now, window_s, shared=None):
        # Another connection settles the send after the totals were read, before the card's own newest-row reads.
        with sqlite3.connect(tmp_path / "custody.sqlite3") as other:
            other.execute("UPDATE outbox SET state='failed', failed_at=?, last_error='Forbidden' WHERE delivery_id=?",
                          (time.time(), lease.delivery_id))
        return original(cls, conn, now, window_s, shared)

    monkeypatch.setattr(CustodyStore, "_card_facts", classmethod(card_after_a_send_finished))
    counts, card = store.read_status()
    # One moment: still sending everywhere, never "sending" beside a newest failure of the same row.
    outbound = card["recent_problems"]["outbound"]
    assert counts["outbox_leased"] == 1 and counts["outbox_failed"] == 0 and outbound["failed"] == 0
    assert outbound["last_failed_at"] == "" and outbound["last_failed_error"] is None
    monkeypatch.undo()
    counts, card = store.read_status()
    outbound = card["recent_problems"]["outbound"]
    assert counts["outbox_leased"] == 0 and outbound["failed"] == 1 and outbound["last_failed_error"]["text"] == "Forbidden"
    store.close()


# ---------- runtime: when and where the newest error was recorded ----------

def test_last_error_records_its_time_and_the_loop_that_wrote_it(tmp_path):
    runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                   submitter=object())
    assert runtime.status_snapshot({})["last_error_at"] == "" and runtime._last_error == ""

    async def in_task(name):
        async def write():
            runtime._last_error = f"from {name}"
        await asyncio.create_task(write(), name=name)
        return runtime.status_snapshot({})

    for name, source in (("telegram-presence-outbox", "outbound"), ("telegram-presence-inbox-1", "inbound"),
                         ("telegram-presence-work", "inbound"), ("Task-7", "polling")):
        snapshot = asyncio.run(in_task(name))
        assert snapshot["last_error"] == f"from {name}"
        assert snapshot["last_error_source"] == source and snapshot["last_error_at"].endswith("Z")
    runtime._last_error = ""
    assert runtime.status_snapshot({})["last_error_at"] == "" and runtime.status_snapshot({})["last_error_source"] == ""
    runtime.store.close()


def test_the_last_error_is_read_whole_never_one_loops_text_with_anothers_source(tmp_path, monkeypatch):
    """A status read during an error's write saw the new text with the previous loop's source ("Could not send"
    labelled inbound). The text, its time and its loop are one record, replaced whole and read once."""
    runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                   submitter=object())

    async def write(name, value):
        async def assign():
            runtime._last_error = value
        await asyncio.create_task(assign(), name=name)

    asyncio.run(write("telegram-presence-inbox-0", "PresenceHostError: Host refused"))
    seen, real = [], runtime_mod._error_source

    def read_during_the_write():
        snapshot = runtime.status_snapshot({})  # as the route reads it from its own thread, mid-write
        seen.append((snapshot["last_error"], snapshot["last_error_source"], bool(snapshot["last_error_at"])))
        return real()
    monkeypatch.setattr(runtime_mod, "_error_source", read_during_the_write)
    asyncio.run(write("telegram-presence-outbox", "TelegramApiError: Could not send"))
    assert seen == [("PresenceHostError: Host refused", "inbound", True)]
    after = runtime.status_snapshot({})
    assert (after["last_error"], after["last_error_source"]) == ("TelegramApiError: Could not send", "outbound")
    runtime._last_error = ""
    cleared = runtime.status_snapshot({})
    assert (cleared["last_error"], cleared["last_error_at"], cleared["last_error_source"]) == ("", "", "")
    runtime.store.close()


def test_the_running_workers_record_which_loop_wrote_the_newest_error(tmp_path):
    """run() names its tasks; an inbox worker's failure is recorded as inbound, the poll loop's own as polling."""
    class Host:
        async def submit(self, event, staged_files):
            raise PresenceHostError("Host answered 500")

    async def run():
        runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                       submitter=Host())

        async def brief(_seconds):
            await asyncio.sleep(0.01)
        runtime._pause = brief
        _hello(runtime.store, 61)
        task = asyncio.create_task(runtime.run())
        try:
            await _until(lambda: runtime.status_snapshot({})["last_error"].startswith("PresenceHostError"))
            snapshot = runtime.status_snapshot({})
            assert snapshot["last_error_source"] == "inbound" and snapshot["last_error_at"].endswith("Z")
        finally:
            runtime.stop()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


# ---------- the route ----------

def _load(tmp_path):
    skill_dir = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("telegram_bot_card_route_test", skill_dir / "plugin.py",
                                                  submodule_search_locations=[str(skill_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    return module, api


def test_route_adds_card_fields_and_keeps_every_existing_key(tmp_path):
    module, api = _load(tmp_path)
    try:
        payload = json.loads(asyncio.run(module._status_route(None)).body)
    finally:
        api.unload()
    for key in ("runtime_state", "bot_label", "last_event_at", "last_delivery_at", "last_error", "delivery_reporting",
                "presence_continuation_version", "reaction_context", "reaction_history", "has_presence_binding",
                "management_group_id", "binding_state", "connect", "telegram_offset", "inbox_waiting", "inbox_leased",
                "inbox_submitted", "inbox_failed", "outbox_waiting", "outbox_held", "outbox_delivered", "outbox_failed",
                "outbox_uncertain", "text", "tone", "title"):
        assert key in payload
    assert payload["observed_at"].endswith("Z")
    assert payload["host_checked"] is False and payload["places_version"] == 0
    assert payload["last_error_at"] == "" and payload["provider_ok_at"] == ""
    assert (payload["poll_result"], payload["poll_error"], payload["poll_failure"]) == ("", "", "")
    assert payload["handoff_retrying"]["count"] == 0
    assert payload["bot_unconfirmed"] is False  # not running: no bot named, nothing to confirm
    assert payload["link"] == "not_started" and payload["poll_stage"] == ""  # the first line's own link judgement
    assert payload["recent_problems"]["outbound"]["failed"] == 0 and payload["held_reasons"] == []
    assert (payload["text"], payload["tone"], payload["title"]) == ("Starting…", "muted", "Telegram transport")


def test_one_route_answer_reads_the_token_and_the_bot_once(tmp_path):
    """A token rotation between two reads once paired bot A's name and ok poll with bot B's chats."""
    module, api = _load(tmp_path)
    try:
        runtime = module._RUNTIME
        runtime.store.record_confirmed_bot("token-a", "9")
        runtime._run_task, runtime._client_token, runtime._bot_id, runtime._bot_label = object(), "token-a", "9", "@a_bot"
        runtime._poll_done("token-a", "ok")
        reads = []

        def rotating():
            reads.append(1)
            return "token-a" if len(reads) == 1 else "token-b"  # B is pasted right after the first read
        runtime.token_provider = rotating
        runtime.confirmed_bot = lambda: (_ for _ in ()).throw(AssertionError("a second runtime read"))
        payload = module._status_payload()
        assert len(reads) == 1
        # One moment: A's name, A's ok attempt and A's chat list (empty here, never null) go together.
        assert payload["bot_label"] == "@a_bot" and payload["poll_result"] == "ok" and payload["connect"] == []
        assert "confirmed_account" not in payload and "confirmed_reason" not in payload
    finally:
        runtime._run_task = None
        api.unload()


# ---------- the Host's capabilities: one completed check, read whole ----------

def _host_client(tmp_path, transport):
    from telegram_bot.host import PresenceHostClient
    return PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "skill-token", host_base="http://127.0.0.1:9",
                              transport=transport)


_SUPPORTING = {"presence_delivery_version": 1, "places_version": 1, "presence_reaction_context": 1,
               "presence_explicit_completion": 1, "presence_continuation_versions": [2], "presence_identity": 1}


def _host_facts(runtime):
    snapshot = runtime.status_snapshot({})
    return (snapshot["host_checked"], snapshot["reaction_context"], snapshot["presence_continuation_version"],
            bool(snapshot["host_checked_at"]))


def test_a_status_read_during_the_capability_check_sees_one_check_whole(tmp_path):
    """/identity completing during a status read once gave host_checked=true and the new time beside the old
    reaction_context, and the card advised an update the Host did not need."""
    seen = []

    class Answer(dict):
        def get(self, key, default=None):
            if key == "presence_continuation_versions":  # the answer is being read: a status read lands here
                seen.append(_host_facts(runtime))
            return super().get(key, default)

    class Transport:
        async def request_json(self, method, url, **kwargs):
            return Answer(_SUPPORTING)

    host = _host_client(tmp_path / "host", Transport())
    runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                   submitter=host)
    before = _host_facts(runtime)
    assert before == (False, "update_required", 0, False)
    asyncio.run(host.discover_delivery_support())
    assert seen == [before]  # the read inside the check saw the previous check whole
    assert _host_facts(runtime) == (True, "available", 2, True)
    runtime.store.close()


class _GatedIdentity:
    """A Host whose /identity requests each answer when their own gate opens: with support, or by timing out."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0
        self.gates = [asyncio.Event() for _ in self.answers]

    async def request_json(self, method, url, **kwargs):
        call, self.calls = self.calls, self.calls + 1
        await self.gates[call].wait()
        if self.answers[call] == "timeout":
            raise TimeoutError()
        return dict(_SUPPORTING)


_REACTION = {"message": {"reaction": {"emoji": "+1"}}}


def test_a_queued_reaction_whose_own_check_answers_yes_is_passed_on(tmp_path):
    """Astra's order: a queued reaction starts the /identity check, the poll loop asks meanwhile, the later ask times
    out and then the reaction's own check answers with support. One check serves both, so its answer reaches the
    reaction (it was withheld for good when the later timeout was published over it)."""
    async def run():
        transport = _GatedIdentity(["supported", "timeout"])
        host = _host_client(tmp_path / "host", transport)
        runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                       submitter=host)
        reaction = asyncio.create_task(runtime._reaction_deliverable(_REACTION))
        await asyncio.sleep(0)
        poll = asyncio.create_task(host.discover_delivery_support())
        await asyncio.sleep(0)
        transport.gates[1].set()  # a second check, if one was started, times out first
        await asyncio.sleep(0.01)
        transport.gates[0].set()  # then the reaction's own check answers with support
        assert await reaction is True
        assert await poll == 1
        assert transport.calls == 1 and host.capabilities.status == "supported"
        runtime.store.close()

    asyncio.run(run())


def test_overlapping_callers_share_one_check_and_its_whole_answer(tmp_path):
    """Carbon's order: the earlier check answers with support, a later one would time out. Callers that arrive while
    a check runs wait for that check: no second request, and the settled answer is never replaced by a timeout that
    keeps the support's places, identity and wake fields beside an "unavailable" status."""
    async def run():
        transport = _GatedIdentity(["supported", "timeout"])
        host = _host_client(tmp_path, transport)
        first = asyncio.create_task(host.discover_delivery_support())
        await asyncio.sleep(0)
        second = asyncio.create_task(host.discover_delivery_support())
        await asyncio.sleep(0)
        transport.gates[0].set()
        await asyncio.sleep(0.01)
        transport.gates[1].set()
        assert (await first, await second) == (1, 1)
        record = host.capabilities
        assert transport.calls == 1
        assert (record.status, record.places_version, record.identity, record.wake_signals) == ("supported", 1, 1, 0)
        assert (record.reaction_context, record.explicit_completion, record.continuation_version) == (1, 1, 2)
        # A settled answer is kept for the client's life (SKILL.md: "keeps a Host's supported or unsupported
        # /identity answer until it restarts"): no further request.
        assert await host.discover_delivery_support() == 1 and transport.calls == 1

    asyncio.run(run())


def test_an_unanswered_check_is_a_fresh_record_and_the_next_ask_checks_again(tmp_path):
    """A check that got no answer publishes a fresh "unavailable" record (nothing carried from anything earlier), and
    the next caller asks again (the unknown-Host behaviour is kept)."""
    from telegram_bot.host import HostCapabilities

    async def run():
        transport = _GatedIdentity(["timeout", "supported"])
        for gate in transport.gates:
            gate.set()
        host = _host_client(tmp_path, transport)
        assert await host.discover_delivery_support() == 0
        assert host.capabilities == HostCapabilities(status="unavailable", checked_at=host.capabilities.checked_at)
        assert host.capabilities.checked_at > 0
        assert await host.discover_delivery_support() == 1 and transport.calls == 2

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["empty", "raises"])
def test_a_check_that_made_no_request_is_never_called_unanswered(tmp_path, provider):
    """Review wave 5, 7d. Without this skill's Ouroboros access token (an empty one, or a Host that cannot issue it)
    the bot makes no /identity request at all. Every gate acts as for an unanswered check (a send to a place is held),
    but the route and the card say Ouroboros was not asked, never that it did not answer."""
    from telegram_bot.host import PresenceHostClient

    class Transport:
        calls = 0

        async def request_json(self, method, url, **kwargs):
            Transport.calls += 1
            return dict(_SUPPORTING)

    def skill_token():
        if provider == "raises":
            raise ValueError("SkillToken cannot be empty")  # what the Host's SkillToken says for an empty token
        return ""

    host = PresenceHostClient(state_dir=tmp_path / "host", token_provider=skill_token,
                              host_base="http://127.0.0.1:9", transport=Transport())
    runtime = runtime_mod.TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                                   submitter=host)
    try:
        assert asyncio.run(host.discover_delivery_support()) == 0
        assert asyncio.run(host.place_hold("9", "-1")) == PLACE_POLICY_UNREAD  # gated as before: held, retried
        assert Transport.calls == 0  # nothing was asked
        snapshot = runtime.status_snapshot({})
        assert (snapshot["delivery_reporting"], snapshot["host_checked"]) == ("unavailable", False)
        payload = _quiet(host_checked=False, delivery_reporting=snapshot["delivery_reporting"],
                         host_unasked=snapshot.get("host_unasked", ""), host_checked_at=snapshot["host_checked_at"])
        assert summarize(payload)["attention"][0]["gaps"] == ["host_unasked"]
    finally:
        runtime.store.close()


@pytest.mark.parametrize("when", ["before it ran", "in flight"])
def test_no_caller_waits_forever_on_a_cancelled_check(tmp_path, when):
    """The shared check is cancelled (a shutdown), before its request or while it waits for the answer: every waiting
    caller returns at once, as for an unanswered check, without inheriting the cancellation; the next caller asks
    again. A caller cancelled while waiting does not cancel the check for the others."""
    async def run():
        transport = _GatedIdentity(["supported", "supported"])
        host = _host_client(tmp_path, transport)
        waiting = [asyncio.create_task(host.discover_delivery_support()) for _ in range(2)]
        await asyncio.sleep(0)
        if when == "in flight":
            await _until(lambda: transport.calls == 1)
        host._discovery_task.cancel()
        results = await asyncio.wait_for(asyncio.gather(*waiting), timeout=2)
        assert results == [0, 0] and host.capabilities.status == "unknown"
        asked = transport.calls
        # A cancelled waiter leaves the next check running for the other one.
        transport.gates = [asyncio.Event() for _ in transport.answers]
        transport.calls = 0
        leaving = asyncio.create_task(host.discover_delivery_support())
        staying = asyncio.create_task(host.discover_delivery_support())
        await _until(lambda: transport.calls == 1)
        leaving.cancel()
        await asyncio.sleep(0)
        transport.gates[0].set()
        assert await asyncio.wait_for(staying, timeout=2) == 1 and leaving.cancelled()
        assert transport.calls == 1 and host.capabilities.status == "supported" and asked == (when == "in flight")

    asyncio.run(run())


