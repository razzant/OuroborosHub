from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urlsplit

import httpx

from .store import InboxItem

_OUTCOMES = frozenset({"message", "silent", "tool_delivered", "deferred"})
_TERMINAL_WORK_STATES = frozenset({"completed", "failed", "cancelled"})
_BINDING_ID_RE = re.compile(r"[0-9a-f]{32}")


class HostAdapterUnavailable(RuntimeError):
    """Raised when the owner has not selected a presence binding."""


class HostContractError(RuntimeError):
    """Raised when the loopback presence endpoint violates its frozen contract."""


class HostBindingTerminalError(HostContractError):
    """Raised when the configured binding is missing or cannot admit the event."""


def normalize_binding_id(value: Any) -> str:
    """Return one canonical Presence Binding ID or reject ambiguous input."""

    binding_id = str(value or "").strip()
    if not _BINDING_ID_RE.fullmatch(binding_id):
        raise HostContractError(
            "binding_id must be 32 lowercase hexadecimal characters"
        )
    return binding_id


@dataclass(frozen=True)
class HostTurnStatus:
    state: str
    error: str = ""
    texts: tuple[str, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in {"ready", "failed"}


@dataclass(frozen=True)
class HostDelivery:
    texts: tuple[str, ...] = ()


class PresenceHostAdapter(Protocol):
    """The provider's narrow submit/status/deliver Host boundary."""

    available: bool

    async def submit(self, event: InboxItem) -> str:
        """Submit one idempotent provider event and return a durable reference."""
        ...

    async def status(self, reference: str) -> HostTurnStatus:
        """Return the current state for a submitted turn or deferred work item."""
        ...

    async def deliver(self, reference: str) -> HostDelivery:
        """Return provider-facing text after the turn reaches `completed`."""
        ...


def _is_loopback_url(url: str) -> bool:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or not parsed.hostname
    ):
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def slack_presence_event(item: InboxItem) -> dict[str, Any]:
    """Map exact Slack facts into the frozen provider-neutral event shape."""

    thread_id = item.thread_ts or item.message_ts
    return {
        "source_event_id": item.provider_event_key,
        "provider": "slack",
        "account_id": item.team_id,
        "conversation_id": item.channel_id,
        "thread_id": thread_id,
        "conversation_key": f"slack:{item.ordering_key}",
        "actor": {
            "platform": "slack",
            "platform_actor_id": item.actor_user_id,
            "actor_team_id": item.actor_team_id or item.team_id,
        },
        "conversation": {
            "platform": "slack",
            "workspace_id": item.team_id,
            "enterprise_id": item.enterprise_id,
            "channel_id": item.channel_id,
            "channel_type": item.channel_type,
            "thread_ts": thread_id,
        },
        "message": {
            "message_id": item.message_ts,
            "thread_id": item.thread_ts,
            "event_id": item.event_id,
            "envelope_id": item.envelope_id,
            "event_ts": item.event_ts,
            "client_msg_id": item.client_msg_id,
            "event_type": item.event_type,
            "subtype": item.subtype,
            "attachments": [
                {
                    "file_id": str(file.get("file_id") or ""),
                    "file_name": str(file.get("name") or ""),
                    "mime_type": str(file.get("mimetype") or ""),
                    "file_size": int(file.get("size") or 0),
                }
                for file in item.files
            ],
        },
        "text": item.text,
    }


def _completed_reference(payload: Mapping[str, Any]) -> str:
    receipt = {
        "status": "completed",
        "outcome": str(payload.get("outcome") or ""),
        "text": str(payload.get("text") or ""),
        "turn_ref": str(payload.get("turn_ref") or ""),
        "work_ref": str(payload.get("work_ref") or ""),
    }
    raw = json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"completed:{encoded}"


def _decode_completed_reference(reference: str) -> dict[str, Any]:
    encoded = reference.removeprefix("completed:")
    encoded += "=" * (-len(encoded) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise HostContractError("Invalid persisted completed-turn receipt") from exc
    if not isinstance(value, dict) or value.get("status") != "completed":
        raise HostContractError("Invalid persisted completed-turn receipt")
    return value


def _deferred_reference(payload: Mapping[str, Any]) -> str:
    receipt = {
        "status": "deferred",
        "text": str(payload.get("text") or ""),
        "turn_ref": str(payload.get("turn_ref") or ""),
        "work_ref": str(payload.get("work_ref") or ""),
    }
    raw = json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"deferred:{encoded}"


def _decode_deferred_reference(reference: str) -> dict[str, Any]:
    encoded = reference.removeprefix("deferred:")
    encoded += "=" * (-len(encoded) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise HostContractError("Invalid persisted deferred-turn receipt") from exc
    if (
        not isinstance(value, dict)
        or value.get("status") != "deferred"
        or not str(value.get("work_ref") or "").strip()
    ):
        raise HostContractError("Invalid persisted deferred-turn receipt")
    return value


class LoopbackPresenceHostAdapter:
    def __init__(
        self,
        *,
        binding_id: str,
        host_service_url: str,
        skill_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        raw_binding_id = str(binding_id or "").strip()
        self.binding_id = normalize_binding_id(raw_binding_id) if raw_binding_id else ""
        self.host_service_url = str(host_service_url or "").rstrip("/")
        self._skill_token = str(skill_token or "").strip()
        self.available = bool(self.binding_id)
        if not _is_loopback_url(self.host_service_url):
            raise HostContractError("HOST_SERVICE_URL must be an HTTP loopback URL")
        if self.available and not self._skill_token:
            raise HostContractError("HOST_SERVICE_TOKEN is missing")
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), trust_env=False
        )
        self._closed = False
        self._terminal_work: dict[str, dict[str, Any]] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "X-Skill-Token": self._skill_token,
            "Content-Type": "application/json",
        }

    async def _json_response(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code in {403, 404}:
            raise HostBindingTerminalError(
                f"Presence binding was rejected by Host (HTTP {response.status_code})"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise HostContractError(
                f"Presence Host returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HostContractError("Presence Host returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HostContractError("Presence Host response must be a JSON object")
        return payload

    @staticmethod
    def _outcome(payload: Mapping[str, Any]) -> str:
        outcome = str(payload.get("outcome") or "").strip().lower()
        if outcome not in _OUTCOMES:
            raise HostContractError(f"Unknown presence outcome: {outcome or '<empty>'}")
        return outcome

    async def submit(self, event: InboxItem) -> str:
        if not self.available:
            raise HostAdapterUnavailable("binding_id is not configured")
        response = await self._http.post(
            f"{self.host_service_url}/presence/turn",
            headers=self._headers(),
            json={
                "binding_id": self.binding_id,
                "event": slack_presence_event(event),
                "staged_files": [
                    str(file.get("path") or "")
                    for file in event.staged_files
                    if str(file.get("path") or "").strip()
                ],
            },
            timeout=1800.0,
        )
        payload = await self._json_response(response)
        if str(payload.get("status") or "") != "completed":
            raise HostContractError("Presence Host did not complete the turn request")
        outcome = self._outcome(payload)
        if outcome == "deferred":
            work_ref = str(payload.get("work_ref") or "").strip()
            if not work_ref:
                raise HostContractError("Deferred presence turn omitted work_ref")
            return _deferred_reference(payload)
        return _completed_reference(payload)

    async def _poll_work(self, work_ref: str) -> dict[str, Any]:
        response = await self._http.get(
            f"{self.host_service_url}/presence/work/{quote(work_ref, safe='')}",
            headers=self._headers(),
            params={"binding_id": self.binding_id},
            timeout=35.0,
        )
        payload = await self._json_response(response)
        status = str(payload.get("status") or "").strip().lower()
        if status == "pending":
            return payload
        if status not in _TERMINAL_WORK_STATES:
            raise HostContractError(
                f"Unknown presence work status: {status or '<empty>'}"
            )
        self._outcome(payload)
        returned_ref = str(payload.get("work_ref") or "").strip()
        if returned_ref and returned_ref != work_ref:
            raise HostContractError("Presence Host returned a different work_ref")
        if status in _TERMINAL_WORK_STATES:
            self._terminal_work[work_ref] = payload
        return payload

    async def status(self, reference: str) -> HostTurnStatus:
        if reference.startswith("completed:"):
            _decode_completed_reference(reference)
            return HostTurnStatus("ready")
        if not reference.startswith("deferred:"):
            raise HostContractError("Unknown presence reference type")
        receipt = _decode_deferred_reference(reference)
        work_ref = str(receipt["work_ref"])
        text = str(receipt.get("text") or "")
        immediate = (text,) if text.strip() else ()
        payload = self._terminal_work.get(work_ref) or await self._poll_work(work_ref)
        status = str(payload.get("status") or "").strip().lower()
        if status == "completed":
            return HostTurnStatus("ready", texts=immediate)
        if status in {"failed", "cancelled"}:
            return HostTurnStatus(
                "failed",
                str(payload.get("error") or status),
                immediate,
            )
        return HostTurnStatus("pending", texts=immediate)

    async def deliver(self, reference: str) -> HostDelivery:
        if reference.startswith("completed:"):
            payload = _decode_completed_reference(reference)
        elif reference.startswith("deferred:"):
            work_ref = str(_decode_deferred_reference(reference)["work_ref"])
            payload = self._terminal_work.get(work_ref) or await self._poll_work(
                work_ref
            )
            if str(payload.get("status") or "").strip().lower() != "completed":
                raise HostContractError("Presence work is not completed")
        else:
            raise HostContractError("Unknown presence reference type")
        text = str(payload.get("text") or "")
        return HostDelivery((text,)) if text.strip() else HostDelivery()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()


def create_host_adapter(
    binding_id: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> PresenceHostAdapter:
    return LoopbackPresenceHostAdapter(
        binding_id=binding_id,
        host_service_url=os.environ.get("HOST_SERVICE_URL", "http://127.0.0.1:8767"),
        skill_token=os.environ.get("HOST_SERVICE_TOKEN", ""),
        http_client=http_client,
    )
