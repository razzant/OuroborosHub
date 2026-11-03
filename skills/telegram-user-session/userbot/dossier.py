"""Opt-in, peer-scoped facts about Telegram users.

This module never consumes messages itself. The caller must explicitly choose a
fact to record; group history and usernames are not an identity source. A fact
is visible only within its numeric account/user/source-peer tuple. SQLite data
is local and must be protected as private user data by the caller.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


MAX_FACT_CHARS = 500
MAX_FACTS_PER_PEER = 24
MAX_FACTS_PER_USER = 64
MAX_TTL_DAYS = 365
DEFAULT_TTL_DAYS = 90
_MAX_ID = (1 << 63) - 1
_DAY_MS = 86_400_000


@dataclass(frozen=True)
class Fact:
    id: int
    account_id: int
    user_id: int
    source_peer_id: int
    source_message_id: int
    source_at: datetime
    fact: str
    expires_at: datetime


def _numeric_id(value: int, label: str, *, allow_negative: bool = False) -> int:
    if type(value) is not int or value == 0 or abs(value) > _MAX_ID or (value < 0 and not allow_negative):
        raise ValueError(f"{label} must be a nonzero {'signed ' if allow_negative else ''}int64")
    return value


def _utc_ms(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source_at must be a timezone-aware datetime")
    return int(value.timestamp() * 1000)


def _as_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"], account_id=row["account_id"], user_id=row["user_id"],
        source_peer_id=row["source_peer_id"], source_message_id=row["source_message_id"],
        source_at=datetime.fromtimestamp(row["source_at_ms"] / 1000, timezone.utc),
        fact=row["fact"],
        expires_at=datetime.fromtimestamp(row["expires_at_ms"] / 1000, timezone.utc),
    )


class DossierStore:
    """Small local SQLite store with no unscoped fact-read method.

    Limits reject extra inserts rather than silently deleting evidence. The
    caller supplies an explicit source peer/message/time for each fact. Replays
    of the same source and same text return the original row if still live.
    Deleting a user requires both the owning account and the numeric user ID.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self._lock = threading.RLock()
        self._clock = clock
        self._db = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout = 10000")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS dossier_facts (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL CHECK (account_id > 0),
                    user_id INTEGER NOT NULL CHECK (user_id > 0),
                    source_peer_id INTEGER NOT NULL CHECK (source_peer_id != 0),
                    source_message_id INTEGER NOT NULL CHECK (source_message_id > 0),
                    source_at_ms INTEGER NOT NULL,
                    fact TEXT NOT NULL CHECK (length(fact) BETWEEN 1 AND 500),
                    expires_at_ms INTEGER NOT NULL,
                    UNIQUE (account_id, user_id, source_peer_id, source_message_id, fact)
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS dossier_visible
                ON dossier_facts (account_id, user_id, source_peer_id, expires_at_ms)
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS dossier_expiry ON dossier_facts (expires_at_ms)
            """)
        except BaseException:
            self._db.close()
            raise

    def __enter__(self) -> DossierStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add_fact(
        self, account_id: int, user_id: int, *, source_peer_id: int,
        source_message_id: int, source_at: datetime, fact: str,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ) -> Fact:
        account_id = _numeric_id(account_id, "account_id")
        user_id = _numeric_id(user_id, "user_id")
        source_peer_id = _numeric_id(source_peer_id, "source_peer_id", allow_negative=True)
        source_message_id = _numeric_id(source_message_id, "source_message_id")
        if type(fact) is not str or not (1 <= len(fact.strip()) <= MAX_FACT_CHARS):
            raise ValueError(f"fact must contain 1..{MAX_FACT_CHARS} characters")
        fact = fact.strip()
        if "\x00" in fact or any(ord(char) < 32 and char not in "\n\t" for char in fact):
            raise ValueError("fact contains control characters")
        if type(ttl_days) is not int or not 1 <= ttl_days <= MAX_TTL_DAYS:
            raise ValueError(f"ttl_days must be an integer from 1 to {MAX_TTL_DAYS}")
        source_ms = _utc_ms(source_at)
        now_ms = int(self._clock() * 1000)
        if source_ms > now_ms + 300_000:
            raise ValueError("source_at is more than five minutes in the future")
        expires_ms = source_ms + ttl_days * _DAY_MS
        if expires_ms <= now_ms:
            raise ValueError("source is already expired")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "DELETE FROM dossier_facts WHERE account_id=? AND user_id=? AND expires_at_ms<=?",
                    (account_id, user_id, now_ms),
                )
                row = self._db.execute(
                    """SELECT * FROM dossier_facts WHERE account_id=? AND user_id=?
                       AND source_peer_id=? AND source_message_id=? AND fact=?""",
                    (account_id, user_id, source_peer_id, source_message_id, fact),
                ).fetchone()
                if row is None:
                    count = self._db.execute(
                        "SELECT count(*) FROM dossier_facts WHERE account_id=? AND user_id=?",
                        (account_id, user_id),
                    ).fetchone()[0]
                    peer_count = self._db.execute(
                        """SELECT count(*) FROM dossier_facts WHERE account_id=? AND user_id=?
                           AND source_peer_id=?""", (account_id, user_id, source_peer_id),
                    ).fetchone()[0]
                    if count >= MAX_FACTS_PER_USER or peer_count >= MAX_FACTS_PER_PEER:
                        raise ValueError("dossier fact limit reached; delete facts explicitly before adding more")
                    cursor = self._db.execute(
                        """INSERT INTO dossier_facts
                           (account_id, user_id, source_peer_id, source_message_id,
                            source_at_ms, fact, expires_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (account_id, user_id, source_peer_id, source_message_id,
                         source_ms, fact, expires_ms),
                    )
                    row = self._db.execute(
                        "SELECT * FROM dossier_facts WHERE id=?", (cursor.lastrowid,)
                    ).fetchone()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return _as_fact(row)

    def list_facts(self, account_id: int, user_id: int, *, peer_id: int) -> list[Fact]:
        """Only return facts originating in this peer; no global/user-only read."""
        account_id = _numeric_id(account_id, "account_id")
        user_id = _numeric_id(user_id, "user_id")
        peer_id = _numeric_id(peer_id, "peer_id", allow_negative=True)
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM dossier_facts WHERE account_id=? AND user_id=?
                   AND source_peer_id=? AND expires_at_ms>?
                   ORDER BY source_at_ms DESC, id DESC LIMIT ?""",
                (account_id, user_id, peer_id, int(self._clock() * 1000), MAX_FACTS_PER_PEER),
            ).fetchall()
        return [_as_fact(row) for row in rows]

    def delete_user(self, account_id: int, user_id: int) -> int:
        """Erase all peer-scoped facts about this user within exactly one account."""
        account_id = _numeric_id(account_id, "account_id")
        user_id = _numeric_id(user_id, "user_id")
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM dossier_facts WHERE account_id=? AND user_id=?",
                (account_id, user_id),
            ).rowcount

    def delete_peer(self, account_id: int, user_id: int, peer_id: int) -> int:
        account_id = _numeric_id(account_id, "account_id")
        user_id = _numeric_id(user_id, "user_id")
        peer_id = _numeric_id(peer_id, "peer_id", allow_negative=True)
        with self._lock, self._db:
            return self._db.execute(
                """DELETE FROM dossier_facts WHERE account_id=? AND user_id=?
                   AND source_peer_id=?""", (account_id, user_id, peer_id),
            ).rowcount

    def purge_expired(self) -> int:
        """Physically erase expired facts; reads also hide them immediately."""
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM dossier_facts WHERE expires_at_ms<=?",
                (int(self._clock() * 1000),),
            ).rowcount
