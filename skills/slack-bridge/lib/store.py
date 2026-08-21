from __future__ import annotations

import json
import pathlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .events import ParsedEnvelope


@dataclass(frozen=True)
class InboxItem:
    row_id: int
    lease_token: str
    envelope_id: str
    event_id: str
    ordering_key: str
    team_id: str
    enterprise_id: str
    event_type: str
    subtype: str
    actor_user_id: str
    actor_team_id: str
    channel_id: str
    channel_type: str
    message_ts: str
    thread_ts: str
    event_ts: str
    client_msg_id: str
    text: str
    files: tuple[dict[str, Any], ...]
    staged_files: tuple[dict[str, Any], ...]
    host_reference: str
    attempts: int

    @property
    def reply_thread_ts(self) -> str:
        return self.thread_ts or self.message_ts

    @property
    def provider_event_key(self) -> str:
        """Stable idempotency key for a host adapter's submit call."""

        return self.event_id or self.envelope_id


@dataclass(frozen=True)
class OutboxItem:
    row_id: int
    lease_token: str
    request_id: str
    chunk_index: int
    chunk_count: int
    target: str
    thread_ts: str
    text: str
    ordering_key: str
    attempts: int


class BridgeStore:
    """Small durable queue shared by the extension child and companion.

    Every method opens its own SQLite connection, so short-lived plugin children
    and the long-lived companion can use the same database safely.
    """

    def __init__(self, state_dir: pathlib.Path | str):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "slack_bridge.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    envelope_id TEXT NOT NULL,
                    event_id TEXT NOT NULL DEFAULT '',
                    dedupe_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    ignored_reason TEXT NOT NULL DEFAULT '',
                    ordering_key TEXT NOT NULL DEFAULT '',
                    team_id TEXT NOT NULL DEFAULT '',
                    enterprise_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT '',
                    subtype TEXT NOT NULL DEFAULT '',
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    actor_team_id TEXT NOT NULL DEFAULT '',
                    channel_id TEXT NOT NULL DEFAULT '',
                    channel_type TEXT NOT NULL DEFAULT '',
                    message_ts TEXT NOT NULL DEFAULT '',
                    thread_ts TEXT NOT NULL DEFAULT '',
                    event_ts TEXT NOT NULL DEFAULT '',
                    client_msg_id TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    files_json TEXT NOT NULL DEFAULT '[]',
                    staged_files_json TEXT NOT NULL DEFAULT '[]',
                    raw_json TEXT NOT NULL,
                    host_reference TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inbox_work
                    ON inbox(state, available_at, id);
                CREATE INDEX IF NOT EXISTS inbox_thread
                    ON inbox(ordering_key, id, state);

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    thread_ts TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    ordering_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    provider_message_ts TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(request_id, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS outbox_work
                    ON outbox(state, available_at, id);
                CREATE INDEX IF NOT EXISTS outbox_ordering
                    ON outbox(ordering_key, id, state);

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def ingest_envelope(
        self,
        raw_payload: Mapping[str, Any],
        parsed: ParsedEnvelope,
    ) -> tuple[int, bool]:
        """Commit an envelope and its dedupe identity before Socket ACK."""

        now = time.time()
        event = parsed.event
        envelope_id = parsed.envelope_id or f"missing:{uuid.uuid4().hex}"
        dedupe_key = (
            f"event:{parsed.event_id}" if parsed.event_id else f"envelope:{envelope_id}"
        )
        values: dict[str, Any] = {
            "envelope_id": envelope_id,
            "event_id": parsed.event_id,
            "dedupe_key": dedupe_key,
            "state": "pending" if parsed.accepted and event is not None else "ignored",
            "ignored_reason": "" if parsed.accepted else parsed.reason,
            "ordering_key": event.ordering_key if event else "",
            "team_id": event.team_id if event else "",
            "enterprise_id": event.enterprise_id if event else "",
            "event_type": event.event_type if event else "",
            "subtype": event.subtype if event else "",
            "actor_user_id": event.actor_user_id if event else "",
            "actor_team_id": event.actor_team_id if event else "",
            "channel_id": event.channel_id if event else "",
            "channel_type": event.channel_type if event else "",
            "message_ts": event.message_ts if event else "",
            "thread_ts": event.thread_ts if event else "",
            "event_ts": event.event_ts if event else "",
            "client_msg_id": event.client_msg_id if event else "",
            "text": event.text if event else "",
            "files_json": json.dumps(
                [item.as_dict() for item in (event.files if event else ())],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "raw_json": json.dumps(
                raw_payload, ensure_ascii=False, separators=(",", ":")
            ),
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                f"INSERT OR IGNORE INTO inbox ({columns}) VALUES ({placeholders})",
                values,
            )
            inserted = cursor.rowcount == 1
            if inserted:
                row_id = int(cursor.lastrowid)
            else:
                row = db.execute(
                    "SELECT id FROM inbox WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Slack envelope dedupe lookup failed")
                row_id = int(row["id"])
            db.commit()
        return row_id, inserted

    @staticmethod
    def _claimable_sql(table: str) -> str:
        return f"""
            SELECT q.id
            FROM {table} AS q
            WHERE q.available_at <= :now
              AND (q.state = 'pending' OR (q.state = 'leased' AND q.lease_until <= :now))
              AND NOT EXISTS (
                  SELECT 1 FROM {table} AS earlier
                  WHERE earlier.ordering_key = q.ordering_key
                    AND earlier.id < q.id
                    AND earlier.state IN ('pending', 'leased')
              )
            ORDER BY q.id
            LIMIT 1
        """

    def claim_inbox(self, *, lease_seconds: float = 60.0) -> InboxItem | None:
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(self._claimable_sql("inbox"), {"now": now}).fetchone()
            if row is None:
                db.commit()
                return None
            row_id = int(row["id"])
            updated = db.execute(
                """
                UPDATE inbox
                SET state='leased', lease_token=?, lease_until=?, attempts=attempts+1,
                    updated_at=?
                WHERE id=? AND (state='pending' OR (state='leased' AND lease_until<=?))
                """,
                (token, now + lease_seconds, now, row_id, now),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            claimed = db.execute("SELECT * FROM inbox WHERE id=?", (row_id,)).fetchone()
            db.commit()
        return self._inbox_item(claimed, token)

    def _inbox_item(self, row: sqlite3.Row, token: str) -> InboxItem:
        files = json.loads(row["files_json"] or "[]")
        staged = json.loads(row["staged_files_json"] or "[]")
        return InboxItem(
            row_id=int(row["id"]),
            lease_token=token,
            envelope_id=str(row["envelope_id"]),
            event_id=str(row["event_id"]),
            ordering_key=str(row["ordering_key"]),
            team_id=str(row["team_id"]),
            enterprise_id=str(row["enterprise_id"]),
            event_type=str(row["event_type"]),
            subtype=str(row["subtype"]),
            actor_user_id=str(row["actor_user_id"]),
            actor_team_id=str(row["actor_team_id"]),
            channel_id=str(row["channel_id"]),
            channel_type=str(row["channel_type"]),
            message_ts=str(row["message_ts"]),
            thread_ts=str(row["thread_ts"]),
            event_ts=str(row["event_ts"]),
            client_msg_id=str(row["client_msg_id"]),
            text=str(row["text"]),
            files=tuple(dict(item) for item in files if isinstance(item, dict)),
            staged_files=tuple(dict(item) for item in staged if isinstance(item, dict)),
            host_reference=str(row["host_reference"]),
            attempts=int(row["attempts"]),
        )

    def set_staged_files(
        self,
        row_id: int,
        lease_token: str,
        files: Sequence[Mapping[str, Any]],
    ) -> None:
        self._leased_update(
            "inbox",
            row_id,
            lease_token,
            "staged_files_json=?",
            (json.dumps(list(files), ensure_ascii=False, separators=(",", ":")),),
        )

    def set_host_reference(self, row_id: int, lease_token: str, reference: str) -> None:
        self._leased_update(
            "inbox", row_id, lease_token, "host_reference=?", (str(reference),)
        )

    def complete_inbox(self, row_id: int, lease_token: str) -> None:
        self._terminal_update("inbox", row_id, lease_token, "delivered", "")

    def fail_inbox(self, row_id: int, lease_token: str, error: str) -> None:
        self._terminal_update("inbox", row_id, lease_token, "failed", error)

    def retry_inbox(
        self,
        row_id: int,
        lease_token: str,
        error: str,
        *,
        delay_seconds: float,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE inbox
                SET state='pending', lease_token='', lease_until=0, available_at=?,
                    last_error=?, updated_at=?
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (
                    now + max(0.0, delay_seconds),
                    str(error)[:1000],
                    now,
                    row_id,
                    lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Slack inbox lease no longer belongs to this worker")

    def enqueue_outbox(
        self,
        *,
        request_id: str,
        target: str,
        thread_ts: str,
        chunks: Sequence[str],
    ) -> int:
        request_id = str(request_id or uuid.uuid4().hex)
        target = str(target or "").strip()
        if not target:
            raise ValueError("Slack target is required")
        clean_chunks = [str(chunk) for chunk in chunks if str(chunk)]
        if not clean_chunks:
            raise ValueError("Slack message text is required")
        now = time.time()
        ordering_key = f"{target}:{str(thread_ts or '')}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = int(
                db.execute(
                    "SELECT COUNT(*) AS count FROM outbox WHERE request_id=?",
                    (request_id,),
                ).fetchone()["count"]
            )
            if existing:
                db.commit()
                return existing
            for index, chunk in enumerate(clean_chunks):
                db.execute(
                    """
                    INSERT OR IGNORE INTO outbox (
                        request_id, chunk_index, chunk_count, target, thread_ts,
                        text, ordering_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        index,
                        len(clean_chunks),
                        target,
                        str(thread_ts or ""),
                        chunk,
                        ordering_key,
                        now,
                        now,
                    ),
                )
            db.commit()
        return len(clean_chunks)

    def claim_outbox(self, *, lease_seconds: float = 60.0) -> OutboxItem | None:
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(self._claimable_sql("outbox"), {"now": now}).fetchone()
            if row is None:
                db.commit()
                return None
            row_id = int(row["id"])
            updated = db.execute(
                """
                UPDATE outbox
                SET state='leased', lease_token=?, lease_until=?, attempts=attempts+1,
                    updated_at=?
                WHERE id=? AND (state='pending' OR (state='leased' AND lease_until<=?))
                """,
                (token, now + lease_seconds, now, row_id, now),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            claimed = db.execute(
                "SELECT * FROM outbox WHERE id=?", (row_id,)
            ).fetchone()
            db.commit()
        return OutboxItem(
            row_id=int(claimed["id"]),
            lease_token=token,
            request_id=str(claimed["request_id"]),
            chunk_index=int(claimed["chunk_index"]),
            chunk_count=int(claimed["chunk_count"]),
            target=str(claimed["target"]),
            thread_ts=str(claimed["thread_ts"]),
            text=str(claimed["text"]),
            ordering_key=str(claimed["ordering_key"]),
            attempts=int(claimed["attempts"]),
        )

    def complete_outbox(
        self,
        row_id: int,
        lease_token: str,
        *,
        provider_message_ts: str = "",
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE outbox
                SET state='delivered', lease_token='', lease_until=0, last_error='',
                    provider_message_ts=?, updated_at=?
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (str(provider_message_ts), now, row_id, lease_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Slack outbox lease no longer belongs to this worker"
                )

    def fail_outbox(self, row_id: int, lease_token: str, error: str) -> None:
        self._terminal_update("outbox", row_id, lease_token, "failed", error)

    def retry_outbox(
        self,
        row_id: int,
        lease_token: str,
        error: str,
        *,
        delay_seconds: float,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE outbox
                SET state='pending', lease_token='', lease_until=0, available_at=?,
                    last_error=?, updated_at=?
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (
                    now + max(0.0, delay_seconds),
                    str(error)[:1000],
                    now,
                    row_id,
                    lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Slack outbox lease no longer belongs to this worker"
                )

    def _leased_update(
        self,
        table: str,
        row_id: int,
        lease_token: str,
        assignment: str,
        values: Iterable[Any],
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                f"UPDATE {table} SET {assignment}, updated_at=? "
                "WHERE id=? AND state='leased' AND lease_token=?",
                (*values, now, row_id, lease_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"Slack {table} lease no longer belongs to this worker"
                )

    def _terminal_update(
        self,
        table: str,
        row_id: int,
        lease_token: str,
        state: str,
        error: str,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                f"""
                UPDATE {table}
                SET state=?, lease_token='', lease_until=0, last_error=?, updated_at=?
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (state, str(error)[:1000], now, row_id, lease_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"Slack {table} lease no longer belongs to this worker"
                )

    def set_runtime(self, **values: Any) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                db.execute(
                    """
                    INSERT INTO runtime_state(key, value_json, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (str(key), json.dumps(value, ensure_ascii=False), now),
                )
            db.commit()

    def status(self) -> dict[str, Any]:
        with self._connect() as db:
            runtime_rows = db.execute(
                "SELECT key, value_json, updated_at FROM runtime_state"
            ).fetchall()
            inbox = {
                str(row["state"]): int(row["count"])
                for row in db.execute(
                    "SELECT state, COUNT(*) AS count FROM inbox GROUP BY state"
                ).fetchall()
            }
            outbox = {
                str(row["state"]): int(row["count"])
                for row in db.execute(
                    "SELECT state, COUNT(*) AS count FROM outbox GROUP BY state"
                ).fetchall()
            }
            last_error = db.execute(
                """
                SELECT last_error FROM (
                    SELECT last_error, updated_at FROM inbox WHERE last_error <> ''
                    UNION ALL
                    SELECT last_error, updated_at FROM outbox WHERE last_error <> ''
                ) ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        runtime: dict[str, Any] = {}
        runtime_updated_at = 0.0
        for row in runtime_rows:
            try:
                runtime[str(row["key"])] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                runtime[str(row["key"])] = None
            runtime_updated_at = max(runtime_updated_at, float(row["updated_at"]))
        return {
            "socket_state": "stopped",
            "host_adapter_state": "missing_binding_id",
            "workspace_name": "",
            "workspace_id": "",
            "bot_user_id": "",
            **runtime,
            "runtime_updated_at": runtime_updated_at,
            "inbox_pending": inbox.get("pending", 0),
            "inbox_leased": inbox.get("leased", 0),
            "inbox_delivered": inbox.get("delivered", 0),
            "inbox_failed": inbox.get("failed", 0),
            "inbox_ignored": inbox.get("ignored", 0),
            "outbox_pending": outbox.get("pending", 0),
            "outbox_leased": outbox.get("leased", 0),
            "outbox_delivered": outbox.get("delivered", 0),
            "outbox_failed": outbox.get("failed", 0),
            "last_delivery_error": str(last_error["last_error"]) if last_error else "",
        }
