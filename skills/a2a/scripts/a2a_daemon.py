from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hmac
import importlib
import inspect
import json
import logging
import os
import pathlib
import ssl
import sys
import time
import types
import urllib.parse
import uuid
from typing import Any, Dict, List

import httpx
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Companion scripts are also loaded directly by the Hub's isolated tests.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from a2a_presence import (PUBLIC, PresenceTransport, public as _public, wire as _wire,
                          HostWaitExpired as _HostWaitExpired, HostWorkCancelled as _HostWorkCancelled,
                          HostMessageConflict as _HostMessageConflict)
from a2a_card import (CAPABILITY_LEAD, TOOLS_FETCH_ATTEMPTS, TOOLS_FETCH_ATTEMPTS_REQUEST, CardSource)
from a2a_records import (TERMINAL_STATES, Records, TaskStateCorrupt, arrival_digest, check_same_arrival,
                         presence_address)

try:
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.agent_execution.simple_request_context_builder import SimpleRequestContextBuilder
    from a2a.server.events.event_queue import EventQueue
    from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.server.tasks.task_updater import TaskUpdater
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Artifact, Part, Role, TaskState
    from a2a.utils.errors import A2AError, InternalError, InvalidParamsError, TaskNotCancelableError
except ImportError as exc:  # The receiving server has one implementation; without its SDK it does not start.
    raise RuntimeError(
        "a2a: the A2A SDK is missing (pip packages 'a2a-sdk[http-server]>=1.0.0,<2.0.0' and 'protobuf<6', "
        "declared in SKILL.md), so the receiving server cannot start. The send, status and discover tools "
        f"still work. Import error: {exc}"
    ) from exc

if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("a2a: the standalone receiver needs uvicorn alongside the SDK declared in SKILL.md") from None

logger = logging.getLogger("a2a_daemon")

STATE_DIR = pathlib.Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".")
_SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Card version — kept in step with the skill version (SKILL.md / catalog entry).
A2A_CARD_VERSION = "2.0.0"

_SUPERSEDED_CANCEL_MESSAGE = (
    "The previous host operation was cancelled, but the task now belongs to another message; "
    "the current operation was not cancelled."
)
_HOST_LOST_MESSAGE = PUBLIC[("lost", "failed")]
# What a peer is told about any failure inside this server; the detail stays in the companion's log.
_INTERNAL_ERROR = "Internal error; the details are in this agent's own log."


def _skill_lib(module):
    """Import the skill's own library as a package under a private name."""
    name = "_a2a_skill_payload"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(_SKILL_ROOT)]
        sys.modules[name] = package
    return importlib.import_module(f"{name}.lib.{module}")


links = _skill_lib("links")
_is_loopback = links.is_loopback
_client_message_id = _skill_lib("letters").legacy_client_message_id


def _host_service_hostname(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        raise RuntimeError("HOST_SERVICE_URL must not contain userinfo")
    return parsed.hostname or ""


HOST_SERVICE_URL = os.environ.get("HOST_SERVICE_URL", "http://127.0.0.1:8767").rstrip("/")
# The client of every Host Service call (identity, tool schemas, Presence); tests give each install its own.
_HOST_HTTP: Any = httpx
_HOST_FACTS: Dict[str, Any] = {}  # the host's /identity document (feature facts)


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


def _server_configuration() -> tuple[Dict[str, Any], str]:
    settings = links.load_server_settings(STATE_DIR)
    public = links.public_url(os.environ.get("A2A_PUBLIC_URL") or settings.get("A2A_PUBLIC_URL"))
    return settings, public


def _startup_settings() -> tuple[Dict[str, Any], str]:
    if __name__ != "__main__":
        return _server_configuration()
    with links.storage.locked(STATE_DIR / links.LOCK_FILE):
        settings, public = _server_configuration()
        password = links.shared_password(os.environ.get(links.SHARED_PASSWORD) or settings.get(links.SHARED_PASSWORD))
        links.record_active_shared(STATE_DIR, password)
        return settings, public


_SETTINGS, A2A_PUBLIC_URL = _startup_settings()


def _setting_text(name: str) -> str:
    return (os.environ.get(name) or str(_SETTINGS.get(name) or "")).strip()


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
_A2A_AGENT_NAME_EXPLICIT = _setting_text("A2A_AGENT_NAME")
_A2A_AGENT_DESCRIPTION_EXPLICIT = _setting_text("A2A_AGENT_DESCRIPTION")
A2A_AGENT_NAME = _A2A_AGENT_NAME_EXPLICIT or "Ouroboros"
A2A_AGENT_DESCRIPTION = _A2A_AGENT_DESCRIPTION_EXPLICIT or "Ouroboros A2A peer"
# The shared password of earlier releases: still accepted, as one unnamed caller.
A2A_SERVER_PASSWORD = _setting_text(links.SHARED_PASSWORD)
# Direct HTTPS (both files, or neither) and the address the card advertises (a proxy, a tunnel).
A2A_TLS_CERT_FILE = _setting_text("A2A_TLS_CERT_FILE")
A2A_TLS_KEY_FILE = _setting_text("A2A_TLS_KEY_FILE")

A2A_MAX_CONCURRENT = _setting_int("A2A_MAX_CONCURRENT", 5, minimum=1, maximum=20)
# The Presence turn wait is bounded at 1800s; stay under it (plus the httpx
# margin) so the daemon, not a socket, always decides what a wait expiry means.
A2A_RESPONSE_TIMEOUT_SEC = _setting_int("A2A_RESPONSE_TIMEOUT_SEC", 600, minimum=1, maximum=1740)
# Total delivery window of one inbound message, deferred work polling and allowance re-offers included.
# Closing this wait never cancels the Host task.
A2A_STREAM_DEADLINE_SEC = _setting_int("A2A_STREAM_DEADLINE_SEC", 3600, minimum=60, maximum=21600)
# Cross-validate the two independently-bounded waits. The deadline is only checked
# AFTER the turn request returns, so a response timeout longer than the total deadline
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


# --- who is calling --------------------------------------------------------
#
# HTTP Basic, where the password names the caller: a link's own inbound password
# gives the principal ``link:<uid>`` (the link's own identity, never its label, so a
# rename keeps its tasks and a link made again under the same id starts afresh); the
# shared password of earlier releases and an anonymous request to a loopback bind are
# the one unnamed caller (principal "", today's scope). The shared password this server
# runs with is checked first, so a link given it before a restart cannot take over its
# callers. A wrong password is refused everywhere, loopback included. The user name is
# not used.


class _Caller:
    """The request user the A2A SDK scopes tasks by (``user_name`` is its ``display_name``)."""

    def __init__(self, principal: str) -> None:
        self.display_name = principal
        self.is_authenticated = bool(principal)


def _principal_for(authorization: "str | None") -> "str | None":
    """The caller's principal, or None to refuse the request."""
    if not authorization:
        return None if links.requires_inbound_auth(A2A_HOST, A2A_PUBLIC_URL) else ""
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    _user, separator, secret = decoded.partition(":")
    if not separator or not secret:
        return None
    if A2A_SERVER_PASSWORD and hmac.compare_digest(secret.encode("utf-8"), A2A_SERVER_PASSWORD.encode("utf-8")):
        return ""
    try:
        link = links.match_secret(STATE_DIR, secret)
    except links.LinkError as exc:  # unreadable links: no named caller is accepted until they are repaired
        logger.warning("a2a links: %s", exc)
        link = None
    return link.principal if link is not None else None


class _LinkAuthentication:
    """Authenticate every request and hand the SDK the caller's principal as the request user."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        principal = _principal_for(Headers(scope=scope).get("authorization"))
        if principal is None:
            response = JSONResponse({"error": "A2A authentication required"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="a2a"'})
            return await response(scope, receive, send)
        if principal:
            scope["user"] = _Caller(principal)
        return await self.app(scope, receive, send)


def _principal_of(call_context) -> str:
    user = getattr(call_context, "user", None)
    return str(getattr(user, "user_name", "") or "")


def _inbound_credentials_exist() -> bool:
    if A2A_SERVER_PASSWORD:
        return True
    try:
        return any(link.uid and link.inbound_hash for link in links.load(STATE_DIR))
    except links.LinkError:
        return False


# --- task records, message identity, arrival reservations (a2a_records.py) --------------------------

_RECORDS = Records(STATE_DIR, window=A2A_STREAM_DEADLINE_SEC, legacy_identity=_client_message_id)
_TASK_RECORD_LOCK = _RECORDS.lock
_TERMINAL_RECORD_STATES = TERMINAL_STATES
_TaskStateCorrupt = TaskStateCorrupt
_tasks_dir, _task_path, _save_task = _RECORDS.tasks_dir, _RECORDS.task_path, _RECORDS.save_task
_load_task, _read_task_record = _RECORDS.load_task, _RECORDS.read_task_record
_update_task_record, _bound_operation, _load_arrival = _RECORDS.update_task, _RECORDS.bound, _RECORDS.load_arrival
_arrival_digest, _is_current = arrival_digest, _RECORDS.is_current
_check_same_arrival, _presence_address = check_same_arrival, presence_address


def _settle(task_id: str, principal: str, wire_id: "str | None", digest: str, state: str, delivery: str,
            answer: str = "") -> Dict[str, Any]:
    """Record an outcome with its public sentence, fenced to the message it answers."""
    text = PUBLIC[(delivery, state)]
    record = _update_task_record(task_id, principal=principal, state=state, message=text,
                                 artifacts=[{"parts": [{"kind": "text", "text": answer}]}] if answer else None,
                                 delivery={"class": delivery, "text": text}, expected_message_id=digest,
                                 wire_id=wire_id)
    if digest and state in _TERMINAL_RECORD_STATES:
        _RECORDS.retire_arrival(record, digest, wire_id or "")
    return record


def _public_sentence(state: str, delivery: "str | None") -> str:
    """The public sentence of a stored state: its delivery class's, else the state's own (none for success)."""
    if (delivery, state) in PUBLIC:
        return PUBLIC[(delivery, state)]
    fallback = {"working": "working", "failed": "failed", "canceled": "canceled"}.get(state)
    return PUBLIC[(fallback, state)] if fallback else ""


def _legacy_replay(task_id: str, principal: str, digest: str, text: str, context_id: str, room: str) -> bool:
    """Whether a message the SDK history does not know is a 1.5 message of this task (its sender spoke JSON-RPC
    without the SDK): its digest is bound to the task and its saved event holds the same words in the same room.
    False for a new message; a conflict when the old record cannot establish that a repeat is the same message."""
    bound = _bound_operation(task_id, principal)
    if digest not in (bound.get("message_ids") or [bound.get("client_message_id")]):
        return False
    receipt = _presence_transport().load(digest)
    if receipt is None or receipt["event"].get("text") != text or (context_id and context_id != room):
        raise _HostMessageConflict("this messageId's earlier record cannot confirm a safe repeat; send a new message")
    return True


def _settle_view(task_id: str, principal: str, wire_id: "str | None", digest: str, view) -> Dict[str, Any]:
    state, delivery, _text = _public(view)
    return _settle(task_id, principal, wire_id, digest, state, delivery,
                   view["text"] if delivery == "answered" else "")


# --- arrival reservations ----------------------------------------------------
#
# The request context builder reserves (principal, messageId) before the SDK creates or acknowledges a
# task and hands the reserved ids to the winning first request; a request that loses the race joins it.

_IN_FLIGHT: set = set()          # arrival digests a live dispatch of this process is delivering
_BACKGROUND: set = set()         # delivery and observation tasks outside a request
_STATUS_READS: set = set()       # one background observation per task while its saved answers are readable


class _ArrivalJoined(Exception):
    """The arrival was reserved by an earlier or concurrent request: join it, do not start a second one."""

    def __init__(self, arrival: Dict[str, Any]) -> None:
        super().__init__(arrival["digest"])
        self.arrival = arrival


def _actor(principal: str) -> Dict[str, Any]:
    if not principal:
        return {"platform": "a2a", "platform_actor_id": "", "display_name": "A2A peer", "is_bot": True,
                "origin": "machine"}
    try:
        link = links.by_principal(STATE_DIR, principal)
    except links.LinkError:
        link = None
    # ``origin: machine`` only lowers authority: a program's words, never an owner's, whatever accounts say.
    actor = {"platform": "a2a", "platform_actor_id": principal, "display_name": link.label if link else "A2A link",
             "is_bot": True, "origin": "machine"}
    if _host_facts().get("presence_identity") == 1:
        actor["authenticated"] = True
    return actor


def _reserve_arrival(principal: str, message_id: str, content: Dict[str, Any], text: str, **where):
    """Reserve one arrival (``Records.reserve``), in flight from the moment it exists; the actor may read the
    host's /identity once, outside the record lock."""
    return _RECORDS.reserve(principal, message_id, content, text, actor=_actor(principal),
                            on_reserved=_IN_FLIGHT.add, **where)


# --- delivery to the host ----------------------------------------------------


class _HostCancelRefused(RuntimeError):
    """The host answered anything but ``cancelled``: the honest reason rides along."""

    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(f"host cancel outcome {outcome!r}: {reason}")
        self.outcome = outcome
        self.reason = reason


class _ServerBusy(RuntimeError):
    """Every dispatch slot was taken at the first offer; nothing was offered."""


def _cancel_host_operation_sync(task_id: str, principal: str = "") -> Dict[str, Any]:
    if not _bound_operation(task_id, principal):
        raise _HostCancelRefused("unbound", "no host operation is bound to this task")
    raise _HostCancelRefused("cancel_unsupported",
        "the Presence protocol has no cancellation endpoint; host work is unchanged")


_PRESENCE_TRANSPORT = None


def _host_facts() -> Dict[str, Any]:
    """The host's ``/identity`` document (feature facts), read once and refreshed with each card build."""
    if not _HOST_FACTS:
        _fetch_identity()
    return _HOST_FACTS


def _presence_transport():
    global _PRESENCE_TRANSPORT
    if _PRESENCE_TRANSPORT is None:
        _PRESENCE_TRANSPORT = PresenceTransport(STATE_DIR, HOST_SERVICE_URL, _host_headers,
            timeout=A2A_RESPONSE_TIMEOUT_SEC, poll_interval=A2A_PROGRESS_POLL_SEC, http=_HOST_HTTP,
            facts=_host_facts)
    return _PRESENCE_TRANSPORT


def _read_operation_sync(operation_ref: str):
    return _presence_transport().read(operation_ref) if operation_ref else None


def _read_letter_view(digest: str, operation_ref: str = ""):
    view = _read_operation_sync(operation_ref or digest)
    if view and view.get("status") == "unknown" and digest in _IN_FLIGHT:
        return None  # the current or an earlier letter may still be getting its own reference
    return view


def _wait_final_after_expiry_sync(conversation_id, deadline_monotonic, operation_ref=""):
    return _presence_transport().wait(operation_ref, deadline_monotonic, read=_read_operation_sync)


def _task_terminal(record):
    return (record.get("status") or {}).get("state") in _TERMINAL_RECORD_STATES


def _refresh_task_record(task_id: str, principal: str = "") -> "Dict[str, Any] | None":
    """Reconcile a saved task with its exact host operations after reconnect (a read; nothing is offered).

    Besides the current message, each earlier message of the task whose own outcome is not filed yet is read
    through its own receipt; a final answer there is filed under its message id, never in the task's state.
    """
    record = _load_task(task_id, principal)
    if not record:
        return None
    bound = record.get("ouroboros") or {}
    if not _task_terminal(record):
        record = _refresh_current_record(task_id, principal, record, bound)
    for digest, wire in _RECORDS.open_messages(record):
        if digest != (record.get("ouroboros") or {}).get("client_message_id"):
            view = _read_letter_view(digest)
            if view and _public(view)[0] in _TERMINAL_RECORD_STATES:
                record = _settle_view(task_id, principal, wire, digest, view)
    return record


def _refresh_current_record(task_id, principal, record, bound):
    digest = str(bound.get("client_message_id") or "")
    view = _read_letter_view(digest, str(bound.get("operation_ref") or ""))
    if not view or (_public(view)[0] == "working" and (record.get("delivery") or {}).get("class") != "no_capacity"):
        return record
    return _settle_view(task_id, principal, bound.get("message_id"), digest, view)


async def _acquire_slot(fail_fast: bool) -> None:
    try:
        await asyncio.wait_for(_get_semaphore().acquire(), timeout=0.1 if fail_fast else None)
    except asyncio.TimeoutError as exc:
        raise _ServerBusy("A2A server is busy; retry later") from exc


async def _deliver(arrival: Dict[str, Any], *, first: bool = True):
    """Offer the reserved event and wait for its outcome within the arrival's delivery window.

    After a spent allowance the same event is offered again at the host's own next attempt; the dispatch
    slot is free while waiting for it. At the end of the window a refusal stays a refusal only if the last
    offer was refused (an offer with an unknown effect is never called refused).
    """
    transport = _presence_transport()
    digest, deadline_wall = arrival["digest"], float(arrival["deadline"])
    deadline = time.monotonic() + max(0.0, deadline_wall - time.time())
    while True:
        await _acquire_slot(first)
        try:
            view = await asyncio.to_thread(transport.offer, arrival["event"], digest, deadline=deadline_wall)
            if view["status"] == "pending":
                receipt = await asyncio.to_thread(transport.load, digest)
                if not (receipt and (receipt.get("turn_ref") or receipt.get("work_ref"))):
                    raise _HostWaitExpired("Host admission is unconfirmed; retry this same messageId", digest)
                view = await asyncio.to_thread(transport.wait, digest, deadline)
        finally:
            _get_semaphore().release()
        if view["status"] != "waiting":
            return view
        first = False
        await asyncio.sleep(max(0.0, float(view["next_at"]) - time.time()))
        if time.time() >= deadline_wall:
            return await asyncio.to_thread(transport.settle_expired, digest)


async def _deliver_and_settle(arrival: Dict[str, Any]) -> None:
    """Deliver an arrival with no request waiting on it (a repeat after an unconfirmed offer, a restart)."""
    digest = arrival["digest"]
    if digest in _IN_FLIGHT:
        return
    _IN_FLIGHT.add(digest)
    try:
        try:
            view = await _deliver(arrival, first=False)
        except _HostWaitExpired:
            return
        await asyncio.to_thread(_settle_view, arrival["task_id"], arrival["principal"],
                                arrival["message_id"], digest, view)
    except Exception as exc:  # noqa: BLE001 - a background delivery must not end silently
        logger.warning("a2a delivery of %s failed: %s", digest, exc)
    finally:
        _IN_FLIGHT.discard(digest)


def _start_background(arrival: Dict[str, Any]) -> None:
    if arrival["digest"] in _IN_FLIGHT:
        return
    task = asyncio.ensure_future(_deliver_and_settle(arrival))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _recovery(arrival: Dict[str, Any]) -> str:
    """What carrying an arrival on needs (``PresenceTransport.recovery``); this decides, it offers nothing."""
    return _presence_transport().recovery(arrival["digest"], arrival.get("deadline"))


async def _carry_on(arrival: Dict[str, Any], *, allow_offer: bool = True, observe: bool = False) -> str:
    """Carry an arrival on (see ``_recovery``); the delivery runs in the background. Returns what was done; a failure
    is logged and leaves the arrival for the next read or restart (it never fails the read that asked)."""
    if arrival["digest"] in _IN_FLIGHT:
        return ""
    transport = _presence_transport()
    try:
        record = await asyncio.to_thread(_RECORDS.attach_arrival, arrival)
        if _RECORDS.settled_arrival(record, arrival["digest"], arrival["message_id"]):
            _RECORDS.retire_arrival(record, arrival["digest"], arrival["message_id"])
            return ""
        action = await asyncio.to_thread(_recovery, arrival)
        if action == "offer":
            if not allow_offer:
                return ""
            _start_background(arrival)
        elif action == "expire":
            view = await asyncio.to_thread(transport.settle_unoffered, arrival["event"], arrival["digest"],
                                           deadline=arrival["deadline"],
                                           outcome={"status": "refused", "refusal": "not_taken_up"})
            await asyncio.to_thread(_settle_view, arrival["task_id"], arrival["principal"], arrival["message_id"],
                                    arrival["digest"], view)
        elif action == "expire_waiting":
            view = await asyncio.to_thread(transport.settle_expired, arrival["digest"])
            if view["status"] == "refused":
                await asyncio.to_thread(_settle_view, arrival["task_id"], arrival["principal"],
                                        arrival["message_id"], arrival["digest"], view)
        elif action == "expire_unknown":
            view = await asyncio.to_thread(_read_letter_view, arrival["digest"])
            if view and view["status"] == "unknown":
                await asyncio.to_thread(_settle_view, arrival["task_id"], arrival["principal"],
                                        arrival["message_id"], arrival["digest"], view)
            else:
                action = ""  # an exact work reference could have arrived since the observation
        elif not action and observe:
            view = await asyncio.to_thread(_read_letter_view, arrival["digest"])
            if view and _public(view)[0] in _TERMINAL_RECORD_STATES:
                await asyncio.to_thread(_settle_view, arrival["task_id"], arrival["principal"], arrival["message_id"],
                                        arrival["digest"], view)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("a2a carrying %s on failed: %s", arrival["digest"], exc)
        return ""
    return action


async def _observe_task_status(task_id, principal):
    record = await asyncio.to_thread(_refresh_task_record, task_id, principal)
    if not record:
        return record
    bound = record.get("ouroboros") or {}
    current = bound.get("client_message_id") or ""
    pending = dict(_RECORDS.open_messages(record))
    if current and not _task_terminal(record):
        pending.setdefault(current, bound.get("message_id") or "")
    expired = False
    for digest, wire in pending.items():
        arrival = await asyncio.to_thread(_load_arrival, principal, wire)
        if arrival and arrival["digest"] == digest and arrival["task_id"] == task_id:
            expired |= (await _carry_on(arrival, allow_offer=digest == current)).startswith("expire")
    if expired:
        record = await asyncio.to_thread(_load_task, task_id, principal)
    return record


def _observe_status_later(task_id, principal):
    key = (principal, task_id)
    if key in _STATUS_READS:
        return
    _STATUS_READS.add(key)

    async def observe():
        try:
            await _observe_task_status(task_id, principal)
        except Exception as exc:
            logger.warning("a2a background status read of %s failed: %s", task_id, exc)
        finally:
            _STATUS_READS.discard(key)

    task = asyncio.create_task(observe())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _resume_custody() -> None:
    """After a restart: carry on every arrival a read would carry on (never offered, waiting for capacity, offered
    with an unconfirmed effect); one never offered before its window closed is told "saved; no response was
    started", and a wait whose window closed with its last offer refused becomes "no capacity"."""
    for arrival in await asyncio.to_thread(_RECORDS.pending_arrivals):
        await _carry_on(arrival, observe=True)


# --- the SDK seam -------------------------------------------------------------


def _content(message) -> Dict[str, Any]:
    """A message's content without its addresses (a repeat may add the task and context ids it was given)."""
    from google.protobuf.json_format import MessageToDict

    body = MessageToDict(message)
    body.pop("taskId", None)
    body.pop("contextId", None)
    return body


def _message_text(message) -> str:
    return "\n".join(part.text for part in message.parts
                     if any(field.name == "text" for field, _value in part.ListFields()))


def _metadata(wire_id: str, delivery: str, state: str, receipt=None) -> Dict[str, Any]:
    """A message's public facts: its id and short delivery state (``a2a_presence.wire``), never the internal class."""
    facts = {"message_id": wire_id, "delivery": _wire(delivery, state)}
    if receipt is not None:
        facts["answers"] = _public_answers({wire_id: receipt})
    return {"ouroboros": facts}


def _public_answers(answers) -> Dict[str, Any]:
    """Each message's filed outcome as a peer reads it: state and answer as saved, delivery as its short state."""
    return {key: {**entry, "delivery": _wire((entry.get("delivery") or {}).get("class"), entry.get("state"))}
            for key, entry in (answers or {}).items() if isinstance(entry, dict)}


def _sdk_task_store():
    """Adapt the SDK to the durable Hub task records, one folder per caller."""
    from a2a.server.owner_resolver import resolve_user_scope
    from a2a.server.tasks.task_store import TaskStore
    from a2a.types import Message, Task
    from google.protobuf.json_format import MessageToDict, ParseDict

    states = {"submitted": TaskState.TASK_STATE_SUBMITTED, "working": TaskState.TASK_STATE_WORKING,
              "completed": TaskState.TASK_STATE_COMPLETED, "failed": TaskState.TASK_STATE_FAILED,
              "canceled": TaskState.TASK_STATE_CANCELED, "rejected": TaskState.TASK_STATE_REJECTED}

    def owned(record, principal) -> bool:
        # An owner was written at the first durable reservation; a record without one predates links and
        # belongs to the unnamed caller only, never to whoever asks.
        return bool(record) and record.get("sdk_owner", "") == principal

    def project_record(record, principal):
        """Project a local snapshot without refreshing or changing its source.

        A peer reads the public vocabulary only: the status message is the sentence of the task's state and
        delivery class (an earlier release kept raw diagnostics there, and the SDK moves its own status notes into
        the history), so the history keeps the peers' own questions and the artifacts the answers, verbatim.
        """
        if not owned(record, principal):
            return None
        task_id = record["id"]
        task = ParseDict(record["sdk_task"], Task()) if record.get("sdk_task") else Task(id=task_id, context_id=record.get("contextId") or task_id)
        state = str((record.get("status") or {}).get("state") or "")
        task.status.state = states.get(state, TaskState.TASK_STATE_WORKING)
        task.status.ClearField("message")
        if text := _public_sentence(state, (record.get("delivery") or {}).get("class")):
            task.status.message.CopyFrom(Message(role=Role.ROLE_AGENT, message_id=uuid.uuid4().hex, parts=[Part(text=text)]))
        questions = [m for m in task.history if m.role == Role.ROLE_USER]
        del task.history[:]
        task.history.extend(questions)
        bound = record.get("ouroboros") or {}
        source = next((m for m in reversed(task.history) if m.role == Role.ROLE_USER), None)
        source_matches = source is not None and _is_current(bound, task_id, source.message_id)
        if "message_id" in bound or (source and not source_matches):
            # For v2, the receipt's own artifacts cannot include an earlier SDK task's accumulated ones.
            task.ClearField("artifacts")
        if not task.artifacts and record.get("artifacts"):
            for item in record["artifacts"]:
                task.artifacts.append(Artifact(artifact_id=uuid.uuid4().hex, parts=[Part(text=str(p.get("text") or "")) for p in item.get("parts", [])]))
        message_id = bound.get("message_id") if "message_id" in bound else (
            source.message_id if source_matches and bound.get("client_message_id") else "")
        facts = {"message_id": str(message_id or "")}
        if record.get("delivery"):
            facts["delivery"] = _wire(record["delivery"].get("class"), state)
        if record.get("answers"):
            facts["answers"] = _public_answers(record["answers"])
        if "message_id" in bound:
            facts["pending_message_ids"] = list(dict.fromkeys(wire for _, wire in _RECORDS.open_messages(record)
                                                            if wire))
        task.metadata.update({"ouroboros": facts})
        return task

    class DurableTaskStore(TaskStore):
        def project(self, record, principal):
            return project_record(record, principal)

        async def save(self, task, context):
            principal = resolve_user_scope(context)
            with _TASK_RECORD_LOCK:
                existing = _load_task(task.id, principal)
                if existing is not None and not owned(existing, principal):
                    raise _TaskStateCorrupt("SDK task belongs to another caller")
                record = existing or {"id": task.id, "contextId": task.context_id, "sdk_owner": principal}
                inbound = next((m for m in reversed(task.history) if m.role == Role.ROLE_USER), None)
                if inbound and not _is_current(record.get("ouroboros") or {}, task.id, inbound.message_id):
                    return
                record["sdk_owner"] = principal
                record["sdk_task"] = MessageToDict(task)
                state = next((key for key, value in states.items() if value == task.status.state), "working")
                if str((record.get("status") or {}).get("state") or "") not in _TERMINAL_RECORD_STATES:
                    record["status"] = {"state": state}
                _save_task(record, principal)

        async def get(self, task_id, context):
            """The SDK also uses this during validation. No host read or work belongs here."""
            principal = resolve_user_scope(context)
            return project_record(await asyncio.to_thread(_load_task, task_id, principal), principal)

        async def get_status(self, task_id, context):
            principal = resolve_user_scope(context)
            record = await asyncio.to_thread(_load_task, task_id, principal)
            if not owned(record, principal):
                return None  # authorized before any host read
            if record.get("answers") or _task_terminal(record):
                # A written answer is ready now. Another letter may still need its own bounded read.
                if _RECORDS.open_messages(record):
                    _observe_status_later(task_id, principal)
            else:
                record = await _observe_task_status(task_id, principal)
            return project_record(record, principal)

        async def list(self, params, context):
            # Reuse the SDK's filtering/pagination over a request-local view;
            # list reads stored snapshots of this caller only; only get reconciles with the Host.
            principal = resolve_user_scope(context)
            records = await asyncio.to_thread(
                lambda: [_read_task_record(path, principal=principal) for path in _tasks_dir(principal).glob("*.json")]
            )
            view = InMemoryTaskStore()
            for record in records:
                task = project_record(record, principal)
                if task is not None:
                    await view.save(task, context)
            return await view.list(params, context)

        async def delete(self, task_id, context):
            principal = resolve_user_scope(context)
            with _TASK_RECORD_LOCK:
                if owned(_load_task(task_id, principal), principal):
                    _task_path(task_id, principal).unlink(missing_ok=True)

    return DurableTaskStore()


@contextlib.asynccontextmanager
async def _holding(lock):
    """Hold the SDK's running-agent lock with no await inside the block.

    SDK 1.3 made it a ``threading.RLock`` (``async with`` on it raised, so every ordinary send answered
    JSON-RPC -32603 after reaching the host); earlier 1.x releases used an ``asyncio.Lock``.
    """
    if hasattr(lock, "__aenter__"):
        async with lock:
            yield
    else:
        with lock:
            yield


class _ReservingContextBuilder(SimpleRequestContextBuilder):
    """Reserve the arrival before the SDK creates or acknowledges a task, and give the winner its ids."""

    async def build(self, context, params=None, task_id=None, context_id=None, task=None):
        if params is not None:
            message = params.message
            requested = {"task_id": message.task_id, "context_id": message.context_id}
            try:
                arrival, fresh = await asyncio.to_thread(
                    _reserve_arrival, _principal_of(context), message.message_id, _content(message),
                    _message_text(message), task_id=task.id if task else "",
                    context_id=task.context_id if task else "", requested=requested)
            except _HostMessageConflict as exc:
                raise InvalidParamsError(message=str(exc)) from exc
            if not fresh:
                raise _ArrivalJoined(arrival)
            context.state["a2a_arrival"] = arrival
            task_id, context_id = arrival["task_id"], arrival["context_id"]
        return await super().build(context=context, params=params, task_id=task_id, context_id=context_id,
                                   task=task)


def _sdk_request_handler(card):
    from google.protobuf.json_format import MessageToDict
    from a2a.server.request_handlers.request_handler import validate, validate_request_params
    from a2a.utils.task import apply_history_length, validate_history_length
    from a2a.utils.errors import TaskNotFoundError
    from a2a.types import Message, Task, TaskStatus
    terminal_states = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_CANCELED,
                       TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED}
    state_values = {"completed": TaskState.TASK_STATE_COMPLETED, "canceled": TaskState.TASK_STATE_CANCELED,
                    "failed": TaskState.TASK_STATE_FAILED, "rejected": TaskState.TASK_STATE_REJECTED,
                    "submitted": TaskState.TASK_STATE_SUBMITTED, "working": TaskState.TASK_STATE_WORKING}

    def guarded(method):
        """The external JSON-RPC boundary: an unexpected failure (a damaged or unavailable state folder, an error
        after acceptance) answers one fixed sentence; the SDK would otherwise send the exception text, a local path
        included. The detail stays in this companion's log. Works for a method that returns an awaitable and for
        one that returns an event stream (decorated SDK methods are not marked as either)."""
        def refusal(exc):
            logger.warning("a2a %s failed: %s", method.__name__, exc, exc_info=True)
            return InternalError(message=_INTERNAL_ERROR)

        async def awaited(result):
            try:
                return await result
            except A2AError:
                raise
            except Exception as exc:  # noqa: BLE001 - see the docstring
                raise refusal(exc) from None

        async def streamed(result):
            try:
                async with contextlib.aclosing(result) as events:
                    async for event in events:
                        yield event
            except A2AError:
                raise
            except Exception as exc:  # noqa: BLE001 - see the docstring
                raise refusal(exc) from None

        @functools.wraps(method)
        def call(self, *args, **kwargs):
            try:
                result = method(self, *args, **kwargs)
            except A2AError:
                raise
            except Exception as exc:  # noqa: BLE001 - see the docstring
                raise refusal(exc) from None
            return streamed(result) if inspect.isasyncgen(result) else awaited(result)
        return call

    class OperationRequestHandler(LegacyRequestHandler):
        on_list_tasks = guarded(LegacyRequestHandler.on_list_tasks)
        on_cancel_task = guarded(LegacyRequestHandler.on_cancel_task)
        on_subscribe_to_task = guarded(LegacyRequestHandler.on_subscribe_to_task)

        @guarded
        @validate_request_params
        async def on_get_task(self, params, context):
            validate_history_length(params)
            task = await self.task_store.get_status(params.id, context)
            if task is None:
                raise TaskNotFoundError
            return apply_history_length(task, params)

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
            # The SDK closes by task id: only the producer still registered for the task retires it, so a
            # stale producer never closes the current one's queue. No await runs while the lock is held.
            async with _holding(self._running_agents_lock):
                current = self._running_agents.get(task_id) is producer_task
                if current:
                    self._running_agents.pop(task_id, None)
            if current:
                await self._queue_manager.close(task_id)

        def _older_message(self, task, message, state, delivery, answer):
            """A synthetic answer for an older message of a moved task: its own outcome, never the current one's."""
            status = TaskStatus(state=state_values[state])
            text = _wire((delivery or {}).get("class"), state)["text"]
            if text:
                status.message.CopyFrom(Message(role=Role.ROLE_AGENT, message_id=uuid.uuid4().hex, parts=[Part(text=text)]))
            original = next((m for m in reversed(task.history)
                             if m.message_id == message.message_id and m.role == Role.ROLE_USER), message)
            older = Task(id=task.id, context_id=task.context_id, status=status, history=[original],
                         artifacts=[Artifact(artifact_id=uuid.uuid4().hex, parts=[Part(text=answer)])] if answer else [])
            receipt = ({"state": state, "delivery": delivery, "answer": answer or ""}
                       if state in _TERMINAL_RECORD_STATES else None)
            older.metadata.update(_metadata(message.message_id, (delivery or {}).get("class") or "", state, receipt))
            return older

        async def _same_message_replay(self, params, context):
            principal = _principal_of(context)
            message = params.message
            requested = {"task_id": message.task_id, "context_id": message.context_id}
            arrival = await asyncio.to_thread(_load_arrival, principal, message.message_id)
            if arrival is not None:
                try:
                    _check_same_arrival(arrival, _content(message), requested)
                except _HostMessageConflict as exc:
                    raise InvalidParamsError(message=str(exc)) from exc
                digest = arrival["digest"]
                task_id = arrival["task_id"]
                record = await asyncio.to_thread(_RECORDS.attach_arrival, arrival)
            else:
                task_id = message.task_id
                task = await self.task_store.get(task_id, context) if task_id else None
                if task is None:
                    return None
                original = next((m for m in reversed(task.history)
                                 if m.message_id == message.message_id and m.role == Role.ROLE_USER), None)
                digest = _client_message_id(task_id, message.message_id)  # a 1.5 binding names it so
                if original is None:
                    try:
                        if not await asyncio.to_thread(_legacy_replay, task_id, principal, digest,
                                                       _message_text(message), message.context_id, task.context_id):
                            return None  # NEW message retains the SDK's terminal-task guard.
                    except _HostMessageConflict as exc:
                        raise InvalidParamsError(message=str(exc)) from exc
                else:
                    before, after = MessageToDict(original), MessageToDict(message)
                    for body in (before, after):
                        body.pop("taskId", None)
                        body.pop("contextId", None)
                    if before != after or (message.context_id and message.context_id != task.context_id):
                        raise InvalidParamsError(message="messageId was already accepted with different content or context")
                record = await asyncio.to_thread(_load_task, task_id, principal)
            if not record:
                raise InvalidParamsError(message="the task this message was accepted on is unavailable")
            bound = record.get("ouroboros") or {}
            view = None
            if bound and not _RECORDS.settled_arrival(record, digest, message.message_id):
                current = bound.get("client_message_id") == digest
                if arrival:  # only a validated repeat of this exact letter, never SDK validation
                    await _carry_on(arrival, allow_offer=current)
                operation = str(bound.get("operation_ref") or "") if current else ""
                view = await asyncio.to_thread(_read_letter_view, digest, operation)
                if view:
                    record = await asyncio.to_thread(_settle_view, task_id, principal, message.message_id, digest, view)
                else:
                    record = await asyncio.to_thread(_load_task, task_id, principal)
            task = self.task_store.project(record, principal)
            if task is None:
                raise InvalidParamsError(message="the task this message was accepted on is unavailable")
            bound = record.get("ouroboros") or {}
            if not bound or bound.get("client_message_id") == digest:
                return task
            saved = (record.get("answers") or {}).get(message.message_id)
            if saved:
                return self._older_message(task, message, saved["state"], saved.get("delivery"), saved.get("answer"))
            state, delivery, text = _public(view)
            return self._older_message(task, message, state, {"class": delivery, "text": text},
                                       view["text"] if delivery == "answered" else "")

        async def _replay_events(self, params, context, task):
            yield task
            if task.status.state in terminal_states:
                return
            # The SDK stream is task-wide. A retry names a letter, so only its saved operation can supply updates.
            principal = _principal_of(context)
            wire = params.message.message_id
            arrival = await asyncio.to_thread(_load_arrival, principal, wire)
            if arrival:
                ref = arrival["digest"]
                remaining = float(arrival["deadline"]) - time.time()
                if remaining <= 0:
                    receipt = await asyncio.to_thread(_presence_transport().load, ref)
                    if receipt and (receipt.get("turn_ref") or receipt.get("work_ref")):
                        remaining = A2A_STREAM_DEADLINE_SEC  # read admitted work, but never extend its offer window
            else:
                ref = _client_message_id(task.id, wire)
                if not await asyncio.to_thread(_presence_transport().load, ref):
                    return
                remaining = A2A_STREAM_DEADLINE_SEC
            try:
                view = await asyncio.to_thread(_wait_final_after_expiry_sync, "", time.monotonic() + remaining, ref)
            except _HostWaitExpired:
                return  # Only the wait ended. Keep this letter's nonterminal snapshot, with no new producer.
            await asyncio.to_thread(_settle_view, task.id, principal, wire, ref, view)
            yield await self._same_message_replay(params, context)

        async def _replayed(self, params, context, replay):
            if params.configuration.return_immediately or replay.status.state in terminal_states:
                return apply_history_length(replay, params.configuration)
            async for event in self._replay_events(params, context, replay):
                if isinstance(event, Task):
                    replay = event
            return apply_history_length(replay, params.configuration)

        @guarded
        @validate_request_params
        async def on_message_send(self, params, context):
            validate_history_length(params.configuration)
            replay = await self._same_message_replay(params, context)
            if replay is None:
                try:
                    return await super().on_message_send(params, context)
                except _ArrivalJoined:  # a concurrent first copy reserved it first: join that one
                    replay = await self._same_message_replay(params, context)
            return await self._replayed(params, context, replay)

        @guarded
        @validate_request_params
        @validate(lambda self: self._agent_card.capabilities.streaming, "Streaming is not supported by the agent")
        async def on_message_send_stream(self, params, context):
            validate_history_length(params.configuration)
            replay = await self._same_message_replay(params, context)
            if replay is None:
                try:
                    async with contextlib.aclosing(super().on_message_send_stream(params, context)) as stream:
                        async for event in stream:
                            yield event
                    return
                except _ArrivalJoined:
                    replay = await self._same_message_replay(params, context)
            async with contextlib.aclosing(self._replay_events(params, context, replay)) as stream:
                async for event in stream:
                    yield event

    store = _sdk_task_store()
    return OperationRequestHandler(agent_executor=OuroborosExecutor(), task_store=store, agent_card=card,
                                   request_context_builder=_ReservingContextBuilder(task_store=store))


def _card_url() -> str:
    """The address the card advertises: the configured public endpoint (path included), else the bind
    address. A wildcard bind (0.0.0.0, ::) is not dialable from elsewhere: set A2A_PUBLIC_URL for it."""
    return links.advertised_url(A2A_HOST, A2A_PORT, A2A_PUBLIC_URL, bool(A2A_TLS_CERT_FILE and A2A_TLS_KEY_FILE))


_CARD = CardSource(host_url=HOST_SERVICE_URL, headers=_host_headers, http=lambda: _HOST_HTTP, facts=_HOST_FACTS,
                   name=_A2A_AGENT_NAME_EXPLICIT, description=_A2A_AGENT_DESCRIPTION_EXPLICIT, url=_card_url,
                   version=A2A_CARD_VERSION, auth_required=links.requires_inbound_auth(A2A_HOST, A2A_PUBLIC_URL))
_fetch_identity, _fetch_tool_schemas, _resolve_identity = (_CARD.fetch_identity, _CARD.fetch_tool_schemas,
                                                           _CARD.resolve_identity)
_agent_card, _start_tools_refresher, _stop_tools_refresher = _CARD.agent_card, _CARD.start_refresher, _CARD.stop_refresher
_CAPABILITY_LEAD, _TOOLS_FETCH_ATTEMPTS, _TOOLS_FETCH_ATTEMPTS_REQUEST = (CAPABILITY_LEAD, TOOLS_FETCH_ATTEMPTS,
                                                                          TOOLS_FETCH_ATTEMPTS_REQUEST)


def _sdk_agent_card():
    card = _agent_card(live=False)  # The handler needs transport capabilities, not a host connection at import.
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
    # the cold-start window the retry ladder exists to survive.
    return JSONResponse(await asyncio.to_thread(_agent_card))


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "a2a"})


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class OuroborosExecutor(AgentExecutor):
    """Streams the host agent's work as spec-shaped A2A events.

    Lifecycle per message: submitted -> working (start note) -> working heartbeats
    while quiet (submitted while the host has no capacity) -> one final state with
    its public delivery class, plus an artifact when there is an answer. Nothing
    an outside peer sees carries an internal reason code or exception text."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        principal = _principal_of(context.call_context)
        message = getattr(context, "message", None)
        task_id, context_id = context.task_id, context.context_id
        wire_id = str(getattr(message, "message_id", "") or "")
        arrival = context.call_context.state.get("a2a_arrival") or await asyncio.to_thread(
            _load_arrival, principal, wire_id)
        updater = TaskUpdater(event_queue, task_id, context_id)

        async def emit(capacity: bool, elapsed: int) -> None:
            if capacity:
                text = PUBLIC[("no_capacity", "submitted")]
                await _maybe_await(updater.update_status(
                    TaskState.TASK_STATE_SUBMITTED, message=updater.new_agent_message([Part(text=text)]),
                    metadata=_metadata(wire_id, "no_capacity", "submitted")))
            else:
                await _maybe_await(updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=updater.new_agent_message([Part(text=f"Still working ({elapsed}s elapsed)")])))

        if not getattr(context, "current_task", None):
            await _maybe_await(updater.submit())
        # The working event names this message, so a non-blocking sender's snapshot is about it (a continued task's
        # metadata still names the previous message until then).
        await _maybe_await(updater.update_status(TaskState.TASK_STATE_WORKING, metadata=_metadata(
            wire_id, "working", "working")))
        if arrival is None:
            logger.warning("a2a task %s has no arrival reservation", task_id)
            return
        digest, answer = arrival["digest"], ""
        try:
            view = await self._run(arrival, emit)
            state, delivery, _text = _public(view)
            answer = view["text"] if delivery == "answered" else ""
        except _HostWaitExpired:
            state, delivery = "working", "working"
        except _HostWorkCancelled:
            state, delivery = "canceled", "canceled"
        except _ServerBusy:
            # Never offered. The reservation and its task stay, so a repeat replays this answer under the same task.
            state, delivery = "failed", "busy"
            await asyncio.to_thread(_presence_transport().settle_unoffered, arrival["event"], digest,
                                    deadline=arrival["deadline"], outcome={"status": "busy"})
        except Exception as exc:  # noqa: BLE001 - the detail stays in the log, never in the peer's status
            logger.warning("a2a executor task %s failed: %s", task_id, exc)
            state, delivery = "failed", "failed"
        finally:
            _IN_FLIGHT.discard(digest)
        text = PUBLIC[(delivery, state)]
        record = await asyncio.to_thread(_settle, task_id, principal, wire_id, digest, state, delivery, answer)
        if str((record.get("ouroboros") or {}).get("client_message_id") or "") != digest:
            return
        metadata = _metadata(wire_id, delivery, state, (record.get("answers") or {}).get(wire_id))
        note = updater.new_agent_message([Part(text=text)]) if text else None
        if state == "working":
            await _maybe_await(updater.update_status(TaskState.TASK_STATE_WORKING, message=note, metadata=metadata))
        elif state == "canceled":
            # The cancel caller may already have published the terminal event.
            with contextlib.suppress(Exception):
                await _maybe_await(updater.update_status(TaskState.TASK_STATE_CANCELED, message=note, metadata=metadata))
        else:
            if answer:
                await _maybe_await(updater.add_artifact([Part(text=answer)], last_chunk=True))
            target = {"completed": TaskState.TASK_STATE_COMPLETED, "rejected": TaskState.TASK_STATE_REJECTED,
                      "failed": TaskState.TASK_STATE_FAILED, "submitted": TaskState.TASK_STATE_SUBMITTED}[state]
            await _maybe_await(updater.update_status(target, message=note, metadata=metadata))
            logger.info("a2a executor finalized task %s (state=%s)", task_id, state)

    async def _run(self, arrival: Dict[str, Any], emit):
        delivery = asyncio.ensure_future(_deliver(arrival))
        started = last_emit = time.monotonic()
        shown = False
        try:
            while True:
                done, _pending = await asyncio.wait({delivery}, timeout=A2A_PROGRESS_POLL_SEC)
                if done:
                    return await delivery
                capacity = bool(await asyncio.to_thread(_presence_transport().waiting, arrival["digest"]))
                if capacity != shown or time.monotonic() - last_emit >= A2A_HEARTBEAT_SEC:
                    await emit(capacity, int(time.monotonic() - started))
                    shown, last_emit = capacity, time.monotonic()
        finally:
            if not delivery.done():
                delivery.cancel()

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
        principal = _principal_of(getattr(context, "call_context", None))
        try:
            outcome = await asyncio.to_thread(_cancel_host_operation_sync, task_id, principal)
        except _HostCancelRefused as exc:
            logger.info("a2a cancel for task %s refused by the host: %s", task_id, exc)
            raise TaskNotCancelableError(
                message=f"Task cannot be canceled: {exc.reason} ({exc.outcome})"
            ) from exc
        record = _update_task_record(task_id, context_id, principal=principal, state="canceled",
                                     expected_message_id=outcome["client_message_id"])
        if str((record.get("ouroboros") or {}).get("client_message_id") or "") != outcome["client_message_id"]:
            # The SDK cancels its current producer after this method returns.
            raise TaskNotCancelableError(message=_SUPERSEDED_CANCEL_MESSAGE)
        updater = TaskUpdater(event_queue, task_id, context_id)
        await _maybe_await(updater.cancel(
            message=updater.new_agent_message(
                [Part(text=f"host work cancelled ({outcome.get('task_id') or 'direct turn'})")]
            ),
        ))


@contextlib.asynccontextmanager
async def _app_lifespan(_app: Starlette):
    """Carry custody across a restart on start; end the tools refresher and background deliveries on stop."""
    _start_tools_refresher()
    resume = asyncio.ensure_future(_resume_custody())
    try:
        yield
    finally:
        resume.cancel()
        for task in list(_BACKGROUND):
            task.cancel()
        _stop_tools_refresher()


def _tls_options() -> Dict[str, str]:
    """uvicorn's TLS arguments; an incomplete or unusable pair is a setup error before anything listens."""
    if not A2A_TLS_CERT_FILE and not A2A_TLS_KEY_FILE:
        return {}
    if not (A2A_TLS_CERT_FILE and A2A_TLS_KEY_FILE):
        raise RuntimeError("A2A_TLS_CERT_FILE and A2A_TLS_KEY_FILE must be set together")
    try:
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(A2A_TLS_CERT_FILE, A2A_TLS_KEY_FILE)
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError(f"the TLS certificate and key cannot be used together ({type(exc).__name__})") from None
    return {"ssl_certfile": A2A_TLS_CERT_FILE, "ssl_keyfile": A2A_TLS_KEY_FILE}


def _plain_http_warning(tls: Dict[str, str]) -> str:
    if A2A_PUBLIC_URL.startswith("http://"):
        return "a2a: the configured public endpoint is plain HTTP; use an HTTPS proxy or a private link."
    if tls or _is_loopback(A2A_HOST):
        return ""
    return (f"a2a: serving plain HTTP on {A2A_HOST}:{A2A_PORT}; passwords and messages cross the network "
            "unencrypted. Use it only over a private link (a VPN, an SSH forward, a TLS proxy) or set "
            "A2A_TLS_CERT_FILE and A2A_TLS_KEY_FILE.")


def _build_app() -> Starlette:
    if links.requires_inbound_auth(A2A_HOST, A2A_PUBLIC_URL) and not _inbound_credentials_exist():
        raise RuntimeError("A network bind or public A2A address needs an inbound credential: set A2A_SERVER_PASSWORD "
                           "or add a link that accepts messages")
    card = _sdk_agent_card()
    handler = _sdk_request_handler(card)
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
    return Starlette(routes=routes, middleware=[Middleware(_LinkAuthentication)], lifespan=_app_lifespan)


app = _build_app()


if __name__ == "__main__":
    try:
        _TLS = _tls_options()
    except RuntimeError as exc:
        logger.error("a2a: %s; the server does not start", exc)
        raise SystemExit(2) from None
    if warning := _plain_http_warning(_TLS):
        logger.warning("%s", warning)
    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT, log_level="warning", **_TLS)
