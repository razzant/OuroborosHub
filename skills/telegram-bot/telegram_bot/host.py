"""Authenticated loopback client for the frozen presence Host contract."""

from __future__ import annotations

import asyncio
import json
import math
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, Sequence


_OUTCOMES = {"message", "silent", "tool_delivered", "deferred"}
_TERMINAL_WORK_STATES = {"completed", "failed", "cancelled"}
_DISPOSITIONS = {"retained", "rejected", "blocked", "retry"}
# Continuation version 2: an early custody answer once the event's author takes mail.
_EARLY_CONTINUATION = 2
# The Host's documented hint for each busy admission resource, when its own is unusable.
_BUSY_RETRY_SEC = {"conversation": 1.0, "capacity": 5.0}
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
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310 - fixed loopback URL
                data = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            body = exc.read(64 * 1024)
            raise PresenceHostHTTPError(exc.code, _error_detail(body), _error_payload(body)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PresenceHostError(
                f"presence Host request failed: {type(exc).__name__}"
            ) from exc
        if len(data) > 2 * 1024 * 1024:
            raise PresenceHostError("presence Host response exceeds 2 MiB")
        try:
            result = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PresenceHostError("presence Host returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise PresenceHostError("presence Host returned a non-object response")
        return result


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
        self.delivery_reporting_status = "unknown"
        self.delivery_reporting_version = 0
        self.places_version = 0
        self.presence_wake_signals = 0
        self.presence_identity = 0
        self.connector_identity = {}
        self.presence_reaction_context = 0
        self.presence_explicit_completion = 0
        # 2 only when ``/identity`` lists it; otherwise each turn waits for its terminal (0).
        self.continuation_version = 0

    async def discover_delivery_support(self) -> int:
        if self.delivery_reporting_status in {"supported", "unsupported"}:
            return self.delivery_reporting_version
        try:
            response = await self.transport.request_json(
                "GET", f"{self.host_base}/identity", headers=self._headers(),
                payload=None, timeout_sec=10.0,
            )
            self.delivery_reporting_version = int(response.get("presence_delivery_version") == 1)
            version = response.get("places_version")
            self.places_version = int(type(version) is int and version >= 1)
            self.presence_wake_signals = int(response.get("presence_wake_signals") == 1)
            self.presence_identity = int(response.get("presence_identity") == 1)
            # A JSON true is not the capability; bool compares equal to 1 in Python.
            self.presence_reaction_context = int(type(response.get("presence_reaction_context")) is int
                                                 and response["presence_reaction_context"] == 1)
            self.presence_explicit_completion = int(type(response.get("presence_explicit_completion")) is int
                                                    and response["presence_explicit_completion"] == 1)
            # The scalar ``presence_continuation_version`` stays 1 for deployed consumers; the list decides.
            versions = response.get("presence_continuation_versions")
            self.continuation_version = _EARLY_CONTINUATION if isinstance(versions, list) and any(
                type(item) is int and item == _EARLY_CONTINUATION for item in versions) else 0
            self.delivery_reporting_status = "supported" if self.delivery_reporting_version else "unsupported"
        except (PresenceHostError, OSError, TimeoutError, asyncio.TimeoutError):
            self.delivery_reporting_status = "unavailable"
            self.delivery_reporting_version = 0
            self.continuation_version = 0
            self.presence_reaction_context = 0
            self.presence_explicit_completion = 0
        return self.delivery_reporting_version

    async def place_hold(self, account_id: str, conversation_id: str, thread_id: str = "") -> str:
        await self.discover_delivery_support()
        if not self.places_version:
            return "Held: place policy unavailable" if self.delivery_reporting_status == "unavailable" else ""
        try:
            # Host excludes a thread's own place from a lookup that names no thread; one without asks as before.
            query = urllib.parse.urlencode({"provider": "telegram", "account_id": account_id,
                                            "conversation_id": conversation_id,
                                            **({"thread_id": thread_id} if thread_id else {})})
            place = await self.transport.request_json(
                "GET", f"{self.host_base}/presence/place?{query}", headers=self._headers(),
                payload=None, timeout_sec=10.0,
            )
        except (PresenceHostError, OSError, TimeoutError):
            return "Held: place policy unavailable"
        state = place.get("state")
        return "" if state == "active" else f"Held: {place.get('label') or 'Telegram'} is {state or 'unavailable'}"

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
    ) -> PresenceSubmission:
        if not isinstance(event, dict) or set(event) - {"facts"} != _EVENT_KEYS:
            raise PresenceHostError("presence event does not match the frozen contract")
        mode = await self.discover_delivery_support()
        binding_id = "" if self.places_version else load_binding_id(self.state_dir)
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
                    **({"binding_id": binding_id} if not self.places_version else {}),
                    "event": dict(event),
                    "staged_files": [str(path) for path in staged_files],
                    **({"delivery_reporting_version": 1} if mode else {}),
                    **({"continuation_version": _EARLY_CONTINUATION} if early else {}),
                },
                timeout_sec=1800.0,
            )
        except PresenceHostHTTPError as exc:
            exc.binding_id = binding_id  # retained references are polled under this binding
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
            binding_id=binding_id,
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


def _error_detail(data: bytes) -> str:
    payload = _error_payload(data)
    return str(payload.get("error") or payload.get("code") or "request rejected")[:300]


__all__ = [
    "PresenceHostClient",
    "PresenceHostError",
    "PresenceHostHTTPError",
    "PresenceHostTransport",
    "PresenceSubmission",
    "PresenceWorkResult",
    "load_binding_id",
]
