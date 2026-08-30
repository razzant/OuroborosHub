"""SQLite custody for Telegram provider updates and deliveries."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from .events import TelegramEvent


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

    def mark_inbox_failed(self, event_id: str, *, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbox
                SET state='failed', lease_until=0, failed_at=?, last_error=?
                WHERE event_id=? AND state='leased'
                """,
                (time.time(), str(reason)[:500], event_id),
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
            if text:
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
                SELECT event_id, binding_id, work_ref, event_json, attempts
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
            return WorkLease(
                event_id=str(row["event_id"]),
                binding_id=str(row["binding_id"]),
                work_ref=str(row["work_ref"]),
                event=json.loads(row["event_json"]),
                attempts=int(row["attempts"]) + 1,
            )

    def release_work(
        self, event_id: str, *, reason: str, retry_after_sec: float
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE presence_work
                SET state='pending', available_at=?, lease_until=0, last_error=?
                WHERE event_id=? AND state='leased'
                """,
                (time.time() + max(0.0, retry_after_sec), str(reason)[:500], event_id),
            )

    def complete_work(self, event_id: str, *, status: str, text: str) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("work status is not terminal")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT event_json FROM presence_work WHERE event_id=? AND state='leased'",
                (event_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ValueError("presence work is not leased")
            updated = conn.execute(
                """
                UPDATE presence_work
                SET state=?, lease_until=0, completed_at=?, last_error=''
                WHERE event_id=? AND state='leased'
                """,
                (status, now, event_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise ValueError("presence work lease changed")
            if text:
                event = json.loads(row["event_json"])
                self._enqueue_outbox(
                    conn,
                    f"presence:{event_id}:work",
                    _reply_payload(event, text),
                    now,
                )
            conn.commit()

    def claim_outbox(self, *, lease_sec: float = 90.0) -> Optional[OutboxLease]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT delivery_id, payload_json, attempts
                FROM outbox
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

    def mark_outbox_failed(self, delivery_id: str, *, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state='failed', lease_until=0, failed_at=?, last_error=?
                WHERE delivery_id=? AND state='leased'
                """,
                (time.time(), str(reason)[:500], delivery_id),
            )

    def mark_delivered(
        self, delivery_id: str, *, provider_receipt: Dict[str, Any]
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state='delivered', lease_until=0, delivered_at=?,
                    provider_receipt_json=?, last_error=''
                WHERE delivery_id=? AND state='leased'
                """,
                (time.time(), _json(provider_receipt), delivery_id),
            )

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
                "inbox_failed": inbox.get("failed", 0),
                "outbox_waiting": outbox.get("pending", 0),
                "outbox_leased": outbox.get("leased", 0),
                "outbox_delivered": outbox.get("delivered", 0),
                "outbox_failed": outbox.get("failed", 0),
                "work_waiting": work.get("pending", 0),
                "work_leased": work.get("leased", 0),
                "work_terminal": sum(
                    work.get(state, 0) for state in ("completed", "failed", "cancelled")
                ),
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
                CREATE TABLE IF NOT EXISTS presence_work(
                    event_id TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL,
                    work_ref TEXT NOT NULL UNIQUE,
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
                );
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
            self._import_legacy_offset(conn)

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
    }
    thread_id = str(event.get("thread_id") or "").strip()
    if thread_id:
        payload["topic_id"] = thread_id
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    reply_id = message.get("message_id")
    if reply_id not in (None, ""):
        payload["reply_to_message_id"] = reply_id
    return payload
