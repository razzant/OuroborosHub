"""Letters this install sent to other agents: one JSON file per message id, written before the first request.

A letter is the sender's durable record of one question: its immutable original (message id, the
destination as the caller named it and its endpoint, the text, the task and context it continued), who
asked (the task, chat and project ids of the tool call, kept as facts, and the host's reference to that call
when the host gives one), and what is known so far (the peer's task and context ids, its state and delivery
class, the answer, whether the asking work received it, the late report). Every change re-reads the letter
under its own lock and commits only a consistent result, so a slower call can never erase or replace a
final outcome a faster one recorded, and an answer to a later message on the same remote task is never
filed here. The order in which letters went to one remote task of one bound peer is kept in a small index
beside them, so telling the letters of a reused task apart needs neither timestamps nor a scan of them all.

The letters still owed a read or a report are named in a second small index (``watching/``): a mark is written
before a confirmed task id is committed to its letter, so a confirmed letter is never missing from it, and the
companion's watch (``scripts/a2a_watch.py``) reads only these marks, never the whole archive of letters.
"""
from __future__ import annotations

import hashlib
from heapq import nlargest
import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from . import storage

SCHEMA = 1
DIR = "letters"
WATCHING = "watching"  # one empty mark per letter still owed a read or a report, under DIR
# A2A task states after which a task never changes; ``input-required`` and ``auth-required`` interrupt it.
TERMINAL = frozenset({"completed", "rejected", "failed", "canceled"})
INTERRUPTED = frozenset({"input-required", "auth-required"})
ANSWERING = frozenset({"completed", "failed", "input-required"})
# Local states before the peer confirmed the request: saved, not sent, refused or of unknown effect.
LOCAL = frozenset({"saved", "unsent", "refused", "sent_no_handle"})
OUTCOME = ("state", "delivery", "answer", "question_id", "unconfirmed")  # what a letter's outcome fixes for good
IMMUTABLE = ("schema", "message_id", "url", "endpoint", "peer", "link_uid", "text", "task_id", "context_id",
             "created_at", "asked_by")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def new_id() -> str:
    return uuid.uuid4().hex


def valid_id(value: str) -> bool:
    return bool(_ID.fullmatch(str(value or "")))


def legacy_client_message_id(task_id: str, message_id: str) -> str:
    """The identity a 1.5 receiver uses for one (task, message) pair."""
    identity = json.dumps([task_id, message_id or "message"], ensure_ascii=False, separators=(",", ":"))
    return "a2a:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _name(message_id: str) -> str:
    return hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:40]


def path(state_dir, message_id: str) -> Path:
    return Path(state_dir) / DIR / f"{_name(message_id)}.json"


def _lock(state_dir, message_id: str) -> Path:
    return Path(state_dir) / DIR / f".{_name(message_id)}.lock"


def load(state_dir, message_id: str) -> dict | None:
    try:
        letter = storage.read_json(path(state_dir, message_id))
    except ValueError:
        raise ValueError(f"the letter {message_id!r} is unreadable") from None
    if letter is None:
        return None
    if not isinstance(letter, dict) or letter.get("message_id") != message_id:
        raise ValueError(f"the letter {message_id!r} has a different identity")
    return letter


def create(state_dir, letter: dict) -> tuple[dict, bool]:
    """Save a new letter before anything is sent; an existing letter with this id is returned unchanged."""
    message_id = letter["message_id"]
    with storage.locked(_lock(state_dir, message_id)):
        current = load(state_dir, message_id)
        if current is not None:
            return current, False
        record = {"schema": SCHEMA, "remote": {"task_id": "", "context_id": ""}, "state": "saved",
                  "delivery": None, "answer": None, "unconfirmed": False, "attempts": 0, "last_error": "",
                  **letter, "created_at": now()}
        record["updated_at"] = record["created_at"]
        storage.write_json(path(state_dir, message_id), record)
        return record, True


def update(state_dir, message_id: str, change: Callable[[dict], dict | None]) -> dict:
    """Re-read the letter under its lock, apply ``change`` to that copy and commit a consistent result."""
    with storage.locked(_lock(state_dir, message_id)):
        current = load(state_dir, message_id)
        if current is None:
            raise ValueError(f"no letter {message_id!r}")
        proposed = change(dict(current))
        if not proposed:
            return current
        merged = merge(current, proposed)
        if merged != current:
            merged["updated_at"] = now()
            storage.write_json(path(state_dir, message_id), merged)
        return merged


def _rank(state: str) -> int:
    return 2 if state in TERMINAL else 0 if state in LOCAL or not state else 1


def has_outcome(letter: dict) -> bool:
    """A confirmed question is also this letter's outcome: an answer to it is another letter."""
    return letter.get("state") in TERMINAL or (
        letter.get("state") == "input-required" and bool((letter.get("answer") or {}).get("text")))


def merge(old: dict, new: dict) -> dict:
    """The original never changes; a confirmed task id never changes; a final outcome (its state, delivery class
    and answer) never changes, so a slower call's older snapshot can neither replace nor add to it."""
    merged = {**old, **new}
    for key in IMMUTABLE:
        if key in old:
            merged[key] = old[key]
    remote_old, remote_new = old.get("remote") or {}, new.get("remote") or {}
    merged["remote"] = {key: remote_old.get(key) or remote_new.get(key) or "" for key in ("task_id", "context_id")}
    old_rank, new_rank = _rank(str(old.get("state") or "")), _rank(str(merged.get("state") or ""))
    if has_outcome(old):
        for key in OUTCOME:
            merged[key] = old.get(key)
    elif old.get("state") in INTERRUPTED and new_rank != 2:
        for key in OUTCOME:
            merged[key] = old.get(key)
    elif old_rank == 1 and new_rank == 0:
        merged["state"], merged["delivery"] = old["state"], old.get("delivery")
    if old.get("answer") and not (merged.get("answer") or {}).get("text"):
        merged["answer"] = old["answer"]
    return merged


def same_origin(letter: dict, origin: dict) -> bool:
    """Whether the caller asked this letter: the same task, or the same chat. A missing id matches nothing."""
    asked = letter.get("asked_by") or {}
    task = str((origin or {}).get("task_id") or "")
    chat = (origin or {}).get("chat_id")
    return bool((task and asked.get("task_id") == task)
                or (isinstance(chat, int) and not isinstance(chat, bool) and asked.get("chat_id") == chat))


def listing(state_dir, origin: dict, limit: int = 50) -> list[dict]:
    """The caller's own latest letters. Only this explicit archive read walks all letter files."""
    if not origin.get("task_id") and origin.get("chat_id") is None:
        return []

    def own_letters():
        for item in sorted((Path(state_dir) / DIR).glob("*.json")):
            try:
                letter = storage.read_json(item)
            except ValueError:
                continue
            if isinstance(letter, dict) and letter.get("message_id") and same_origin(letter, origin):
                yield letter

    return nlargest(limit, own_letters(), key=lambda letter: str(letter.get("created_at") or ""))


def _task_index(state_dir, task_key: list[str]) -> Path:
    name = hashlib.sha256(json.dumps(task_key).encode("utf-8")).hexdigest()[:40]
    return Path(state_dir) / DIR / "by-task" / f"{name}.json"


def _order(index_path: Path) -> list:
    index = storage.read_json(index_path)
    messages = [] if index is None else index.get("messages") if isinstance(index, dict) else None
    if index is not None and (not isinstance(messages, list) or not all(
            isinstance(message_id, str) and valid_id(message_id) for message_id in messages)):
        raise ValueError("the task index is not a message list")
    return list(messages or [])


def _task_key(letter: dict, task_id: str) -> list[str]:
    return [letter["endpoint"], str(letter.get("link_uid") or ""), task_id]


def note_task(state_dir, letter: dict, task_id: str) -> None:
    """Record that this letter went to this remote task (once): before it is sent with a task id it names, and
    before the task id a reply discloses is committed to it, so an index entry never trails its letter."""
    if not task_id:
        return
    message_id = letter["message_id"]
    task_key = _task_key(letter, task_id)
    index = _task_index(state_dir, task_key)
    try:
        if message_id in _order(index):
            return
    except ValueError:
        return  # never turn an unreadable membership record into proof of a sole letter
    with storage.locked(index.with_name(f".{index.stem}.lock")):
        try:
            messages = _order(index)
        except ValueError:
            return  # never turn an unreadable membership record into proof of a sole letter
        if message_id not in messages:
            storage.write_json(index, {"task_key": task_key, "messages": messages + [message_id]})


def retry_safe(letter: dict) -> bool:
    """A retry can only follow a letter with no possible prior acceptance."""
    if (letter.get("remote") or {}).get("task_id"):
        return False
    state, attempts = letter.get("state"), letter.get("attempts", 0)
    if state == "saved":
        return attempts == 0
    dispatch = letter.get("dispatch") or {}
    # A second failed attempt on an older record says nothing about its first attempt.
    return (state in ("unsent", "refused") and dispatch.get("state") == state
            and (attempts == 1 or dispatch.get("retry_safe") is True))


def only_on_task(state_dir, letter: dict, task_id: str, *, first_receipt=False) -> bool:
    """Check local membership, not authorship, of an initial task.

    Its first reply can precede indexing; a provisionally indexed but provably unaccepted
    letter does not count as another question. Unknown attempts and unreadable rows do.
    """
    if letter.get("task_id") or not task_id:
        return False
    try:
        key = _task_key(letter, task_id)
        legacy = _order(_task_index(state_dir, [key[0], task_id]))
        messages = set(_order(_task_index(state_dir, key)))
        for message_id in legacy:
            previous = load(state_dir, message_id)
            if previous is None:
                return False  # an old endpoint-only entry whose identity cannot be read
            if _task_key(previous, task_id) == key:
                messages.add(message_id)
        own = letter["message_id"]
        if not first_receipt and own not in messages:
            return False
        for message_id in messages - {own}:
            other = load(state_dir, message_id)
            if other is None or not retry_safe(other):
                return False
    except ValueError:
        return False
    return True


def follows_question(state_dir, letter: dict, task_id: str, context_id: str,
                     previous_id: str, question_id: str, question_text: str) -> bool:
    """An exact saved question and no other possibly accepted local input after it on this task."""
    if not task_id or not context_id or letter.get("task_id") != task_id or not isinstance(previous_id, str) \
            or not valid_id(previous_id) or not question_id:
        return False
    try:
        previous = load(state_dir, previous_id)
        if not previous or previous.get("state") != "input-required" or previous.get("question_id") != question_id \
                or (previous.get("answer") or {}).get("text") != question_text \
                or _task_key(previous, task_id) != _task_key(letter, task_id):
            return False
        known = previous.get("remote") or {}
        if known.get("task_id") != task_id or known.get("context_id") != context_id:
            return False
        order = _order(_task_index(state_dir, _task_key(letter, task_id)))
        since_question = order[order.index(previous_id) + 1:]
        if letter["message_id"] not in since_question:
            return False
        for message_id in since_question:
            if message_id != letter["message_id"]:
                other = load(state_dir, message_id)
                if other is None or not retry_safe(other):
                    return False
        return True
    except ValueError:
        return False


def _mark(state_dir, message_id: str) -> Path:
    return Path(state_dir) / DIR / WATCHING / f"{_name(message_id)}.json"


def watch(state_dir, message_id: str) -> None:
    """Name a letter in the index of letters still owed a read or a report (once)."""
    mark = _mark(state_dir, message_id)
    if not mark.exists():
        storage.write_json(mark, {"message_id": message_id})


def unwatch(state_dir, message_id: str, expected: dict | None) -> bool:
    """Retire the mark only if the letter has not changed since the watch decided it was settled."""
    with storage.locked(_lock(state_dir, message_id)):
        if load(state_dir, message_id) != expected:
            return False
        _mark(state_dir, message_id).unlink(missing_ok=True)
        return True


def watched(state_dir) -> list[str]:
    """The message ids the index names. A mark that does not name its own letter is skipped, never guessed."""
    found = []
    for item in sorted((Path(state_dir) / DIR / WATCHING).glob("*.json")):
        try:
            mark = storage.read_json(item)
        except ValueError:
            continue
        message_id = mark.get("message_id") if isinstance(mark, dict) else None
        if valid_id(message_id) and item.name == f"{_name(message_id)}.json":
            found.append(message_id)
    return found
