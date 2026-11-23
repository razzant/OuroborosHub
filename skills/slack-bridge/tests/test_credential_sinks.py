"""No credential reaches an outside sink (1.13.0 round 8).

Sinks: the card (every field of GET status), every log line (the bridge's loggers, third-party loggers and Python's
own printers of uncaught exceptions), and every report sent to the Host, including one queued by an older version.
The message posted to Slack, and its copy in the report's ``text``, are never changed. Token-shaped literals are
split so secret scanners do not read them as real tokens; every value here is synthetic.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
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

from lib.redaction import MARK, install_log_redaction, redact
from lib.runtime import OutboundWorker
from lib.store import BridgeStore
from test_status_contract import _read_status_route, _TokensApi
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


# ---------- plugin.py (runs in the Host process): the settings form's answers ----------

def test_a_token_pasted_into_the_settings_form_is_not_echoed_back(tmp_path) -> None:
    from test_workers_plugin import _http_request, _settings_route

    pasted = BOT + "9182736450-5647382910-PastedIntoTheChannelField"

    async def run() -> tuple[int, dict]:
        handler, _methods, _fields = _settings_route(tmp_path)
        body = json.dumps({"SLACK_NEW_ROOT_CHANNELS": pasted}).encode()
        response = await handler(_http_request("POST", body))
        return response.status_code, json.loads(response.body)

    status, answer = asyncio.run(run())
    assert status == 400 and "is not a Slack channel ID" in answer["error"], answer
    assert "PastedIntoTheChannelField" not in answer["error"] and MARK in answer["error"]
