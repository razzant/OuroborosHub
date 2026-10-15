"""Non-owner Telegram correspondence through the Host Service Presence boundary.

This module does not acquire a Presence binding or authenticate Telegram users as the
owner. The caller supplies an owner-created binding, a skill-scoped Host token, and
provider facts. It persists/replays inbound requests and outbound send intents outside
this module; a timeout here is *not* proof that a turn or send failed.
"""

from __future__ import annotations

from copy import Error as CopyError
from dataclasses import dataclass
import json
import os
from typing import Any, Callable
from urllib import error, parse, request


Transport = Callable[[str, str, dict[str, Any] | None, dict[str, str]], tuple[int, dict[str, Any]]]
_EVENT_FIELDS = frozenset({
    "source_event_id", "provider", "account_id", "conversation_id", "thread_id",
    "conversation_key", "actor", "conversation", "message", "text",
})


class PresenceToken:
    """Opaque Host token for an isolated companion without host-package imports.

    Keep the environment read and the revealed request header out of persistent
    client attributes. This mirrors ouroboros.skill_token.SkillToken's redaction
    and serialization contract while remaining importable from the skill runtime.
    """

    _REDACTED = "<PresenceToken redacted>"

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Host Service token is required")
        self._value = value.strip()

    @classmethod
    def from_env(cls, key: str = "HOST_SERVICE_TOKEN") -> PresenceToken:
        return cls(os.environ.get(key, ""))

    def use_in_request(self) -> str:
        """Reveal only at the HTTP request boundary."""
        return self._value

    def __repr__(self) -> str:
        return self._REDACTED

    def __str__(self) -> str:
        return self._REDACTED

    def __format__(self, _format_spec: str) -> str:
        return self._REDACTED

    def __reduce__(self) -> Any:
        raise TypeError("PresenceToken cannot be pickled")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("PresenceToken cannot be pickled")

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError("PresenceToken state is not serializable")

    def __copy__(self) -> PresenceToken:
        raise CopyError("PresenceToken cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> PresenceToken:
        raise CopyError("PresenceToken cannot be deep-copied")


def _number(value: int, label: str, *, signed: bool = False) -> int:
    if type(value) is not int or (value == 0 if signed else value <= 0):
        raise ValueError(f"{label} must be a nonzero integer" if signed else f"{label} must be a positive integer")
    return value


def build_event(
    account_id: int, peer_id: int, message_id: int, actor_id: int, text: str, *,
    topic_id: int | None = None, provider: str = "telegram",
    conversation: dict[str, Any] | None = None, message: dict[str, Any] | None = None,
    event_kind: str = "new", revision: str = "",
) -> dict[str, Any]:
    """Build the exact Host event from numeric Telegram identities.

    ``topic_id=None`` is General/root (the Host identity uses ``0``); a forum
    topic uses its positive root message id. The caller must classify the topic
    against its verified forum catalog before calling this function. Telegram
    message ids are peer-local, hence account+peer+topic are part of the stable
    event id. For an edit, ``revision`` is a stable provider edit timestamp
    (for example, UTC ISO-8601 with microseconds) supplied by the caller;
    repeated delivery of one revision must reuse exactly the same payload.
    Changed text with the same revision causes a Host identity conflict rather
    than silently overwriting the first observed event.
    """
    _number(account_id, "account_id")
    _number(peer_id, "peer_id", signed=True)
    _number(message_id, "message_id")
    _number(actor_id, "actor_id", signed=True)
    if topic_id is not None:
        _number(topic_id, "topic_id")
    if not isinstance(provider, str) or not provider or provider != provider.strip() or ":" in provider:
        raise ValueError("provider must be a nonempty unambiguous transport name")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(event_kind, str) or event_kind not in {"new", "edit"}:
        raise ValueError("event_kind must be new or edit")
    if not isinstance(revision, str) or (event_kind == "new" and revision) or (
        event_kind == "edit" and (not revision or revision != revision.strip())
    ):
        raise ValueError("edits require a stable nonempty revision; new events must not have one")
    if conversation is not None and not isinstance(conversation, dict):
        raise TypeError("conversation must be an object")
    if message is not None and not isinstance(message, dict):
        raise TypeError("message must be an object")
    thread = str(topic_id) if topic_id is not None else ""
    account, peer = str(account_id), str(peer_id)
    event_id = f"{provider}:{account}:{peer}:{thread or '0'}:{message_id}"
    if event_kind == "edit":
        event_id += ":edit:" + parse.quote(revision, safe="")
    message_facts = {**(message or {}), "message_id": str(message_id)}
    if event_kind == "edit":
        message_facts.update({"event_kind": "edit", "revision": revision})
    return {
        "source_event_id": event_id,
        "provider": provider, "account_id": account, "conversation_id": peer,
        "thread_id": thread,
        "conversation_key": f"{provider}:{account}:{peer}:{thread or '0'}",
        "actor": {"platform_actor_id": str(actor_id)},
        "conversation": dict(conversation or {}),
        # Provider identity cannot be replaced by untrusted auxiliary fields.
        "message": message_facts,
        "text": text,
    }


@dataclass(frozen=True)
class PresenceResult:
    """Host outcome, with ambiguous transport failure represented as retry."""

    kind: str  # completed, pending, retry, blocked, rejected
    outcome: str = ""  # message, silent, tool_delivered, deferred on completed turns
    text: str = ""
    turn_ref: str = ""
    work_ref: str = ""
    delivery_reporting_version: int = 0
    code: str = ""
    error: str = ""
    retry_after: float | None = None


def _urllib_transport(base_url: str, *, timeout: float) -> Transport:
    url = base_url.rstrip("/")
    parsed = parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Host Service URL must be loopback")
    class NoRedirect(request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # urllib normally forwards custom request headers across redirects.
            # Even a compromised loopback endpoint must not relay its token to
            # an arbitrary remote Location before the caller can inspect it.
            return None

    # Host tokens must never transit an ambient HTTP_PROXY, even if the
    # operator omitted NO_PROXY for loopback.
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect)
    def send(method: str, path: str, payload: dict[str, Any] | None,
             headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        req = request.Request(url + path, data=body, method=method,
                              headers={**headers, "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            # A typed Host error is an ordinary response, not a retry exception.
            return exc.code, json.loads(exc.read())

    return send


class PresenceClient:
    """A thin Host transport; caller owns durable inbound/outbound custody."""

    def __init__(self, base_url: str, token: PresenceToken, binding_id: str, *,
                 transport: Transport | None = None, timeout: float = 30.0) -> None:
        if not isinstance(binding_id, str) or not binding_id.strip():
            raise ValueError("an owner-created binding_id is required")
        if type(token) is not PresenceToken:
            raise TypeError("an opaque PresenceToken is required")
        self.binding_id = binding_id
        self._token = token
        self._transport = transport or _urllib_transport(base_url, timeout=timeout)

    def __repr__(self) -> str:
        return f"PresenceClient(binding_id={self.binding_id!r}, token=<redacted>)"

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError("PresenceClient state is not serializable")

    def delivery_capability(self) -> tuple[int, str]:
        """Negotiate once at startup, BEFORE requesting receipt-bearing turns.

        An unreadable identity never licenses version 1. Legacy hosts may lack
        the field; both that case and an unavailable read are surfaced to the
        owner separately from a successful receipt.
        """
        try:
            status, body = self._transport("GET", "/identity", None, {
                "x-skill-token": self._token.use_in_request(),
            })
        except (OSError, TimeoutError, ValueError, TypeError):
            return 0, "identity_unavailable"
        if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            return 0, "identity_unavailable"
        if body.get("presence_delivery_version") == 1:
            return 1, "available"
        return 0, "host_does_not_advertise_delivery"

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> PresenceResult:
        try:
            # The revealed string and header exist only for this transport call;
            # neither is retained on the client or included in error diagnostics.
            status, body = self._transport(method, path, payload, {
                "x-skill-token": self._token.use_in_request(),
            })
        except (OSError, TimeoutError, ValueError, TypeError):
            # A POST may already have landed. Only repeat the *identical* event or receipt.
            return PresenceResult("retry", code="transport_unknown", error="Host transport unavailable")
        if not isinstance(body, dict):
            return PresenceResult("retry", code="protocol_invalid", error="Host response is not an object")
        if body.get("ok") is True:
            host_status = body.get("status")
            if status == 202 and host_status == "pending" and isinstance(body.get("work_ref"), str):
                return PresenceResult("pending", work_ref=body["work_ref"])
            if status == 200 and host_status in {"failed", "cancelled"}:
                return PresenceResult("blocked", outcome=str(body.get("outcome") or ""),
                                      work_ref=str(body.get("work_ref") or ""),
                                      code="presence_work_" + host_status)
            if status == 200 and host_status == "completed" and body.get("outcome") in {
                "message", "silent", "tool_delivered", "deferred"
            } and isinstance(body.get("text"), str):
                version = body.get("delivery_reporting_version", 0)
                if type(version) is not int or version not in (0, 1):
                    return PresenceResult("retry", code="protocol_invalid", error="invalid delivery version")
                return PresenceResult("completed", outcome=body["outcome"], text=body["text"],
                                      turn_ref=str(body.get("turn_ref") or ""),
                                      work_ref=str(body.get("work_ref") or ""),
                                      delivery_reporting_version=version)
            if status == 200 and body.get("recorded") is True:
                return PresenceResult("completed")
            return PresenceResult("retry", code="protocol_invalid", error="unexpected Host success shape")
        disposition = body.get("disposition")
        if disposition in {"retry", "blocked", "rejected"} and body.get("ok") is False:
            return PresenceResult(disposition, code=str(body.get("code") or ""),
                                  error=str(body.get("error") or ""),
                                  turn_ref=str(body.get("turn_ref") or ""),
                                  work_ref=str(body.get("work_ref") or ""))
        return PresenceResult("retry", code="protocol_invalid", error="untyped Host response")

    def turn(self, event: dict[str, Any], *, delivery_reporting_version: int = 0) -> PresenceResult:
        if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
            raise ValueError("event must have exactly the Host Presence event fields")
        if type(delivery_reporting_version) is not int or delivery_reporting_version not in (0, 1):
            raise ValueError("delivery_reporting_version must be 0 or 1")
        return self._call("POST", "/presence/turn", {
            "binding_id": self.binding_id, "event": dict(event),
            "delivery_reporting_version": delivery_reporting_version,
        })

    def work(self, work_ref: str) -> PresenceResult:
        if not isinstance(work_ref, str) or not work_ref or "/" in work_ref:
            raise ValueError("invalid work_ref")
        path = "/presence/work/" + parse.quote(work_ref, safe="") + "?binding_id=" + parse.quote(self.binding_id, safe="")
        return self._call("GET", path)

    def report_delivered(
        self, *, delivery_id: str, part_id: str, provider_message_id: int,
        account_id: int, peer_id: int, text: str, topic_id: int | None = None,
        source_event_id: str = "", task_id: str = "", format: str = "text",
        delivery_reporting_version: int, provider: str = "telegram",
    ) -> PresenceResult:
        """Record a *confirmed* provider send, never an intent/attempt/timeout.

        Call only after Telethon returns a concrete message id. Unknown send outcomes
        must stay unknown in the caller's durable queue and must not call this method.
        The negotiated turn must have echoed reporting version 1.
        """
        if type(delivery_reporting_version) is not int or delivery_reporting_version != 1:
            raise ValueError("Host did not negotiate provider delivery reporting")
        _number(provider_message_id, "provider_message_id")
        _number(account_id, "account_id")
        _number(peer_id, "peer_id", signed=True)
        if topic_id is not None:
            _number(topic_id, "topic_id")
        if not isinstance(text, str) or not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError("text and delivery_id are required")
        if not isinstance(part_id, str) or not (part_id == "status" or
                                               part_id.isascii() and part_id.isdecimal()):
            raise ValueError("part_id must be a numeric index or status")
        if not isinstance(source_event_id, str) or not isinstance(task_id, str):
            raise TypeError("origin ids must be strings")
        if not isinstance(format, str) or not isinstance(provider, str) or not provider:
            raise ValueError("invalid provider or format")
        receipt = {
            "schema_version": 1, "delivery_id": delivery_id, "part_id": part_id,
            "state": "delivered", "provider": provider, "account_id": str(account_id),
            "conversation_id": str(peer_id), "thread_id": str(topic_id) if topic_id is not None else "",
            "text": text, "format": format, "message": {"message_id": str(provider_message_id)},
            "origin": {"kind": "automatic", "task_id": task_id, "source_event_id": source_event_id},
        }
        return self._call("POST", "/presence/delivery", receipt)
