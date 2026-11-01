from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .store import InboxItem
from .facts import email_facts, mailbox_identity

_OUTCOMES = frozenset({"message", "silent", "tool_delivered", "deferred"})
_SPEECH = frozenset({"message", "deferred"})
_DISPOSITIONS = frozenset({"retained", "rejected", "blocked", "retry"})
# Nonterminal observations: work still running, or no canonical answer yet.
_OBSERVING = frozenset({"pending", "unresolved"})
# A continuation author's terminal views; "interrupted" is an older Host's lost author.
_AUTHOR_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})

class HostContractError(RuntimeError): pass
class HostBindingTerminalError(HostContractError): pass
class HostAdapterUnavailable(RuntimeError): pass

class HostRetryError(HostContractError):
    """Typed retry or blocked: the event stays queued, keeping the Host's hint."""
    def __init__(self, message="", *, code="", field="", retry_after=None):
        super().__init__(message)
        self.code, self.field, self.retry_after = code, field, retry_after

@dataclass(frozen=True)
class HostOutput:
    """One reference's canonical late output, queued once under its identity.

    ``anchor`` is a version 2 author output's exact ``reply_anchor``
    (``{source_event_id, message}``) naming the covered input it answers; empty means
    reply to the polled event.
    """
    identity: str
    text: str
    turn_ref: str
    anchor: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class HostTurnStatus:
    state: str
    error: str = ""
    texts: tuple[str, ...] = ()
    delivery_reporting_version: int = 0
    turn_ref: str = ""
    outputs: tuple[HostOutput, ...] = ()
    # A retained receipt that now names a newly discovered child; persist before polling it.
    host_reference: str = ""

@dataclass(frozen=True)
class HostDelivery:
    texts: tuple[str, ...] = ()
    delivery_reporting_version: int = 0
    turn_ref: str = ""

def normalize_binding_id(value: Any) -> str:
    value = str(value or "").strip()
    if value and not re.fullmatch(r"[0-9a-f]{32}", value):
        raise HostContractError("binding_id must be 32 lowercase hexadecimal characters")
    return value

def email_presence_event(item: InboxItem, *, account_id: str, places: bool = False,
                         identity: bool = False, words=(), address="", senders=(), sender="") -> dict[str, Any]:
    return {
        "source_event_id": item.provider_event_key,
        "provider": "email",
        "account_id": str(account_id or "").strip(),
        "conversation_id": item.thread_key,
        "thread_id": item.thread_key,
        "conversation_key": f"email:{item.thread_key}",
        "actor": {"platform": "email", "platform_actor_id": item.sender,
                  "display_name": item.context.get("sender_name", ""),
                  **({"authenticated": False} if identity else {})},
        "conversation": {"platform": "email", "mailbox": str(account_id or "").strip(), "thread_id": item.thread_key},
        "message": {"message_id": item.message_id, "in_reply_to": item.in_reply_to,
                    "references": list(item.references), "imap_uid": item.uid,
                    "folder": item.folder, "uidvalidity": item.uidvalidity,
                    "subject": item.subject, "to": list(item.recipients),
                    **item.context},
        "text": f"Subject: {item.subject}\n\n{item.body}",
        **({"facts": email_facts(item, address or account_id, words, senders)} if places else {}),
        **({"identity": mailbox_identity(address or account_id, senders=senders, sender=sender)} if identity else {}),
    }

def _ref(kind: str, payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode()
    return f"{kind}:{base64.urlsafe_b64encode(raw).decode().rstrip('=')}"

def _decode(reference: str, kind: str) -> dict[str, Any]:
    if not reference.startswith(kind + ":"): raise HostContractError("Unknown presence reference type")
    encoded = reference.split(":", 1)[1] + "=" * (-len(reference.split(":", 1)[1]) % 4)
    try: value = json.loads(base64.urlsafe_b64decode(encoded).decode())
    except Exception as exc: raise HostContractError("Invalid persisted presence receipt") from exc
    if not isinstance(value, dict): raise HostContractError("Invalid persisted presence receipt")
    return value

def _summary(receipt: Mapping[str, Any]) -> str:
    response = receipt.get("response") if isinstance(receipt.get("response"), dict) else {}
    reason = str(response.get("error") or "")
    return (f"Presence Host {response.get('disposition')} (HTTP {receipt.get('http_status')}, "
            f"{response.get('code') or ''})" + (f": {reason}" if reason else ""))

def _child_ref(payload: Mapping[str, Any], polled: str) -> str:
    """Admitted child work named by an unresolved view; the original is never its own child."""
    original = {polled, str(payload.get("turn_ref") or "").strip()}
    for key in ("child_work_ref", "work_ref"):
        value = str(payload.get(key) or "").strip()
        if value and value not in original:
            return value
    return ""

def _retry_after(payload: Mapping[str, Any]):
    value = payload.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
        return None
    return float(value)

def _anchor(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The whole ``reply_anchor`` (``source_event_id`` and its ``message``), or ``{}``."""
    anchor = payload.get("reply_anchor")
    if not isinstance(anchor, Mapping) or not str(anchor.get("source_event_id") or "").strip():
        return {}
    message = anchor.get("message")
    return {"source_event_id": str(anchor["source_event_id"]).strip(),
            "message": dict(message) if isinstance(message, Mapping) else {}}

def _receipt_anchor(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The anchor a persisted receipt kept (older receipts kept none)."""
    anchor = receipt.get("anchor")
    return dict(anchor) if isinstance(anchor, Mapping) else {}

def _output_identity(output_ref: str, fallback: str) -> str:
    return f"output:{output_ref}" if output_ref else fallback

def _released_outputs(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """A continuation view's ordered ``outputs``, validated without dropping any."""
    rows = payload.get("outputs", [])
    if not isinstance(rows, list): raise HostContractError("Presence continuation outputs must be a list")
    outputs = []
    for row in rows:
        if (not isinstance(row, Mapping) or not isinstance(row.get("output_ref"), str) or not row["output_ref"].strip()
                or not isinstance(row.get("text"), str)):
            raise HostContractError("Released presence output omitted its identity")
        outcome = str(row.get("outcome") or "").strip().lower()
        if outcome not in _OUTCOMES: raise HostContractError(f"Unknown presence outcome: {outcome or '<empty>'}")
        outputs.append({"output_ref": row["output_ref"].strip(), "outcome": outcome, "text": row["text"], "anchor": _anchor(row)})
    return outputs

def _loopback(url: str) -> bool:
    p = urlsplit(url)
    if p.scheme != "http" or p.username or p.password or not p.hostname: return False
    if p.hostname == "localhost": return True
    try: return ipaddress.ip_address(p.hostname).is_loopback
    except ValueError: return False

class _SkillToken:
    def __init__(self, value):
        self._source = value if hasattr(value, "use_in_request") else None
        self._value = str(value or "") if self._source is None else ""
    @property
    def available(self): return bool(self._source is not None or self._value)
    def use_in_request(self) -> str:
        if self._source is not None:
            return self._source.use_in_request()
        if not self._value: raise HostContractError("Presence Host token is unavailable")
        return self._value
    def __repr__(self) -> str: return "<SkillToken:redacted>"


class LoopbackPresenceHostAdapter:
    def __init__(self, binding_id: str, *, host_service_url: str, skill_token: str, http_client: httpx.AsyncClient | None = None,
                 place_http_client: httpx.Client | None = None, words=(), account_id="", address="", senders=(), sender="", sync_only=False):
        self.binding_id = normalize_binding_id(binding_id) if str(binding_id or "").strip() else ""
        self.host_service_url = host_service_url.rstrip("/")
        if not _loopback(self.host_service_url): raise HostContractError("HOST_SERVICE_URL must be an HTTP loopback URL")
        self._token = _SkillToken(skill_token); self.available = bool(self.binding_id and self._token.available)
        self._http = http_client if sync_only else http_client or httpx.AsyncClient(timeout=35, trust_env=False)
        self._owns = http_client is None and not sync_only
        self._terminal: dict[str, dict[str, Any]] = {}
        self.delivery_reporting_status = "unknown"
        self.delivery_reporting_version = 0
        self.places_version = 0
        self.presence_identity = 0
        self.presence_wake_signals = 0
        self.presence_place_binding_version = 0
        # 2 only when /identity lists it in presence_continuation_versions; this
        # transport never negotiated version 1, so an older Host keeps the plain turn.
        self.continuation_version = 0
        self.words = words
        self.address, self.senders, self.sender = address, tuple(senders), sender
        self.account_id = account_id
        self._place_http = place_http_client

    def _identity(self, payload):
        version = payload.get("places_version")
        self.places_version = int(type(version) is int and version >= 1)
        self.presence_identity = int(payload.get("presence_identity") == 1)
        self.presence_wake_signals = int(payload.get("presence_wake_signals") == 1)
        self.available = bool(self._token.available and (self.binding_id or self.places_version))
        self.presence_place_binding_version = int(payload.get("presence_place_binding_version") == 1)
        self.delivery_reporting_version = int(payload.get("presence_delivery_version") == 1)
        self.delivery_reporting_status = "supported" if self.delivery_reporting_version else "unsupported"
        listed = payload.get("presence_continuation_versions")
        self.continuation_version = 2 if isinstance(listed, list) and any(type(v) is int and v == 2 for v in listed) else 0

    def place_hold(self, account_id, conversation_id):
        """The SMTP worker is synchronous; use its own HTTP client on that thread."""
        try:
            if self._place_http is None:
                self._place_http = httpx.Client(timeout=10, trust_env=False)
            if self.delivery_reporting_status in {"unknown", "unavailable"}:
                identity = self._place_http.get(f"{self.host_service_url}/identity", headers=self._headers())
                identity.raise_for_status()
                self._identity(identity.json())
            if not self.places_version:
                return ""
            response = self._place_http.get(f"{self.host_service_url}/presence/place", headers=self._headers(),
                                            params={"provider": "email", "account_id": account_id,
                                                    "conversation_id": conversation_id})
            if response.status_code == 404:
                payload = response.json()
                if payload.get("code") == "place_not_connected":
                    return "Held: no connected Place; this legacy send has no captured unbound authorization"
            response.raise_for_status()
            place = response.json()
            state = place.get("state")
            return "" if state == "active" else f"Held: {place.get('label') or 'Email'} is {state or 'unavailable'}"
        except (HostContractError, httpx.HTTPError, ValueError, AttributeError):
            return "Held: place policy unavailable"

    def _place_client(self):
        if self._place_http is None:
            self._place_http = httpx.Client(timeout=10, trust_env=False)
        return self._place_http

    def capture_binding(self, account_id, conversation_id, *, thread_id="", require_place=False):
        if self.delivery_reporting_status in {"unknown", "unavailable"}:
            identity = self._place_client().get(f"{self.host_service_url}/identity", headers=self._headers())
            identity.raise_for_status()
            self._identity(identity.json())
        if not self.presence_place_binding_version:
            return {"supported": False}
        response = self._place_client().get(f"{self.host_service_url}/presence/place", headers=self._headers(), params={
            "operation": "bind", "provider": "email", "account_id": account_id,
            "conversation_id": conversation_id, "thread_id": thread_id,
            "require_place": "true" if require_place else "false"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or type(payload.get("allowed")) is not bool or "binding" not in payload:
            raise HostContractError("Host omitted the mail operation's binding facts")
        return {**payload, "supported": True}

    def binding_hold(self, binding, account_id, conversation_id, *, thread_id=""):
        if not isinstance(binding, dict):
            return "Held: the original Place binding was not captured; inspect the receipt before a new send"
        try:
            response = self._place_client().get(f"{self.host_service_url}/presence/place", headers=self._headers(), params={
                "operation": "check", "provider": "email", "account_id": account_id,
                "conversation_id": conversation_id, "thread_id": thread_id,
                "binding": json.dumps(binding, separators=(",", ":"))})
            payload = response.json()
            if response.status_code == 403:
                return "Held: host authorization blocked"
            if response.status_code >= 400:
                return "Held: " + str(payload.get("code") or "place policy unavailable")
            if payload.get("allowed") is True:
                return ""
            return "Held: " + str(payload.get("code") or payload.get("state") or "place policy unavailable")
        except (HostContractError, httpx.HTTPError, ValueError, AttributeError):
            return "Held: place policy unavailable"

    def close_sync(self):
        if self._place_http is not None:
            self._place_http.close()
            self._place_http = None
    def _headers(self): return {"X-Skill-Token": self._token.use_in_request(), "Content-Type": "application/json"}
    async def discover_delivery_support(self):
        if self.delivery_reporting_status in {"supported", "unsupported"}:
            return self.delivery_reporting_version
        try:
            response = await self._http.get(f"{self.host_service_url}/identity", headers=self._headers(), timeout=10)
            payload = await self._json(response)
            self._identity(payload)
        except (HostContractError, httpx.HTTPError, TimeoutError, asyncio.TimeoutError):
            self.delivery_reporting_version = 0
            self.continuation_version = 0
            self.delivery_reporting_status = "unavailable"
        return self.delivery_reporting_version
    async def report_delivery(self, payload):
        response = await self._http.post(f"{self.host_service_url}/presence/delivery", headers=self._headers(), json=payload, timeout=10)
        result = await self._json(response)
        if result.get("ok") is not True or result.get("recorded") is not True:
            raise HostContractError("Presence delivery report was not acknowledged")
    async def _json(self, response, *, turn=False):
        if turn and 400 <= response.status_code < 600:
            # The typed disposition decides recovery; no HTTP status, 403/404 included, ends an event.
            try: payload = response.json()
            except ValueError: payload = None
            if isinstance(payload, dict) and payload.get("ok") is False and str(payload.get("disposition") or "") in _DISPOSITIONS:
                return payload
            raise HostContractError(f"Presence Host returned HTTP {response.status_code} without a typed disposition")
        if response.status_code in (403,404): raise HostBindingTerminalError(f"Presence binding rejected (HTTP {response.status_code})")
        if response.status_code < 200 or response.status_code >= 300: raise HostContractError(f"Presence Host returned HTTP {response.status_code}")
        try: payload=response.json()
        except ValueError as exc: raise HostContractError("Presence Host returned invalid JSON") from exc
        if not isinstance(payload,dict): raise HostContractError("Presence Host response must be an object")
        return payload
    @staticmethod
    def _outcome(payload):
        outcome=str(payload.get("outcome") or "").lower()
        if outcome not in _OUTCOMES: raise HostContractError(f"Unknown presence outcome: {outcome or '<empty>'}")
        return outcome
    async def submit(self, item: InboxItem) -> str:
        mode = await self.discover_delivery_support()
        if not self.available: raise HostAdapterUnavailable("Place or legacy binding is not configured")
        request = {**({"binding_id": self.binding_id} if not self.places_version else {}),
                   "event": email_presence_event(item, account_id=self.account_id or os.environ.get("EMAIL_USER", ""),
                                                  places=bool(self.places_version), identity=bool(self.presence_identity),
                                                  words=self.words if self.presence_wake_signals else (),
                                                  address=self.address, senders=self.senders, sender=self.sender)}
        if item.staged_files:
            request["staged_files"] = [str(file.get("path") or "") for file in item.staged_files if str(file.get("path") or "").strip()]
        if mode:
            request["delivery_reporting_version"] = 1
        version = self.continuation_version
        if version == 2:
            request["continuation_version"] = 2
        response=await self._http.post(f"{self.host_service_url}/presence/turn",headers=self._headers(),json=request,timeout=1800)
        payload=await self._json(response, turn=True)
        disposition = str(payload.get("disposition") or "")
        if payload.get("ok") is False and disposition in _DISPOSITIONS:
            receipt = {"http_status": response.status_code, "response": dict(payload)}
            if disposition == "retained":
                # An admitted attempt the Host will not regenerate: keep its receipt and observe it.
                if not str(payload.get("turn_ref") or "").strip(): raise HostContractError("Retained presence refusal omitted its original turn_ref")
                receipt.update(delivery_reporting_version=int(payload.get("delivery_reporting_version") == 1) if "delivery_reporting_version" in payload else mode, work_refs=[])
                return _ref("retained", receipt)
            if disposition == "rejected": return _ref("rejected", receipt)
            # A busy conversation or full capacity (v2 429) wrote no task row: the same
            # event stays first in its thread and is resubmitted after the Host's hint.
            raise HostRetryError(_summary(receipt), code=str(payload.get("code") or ""),
                                 field=str(payload.get("field") or ""), retry_after=_retry_after(payload))
        status = str(payload.get("status") or "")
        continuation_ref = str(payload.get("continuation_ref") or "").strip()
        if version == 2 and (status == "continuing" or (status == "completed" and continuation_ref)):
            # Custody, not a reply: the author's intake is open. Its outputs, terminal
            # and any promoted child are read from /presence/work/{continuation_ref}.
            if payload.get("continuation_version") != 2 or not continuation_ref:
                raise HostContractError("Continuing presence turn omitted its continuation reference")
            outcome = self._outcome(payload)
            return _ref("continuing", {"continuation_ref": continuation_ref, "turn_ref": str(payload.get("turn_ref") or ""),
                                       "outcome": outcome, "text": str(payload.get("text") or ""),
                                       "output_ref": str(payload.get("output_ref") or ""),
                                       "anchor": _anchor(payload),
                                       "work_ref": str(payload.get("work_ref") or "").strip(),
                                       "delivery_reporting_version": int(payload.get("delivery_reporting_version") == 1),
                                       "continuation_version": 2, "work_refs": []})
        if status != "completed": raise HostContractError("Presence Host did not complete the turn request")
        outcome=self._outcome(payload)
        data={"status":"deferred" if outcome=="deferred" else "completed","text":str(payload.get("text") or "") if outcome in {"message", "deferred"} else "","turn_ref":str(payload.get("turn_ref") or ""),"work_ref":str(payload.get("work_ref") or "")}
        data["delivery_reporting_version"] = int(payload.get("delivery_reporting_version") == 1)
        if outcome=="deferred" and not data["work_ref"]: raise HostContractError("Deferred presence turn omitted work_ref")
        if outcome != "deferred" and version == 2 and payload.get("continuation_version") == 2:
            # Heard/context (silent) or an author that ended before opening intake:
            # its reply keeps the Host output identity and anchor.
            data.update(continuation_version=2, output_ref=str(payload.get("output_ref") or ""), anchor=_anchor(payload))
        return _ref(outcome, data) if outcome=="deferred" else _ref("completed", data)
    async def _get_work(self, work_ref):
        await self.discover_delivery_support()
        response=await self._http.get(f"{self.host_service_url}/presence/work/{quote(work_ref,safe='')}",headers=self._headers(),params={} if self.places_version else {"binding_id":self.binding_id},timeout=35)
        return await self._json(response)
    async def _poll(self, work_ref):
        payload=await self._get_work(work_ref); state=str(payload.get("status") or "").lower()
        if state in _OBSERVING: return payload
        if state not in {"completed","failed","cancelled"}: raise HostContractError(f"Unknown presence work status: {state or '<empty>'}")
        self._outcome(payload); self._terminal[work_ref]=payload; return payload
    async def _poll_continuation(self, continuation_ref):
        """One version 2 author view; another shape is refused, never guessed."""
        payload = await self._get_work(continuation_ref)
        if payload.get("continuation_version") != 2:
            raise HostContractError("Presence Host answered a continuation poll without continuation_version 2")
        returned = str(payload.get("continuation_ref") or payload.get("work_ref") or "").strip()
        if returned and returned != continuation_ref: raise HostContractError("Presence Host returned a different continuation_ref")
        payload["outputs"] = _released_outputs(payload)
        state = str(payload.get("status") or "").lower()
        if state in _OBSERVING: return payload
        if state not in _AUTHOR_TERMINAL: raise HostContractError(f"Unknown presence work status: {state or '<empty>'}")
        if state != "interrupted": self._outcome(payload)
        self._terminal[continuation_ref] = payload; return payload
    async def _continuing_status(self, reference: str) -> HostTurnStatus:
        """One observation pass over a version 2 author and its promoted children.

        Each released or terminal output is reported under its ``output_ref`` identity
        with the author's ``reply_anchor``; children carry none. A child named later is
        returned for persistence and polled on the next pass. Unreadable polls keep
        custody. Ready once every reference is terminal and completed; failed if one
        ended otherwise (a Stop's cancellation names no new speech).
        """
        receipt = _decode(reference, "continuing"); ref = str(receipt.get("continuation_ref") or "").strip()
        if not ref: raise HostContractError("Invalid persisted continuing receipt")
        refs = [ref, *[value for value in dict.fromkeys([str(receipt.get("work_ref") or "").strip(),
                                                          *[str(v).strip() for v in receipt.get("work_refs", [])]]) if value and value != ref]]
        known, settled, notes, ended, outputs = list(refs), {}, [], [], {}
        def add(identity, text, turn_ref, anchor=None):
            if text.strip() and identity not in outputs: outputs[identity] = HostOutput(identity, text, turn_ref, anchor or {})
        if receipt.get("outcome") in _SPEECH:
            add(_output_identity(str(receipt.get("output_ref") or ""), f"initial:{ref}"), str(receipt.get("text") or ""), ref,
                _receipt_anchor(receipt))
        for source in known:
            author = source == ref
            try: payload = self._terminal.get(source) or await (self._poll_continuation(source) if author else self._poll(source))
            except (HostContractError, httpx.HTTPError) as exc:
                notes.append(f"{'continuation' if author else 'work'} {source}: {exc if isinstance(exc, HostContractError) else type(exc).__name__}"); continue
            state = str(payload.get("status") or "").lower()
            if author:
                child = _child_ref(payload, ref)
                if child and child not in refs: refs.append(child)  # persisted before it is polled
                for row in payload["outputs"]:
                    if row["outcome"] in _SPEECH: add(f"output:{row['output_ref']}", row["text"], ref, row["anchor"])
            if state in _OBSERVING:
                if state == "unresolved": notes.append(f"{'continuation' if author else 'work'} {source} unresolved ({payload.get('code') or 'retained'})")
                continue
            settled[source] = state
            if state != "interrupted" and payload.get("outcome") in _SPEECH:
                output_ref = str(payload.get("output_ref") or "")
                if author: add(_output_identity(output_ref, f"terminal:{ref}"), str(payload.get("text") or ""), ref, _anchor(payload))
                else: add(_output_identity(output_ref, f"work:{source}"), str(payload.get("text") or ""), str(payload.get("turn_ref") or source))
            if state == "interrupted": ended.append(f"continuation {source}: interrupted; it is not restarted automatically")
            elif state != "completed": ended.append(f"{'continuation' if author else 'work'} {source}: {payload.get('error') or state}")
        state = "pending"
        if len(refs) == len(known) and len(settled) == len(refs): state = "failed" if ended else "ready"
        updated = _ref("continuing", {**receipt, "work_refs": refs[1:]}) if len(refs) != len(known) else ""
        return HostTurnStatus(state, "; ".join(notes + ended), (), int(receipt.get("delivery_reporting_version") == 1),
                              ref, tuple(outputs.values()), updated)
    async def _retained_status(self, reference: str) -> HostTurnStatus:
        """Observe a retained event's original attempt and real children independently.

        The refusal is the event's receipt, never speech. A canonical message from
        any reference is one output under its own identity. A newly named child is
        returned for persistence first and polled on the next pass; unresolved or
        unreadable polls keep custody. Ready only once the original completed.
        """
        receipt = _decode(reference, "retained"); response = receipt.get("response") if isinstance(receipt.get("response"), dict) else {}
        turn_ref = str(response.get("turn_ref") or "").strip()
        if not turn_ref: raise HostContractError("Invalid persisted retained receipt")
        refs = [ref for ref in dict.fromkeys([turn_ref, str(response.get("work_ref") or "").strip(), *[str(value).strip() for value in receipt.get("work_refs", [])]]) if ref]
        known, settled, notes, outputs = list(refs), {}, [], []
        for ref in known:
            try: payload = self._terminal.get(ref) or await self._poll(ref)
            except (HostContractError, httpx.HTTPError) as exc:
                notes.append(f"work {ref}: {exc if isinstance(exc, HostContractError) else type(exc).__name__}"); continue
            state = str(payload.get("status") or "").lower(); child = _child_ref(payload, ref)
            if child and child not in refs: refs.append(child)
            for selected in payload.get("outputs") or []:
                if not isinstance(selected, dict) or not selected.get("output_ref"):
                    raise HostContractError("Released presence output omitted its identity")
                if self._outcome(selected) in {"message", "deferred"} and str(selected.get("text") or "").strip():
                    outputs.append(HostOutput(f"output:{selected['output_ref']}", str(selected["text"]), ref))
            if state in _OBSERVING:
                if state == "unresolved": notes.append(f"work {ref} unresolved ({payload.get('code') or 'retained'})")
                continue
            settled[ref] = state; text = str(payload.get("text") or "")
            if payload.get("outcome") in {"message", "deferred"} and text.strip():
                identity = f"output:{payload['output_ref']}" if payload.get("output_ref") else f"work:{ref}"
                outputs.append(HostOutput(identity, text, ref))
        state = "ready" if settled.get(turn_ref) == "completed" else "failed"
        if len(refs) != len(known) or len(settled) != len(refs): state = "pending"
        original = [f"original {turn_ref}: {settled[turn_ref]}"] if turn_ref in settled else []
        updated = _ref("retained", {**receipt, "work_refs": refs[1:]}) if len(refs) != len(known) else ""
        return HostTurnStatus(state, "" if state == "ready" else "; ".join([_summary(receipt), *notes, *original]), (),
                              int(receipt.get("delivery_reporting_version") == 1), turn_ref, tuple(outputs), updated)
    def initial_status(self, reference: str) -> HostTurnStatus:
        """What a just-stored reference already says, without any Host request.

        ``completed:`` is final and ``rejected:`` terminal. Every other reference names
        work still to be observed: it stays ``pending`` with only the speech its
        envelope carries (a deferred acknowledgement or a continuing initial output).
        """
        if reference.startswith(("completed:", "rejected:")):
            return self._local_status(reference)
        if reference.startswith("deferred:"):
            receipt = _decode(reference, "deferred"); text = str(receipt.get("text") or "")
            return HostTurnStatus("pending", "", (text,) if text else (), int(receipt.get("delivery_reporting_version") == 1),
                                  str(receipt.get("turn_ref") or ""))
        if reference.startswith("continuing:"):
            receipt = _decode(reference, "continuing"); ref = str(receipt.get("continuation_ref") or "")
            text, outputs = str(receipt.get("text") or ""), ()
            if receipt.get("outcome") in _SPEECH and text.strip():
                outputs = (HostOutput(_output_identity(str(receipt.get("output_ref") or ""), f"initial:{ref}"), text, ref,
                                      _receipt_anchor(receipt)),)
            return HostTurnStatus("pending", "", (), int(receipt.get("delivery_reporting_version") == 1), ref, outputs)
        if reference.startswith("retained:"):
            receipt = _decode(reference, "retained")
            return HostTurnStatus("pending", _summary(receipt), (), int(receipt.get("delivery_reporting_version") == 1),
                                  str((receipt.get("response") or {}).get("turn_ref") or ""))
        raise HostContractError("Unknown presence reference type")
    def _local_status(self, reference: str) -> HostTurnStatus:
        if reference.startswith("rejected:"):
            return HostTurnStatus("failed", _summary(_decode(reference, "rejected")))
        receipt = _decode(reference, "completed"); turn_ref = str(receipt.get("turn_ref") or "")
        outputs = ()
        if receipt.get("continuation_version") == 2 and str(receipt.get("text") or "").strip():
            # Its one selection, under the Host output identity and anchor.
            outputs = (HostOutput(_output_identity(str(receipt.get("output_ref") or ""), "final"), str(receipt["text"]),
                                  turn_ref, _receipt_anchor(receipt)),)
        return HostTurnStatus("ready", delivery_reporting_version=int(receipt.get("delivery_reporting_version") == 1),
                              turn_ref=turn_ref, outputs=outputs)
    async def status(self, reference: str) -> HostTurnStatus:
        if reference.startswith(("completed:", "rejected:")):
            return self._local_status(reference)
        if reference.startswith("retained:"):
            return await self._retained_status(reference)
        if reference.startswith("continuing:"):
            return await self._continuing_status(reference)
        receipt=_decode(reference,"deferred"); work_ref=str(receipt.get("work_ref") or ""); immediate=(str(receipt.get("text") or ""),) if receipt.get("text") else ()
        payload=self._terminal.get(work_ref) or await self._poll(work_ref); state=str(payload.get("status") or "").lower()
        return HostTurnStatus("ready" if state == "completed" else "failed" if state in {"failed", "cancelled"} else "pending",
                              str(payload.get("error") or state) if state in {"failed", "cancelled"} else "", immediate,
                              int(receipt.get("delivery_reporting_version") == 1), str(receipt.get("turn_ref") or ""))
    async def deliver(self, reference: str) -> HostDelivery:
        if reference.startswith("retained:"):  # each reference's output was already reported by status
            receipt = _decode(reference, "retained")
            return HostDelivery((), int(receipt.get("delivery_reporting_version") == 1), str((receipt.get("response") or {}).get("turn_ref") or ""))
        if reference.startswith("continuing:"):  # every output was already reported by status
            receipt = _decode(reference, "continuing")
            return HostDelivery((), int(receipt.get("delivery_reporting_version") == 1), str(receipt.get("continuation_ref") or ""))
        receipt = _decode(reference, "completed" if reference.startswith("completed:") else "deferred")
        if receipt.get("continuation_version") == 2:  # its selection was reported by status
            return HostDelivery((), int(receipt.get("delivery_reporting_version") == 1), str(receipt.get("turn_ref") or ""))
        if reference.startswith("completed:"): payload=receipt
        else:
            work_ref=str(_decode(reference,"deferred").get("work_ref") or ""); payload=self._terminal.get(work_ref) or await self._poll(work_ref)
            if str(payload.get("status") or "").lower()!="completed": raise HostContractError("Presence work is not completed")
        text=str(payload.get("text") or "") if payload.get("outcome", "message") == "message" else ""
        return HostDelivery((text,) if text.strip() else (), int(receipt.get("delivery_reporting_version") == 1), str(receipt.get("turn_ref") or ""))
    async def aclose(self):
        if self._owns: await self._http.aclose()
        if self._place_http is not None: self._place_http.close()

def create_host_adapter(binding_id: str, *, http_client=None, words=(), account_id="", address="", senders=(), sender=""):
    return LoopbackPresenceHostAdapter(binding_id, host_service_url=os.environ.get("HOST_SERVICE_URL", "http://127.0.0.1:8767"),
                                      skill_token=os.environ.get("HOST_SERVICE_TOKEN", ""), http_client=http_client, words=words,
                                      account_id=account_id, address=address, senders=senders, sender=sender)
