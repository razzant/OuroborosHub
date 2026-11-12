"""The companion's Presence protocol: host capabilities, events in per-ticket order, late outputs, receipts.

Nothing here decides what to say or whom to wake: the host decides attention, the mind decides words. A turn
answer's disposition is followed as documented (retry keeps the event at the head of its ticket, retained
releases it, blocked waits for repair, rejected ends it). An output the host released for a ticket after a turn
is posted once, as a private note; customers hear only what the reply and email tools send.
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
OPEN_HEAD = ("ready", "submitting", "blocked")


class Presence:
    def __init__(self, store, host, *, sends, account: str):
        self.store, self.host, self.sends, self.account = store, host, sends, account
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT, thread_name_prefix="freshdesk-turn")
        self.inflight: dict = {}

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
        capabilities = {"ready": not missing, "missing": missing, "presence_identity": data.get("presence_identity"),
                        "observed_at": now_iso()}
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
        for future, (event_id, ticket_id) in list(self.inflight.items()):
            if not future.done():
                continue
            del self.inflight[future]
            try:
                status, data = future.result()
            except HostUnavailable as exc:
                status, data = 0, {"disposition": "retry", "code": exc.code}
            self.accept(event_id, ticket_id, status, data if isinstance(data, dict) else {})

    def dispatch(self) -> int:
        """Offer each ticket's oldest open event, at most ``MAX_IN_FLIGHT`` tickets at a time."""
        self.settle()
        busy = {ticket for _, ticket in self.inflight.values()}
        offered, heads = 0, set()
        for row in self.store.rows("SELECT * FROM inbox WHERE state IN ('ready','submitting','blocked') ORDER BY seq"):
            if row["ticket_id"] in heads:
                continue
            heads.add(row["ticket_id"])  # only the head of each ticket's queue may go
            if row["ticket_id"] in busy or row["state"] != "ready" or row["due"] > time.time():
                continue
            if len(self.inflight) >= MAX_IN_FLIGHT:
                break
            payload = {"event": json.loads(row["event"]), "staged_files": [], "continuation_version": 2,
                       "delivery_reporting_version": 1}
            self.store.db.execute("UPDATE inbox SET state='submitting', attempts=attempts+1 WHERE source_event_id=?",
                                  (row["source_event_id"],))
            self.inflight[self.pool.submit(self.host.turn, payload)] = (row["source_event_id"], row["ticket_id"])
            busy.add(row["ticket_id"])
            offered += 1
        return offered

    def accept(self, event_id: str, ticket_id: str, status: int, data: dict) -> None:
        disposition = data.get("disposition")
        retry = data.get("retry_after")
        retry = max(1.0, float(retry)) if isinstance(retry, (int, float)) else 5.0
        if disposition == "retry" or status in (0, 429, 502, 503, 504) or (status >= 500 and not disposition):
            state, due = "ready", time.time() + retry
        elif disposition == "rejected" or status in (400, 409):
            state, due = "rejected", 0
            self.store.gap(f"event:{event_id}", str(data.get("code") or "rejected"), {"status": status})
        elif disposition == "blocked" or status in (401, 403, 404):
            state, due = "blocked", time.time() + 60
            self.store.gap(f"event:{event_id}", str(data.get("code") or "blocked"), {"status": status})
        elif disposition == "retained":
            state, due = "retained", 0
        else:
            state, due = "accepted", 0
        if state in ("accepted", "retained"):
            self.store.resolve(f"event:{event_id}")
            self._remember_refs(event_id, ticket_id, data)
            self._take_outputs(event_id, ticket_id, data)
        self.store.db.execute("UPDATE inbox SET state=?, due=?, response=? WHERE source_event_id=?",
                              (state, due, dump({"status": status, **{k: data.get(k) for k in (
                                  "status", "outcome", "disposition", "code", "turn_ref", "work_ref",
                                  "continuation_ref", "child_work_ref")}}), event_id))

    def _remember_refs(self, event_id: str, ticket_id: str, data: dict) -> None:
        refs = []
        if data.get("status") == "continuing" and data.get("continuation_ref"):
            refs.append((data["continuation_ref"], "continuation"))
        for key in ("work_ref", "child_work_ref"):
            if data.get(key) and data.get(key) != data.get("turn_ref") or (key == "child_work_ref" and data.get(key)):
                refs.append((data[key], "work" if key == "work_ref" else "child"))
        for ref, kind in refs:
            self.store.db.execute("INSERT OR IGNORE INTO refs(ref, source_event_id, ticket_id, kind, due) VALUES(?,?,?,?,?)",
                                  (str(ref), event_id, ticket_id, kind, time.time() + 5))

    def _take_outputs(self, event_id: str, ticket_id: str, data: dict) -> None:
        outputs = [o for o in (data.get("outputs") or []) if isinstance(o, dict)]
        if data.get("text"):
            outputs.append({k: data.get(k) for k in ("output_ref", "outcome", "text")})
        for output in outputs:
            text = str(output.get("text") or "").strip()
            ref = str(output.get("output_ref") or "")
            if not text or output.get("outcome") not in ("message", "deferred"):
                continue
            if not ref:
                self.store.gap(f"output:{event_id}", "output_ref_missing")
                continue
            self.store.db.execute("INSERT OR IGNORE INTO outputs(output_ref, ticket_id, text, source_event_id) "
                                  "VALUES(?,?,?,?)", (ref, ticket_id, text, event_id))

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
            self._take_outputs(row["source_event_id"], row["ticket_id"], data)
            if data.get("child_work_ref"):
                self.store.db.execute("INSERT OR IGNORE INTO refs(ref, source_event_id, ticket_id, kind, due) "
                                      "VALUES(?,?,?,?,?)", (str(data["child_work_ref"]), row["source_event_id"],
                                                            row["ticket_id"], "child", time.time() + 5))
            if status == 404 or (status == 200 and data.get("status") in TERMINAL):
                state, due = "done", 0
            elif status in (200, 202):
                state, due = "pending", time.time() + 10
            else:
                state, due = "pending", time.time() + 30
            self.store.db.execute("UPDATE refs SET state=?, due=?, response=? WHERE ref=?",
                                  (state, due, dump({"status": status, "work": data.get("status")}), row["ref"]))

    def deliver_outputs(self) -> None:
        for row in self.store.rows("SELECT * FROM outputs WHERE state='queued' AND due<=? LIMIT 5", (time.time(),)):
            request_id = "output:" + hashlib.sha256(row["output_ref"].encode("utf-8")).hexdigest()[:40]
            try:
                result = self.sends.note(None, account_id=self.account, ticket_id=row["ticket_id"], text=row["text"],
                                         request_id=request_id, origin="automatic",
                                         source_event_id=row["source_event_id"])
            except Exception as exc:  # a refused argument or a store error: keep it visible, never resend blindly
                result = {"ok": False, "code": type(exc).__name__}
            if result.get("ok"):
                state = "delivered"
            elif str(result.get("code", "")).startswith("place_"):
                state = "held"
                self.store.gap(f"output:{row['output_ref']}", result["code"], {"ticket_id": row["ticket_id"]})
            elif result.get("code") in ("rate_limited", "place_unknown"):
                self.store.db.execute("UPDATE outputs SET due=? WHERE output_ref=?",
                                      (time.time() + float(result.get("retry_after") or 30), row["output_ref"]))
                continue
            else:
                state = "uncertain" if result.get("code") == "uncertain" else "failed"
                self.store.gap(f"output:{row['output_ref']}", str(result.get("code")), {"ticket_id": row["ticket_id"]})
            self.store.db.execute("UPDATE outputs SET state=?, request_id=?, detail=? WHERE output_ref=?",
                                  (state, request_id, dump(result)[:2000], row["output_ref"]))

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
