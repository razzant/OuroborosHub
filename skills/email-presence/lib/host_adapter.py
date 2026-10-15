from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .store import InboxItem

_OUTCOMES = frozenset({"message", "silent", "tool_delivered", "deferred"})
_DISPOSITIONS = frozenset({"retained", "rejected", "blocked", "retry"})
# Nonterminal observations: work still running, or no canonical answer yet.
_OBSERVING = frozenset({"pending", "unresolved"})

class HostContractError(RuntimeError): pass
class HostBindingTerminalError(HostContractError): pass
class HostRetryError(HostContractError): pass  # typed retry or blocked: the event stays queued
class HostAdapterUnavailable(RuntimeError): pass

@dataclass(frozen=True)
class HostOutput:
    """One reference's canonical late output, queued once under its identity."""
    identity: str
    text: str
    turn_ref: str

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

def email_presence_event(item: InboxItem, *, account_id: str) -> dict[str, Any]:
    return {
        "source_event_id": item.provider_event_key,
        "provider": "email",
        "account_id": str(account_id or "").strip(),
        "conversation_id": item.thread_key,
        "thread_id": item.thread_key,
        "conversation_key": f"email:{item.thread_key}",
        "actor": {"platform": "email", "platform_actor_id": item.sender,
                  "display_name": item.context.get("sender_name", "")},
        "conversation": {"platform": "email", "mailbox": str(account_id or "").strip(), "thread_id": item.thread_key},
        "message": {"message_id": item.message_id, "in_reply_to": item.in_reply_to,
                    "references": list(item.references), "imap_uid": item.uid,
                    "folder": item.folder, "uidvalidity": item.uidvalidity,
                    "subject": item.subject, "to": list(item.recipients),
                    **item.context},
        "text": f"Subject: {item.subject}\n\n{item.body}",
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

def _loopback(url: str) -> bool:
    p = urlsplit(url)
    if p.scheme != "http" or p.username or p.password or not p.hostname: return False
    if p.hostname == "localhost": return True
    try: return ipaddress.ip_address(p.hostname).is_loopback
    except ValueError: return False

class _SkillToken:
    def __init__(self, value: str): self._value = str(value or "")
    def use_in_request(self) -> str:
        if not self._value: raise HostContractError("Presence Host token is unavailable")
        return self._value
    def __repr__(self) -> str: return "<SkillToken:redacted>"


class LoopbackPresenceHostAdapter:
    def __init__(self, binding_id: str, *, host_service_url: str, skill_token: str, http_client: httpx.AsyncClient | None = None):
        self.binding_id = normalize_binding_id(binding_id) if str(binding_id or "").strip() else ""
        self.host_service_url = host_service_url.rstrip("/")
        if not _loopback(self.host_service_url): raise HostContractError("HOST_SERVICE_URL must be an HTTP loopback URL")
        self._token = _SkillToken(skill_token); self.available = bool(self.binding_id and skill_token)
        self._http = http_client or httpx.AsyncClient(timeout=35, trust_env=False); self._owns = http_client is None; self._terminal: dict[str, dict[str, Any]] = {}
        self.delivery_reporting_status = "unknown"
        self.delivery_reporting_version = 0
    def _headers(self): return {"X-Skill-Token": self._token.use_in_request(), "Content-Type": "application/json"}
    async def discover_delivery_support(self):
        if self.delivery_reporting_status in {"supported", "unsupported"}:
            return self.delivery_reporting_version
        try:
            response = await self._http.get(f"{self.host_service_url}/identity", headers=self._headers(), timeout=10)
            payload = await self._json(response)
            self.delivery_reporting_version = int(payload.get("presence_delivery_version") == 1)
            self.delivery_reporting_status = "supported" if self.delivery_reporting_version else "unsupported"
        except (HostContractError, httpx.HTTPError, TimeoutError, asyncio.TimeoutError):
            self.delivery_reporting_version = 0
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
        if not self.available: raise HostAdapterUnavailable("Presence binding or Host Service token is not configured")
        mode = await self.discover_delivery_support()
        request = {"binding_id": self.binding_id, "event": email_presence_event(item, account_id=os.environ.get("EMAIL_USER", ""))}
        if item.staged_files:
            request["staged_files"] = [str(file.get("path") or "") for file in item.staged_files if str(file.get("path") or "").strip()]
        if mode:
            request["delivery_reporting_version"] = 1
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
            raise HostRetryError(_summary(receipt))
        if str(payload.get("status") or "") != "completed": raise HostContractError("Presence Host did not complete the turn request")
        outcome=self._outcome(payload)
        data={"status":"deferred" if outcome=="deferred" else "completed","text":str(payload.get("text") or "") if outcome in {"message", "deferred"} else "","turn_ref":str(payload.get("turn_ref") or ""),"work_ref":str(payload.get("work_ref") or "")}
        data["delivery_reporting_version"] = int(payload.get("delivery_reporting_version") == 1)
        if outcome=="deferred" and not data["work_ref"]: raise HostContractError("Deferred presence turn omitted work_ref")
        return _ref(outcome, data) if outcome=="deferred" else _ref("completed", data)
    async def _poll(self, work_ref):
        response=await self._http.get(f"{self.host_service_url}/presence/work/{quote(work_ref,safe='')}",headers=self._headers(),params={"binding_id":self.binding_id},timeout=35)
        payload=await self._json(response); state=str(payload.get("status") or "").lower()
        if state in _OBSERVING: return payload
        if state not in {"completed","failed","cancelled"}: raise HostContractError(f"Unknown presence work status: {state or '<empty>'}")
        self._outcome(payload); self._terminal[work_ref]=payload; return payload
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
    async def status(self, reference: str) -> HostTurnStatus:
        if reference.startswith("completed:"):
            receipt = _decode(reference, "completed")
            return HostTurnStatus("ready", delivery_reporting_version=int(receipt.get("delivery_reporting_version") == 1), turn_ref=str(receipt.get("turn_ref") or ""))
        if reference.startswith("rejected:"):
            return HostTurnStatus("failed", _summary(_decode(reference, "rejected")))
        if reference.startswith("retained:"):
            return await self._retained_status(reference)
        receipt=_decode(reference,"deferred"); work_ref=str(receipt.get("work_ref") or ""); immediate=(str(receipt.get("text") or ""),) if receipt.get("text") else ()
        payload=self._terminal.get(work_ref) or await self._poll(work_ref); state=str(payload.get("status") or "").lower()
        return HostTurnStatus("ready" if state == "completed" else "failed" if state in {"failed", "cancelled"} else "pending",
                              str(payload.get("error") or state) if state in {"failed", "cancelled"} else "", immediate,
                              int(receipt.get("delivery_reporting_version") == 1), str(receipt.get("turn_ref") or ""))
    async def deliver(self, reference: str) -> HostDelivery:
        if reference.startswith("retained:"):  # each reference's output was already reported by status
            receipt = _decode(reference, "retained")
            return HostDelivery((), int(receipt.get("delivery_reporting_version") == 1), str((receipt.get("response") or {}).get("turn_ref") or ""))
        receipt = _decode(reference, "completed" if reference.startswith("completed:") else "deferred")
        if reference.startswith("completed:"): payload=receipt
        else:
            work_ref=str(_decode(reference,"deferred").get("work_ref") or ""); payload=self._terminal.get(work_ref) or await self._poll(work_ref)
            if str(payload.get("status") or "").lower()!="completed": raise HostContractError("Presence work is not completed")
        text=str(payload.get("text") or "") if payload.get("outcome", "message") == "message" else ""
        return HostDelivery((text,) if text.strip() else (), int(receipt.get("delivery_reporting_version") == 1), str(receipt.get("turn_ref") or ""))
    async def aclose(self):
        if self._owns: await self._http.aclose()

def create_host_adapter(binding_id: str, *, http_client=None):
    return LoopbackPresenceHostAdapter(binding_id,host_service_url=os.environ.get("HOST_SERVICE_URL","http://127.0.0.1:8767"),skill_token=os.environ.get("HOST_SERVICE_TOKEN",""),http_client=http_client)
