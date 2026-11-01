"""Groups the bot has been seen in, offered as the owner's Connect choices.

Each group or supergroup update leaves its chat id, type and title and the bot's
newest membership evidence beside custody, in the transaction that advances the
provider offset. Nothing here creates an inbox event, a turn or a place: the
owner connects each group. A listed group is an observation, not a live
membership check.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Dict, List, Optional, Sequence, Tuple

from .events import _integer


_ACCOUNT = re.compile(r"[1-9][0-9]*")
_GROUP_TYPES = ("group", "supergroup")
_BACKFILL_KEY = "group_backfill"
_BACKFILL_BATCH = 200

GROUPS_TABLE = """
CREATE TABLE IF NOT EXISTS telegram_groups(
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    title TEXT NOT NULL,
    title_update_id INTEGER NOT NULL,
    membership TEXT NOT NULL CHECK(membership IN ('present','left','kicked','migrated')),
    membership_update_id INTEGER NOT NULL,
    PRIMARY KEY(account_id, chat_id)
)"""

# Metadata and membership each keep the evidence of the newest update, so an
# older message, a replay or a legacy row never undoes a removal, a migration or
# a rename, in whatever order they are recorded.
_RECORD = """
INSERT INTO telegram_groups(
    account_id, chat_id, chat_type, title, title_update_id, membership, membership_update_id
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(account_id, chat_id) DO UPDATE SET
    chat_type=CASE WHEN excluded.title_update_id>title_update_id
                   THEN excluded.chat_type ELSE chat_type END,
    title=CASE WHEN excluded.title_update_id>title_update_id THEN excluded.title ELSE title END,
    title_update_id=MAX(title_update_id, excluded.title_update_id),
    membership=CASE WHEN excluded.membership_update_id>membership_update_id
                    THEN excluded.membership ELSE membership END,
    membership_update_id=MAX(membership_update_id, excluded.membership_update_id)
"""


@dataclass(frozen=True)
class GroupSighting:
    account_id: str
    chat_id: str
    membership: str  # present | left | kicked | migrated
    chat_type: str = "group"
    title: Optional[str] = None  # None: the update names this chat without its metadata


def group_sightings(update: Any, account_id: str) -> Tuple[GroupSighting, ...]:
    """What one raw update shows about the groups of the bot ``account_id``.

    A message, edit, service message or reaction from a group shows the bot there
    when Telegram generated the update. The bot's own ``my_chat_member`` status,
    or a service message naming it as the member who left, records a removal. A
    group upgraded to a supergroup is retired by ``migrate_to_chat_id``; the
    supergroup is observed under its new id from its own updates, which carry its
    type and title. Private chats and channels are not groups.
    """
    if not isinstance(update, dict) or not _ACCOUNT.fullmatch(str(account_id)):
        return ()
    change = update.get("my_chat_member")
    if isinstance(change, dict):
        member = _object(change.get("new_chat_member"))
        if _user_id(member.get("user")) != account_id:
            return ()
        status = member.get("status")
        if status in ("creator", "administrator", "member") or (
            status == "restricted" and member.get("is_member") is True
        ):
            return _sighting(account_id, change.get("chat"), "present")
        if status in ("left", "kicked", "restricted"):
            return _sighting(account_id, change.get("chat"), "kicked" if status == "kicked" else "left")
        return ()
    message = next((update[kind] for kind in ("message", "edited_message")
                    if isinstance(update.get(kind), dict)), None)
    if message is None:
        reaction = update.get("message_reaction") or update.get("message_reaction_count")
        return _sighting(account_id, _object(reaction).get("chat"), "present")
    chat = message.get("chat")
    if _user_id(message.get("left_chat_member")) == account_id:
        return _sighting(account_id, chat, "left")
    if _integer(message.get("migrate_to_chat_id")) is not None:
        return _sighting(account_id, chat, "migrated")
    seen = _sighting(account_id, chat, "present")
    old = _integer(message.get("migrate_from_chat_id"))
    if seen and old is not None and str(old) != seen[0].chat_id:
        seen += (GroupSighting(account_id, str(old), "migrated"),)
    return seen


def record_sightings(conn: sqlite3.Connection, update_id: int, sightings: Sequence[GroupSighting]) -> None:
    """Keep the newest evidence of each sighting, inside the caller's transaction."""
    for item in sightings:
        conn.execute(_RECORD, (
            item.account_id, item.chat_id, item.chat_type, item.title or "",
            int(update_id) if item.title is not None else -1, item.membership, int(update_id),
        ))


def offered_groups(conn: sqlite3.Connection, account_id: str) -> List[Dict[str, str]]:
    """Connect rows for groups whose newest evidence shows this bot present, by title.

    Every such group is listed. The label carries the numeric id beside the
    title, so groups with the same title stay distinguishable.
    """
    rows = conn.execute(
        "SELECT chat_id, title FROM telegram_groups WHERE account_id=? AND membership='present'",
        (account_id,),
    ).fetchall()
    return [
        {"account_id": account_id, "conversation_id": chat_id,
         "label": f"{' '.join(title.split()) or 'Group'} ({chat_id})"}
        for chat_id, title in sorted(((str(row[0]), str(row[1])) for row in rows),
                                     key=lambda row: (row[1].casefold(), row[0]))
    ]


def backfill_legacy(connect: Callable[[], ContextManager[sqlite3.Connection]]) -> None:
    """Observe the groups of inbox rows captured before this projection, once.

    The inbox is read in short batches; each commits its sightings with its
    progress, so an interrupted backfill resumes and a finished one never runs
    again. Inbox, work and outbox rows, leases, receipts and the offset are only
    read. A row whose origin cannot be read is counted, never attributed to a
    guessed or the current bot.
    """
    with connect() as conn:
        if _progress(conn)["done"]:
            return
    while True:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            progress = _progress(conn)
            if progress["done"]:
                conn.rollback()
                return
            rows = conn.execute(
                "SELECT update_id, payload_json FROM inbox WHERE update_id>? ORDER BY update_id LIMIT ?",
                (progress["after"], _BACKFILL_BATCH),
            ).fetchall()
            for update_id, payload in rows:
                sightings = _captured_group(payload)
                if sightings is None:
                    progress["unreadable"] += 1
                else:
                    record_sightings(conn, update_id, sightings)
            if rows:
                progress["after"] = int(rows[-1][0])
            progress["done"] = len(rows) < _BACKFILL_BATCH
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_BACKFILL_KEY, json.dumps(progress, sort_keys=True)),
            )
            conn.commit()
        if progress["done"]:
            return


def backfill_unreadable(conn: sqlite3.Connection) -> int:
    """Inbox rows the legacy backfill could not attribute to a bot and a chat."""
    return _progress(conn)["unreadable"]


def _captured_group(payload: Any) -> Optional[Tuple[GroupSighting, ...]]:
    """The group a saved inbox event came from; ``None`` when its origin is unreadable."""
    try:
        event = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        return None
    conversation = _object(event.get("conversation") if isinstance(event, dict) else None)
    account = str(event.get("account_id") or "") if conversation else ""
    chat_id = _integer(event.get("conversation_id")) if conversation else None
    chat_type = conversation.get("chat_type")
    if (not _ACCOUNT.fullmatch(account) or chat_id is None
            or str(conversation.get("chat_id")) != str(chat_id)
            or chat_type not in ("private", "channel", *_GROUP_TYPES)):
        return None
    if chat_type not in _GROUP_TYPES:
        return ()
    title = conversation.get("title")
    return (GroupSighting(account, str(chat_id), "present", chat_type, title if isinstance(title, str) else ""),)


def _progress(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (_BACKFILL_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row is not None else {}
        return {"after": int(value["after"]), "unreadable": int(value["unreadable"]),
                "done": value["done"] is True}
    except (KeyError, TypeError, ValueError):
        return {"after": -1, "unreadable": 0, "done": False}


def _sighting(account_id: str, chat: Any, membership: str) -> Tuple[GroupSighting, ...]:
    chat = _object(chat)
    chat_id, title = _integer(chat.get("id")), chat.get("title")
    if chat.get("type") not in _GROUP_TYPES or chat_id is None:
        return ()
    return (GroupSighting(account_id, str(chat_id), membership, chat["type"],
                          title if isinstance(title, str) else ""),)


def _object(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _user_id(value: Any) -> str:
    return str(value.get("id")) if isinstance(value, dict) else ""
