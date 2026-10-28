"""Private Telegram chats as Places: durable custody, nonblocking admission, receipts.

Lane. The owner chooses it (``TELEGRAM_ORDINARY_CHAT``): ``main``, the default, keeps
every ordinary owner message on the classic Main relay with no Host lookup at all, as
before places existed. With ``place``, an ordinary private message (not a slash
command, not a quiz answer) is a Place event when the Host resolves a connected place
for its exact chat
(``GET /presence/place`` 200, any state). Only the Host's exact ``404
place_not_connected`` keeps the classic owner relay (``/chat/inject``) for the
owner and leaves anyone else unanswered, as before. Every other answer, an
authentication refusal included, keeps the message here undecided and asks
again: an uncertain or refused lookup never sends it to Main. Once a message
entered the Place lane it never leaves it; a later "not connected" holds it.

Custody. The poller commits the provider message (with its Telegram file ids,
which are durable provider references, not bytes) before the offset moves; the
runner, a separate supervised task, owns everything after. Bytes are downloaded
once, kept with their digest and reused for every later request until the Host
has answered; a file Telegram will not serve is admitted as an explicit
``unavailable`` fact, never as a staged file. The runner never awaits a turn per
chat: the next message of a chat is sent as soon as the Host has observably
captured the previous one (its inbound room row, written under the Host's intake
lock before any attention decision) or answered it, so the Host can hand it to a
live author as context mail. No timer stands in for that fact. A request that
ends without an answer is posted again with the same frozen event, which joins
the same turn; ``retained``/``continuing``/``deferred`` answers release the
message and their references are observed through ``/presence/work``.

Delivery. Each selected output is one outbox row keyed by its canonical
``output_ref``; an output without one is recorded as a gap and never sent. Chunks
are frozen when queued. Before every part the place is read again: Silent,
Paused, disconnected or unreadable holds the bytes without spending retries. A
part is marked in flight before it is sent; one found in flight after a restart,
one whose send may have reached Telegram, or one Telegram confirmed without a
message id is ``uncertain``: reported, never resent. Receipts go to
``/presence/delivery`` from their own queue, so a slow report never holds a send.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from .telegram_api import (
    TelegramRequestRejected,
    TelegramTransportError,
    _telegram_html_to_plain,
    markdown_to_telegram_chunks,
)
from .telegram_state import _message_ref, _read_json_file, _remember_message, _reply_reference, _state_file

PROVIDER = "telegram"
MAX_INFLIGHT_TURNS = 4    # the Host's own per-skill presence budget answers 429 beyond its limit
TURN_TIMEOUT_SEC = 3600   # a request still open after this is posted again; the Host joins it
LOOKUP_TIMEOUT_SEC = 3
HOLD_RECHECK_SEC = 15
MAX_SEND_ATTEMPTS = 8
TICK_SEC = 0.25
REQUIRED_IDENTITY = ("places_version", "presence_work_output_version")
# Set by tests to an in-process ASGI transport; production uses the loopback Host Service.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    update_id INTEGER PRIMARY KEY, source_event_id TEXT UNIQUE NOT NULL, chat_id INTEGER NOT NULL,
    owner INTEGER NOT NULL, lane TEXT NOT NULL DEFAULT '', place_id TEXT NOT NULL DEFAULT '',
    message_json TEXT NOT NULL, event_json TEXT NOT NULL DEFAULT '', staged_json TEXT NOT NULL DEFAULT '',
    conversation_key TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
    dispatched_at REAL NOT NULL DEFAULT 0, turn_ref TEXT NOT NULL DEFAULT '',
    receipt_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS inbox_open ON inbox(state, lane, chat_id, update_id);
CREATE TABLE IF NOT EXISTS work (
    ref TEXT PRIMARY KEY, update_id INTEGER NOT NULL, place_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'observe',
    attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0, last_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS work_open ON work(state, available_at);
CREATE TABLE IF NOT EXISTS outbox (
    delivery_id TEXT PRIMARY KEY, update_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, reply_to INTEGER NOT NULL,
    task_id TEXT NOT NULL, source_event_id TEXT NOT NULL, reporting INTEGER NOT NULL, parts_json TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'automatic', requires_place INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'place_reply', state TEXT NOT NULL DEFAULT 'pending',
    next_part INTEGER NOT NULL DEFAULT 0, sending_part INTEGER NOT NULL DEFAULT -1,
    attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    receipts_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_open ON outbox(state, available_at, chat_id);
CREATE TABLE IF NOT EXISTS reports (
    delivery_id TEXT NOT NULL, part_id TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', PRIMARY KEY (delivery_id, part_id, state));
CREATE INDEX IF NOT EXISTS reports_open ON reports(done, available_at);
CREATE TABLE IF NOT EXISTS gaps (
    ref TEXT NOT NULL, detail TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (ref, detail));
CREATE TABLE IF NOT EXISTS handoffs (
    update_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, client_message_id TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS actions (
    delivery_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, state TEXT NOT NULL,
    receipt_json TEXT NOT NULL DEFAULT '[]', report_json TEXT NOT NULL DEFAULT '', at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY, address TEXT UNIQUE NOT NULL, chat_id INTEGER NOT NULL, project TEXT NOT NULL,
    task_id TEXT NOT NULL, ts TEXT NOT NULL, text TEXT NOT NULL, refs_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending', decision TEXT NOT NULL DEFAULT '', delivery_id TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS news_open ON news(state, id);
"""
_READY: set[str] = set()


# ---- custody ---------------------------------------------------------------------------

def _path(api) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / "places.sqlite"


@contextlib.contextmanager
def _db(api, *, create: bool = True):
    """One short connection; passive reads neither create nor repair a custody store."""
    path = _path(api)
    key = str(path)
    if not create:
        if not path.exists():
            raise FileNotFoundError(key)
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if create and key not in _READY:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            _READY.add(key)
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _tx(api):
    """One transaction: every write inside commits together or not at all."""
    with _db(api) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def _rows(api, sql: str, args: tuple = ()) -> list[dict]:
    with _db(api) as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _exec(api, sql: str, args: tuple = ()) -> int:
    with _db(api) as conn:
        return conn.execute(sql, args).rowcount


def source_event_id(chat_id: int, message_id: int) -> str:
    return f"{PROVIDER}:{chat_id}:{message_id}"


def commit(api, update_id: int, message: dict, *, owner: bool, lane: str, place_id: str = "") -> bool:
    """Retain one provider message before the offset moves; a redelivered update adds nothing."""
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    event_id = source_event_id(chat_id, int(message.get("message_id") or 0))
    with _db(api) as conn:
        return conn.execute(
            "INSERT OR IGNORE INTO inbox(update_id,source_event_id,chat_id,owner,lane,place_id,message_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (update_id, event_id, chat_id, int(owner), lane, place_id,
             json.dumps(message, ensure_ascii=False), time.time())).rowcount == 1


def recover(api) -> dict:
    """Start of a runner: requests in flight are posted again (the Host joins them);
    a part, edit or reaction that was in flight may have taken effect, so it becomes
    uncertain, with its report when reporting was negotiated; none is sent again."""
    with _tx(api) as conn:
        turns = conn.execute("UPDATE inbox SET state='pending', available_at=0 WHERE state='submitting'").rowcount
        stuck = [dict(row) for row in conn.execute("SELECT * FROM outbox WHERE state='sending'")]
        bot = str(bot_identity(api).get("id") or "")
        for row in stuck:
            if row["sending_part"] >= 0:
                _settle_part(conn, row, row["sending_part"], "uncertain", {}, account_id=bot,
                             error="restarted_during_send")
            else:
                conn.execute("UPDATE outbox SET state='pending' WHERE delivery_id=?", (row["delivery_id"],))
        actions = [dict(row) for row in conn.execute("SELECT * FROM actions WHERE state='sending'")]
        for row in actions:  # an edit or reaction whose call never returned
            conn.execute("UPDATE actions SET state='uncertain', receipt_json=? WHERE delivery_id=?",
                         (json.dumps([{"error": "restarted_during_action"}]), row["delivery_id"]))
            if row["report_json"]:
                facts = json.loads(row["report_json"])
                report(conn, delivery_id=row["delivery_id"], part_id="0", state="uncertain", chat_id=facts["chat_id"],
                       account_id=facts["account_id"], text="", fmt="",
                       message={**facts["message"], "error": "restarted_during_action"}, origin=facts["origin"])
    return {"turns_rejoined": turns, "parts_uncertain": sum(1 for row in stuck if row["sending_part"] >= 0),
            "actions_uncertain": len(actions)}


def record_gap(api, ref: str, detail: str) -> None:
    """A host fact this bridge cannot act on (an output without identity): kept, shown, not sent."""
    _exec(api, "INSERT OR IGNORE INTO gaps(ref, detail, at) VALUES (?,?,?)", (ref, detail, time.time()))


# ---- Host I/O ----------------------------------------------------------------------------

async def _host(api, method: str, path: str, *, params: dict | None = None, body: Any = None,
                timeout: float = 60) -> tuple[int, dict]:
    port = int(os.environ.get("OUROBOROS_HOST_SERVICE_PORT", "8767"))
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=timeout, trust_env=False,
                                 follow_redirects=False, transport=_TRANSPORT) as client:
        response = await client.request(method, path, params=params, json=body,
                                        headers={"X-Skill-Token": api.get_skill_token().use_in_request()})
    try:
        data = response.json()
    except ValueError:
        data = {}
    return response.status_code, data if isinstance(data, dict) else {}


_IDENTITY: dict = {"at": 0.0, "body": {}}


async def host_identity(api) -> dict:
    """The Host's advertised facts, read again every minute; missing facts are setup gaps."""
    if time.monotonic() - _IDENTITY["at"] > 60 or not _IDENTITY["body"]:
        status, body = await _host(api, "GET", "/identity", timeout=10)
        if status == 200:
            _IDENTITY.update(at=time.monotonic(), body=body)
    return _IDENTITY["body"]


def identity_gaps(identity: dict) -> list[str]:
    gaps = [key for key in REQUIRED_IDENTITY if type(identity.get(key)) is not int or identity[key] < 1]
    if "direct_message" not in (identity.get("presence_wake_facts") or []):
        gaps.append("presence_wake_facts")
    return gaps


_BOT: dict = {}


def bot_identity(api) -> dict:
    return dict(_BOT) or _read_json_file(_state_file(api, "bot_identity.json"))


def remember_bot(api, me: Any) -> None:
    """getMe facts: the Place account id, shown in status; never the token. Never fails startup."""
    if not isinstance(me, dict) or not me.get("id"):
        return
    _BOT.update(id=str(me["id"]), username=str(me.get("username") or ""))
    path = _state_file(api, "bot_identity.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_BOT), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        api.log("warning", "Telegram bot identity was not saved; status reads it again after the next start.")


async def place_lookup(api, chat_id: int) -> tuple[str, dict]:
    """``("place", body)``; ``("none", {})`` only for the Host's exact not-connected answer;
    else ``("unknown", {"code": ...})``, including before this bridge knows its own bot account."""
    bot = bot_identity(api).get("id")
    if not bot:
        return "unknown", {"code": "bot_identity_unknown"}
    try:
        status, body = await _host(api, "GET", "/presence/place", timeout=LOOKUP_TIMEOUT_SEC, params={
            "provider": PROVIDER, "account_id": bot, "conversation_id": str(chat_id), "thread_id": ""})
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return "unknown", {"code": type(exc).__name__}
    if status == 200 and body.get("place_id"):
        return "place", body
    if (status, body.get("code")) == (404, "place_not_connected"):
        return "none", {}
    return "unknown", {"code": str(body.get("code") or f"http_{status}")}


# ---- poller side ------------------------------------------------------------------------

class PlaceCustodyUnavailable(RuntimeError):
    """The poller may neither acknowledge this update nor reroute it to Main."""


def place_lane(api) -> bool:
    """The owner's choice for ordinary messages: ``place`` asks the Host, ``main`` never does."""
    from .telegram_state import _load_settings

    return str(_load_settings(api).get("TELEGRAM_ORDINARY_CHAT") or "main").strip().lower() == "place"


def retained(api, update_id: int) -> bool:
    """Whether this update is in Place custody; an unreadable store is not an absent update."""
    try:
        with _db(api, create=False) as conn:
            return conn.execute("SELECT 1 FROM inbox WHERE update_id=?", (update_id,)).fetchone() is not None
    except FileNotFoundError:
        return False


async def offer(api, update_id: int, message: dict, *, owner: bool) -> bool:
    """Whether this ordinary private message is now in Place custody (else: classic/ignored).

    A redelivered update keeps the road it already took, whatever the lane setting says now
    (an offset lost after the commit must not send the same words to Main as well); only a
    new message follows the current setting."""
    try:
        if retained(api, update_id):
            return True
        if not place_lane(api):
            return False  # the classic choice: no lookup, the message takes the Main relay as before
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        if _rows(api, "SELECT 1 FROM inbox WHERE chat_id=? AND lane='' AND state='pending'", (chat_id,)):
            lane, place = "unknown", {"code": "earlier_message_undecided"}  # keep this chat's order
        else:
            lane, place = await place_lookup(api, chat_id)
        if lane == "none":
            return False
        commit(api, update_id, message, owner=owner, lane="presence" if lane == "place" else "",
               place_id=str(place.get("place_id") or ""))
        if lane == "unknown":
            _note_undecided(api, update_id, place.get("code", ""))
        return True
    except (sqlite3.Error, OSError) as exc:
        raise PlaceCustodyUnavailable(f"Place custody unavailable ({type(exc).__name__})") from exc


def _note_undecided(api, update_id: int, code: str) -> None:
    if _exec(api, "UPDATE inbox SET error=? WHERE update_id=? AND error<>?", (f"lookup:{code}", update_id, f"lookup:{code}")):
        api.log("warning", f"Telegram message {update_id} waits for a readable place answer ({code}); "
                           "it is not sent to Main while that answer is unknown.")


# ---- events -----------------------------------------------------------------------------

def _iso(seconds: Any) -> str:
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def forward_origin(message: dict) -> dict:
    origin = message.get("forward_origin")
    if isinstance(origin, dict):
        return {"type": str(origin.get("type") or ""), **{k: origin[k] for k in (
            "sender_user", "sender_user_name", "chat", "sender_chat", "date") if k in origin}}
    legacy = {k: message[k] for k in ("forward_from", "forward_from_chat", "forward_sender_name", "forward_date")
              if k in message}
    return {"type": "legacy", **legacy} if legacy else {}


def reply_reference(api, chat_id: int, reply: dict, bot_id: str) -> dict:
    """What the bridge knows about the replied-to message: host facts, never a guess."""
    mid = int(reply.get("message_id") or 0)
    record = _message_ref(api, chat_id, mid)
    if record.get("kind") in {"place_reply", "place_tool", "news"}:
        return {"state": record["kind"], "delivery_id": str(record.get("delivery_id") or "")}
    address = _reply_reference(api, chat_id, mid)
    if address:
        return {"state": "known", "host_reference": address, "kind": str(record.get("kind") or "")}
    author = str((reply.get("from") or {}).get("id") or "")
    return {"state": "unknown" if author == bot_id else "not_mapped"}


def _attachment_kinds(message: dict) -> list[dict]:
    from .telegram_inbound import _UNSUPPORTED_KINDS, inbound_file

    photos = message.get("photo") or []
    if photos:
        best = photos[-1] or {}
        return [{"kind": "photo", "file_unique_id": str(best.get("file_unique_id") or ""), "width": best.get("width"),
                 "height": best.get("height"), "size": best.get("file_size")}]
    info = inbound_file(message)
    rows = [{k: info[k] for k in ("kind", "name", "mime", "size")}] if info is not None else []
    # Kinds the bridge cannot relay as files are still named, never silently dropped.
    return rows + [{"kind": kind, "staged": False, "unavailable": "unsupported_kind"}
                   for kind in _UNSUPPORTED_KINDS if message.get(kind)]


def build_event(api, message: dict, bot_id: str, staged: list[dict]) -> dict:
    """The frozen /presence/turn event: provider facts only, no text interpretation."""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id, mid = int(chat.get("id") or 0), int(message.get("message_id") or 0)
    name = " ".join(str(p) for p in (sender.get("first_name"), sender.get("last_name")) if p).strip()
    facts = ["direct_message"]
    attachments = _attachment_kinds(message)
    for row, stage in zip(attachments, staged):  # staging decided once, before the event froze
        row.update(staged=bool(stage.get("path")), **({"unavailable": stage["unavailable"]}
                                                     if stage.get("unavailable") else {}))
    facts_message: Dict[str, Any] = {"message_id": mid, "date": message.get("date"),
                                     "timestamp": _iso(message.get("date")), "event_kind": "message",
                                     "attachments": attachments}
    reply = message.get("reply_to_message")
    if isinstance(reply, dict):
        author = reply.get("from") or {}
        if str(author.get("id") or "") == bot_id:
            facts.append("reply_to_me")
        facts_message["reply_to"] = {
            "message_id": int(reply.get("message_id") or 0), "author_id": str(author.get("id") or ""),
            "from_bot": author.get("is_bot") is True, "date": reply.get("date"),
            # The replied-to words are a separate source fact, never this sender's.
            "text": str(reply.get("text") or reply.get("caption") or ""),
            "reference": reply_reference(api, chat_id, reply, bot_id)}
    if isinstance(message.get("quote"), dict):
        facts_message["quote"] = {"text": str(message["quote"].get("text") or "")}
    if forward_origin(message):
        facts_message["forward_origin"] = forward_origin(message)
    if message.get("media_group_id"):
        facts_message["media_group_id"] = str(message["media_group_id"])
    return {
        "source_event_id": source_event_id(chat_id, mid), "provider": PROVIDER, "account_id": bot_id,
        "conversation_id": str(chat_id), "thread_id": "", "conversation_key": f"{PROVIDER}:{bot_id}:{chat_id}:0",
        "actor": {"platform_actor_id": str(sender.get("id") or ""), "id": str(sender.get("id") or ""),
                  "display_name": name or str(sender.get("username") or ""),
                  "username": str(sender.get("username") or ""), "is_bot": sender.get("is_bot") is True},
        "conversation": {"type": "private", "chat_id": str(chat_id), "title": name or str(sender.get("username") or "")},
        "message": facts_message, "text": str(message.get("text") or message.get("caption") or ""),
        "facts": facts,
    }


def _stored_name(kind: str, display: str) -> str:
    """The basename staged bytes are kept under; the sender's name is display metadata.

    That name is never a native path: it may carry another OS's separators, drive, stream
    or device name. The bytes are kept under the message's kind, with the name's extension
    only when it is plainly alphanumeric (the Host labels a staged file by its basename and
    types a non-media file by its extension). The Host judges secret-shaped names on that
    basename, so a name its own rule refuses is kept under one the same rule refuses: the
    Host, not this bridge, still refuses it, with the same admission.
    """
    from ouroboros.workspace_patch_rules import _sensitive_untracked_reason

    if _sensitive_untracked_reason(display):
        return kind + ".env"
    suffix = pathlib.PurePosixPath(display.replace("\\", "/")).suffix
    return kind + (suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) else "")


async def stage_files(api, client_factory, update_id: int, message: dict) -> list[dict]:
    """Bytes and digest, downloaded once; a provider refusal becomes an ``unavailable`` fact.

    A transport failure raises: the message keeps its custody and the download is tried
    again later. Previously staged bytes are reused; changed or missing ones refuse.
    """
    from .telegram_inbound import inbound_file

    row = _rows(api, "SELECT staged_json FROM inbox WHERE update_id=?", (update_id,))
    if row and row[0]["staged_json"]:
        staged = json.loads(row[0]["staged_json"])
        for item in staged:
            if not item.get("path"):
                continue
            path = pathlib.Path(item["path"])
            if not path.is_file() or hashlib.sha256(data := path.read_bytes()).hexdigest() != item["sha256"]:
                raise OSError("staged attachment bytes changed or went missing before the Host answered")
            # Access is fixed per process, so after a Restart the Host's rule may answer differently:
            # the bytes follow the name it asks for now (a copy; the folder goes when the Host answers).
            current = path.with_name(_stored_name(item["kind"], item["name"]))
            if current != path:
                current.write_bytes(data)
                item["path"] = str(current)
                _exec(api, "UPDATE inbox SET staged_json=? WHERE update_id=?", (json.dumps(staged), update_id))
        return staged
    photos = message.get("photo") or []
    info = None if photos else inbound_file(message)
    staged: list[dict] = []
    if photos or info is not None:
        folder = pathlib.Path(api.get_state_dir()) / "places_staging" / str(update_id)
        try:
            client = client_factory()
            if photos:
                import base64
                encoded, mime = await client.download_photo(str((photos[-1] or {}).get("file_id") or ""))
                data, kind = base64.b64decode(encoded), "photo"
                display = "photo.png" if mime == "image/png" else "photo.jpg"
            elif info.get("refusal"):
                raise ValueError(info["refusal"])
            else:
                data, kind, display = await client.download_file(info["file_id"]), info["kind"], str(info["name"])
        except (TelegramTransportError, TelegramRequestRejected) as exc:
            if isinstance(exc, TelegramTransportError) or exc.transient:
                raise
            staged.append({"unavailable": f"provider_refused:{exc}"[:200]})
        except (ValueError, RuntimeError) as exc:  # over the download cap, or no file path from Telegram
            staged.append({"unavailable": str(exc)[:200]})
        else:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / _stored_name(kind, display)
            path.write_bytes(data)
            staged.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "name": display,
                           "kind": kind})
    _exec(api, "UPDATE inbox SET staged_json=? WHERE update_id=?", (json.dumps(staged), update_id))
    return staged


# ---- runner -----------------------------------------------------------------------------

class Runner:
    """Admission, observation, delivery and receipts after the poller's commit."""

    def __init__(self, api, *, client_factory: Callable[[], Any],
                 classic: Callable[[int, dict], Awaitable[Any]]):
        self.api, self.client_factory, self.classic = api, client_factory, classic
        self.turns: dict[int, asyncio.Task] = {}
        self.busy: dict[str, asyncio.Task] = {}

    async def run(self) -> None:
        facts = recover(self.api)
        if any(facts.values()):
            self.api.log("info", f"Telegram places resumed custody: {facts}")
        try:
            while True:
                await self.tick()
                await asyncio.sleep(TICK_SEC)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Cancel and join every step, so no callback outlives a reload."""
        tasks = [*self.turns.values(), *self.busy.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.turns.clear()
        self.busy.clear()

    async def tick(self) -> None:
        """One pass; every step is bounded and none waits on a turn."""
        now = time.time()
        self.busy = {key: task for key, task in self.busy.items() if not task.done()}
        await self._resolve_lanes(now)
        self._dispatch_turns(now)
        for row in _rows(self.api, "SELECT ref FROM work WHERE state='observe' AND available_at<=?", (now,)):
            self._spawn("work:" + row["ref"], lambda ref=row["ref"]: self._observe(ref))
        for row in _rows(self.api, "SELECT DISTINCT chat_id FROM outbox WHERE state IN ('pending','held') "
                         "AND available_at<=?", (now,)):
            self._spawn(f"send:{row['chat_id']}", lambda chat=row["chat_id"]: self._deliver_chat(chat))
        if _rows(self.api, "SELECT 1 FROM reports WHERE done=0 AND available_at<=? LIMIT 1", (now,)):
            self._spawn("reports", self._report)

    def _spawn(self, key: str, step: Callable[[], Awaitable[Any]]) -> None:
        """One task per key; the step's coroutine is made only once its task runs."""
        if key not in self.busy:
            self.busy[key] = asyncio.create_task(self._guard(key, step))

    async def _guard(self, key: str, step) -> None:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.api.log("warning", f"Telegram places step {key.split(':', 1)[0]} failed ({type(exc).__name__}).")

    async def _resolve_lanes(self, now: float) -> None:
        # Only each chat's earliest undecided message is asked about or relayed, and a chat
        # whose earliest one waits for its retry waits whole: a later message never takes
        # Main, or a lane, ahead of it. Other chats are not held.
        for row in _rows(self.api, "SELECT update_id, chat_id, owner, message_json FROM inbox WHERE update_id IN ("
                         "SELECT min(update_id) FROM inbox WHERE state='pending' AND lane='' GROUP BY chat_id) "
                         "AND available_at<=? ORDER BY update_id LIMIT 20", (now,)):
            lane, place = await place_lookup(self.api, row["chat_id"])
            if lane == "place":
                _exec(self.api, "UPDATE inbox SET lane='presence', place_id=?, error='' WHERE update_id=?",
                      (place["place_id"], row["update_id"]))
            elif lane == "none":
                # Exactly not connected and never sent to the Host: the owner's words take the
                # classic road (idempotent by client message id); anyone else stays unanswered.
                if row["owner"]:
                    try:
                        await self.classic(row["update_id"], json.loads(row["message_json"]))
                    except Exception as exc:
                        _exec(self.api, "UPDATE inbox SET available_at=?, error=? WHERE update_id=?",
                              (now + 5, f"classic:{type(exc).__name__}", row["update_id"]))
                        continue
                _exec(self.api, "UPDATE inbox SET lane='classic', state=? WHERE update_id=?",
                      ("classic" if row["owner"] else "ignored", row["update_id"]))
            else:
                _exec(self.api, "UPDATE inbox SET available_at=? WHERE update_id=?", (now + 5, row["update_id"]))
                _note_undecided(self.api, row["update_id"], place.get("code", ""))

    def _dispatch_turns(self, now: float) -> None:
        self.turns = {uid: task for uid, task in self.turns.items() if not task.done()}
        if len(self.turns) >= MAX_INFLIGHT_TURNS:
            return
        seen: set[int] = set()
        for row in _rows(self.api, "SELECT update_id, chat_id, lane, state, available_at, dispatched_at FROM inbox "
                         "WHERE state IN ('pending','submitting') AND lane IN ('presence','') ORDER BY update_id"):
            chat = row["chat_id"]
            if chat in seen:
                continue  # one message at a time enters each chat, in arrival order
            seen.add(chat)
            if row["state"] == "submitting" and host_captured(self.api, row["update_id"]):
                seen.discard(chat)  # the Host holds it: the next message of this chat may follow
                continue
            ready = (row["state"] == "pending" and row["lane"] == "presence" and row["available_at"] <= now
                     and row["update_id"] not in self.turns)
            if ready and len(self.turns) < MAX_INFLIGHT_TURNS and _exec(
                    self.api, "UPDATE inbox SET state='submitting', dispatched_at=?, attempts=attempts+1 "
                    "WHERE update_id=? AND state='pending'", (now, row["update_id"])):
                self.turns[row["update_id"]] = asyncio.create_task(
                    self._guard("turn", lambda uid=row["update_id"]: self._submit(uid)))

    async def _submit(self, uid: int) -> None:
        api = self.api
        row = _rows(api, "SELECT * FROM inbox WHERE update_id=?", (uid,))[0]
        message = json.loads(row["message_json"])
        try:
            identity = await host_identity(api)
            gaps = identity_gaps(identity)
            if gaps:
                return self._release(uid, 60, "host_missing_facts:" + ",".join(gaps))
            staged = await stage_files(api, self.client_factory, uid, message)
            if not row["event_json"]:  # frozen once: every retry repeats the identical event
                event = build_event(api, message, str(bot_identity(api).get("id") or ""), staged)
                if _exec(api, "UPDATE inbox SET event_json=?, conversation_key=? WHERE update_id=?",
                         (json.dumps(event, ensure_ascii=False), event["conversation_key"], uid)):
                    _remember_room(api, event["conversation_key"])
            else:
                event = json.loads(row["event_json"])
            body = {"event": event, "staged_files": [item["path"] for item in staged if item.get("path")]}
            if identity.get("presence_continuation_version") == 1:
                body["continuation_version"] = 1
            if identity.get("presence_delivery_version") == 1:
                body["delivery_reporting_version"] = 1
            status, answer = await _host(api, "POST", "/presence/turn", body=body, timeout=TURN_TIMEOUT_SEC)
        except asyncio.CancelledError:
            raise  # stays submitting; the next runner posts it again and joins the same turn
        except Exception as exc:
            return self._release(uid, min(60, 2 ** min(row["attempts"], 5)), f"before_answer:{type(exc).__name__}")
        self._settle_turn(row, status, answer)

    def _release(self, uid: int, delay: float, error: str) -> None:
        if _exec(self.api, "UPDATE inbox SET state='pending', available_at=?, error=? WHERE update_id=? AND error<>?",
                 (time.time() + delay, error, uid, error)):
            self.api.log("info", f"Telegram place message {uid} waits to be admitted again ({error}).")
        else:
            _exec(self.api, "UPDATE inbox SET state='pending', available_at=? WHERE update_id=?", (time.time() + delay, uid))

    def _settle_turn(self, row: dict, status: int, answer: dict) -> None:
        uid, place = row["update_id"], row["place_id"]
        if status == 200 and answer.get("ok"):
            turn_ref = str(answer.get("turn_ref") or "")
            self._output(row, turn_ref, answer, where=f"turn:{turn_ref}")
            refs = [answer.get("continuation_ref"), answer.get("work_ref")]
            if answer.get("outcome") == "deferred" and not any(refs):
                refs.append(turn_ref)
            for ref in filter(None, refs):
                _observe_ref(self.api, str(ref), uid, place)
            return self._finish(uid, "settled", answer, turn_ref)
        disposition = str(answer.get("disposition") or "retry")
        if answer.get("code") == "place_not_connected":
            # Disconnected after this message entered the Place lane: an earlier attempt may have
            # been captured, so it keeps its road and waits for a reconnect; never Main.
            self._release(uid, 60, "place_not_connected: held in the place lane")
        elif disposition == "retained":
            for ref in filter(None, [answer.get("turn_ref"), answer.get("work_ref")]):
                _observe_ref(self.api, str(ref), uid, place)
            self._finish(uid, "retained", answer, str(answer.get("turn_ref") or ""))
        elif disposition == "rejected":
            self._finish(uid, "rejected", answer, "")
        else:  # retry, blocked, or unknown: the event keeps its custody and identity
            delay = float(answer.get("retry_after") or (60 if disposition == "blocked" else 5))
            self._release(uid, min(delay, 3600), f"{answer.get('code') or f'http_{status}'}: {str(answer.get('error') or '')[:160]}")

    def _finish(self, uid: int, state: str, answer: dict, turn_ref: str) -> None:
        """The Host answered, so it holds its own copy of any staged file: release ours."""
        _exec(self.api, "UPDATE inbox SET state=?, turn_ref=?, receipt_json=?, error='' WHERE update_id=?",
              (state, turn_ref, json.dumps(answer, ensure_ascii=False), uid))
        shutil.rmtree(pathlib.Path(self.api.get_state_dir()) / "places_staging" / str(uid), ignore_errors=True)

    def _output(self, row: dict, task_id: str, view: dict, *, where: str) -> None:
        """Queue one selected answer under its canonical output identity, once."""
        text = str(view.get("text") or "")
        if view.get("outcome") != "message" or not text.strip():
            return
        if not view.get("output_ref"):
            return record_gap(self.api, where, "selected output without output_ref: not sent")
        message = json.loads(row["message_json"]) if row.get("message_json") else {}
        enqueue(self.api, str(view["output_ref"]), update_id=row["update_id"], chat_id=row["chat_id"],
                reply_to=int(message.get("message_id") or 0), task_id=task_id,
                source_event_id=row.get("source_event_id") or "", text=text,
                reporting=int(view.get("delivery_reporting_version") == 1))

    async def _observe(self, ref: str) -> None:
        api = self.api
        work = _rows(api, "SELECT * FROM work WHERE ref=?", (ref,))[0]
        inbox = _rows(api, "SELECT * FROM inbox WHERE update_id=?", (work["update_id"],))[0]
        try:
            status, view = await _host(api, "GET", f"/presence/work/{ref}", params={"binding_id": work["place_id"]})
        except (httpx.HTTPError, OSError):
            status, view = 0, {}
        if status in {200, 202} and view.get("ok"):
            producer = str(view.get("task_id") or ref)
            for index, item in enumerate(view.get("outputs") or []):
                if isinstance(item, dict):  # released outputs, in order; each identity is sent once
                    self._output(inbox, producer, {**item, "delivery_reporting_version": view.get(
                        "delivery_reporting_version")}, where=f"work:{ref}:output:{index}")
            child = str(view.get("child_work_ref") or "")
            if child and child != ref:
                _observe_ref(api, child, work["update_id"], work["place_id"])
            if status == 200:
                self._output(inbox, producer, view, where=f"work:{ref}")
                _exec(api, "UPDATE work SET state='done', last_json=? WHERE ref=?", (json.dumps(view), ref))
                return
        elif status == 404:
            _exec(api, "UPDATE work SET state='done', last_json=? WHERE ref=?", (json.dumps(view), ref))
            return
        delay = min(30, 3 * 2 ** min(work["attempts"], 4)) if status in {202, 200} else 30
        _exec(api, "UPDATE work SET attempts=attempts+1, available_at=?, last_json=? WHERE ref=?",
              (time.time() + delay, json.dumps(view), ref))

    async def _deliver_chat(self, chat_id: int) -> None:
        """One chat's outbox in order; a held or failing row stops that chat's later rows."""
        for row in _rows(self.api, "SELECT * FROM outbox WHERE chat_id=? AND state IN ('pending','held') "
                         "ORDER BY created_at, delivery_id", (chat_id,)):
            if row["available_at"] > time.time() or not await self._deliver(row):
                return

    async def _may_send(self, row: dict) -> bool:
        """Read the place before each part; a place's state governs even news to the owner."""
        lane, place = await place_lookup(self.api, row["chat_id"])
        if (lane == "place" and place.get("state") == "active") or (lane == "none" and not row["requires_place"]):
            return True
        _exec(self.api, "UPDATE outbox SET state='held', available_at=?, error=? WHERE delivery_id=?",
              (time.time() + HOLD_RECHECK_SEC, f"held:{place.get('state') or place.get('code') or lane}",
               row["delivery_id"]))
        return False

    async def _deliver(self, row: dict) -> bool:
        api = self.api
        parts = json.loads(row["parts_json"])
        client, bot = self.client_factory(), str(bot_identity(api).get("id") or "")
        for index in range(row["next_part"], len(parts)):
            if not await self._may_send(row):
                return False
            with _db(api) as conn:  # write-ahead: found in flight later means uncertain
                conn.execute("UPDATE outbox SET state='sending', sending_part=? WHERE delivery_id=?",
                             (index, row["delivery_id"]))
            data = {"chat_id": str(row["chat_id"]), "text": parts[index]["html"], "parse_mode": "HTML"}
            if index == 0 and row["reply_to"]:
                data.update(reply_to_message_id=row["reply_to"], allow_sending_without_reply=True)
            fmt = "html"
            try:
                try:
                    result = await client.call("sendMessage", data=data)
                except TelegramRequestRejected as exc:
                    if not exc.plain_retry_safe:
                        raise
                    data.pop("parse_mode")
                    data["text"], fmt = parts[index]["plain"], "text"
                    result = await client.call("sendMessage", data=data)
            except (TelegramRequestRejected, TelegramTransportError) as exc:
                retry = (exc.transient if isinstance(exc, TelegramRequestRejected) else not exc.maybe_sent)
                with _tx(api) as conn:
                    if retry and row["attempts"] + 1 < MAX_SEND_ATTEMPTS:  # provably not delivered
                        conn.execute("UPDATE outbox SET state='pending', sending_part=-1, attempts=attempts+1, "
                                     "available_at=? WHERE delivery_id=?",
                                     (time.time() + 5 * 2 ** row["attempts"], row["delivery_id"]))
                    else:
                        state = "failed" if isinstance(exc, TelegramRequestRejected) or not exc.maybe_sent else "uncertain"
                        _settle_part(conn, row, index, state, {}, account_id=bot, error=str(exc))
                return False
            receipt = result.get("result") if isinstance(result.get("result"), dict) else {}
            with _tx(api) as conn:  # the part's progress, receipt and report commit together
                if not receipt.get("message_id"):  # accepted without a provable message: never "delivered"
                    _settle_part(conn, row, index, "uncertain", {}, account_id=bot, error="no_message_id")
                    return False
                _settle_part(conn, row, index, "delivered", receipt, account_id=bot, text=data["text"], fmt=fmt,
                             done=index + 1 == len(parts))
            _remember_message(api, row["chat_id"], int(receipt["message_id"]),
                              {"kind": row["kind"], "delivery_id": row["delivery_id"], "chat_id": row["chat_id"]})
        return True

    async def _report(self) -> None:
        api = self.api
        for row in _rows(api, "SELECT * FROM reports WHERE done=0 AND available_at<=? ORDER BY rowid LIMIT 20",
                         (time.time(),)):
            key = (row["delivery_id"], row["part_id"], row["state"])
            try:
                status, body = await _host(api, "POST", "/presence/delivery", body=json.loads(row["payload_json"]))
            except (httpx.HTTPError, OSError) as exc:
                status, body = 0, {"code": type(exc).__name__}
            if status == 200 and body.get("ok"):
                _exec(api, "UPDATE reports SET done=1, error='' WHERE delivery_id=? AND part_id=? AND state=?", key)
            elif status in {400, 409}:  # an invalid or conflicting report is kept, not retried forever
                _exec(api, "UPDATE reports SET done=2, error=? WHERE delivery_id=? AND part_id=? AND state=?",
                      (str(body.get("code") or status), *key))
            else:
                _exec(api, "UPDATE reports SET attempts=attempts+1, available_at=?, error=? WHERE delivery_id=? "
                      "AND part_id=? AND state=?",
                      (time.time() + min(300, 5 * 2 ** min(row["attempts"], 6)), str(body.get("code") or status), *key))


def host_captured(api, update_id: int) -> bool:
    """Whether the Host has captured this message: its inbound room row for the turn id the
    Host derives, carrying this exact event identity. A legacy binding or an unreadable store
    reads as not captured, so the chat simply waits for the answer."""
    from ouroboros import dialogue_store
    from ouroboros.presence_runner import PresenceTurnEvent, presence_event_identity, presence_turn_task_id
    from .telegram_state import _data_dir

    row = _rows(api, "SELECT place_id, source_event_id, event_json FROM inbox WHERE update_id=?", (update_id,))
    if not row or not row[0]["event_json"] or not row[0]["place_id"]:
        return False
    frozen = json.loads(row[0]["event_json"])
    identity = presence_event_identity(row[0]["place_id"], PresenceTurnEvent(**{key: frozen[key] for key in (
        "source_event_id", "provider", "account_id", "conversation_id", "thread_id", "conversation_key",
        "actor", "conversation", "message", "text")}))
    turn = presence_turn_task_id(row[0]["place_id"], row[0]["source_event_id"])
    try:
        return any(stored.row.get("direction") == "in" and stored.row.get("presence_event_identity") == identity
                   for stored in dialogue_store.task_rows(_data_dir(api), [turn], stream="chat"))
    except Exception:
        return False


def _observe_ref(api, ref: str, update_id: int, place_id: str) -> None:
    _exec(api, "INSERT OR IGNORE INTO work(ref, update_id, place_id) VALUES (?,?,?)", (ref, update_id, place_id))


def parts_for(text: str) -> list[dict]:
    """The frozen Telegram chunks of one text: what is sent, and its visible plain form."""
    return [{"html": chunk, "plain": _telegram_html_to_plain(chunk)} for chunk in markdown_to_telegram_chunks(text)]


def enqueue(api, delivery_id: str, *, update_id: int, chat_id: int, reply_to: int, task_id: str,
            source_event_id: str, text: str, reporting: int, origin: str = "automatic",
            requires_place: bool = True, kind: str = "place_reply", conn=None) -> bool:
    """Freeze the chunks once; the same delivery identity is queued at most once.

    ``conn`` joins the caller's ``_tx``, so a request and its queued words commit together."""
    parts = parts_for(text)
    if not parts:
        return False
    with (contextlib.nullcontext(conn) if conn is not None else _db(api)) as conn:
        return conn.execute(
            "INSERT OR IGNORE INTO outbox(delivery_id,update_id,chat_id,reply_to,task_id,source_event_id,reporting,"
            "parts_json,origin,requires_place,kind,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (delivery_id, update_id, chat_id, reply_to, task_id, source_event_id, reporting,
             json.dumps(parts, ensure_ascii=False), origin, int(requires_place), kind, time.time())).rowcount == 1


def report(conn, *, delivery_id: str, part_id: str, state: str, chat_id: int, account_id: str, text: str,
           fmt: str, message: dict, origin: dict) -> None:
    """Queue one physical outcome for /presence/delivery; the runner reports it."""
    payload = {
        "schema_version": 1, "delivery_id": delivery_id, "part_id": part_id, "state": state,
        "provider": PROVIDER, "account_id": account_id, "conversation_id": str(chat_id), "thread_id": "",
        "text": text, "format": fmt, "message": message, "origin": origin,
    }
    conn.execute("INSERT OR IGNORE INTO reports(delivery_id,part_id,state,payload_json) VALUES (?,?,?,?)",
                 (delivery_id, part_id, state, json.dumps(payload, ensure_ascii=False)))


def _settle_part(conn, row: dict, index: int, state: str, receipt: dict, *, account_id: str, text: str = "",
                 fmt: str = "", error: str = "", done: bool = True) -> None:
    """One physical part's outcome, its receipt and its report; callers hold one ``_tx``."""
    terminal = {"delivered": "done" if done else "pending", "failed": "failed", "uncertain": "uncertain"}[state]
    receipts = json.loads(row.get("receipts_json") or "[]")
    receipts.append({"part": index, "state": state, **{k: receipt[k] for k in ("message_id", "date") if k in receipt}})
    conn.execute("UPDATE outbox SET state=?, next_part=?, sending_part=-1, error=?, receipts_json=? WHERE delivery_id=?",
                 (terminal, index + 1 if state == "delivered" else index, error, json.dumps(receipts),
                  row["delivery_id"]))
    row["receipts_json"] = json.dumps(receipts)
    if not row["reporting"]:
        return
    message = {k: receipt[k] for k in ("message_id", "date") if k in receipt}
    if index == 0 and row["reply_to"]:
        message["reply_to_message_id"] = row["reply_to"]
    if error:
        message["error"] = error[:300]
    origin = {"kind": row.get("origin") or "automatic", "task_id": row["task_id"], "source_event_id": row["source_event_id"]}
    report(conn, delivery_id=row["delivery_id"], part_id=str(index), state=state, chat_id=row["chat_id"],
           account_id=account_id, text=text or json.loads(row["parts_json"])[index]["plain"], fmt=fmt or "text",
           message=message, origin={k: v for k, v in origin.items() if v})


_ROOMS: dict[str, frozenset] = {}


def room_event(api, event: dict) -> bool:
    """A row of one of this bridge's own Place rooms: its outbox delivers it, the mirror must not.

    The rooms are the Host's derived ids of the conversations this bridge itself submitted;
    nothing is guessed from a numeric range.
    """
    from ouroboros.places import conversation_chat_id

    key = str(_path(api))
    if key not in _ROOMS:
        try:
            with _db(api, create=False) as conn:
                keys = [r[0] for r in conn.execute("SELECT DISTINCT conversation_key FROM inbox WHERE conversation_key<>''")]
        except FileNotFoundError:
            keys = []
        _ROOMS[key] = frozenset(conversation_chat_id(k) for k in keys)
    try:
        return int(event.get("chat_id") or 0) in _ROOMS[key]
    except (TypeError, ValueError):
        return False


def _remember_room(api, conversation_key: str) -> None:
    """A conversation whose event just froze joins rooms already read; an unread set is
    still read whole, from custody, when it is first needed."""
    from ouroboros.places import conversation_chat_id

    key, room = str(_path(api)), conversation_chat_id(conversation_key)
    if key in _ROOMS and room not in _ROOMS[key]:
        _ROOMS[key] = _ROOMS[key] | {room}


def status(api) -> dict:
    """Nonsecret readiness: the ids a place connection needs, custody counts, host gaps."""
    from .telegram_state import _load_settings

    facts = {"bot_account_id": str(bot_identity(api).get("id") or ""),
             "bot_username": str(bot_identity(api).get("username") or ""),
             "owner_chat_id": str(_load_settings(api).get("TELEGRAM_CHAT_ID") or ""),
             "host_gaps": identity_gaps(_IDENTITY["body"]) if _IDENTITY["body"] else ["not_checked_yet"]}
    counts = {"inbox_open": "inbox WHERE state IN ('pending','submitting')",
              "inbox_undecided": "inbox WHERE lane='' AND state='pending'",
              "observing": "work WHERE state='observe'",
              "outbox_waiting": "outbox WHERE state IN ('pending','sending')",
              "outbox_held": "outbox WHERE state='held'", "outbox_uncertain": "outbox WHERE state='uncertain'",
              "outbox_failed": "outbox WHERE state='failed'", "reports_pending": "reports WHERE done=0",
              "output_gaps": "gaps", "news_waiting": "news WHERE state='pending'"}
    try:
        with _db(api, create=False) as conn:  # a passive read: no custody file is created here
            facts.update({name: int(conn.execute(f"SELECT count(*) FROM {sql}").fetchone()[0])
                          for name, sql in counts.items()})
            facts["last_error"] = next((r[0] for r in conn.execute(
                "SELECT error FROM inbox WHERE error<>'' AND state IN ('pending','submitting') "
                "ORDER BY update_id DESC LIMIT 1")), "")
    except FileNotFoundError:
        facts.update({name: 0 for name in counts}, last_error="")
    except sqlite3.Error as exc:
        facts["error"] = f"custody unreadable ({type(exc).__name__})"
    return facts
