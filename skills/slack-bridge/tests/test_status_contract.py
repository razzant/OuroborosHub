"""Additive status-route contract: observed_at, honest link state, recent problems, structured last_error, and units."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import HostTurnStatus
from lib.runtime import InboundWorker, OutboundWorker
from lib.slack_api import chunk_message
from lib.store import (
    LINK_DETAILS,
    LINK_STATES,
    OUTBOX_CHUNK_MAX_CHARS,
    RECENT_PROBLEM_WINDOW_SECONDS,
    BridgeStore,
    _iso_z,
)
from test_workers_plugin import _Api, _Slack, _load_plugin


class _TokensApi(_Api):
    def __init__(self, state_dir: Path, *, bot_token: str = "xoxb-synth", app_token: str = "xapp-synth") -> None:
        super().__init__(state_dir)
        self._tokens = {"SLACK_BOT_TOKEN": bot_token, "SLACK_APP_TOKEN": app_token}

    def get_settings(self, keys):
        return {key: self._tokens.get(key, "") for key in keys}


def _read_status_route(api: _Api) -> dict:
    module = _load_plugin()
    module.register(api)
    handler, methods = api.routes["status"]
    assert methods == ("GET",)
    response = asyncio.run(handler())
    assert response.status_code == 200
    return json.loads(response.body)


def _envelope(index: int, *, channel: str = "C09SYNTH01", text: str = "synthetic message") -> dict:
    return {
        "type": "events_api",
        "envelope_id": f"env-synth-{index}",
        "payload": {
            "event_id": f"EvSYNTH{index}",
            "team_id": "T09SYNTH01",
            "event": {
                "type": "message",
                "user": "U09SYNTH01",
                "channel": channel,
                "channel_type": "channel",
                "ts": f"{1700000000 + index}.000100",
                "event_ts": f"{1700000000 + index}.000100",
                "text": text,
            },
        },
    }


def _parse_iso_z(value: str) -> float:
    assert isinstance(value, str) and value.endswith("Z"), f"expected UTC ISO 8601 with Z, got {value!r}"
    assert "+00:00" not in value
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    assert parsed.tzinfo == timezone.utc
    return parsed.timestamp()


def _set_row_time(db_path: Path, table: str, where: str, params: tuple, updated_at: float) -> None:
    with sqlite3.connect(db_path) as conn:
        with conn:
            conn.execute(f"UPDATE {table} SET updated_at=? WHERE {where}", (updated_at, *params))


def test_observed_at_is_utc_iso8601_with_z_and_all_existing_keys_are_preserved(tmp_path, monkeypatch) -> None:
    import lib.store as store_mod

    fixed_now = 1795384860.25
    monkeypatch.setattr(store_mod.time, "time", lambda: fixed_now)

    store = BridgeStore(tmp_path)
    store.set_runtime(
        socket_state="connected",
        host_adapter_state="active",
        reaction_context_state="available",
        workspace_name="Northstar Labs",
        workspace_id="T09NSLABS01",
        bot_user_id="U09OUROBOT1",
        connected_at=fixed_now - 600.0,
        companion_pid=os.getpid(),
        presence_identity=1,
        connector_identity={"sends_as": {"id": "U09OUROBOT1", "label": "ouroboros-bot", "kind": "bot"}},
    )
    store.close()

    binding_id = "9f4c2a7e1b8053d6c4e2a9187b06f5d3"
    (tmp_path / "settings.json").write_text(json.dumps({"binding_id": binding_id}), encoding="utf-8")

    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["observed_at"] == "2026-11-22T22:01:00.250Z"
    assert _parse_iso_z(payload["observed_at"]) == pytest.approx(fixed_now)

    # Places & existing consumers rely on exact names, types, and values of pre-existing keys.
    # The Places line is the card's first line: the same words, the worst current tone (test_status_summary.py).
    assert payload["text"] == "Connected · nothing needs attention"
    assert payload["tone"] == "ok"
    assert payload["attention"] == []
    assert payload["title"] == "Slack transport"
    assert payload["workspace_name"] == "Northstar Labs"
    assert payload["workspace_id"] == "T09NSLABS01"
    assert payload["bot_user_id"] == "U09OUROBOT1"
    assert payload["identity"] == {"sends_as": {"id": "U09OUROBOT1", "label": "ouroboros-bot", "kind": "bot"}}
    assert "connector_identity" not in payload
    assert payload["socket_state"] == "connected"
    assert payload["host_adapter_state"] == "active"
    assert payload["reaction_context_state"] == "available"
    assert payload["has_bot_token"] is True
    assert payload["has_app_token"] is True
    assert payload["has_presence_binding"] is True
    assert payload["binding_state"] == "configured"
    assert payload["local_settings_error"] == ""


def test_fresh_install_edge_reports_not_configured_zero_recent_and_null_last_error(tmp_path) -> None:
    payload = _read_status_route(_TokensApi(tmp_path, bot_token="", app_token=""))

    assert payload["socket_state"] == "stopped"
    assert payload["host_adapter_state"] == "missing_binding_id"
    assert payload["workspace_name"] == ""
    assert payload["text"] == "Not set up · Slack tokens are missing"
    assert payload["tone"] == "warn"
    assert _parse_iso_z(payload["observed_at"]) > 0

    assert payload["link"] == {
        "state": "not_configured",
        "detail": "missing_tokens",
        "companion_alive": False,
        "since": None,
        "error": "",
    }
    assert payload["last_error"] is None
    assert payload["last_delivery_error"] == ""
    assert payload["recent_problems"] == {
        "window_seconds": RECENT_PROBLEM_WINDOW_SECONDS,
        "inbound": {
            "failed": 0,
            "uncertain": 0,
            "last_failed_at": None,
            "last_uncertain_at": None,
            "last_error": None,
        },
        "outbound_text": {
            "failed": 0,
            "uncertain": 0,
            "failed_messages": 0,
            "uncertain_messages": 0,
            "failed_messages_partly_delivered": 0,
            "uncertain_messages_partly_delivered": 0,
            "last_failed_at": None,
            "last_uncertain_at": None,
            "last_error": None,
        },
        "mutations": {
            "failed": 0,
            "uncertain": 0,
            "last_failed_at": None,
            "last_uncertain_at": None,
            "last_error": None,
        },
    }
    assert payload["outbound_text_chunks"] == {
        "waiting": 0,
        "held": 0,
        "leased": 0,
        "delivered": 0,
        "failed": 0,
        "uncertain": 0,
    }
    assert payload["outbound_messages"] == {
        "waiting": 0,
        "held": 0,
        "leased": 0,
        "delivered": 0,
        "failed": 0,
        "uncertain": 0,
    }

    # Once tokens are granted before the companion has run, link moves from not_configured to disconnected/not_started.
    with_tokens = _read_status_route(_TokensApi(tmp_path))
    assert with_tokens["link"] == {
        "state": "disconnected",
        "detail": "not_started",
        "companion_alive": False,
        "since": None,
        "error": "",
    }


def test_dead_companion_edge_never_reports_connected_starting_or_reconnecting(tmp_path) -> None:
    # Spawn and reap a real short-lived subprocess so dead_pid is a provably exited OS PID.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait(timeout=10.0) == 0
    dead_pid = child.pid

    store = BridgeStore(tmp_path)
    for stale_state, last_err in (
        ("connected", ""),
        ("starting", ""),
        ("authorized", ""),
        ("connecting", ""),
        ("reconnecting", "refresh_requested"),
    ):
        store.set_runtime(
            socket_state=stale_state,
            host_adapter_state="active",
            connected_at=1795381200.0,
            last_socket_error=last_err,
            companion_pid=dead_pid,
        )
        store.close()
        payload = _read_status_route(_TokensApi(tmp_path))
        # Raw socket_state stays untouched for compatibility, while link tells the truth.
        assert payload["socket_state"] == stale_state
        assert payload["link"] == {
            "state": "disconnected",
            "detail": "companion_not_running",
            "companion_alive": False,
            "since": None,
            "error": last_err,
        }


def test_daemon_clears_companion_pid_on_clean_and_error_exit(tmp_path, monkeypatch) -> None:
    daemon_path = Path(__file__).resolve().parents[1] / "scripts" / "slack_daemon.py"
    spec = importlib.util.spec_from_file_location("slack_daemon_pid_test", daemon_path)
    assert spec is not None and spec.loader is not None
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)

    class _FakeSlack:
        def __init__(self, *_args):
            pass

        async def auth_test(self):
            return {"user_id": "U09BOT", "team_id": "T09TEAM", "team": "Northstar Labs"}

        async def aclose(self):
            pass

    class _FakeRuntime:
        def __init__(self, *, store, **_kwargs):
            self.store = store
            assert store.runtime_value("companion_pid") == os.getpid()

        async def run(self):
            self.store.set_runtime(socket_state="connected", host_adapter_state="active")

        async def close(self):
            self.store.set_runtime(socket_state="disconnected")

    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "SlackClient", _FakeSlack)
    monkeypatch.setattr(daemon, "BridgeRuntime", _FakeRuntime)
    monkeypatch.setattr(daemon, "create_host_adapter", lambda *_a, **_k: object())

    async def run_clean():
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_a: None)
        await daemon._run()

    asyncio.run(run_clean())
    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["companion_pid"] is None
    assert payload["link"] == {
        "state": "disconnected",
        "detail": "disconnected",
        "companion_alive": False,
        "since": None,
        "error": "",
    }


def test_link_enum_covers_live_connected_adapter_reconnecting_settings_and_token_states(tmp_path) -> None:
    connected_epoch = 1795381200.0
    store = BridgeStore(tmp_path)
    store.set_runtime(
        socket_state="connected",
        host_adapter_state="active",
        connected_at=connected_epoch,
        companion_pid=os.getpid(),
        last_socket_error="",
    )
    store.close()

    ok_link = _read_status_route(_TokensApi(tmp_path))["link"]
    assert ok_link == {
        "state": "connected",
        "detail": "ok",
        "companion_alive": True,
        "since": _iso_z(connected_epoch),
        "error": "",
    }
    assert ok_link["state"] in LINK_STATES and ok_link["detail"] in LINK_DETAILS

    # Host adapter inactive while Socket Mode is connected.
    store = BridgeStore(tmp_path)
    store.set_runtime(socket_state="connected", host_adapter_state="missing_binding_id")
    store.close()
    inactive_link = _read_status_route(_TokensApi(tmp_path))["link"]
    assert inactive_link == {
        "state": "connected",
        "detail": "host_adapter_inactive",
        "companion_alive": True,
        "since": _iso_z(connected_epoch),
        "error": "",
    }

    # Reconnecting states do not reuse historical connected_at as 'since'.
    for raw_err, expected_detail in (
        ("refresh_requested", "refresh_requested"),
        ("connection_closed:1006", "connection_closed"),
        (" custom_reason ", "reconnecting"),
    ):
        store = BridgeStore(tmp_path)
        store.set_runtime(socket_state="reconnecting", last_socket_error=raw_err, companion_pid=os.getpid())
        store.close()
        rec_link = _read_status_route(_TokensApi(tmp_path))["link"]
        assert rec_link == {
            "state": "reconnecting",
            "detail": expected_detail,
            "companion_alive": True,
            "since": None,
            "error": raw_err,
        }
        assert rec_link["state"] in LINK_STATES and rec_link["detail"] in LINK_DETAILS

    # Starting states.
    for phase in ("starting", "authorized", "connecting"):
        store = BridgeStore(tmp_path)
        store.set_runtime(socket_state=phase, last_socket_error="", companion_pid=os.getpid())
        store.close()
        start_link = _read_status_route(_TokensApi(tmp_path))["link"]
        assert start_link == {
            "state": "starting",
            "detail": phase,
            "companion_alive": True,
            "since": None,
            "error": "",
        }

    # Error states (live companion backoff vs exited companion).
    store = BridgeStore(tmp_path)
    store.set_runtime(socket_state="error", last_socket_error="invalid_auth", companion_pid=os.getpid())
    store.close()
    assert _read_status_route(_TokensApi(tmp_path))["link"] == {
        "state": "error",
        "detail": "socket_error",
        "companion_alive": True,
        "since": None,
        "error": "invalid_auth",
    }
    store = BridgeStore(tmp_path)
    store.set_runtime(socket_state="error", last_socket_error="invalid_auth", companion_pid=None)
    store.close()
    assert _read_status_route(_TokensApi(tmp_path))["link"] == {
        "state": "error",
        "detail": "companion_exited_error",
        "companion_alive": False,
        "since": None,
        "error": "invalid_auth",
    }

    # Partial token configuration when companion is not running.
    assert _read_status_route(_TokensApi(tmp_path, bot_token="", app_token="xapp-only"))["link"] == {
        "state": "not_configured",
        "detail": "missing_bot_token",
        "companion_alive": False,
        "since": None,
        "error": "invalid_auth",
    }
    assert _read_status_route(_TokensApi(tmp_path, bot_token="xoxb-only", app_token=""))["link"] == {
        "state": "not_configured",
        "detail": "missing_app_token",
        "companion_alive": False,
        "since": None,
        "error": "invalid_auth",
    }

    # Invalid binding_id and unreadable settings.json outrank token checks.
    (tmp_path / "settings.json").write_text('{"binding_id": "not-32-hex"}', encoding="utf-8")
    assert _read_status_route(_TokensApi(tmp_path))["link"]["detail"] == "invalid_binding_id"
    (tmp_path / "settings.json").write_bytes(b"{corrupt json")
    unreadable = _read_status_route(_TokensApi(tmp_path, bot_token="", app_token=""))["link"]
    assert unreadable["state"] == "error"
    assert unreadable["detail"] == "unreadable_settings"
    assert unreadable["error"]


def test_recent_problems_and_old_failure_outside_window_edge(tmp_path) -> None:
    now = 1795384800.0
    old_ts = now - RECENT_PROBLEM_WINDOW_SECONDS - 1.0  # 1s outside the 24h window
    ancient_ts = now - 7 * RECENT_PROBLEM_WINDOW_SECONDS

    store = BridgeStore(tmp_path)

    # 1. Seed old failures/uncertainties strictly outside the 24h window across all three buckets.
    raw_in_old = _envelope(1)
    store.ingest_envelope(raw_in_old, parse_socket_envelope(raw_in_old, bot_user_id="U_BOT"))
    in_old = store.claim_inbox()
    assert in_old is not None
    store.fail_inbox(in_old.row_id, in_old.lease_token, "old inbound turn failure")
    _set_row_time(store.path, "inbox", "id=?", (in_old.row_id,), ancient_ts)

    store.enqueue_outbox(request_id="old-text-fail", target="C1", thread_ts="", chunks=("old chunk 1", "old chunk 2"))
    c1 = store.claim_outbox()
    assert c1 is not None
    store.fail_outbox(c1.row_id, c1.lease_token, "channel_not_found", state="failed")
    c2 = store.claim_outbox()
    assert c2 is not None
    store.fail_outbox(c2.row_id, c2.lease_token, "channel_not_found", state="failed")
    _set_row_time(store.path, "outbox", "request_id=?", ("old-text-fail",), old_ts)

    store.enqueue_mutation(request_id="old-mut-unc", operation="update_message", payload={"channel": "C1", "ts": "1.0"})
    m_old = store.claim_outbox()
    assert m_old is not None
    store.fail_outbox(m_old.row_id, m_old.lease_token, "timeout_after_send", state="uncertain")
    _set_row_time(store.path, "outbox", "request_id=?", ("old-mut-unc",), old_ts - 10.0)

    old_only = store.status(now=now, pid_alive=lambda _pid: False)
    # All-time counters stay unchanged (> 0).
    assert old_only["inbox_failed"] == 1
    assert old_only["outbox_failed"] == 2
    assert old_only["outbox_uncertain"] == 1
    assert old_only["mutations_uncertain"] == 1
    # Recent window counts are all 0 because every problem is older than 24h, while last_*_at keeps the stored timestamps.
    assert old_only["recent_problems"] == {
        "window_seconds": RECENT_PROBLEM_WINDOW_SECONDS,
        "inbound": {
            "failed": 0,
            "uncertain": 0,
            "last_failed_at": _iso_z(ancient_ts),
            "last_uncertain_at": None,
            "last_error": {"at": _iso_z(ancient_ts), "state": "failed", "text": "old inbound turn failure"},
        },
        "outbound_text": {
            "failed": 0,
            "uncertain": 0,
            "failed_messages": 0,
            "uncertain_messages": 0,
            "failed_messages_partly_delivered": 0,
            "uncertain_messages_partly_delivered": 0,
            "last_failed_at": _iso_z(old_ts),
            "last_uncertain_at": None,
            "last_error": {"at": _iso_z(old_ts), "state": "failed", "text": "channel_not_found"},
        },
        "mutations": {
            "failed": 0,
            "uncertain": 0,
            "last_failed_at": None,
            "last_uncertain_at": _iso_z(old_ts - 10.0),
            "last_error": {
                "at": _iso_z(old_ts - 10.0),
                "state": "uncertain",
                "operation": "update_message",
                "text": "timeout_after_send",
            },
        },
    }
    assert old_only["last_error"] == {
        "at": _iso_z(old_ts),
        "direction": "outbound",
        "kind": "text",
        "operation": "",
        "state": "failed",
        "text": "channel_not_found",
    }

    # 2. Now add recent problems inside [now - 86400, now] and verify counts + updated timestamps.
    raw_in_recent = _envelope(2)
    store.ingest_envelope(raw_in_recent, parse_socket_envelope(raw_in_recent, bot_user_id="U_BOT"))
    in_rec = store.claim_inbox()
    assert in_rec is not None
    store.fail_inbox(in_rec.row_id, in_rec.lease_token, "recent host 502")
    _set_row_time(store.path, "inbox", "id=?", (in_rec.row_id,), now - 120.0)

    store.enqueue_outbox(request_id="rec-text-unc", target="C2", thread_ts="", chunks=("uncertain part",))
    t_unc = store.claim_outbox()
    assert t_unc is not None
    store.fail_outbox(t_unc.row_id, t_unc.lease_token, "lost_post_response", state="uncertain")
    _set_row_time(store.path, "outbox", "request_id=?", ("rec-text-unc",), now - 60.0)

    store.enqueue_mutation(request_id="rec-mut-fail", operation="upload_file", payload={"channel": "C2", "path": "/tmp/x"})
    m_fail = store.claim_outbox()
    assert m_fail is not None
    store.fail_outbox(m_fail.row_id, m_fail.lease_token, "missing_scope", state="failed")
    _set_row_time(store.path, "outbox", "request_id=?", ("rec-mut-fail",), now - 15.0)

    mixed = store.status(now=now, pid_alive=lambda _pid: True)
    assert mixed["inbox_failed"] == 2
    assert mixed["outbox_failed"] == 3
    assert mixed["outbox_uncertain"] == 2
    assert mixed["mutations_failed"] == 1
    assert mixed["mutations_uncertain"] == 1

    assert mixed["recent_problems"]["inbound"]["failed"] == 1
    assert mixed["recent_problems"]["inbound"]["last_failed_at"] == _iso_z(now - 120.0)
    assert mixed["recent_problems"]["outbound_text"]["failed"] == 0
    assert mixed["recent_problems"]["outbound_text"]["uncertain"] == 1
    assert mixed["recent_problems"]["outbound_text"]["uncertain_messages"] == 1
    assert mixed["recent_problems"]["outbound_text"]["last_uncertain_at"] == _iso_z(now - 60.0)
    assert mixed["recent_problems"]["mutations"]["failed"] == 1
    assert mixed["recent_problems"]["mutations"]["uncertain"] == 0
    assert mixed["recent_problems"]["mutations"]["last_failed_at"] == _iso_z(now - 15.0)
    assert mixed["last_error"] == {
        "at": _iso_z(now - 15.0),
        "direction": "outbound",
        "kind": "mutation",
        "operation": "upload_file",
        "state": "failed",
        "text": "missing_scope",
    }
    store.close()


def test_held_only_edge_keeps_last_error_null_and_preserves_last_delivery_error(tmp_path) -> None:
    class _PausedHost:
        async def place_hold(self, _account: str, _conversation: str) -> str:
            return "Held: Northstar Labs is paused"

    store = BridgeStore(tmp_path)
    store.set_runtime(workspace_id="T09NSLABS01", socket_state="connected", host_adapter_state="active", companion_pid=os.getpid(),
                      reaction_context_state="available", history_reporting_state="supported")
    store.enqueue_outbox(request_id="held-msg", target="C09OPS", thread_ts="", chunks=("hold me",))
    store.enqueue_mutation(request_id="held-mut", operation="reaction_add", payload={"channel": "C09OPS", "ts": "1.0", "name": "eyes"})

    worker = OutboundWorker(store, _Slack(), _PausedHost())
    assert asyncio.run(worker.process_once()) is True
    assert asyncio.run(worker.process_once()) is True
    store.close()

    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["outbox_held"] == 2
    assert payload["text"] == "Connected · 1 message and 1 action held"
    assert payload["attention"] == [{"kind": "held", "tone": "muted", "headline": "1 message and 1 action held",
                                     "messages": 1, "actions": 1,
                                     "reasons": [{"reason": "Northstar Labs is paused", "count": 2}],
                                     "reason": "Northstar Labs is paused"}]
    assert payload["held_reasons"] == [{"reason": "Northstar Labs is paused", "note": "Held: Northstar Labs is paused",
                                        "count": 2}]
    assert payload["tone"] == "muted"  # held by a paused place: someone's choice, explained on the card, never amber
    assert payload["last_delivery_error"] == "Held: Northstar Labs is paused"
    assert payload["last_error"] is None
    assert payload["recent_problems"]["outbound_text"]["failed"] == 0
    assert payload["recent_problems"]["mutations"]["failed"] == 0
    assert payload["outbound_text_chunks"]["held"] == 1
    assert payload["outbound_messages"]["held"] == 1


def test_pending_note_pollution_edge_never_pollutes_or_masks_last_error(tmp_path) -> None:
    class _PendingHost:
        available = True

        async def submit(self, _event):
            return "deferred:turn-pending-1"

        async def status(self, _reference):
            return HostTurnStatus("pending")

        async def deliver(self, _reference):
            raise AssertionError("pending turn should not deliver")

    store = BridgeStore(tmp_path)
    raw = _envelope(10)
    store.ingest_envelope(raw, parse_socket_envelope(raw, bot_user_id="U_BOT"))
    inbound = InboundWorker(store, _Slack(), _PendingHost(), staged_root=tmp_path / "staged")
    assert asyncio.run(inbound.process_once()) is True

    # 1. With only "Host turn state: pending" in inbox.last_error, last_error stays None.
    only_pending = store.status()
    assert only_pending["last_delivery_error"] == "Host turn state: pending"
    assert only_pending["last_error"] is None
    assert only_pending["recent_problems"]["inbound"]["last_error"] is None

    # 2. Record a real failure at t=1000, then overwrite last_delivery_error with newer pending & Held notes at t=1050/1060.
    store.enqueue_outbox(request_id="real-fail", target="C09MISSING", thread_ts="", chunks=("important",))
    out_item = store.claim_outbox()
    assert out_item is not None
    store.fail_outbox(out_item.row_id, out_item.lease_token, "not_in_channel", state="failed")
    _set_row_time(store.path, "outbox", "id=?", (out_item.row_id,), 1000.0)

    _set_row_time(store.path, "inbox", "envelope_id=?", ("env-synth-10",), 1050.0)

    store.enqueue_outbox(request_id="later-held", target="C09HELD", thread_ts="", chunks=("held text",))
    held_item = store.claim_outbox()
    assert held_item is not None
    store.hold_outbox(held_item, "Held: Quiet room is silent")
    _set_row_time(store.path, "outbox", "id=?", (held_item.row_id,), 1060.0)

    polluted = store.status(now=1100.0)
    assert polluted["last_delivery_error"] == "Held: Quiet room is silent"
    assert polluted["last_error"] == {
        "at": _iso_z(1000.0),
        "direction": "outbound",
        "kind": "text",
        "operation": "",
        "state": "failed",
        "text": "not_in_channel",
    }
    store.close()


def test_units_and_logical_message_counts_separate_3900_char_chunks_messages_and_mutations(tmp_path) -> None:
    store = BridgeStore(tmp_path)

    # 1. One long 3-chunk message completely delivered (3 chunks -> 1 logical delivered message).
    long_text = "A" * (OUTBOX_CHUNK_MAX_CHARS * 2 + 150)
    three_chunks = chunk_message(long_text)
    assert len(three_chunks) == 3
    store.enqueue_outbox(request_id="msg-delivered-3", target="C01", thread_ts="", chunks=three_chunks)
    for _ in range(3):
        part = store.claim_outbox()
        assert part is not None
        store.complete_outbox(part.row_id, part.lease_token, provider_message_ts="1.1")

    # 2. One 2-chunk message where chunk 0 delivered and chunk 1 failed -> 1 failed logical message (never delivered!).
    store.enqueue_outbox(request_id="msg-partial-fail", target="C02", thread_ts="", chunks=("part 1", "part 2"))
    p0 = store.claim_outbox()
    assert p0 is not None
    store.complete_outbox(p0.row_id, p0.lease_token, provider_message_ts="2.1")
    p1 = store.claim_outbox()
    assert p1 is not None
    store.fail_outbox(p1.row_id, p1.lease_token, "invalid_arguments", state="failed")

    # 3. One 2-chunk message where chunk 0 failed and chunk 1 ended uncertain -> 1 uncertain logical message.
    store.enqueue_outbox(request_id="msg-partial-unc", target="C03", thread_ts="", chunks=("u1", "u2"))
    u0 = store.claim_outbox()
    assert u0 is not None
    store.fail_outbox(u0.row_id, u0.lease_token, "bad_arg", state="failed")
    u1 = store.claim_outbox()
    assert u1 is not None
    store.fail_outbox(u1.row_id, u1.lease_token, "lost_response", state="uncertain")

    # 4. One 2-chunk message waiting in queue, one 2-chunk message held, and one 1-chunk message currently leased.
    store.enqueue_outbox(request_id="msg-waiting-2", target="C04", thread_ts="", chunks=("w1", "w2"))
    store.enqueue_outbox(request_id="msg-held-2", target="C05", thread_ts="", chunks=("h1", "h2"))
    h_claim = store.claim_outbox()  # claims w1 on C04 first unless we claim per lane; claim until msg-held-2
    assert h_claim is not None and h_claim.request_id == "msg-waiting-2"
    # Return w1 to unheld pending retry so msg-waiting-2 stays waiting.
    store.retry_outbox(h_claim.row_id, h_claim.lease_token, "ratelimited", delay_seconds=30.0)
    held_claim = store.claim_outbox()
    assert held_claim is not None and held_claim.request_id == "msg-held-2"
    store.hold_outbox(held_claim, "Held: C05 is paused")

    store.enqueue_outbox(request_id="msg-leased-1", target="C06", thread_ts="", chunks=("in flight",))
    leased_claim = store.claim_outbox()
    assert leased_claim is not None and leased_claim.request_id == "msg-leased-1"

    # 5. Three mutations (1 delivered, 1 failed, 1 uncertain).
    store.enqueue_mutation(request_id="mut-ok", operation="pin_add", payload={"channel": "C07", "ts": "1.0"})
    mo = store.claim_outbox()
    assert mo is not None
    store.complete_outbox(mo.row_id, mo.lease_token)

    store.enqueue_mutation(request_id="mut-fail", operation="join_conversation", payload={"channel_id": "C08"})
    mf = store.claim_outbox()
    assert mf is not None
    store.fail_outbox(mf.row_id, mf.lease_token, "is_archived", state="failed")

    store.enqueue_mutation(request_id="mut-unc", operation="delete_message", payload={"channel": "C09", "ts": "1.0"})
    mu = store.claim_outbox()
    assert mu is not None
    store.fail_outbox(mu.row_id, mu.lease_token, "timeout", state="uncertain")

    status = store.status()
    store.close()

    # Legacy outbox_* counters count chunks + mutations (unchanged).
    assert status["outbox_delivered"] == 4 + 1  # 4 text chunks + 1 mutation
    assert status["outbox_failed"] == 2 + 1     # 2 text chunks + 1 mutation
    assert status["outbox_uncertain"] == 1 + 1  # 1 text chunk + 1 mutation
    assert status["outbox_pending"] == 4        # 2 waiting chunks + 2 chunks of msg-held-2 (1 marked Held:, 1 queued behind it)
    assert status["outbox_held"] == 1
    assert status["outbox_leased"] == 1

    # Explicit chunk-only counters (excluding mutations).
    assert status["outbound_text_chunks"] == {
        "waiting": 3,
        "held": 1,
        "leased": 1,
        "delivered": 4,
        "failed": 2,
        "uncertain": 1,
    }

    # Disjoint logical text message counts (6 distinct request_ids).
    assert status["outbound_messages"] == {
        "waiting": 1,
        "held": 1,
        "leased": 1,
        "delivered": 1,
        "failed": 1,
        "uncertain": 1,
    }

    units = status["units"]
    assert units["inbox"] == "envelopes"
    assert units["outbox"] == "text_chunks_3900_chars_plus_mutations"
    assert units["outbox_chunk_max_chars"] == 3900
    assert units["outbound_text_chunks"] == "text_chunks_3900_chars"
    assert units["outbound_messages"] == "logical_text_requests"
    assert units["mutations"] == "outbox_subset_operations"
    assert "3,900-char" in units["note"] and "request_ids" in units["note"]
