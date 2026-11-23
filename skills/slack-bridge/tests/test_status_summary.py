"""The status line rule shared by Places and the Widgets card, plus the companion-PID guards it relies on."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import lib.store as store_mod
from lib.status_summary import SKILL_ACCESS_REFUSED, TONE_RANK, attention_items, summarize
from lib.store import BridgeStore


def _quiet(**overrides):
    payload = {
        "link": {"state": "connected", "detail": "ok", "companion_alive": True, "since": None, "error": ""},
        "recent_problems": {
            "window_seconds": 86400,
            "inbound": {"failed": 0, "uncertain": 0, "last_failed_at": None, "last_uncertain_at": None, "last_error": None},
            "outbound_text": {"failed": 0, "uncertain": 0, "failed_messages": 0, "uncertain_messages": 0,
                              "last_failed_at": None, "last_uncertain_at": None, "last_error": None},
            "mutations": {"failed": 0, "uncertain": 0, "last_failed_at": None, "last_uncertain_at": None, "last_error": None},
        },
        "outbox_held": 0,
        "outbound_text_chunks": {"held": 0},
        "outbound_messages": {"held": 0},
        "reaction_context_state": "available",
        "last_delivery_error": "",
        "inbox_pending": 0,
    }
    payload.update(overrides)
    return payload


def _with_recent(**parts):
    payload = _quiet()
    for key, values in parts.items():
        payload["recent_problems"][key].update(values)
    return payload


def test_healthy_connected_says_nothing_needs_attention_with_ok_tone():
    assert summarize(_quiet()) == {"text": "Connected · nothing needs attention", "tone": "ok", "attention": []}


@pytest.mark.parametrize(
    ("state", "detail", "text", "tone"),
    [
        ("starting", "connecting", "Connecting to Slack…", "muted"),
        ("reconnecting", "refresh_requested", "Reconnecting to Slack…", "muted"),
        ("disconnected", "companion_not_running", "Disconnected · the bridge process is not running", "error"),
        ("disconnected", "disconnected", "Disconnected · the bridge closed its Slack connection", "error"),
        ("disconnected", "not_started", "Disconnected · the bridge has not started", "error"),
        ("not_configured", "missing_tokens", "Not set up · Slack tokens are missing", "warn"),
        ("not_configured", "missing_app_token", "Not set up · the Slack app token is missing", "warn"),
        ("error", "socket_error", "Connection error · the Slack connection failed", "error"),
        ("error", "unreadable_settings", "Connection error · the bridge settings can't be read", "error"),
        ("connected", "host_adapter_inactive", "Connected · Slack events are not passed to Ouroboros", "warn"),
    ],
)
def test_every_link_state_has_words_and_its_own_tone(state, detail, text, tone):
    payload = _quiet(link={"state": state, "detail": detail, "companion_alive": True, "since": None, "error": ""})
    summary = summarize(payload)
    assert (summary["text"], summary["tone"]) == (text, tone)


def test_link_error_text_travels_with_its_item():
    payload = _quiet(link={"state": "error", "detail": "socket_error", "companion_alive": True, "since": None,
                           "error": "invalid_auth"})
    assert attention_items(payload)[0]["error"] == {"text": "invalid_auth"}


def test_attention_order_is_worst_tone_first_then_consequence():
    payload = _with_recent(
        inbound={"failed": 1, "last_failed_at": "2026-11-22T20:00:00Z"},
        outbound_text={"failed": 2, "failed_messages": 1, "uncertain": 1, "uncertain_messages": 1,
                       "last_uncertain_at": "2026-11-22T21:00:00Z", "last_failed_at": "2026-11-22T21:30:00Z"},
        mutations={"uncertain": 2, "last_uncertain_at": "2026-11-22T21:10:00Z"},
    )
    payload.update(outbox_held=1, outbound_text_chunks={"held": 1}, outbound_messages={"held": 1},
                   reaction_context_state="update_required", reactions_withheld=4,
                   link={"state": "disconnected", "detail": "companion_not_running", "companion_alive": False,
                         "since": None, "error": ""})
    items = attention_items(payload)
    # error (link, not delivered, not handled), then warn (outcome unknown, reactions), then muted (held by a place).
    assert [item["kind"] for item in items] == [
        "link", "not_delivered", "not_handled", "outcome_unknown", "reactions", "held"]
    unknown = items[3]
    assert unknown["headline"] == "Outcome unknown for 1 message and 2 actions"
    assert unknown["at"] == "2026-11-22T21:10:00Z"
    summary = summarize(payload)
    assert summary["tone"] == "error"
    assert summary["text"] == "Disconnected · the bridge process is not running · 5 more need attention"


def test_the_first_line_names_the_item_that_sets_its_tone():
    # An unknown outcome (warn) beside a refusal (error): the red line must name the refusal, not the unknown.
    payload = _with_recent(outbound_text={"failed": 1, "failed_messages": 1, "uncertain": 3, "uncertain_messages": 3})
    summary = summarize(payload)
    assert summary["tone"] == "error"
    assert summary["text"] == "Connected · 1 message not delivered · 1 more needs attention"
    assert [item["kind"] for item in summary["attention"]] == ["not_delivered", "outcome_unknown"]


def test_a_partly_known_state_is_never_all_clear():
    unanswered = summarize(_quiet(history_reporting_state="unavailable", reaction_context_state="update_required"))
    assert (unanswered["text"], unanswered["tone"]) == ("Connected · state partly known", "muted")
    # A guess made while Ouroboros did not answer is not reported as needing an update (inventory D8).
    assert [item["kind"] for item in unanswered["attention"]] == ["partial"]
    assert unanswered["attention"][0]["gaps"] == ["host_unanswered"]
    unchecked = summarize(_quiet(reaction_context_state="unknown"))
    assert unchecked["tone"] == "muted" and unchecked["attention"][0]["gaps"] == ["capabilities_unchecked"]
    legacy = _quiet()
    del legacy["recent_problems"]
    assert summarize(legacy)["attention"][0]["gaps"] == ["no_recent_window"]
    # Not connected: the link item already says what is known.
    down = _quiet(reaction_context_state="unknown", link={"state": "disconnected", "detail": "disconnected"})
    assert [item["kind"] for item in summarize(down)["attention"]] == ["link"]


def test_outcome_unknown_is_never_called_a_failure_and_reads_as_the_owners_check():
    payload = _with_recent(outbound_text={"uncertain": 1, "uncertain_messages": 1,
                                          "last_uncertain_at": "2026-11-22T21:00:00Z",
                                          "last_error": {"at": "2026-11-22T21:00:00Z", "state": "uncertain",
                                                         "text": "Transport dropped after send started"}})
    summary = summarize(payload)
    assert summary["tone"] == "warn"
    assert summary["text"] == "Connected · outcome unknown for 1 message"
    item = summary["attention"][0]
    assert "fail" not in item["headline"].lower()
    assert item["error"]["state"] == "uncertain"


def test_failed_counts_logical_messages_not_chunks():
    payload = _with_recent(outbound_text={"failed": 5, "failed_messages": 2, "last_failed_at": "2026-11-22T21:30:00Z"})
    assert summarize(payload)["text"] == "Connected · 2 messages not delivered"


def test_old_problems_outside_the_window_are_history_not_attention():
    # Lifetime totals are never attention by themselves; only the store's recent window is.
    payload = _quiet(inbox_failed=32, outbox_failed=5, outbox_uncertain=3, mutations_uncertain=3)
    payload["recent_problems"]["outbound_text"]["last_failed_at"] = "2026-10-01T00:00:00Z"
    assert summarize(payload) == {"text": "Connected · nothing needs attention", "tone": "ok", "attention": []}


def test_held_reason_is_read_only_from_a_hold_note():
    held = dict(outbox_held=2, outbound_text_chunks={"held": 1}, outbound_messages={"held": 1})
    with_reason = attention_items(_quiet(last_delivery_error="Held: Ops room is paused", **held))[0]
    assert with_reason["reason"] == "Ops room is paused"
    assert with_reason["headline"] == "1 message and 1 action held"
    assert with_reason["tone"] == "muted"  # a paused place is someone's choice, never amber
    pending_note = attention_items(_quiet(last_delivery_error="Host turn state: pending", **held))[0]
    assert "reason" not in pending_note
    refused = attention_items(_quiet(last_delivery_error=SKILL_ACCESS_REFUSED, **held))[0]
    assert refused["tone"] == "warn" and refused["reason"] == "Check this skill's access in Skills."
    summary = summarize(_quiet(last_delivery_error="Held: Ops room is paused", **held))
    assert (summary["text"], summary["tone"]) == ("Connected · 1 message and 1 action held", "muted")


def test_the_held_constant_matches_the_adapter():
    from lib.host_adapter import SKILL_ACCESS_REFUSED as ADAPTER_CONSTANT
    assert SKILL_ACCESS_REFUSED == ADAPTER_CONSTANT


def test_capability_and_setting_notes():
    items = attention_items(_quiet(reaction_context_state="update_required", reactions_withheld=19,
                                   new_root_channels_error="not a channel id: general"))
    assert [(item["kind"], item["tone"], item.get("count")) for item in items] == [
        ("reactions", "warn", 19), ("new_root_channels", "warn", None)]
    assert items[1]["error"] == {"text": "not a channel id: general"}


def test_tones_are_the_places_vocabulary():
    assert set(TONE_RANK) == {"muted", "ok", "warn", "error"}


def test_unknown_link_state_says_so_without_inventing_one():
    assert summarize(_quiet(link={}))["text"] == "Status unknown"


def test_clear_runtime_if_equal_never_erases_a_newer_companion_pid(tmp_path):
    store = BridgeStore(tmp_path)
    try:
        store.set_runtime(companion_pid=1111)
        assert store.clear_runtime_if_equal("companion_pid", 2222) is False
        assert store.runtime_value("companion_pid") == 1111
        assert store.clear_runtime_if_equal("companion_pid", 1111) is True
        assert store.runtime_value("companion_pid") is None
        assert store.clear_runtime_if_equal("companion_pid", 1111) is False
    finally:
        store.close()


def test_daemon_exit_clears_only_its_own_pid():
    daemon = (Path(__file__).resolve().parents[1] / "scripts" / "slack_daemon.py").read_text(encoding="utf-8")
    assert 'store.clear_runtime_if_equal("companion_pid", os.getpid())' in daemon
    assert "set_runtime(companion_pid=None)" not in daemon


class _Fn:
    def __init__(self, result):
        self.result, self.argtypes, self.restype, self.calls = result, None, None, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result(*args) if callable(self.result) else self.result


def _fake_windows(exit_code: int, *, handle: int = 0x7FFF_0000_1234, last_error: int = 0):
    wintypes = SimpleNamespace(DWORD=type("DWORD", (), {"__init__": lambda self: setattr(self, "value", 0)}),
                               BOOL="BOOL", HANDLE="HANDLE")

    def get_exit(_handle, ref):
        ref.value = exit_code
        return 1

    kernel32 = SimpleNamespace(OpenProcess=_Fn(handle), GetExitCodeProcess=_Fn(get_exit), CloseHandle=_Fn(1))
    ctypes = SimpleNamespace(WinDLL=lambda *_a, **_k: kernel32, POINTER=lambda kind: ("POINTER", kind),
                             byref=lambda value: value, get_last_error=lambda: last_error, wintypes=wintypes)
    return ctypes, wintypes, kernel32


def test_windows_probe_declares_handle_signatures(monkeypatch):
    ctypes, wintypes, kernel32 = _fake_windows(259)
    store_mod._windows_kernel32(ctypes, wintypes)
    assert kernel32.OpenProcess.argtypes == (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    assert kernel32.OpenProcess.restype == wintypes.HANDLE
    assert kernel32.GetExitCodeProcess.argtypes == (wintypes.HANDLE, ("POINTER", wintypes.DWORD))
    assert kernel32.GetExitCodeProcess.restype == wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == (wintypes.HANDLE,)
    assert kernel32.CloseHandle.restype == wintypes.BOOL


@pytest.mark.parametrize(("exit_code", "alive"), [(259, True), (0, False)])
def test_windows_probe_reads_exit_code_without_signal_zero(monkeypatch, exit_code, alive):
    ctypes, wintypes, kernel32 = _fake_windows(exit_code)
    monkeypatch.setitem(__import__("sys").modules, "ctypes", ctypes)
    monkeypatch.setitem(__import__("sys").modules, "ctypes.wintypes", wintypes)
    monkeypatch.setattr(store_mod, "_IS_WINDOWS", True)
    monkeypatch.setattr(store_mod.os, "kill", lambda *_a: pytest.fail("os.kill must not run on Windows"))
    assert store_mod._pid_is_alive(4242) is alive
    assert kernel32.OpenProcess.calls == [(0x1000, False, 4242)]
    assert kernel32.CloseHandle.calls == [(0x7FFF_0000_1234,)]


def test_windows_probe_counts_access_denied_as_alive(monkeypatch):
    ctypes, wintypes, kernel32 = _fake_windows(0, handle=0, last_error=5)
    monkeypatch.setitem(__import__("sys").modules, "ctypes", ctypes)
    monkeypatch.setitem(__import__("sys").modules, "ctypes.wintypes", wintypes)
    monkeypatch.setattr(store_mod, "_IS_WINDOWS", True)
    assert store_mod._pid_is_alive(4242) is True
    assert kernel32.CloseHandle.calls == []


def test_posix_probe_still_sees_this_process():
    assert store_mod._pid_is_alive(os.getpid()) is True
    assert store_mod._pid_is_alive(True) is False
