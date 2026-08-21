"""Authenticated loopback client for the frozen presence Host contract."""

from __future__ import annotations

import asyncio
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, Sequence


_OUTCOMES = {"message", "silent", "tool_delivered", "deferred"}
_TERMINAL_WORK_STATES = {"completed", "failed", "cancelled"}
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

    def __init__(self, status_code: int, detail: str):
        self.status_code = int(status_code)
        super().__init__(f"presence Host returned HTTP {self.status_code}: {detail}")


@dataclass(frozen=True)
class PresenceSubmission:
    status: str
    outcome: str
    text: str
    turn_ref: str
    work_ref: str
    binding_id: str


@dataclass(frozen=True)
class PresenceWorkResult:
    status: str
    outcome: str = ""
    text: str = ""
    work_ref: str = ""

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
            detail = _error_detail(exc.read(64 * 1024))
            raise PresenceHostHTTPError(exc.code, detail) from exc
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

    async def submit(
        self,
        event: Dict[str, Any],
        staged_files: Sequence[pathlib.Path],
    ) -> PresenceSubmission:
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            raise PresenceHostError("presence event does not match the frozen contract")
        binding_id = load_binding_id(self.state_dir)
        response = await self.transport.request_json(
            "POST",
            f"{self.host_base}/presence/turn",
            headers=self._headers(),
            payload={
                "binding_id": binding_id,
                "event": dict(event),
                "staged_files": [str(path) for path in staged_files],
            },
            timeout_sec=1800.0,
        )
        if str(response.get("status") or "") != "completed":
            raise PresenceHostError("presence Host did not complete the turn request")
        outcome = str(response.get("outcome") or "")
        if outcome not in _OUTCOMES:
            raise PresenceHostError("presence Host returned an invalid outcome")
        work_ref = str(response.get("work_ref") or "").strip()
        if outcome == "deferred" and not work_ref:
            raise PresenceHostError("deferred presence result is missing work_ref")
        return PresenceSubmission(
            status="completed",
            outcome=outcome,
            text=str(response.get("text") or ""),
            turn_ref=str(response.get("turn_ref") or "").strip(),
            work_ref=work_ref,
            binding_id=binding_id,
        )

    async def poll(self, work_ref: str, binding_id: str) -> PresenceWorkResult:
        encoded_ref = urllib.parse.quote(str(work_ref), safe="")
        query = urllib.parse.urlencode({"binding_id": str(binding_id)})
        response = await self.transport.request_json(
            "GET",
            f"{self.host_base}/presence/work/{encoded_ref}?{query}",
            headers=self._headers(),
            payload=None,
            timeout_sec=35.0,
        )
        status = str(response.get("status") or "")
        if status == "pending":
            return PresenceWorkResult(status=status, work_ref=str(work_ref))
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


def _error_detail(data: bytes) -> str:
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return "request rejected"
    if not isinstance(payload, dict):
        return "request rejected"
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
