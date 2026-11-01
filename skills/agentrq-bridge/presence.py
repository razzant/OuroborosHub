"""AgentRQ events and durable Host receipts; no owner-chat entry path."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime
import hashlib
import json
import math
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Continuation version 2: the Host answers an event as soon as its author takes the task's
# later events, and a busy conversation or slot is a typed retry, never a wait in its gate.
EARLY_CONTINUATION = 2
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
SETTLED = frozenset({"completed", "failed", "uncertain"})


def provider_event(params, mcp_url):
    url = urllib.parse.urlsplit(mcp_url)
    account = urllib.parse.urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path.rstrip("/"), "", ""))
    task = str(params.get("taskId") or params.get("task_id") or "")
    kind = str(params.get("type") or "")
    source = str(params.get("eventId") or params.get("messageId") or params.get("id") or "")
    if not source:
        source = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    facts = ["any_message"] if params.get("text") or params.get("content") else []
    if (kind == "task.created" and params.get("assignee") == "agent") or (kind == "message.created" and params.get("sender") == "human"):
        facts.append("direct_message")
    if params.get("sender") == "agent" or params.get("is_bot") is True:
        facts.append("from_bot")
    return {
        "source_event_id": "agentrq:" + kind + ":" + source, "provider": "agentrq", "account_id": account,
        "conversation_id": task or "workspace", "thread_id": task,
        "conversation_key": f"agentrq:{account}:{task or 'workspace'}",
        "actor": {"platform": "agentrq", "platform_actor_id": str(params.get("senderId") or ""),
                  "display_name": str(params.get("sender") or ""), "is_bot": "from_bot" in facts},
        "conversation": {"platform": "agentrq", "task_id": task},
        "message": dict(params), "text": str(params.get("text") or params.get("content") or params.get("title") or ""),
        "facts": facts,
    }


def lists_early_continuation(identity):
    """Whether ``GET /identity`` lists continuation version 2 (its scalar version stays 1)."""
    versions = identity.get("presence_continuation_versions") if isinstance(identity, dict) else None
    return isinstance(versions, list) and any(type(v) is int and v == EARLY_CONTINUATION for v in versions)


def retry_seconds(value):
    """A Host ``retry_after``: positive seconds, or a timezone-qualified ISO time; else None."""
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            return max(1.0, datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None
    return None


def busy_delay(field, retry_after):
    """How long a busy event stays its task's head before the same event goes again.

    ``presence_capacity_full`` names the held resource: ``conversation`` (its author is
    starting or closing; the Host says 1 s, so a starting author's intake stays responsive)
    or ``capacity`` (every active slot is held; 5 s). The Host's ``retry_after`` is a floor
    and is used as it is, for every consecutive busy answer; the older in-flight refusal
    names neither and carries no hint, so it waits like capacity. Never the long backoff
    of a failure.
    """
    return retry_after or (1.0 if field == "conversation" else 5.0)


def _unique(refs):
    return list(dict.fromkeys(str(ref) for ref in refs if ref))


class PresenceInbox:
    """Durable AgentRQ Presence custody: capture order, task FIFO, observation, replies.

    Intake offers each task's oldest event the Host has not taken yet; tasks go side by
    side and one task's events go in capture order. A Host reference in the answer is
    custody: the next event of that task may go, while observation reads every reference
    (the original turn, its continuation and each child) apart until its own end and
    delivers each selected output once. Intake and observation never wait for each other.
    """

    def __init__(self, state_dir, host_base, token, reply, *, intake_limit=3):
        self.root = pathlib.Path(state_dir) / "presence"
        self.base, self.token, self.reply = host_base.rstrip("/"), token, reply
        self.lock = threading.RLock()
        # Set by a capture and by a finished submission; the intake loop waits on it.
        self.wake = threading.Event()
        # Set by close(): nothing new is submitted, read or replied after a stop.
        self.closed = threading.Event()
        self.intake_limit = intake_limit
        # Negotiated on first need: 2 when the Host lists it, else 0 (the earlier request bytes).
        self.continuation = None
        self._negotiation = threading.Lock()
        self._pool = None
        self._submitting = {}
        self._not_before = {}
        self._settled = set()

    def _save(self, path, record):
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def _load(self, path):
        with self.lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def _next_seq(self):
        """The next capture ordinal, kept beside the records so a restart continues it."""
        counter = self.root / "order.seq"
        try:
            last = int(counter.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            last = 0
            for path in self.root.glob("*.json"):
                try:
                    seq = json.loads(path.read_text(encoding="utf-8")).get("seq", 0)
                except (OSError, ValueError, AttributeError):
                    continue
                if type(seq) is int:
                    last = max(last, seq)
        temp = counter.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(str(last + 1), encoding="ascii")
        temp.replace(counter)
        return last + 1

    def enqueue(self, event):
        key = json.dumps([event["account_id"], event["source_event_id"]], separators=(",", ":"))
        path = self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                # Capture order: a task's events reach the Host in the order they arrived.
                self._save(path, {"event": event, "state": "pending", "outputs": {}, "seq": self._next_seq()})
        self.wake.set()
        return path

    def _request(self, method, route, payload=None):
        request = urllib.request.Request(self.base + route, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"X-Skill-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            if isinstance(payload, dict) and payload.get("disposition"):
                return payload
            raise

    def continuation_version(self):
        """2 when ``/identity`` lists it, else 0; read once before the first submission."""
        with self._negotiation:
            if self.continuation is None:
                identity = self._request("GET", "/identity")
                if not isinstance(identity, dict) or identity.get("ok") is not True:
                    # A refusal or an unreadable answer decides nothing; the next submission asks again.
                    raise RuntimeError("Host identity unavailable")
                self.continuation = EARLY_CONTINUATION if lists_early_continuation(identity) else 0
            return self.continuation

    def records(self):
        """Unsettled records in capture order; records written before it existed come first."""
        rows = []
        for path in self.root.glob("*.json"):
            if path in self._settled:
                continue
            try:
                record = self._load(path)
            except (OSError, ValueError):
                continue  # Retained on disk for inspection; never guessed at.
            if record.get("state") in SETTLED:
                self._settled.add(path)
                continue
            seq = record.get("seq")
            rows.append((seq if type(seq) is int else 0, path.name, path, record))
        rows.sort(key=lambda row: row[:2])
        return [(path, record) for _seq, _name, path, record in rows]

    @staticmethod
    def _admitted(record):
        """Whether the Host took this event: it then no longer holds its task's later events."""
        return bool(record.get("refs") or record.get("turn_ref") or record.get("work_ref") or record.get("terminal")
                    or record.get("state") in SETTLED | {"held", "sending"})

    def _heads(self, now):
        """Each task's oldest event the Host has not taken yet, and seconds until a waiting one is due."""
        heads, due, wait = {}, [], 5.0
        for path, record in self.records():
            key = record["event"].get("conversation_key") or path.name
            if key not in heads and not self._admitted(record):
                heads[key] = path
        for key, path in heads.items():
            not_before = self._not_before.get(path, 0)
            if not_before > now:
                wait = min(wait, not_before - now)
            else:
                due.append((key, path))
        return due, wait

    def _executor(self):
        if self.closed.is_set():
            raise RuntimeError("presence inbox closed")
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(self.intake_limit, thread_name_prefix="agentrq-presence-intake")
        return self._pool

    def intake_pending(self):
        """Start each due task head without waiting for another task's answer.

        Returns the seconds until the intake loop should look again (at most five). A
        finished submission wakes it, so a task's next event follows the Host's answer.
        """
        for key, future in list(self._submitting.items()):
            if future.done():
                del self._submitting[key]
                if future.exception() is not None:
                    # Source and receipt stay on disk for the next pass; no ACK loss.
                    print(f"[listener] Presence intake retry: {type(future.exception()).__name__}", flush=True)
        if self.closed.is_set():
            return 5.0
        due, wait = self._heads(time.time())
        for key, path in due:
            if key in self._submitting:
                continue
            if len(self._submitting) >= self.intake_limit:
                break
            future = self._executor().submit(self.submit, path)
            self._submitting[key] = future
            future.add_done_callback(lambda _future: self.wake.set())
        return max(0.05, wait)

    def close(self):
        """Stop: no new submission, read or reply starts; queued submissions are cancelled and
        running ones joined, so their answers are kept.

        An event whose submission got no answer stays unadmitted on disk and is offered again,
        unchanged, at the next start; a reply already sending stays ``sending`` and becomes
        ``uncertain`` at the next start, never sent again.
        """
        self.closed.set()
        self.wake.set()
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def submit(self, path):
        """Offer one event; a Host reference in the answer is custody, observed apart.

        A retry keeps the event its task's head: a busy conversation or capacity soon, any
        other after the Host's ``retry_after`` or five seconds. The same event goes again.
        """
        try:
            record = self._load(path)
            if self._admitted(record) or self.closed.is_set():
                return
            version = self.continuation_version()
            if self.closed.is_set():
                return  # stopped while the Host's identity was read: nothing is submitted
            body = {"event": record["event"]}
            if version:
                body["continuation_version"] = version
            payload = self._request("POST", "/presence/turn", body)
        except Exception:
            self._not_before[path] = time.time() + 5
            raise
        with self.lock:
            record = self._load(path)
            self._admit(path, record, payload, version)

    def _admit(self, path, record, payload, version):
        record["response"] = payload
        if payload.get("ok") is False:
            disposition = payload.get("disposition")
            record["error"] = str(payload.get("error") or disposition or "Host admission unconfirmed")
            if disposition == "retained" and payload.get("turn_ref"):
                # The Host keeps the admitted turn: observe it and a named child, never resubmit.
                record["turn_ref"] = str(payload["turn_ref"])
                record["refs"] = _unique([payload["turn_ref"], payload.get("work_ref")])
            elif disposition == "rejected":
                record["state"] = "failed"
            elif disposition == "retry" and version == EARLY_CONTINUATION and payload.get("code") == "presence_capacity_full":
                # Nothing ran: the same event stays the head and goes again after the Host's wait.
                self._not_before[path] = time.time() + busy_delay(
                    str(payload.get("field") or ""), retry_seconds(payload.get("retry_after")))
            else:
                self._not_before[path] = time.time() + (retry_seconds(payload.get("retry_after")) or 5)
            self._save(path, record)
            return
        status = payload.get("status")
        turn = str(payload.get("turn_ref") or "")
        if status == "continuing":
            # Version 2: the author holds the task's later events; read its continuation.
            refs = [payload.get("continuation_ref") or turn, payload.get("work_ref"), payload.get("child_work_ref")]
        elif status in TERMINAL:
            # Ended within the request; a turn names a promoted child in work_ref.
            refs = [ref for ref in (payload.get("child_work_ref"), payload.get("work_ref")) if ref and ref != turn][:1]
        else:
            # Pending, or an older Host's unresolved turn: the turn itself is read.
            refs = [payload.get("work_ref") or turn, payload.get("child_work_ref")]
        refs = _unique(refs)
        if not refs and status not in TERMINAL:
            # Accepted, yet neither ended nor naming a reference to read: the same event goes again.
            record["error"] = "Host answer names no reference to read"
            self._not_before[path] = time.time() + 5
            self._save(path, record)
            return
        self._not_before.pop(path, None)
        record.pop("error", None)
        record["turn_ref"] = turn
        self._take_outputs(record, payload, "")
        record["refs"] = refs
        record["terminal"] = not record["refs"]
        if record["terminal"] and all(output["state"] == "delivered" for output in record["outputs"].values()):
            record["state"] = "completed"
        self._save(path, record)

    def _take_outputs(self, record, payload, producer):
        """Queue each selected output once by its identity, with its anchor in this task.

        Text of failed, cancelled or still-continuing work is speech only with the Host's
        output identity; a stop without one sends nothing. A version-2 anchor names the
        input the output covers; it is kept only when it is this task's, and the reply
        still goes to this task.
        """
        status = payload.get("status")
        task = record["event"]["conversation"]["task_id"]
        released = [row for row in payload.get("outputs") or [] if isinstance(row, dict) and row.get("output_ref")]
        final = {key: payload.get(key) for key in ("output_ref", "outcome", "text", "reply_anchor")}
        if not final["output_ref"] and status != "completed":
            final = {}
        for row in released + [final]:
            outcome, text = row.get("outcome"), row.get("text")
            if outcome not in {"message", "deferred"} or not isinstance(text, str) or not text.strip():
                continue
            fallback = ("immediate" if outcome == "deferred" else "final")
            identity = str(row.get("output_ref") or (producer + ":" + fallback if producer else fallback))
            output = record["outputs"].setdefault(identity, {"text": text, "state": "queued"})
            anchor = row.get("reply_anchor")
            message = anchor.get("message") if isinstance(anchor, dict) else None
            if ("anchor" not in output and isinstance(message, dict)
                    and str(message.get("taskId") or message.get("task_id") or "") == task):
                output["anchor"] = str(anchor.get("source_event_id") or "")

    def observe(self, path):
        """Read an admitted event's live references once, then deliver its queued outputs."""
        if self.closed.is_set():
            return
        with self.lock:
            record = self._load(path)
            if record["state"] in SETTLED:
                return
            if record["state"] == "sending":
                record.update(state="uncertain", error="Reply acceptance unknown after restart; inspect AgentRQ before retry")
                self._save(path, record)
                return
        event = record["event"]
        if not record.get("terminal"):
            refs = record.get("refs")
            if refs is None:  # recorded before references were read apart: the one it read next
                refs = _unique([record.get("work_ref") or record.get("turn_ref")])
            ended, live = list(record.get("ended_refs") or []), []
            for index, ref in enumerate(refs):
                if self.closed.is_set():
                    live.extend(refs[index:])  # stopped meanwhile: no later reference is read, none dropped
                    break
                payload = self._request("GET", "/presence/work/" + urllib.parse.quote(ref, safe=""))
                record["response"] = payload
                if payload.get("ok") is False:
                    record["error"] = str(payload.get("error") or payload.get("disposition") or "Host work unavailable")
                    if payload.get("disposition") == "rejected":
                        record["state"] = "failed"
                        self._save(path, record)
                        return
                    live.append(ref)
                    continue
                record.pop("error", None)
                self._take_outputs(record, payload, ref)
                if payload.get("status") in TERMINAL:
                    ended.append(ref)
                else:
                    live.append(ref)  # pending, or an older Host's unresolved
                child = payload.get("child_work_ref")
                if child and child not in refs + live + ended:
                    live.append(child)  # a child is read beside its parent until its own end
            record["refs"], record["ended_refs"] = _unique(live), _unique(ended)
            record["terminal"] = not record["refs"]
            self._save(path, record)  # retain references/output identity before sending
        for output in record["outputs"].values():
            if output["state"] == "delivered":
                continue
            if self.closed.is_set():
                self._save(path, record)  # still queued: the next start replies once
                return
            query = urllib.parse.urlencode({key: event[key] for key in ("provider", "account_id", "conversation_id")})
            place = self._request("GET", "/presence/place?" + query)
            if place.get("state") != "active":
                record.update(state="held", error=f"Held: {place.get('label') or 'AgentRQ'} is {place.get('state') or 'unavailable'}")
                self._save(path, record)
                return
            task = event["conversation"]["task_id"]
            if not task:
                record.update(state="held", error="Held: workspace event has no task to reply to")
                self._save(path, record)
                return
            if self.closed.is_set():
                self._save(path, record)  # stopped while the place was read: still queued, the next start replies once
                return
            record.update(state="sending", error="")
            self._save(path, record)
            try:
                receipt = self.reply(task, output["text"])
                if receipt.get("error"):
                    raise RuntimeError(str(receipt["error"]))
            except Exception:
                record.update(state="uncertain", error="Reply acceptance unknown; inspect AgentRQ before retry")
                self._save(path, record)
                return
            output.update(state="delivered", receipt=receipt)
            record["state"] = "pending"
            self._save(path, record)
        record["state"] = "completed" if record.get("terminal") else "pending"
        self._save(path, record)

    def process(self, path):
        """Submit the event if the Host has not taken it, then observe and deliver."""
        if not self._admitted(self._load(path)):
            self.submit(path)
            if not self._admitted(self._load(path)):
                return  # still its task's head (a retry) or refused
        self.observe(path)

    def observe_pending(self):
        for path, record in self.records():
            if self.closed.is_set():
                break
            if not self._admitted(record):
                continue
            try:
                self.observe(path)
            except Exception as exc:
                # Source and receipt stay on disk for the next pass; no ACK loss.
                print(f"[listener] Presence retry: {type(exc).__name__}", flush=True)

    def process_pending(self):
        """One intake pass in this thread, then one observation pass (the listener runs them apart)."""
        due, _wait = self._heads(time.time())
        for _key, path in due:
            try:
                self.submit(path)
            except Exception as exc:
                print(f"[listener] Presence retry: {type(exc).__name__}", flush=True)
        self.observe_pending()
