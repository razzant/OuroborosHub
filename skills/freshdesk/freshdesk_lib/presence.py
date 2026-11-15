"""The companion's Presence protocol: host capabilities, events in per-ticket order, late outputs, receipts.

Nothing here decides what to say or whom to wake: the host decides attention, the mind decides words. A turn
answer's disposition is followed as documented, never its HTTP status (retry keeps the event at the head of its
ticket, retained releases it and its work is observed, blocked waits for repair and is offered again, rejected
ends it). An output the host released for a ticket after a turn is posted once, as a private note, only while the
place bound when the work was queued is still connected and Active; customers hear only what the reply and email
tools send.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time

from .host import HostUnavailable
from .store import dump, now_iso

REQUIRED = {"presence_explicit_completion": 1, "presence_identity": 1, "presence_delivery_version": 1,
            "presence_work_output_version": 1}
MAX_IN_FLIGHT = 4
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
BLOCKED_RETRY_SECONDS = 60  # a blocked event (place or grant to repair) is offered again after this


class Presence:
    def __init__(self, store, host, *, sends, account: str):
        self.store, self.host, self.sends, self.account = store, host, sends, account
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT, thread_name_prefix="freshdesk-turn")
        self.inflight: dict = {}
        self.binding = False  # the host binds queued work to its place (presence_place_binding_version 1)

    def close(self) -> None:
        self.pool.shutdown(wait=True)
        self.settle()

    # -- capabilities and place ------------------------------------------------------------------------
    def negotiate(self) -> dict:
        data = self.host.identity()
        missing = []
        if type(data.get("places_version")) is not int or data["places_version"] < 1:
            missing.append("places_version")
        missing += [key for key, value in REQUIRED.items() if data.get(key) != value]
        versions = data.get("presence_continuation_versions")
        if not (isinstance(versions, list) and 2 in versions):
            missing.append("presence_continuation_versions[2]")
        self.binding = data.get("presence_place_binding_version") == 1
        capabilities = {"ready": not missing, "missing": missing, "presence_identity": data.get("presence_identity"),
                        "place_binding": self.binding, "observed_at": now_iso()}
        self.store.set_meta("host_capabilities", capabilities)
        if missing:
            self.store.gap("host", "host_capability_gap", {"missing": missing})
        else:
            self.store.resolve("host")
        return capabilities

    def place_state(self) -> str:
        """``active``/``silent``/``paused`` while the helpdesk-wide place exists, ``unbound`` when it does not."""
        return self.host.place(self.account, "", "")["state"]

    # -- intake -----------------------------------------------------------------------------------------------
    def settle(self) -> None:
        for future, (event_id, ticket_id, binding) in list(self.inflight.items()):
            if not future.done():
                continue
            del self.inflight[future]
            try:
                status, data = future.result()
            except HostUnavailable as exc:
                status, data = 0, {"disposition": "retry", "code": exc.code}
            self.accept(event_id, ticket_id, status, data if isinstance(data, dict) else {}, binding=binding)

    def dispatch(self) -> int:
        """Offer each ticket's oldest open event, at most ``MAX_IN_FLIGHT`` tickets at a time. The place binding is
        captured before the offer, so whatever the turn releases later belongs to the connection that heard it."""
        self.settle()
        busy = {ticket for _, ticket, _ in self.inflight.values()}
        offered, heads = 0, set()
        for row in self.store.rows("SELECT * FROM inbox WHERE state IN ('ready','submitting','blocked') ORDER BY seq"):
            if row["ticket_id"] in heads:
                continue
            heads.add(row["ticket_id"])  # only the head of each ticket's queue may go
            if row["ticket_id"] in busy or row["state"] == "submitting" or row["due"] > time.time():
                continue
            if len(self.inflight) >= MAX_IN_FLIGHT:
                break
            try:
                binding = self._bind(row["ticket_id"])
            except HostUnavailable:  # offer it once the host answers again
                binding = None
            if binding is None or '"unbound"' in binding:  # no host now, or the place went away just now
                self.store.db.execute("UPDATE inbox SET due=? WHERE source_event_id=?",
                                      (time.time() + 5, row["source_event_id"]))
                continue
            payload = {"event": json.loads(row["event"]), "staged_files": [], "continuation_version": 2,
                       "delivery_reporting_version": 1}
            self.store.db.execute("UPDATE inbox SET state='submitting', attempts=attempts+1, connection=? "
                                  "WHERE source_event_id=?",
                                  (self.store.meta("connected_at") or "", row["source_event_id"]))
            self.inflight[self.pool.submit(self.host.turn, payload)] = (row["source_event_id"], row["ticket_id"],
                                                                         binding)
            busy.add(row["ticket_id"])
            offered += 1
        return offered

    def accept(self, event_id: str, ticket_id: str, status: int, data: dict, *, binding: str = "") -> None:
        """Follow the answer's ``disposition``; HTTP status alone decides nothing (core returns 409 for retained,
        blocked and retry alike). An answer without a disposition is a turn's answer only with 200; otherwise, like
        an unknown disposition, it stays retryable."""
        disposition = data.get("disposition")
        retry = data.get("retry_after")
        retry = max(1.0, float(retry)) if isinstance(retry, (int, float)) else 5.0
        if disposition == "rejected":
            state, due = "rejected", 0
            self.store.gap(f"event:{event_id}", str(data.get("code") or "rejected"), {"status": status})
        elif disposition == "blocked":
            state, due = "blocked", time.time() + BLOCKED_RETRY_SECONDS
            self.store.gap(f"event:{event_id}", str(data.get("code") or "blocked"), {"status": status})
        elif disposition == "retained":
            state, due = "retained", 0
        elif disposition or status != 200:
            state, due = "ready", time.time() + retry
        else:
            state, due = "accepted", 0
        if state in ("ready", "blocked"):  # to be offered again: only while the connection that heard it lasts
            offered = self.store.one("SELECT connection FROM inbox WHERE source_event_id=?", (event_id,))
            if offered is not None and offered["connection"] != (self.store.meta("connected_at") or ""):
                state, due = "ended", 0
                self.store.resolve(f"event:{event_id}")
                ended = self.store.meta("connection_ended") or {}
                self.store.set_meta("connection_ended", {**ended, "events_not_offered":
                                                         int(ended.get("events_not_offered") or 0) + 1})
        if state in ("accepted", "retained"):
            self.store.resolve(f"event:{event_id}")
            refs, outputs = self._refs(data), self._outputs(event_id, data)
            for ref, kind in refs:
                self.store.db.execute("INSERT OR IGNORE INTO refs(ref, source_event_id, ticket_id, kind, due, binding) "
                                      "VALUES(?,?,?,?,?,?)", (str(ref), event_id, ticket_id, kind, time.time() + 5, binding))
            self._queue_outputs(event_id, ticket_id, outputs, binding)
        self.store.db.execute("UPDATE inbox SET state=?, due=?, response=? WHERE source_event_id=?",
                              (state, due, dump({"status": status, **{k: data.get(k) for k in (
                                  "status", "outcome", "disposition", "code", "turn_ref", "work_ref",
                                  "continuation_ref", "child_work_ref")}}), event_id))

    @staticmethod
    def _refs(data: dict) -> list:
        """References whose later work may still release output: a continuing author, a retained turn, real work."""
        refs = []
        if data.get("status") == "continuing" and data.get("continuation_ref"):
            refs.append((data["continuation_ref"], "continuation"))
        if data.get("disposition") == "retained" and data.get("turn_ref"):
            refs.append((data["turn_ref"], "turn"))
        if data.get("work_ref") and data["work_ref"] not in (data.get("turn_ref"), data.get("continuation_ref")):
            refs.append((data["work_ref"], "work"))
        if data.get("child_work_ref"):
            refs.append((data["child_work_ref"], "child"))
        return refs

    def _outputs(self, event_id: str, data: dict) -> list:
        outputs = [o for o in (data.get("outputs") or []) if isinstance(o, dict)]
        if data.get("text"):
            outputs.append({k: data.get(k) for k in ("output_ref", "outcome", "text")})
        kept = []
        for output in outputs:
            text = str(output.get("text") or "").strip()
            ref = str(output.get("output_ref") or "")
            if not text or output.get("outcome") not in ("message", "deferred"):
                continue
            if not ref:
                self.store.gap(f"output:{event_id}", "output_ref_missing")
                continue
            kept.append((ref, text))
        return kept

    def _queue_outputs(self, event_id: str, ticket_id: str, outputs: list, binding: str) -> None:
        for ref, text in outputs:
            self.store.db.execute("INSERT OR IGNORE INTO outputs(output_ref, ticket_id, text, source_event_id, binding) "
                                  "VALUES(?,?,?,?,?)", (ref, ticket_id, text, event_id, binding))

    def _bind(self, ticket_id: str) -> str:
        """The place this ticket's later output belongs to, captured when its event is offered. A host without
        binding leaves it empty: delivery then needs the place to be Active at that moment. An unreachable host
        raises ``HostUnavailable``: the event waits rather than going out unbound."""
        if not self.binding:
            return ""
        answer = self.host.bind(self.account, ticket_id, ticket_id, require_place=True)
        if isinstance(answer.get("binding"), dict):
            return dump(answer["binding"])
        return dump({"unbound": str(answer.get("code") or "place_not_connected")})

    # -- late work ----------------------------------------------------------------------------------------
    def poll_refs(self, limit: int = 5) -> None:
        for row in self.store.rows("SELECT * FROM refs WHERE state='pending' AND due<=? ORDER BY due LIMIT ?",
                                   (time.time(), limit)):
            try:
                status, data = self.host.work(row["ref"])
            except HostUnavailable:
                self.store.db.execute("UPDATE refs SET due=? WHERE ref=?", (time.time() + 30, row["ref"]))
                continue
            data = data if isinstance(data, dict) else {}
            self._queue_outputs(row["source_event_id"], row["ticket_id"], self._outputs(row["source_event_id"], data),
                                row["binding"])
            if data.get("child_work_ref"):
                self.store.db.execute("INSERT OR IGNORE INTO refs(ref, source_event_id, ticket_id, kind, due, binding) "
                                      "VALUES(?,?,?,?,?,?)", (str(data["child_work_ref"]), row["source_event_id"],
                                                              row["ticket_id"], "child", time.time() + 5, row["binding"]))
            if status == 404 or (status == 200 and data.get("status") in TERMINAL):
                state, due = "done", 0
            elif status in (200, 202):
                state, due = "pending", time.time() + 10
            else:
                state, due = "pending", time.time() + 30
            self.store.db.execute("UPDATE refs SET state=?, due=?, response=? WHERE ref=?",
                                  (state, due, dump({"status": status, "work": data.get("status")}), row["ref"]))

    def _bound_refusal(self, row) -> dict | None:
        """The retained binding's verdict just before posting: a place disconnected since (even if connected again),
        changed, Silent or Paused holds the output."""
        if not row["binding"]:
            return None  # no binding: the post itself needs the place connected and Active right now
        binding = json.loads(row["binding"])
        if "unbound" in binding:
            return {"ok": False, "code": binding["unbound"]}
        answer = self.host.check(binding, self.account, row["ticket_id"], row["ticket_id"])
        if answer.get("allowed") is True:
            return None
        code = str(answer.get("code") or "place_not_allowed")
        return {"ok": False, "code": code if code.startswith("place_") else f"place_{code}"}

    def deliver_outputs(self) -> None:
        for row in self.store.rows("SELECT * FROM outputs WHERE state='queued' AND due<=? LIMIT 5", (time.time(),)):
            request_id = "output:" + hashlib.sha256(row["output_ref"].encode("utf-8")).hexdigest()[:40]
            try:
                result = self._bound_refusal(row) or self.sends.note(
                    None, account_id=self.account, ticket_id=row["ticket_id"], text=row["text"], request_id=request_id,
                    origin="automatic", source_event_id=row["source_event_id"])
            except HostUnavailable:
                result = {"ok": False, "code": "place_unknown"}
            except Exception as exc:  # a refused argument or a store error: keep it visible, never resend blindly
                result = {"ok": False, "code": type(exc).__name__}
            if not result.get("code") and result.get("recorded"):
                result = {**result, "code": result.get("state")}  # a call recorded earlier states its outcome
            if result.get("ok"):
                state = "delivered"
                self.store.resolve(f"output:{row['output_ref']}")  # an earlier hold of it is over
            elif result.get("code") in ("rate_limited", "place_unknown", "in_flight"):  # nothing sent: try later
                self.store.db.execute("UPDATE outputs SET due=? WHERE output_ref=?",
                                      (time.time() + float(result.get("retry_after") or 30), row["output_ref"]))
                continue
            elif str(result.get("code", "")).startswith("place_"):
                state = "held"
                self.store.gap(f"output:{row['output_ref']}", result["code"], {"ticket_id": row["ticket_id"]})
            else:
                state = "uncertain" if result.get("code") == "uncertain" else "failed"
                self.store.gap(f"output:{row['output_ref']}", str(result.get("code")), {"ticket_id": row["ticket_id"]})
            hold = str(result.get("code") or "") if state == "held" else ""
            self.store.db.execute("UPDATE outputs SET state=?, request_id=?, detail=?, hold=? WHERE output_ref=?",
                                  (state, request_id, dump(result)[:2000], hold, row["output_ref"]))

    def resume_held(self) -> int:
        """Called while the place is Active: outputs this companion held because the place turned Silent or Paused
        between the host releasing them and their post go out now. (An answer the host itself holds, because the
        place was Silent or Paused when it was projected, never reaches this queue: it stays with the owner in the
        room.) Work of a place disconnected since stays held: its binding check refuses it again."""
        cursor = self.store.db.execute("UPDATE outputs SET state='queued', due=0, hold='' WHERE state='held' AND "
                                       "hold IN ('place_silent', 'place_paused')")
        return cursor.rowcount

    # -- receipts -----------------------------------------------------------------------------------------
    def report_receipts(self) -> None:
        for row in self.store.pending_reports(limit=10):
            ticket = row["ticket_id"]
            origin = {"kind": row["origin"]}
            if row["task_id"]:
                origin["task_id"] = row["task_id"]
            if row["source_event_id"]:
                origin["source_event_id"] = row["source_event_id"]
            report = {"schema_version": 1, "delivery_id": row["request_id"], "part_id": "0", "state": row["state"],
                      "provider": "freshdesk", "account_id": row["host"], "conversation_id": ticket,
                      "thread_id": ticket, "text": row["text"], "format": "text",
                      "message": {"message_id": row["provider_id"]} if row["provider_id"] else {}, "origin": origin}
            try:
                status, data = self.host.delivery(report)
            except HostUnavailable:
                self.store.mark_report(row["request_id"], "pending", retry_in=30)
                continue
            if status == 200 and isinstance(data, dict) and data.get("recorded"):
                self.store.mark_report(row["request_id"], "done")
            elif status in (400, 409):
                self.store.mark_report(row["request_id"], "refused")
                self.store.gap(f"report:{row['request_id']}", f"report_{status}", {"code": (data or {}).get("code")})
            else:
                self.store.mark_report(row["request_id"], "pending", retry_in=30)
