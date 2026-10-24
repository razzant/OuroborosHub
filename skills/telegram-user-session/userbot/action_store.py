"""Private durable custody for single-message edit/delete/react intentions.

This is NOT a model-facing tool, authorization grant, Telegram adapter, or
payload vault. The caller persists an immutable opaque payload_ref outside the
DB and checks its SHA-256 against payload_sha256 BEFORE marking attempted.
Never put private message text, emoji, credentials, or exception strings in
this DB. Only an exclusive companion may open recover_attempted=True, after
its predecessor has stopped: an attempted action may already have happened,
so recovery makes it UNKNOWN and never retries it. SQLite/WAL is logical
custody, not secure erasure of old pages or backups.

Preflight allowlist/topic/target/auth failures can reject_before_attempt;
transient failures can release_before_attempt. Commit mark_attempted before
calling Actions.edit/delete/react. An ambiguous outcome calls mark_unknown;
only a definitive result may call settle. Distinct new intentions are not
safe substitutes for reconciling an unknown one.
"""

from __future__ import annotations

import re
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_REF = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_BINDING = re.compile(r"[a-fA-F0-9]{32}\Z")
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_OPERATIONS = frozenset({"edit", "delete", "react"})


@dataclass(frozen=True)
class ActionIntent:
    account_id: str
    intent_id: str
    operation: str
    peer_id: int
    binding_id: str
    topic_id: int | None
    message_id: int
    payload_ref: str | None
    payload_sha256: str | None
    status: str
    lease_token: str | None
    lease_until: float | None
    error_code: str | None
    next_attempt_at: float


@dataclass(frozen=True)
class HistoryQuery:
    """Bounded read request; never placed in the mutation queue."""

    peer_id: int
    topic_id: int | None
    limit: int
    min_id: int


def _peer(value: int) -> None:
    if type(value) is not int or value == 0 or not -(2**63) < value < 2**63:
        raise ValueError("peer_id must be a nonzero signed int64")


def _message(value: int, name: str) -> None:
    if type(value) is not int or not 0 < value < 2**31:
        raise ValueError(f"{name} must be a positive Telegram int32")


def _topic(value: int | None) -> None:
    if value is not None:
        _message(value, "topic_id")


def _key(value: str, name: str) -> None:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise ValueError(f"{name} must be an opaque nonsecret identifier")


def _account(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(ch) < 33 for ch in value):
        raise ValueError("account_id must be a bounded nonempty identifier")


def validate_history_query(peer_id: int, *, topic_id: int | None,
                           limit: int = 50, min_id: int = 0) -> HistoryQuery:
    """Validate a read-only bounded Actions.history query; no intent is created.

    The parent still checks owner authority, peer allowlist and refreshed topic
    catalog with Actions.history. This function cannot authorize a read.
    """
    _peer(peer_id)
    _topic(topic_id)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be in [1, 100]")
    if type(min_id) is not int or not 0 <= min_id < 2**31:
        raise ValueError("min_id must be a nonnegative Telegram int32")
    return HistoryQuery(peer_id, topic_id, limit, min_id)


class ActionStore:
    """SQLite WAL queue; only one companion may claim at a time."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 recover_attempted: bool = False):
        if type(recover_attempted) is not bool:
            raise ValueError("recover_attempted must be bool")
        requested = Path(path)
        if not requested.is_absolute():
            raise ValueError("database path must be absolute private runtime state")
        self.path = requested.resolve()
        if self.path.is_relative_to(Path(__file__).resolve().parent.parent):
            raise ValueError("database must not live inside the skill/repository")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA busy_timeout=10000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("""CREATE TABLE IF NOT EXISTS actions (
                    account_id TEXT NOT NULL, intent_id TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK(operation IN ('edit','delete','react')),
                    peer_id INTEGER NOT NULL, binding_id TEXT NOT NULL,
                    topic_id INTEGER, message_id INTEGER NOT NULL,
                    payload_ref TEXT, payload_sha256 TEXT,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','leased','attempted','sent','failed','unknown')),
                    lease_token TEXT, lease_until REAL, error_code TEXT,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, intent_id)
                )""")
                fields = {row["name"] for row in self.db.execute("PRAGMA table_info(actions)")}
                if "next_attempt_at" not in fields:
                    self.db.execute("ALTER TABLE actions ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")
                self.db.execute("""CREATE INDEX IF NOT EXISTS actions_claim
                    ON actions(account_id, status, lease_until)""")
                if recover_attempted:
                    self.db.execute("""UPDATE actions SET status='unknown', lease_until=NULL
                        WHERE status='attempted'""")
        except BaseException:
            self.db.close()
            raise

    def __enter__(self) -> ActionStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _intent(row: sqlite3.Row) -> ActionIntent:
        return ActionIntent(**dict(row))

    def enqueue(self, account_id: str, intent_id: str, *, operation: str,
                peer_id: int, binding_id: str, topic_id: int | None, message_id: int,
                payload_ref: str | None = None,
                payload_sha256: str | None = None) -> ActionIntent:
        """Freeze full operation identity. Reuse is permitted only with exact inputs.

        Edit/react payload is an immutable private external file or reference;
        the DB stores only its opaque name and its digest. Delete has no payload.
        """
        _account(account_id)
        _key(intent_id, "intent_id")
        if operation not in _OPERATIONS:
            raise ValueError("operation must be edit, delete or react")
        _peer(peer_id)
        if not isinstance(binding_id, str) or not _BINDING.fullmatch(binding_id):
            raise ValueError("an exact owner Presence binding_id is required")
        _topic(topic_id)
        _message(message_id, "message_id")
        if operation == "delete":
            if payload_ref is not None or payload_sha256 is not None:
                raise ValueError("delete must not carry a payload")
        elif (payload_ref is None or payload_sha256 is None or
              not isinstance(payload_sha256, str) or not _HASH.fullmatch(payload_sha256)):
            raise ValueError("edit/react require opaque payload_ref and lowercase SHA-256")
        if payload_ref is not None:
            _key(payload_ref, "payload_ref")
        identity = (operation, peer_id, binding_id, topic_id, message_id, payload_ref, payload_sha256)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM actions WHERE account_id=? AND intent_id=?",
                                  (account_id, intent_id)).fetchone()
            if row is not None:
                if tuple(row[key] for key in ("operation", "peer_id", "binding_id", "topic_id", "message_id",
                                               "payload_ref", "payload_sha256")) != identity:
                    raise ValueError("intent_id already bound to different action bytes/scope")
                return self._intent(row)
            self.db.execute("""INSERT INTO actions
                (account_id,intent_id,operation,peer_id,binding_id,topic_id,message_id,
                 payload_ref,payload_sha256,status)
                VALUES (?,?,?,?,?,?,?,?,?,'queued')""",
                (account_id, intent_id, *identity))
            return self.get(account_id, intent_id)

    def get(self, account_id: str, intent_id: str) -> ActionIntent | None:
        _account(account_id)
        _key(intent_id, "intent_id")
        row = self.db.execute("SELECT * FROM actions WHERE account_id=? AND intent_id=?",
                              (account_id, intent_id)).fetchone()
        return self._intent(row) if row is not None else None

    def claim(self, account_id: str, *, lease_seconds: float = 60) -> ActionIntent | None:
        """FIFO among unattempted actions only; reclaimed leases get new tokens."""
        _account(account_id)
        if (type(lease_seconds) not in (int, float) or
                not 0 < lease_seconds <= 86400):
            raise ValueError("lease_seconds must be in (0, 86400]")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            self.db.execute("""UPDATE actions SET status='unknown', lease_until=NULL
                WHERE account_id=? AND status='attempted' AND lease_until<=?""",
                (account_id, now))
            row = self.db.execute("""SELECT intent_id FROM actions WHERE account_id=? AND
                next_attempt_at<=? AND (status='queued' OR (status='leased' AND lease_until<=?))
                ORDER BY rowid LIMIT 1""", (account_id, now, now)).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            self.db.execute("""UPDATE actions SET status='leased',lease_token=?,lease_until=?
                WHERE account_id=? AND intent_id=?""",
                (token, now + lease_seconds, account_id, row["intent_id"]))
            return self.get(account_id, row["intent_id"])

    def _before_attempt(self, account_id: str, intent_id: str, token: str,
                        *, status: str, error_code: str | None = None,
                        delay_seconds: float = 0) -> ActionIntent:
        _account(account_id)
        _key(intent_id, "intent_id")
        _key(token, "lease_token")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute("""UPDATE actions SET status=?, error_code=?,
                next_attempt_at=?,
                lease_until=CASE WHEN ?='attempted' THEN lease_until ELSE NULL END,
                lease_token=CASE WHEN ?='attempted' THEN lease_token ELSE NULL END
                WHERE account_id=? AND intent_id=? AND status='leased'
                AND lease_token=? AND lease_until>?""",
                (status, error_code, self.clock() + delay_seconds if status == 'queued' else 0,
                 status, status, account_id, intent_id, token, self.clock()))
            if changed.rowcount != 1:
                raise ValueError("lease lost or already attempted; do not invoke Telegram")
            return self.get(account_id, intent_id)

    def mark_attempted(self, account_id: str, intent_id: str, lease_token: str) -> ActionIntent:
        """Durably commit this boundary just before a Telegram mutation call."""
        return self._before_attempt(account_id, intent_id, lease_token, status="attempted")

    def reject_before_attempt(self, account_id: str, intent_id: str,
                              lease_token: str, error_code: str) -> ActionIntent:
        """Permanently invalid preflight fails this action; later FIFO proceeds."""
        if not isinstance(error_code, str) or not _CODE.fullmatch(error_code):
            raise ValueError("safe machine error_code required")
        return self._before_attempt(account_id, intent_id, lease_token,
                                    status="failed", error_code=error_code)

    def release_before_attempt(self, account_id: str, intent_id: str,
                               lease_token: str, *, delay_seconds: float = 0) -> ActionIntent:
        """Transient preflight failure: defer without consuming mutation authority."""
        if (type(delay_seconds) not in (int, float) or not math.isfinite(delay_seconds)
                or not 0 <= delay_seconds <= 30 * 86400):
            raise ValueError("pre-attempt backoff must be finite and <=30 days")
        return self._before_attempt(account_id, intent_id, lease_token,
                                    status="queued", delay_seconds=delay_seconds)

    def mark_unknown(self, account_id: str, intent_id: str,
                     lease_token: str) -> ActionIntent:
        """Mutation invoked with uncertain result; never auto-retry it."""
        _account(account_id)
        _key(intent_id, "intent_id")
        _key(lease_token, "lease_token")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute("""UPDATE actions SET status='unknown',lease_until=NULL
                WHERE account_id=? AND intent_id=? AND lease_token=?
                AND status='attempted'""", (account_id, intent_id, lease_token))
            if changed.rowcount != 1:
                raise ValueError("action not attempted or token changed")
            return self.get(account_id, intent_id)

    def settle(self, account_id: str, intent_id: str, lease_token: str, *,
               success: bool, error_code: str | None = None) -> ActionIntent:
        """Record definitive success or refusal (including late resolution of unknown)."""
        _account(account_id)
        _key(intent_id, "intent_id")
        _key(lease_token, "lease_token")
        if type(success) is not bool or (success and error_code is not None) or (not success and
                (not isinstance(error_code, str) or not _CODE.fullmatch(error_code))):
            raise ValueError("success bool or failure with safe error_code required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute("""UPDATE actions SET status=?,error_code=?,lease_until=NULL
                WHERE account_id=? AND intent_id=? AND lease_token=?
                AND status IN ('attempted','unknown')""",
                ("sent" if success else "failed", error_code,
                 account_id, intent_id, lease_token))
            if changed.rowcount != 1:
                raise ValueError("action not attempted or token changed")
            return self.get(account_id, intent_id)

    def list_unknown(self, account_id: str) -> list[ActionIntent]:
        _account(account_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""UPDATE actions SET status='unknown',lease_until=NULL
                WHERE account_id=? AND status='attempted' AND lease_until<=?""",
                (account_id, self.clock()))
            rows = self.db.execute("""SELECT * FROM actions WHERE account_id=?
                AND status='unknown' ORDER BY rowid""", (account_id,)).fetchall()
            return [self._intent(row) for row in rows]
