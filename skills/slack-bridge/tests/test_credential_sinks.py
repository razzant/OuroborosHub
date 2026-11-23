"""No credential reaches an outside sink (1.13.0 round 8).

Sinks: the card (every field of GET status), every log line (the bridge's loggers, third-party loggers and Python's
own printers of uncaught exceptions), and every report sent to the Host, including one queued by an older version.
The message posted to Slack, and its copy in the report's ``text``, are never changed. Token-shaped literals are
split so secret scanners do not read them as real tokens; every value here is synthetic.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
import io
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading

import pytest

from lib.events import parse_socket_envelope
from lib.redaction import MARK, install_log_redaction, redact
from lib.runtime import OutboundWorker
from lib.store import BridgeStore
from test_status_contract import _envelope, _load_daemon, _read_status_route, _TokensApi
from test_workers_plugin import _Slack

SKILL = Path(__file__).resolve().parents[1]
BOT = "xox" + "b-"
APP = "xa" + "pp-"


def _windows(secret: str, size: int = 6) -> set[str]:
    """Every run of ``size`` characters of the secret that does not cross whitespace or a control character."""
    runs = "".join(ch if ch.isprintable() and not ch.isspace() else "\0" for ch in secret).split("\0")
    return {run[i:i + size] for run in runs if run for i in range(len(run) - size + 1)}


# The tokens of the brief: an interior newline, a carriage return, and Carbon's case 1 (outer spaces and an
# interior newline before a four-character tail).
TOKENS = {
    "newline": BOT + "9182736450-5647382910-QwErTyUiOpAsDf\nZxCvBnMmLkJhGf",
    "carriage_return": BOT + "9182736450-5647382910-QwErTyUiOpAsDf\rZxCvBnMmLkJhGf",
    "outer_spaces_short_tail": "  " + BOT + "9182736450-5647382910-QwErTy\nZx99  ",
}
TAILS = {"newline": "ZxCvBnMmLkJhGf", "carriage_return": "ZxCvBnMmLkJhGf", "outer_spaces_short_tail": "Zx99"}
APP_TOKEN = APP + "1-A09SYNTH-0000000000-abcdefabcdef"


def _leaks(text: str, token: str, tail: str) -> list[str]:
    return sorted({piece for piece in _windows(token) if piece in text} | ({tail} if tail in text else set()))


# ---------- R8-11: no part of a split credential survives ----------

@pytest.mark.parametrize("text,expected", [
    # Case 1: the configured token has outer spaces; the client strips it, the header error prints it escaped.
    ("Illegal header value " + repr(("Bearer " + TOKENS["outer_spaces_short_tail"].strip()).encode()),
     "Illegal header value b'Bearer [redacted]'"),
    # The same token printed raw (a real newline) inside a log line.
    ("auth failed: " + TOKENS["outer_spaces_short_tail"].strip() + " (retrying)", "auth failed: [redacted] (retrying)"),
])
def test_a_configured_token_with_outer_spaces_leaves_no_tail(monkeypatch, text, expected) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["outer_spaces_short_tail"])
    monkeypatch.setenv("SLACK_APP_TOKEN", APP_TOKEN)
    assert redact(text) == expected


@pytest.mark.parametrize("text,expected", [
    # Case 2: a token that is not configured (older or rotated), split by a real newline after its prefix.
    ("'Authorization: " + BOT + "9182736450\nTailSecret99'", "'Authorization: [redacted]'"),
    ("Authorization: Bearer " + BOT + "9182736450\\nTailSecret99", "Authorization: Bearer [redacted]"),
    ("headers={'Authorization': 'Bearer abcdef0123\\r\\nTailSecret99'}", "headers={'Authorization': 'Bearer [redacted]'}"),
    # A token-shaped value with no prefix at all.
    ("posting failed for " + BOT + "9182736450\nTailSecret99 today", "posting failed for [redacted] today"),
    ("posting failed for " + BOT + "9182736450\\tTailSecret99 today", "posting failed for [redacted] today"),
])
def test_a_split_credential_that_is_not_configured_leaves_no_tail(monkeypatch, text, expected) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    assert redact(text) == expected
    assert "TailSecret99" not in redact(text)
    assert redact(redact(text)) == redact(text)  # cleaning twice changes nothing (reports are cleaned twice)


@pytest.mark.parametrize("text", ["channel_not_found", "ReadTimeout", "Held: Ops room is paused", "Bearer token expected",
                                  "missing Authorization header", "Slack API error: invalid_auth",
                                  "not_in_channel: #ops-room", "https://slack.com/api/chat.postMessage?channel=C09A",
                                  "Authorization: Bearer [redacted]", "multi\nline\nnote"])
def test_ordinary_text_is_byte_identical(monkeypatch, text) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["newline"])
    monkeypatch.setenv("SLACK_APP_TOKEN", APP_TOKEN)
    assert redact(text) == text


# ---------- R8-1: an uncaught exception never prints a token to stderr ----------

_DRIVER = r'''
import asyncio, gc, importlib.util, os, sys, threading, warnings
skill, mode, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, skill)
spec = importlib.util.spec_from_file_location("slack_daemon_driver", os.path.join(skill, "scripts", "slack_daemon.py"))
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)
import httpx
from lib.slack_api import SlackClient
token = os.environ["SLACK_BOT_TOKEN"]


class Local(httpx.AsyncHTTPTransport):
    # The real transport, pointed at a local listener: h11 refuses the token's illegal character itself.
    async def handle_async_request(self, request):
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
        return await super().handle_async_request(request)


class RealSlack(SlackClient):
    def __init__(self, bot, app):
        super().__init__(bot, app, http_client=httpx.AsyncClient(transport=Local(), trust_env=False))


class Leaky:
    def __del__(self):
        raise RuntimeError("finalizer saw Bearer " + token)


class PrintersSlack:
    """auth.test exercises every default printer before it raises: a thread, an unretrieved task, a finalizer, a warning."""
    def __init__(self, *_args):
        pass

    async def auth_test(self):
        worker = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError("thread saw " + token)), name="leaky")
        worker.start()
        worker.join()

        async def boom():
            raise RuntimeError("task saw Authorization: " + token)

        task = asyncio.ensure_future(boom())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del task
        gc.collect()
        Leaky()
        gc.collect()
        warnings.warn("warning saw " + token)
        raise RuntimeError("auth.test failed: " + token)

    async def aclose(self):
        pass


class SignedIn:
    def __init__(self, *_args):
        pass

    async def auth_test(self):
        return {"user_id": "U09BOT", "bot_id": "B09BOT", "team_id": "T09TEAM", "team": "Northstar Labs"}

    async def aclose(self):
        pass


class QuietRuntime:
    def __init__(self, *, store, **_kwargs):
        self.store = store

    async def run(self):
        self.store.set_runtime(socket_state="connected")

    async def close(self):
        self.store.set_runtime(socket_state="disconnected")


if mode == "header":
    daemon.SlackClient = RealSlack
elif mode == "printers":
    daemon.SlackClient = PrintersSlack
else:
    daemon.SlackClient = SignedIn
    daemon.BridgeRuntime = QuietRuntime
    daemon.create_host_adapter = lambda *_a, **_k: object()
daemon.main()
'''


def _run_daemon(tmp_path, mode: str, token: str) -> subprocess.CompletedProcess:
    """The real ``main()`` and ``_run()`` in their own process, as the companion runs; stderr is its log."""
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)  # the kernel accepts; nothing is ever read
        env = {key: value for key, value in os.environ.items() if not key.startswith(("SLACK_", "HOST_SERVICE"))}
        env.update(SLACK_BOT_TOKEN=token, SLACK_APP_TOKEN=APP_TOKEN, OUROBOROS_SKILL_STATE_DIR=str(tmp_path),
                   SLACK_BRIDGE_LOG_LEVEL="DEBUG", PYTHONWARNINGS="always")
        return subprocess.run([sys.executable, str(driver), str(SKILL), mode, str(listener.getsockname()[1])],
                              env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_a_crash_at_sign_in_exits_nonzero_without_printing_the_token(tmp_path, name) -> None:
    done = _run_daemon(tmp_path, "header", TOKENS[name])
    assert done.returncode != 0
    assert "LocalProtocolError" in done.stderr and "Slack bridge stopped after an error" in done.stderr, done.stderr
    assert _leaks(done.stderr + done.stdout, TOKENS[name], TAILS[name]) == [], done.stderr
    with closing(BridgeStore(tmp_path)) as store:
        stored = store.runtime_value("last_socket_error")
    assert stored.startswith("Illegal header value b'Bearer [redacted]") and _leaks(stored, TOKENS[name], TAILS[name]) == []


def test_every_default_printer_goes_through_the_redacting_handlers(tmp_path) -> None:
    token = TOKENS["newline"]
    done = _run_daemon(tmp_path, "printers", token)
    assert done.returncode != 0
    for words in ("Uncaught exception in thread leaky", "Task exception was never retrieved", "Exception ignored",
                  "warning saw", "Slack bridge stopped after an error"):
        assert words in done.stderr, (words, done.stderr)
    assert _leaks(done.stderr + done.stdout, token, TAILS["newline"]) == [], done.stderr


def test_a_clean_run_exits_zero_and_prints_no_traceback(tmp_path) -> None:
    done = _run_daemon(tmp_path, "clean", TOKENS["newline"])
    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr and "stopped after an error" not in done.stderr
    with closing(BridgeStore(tmp_path)) as store:
        assert store.runtime_value("socket_state") == "disconnected"


# ---------- R8-2: the Socket Mode ticket never reaches a DEBUG log ----------

@pytest.fixture
def redacting_root():
    """The daemon's logging set-up on a captured root handler; hooks and handlers are restored afterwards."""
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), sys.excepthook, threading.excepthook, sys.unraisablehook)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG)
    install_log_redaction()
    try:
        yield stream
    finally:
        logging.captureWarnings(False)
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]
        sys.excepthook, threading.excepthook, sys.unraisablehook = saved[2:]


def test_the_socket_mode_handshake_logs_no_ticket_and_still_connects(tmp_path, monkeypatch, redacting_root) -> None:
    import websockets
    from lib.socket_mode import SocketModeClient

    ticket = "synthetic-ticket-7d1e0c94b2a54f6e8a3b"
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["newline"])
    monkeypatch.setenv("SLACK_APP_TOKEN", APP_TOKEN)
    seen_paths = []

    async def run() -> str:
        async def handler(connection):
            seen_paths.append(connection.request.path)
            await connection.send(json.dumps({"type": "hello"}))
            await connection.wait_closed()

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            class Slack:
                async def open_socket_url(self):
                    return f"ws://127.0.0.1:{port}/link/?ticket={ticket}&app_id=A09SYNTH"

            with closing(BridgeStore(tmp_path)) as store:
                client = SocketModeClient(Slack(), store, bot_user_id="U09OUROBOT1")
                task = asyncio.create_task(client.run())
                for _ in range(200):
                    if store.runtime_value("socket_state") == "connected":
                        break
                    await asyncio.sleep(0.02)
                state = store.runtime_value("socket_state")
                await client.close()
                await asyncio.wait_for(task, 10)
                return state

    assert asyncio.run(run()) == "connected"
    assert seen_paths == [f"/link/?ticket={ticket}&app_id=A09SYNTH"]  # the URL actually used is unchanged
    output = redacting_root.getvalue()
    assert "ticket=" + MARK in output and "app_id=A09SYNTH" in output, output  # websockets did log the path
    assert ticket not in output and "7d1e0c94b2a5" not in output, output


# ---------- R8-3: provider display labels on the card ----------

@pytest.mark.parametrize("workspace,label,clean_workspace,clean_label", [
    (BOT + "4417-0000-SYNTHETICWORKSPACE", "Bearer synthetic0123456789", MARK, "Bearer " + MARK),
    ("Northstar Labs", "ouroboros-bot", "Northstar Labs", "ouroboros-bot"),
])
def test_provider_labels_pass_through_redaction_on_the_status_route(tmp_path, workspace, label, clean_workspace,
                                                                    clean_label) -> None:
    with closing(BridgeStore(tmp_path)) as store:
        store.set_runtime(socket_state="connected", host_adapter_state="active", companion_pid=os.getpid(),
                          reaction_context_state="available", history_reporting_state="supported",
                          workspace_name=workspace, workspace_id="T09SYNTH01", bot_user_id="U09OUROBOT1",
                          presence_identity=1,
                          connector_identity={"sends_as": {"id": "U09OUROBOT1", "label": label, "kind": "bot"}})
    payload = _read_status_route(_TokensApi(tmp_path))
    assert payload["workspace_name"] == clean_workspace
    assert payload["identity"]["sends_as"] == {"id": "U09OUROBOT1", "label": clean_label, "kind": "bot"}
    assert payload["workspace_id"] == "T09SYNTH01" and payload["text"] == "Connected · nothing needs attention"
    if clean_workspace == MARK:
        text = json.dumps(payload)
        assert "SYNTHETICWORKSPACE" not in text and "synthetic0123456789" not in text


# ---------- R8-4: Host reports, new and queued by an older version ----------

class _Host:
    explicit_completion = 1

    def __init__(self):
        self.payloads = []

    async def report_delivery(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))


def _failed_part(store: BridgeStore, request_id: str, text: str):
    import dataclasses
    store.enqueue_outbox(request_id=request_id, target="C09SYNTH01", thread_ts="", chunks=(text,),
                         delivery_reporting_version=1)
    return dataclasses.replace(store.claim_outbox(), provider_account_id="T09SYNTH01")


def _old_report(item, error: str) -> dict:
    """The exact payload 1.12.2's ``OutboxItem.delivery_report('failed', error=...)`` built: the error kept verbatim."""
    return {"schema_version": 1, "delivery_id": item.request_id, "part_id": str(item.chunk_index), "state": "failed",
            "provider": "slack", "account_id": item.provider_account_id, "conversation_id": item.target,
            "thread_id": item.thread_ts, "text": item.text, "format": item.text_format,
            "message": {"provider_message_id": "", "requested_target": item.target, "target_resolved": False,
                        "chunk_count": item.chunk_count, "error": error},
            "origin": item.origin}


@pytest.mark.parametrize("built_by", ["1.13.0", "1.12.2"])
def test_a_report_reaches_the_host_without_the_token_and_otherwise_unchanged(tmp_path, monkeypatch, built_by) -> None:
    token = TOKENS["newline"]
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)
    monkeypatch.setenv("SLACK_APP_TOKEN", APP_TOKEN)
    error = "Illegal header value " + repr(("Bearer " + token).encode()) + "; note: Authorization: " + token
    # The posted message is never changed, nor its copy in the report: the Host matches it against the turn's reply.
    message_text = "Deploy note: rotate the key before Friday (ordinary words)."

    async def run() -> tuple[dict, dict]:
        host = _Host()
        with closing(BridgeStore(tmp_path)) as store:
            item = _failed_part(store, "synthetic-report", message_text)
            report = item.delivery_report("failed", error=error) if built_by == "1.13.0" else _old_report(item, error)
            store.fail_outbox(item.row_id, item.lease_token, "placeholder", report_payload=report)
            with sqlite3.connect(store.path) as conn:
                saved = json.loads(conn.execute("SELECT report_payload_json FROM outbox WHERE id=?",
                                                (item.row_id,)).fetchone()[0])
            await OutboundWorker(store, _Slack(), host)._report(store.claim_report())
            return saved, host.payloads[-1]

    saved, sent = asyncio.run(run())
    if built_by == "1.12.2":
        assert TAILS["newline"] in saved["message"]["error"]  # the older row really holds the token
    for text in (json.dumps(sent), json.dumps(sent, ensure_ascii=False)):
        assert _leaks(text, token, TAILS["newline"]) == [], text
    assert sent["message"]["error"] == "Illegal header value b'Bearer [redacted]'; note: Authorization: [redacted]"
    without_error = {**sent, "message": {k: v for k, v in sent["message"].items() if k != "error"}}
    assert without_error == {**saved, "message": {k: v for k, v in saved["message"].items() if k != "error"}}
    assert sent["text"] == message_text


def test_an_ordinary_report_is_byte_identical(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["newline"])

    async def run() -> tuple[dict, dict]:
        host = _Host()
        with closing(BridgeStore(tmp_path)) as store:
            item = _failed_part(store, "synthetic-plain", "hello #ops-room")
            report = _old_report(item, "channel_not_found")
            store.fail_outbox(item.row_id, item.lease_token, "channel_not_found", report_payload=report)
            claimed = store.claim_report()
            assert claimed["cleaned"] is False
            await OutboundWorker(store, _Slack(), host)._report(claimed)
            return report, host.payloads[-1]

    built, sent = asyncio.run(run())
    assert json.dumps(sent, sort_keys=True) == json.dumps(built, sort_keys=True)


@pytest.mark.parametrize("cleaned", [True, False])
def test_a_receipt_conflict_ends_only_a_cleaned_older_report(tmp_path, monkeypatch, cleaned) -> None:
    """An older report the Host recorded before its answer was lost differs from its cleaned copy (HTTP 409)."""
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter

    token = TOKENS["newline"]
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "error": "presence delivery identity already has different facts",
                                         "code": "presence_receipt_conflict", "disposition": "rejected"})

    async def run() -> tuple[str, str]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        host = LoopbackPresenceHostAdapter(binding_id="9f4c2a7e1b8053d6c4e2a9187b06f5d3",
                                           host_service_url="http://127.0.0.1:8767", skill_token="skill-token",
                                           http_client=http)
        try:
            with closing(BridgeStore(tmp_path)) as store:
                item = _failed_part(store, "synthetic-conflict", "hello")
                report = _old_report(item, ("Authorization: " + token) if cleaned else "channel_not_found")
                store.fail_outbox(item.row_id, item.lease_token, "placeholder", report_payload=report)
                claimed = store.claim_report()
                assert claimed["cleaned"] is cleaned
                await OutboundWorker(store, _Slack(), host)._report(claimed)
                with sqlite3.connect(store.path) as conn:
                    return conn.execute("SELECT report_state, report_error FROM outbox WHERE id=?",
                                        (item.row_id,)).fetchone()
        finally:
            await http.aclose()

    state, error = asyncio.run(run())
    if cleaned:
        assert (state, error) == ("acked", "")  # the Host holds this part's receipt
    else:
        assert state == "pending" and "different receipt" in error  # a new report's conflict stays visible


# ---------- plugin.py (runs in the Host process): the settings form's answers (R9-3) ----------

# Tokens joined to what precedes them, and not in this process's environment.
UNDERSCORE_JOINED = "C09SYNTH01_" + BOT + "9182736450-5647382910-JoinedByUnderscore"
DIRECTLY_APPENDED = "C09SYNTH01" + BOT + "9182736450-5647382910-AppendedDirectly"


def _save_settings(tmp_path, body: dict) -> tuple[int, dict]:
    from test_workers_plugin import _http_request, _settings_route

    async def run() -> tuple[int, dict]:
        handler, _methods, _fields = _settings_route(tmp_path)
        response = await handler(_http_request("POST", json.dumps(body).encode()))
        return response.status_code, json.loads(response.body)

    return asyncio.run(run())


@pytest.mark.parametrize("entry,position", [(BOT + "9182736450-5647382910-PastedIntoTheChannelField", 1),
                                            ("C09SYNTH02 " + UNDERSCORE_JOINED, 2),
                                            ("C09SYNTH02, C09SYNTH03 " + DIRECTLY_APPENDED, 3)])
def test_a_rejected_channel_entry_is_named_by_position_never_echoed(tmp_path, monkeypatch, entry, position) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    status, answer = _save_settings(tmp_path, {"SLACK_NEW_ROOT_CHANNELS": entry})
    assert status == 400 and answer["error"].startswith(f"SLACK_NEW_ROOT_CHANNELS: entry {position} is not a Slack channel ID")
    for piece in ("9182736450", "Pasted", "Joined", "Appended", BOT):
        assert piece not in answer["error"], answer


def test_every_settings_save_error_is_value_free(tmp_path) -> None:
    secret = "secret-" + "9182736450"
    for body in ({"binding_id": "not-hex-" + secret}, {"SLACK_INBOUND_WORKERS": secret}):
        status, answer = _save_settings(tmp_path, body)
        assert status == 400 and secret not in answer["error"] and "9182736450" not in answer["error"], answer


def test_valid_channel_ids_and_ordinary_labels_pass_unchanged(tmp_path, monkeypatch) -> None:
    """Controls: a valid list saves as typed; ordinary labels, also with underscores or "app-", stay as they are."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    status, answer = _save_settings(tmp_path, {"SLACK_NEW_ROOT_CHANNELS": "C09SYNTH01, C09SYNTH02"})
    assert status == 200 and answer["ok"] is True
    for label in ("Northstar_Labs", "ops_room-2", "flexapp-team", "whatsapp-bridge", "xoxo-gossip", "C09SYNTH01"):
        assert redact(label) == label


@pytest.mark.parametrize("workspace,label", [("Northstar_" + BOT + "9182736450-5647382910-WorkspaceTail", "ouroboros-bot"),
                                             ("Northstar Labs", "ops" + BOT + "9182736450-5647382910-LabelTail")])
def test_a_token_joined_to_a_label_is_redacted_on_the_status_route(tmp_path, monkeypatch, workspace, label) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    with closing(BridgeStore(tmp_path)) as store:
        store.set_runtime(socket_state="connected", host_adapter_state="active", companion_pid=os.getpid(),
                          reaction_context_state="available", history_reporting_state="supported",
                          workspace_name=workspace, presence_identity=1,
                          connector_identity={"sends_as": {"id": "U09OUROBOT1", "label": label, "kind": "bot"}})
    payload = _read_status_route(_TokensApi(tmp_path))
    text = json.dumps(payload)
    for tail in ("WorkspaceTail", "LabelTail", "9182736450"):
        assert tail not in text
    assert payload["workspace_name"] in ("Northstar_" + MARK, "Northstar Labs")
    assert payload["identity"]["sends_as"]["label"] in ("ops" + MARK, "ouroboros-bot")


# ---------- Host report identity and text (R9-1, R9-2) ----------

def _report_for(tmp_path, request_id: str, text: str, state: str, error: str = "") -> dict:
    with closing(BridgeStore(tmp_path)) as store:
        item = _failed_part(store, request_id, text)
        return item.delivery_report(state, result={"ts": "1700000000.000100", "channel": "C09SYNTH01"}
                                    if state == "delivered" else None, error=error)


def test_report_identity_passes_byte_identical(tmp_path, monkeypatch) -> None:
    from lib.redaction import redact_report

    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["newline"])
    first = _report_for(tmp_path / "a", "synthetic-req?ticket=101", "one", "failed", error="channel_not_found")
    second = _report_for(tmp_path / "b", "synthetic-req?ticket=102", "two", "failed", error="channel_not_found")
    assert (first["delivery_id"], second["delivery_id"]) == ("synthetic-req?ticket=101", "synthetic-req?ticket=102")
    shaped = {"schema_version": 1, "delivery_id": "d?token=abc123def456", "part_id": "0", "state": "failed",
              "provider": "slack", "account_id": "T09SYNTH01?sig=abcdef012345", "conversation_id": "C09?ticket=777777",
              "thread_id": "1700000000.000100", "text": "", "format": "mrkdwn",
              "message": {"provider_message_id": "1700000000.000200", "requested_target": "C09?ticket=888888",
                          "error": "Bearer abcdef0123456789"},
              "origin": {"kind": "tool", "task_id": "task?ticket=999999", "source_event_id": "Ev?token=123456"}}
    clean = redact_report(shaped)
    assert clean["message"]["error"] == "Bearer " + MARK
    assert {**clean, "message": {**clean["message"], "error": shaped["message"]["error"]}} == shaped


class _CoreReceiver:
    """A fake /presence/delivery keyed like the core (presence_delivery._key: skill, account_id, delivery_id, part_id,
    state), with the core's digest of the whole payload: the same key with other facts is a 409 conflict."""

    def __init__(self):
        self.index, self.conflicts = {}, []

    def handler(self, request):
        import hashlib
        import httpx
        payload = json.loads(request.content)
        key = ("slack-bridge", *(payload[name] for name in ("account_id", "delivery_id", "part_id", "state")))
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if key in self.index and self.index[key] != digest:
            self.conflicts.append(key)
            return httpx.Response(409, json={"ok": False, "error": "presence delivery identity already has different facts",
                                             "code": "presence_receipt_conflict", "disposition": "rejected"})
        self.index[key] = digest
        return httpx.Response(200, json={"ok": True, "recorded": True})


def _dispatch_all(tmp_path, reports: list[dict], receiver) -> list[tuple]:
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter

    async def run() -> list[tuple]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
        host = LoopbackPresenceHostAdapter(binding_id="9f4c2a7e1b8053d6c4e2a9187b06f5d3",
                                           host_service_url="http://127.0.0.1:8767", skill_token="skill-token",
                                           http_client=http)
        try:
            with closing(BridgeStore(tmp_path)) as store:
                for index, report in enumerate(reports):
                    item = _failed_part(store, report["delivery_id"], report["text"])
                    store.fail_outbox(item.row_id, item.lease_token, "placeholder", report_payload=report)
                while (claimed := store.claim_report()) is not None:
                    await OutboundWorker(store, _Slack(), host)._report(claimed)
                with sqlite3.connect(store.path) as conn:
                    return conn.execute("SELECT request_id, report_state, report_error FROM outbox ORDER BY id").fetchall()
        finally:
            await http.aclose()

    return asyncio.run(run())


def _legacy(request_id: str, text: str, error: str, state: str = "failed") -> dict:
    """The exact payload 1.12.2 built and queued, credential included."""
    return {"schema_version": 1, "delivery_id": request_id, "part_id": "0", "state": state, "provider": "slack",
            "account_id": "T09SYNTH01", "conversation_id": "C09SYNTH01", "thread_id": "", "text": text,
            "format": "mrkdwn", "message": {"provider_message_id": "", "requested_target": "C09SYNTH01",
                                            "target_resolved": False, "chunk_count": 1, "error": error},
            "origin": {"kind": "automatic", "source_event_id": "EvSYNTH1"}}


def test_an_upgrade_replay_records_each_report_under_its_own_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["newline"])
    error = "Authorization: " + TOKENS["newline"]
    receiver = _CoreReceiver()
    rows = _dispatch_all(tmp_path, [_legacy("synthetic-req?ticket=101", "one", error),
                                    _legacy("synthetic-req?ticket=102", "two", error)], receiver.handler)
    assert sorted(key[2] for key in receiver.index) == ["synthetic-req?ticket=101", "synthetic-req?ticket=102"]
    assert receiver.conflicts == [] and [row[1] for row in rows] == ["acked", "acked"]


def test_a_conflict_on_the_same_key_counts_as_recorded_only_for_a_cleaned_copy(tmp_path, monkeypatch) -> None:
    """The Host recorded 101's original before its answer was lost; the cleaned copy then conflicts on the same key."""
    import hashlib
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["newline"])
    error = "Authorization: " + TOKENS["newline"]
    receiver = _CoreReceiver()
    original = _legacy("synthetic-req?ticket=101", "one", error)
    receiver.index[("slack-bridge", "T09SYNTH01", "synthetic-req?ticket=101", "0", "failed")] = hashlib.sha256(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    rows = _dispatch_all(tmp_path, [original, _legacy("synthetic-req?ticket=102", "two", error)], receiver.handler)
    assert [key[2] for key in receiver.conflicts] == ["synthetic-req?ticket=101"]
    assert [row[1] for row in rows] == ["acked", "acked"]
    assert ("slack-bridge", "T09SYNTH01", "synthetic-req?ticket=102", "0", "failed") in receiver.index


def test_any_other_409_stays_pending(tmp_path, monkeypatch) -> None:
    """Control: a 409 that is not presence_receipt_conflict leaves even a cleaned report pending."""
    import httpx
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKENS["newline"])
    rows = _dispatch_all(tmp_path, [_legacy("synthetic-req?ticket=101", "one", "Authorization: " + TOKENS["newline"])],
                         lambda _request: httpx.Response(409, json={"ok": False, "code": "presence_origin_mismatch",
                                                                    "disposition": "rejected"}))
    assert rows[0][1] == "pending" and "HTTP 409" in rows[0][2]


PASTED = "notes: Bearer " + "abcdef" + "0123456789 then " + BOT + "9182736450-5647382910-InTheText"


@pytest.mark.parametrize("state,exact", [("delivered", True), ("accepted", True), ("failed", False), ("uncertain", False)])
def test_report_text_stays_exact_only_where_the_core_compares_it(tmp_path, monkeypatch, state, exact) -> None:
    from lib.redaction import redact_report

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    built = (_report_for(tmp_path, "synthetic-text", PASTED, state) if state != "accepted"
             else redact_report(_legacy("synthetic-text", PASTED, "", state="accepted")))
    assert (built["text"] == PASTED) is exact
    if not exact:
        assert built["text"] == "notes: Bearer " + MARK + " then " + MARK
    assert redact_report(built) == built  # dispatch cleans again and changes nothing


@pytest.mark.parametrize("state,exact", [("delivered", True), ("failed", False), ("uncertain", False)])
def test_a_reopened_legacy_report_is_cleaned_by_its_outcome(tmp_path, monkeypatch, state, exact) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    received = []

    def receiver(request):
        import httpx
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "recorded": True})

    _dispatch_all(tmp_path, [_legacy("synthetic-legacy", PASTED, "", state=state)], receiver)
    assert (received[0]["text"] == PASTED) is exact
    assert ("InTheText" in received[0]["text"]) is exact


# ---------- moved from test_status_contract.py (rounds 7 and 7b): error text, logs and reports ----------

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
        assert redact(words, (BOT + "synthetic-bot-token", APP + "synthetic-app-token")) == words
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
