from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
from copy import Error as CopyError
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Protocol
from urllib.parse import quote, urlsplit

import httpx

from .events import DECLARED_FILE_KEYS, file_facts, provider_facts
from .store import InboxItem
from .provider_context import enrich_event

try:
    # The host's own opaque-credential wrapper. It is importable in the
    # extension child; a host-supervised companion runs a bare `python3` that
    # only receives HOST_SERVICE_TOKEN in its environment, so keep an identical
    # local mirror for that process instead of falling back to a raw string.
    from ouroboros.skill_token import SkillToken
except ImportError:  # companion process: the host package is not on sys.path

    class SkillToken:  # type: ignore[no-redef]
        """Local mirror of ``ouroboros.skill_token.SkillToken``.

        Same contract: the value is revealed only by ``use_in_request()`` and
        every stringify/copy/pickle path refuses instead of leaking it.
        """

        _REDACTED = "<SkillToken redacted>"

        def __init__(self, value: str) -> None:
            token = str(value or "").strip()
            if not token:
                raise ValueError("SkillToken cannot be empty")
            self._value = token

        @classmethod
        def from_env(cls, key: str = "HOST_SERVICE_TOKEN") -> "SkillToken":
            return cls(os.environ.get(key, ""))

        def use_in_request(self) -> str:
            """Explicitly reveal the token at an HTTP-auth call site."""
            return self._value

        def __repr__(self) -> str:
            return self._REDACTED

        def __str__(self) -> str:
            return self._REDACTED

        def __format__(self, _format_spec: str) -> str:
            return self._REDACTED

        def __reduce__(self) -> Any:
            raise TypeError("SkillToken cannot be pickled")

        def __reduce_ex__(self, _protocol: int) -> Any:
            raise TypeError("SkillToken cannot be pickled")

        def __copy__(self) -> "SkillToken":
            raise CopyError("SkillToken cannot be copied")

        def __deepcopy__(self, _memo: dict[int, Any]) -> "SkillToken":
            raise CopyError("SkillToken cannot be deep-copied")

        def __getstate__(self) -> dict[str, Any]:
            raise TypeError("SkillToken state is not serializable")


_OUTCOMES = frozenset({"message", "silent", "tool_delivered", "deferred"})
# Outcomes whose text is speech the transport sends; silent/tool_delivered send nothing.
_SPEECH = frozenset({"message", "deferred"})
_TERMINAL_WORK_STATES = frozenset({"completed", "failed", "cancelled"})
# A lost author is terminal for the transport: it is never restarted automatically.
_CONTINUATION_TERMINAL = _TERMINAL_WORK_STATES | {"interrupted"}
_BINDING_ID_RE = re.compile(r"[0-9a-f]{32}")


class HostAdapterUnavailable(RuntimeError):
    """Raised when the owner has not selected a presence binding."""


class HostContractError(RuntimeError):
    """Raised when the loopback presence endpoint violates its frozen contract."""


class HostBindingTerminalError(HostContractError):
    """Raised when the configured binding is missing or cannot admit the event."""


class HostRetryError(HostContractError):
    """Host explicitly permits retry of a turn that has no admitted work ref."""


def normalize_binding_id(value: Any) -> str:
    """Return one canonical Presence Binding ID or reject ambiguous input."""

    binding_id = str(value or "").strip()
    if not _BINDING_ID_RE.fullmatch(binding_id):
        raise HostContractError(
            "binding_id must be 32 lowercase hexadecimal characters"
        )
    return binding_id


@dataclass(frozen=True)
class HostOutput:
    """One Host-selected outward message and the identity it is queued under once.

    ``identity`` is ``output:<output_ref>`` for a Host-minted output, otherwise a
    stable reference-derived fallback; never the text, since an identical
    correction is new speech.
    """

    identity: str
    text: str
    turn_ref: str = ""
    output_ref: str = ""


@dataclass(frozen=True)
class HostTurnStatus:
    state: str
    error: str = ""
    texts: tuple[str, ...] = ()
    delivery_reporting_version: int = 0
    turn_ref: str = ""
    # A continuing author's released outputs, its terminal output and its
    # promoted child's result, in the order observed (Presence continuation).
    outputs: tuple[HostOutput, ...] = ()
    transport_queue_recorded: bool = False
    # Checkpoint newly discovered references before advancing the poll iterator.
    host_reference: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in {"ready", "failed"}


@dataclass(frozen=True)
class HostDelivery:
    texts: tuple[str, ...] = ()
    delivery_reporting_version: int = 0
    turn_ref: str = ""
    outputs: tuple[HostOutput, ...] = ()


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


def _as_skill_token(value: "SkillToken | str | None") -> "SkillToken | None":
    """Hold the Host Service credential as a SkillToken, never as a raw string."""

    if value is None or isinstance(value, SkillToken):
        return value
    try:
        return SkillToken(str(value))
    except ValueError:
        return None


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


def _staged_path(record: Mapping[str, Any]) -> str:
    """The one predicate for bytes the bridge staged and submits to Host.

    A nonblank path is the name the bridge wrote and is kept byte-for-byte;
    whitespace only decides whether there is a path at all.
    """
    path = str(record.get("path") or "")
    return path if path.strip() else ""


def _attachments(item: InboxItem) -> list[dict[str, Any]]:
    """Describe every declared file and what this bridge's staging observed.

    ``content_available`` means the bridge staged bytes; the Host attachment
    manifest remains authoritative for what the model can actually open.
    """
    attachments = []
    for index, file in enumerate(item.files):
        outcome = item.staged_files[index] if index < len(item.staged_files) else {}
        path = _staged_path(outcome)
        entry = {
            "file_id": str(file.get("file_id") or ""),
            "file_name": str(file.get("name") or ""),
            "mime_type": str(file.get("mimetype") or ""),
            "file_size": int(file.get("size") or 0),
            **{key: value for key, value in file_facts(file).items() if key not in DECLARED_FILE_KEYS},
            "content_available": bool(path),
        }
        if path:
            # The staged basename; Host derives its attachment label from it
            # and may shorten or normalize that label.
            entry["staged_as"] = os.path.basename(path)
        if outcome.get("stage_error"):
            entry["stage_error"] = str(outcome["stage_error"])
            details = outcome.get("stage_error_details")
            entry["stage_error_details"] = dict(details) if isinstance(details, Mapping) else {}
        attachments.append(entry)
    return attachments


def slack_presence_event(item: InboxItem, *, transport_queue: bool = False) -> dict[str, Any]:
    """Map exact Slack facts into the frozen provider-neutral event shape.

    ``transport_queue`` adds the inbox snapshot taken before the event's first
    submission attempt (``BridgeStore.conversation_queue``) to
    ``event.conversation``; only a continuation-capable Host is sent it.
    """

    thread_id = item.thread_ts or item.message_ts
    event = {
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
            "attachments": _attachments(item),
            "blocks": list(item.structured.get("blocks") or []),
            "provider_facts": provider_facts(item.structured),
        },
        "text": item.text,
    }
    event = enrich_event(event, item.provider_context)
    if transport_queue and item.transport_queue is not None:
        event["conversation"]["transport_queue"] = dict(item.transport_queue)
    return event


def _encode_receipt(status: str, receipt: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(receipt), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{status}:{encoded}"


def _decode_receipt(reference: str, status: str, label: str) -> dict[str, Any]:
    encoded = reference.removeprefix(f"{status}:")
    encoded += "=" * (-len(encoded) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise HostContractError(f"Invalid persisted {label} receipt") from exc
    if not isinstance(value, dict) or value.get("status") != status:
        raise HostContractError(f"Invalid persisted {label} receipt")
    return value


def _completed_reference(payload: Mapping[str, Any]) -> str:
    return _encode_receipt("completed", {
        "status": "completed",
        "outcome": str(payload.get("outcome") or ""),
        "text": str(payload.get("text") or ""),
        "turn_ref": str(payload.get("turn_ref") or ""),
        "work_ref": str(payload.get("work_ref") or ""),
        "delivery_reporting_version": _reporting_version(payload),
        "continuation_version": 1 if payload.get("continuation_version") == 1 else 0,
        "output_ref": str(payload.get("output_ref") or ""),
    })


def _decode_completed_reference(reference: str) -> dict[str, Any]:
    return _decode_receipt(reference, "completed", "completed-turn")


def _deferred_reference(payload: Mapping[str, Any]) -> str:
    return _encode_receipt("deferred", {
        "status": "deferred",
        "text": str(payload.get("text") or ""),
        "turn_ref": str(payload.get("turn_ref") or ""),
        "work_ref": str(payload.get("work_ref") or ""),
        "delivery_reporting_version": _reporting_version(payload),
        "continuation_version": 1 if payload.get("continuation_version") == 1 else 0,
        "output_ref": str(payload.get("output_ref") or ""),
    })


def _decode_deferred_reference(reference: str) -> dict[str, Any]:
    value = _decode_receipt(reference, "deferred", "deferred-turn")
    if not str(value.get("work_ref") or "").strip():
        raise HostContractError("Invalid persisted deferred-turn receipt")
    return value


def _continuing_reference(payload: Mapping[str, Any]) -> str:
    """The write-once initial envelope of a live author, and its promoted child's ref."""
    return _encode_receipt("continuing", {
        "status": "continuing",
        "continuation_ref": str(payload.get("continuation_ref") or "").strip(),
        "turn_ref": str(payload.get("turn_ref") or ""),
        "outcome": str(payload.get("outcome") or ""),
        "text": str(payload.get("text") or ""),
        "output_ref": str(payload.get("output_ref") or ""),
        "work_ref": str(payload.get("work_ref") or "").strip(),
        "delivery_reporting_version": _reporting_version(payload),
    })


def _decode_continuing_reference(reference: str) -> dict[str, Any]:
    value = _decode_receipt(reference, "continuing", "continuing-turn")
    if not str(value.get("continuation_ref") or "").strip():
        raise HostContractError("Invalid persisted continuing-turn receipt")
    return value


def _refused_reference(payload: Mapping[str, Any], http_status: int, reporting_version: int) -> str:
    # The refusal belongs to the original event, even if its admitted child
    # later succeeds. Keep the complete typed response, not just a log string.
    return _encode_receipt("refused", {
        "status": "refused", "http_status": http_status, "response": dict(payload),
        "delivery_reporting_version": reporting_version,
    })


def _decode_refused_reference(reference: str) -> dict[str, Any]:
    receipt = _decode_receipt(reference, "refused", "refused-turn")
    if not isinstance(receipt.get("response"), dict):
        raise HostContractError("Invalid persisted refused-turn receipt")
    return receipt


def _refusal_error(payload: Mapping[str, Any], http_status: int) -> str:
    reason = str(payload.get("error") or "")
    code = str(payload.get("code") or "")
    return (f"Presence Host {payload.get('disposition')} (HTTP {http_status}, {code})"
            + (f": {reason}" if reason else ""))


def _reporting_version(payload: Mapping[str, Any]) -> int:
    return 1 if payload.get("delivery_reporting_version") == 1 else 0


def _output_identity(output_ref: str, fallback: str) -> str:
    return f"output:{output_ref}" if output_ref else fallback


def _released_outputs(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Validate a continuation poll's ordered ``outputs`` without dropping any."""
    rows = payload.get("outputs", [])
    if not isinstance(rows, list):
        raise HostContractError("Presence continuation outputs must be a list")
    outputs = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise HostContractError("Presence continuation output must be an object")
        output_ref = row.get("output_ref")
        outcome = str(row.get("outcome") or "").strip().lower()
        if not isinstance(output_ref, str) or not output_ref.strip() or not isinstance(row.get("text"), str):
            raise HostContractError("Presence continuation output lacks output_ref or text")
        if outcome not in _OUTCOMES:
            raise HostContractError(f"Unknown presence outcome: {outcome or '<empty>'}")
        outputs.append({"output_ref": output_ref.strip(), "outcome": outcome, "text": row["text"]})
    return outputs


class LoopbackPresenceHostAdapter:
    def __init__(
        self,
        *,
        binding_id: str,
        host_service_url: str,
        skill_token: "SkillToken | str | None",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        raw_binding_id = str(binding_id or "").strip()
        self.binding_id = normalize_binding_id(raw_binding_id) if raw_binding_id else ""
        self.host_service_url = str(host_service_url or "").rstrip("/")
        self._skill_token = _as_skill_token(skill_token)
        self.available = bool(self.binding_id)
        if not _is_loopback_url(self.host_service_url):
            raise HostContractError("HOST_SERVICE_URL must be an HTTP loopback URL")
        if self.available and self._skill_token is None:
            raise HostContractError("HOST_SERVICE_TOKEN is missing")
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), trust_env=False
        )
        self._closed = False
        self._terminal_work: dict[str, dict[str, Any]] = {}
        self.delivery_reporting_version = 0
        self.delivery_reporting_status = "unknown"
        # Discovered with delivery reporting from the same /identity answer.
        self.continuation_version = 0

    async def discover_delivery_support(self) -> int:
        if self.delivery_reporting_status in {"supported", "unsupported"}:
            return self.delivery_reporting_version
        if self._skill_token is None:
            # No owner binding means no credential: never probe Host unauthenticated.
            self.delivery_reporting_version = 0
            self.continuation_version = 0
            self.delivery_reporting_status = "unavailable"
            return 0
        try:
            response = await self._http.get(
                f"{self.host_service_url}/identity",
                headers={
                    "X-Skill-Token": self._skill_token.use_in_request(),
                    "Content-Type": "application/json",
                },
                timeout=5.0,
            )
            payload = await self._json_response(response)
            self.delivery_reporting_version = 1 if payload.get("presence_delivery_version") == 1 else 0
            self.continuation_version = 1 if payload.get("presence_continuation_version") == 1 else 0
            self.delivery_reporting_status = "supported" if self.delivery_reporting_version else "unsupported"
        except (HostContractError, httpx.HTTPError):
            # Capability discovery must never prevent sending on an old or
            # temporarily unavailable Host. The next operation can retry it.
            self.delivery_reporting_version = 0
            self.continuation_version = 0
            self.delivery_reporting_status = "unavailable"
        return self.delivery_reporting_version

    async def report_delivery(self, payload: Mapping[str, Any]) -> None:
        if self._skill_token is None:
            raise HostContractError("HOST_SERVICE_TOKEN is missing")
        response = await self._http.post(
            f"{self.host_service_url}/presence/delivery",
            headers={
                "X-Skill-Token": self._skill_token.use_in_request(),
                "Content-Type": "application/json",
            },
            json=dict(payload), timeout=10.0,
        )
        result = await self._json_response(response)
        if result.get("ok") is not True or result.get("recorded") is not True:
            raise HostContractError("Presence delivery report was not acknowledged")

    async def _json_response(self, response: httpx.Response, *, turn_response: bool = False) -> dict[str, Any]:
        # Host chooses recovery via disposition, not the HTTP status class. A
        # typed refusal may retain admitted work even though the turn failed.
        if turn_response and 400 <= response.status_code < 600:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if (isinstance(payload, dict) and payload.get("ok") is False
                    and str(payload.get("disposition") or "") in {"rejected", "blocked", "retry"}):
                return payload
        if response.status_code in {403, 404}:
            raise HostBindingTerminalError(
                f"Presence binding was rejected by Host (HTTP {response.status_code})"
            )
        if not 200 <= response.status_code < 300:
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
        if self._skill_token is None:
            raise HostContractError("HOST_SERVICE_TOKEN is missing")
        mode = await self.discover_delivery_support()
        continuing = self.continuation_version == 1
        response = await self._http.post(
            f"{self.host_service_url}/presence/turn",
            headers={
                "X-Skill-Token": self._skill_token.use_in_request(),
                "Content-Type": "application/json",
            },
            json={
                "binding_id": self.binding_id,
                "event": slack_presence_event(event, transport_queue=continuing),
                "staged_files": [
                    _staged_path(file) for file in event.staged_files if _staged_path(file)
                ],
                **({"delivery_reporting_version": 1} if mode else {}),
                **({"continuation_version": 1} if continuing else {}),
            },
            timeout=1800.0,
        )
        payload = await self._json_response(response, turn_response=True)
        status = str(payload.get("status") or "")
        disposition = str(payload.get("disposition") or "")
        if payload.get("ok") is False and disposition in {"rejected", "blocked", "retry"}:
            if disposition == "retry" and not str(payload.get("work_ref") or "").strip():
                raise HostRetryError(_refusal_error(payload, response.status_code))
            return _refused_reference(payload, response.status_code, mode)
        if continuing and (status == "continuing" or (
                status == "completed" and str(payload.get("continuation_ref") or "").strip())):
            # The author answered early and lives on (or ended after releasing outputs):
            # its released outputs, terminal and child are all read from its poll.
            if payload.get("continuation_version") != 1 or not str(payload.get("continuation_ref") or "").strip():
                raise HostContractError("Continuing presence turn omitted its continuation reference")
            self._outcome(payload)
            return _continuing_reference(payload)
        if status != "completed":
            raise HostContractError("Presence Host did not complete the turn request")
        outcome = self._outcome(payload)
        if outcome == "deferred":
            work_ref = str(payload.get("work_ref") or "").strip()
            if not work_ref:
                raise HostContractError("Deferred presence turn omitted work_ref")
            return _deferred_reference(payload)
        return _completed_reference(payload)

    async def _get_work(self, work_ref: str) -> dict[str, Any]:
        if self._skill_token is None:
            raise HostContractError("HOST_SERVICE_TOKEN is missing")
        response = await self._http.get(
            f"{self.host_service_url}/presence/work/{quote(work_ref, safe='')}",
            headers={
                "X-Skill-Token": self._skill_token.use_in_request(),
                "Content-Type": "application/json",
            },
            params={"binding_id": self.binding_id},
            timeout=35.0,
        )
        return await self._json_response(response)

    async def _poll_continuation(self, continuation_ref: str) -> dict[str, Any]:
        """One continuing author's poll; a legacy-shaped answer is refused, never guessed."""
        payload = await self._get_work(continuation_ref)
        status = str(payload.get("status") or "").strip().lower()
        if payload.get("continuation_version") != 1:
            raise HostContractError("Presence Host answered a continuation poll without continuation_version 1")
        returned_ref = str(payload.get("continuation_ref") or payload.get("work_ref") or "").strip()
        if returned_ref and returned_ref != continuation_ref:
            raise HostContractError("Presence Host returned a different continuation_ref")
        payload["outputs"] = _released_outputs(payload)
        if status == "pending":
            return payload
        if status not in _CONTINUATION_TERMINAL:
            raise HostContractError(f"Unknown presence work status: {status or '<empty>'}")
        if status != "interrupted":
            self._outcome(payload)
        self._terminal_work[continuation_ref] = payload
        return payload

    async def _poll_work(self, work_ref: str) -> dict[str, Any]:
        payload = await self._get_work(work_ref)
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

    async def _continuation_updates(self, receipt: Mapping[str, Any],
                                    transport_queue: Mapping[str, Any] | None = None) -> AsyncIterator[HostTurnStatus]:
        """Release known speech first, then each independent poll as it arrives.

        Poll tasks belong to this iterator and are cancelled/awaited on close. No
        detached poll or new queue survives the existing inbox worker's custody.
        """
        ref = str(receipt["continuation_ref"])
        mode = _reporting_version(receipt)
        outputs: dict[str, HostOutput] = {}
        transient: list[str] = []
        rejected: list[str] = []
        failures: list[str] = []
        pending = False
        queue_recorded = False
        tasks: dict[asyncio.Task, tuple[str, bool]] = {}
        seen: set[str] = set()
        work_refs = list(dict.fromkeys([
            str(receipt.get("work_ref") or "").strip(),
            *[str(value).strip() for value in receipt.get("work_refs", [])],
        ]))
        work_refs = [source for source in work_refs if source and source != ref]
        reference = _encode_receipt("continuing", receipt)

        def add(identity: str, text: str, turn_ref: str, output_ref: str = "") -> None:
            if text.strip() and identity not in outputs:
                outputs[identity] = HostOutput(identity, text, turn_ref, output_ref)

        def snapshot(*, final: bool = False) -> HostTurnStatus:
            state, error = "pending", "; ".join(transient + rejected + failures)
            if final:
                if not transient and not pending:
                    state = "failed" if rejected or failures else "ready"
            return HostTurnStatus(state, error, (), mode, ref, tuple(outputs.values()), queue_recorded, reference)

        async def poll(source: str, author: bool) -> dict[str, Any]:
            nonlocal queue_recorded
            cached = self._terminal_work.get(source)
            if cached is not None:
                return cached
            if author and transport_queue is not None:
                await self.refresh_transport_queue(source, transport_queue)
                queue_recorded = True
            return await (self._poll_continuation(source) if author else self._poll_work(source))

        def start(source: str, *, author: bool = False) -> None:
            source = source.strip()
            if source and source not in seen:
                seen.add(source)
                tasks[asyncio.create_task(poll(source, author))] = (source, author)

        if str(receipt.get("outcome") or "") in _SPEECH:
            output_ref = str(receipt.get("output_ref") or "")
            add(_output_identity(output_ref, f"initial:{ref}"), str(receipt.get("text") or ""), ref, output_ref)
        # This yield precedes even starting an HTTP poll. Runtime commits these
        # outputs before it requests the next update.
        yield snapshot()
        start(ref, author=True)
        for work_ref in work_refs:
            start(work_ref)
        try:
            while tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    source, author = tasks.pop(task)
                    try:
                        payload = task.result()
                    except HostBindingTerminalError as exc:
                        rejected.append(str(exc))
                        continue
                    except (HostContractError, httpx.HTTPError) as exc:
                        transient.append(f"{'continuation' if author else 'work'} {source}: {exc or type(exc).__name__}")
                        continue
                    state = str(payload.get("status") or "").strip().lower()
                    if author:
                        for row in payload["outputs"]:
                            if row["outcome"] in _SPEECH:
                                add(f"output:{row['output_ref']}", row["text"], ref, row["output_ref"])
                        # Promotion can happen on any later reentry, including a
                        # park whose author subsequently dies. Retain the child
                        # before treating the author as pending or interrupted.
                        child = str(payload.get("child_work_ref") or "").strip()
                        if child and child != ref and child not in work_refs:
                            work_refs.append(child)
                            reference = _encode_receipt("continuing", {**receipt, "work_refs": work_refs})
                            # Runtime persists this before the child request can
                            # run; restart then polls it independently of author.
                            yield snapshot()
                        start(child)
                    if state == "pending":
                        pending = True
                        continue
                    if author:
                        if state != "interrupted" and self._outcome(payload) in _SPEECH:
                            output_ref = str(payload.get("output_ref") or "")
                            add(_output_identity(output_ref, f"terminal:{ref}"), str(payload.get("text") or ""), ref, output_ref)
                    elif state == "completed" and self._outcome(payload) == "message":
                        output_ref = str(payload.get("output_ref") or "")
                        add(_output_identity(output_ref, f"work:{source}"), str(payload.get("text") or ""),
                            str(payload.get("turn_ref") or source), output_ref)
                    if state == "interrupted":
                        failures.append("Presence author was interrupted; it is not restarted automatically")
                    elif state != "completed":
                        failures.append(str(payload.get("error") or f"Presence work {state}"))
                yield snapshot(final=not tasks)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def refresh_transport_queue(self, continuation_ref: str, snapshot: Mapping[str, Any]) -> None:
        """Report dated inbox facts on the existing binding-scoped work route."""
        if self._skill_token is None:
            raise HostContractError("HOST_SERVICE_TOKEN is missing")
        response = await self._http.post(
            f"{self.host_service_url}/presence/work/{quote(continuation_ref, safe='')}",
            headers={"X-Skill-Token": self._skill_token.use_in_request(), "Content-Type": "application/json"},
            json={"binding_id": self.binding_id, "transport_queue": dict(snapshot)}, timeout=10.0,
        )
        payload = await self._json_response(response)
        if payload.get("ok") is not True or payload.get("status") not in {"recorded", "duplicate", "stale"}:
            raise HostContractError("Presence transport queue observation was not acknowledged")

    async def status_updates(self, reference: str, *,
                             transport_queue: Mapping[str, Any] | None = None) -> AsyncIterator[HostTurnStatus]:
        """Available outputs before network waits, and then every finished poll."""
        if reference.startswith("refused:"):
            receipt = _decode_refused_reference(reference)
            response = receipt["response"]
            error = _refusal_error(response, receipt["http_status"])
            work_ref = str(response.get("work_ref") or "").strip()
            mode = _reporting_version(receipt)
            if not work_ref:
                yield HostTurnStatus("failed", error)
                return
            # A refusal's text is diagnostic, never a selected outward message.
            yield HostTurnStatus("pending", error)
            try:
                payload = self._terminal_work.get(work_ref) or await self._poll_work(work_ref)
            except HostBindingTerminalError as exc:
                yield HostTurnStatus("failed", f"{error}; work {work_ref}: {exc}")
                return
            except (HostContractError, httpx.HTTPError) as exc:
                yield HostTurnStatus("pending", f"{error}; work {work_ref}: {exc or type(exc).__name__}")
                return
            state = str(payload.get("status") or "").strip().lower()
            outputs: tuple[HostOutput, ...] = ()
            if state == "completed" and self._outcome(payload) == "message":
                text = str(payload.get("text") or "")
                output_ref = str(payload.get("output_ref") or "")
                if text.strip():
                    outputs = (HostOutput(_output_identity(output_ref, f"work:{work_ref}"), text,
                                          str(payload.get("turn_ref") or work_ref), output_ref),)
            elif state in {"failed", "cancelled"}:
                error += f"; work {work_ref}: {payload.get('error') or state}"
            # Finishing the child never resolves the original event's refusal.
            yield HostTurnStatus("pending" if state == "pending" else "failed", error,
                                 delivery_reporting_version=mode, outputs=outputs)
        elif reference.startswith("continuing:"):
            updates = self._continuation_updates(_decode_continuing_reference(reference), transport_queue)
            try:
                async for update in updates:
                    yield update
            finally:
                await updates.aclose()
        else:
            if reference.startswith("deferred:"):
                receipt = _decode_deferred_reference(reference)
                yield self._initial_status(receipt, "pending")
            yield await self.status(reference)

    @staticmethod
    def _initial_status(receipt: Mapping[str, Any], state: str, error: str = "") -> HostTurnStatus:
        text, ref = str(receipt.get("text") or ""), str(receipt.get("turn_ref") or "")
        mode = _reporting_version(receipt)
        if receipt.get("continuation_version") == 1:
            output_ref = str(receipt.get("output_ref") or "")
            speech = receipt.get("status") == "deferred" or receipt.get("outcome") in _SPEECH
            outputs = (HostOutput(_output_identity(output_ref, f"initial:{ref}"), text, ref, output_ref),) if speech and text.strip() else ()
            return HostTurnStatus(state, error, (), mode, ref, outputs)
        texts = (text,) if receipt.get("status") == "deferred" and text.strip() else ()
        return HostTurnStatus(state, error, texts, mode, ref)

    async def status(self, reference: str) -> HostTurnStatus:
        if reference.startswith("completed:"):
            receipt = _decode_completed_reference(reference)
            return self._initial_status(receipt, "ready")
        if reference.startswith(("continuing:", "refused:")):
            async for update in self.status_updates(reference):
                status = update
            return status
        if not reference.startswith("deferred:"):
            raise HostContractError("Unknown presence reference type")
        receipt = _decode_deferred_reference(reference)
        work_ref = str(receipt["work_ref"])
        payload = self._terminal_work.get(work_ref) or await self._poll_work(work_ref)
        state = str(payload.get("status") or "").strip().lower()
        if state == "completed":
            return self._initial_status(receipt, "ready")
        if state in {"failed", "cancelled"}:
            return self._initial_status(receipt, "failed", str(payload.get("error") or state))
        return self._initial_status(receipt, "pending")

    async def deliver(self, reference: str) -> HostDelivery:
        if reference.startswith("continuing:"):
            # Every output of a continuation is already reported by ``status``.
            receipt = _decode_continuing_reference(reference)
            return HostDelivery((), _reporting_version(receipt), str(receipt["continuation_ref"]))
        if reference.startswith("completed:"):
            payload = _decode_completed_reference(reference)
            receipt = payload
            if receipt.get("continuation_version") == 1:
                # Its selected identity was already emitted by status_updates.
                return HostDelivery((), _reporting_version(receipt), str(receipt.get("turn_ref") or ""))
        elif reference.startswith("deferred:"):
            receipt = _decode_deferred_reference(reference)
            work_ref = str(receipt["work_ref"])
            payload = self._terminal_work.get(work_ref) or await self._poll_work(
                work_ref
            )
            if str(payload.get("status") or "").strip().lower() != "completed":
                raise HostContractError("Presence work is not completed")
        else:
            raise HostContractError("Unknown presence reference type")
        text = (
            str(payload.get("text") or "")
            if self._outcome(payload) == "message"
            else ""
        )
        turn_ref = str(payload.get("turn_ref") or receipt.get("work_ref") or receipt.get("turn_ref") or "")
        if receipt.get("continuation_version") == 1:
            output_ref = str(payload.get("output_ref") or "")
            output = HostOutput(_output_identity(output_ref, f"work:{receipt.get('work_ref')}"), text, turn_ref, output_ref)
            return HostDelivery((), _reporting_version(receipt), turn_ref, (output,) if text.strip() else ())
        return HostDelivery((text,) if text.strip() else (), _reporting_version(receipt), turn_ref)

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
    try:
        # The host injects HOST_SERVICE_TOKEN into this companion's environment;
        # SkillToken.from_env is the host's own reader for exactly that variable.
        skill_token: SkillToken | None = SkillToken.from_env("HOST_SERVICE_TOKEN")
    except ValueError:
        skill_token = None
    return LoopbackPresenceHostAdapter(
        binding_id=binding_id,
        host_service_url=os.environ.get("HOST_SERVICE_URL", "http://127.0.0.1:8767"),
        skill_token=skill_token,
        http_client=http_client,
    )
