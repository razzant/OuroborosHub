"""Authenticated loopback client for the frozen presence Host contract."""

from __future__ import annotations

import asyncio
import concurrent.futures
import http.client
import json
import math
import pathlib
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, Sequence

from .api import _declared_length, _open_multipart


_OUTCOMES = {"message", "silent", "tool_delivered", "deferred"}
_TERMINAL_WORK_STATES = {"completed", "failed", "cancelled"}
_DISPOSITIONS = {"retained", "rejected", "blocked", "retry"}
# Continuation version 2: an early custody answer once the event's author takes mail.
_EARLY_CONTINUATION = 2
# The Host's documented hint for each busy admission resource, when its own is unusable.
_BUSY_RETRY_SEC = {"conversation": 1.0, "capacity": 5.0}
# The Host did not say whether this place allows sending. The outbox counts a hold by its prefix.
PLACE_POLICY_UNREAD = "Held: It retries by itself."
# Why a capability check made no request (HostCapabilities.unasked): this skill's Ouroboros access token (the
# X-Skill-Token the Host issues) could not be had, so Ouroboros was not asked, which is not "no answer".
UNASKED_SKILL_TOKEN = "skill_token_unavailable"
# The Host refused this skill itself (no grant, turned off, unreviewed or a stale token): only its owner can change that.
SKILL_ACCESS_REFUSED = "Held: Check this skill's access in Skills."
# Holds only the owner can end, by the type of the Host's answer: the skill's access was refused, or the place is not
# connected (the Host's own words say how to connect it). The outbox keeps this kind beside the note.
HOLD_SKILL_ACCESS = "skill_access"
HOLD_PLACE_NOT_CONNECTED = "place_not_connected"
HOLD_BINDING_CHANGED = "binding_changed"
OWNER_HOLD_KINDS = frozenset({HOLD_SKILL_ACCESS, HOLD_PLACE_NOT_CONNECTED})
_OWNER_CONTRIBUTION_ID = "owner_bot"


class HoldNote(str):
    """A hold note (``Held: …``) that also says which Host answer produced it (``kind``, "" for an ordinary pause)."""

    kind = ""

    def __new__(cls, note: str, kind: str = "") -> "HoldNote":
        value = super().__new__(cls, note)
        value.kind = kind
        return value
_EVENT_KEYS = {
    "source_event_id",
    "provider",
    "account_id",
    "conversation_id",
    "thread_id",
    "conversation_key",
    "actor",
    "conversation",
    "message",
    "text",
}


class PresenceHostError(RuntimeError):
    """The local Host rejected a presence request or returned an invalid response."""


class PresenceHostResponseTooLarge(PresenceHostError):
    """The response exceeded the local JSON read limit; no partial body is usable."""


class PresenceHostUnreachable(PresenceHostError):
    """No connection to the local Host existed (its name did not resolve or it refused the connection), so no byte of
    the request left this machine. Any failure once a connection exists stays a plain PresenceHostError: the Host
    may have acted on it."""


class PresenceHostHTTPError(PresenceHostError):
    """The local Host returned a concrete HTTP rejection."""

    def __init__(self, status_code: int, detail: str, payload: Optional[Dict[str, Any]] = None):
        self.status_code = int(status_code)
        # The complete JSON error object: a typed refusal is a durable receipt.
        self.payload = dict(payload) if isinstance(payload, dict) else {}
        self.binding_id = ""
        super().__init__(f"presence Host returned HTTP {self.status_code}: {detail}")

    @property
    def disposition(self) -> str:
        """The Host's typed recovery instruction, or "" for an untyped error.

        The disposition decides, never the HTTP status. A retained receipt that
        does not name its original attempt is unusable and stays a retry.
        """
        disposition = str(self.payload.get("disposition") or "")
        if self.payload.get("ok") is not False or disposition not in _DISPOSITIONS:
            return ""
        if disposition == "retained" and not str(self.payload.get("turn_ref") or "").strip():
            return ""
        return disposition

    @property
    def busy_retry_after(self) -> Optional[float]:
        """Seconds until the same event retries after a busy admission, else ``None``.

        Version 2 answers ``presence_capacity_full`` for a held ``conversation`` or full
        ``capacity`` at once instead of queueing: nothing was admitted, so it is not
        custody. The Host's finite ``retry_after`` is a minimum delay; it is never shortened.
        """
        field = self.payload.get("field")
        if (self.disposition != "retry" or self.payload.get("code") != "presence_capacity_full"
                or field not in _BUSY_RETRY_SEC):
            return None
        hint = self.payload.get("retry_after")
        if isinstance(hint, bool) or not isinstance(hint, (int, float)) or not math.isfinite(hint):
            hint = _BUSY_RETRY_SEC[field]
        return max(1.0, float(hint))


@dataclass(frozen=True)
class PresenceSubmission:
    status: str
    outcome: str
    text: str
    turn_ref: str
    work_ref: str
    binding_id: str
    delivery_reporting_version: int = 0
    # Version 2: the author's own reference while it runs (``continuing``), the Host-minted
    # identity of any text, and the covered message that text answers.
    continuation_ref: str = ""
    output_ref: str = ""
    reply_anchor: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class PresenceWorkResult:
    status: str
    outcome: str = ""
    text: str = ""
    work_ref: str = ""
    delivery_reporting_version: int = 0
    # Any view may name admitted child work; an older Host's unresolved view also names its code.
    code: str = ""
    child_work_ref: str = ""
    outputs: tuple[Dict[str, Any], ...] = ()
    output_ref: str = ""
    reply_anchor: Optional[Dict[str, Any]] = None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_WORK_STATES


class PresenceHostTransport(Protocol):
    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Dict[str, Any]: ...


def _open_host(request: urllib.request.Request, timeout: float) -> Any:
    """Open one Host request and read only that request's own answer: the opener file sends already use, which never
    follows a redirect, so neither the request nor this skill's token header reaches another address."""
    return _open_multipart(request, timeout=timeout)


def _whole(response: Any, data: bytes) -> bool:
    """Whether ``data`` is the whole body the answer declared; an answer that declared no length counts as whole."""
    declared = _declared_length(response)
    return declared is None or len(data) == declared


class UrllibPresenceHostTransport:
    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._request_json,
            method,
            url,
            headers,
            payload,
            timeout_sec,
        )

    @staticmethod
    def _request_json(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Dict[str, Any]:
        body = None
        request_headers = {**headers, "Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=request_headers,
        )
        try:
            with _open_host(request, timeout_sec) as response:  # noqa: S310 - fixed loopback URL
                data = response.read(2 * 1024 * 1024 + 1)
                whole = _whole(response, data)
        except urllib.error.HTTPError as exc:
            # A redirect arrives here too (it is not followed): neither its status nor its body answers this request.
            # An error body cut short or unreadable is no answer either; only its status stays.
            try:
                with exc:
                    body = exc.read(64 * 1024) if exc.code >= 400 else b""
                body = body if _whole(exc, body) else b""
            except (OSError, ValueError, http.client.HTTPException):
                body = b""
            raise PresenceHostHTTPError(exc.code, _error_detail(body), _error_payload(body)) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError)):
                raise PresenceHostUnreachable(
                    f"presence Host request failed: {type(exc).__name__} ({type(exc.reason).__name__})"
                ) from exc
            raise PresenceHostError(f"presence Host request failed: {type(exc).__name__}") from exc
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise PresenceHostError(
                f"presence Host request failed: {type(exc).__name__}"
            ) from exc
        if len(data) > 2 * 1024 * 1024:
            raise PresenceHostResponseTooLarge("presence Host response exceeds 2 MiB")
        if not whole:
            raise PresenceHostError("presence Host answer was cut short")
        try:
            result = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PresenceHostError("presence Host returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise PresenceHostError("presence Host returned a non-object response")
        return result


@dataclass(frozen=True)
class HostCapabilities:
    """What one completed /identity check found, with its outcome (``status``) and when it ended (``checked_at``)."""
    status: str = "unknown"  # supported | unsupported | unavailable (no answer) | unknown (never asked)
    delivery_version: int = 0
    places_version: int = 0
    wake_signals: int = 0
    identity: int = 0
    reaction_context: int = 0
    explicit_completion: int = 0
    continuation_version: int = 0
    telegram_group_feed: int = 0
    telegram_group_poll_claim: int = 0
    checked_at: float = 0.0
    # Set when the check made no request (UNASKED_SKILL_TOKEN). Its status stays "unavailable", so every gate (place
    # holds, reactions, reports) acts as for an unanswered check; only the words differ: Ouroboros was not asked.
    unasked: str = ""


class PresenceHostClient:
    """Submit provider events and poll deferred work through one Host permission."""

    def __init__(
        self,
        *,
        state_dir: pathlib.Path,
        token_provider: Callable[[], str],
        host_base: str,
        transport: Optional[PresenceHostTransport] = None,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.token_provider = token_provider
        self.host_base = str(host_base).rstrip("/")
        self.transport = transport or UrllibPresenceHostTransport()
        self.connector_identity = {}
        # What the last completed /identity check found, published whole (HostCapabilities): a reader in another thread
        # (the status route) takes the record once and never pairs one check's fields with another's.
        self.capabilities = HostCapabilities()
        # One /identity check at a time: a caller arriving while it runs (the poll loop, a worker, a reaction's gate)
        # waits for that check and gets its answer; no second request, so no later check can be published over it.
        self._discovery: Optional[asyncio.Future] = None
        self._discovery_task: Optional[asyncio.Task] = None

    # The record's fields under the names the transport has always read.
    delivery_reporting_status = property(lambda self: self.capabilities.status)
    delivery_reporting_version = property(lambda self: self.capabilities.delivery_version)
    places_version = property(lambda self: self.capabilities.places_version)
    presence_wake_signals = property(lambda self: self.capabilities.wake_signals)
    presence_identity = property(lambda self: self.capabilities.identity)
    presence_reaction_context = property(lambda self: self.capabilities.reaction_context)
    presence_explicit_completion = property(lambda self: self.capabilities.explicit_completion)
    # 2 only when ``/identity`` lists it; otherwise each turn waits for its terminal (0).
    continuation_version = property(lambda self: self.capabilities.continuation_version)
    telegram_group_feed = property(lambda self: self.capabilities.telegram_group_feed)
    telegram_group_poll_claim = property(lambda self: self.capabilities.telegram_group_poll_claim)
    # When the last /identity check ended (answered or not): the card words its capabilities as that past check.
    identity_checked_at = property(lambda self: self.capabilities.checked_at)

    async def discover_delivery_support(self) -> int:
        """The Host's /identity answer: once settled (supported or unsupported) it is kept for this client's life, as
        SKILL.md says and 0.13.0 did; while unknown or unanswered, one shared check asks again."""
        if self.capabilities.status in {"supported", "unsupported"}:
            return self.capabilities.delivery_version
        if self._discovery is None or self._discovery.done():
            loop = asyncio.get_running_loop()
            done = self._discovery = loop.create_future()
            self._discovery_task = loop.create_task(self._check_identity(done))
            # Also when the check is cancelled before it ever ran (its own finally then never runs).
            self._discovery_task.add_done_callback(lambda _task: done.done() or done.set_result(None))
        # shield: a caller cancelled while it waits leaves the check running for the others. The future is always
        # resolved (see _check_identity), even when the check itself is cancelled, so no caller waits for ever.
        await asyncio.shield(self._discovery)
        return self.capabilities.delivery_version

    async def _check_identity(self, done: asyncio.Future) -> None:
        """One /identity request, published as one fresh HostCapabilities record; then ``done`` is resolved."""
        record: Optional[HostCapabilities] = None
        try:
            try:
                headers = self._headers()
            except Exception:
                # Without this skill's token no request is made: the check was not asked (never "no answer").
                record = HostCapabilities(status="unavailable", unasked=UNASKED_SKILL_TOKEN, checked_at=time.time())
                return
            response = await self.transport.request_json(
                "GET", f"{self.host_base}/identity", headers=headers,
                payload=None, timeout_sec=10.0,
            )
            delivery_version = int(response.get("presence_delivery_version") == 1)
            version = response.get("places_version")
            # The scalar ``presence_continuation_version`` stays 1 for deployed consumers; the list decides.
            versions = response.get("presence_continuation_versions")
            record = HostCapabilities(
                status="supported" if delivery_version else "unsupported",
                delivery_version=delivery_version,
                places_version=int(type(version) is int and version >= 1),
                wake_signals=int(response.get("presence_wake_signals") == 1),
                identity=int(response.get("presence_identity") == 1),
                # A JSON true is not the capability; bool compares equal to 1 in Python.
                reaction_context=int(type(response.get("presence_reaction_context")) is int
                                     and response["presence_reaction_context"] == 1),
                explicit_completion=int(type(response.get("presence_explicit_completion")) is int
                                        and response["presence_explicit_completion"] == 1),
                continuation_version=_EARLY_CONTINUATION if isinstance(versions, list) and any(
                    type(item) is int and item == _EARLY_CONTINUATION for item in versions) else 0,
                telegram_group_feed=int(type(response.get("telegram_group_feed")) is int
                                        and response["telegram_group_feed"] == 1),
                telegram_group_poll_claim=int(type(response.get("telegram_group_poll_claim")) is int
                                              and response["telegram_group_poll_claim"] == 1),
                checked_at=time.time())
        except (PresenceHostError, OSError, TimeoutError, asyncio.TimeoutError):
            # No answer: a fresh record, nothing carried from any earlier one; the next caller asks again.
            record = HostCapabilities(status="unavailable", checked_at=time.time())
        except Exception:
            # Any other failure of this check is also no answer: callers share it, so it is never raised into them.
            record = HostCapabilities(status="unavailable", checked_at=time.time())
        finally:
            # A settled answer is never replaced (none can be in flight once one is settled; this keeps it so).
            if record is not None and self.capabilities.status not in {"supported", "unsupported"}:
                self.capabilities = record
            if not done.done():
                done.set_result(None)  # also when this check was cancelled: the callers go on with what is known

    async def place_hold(self, account_id: str, conversation_id: str, thread_id: str = "") -> str:
        await self.discover_delivery_support()
        if not self.places_version:
            return PLACE_POLICY_UNREAD if self.delivery_reporting_status == "unavailable" else ""
        try:
            # Host excludes a thread's own place from a lookup that names no thread; one without asks as before.
            query = urllib.parse.urlencode({"provider": "telegram", "account_id": account_id,
                                            "conversation_id": conversation_id,
                                            **({"thread_id": thread_id} if thread_id else {})})
            place = await self.transport.request_json(
                "GET", f"{self.host_base}/presence/place?{query}", headers=self._headers(),
                payload=None, timeout_sec=10.0,
            )
        except PresenceHostHTTPError as exc:
            # "Not connected" and a refused skill are the Host's answers, not an unread policy:
            # the first says what to do in its own words; the second waits for the owner, not for a retry.
            if exc.payload.get("code") == "presence_auth_blocked":
                return HoldNote(SKILL_ACCESS_REFUSED, HOLD_SKILL_ACCESS)
            words = exc.payload.get("error") if exc.payload.get("code") == "place_not_connected" else ""
            return HoldNote(f"Held: {words}", HOLD_PLACE_NOT_CONNECTED) if words else PLACE_POLICY_UNREAD
        except (PresenceHostError, OSError, TimeoutError):
            return PLACE_POLICY_UNREAD
        state = place.get("state")
        if not state:
            return PLACE_POLICY_UNREAD
        return "" if state == "active" else f"Held: {place.get('label') or 'Telegram'} is {state}"

    async def bind_owner_place(
        self, account_id: str, conversation_id: str, thread_id: str = ""
    ) -> Dict[str, Any]:
        """Capture an exact wire-v2 ``owner_bot`` place binding via ``GET /presence/place?operation=bind``."""
        query = urllib.parse.urlencode({
            "operation": "bind",
            "provider": "telegram",
            "account_id": str(account_id),
            "conversation_id": str(conversation_id),
            "thread_id": str(thread_id or ""),
            "require_place": "true",
            "contribution_id": _OWNER_CONTRIBUTION_ID,
        })
        url = f"{self.host_base}/presence/place?{query}"
        try:
            response = await self.transport.request_json(
                "GET", url, headers=self._headers(), payload=None, timeout_sec=10.0,
            )
        except PresenceHostHTTPError as exc:
            return _owner_bind_http_error(exc)
        except (PresenceHostError, OSError, TimeoutError) as exc:
            return {
                "allowed": False,
                "binding": None,
                "state": "unavailable",
                "code": "place_binding_unavailable",
                "error": str(exc),
            }
        return dict(response) if isinstance(response, dict) else {
            "allowed": False,
            "binding": None,
            "state": "unavailable",
            "code": "place_binding_unavailable",
            "error": "presence Host returned an invalid place binding response",
        }

    def bind_owner_place_sync(
        self, account_id: str, conversation_id: str, thread_id: str = ""
    ) -> Dict[str, Any]:
        """Synchronous owner-bot place bind for tool handlers (uses direct urllib in production)."""
        if isinstance(self.transport, UrllibPresenceHostTransport):
            query = urllib.parse.urlencode({
                "operation": "bind",
                "provider": "telegram",
                "account_id": str(account_id),
                "conversation_id": str(conversation_id),
                "thread_id": str(thread_id or ""),
                "require_place": "true",
                "contribution_id": _OWNER_CONTRIBUTION_ID,
            })
            url = f"{self.host_base}/presence/place?{query}"
            try:
                response = self.transport._request_json("GET", url, self._headers(), None, 10.0)
            except PresenceHostHTTPError as exc:
                return _owner_bind_http_error(exc)
            except (PresenceHostError, OSError, TimeoutError) as exc:
                return {
                    "allowed": False,
                    "binding": None,
                    "state": "unavailable",
                    "code": "place_binding_unavailable",
                    "error": str(exc),
                }
            return dict(response) if isinstance(response, dict) else {
                "allowed": False,
                "binding": None,
                "state": "unavailable",
                "code": "place_binding_unavailable",
                "error": "presence Host returned an invalid place binding response",
            }
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.bind_owner_place(account_id, conversation_id, thread_id))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run, self.bind_owner_place(account_id, conversation_id, thread_id)
            ).result(timeout=15.0)

    async def check_owner_place(
        self,
        binding: Dict[str, Any],
        *,
        account_id: str,
        conversation_id: str,
        thread_id: str = "",
    ) -> str:
        """Recheck a retained ``owner_bot`` place binding via ``GET /presence/place?operation=check``."""
        query = urllib.parse.urlencode({
            "operation": "check",
            "provider": "telegram",
            "account_id": str(account_id),
            "conversation_id": str(conversation_id),
            "thread_id": str(thread_id or ""),
            "require_place": "true",
            "contribution_id": _OWNER_CONTRIBUTION_ID,
            "binding": json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        })
        try:
            place = await self.transport.request_json(
                "GET", f"{self.host_base}/presence/place?{query}", headers=self._headers(),
                payload=None, timeout_sec=10.0,
            )
        except PresenceHostHTTPError as exc:
            if exc.status_code == 403 or exc.payload.get("code") == "presence_auth_blocked":
                return HoldNote(SKILL_ACCESS_REFUSED, HOLD_SKILL_ACCESS)
            if exc.status_code in (400, 404, 409) and isinstance(exc.payload, dict):
                return _owner_place_hold_verdict(exc.payload)
            return PLACE_POLICY_UNREAD
        except (PresenceHostError, OSError, TimeoutError):
            return PLACE_POLICY_UNREAD
        if not isinstance(place, dict):
            return PLACE_POLICY_UNREAD
        return _owner_place_hold_verdict(place)

    async def report_delivery(self, payload: Dict[str, Any]) -> None:
        response = await self.transport.request_json(
            "POST", f"{self.host_base}/presence/delivery", headers=self._headers(),
            payload=payload, timeout_sec=10.0,
        )
        if response.get("ok") is not True or response.get("recorded") is not True:
            raise PresenceHostError("Presence delivery report was not acknowledged")

    async def submit(
        self,
        event: Dict[str, Any],
        staged_files: Sequence[pathlib.Path],
        *,
        binding_id: str = "",
        connected_revision: Optional[int] = None,
    ) -> PresenceSubmission:
        if not isinstance(event, dict) or set(event) - {"facts"} != _EVENT_KEYS:
            raise PresenceHostError("presence event does not match the frozen contract")
        mode = await self.discover_delivery_support()
        explicit_binding = str(binding_id or "").strip()
        resolved_binding = explicit_binding or ("" if self.places_version else load_binding_id(self.state_dir))
        event = dict(event)
        if not self.places_version:
            event.pop("facts", None)
        elif not self.presence_wake_signals and "facts" in event:
            event["facts"] = [fact for fact in event["facts"] if fact != "signal.words"]
        if self.presence_identity and self.connector_identity:
            event["identity"] = self.connector_identity
        early = self.continuation_version == _EARLY_CONTINUATION
        try:
            response = await self.transport.request_json(
                "POST",
                f"{self.host_base}/presence/turn",
                headers=self._headers(),
                payload={
                    **({"binding_id": resolved_binding} if (explicit_binding or not self.places_version) else {}),
                    **({"connected_revision": connected_revision}
                       if type(connected_revision) is int and connected_revision >= 0 else {}),
                    "event": dict(event),
                    "staged_files": [str(path) for path in staged_files],
                    **({"delivery_reporting_version": 1} if mode else {}),
                    **({"continuation_version": _EARLY_CONTINUATION} if early else {}),
                },
                timeout_sec=1800.0,
            )
        except PresenceHostHTTPError as exc:
            exc.binding_id = resolved_binding  # retained references are polled under this binding
            raise
        status = str(response.get("status") or "")
        continuation_ref = ""
        if status == "continuing" and early:
            # Custody, not a reply: this event's own author runs and takes the conversation's
            # later events as context. Only its real reference releases the input queue.
            continuation_ref = str(response.get("continuation_ref") or "").strip()
            if (response.get("continuation_version") != _EARLY_CONTINUATION or not continuation_ref
                    or continuation_ref != str(response.get("turn_ref") or "").strip()):
                raise PresenceHostError("continuing presence answer lacks its version-2 continuation_ref")
        elif status != "completed":
            raise PresenceHostError("presence Host did not complete the turn request")
        outcome = str(response.get("outcome") or "")
        if outcome not in _OUTCOMES:
            raise PresenceHostError("presence Host returned an invalid outcome")
        work_ref = str(response.get("work_ref") or "").strip()
        if outcome == "deferred" and not work_ref and not continuation_ref:
            raise PresenceHostError("deferred presence result is missing work_ref")
        return PresenceSubmission(
            status=status,
            outcome=outcome,
            text=str(response.get("text") or ""),
            turn_ref=str(response.get("turn_ref") or "").strip(),
            work_ref=work_ref,
            binding_id=resolved_binding,
            delivery_reporting_version=int(response.get("delivery_reporting_version") == 1),
            continuation_ref=continuation_ref,
            output_ref=str(response.get("output_ref") or "").strip(),
            reply_anchor=_reply_anchor(response.get("reply_anchor")),
        )

    async def poll(self, work_ref: str, binding_id: str) -> PresenceWorkResult:
        encoded_ref = urllib.parse.quote(str(work_ref), safe="")
        query = urllib.parse.urlencode({"binding_id": str(binding_id)}) if binding_id else ""
        response = await self.transport.request_json(
            "GET",
            f"{self.host_base}/presence/work/{encoded_ref}?{query}",
            headers=self._headers(),
            payload=None,
            timeout_sec=35.0,
        )
        status = str(response.get("status") or "")
        outputs = _selected_outputs(response)
        child = _child_ref(response, str(work_ref))
        if status == "pending":
            return PresenceWorkResult(status=status, work_ref=str(work_ref), child_work_ref=child, outputs=outputs)
        if status == "unresolved":
            # An older Host without a canonical answer yet: an observation, never an outcome or a verdict.
            return PresenceWorkResult(status=status, work_ref=str(work_ref),
                                      code=str(response.get("code") or ""),
                                      child_work_ref=child, outputs=outputs)
        if status not in _TERMINAL_WORK_STATES:
            raise PresenceHostError("presence Host returned an invalid work status")
        outcome = str(response.get("outcome") or "")
        if outcome not in _OUTCOMES:
            raise PresenceHostError("presence Host returned an invalid work outcome")
        returned_ref = str(response.get("work_ref") or "").strip()
        if returned_ref and returned_ref != str(work_ref):
            raise PresenceHostError("presence Host returned a different work_ref")
        return PresenceWorkResult(
            status=status,
            outcome=outcome,
            text=str(response.get("text") or ""),
            work_ref=str(work_ref),
            delivery_reporting_version=int(response.get("delivery_reporting_version") == 1),
            child_work_ref=child, outputs=outputs, output_ref=str(response.get("output_ref") or ""),
            reply_anchor=_reply_anchor(response.get("reply_anchor")),
        )

    def _headers(self) -> Dict[str, str]:
        token = str(self.token_provider() or "").strip()
        if not token:
            raise PresenceHostError("presence skill token is unavailable")
        return {"X-Skill-Token": token}


def load_binding_id(state_dir: pathlib.Path) -> str:
    """Read the opaque owner-created binding from skill-local settings."""
    path = pathlib.Path(state_dir) / "settings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PresenceHostError("presence binding is not configured") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PresenceHostError("presence binding settings are unreadable") from exc
    binding_id = payload.get("binding_id") if isinstance(payload, dict) else None
    if not isinstance(binding_id, str) or not binding_id.strip():
        raise PresenceHostError("presence binding is not configured")
    return binding_id.strip()


def _selected_outputs(response: Dict[str, Any]) -> tuple[Dict[str, Any], ...]:
    """Already selected speech keeps its identity even while the author is unresolved."""
    outputs = list(response.get("outputs") or [])
    if response.get("output_ref") and response.get("text"):
        outputs.append({key: response.get(key) for key in ("output_ref", "outcome", "text", "reply_anchor")})
    selected = []
    for output in outputs:
        if not isinstance(output, dict) or not output.get("output_ref") or output.get("outcome") not in _OUTCOMES:
            raise PresenceHostError("invalid released presence output")
        if output["outcome"] in {"message", "deferred"} and str(output.get("text") or "").strip():
            selected.append({**output, "reply_anchor": _reply_anchor(output.get("reply_anchor"))})
    return tuple(selected)


def _reply_anchor(value: Any) -> Optional[Dict[str, Any]]:
    """A version-2 output's last covered input: ``{source_event_id, message}``, else ``None``.

    Promoted children, retained refusals and older Hosts name none; their speech keeps
    answering the original event.
    """
    if not isinstance(value, dict) or not isinstance(value.get("message"), dict):
        return None
    source = str(value.get("source_event_id") or "").strip()
    return {"source_event_id": source, "message": dict(value["message"])} if source else None


def _child_ref(response: Dict[str, Any], polled: str) -> str:
    """Admitted child work named by any work view; the original is never its own child."""
    original = {polled, str(response.get("turn_ref") or "").strip()}
    for key in ("child_work_ref", "work_ref"):
        value = str(response.get(key) or "").strip()
        if value and value not in original:
            return value
    return ""


def _error_payload(data: bytes) -> Dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _owner_bind_http_error(exc: PresenceHostHTTPError) -> Dict[str, Any]:
    if exc.status_code == 403 or exc.payload.get("code") == "presence_auth_blocked":
        return {
            "allowed": False,
            "binding": None,
            "state": "auth_blocked",
            "code": "presence_auth_blocked",
            "error": str(exc.payload.get("error") or SKILL_ACCESS_REFUSED),
        }
    if exc.status_code in (400, 404, 409) and isinstance(exc.payload, dict):
        return {
            **exc.payload,
            "allowed": False,
            "binding": exc.payload.get("binding"),
        }
    return {
        "allowed": False,
        "binding": None,
        "state": "unavailable",
        "code": "place_binding_unavailable",
        "error": str(exc),
    }


def _owner_place_hold_verdict(payload: Dict[str, Any]) -> str:
    code = str(payload.get("code") or "").strip()
    state = str(payload.get("state") or "").strip()
    label = str(payload.get("label") or "").strip() or "Telegram"
    error = str(payload.get("error") or "").strip()
    if payload.get("allowed") is True and state == "active":
        return ""
    if code == "presence_auth_blocked":
        return HoldNote(SKILL_ACCESS_REFUSED, HOLD_SKILL_ACCESS)
    # The connection a row was bound to ended: a reconnect is a new connection, so this never undoes itself and the
    # runtime ends the row unsent (its words are the row's final error).
    if state == "disconnected" or code == "place_disconnected":
        return HoldNote(
            f"Not sent: {label} was disconnected or connected again after this was queued; "
            "it is never sent to the new connection.",
            HOLD_BINDING_CHANGED,
        )
    if state == "changed" or code in ("place_scope_changed", "place_binding_invalid"):
        return HoldNote(
            f"Not sent: {label} no longer covers this chat or topic as it did when this was queued; "
            "it is never sent elsewhere.",
            HOLD_BINDING_CHANGED,
        )
    if code == "place_not_connected":
        return HoldNote(f"Held: {error or 'Connect this chat in Places first.'}", HOLD_PLACE_NOT_CONNECTED)
    if state in ("silent", "paused"):
        return HoldNote(f"Held: {label} is {state}", "")
    return PLACE_POLICY_UNREAD


def _error_detail(data: bytes) -> str:
    payload = _error_payload(data)
    return str(payload.get("error") or payload.get("code") or "request rejected")[:300]


__all__ = [
    "PresenceHostClient",
    "PresenceHostError",
    "PresenceHostHTTPError",
    "PresenceHostUnreachable",
    "PresenceHostTransport",
    "PresenceSubmission",
    "PresenceWorkResult",
    "UNASKED_SKILL_TOKEN",
    "load_binding_id",
]
