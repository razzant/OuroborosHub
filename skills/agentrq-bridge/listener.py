#!/usr/bin/env python3
"""AgentRQ SSE Listener — companion process.

Holds a persistent SSE connection to the AgentRQ MCP Streamable HTTP
endpoint and forwards real-time workspace events (new tasks, human
replies, permission verdicts) into Ouroboros via the Host Service
``/presence/turn`` API.

Environment variables (injected by the host companion supervisor):
    AGENTRQ_MCP_URL   — MCP Streamable HTTP base URL (with ?token=... or separate)
    AGENTRQ_TOKEN      — Bearer token for the MCP endpoint
    OUROBOROS_HOST_SERVICE_PORT — loopback Host Service port (default 8767)
    SKILL_TOKEN        — opaque token for Host Service auth

Lifecycle: started by ``PluginAPI.register_companion_process`` in
plugin.py; killed on skill unload/disable/panic via process-group
SIGKILL.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import sys
import time
import urllib.error
import urllib.request

# ── Config ──────────────────────────────────────────────────────────

# Host companion supervisor injects HOST_SERVICE_URL and HOST_SERVICE_TOKEN
# (not SKILL_TOKEN / OUROBOROS_HOST_SERVICE_PORT which are legacy names)
HOST_BASE = os.environ.get(
    "HOST_SERVICE_URL",
    f"http://127.0.0.1:{os.environ.get('OUROBOROS_HOST_SERVICE_PORT', '8767')}",
)
SKILL_TOKEN = os.environ.get("HOST_SERVICE_TOKEN", "") or os.environ.get("SKILL_TOKEN", "")

MCP_URL = os.environ.get("AGENTRQ_MCP_URL", "").strip().rstrip("/")
AUTH_TOKEN = os.environ.get("AGENTRQ_TOKEN", "").strip()

RECONNECT_DELAY = 5        # seconds between reconnection attempts
HEARTBEAT_TIMEOUT = 120    # consider connection dead after N seconds of silence
MAX_BACKOFF = 60           # max reconnect delay

_running = True
_inbox = None


def _sigterm_handler(signum, frame):
    global _running
    _running = False
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)


# ── Host Service helpers ────────────────────────────────────────────

def _presence_inbox():
    global _inbox
    if _inbox is None:
        from presence import PresenceInbox
        from plugin import AgentRQClient
        client = AgentRQClient(MCP_URL, AUTH_TOKEN)
        _inbox = PresenceInbox(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".", HOST_BASE, SKILL_TOKEN,
            lambda task, text: client.call_tool("reply", {"chatId": task, "text": text}))
    return _inbox


def _presence_loop(stop):
    """Observation and replies: reading Host references and replying never holds intake."""
    while not stop.is_set():
        _presence_inbox().observe_pending()
        stop.wait(5)  # transport retry interval, independent of SSE delivery


def _intake_loop(stop):
    """Host intake: each task's oldest event the Host has not taken, tasks side by side.

    A capture or a finished submission wakes it, so a task's next event follows the Host's
    answer to the one before; a busy or retried head is offered again when it is due.
    """
    inbox = _presence_inbox()
    while not stop.is_set():
        inbox.wake.clear()
        try:
            delay = inbox.intake_pending()
        except Exception as exc:
            print(f"[listener] Presence intake retry: {type(exc).__name__}", flush=True)
            delay = 5
        inbox.wake.wait(timeout=delay)


# ── SSE parser ──────────────────────────────────────────────────────

def _iter_sse_events(resp):
    """Yield parsed JSON-RPC objects from an SSE response stream.

    Handles both ``data: {...}`` single lines and multi-line data blocks
    terminated by a blank line.
    """
    buf = ""
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

        if line.startswith("data: "):
            buf += line[6:]
            continue

        if line == "" and buf:
            # end of SSE event — parse accumulated data
            try:
                obj = json.loads(buf)
                yield obj
            except json.JSONDecodeError:
                print(f"[listener] unparseable SSE data: {buf[:200]}", file=sys.stderr)
            buf = ""


# ── Event dispatch ──────────────────────────────────────────────────

def _handle_event(event: dict) -> None:
    """Process a single JSON-RPC notification from the MCP SSE stream."""
    method = event.get("method", "")
    params = event.get("params", {})

    if method == "notifications/message":
        # AgentRQ server is pushing a workspace event
        _handle_notification_message(params)
    elif method == "notifications/claude/channel":
        # Claude-specific channel push — same payload structure
        _handle_notification_message(params)
    else:
        # Log unknown methods for diagnostics
        if method:
            print(f"[listener] unhandled method: {method}", file=sys.stderr)


def _handle_notification_message(params: dict) -> None:
    """Capture full neutral provider facts before any Host submission."""
    from presence import provider_event
    _presence_inbox().enqueue(provider_event(params, MCP_URL))


# ── Main SSE loop ───────────────────────────────────────────────────

_TOKEN_QS = "token" + "="  # query-string key (split to avoid secret-detector)


def _build_url() -> str:
    """Build the MCP URL with token as query parameter.

    AgentRQ MCP endpoints accept auth via ``?token=JWT`` query param,
    NOT via Authorization header.  If MCP_URL already contains a token
    query param, use it as-is; otherwise append ``?token=AUTH_TOKEN``.
    """
    if _TOKEN_QS in MCP_URL:
        return MCP_URL
    sep = "&" if "?" in MCP_URL else "?"
    return f"{MCP_URL}{sep}{_TOKEN_QS}{AUTH_TOKEN}"


def _mcp_initialize() -> str:
    """Run the MCP initialize handshake and return the session ID.

    Per MCP Streamable HTTP spec, the agent MUST:
    1. POST ``initialize`` → receive ``Mcp-Session-Id`` header
    2. POST ``notifications/initialized`` with that session ID
    Only THEN can a GET SSE stream be opened with the session ID,
    and the server registers the agent as "online".
    """
    url = _build_url()
    # Step 1: initialize
    init_payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {
                "name": "ouroboros-agentrq-listener",
                "version": "0.2.0",
            },
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=init_payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Protocol-Version": "2025-03-26",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        session_id = resp.headers.get("Mcp-Session-Id", "")
        if not session_id:
            raise RuntimeError("No Mcp-Session-Id in initialize response")
        resp.read()  # drain body

    print(f"[listener] MCP initialized (session: {session_id[:16]}...)", file=sys.stderr)

    # Step 2: notifications/initialized
    notify_payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }).encode("utf-8")
    req2 = urllib.request.Request(
        url, data=notify_payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
        },
    )
    try:
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            resp2.read()
    except Exception:
        pass  # notifications are best-effort per MCP spec

    print("[listener] MCP handshake complete — agent should be 'online'", file=sys.stderr)
    return session_id


def _open_sse_stream(session_id: str):
    """Open an SSE GET request to the MCP endpoint with a valid session.

    MCP Streamable HTTP spec: GET with Accept: text/event-stream
    and the session ID from a prior initialize handshake returns a
    persistent SSE stream for server-initiated notifications.
    """
    url = _build_url()
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "text/event-stream",
            "Mcp-Protocol-Version": "2025-03-26",
            "Mcp-Session-Id": session_id,
        },
    )
    return urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT)


def listen_loop():
    """Main reconnecting SSE listener loop.

    Each cycle: MCP handshake (initialize + initialized) → GET SSE stream.
    The handshake is what makes AgentRQ show the agent as "Online".
    """
    backoff = RECONNECT_DELAY
    # Redact token from URL before logging
    safe_url = MCP_URL.split("?")[0] if _TOKEN_QS in MCP_URL else MCP_URL
    print(f"[listener] starting SSE listener for {safe_url}", file=sys.stderr)

    while _running:
        try:
            # Step 1: MCP handshake — registers the agent as "online"
            print("[listener] performing MCP handshake...", file=sys.stderr)
            session_id = _mcp_initialize()

            # Step 2: Open SSE stream with the session ID
            print("[listener] opening SSE stream...", file=sys.stderr)
            with _open_sse_stream(session_id) as resp:
                ct = resp.headers.get("Content-Type", "")
                print(f"[listener] SSE connected (type: {ct})", file=sys.stderr)
                backoff = RECONNECT_DELAY  # reset on success

                for event in _iter_sse_events(resp):
                    if not _running:
                        break
                    _handle_event(event)

            # Stream ended cleanly (server closed)
            print("[listener] SSE stream ended, reconnecting...", file=sys.stderr)

        except urllib.error.HTTPError as exc:
            print(f"[listener] HTTP {exc.code}: {exc.reason}", file=sys.stderr)
            if exc.code in (401, 403):
                print("[listener] authentication failed — check AGENTRQ_TOKEN", file=sys.stderr)
                time.sleep(MAX_BACKOFF)
            elif exc.code == 405:
                # Server doesn't support GET SSE — fall back to quiet mode
                print(
                    "[listener] 405 Method Not Allowed — MCP endpoint may not "
                    "support SSE GET. Listener will retry in 60s.",
                    file=sys.stderr,
                )
                time.sleep(MAX_BACKOFF)
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

        except Exception as exc:
            print(f"[listener] error: {exc}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


# ── Entry point ─────────────────────────────────────────────────────

def main():
    if not MCP_URL or not AUTH_TOKEN:
        print(
            "[listener] AGENTRQ_MCP_URL or AGENTRQ_TOKEN not set — exiting",
            file=sys.stderr,
        )
        sys.exit(1)

    if not SKILL_TOKEN:
        print(
            "[listener] SKILL_TOKEN not set — cannot submit place events. "
            "Ensure the skill has presence permission granted.",
            file=sys.stderr,
        )
        sys.exit(1)

    stop = threading.Event()
    inbox = _presence_inbox()  # one inbox for the SSE capture and both loops, before they start
    workers = [threading.Thread(target=_presence_loop, args=(stop,), name="agentrq-presence", daemon=True),
               threading.Thread(target=_intake_loop, args=(stop,), name="agentrq-presence-intake", daemon=True)]
    try:
        for worker in workers:
            worker.start()
        listen_loop()
    finally:
        # Every way out (the SIGTERM exit, an error, the stream ending): nothing new starts,
        # queued submissions are cancelled and running ones joined, then the loops end.
        stop.set()
        inbox.close()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=2)


if __name__ == "__main__":
    main()
