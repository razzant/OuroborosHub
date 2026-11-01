from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import inspect
import ipaddress
import threading
import time
import urllib.parse
import uuid
import sys
import base64
from typing import Any, Dict, List

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Companion scripts are also loaded directly by the Hub's isolated tests.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from a2a_presence import (PresenceTransport, HostWaitExpired as _HostWaitExpired,
                          HostWorkCancelled as _HostWorkCancelled,
                          HostMessageConflict as _HostMessageConflict)

try:
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.events.event_queue import EventQueue
    from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.server.tasks.task_updater import TaskUpdater
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentSkill,
        Artifact,
        Part,
        Role,
        TaskState,
    )
    from a2a.utils.errors import TaskNotCancelableError
    _A2A_SDK_AVAILABLE = True
except Exception:
    _A2A_SDK_AVAILABLE = False

    class TaskNotCancelableError(RuntimeError):  # type: ignore[no-redef]
        """The SDK's refusal shape (same constructor), for the no-SDK fallback."""

        def __init__(self, message: "str | None" = None, data: "dict | None" = None) -> None:
            self.message = message or "Task cannot be canceled"
            self.data = data
            super().__init__(self.message)

logger = logging.getLogger("a2a_daemon")

STATE_DIR = pathlib.Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".")

# Card version — kept in step with the skill version (SKILL.md / catalog entry).
A2A_CARD_VERSION = "1.5.0"

# JSON-RPC error code the A2A spec assigns to TaskNotCancelableError.
_JSONRPC_TASK_NOT_CANCELABLE = -32002
_SUPERSEDED_CANCEL_MESSAGE = (
    "The previous host operation was cancelled, but the task now belongs to another message; "
    "the current operation was not cancelled."
)
_HOST_LOST_MESSAGE = "the host restarted before answering; the message was not carried over"


def _is_loopback(host: str) -> bool:
    clean = str(host or "").strip().strip("[]")
    if clean == "localhost":
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def _host_service_hostname(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        raise RuntimeError("HOST_SERVICE_URL must not contain userinfo")
    return parsed.hostname or ""


HOST_SERVICE_URL = os.environ.get("HOST_SERVICE_URL", "http://127.0.0.1:8767").rstrip("/")


class _SkillToken:
    """Companion-side SkillToken wrapper: prevents accidental logging of the raw token."""

    __slots__ = ("_value",)

    def __init__(self, raw: str) -> None:
        self._value = raw

    def use_in_request(self) -> str:
        """Deliberate access at request construction sites."""
        return self._value

    def __str__(self) -> str:
        return "<SkillToken:redacted>"

    def __repr__(self) -> str:
        return "<SkillToken:redacted>"


_HOST_TOKEN = _SkillToken(os.environ.get("HOST_SERVICE_TOKEN", ""))

# Enforce loopback-only Host Service calls (checklist item 12: host_token_handling)
if not _is_loopback(_host_service_hostname(HOST_SERVICE_URL)):
    raise RuntimeError(
        "HOST_SERVICE_URL must be a loopback address; "
        "refusing to send skill token to a non-local endpoint"
    )


def _load_settings() -> Dict[str, Any]:
    path = STATE_DIR / "settings.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # A malformed or unreadable settings file must not be indistinguishable
        # from "never configured": defaults still apply, but say why.
        logger.warning("a2a settings: %s is unreadable (%s); using defaults", path, exc)
        return {}
    if not isinstance(document, dict):
        logger.warning(
            "a2a settings: %s is valid JSON but not an object (%s); using defaults",
            path,
            type(document).__name__,
        )
        return {}
    return document


_SETTINGS = _load_settings()


def _setting_int(name: str, default: int, *, minimum: int = 1, maximum: int = 600) -> int:
    """Bounded int setting.

    Defined here, ABOVE its first use: a bare int() conversion at module scope
    raised on one malformed owner value before the `app` object existed, which
    crash-looped the host-supervised companion with no server left to report why.
    An unparseable or out-of-range value is clamped and logged instead; the typed
    rejection belongs at the WRITE boundary (plugin.py's settings route), where an
    operator can see which field was refused.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        raw = _SETTINGS.get(name)
    try:
        value = int(raw if raw is not None and str(raw).strip() != "" else default)
    except (TypeError, ValueError):
        logger.warning(
            "a2a settings: %s=%r is not an integer; using default %d", name, raw, default
        )
        value = default
    bounded = max(minimum, min(maximum, value))
    if bounded != value:
        logger.warning(
            "a2a settings: %s=%d is outside %d..%d; clamped to %d",
            name, value, minimum, maximum, bounded,
        )
    return bounded


A2A_HOST = os.environ.get("A2A_HOST") or str(_SETTINGS.get("A2A_HOST") or "127.0.0.1")
# A port is 1..65535. Reaching uvicorn with an out-of-range value prevented the
# companion from starting at all.
A2A_PORT = _setting_int("A2A_PORT", 18800, minimum=1, maximum=65535)

# Distinguish an OPERATOR-SET name/description from the built-in default. When the
# operator explicitly configures A2A_AGENT_NAME / A2A_AGENT_DESCRIPTION (env or
# settings), that value WINS over the live /identity value. When neither is set,
# the card is populated from the host /identity endpoint, falling back to these
# defaults only if /identity is unavailable.
_A2A_AGENT_NAME_EXPLICIT = (os.environ.get("A2A_AGENT_NAME") or str(_SETTINGS.get("A2A_AGENT_NAME") or "")).strip()
_A2A_AGENT_DESCRIPTION_EXPLICIT = (os.environ.get("A2A_AGENT_DESCRIPTION") or str(_SETTINGS.get("A2A_AGENT_DESCRIPTION") or "")).strip()
A2A_AGENT_NAME = _A2A_AGENT_NAME_EXPLICIT or "Ouroboros"
A2A_AGENT_DESCRIPTION = _A2A_AGENT_DESCRIPTION_EXPLICIT or "Ouroboros A2A peer"
A2A_SERVER_PASSWORD = (os.environ.get("A2A_SERVER_PASSWORD") or str(_SETTINGS.get("A2A_SERVER_PASSWORD") or "")).strip()

# Bounded retry for the host tool-schema fetch: the companion routinely starts
# before the host chat-agent is built, so GET /tools/schemas answers 200 with an
# EMPTY tools list for the first few seconds. An empty 200 must be retried like a
# transient failure (not accepted as final), or the card collapses to the
# identity-only entry. The window is generous enough to survive that startup race.
_TOOLS_FETCH_ATTEMPTS = 8
_TOOLS_FETCH_BACKOFF_SEC = 0.5
_HOST_FETCH_TIMEOUT_SEC = 5

# Last non-empty tool list seen this process. Once the card has populated, a later
# transient empty fetch must never regress it back to a tool-less card
# (self-healing stability across per-request rebuilds and the startup bake).
_LAST_GOOD_TOOLS: List[Dict[str, Any]] = []

# In-request fetch is SHORT: a discovery GET must answer in seconds, not ride the
# full warmup ladder (a cold host once held /.well-known/agent-card.json for ~57s).
# The full ladder lives in the background refresher started with the app.
_TOOLS_FETCH_ATTEMPTS_REQUEST = 2

# Curated capability categories for the card's skill list. These are STABLE core
# capabilities of every Ouroboros install, so the card stays honest and USEFUL for
# integrators even while the host tool registry is warming up or unavailable —
# the two prior regressions were a contentless "General" stub and an identity-only
# persona entry, both useless to a consumer that reads capabilities off the card.
_CORE_CAPABILITY_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "code-and-files",
        "name": "Code and files",
        "description": (
            "Read, search, edit and run code; structural code queries (definitions, "
            "callers, impact), git operations, cherry-picks and code review."
        ),
        "tags": ["code", "files", "git"],
    },
    {
        "id": "web-and-media",
        "name": "Web and media",
        "description": (
            "Web search, open pages in a real browser, screenshots, PDFs, images, "
            "video frame extraction and YouTube subtitles."
        ),
        "tags": ["web", "media", "research"],
    },
    {
        "id": "long-running-tasks",
        "name": "Long-running tasks and projects",
        "description": (
            "Background tasks with live progress across multiple rounds, and named "
            "projects with a durable journal and memory."
        ),
        "tags": ["tasks", "projects", "background"],
    },
    {
        "id": "self-modification",
        "name": "Self-modification",
        "description": (
            "Edits its own code, prompts and configuration, landing changes through a "
            "mandatory multi-model review and restart cycle."
        ),
        "tags": ["self-modification", "evolution"],
    },
    {
        "id": "skills-extensions",
        "name": "Skills (extensions)",
        "description": (
            "Installable skills extend the agent (A2A bridge, desktop observation and "
            "control, and more); per-tool entries below reflect what is enabled live."
        ),
        "tags": ["skills", "extensions"],
    },
]

# Capability-first top-level description. The identity file's own first line is
# appended as flavor — personality belongs on the card, but never INSTEAD of the
# capability summary an integrator consumes.
_CAPABILITY_LEAD = (
    "General-purpose autonomous AI agent: code and files (including git and code "
    "review), web research and media, long-running background tasks and projects, "
    "reviewed self-modification, and installable skills."
)


A2A_MAX_CONCURRENT = _setting_int("A2A_MAX_CONCURRENT", 5, minimum=1, maximum=20)
# The Presence turn wait is bounded at 1800s; stay under it (plus the httpx
# margin) so the daemon, not a socket, always decides what a wait expiry means.
A2A_RESPONSE_TIMEOUT_SEC = _setting_int("A2A_RESPONSE_TIMEOUT_SEC", 600, minimum=1, maximum=1740)
# The old hardcoded 5s allocate timeout was the source of spurious "timed out"
# stream errors on a busy host — allocation is cheap but the host polls at 1s.
A2A_ALLOCATE_TIMEOUT_SEC = _setting_int("A2A_ALLOCATE_TIMEOUT_SEC", 30, minimum=1, maximum=120)
# Total wait for one inbound message including deferred work polling.
# Closing this wait never cancels the Host task.
A2A_STREAM_DEADLINE_SEC = _setting_int("A2A_STREAM_DEADLINE_SEC", 3600, minimum=60, maximum=21600)
# Cross-validate the two independently-bounded waits. The deadline is only checked
# AFTER _inject_sync returns, so a response timeout longer than the total deadline
# (e.g. 1740s against a 60s deadline) blocks for the full socket wait and then
# gives up immediately — defeating the deadline it was supposed to respect.
if A2A_RESPONSE_TIMEOUT_SEC + 15 > A2A_STREAM_DEADLINE_SEC:
    _clamped_response_timeout = max(1, A2A_STREAM_DEADLINE_SEC - 15)
    logger.warning(
        "a2a settings: A2A_RESPONSE_TIMEOUT_SEC=%d exceeds A2A_STREAM_DEADLINE_SEC=%d "
        "(minus the 15s socket margin); clamping the response wait to %d",
        A2A_RESPONSE_TIMEOUT_SEC,
        A2A_STREAM_DEADLINE_SEC,
        _clamped_response_timeout,
    )
    A2A_RESPONSE_TIMEOUT_SEC = _clamped_response_timeout

A2A_PROGRESS_POLL_SEC = _setting_int("A2A_PROGRESS_POLL_SEC", 3, minimum=1, maximum=60)
A2A_HEARTBEAT_SEC = _setting_int("A2A_HEARTBEAT_SEC", 25, minimum=5, maximum=300)


_A2A_SEMAPHORE = None

def _get_semaphore() -> asyncio.Semaphore:
    global _A2A_SEMAPHORE
    if _A2A_SEMAPHORE is None:
        _A2A_SEMAPHORE = asyncio.Semaphore(A2A_MAX_CONCURRENT)
    return _A2A_SEMAPHORE


def _host_headers() -> Dict[str, str]:
    return {"X-Skill-Token": _HOST_TOKEN.use_in_request()}


class _A2AAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_loopback(A2A_HOST):
            return await call_next(request)
        expected = "Basic " + base64.b64encode(f"ouroboros:{A2A_SERVER_PASSWORD}".encode()).decode()
        if not A2A_SERVER_PASSWORD or request.headers.get("authorization") != expected:
            return JSONResponse({"error": "A2A authentication required"}, status_code=401)
        return await call_next(request)


def _tasks_dir() -> pathlib.Path:
    path = STATE_DIR / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


class _TaskStateCorrupt(RuntimeError):
    """A task record exists but is unreadable or is not a JSON object."""


def _task_path(task_id: str) -> pathlib.Path:
    """Map a task id to a state file INJECTIVELY.

    Stripping every character outside [A-Za-z0-9_-] is a lossy mapping: distinct
    ids such as "job/a" and "joba" collapsed onto one filename, so one peer's task
    could overwrite or return another's record. The sha256 of the COMPLETE id is
    the identity; the sanitized prefix is retained only to keep the directory
    human-readable. An empty id now maps deterministically as well — the previous
    uuid fallback meant _load_task("") could never find what _save_task wrote.
    """
    raw = str(task_id or "")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    prefix = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))[:48]
    return _tasks_dir() / (f"{prefix}-{digest}.json" if prefix else f"{digest}.json")


def _save_task(task: Dict[str, Any]) -> None:
    """Atomically replace the existing task record; callers serialize merges."""
    path = _task_path(str(task["id"]))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_task(task_id: str) -> Dict[str, Any] | None:
    path = _task_path(task_id)
    if not path.exists():
        return None
    return _read_task_record(path, task_id)


def _read_task_record(path: pathlib.Path, expected_task_id: str | None = None) -> Dict[str, Any]:
    """Read one task file with the same typed failure for lookup and listing."""
    label = expected_task_id if expected_task_id is not None else path.name
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # A present-but-unreadable record is NOT "task not found": report it as an
        # actionable task-state failure instead of letting a bare decode error
        # escape as an opaque server error.
        raise _TaskStateCorrupt(f"task state for {label!r} is unreadable: {exc}") from exc
    if not isinstance(record, dict):
        raise _TaskStateCorrupt(f"task state for {label!r} is not a JSON object")
    task_id = record.get("id")
    if (not isinstance(task_id, str) or (expected_task_id is not None and task_id != expected_task_id)
            or _task_path(task_id) != path):
        raise _TaskStateCorrupt(f"task state for {label!r} has a different identity")
    return record


_TASK_RECORD_LOCK = threading.RLock()


# --- host operation binding (#667) ----------------------------------------
#
# One inbound A2A message maps to ONE host operation. The message identity the
# host is told (``client_message_id``) is deterministic in the A2A task and
# message ids, so an SDK retry or a reconnect re-delivering the same message
# REJOINS the accepted host work instead of starting a second turn; the host's
# ``operation_ref`` is that same identity behind the allocated chat id. The
# binding lives in the skill's existing durable task record (shared by the SDK
# executor and the no-SDK fallback), which is what ``tasks/cancel`` reads.


def _client_message_id(task_id: str, message_id: str) -> str:
    identity = json.dumps([task_id, message_id or "message"], ensure_ascii=False, separators=(",", ":"))
    return "a2a:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _operation_ref(chat_id: int, client_message_id: str) -> str:
    """The host's documented ref spelling: the identity the caller supplied."""
    return client_message_id


def _update_task_record(
    task_id: str,
    context_id: str = "",
    *,
    binding: "Dict[str, Any] | None" = None,
    state: str = "",
    message: str = "",
    artifacts: "List[Dict[str, Any]] | None" = None,
    expected_message_id: str = "",
) -> Dict[str, Any]:
    """Merge through one current-message fence for both disk and returned views."""
    with _TASK_RECORD_LOCK:
        record = _load_task(task_id) or {}
        bound = dict(record.get("ouroboros") or {})
        current = str(bound.get("client_message_id") or "")
        if expected_message_id and current and expected_message_id != current:
            return record  # Neither persistence nor its returned projection may mix messages.
        record.setdefault("id", task_id)
        record.setdefault("contextId", context_id or task_id)
        record.setdefault("status", {"state": "working"})
        if binding:
            incoming = str(binding.get("client_message_id") or "")
            seen = list(bound.get("message_ids") or ([current] if current else []))
            if incoming not in seen:
                seen.append(incoming)
                if binding.get("protocol") == "presence" and bound.get("chat_id"):
                    bound["legacy_message_ids"] = list(seen[:-1])
                    bound.pop("chat_id", None)
                bound.update(binding)
                bound["message_ids"] = seen
                record["status"] = {"state": "working"}
                record.pop("artifacts", None)
            record["ouroboros"] = bound
        if state:
            record["status"] = {"state": state}
            if message:
                record["status"]["message"] = {"parts": [{"kind": "text", "text": message}]}
        if artifacts is not None:
            record["artifacts"] = artifacts
        _save_task(record)
        return record


def _bound_operation(task_id: str) -> Dict[str, Any]:
    record = _load_task(task_id) or {}
    bound = record.get("ouroboros")
    return dict(bound) if isinstance(bound, dict) else {}


class _HostCancelRefused(RuntimeError):
    """The host answered anything but ``cancelled``: the honest reason rides along."""

    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(f"host cancel outcome {outcome!r}: {reason}")
        self.outcome = outcome
        self.reason = reason


def _cancel_host_operation_sync(task_id: str) -> Dict[str, Any]:
    if not _bound_operation(task_id):
        raise _HostCancelRefused("unbound", "no host operation is bound to this task")
    raise _HostCancelRefused("cancel_unsupported",
        "the Presence protocol has no cancellation endpoint; host work is unchanged")


def _read_operation_sync(operation_ref: str):
    return _presence_transport().read(operation_ref) if operation_ref else None


def _refresh_task_record(task_id: str) -> "Dict[str, Any] | None":
    """Reconcile a saved task with its exact host operation after reconnect."""
    record = _load_task(task_id)
    if not record:
        return None
    bound = record.get("ouroboros") or {}
    view = _read_operation_sync(str(bound.get("operation_ref") or ""))
    states = {"completed": "completed", "cancelled": "canceled", "failed": "failed",
              "rejected_duplicate": "rejected", "lost": "failed"}
    state = states.get(str((view or {}).get("status") or ""))
    if state:
        text = _HOST_LOST_MESSAGE if view.get("status") == "lost" else str(view.get("text") or "")
        return _update_task_record(task_id, state=state, message=text if state != "completed" else "",
                                   artifacts=[{"parts": [{"kind": "text", "text": text}]}] if state == "completed" else None,
                                   expected_message_id=str(bound.get("client_message_id") or ""))
    return record


def _sdk_task_store():
    """Adapt the SDK to the SAME durable Hub task record used by the fallback."""
    from a2a.server.owner_resolver import resolve_user_scope
    from a2a.server.tasks.task_store import TaskStore
    from a2a.types import Message, Task
    from google.protobuf.json_format import MessageToDict, ParseDict

    states = {"submitted": TaskState.TASK_STATE_SUBMITTED, "working": TaskState.TASK_STATE_WORKING,
              "completed": TaskState.TASK_STATE_COMPLETED, "failed": TaskState.TASK_STATE_FAILED,
              "canceled": TaskState.TASK_STATE_CANCELED, "rejected": TaskState.TASK_STATE_REJECTED}

    def project_record(record, context):
        """Project a local snapshot without refreshing or changing its source."""
        if not record or record.get("sdk_owner", resolve_user_scope(context)) != resolve_user_scope(context):
            return None
        task_id = record["id"]
        task = ParseDict(record["sdk_task"], Task()) if record.get("sdk_task") else Task(id=task_id, context_id=record.get("contextId") or task_id)
        task.status.state = states.get(str((record.get("status") or {}).get("state") or ""), TaskState.TASK_STATE_WORKING)
        message = (record.get("status") or {}).get("message")
        if message:
            task.status.message.CopyFrom(Message(role=Role.ROLE_AGENT, message_id=uuid.uuid4().hex,
                parts=[Part(text=str(part.get("text") or "")) for part in message.get("parts", [])]))
        current = str((record.get("ouroboros") or {}).get("client_message_id") or "")
        source = next((m for m in reversed(task.history) if m.role == Role.ROLE_USER), None)
        if current and source and _client_message_id(task_id, source.message_id) != current:
            task.ClearField("artifacts")
        if not task.artifacts and record.get("artifacts"):
            for item in record["artifacts"]:
                task.artifacts.append(Artifact(artifact_id=uuid.uuid4().hex, parts=[Part(text=str(p.get("text") or "")) for p in item.get("parts", [])]))
        return task

    class DurableTaskStore(TaskStore):
        async def save(self, task, context):
            with _TASK_RECORD_LOCK:
                record = _load_task(task.id) or {"id": task.id, "contextId": task.context_id}
                owner = resolve_user_scope(context)
                if record.get("sdk_owner", owner) != owner:
                    raise _TaskStateCorrupt("SDK task belongs to another caller")
                current = str((record.get("ouroboros") or {}).get("client_message_id") or "")
                inbound = next((m for m in reversed(task.history) if m.role == Role.ROLE_USER), None)
                if current and inbound and _client_message_id(task.id, inbound.message_id) != current:
                    return
                record["sdk_owner"] = owner
                record["sdk_task"] = MessageToDict(task)
                state = next((key for key, value in states.items() if value == task.status.state), "working")
                if str((record.get("status") or {}).get("state") or "") not in _TERMINAL_RECORD_STATES:
                    record["status"] = {"state": state}
                _save_task(record)

        async def get(self, task_id, context):
            record = await asyncio.to_thread(_refresh_task_record, task_id)
            return project_record(record, context)

        async def list(self, params, context):
            # Reuse the SDK's filtering/pagination over a request-local view;
            # list reads stored snapshots; only get reconciles with the Host.
            records = await asyncio.to_thread(
                lambda: [_read_task_record(path) for path in _tasks_dir().glob("*.json")]
            )
            view = InMemoryTaskStore()
            for record in records:
                task = project_record(record, context)
                if task is not None:
                    await view.save(task, context)
            return await view.list(params, context)

        async def delete(self, task_id, context):
            with _TASK_RECORD_LOCK:
                record = _load_task(task_id)
                if record and record.get("sdk_owner", resolve_user_scope(context)) == resolve_user_scope(context):
                    _task_path(task_id).unlink(missing_ok=True)

    return DurableTaskStore()


def _sdk_request_handler(card):
    from google.protobuf.json_format import MessageToDict
    from a2a.server.request_handlers.request_handler import validate, validate_request_params
    from a2a.utils.task import apply_history_length, validate_history_length
    from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, UnsupportedOperationError
    from a2a.types import Message, SubscribeToTaskRequest, Task, TaskStatus
    terminal_states = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_CANCELED,
                       TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED}

    class OperationRequestHandler(LegacyRequestHandler):
        async def _run_event_stream(self, request, queue):
            # A superseded producer does not own the current task's queue.
            await self.agent_executor.execute(request, queue)
            if self._running_agents.get(request.task_id) is asyncio.current_task():
                await queue.close()

        async def _cleanup_producer(self, producer_task, task_id):
            try:
                await producer_task
            except asyncio.CancelledError:
                logger.debug("Producer task %s was cancelled during cleanup", task_id)
            # The SDK closes by task id; keep its existing owner lock across
            # identity validation, queue close and retirement.
            async with self._running_agents_lock:
                if self._running_agents.get(task_id) is not producer_task:
                    return
                await self._queue_manager.close(task_id)
                self._running_agents.pop(task_id, None)

        async def _same_message_replay(self, params, context):
            task_id = params.message.task_id
            task = await self.task_store.get(task_id, context) if task_id else None
            if task is None:
                return None
            original = next((m for m in reversed(task.history)
                             if m.message_id == params.message.message_id and m.role == Role.ROLE_USER), None)
            if original is None:
                return None  # NEW message retains the SDK's terminal-task guard.
            before, after = MessageToDict(original), MessageToDict(params.message)
            for body in (before, after):
                body.pop("taskId", None)
                body.pop("contextId", None)
            if before != after or (params.message.context_id and params.message.context_id != task.context_id):
                raise InvalidParamsError(message="messageId was already accepted with different content or context")
            expected = _client_message_id(task_id, params.message.message_id)
            bound = _bound_operation(task_id)
            if not bound or bound.get("client_message_id") == expected:
                return task
            # An old message must never receive current M2's terminal answer.
            # Reconcile M1 through the same host operation; do not write M2.
            view = await asyncio.to_thread(_read_operation_sync, expected)
            state = {"completed": TaskState.TASK_STATE_COMPLETED, "cancelled": TaskState.TASK_STATE_CANCELED,
                     "failed": TaskState.TASK_STATE_FAILED, "rejected_duplicate": TaskState.TASK_STATE_REJECTED,
                     "lost": TaskState.TASK_STATE_FAILED}.get((view or {}).get("status"))
            if state is None:
                raise InvalidParamsError(message="previous message outcome is not confirmed; current task is unchanged")
            status = TaskStatus(state=state)
            if view.get("status") == "lost":
                status.message.CopyFrom(Message(role=Role.ROLE_AGENT, message_id=uuid.uuid4().hex,
                                                parts=[Part(text=_HOST_LOST_MESSAGE)]))
            return Task(id=task_id, context_id=task.context_id, status=status, history=[original],
                        artifacts=[Artifact(artifact_id=uuid.uuid4().hex, parts=[Part(text=str(view.get("text") or ""))])]
                        if state == TaskState.TASK_STATE_COMPLETED else [])

        async def _replay_events(self, params, context, task):
            if task.status.state in terminal_states:
                yield task
                return
            # Reuse the SDK subscription/aggregator owner without creating an
            # executor or replacing its running-agent entry/context identity.
            if await self._queue_manager.get(task.id) is not None:
                try:
                    async for event in super().on_subscribe_to_task(SubscribeToTaskRequest(id=task.id), context):
                        yield event
                    yield await self._same_message_replay(params, context)
                    return
                except (TaskNotFoundError, UnsupportedOperationError):
                    pass  # Queue completion raced the tap; reconcile below.
            yield task
            bound = _bound_operation(task.id)
            expected = _client_message_id(task.id, params.message.message_id)
            if bound.get("protocol") != "presence":
                task.status.message.CopyFrom(Message(role=Role.ROLE_AGENT, message_id=uuid.uuid4().hex,
                                                     parts=[Part(text="The host operation binding is unavailable; delivery outcome is unknown.")]))
                yield task
                return
            ref = expected
            try:
                answer = await asyncio.to_thread(_wait_final_after_expiry_sync, bound.get("conversation_id", ""),
                                                time.monotonic() + A2A_STREAM_DEADLINE_SEC, ref)
                _update_task_record(task.id, state="completed", expected_message_id=expected,
                                    artifacts=[{"parts": [{"kind": "text", "text": answer}]}])
            except _HostWaitExpired as exc:
                # The reconnect wait ended, not the host operation. Return its
                # nonterminal Task and explicit uncertainty without writing a
                # false failed/completed state or starting another producer.
                task.status.message.CopyFrom(Message(role=Role.ROLE_AGENT, message_id=uuid.uuid4().hex,
                                                     parts=[Part(text=str(exc))]))
                yield task
                return
            except _HostWorkCancelled as exc:
                _update_task_record(task.id, state="canceled", message=str(exc), expected_message_id=expected)
            except RuntimeError as exc:
                _update_task_record(task.id, state="failed", message=str(exc), expected_message_id=expected)
            yield await self._same_message_replay(params, context)

        @validate_request_params
        async def on_message_send(self, params, context):
            validate_history_length(params.configuration)
            replay = await self._same_message_replay(params, context)
            if replay is None:
                return await super().on_message_send(params, context)
            if params.configuration.return_immediately or replay.status.state in terminal_states:
                return apply_history_length(replay, params.configuration)
            async for event in self._replay_events(params, context, replay):
                if isinstance(event, Task):
                    replay = event
            return apply_history_length(replay, params.configuration)

        @validate_request_params
        @validate(lambda self: self._agent_card.capabilities.streaming, "Streaming is not supported by the agent")
        async def on_message_send_stream(self, params, context):
            replay = await self._same_message_replay(params, context)
            if replay is not None:
                async with contextlib.aclosing(self._replay_events(params, context, replay)) as stream:
                    async for event in stream:
                        yield event
                return
            async with contextlib.aclosing(super().on_message_send_stream(params, context)) as stream:
                async for event in stream:
                    yield event

    return OperationRequestHandler(agent_executor=OuroborosExecutor(), task_store=_sdk_task_store(), agent_card=card)


def _fetch_identity() -> Dict[str, str]:
    """Best-effort read of the host's real name/description from GET /identity.

    Returns {} when the endpoint is unavailable so the caller can fall back to the
    configured A2A_AGENT_NAME / A2A_AGENT_DESCRIPTION defaults.
    """
    try:
        response = httpx.get(
            f"{HOST_SERVICE_URL}/identity", headers=_host_headers(), timeout=_HOST_FETCH_TIMEOUT_SEC
        )
        if response.status_code == 200:
            data = response.json() or {}
            return {
                "name": str(data.get("name") or "").strip(),
                "description": str(data.get("description") or "").strip(),
            }
        logger.warning("a2a agent-card: /identity returned status %s", response.status_code)
    except Exception as exc:
        logger.warning("a2a agent-card: /identity unavailable (%s)", exc)
    return {}


def _fetch_tool_schemas(
    attempts_override: "int | None" = None,
    *,
    stop_event: "threading.Event | None" = None,
) -> List[Dict[str, Any]]:
    """Fetch the host tool schemas, treating an empty list as "not ready yet".

    The companion routinely starts before the host chat-agent is built, so
    GET /tools/schemas answers 200 {"tools": []} for the first few seconds. A
    single 200 must NOT be accepted as final when the list is empty — that was
    the regression that let the agent card collapse to the identity-only entry
    on a peer whose host was still warming up. So an empty 200 is retried like a
    transient failure; the last non-empty result is cached at module scope; and
    a populated card is never regressed back to empty. If every attempt yields
    empty we serve the last known-good tool list when we have one, and fall back
    to the identity-derived entry only when we have never seen a populated list.

    ``stop_event`` (passed only by the background refresher) aborts the ladder and
    its backoff waits as soon as companion shutdown is requested.
    """
    global _LAST_GOOD_TOOLS
    attempts = _TOOLS_FETCH_ATTEMPTS if attempts_override is None else max(1, int(attempts_override))
    last_error = "no attempt made"
    for attempt in range(attempts):
        if stop_event is not None and stop_event.is_set():
            last_error = "shutdown requested"
            break
        try:
            response = httpx.get(
                f"{HOST_SERVICE_URL}/tools/schemas",
                headers=_host_headers(),
                timeout=_HOST_FETCH_TIMEOUT_SEC,
            )
            if response.status_code == 200:
                tools = response.json().get("tools") or []
                if tools:
                    _LAST_GOOD_TOOLS = tools
                    return tools
                last_error = "host returned an empty tool list (chat-agent not ready yet)"
            else:
                last_error = f"status {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            delay = _TOOLS_FETCH_BACKOFF_SEC * (attempt + 1)
            # Shutdown must interrupt the ladder, not merely the interval between
            # runs: the full backoff chain sleeps ~14s, longer than
            # _REFRESHER_JOIN_TIMEOUT_SEC, so a plain sleep here made a graceful
            # stop time out its join and leak the thread with a warning.
            if stop_event is not None:
                if stop_event.wait(delay):
                    last_error = "shutdown requested"
                    break
            else:
                time.sleep(delay)
    if _LAST_GOOD_TOOLS:
        logger.warning(
            "a2a agent-card: /tools/schemas empty after %d attempts (%s); "
            "serving last known-good tool list (%d tools)",
            attempts,
            last_error,
            len(_LAST_GOOD_TOOLS),
        )
        return _LAST_GOOD_TOOLS
    logger.warning(
        "a2a agent-card: /tools/schemas unavailable after %d attempts (%s); "
        "card will advertise the curated capability categories only",
        attempts,
        last_error,
    )
    return []


def _resolve_identity() -> Dict[str, str]:
    """Resolve the card's top-level name/description.

    Operator-set A2A_AGENT_NAME / A2A_AGENT_DESCRIPTION always win. Otherwise the
    NAME is the stable product name "Ouroboros" — the /identity name is the
    identity document's first heading, which in practice is a section title like
    "Who I Am", not an agent name — and the DESCRIPTION leads with the curated
    capability summary, with the live /identity first line appended as flavor
    (personality on the card, never INSTEAD of capabilities: an integrator reads
    what the agent can do off this field)."""
    identity = _fetch_identity()
    name = _A2A_AGENT_NAME_EXPLICIT or "Ouroboros"
    if _A2A_AGENT_DESCRIPTION_EXPLICIT:
        description = _A2A_AGENT_DESCRIPTION_EXPLICIT
    else:
        flavor = str(identity.get("description") or "").strip()
        description = _CAPABILITY_LEAD + ((" " + flavor) if flavor else "")
    return {"name": name, "description": description}


def _agent_card() -> Dict[str, Any]:
    ident = _resolve_identity()
    name = ident["name"]
    description = ident["description"]

    # Curated capability categories come FIRST and are ALWAYS present: the card must
    # describe what this agent can actually do even while the host tool registry is
    # warming up or unavailable (the two prior regressions served a contentless
    # "General" stub and then an identity-persona-only entry — both useless to an
    # integrator that consumes capabilities off the card).
    skills = [dict(entry) for entry in _CORE_CAPABILITY_SKILLS]

    # Live per-tool entries follow, when available. The in-request fetch is SHORT
    # (the background refresher owns the long warmup ladder and keeps the last-good
    # list warm), so a discovery GET answers in seconds even on a cold host.
    seen_ids = {entry["id"] for entry in skills}
    tools = _LAST_GOOD_TOOLS or _fetch_tool_schemas(attempts_override=_TOOLS_FETCH_ATTEMPTS_REQUEST)
    for schema in tools:
        func = schema.get("function", schema) if isinstance(schema, dict) else {}
        tool_name = str(func.get("name") or "")
        if tool_name and tool_name not in seen_ids:
            seen_ids.add(tool_name)
            skills.append({
                "id": tool_name,
                "name": tool_name,
                "description": str(func.get("description") or "")[:200],
                "tags": [tool_name.split("_", 1)[0] if "_" in tool_name else "tool"],
            })

    base_url = f"http://{A2A_HOST}:{A2A_PORT}/"
    return {
        "name": name,
        "description": description,
        "url": base_url,
        "version": A2A_CARD_VERSION,
        # A2: advertise the A2A v0.3 transport interface so v0.3-aware clients can negotiate.
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [{"url": base_url, "transport": "JSONRPC"}],
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
    }


def _sdk_agent_card():
    card = _agent_card()
    # The SDK AgentCard requires the card URL, and v0.3-aware SDKs also carry the
    # transport identity fields the dict card already publishes. Omitting them
    # raised during _build_app() and killed the supervised companion before it
    # served a single request. Values come from the dict card so the two cards
    # cannot drift; unknown kwargs are filtered so an older SDK still constructs.
    kwargs = dict(
        name=card["name"],
        description=card["description"],
        version=card["version"],
        url=card["url"],
        protocol_version=card["protocolVersion"],
        preferred_transport=card["preferredTransport"],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id=str(skill.get("id") or skill.get("name") or "general"),
                name=str(skill.get("name") or skill.get("id") or "General"),
                description=str(skill.get("description") or ""),
                tags=list(skill.get("tags") or ["general"]),
            )
            for skill in card.get("skills", [])
        ],
    )
    # The filter must cover BOTH card families an SDK can export: pydantic models
    # (model_fields) and protobuf messages, whose fields live on
    # DESCRIPTOR.fields_by_name and which raise ValueError — not TypeError — for an
    # unknown kwarg. a2a-sdk 1.1.2 exports the PROTO AgentCard, which has no `url`
    # field: passing it raised at import time, so the host-supervised companion
    # died before serving a single request and just restart-looped.
    fields = getattr(AgentCard, "model_fields", None) or getattr(AgentCard, "__fields__", None)
    if not fields:
        descriptor = getattr(AgentCard, "DESCRIPTOR", None)
        if descriptor is not None:
            fields = dict.fromkeys(descriptor.fields_by_name)
    if fields:
        kwargs = {key: value for key, value in kwargs.items() if key in fields}
    try:
        return AgentCard(**kwargs)
    except (ValueError, TypeError) as exc:
        # An SDK-shape mismatch must never stop the daemon from booting: the dict
        # card served at /.well-known/ is the authoritative one either way.
        logger.warning("a2a agent-card: SDK card rejected kwargs (%s); retrying minimal card", exc)
        minimal = {
            key: kwargs[key]
            for key in ("name", "description", "version", "capabilities", "skills")
            if key in kwargs
        }
        return AgentCard(**minimal)


async def agent_card(_request: Request) -> JSONResponse:
    # _agent_card() does BLOCKING httpx.get calls plus time.sleep backoff on the
    # cold path (_LAST_GOOD_TOOLS empty / host unreachable). uvicorn runs this app
    # on one event loop, so calling it inline stalled EVERY concurrent request —
    # including in-flight message/send dispatch — for up to ~10s during exactly
    # the cold-start window the retry ladder exists to survive. _agent_card stays
    # synchronous because _sdk_agent_card() calls it once at startup, off the
    # request path, where blocking is correct.
    return JSONResponse(await asyncio.to_thread(_agent_card))


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "a2a"})


def _extract_text(params: Dict[str, Any]) -> str:
    # Independently defensive: safe even if reached from a path that has not
    # already proven the nested shapes.
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, dict):
            texts.append(str(part.get("text") or ""))
    return "\n".join(text for text in texts if text).strip()


def _jsonrpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


async def jsonrpc(request: Request) -> JSONResponse:
    # Untrusted bodies are validated at EVERY nesting level this handler touches
    # before any .get access: a valid JSON list or scalar previously raised an
    # uncaught AttributeError that surfaced as an opaque HTTP 500 rather than a
    # JSON-RPC invalid-request response.
    try:
        payload = await request.json()
    except ValueError as exc:
        return _jsonrpc_error(None, -32700, f"parse error: {exc}")
    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "invalid request: body must be a JSON object")
    request_id = payload.get("id") or uuid.uuid4().hex
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32600, "invalid request: params must be a JSON object")
    message = params.get("message")
    if message is not None and not isinstance(message, dict):
        return _jsonrpc_error(request_id, -32600, "invalid request: params.message must be a JSON object")
    if method == "tasks/get":
        try:
            task = await asyncio.to_thread(_refresh_task_record, str(params.get("id") or ""))
        except _TaskStateCorrupt as exc:
            # Distinct from "task not found": the record exists but cannot be read,
            # so the caller gets an actionable internal error, not a bare traceback.
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}})
        if not task:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32004, "message": "task not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})
    if method == "tasks/cancel":
        return await _jsonrpc_cancel(request_id, str(params.get("id") or ""))
    if method != "message/send":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
    text = _extract_text(params)
    message = params.get("message") or {}
    task_id = str(message.get("taskId") or uuid.uuid4().hex)
    context_id = str(message.get("contextId") or task_id)
    message_id = str(message.get("messageId") or "")
    expected_message_id = _client_message_id(task_id, message_id)
    try:
        response_text = await _dispatch_to_host(
            text, task_id=task_id, context_id=context_id, message_id=message_id,
        )
        task = _update_task_record(
            task_id, context_id, state="completed",
            artifacts=[{"parts": [{"kind": "text", "text": response_text}]}],
            expected_message_id=expected_message_id,
        )
    except _HostWaitExpired as exc:
        task = _update_task_record(task_id, context_id, state="working", message=str(exc), expected_message_id=expected_message_id)
    except _HostMessageConflict as exc:
        return _jsonrpc_error(request_id, -32602, str(exc))
    except _HostWorkCancelled as exc:
        task = _update_task_record(task_id, context_id, state="canceled", message=str(exc), expected_message_id=expected_message_id)
    except Exception as exc:
        task = _update_task_record(task_id, context_id, state="failed", message=str(exc), expected_message_id=expected_message_id)
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})


_TERMINAL_RECORD_STATES = frozenset({"completed", "failed", "canceled", "rejected"})


async def _jsonrpc_cancel(request_id: Any, task_id: str) -> JSONResponse:
    """No-SDK ``tasks/cancel``: the host's cancellation owner decides; the
    record turns ``canceled`` ONLY on the host's ``cancelled`` outcome."""
    try:
        record = _load_task(task_id)
    except _TaskStateCorrupt as exc:
        return _jsonrpc_error(request_id, -32603, str(exc))
    if not record:
        return _jsonrpc_error(request_id, -32001, "task not found")
    state = str((record.get("status") or {}).get("state") or "")
    if state in _TERMINAL_RECORD_STATES:
        return _jsonrpc_error(
            request_id, _JSONRPC_TASK_NOT_CANCELABLE, f"Task cannot be canceled - current state: {state}",
        )
    try:
        outcome = await asyncio.to_thread(_cancel_host_operation_sync, task_id)
    except _HostCancelRefused as exc:
        return _jsonrpc_error(
            request_id, _JSONRPC_TASK_NOT_CANCELABLE, f"Task cannot be canceled: {exc.reason} ({exc.outcome})",
        )
    task = _update_task_record(
        task_id, str(record.get("contextId") or task_id), state="canceled",
        message=f"host work cancelled ({outcome.get('task_id') or 'direct turn'})",
        expected_message_id=outcome["client_message_id"],
    )
    if str((task.get("ouroboros") or {}).get("client_message_id") or "") != outcome["client_message_id"]:
        return _jsonrpc_error(request_id, _JSONRPC_TASK_NOT_CANCELABLE, _SUPERSEDED_CANCEL_MESSAGE)
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class OuroborosExecutor(AgentExecutor if _A2A_SDK_AVAILABLE else object):
    """Streams the host agent's work as spec-shaped A2A events.

    Lifecycle per message: submitted -> working (start note) -> working updates
    carrying the host agent's own progress notes (plus heartbeats while quiet)
    -> one final artifact + completed. Failures become a terminal failed status
    with a human-readable message instead of a JSON-RPC -32603 stream error."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = getattr(context, "message", None)
        parts = getattr(message, "parts", []) or []
        text = "\n".join(str(getattr(part, "text", "") or "") for part in parts if getattr(part, "text", ""))
        # Reuse the SDK-provided ids: the request handler rejects events whose
        # task_id differs from RequestContext.task_id when the client named one.
        task_id = getattr(context, "task_id", "") or uuid.uuid4().hex
        context_id = getattr(context, "context_id", "") or task_id
        message_id = str(getattr(message, "message_id", "") or "")
        expected_message_id = _client_message_id(task_id, message_id)
        updater = TaskUpdater(event_queue, task_id, context_id)

        async def emit_working(note: str) -> None:
            await _maybe_await(
                updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=updater.new_agent_message([Part(text=note)]),
                )
            )

        if not getattr(context, "current_task", None):
            await _maybe_await(updater.submit())
        await _maybe_await(updater.start_work())
        state, note, artifacts = "completed", "", None
        try:
            response_text = await self._run_dispatch(
                text, emit_working, task_id=task_id, context_id=context_id, message_id=message_id,
            )
            artifacts = [{"parts": [{"kind": "text", "text": response_text}]}]
        except _HostWaitExpired as exc:
            state, note = "working", str(exc)
        except _HostWorkCancelled as exc:
            state, note = "canceled", str(exc)
            logger.info("a2a executor task %s cancelled at the host: %s", task_id, exc)
        except Exception as exc:
            state, note = "failed", f"dispatch failed: {exc}"
            logger.warning("a2a executor task %s failed: %s", task_id, exc)
        record = _update_task_record(task_id, context_id, state=state, message=note,
                                    artifacts=artifacts, expected_message_id=expected_message_id)
        current = str((record.get("ouroboros") or {}).get("client_message_id") or "")
        if current and current != expected_message_id:
            return
        if state == "working":
            await emit_working(note)
        elif state == "canceled":
            # The cancel caller may already have published the terminal event.
            try:
                await _maybe_await(updater.cancel(message=updater.new_agent_message([Part(text=note)])))
            except Exception:
                pass
        elif state == "failed":
            # Dispatch failures remain a typed terminal status on the stream.
            await _maybe_await(updater.failed(message=updater.new_agent_message([Part(text=note)])))
        else:
            await _maybe_await(updater.add_artifact([Part(text=response_text)], last_chunk=True))
            await _maybe_await(updater.complete())
            logger.info("a2a executor finalized task %s (state=completed)", task_id)

    async def _run_dispatch(
        self, text: str, emit_working, *, task_id: str = "", context_id: str = "", message_id: str = "",
    ) -> str:
        _guard_inbound_text(text)
        try:
            semaphore = _get_semaphore()
            await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("A2A server is busy; retry later") from exc
        try:
            chat_id, client_message_id = await asyncio.to_thread(
                _bind_inbound_message_sync, task_id, context_id, message_id,
            )
            started = time.monotonic()
            deadline = started + A2A_STREAM_DEADLINE_SEC
            await emit_working("Message accepted; dispatching to the host agent")
            dispatch = asyncio.ensure_future(
                asyncio.to_thread(_dispatch_after_allocate_sync, chat_id, text, deadline, client_message_id)
            )
            last_emit = time.monotonic()
            while True:
                done, _pending = await asyncio.wait({dispatch}, timeout=A2A_PROGRESS_POLL_SEC)
                if done:
                    break
                notes: List[str] = []
                for note in notes:
                    await emit_working(note)
                    last_emit = time.monotonic()
                if time.monotonic() - last_emit >= A2A_HEARTBEAT_SEC:
                    await emit_working(f"Still working ({int(time.monotonic() - started)}s elapsed)")
                    last_emit = time.monotonic()
            return await dispatch
        finally:
            semaphore.release()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """``tasks/cancel``: the host's cancellation owner decides.

        Only the host's ``cancelled`` outcome publishes the canceled state;
        every other answer (nothing addressable yet, work steered into a
        pre-existing task, already terminal, custody unresolved, host
        unreachable) is the SDK's ``TaskNotCancelableError`` carrying the
        host's reason — never a canceled task over work that keeps running.
        """
        task_id = getattr(context, "task_id", "") or ""
        context_id = getattr(context, "context_id", "") or task_id
        try:
            outcome = await asyncio.to_thread(_cancel_host_operation_sync, task_id)
        except _HostCancelRefused as exc:
            logger.info("a2a cancel for task %s refused by the host: %s", task_id, exc)
            raise TaskNotCancelableError(
                message=f"Task cannot be canceled: {exc.reason} ({exc.outcome})"
            ) from exc
        record = _update_task_record(task_id, context_id, state="canceled", expected_message_id=outcome["client_message_id"])
        if str((record.get("ouroboros") or {}).get("client_message_id") or "") != outcome["client_message_id"]:
            # The SDK cancels its current producer after this method returns.
            raise TaskNotCancelableError(message=_SUPERSEDED_CANCEL_MESSAGE)
        updater = TaskUpdater(event_queue, task_id, context_id)
        await _maybe_await(updater.cancel(
            message=updater.new_agent_message(
                [Part(text=f"host work cancelled ({outcome.get('task_id') or 'direct turn'})")]
            ),
        ))


def _guard_inbound_text(text: str) -> None:
    # Presence receives slash-prefixed peer text as ordinary conversation data.
    if not isinstance(text, str):
        raise ValueError("peer text must be a string")


_PRESENCE_TRANSPORT = None


def _presence_transport():
    global _PRESENCE_TRANSPORT
    if _PRESENCE_TRANSPORT is None:
        _PRESENCE_TRANSPORT = PresenceTransport(STATE_DIR, HOST_SERVICE_URL, _host_headers,
            timeout=A2A_RESPONSE_TIMEOUT_SEC, poll_interval=A2A_PROGRESS_POLL_SEC)
    return _PRESENCE_TRANSPORT


def _bind_inbound_message_sync(task_id: str, context_id: str, message_id: str):
    task_id = task_id or uuid.uuid4().hex
    client_id = _client_message_id(task_id, message_id)
    with _TASK_RECORD_LOCK:
        bound = _bound_operation(task_id)
        if client_id in bound.get("legacy_message_ids", []) or (bound.get("chat_id") and client_id in bound.get("message_ids", [bound.get("client_message_id")])):
            raise _HostMessageConflict("Legacy owner-chat message cannot be resubmitted as a place event; inspect its saved task")
        if bound.get("conversation_id") and context_id and bound["conversation_id"] != context_id:
            raise _HostMessageConflict("taskId was already accepted in another context")
        conversation = str(bound.get("conversation_id") or context_id or task_id)
        _update_task_record(task_id, context_id, binding={
            "conversation_id": conversation, "client_message_id": client_id,
            "operation_ref": client_id, "protocol": "presence",
        })
    return conversation, client_id


def _inject_sync(conversation_id, text, client_message_id=""):
    return _presence_transport().submit(conversation_id, text, client_message_id or uuid.uuid4().hex)


def _wait_final_after_expiry_sync(conversation_id, deadline_monotonic, operation_ref=""):
    return _presence_transport().wait(operation_ref, deadline_monotonic, read=_read_operation_sync)


def _dispatch_after_allocate_sync(conversation_id, text, deadline_monotonic, client_message_id=""):
    # Kept as the dispatch seam used by SDK and fallback callers; no chat allocation.
    try:
        return _inject_sync(conversation_id, text, client_message_id)
    except _HostWaitExpired as exc:
        record = _presence_transport().load(exc.operation_ref)
        if not record or not (record.get("turn_ref") or record.get("work_ref")):
            raise
        return _wait_final_after_expiry_sync(conversation_id, deadline_monotonic, exc.operation_ref)


def _dispatch_to_host_sync(text: str, *, task_id: str = "", context_id: str = "", message_id: str = "") -> str:
    conversation, client_id = _bind_inbound_message_sync(task_id, context_id, message_id)
    return _dispatch_after_allocate_sync(conversation, text, time.monotonic() + A2A_STREAM_DEADLINE_SEC, client_id)


async def _dispatch_to_host(text: str, *, task_id: str = "", context_id: str = "", message_id: str = "") -> str:
    """Non-streaming dispatch used by the no-SDK fallback message/send route."""
    _guard_inbound_text(text)
    try:
        semaphore = _get_semaphore()
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("A2A server is busy; retry later") from exc
    try:
        return await asyncio.to_thread(
            _dispatch_to_host_sync, text, task_id=task_id, context_id=context_id, message_id=message_id,
        )
    finally:
        semaphore.release()


_REFRESHER_STARTED = False
_REFRESHER_STOP = threading.Event()
_REFRESHER_THREAD: "threading.Thread | None" = None


def _stop_tools_refresher() -> None:
    """Terminate the refresher thread and allow a later restart.

    Invoked from the app lifespan shutdown path, so an ordinary companion
    stop (host unload/disable, uvicorn SIGTERM) ends the thread through a real
    code path instead of relying on the interpreter tearing a daemon thread
    down. Panic still hard-kills the process group; this is the graceful path
    that previously did not exist at all. Idempotent, and safe to call when the
    thread was never started."""
    global _REFRESHER_STARTED, _REFRESHER_THREAD
    _REFRESHER_STOP.set()
    thread = _REFRESHER_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=_REFRESHER_JOIN_TIMEOUT_SEC)
        if thread.is_alive():
            logger.warning(
                "a2a tools refresher did not exit within %ss; leaving it as a daemon thread",
                _REFRESHER_JOIN_TIMEOUT_SEC,
            )
    _REFRESHER_THREAD = None
    _REFRESHER_STARTED = False


def _start_tools_refresher() -> None:
    """Keep ``_LAST_GOOD_TOOLS`` warm from a daemon thread.

    The host chat-agent can take minutes to build on a fresh install, far longer
    than any latency a discovery GET should pay. This thread owns the LONG warmup:
    it runs the full retry ladder until the first non-empty tool list lands, then
    re-polls occasionally so an enabled/disabled skill is reflected without a
    companion restart. Card requests never block on it — they serve the curated
    categories plus whatever this thread has already warmed.

    ``A2A_TOOLS_REFRESHER=0`` disables the thread (tests and embedders that build
    the app repeatedly must not accumulate pollers); repeated calls are no-ops.
    ``_stop_tools_refresher`` ends it on companion shutdown."""
    global _REFRESHER_STARTED, _REFRESHER_THREAD
    if _REFRESHER_STARTED or os.environ.get("A2A_TOOLS_REFRESHER", "1") == "0":
        return
    _REFRESHER_STARTED = True
    _REFRESHER_STOP.clear()

    def _loop() -> None:
        while not _REFRESHER_STOP.is_set():
            try:
                _fetch_tool_schemas(stop_event=_REFRESHER_STOP)
            except Exception as exc:
                # A poll error must not end the refresher permanently. The thread
                # body has no other recovery path, so a single unexpected
                # exception (the stop_event signature mismatch was exactly this)
                # killed background warming for the whole companion lifetime
                # after one stderr traceback nobody reads.
                logger.warning("a2a tools refresher poll failed: %s", exc)
            if _REFRESHER_STOP.is_set():
                break
            interval = _TOOLS_REFRESH_INTERVAL_SEC if _LAST_GOOD_TOOLS else _TOOLS_RETRY_INTERVAL_SEC
            if _REFRESHER_STOP.wait(interval):
                break

    _REFRESHER_THREAD = threading.Thread(target=_loop, name="a2a-tools-refresher", daemon=True)
    _REFRESHER_THREAD.start()


_TOOLS_REFRESH_INTERVAL_SEC = 300
_TOOLS_RETRY_INTERVAL_SEC = 15
_REFRESHER_JOIN_TIMEOUT_SEC = 10


@contextlib.asynccontextmanager
async def _app_lifespan(_app: Starlette):
    """Modern-starlette shutdown hook (the on_shutdown kwarg no longer exists):
    end the tools refresher through a real code path on companion stop."""
    try:
        yield
    finally:
        _stop_tools_refresher()


def _build_app() -> Starlette:
    if not _is_loopback(A2A_HOST) and not A2A_SERVER_PASSWORD:
        raise RuntimeError("Refusing non-loopback A2A bind without A2A_SERVER_PASSWORD")
    _start_tools_refresher()
    if _A2A_SDK_AVAILABLE:
        card = _sdk_agent_card()
        handler = _sdk_request_handler(card)
        logger.info("a2a daemon using SDK agent-card routes")
        # A2: serve the v0.3-complete DICT card at BOTH well-known paths, registered BEFORE
        # the SDK helper so Starlette first-match makes it authoritative. The SDK AgentCard
        # object deliberately omits the v0.3 transport fields (protocolVersion /
        # preferredTransport / additionalInterfaces — kept out for SDK-version safety), so it
        # must NOT own the v0.3 path; the dict card carries them. The SDK's own card route is
        # then a harmless shadow, and the JSON-RPC handler still holds the card object.
        routes = [
            Route("/health", health, methods=["GET"]),
            Route("/.well-known/agent.json", agent_card, methods=["GET"]),
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=True),
        ]
        return Starlette(
            routes=routes,
            middleware=[Middleware(_A2AAuthMiddleware)],
            lifespan=_app_lifespan,
        )
    logger.info("a2a daemon using fallback (no SDK) agent-card routes")
    return Starlette(
        routes=[
            Route("/.well-known/agent.json", agent_card, methods=["GET"]),
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/", jsonrpc, methods=["POST"]),
        ],
        middleware=[Middleware(_A2AAuthMiddleware)],
        lifespan=_app_lifespan,
    )


app = _build_app()


if __name__ == "__main__":
    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT, log_level="warning")
