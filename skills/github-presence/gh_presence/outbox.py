"""One durable path for automatic/tool comments and dependent issue closure."""
from __future__ import annotations

import json
import time

from .config import connection_account
from .events import context_fingerprint
from .provider import can_close_issue
from .http import Effect, TransportError
from .store import digest, dump, now

# A conservative per-comment size, not a content cap. Every character is retained
# in deterministic physical parts; validation failures remain visible.
PART_CHARS = 12000


class Outbox:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def enqueue(self, delivery_id, event_id, body, origin, *, operation="comment", reason=None, reporting=1):
        if not isinstance(body, str) or not body.strip():
            raise ValueError("nonempty_body_required")
        event = self.store.event(event_id)
        if event["message"].get("discovery_only"):
            raise ValueError("notification_discovery_has_no_write_authority")
        route = event["message"]["reply_route"]
        if event["account_id"] != connection_account(self.config):
            raise ValueError("writer_account_changed")
        if route["repo"]["id"] not in {r["id"] for r in self.config["repositories"]}:
            raise ValueError("repository_not_selected")
        if operation == "close" and (route["thread_kind"] != "issue" or reason not in ("completed", "not_planned")):
            raise ValueError("only_explained_issue_closure_supported")
        intent = {"event_id": event_id, "body": body, "origin": origin, "route": route,
                  "operation": operation, "state_reason": reason, "reporting": reporting,
                  "account_id": event["account_id"], "conversation_id": event["conversation_id"],
                  "thread_id": event["thread_id"], "writer_id": self.config["expected_writer_id"],
                  "output_ref": delivery_id[len("output:"):] if delivery_id.startswith("output:") else None,
                  "context_fingerprint": event["message"].get("context_fingerprint")}
        with self.store.transaction() as db:
            row = db.execute("SELECT intent FROM outbox WHERE id=?", (delivery_id,)).fetchone()
            if row and row["intent"] != dump(intent):
                raise ValueError("request_identity_conflict")
            if not row:
                db.execute("INSERT INTO outbox(id,event_id,intent,created) VALUES(?,?,?,?)",
                           (delivery_id, event_id, dump(intent), now()))
                for part, start in enumerate(range(0, len(body), PART_CHARS)):
                    marker = "<!-- ouroboros-output-ref:" + digest([delivery_id, part]) + " -->"
                    physical = body[start:start + PART_CHARS] + "\n\n" + marker
                    db.execute("INSERT INTO parts(delivery_id,part,body,marker,body_hash) VALUES(?,?,?,?,?)",
                               (delivery_id, part, physical, marker, digest(physical)))
        return self.inspect(delivery_id)

    def tool(self, *, account_id, conversation_id, thread_id, source_event_id, body, request_id,
             operation="comment", state_reason=None, task_id=None, reconsider_fingerprint=None):
        if not request_id or not isinstance(request_id, str) or len(request_id) > 240:
            raise ValueError("stable_request_id_required")
        event = self.store.event(source_event_id)
        if (account_id, conversation_id, thread_id) != tuple(event[k] for k in ("account_id", "conversation_id", "thread_id")):
            raise ValueError("source_address_mismatch")
        origin = {"kind": "tool", "source_event_id": source_event_id}
        if task_id:
            origin["task_id"] = task_id
        delivery_id = "tool:" + request_id
        # Provenance is frozen on first request; a later task may inspect/reconsider it.
        existing = self.store.one("SELECT intent FROM outbox WHERE id=?", (delivery_id,))
        if existing:
            origin = json.loads(existing["intent"])["origin"]
        self.enqueue(delivery_id, source_event_id, body, origin, operation=operation, reason=state_reason)
        if reconsider_fingerprint:
            with self.store.transaction() as db:
                row = db.execute("SELECT state,detail FROM outbox WHERE id=?", (delivery_id,)).fetchone()
                detail = json.loads(row["detail"])
                if row["state"] != "needs_reconsideration" or detail.get("current_fingerprint") != reconsider_fingerprint:
                    raise ValueError("reconsideration_observation_mismatch")
                detail["approved_fingerprint"] = reconsider_fingerprint
                db.execute("UPDATE outbox SET state='queued',due=0,detail=? WHERE id=?", (dump(detail), delivery_id))
        return self.inspect(delivery_id)

    def inspect(self, delivery_id):
        row = self.store.one("SELECT id,state,due,detail FROM outbox WHERE id=?", (delivery_id,))
        if not row:
            raise ValueError("unknown_delivery_id")
        row["detail"] = json.loads(row["detail"])
        row["parts"] = self.store.rows("SELECT part,state,receipt FROM parts WHERE delivery_id=? ORDER BY part", (delivery_id,))
        for part in row["parts"]:
            part["receipt"] = json.loads(part["receipt"]) if part["receipt"] else None
        return row


class Sender:
    def __init__(self, store, provider, host, config):
        self.store, self.provider, self.host, self.config = store, provider, host, config

    def hold(self, row, state, code, detail=None, due=0):
        detail = {**json.loads(row["detail"]), **(detail or {}), "code": code, "observed_at": now()}
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET state=?,detail=?,due=?,report_due=? WHERE id=?",
                       (state, dump(detail), due, int(bool(detail.get("closure_receipt"))), row["id"]))

    def report(self, row, intent, part, state, text, message):
        if intent["reporting"] != 1:
            self.store.gap("reporting:" + row["id"], "host_reporting_not_negotiated")
            return
        value = {"schema_version": 1, "delivery_id": row["id"], "part_id": str(part), "state": state,
                 "provider": "github", "account_id": intent["account_id"], "conversation_id": intent["conversation_id"],
                 "thread_id": intent["thread_id"], "text": text, "format": "markdown", "message": message,
                 "origin": intent["origin"]}
        key = row["id"] + ":" + str(part)
        with self.store.transaction() as db:
            old = db.execute("SELECT value FROM reports WHERE id=?", (key,)).fetchone()
            if old and json.loads(old["value"])["state"] != state:
                # The real host keys receipts by (delivery, part, state).
                # Preserve the earlier unknown report and the same physical
                # part ID when exact readback subsequently proves delivery.
                key += ":" + state
                old = db.execute("SELECT value FROM reports WHERE id=?", (key,)).fetchone()
            if old and old["value"] != dump(value):
                raise ValueError("immutable_report_conflict")
            db.execute("INSERT OR IGNORE INTO reports(id,value) VALUES(?,?)", (key, dump(value)))
            if isinstance(part, int):
                db.execute("UPDATE parts SET report_due=0 WHERE delivery_id=? AND part=?", (row["id"], part))
            else:
                db.execute("UPDATE outbox SET report_due=0 WHERE id=?", (row["id"],))

    def verify_comment(self, intent, body, item, physical_route=None):
        route = physical_route or intent["route"]
        if not item or item.get("body") != body:
            raise TransportError("readback_content_mismatch")
        actor = self.provider.actor(item)
        if actor.get("platform_actor_id") != str(intent["writer_id"]):
            raise TransportError("readback_author_mismatch")
        if route["kind"] == "discussion":
            if (item.get("discussion") or {}).get("id") != route["discussion_id"]:
                raise TransportError("readback_discussion_mismatch")
            if (item.get("replyTo") or {}).get("id") != route.get("reply_to"):
                raise TransportError("readback_parent_mismatch")
        else:
            # Authoritative resource URLs come from authenticated readback, not input markdown.
            expected = "/repos/{}/{}s/{}".format(route["repo"]["full_name"],
                                                "pull" if route["kind"] == "inline" else "issue", route["number"])
            field = "pull_request_url" if route["kind"] == "inline" else "issue_url"
            if item.get(field) != self.provider.http.base + expected:
                raise TransportError("readback_thread_mismatch")
            if route["kind"] == "inline" and item.get("in_reply_to_id") != route["root_comment_id"]:
                raise TransportError("readback_parent_mismatch")
        numeric_id = item.get("databaseId") if route["kind"] == "discussion" else item.get("id")
        if type(numeric_id) is not int or numeric_id <= 0:
            raise TransportError("numeric_provider_receipt_missing")
        return {"provider_id": numeric_id, "node_id": item.get("id") if route["kind"] == "discussion" else item.get("node_id"),
                "url": item.get("html_url") or item.get("url"), "author_id": actor["platform_actor_id"],
                "route": intent["route"], "physical_route": route, "confirmed_at": now(), "output_ref": intent.get("output_ref")}

    def repair_reports(self):
        # Repairs the crash window between local receipt COMMIT and report creation.
        for part in self.store.rows("SELECT p.*,o.intent,o.detail FROM parts p JOIN outbox o ON o.id=p.delivery_id "
                                    "WHERE p.report_due=1 AND (p.state='uncertain' OR "
                                    "(p.state IN ('delivered','failed') AND p.receipt IS NOT NULL)) LIMIT 32"):
            intent = json.loads(part["intent"])
            message = ({"code": "effect_unconfirmed", "route": intent["route"]} if part["state"] == "uncertain"
                       else json.loads(part["receipt"]))
            self.report({"id": part["delivery_id"]}, intent, part["part"], part["state"], part["body"], message)
        # Old closure rows may have report_due=0: their known uncertainty must
        # still be reconstructible without a successful provider read.
        for row in self.store.rows("SELECT * FROM outbox o WHERE state='uncertain' "
                                   "AND json_extract(detail,'$.closure_started') IS NOT NULL AND NOT EXISTS "
                                   "(SELECT 1 FROM reports r WHERE json_extract(r.value,'$.delivery_id')=o.id "
                                   "AND json_extract(r.value,'$.part_id')='status' AND json_extract(r.value,'$.state')='uncertain') LIMIT 32"):
            intent = json.loads(row["intent"])
            self.report(row, intent, "status", "uncertain", "", {"code": "closure_not_proven", "route": intent["route"]})
        for row in self.store.rows("SELECT * FROM outbox WHERE report_due=1 AND state IN ('delivered','failed') LIMIT 32"):
            detail, intent = json.loads(row["detail"]), json.loads(row["intent"])
            if detail.get("closure_receipt"):
                self.report(row, intent, "status", row["state"], "", detail["closure_receipt"])
            else:
                with self.store.transaction() as db:
                    db.execute("UPDATE outbox SET report_due=0 WHERE id=?", (row["id"],))

    def tick(self):
        if self.store.meta("last_mutation_at", 0) + 1 > time.time():
            return False
        row = self.store.one("SELECT * FROM outbox WHERE state IN ('queued','held','closing') AND due<=? ORDER BY created,id LIMIT 1",
                             (time.time(),))
        if not row:
            return False
        intent = json.loads(row["intent"])
        try:
            if not self.config["enabled"]:
                self.hold(row, "held", "collection_disabled", due=time.time() + 30)
                return False
            if intent["account_id"] != connection_account(self.config) or intent["route"]["repo"]["id"] not in {r["id"] for r in self.config["repositories"]}:
                self.hold(row, "held", "configuration_identity_changed", due=time.time() + 60)
                return False
            self.host.active(intent)
            self.provider.identity(intent["writer_id"])
            repo, target, history = self.provider.thread_snapshot(intent["route"])
            physical_route = {**intent["route"], "repo": {"id": repo["id"], "full_name": repo["full_name"]}}
            if repo.get("archived") or target.get("locked"):
                self.hold(row, "failed", "target_locked_or_archived")
                self.report(row, intent, "status", "failed", "", {"code": "target_locked_or_archived"})
                return False
            parts = self.store.rows("SELECT * FROM parts WHERE delivery_id=? ORDER BY part", (row["id"],))
            if any(p["state"] in ("uncertain", "failed", "sending") for p in parts):
                self.hold(row, "uncertain", "explanation_or_part_unconfirmed")
                return False
            # Exclude only our receipt-proven physical parts from the freshness comparison.
            owned_ids = {str(json.loads(p["receipt"])["provider_id"]) for p in parts if p["receipt"]}
            relevant = [r for r in history if str(r.get("databaseId", r.get("id"))) not in owned_ids]
            current = context_fingerprint(relevant)
            detail = json.loads(row["detail"])
            expected = detail.get("approved_fingerprint") or intent["context_fingerprint"]
            if not expected or expected != current:
                self.hold(row, "needs_reconsideration", "context_changed",
                          {"current_fingerprint": current, "history": history, "expected_fingerprint": expected})
                return False
            part = next((p for p in parts if p["state"] == "queued"), None)
            # Lookup and identity are deliberately repeated before *every* physical part.
            if part:
                self.host.active(intent)
                with self.store.transaction() as db:
                    db.execute("UPDATE parts SET state='sending' WHERE delivery_id=? AND part=?", (row["id"], part["part"]))
                    db.execute("UPDATE outbox SET state='sending' WHERE id=?", (row["id"],))
                self.store.set_meta("last_mutation_at", time.time())
                post_returned = False
                try:
                    sent = self.provider.send(physical_route, part["body"])
                    post_returned = True
                    row["detail"] = dump({**json.loads(row["detail"]), "candidate_response": sent,
                                          "candidate_part": part["part"]})
                    self.hold(row, "sending", "readback_pending")
                    readback = self.provider.readback(physical_route, sent)
                    receipt = self.verify_comment(intent, part["body"], readback, physical_route)
                except (TransportError, KeyError, TypeError, ValueError) as exc:
                    response = getattr(exc, "response", None)
                    if not post_returned and getattr(exc, "effect", None) == Effect.RATE_REFUSED:
                        with self.store.transaction() as db:
                            db.execute("UPDATE parts SET state='queued' WHERE delivery_id=? AND part=?", (row["id"], part["part"]))
                        self.hold(row, "held", "provider_rate_refusal", due=exc.retry_at or time.time() + 60)
                        return False
                    if not post_returned and getattr(exc, "effect", None) == Effect.REJECTED:
                        # A definite HTTP rejection is distinct from a timed-out
                        # request. It is never silently regenerated by a retry.
                        self.hold(row, "failed", "provider_rejected_comment")
                        failure = {"code": "provider_rejected_comment", "http_status": response.status}
                        with self.store.transaction() as db:
                            db.execute("UPDATE parts SET state='failed',receipt=? WHERE delivery_id=? AND part=?", (dump(failure), row["id"], part["part"]))
                        self.report(row, intent, part["part"], "failed", part["body"],
                                    failure)
                        return False
                    # Readback rejection/rate limits say nothing about the POST's
                    # effect. Likewise 5xx/timeout/partial GraphQL may have committed.
                    self.hold(row, "uncertain", getattr(exc, "code", "provider_receipt_invalid"))
                    with self.store.transaction() as db:
                        db.execute("UPDATE parts SET state='uncertain' WHERE delivery_id=? AND part=?", (row["id"], part["part"]))
                    return False
                with self.store.transaction() as db:
                    db.execute("UPDATE parts SET state='delivered',receipt=? WHERE delivery_id=? AND part=?",
                               (dump(receipt), row["id"], part["part"]))
                    db.execute("UPDATE outbox SET state='queued' WHERE id=?", (row["id"],))
                self.report(row, intent, part["part"], "delivered", part["body"], receipt)
                return True
            if intent["operation"] == "close":
                # An explanatory comment is already confirmed, never merely queued.
                if target.get("state") == "closed":
                    self.hold(row, "held", "already_closed_effect_not_attributed", due=time.time() + 60)
                    return False
                if not can_close_issue(self.provider, repo, target, intent["writer_id"]):
                    self.hold(row, "held", "closure_permission_not_proven", due=time.time() + 60)
                    return False
                self.host.active(intent)
                with self.store.transaction() as db:
                    db.execute("UPDATE outbox SET state='sending',detail=? WHERE id=?",
                               (dump({**detail, "closure_started": now()}), row["id"]))
                self.store.set_meta("last_mutation_at", time.time())
                close_returned = False
                try:
                    changed = self.provider.close_issue(physical_route, intent["state_reason"])
                    close_returned = True
                    fresh = self.provider.get("/repos/{}/issues/{}".format(physical_route["repo"]["full_name"], physical_route["number"])).data
                    if fresh.get("state") != "closed" or fresh.get("id") != changed.get("id"):
                        raise TransportError("closure_readback_mismatch")
                    receipt = {"operation": "close_issue", "provider_id": changed["id"], "state": "closed",
                               "state_reason": changed.get("state_reason"), "url": changed.get("html_url"),
                               "route": intent["route"], "confirmed_at": now()}
                except (TransportError, KeyError, TypeError, ValueError) as exc:
                    if not close_returned and getattr(exc, "effect", None) == Effect.RATE_REFUSED:
                        self.hold(row, "held", "provider_rate_refusal", due=exc.retry_at or time.time() + 60)
                        return False
                    if not close_returned and getattr(exc, "effect", None) == Effect.REJECTED:
                        failure = {"code": "provider_rejected_closure", "http_status": exc.response.status}
                        self.hold(row, "failed", failure["code"], {"closure_receipt": failure})
                        self.report(row, intent, "status", "failed", "", failure)
                        return False
                    self.hold(row, "uncertain", getattr(exc, "code", "closure_receipt_invalid"), {"closure_started": now()})
                    return False
                self.hold(row, "delivered", "issue_closed", {"closure_receipt": receipt})
                self.report(row, intent, "status", "delivered", "", receipt)
            else:
                self.hold(row, "delivered", "all_parts_confirmed")
            return True
        except TransportError as exc:
            self.hold(row, "held", exc.code, due=exc.retry_at or time.time() + 30)
            return False

    def reconcile_uncertain(self):
        """Retry read failures fairly. Absence never authorizes another mutation."""
        row = self.store.one("SELECT * FROM outbox WHERE state='uncertain' AND due<=? ORDER BY due,created LIMIT 1",
                             (time.time(),))
        if not row:
            return
        detail, intent = json.loads(row["detail"]), json.loads(row["intent"])
        # Report already-known uncertainty before attempting reconciliation.
        # Its fixed bytes and state identity survive read failures and later
        # exact evidence, which gets a separate delivered report.
        for part in self.store.rows("SELECT * FROM parts WHERE delivery_id=? AND state='uncertain'", (row["id"],)):
            self.report(row, intent, part["part"], "uncertain", part["body"], {"code": "effect_unconfirmed", "route": intent["route"]})
        if intent["operation"] == "close" and detail.get("closure_started"):
            self.report(row, intent, "status", "uncertain", "", {"code": "closure_not_proven", "route": intent["route"]})
        try:
            repo, _, history = self.provider.thread_snapshot(intent["route"])
            physical_route = {**intent["route"], "repo": {"id": repo["id"], "full_name": repo["full_name"]}}
            ambiguous = False
            for part in self.store.rows("SELECT * FROM parts WHERE delivery_id=? AND state='uncertain'", (row["id"],)):
                matches = [item for item in history if item.get("body") == part["body"] and part["marker"] in item.get("body", "")]
                verified = []
                for item in matches:
                    # A transport failure is not a negative identity observation.
                    readback = self.provider.readback(physical_route, item)
                    try:
                        verified.append(self.verify_comment(intent, part["body"], readback, physical_route))
                    except TransportError as exc:
                        if not exc.code.startswith(("readback_", "numeric_provider_")):
                            raise
                if len(verified) == 1:
                    with self.store.transaction() as db:
                        db.execute("UPDATE parts SET state='delivered',receipt=?,report_due=1 WHERE delivery_id=? AND part=?",
                                   (dump(verified[0]), row["id"], part["part"]))
                    self.report(row, intent, part["part"], "delivered", part["body"], verified[0])
                elif len(verified) > 1:
                    ambiguous = True
                    self.store.gap("reconciliation:" + row["id"], "multiple_exact_provider_matches")
            self.hold(row, "uncertain", detail.get("code", "unknown_effect"),
                      {"reconciliation_completed": now()}, due=time.time() + 300)
            if not ambiguous:
                self.store.resolve("reconciliation:" + row["id"])
        except (TransportError, KeyError, ValueError, TypeError) as exc:
            self.store.gap("reconciliation:" + row["id"], getattr(exc, "code", "provider_shape_invalid"))
            self.hold(row, "uncertain", detail.get("code", "unknown_effect"),
                      {"reconciliation_read_error": getattr(exc, "code", "provider_shape_invalid")},
                      due=getattr(exc, "retry_at", 0) or time.time() + 60)
            return
        remaining = self.store.rows("SELECT * FROM parts WHERE delivery_id=? AND state='uncertain'", (row["id"],))
        if not remaining and not (intent["operation"] == "close" and detail.get("closure_started")):
            parts = self.store.rows("SELECT * FROM parts WHERE delivery_id=?", (row["id"],))
            if all(p["state"] == "delivered" for p in parts) and intent["operation"] == "comment":
                self.hold(row, "delivered", "reconciled_by_exact_receipt")
            elif any(p["receipt"] for p in parts):
                own = {str(json.loads(p["receipt"])["provider_id"]) for p in parts if p["receipt"]}
                current = context_fingerprint([r for r in history if str(r.get("databaseId", r.get("id"))) not in own])
                self.hold(row, "needs_reconsideration", "recovered_part_confirm_next_effect",
                          {"current_fingerprint": current, "history": history})
