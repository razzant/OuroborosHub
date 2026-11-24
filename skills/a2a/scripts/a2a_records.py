"""Durable receiver records: task records per caller, message identities and arrival reservations.

Standard library only (plus the Presence event shape); the daemon owns one ``Records`` over its state
folder. An unnamed caller's task records stay in the flat legacy ``tasks/``; a named link's records live
in a folder of their own, outside any earlier release's scan, so a downgrade cannot serve them to
another caller. One inbound A2A message maps to ONE host event: its identity is the caller's principal
and its messageId (no task id, since a repeated first message has none), and the Presence
``source_event_id`` is that digest, so a repeat joins the accepted host work. A task record bound by an
earlier release names its message by the digest of the task id and message id; it stays readable as is.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import threading
import time
import uuid
from typing import Any, Dict, List

from a2a_presence import HostMessageConflict, presence_event

logger = logging.getLogger("a2a_daemon")
TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
RESERVED_PREFIXES = ("link:", "ctx:")


class TaskStateCorrupt(RuntimeError):
    """A task record exists but is unreadable or is not a JSON object."""


def arrival_digest(principal: str, message_id: str) -> str:
    identity = json.dumps(["arrival", principal, message_id], ensure_ascii=False, separators=(",", ":"))
    return "a2a:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def presence_address(principal: str, context_id: str) -> "tuple[str, str]":
    """(conversation_id, thread_id) of a new arrival.

    A named link's conversation is the link (its own room in the one A2A place); each remote context is a
    thread of it. An unnamed caller's context id stays its conversation (an existing conversation keeps its
    room); one that starts with a reserved prefix is escaped, so it can never equal a link's conversation.
    """
    if principal:
        return principal, context_id
    context_id = str(context_id)
    return (("ctx:" + context_id) if context_id.startswith(RESERVED_PREFIXES) else context_id), ""


def check_same_arrival(arrival: Dict[str, Any], content: Dict[str, Any], requested: Dict[str, str]) -> None:
    """A repeat must carry the same content; a task or context id it names must be the assigned one."""
    if arrival.get("content") != content:
        raise HostMessageConflict("messageId was already accepted with different content or context")
    if requested.get("task_id") and requested["task_id"] != arrival.get("task_id"):
        raise HostMessageConflict("messageId was already accepted on another task")
    if requested.get("context_id") and requested["context_id"] != arrival.get("context_id"):
        raise HostMessageConflict("messageId was already accepted in another context")


def _answer_entry(state: str, artifacts, delivery) -> Dict[str, Any]:
    answer = "\n".join(str(part.get("text") or "") for item in artifacts or [] for part in item.get("parts", []))
    return {"state": state, "delivery": delivery, "answer": answer}


class Records:
    def __init__(self, root, *, window: float, legacy_identity) -> None:
        self.root = pathlib.Path(root)
        self.window = float(window)  # an arrival's delivery window (A2A_STREAM_DEADLINE_SEC)
        self.lock = threading.RLock()
        self.legacy_identity = legacy_identity

    def is_current(self, bound: Dict[str, Any], task_id: str, message_id: str) -> bool:
        """Whether the task's current message is this one (a binding from 1.5 by its digest)."""
        current = str(bound.get("client_message_id") or "")
        if not current:
            return True
        if "message_id" in bound:
            return bound["message_id"] == message_id
        return current == self.legacy_identity(task_id, message_id)

    # --- task records ------------------------------------------------------

    def tasks_dir(self, principal: str = "") -> pathlib.Path:
        if principal:
            path = self.root / "link-tasks" / hashlib.sha256(principal.encode("utf-8")).hexdigest()[:24]
        else:
            path = self.root / "tasks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def task_path(self, task_id: str, principal: str = "") -> pathlib.Path:
        """Map a task id to a state file INJECTIVELY.

        Stripping every character outside [A-Za-z0-9_-] is a lossy mapping: distinct
        ids such as "job/a" and "joba" collapsed onto one filename, so one peer's task
        could overwrite or return another's record. The sha256 of the COMPLETE id is
        the identity; the sanitized prefix is retained only to keep the directory
        human-readable. An empty id maps deterministically as well.
        """
        raw = str(task_id or "")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        prefix = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))[:48]
        return self.tasks_dir(principal) / (f"{prefix}-{digest}.json" if prefix else f"{digest}.json")

    def save_task(self, task: Dict[str, Any], principal: str = "") -> None:
        """Atomically replace the existing task record; callers serialize merges."""
        path = self.task_path(str(task["id"]), principal)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def load_task(self, task_id: str, principal: str = "") -> Dict[str, Any] | None:
        path = self.task_path(task_id, principal)
        if not path.exists():
            return None
        return self.read_task_record(path, task_id, principal)

    def read_task_record(self, path: pathlib.Path, expected_task_id: str | None = None,
                         principal: str = "") -> Dict[str, Any]:
        """Read one task file with the same typed failure for lookup and listing (its message names no file)."""
        label = expected_task_id if expected_task_id is not None else path.name
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            # A present-but-unreadable record is NOT "task not found": an actionable task-state failure.
            logger.warning("a2a task state %s is unreadable: %s", path.name, exc)
            raise TaskStateCorrupt(f"task state for {label!r} is unreadable") from exc
        if not isinstance(record, dict):
            raise TaskStateCorrupt(f"task state for {label!r} is not a JSON object")
        task_id = record.get("id")
        if (not isinstance(task_id, str) or (expected_task_id is not None and task_id != expected_task_id)
                or self.task_path(task_id, principal) != path):
            raise TaskStateCorrupt(f"task state for {label!r} has a different identity")
        return record

    def update_task(
        self,
        task_id: str,
        context_id: str = "",
        *,
        principal: str = "",
        binding: "Dict[str, Any] | None" = None,
        state: str = "",
        message: str = "",
        artifacts: "List[Dict[str, Any]] | None" = None,
        delivery: "Dict[str, Any] | None" = None,
        expected_message_id: str = "",
        wire_id: "str | None" = None,
        owner: "str | None" = None,
        make_current: bool = True,
    ) -> Dict[str, Any]:
        """Merge through one current-message fence for both disk and returned views.

        An outcome for a message that is no longer the task's current one never touches the task's state or
        artifacts; a terminal one is still filed under its own message id (``answers``), where its asker's
        read-only poll finds it.
        """
        with self.lock:
            record = self.load_task(task_id, principal) or {}
            bound = dict(record.get("ouroboros") or {})
            current = str(bound.get("client_message_id") or "")
            terminal = state in TERMINAL_STATES and bool(wire_id)
            pending = dict(self.open_messages(record))
            if expected_message_id and wire_id and wire_id in (record.get("answers") or {}):
                return record  # an earlier observation already settled this exact letter
            if expected_message_id and expected_message_id == current \
                    and (record.get("status") or {}).get("state") in TERMINAL_STATES:
                return record
            if expected_message_id and current and expected_message_id != current:
                if record and terminal:
                    record.setdefault("answers", {})[wire_id] = _answer_entry(state, artifacts, delivery)
                    pending.pop(expected_message_id, None)
                    record["pending_wires"] = pending
                    self.save_task(record, principal)
                return record  # Neither persistence nor its returned projection may mix messages.
            record.setdefault("id", task_id)
            record.setdefault("contextId", context_id or task_id)
            record.setdefault("status", {"state": "working"})
            if owner is not None:
                record.setdefault("sdk_owner", owner)
            if binding:
                incoming = str(binding.get("client_message_id") or "")
                seen = list(bound.get("message_ids") or ([current] if current else []))
                if incoming not in seen:
                    seen.append(incoming)
                    bound["message_ids"] = seen
                    if make_current:
                        if binding.get("protocol") == "presence" and bound.get("chat_id"):
                            bound["legacy_message_ids"] = list(seen[:-1])
                            bound.pop("chat_id", None)
                        bound.update(binding)
                        record["status"] = {"state": "working"}
                        record.pop("artifacts", None)
                        record.pop("delivery", None)
                    if binding.get("message_id"):  # each message's own digest -> its id, for its own late answer
                        bound["wire_ids"] = {**(bound.get("wire_ids") or {}), incoming: binding["message_id"]}
                        pending[incoming] = binding["message_id"]
                record["ouroboros"] = bound
            if state:
                record["status"] = {"state": state}
                if message:
                    record["status"]["message"] = {"parts": [{"kind": "text", "text": message}]}
            if artifacts is not None:
                record["artifacts"] = artifacts
            if delivery is not None:
                record["delivery"] = delivery
            if terminal:
                record.setdefault("answers", {})[wire_id] = _answer_entry(state, artifacts, delivery)
                pending.pop(expected_message_id or current, None)
            record["pending_wires"] = pending
            self.save_task(record, principal)
            return record

    def bound(self, task_id: str, principal: str = "") -> Dict[str, Any]:
        record = self.load_task(task_id, principal) or {}
        bound = record.get("ouroboros")
        return dict(bound) if isinstance(bound, dict) else {}

    @staticmethod
    def open_messages(record: Dict[str, Any]) -> "List[tuple[str, str]]":
        """(digest, message id) of each message of this task whose own outcome is not filed yet."""
        if isinstance(record.get("pending_wires"), dict):
            return list(record["pending_wires"].items())
        bound = record.get("ouroboros") if isinstance(record.get("ouroboros"), dict) else {}
        answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
        return [(digest, wire) for digest, wire in (bound.get("wire_ids") or {}).items() if wire not in answers]

    # --- arrival reservations ------------------------------------------------
    #
    # (principal, messageId) is reserved with its exact input, one task/context pair and the immutable
    # Presence event before the task publishes the binding or the SDK acknowledges it.

    def arrival_path(self, digest: str) -> pathlib.Path:
        return self.root / "arrivals" / (digest.split(":", 1)[-1] + ".json")

    def load_arrival(self, principal: str, message_id: str) -> "Dict[str, Any] | None":
        if not message_id:
            return None
        digest = arrival_digest(principal, message_id)
        with self.lock:  # wait for an ordinary reservation to finish before joining it
            try:
                arrival = json.loads(self.arrival_path(digest).read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError) as exc:
                raise TaskStateCorrupt("an arrival record is unreadable") from exc
        if not isinstance(arrival, dict) or arrival.get("digest") != digest:
            raise TaskStateCorrupt("an arrival record has a different identity")
        return arrival

    def arrivals(self) -> List[Dict[str, Any]]:
        found = []
        for path in sorted((self.root / "arrivals").glob("*.json")):
            try:
                arrival = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(arrival, dict) and arrival.get("digest"):
                found.append(arrival)
        return found

    @staticmethod
    def settled_arrival(record: dict, digest: str, wire_id: str) -> bool:
        bound = record.get("ouroboros") or {}
        return bool(wire_id and wire_id in (record.get("answers") or {})) or (
            bound.get("client_message_id") == digest and (record.get("status") or {}).get("state") in TERMINAL_STATES)

    def _pending_mark(self, digest: str) -> pathlib.Path:
        return self.root / "pending-arrivals" / (digest.split(":", 1)[-1] + ".mark")

    def _watch_arrival(self, digest: str) -> None:
        mark = self._pending_mark(digest)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch(exist_ok=True)

    def retire_arrival(self, record: dict, digest: str, wire_id: str) -> None:
        if self.settled_arrival(record, digest, wire_id):
            self._pending_mark(digest).unlink(missing_ok=True)

    def pending_arrivals(self) -> List[Dict[str, Any]]:
        """Read unsettled reservations on startup. Seed the index once for pre-index state."""
        with self.lock:
            index = self.root / "pending-arrivals"
            initialized = index / "initialized"
            if not initialized.exists():
                index.mkdir(parents=True, exist_ok=True)
                for arrival in self.arrivals():
                    try:
                        task = self.load_task(arrival["task_id"], arrival["principal"]) or {}
                    except TaskStateCorrupt:
                        task = {}  # keep custody; an unreadable record is not a terminal one
                    if not self.settled_arrival(task, arrival["digest"], arrival["message_id"]):
                        self._watch_arrival(arrival["digest"])
                initialized.touch()
            pending = []
            for mark in index.glob("*.mark"):
                digest = "a2a:" + mark.stem
                try:
                    arrival = json.loads(self.arrival_path(digest).read_text(encoding="utf-8"))
                except FileNotFoundError:
                    mark.unlink(missing_ok=True)  # crash before publishing a reservation, under the same lock
                    continue
                except (OSError, ValueError):
                    continue
                if isinstance(arrival, dict) and arrival.get("digest") == digest:
                    pending.append(arrival)
            return pending

    def attach_arrival(self, arrival: dict) -> dict:
        """Finish publishing an already stored reservation, including after a process stops between writes."""
        task_id, principal, digest = arrival["task_id"], arrival["principal"], arrival["digest"]
        with self.lock:
            record = self.load_task(task_id, principal) or {}
            bound = record.get("ouroboros") or {}
            current = bound.get("client_message_id") or ""
            if digest in (bound.get("message_ids") or [current]):
                return record
            # The old format attached first; an absent binding is not enough to recreate it. A continuation
            # likewise cannot recreate its missing task. A newer letter, if any, keeps its own current state.
            if "previous" not in arrival or (arrival["previous"] is not None and not record):
                raise TaskStateCorrupt("the arrival's task binding is unavailable")
            event = arrival["event"]
            return self.update_task(task_id, arrival["context_id"], principal=principal, owner=principal, binding={
                "conversation_id": event["conversation_id"], "thread_id": event["thread_id"],
                "client_message_id": digest, "operation_ref": digest, "protocol": "presence",
                "message_id": arrival["message_id"],
            }, state="submitted" if not record else "", make_current=not record or arrival["previous"] == current)

    def reserve(self, principal: str, message_id: str, content: Dict[str, Any], text: str, *, actor: Dict[str, Any],
                task_id: str = "", context_id: str = "", requested: "Dict[str, str] | None" = None,
                on_reserved=None):
        """Reserve one arrival; return it and whether this call made the reservation.

        ``task_id``/``context_id`` name an existing task the message continues; otherwise one pair is minted
        here. A missing messageId gets a one-shot identity: such a message cannot be recognised when repeated.
        ``on_reserved(digest)`` marks it in flight before a second request can join it.
        """
        requested = requested or {}
        with self.lock:
            if message_id:
                existing = self.load_arrival(principal, message_id)
                if existing is not None:
                    check_same_arrival(existing, content, requested)
                    return existing, False
            digest = arrival_digest(principal, message_id or "one-shot:" + uuid.uuid4().hex)
            fresh = not task_id
            if not fresh and requested.get("context_id") and requested["context_id"] != context_id:
                # Decided before anything is reserved, changed or offered: words never go to another room.
                raise HostMessageConflict("contextId is not the context of the task this message names")
            if fresh:
                task_id, context_id = uuid.uuid4().hex, requested.get("context_id") or uuid.uuid4().hex
            bound = self.bound(task_id, principal)
            legacy = self.legacy_identity(task_id, message_id)
            if legacy in bound.get("legacy_message_ids", []) or (
                    bound.get("chat_id") and legacy in bound.get("message_ids", [bound.get("client_message_id")])):
                raise HostMessageConflict(
                    "Legacy owner-chat message cannot be resubmitted as a place event; inspect its saved task")
            if bound.get("conversation_id"):  # a saved binding keeps its room (records of 1.5 included)
                conversation, thread = str(bound["conversation_id"]), str(bound.get("thread_id") or "")
            else:
                conversation, thread = presence_address(principal, context_id)
            now = time.time()
            arrival = {
                "schema": 1, "digest": digest, "principal": principal, "message_id": message_id,
                "retryable": bool(message_id), "task_id": task_id, "context_id": context_id, "content": content,
                "event": presence_event(source_id=digest, conversation_id=conversation, thread_id=thread,
                                        actor=actor, text=text),
                "created_at": now, "deadline": now + self.window,
                "previous": None if fresh else bound.get("client_message_id") or "",
            }
            path = self.arrival_path(digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._watch_arrival(digest)  # never trails its published reservation
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(arrival, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            self.attach_arrival(arrival)
            if on_reserved is not None:
                on_reserved(digest)
            return arrival, True
