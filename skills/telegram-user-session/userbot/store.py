"""Durable, account-scoped inbox deduplication and one-shot outgoing send intents.

The caller persists a NONSECRET opaque outgoing payload reference before
enqueue(), and resolves that reference to message content outside this database.
Incoming Host Presence event JSON (including private message text) is stored
until Host ACK for crash-safe delivery. ACK replaces the JSON with an empty
tombstone, retaining its digest to reject changed replays. Keep the database
in private runtime state, never in Git; do not put credentials, sessions,
tokens or raw exceptions here. SQLite/WAL backups may retain old page bytes:
this is logical retention, not forensic secure erasure.

A single companion owns dispatch at a time; plugin tool handlers may open
additional handles for enqueue/get. At exclusive companion startup, pass
recover_attempted=True: unsettled attempted sends become UNKNOWN, because
Telegram may have accepted them. They are never claimed again. An unattempted
lease may be reclaimed after expiry. Call mark_attempted() and commit it BEFORE
the network send; otherwise exactly-once behavior is impossible across a crash.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_SAFE_REF = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z")
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
RAW_INGRESS_MAX_EVENTS = 1000  # Pending/held entries; each text <=60 KiB at capture.


class IngressBackpressure(RuntimeError):
    """Capture refused before the durable boundary; manual recovery is required."""
_HOST_EVENT_KEYS = frozenset({
    "source_event_id", "provider", "account_id", "conversation_id",
    "thread_id", "conversation_key", "actor", "conversation", "message", "text",
})
_PROVIDER_RAW_FIELDS = (
    "account_id", "peer_id", "message_id", "kind", "revision", "actor_id",
    "text", "mentioned", "forum_topic", "reply_to_top_id", "reply_to_msg_id",
    "action_constructor_id",
)


@dataclass(frozen=True)
class OutgoingJob:
    account_id: str
    intent_id: str
    peer_id: int
    payload_ref: str
    topic_id: int | None
    reply_id: int | None
    random_id: int
    status: str
    lease_token: str | None
    lease_until: float | None
    remote_message_id: int | None
    error_code: str | None
    reported: int
    report_next_at: float
    report_error: str


@dataclass(frozen=True)
class InboxEvent:
    account_id: str
    event_id: str
    payload_json: str
    status: str
    payload_digest: str

    @property
    def payload(self) -> dict:
        """The exact persisted event, decoded for the Host Presence POST."""
        if self.status != "pending":
            raise ValueError("acknowledged event body has been discarded")
        return json.loads(self.payload_json)


class DurableStore:
    """SQLite WAL store. ``path`` belongs in private runtime data, NOT in git.

    Thread ownership follows sqlite3's default check_same_thread rule. Multiple
    handles can enqueue/get; only the exclusive companion calls claim and opens
    a handle with recover_attempted=True after any old companion has stopped.
    Close a handle before leaving its owner.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 recover_attempted: bool = False):
        if not isinstance(recover_attempted, bool):
            raise ValueError("recover_attempted must be bool")
        requested_path = Path(path)
        if not requested_path.is_absolute():
            raise ValueError("store path must be an absolute private runtime-state path")
        self.path = requested_path.resolve()
        if self.path.is_relative_to(Path(__file__).resolve().parent.parent):
            raise ValueError("store must not be created inside the skill/repository tree")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA busy_timeout=10000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self._init_schema(recover_attempted=recover_attempted)
        except BaseException:
            self.db.close()
            raise

    def _init_schema(self, *, recover_attempted: bool) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                account_id TEXT NOT NULL, peer_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
                PRIMARY KEY (account_id, peer_id, message_id, kind, version)
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS inbox_delivery (
                account_id TEXT NOT NULL, event_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'acked')),
                payload_digest TEXT NOT NULL,
                PRIMARY KEY (account_id, event_id)
            )""")
            # Older private databases stored full JSON forever, including
            # ACKed events. The migration is atomic with the digest backfill:
            # a crash cannot leave a scrubbed row without replay identity.
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(inbox_delivery)")}
            if "payload_digest" not in columns:
                self.db.execute("""ALTER TABLE inbox_delivery ADD COLUMN
                    payload_digest TEXT NOT NULL DEFAULT ''""")
                for row in self.db.execute("""SELECT account_id, event_id,
                    payload_json, status FROM inbox_delivery""").fetchall():
                    digest = self._payload_digest(row["payload_json"])
                    self.db.execute("""UPDATE inbox_delivery SET payload_digest=?,
                        payload_json=? WHERE account_id=? AND event_id=?""",
                        (digest, row["payload_json"] if row["status"] == "pending" else "",
                         row["account_id"], row["event_id"]),
                    )
            self.db.execute("""CREATE INDEX IF NOT EXISTS inbox_delivery_pending
                ON inbox_delivery(account_id, status)""")
            # This transaction, rather than a hash filename or mutable mtime,
            # is the authority for local callback-capture order. The complete
            # original event also lets the companion restore a file if it dies
            # after this commit but before the redundant raw spool is written.
            self.db.execute("""CREATE TABLE IF NOT EXISTS raw_ingress (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                filename TEXT NOT NULL, peer_id INTEGER NOT NULL,
                initial_json TEXT NOT NULL, initial_digest TEXT NOT NULL,
                provider_digest TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('pending','done','held')),
                UNIQUE(account_id, filename)
            )""")
            raw_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(raw_ingress)")}
            if "peer_id" not in raw_columns:
                self.db.execute("ALTER TABLE raw_ingress ADD COLUMN peer_id INTEGER NOT NULL DEFAULT 0")
            if "initial_digest" not in raw_columns:
                self.db.execute("ALTER TABLE raw_ingress ADD COLUMN initial_digest TEXT NOT NULL DEFAULT ''")
            if "provider_digest" not in raw_columns:
                self.db.execute("ALTER TABLE raw_ingress ADD COLUMN provider_digest TEXT NOT NULL DEFAULT ''")
            for row in self.db.execute("""SELECT seq,initial_json,status FROM raw_ingress
                    WHERE initial_digest='' OR peer_id=0""").fetchall():
                original = json.loads(row["initial_json"])
                peer = original["peer_id"]
                digest = self._payload_digest(row["initial_json"])
                self.db.execute("""UPDATE raw_ingress SET peer_id=?, initial_digest=?,
                    provider_digest=?, initial_json=? WHERE seq=?""", (peer, digest,
                    self._provider_raw_digest(original),
                    "" if row["status"] == "done" else row["initial_json"], row["seq"]))
            for row in self.db.execute("""SELECT seq,initial_json FROM raw_ingress
                    WHERE provider_digest='' AND initial_json!=''""").fetchall():
                self.db.execute("UPDATE raw_ingress SET provider_digest=? WHERE seq=?",
                                (self._provider_raw_digest(json.loads(row["initial_json"])), row["seq"]))
            self.db.execute("""CREATE INDEX IF NOT EXISTS raw_ingress_pending
                ON raw_ingress(account_id, status, peer_id, seq)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS raw_ingress_peer_head
                ON raw_ingress(account_id, status, peer_id, seq)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS outgoing (
                account_id TEXT NOT NULL, intent_id TEXT NOT NULL,
                peer_id INTEGER NOT NULL, payload_ref TEXT NOT NULL,
                topic_id INTEGER, reply_id INTEGER, random_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('queued','leased','attempted','unknown','sent','failed')),
                lease_token TEXT, lease_until REAL, remote_message_id INTEGER,
                error_code TEXT,
                PRIMARY KEY (account_id, intent_id),
                UNIQUE (account_id, random_id)
            )""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS outgoing_claim
                ON outgoing(account_id, status, lease_until)""")
            outgoing_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(outgoing)")}
            if "reported" not in outgoing_columns:
                self.db.execute("ALTER TABLE outgoing ADD COLUMN reported INTEGER NOT NULL DEFAULT 0")
            if "report_next_at" not in outgoing_columns:
                self.db.execute("ALTER TABLE outgoing ADD COLUMN report_next_at REAL NOT NULL DEFAULT 0")
            if "report_error" not in outgoing_columns:
                self.db.execute("ALTER TABLE outgoing ADD COLUMN report_error TEXT NOT NULL DEFAULT ''")
            self.db.execute("""CREATE INDEX IF NOT EXISTS outgoing_report
                ON outgoing(account_id, status, reported)""")
            # Only the exclusive companion startup is the recovery boundary.
            # Ordinary plugin handles must never disturb an in-flight send.
            if recover_attempted:
                self.db.execute("UPDATE outgoing SET status='unknown' WHERE status='attempted'")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> DurableStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _account(account_id: str) -> None:
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id must be a nonempty string")

    @staticmethod
    def _positive(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
            raise ValueError(f"{name} must be a positive 64-bit integer")

    @staticmethod
    def _peer(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not -(2**63) < value < 2**63:
            raise ValueError("peer_id must be a nonzero signed 64-bit integer")

    @staticmethod
    def _ref(value: str, name: str) -> None:
        if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
            raise ValueError(f"{name} must be an opaque nonsecret identifier")

    @staticmethod
    def _job(row: sqlite3.Row) -> OutgoingJob:
        return OutgoingJob(**dict(row))

    @staticmethod
    def _event_id(event_id: str) -> None:
        if (not isinstance(event_id, str) or not event_id or len(event_id) > 1024
                or any(ord(char) < 32 for char in event_id)):
            raise ValueError("event_id must be a nonempty stable key without control characters")

    @staticmethod
    def _payload_digest(serialized: str) -> str:
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _provider_raw_digest(raw: dict) -> str:
        """Provider observation only: changing enrollment cannot change its identity."""
        return DurableStore._payload_digest(json.dumps(
            {key: raw.get(key) for key in _PROVIDER_RAW_FIELDS},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))

    def stage_raw(self, account_id: str, filename: str, raw: dict) -> str:
        """Atomically assign capture order to an immutable original event.

        Returns its existing status on redelivery; neither a duplicate nor a
        file rewrite moves its ordinal. Only the companion invokes this seam.
        """
        self._account(account_id)
        if (not isinstance(filename, str) or not re.fullmatch(r"[0-9a-f]{64}\.json", filename)
                or not isinstance(raw, dict)):
            raise ValueError("invalid raw ingress identity")
        serialized = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False)
        peer = raw.get("peer_id")
        self._peer(peer)
        digest = self._payload_digest(serialized)
        provider_digest = self._provider_raw_digest(raw)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("""SELECT initial_digest,provider_digest,status FROM raw_ingress
                WHERE account_id=? AND filename=?""", (account_id, filename)).fetchone()
            if existing:
                if existing["provider_digest"] and existing["provider_digest"] != provider_digest:
                    raise ValueError("raw event identity changed")
                if not existing["provider_digest"] and existing["initial_digest"] != digest:
                    return "unverifiable"  # old bodyless tombstone, never re-admit
                return existing["status"]
            depth = self.db.execute("""SELECT count(*) FROM raw_ingress
                WHERE account_id=? AND status IN ('pending','held')""", (account_id,)).fetchone()[0]
            if depth >= RAW_INGRESS_MAX_EVENTS:
                raise IngressBackpressure("private ingress backlog at declared bound")
            self.db.execute("""INSERT INTO raw_ingress
                (account_id,filename,peer_id,initial_json,initial_digest,provider_digest,status)
                VALUES (?,?,?,?,?,?,'pending')""",
                (account_id, filename, peer, serialized, digest, provider_digest))
        return "pending"

    def next_raw(self, account_id: str, blocked_peers: set[int],
                 after_seq: int = 0) -> tuple[int, str, dict] | None:
        """One bounded peer-head read; an unsettled earlier event holds its peer.

        The query returns one JSON object, regardless of backlog size. A blocked
        peer in this pass does not stop unrelated peers; the next pass tries it
        again. seq, not filename, is the durable callback-capture order.
        """
        self._account(account_id)
        blocked = sorted(blocked_peers)
        placeholders = ",".join("?" for _ in blocked)
        exclusion = f"AND r.peer_id NOT IN ({placeholders})" if blocked else ""
        for lower in (after_seq, 0):
            row = self.db.execute(f"""SELECT r.seq,r.filename,r.initial_json FROM raw_ingress r
                WHERE r.account_id=? AND r.status='pending' AND r.seq>? {exclusion}
                AND NOT EXISTS (SELECT 1 FROM raw_ingress prior WHERE
                    prior.account_id=r.account_id AND prior.peer_id=r.peer_id
                    AND prior.status='pending' AND prior.seq<r.seq)
                ORDER BY r.seq LIMIT 1""", (account_id, lower, *blocked)).fetchone()
            if row:
                return row["seq"], row["filename"], json.loads(row["initial_json"])
            if after_seq == 0:
                break
        return None

    def pending_raw_count(self, account_id: str) -> int:
        self._account(account_id)
        return int(self.db.execute("""SELECT count(*) FROM raw_ingress
            WHERE account_id=? AND status='pending'""", (account_id,)).fetchone()[0])

    def settle_raw(self, account_id: str, filename: str, *, held: bool = False) -> None:
        self._account(account_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""UPDATE raw_ingress SET status=?,
                initial_json=CASE WHEN ? THEN initial_json ELSE '' END WHERE account_id=?
                AND filename=? AND status='pending'""",
                ("held" if held else "done", int(held), account_id, filename))

    def reserve_inbox(self, account_id: str, event_id: str, payload: dict) -> InboxEvent:
        """Persist complete non-credential Host event JSON BEFORE attempting POST.

        Retry with the same key and equal canonical JSON keeps its pending/acked
        state. After ACK the returned event is a bodyless tombstone; differing
        bytes under that key are refused by digest comparison. Caller forms a globally unique id
        (account + peer + topic + message + edit version) also supplied inside
        the Host event as source_event_id.
        """
        self._account(account_id)
        self._event_id(event_id)
        if (not isinstance(payload, dict) or set(payload) != _HOST_EVENT_KEYS
                or payload.get("source_event_id") != event_id
                or payload.get("account_id") != account_id):
            raise ValueError("exact Host event keys and matching account/source event id required")
        try:
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Host event must be JSON serializable") from exc
        if len(serialized.encode("utf-8")) > 1_000_000:
            raise ValueError("Host event exceeds 1 MB; stage large media separately")
        digest = self._payload_digest(serialized)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("""SELECT * FROM inbox_delivery
                WHERE account_id=? AND event_id=?""", (account_id, event_id)).fetchone()
            if existing is not None:
                if (existing["payload_digest"] != digest or
                        (existing["status"] == "pending" and existing["payload_json"] != serialized)):
                    raise ValueError("event_id already reserved with different Host event bytes")
                return InboxEvent(**dict(existing))
            self.db.execute("""INSERT INTO inbox_delivery
                (account_id, event_id, payload_json, status, payload_digest)
                VALUES (?, ?, ?, 'pending', ?)""",
                (account_id, event_id, serialized, digest))
            return InboxEvent(account_id, event_id, serialized, "pending", digest)

    def pending_inbox(self, account_id: str, *, limit: int = 100) -> list[InboxEvent]:
        """Re-read exact event bytes after interruption; replay uses same Host id."""
        self._account(account_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 10000:
            raise ValueError("limit must be in [1, 10000]")
        rows = self.db.execute("""SELECT * FROM inbox_delivery
            WHERE account_id=? AND status='pending' ORDER BY rowid LIMIT ?""",
            (account_id, limit),
        ).fetchall()
        return [InboxEvent(**dict(row)) for row in rows]

    def ack_inbox(self, account_id: str, event_id: str) -> bool:
        """ACK only after Host accepts; scrub JSON in the same transaction.

        False if already ACKed, missing raises. Host Presence idempotency with
        the same source_event_id handles a crash after POST but before this
        local ACK. Retain the digest and identity as a replay tombstone.
        """
        self._account(account_id)
        self._event_id(event_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute("""UPDATE inbox_delivery
                SET status='acked', payload_json=''
                WHERE account_id=? AND event_id=? AND status='pending'""",
                (account_id, event_id),
            )
            if cursor.rowcount:
                return True
            row = self.db.execute("""SELECT status FROM inbox_delivery
                WHERE account_id=? AND event_id=?""", (account_id, event_id)).fetchone()
            if row is None:
                raise LookupError("no inbound event reserved under this account")
            return False

    def record_inbox(
        self, account_id: str, peer_id: int, message_id: int, *,
        kind: str = "new", version: str = "",
    ) -> bool:
        """True on first sight of this event, False for an exact duplicate.

        For edits, supply a stable nonempty version (e.g. edit_date UTC with
        microseconds). A new message and each distinct edit remain distinct.
        This legacy first-sight helper is NOT crash-safe Host delivery: use
        reserve_inbox/pending_inbox/ack_inbox for the actual inbound path.
        """
        self._account(account_id)
        self._peer(peer_id)
        self._positive(message_id, "message_id")
        if kind not in ("new", "edit"):
            raise ValueError("kind must be new or edit")
        if not isinstance(version, str) or len(version) > 128 or (kind == "edit" and not version):
            raise ValueError("edit version must be a stable nonempty string of at most 128 characters")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO inbox VALUES (?, ?, ?, ?, ?)",
                (account_id, peer_id, message_id, kind, version),
            )
            return cursor.rowcount == 1

    def enqueue(
        self, account_id: str, intent_id: str, *, peer_id: int,
        payload_ref: str, topic_id: int | None = None, reply_id: int | None = None,
    ) -> OutgoingJob:
        """Commit a stable intent before dispatch; repeat with equal args is safe.

        A changed intent under the same account+intent_id raises ValueError;
        NEVER infer an automatic retry from an existing terminal/unknown job.
        ``payload_ref`` is a reference to separately persisted content, not
        message content itself. The random_id is signed TL int64 and immutable.
        """
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        self._ref(payload_ref, "payload_ref")
        self._peer(peer_id)
        for name, value in (("topic_id", topic_id), ("reply_id", reply_id)):
            if value is not None:
                self._positive(value, name)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT * FROM outgoing WHERE account_id=? AND intent_id=?",
                (account_id, intent_id),
            ).fetchone()
            if existing is not None:
                if (existing["peer_id"], existing["payload_ref"], existing["topic_id"], existing["reply_id"]) != (peer_id, payload_ref, topic_id, reply_id):
                    raise ValueError("intent_id already used for different send")
                return self._job(existing)
            for _ in range(8):
                bits = secrets.randbits(64)
                random_id = bits - (1 << 64) if bits >= 1 << 63 else bits
                if random_id == 0:
                    continue
                try:
                    self.db.execute("""INSERT INTO outgoing
                        (account_id, intent_id, peer_id, payload_ref, topic_id,
                         reply_id, random_id, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
                        (account_id, intent_id, peer_id, payload_ref, topic_id, reply_id, random_id),
                    )
                    break
                except sqlite3.IntegrityError:
                    # Extremely unlikely per-account random_id collision.
                    continue
            else:
                raise RuntimeError("could not allocate unique random_id")
            return self.get(account_id, intent_id)

    def get(self, account_id: str, intent_id: str) -> OutgoingJob | None:
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        row = self.db.execute(
            "SELECT * FROM outgoing WHERE account_id=? AND intent_id=?",
            (account_id, intent_id),
        ).fetchone()
        return self._job(row) if row is not None else None

    def claim(self, account_id: str, *, lease_seconds: float = 60) -> OutgoingJob | None:
        """Lease one unattempted job; expired *unattempted* leases can recur.

        An attempted or unknown send is never returned, regardless of lease.
        Pass the returned lease_token to mark_attempted and settle to fence a
        stale worker whose lease was reclaimed before it made the send.
        """
        self._account(account_id)
        if not isinstance(lease_seconds, (float, int)) or isinstance(lease_seconds, bool) or not 0 < lease_seconds <= 86400:
            raise ValueError("lease_seconds must be in (0, 86400]")
        now = self.clock()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE outgoing SET status='unknown' WHERE account_id=? AND status='attempted' AND lease_until<=?", (account_id, now))
            row = self.db.execute("""SELECT intent_id FROM outgoing
                WHERE account_id=? AND (status='queued' OR
                    (status='leased' AND lease_until<=?))
                ORDER BY rowid LIMIT 1""", (account_id, now)).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            self.db.execute("""UPDATE outgoing SET status='leased',
                lease_token=?, lease_until=? WHERE account_id=? AND intent_id=?""",
                (token, now + lease_seconds, account_id, row["intent_id"]),
            )
            return self.get(account_id, row["intent_id"])

    def mark_attempted(self, account_id: str, intent_id: str, lease_token: str) -> OutgoingJob:
        """Durable send boundary: call immediately before the network request."""
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        self._ref(lease_token, "lease_token")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute("""UPDATE outgoing SET status='attempted'
                WHERE account_id=? AND intent_id=? AND status='leased'
                AND lease_token=? AND lease_until>?""",
                (account_id, intent_id, lease_token, self.clock()),
            )
            if cursor.rowcount != 1:
                raise ValueError("lease lost or intent already attempted; do not send")
            return self.get(account_id, intent_id)

    def reject_before_send(self, account_id: str, intent_id: str,
                           lease_token: str, error_code: str) -> OutgoingJob:
        """Retire a permanently invalid leased intent without pretending a send began.

        The validated lease token fences a concurrent owner. Transient catalog
        or file read failures must NOT use this path; let the lease expire so
        the exact unattempted intent may be checked again.
        """
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        self._ref(lease_token, "lease_token")
        if not isinstance(error_code, str) or not _SAFE_CODE.fullmatch(error_code):
            raise ValueError("safe error_code required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute("""UPDATE outgoing SET status='failed',
                error_code=?, lease_until=NULL WHERE account_id=? AND intent_id=?
                AND status='leased' AND lease_token=?""",
                (error_code, account_id, intent_id, lease_token),
            )
            if changed.rowcount != 1:
                raise ValueError("lease lost or intent already dispatched")
            return self.get(account_id, intent_id)

    def settle(
        self, account_id: str, intent_id: str, lease_token: str, *,
        success: bool, remote_message_id: int | None = None,
        error_code: str | None = None,
    ) -> OutgoingJob:
        """Persist send result, including a late result after recovery to unknown.

        On failure store only a bounded machine error code, never exception text.
        A send with no definitive outcome remains unknown on restart/expiry.
        """
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        self._ref(lease_token, "lease_token")
        if not isinstance(success, bool):
            raise ValueError("success must be bool")
        if success:
            if remote_message_id is not None:
                self._positive(remote_message_id, "remote_message_id")
            if error_code is not None:
                raise ValueError("successful send cannot have an error_code")
        elif remote_message_id is not None or error_code is None or not _SAFE_CODE.fullmatch(error_code):
            raise ValueError("failure needs a safe error_code and no remote_message_id")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute("""UPDATE outgoing SET status=?,
                remote_message_id=?, error_code=?, lease_until=NULL
                WHERE account_id=? AND intent_id=? AND lease_token=?
                AND status IN ('attempted','unknown')""",
                ("sent" if success else "failed", remote_message_id, error_code,
                 account_id, intent_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("intent not attempted or lease token changed")
            return self.get(account_id, intent_id)

    def list_unknown(self, account_id: str) -> list[OutgoingJob]:
        """Unresolved sends requiring an explicit external reconciliation decision."""
        self._account(account_id)
        now = self.clock()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE outgoing SET status='unknown' WHERE account_id=? AND status='attempted' AND lease_until<=?", (account_id, now))
            rows = self.db.execute("""SELECT * FROM outgoing WHERE account_id=?
                AND status='unknown' ORDER BY rowid""", (account_id,)).fetchall()
            return [self._job(row) for row in rows]

    def unreported_auto_sends(self, account_id: str, *, limit: int = 100) -> list[OutgoingJob]:
        """Bounded durable reporting queue, not a scan of all retained payloads."""
        self._account(account_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("report limit must be between 1 and 1000")
        rows = self.db.execute("""SELECT * FROM outgoing WHERE account_id=?
            AND status='sent' AND reported=0 AND report_next_at<=?
            AND substr(intent_id,1,5)='auto_' ORDER BY rowid LIMIT ?""",
            (account_id, self.clock(), limit)).fetchall()
        return [self._job(row) for row in rows]

    def mark_reported(self, account_id: str, intent_id: str) -> None:
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""UPDATE outgoing SET reported=1 WHERE account_id=?
                AND intent_id=? AND status='sent'""", (account_id, intent_id))

    def mark_report_invalid(self, account_id: str, intent_id: str) -> None:
        """Retire an unreportable row without mislabelling it delivered."""
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""UPDATE outgoing SET reported=2 WHERE account_id=?
                AND intent_id=? AND status='sent' AND reported=0""", (account_id, intent_id))

    def mark_report_unsupported(self, account_id: str, intent_id: str) -> None:
        """No v1 receipt was negotiated; do not call this invalid or delivered."""
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""UPDATE outgoing SET reported=3 WHERE account_id=?
                AND intent_id=? AND status='sent' AND reported=0""", (account_id, intent_id))

    def defer_report(self, account_id: str, intent_id: str) -> None:
        """One failed Host report delays only itself; another receipt may advance."""
        self._account(account_id)
        self._ref(intent_id, "intent_id")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""UPDATE outgoing SET report_next_at=?,
                report_error='HOST_REPORT_UNAVAILABLE' WHERE account_id=?
                AND intent_id=? AND status='sent' AND reported=0""",
                (self.clock() + 60, account_id, intent_id))

    def report_counts(self, account_id: str) -> dict[str, int]:
        self._account(account_id)
        row = self.db.execute("""SELECT
            SUM(CASE WHEN reported=0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN reported=0 AND report_error<>'' THEN 1 ELSE 0 END),
            SUM(CASE WHEN reported=2 THEN 1 ELSE 0 END),
            SUM(CASE WHEN reported=3 THEN 1 ELSE 0 END)
            FROM outgoing WHERE account_id=? AND status='sent'
            AND substr(intent_id,1,5)='auto_'""", (account_id,)).fetchone()
        return dict(zip(("pending", "retrying", "invalid", "unsupported"),
                        (int(value or 0) for value in row)))
