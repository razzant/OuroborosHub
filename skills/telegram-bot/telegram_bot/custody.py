"""SQLite custody for Telegram provider updates and deliveries."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Sequence, Any, Dict, Iterator, Optional

from .events import TelegramEvent
from .formatting import prepare_text


@dataclass(frozen=True)
class InboxLease:
    event_id: str
    payload: Dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class OutboxLease:
    delivery_id: str
    payload: Dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class WorkLease:
    event_id: str
    binding_id: str
    work_ref: str
    event: Dict[str, Any]
    attempts: int
    role: str = "deferred"


# One row per observed Host reference. ``deferred`` is a successful handoff's
# work; ``turn`` is a retained event's original attempt and ``child`` real
# admitted work of that event. Each is leased, polled and settled on its own.
_PRESENCE_WORK_TABLE = """
CREATE TABLE IF NOT EXISTS {name}(
    work_ref TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'deferred' CHECK(role IN ('deferred','turn','child')),
    event_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(
        state IN ('pending','leased','completed','failed','cancelled')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    lease_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    completed_at REAL,
    last_error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(event_id) REFERENCES inbox(event_id)
)"""


class CustodyStore:
    """Small connection-per-call store; SQLite owns queue truth across restarts."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def commit_update(self, update_id: int, event: Optional[TelegramEvent]) -> bool:
        """Atomically deduplicate an event and advance the provider offset."""
        now = time.time()
        inserted = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if event is not None:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO inbox(
                        event_id, update_id, conversation_key, payload_json,
                        state, attempts, available_at, received_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (
                        event.source_event_id,
                        int(update_id),
                        event.conversation_key,
                        _json(event.to_dict()),
                        now,
                        now,
                    ),
                )
                inserted = cursor.rowcount == 1
            current = self._metadata_int(conn, "telegram_offset")
            next_offset = max(current, int(update_id) + 1)
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES('telegram_offset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(next_offset),),
            )
            conn.commit()
        return inserted

    def telegram_offset(self) -> int:
        with self._connect() as conn:
            return self._metadata_int(conn, "telegram_offset")

    def claim_inbox(self, *, lease_sec: float = 90.0) -> Optional[InboxLease]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT event_id, payload_json, attempts
                FROM inbox AS candidate
                WHERE (
                    (candidate.state='pending' AND candidate.available_at<=?)
                    OR (candidate.state='leased' AND candidate.lease_until<=?)
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM inbox AS earlier
                    WHERE earlier.conversation_key=candidate.conversation_key
                      AND earlier.update_id<candidate.update_id
                      AND earlier.state IN ('pending','leased')
                )
                ORDER BY candidate.update_id ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            updated = conn.execute(
                """
                UPDATE inbox
                SET state='leased', lease_until=?, attempts=attempts+1, last_error=''
                WHERE event_id=? AND (
                    (state='pending' AND available_at<=?)
                    OR (state='leased' AND lease_until<=?)
                )
                """,
                (now + max(1.0, lease_sec), row["event_id"], now, now),
            )
            conn.commit()
            if updated.rowcount != 1:
                return None
            return InboxLease(
                event_id=str(row["event_id"]),
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]) + 1,
            )

    def release_inbox(
        self, event_id: str, *, reason: str, retry_after_sec: float
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbox
                SET state='pending', available_at=?, lease_until=0, last_error=?
                WHERE event_id=? AND state='leased'
                """,
                (time.time() + max(0.0, retry_after_sec), str(reason)[:500], event_id),
            )

    def release_own_leases(self) -> Dict[str, int]:
        """Return leases left by a stopped runtime to their queues at once.

        Call only from ``TelegramTransportRuntime.run()`` before its workers
        start, never when the store is opened for status or tools.  It assumes
        a single owner: only one runtime's workers claim rows for this state
        dir, so a lease that exists before they start belongs to a runtime
        that is no longer running.  Waiting for its expiry would hold the
        conversation's FIFO for up to the full Host-turn lease.  Only ``leased``
        rows move to ``pending``; attempts, send checkpoints, receipts, and
        report state are left unchanged, and the next claim counts the retry.

        Residual: if an extension reload starts the new runtime before the old
        one has stopped, this can release a live lease, and the row may be sent
        twice -- the same outcome as a lease expiring, only sooner.
        """
        now = time.time()
        released: Dict[str, int] = {}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("inbox", "outbox", "presence_work"):
                cursor = conn.execute(
                    f"UPDATE {table} SET state='pending', available_at=?, lease_until=0, "
                    "last_error='released_at_runtime_start' WHERE state='leased'",
                    (now,),
                )
                released[table] = cursor.rowcount
            conn.commit()
        return released

    def mark_inbox_failed(
        self, event_id: str, *, reason: str, receipt: Optional[Dict[str, Any]] = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbox
                SET state='failed', lease_until=0, failed_at=?, last_error=?,
                    host_receipt_json=COALESCE(?, host_receipt_json)
                WHERE event_id=? AND state='leased'
                """,
                (time.time(), str(reason)[:500], _json(receipt) if receipt else None, event_id),
            )

    def record_retention(
        self, event_id: str, *, binding_id: str, receipt: Dict[str, Any]
    ) -> None:
        """Release a retained event's conversation without resubmitting it.

        The Host kept an admitted attempt it will not regenerate. The inbox row
        keeps the source event and the complete typed receipt; the original
        attempt and any real admitted child become separately observed work.
        """
        response = receipt.get("response") if isinstance(receipt.get("response"), dict) else {}
        turn_ref = str(response.get("turn_ref") or "").strip()
        if not turn_ref:
            raise ValueError("retained receipt must name its original turn")
        child_ref = str(response.get("work_ref") or "").strip()
        mode = int(response.get("delivery_reporting_version") == 1)
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_json FROM inbox WHERE event_id=? AND state='leased'",
                (event_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ValueError("inbox event is not leased")
            event = json.loads(row["payload_json"])
            self._add_work(conn, event_id, binding_id, turn_ref, "turn", event, mode, now)
            if child_ref and child_ref != turn_ref:
                self._add_work(conn, event_id, binding_id, child_ref, "child", event, mode, now)
            updated = conn.execute(
                """
                UPDATE inbox
                SET state='submitted', lease_until=0, submitted_at=?, submission_ref=?,
                    host_receipt_json=?, last_error=''
                WHERE event_id=? AND state='leased'
                """,
                (now, turn_ref[:500], _json(receipt), event_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise ValueError("inbox event lease changed")
            conn.commit()

    @staticmethod
    def _add_work(
        conn: sqlite3.Connection,
        event_id: str,
        binding_id: str,
        work_ref: str,
        role: str,
        event: Dict[str, Any],
        mode: int,
        now: float,
    ) -> None:
        """Observe one more reference of an event; its late output names its own task."""
        source = {key: value for key, value in event.items() if key != "_reporting"}
        source["_reporting"] = {
            "version": mode,
            "account_id": event.get("account_id", ""),
            "origin": {"kind": "automatic", "task_id": work_ref,
                       "source_event_id": event.get("source_event_id", "")},
        }
        conn.execute(
            """
            INSERT OR IGNORE INTO presence_work(
                work_ref, event_id, binding_id, role, event_json, state,
                attempts, available_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (work_ref, event_id, binding_id, role, _json(source), now, now),
        )

    def record_submission(
        self,
        event_id: str,
        *,
        binding_id: str,
        turn_ref: str,
        outcome: str,
        text: str,
        work_ref: str,
        delivery_reporting_version: int = 0,
    ) -> None:
        """Atomically retain Host custody, immediate text, and deferred work."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_json FROM inbox WHERE event_id=? AND state='leased'",
                (event_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ValueError("inbox event is not leased")
            event = json.loads(row["payload_json"])
            event["_reporting"] = {
                "version": delivery_reporting_version,
                "account_id": event.get("account_id", ""),
                "origin": {"kind": "automatic", "task_id": turn_ref,
                           "source_event_id": event.get("source_event_id", "")},
            }
            if text and outcome in {"message", "deferred"}:
                self._enqueue_outbox(
                    conn,
                    f"presence:{event_id}:turn",
                    _reply_payload(event, text),
                    now,
                )
            if outcome == "deferred":
                if not work_ref:
                    conn.rollback()
                    raise ValueError("deferred submission requires work_ref")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO presence_work(
                        event_id, binding_id, work_ref, event_json, state,
                        attempts, available_at, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (event_id, binding_id, work_ref, _json(event), now, now),
                )
            updated = conn.execute(
                """
                UPDATE inbox
                SET state='submitted', lease_until=0, submitted_at=?, submission_ref=?, last_error=''
                WHERE event_id=? AND state='leased'
                """,
                (now, str(turn_ref or work_ref)[:500], event_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise ValueError("inbox event lease changed")
            conn.commit()

    def enqueue_outbox(self, delivery_id: str, payload: Dict[str, Any]) -> bool:
        now = time.time()
        with self._connect() as conn:
            cursor = self._enqueue_outbox(conn, delivery_id, payload, now)
            return cursor.rowcount == 1

    @staticmethod
    def _enqueue_outbox(
        conn: sqlite3.Connection,
        delivery_id: str,
        payload: Dict[str, Any],
        now: float,
    ) -> sqlite3.Cursor:
        return conn.execute(
            """
            INSERT OR IGNORE INTO outbox(
                delivery_id, payload_json, state, attempts, available_at, created_at
            ) VALUES (?, ?, 'pending', 0, ?, ?)
            """,
            (delivery_id, _json(payload), now, now),
        )

    def claim_work(self, *, lease_sec: float = 90.0) -> Optional[WorkLease]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT event_id, binding_id, work_ref, role, event_json, attempts
                FROM presence_work
                WHERE (state='pending' AND available_at<=?)
                   OR (state='leased' AND lease_until<=?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            updated = conn.execute(
                """
                UPDATE presence_work
                SET state='leased', lease_until=?, attempts=attempts+1, last_error=''
                WHERE work_ref=? AND (
                    (state='pending' AND available_at<=?)
                    OR (state='leased' AND lease_until<=?)
                )
                """,
                (now + max(1.0, lease_sec), row["work_ref"], now, now),
            )
            conn.commit()
            if updated.rowcount != 1:
                return None
            return WorkLease(
                event_id=str(row["event_id"]),
                binding_id=str(row["binding_id"]),
                work_ref=str(row["work_ref"]),
                event=json.loads(row["event_json"]),
                attempts=int(row["attempts"]) + 1,
                role=str(row["role"]),
            )

    def release_work(
        self, work_ref: str, *, reason: str, retry_after_sec: float, child_ref: str = ""
    ) -> None:
        """Keep observing a reference; retain a newly named child in the same commit."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT event_id, binding_id, event_json FROM presence_work "
                "WHERE work_ref=? AND state='leased'",
                (work_ref,),
            ).fetchone()
            if row is not None and child_ref and child_ref != work_ref:
                event = json.loads(row["event_json"])
                mode = int((event.get("_reporting") or {}).get("version") == 1)
                self._add_work(conn, row["event_id"], row["binding_id"], child_ref, "child",
                               event, mode, now)
            conn.execute(
                """
                UPDATE presence_work
                SET state='pending', available_at=?, lease_until=0, last_error=?
                WHERE work_ref=? AND state='leased'
                """,
                (now + max(0.0, retry_after_sec), str(reason)[:500], work_ref),
            )
            conn.commit()

    def record_work_outputs(self, work_ref: str, outputs: Sequence[Dict[str, Any]]) -> None:
        """Checkpoint selected outputs before releasing or settling their observation."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT event_id, event_json FROM presence_work WHERE work_ref=?",
                               (work_ref,)).fetchone()
            if row is None:
                raise ValueError("presence work reference is missing")
            event = json.loads(row["event_json"])
            for output in outputs:
                identity = f"presence:{row['event_id']}:output:{work_ref}:{output['output_ref']}"
                self._enqueue_outbox(conn, identity, _reply_payload(event, str(output["text"])), time.time())
            conn.commit()

    def complete_work(self, work_ref: str, *, status: str, text: str) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("work status is not terminal")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT event_id, role, event_json FROM presence_work "
                "WHERE work_ref=? AND state='leased'",
                (work_ref,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ValueError("presence work is not leased")
            updated = conn.execute(
                """
                UPDATE presence_work
                SET state=?, lease_until=0, completed_at=?, last_error=''
                WHERE work_ref=? AND state='leased'
                """,
                (status, now, work_ref),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise ValueError("presence work lease changed")
            if text:
                event_id, role = str(row["event_id"]), str(row["role"])
                # A deferred handoff keeps its original identity; each retained
                # reference speaks at most once under its own.
                delivery_id = (f"presence:{event_id}:work" if role == "deferred"
                               else f"presence:{event_id}:{role}:{work_ref}")
                event = json.loads(row["event_json"])
                self._enqueue_outbox(conn, delivery_id, _reply_payload(event, text), now)
            conn.commit()

    def claim_outbox(self, *, lease_sec: float = 90.0) -> Optional[OutboxLease]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT candidate.delivery_id, candidate.payload_json, candidate.attempts
                FROM outbox AS candidate
                WHERE ((candidate.state='pending' AND candidate.available_at<=?)
                   OR (candidate.state='leased' AND candidate.lease_until<=?))
                AND NOT EXISTS (
                    SELECT 1 FROM outbox AS earlier
                    WHERE earlier.rowid < candidate.rowid
                      AND earlier.state IN ('pending','leased')
                      AND json_extract(earlier.payload_json, '$.chat_id') = json_extract(candidate.payload_json, '$.chat_id')
                      AND COALESCE(json_extract(earlier.payload_json, '$.topic_id'), '') = COALESCE(json_extract(candidate.payload_json, '$.topic_id'), '')
                )
                ORDER BY candidate.created_at ASC, candidate.rowid ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            updated = conn.execute(
                """
                UPDATE outbox
                SET state='leased', lease_until=?, attempts=attempts+1, last_error=''
                WHERE delivery_id=? AND (
                    (state='pending' AND available_at<=?)
                    OR (state='leased' AND lease_until<=?)
                )
                """,
                (now + max(1.0, lease_sec), row["delivery_id"], now, now),
            )
            conn.commit()
            if updated.rowcount != 1:
                return None
            return OutboxLease(
                delivery_id=str(row["delivery_id"]),
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]) + 1,
            )

    def release_outbox(
        self, delivery_id: str, *, reason: str, retry_after_sec: float
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state='pending', available_at=?, lease_until=0, last_error=?
                WHERE delivery_id=? AND state='leased'
                """,
                (
                    time.time() + max(0.0, retry_after_sec),
                    str(reason)[:500],
                    delivery_id,
                ),
            )

    def mark_outbox_failed(self, delivery_id: str, *, reason: str, report=None,
                           state: str = "failed") -> None:
        if state not in {"failed", "uncertain"}:
            raise ValueError("terminal outbox state must be failed or uncertain")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_json FROM outbox WHERE delivery_id=? AND state='leased'",
                               (delivery_id,)).fetchone() if state == "uncertain" else None
            payload = json.loads(row[0]) if row is not None else None
            if payload is not None:
                payload["_terminal_outcome"] = "uncertain"
            conn.execute(
                """
                UPDATE outbox
                SET state='failed', lease_until=0, failed_at=?, last_error=?,
                    payload_json=COALESCE(?,payload_json)
                WHERE delivery_id=? AND state='leased'
                """,
                (time.time(), str(reason)[:500], _json(payload) if payload is not None else None,
                 delivery_id),
            )
            self._add_reports(conn, delivery_id, [report] if report else [])
            conn.commit()

    def checkpoint_outbox(self, delivery_id: str, payload: Dict[str, Any], *, report=None) -> None:
        """Retain confirmed text chunks before attempting the next provider call."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE outbox SET payload_json=? WHERE delivery_id=? AND state='leased'",
                (_json(payload), delivery_id),
            )
            self._add_reports(conn, delivery_id, [report] if report else [])
            conn.commit()

    def mark_delivered(
        self, delivery_id: str, *, provider_receipt: Dict[str, Any], report=None
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE outbox
                SET state='delivered', lease_until=0, delivered_at=?,
                    provider_receipt_json=?, last_error=''
                WHERE delivery_id=? AND state='leased'
                """,
                (time.time(), _json(provider_receipt), delivery_id),
            )
            self._add_reports(conn, delivery_id, [report] if report else [])
            conn.commit()

    def _add_reports(self, conn, delivery_id, reports):
        if not reports:
            return
        row = conn.execute("SELECT reports_json FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone()
        entries = json.loads(row[0])
        keys = {(x["payload"]["part_id"], x["payload"]["state"]) for x in entries}
        for report in reports:
            key = (report["part_id"], report["state"])
            if key not in keys:
                entries.append({"payload": report, "acked": False, "attempts": 0, "available_at": time.time(), "error": ""})
                keys.add(key)
        self._write_reports(conn, delivery_id, entries)

    @staticmethod
    def _write_reports(conn, delivery_id, entries):
        due = min((x["available_at"] for x in entries if not x["acked"]), default=0)
        conn.execute("UPDATE outbox SET reports_json=?, report_due_at=? WHERE delivery_id=?", (_json(entries), due, delivery_id))

    def next_delivery_report(self):
        with self._connect() as conn:
            row = conn.execute("SELECT delivery_id,reports_json FROM outbox WHERE report_due_at>0 AND report_due_at<=? ORDER BY report_due_at LIMIT 1", (time.time(),)).fetchone()
        if row:
            for index, entry in enumerate(json.loads(row["reports_json"])):
                if not entry["acked"] and entry["available_at"] <= time.time():
                    return row["delivery_id"], index, entry["payload"]
        return None

    def finish_delivery_report(self, delivery_id, index, *, error=""):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entries = json.loads(conn.execute("SELECT reports_json FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone()[0])
            entry = entries[index]
            entry["attempts"] += 1
            entry.update(acked=not error, error=error, available_at=time.time() + min(300, 2 ** min(entry["attempts"], 8)))
            self._write_reports(conn, delivery_id, entries)
            conn.commit()

    def outbox_payload(self, delivery_id):
        with self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def delivery_receipt(self, delivery_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, attempts, delivered_at, provider_receipt_json, last_error, payload_json, reports_json "
                "FROM outbox WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            return {"state": "not_found"}
        payload = json.loads(row["payload_json"])
        return {
            "state": "uncertain" if payload.get("_terminal_outcome") == "uncertain" else str(row["state"]),
            "attempts": int(row["attempts"]),
            "delivered_at": _timestamp(row["delivered_at"]),
            "provider_receipt": json.loads(row["provider_receipt_json"] or "{}"),
            "error": str(row["last_error"]),
            "delivery_reporting": payload.get("_reporting", {"version": 0, "status": "legacy_unconfirmed"}),
            "history_reports": json.loads(row["reports_json"]),
        }

    def status_snapshot(self) -> Dict[str, Any]:
        with self._connect() as conn:
            inbox = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS count FROM inbox GROUP BY state"
                )
            }
            outbox = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS count FROM outbox GROUP BY state"
                )
            }
            work = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS count FROM presence_work GROUP BY state"
                )
            }
            last_event = conn.execute(
                "SELECT MAX(received_at) AS ts FROM inbox"
            ).fetchone()["ts"]
            last_delivery = conn.execute(
                "SELECT MAX(delivered_at) AS ts FROM outbox"
            ).fetchone()["ts"]
            return {
                "telegram_offset": self._metadata_int(conn, "telegram_offset"),
                "inbox_waiting": inbox.get("pending", 0),
                "inbox_leased": inbox.get("leased", 0),
                "inbox_submitted": inbox.get("submitted", 0),
                # Retained events whose original attempt or child is still observed.
                "inbox_retained": conn.execute(
                    "SELECT COUNT(DISTINCT event_id) FROM presence_work "
                    "WHERE role IN ('turn','child') AND state IN ('pending','leased')"
                ).fetchone()[0],
                "inbox_failed": inbox.get("failed", 0),
                "outbox_waiting": outbox.get("pending", 0),
                "outbox_leased": outbox.get("leased", 0),
                "outbox_delivered": outbox.get("delivered", 0),
                "outbox_failed": outbox.get("failed", 0) - conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE state='failed' AND json_extract(payload_json,'$._terminal_outcome')='uncertain'"
                ).fetchone()[0],
                "outbox_uncertain": conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE state='failed' AND json_extract(payload_json,'$._terminal_outcome')='uncertain'"
                ).fetchone()[0],
                "work_waiting": work.get("pending", 0),
                "work_leased": work.get("leased", 0),
                "work_terminal": sum(
                    work.get(state, 0) for state in ("completed", "failed", "cancelled")
                ),
                "delivery_reports_pending": conn.execute("SELECT COUNT(*) FROM outbox WHERE report_due_at>0").fetchone()[0],
                "last_event_at": _timestamp(last_event),
                "last_delivery_at": _timestamp(last_delivery),
            }

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox(
                    event_id TEXT PRIMARY KEY,
                    update_id INTEGER NOT NULL UNIQUE,
                    conversation_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','leased','submitted','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    received_at REAL NOT NULL,
                    submitted_at REAL,
                    failed_at REAL,
                    submission_ref TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS inbox_ready_idx
                    ON inbox(state, available_at, lease_until, update_id);
                """
                + _PRESENCE_WORK_TABLE.format(name="presence_work")
                + """;
                CREATE INDEX IF NOT EXISTS presence_work_ready_idx
                    ON presence_work(state, available_at, lease_until, created_at);
                CREATE TABLE IF NOT EXISTS outbox(
                    delivery_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','leased','delivered','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    failed_at REAL,
                    provider_receipt_json TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS outbox_ready_idx
                    ON outbox(state, available_at, lease_until, created_at);
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
            if "reports_json" not in columns:
                conn.execute("ALTER TABLE outbox ADD COLUMN reports_json TEXT NOT NULL DEFAULT '[]'")
                conn.execute("ALTER TABLE outbox ADD COLUMN report_due_at REAL NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS outbox_reports_due ON outbox(report_due_at)")
            if "host_receipt_json" not in {r[1] for r in conn.execute("PRAGMA table_info(inbox)")}:
                conn.execute("ALTER TABLE inbox ADD COLUMN host_receipt_json TEXT NOT NULL DEFAULT ''")
            if "role" not in {r[1] for r in conn.execute("PRAGMA table_info(presence_work)")}:
                self._key_work_by_reference(conn)
            conn.commit()
            self._import_legacy_offset(conn)

    @staticmethod
    def _key_work_by_reference(conn: sqlite3.Connection) -> None:
        """Rebuild the 0.4 work table, keyed by event id, under its Host reference.

        SQLite cannot change a primary key in place. Every row moves unchanged as a
        ``deferred`` handoff, so its leases, attempts and delivery identity survive.
        """
        conn.execute(_PRESENCE_WORK_TABLE.format(name="presence_work_by_ref"))
        conn.execute(
            """
            INSERT INTO presence_work_by_ref(
                work_ref, event_id, binding_id, role, event_json, state, attempts,
                available_at, lease_until, created_at, completed_at, last_error
            )
            SELECT work_ref, event_id, binding_id, 'deferred', event_json, state, attempts,
                   available_at, lease_until, created_at, completed_at, last_error
            FROM presence_work
            """
        )
        conn.execute("DROP TABLE presence_work")
        conn.execute("ALTER TABLE presence_work_by_ref RENAME TO presence_work")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS presence_work_ready_idx "
            "ON presence_work(state, available_at, lease_until, created_at)"
        )

    def _import_legacy_offset(self, conn: sqlite3.Connection) -> None:
        if self._metadata_int(conn, "telegram_offset") > 0:
            return
        legacy = self.path.parent / "offsets.json"
        if not legacy.is_file():
            return
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            offset = int(payload.get("last_offset") or payload.get("offset") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if offset > 0:
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('telegram_offset', ?)",
                (str(offset),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _metadata_int(conn: sqlite3.Connection, key: str) -> int:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            return 0
        try:
            return max(0, int(row["value"]))
        except (TypeError, ValueError):
            return 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value: Any) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))


def _reply_payload(event: Dict[str, Any], text: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "kind": "message",
        "chat_id": str(event.get("conversation_id") or ""),
        "text": str(text),
        "markdown": True,
        "_rendered_chunks": prepare_text(str(text)),
        "_reporting": event.get("_reporting", {"version": 0}),
    }
    thread_id = str(event.get("thread_id") or "").strip()
    if thread_id:
        payload["topic_id"] = thread_id
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    reply_id = message.get("message_id")
    if reply_id not in (None, ""):
        payload["reply_to_message_id"] = reply_id
    return payload
