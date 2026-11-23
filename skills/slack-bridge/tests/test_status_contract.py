"""Additive status-route contract: observed_at, honest link state, recent problems, structured last_error, and units."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
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
            "last_failed_error": None,
            "last_uncertain_error": None,
        },
        "outbound_text": {
            "failed": 0,
            "uncertain": 0,
            "failed_messages": 0,
            "uncertain_messages": 0,
            "failed_messages_partly_delivered": 0,
            "uncertain_messages_partly_delivered": 0,
            "failed_messages_still_queued": 0,
            "uncertain_messages_still_queued": 0,
            "failed_messages_others_delivered_or_unknown": 0,
            "failed_messages_every_part_refused": 0,
            "uncertain_messages_partly_delivered_none_queued": 0,
            "failed_messages_in_parts": 0,
            "uncertain_messages_in_parts": 0,
            "uncertain_messages_with_refused_part": 0,
            "last_failed_at": None,
            "last_uncertain_at": None,
            "last_error": None,
            "last_failed_error": None,
            "last_uncertain_error": None,
        },
        "mutations": {
            "failed": 0,
            "uncertain": 0,
            "last_failed_at": None,
            "last_uncertain_at": None,
            "last_error": None,
            "last_failed_error": None,
            "last_uncertain_error": None,
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
        # The route's first line and tone for a dead companion (also the shape every path must give).
        assert (payload["text"], payload["tone"]) == ("Disconnected · the bridge process is not running", "error")


def _load_daemon():
    daemon_path = Path(__file__).resolve().parents[1] / "scripts" / "slack_daemon.py"
    spec = importlib.util.spec_from_file_location("slack_daemon_pid_test", daemon_path)
    assert spec is not None and spec.loader is not None
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)
    return daemon


class _SignedInSlack:
    """auth.test answers; opening Socket Mode answers ``socket_error`` when one is set."""

    socket_error = ""

    def __init__(self, *_args):
        pass

    async def auth_test(self):
        return {"user_id": "U09BOT", "bot_id": "B09BOT", "team_id": "T09TEAM", "team": "Northstar Labs"}

    async def open_socket_url(self):
        from lib.slack_api import SlackApiError
        raise SlackApiError(self.socket_error)

    async def aclose(self):
        pass


def _run_daemon(daemon, monkeypatch):
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_a: None)
        await daemon._run()

    asyncio.run(run())


def test_daemon_clean_exit_ends_disconnected_and_keeps_a_successors_pid(tmp_path, monkeypatch) -> None:
    daemon = _load_daemon()
    successor = os.getpid() + 1

    class _FakeRuntime:
        def __init__(self, *, store, **_kwargs):
            self.store = store
            assert store.runtime_value("companion_pid") == os.getpid()

        async def run(self):
            self.store.set_runtime(socket_state="connected", host_adapter_state="active")
            # A newer companion started and wrote its PID before this one finished.
            self.store.set_runtime(companion_pid=successor)

        async def close(self):
            self.store.set_runtime(socket_state="disconnected")

    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "SlackClient", _SignedInSlack)
    monkeypatch.setattr(daemon, "BridgeRuntime", _FakeRuntime)
    monkeypatch.setattr(daemon, "create_host_adapter", lambda *_a, **_k: object())
    _run_daemon(daemon, monkeypatch)
    store = BridgeStore(tmp_path)
    try:
        assert store.runtime_value("companion_pid") == successor  # never erased by the exiting one
        assert store.runtime_value("socket_state") == "disconnected"
        store.set_runtime(companion_pid=None)
    finally:
        store.close()
    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["link"] == {"state": "disconnected", "detail": "disconnected", "companion_alive": False,
                               "since": None, "error": ""}


def test_daemon_error_exit_clears_its_own_pid_and_keeps_the_error(tmp_path, monkeypatch) -> None:
    daemon = _load_daemon()

    class _FailingRuntime:
        def __init__(self, *, store, **_kwargs):
            self.store = store

        async def run(self):
            raise OSError("disk full")

        async def close(self):
            self.store.set_runtime(socket_state="disconnected")

    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "SlackClient", _SignedInSlack)
    monkeypatch.setattr(daemon, "BridgeRuntime", _FailingRuntime)
    monkeypatch.setattr(daemon, "create_host_adapter", lambda *_a, **_k: object())
    with pytest.raises(OSError):
        _run_daemon(daemon, monkeypatch)
    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["companion_pid"] is None
    assert payload["link"] == {"state": "error", "detail": "companion_exited_error", "companion_alive": False,
                               "since": None, "error": "disk full"}
    assert payload["text"] == "Connection error · the bridge stopped after an error"
    assert payload["attention"][0]["auth"] is False


@pytest.mark.parametrize("code", ["invalid_auth", "missing_scope"])
def test_app_token_refused_after_sign_in_reaches_the_token_advice(tmp_path, monkeypatch, code) -> None:
    """The real daemon and BridgeRuntime: auth.test succeeds, then Slack refuses to open Socket Mode."""
    daemon = _load_daemon()

    class _Host:
        available = False
        delivery_reporting_status = "unsupported"
        reaction_context = 1  # a current Host: the only item left is the refusal itself
        explicit_completion = 1

    slack = type("RefusingSlack", (_SignedInSlack,), {"socket_error": code})
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "SlackClient", slack)
    monkeypatch.setattr(daemon, "create_host_adapter", lambda *_a, **_k: _Host())
    from lib.slack_api import SlackApiError
    with pytest.raises(SlackApiError):
        _run_daemon(daemon, monkeypatch)
    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["companion_pid"] is None
    assert payload["socket_state"] == "error"
    assert payload["link"] == {"state": "error", "detail": "companion_exited_error", "companion_alive": False,
                               "since": None, "error": f"Slack API error: {code}"}
    assert payload["text"] == "Connection error · Slack refused the bridge's tokens"
    assert payload["tone"] == "error"
    item = payload["attention"][0]
    assert (item["kind"], item["auth"], item["error"]) == ("link", True, {"text": f"Slack API error: {code}"})


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
            # Outside the window: no error of either state is attached to an item.
            "last_failed_error": None,
            "last_uncertain_error": None,
        },
        "outbound_text": {
            "failed": 0,
            "uncertain": 0,
            "failed_messages": 0,
            "uncertain_messages": 0,
            "failed_messages_partly_delivered": 0,
            "uncertain_messages_partly_delivered": 0,
            "failed_messages_still_queued": 0,
            "uncertain_messages_still_queued": 0,
            "failed_messages_others_delivered_or_unknown": 0,
            "failed_messages_every_part_refused": 0,
            "uncertain_messages_partly_delivered_none_queued": 0,
            "failed_messages_in_parts": 0,
            "uncertain_messages_in_parts": 0,
            "uncertain_messages_with_refused_part": 0,
            "last_failed_at": _iso_z(old_ts),
            "last_uncertain_at": None,
            "last_error": {"at": _iso_z(old_ts), "state": "failed", "text": "channel_not_found"},
            "last_failed_error": None,
            "last_uncertain_error": None,
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
            "last_failed_error": None,
            "last_uncertain_error": None,
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
                                     "messages": 1, "actions": 1, "with_problem_parts": 0,
                                     "with_recent_problem_parts": 0,
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


# ---------- one read, queue rows, holds and parts (moved from the review-wave module) ----------

def _quiet_route_payload() -> dict:
    return {"link": {"state": "connected", "detail": "ok", "companion_alive": True, "since": None, "error": ""},
            "reaction_context_state": "available", "history_reporting_state": "supported"}


def test_status_reads_every_counter_in_one_transaction(tmp_path) -> None:
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
        # The outgoing text rows are grouped by message once, not once per counter.
        assert sum("GROUP BY request_id" in line for line in seen) == 1
    finally:
        store.close()


def test_events_the_host_refused_for_now_are_counted_from_their_rows_and_accepted_ones_are_not(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    try:
        for index in (1, 2, 3, 4):
            raw = _envelope(index, channel=f"C09SYNTH0{index}")
            store.ingest_envelope(raw, parse_socket_envelope(raw, bot_user_id="U_BOT"))
        first, second, accepted = store.claim_inbox(), store.claim_inbox(), store.claim_inbox()
        # A refusal the worker will retry, and a turn that is simply running (its routine note).
        store.retry_inbox(first.row_id, first.lease_token, "HostRetryError: capacity full", delay_seconds=30.0)
        store.retry_inbox(second.row_id, second.lease_token, "Host turn state: pending", delay_seconds=5.0)
        # Ouroboros accepted this one (its reference is stored); only reading its turn's status failed.
        store.set_host_reference(accepted.row_id, accepted.lease_token, "deferred:turn-synth-1")
        store.retry_inbox(accepted.row_id, accepted.lease_token, "status poll: ReadTimeout", delay_seconds=5.0)
        status = store.status()
        assert status["inbox_retrying"]["count"] == 1  # the fourth row (attempts=0) is an ordinary wait
        assert status["inbox_retrying"]["last_error"]["text"] == "HostRetryError: capacity full"
        assert status["inbox_retrying"]["last_error"]["at"].endswith("Z")
    finally:
        store.close()


def test_every_hold_reason_is_read_with_its_count(tmp_path) -> None:
    from lib.status_summary import SKILL_ACCESS_REFUSED
    store = BridgeStore(tmp_path)
    try:
        for index in range(4):
            store.enqueue_outbox(request_id=f"held-{index}", target=f"C09HOLD{index}", thread_ts="", chunks=("hold",))
        for index in range(4):
            item = store.claim_outbox()
            store.hold_outbox(item, SKILL_ACCESS_REFUSED if index == 0 else "Held: Ops room is paused")
        assert store.status()["held_reasons"] == [
            {"reason": "Ops room is paused", "note": "Held: Ops room is paused", "count": 3},
            {"reason": "Check this skill's access in Skills.", "note": SKILL_ACCESS_REFUSED, "count": 1},
        ]
    finally:
        store.close()


def test_partly_delivered_messages_are_counted(tmp_path) -> None:
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


def test_a_problem_part_counts_while_another_part_waits_or_is_held(tmp_path) -> None:
    from lib.status_summary import summarize
    store = BridgeStore(tmp_path)
    try:
        store.enqueue_outbox(request_id="mixed", target="C09MIX", thread_ts="", chunks=("one", "two", "three"))
        first = store.claim_outbox()
        store.complete_outbox(first.row_id, first.lease_token, provider_message_ts="1.1")
        second = store.claim_outbox()
        store.fail_outbox(second.row_id, second.lease_token, "ReadTimeout after send", state="uncertain")
        waiting = store.status()
        text = waiting["recent_problems"]["outbound_text"]
        assert (text["uncertain_messages"], text["uncertain_messages_partly_delivered"]) == (1, 1)
        assert waiting["outbound_messages"]["waiting"] == 1  # the queue view still says the third part waits
        payload = {**waiting, **_quiet_route_payload()}
        summary = summarize(payload)
        assert summary["text"] == "Connected · outcome unknown for 1 message" and summary["tone"] == "warn"
        third = store.claim_outbox()
        store.hold_outbox(third, "Held: Ops room is paused")
        held = store.status()
        assert held["recent_problems"]["outbound_text"]["uncertain_messages"] == 1
        assert held["outbound_messages"]["held"] == 1
        payload = {**held, **_quiet_route_payload()}
        kinds = [item["kind"] for item in summarize(payload)["attention"]]
        assert kinds == ["outcome_unknown", "held"]
    finally:
        store.close()


def test_actions_in_progress_are_counted(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    try:
        store.enqueue_mutation(request_id="edit-1", operation="update_message", payload={"channel": "C1", "ts": "1.0"})
        assert store.claim_outbox() is not None
        status = store.status()
        assert status["outbox_leased"] == 1 and status["outbound_text_chunks"]["leased"] == 0
        assert status["mutations_leased"] == 1
    finally:
        store.close()


def test_each_queue_keeps_its_newest_error_per_state_inside_the_window(tmp_path) -> None:
    from lib.status_summary import attention_items
    now = 1795384800.0
    store = BridgeStore(tmp_path)
    try:
        # An upload refused five days ago, then today a text message refused and a later one with an unknown outcome.
        store.enqueue_mutation(request_id="old-upload", operation="upload_file", payload={"channel": "C1", "path": "/x"})
        old = store.claim_outbox()
        store.fail_outbox(old.row_id, old.lease_token, "missing_scope", state="failed")
        _set_row_time(store.path, "outbox", "id=?", (old.row_id,), now - 5 * 86400)
        store.enqueue_outbox(request_id="refused", target="C09A", thread_ts="", chunks=("a",))
        refused = store.claim_outbox()
        store.fail_outbox(refused.row_id, refused.lease_token, "invalid_arguments", state="failed")
        _set_row_time(store.path, "outbox", "id=?", (refused.row_id,), now - 600)
        store.enqueue_outbox(request_id="unknown", target="C09B", thread_ts="", chunks=("b",))
        unknown = store.claim_outbox()
        store.fail_outbox(unknown.row_id, unknown.lease_token, "ReadTimeout", state="uncertain")
        _set_row_time(store.path, "outbox", "id=?", (unknown.row_id,), now - 60)
        status = store.status(now=now, pid_alive=lambda _pid: True)
    finally:
        store.close()
    text, mutations = status["recent_problems"]["outbound_text"], status["recent_problems"]["mutations"]
    assert text["last_error"]["text"] == "ReadTimeout"  # compatibility: the newest of either state
    assert text["last_failed_error"] == {"at": _iso_z(now - 600), "state": "failed", "text": "invalid_arguments"}
    assert text["last_uncertain_error"] == {"at": _iso_z(now - 60), "state": "uncertain", "text": "ReadTimeout"}
    assert mutations["last_failed_error"] is None and mutations["last_error"]["text"] == "missing_scope"
    items = {item["kind"]: item for item in attention_items({**status, **_quiet_route_payload()})}
    assert items["not_delivered"]["error"]["text"] == "invalid_arguments"  # not the old upload's refusal
    assert items["outcome_unknown"]["error"]["text"] == "ReadTimeout"


def test_a_recheck_that_finds_the_host_binding_updates_events_passed_on(tmp_path) -> None:
    from lib.runtime import _discover_reporting

    class _Host:
        available = False

        async def discover_delivery_support(self):
            return 0

    store = BridgeStore(tmp_path)
    try:
        host = _Host()
        store.set_runtime(host_adapter_state="missing_binding_id")  # what run() recorded at start
        asyncio.run(_discover_reporting(host, store))
        assert store.runtime_value("host_adapter_state") == "missing_binding_id"
        host.available = True  # lib/host_adapter.py: a later /identity answer restores the binding
        asyncio.run(_discover_reporting(host, store))
        assert store.runtime_value("host_adapter_state") == "active"
    finally:
        store.close()


def test_a_bridge_that_never_started_has_not_checked_the_host(tmp_path) -> None:
    fresh = _read_status_route(_TokensApi(tmp_path))
    assert fresh["host_adapter_checked"] is False
    assert fresh["host_adapter_state"] == "missing_binding_id"  # the existing key keeps its old default
    store = BridgeStore(tmp_path)
    store.set_runtime(host_adapter_state="missing_binding_id")
    store.close()
    assert _read_status_route(_TokensApi(tmp_path))["host_adapter_checked"] is True


def test_status_route_answers_text_and_tone_on_every_path(tmp_path) -> None:
    """The next shared card accepts a status only with string text and tone; every route path gives both.

    The dead-companion path is asserted in test_dead_companion_edge_never_reports_connected_starting_or_reconnecting.
    """
    seen = {}

    def read(name, api):
        payload = _read_status_route(api)
        assert isinstance(payload["text"], str) and payload["text"], (name, payload.get("text"))
        assert isinstance(payload["tone"], str) and payload["tone"] in {"ok", "muted", "warn", "error"}, name
        seen[name] = (payload["text"], payload["tone"])
        return payload

    read("not configured", _TokensApi(tmp_path / "a", bot_token="", app_token=""))
    read("never started", _TokensApi(tmp_path / "a"))
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "settings.json").write_bytes(b"{corrupt json")
    read("settings unreadable", _TokensApi(tmp_path / "c"))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "slack_bridge.sqlite3").write_bytes(b"this is not a database" * 64)
    unreadable = read("store unreadable", _TokensApi(tmp_path / "d"))
    assert unreadable["link"]["detail"] == "store_unreadable" and unreadable["link"]["error"]
    assert seen == {
        "not configured": ("Not set up · Slack tokens are missing", "warn"),
        "never started": ("Disconnected · the bridge has not started", "error"),
        "settings unreadable": ("Connection error · the bridge settings can't be read", "error"),
        "store unreadable": ("Status unknown · the bridge's queue store can't be read", "error"),
    }


# ---------- round 6: each outcome's own window, mixed parts, unknown liveness, Host recovery ----------

@pytest.mark.parametrize("old_state,new_state,new_error", [
    ("uncertain", "failed", "channel_not_found"),  # unknown two days ago, refused today: today's refusal shows
    ("failed", "uncertain", "ReadTimeout"),  # refused two days ago, unknown today: today's unknown shows
])
def test_each_outcome_of_one_message_is_windowed_by_its_own_time(tmp_path, old_state, new_state, new_error) -> None:
    from lib.status_summary import attention_items
    now = 1795384800.0
    store = BridgeStore(tmp_path)
    try:
        store.enqueue_outbox(request_id="two-days", target="C09A", thread_ts="", chunks=("part one", "part two"))
        first = store.claim_outbox()
        store.fail_outbox(first.row_id, first.lease_token, "old error", state=old_state)
        _set_row_time(store.path, "outbox", "id=?", (first.row_id,), now - 2 * 86400)
        second = store.claim_outbox()
        store.fail_outbox(second.row_id, second.lease_token, new_error, state=new_state)
        _set_row_time(store.path, "outbox", "id=?", (second.row_id,), now - 60)
        status = store.status(now=now, pid_alive=lambda _pid: True)
    finally:
        store.close()
    text = status["recent_problems"]["outbound_text"]
    # The messages agree with the parts of the window.
    assert (text["failed"], text["uncertain"]) == ((1, 0) if new_state == "failed" else (0, 1))
    assert (text["failed_messages"], text["uncertain_messages"]) == ((1, 0) if new_state == "failed" else (0, 1))
    items = attention_items({**status, **_quiet_route_payload()})
    kind = "not_delivered" if new_state == "failed" else "outcome_unknown"
    assert [item["kind"] for item in items] == [kind]
    assert items[0]["error"]["text"] == new_error


def test_a_refused_part_with_another_part_still_queued_is_one_message_with_two_outcomes(tmp_path) -> None:
    from lib.status_summary import attention_items
    store = BridgeStore(tmp_path)
    try:
        store.enqueue_outbox(request_id="mixed", target="C09MIX", thread_ts="", chunks=("part one", "part two"))
        first = store.claim_outbox()
        store.fail_outbox(first.row_id, first.lease_token, "msg_blocked", state="failed")
        second = store.claim_outbox()
        store.hold_outbox(second, "Held: Ops room is paused")
        store.enqueue_outbox(request_id="whole", target="C09ONE", thread_ts="", chunks=("only part",))
        only = store.claim_outbox()
        store.fail_outbox(only.row_id, only.lease_token, "channel_not_found", state="failed")
        status = store.status()
    finally:
        store.close()
    text = status["recent_problems"]["outbound_text"]
    assert (text["failed_messages"], text["failed_messages_still_queued"]) == (2, 1)
    assert status["outbound_messages"]["held"] == 1
    assert status["outbound_messages_with_problem_parts"] == {"waiting": 0, "held": 1, "leased": 0}
    items = {item["kind"]: item for item in attention_items({**status, **_quiet_route_payload()})}
    assert items["not_delivered"]["still_queued"] == 1  # the card advises waiting for that one, not resending it
    assert items["held"]["with_problem_parts"] == 1  # the held message is the same one, and the card says so


def test_an_unanswered_probe_marks_the_link_item_as_its_last_record(tmp_path) -> None:
    from lib.status_summary import attention_items
    store = BridgeStore(tmp_path)
    try:
        store.set_runtime(socket_state="error", last_socket_error="ConnectError: All connection attempts failed",
                          companion_pid=4242, host_adapter_state="active")
        unknown = store.status(pid_alive=lambda _pid: None)
        running = store.status(pid_alive=lambda _pid: True)
    finally:
        store.close()
    assert unknown["link"]["detail"] == "socket_error" and unknown["link"]["companion_alive"] is None
    link_item = attention_items(unknown)[0]
    # The card's retry promise belongs to a process confirmed running; this item says the probe did not answer.
    assert link_item["kind"] == "link" and link_item["process_unknown"] is True
    assert "process_unknown" not in attention_items(running)[0]


def test_an_event_whose_hand_off_reply_was_lost_waits_for_confirmation(tmp_path) -> None:
    from lib.status_summary import summarize
    store = BridgeStore(tmp_path)
    try:
        raw = _envelope(1)
        store.ingest_envelope(raw, parse_socket_envelope(raw, bot_user_id="U_BOT"))
        item = store.claim_inbox()
        # The Host recorded the turn, but the reply to the POST was lost: no reference is stored here.
        store.retry_inbox(item.row_id, item.lease_token, "ReadTimeout: reply lost after Host recorded the turn",
                          delay_seconds=5.0)
        status = store.status()
    finally:
        store.close()
    assert status["inbox_retrying"]["count"] == 1
    summary = summarize({**status, **_quiet_route_payload()})
    assert summary["text"] == "Connected · 1 Slack event waits for Ouroboros to confirm it"
    assert "not taken" not in summary["text"] and "passed to Ouroboros" not in summary["text"]


@pytest.mark.parametrize("path", ["inbound", "outbound"])
def test_a_later_request_that_confirms_the_host_binding_reaches_the_status(tmp_path, path) -> None:
    """After a 503 at start, the next /identity answer from submit or a place check records the binding."""
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import BridgeRuntime, _discover_reporting
    from test_host_adapter import _item

    identity_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity":
            identity_calls.append(request)
            if len(identity_calls) == 1:
                return httpx.Response(503, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "places_version": 1, "presence_delivery_version": 1,
                                             "presence_reaction_context": 1, "presence_explicit_completion": 1})
        if request.url.path == "/presence/place":
            return httpx.Response(200, json={"state": "active", "label": "Ops room"})
        if request.url.path == "/presence/turn":
            return httpx.Response(200, json={"status": "completed", "outcome": "silent", "text": "",
                                             "turn_ref": "turn-synth-1", "work_ref": ""})
        return httpx.Response(404, json={"ok": False})

    async def run() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:8767",
                                              skill_token="skill-token", http_client=http)
        store = BridgeStore(tmp_path)
        try:
            BridgeRuntime(store=store, slack=_Slack(), host=adapter, bot_user_id="U09OUROBOT1")
            # What run() does at start: the 503 leaves the binding unconfirmed.
            store.set_runtime(socket_state="connected", companion_pid=os.getpid())
            await _discover_reporting(adapter, store)
            store.set_runtime(host_adapter_state="active" if adapter.available else "missing_binding_id")
            assert store.status()["link"]["detail"] == "host_adapter_inactive"
            writes = []
            original = store.set_runtime
            store.set_runtime = lambda **values: (writes.append(values), original(**values))[1]
            if path == "inbound":
                await adapter.submit(_item())  # its own discovery confirms the binding, then the turn is taken
            else:
                assert await adapter.place_hold("T1", "C1") == ""  # the outbound worker's check before a send
            store.set_runtime = original
            assert len(writes) == 1 and writes[0]["host_adapter_state"] == "active"  # one write, at the change
            await adapter.place_hold("T1", "C1")  # already known: no second discovery request, no write
            assert len(identity_calls) == 2
        finally:
            store.close()
            await http.aclose()

    asyncio.run(run())
    reads = len(identity_calls)
    payload = _read_status_route(_TokensApi(tmp_path))
    assert len(identity_calls) == reads  # a status read asks nothing of the Host
    assert payload["host_adapter_state"] == "active" and payload["link"]["detail"] == "ok"
    assert payload["text"] == "Connected · nothing needs attention"


# ---------- round 7: credentials in error text, refused messages by their other parts, each item's own error ----------

_ILLEGAL = {"newline": "\n", "carriage_return": "\r"}


def _synthetic_token(illegal: str) -> str:
    # Split so secret scanners never read this synthetic value as a real Slack token.
    return "xox" + f"b-9182736450-5647382910-QwErTyUiOpAsDf{illegal}ZxCvBnMmLkJhGf"


def _fragments(token: str) -> set[str]:
    """Every run of 6 characters of the token that does not cross its illegal character."""
    runs = [run for run in token.replace("\n", "\0").replace("\r", "\0").split("\0") if run]
    return {run[i:i + 6] for run in runs for i in range(len(run) - 5)}


@asynccontextmanager
async def _local_header_client():
    """Real httpx/h11 against a local listener, shared by the store and log regressions."""
    import httpx
    from lib.slack_api import SlackClient

    class _Local(httpx.AsyncHTTPTransport):
        # The real transport, pointed at a local listener: h11 refuses the header before anything is sent.
        async def handle_async_request(self, request):
            request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=_Local.port)
            return await super().handle_async_request(request)

    class _RealClient(SlackClient):
        def __init__(self, bot, app):
            super().__init__(bot, app, http_client=httpx.AsyncClient(transport=_Local(), trust_env=False))

    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    _Local.port = server.sockets[0].getsockname()[1]
    try:
        yield _RealClient
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("illegal", sorted(_ILLEGAL))
def test_a_token_in_a_real_http_error_never_reaches_the_store_or_the_status(tmp_path, monkeypatch, illegal) -> None:
    """The real SlackClient's request fails in httpx on the token's illegal character; the daemon stores the error."""
    import httpx

    token = _synthetic_token(_ILLEGAL[illegal])
    daemon = _load_daemon()

    async def run() -> None:
        async with _local_header_client() as client:
            monkeypatch.setattr(daemon, "SlackClient", client)
            monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_a: None)
            await daemon._run()

    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp" + "-1-A09SYNTH-0000000000-abcdefabcdef")
    with pytest.raises(httpx.LocalProtocolError) as raised:
        asyncio.run(run())
    assert any(piece in str(raised.value) for piece in _fragments(token))  # the library did print the token
    store = BridgeStore(tmp_path)
    try:
        stored = store.runtime_value("last_socket_error")
    finally:
        store.close()
    payload = _read_status_route(_TokensApi(tmp_path, bot_token=token))
    for text in (stored, json.dumps(payload, ensure_ascii=False), json.dumps(payload)):
        leaked = sorted(piece for piece in _fragments(token) if piece in text)
        assert leaked == [], (leaked, text)
    assert stored.startswith("Illegal header value b'Bearer [redacted]")
    assert payload["link"]["error"] == stored and payload["attention"][0]["error"] == {"text": stored}


@pytest.mark.parametrize("illegal", sorted(_ILLEGAL))
def test_real_http_error_is_clean_in_all_bridge_logs_and_exception_text(monkeypatch, caplog, illegal) -> None:
    import logging
    import httpx

    token = _synthetic_token(_ILLEGAL[illegal])
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)
    # A non-shaped configured value proves logs use the same configured-secrets source too.
    app = "fixture-configured-app\rOtherFixtureSecret"
    monkeypatch.setenv("SLACK_APP_TOKEN", app)
    caplog.set_level(logging.DEBUG)
    daemon = _load_daemon()

    async def run():
        async with _local_header_client() as client:
            slack = client(token, "xapp" + "-fixture")
            try:
                await slack.auth_test()
            except httpx.LocalProtocolError as exc:
                assert any(piece in str(exc) for piece in _fragments(token))
                for name in ("lib.runtime", "lib.socket_mode", "slack_bridge"):
                    logger = logging.getLogger(name)
                    logger.warning("Request failed: %s; app=%r", exc, app)
                    logger.debug("Request traceback", exc_info=True)
                    logger.exception("Request failed with traceback: %s", exc)
                logging.getLogger("lib.socket_mode").warning("channel_not_found")
            finally:
                await slack._http.aclose()

    monkeypatch.setattr(daemon, "_run", run)
    # Use the actual daemon entry point: its configured handlers also serve httpcore DEBUG records.
    filters_before = {h: list(h.filters) for h in logging.getLogger().handlers}
    try:
        daemon.main()
        output = caplog.text
        assert "LocalProtocolError" in output and "Traceback" in output
        assert "channel_not_found" in output
        assert {"lib.runtime", "lib.socket_mode", "slack_bridge"} <= {r.name for r in caplog.records}
        for secret in (token, app):
            assert not sorted(piece for piece in _fragments(secret) if piece in output)
        for record in caplog.records:
            rendered = logging.Formatter("%(message)s").format(record)
            assert not any(piece in rendered for piece in _fragments(token) | _fragments(app))
    finally:
        for handler, filters in filters_before.items():
            handler.filters[:] = filters


@pytest.mark.parametrize("kind", ("delivery", "reaction"))
def test_report_error_is_clean_when_stored_and_handed_to_host(tmp_path, monkeypatch, kind) -> None:
    import dataclasses

    token = _synthetic_token("\n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)
    app = "fixture-configured-app\rOtherFixtureSecret"
    monkeypatch.setenv("SLACK_APP_TOKEN", app)

    class Host:
        explicit_completion = 1

        def __init__(self):
            self.payloads = []

        async def report_delivery(self, payload):
            self.payloads.append(payload)

    async def run():
        host = Host()
        with closing(BridgeStore(tmp_path)) as store:
            for index, error in enumerate((f"Illegal header value {f'Bearer {token}'.encode()!r}; {app!r}",
                                           "channel_not_found")):
                if kind == "delivery":
                    store.enqueue_outbox(request_id=f"r{index}", target="C09SYNTH01", thread_ts="",
                                         chunks=("Synthetic reply",), delivery_reporting_version=1)
                else:
                    store.enqueue_mutation(request_id=f"r{index}", operation="reaction_add",
                                           payload={"channel": "C09SYNTH01", "ts": "1.0", "name": "eyes"},
                                           delivery_reporting_version=1)
                item = dataclasses.replace(store.claim_outbox(), provider_account_id="T09SYNTH01")
                report = (item.delivery_report if kind == "delivery" else item.reaction_report)("failed", error=error)
                store.fail_outbox(item.row_id, item.lease_token, error, report_payload=report)
                with sqlite3.connect(tmp_path / "slack_bridge.sqlite3") as conn:
                    saved = json.loads(conn.execute("SELECT report_payload_json FROM outbox WHERE id=?",
                                                    (item.row_id,)).fetchone()[0])
                await OutboundWorker(store, _Slack(), host)._report(store.claim_report())
                assert saved == host.payloads[-1] == report
                for payload in (saved, host.payloads[-1]):
                    for text in (json.dumps(payload), json.dumps(payload, ensure_ascii=False)):
                        assert not any(piece in text for piece in _fragments(token) | _fragments(app))
                assert saved["message"]["error"] == ("channel_not_found" if index else
                                                       "Illegal header value b'Bearer [redacted]'; '[redacted]'")

    asyncio.run(run())


def test_records_of_an_older_version_holding_a_token_come_out_clean(tmp_path) -> None:
    token = _synthetic_token("\n")
    printed = repr(f"Bearer {token}".encode())  # how a header refusal prints it, with "\\n"
    store = BridgeStore(tmp_path)
    try:
        store.set_runtime(socket_state="connected", host_adapter_state="active", companion_pid=os.getpid(),
                          reaction_context_state="available", history_reporting_state="supported")
        raw = _envelope(1)
        store.ingest_envelope(raw, parse_socket_envelope(raw, bot_user_id="U_BOT"))
        event = store.claim_inbox()
        store.fail_inbox(event.row_id, event.lease_token, "placeholder")
        store.enqueue_outbox(request_id="old", target="C09A", thread_ts="", chunks=("one",))
        part = store.claim_outbox()
        store.fail_outbox(part.row_id, part.lease_token, "placeholder", state="failed")
        store.enqueue_outbox(request_id="held", target="C09B", thread_ts="", chunks=("two",))
        store.hold_outbox(store.claim_outbox(), "Held: placeholder")
    finally:
        store.close()
    # What 1.12.x wrote before any redaction existed, put straight into the file.
    with sqlite3.connect(tmp_path / "slack_bridge.sqlite3") as conn:
        conn.execute("UPDATE runtime_state SET value_json=? WHERE key='socket_state'", (json.dumps("error"),))
        conn.execute("INSERT OR REPLACE INTO runtime_state(key, value_json, updated_at) VALUES('last_socket_error', ?, 1)",
                     (json.dumps(f"Illegal header value {printed}"),))
        conn.execute("UPDATE inbox SET last_error=?", (f"Authorization: Bearer {token}",))
        conn.execute("UPDATE outbox SET last_error=? WHERE state='failed'", (f"headers={{'Authorization': 'Bearer {token}'}}",))
        conn.execute("UPDATE outbox SET last_error=? WHERE state='pending'", (f"Held: token {token} refused",))
    payload = _read_status_route(_TokensApi(tmp_path, bot_token=token))
    text = json.dumps(payload, ensure_ascii=False)
    assert sorted(piece for piece in _fragments(token) if piece in text) == []
    assert "[redacted]" in payload["last_socket_error"] and "[redacted]" in payload["last_delivery_error"]
    assert payload["recent_problems"]["inbound"]["last_failed_error"]["text"] == "Authorization: Bearer [redacted]"
    assert payload["held_reasons"][0]["reason"] == "token [redacted] refused"


@pytest.mark.parametrize("text", ["channel_not_found", "Slack API error: invalid_auth", "ReadTimeout",
                                  "not_in_channel: #ops-room", "Presence Host returned HTTP 503",
                                  "missing Authorization header"])
def test_ordinary_error_text_is_left_as_it_is(tmp_path, text) -> None:
    from lib.redaction import redact
    for words in (text, "Held: Ops room is paused", "Bearer token expected"):
        assert redact(words, ("xoxb-synthetic-bot-token", "xapp-synthetic-app-token")) == words
    store = BridgeStore(tmp_path)
    try:
        store.set_runtime(socket_state="error", last_socket_error=text, companion_pid=os.getpid())
        store.enqueue_outbox(request_id="m", target="C09A", thread_ts="", chunks=("one",))
        part = store.claim_outbox()
        store.fail_outbox(part.row_id, part.lease_token, text, state="failed")
    finally:
        store.close()
    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["last_socket_error"] == text and payload["link"]["error"] == text
    assert payload["recent_problems"]["outbound_text"]["last_failed_error"]["text"] == text


def _deliver(store: BridgeStore) -> None:
    part = store.claim_outbox()
    store.complete_outbox(part.row_id, part.lease_token, provider_message_ts="1700000000.000100")


def _fail(store: BridgeStore, error: str, *, state: str = "failed", at: float | None = None) -> None:
    part = store.claim_outbox()
    store.fail_outbox(part.row_id, part.lease_token, error, state=state)
    if at is not None:
        _set_row_time(store.path, "outbox", "id=?", (part.row_id,), at)


def test_refused_messages_are_classed_by_their_other_parts(tmp_path) -> None:
    from lib.status_summary import attention_items
    now = 1795384800.0
    store = BridgeStore(tmp_path)
    try:
        # A: part 1 delivered, part 2 refused (nothing left queued).
        store.enqueue_outbox(request_id="a", target="C09A", thread_ts="", chunks=("a1", "a2"))
        _deliver(store)
        _fail(store, "msg_too_long", at=now - 60)
        # C: part 1's outcome unknown two days ago, part 2 refused today.
        store.enqueue_outbox(request_id="c", target="C09C", thread_ts="", chunks=("c1", "c2"))
        _fail(store, "ReadTimeout", state="uncertain", at=now - 2 * 86400)
        _fail(store, "not_in_channel", at=now - 40)
        # D: its only part refused.
        store.enqueue_outbox(request_id="d", target="C09D", thread_ts="", chunks=("d1",))
        _fail(store, "is_archived", at=now - 30)
        # B: part 1 refused, part 2 still waiting (enqueued last, so no later claim takes it).
        store.enqueue_outbox(request_id="b", target="C09B", thread_ts="", chunks=("b1", "b2"))
        _fail(store, "channel_not_found", at=now - 50)
        status = store.status(now=now, pid_alive=lambda _pid: True)
    finally:
        store.close()
    text = status["recent_problems"]["outbound_text"]
    assert text["failed_messages"] == 4
    assert (text["failed_messages_still_queued"], text["failed_messages_others_delivered_or_unknown"],
            text["failed_messages_every_part_refused"]) == (1, 2, 1)
    item = next(item for item in attention_items({**status, **_quiet_route_payload()}) if item["kind"] == "not_delivered")
    assert (item["still_queued"], item["others_settled"], item["every_part_refused"]) == (1, 2, 1)


def test_an_item_shows_the_error_and_time_of_the_messages_it_counts(tmp_path) -> None:
    from lib.status_summary import attention_items
    now = 1795384800.0
    store = BridgeStore(tmp_path)
    try:
        # Y: refused an hour ago; this is the one message counted as not delivered.
        store.enqueue_outbox(request_id="y", target="C09Y", thread_ts="", chunks=("y1",))
        _fail(store, "channel_not_found", at=now - 3600)
        # X: a newer refused part, but its other part's outcome is unknown, so X counts as unknown.
        store.enqueue_outbox(request_id="x", target="C09X", thread_ts="", chunks=("x1", "x2"))
        _fail(store, "invalid_auth", at=now - 60)
        _fail(store, "ReadTimeout", state="uncertain", at=now - 120)
        status = store.status(now=now, pid_alive=lambda _pid: True)
    finally:
        store.close()
    text = status["recent_problems"]["outbound_text"]
    assert (text["failed_messages"], text["uncertain_messages"]) == (1, 1)
    items = {item["kind"]: item for item in attention_items({**status, **_quiet_route_payload()})}
    assert items["not_delivered"]["error"]["text"] == "channel_not_found"
    assert items["not_delivered"]["at"] == _iso_z(now - 3600)
    assert items["outcome_unknown"]["error"]["text"] == "ReadTimeout"
    assert items["outcome_unknown"]["at"] == _iso_z(now - 120)
    assert status["last_error"]["text"] == "invalid_auth"  # the lifetime history keeps the newest of all


def test_a_held_message_with_an_old_problem_part_is_not_said_to_be_listed_above(tmp_path) -> None:
    from lib.status_summary import attention_items
    now = 1795384800.0
    store = BridgeStore(tmp_path)
    try:
        store.enqueue_outbox(request_id="old", target="C09A", thread_ts="", chunks=("one", "two"))
        _fail(store, "msg_blocked", at=now - 2 * 86400)
        store.hold_outbox(store.claim_outbox(), "Held: Ops room is paused")
        status = store.status(now=now, pid_alive=lambda _pid: True)
    finally:
        store.close()
    assert status["outbound_messages_with_problem_parts"]["held"] == 1
    assert status["outbound_messages_with_recent_problem_parts"]["held"] == 0
    items = attention_items({**status, **_quiet_route_payload()})
    assert [item["kind"] for item in items] == ["held"]  # no refused item is shown for it
    assert (items[0]["with_problem_parts"], items[0]["with_recent_problem_parts"]) == (1, 0)


@pytest.mark.parametrize("first", ["200", "503"])
def test_overlapping_host_checks_leave_the_published_capabilities_equal_to_the_adapters(tmp_path, first) -> None:
    """Two discoveries in flight at once, one answered 200 and one 503, finishing in either order."""
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import BridgeRuntime, _reactions_live

    gates = {"200": asyncio.Event(), "503": asyncio.Event()}
    started: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        answer = "200" if len(started) == 1 else "503"  # the first call is the start check (503 below)
        if len(started) == 0:
            started.append("start")
            return httpx.Response(503, json={"ok": False})
        started.append(answer)
        await gates[answer].wait()
        if answer == "503":
            return httpx.Response(503, json={"ok": False})
        # A Host bound by binding_id (no Places): the 503 then returns the adapter exactly to its state before.
        return httpx.Response(200, json={"ok": True, "presence_delivery_version": 1,
                                         "presence_reaction_context": 1, "presence_explicit_completion": 1})

    async def run() -> dict:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = LoopbackPresenceHostAdapter(binding_id="9f4c2a7e1b8053d6c4e2a9187b06f5d3",
                                              host_service_url="http://127.0.0.1:8767",
                                              skill_token="skill-token", http_client=http)
        store = BridgeStore(tmp_path)
        try:
            BridgeRuntime(store=store, slack=_Slack(), host=adapter, bot_user_id="U09OUROBOT1")
            await adapter.discover_delivery_support()  # the start check: 503
            ok = asyncio.create_task(adapter.discover_delivery_support())
            await asyncio.sleep(0)
            failed = asyncio.create_task(adapter.discover_delivery_support())
            await asyncio.sleep(0)
            assert started == ["start", "200", "503"]
            second = "503" if first == "200" else "200"
            gates[first].set()
            await (ok if first == "200" else failed)
            gates[second].set()
            await asyncio.gather(ok, failed)
            status = store.status()
            return {"store": (status["reaction_context_state"], status["history_reporting_state"]),
                    "adapter": ("available" if _reactions_live(adapter) else "update_required",
                                adapter.delivery_reporting_status)}
        finally:
            store.close()
            await http.aclose()

    seen = asyncio.run(run())
    assert seen["store"] == seen["adapter"], seen
