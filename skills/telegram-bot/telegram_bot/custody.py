"""SQLite custody for Telegram provider updates and deliveries."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Sequence, Any, Dict, Iterator, List, Optional

from . import discovery
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
    # An earlier attempt may have reached Telegram: it ended with no answer (stale lease, restart or cancel), or
    # 0.9.0 made it and kept no proof that it did nothing.
    previous_unanswered: bool = False


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


# The retained answer to "which conversation is this message in, who wrote it and what did it
# say", by exact account, chat and message. Own messages live only in outbox JSON, so nothing
# else can answer it without scanning. Rows older than this table stay unknown, never guessed.
_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS telegram_messages(
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    topic_id TEXT NOT NULL DEFAULT '',
    chat_forum INTEGER,
    author_id TEXT NOT NULL DEFAULT '',
    author_name TEXT NOT NULL DEFAULT '',
    is_self INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    media_group_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(account_id, chat_id, message_id)
)"""
# A private message outside a topic records no forum fact, so the newest row that does is found without reading those.
_MESSAGES_FORUM_INDEX = """
CREATE INDEX IF NOT EXISTS telegram_messages_forum
    ON telegram_messages(account_id, chat_id, message_id) WHERE chat_forum IS NOT NULL"""
_REMEMBER_MESSAGE = """
INSERT OR REPLACE INTO telegram_messages(
    account_id, chat_id, message_id, topic_id, chat_forum, author_id, author_name, is_self, text, media_group_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
# Rows with an unacknowledged report always have a due time, so acknowledged history is not expanded.
_HELD_REPORTS = (
    "SELECT COUNT(*) FROM outbox, json_each(outbox.reports_json) AS entry WHERE outbox.report_due_at>0 "
    "AND json_extract(entry.value,'$.acked')=0 AND json_extract(entry.value,'$.error') LIKE 'Held:%'"
)
# reaction.target.excerpt never exceeds this; the full words stay in message.target_lookup.
_REACTION_EXCERPT_CHARS = 500
# How an outbox attempt that got no answer is recorded; a provider refusal or a hold names itself instead.
_UNANSWERED_ATTEMPT = frozenset({"released_at_runtime_start", "worker_cancelled"})
# The attempts count a reaction row had when this version last wrote it. Each attempt of this version stores the send
# marker before Telegram can be asked, so while the count is unchanged the row's own record says whether it was.
# Attempts past it are another version's, such as 0.9.0 after a rollback, which stores no marker.
REACTION_ATTEMPTS_MARKED = "_attempts_marked"
_STAMP_PATH = "$." + REACTION_ATTEMPTS_MARKED
# 0.9.0 stored no marker and kept only its newest attempt's error, in its client's words before Telegram's
# description. Only these prove that attempt did nothing: Telegram's 400, 401, 403, 404 or 429 to this call,
# or a failure before the call.
_UNMARKED_NO_EFFECT = re.compile(
    r"TelegramApiError: Telegram (?:setMessageReaction failed \((?:400|401|403|404|429)\)"
    r"|(?:getMe|deleteWebhook) failed \(\w+\)): |RuntimeError: telegram token is unavailable\Z")
# An inbox row kept on a Host that cannot hold it as context-only; it is never submitted later.
REACTION_WITHHELD = "reaction_host_context_unavailable"
# The bot getMe last confirmed, for tools in processes whose runtime does not poll: "<token digest>:<bot id>".
# The digest ties it to that one token. The token itself is not stored, and this row is read only by confirmed_account.
_CONFIRMED_BOT = "confirmed_bot"


def _token_binding(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest() + ":"


def _chat_forum(chat_type: Any, is_forum: Any, topic_id: Any = "") -> Optional[int]:
    """1 has topics, 0 known not to, None when this record does not say.

    A private chat can have topics too, so its message outside one says nothing either way.
    """
    if is_forum is True or topic_id not in ("", None):
        return 1
    return 0 if chat_type in {"group", "supergroup", "channel"} else None


def _unmarked_reaction(payload: Dict[str, Any]) -> bool:
    """A reaction row whose record holds no send marker or result; one that does decides by it."""
    return (payload.get("kind") == "operation" and payload.get("method") == "setMessageReaction"
            and not {"_provider_call_started", "_operation_result"} & set(payload))


def _remember_event(conn: sqlite3.Connection, event: TelegramEvent) -> None:
    actor = event.actor
    name = " ".join(part for part in (str(actor.get("first_name") or ""), str(actor.get("last_name") or "")) if part)
    conn.execute(_REMEMBER_MESSAGE, (
        event.account_id, event.conversation_id, int(event.message["message_id"]), event.thread_id,
        _chat_forum(event.conversation.get("chat_type"), event.conversation.get("is_forum"), event.thread_id),
        str(actor.get("platform_actor_id") or ""),
        name or str((actor.get("sender_chat") or {}).get("title") or ""), 0, event.text,
        str(event.message.get("media_group_id") or ""),
    ))


def _retained_message(conn: sqlite3.Connection, account_id: str, chat_id: str, message_id: Any) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM telegram_messages WHERE account_id=? AND chat_id=? AND message_id=?",
        (str(account_id), str(chat_id), int(message_id)),
    ).fetchone()


def _newest_chat_forum(conn: sqlite3.Connection, event: Any) -> Optional[int]:
    newest = conn.execute(
        "SELECT chat_forum FROM telegram_messages WHERE account_id=? AND chat_id=? "
        "AND chat_forum IS NOT NULL ORDER BY message_id DESC LIMIT 1",
        (str(event.account_id), str(event.conversation_id)),
    ).fetchone()
    return newest["chat_forum"] if newest else None


def _with_reaction_target(conn: sqlite3.Connection, event: TelegramEvent) -> TelegramEvent:
    """Add what an exact retained record proves about the target; a miss changes nothing."""
    fact = event.message["reaction"]
    row = _retained_message(conn, event.account_id, event.conversation_id, event.message["message_id"])
    if row is None:
        if fact["target"]["resolution"] == "resolved" and _newest_chat_forum(conn, event) == 1:
            # The update named no forum, yet this chat has shown topics: the conversation is unproven.
            return replace(event, message={**event.message, "reaction": {
                **fact, "target": {**fact["target"], "resolution": "unresolved"}}})
        return event
    author = {**({"platform_actor_id": row["author_id"]} if row["author_id"] else {}),
              **({"display_name": row["author_name"]} if row["author_name"] else {}),
              "is_self": bool(row["is_self"])}
    words = str(row["text"])
    target = {**fact["target"], "resolution": "resolved", "author": author,
              **({"excerpt": {"text": words[:_REACTION_EXCERPT_CHARS],
                              "truncated": len(words) > _REACTION_EXCERPT_CHARS}} if words.strip() else {})}
    topic = str(row["topic_id"])
    return replace(
        event, thread_id=topic,
        conversation_key=f"telegram:{event.account_id}:{event.conversation_id}:{topic or '0'}",
        conversation={**event.conversation, "topic_id": int(topic) if topic else None},
        message={**event.message, "reaction": {**fact, "target": target},
                 "target_lookup": {"source": "telegram-bot custody", "text": words, "topic_id": topic,
                                   "author_id": row["author_id"], "author_name": row["author_name"],
                                   "is_self": bool(row["is_self"])}},
    )


class CustodyStore:
    """Small connection-per-call store; SQLite owns queue truth across restarts."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def commit_update(self, update_id: int, event: Optional[TelegramEvent],
                      groups: Sequence[discovery.GroupSighting] = ()) -> bool:
        """Atomically deduplicate an event, note its groups and advance the provider offset."""
        now = time.time()
        inserted = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if event is not None:
                if "reaction" in event.message:
                    event = _with_reaction_target(conn, event)
                else:
                    _remember_event(conn, event)
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
            discovery.record_sightings(conn, int(update_id), groups)
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

    def record_confirmed_bot(self, token: str, account_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_CONFIRMED_BOT, _token_binding(token) + account_id),
            )

    def forget_confirmed_bot(self, token: str) -> None:
        """getMe no longer confirms this token. A record of another token is not this answer's to remove."""
        with self._connect() as conn:
            conn.execute("DELETE FROM metadata WHERE key=? AND value LIKE ?", (_CONFIRMED_BOT, _token_binding(token) + "%"))

    def confirmed_account(self, token: str) -> str:
        """The bot getMe last confirmed for exactly this token; ``""`` for any other token or none."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (_CONFIRMED_BOT,)).fetchone()
        binding = _token_binding(token)
        return row["value"][len(binding):] if row is not None and row["value"].startswith(binding) else ""

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

    def defer_inbox(
        self, event_id: str, *, reason: str, retry_after_sec: float
    ) -> None:
        """Keep a busy-refused event first in its conversation; the attempt is not counted.

        The Host admitted nothing, so this is neither custody nor a failure: the same
        event retries after the Host's hint, while other conversations proceed.
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbox
                SET state='pending', available_at=?, lease_until=0, last_error=?,
                    attempts=MAX(0, attempts-1)
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
        continuation_ref: str = "",
        output_ref: str = "",
        reply_anchor: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically retain Host custody, immediate text, and deferred work.

        A version-2 ``continuation_ref`` names this event's running author: it is
        observed like deferred work, beside any promoted child ``work_ref``, and the
        conversation's next event may go to the Host at once.
        """
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
                if output_ref:
                    self._enqueue_output(conn, event_id, turn_ref, event, {
                        "output_ref": output_ref, "text": text, "reply_anchor": reply_anchor}, now)
                else:
                    self._enqueue_outbox(
                        conn,
                        f"presence:{event_id}:turn",
                        _reply_payload(event, text, _anchor_message_id(conn, event, reply_anchor)),
                        now,
                    )
            observed = continuation_ref or (work_ref if outcome == "deferred" else "")
            if outcome == "deferred" and not observed:
                conn.rollback()
                raise ValueError("deferred submission requires work_ref")
            if observed:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO presence_work(
                        event_id, binding_id, work_ref, event_json, state,
                        attempts, available_at, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (event_id, binding_id, observed, _json(event), now, now),
                )
            if continuation_ref and work_ref and work_ref != continuation_ref:
                self._add_work(conn, event_id, binding_id, work_ref, "child", event,
                               delivery_reporting_version, now)
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
            # Due order: a reference released after its read is due again later
            # than every one already waiting, so older work never takes every turn.
            row = conn.execute(
                """
                SELECT event_id, binding_id, work_ref, role, event_json, attempts
                FROM presence_work
                WHERE (state='pending' AND available_at<=?)
                   OR (state='leased' AND lease_until<=?)
                ORDER BY available_at ASC, created_at ASC
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
                self._enqueue_output(conn, str(row["event_id"]), work_ref, event, output, time.time())
            conn.commit()

    def _enqueue_output(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        work_ref: str,
        event: Dict[str, Any],
        output: Dict[str, Any],
        now: float,
    ) -> None:
        """Queue a Host-minted output once for its event, whichever reference names it.

        The original turn, its continuation, a promoted child or a retained reference
        can each repeat it: identity is the exact ``output_ref``, never text or the
        polled reference. Its ``reply_anchor`` answers the covered message it names.
        """
        output_ref = str(output["output_ref"])
        prefix = f"presence:{event_id}:output:"
        queued = conn.execute(
            "SELECT delivery_id FROM outbox WHERE delivery_id >= ? AND delivery_id < ?",
            (prefix, prefix[:-1] + ";"),
        ).fetchall()
        if any(str(item[0]).endswith(":" + output_ref) for item in queued):
            return
        reply_to = _anchor_message_id(conn, event, output.get("reply_anchor"))
        self._enqueue_outbox(conn, f"{prefix}{work_ref}:{output_ref}",
                             _reply_payload(event, str(output["text"]), reply_to), now)

    def complete_work(self, work_ref: str, *, status: str, text: str, child_ref: str = "",
                      reply_anchor: Optional[Dict[str, Any]] = None) -> None:
        """Settle a reference; retain a child its terminal view names in the same commit."""
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("work status is not terminal")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT event_id, binding_id, role, event_json FROM presence_work "
                "WHERE work_ref=? AND state='leased'",
                (work_ref,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ValueError("presence work is not leased")
            if child_ref and child_ref != work_ref:
                event = json.loads(row["event_json"])
                mode = int((event.get("_reporting") or {}).get("version") == 1)
                self._add_work(conn, row["event_id"], row["binding_id"], child_ref, "child", event, mode, now)
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
                self._enqueue_outbox(conn, delivery_id, _reply_payload(
                    event, text, _anchor_message_id(conn, event, reply_anchor)), now)
            conn.commit()

    def claim_outbox(self, *, lease_sec: float = 90.0) -> Optional[OutboxLease]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT candidate.delivery_id, candidate.payload_json, candidate.attempts,
                       candidate.state, candidate.last_error
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
            payload = json.loads(row["payload_json"])
            unanswered = row["state"] == "leased" or row["last_error"] in _UNANSWERED_ATTEMPT
            marked = None
            if _unmarked_reaction(payload):
                # Attempts past the stamp were another version's, which kept only its newest error. Decided before
                # this claim clears that error, so no later failure can overwrite it.
                stamp = payload.get(REACTION_ATTEMPTS_MARKED)
                other = row["attempts"] - (stamp if type(stamp) is int else 0)
                unanswered = unanswered or not 0 <= other <= 1 or (
                    other == 1 and not _UNMARKED_NO_EFFECT.match(row["last_error"]))
                if not unanswered:
                    payload = {**payload, REACTION_ATTEMPTS_MARKED: row["attempts"] + 1}
                    marked = _json(payload)
            updated = conn.execute(
                """
                UPDATE outbox
                SET state='leased', lease_until=?, attempts=attempts+1, last_error='',
                    payload_json=COALESCE(?, payload_json)
                WHERE delivery_id=? AND (
                    (state='pending' AND available_at<=?)
                    OR (state='leased' AND lease_until<=?)
                )
                """,
                (now + max(1.0, lease_sec), marked, row["delivery_id"], now, now),
            )
            conn.commit()
            if updated.rowcount != 1:
                return None
            return OutboxLease(
                delivery_id=str(row["delivery_id"]),
                payload=payload,
                attempts=int(row["attempts"]) + 1,
                previous_unanswered=unanswered,
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

    def hold_outbox(self, delivery_id: str, reason: str) -> None:
        # A hold returns its attempt to the count; a reaction's stamp of that count moves with it.
        with self._connect() as conn:
            conn.execute("UPDATE outbox SET state='pending',available_at=?,lease_until=0,"
                         "last_error=?,attempts=MAX(0,attempts-1),payload_json=CASE WHEN "
                         "json_extract(payload_json,?)=attempts THEN json_set(payload_json,?,MAX(0,attempts-1)) "
                         "ELSE payload_json END WHERE delivery_id=? AND state='leased'",
                         (time.time() + 5, reason, _STAMP_PATH, _STAMP_PATH, delivery_id))

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

    def checkpoint_outbox(self, delivery_id: str, payload: Dict[str, Any], *, report=None,
                          sent: Optional[Sequence[Any]] = None) -> None:
        """Retain confirmed text chunks before attempting the next provider call.

        ``sent`` remembers a just-confirmed own message: its exact creation receipt is what
        later identifies a reaction on it as a reaction on this bot's words.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE outbox SET payload_json=? WHERE delivery_id=? AND state='leased'",
                (_json(payload), delivery_id),
            )
            if sent:
                conn.execute(_REMEMBER_MESSAGE, sent)
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

    def reaction_target(self, account_id: str, chat_id: str, message_id: int) -> Dict[str, Any]:
        """What retained records say about one message's conversation, from indexed rows only.

        ``retained`` names the exact message and its topic. Otherwise ``forum`` is what the newest
        retained message of this chat that says anything recorded (1, 0 or None). In a private chat
        only a topic message says, so a later message outside a topic leaves its topics shown.
        """
        with self._connect() as conn:
            row = _retained_message(conn, account_id, chat_id, message_id)
            if row is not None:
                return {"retained": True, "topic_id": str(row["topic_id"]), "media_group_id": str(row["media_group_id"])}
            scope = SimpleNamespace(account_id=account_id, conversation_id=chat_id)
            return {"retained": False, "forum": _newest_chat_forum(conn, scope)}

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
        reaction = payload.get("kind") == "operation" and payload.get("method") == "setMessageReaction"
        reporting = payload.get("_reporting") or {}
        return {
            # -1: the outbox has not yet asked this Host.
            **({"history_limitation": "Host history for reactions is unavailable: this receipt stays local."}
               if reaction and (reporting.get("version") != -1 or row["state"] != "pending")
               and reporting.get("explicit_completion") != 1
               and not any(entry["acked"] for entry in json.loads(row["reports_json"])) else {}),
            "state": "held" if row["state"] == "pending" and str(row["last_error"]).startswith("Held:") else "uncertain" if payload.get("_terminal_outcome") == "uncertain" else str(row["state"]),
            "attempts": int(row["attempts"]),
            "delivered_at": _timestamp(row["delivered_at"]),
            "provider_receipt": json.loads(row["provider_receipt_json"] or "{}"),
            "error": str(row["last_error"]),
            "delivery_reporting": payload.get("_reporting", {"version": 0, "status": "legacy_unconfirmed"}),
            "history_reports": json.loads(row["reports_json"]),
        }

    def offered_groups(self, account_id: str) -> List[Dict[str, str]]:
        """Connect choices of one bot; reads its groups, never the message history."""
        with self._connect() as conn:
            return discovery.offered_groups(conn, account_id)

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
                "inbox_failed": inbox.get("failed", 0) - (withheld := conn.execute(
                    "SELECT COUNT(*) FROM inbox WHERE state='failed' AND last_error=?", (REACTION_WITHHELD,)
                ).fetchone()[0]),
                "reactions_withheld": withheld,
                "outbox_waiting": outbox.get("pending", 0),
                "outbox_held": conn.execute("SELECT COUNT(*) FROM outbox WHERE state='pending' AND last_error LIKE 'Held:%'").fetchone()[0],
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
                "delivery_reports_held": conn.execute(_HELD_REPORTS).fetchone()[0],
                "group_backfill_unreadable": discovery.backfill_unreadable(conn),
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
                + discovery.GROUPS_TABLE
                + ";"
                + _MESSAGES_TABLE
                + ";"
                + _MESSAGES_FORUM_INDEX
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
        discovery.backfill_legacy(self._connect)

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


def _anchor_message_id(
    conn: sqlite3.Connection, event: Dict[str, Any], anchor: Optional[Dict[str, Any]]
) -> Any:
    """The message a version-2 output answers when its anchor is one this chat captured.

    The Host names the last covered input by ``source_event_id`` and its exact captured
    ``message``. An anchor from another conversation, or unlike that capture, is not
    used: the reply then answers the work's own event, as without an anchor.
    """
    if not isinstance(anchor, dict) or not isinstance(anchor.get("message"), dict):
        return None
    message_id = anchor["message"].get("message_id")
    if isinstance(message_id, bool) or message_id in (None, ""):
        return None
    row = conn.execute(
        "SELECT conversation_key, payload_json FROM inbox WHERE event_id=?",
        (str(anchor.get("source_event_id") or ""),),
    ).fetchone()
    if row is None or row["conversation_key"] != event.get("conversation_key"):
        return None
    captured = (json.loads(row["payload_json"]).get("message") or {}).get("message_id")
    return captured if str(captured) == str(message_id) else None


def _reply_payload(event: Dict[str, Any], text: str, reply_to: Any = None) -> Dict[str, Any]:
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
    reply_id = message.get("message_id") if reply_to in (None, "") else reply_to
    if reply_id not in (None, ""):
        payload["reply_to_message_id"] = reply_id
    return payload
