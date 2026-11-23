"""SQLite custody for Telegram provider updates and deliveries."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Sequence, Any, Dict, Iterator, List, Optional, Tuple

from . import discovery, outbox_files
from .delivery import stored_answers
from .events import ALBUM_POSITION_KEY, TelegramEvent
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
    # A send row (text, file or album): an earlier attempt left no reliable proof that it did nothing, so it may
    # have reached Telegram (see claim_outbox).
    unrecorded_attempt: bool = False


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
# An album member's place counts only that album's earlier members, not the chat's history; album rows only.
# The query repeats the index's media_group_id<>'' term: without it SQLite cannot use a partial index.
_MESSAGES_ALBUM_INDEX = """
CREATE INDEX IF NOT EXISTS telegram_messages_album
    ON telegram_messages(account_id, chat_id, media_group_id, message_id) WHERE media_group_id<>''"""
_ALBUM_EARLIER = ("SELECT COUNT(*) FROM telegram_messages WHERE account_id=? AND chat_id=? AND media_group_id=? "
                  "AND media_group_id<>'' AND message_id<?")
_REMEMBER_MESSAGE = """
INSERT OR REPLACE INTO telegram_messages(
    account_id, chat_id, message_id, topic_id, chat_forum, author_id, author_name, is_self, text, media_group_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
# One row per attachment whose download failed: how often while Telegram answered the bot (own_failures, from
# first_failed_at) and the newest failure of any kind. The runtime turns it into a terminal record (runtime.py).
_DOWNLOAD_FAILURES_TABLE = """
CREATE TABLE IF NOT EXISTS download_failures(
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    own_failures INTEGER NOT NULL DEFAULT 0,
    first_failed_at REAL NOT NULL DEFAULT 0,
    last_failed_at REAL NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(event_id, ordinal)
)"""
# A model's request to show "typing…" in its turn's conversation. Not custody: the runtime empties it when it
# starts, so a request is never replayed after a restart, and nothing here is a receipt or a delivery.
_TYPING_TABLE = """
CREATE TABLE IF NOT EXISTS typing_requests(
    chat_id TEXT NOT NULL,
    topic_id TEXT NOT NULL DEFAULT '',
    turn_ref TEXT NOT NULL,
    source_event_id TEXT NOT NULL DEFAULT '',
    requested_at REAL NOT NULL,
    PRIMARY KEY(chat_id, topic_id, turn_ref)
)"""
# Rows with an unacknowledged report always have a due time, so acknowledged history is not expanded.
_HELD_REPORTS = (
    "SELECT COUNT(*) FROM outbox, json_each(outbox.reports_json) AS entry WHERE outbox.report_due_at>0 "
    "AND json_extract(entry.value,'$.acked')=0 AND json_extract(entry.value,'$.error') LIKE 'Held:%'"
)
# reaction.target.excerpt never exceeds this; the full words stay in message.target_lookup.
_REACTION_EXCERPT_CHARS = 500
# How an outbox attempt that got no answer is recorded; a provider refusal or a hold names itself instead.
_UNANSWERED_ATTEMPT = frozenset({"released_at_runtime_start", "worker_cancelled"})
# The attempts count a reaction or send row had when this version last claimed it. Each attempt of this version
# stores the call marker before Telegram can be asked, so while the count is unchanged the row's own record says
# whether it was. Attempts past it are another version's, such as 0.9.0 or 0.12.0 after a rollback, which store none.
REACTION_ATTEMPTS_MARKED = "_attempts_marked"
_STAMP_PATH = "$." + REACTION_ATTEMPTS_MARKED
# 0.12.1 also wrote the shared stamp, but could clear a file call's marker on a bare HTTP error
# or on the answer to a different request (redirect or plain-caption retry). Only this stamp
# accounts for file attempts made with strict replies. Keep the shared stamp for rollback readers.
_FILE_ATTEMPTS_MARKED = "_file_attempts_marked"
_FILE_STAMP_PATH = "$." + _FILE_ATTEMPTS_MARKED
# A send row's record of the provider call this version is about to make: {"part", "method"}, part being the text
# chunk's index (0 for a file or an album). It is stored before the call and removed only in the commit that records
# Telegram's accepted message, or after Telegram answered that it did nothing. A row that still holds it may have
# reached Telegram, and that call is never made again (runtime.py).
CALL_STARTED = "_call_started"
# 0.9.0 stored no marker and kept only its newest attempt's error, in its client's words before Telegram's
# description. Only these prove that attempt did nothing: Telegram's 400, 401, 403, 404 or 429 to this call,
# or a failure before the call.
_UNMARKED_NO_EFFECT = re.compile(
    r"TelegramApiError: Telegram (?:setMessageReaction failed \((?:400|401|403|404|429)\)"
    r"|(?:getMe|deleteWebhook) failed \(\w+\)): |RuntimeError: telegram token is unavailable\Z")
_PREFLIGHT_NO_EFFECT = (r"TelegramApiError: Telegram (?:getMe|deleteWebhook) failed \(\w+\): "
                        r"|RuntimeError: telegram token is unavailable\Z")
# Keep the earlier text-send upgrade rule; file calls below require proof about the original upload.
_UNRECORDED_TEXT_NO_EFFECT = re.compile(
    r"TelegramApiError: Telegram (?:sendMessage failed \((?:400|401|403|404|429)\): |upload failed \(transport\): )"
    r"|(?:FileNotFoundError|IsADirectoryError|NotADirectoryError|PermissionError): |" + _PREFLIGHT_NO_EFFECT)
_LEGACY_FILE_PREFLIGHT_FAILURE = re.compile(
    _PREFLIGHT_NO_EFFECT + r"|PresenceHost(?:HTTP)?Error: |ValueError: the copy of ")
_LEGACY_FILE_BUILD_FAILURE = re.compile(
    r"Telegram(?:ApiError|RequestNotSent): Telegram upload failed \(transport\): "
    r"|(?:FileNotFoundError|IsADirectoryError|NotADirectoryError|PermissionError): ")
# The rows whose calls are recorded before they are made: text, every file kind (api.MEDIA_METHODS) and albums.
_FILE_SEND_KINDS = frozenset({"photo", "document", "video", "audio", "voice", "animation", "album"})
_SEND_KINDS = _FILE_SEND_KINDS | {"message"}
# An inbox row kept on a Host that cannot hold it as context-only; it is never submitted later.
REACTION_WITHHELD = "reaction_host_context_unavailable"
# The Widgets card lists terminal problems of the last day as attention; older ones are history in the totals.
RECENT_PROBLEM_WINDOW_S = 86400
# A mark 0.13.1 writes with a row's settlement (an inbox row's ``last_stage``, an outbox hold's ``hold_kind``) counts
# only while the row still has the ``available_at`` written with it (``last_stage_due`` / ``hold_kind_due``). After a
# rollback 0.13.0 claims and settles rows without touching the marks, and each of its releases, deferrals and holds
# writes a new available_at: its rows then read as unrecorded (plain waiting, an ordinary hold), never by a stale mark.
_STAGE = "(CASE WHEN last_stage_due=available_at THEN last_stage ELSE '' END)"
_STAGE_AT = "(CASE WHEN last_stage_due=available_at THEN last_stage_at ELSE 0 END)"
_HOLD_KIND = "(CASE WHEN hold_kind_due=available_at THEN hold_kind ELSE '' END)"
# A waiting inbox row whose last attempt failed and is retried by itself. Its stage says where that attempt stopped
# (written by release_inbox): "download" (an attachment from Telegram), "submit" (the handoff to Ouroboros) or ""
# (not recorded: a row from an older version, or a failure after the handoff answered). Not counted: a busy deferral
# ("deferred": defer_inbox gives the attempt back), a lease released at runtime start or by a cancelled worker.
_INBOX_RETRYING = (f"state='pending' AND attempts>0 AND {_STAGE}<>'deferred' "
                   "AND last_error NOT IN ('', 'released_at_runtime_start', 'worker_cancelled')")
INBOX_STAGES = frozenset({"download", "submit"})
# A failed outbox row's outcome is unknown when its payload marks _terminal_outcome uncertain (IFNULL: a row without the
# mark is a plain failure, never NULL in an aggregate).
_UNCERTAIN = "IFNULL(json_extract(payload_json,'$._terminal_outcome'),'')='uncertain'"


def _confirms(key: str) -> str:
    """SQL: the row's stored answers under ``key`` (a list, or one answer) name a message (delivery.names_message).

    Every branch checks a value's JSON type before reading inside it, and CASE (unlike AND/OR) fixes the order: an
    answer stored by any version (an object with a text caption, a bare string, a number, a list of mixed items) is
    read as "names no message", never handed to a JSON function as text, which SQLite refuses as malformed JSON.
    """
    return (f"CASE json_type(payload_json,'$.{key}') "
            f"WHEN 'array' THEN EXISTS(SELECT 1 FROM json_each(payload_json,'$.{key}') AS answer WHERE "
            f"CASE WHEN answer.type='object' THEN json_type(answer.value,'$.message_id')='integer' "
            f"AND json_extract(answer.value,'$.message_id')>0 ELSE 0 END) "
            f"WHEN 'object' THEN json_type(payload_json,'$.{key}.message_id')='integer' "
            f"AND json_extract(payload_json,'$.{key}.message_id')>0 "
            f"ELSE 0 END")


# Some part of the row was confirmed: its stored answers name a message, as delivery.stored_answers reads them
# (_sent_messages first, else _sent_media).
_PART_CONFIRMED = (f"CASE WHEN json_type(payload_json,'$._sent_messages') IS NOT NULL THEN ({_confirms('_sent_messages')}) "
                   f"ELSE ({_confirms('_sent_media')}) END")
# One pass over the failed sends, shared by the totals and the card: lifetime counts per kind, the counts within the
# recent window, and how many of those Telegram confirmed in part. The outcome mark is read once per row; the
# confirmed parts only for rows inside the window.
_FAILED_PASS = (
    "SELECT COUNT(*), IFNULL(SUM(u),0), IFNULL(SUM(r AND u=0),0), IFNULL(SUM(r AND u=1),0), "
    "IFNULL(SUM(r AND u=0 AND p),0), IFNULL(SUM(r AND u=1 AND p),0) FROM ("
    f"SELECT ({_UNCERTAIN}) AS u, IFNULL(failed_at>=?,0) AS r, "
    f"CASE WHEN failed_at>=? THEN ({_PART_CONFIRMED}) ELSE 0 END AS p FROM outbox WHERE state='failed')")
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


def recorded_calls(payload: Dict[str, Any]) -> bool:
    """A send row: text, a file or an album, whose provider calls this version records before making them.

    A row without ``kind`` is a text message. Edits, reactions and moderation keep their own handling unchanged.
    """
    return str(payload.get("kind") or "message") in _SEND_KINDS


def _held_by(attempts: Optional[int]) -> tuple:
    """The WHERE term (and its argument) that keeps an outbox write to the attempt holding the row: the attempts count
    its claim set, as start_outbox_call checks. A send attempt passes it, so after another claim of its row (an
    overlapping reload or an expired lease) its writes change nothing there. Callers that name no attempt (edits,
    moderation, reactions) keep their earlier writes."""
    return ("", ()) if attempts is None else (" AND attempts=?", (int(attempts),))


def _unproven_earlier_attempt(payload: Dict[str, Any], attempts: int, last_error: str, no_effect: "re.Pattern") -> bool:
    """Whether an attempt past this version's stamp may have reached Telegram.

    Attempts past the stamp were another version's, which kept only its newest error: one is judged by that error,
    and more than one cannot be. Decided before a claim clears that error, so no later failure can overwrite it.
    """
    stamp = payload.get(REACTION_ATTEMPTS_MARKED)
    other = attempts - (stamp if type(stamp) is int else 0)
    return not 0 <= other <= 1 or (other == 1 and not no_effect.match(last_error))


def _unproven_file_attempt(payload: Dict[str, Any], attempts: int, last_error: str) -> bool:
    """Whether a file call could have left under an earlier reader, even without its call marker."""
    stamp = payload.get(_FILE_ATTEMPTS_MARKED)
    other = attempts - (stamp if type(stamp) is int else 0)
    if other <= 0:
        return other < 0
    # Even the earlier runtimes persisted resolved reporting before sending. If still unresolved, no upload ran.
    if (payload.get("_reporting") or {}).get("version") == -1:
        return False
    if other != 1:
        return True  # a later preflight failure could have overwritten the only record of an earlier upload
    if _LEGACY_FILE_PREFLIGHT_FAILURE.match(last_error):
        return False
    if not _LEGACY_FILE_BUILD_FAILURE.match(last_error):
        return True  # a refusal string (even a plausible one), or a restart with no marker, is not proof
    # A 0.12.1 body build failure only proves no POST if it could not have occurred on the plain-caption retry.
    # The old client omitted empty captions (including their parse mode); any nonempty HTML could trigger a retry.
    if payload.get("kind") == "album":
        items, queued = payload.get("_rendered_items"), payload.get("items")
        if not isinstance(items, list) or not items or not isinstance(queued, list) or len(items) != len(queued):
            return True
        captions = [item.get("caption") if isinstance(item, dict) else None for item in items]
    else:
        captions = [payload.get("_rendered_caption")]
    return not all(
        isinstance(caption, dict) and isinstance(caption.get("text"), str)
        and (caption.get("parse_mode") == "" or (caption.get("parse_mode") == "HTML" and caption["text"] == ""))
        for caption in captions
    )


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


def _with_album_position(conn: sqlite3.Connection, event: TelegramEvent) -> TelegramEvent:
    """Its 1-based place among the album's members this bot received, which are retained one per committed update.

    Telegram delivers an album's members as separate updates in message order, possibly in different getUpdates
    batches; each is committed with its offset before the next is read, so the earlier members are already here.
    """
    group = str(event.message.get("media_group_id") or "")
    if not group:
        return event
    earlier = conn.execute(
        _ALBUM_EARLIER, (event.account_id, event.conversation_id, group, int(event.message["message_id"])),
    ).fetchone()[0]
    return replace(event, message={**event.message, ALBUM_POSITION_KEY: int(earlier) + 1})


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


# Candidates of the three claims, oldest first. A claim reads its query plainly first and takes the write
# lock only when it found a row; under that lock the same query decides again (see _claim_candidate).
_CLAIMABLE_INBOX = """
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
"""
# Due order: a reference released after its read is due again later
# than every one already waiting, so older work never takes every turn.
_CLAIMABLE_WORK = """
SELECT event_id, binding_id, work_ref, role, event_json, attempts
FROM presence_work
WHERE (state='pending' AND available_at<=?)
   OR (state='leased' AND lease_until<=?)
ORDER BY available_at ASC, created_at ASC
LIMIT 1
"""
_CLAIMABLE_OUTBOX = """
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
"""

# This process's store of each custody file, for as long as something holds it (see CustodyStore.shared).
_SHARED: "weakref.WeakValueDictionary[str, CustodyStore]" = weakref.WeakValueDictionary()
_SHARED_LOCK = threading.Lock()


class CustodyStore:
    """SQLite custody over one kept connection; SQLite owns queue truth across restarts.

    The connection opens on first use and stays open until ``close``; the next use opens it
    again. Callers take turns on it under the store's lock, whatever thread they run on. An
    idle poll therefore opens no file, and a claim takes the write lock only when a row is
    claimable. Every commit is synchronous=FULL: a send marker, a delivered chunk or an
    advanced Telegram offset must survive a power loss, or a message could go out twice in
    someone's name or an acknowledged update be lost.
    """

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._initialize()

    @classmethod
    def shared(cls, path: pathlib.Path) -> "CustodyStore":
        """The store of this file that something in this process already holds, else a new one.

        The runtime, the status route and the tools of one process then share one connection,
        and the file is initialized once per process rather than once per tool call.
        """
        key = os.path.realpath(path)
        with _SHARED_LOCK:
            store = _SHARED.get(key)
            if store is None:
                store = _SHARED[key] = cls(path)
            return store

    def __del__(self) -> None:
        # Short-lived tool callers may drop the store without an explicit stop.
        conn = getattr(self, "_conn", None)
        if conn is not None:
            conn.close()

    def close(self) -> None:
        """Close the kept connection once its current user is done; a later call opens it again."""
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()

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
                    event = _with_album_position(conn, event)
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

    @staticmethod
    def _claim_candidate(conn: sqlite3.Connection, query: str, now: float) -> Optional[sqlite3.Row]:
        """The row a claim may lease, chosen under the write lock; ``None`` when nothing is claimable.

        A plain read answers first, so an idle poll takes no write lock and writes nothing.
        Only when it finds a row does ``BEGIN IMMEDIATE`` start, and the same query decides
        again under it, as every claim always did; the caller's compare-and-set UPDATE stays
        the authority on the lease.
        """
        if conn.execute(query, (now, now)).fetchone() is None:
            return None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(query, (now, now)).fetchone()
        if row is None:
            conn.commit()
        return row

    def claim_inbox(self, *, lease_sec: float = 90.0) -> Optional[InboxLease]:
        now = time.time()
        with self._connect() as conn:
            row = self._claim_candidate(conn, _CLAIMABLE_INBOX, now)
            if row is None:
                return None
            updated = conn.execute(
                """
                UPDATE inbox
                SET state='leased', lease_until=?, attempts=attempts+1, last_error='', last_stage=''
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
        self, event_id: str, *, reason: str, retry_after_sec: float, stage: str = ""
    ) -> None:
        """Return a leased event to its queue; ``stage`` (INBOX_STAGES or "") says where its attempt stopped, and is
        stamped with this attempt's end and the available_at it sets (_STAGE)."""
        now = time.time()
        due = now + max(0.0, retry_after_sec)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbox
                SET state='pending', available_at=?, lease_until=0, last_error=?, last_stage=?,
                    last_stage_at=?, last_stage_due=?
                WHERE event_id=? AND state='leased'
                """,
                (due, str(reason)[:500], stage if stage in INBOX_STAGES else "", now, due, event_id),
            )

    def defer_inbox(
        self, event_id: str, *, reason: str, retry_after_sec: float
    ) -> None:
        """Keep a busy-refused event first in its conversation; the attempt is not counted.

        The Host admitted nothing, so this is neither custody nor a failure: the same
        event retries after the Host's hint, while other conversations proceed.
        """
        now = time.time()
        due = now + max(0.0, retry_after_sec)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbox
                SET state='pending', available_at=?, lease_until=0, last_error=?, last_stage='deferred',
                    last_stage_at=?, last_stage_due=?, attempts=MAX(0, attempts-1)
                WHERE event_id=? AND state='leased'
                """,
                (due, str(reason)[:500], now, due, event_id),
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
        one has stopped, this can release a live lease -- the same outcome as a
        lease expiring, only sooner. An event may then be submitted to the Host
        again and an edit or moderation repeated; a send or reaction whose call
        may have started ends uncertain instead (runtime.py).
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

    def checkpoint_inbox(self, event_id: str, payload: Dict[str, Any]) -> None:
        """Keep a leased event's decided attachment outcomes, so every later attempt submits the same record."""
        with self._connect() as conn:
            conn.execute("UPDATE inbox SET payload_json=? WHERE event_id=? AND state='leased'", (_json(payload), event_id))

    def download_failure(self, event_id: str, ordinal: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM download_failures WHERE event_id=? AND ordinal=?",
                               (event_id, int(ordinal))).fetchone()
        return dict(row) if row is not None else None

    def note_download_failure(self, event_id: str, ordinal: int, *, error: str, counted: bool,
                              now: float) -> Dict[str, Any]:
        """One more failed download of this attachment; ``counted`` when it says something about the file."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO download_failures(event_id, ordinal, own_failures, first_failed_at, last_failed_at,
                                              last_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id, ordinal) DO UPDATE SET
                    own_failures=own_failures + excluded.own_failures,
                    first_failed_at=CASE WHEN first_failed_at=0 THEN excluded.first_failed_at
                                         ELSE first_failed_at END,
                    last_failed_at=excluded.last_failed_at, last_error=excluded.last_error
                """,
                (event_id, int(ordinal), int(counted), now if counted else 0.0, now, str(error)[:500]),
            )
            return dict(conn.execute("SELECT * FROM download_failures WHERE event_id=? AND ordinal=?",
                                     (event_id, int(ordinal))).fetchone())

    def forget_download_failures(self, event_id: str) -> None:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM download_failures WHERE event_id=? LIMIT 1", (event_id,)).fetchone():
                conn.execute("DELETE FROM download_failures WHERE event_id=?", (event_id,))

    def request_typing(self, *, chat_id: str, topic_id: str, turn_ref: str, source_event_id: str) -> None:
        """Ask the runtime to show "typing…" for this turn; a repeated call renews its two-minute window."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO typing_requests(chat_id, topic_id, turn_ref, source_event_id, requested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(chat_id), str(topic_id or ""), str(turn_ref), str(source_event_id or ""), time.time()),
            )

    def typing_requests(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM typing_requests ORDER BY requested_at")]

    def forget_typing(self, rows: Sequence[Dict[str, Any]]) -> None:
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM typing_requests WHERE chat_id=? AND topic_id=? AND turn_ref=? AND requested_at=?",
                [(row["chat_id"], row["topic_id"], row["turn_ref"], row["requested_at"]) for row in rows],
            )

    def clear_typing(self) -> None:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM typing_requests LIMIT 1").fetchone():
                conn.execute("DELETE FROM typing_requests")

    def turn_live(self, source_event_id: str, turn_ref: str) -> bool:
        """Whether that turn may still answer, by this transport's own records of it.

        Its ``/presence/turn`` request is still open (the event's inbox row is leased), or the Host's version-2
        continuation or a work reference named ``turn_ref`` is still observed.
        """
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM inbox WHERE event_id=? AND state='leased' UNION ALL "
                "SELECT 1 FROM presence_work WHERE work_ref=? AND state IN ('pending','leased') LIMIT 1",
                (str(source_event_id or ""), str(turn_ref or "")),
            ).fetchone() is not None

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
            row = self._claim_candidate(conn, _CLAIMABLE_WORK, now)
            if row is None:
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
            row = self._claim_candidate(conn, _CLAIMABLE_OUTBOX, now)
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            unanswered = row["state"] == "leased" or row["last_error"] in _UNANSWERED_ATTEMPT
            unrecorded = False
            marked = None
            if _unmarked_reaction(payload):
                unanswered = unanswered or _unproven_earlier_attempt(
                    payload, row["attempts"], row["last_error"], _UNMARKED_NO_EFFECT)
                if not unanswered:
                    payload = {**payload, REACTION_ATTEMPTS_MARKED: row["attempts"] + 1}
                    marked = _json(payload)
            elif recorded_calls(payload) and CALL_STARTED not in payload:
                # A missing marker settles only attempts whose reader used the applicable record protocol.
                # Do not stamp an uncertain prior attempt: a crash before settlement must not authorize a retry.
                file_send = payload.get("kind") in _FILE_SEND_KINDS
                if file_send:
                    unrecorded = _unproven_file_attempt(payload, row["attempts"], row["last_error"])
                else:
                    unrecorded = _unproven_earlier_attempt(
                        payload, row["attempts"], row["last_error"], _UNRECORDED_TEXT_NO_EFFECT)
                if not unrecorded:
                    payload = {**payload, REACTION_ATTEMPTS_MARKED: row["attempts"] + 1}
                    if file_send:
                        payload[_FILE_ATTEMPTS_MARKED] = row["attempts"] + 1
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
                unrecorded_attempt=unrecorded,
            )

    def release_outbox(
        self, delivery_id: str, *, reason: str, retry_after_sec: float, attempts: Optional[int] = None
    ) -> bool:
        """Return a leased row to its queue; with ``attempts``, only while that attempt holds it (_held_by)."""
        held, args = _held_by(attempts)
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE outbox
                SET state='pending', available_at=?, lease_until=0, last_error=?
                WHERE delivery_id=? AND state='leased'
                """ + held,
                (
                    time.time() + max(0.0, retry_after_sec),
                    str(reason)[:500],
                    delivery_id,
                    *args,
                ),
            )
            return updated.rowcount == 1

    def hold_outbox(self, delivery_id: str, reason: str, *, attempts: Optional[int] = None, kind: str = "") -> bool:
        # A hold returns its attempt to the count; stamps for that attempt move with it. ``kind`` names the Host
        # answer type of a hold only the owner can end (host.OWNER_HOLD_KINDS), "" otherwise.
        held, args = _held_by(attempts)
        due = time.time() + 5
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE outbox SET state='pending',available_at=?,lease_until=0,hold_kind=?,hold_kind_due=?,"
                "last_error=?,attempts=MAX(0,attempts-1),payload_json=CASE "
                "WHEN json_extract(payload_json,?)=attempts "
                "THEN json_set(payload_json,?,MAX(0,attempts-1),?,MAX(0,attempts-1)) "
                "WHEN json_extract(payload_json,?)=attempts THEN json_set(payload_json,?,MAX(0,attempts-1)) "
                "ELSE payload_json END WHERE delivery_id=? AND state='leased'" + held,
                (due, str(kind or ""), due, reason, _FILE_STAMP_PATH, _FILE_STAMP_PATH, _STAMP_PATH,
                 _STAMP_PATH, _STAMP_PATH, delivery_id, *args))
            return updated.rowcount == 1

    def mark_outbox_failed(self, delivery_id: str, *, reason: str, report=None,
                           state: str = "failed", attempts: Optional[int] = None) -> bool:
        if state not in {"failed", "uncertain"}:
            raise ValueError("terminal outbox state must be failed or uncertain")
        held, args = _held_by(attempts)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_json FROM outbox WHERE delivery_id=? AND state='leased'" + held,
                               (delivery_id, *args)).fetchone() if state == "uncertain" else None
            payload = json.loads(row[0]) if row is not None else None
            if payload is not None:
                payload["_terminal_outcome"] = "uncertain"
            updated = conn.execute(
                """
                UPDATE outbox
                SET state='failed', lease_until=0, failed_at=?, last_error=?,
                    payload_json=COALESCE(?,payload_json)
                WHERE delivery_id=? AND state='leased'
                """ + held,
                (time.time(), str(reason)[:500], _json(payload) if payload is not None else None,
                 delivery_id, *args),
            )
            if attempts is not None and updated.rowcount != 1:
                conn.rollback()  # another claim holds the row: its record and history stay as they are
                return False
            self._add_reports(conn, delivery_id, [report] if report else [])
            conn.commit()
            return updated.rowcount == 1

    def start_outbox_call(self, delivery_id: str, attempts: int, payload: Dict[str, Any]) -> bool:
        """Store a send row's record with its CALL_STARTED marker before the call it names; ``False``, with nothing
        stored, when this attempt no longer holds the lease (the row was released or claimed again)."""
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE outbox SET payload_json=? WHERE delivery_id=? AND state='leased' AND attempts=?",
                (_json(payload), delivery_id, int(attempts)),
            )
            return updated.rowcount == 1

    def record_transfer(self, delivery_id: str, attempts: int, transfer: Dict[str, Any], *, lease_sec: float) -> bool:
        """Keep a send attempt's newest observation of its file request, and its lease at least ``lease_sec`` more.

        Only while that attempt holds the row, as start_outbox_call: once the row was released, ended or claimed again,
        nothing changes (``False``). It is a separate column, so the row's record, answers and state never move with
        it, and it stays as last written after the row ends, for the receipt.
        """
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE outbox SET transfer_json=?, lease_until=MAX(lease_until, ?) "
                "WHERE delivery_id=? AND state='leased' AND attempts=?",
                (_json(transfer), time.time() + max(0.0, lease_sec), delivery_id, int(attempts)),
            )
            return updated.rowcount == 1

    def checkpoint_outbox(self, delivery_id: str, payload: Dict[str, Any], *, report=None,
                          sent: Optional[Sequence[Any]] = None, attempts: Optional[int] = None) -> bool:
        """Retain confirmed text chunks before attempting the next provider call.

        ``sent`` remembers a just-confirmed own message: its exact creation receipt is what
        later identifies a reaction on it as a reaction on this bot's words. An album confirms
        several at once: then ``report`` and ``sent`` are lists, one entry per message. With
        ``attempts`` nothing at all is stored unless that attempt holds the row (``False``).
        """
        reports = report if isinstance(report, list) else [report] if report else []
        rows = sent if isinstance(sent, list) else [sent] if sent else []
        held, args = _held_by(attempts)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE outbox SET payload_json=? WHERE delivery_id=? AND state='leased'" + held,
                (_json(payload), delivery_id, *args),
            )
            if attempts is not None and updated.rowcount != 1:
                conn.rollback()
                return False
            for row in rows:
                if row:
                    conn.execute(_REMEMBER_MESSAGE, row)
            self._add_reports(conn, delivery_id, [entry for entry in reports if entry])
            conn.commit()
            return updated.rowcount == 1

    def mark_delivered(
        self, delivery_id: str, *, provider_receipt: Dict[str, Any], report=None, attempts: Optional[int] = None
    ) -> bool:
        held, args = _held_by(attempts)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE outbox
                SET state='delivered', lease_until=0, delivered_at=?,
                    provider_receipt_json=?, last_error=''
                WHERE delivery_id=? AND state='leased'
                """ + held,
                (time.time(), _json(provider_receipt), delivery_id, *args),
            )
            if attempts is not None and updated.rowcount != 1:
                conn.rollback()
                return False
            self._add_reports(conn, delivery_id, [report] if report else [])
            conn.commit()
            return updated.rowcount == 1

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
                "SELECT state, attempts, created_at, delivered_at, provider_receipt_json, last_error, payload_json, "
                "reports_json, transfer_json FROM outbox WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            return {"state": "not_found"}
        payload = json.loads(row["payload_json"])
        reaction = payload.get("kind") == "operation" and payload.get("method") == "setMessageReaction"
        reporting = payload.get("_reporting") or {}
        # Until a row is delivered its provider receipt is empty; the messages Telegram confirmed so far, such as an
        # accepted album's that ended uncertain, are in this same record. An answer stored without its message id (an
        # earlier version kept any ok) is shown apart, as stored, and confirms nothing.
        answers = stored_answers(payload)
        # The bytes a file send was queued with (0.12.1); a row queued earlier names only its path, unproven.
        files = [{"file_name": pathlib.PurePath(str(item["path"])).name, "source_path": str(item.get("source") or ""),
                  "copy_path": str(item["path"]), "sha256": str(item.get("sha256") or ""),
                  "size_bytes": item.get("size")} for item in outbox_files.records(payload)]
        return {
            # -1: the outbox has not yet asked this Host.
            **({"history_limitation": "Host history for reactions is unavailable: this receipt stays local."}
               if reaction and (reporting.get("version") != -1 or row["state"] != "pending")
               and reporting.get("explicit_completion") != 1
               and not any(entry["acked"] for entry in json.loads(row["reports_json"])) else {}),
            "state": "held" if row["state"] == "pending" and str(row["last_error"]).startswith("Held:") else "uncertain" if payload.get("_terminal_outcome") == "uncertain" else str(row["state"]),
            "attempts": int(row["attempts"]),
            "queued_at": _timestamp(row["created_at"]),
            "delivered_at": _timestamp(row["delivered_at"]),
            "provider_receipt": json.loads(row["provider_receipt_json"] or "{}"),
            **({"confirmed_messages": answers[0], **({"answers_without_message_id": answers[1]} if answers[1] else {})}
               if answers is not None and row["state"] != "delivered" else {}),
            **({"files": files} if files else {}),
            **_transfer_receipt(row["transfer_json"]),
            "error": str(row["last_error"]),
            "delivery_reporting": payload.get("_reporting", {"version": 0, "status": "legacy_unconfirmed"}),
            "history_reports": json.loads(row["reports_json"]),
        }

    def offered_groups(self, account_id: str) -> List[Dict[str, str]]:
        """Connect choices of one bot; reads its chat projection, never message history."""
        with self._connect() as conn:
            return discovery.offered_groups(conn, account_id)

    def card_snapshot(self, now: Optional[float] = None, window_s: float = RECENT_PROBLEM_WINDOW_S) -> Dict[str, Any]:
        """Additive status facts for the Widgets card: recent terminal problems with their time, and every hold reason.

        Lifetime totals stay in status_snapshot(); these count only rows that ended within ``window_s``. An
        uncertain send is a failed row whose payload marks ``_terminal_outcome`` uncertain (as status_snapshot
        counts it); a withheld reaction is not an inbound failure. ``handoff_retrying`` counts waiting updates whose
        last attempt failed, by where it stopped (download, handoff, not recorded; see _INBOX_RETRYING).
        """
        with self._connect() as conn:
            return self._card_facts(conn, time.time() if now is None else float(now), window_s)

    def status_snapshot(self) -> Dict[str, Any]:
        with self._connect() as conn:
            return self._status_counts(conn)

    def read_status(self, now: Optional[float] = None,
                    window_s: float = RECENT_PROBLEM_WINDOW_S) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """``(status_snapshot(), card_snapshot())`` read in ONE SQLite read transaction: one consistent moment.

        Read separately, a send finishing between the two reads could be counted as sending in one and as failed
        in the other.
        """
        now = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN")  # deferred: the first SELECT fixes the snapshot every later SELECT reads
            try:
                shared = self._shared_facts(conn, now - float(window_s))  # read once, used by both groups
                return self._status_counts(conn, shared), self._card_facts(conn, now, window_s, shared)
            finally:
                conn.rollback()  # nothing was written

    @staticmethod
    def _shared_facts(conn: sqlite3.Connection, since: float) -> Dict[str, Any]:
        """The passes the totals and the card both need, read once by read_status():

        ``failed`` (_FAILED_PASS): failed rows, of them uncertain, recent failed, recent uncertain, and recent failed /
        uncertain confirmed in part; ``inbox_failed``: refused rows that are withheld reactions, refused rows (not
        withheld) within the window; ``held``: each hold note with its count and kind.
        """
        failed = tuple(int(value) for value in conn.execute(_FAILED_PASS, (since, since)).fetchone())
        inbox_failed = tuple(int(value) for value in conn.execute(
            "SELECT IFNULL(SUM(last_error=?),0), IFNULL(SUM(last_error<>? AND failed_at>=?),0) FROM inbox "
            "WHERE state='failed'", (REACTION_WITHHELD, REACTION_WITHHELD, since)).fetchone())
        # Each reason once, with the Host answer type it came from (hold_kind; a row held before 0.13.1 kept none).
        held = conn.execute(
            f"SELECT last_error, COUNT(*), MAX({_HOLD_KIND}) FROM outbox WHERE state='pending' AND last_error LIKE 'Held:%' "
            "GROUP BY last_error ORDER BY COUNT(*) DESC, last_error").fetchall()
        return {"failed": failed, "inbox_failed": inbox_failed, "held": held}

    @classmethod
    def _card_facts(cls, conn: sqlite3.Connection, now: float, window_s: float,
                    shared: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        since = now - float(window_s)
        shared = cls._shared_facts(conn, since) if shared is None else shared
        failed, held = shared["failed"], shared["held"]
        def one(sql: str, params: tuple = ()) -> Any:
            return conn.execute(sql, params).fetchone()

        # Each kind's newest row gives its last time and error. outbox_failed_idx walks the failed rows newest first,
        # so each query reads the outcome mark only until its first match; a kind the shared pass counted none of
        # (same snapshot) is not looked for, since that walk would read every failed row to find nothing.
        newest_failed = one(f"SELECT failed_at, last_error, rowid FROM outbox WHERE state='failed' AND NOT ({_UNCERTAIN}) "
                            "ORDER BY failed_at DESC, rowid DESC LIMIT 1") if failed[0] - failed[1] else None
        newest_uncertain = one(f"SELECT failed_at, last_error, rowid FROM outbox WHERE state='failed' AND {_UNCERTAIN} "
                               "ORDER BY failed_at DESC, rowid DESC LIMIT 1") if failed[1] else None
        # The newest failed send of either kind, by the same order the two queries use.
        newest_send = max((row for row in (newest_failed, newest_uncertain) if row is not None),
                          key=lambda row: (row["failed_at"] or 0, row["rowid"]), default=None)
        newest_in = one("SELECT failed_at, last_error FROM inbox WHERE state='failed' AND last_error<>? "
                        "ORDER BY failed_at DESC, update_id DESC LIMIT 1", (REACTION_WITHHELD,))
        # Waiting updates whose last attempt failed, by where it stopped (_INBOX_RETRYING). The error shown is the
        # latest failed attempt's (by when it ended; rows whose end was not recorded come after, newest update first).
        handoff = one(
            f"SELECT COUNT(*), MIN(received_at), MIN(available_at), SUM({_STAGE}='submit'), "
            f"SUM({_STAGE}='download'), SUM({_STAGE_AT}=0) FROM inbox WHERE {_INBOX_RETRYING}")
        newest_wait = one(f"SELECT last_error, {_STAGE} AS stage, {_STAGE_AT} AS ended FROM inbox "
                          f"WHERE {_INBOX_RETRYING} ORDER BY ended DESC, update_id DESC LIMIT 1")
        return {
            "recent_problems": {
                "window_seconds": int(window_s),
                "outbound": {
                    "failed": failed[2], "uncertain": failed[3],
                    # Of those, how many Telegram confirmed in part (the receipt's confirmed_messages lists them).
                    "failed_partly_confirmed": failed[4], "uncertain_partly_confirmed": failed[5],
                    "last_failed_at": _timestamp(newest_failed["failed_at"] if newest_failed else None),
                    "last_uncertain_at": _timestamp(newest_uncertain["failed_at"] if newest_uncertain else None),
                    "last_error": ({"at": _timestamp(newest_send["failed_at"]),
                                    "state": "uncertain" if newest_send is newest_uncertain else "failed",
                                    "text": str(newest_send["last_error"] or "")} if newest_send else None),
                    # Each kind's own newest error, so a newer unknown outcome never hides the last refusal.
                    "last_failed_error": _card_error(newest_failed, "failed"),
                    "last_uncertain_error": _card_error(newest_uncertain, "uncertain"),
                },
                "inbound": {
                    "failed": shared["inbox_failed"][1],
                    "last_failed_at": _timestamp(newest_in["failed_at"] if newest_in else None),
                    "last_error": ({"at": _timestamp(newest_in["failed_at"]), "state": "failed",
                                    "text": str(newest_in["last_error"] or "")} if newest_in else None),
                },
            },
            "handoff_retrying": {
                "count": int(handoff[0]),
                # Where the attempts stopped: passing to Ouroboros, downloading from Telegram, or not recorded.
                "submit": int(handoff[3] or 0), "download": int(handoff[4] or 0),
                "other": int(handoff[0]) - int(handoff[3] or 0) - int(handoff[4] or 0),
                "since": _timestamp(handoff[1]) if handoff[0] else "",
                "next_attempt_at": _timestamp(handoff[2]) if handoff[0] else "",
                "last_error": str(newest_wait["last_error"] or "") if newest_wait else "",
                "last_stage": str(newest_wait["stage"] or "") if newest_wait else "",
                # When that attempt ended, where recorded ("" for a row whose end was not recorded).
                "last_error_at": _timestamp(newest_wait["ended"]) if newest_wait else "",
                # Whether that note's attempt has a recorded end: a row 0.13.0 released or deferred after a rollback
                # has none, and its note may be a busy deferral's, so nothing shows that its last attempt failed.
                "last_error_recorded": bool(newest_wait and newest_wait["ended"]),
                # Whether that error is the latest: only when its own end is recorded and so is every other waiting
                # update's; else it is an example (or, unrecorded itself, a note an earlier version left).
                "last_error_newest": bool(newest_wait and newest_wait["ended"]) and not int(handoff[5] or 0),
            },
            "held_reasons": [{"reason": str(row[0])[len("Held:"):].strip(), "note": str(row[0]), "count": int(row[1]),
                              **({"kind": str(row[2])} if row[2] else {})}
                             for row in held],
        }

    def _status_counts(self, conn: sqlite3.Connection, shared: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        shared = self._shared_facts(conn, time.time()) if shared is None else shared
        failed, withheld = shared["failed"], shared["inbox_failed"][0]
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
            "inbox_failed": inbox.get("failed", 0) - withheld,
            "reactions_withheld": withheld,
            "outbox_waiting": outbox.get("pending", 0),
            # The same held rows the card groups by reason (_shared_facts).
            "outbox_held": sum(int(row[1]) for row in shared["held"]),
            "outbox_leased": outbox.get("leased", 0),
            "outbox_delivered": outbox.get("delivered", 0),
            # The shared pass over the failed rows (_FAILED_PASS): all of them, and those whose outcome is unknown.
            "outbox_failed": failed[0] - failed[1],
            "outbox_uncertain": failed[1],
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
                + ";"
                + _MESSAGES_ALBUM_INDEX
                + ";"
                + _DOWNLOAD_FAILURES_TABLE
                + ";"
                + _TYPING_TABLE
            )
            conn.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
            if "reports_json" not in columns:
                conn.execute("ALTER TABLE outbox ADD COLUMN reports_json TEXT NOT NULL DEFAULT '[]'")
                conn.execute("ALTER TABLE outbox ADD COLUMN report_due_at REAL NOT NULL DEFAULT 0")
            if "transfer_json" not in columns:
                # A file request's last observation (record_transfer). No checkpoint or end of the row writes it.
                conn.execute("ALTER TABLE outbox ADD COLUMN transfer_json TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS outbox_reports_due ON outbox(report_due_at)")
            inbox_columns = {r[1] for r in conn.execute("PRAGMA table_info(inbox)")}
            if "host_receipt_json" not in inbox_columns:
                conn.execute("ALTER TABLE inbox ADD COLUMN host_receipt_json TEXT NOT NULL DEFAULT ''")
            # 0.13.1: the status reads each kind's newest failed send by walking this index from the newest end,
            # instead of reading the outcome mark of every failed row (measured: perf_status_read.py).
            conn.execute("CREATE INDEX IF NOT EXISTS outbox_failed_idx ON outbox(state, failed_at)")
            # 0.13.1 marks and their stamps (see _STAGE): additive, with defaults, so 0.13.0's inserts still work.
            outbox_columns = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
            for column, kind in (("hold_kind", "TEXT NOT NULL DEFAULT ''"), ("hold_kind_due", "REAL NOT NULL DEFAULT 0")):
                if column not in outbox_columns:
                    conn.execute(f"ALTER TABLE outbox ADD COLUMN {column} {kind}")
            for column, kind in (("last_stage", "TEXT NOT NULL DEFAULT ''"), ("last_stage_at", "REAL NOT NULL DEFAULT 0"),
                                 ("last_stage_due", "REAL NOT NULL DEFAULT 0")):
                if column not in inbox_columns:
                    conn.execute(f"ALTER TABLE inbox ADD COLUMN {column} {kind}")
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
        """The kept connection, for one caller at a time.

        A transaction its caller left unfinished, by an error, is rolled back on the way out,
        as closing a per-call connection did.
        """
        with self._lock:
            if self._conn is None:
                self._conn = self._open()
            conn = self._conn
            try:
                yield conn
            finally:
                if conn.in_transaction:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        # A connection that cannot roll back is not reused.
                        self._conn = None
                        conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            # Set here, never inherited: synchronous is per connection, and this one makes every commit.
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()
            raise
        return conn

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


def _card_error(row: Any, state: str) -> Optional[Dict[str, Any]]:
    return {"at": _timestamp(row["failed_at"]), "state": state, "text": str(row["last_error"] or "")} if row else None


def _timestamp(value: Any) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))


# The receipt's ``transfer`` fields; updated_at marks observed progress, ended_at an observed end of the request.
_TRANSFER_FIELDS = ("attempt", "request", "phase", "file_bytes_written", "file_bytes_total", "started_at", "updated_at",
                    "ended_at", "answer", "error_code")
_TRANSFER_TIMES = frozenset({"started_at", "updated_at", "ended_at"})


def _transfer_receipt(stored: Any) -> Dict[str, Any]:
    """``{"transfer": ...}`` when this version observed a file request of the row, else nothing: no stage is inferred."""
    try:
        record = json.loads(stored) if stored else None
    except (TypeError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return {"transfer": {key: _timestamp(record[key]) if key in _TRANSFER_TIMES else record[key]
                         for key in _TRANSFER_FIELDS if key in record}}


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
