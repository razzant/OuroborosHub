"""Durable peer-event custody over the Host's Presence protocol, and what an outside peer is told.

No owner-chat allocation, log scraping or owner authority is used here. A receipt keeps the exact event,
the place it was first offered to (its binding), the host's answer and each work reference across
companion restarts. A receipt is saved before the Host boundary is crossed, and every offer is marked
uncertain before it crosses (until the host answers, its effect is unknown, never a refusal). An event is
offered again only under its own source id and binding, and reading a receipt never offers anything.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import time
import uuid
from urllib.parse import quote, urlencode

import httpx

PROVIDER, ACCOUNT = "a2a", "server"
ALLOWANCE_CODE = "consciousness_allowance_exhausted"
ATTENTION_VERSION = 1
RECEIPT_SCHEMA = 2
# A host with ``presence_attention_version: 1`` names a silent arrival's attention: ``heard`` (kept, no focus
# started), ``handed_off`` (running work took it), ``completed_silent`` (a focus finished and said nothing),
# ``focus_started`` (a start only: an answer may be hidden from this view) or ``unknown``. The earlier form said a
# handoff as ``heard`` with one of these two reasons; it is still read that way. No reason ever leaves this install.
HANDOFF_REASONS = frozenset({"current_author", "conversation_work"})
_TERMINAL_VIEW = frozenset({"completed", "failed", "cancelled", "lost", "refused", "busy", "unknown"})
UNRESOLVED = "\x00unresolved"  # a host with places could not say now which place takes an event
_POLL_TERMINAL = {"completed": "completed", "failed": "failed", "interrupted": "failed", "cancelled": "cancelled",
                  "lost": "lost"}

# What an outside peer is told, by (delivery class, A2A state): one short state (WIRE) and one sentence (PUBLIC),
# never an internal reason (capacity, a place's or allowance's state, a restart, running work), a reset time, a
# recipient or exception text. The internal classes stay for custody (``classify``) and never leave this install.
# How much an outside agent learns is the owner's choice (short states without internal reasons); changing it
# means changing these two tables only.
PUBLIC = {
    ("answered", "completed"): "",
    ("answered", "failed"): "Answered; the work did not complete.",
    ("answered_separately", "completed"): "Passed on; no separate answer will follow.",
    ("no_answer", "completed"): "Processed; no answer was given.",
    ("passed_to_running_work", "completed"): "Passed on; no separate answer will follow.",
    ("not_taken_up", "rejected"): "Saved; no response was started.",
    ("no_capacity", "submitted"): "Saved; not taken up yet.",
    ("no_capacity", "rejected"): "Not taken up; send it again later.",
    ("not_connected", "rejected"): "Not taken up; send it again later.",
    ("unknown", "completed"): "No answer; whether it was read is not known.",
    ("working", "working"): "Still working; the outcome is not known yet.",
    ("busy", "failed"): "Not taken up; send a new message later.",
    ("lost", "failed"): "Not completed; send it again later.",
    ("canceled", "canceled"): "Cancelled.",
    ("failed", "failed"): "Not completed; send it again later.",
}
WIRE = {
    ("answered", "completed"): "answered", ("answered", "failed"): "answered",
    ("answered_separately", "completed"): "passed_on", ("passed_to_running_work", "completed"): "passed_on",
    ("no_answer", "completed"): "no_answer",
    ("not_taken_up", "rejected"): "saved", ("no_capacity", "submitted"): "saved",
    ("no_capacity", "rejected"): "try_later", ("not_connected", "rejected"): "try_later",
    ("busy", "failed"): "try_later", ("lost", "failed"): "try_later", ("failed", "failed"): "try_later",
    ("unknown", "completed"): "unknown", ("working", "working"): "working", ("canceled", "canceled"): "canceled",
}
_BY_STATE = {"working": ("working", "working"), "failed": ("failed", "failed"), "canceled": ("canceled", "canceled")}


def wire(delivery, state) -> dict:
    """What a peer reads about one message: its short state and sentence. A pair this release does not name (an
    earlier record) reads as its A2A state's own, else ``unknown`` with no sentence."""
    key = (str(delivery or ""), str(state or ""))
    if key not in PUBLIC:
        key = _BY_STATE.get(key[1], key)
    return {"class": WIRE.get(key, "unknown"), "text": PUBLIC.get(key, "")}


class HostWaitExpired(RuntimeError):
    def __init__(self, message, operation_ref=""):
        super().__init__(message)
        self.operation_ref = operation_ref


class HostWorkCancelled(RuntimeError):
    pass


class HostMessageConflict(ValueError):
    pass


def conversation_key(conversation_id: str, thread_id: str = "") -> str:
    """The host's own derivation (presence_bindings.conversation_key): both the conversation and its thread."""
    return ":".join((PROVIDER, ACCOUNT, str(conversation_id or ""), str(thread_id or "") or "0"))


def presence_event(*, source_id: str, conversation_id: str, thread_id: str, actor: dict, text: str) -> dict:
    return {
        "source_event_id": source_id, "provider": PROVIDER, "account_id": ACCOUNT,
        "conversation_id": str(conversation_id), "thread_id": str(thread_id or ""),
        "conversation_key": conversation_key(conversation_id, thread_id),
        "actor": dict(actor), "conversation": {"platform": PROVIDER}, "message": {"message_id": source_id},
        "text": text, "facts": ["direct_message", "from_bot"],
    }


def same_event(saved: dict, event: dict) -> bool:
    """The same arrival: source id, address, author and words. Display facts (a renamed link) do not count."""
    def identity(item):
        actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
        return (item.get("source_event_id"), item.get("conversation_id"), item.get("thread_id") or "",
                item.get("text"), actor.get("platform_actor_id") or "")
    return identity(saved) == identity(event)


def classify(view: dict | None) -> tuple[str, str]:
    """(A2A state, delivery class) of a host view, read in order: outer custody first, then the outcome.

    A silent outcome is read by its attention: ``heard`` was kept with no focus (``not_taken_up``),
    ``handed_off`` or the earlier ``heard`` with a handoff reason went to running work, ``completed_silent``
    finished and said nothing (``no_answer``); a focus that only started, anything else, or no attention at all
    is ``unknown``. A handoff or a separate delivery promises no later answer to this message.
    """
    view = view or {}
    status = view.get("status") or "pending"
    if status == "pending":
        return "working", "working"
    if status == "busy":
        return "failed", "busy"
    if status == "waiting":
        return "submitted", "no_capacity"
    if status == "refused":
        return "rejected", view.get("refusal") or "not_connected"
    outcome = view.get("outcome")
    if outcome == "message" and view.get("text"):
        return ("completed" if status == "completed" else "failed"), "answered"
    if status == "cancelled":
        return "canceled", "canceled"
    if status == "lost":
        return "failed", "lost"
    if status == "failed":
        return "failed", "failed"
    if outcome == "tool_delivered":
        return "completed", "answered_separately"
    if outcome == "silent":
        attention = view.get("attention") if isinstance(view.get("attention"), dict) else {}
        decision, reason = attention.get("decision"), attention.get("reason")
        if decision == "handed_off" or (decision == "heard" and reason in HANDOFF_REASONS):
            return "completed", "passed_to_running_work"
        if decision == "heard":
            return "rejected", "not_taken_up"
        if decision == "completed_silent":
            return "completed", "no_answer"
    return "completed", "unknown"


def public(view: dict | None) -> tuple[str, str, str]:
    """(A2A state, delivery class, sentence) an outside peer may be told."""
    state, delivery = classify(view)
    return state, delivery, PUBLIC[(delivery, state)]


def _upgraded(record: dict) -> dict:
    """A 1.5 receipt read with today's rules: its last saved answer (an offer or a poll) stands, and its
    references are observed as they were; with no answer and no reference its offer stays unconfirmed."""
    if record.get("schema") == RECEIPT_SCHEMA or "legacy_response_ref" in record:
        return record
    upgraded = dict(record)
    payload = record.get("response") if isinstance(record.get("response"), dict) else {}
    upgraded["http_status"] = 409 if payload.get("ok") is False else 200
    turn = record.get("turn_ref") or ""
    work = record.get("work_ref") or ""
    refs = {ref for ref in (turn, work) if ref}
    # In 1.5 the last response could be the original turn or a work poll. The turn envelope names the
    # original explicitly; a work projection names the polled reference. Neither implies the other finished.
    ref = turn if turn and payload.get("turn_ref") == turn else payload.get("work_ref")
    if ref not in refs:
        ref = next(iter(refs)) if len(refs) == 1 else ""
    terminal = _poll_view(payload) is not None and payload.get("outcome") != "deferred" and payload.get("ok") is not False
    views = {key: value for key, value in (record.get("views") or {}).items()
             if value != payload or key == ref}  # keep independently observed results from an earlier read
    if terminal and ref:
        views.setdefault(ref, payload)
    upgraded.update(legacy=True, legacy_response_ref=ref, views=views,
                    observe_turn=bool(turn) and not (ref == turn and payload.get("status") == "completed"))
    return upgraded


def _poll_view(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    status = _POLL_TERMINAL.get(str(payload.get("status") or ""))
    if status is None:
        return None
    text = str(payload.get("text") or "") if payload.get("outcome") == "message" else ""
    return {"status": status, "outcome": str(payload.get("outcome") or ""), "text": text,
            "output_ref": str(payload.get("output_ref") or ""),
            "attention": payload.get("attention") if isinstance(payload.get("attention"), dict) else None,
            "detail": str(payload.get("error") or payload.get("code") or payload.get("reason") or "")}


def _seconds(value) -> "float | None":
    if type(value) not in (int, float):
        return None
    return max(1.0, float(value))


class PresenceTransport:
    def __init__(self, state_dir, base, headers, *, timeout, poll_interval, http=httpx, facts=None, clock=time.time,
                 lookup_attempts=3, lookup_pause=1.0):
        self.root = pathlib.Path(state_dir) / "presence"
        self.base, self.headers = base, headers
        self.timeout, self.poll_interval, self.http = timeout, poll_interval, http
        self.facts = facts or (lambda: {})
        self.clock = clock
        self.lookup_attempts, self.lookup_pause = lookup_attempts, lookup_pause
        self.lock = threading.RLock()
        self._offering: set[str] = set()  # an actual synchronous POST can outlive its awaiting coroutine

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

    def _update(self, source_id, change):
        with self.lock:
            record = self.load(source_id)
            if record is None:
                raise RuntimeError("Presence source is missing")
            record = change(dict(record))
            self._save(source_id, record)
            return record

    def _attention(self) -> bool:
        version = self.facts().get("presence_attention_version")
        return type(version) is int and version >= ATTENTION_VERSION

    def _place(self, event) -> str | None:
        """The place id this event is first offered to; None when the host says no place takes it.

        The binding is kept with the receipt, so a narrower place connected later never claims an event already
        offered. An older host without places leaves the choice to the host (""). A host that has places, or whose
        facts could not be read, and that cannot say now is asked again a few times; then the event stays
        unoffered (``UNRESOLVED``) rather than going out with whatever the host would choose at that moment.
        """
        query = urlencode({key: event.get(key) or "" for key in ("provider", "account_id", "conversation_id",
                                                                 "thread_id")})
        facts = {}
        for attempt in range(max(1, int(self.lookup_attempts))):
            if attempt:
                time.sleep(self.lookup_pause)
            facts = facts or self.facts()
            if facts and not facts.get("places_version"):
                return ""
            if not facts:
                continue
            try:
                response = self.http.get(self.base + "/presence/place?" + query, headers=self.headers(), timeout=10)
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if response.status_code == 200 and payload.get("place_id"):
                return str(payload["place_id"])
            if response.status_code == 404 and payload.get("code") == "place_not_connected":
                return None
        return UNRESOLVED

    def offer(self, event, source_id, *, deadline):
        """Offer the event once (or answer from its receipt) and return the view of what the host said.

        ``deadline`` is the wall-clock end of this arrival's delivery window: an allowance refusal whose
        next attempt falls after it settles as refused instead of waiting.
        """
        with self.lock:
            record = self.load(source_id)
            if record is not None and not same_event(record["event"], event):
                raise HostMessageConflict("messageId was already accepted with different content or context")
            if record is None:
                record = {"schema": RECEIPT_SCHEMA, "event": event, "deadline": float(deadline)}
                self._save(source_id, record)  # before crossing the Host boundary
        record = _upgraded(record)
        if record.get("settled") or record.get("turn_ref") or record.get("work_ref"):
            return self.read(source_id)
        action = self._recovery(record, deadline)
        if action != "offer":
            return self._not_offered(record, source_id, action)
        if self._waiting(record) and self.clock() < float(record["waiting"]["next_at"]):
            return self.view(record)  # the host named its next attempt; nothing is offered before it
        binding = record.get("binding_id", "")
        if "binding_id" not in record:
            binding = self._place(record["event"])
        elif not binding and record.get("last_offer") and (not self.facts() or self.facts().get("places_version")):
            # Offered unbound (to a host without places): offering it again now could let the host's current
            # automatic choice take it as new work, so its effect stays unknown instead.
            return self.view(record)
        with self.lock:
            record = self.load(source_id)
            action = self._recovery(record, deadline)  # place lookup or slot acquisition may have used the window
            if action != "offer":
                return self._not_offered(record, source_id, action)
            if self._waiting(record) and self.clock() < float(record["waiting"]["next_at"]):
                return self.view(record)
            binding = record.get("binding_id", binding)
            if binding is None:
                record.update(binding_id="", last_offer="refused",
                              settled={"status": "refused", "refusal": "not_connected"})
                self._save(source_id, record)
                return self.view(record)
            if binding == UNRESOLVED:
                raise HostWaitExpired("The host could not say which place takes this message; nothing was offered",
                                      source_id)
            # In flight: old attempt details belong to the previous offer, not this unknown effect.
            for key in ("waiting", "response", "http_status", "attention"):
                record.pop(key, None)
            record.update(binding_id=binding, last_offer="uncertain")
            self._save(source_id, record)
            self._offering.add(source_id)
        try:
            body = {"event": record["event"]}
            if record.get("binding_id"):
                body["binding_id"] = record["binding_id"]
            if self._attention():
                body["attention_version"] = ATTENTION_VERSION
            try:
                response = self.http.post(self.base + "/presence/turn", headers=self.headers(), json=body,
                                          timeout=self.timeout + 15)
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise HostWaitExpired("Host outcome is unknown; retry this same messageId", source_id) from exc
            if not isinstance(payload, dict):
                raise HostWaitExpired("Host response is not an object; retry this same messageId", source_id)
            record = self._update(source_id, lambda r: self._answered(r, payload, response.status_code))
        finally:
            with self.lock:
                self._offering.discard(source_id)
        return self.view(record)

    def _answered(self, record, payload, status_code):
        record.update(response=payload, http_status=status_code)
        if isinstance(payload.get("attention"), dict):
            record["attention"] = payload["attention"]
        turn, work = str(payload.get("turn_ref") or ""), str(payload.get("child_work_ref") or payload.get("work_ref") or "")
        if turn and not record.get("turn_ref"):
            record["turn_ref"] = turn
        if work and work != record.get("turn_ref"):
            record["work_ref"] = work
        # The turn itself is observed unless this answer ended it ("completed", deferred included): an older
        # host's "unresolved" and a retained refusal leave the original attempt to be read on its own.
        record["observe_turn"] = bool(record.get("turn_ref")) and payload.get("status") != "completed"
        disposition, code = payload.get("disposition"), payload.get("code")
        retry_after = _seconds(payload.get("retry_after"))
        record.pop("waiting", None)
        if payload.get("ok") is not False and status_code < 400:
            record["last_offer"] = "admitted"
        elif status_code == 429 and disposition == "retry" and code == ALLOWANCE_CODE and retry_after is not None:
            # Only the host's exact "allowance spent, offer it again then" is offered again at its own time.
            record["last_offer"] = "refused"
            next_at = self.clock() + retry_after
            if next_at >= float(record.get("deadline") or 0):
                record["settled"] = {"status": "refused", "refusal": "no_capacity"}
            else:
                record["waiting"] = {"next_at": next_at}
        elif disposition == "blocked" and code == "place_not_connected":
            record.update(last_offer="refused", settled={"status": "refused", "refusal": "not_connected"})
        elif disposition == "blocked":  # a grant or policy the host refuses: a coarse failure, not "not connected"
            record.update(last_offer="refused", settled={"status": "failed"})
        elif disposition == "rejected":
            record.update(last_offer="refused", settled={"status": "failed"})
        elif disposition == "retained" and turn:
            record["last_offer"] = "admitted"
        else:
            record["last_offer"] = "uncertain"
        return record

    def recovery(self, source_id, deadline) -> str:
        """What carrying an arrival on needs, as a read, a repeat or a restart sees it (nothing is offered here).

        Within the window, an unoffered or uncertain arrival can be offered under its saved identity.
        After it, never start an offer: expire a definitely unoffered arrival or an allowance wait;
        a previously uncertain effect stays unknown and an admitted operation can still be observed.
        """
        try:
            record = self.load(source_id)
        except RuntimeError:
            return ""
        return self._recovery(record, deadline)

    @staticmethod
    def _waiting(record):
        return record.get("last_offer") == "refused" and record.get("waiting")

    def _recovery(self, record, deadline):
        if record is not None and (record.get("schema") != RECEIPT_SCHEMA or record.get("settled")
                                   or record.get("turn_ref") or record.get("work_ref")):
            return ""
        if record is not None:
            with self.lock:
                if record["event"]["source_event_id"] in self._offering:
                    return ""
            status = self.view(record)["status"]
            if status == "unknown":
                return "expire_unknown"
            if status not in ("pending", "waiting") or record.get("last_offer") == "admitted":
                return ""
        if self.clock() < float((record or {}).get("deadline", deadline) or 0):
            return "offer"
        if record is None or not record.get("last_offer"):
            return "expire"
        if self._waiting(record):
            return "expire_waiting"
        return ""

    def _not_offered(self, record, source_id, action):
        if action == "expire":
            return self.settle_unoffered(record["event"], source_id, deadline=record["deadline"],
                                         outcome={"status": "refused", "refusal": "not_taken_up"})
        if action == "expire_waiting":
            return self.settle_expired(source_id)
        return self.view(record)

    def settle_unoffered(self, event, source_id, *, deadline, outcome):
        """Settle an arrival that was never offered (every dispatch slot taken, its window closed): a final
        outcome a repeat, a read or a restart replays instead of offering the event."""
        with self.lock:
            record = self.load(source_id) or {"schema": RECEIPT_SCHEMA, "event": event, "deadline": float(deadline)}
            if not record.get("last_offer") and not record.get("settled"):
                record = {**record, "settled": dict(outcome)}
                self._save(source_id, record)
        return self.view(record)

    def settle_expired(self, source_id):
        """At the end of the delivery window: a pending allowance refusal becomes final, but only when the last
        offer was refused (an offer with an unknown effect is never called refused)."""
        def settle(record):
            if self._waiting(record):
                record.pop("waiting", None)
                record["settled"] = {"status": "refused", "refusal": "no_capacity"}
            return record
        return self.view(self._update(source_id, settle))

    def waiting(self, source_id):
        """``{next_at, deadline}`` of an arrival waiting to be offered again, or None."""
        record = self.load(source_id)
        if not record or not self._waiting(record) or record.get("settled"):
            return None
        return {"next_at": float(record["waiting"]["next_at"]), "deadline": float(record.get("deadline") or 0)}

    def _open_refs(self, record):
        views = record.get("views") or {}
        turn, work = record.get("turn_ref") if record.get("observe_turn") else "", record.get("work_ref")
        refs = [ref for ref in ((work, turn) if record.get("legacy") else (turn, work))
                if ref and (views.get(ref) or {}).get("status") not in _POLL_TERMINAL]
        return list(dict.fromkeys(refs))

    def _work(self, ref, binding_id=""):
        """(HTTP status, payload) of one work read, or None when it cannot be used (unreachable, refused)."""
        params = {"attention_version": ATTENTION_VERSION} if self._attention() else {}
        if binding_id and binding_id != UNRESOLVED:
            params["binding_id"] = binding_id
        query = "?" + urlencode(params) if params else ""
        try:
            response = self.http.get(self.base + "/presence/work/" + quote(ref, safe="") + query,
                                     headers=self.headers(), timeout=10)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if (response.status_code not in (200, 202) or not isinstance(payload, dict) or payload.get("ok") is False
                or (payload.get("work_ref") and payload["work_ref"] != ref)):
            return None
        return response.status_code, payload

    def read(self, source_id):
        """Observe the receipt's open references (each on its own) and return the view; never offers.

        A child a work read names first (pending or final) is kept before that read's state is, and read in
        turn. A 202 or an unfinished status is pending; an unreachable or refused poll changes nothing, so it
        can neither erase what is known nor certify a loss.
        """
        with self.lock:
            record = self.load(source_id)
            if record is None:
                return None  # Legacy task records remain readable, never resubmitted.
            record = _upgraded(record)
            view = self.view(record)
            if view["status"] != "pending" or source_id in self._offering:
                return view
        polled = set()
        while refs := [ref for ref in self._open_refs(record) if ref not in polled]:
            for ref in refs:
                polled.add(ref)
                answer = self._work(ref, record.get("binding_id") or "")
                if answer is None:
                    continue
                status_code, payload = answer
                child = str(payload.get("child_work_ref") or "")
                if child and child not in (ref, record.get("turn_ref")) and not record.get("work_ref"):
                    record = self._update(source_id, lambda saved, child=child: {**_upgraded(saved), "work_ref": child})
                if status_code == 202 or _poll_view(payload) is None:
                    continue  # pending: nothing new to keep

                def keep(saved, ref=ref, payload=payload):
                    saved = _upgraded(saved)
                    saved["views"] = {**(saved.get("views") or {}), ref: payload}
                    return saved
                record = self._update(source_id, keep)
                if record.get("legacy") and ref == record.get("work_ref") and payload.get("work_ref") == ref \
                        and _poll_view(payload)["text"]:
                    return self.view(record)  # an authored legacy child answer needs no original-turn read
        return self.view(record)

    def view(self, record):
        """What the receipt says now: pending, waiting, refused, or a terminal outcome with its answer."""
        record = _upgraded(record)
        base = {"status": "pending", "outcome": "", "text": "", "attention": record.get("attention"),
                "refusal": "", "detail": "", "next_at": None, "operation_ref": record["event"]["source_event_id"]}
        settled = record.get("settled")
        if settled:
            if settled.get("status") in ("busy", "failed"):
                return {**base, "status": settled["status"]}
            return {**base, "status": "refused", "refusal": settled.get("refusal") or "not_connected"}
        deadline = record.get("deadline")
        payload = record.get("response") if isinstance(record.get("response"), dict) else {}
        if not (record.get("turn_ref") or record.get("work_ref")):
            with self.lock:
                offering = base["operation_ref"] in self._offering
            if not offering and ((record.get("legacy") and _poll_view(payload) is None) or (
                    deadline and self.clock() >= float(deadline) and record.get("last_offer") in ("uncertain", "admitted"))):
                # A legacy event alone or a spent window without a reference proves no rejection.
                base["status"] = "unknown"
        if not payload and not (record.get("turn_ref") or record.get("work_ref")):
            return base
        error = payload.get("ok") is False or int(record.get("http_status") or 200) >= 400
        if error:
            if self._waiting(record):
                return {**base, "status": "waiting", "next_at": record["waiting"]["next_at"]}
            if payload.get("disposition") == "rejected" and record.get("last_offer") != "uncertain":
                return {**base, "status": "failed", "detail": str(payload.get("error") or payload.get("code") or "")}
            if not (payload.get("disposition") == "retained" and record.get("turn_ref")):
                if record.get("last_offer") == "uncertain":
                    return {**base, "attention": None}
                return {**base, "detail": str(payload.get("error") or payload.get("code") or "")}
        views = record.get("views") or {}
        offered = None if error or payload.get("outcome") == "deferred" else _poll_view(payload)
        if record.get("legacy") and offered:
            ref = record["legacy_response_ref"]
            if not ref and record.get("turn_ref") and record.get("work_ref"):
                # The old shape sometimes did not identify which of two references produced an answer.
                # Keep the saved outcome but do not attach it to either reference by guessing.
                return {**base, **offered}
            if ref and ref == record.get("work_ref"):
                offered = None
        turn_ref, work_ref = record.get("turn_ref"), record.get("work_ref")
        child = views.get(work_ref) or {}
        work = _poll_view(child) if work_ref else None
        if not record.get("legacy") and not turn_ref and work_ref and payload.get("work_ref") == work_ref \
                and not payload.get("child_work_ref") and offered and offered["text"] and offered["output_ref"]:
            # The offer itself carried this operation's terminal output. No GET is owed for that reference.
            # A deferred reply (or an explicitly separate child) still needs its own observation.
            work, offered = work or offered, None
        turn = _poll_view(views.get(turn_ref)) or (offered if not record.get("observe_turn") else None)
        legacy_answer = record.get("legacy") and work and work["text"] and child.get("work_ref") == work_ref
        if (record.get("observe_turn") and turn is None and not legacy_answer) or (work_ref and work is None):
            return base
        deciding = work or turn
        if deciding is None:
            if payload.get("outcome") == "deferred" and not error:
                return {**base, "status": "completed", "outcome": "deferred"}  # nothing further to follow
            return base
        outputs, seen = [], set()
        for item in (turn, work):
            if item and item["text"] and (not item["output_ref"] or item["output_ref"] not in seen):
                seen.add(item["output_ref"])
                outputs.append(item["text"])
        return {**base, "status": deciding["status"], "outcome": "message" if outputs else deciding["outcome"],
                "text": "\n\n".join(outputs), "detail": deciding["detail"],
                "attention": deciding["attention"] or record.get("attention")}

    def wait(self, source_id, deadline, read=None):
        """Poll until the receipt is terminal; at ``deadline`` (monotonic) the outcome is still unknown."""
        while time.monotonic() < deadline:
            view = (read or self.read)(source_id)
            if view and view.get("status") in _TERMINAL_VIEW:
                return view
            time.sleep(max(0.01, float(self.poll_interval)))  # A2A_PROGRESS_POLL_SEC is at least 1 s in service
        raise HostWaitExpired("A2A_STREAM_DEADLINE_SEC expired; Host work may still be running", source_id)
