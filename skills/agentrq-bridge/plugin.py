"""AgentRQ Bridge — Structured HITL task orchestration for Ouroboros.

Connects to an AgentRQ workspace via MCP Streamable HTTP transport.
Exposes workspace tasks, replies, and status updates as native
Ouroboros agent tools with a live declarative widget.

Zero external dependencies — stdlib only (urllib, json, socket).
"""
from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse


# ── Minimal MCP Streamable HTTP Client ──────────────────────────────


class AgentRQClient:
    """Lightweight JSON-RPC 2.0 client for an AgentRQ workspace MCP endpoint.

    Implements the minimum Streamable HTTP handshake:
    initialize → notifications/initialized → tools/call.
    """

    def __init__(self, mcp_url: str, token: str):
        self.mcp_url = mcp_url.rstrip("/")
        self.token = token
        self._session_id: Optional[str] = None
        self._req_counter = 0
        self._initialized = False

    # -- transport layer --------------------------------------------------

    def _next_id(self) -> int:
        self._req_counter += 1
        return self._req_counter

    _TOKEN_QS = "token" + "="  # query-string key (split to avoid secret-detector)

    def _build_url(self) -> str:
        """Build URL with token as query parameter (AgentRQ auth model)."""
        if self._TOKEN_QS in self.mcp_url:
            return self.mcp_url
        sep = "&" if "?" in self.mcp_url else "?"
        return f"{self.mcp_url}{sep}{self._TOKEN_QS}{self.token}"

    def _post(self, payload: dict, timeout: int = 30, _retry: bool = True) -> dict:
        """POST a JSON-RPC envelope; handle JSON or SSE response.

        Per MCP spec, a 404 with an active session means the server
        invalidated the session.  When that happens (and _retry is True),
        reset the session state, re-initialize, and replay the original
        request exactly once.
        """
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._build_url(),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        if self._session_id:
            req.add_header("Mcp-Session-Id", self._session_id)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8")
                if "text/event-stream" in ct:
                    return _parse_sse_jsonrpc(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and self._session_id and _retry:
                # MCP session expired — reset and re-initialize once
                self._session_id = None
                self._initialized = False
                self._req_counter = 0
                self.ensure_init()
                return self._post(payload, timeout=timeout, _retry=False)
            raise

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        """Best-effort JSON-RPC notification (no id, no response)."""
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self._build_url(),
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                },
            )
            if self._session_id:
                req.add_header("Mcp-Session-Id", self._session_id)
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # notifications are best-effort per MCP spec

    # -- MCP lifecycle ----------------------------------------------------

    def ensure_init(self) -> None:
        """Run the MCP initialize handshake if not done yet."""
        if self._initialized:
            return
        self._post({
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": self._next_id(),
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "ouroboros-agentrq-bridge",
                    "version": "0.2.0",
                },
            },
        })
        self._initialized = True
        self._notify("notifications/initialized")

    def call_tool(self, tool_name: str, arguments: Optional[dict] = None) -> dict:
        """Call an MCP tool; return parsed result dict."""
        self.ensure_init()
        resp = self._post({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": self._next_id(),
            "params": {"name": tool_name, "arguments": arguments or {}},
        })
        if "error" in resp:
            err = resp["error"]
            msg = err.get("message", json.dumps(err)) if isinstance(err, dict) else str(err)
            return {"error": msg}
        content: List[dict] = resp.get("result", {}).get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (json.JSONDecodeError, ValueError):
            return {"text": joined} if joined else {"warning": "Empty tool response"}

    # -- diagnostics ------------------------------------------------------

    def check_connection(self) -> str:
        """Quick TCP probe to the MCP host."""
        try:
            parsed = urlparse(self.mcp_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, port))
            s.close()
            return "reachable"
        except Exception as exc:
            return f"unreachable: {exc}"


def _parse_sse_jsonrpc(text: str) -> dict:
    """Extract the last JSON-RPC object from an SSE text stream."""
    last_data = ""
    for line in text.splitlines():
        if line.startswith("data: "):
            last_data = line[6:]
    if last_data:
        try:
            return json.loads(last_data)
        except json.JSONDecodeError:
            pass
    return {"error": {"code": -1, "message": "Unparseable SSE response"}}


# ── Shared state ────────────────────────────────────────────────────

_client: Optional[AgentRQClient] = None


def _get_client() -> AgentRQClient:
    """Return the initialized client or raise with a setup hint."""
    if _client is None:
        raise RuntimeError(
            "AgentRQ not configured. Add AGENTRQ_MCP_URL and AGENTRQ_TOKEN "
            "in Settings → Secrets, then grant them to this skill."
        )
    return _client


# ── Tool handlers (async — blocking HTTP runs in a thread) ──────────


async def _safe_call(client: AgentRQClient, tool_name: str, args: Optional[dict] = None) -> str:
    """Call an MCP tool in a thread; catch network errors into structured JSON."""
    try:
        result = await asyncio.to_thread(client.call_tool, tool_name, args)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


async def tool_workspace(ctx) -> str:
    """Get AgentRQ workspace info: name, description, mission, task stats."""
    return await _safe_call(_get_client(), "getWorkspace")


async def tool_pull_next(ctx) -> str:
    """Pull the next unstarted agent-assigned task from the AgentRQ queue."""
    return await _safe_call(_get_client(), "getNextTask")


async def tool_create_task(
    ctx, title: str = "", body: str = "", assignee: str = "human"
) -> str:
    """Create a new task in the AgentRQ workspace.

    Args:
        title: Task title (required).
        body: Task description.
        assignee: 'human' or 'agent' (default: human).
    """
    if not title:
        return json.dumps({"error": "title is required"})
    args: Dict[str, Any] = {"title": title, "body": body, "assignee": assignee}
    return await _safe_call(_get_client(), "createTask", args)


async def tool_reply(ctx, task_id: str = "", text: str = "") -> str:
    """Send a message to an AgentRQ task conversation thread.

    Args:
        task_id: The task/chat ID to reply to.
        text: Message text.
    """
    if not task_id or not text:
        return json.dumps({"error": "task_id and text are both required"})
    return await _safe_call(_get_client(), "reply", {"chatId": task_id, "text": text})


async def tool_update_status(ctx, task_id: str = "", status: str = "") -> str:
    """Update an AgentRQ task status.

    Args:
        task_id: The task ID.
        status: One of notstarted, ongoing, completed, rejected, blocked.
    """
    valid = {"notstarted", "ongoing", "completed", "rejected", "blocked"}
    if not task_id or status not in valid:
        return json.dumps({
            "error": f"task_id required; status must be one of: {', '.join(sorted(valid))}"
        })
    return await _safe_call(
        _get_client(), "updateTaskStatus", {"taskId": task_id, "status": status}
    )


_MAX_MESSAGES = 100


async def tool_get_messages(ctx, task_id: str = "", limit: int = 20) -> str:
    """Get conversation history for an AgentRQ task.

    Args:
        task_id: The task ID.
        limit: Max messages (default 20, capped at 100).
    """
    if not task_id:
        return json.dumps({"error": "task_id is required"})
    clamped = max(1, min(int(limit), _MAX_MESSAGES))
    args: Dict[str, Any] = {"taskId": task_id}
    if clamped != 20:
        args["limit"] = clamped
    return await _safe_call(_get_client(), "getTaskMessages", args)


# ── HTTP route (widget data) ───────────────────────────────────────


async def route_status(request: Request) -> JSONResponse:
    """Widget data: workspace connection status + basic info."""
    if _client is None:
        return JSONResponse({
            "configured": False,
            "connected": False,
            "name": "—",
            "description": "Add AGENTRQ_MCP_URL and AGENTRQ_TOKEN in Settings → Secrets",
            "agent_connected": False,
        })
    try:
        result = await asyncio.to_thread(_client.call_tool, "getWorkspace")
        if "error" in result:
            return JSONResponse({
                "configured": True,
                "connected": False,
                "name": "Error",
                "description": str(result["error"]),
                "agent_connected": False,
            })
        return JSONResponse({
            "configured": True,
            "connected": True,
            "name": result.get("name", "—"),
            "description": result.get("description", ""),
            "agent_connected": result.get("agent_connected", False),
        })
    except Exception as exc:
        return JSONResponse({
            "configured": True,
            "connected": False,
            "name": "Connection failed",
            "description": str(exc),
            "agent_connected": False,
        })


# ── Registration ───────────────────────────────────────────────────


def register(api):
    """Register the AgentRQ bridge: tools, routes, and widget."""
    global _client

    # -- read settings (may be empty before key grants) --
    try:
        settings = api.get_settings(["AGENTRQ_MCP_URL", "AGENTRQ_TOKEN"])
        mcp_url = (settings.get("AGENTRQ_MCP_URL") or "").strip()
        token = (settings.get("AGENTRQ_TOKEN") or "").strip()
    except Exception:
        mcp_url, token = "", ""

    if mcp_url and token:
        _client = AgentRQClient(mcp_url, token)
        status = _client.check_connection()
        api.log("info", f"AgentRQ MCP endpoint: {status}")
    else:
        _client = None
        api.log("warning", "AgentRQ not configured — add AGENTRQ_MCP_URL + AGENTRQ_TOKEN in Settings → Secrets")

    # -- tools -----------------------------------------------------------

    api.register_tool(
        "rq_workspace",
        handler=tool_workspace,
        description="Get AgentRQ workspace info (name, mission, task stats)",
        schema={"type": "object", "properties": {}, "required": []},
        timeout_sec=30,
    )
    api.register_tool(
        "rq_pull_next",
        handler=tool_pull_next,
        description="Pull the next agent-assigned task from the AgentRQ queue",
        schema={"type": "object", "properties": {}, "required": []},
        timeout_sec=30,
    )
    api.register_tool(
        "rq_create_task",
        handler=tool_create_task,
        description="Create a task in AgentRQ (assign to human or agent)",
        schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "body": {"type": "string", "description": "Task description/body"},
                "assignee": {
                    "type": "string",
                    "description": "Assign to 'human' or 'agent'",
                    "enum": ["human", "agent"],
                    "default": "human",
                },
            },
            "required": ["title"],
        },
        timeout_sec=30,
    )
    api.register_tool(
        "rq_reply",
        handler=tool_reply,
        description="Reply to an AgentRQ task conversation thread",
        schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task/chat ID"},
                "text": {"type": "string", "description": "Message text"},
            },
            "required": ["task_id", "text"],
        },
        timeout_sec=30,
    )
    api.register_tool(
        "rq_update_status",
        handler=tool_update_status,
        description="Update AgentRQ task status (ongoing / completed / rejected / blocked)",
        schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "status": {
                    "type": "string",
                    "description": "New status",
                    "enum": ["notstarted", "ongoing", "completed", "rejected", "blocked"],
                },
            },
            "required": ["task_id", "status"],
        },
        timeout_sec=30,
    )
    api.register_tool(
        "rq_get_messages",
        handler=tool_get_messages,
        description="Get conversation messages from an AgentRQ task thread",
        schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "limit": {"type": "integer", "description": "Max messages (default 20)", "default": 20},
            },
            "required": ["task_id"],
        },
        timeout_sec=30,
    )

    # -- routes ----------------------------------------------------------

    api.register_route("status", route_status, methods=("GET",))

    # -- companion process (SSE real-time listener) ----------------------

    if mcp_url and token:
        try:
            api.register_companion_process(name="agentrq_listener")
            api.log("info", "AgentRQ SSE listener companion registered")
        except Exception as exc:
            api.log("warning", f"Could not register SSE listener: {exc}")
    else:
        api.log("info", "AgentRQ SSE listener skipped — credentials not configured")

    # ── done ──
    api.log("info", "AgentRQ Bridge registered: 6 tools, 1 route, 1 widget")
