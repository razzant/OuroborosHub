"""Durable peer-event custody over the Host's Presence protocol.

No owner-chat allocation, log scraping or owner authority is used here. The
journal keeps the exact event and server references across companion restarts.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import time
import uuid
from urllib.parse import quote

import httpx


class HostWaitExpired(RuntimeError):
    def __init__(self, message, operation_ref=""):
        super().__init__(message)
        self.operation_ref = operation_ref


class HostWorkCancelled(RuntimeError):
    pass


class HostMessageConflict(ValueError):
    pass


class PresenceTransport:
    def __init__(self, state_dir, base, headers, *, timeout, poll_interval, http=httpx):
        self.root = pathlib.Path(state_dir) / "presence"
        self.base, self.headers = base, headers
        self.timeout, self.poll_interval, self.http = timeout, poll_interval, http
        self.lock = threading.RLock()

    def _path(self, source_id):
        return self.root / (hashlib.sha256(source_id.encode()).hexdigest() + ".json")

    def load(self, source_id):
        path = self._path(source_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RuntimeError("Presence receipt is unreadable; left unchanged") from exc
        if not isinstance(value, dict) or (value.get("event") or {}).get("source_event_id") != source_id:
            raise RuntimeError("Presence receipt identity is invalid; left unchanged")
        return value

    def _save(self, source_id, record):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(source_id)
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def _checkpoint(self, source_id, payload):
        with self.lock:
            record = self.load(source_id)
            if record is None:
                raise RuntimeError("Presence source is missing")
            record["response"] = payload
            # Keep the original turn ref even when a child later becomes visible.
            record["turn_ref"] = record.get("turn_ref") or payload.get("turn_ref", "")
            child = str(payload.get("child_work_ref") or payload.get("work_ref") or "")
            if child and child != record.get("turn_ref"):
                record["work_ref"] = child
            self._save(source_id, record)
            return record

    def submit(self, conversation_id, text, source_id):
        event = {
            "source_event_id": source_id, "provider": "a2a", "account_id": "server",
            "conversation_id": str(conversation_id), "thread_id": "",
            "conversation_key": "a2a:server:" + str(conversation_id),
            "actor": {"platform": "a2a", "platform_actor_id": "", "is_bot": True},
            "conversation": {"platform": "a2a"}, "message": {"message_id": source_id},
            "text": text, "facts": ["direct_message", "from_bot"],
        }
        with self.lock:
            record = self.load(source_id)
            if record and record["event"] != event:
                raise HostMessageConflict("messageId was already accepted with different content or context")
            if not record:
                record = {"event": event}
                self._save(source_id, record)  # before crossing the Host boundary
        if record.get("turn_ref") or record.get("response", {}).get("status") in {"completed", "failed", "cancelled"}:
            return self.result(self.read(source_id), source_id)
        try:
            response = self.http.post(self.base + "/presence/turn", headers=self.headers(),
                                      json={"event": event}, timeout=self.timeout + 15)
        except httpx.TimeoutException as exc:
            raise HostWaitExpired("Host outcome is unknown; retry this same messageId", source_id) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HostWaitExpired("Host response is unreadable; retry this same messageId", source_id) from exc
        if not isinstance(payload, dict):
            raise HostWaitExpired("Host response is not an object; retry this same messageId", source_id)
        self._checkpoint(source_id, payload)
        if payload.get("ok") is False or response.status_code >= 400:
            disposition = payload.get("disposition")
            if disposition == "retained" and payload.get("turn_ref"):
                return self.result(self.read(source_id), source_id)
            if disposition in {"retry", "blocked"}:
                raise HostWaitExpired(str(payload.get("error") or disposition), source_id)
            if disposition == "rejected":
                raise RuntimeError(str(payload.get("error") or "Host rejected peer event"))
            # An untyped HTTP error never proves that an attempt was not admitted.
            raise HostWaitExpired(str(payload.get("error") or "Host admission is unconfirmed"), source_id)
        return self.result(self.view(record=self.load(source_id)), source_id)

    def read(self, source_id):
        record = self.load(source_id)
        if record is None:
            return None  # Legacy task records remain readable, never resubmitted.
        payload = record.get("response") or {}
        if payload.get("status") == "completed" and payload.get("outcome") != "deferred":
            return self.view(record)
        ref = record.get("work_ref") or record.get("turn_ref")
        if not ref:
            return self.view(record)
        try:
            response = self.http.get(self.base + "/presence/work/" + quote(ref, safe=""),
                                     headers=self.headers(), timeout=10)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if response.status_code != 200 or not isinstance(payload, dict):
            return None
        # A lost/interrupted author is terminal. Unresolved custody stays pending.
        return self.view(self._checkpoint(source_id, payload))

    @staticmethod
    def view(record):
        payload = record.get("response") or {}
        status = payload.get("status")
        if payload.get("ok") is False or (status == "completed" and payload.get("outcome") not in {"message", "silent", "tool_delivered"}) or status not in {"completed", "failed", "cancelled", "interrupted"}:
            status = "pending"
        if status == "interrupted":
            status = "failed"
        text = str(payload.get("text") or "") if payload.get("outcome") == "message" else ""
        return {"status": status, "text": text, "reason": payload.get("error") or payload.get("code") or "",
                "operation_ref": record["event"]["source_event_id"]}

    @staticmethod
    def result(view, source_id):
        status = (view or {}).get("status")
        if status == "completed":
            return str(view.get("text") or "")
        if status == "cancelled":
            raise HostWorkCancelled("Host work was cancelled")
        if status == "lost":
            raise RuntimeError("the host restarted before answering; the message was not carried over")
        if status == "failed":
            raise RuntimeError(str(view.get("reason") or "Host work failed"))
        raise HostWaitExpired("Host work may still be running; poll this task", source_id)

    def wait(self, source_id, deadline, read=None):
        while time.monotonic() < deadline:
            try:
                return self.result((read or self.read)(source_id), source_id)
            except HostWaitExpired:
                time.sleep(max(1.0, float(self.poll_interval)))
        raise HostWaitExpired("A2A_STREAM_DEADLINE_SEC expired; Host work may still be running", source_id)
