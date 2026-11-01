"""Bounded, read-only reader for the usage store (``state/usage.sqlite``).

This module is the data-source boundary and nothing else: it opens one fixed
SQLite file read-only, proves it is the store this version understands, reads
one bounded selection inside one short read transaction, releases the
transaction, and hands back plain column values. It sanitizes nothing and
decides nothing about meaning — ``lens_core`` does that. No Ouroboros import,
no host import, no network, no write of any kind.

Store facts this relies on (core ``ouroboros/usage_store.py``, schema 1;
``docs/USAGE_STORE.md``)
-----------------------------------------------------------------------
* ``attempts`` holds ONE row per attempt id, UPDATEd in place on every
  transition. ``ts_last`` / ``ts_last_epoch`` is the time of the latest
  accounting write to that row — a late receipt or a price refinement moves it.
  There is no append order to resume from, so every read is a fresh selection.
* ``attempts_category_time`` indexes ``(category, ts_last_epoch)``. A global
  ``ORDER BY ts_last_epoch`` is a full scan plus a temporary B-tree, so this
  reader walks one indexed stream per category (newest first) and merges them
  until it holds ``max_rows + 1`` rows in total — never ``max_rows`` per stream.
* Category values come from ``summaries`` rows of scope ``category``; NULL and
  empty categories have no such row, so they get streams of their own. Core
  summarises legacy imports as unattributed, without a category summary. A
  legacy-only named category is therefore not enumerated. Completeness and
  newest-record facts refer to the enumerated streams, not all store rows.
  Core does summarise named categories of physical attempts, so an unbounded
  selection covers all timestamp-eligible physical attempts in the span.
* ``meta`` carries ``schema_version``, ``lock_tier`` and ``import`` (JSON). The
  database header's ``application_id`` names the lock protocol. ``enforced`` is
  SQLite's own file locks, which a ``mode=ro`` connection honours. ``name`` is a
  filesystem without kernel locks where EVERY access must run inside the core's
  name-protocol money lock; taking that lock means creating a file, so this
  reader refuses that tier rather than read around the protocol.

How the file is touched
-----------------------
Never with ``open()``. A process that closes ANY descriptor on a SQLite file
drops every POSIX lock it holds on that file, and this skill may run inside the
same process as the core's own store connections. So the path is checked with
``lstat`` only (no symlink at ``state`` or at the file, a regular file), and the
file itself is only ever opened by SQLite: ``mode=ro`` URI, ``query_only``, a
short busy timeout, one deferred read transaction. ``immutable`` and ``nolock``
are never used on this live file. After the transaction the path's identity is
checked again, so a store replaced while it was read is reported, not mixed in.
"""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import stat as _stat
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

STORE_REL_PARTS = ("state", "usage.sqlite")
SCHEMA_VERSION = 1
LOCK_TIER_ENFORCED = "enforced"
APPLICATION_ID_ENFORCED = 0x4F555345   # "OUSE" — core _APPLICATION_ID["enforced"]
APPLICATION_ID_NAME = 0x4F55534E       # "OUSN" — core _APPLICATION_ID["name"]
CATEGORY_INDEX = "attempts_category_time"

# Fixed bounds (documented in SKILL.md; no route may raise them).
MAX_ROWS = 4000                        # rows of any kind in one selection
MAX_CATEGORIES = 64                    # named category streams (+ NULL and empty)
MAX_CATEGORY_LEN = 200                 # a longer summary key is not used as a stream
MAX_EXTRA_CHARS = 16384                # one row's `extra` read at most this long
MAX_EXTRA_TOTAL_CHARS = 8 * 1024 * 1024
MAX_META_CHARS = 1024 * 1024
UNKNOWN_COUNT_CAP = 10000              # per category, per unusable-time bucket
BUSY_TIMEOUT_SEC = 0.25                # SQLite's own wait for the shared lock
MERGE_BUDGET_SEC = 0.4                 # the merge stops here and says it stopped
MERGE_STEP_BUDGET = 40_000_000         # SQLite VM steps inside the merge
HARD_BUDGET_SEC = 2.0                  # the whole read is abandoned past this
PROGRESS_INTERVAL = 1000               # VM steps between budget checks

_REQUIRED_ATTEMPT_COLUMNS = frozenset((
    "attempt_id", "kind", "state", "task_id", "root_task_id", "parent_task_id",
    "model", "provider", "category", "source", "ts_last", "ts_last_epoch",
    "prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens",
    "weight", "late_receipt", "extra",
))

# The ONE row query, in two spellings of its metadata column. `category IS ?2`
# matches NULL with a NULL parameter, so the same statement serves every
# stream, and every stream is an index range read in index order (pinned by
# tests through EXPLAIN QUERY PLAN). From `extra` only `physical_context` is
# wanted: with SQLite's JSON functions it is extracted in place (a few hundred
# bytes per row cross into Python instead of the whole metadata object);
# without them the bounded text is read and parsed after the transaction ends.
_STREAM_HEAD = (
    "SELECT rowid, ts_last_epoch, attempt_id, kind, state, task_id, root_task_id, "
    "parent_task_id, model, provider, category, source, ts_last, prompt_tokens, "
    "completion_tokens, cached_tokens, cache_write_tokens, weight, late_receipt, length(extra), "
)
_STREAM_TAIL = (
    " FROM attempts WHERE category IS ?2 AND ts_last_epoch >= ?3 AND ts_last_epoch <= ?4 "
    "ORDER BY ts_last_epoch DESC, rowid DESC LIMIT ?5"
)
STREAM_SQL_JSON = _STREAM_HEAD + (
    "CASE WHEN length(extra) > ?1 THEN NULL WHEN json_valid(extra) THEN json_type(extra) "
    "ELSE 'malformed' END, "
    "CASE WHEN length(extra) <= ?1 AND json_valid(extra) "
    "AND json_type(extra, '$.physical_context') = 'object' "
    "THEN json_extract(extra, '$.physical_context') END"
) + _STREAM_TAIL
STREAM_SQL_TEXT = _STREAM_HEAD + (
    "NULL, CASE WHEN length(extra) <= ?1 THEN extra END"
) + _STREAM_TAIL
STREAM_COLUMNS = (
    "rowid", "ts_last_epoch", "attempt_id", "kind", "state", "task_id", "root_task_id",
    "parent_task_id", "model", "provider", "category", "source", "ts_last", "prompt_tokens",
    "completion_tokens", "cached_tokens", "cache_write_tokens", "weight", "late_receipt",
    "extra_len", "extra_shape", "extra_text",
)
NEWEST_SQL = (
    "SELECT ts_last_epoch FROM attempts WHERE category IS ? AND ts_last_epoch >= ? "
    "AND ts_last_epoch <= ? ORDER BY ts_last_epoch DESC LIMIT 1"
)
UNTIMED_SQL = (
    "SELECT count(*) FROM (SELECT 1 FROM attempts WHERE category IS ? "
    "AND ts_last_epoch IS NULL LIMIT ?)"
)
BELOW_BAND_SQL = (
    "SELECT count(*) FROM (SELECT 1 FROM attempts WHERE category IS ? "
    "AND ts_last_epoch < ? LIMIT ?)"
)
ABOVE_BAND_SQL = (
    "SELECT count(*) FROM (SELECT 1 FROM attempts WHERE category IS ? "
    "AND ts_last_epoch > ? LIMIT ?)"
)
CATEGORY_SQL = "SELECT key FROM summaries WHERE scope = 'category' ORDER BY key LIMIT ?"


class StoreMissing(Exception):
    """Nothing exists at the store path. The ONLY case a caller may read the
    retired journal instead: an existing store that cannot be read is never
    replaced by older data."""


class StoreUnavailable(Exception):
    """The store exists but is not read. Carries a typed code, never a path,
    never an exception message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Budget:
    """SQLite progress handler: a soft bound for the merge, a hard bound for all."""

    def __init__(self, hard_sec: float) -> None:
        self.started = time.monotonic()
        self.hard_deadline = self.started + hard_sec
        self.soft_deadline: Optional[float] = None
        self.soft_steps: Optional[int] = None
        self.steps = 0
        self.tripped: Optional[str] = None

    def arm(self, seconds: float, steps: int) -> None:
        self.soft_deadline = time.monotonic() + seconds
        self.soft_steps = self.steps + steps

    def disarm(self) -> None:
        self.soft_deadline = None
        self.soft_steps = None

    def __call__(self) -> int:
        self.steps += PROGRESS_INTERVAL
        now = time.monotonic()
        if now > self.hard_deadline:
            self.tripped = "hard"
            return 1
        if self.soft_deadline is not None and now > self.soft_deadline:
            self.tripped = "time_budget"
            return 1
        if self.soft_steps is not None and self.steps > self.soft_steps:
            self.tripped = "step_budget"
            return 1
        return 0


def _identity(path: str) -> Tuple[int, int]:
    info = os.lstat(path)
    if _stat.S_ISLNK(info.st_mode):
        raise StoreUnavailable("store_not_confined")
    if not _stat.S_ISREG(info.st_mode):
        raise StoreUnavailable("store_not_regular")
    return int(info.st_dev), int(info.st_ino)


def store_path(data_dir: str) -> Tuple[str, Tuple[int, int]]:
    """The fixed store path and its identity, checked without opening anything.

    The serving root and ``state`` must be real directories (a symlink at either
    could steer SQLite to another tree), and the store a regular file that is
    not a symlink. ``StoreMissing`` is raised ONLY when nothing exists at the
    file's name.
    """
    root = os.path.abspath(str(data_dir))
    try:
        for directory in (root, os.path.join(root, STORE_REL_PARTS[0])):
            info = os.lstat(directory)
            if _stat.S_ISLNK(info.st_mode):
                raise StoreUnavailable("store_not_confined")
            if not _stat.S_ISDIR(info.st_mode):
                raise StoreUnavailable("store_not_regular")
    except FileNotFoundError:
        raise StoreMissing() from None
    except OSError:
        raise StoreUnavailable("store_unreadable") from None
    path = os.path.join(root, *STORE_REL_PARTS)
    try:
        identity = _identity(path)
    except FileNotFoundError:
        raise StoreMissing() from None
    except OSError:
        raise StoreUnavailable("store_unreadable") from None
    # The store runs a rollback journal. A live `-wal` beside it means a WAL
    # file, and SQLite creates WAL side files for any connection that opens
    # one — so that store is refused here, before anything opens it.
    if os.path.lexists(path + "-wal"):
        raise StoreUnavailable("store_unsupported")
    return path, identity


def connect(path: str) -> sqlite3.Connection:
    """A strictly read-only connection to the live store.

    ``mode=ro`` (never creates, never writes), no ``immutable`` and no
    ``nolock`` (the store is live and its file locks are the protocol), a short
    busy wait, and ``query_only`` as a second refusal of any write.
    """
    uri = "file:%s?mode=ro" % _uri_path(path)
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SEC, isolation_level=None)
    except sqlite3.Error as exc:
        raise _typed(exc) from None
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        conn.close()
        raise _typed(exc) from None
    return conn


def _uri_path(path: str) -> str:
    from urllib.parse import quote

    return quote(os.path.abspath(path), safe="/")


def _typed(exc: BaseException) -> StoreUnavailable:
    code = getattr(exc, "sqlite_errorcode", None)
    text = str(exc).lower()
    if code in (5, 6) or "locked" in text or "busy" in text:
        return StoreUnavailable("store_busy")
    return StoreUnavailable("store_unreadable")


def _meta_json(rows: Dict[str, Any], key: str) -> Any:
    raw = rows.get(key)
    if not isinstance(raw, str) or len(raw) > MAX_META_CHARS:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _validate(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Prove this is the store version and lock tier this reader understands.

    Runs inside the read transaction; the first statement takes the shared lock.
    The application id is checked FIRST, so on a name-tier store nothing but the
    header is ever read.
    """
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    if application_id == APPLICATION_ID_NAME:
        raise StoreUnavailable("store_name_tier")
    if application_id != APPLICATION_ID_ENFORCED:
        raise StoreUnavailable("store_unsupported")
    mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
    if mode == "wal":
        # Core's store runs a rollback journal (docs/USAGE_STORE.md). A WAL file
        # is refused; note that SQLite may already have created its side files
        # when it opened this one — see store_path() for the pre-open check.
        raise StoreUnavailable("store_unsupported")
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    if not {"attempts", "summaries", "meta", CATEGORY_INDEX} <= tables:
        raise StoreUnavailable("store_unsupported")
    meta = {row[0]: row[1] for row in conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('schema_version', 'lock_tier', 'import')")}
    if _meta_json(meta, "schema_version") != SCHEMA_VERSION:
        raise StoreUnavailable("store_unsupported")
    if _meta_json(meta, "lock_tier") != LOCK_TIER_ENFORCED:
        raise StoreUnavailable("store_unsupported")
    provenance = _meta_json(meta, "import")
    if not isinstance(provenance, dict) or provenance.get("status") != "completed":
        raise StoreUnavailable("store_not_ready")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
    if not _REQUIRED_ATTEMPT_COLUMNS <= columns:
        raise StoreUnavailable("store_unsupported")
    indexed = [row[2] for row in conn.execute("PRAGMA index_info(%s)" % CATEGORY_INDEX)]
    if indexed != ["category", "ts_last_epoch"]:
        raise StoreUnavailable("store_unsupported")
    return {"schema_version": SCHEMA_VERSION, "lock_tier": LOCK_TIER_ENFORCED,
            "metadata_read": "sqlite_json" if _json_functions(conn) else "python_text"}


def _json_functions(conn: sqlite3.Connection) -> bool:
    """Does this SQLite build carry the JSON functions? (Built in since 3.38.)"""
    try:
        conn.execute("SELECT json_valid('{}'), json_type('{}'), json_extract('{}', '$.a')").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such function" not in str(exc).lower():
            raise
        return False
    return True


def _categories(conn: sqlite3.Connection, max_categories: int) -> Tuple[List[Optional[str]], bool]:
    """Enumerate summary-backed categories, not DISTINCT categories of all rows.

    Legacy-only named categories can be absent; discovering them would require
    a history scan. NULL and empty streams still include their legacy rows.
    """
    keys = [row[0] for row in conn.execute(CATEGORY_SQL, (max_categories + 1,))]
    capped = len(keys) > max_categories
    named = [key for key in keys[:max_categories]
             if isinstance(key, str) and key and len(key) <= MAX_CATEGORY_LEN]
    capped = capped or len(named) < len(keys[:max_categories])
    # NULL and the empty string have no summary row of their own.
    return [None, ""] + named, capped


def read_store(
    data_dir: str,
    *,
    lower_s: float,
    upper_s: float,
    band_lower_s: float,
    max_rows: int = MAX_ROWS,
    max_categories: int = MAX_CATEGORIES,
    max_extra_chars: int = MAX_EXTRA_CHARS,
    max_extra_total: int = MAX_EXTRA_TOTAL_CHARS,
    merge_budget_sec: float = MERGE_BUDGET_SEC,
    merge_step_budget: int = MERGE_STEP_BUDGET,
    hard_budget_sec: float = HARD_BUDGET_SEC,
) -> Dict[str, Any]:
    """The newest rows in enumerated categories with ``lower_s <= ts_last_epoch <= upper_s``.

    ``band_lower_s`` / ``upper_s`` are the admitted clock band: a row outside it
    (or with no time at all) cannot be placed, so it is counted, never selected.
    Returns plain column values plus the facts of the read; the transaction is
    closed before this returns, so nothing downstream holds the store's lock.
    """
    path, before = store_path(data_dir)
    budget = _Budget(hard_budget_sec)
    started = time.monotonic()
    conn = connect(path)
    rows: List[Dict[str, Any]] = []
    partial: List[str] = []
    began = ended = None
    try:
        conn.set_progress_handler(budget, PROGRESS_INTERVAL)
        try:
            conn.execute("BEGIN")
            began = time.monotonic()
            facts = _validate(conn)
            categories, category_capped = _categories(conn, max_categories)
            if category_capped:
                partial.append("category_cap")
            newest = None
            untimed = 0
            untimed_capped = False
            for category in categories:
                found = conn.execute(NEWEST_SQL, (category, band_lower_s, upper_s)).fetchone()
                if found and isinstance(found[0], (int, float)):
                    newest = found[0] if newest is None else max(newest, found[0])
                for sql, args in (
                    (UNTIMED_SQL, (category, UNKNOWN_COUNT_CAP + 1)),
                    (BELOW_BAND_SQL, (category, band_lower_s, UNKNOWN_COUNT_CAP + 1)),
                    (ABOVE_BAND_SQL, (category, upper_s, UNKNOWN_COUNT_CAP + 1)),
                ):
                    count = int(conn.execute(sql, args).fetchone()[0])
                    if count > UNKNOWN_COUNT_CAP:
                        untimed_capped = True
                        count = UNKNOWN_COUNT_CAP
                    untimed += count
            sql = STREAM_SQL_JSON if facts["metadata_read"] == "sqlite_json" else STREAM_SQL_TEXT
            overflow, stopped = _merge(conn, sql, categories, rows, budget,
                                       lower_s=lower_s, upper_s=upper_s, max_rows=max_rows,
                                       max_extra_chars=max_extra_chars,
                                       max_extra_total=max_extra_total,
                                       merge_budget_sec=merge_budget_sec,
                                       merge_step_budget=merge_step_budget)
            if overflow:
                partial.append("row_cap")
            if stopped:
                partial.append(stopped)
        except sqlite3.Error as exc:
            if budget.tripped == "hard":
                raise StoreUnavailable("store_slow") from None
            raise _typed(exc) from None
    finally:
        # A read transaction has nothing to commit: ending it is the release.
        conn.set_progress_handler(None, 0)
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        ended = time.monotonic()
    try:
        after = _identity(path)
    except (OSError, StoreUnavailable):
        raise StoreUnavailable("store_replaced") from None
    if after != before:
        raise StoreUnavailable("store_replaced")
    # The transaction is closed: parsing happens on this side of it.
    json_functions = facts["metadata_read"] == "sqlite_json"
    for row in rows:
        _metadata(row, json_functions, max_extra_chars)
    facts.update({
        "category_enumeration": "summary_keys_plus_null_and_empty",
        "legacy_category_coverage": "not_guaranteed",
        "rows_selected": len(rows),
        "partial_reasons": partial,
        "newest_epoch_s": newest,
        "unknown_timestamp": untimed,
        "unknown_timestamp_capped": untimed_capped,
        "categories_read": len(categories),
        "read_ms": int(round((time.monotonic() - started) * 1000)),
        # How long this read held the store's shared lock (writers' COMMIT waits on it).
        "transaction_ms": int(round(((ended or started) - (began or started)) * 1000)),
        "sql_steps": budget.steps,
    })
    return {"rows": rows, "facts": facts}


def _merge(
    conn: sqlite3.Connection,
    sql: str,
    categories: Sequence[Optional[str]],
    rows: List[Dict[str, Any]],
    budget: _Budget,
    *,
    lower_s: float,
    upper_s: float,
    max_rows: int,
    max_extra_chars: int,
    max_extra_total: int,
    merge_budget_sec: float,
    merge_step_budget: int,
) -> Tuple[bool, Optional[str]]:
    """Merge the per-category streams, newest first, up to ``max_rows`` rows.

    Answers ``(overflow, stopped)``: overflow when row ``max_rows + 1`` exists,
    stopped when a soft bound ended the merge early. Either way ``rows`` is an
    exact newest-first prefix of the selection: a row is taken only once it is
    newer than every other stream's head, and a stream's unread rows are never
    newer than the row it last yielded.
    """
    cursors: List[sqlite3.Cursor] = []
    heap: List[Tuple[float, int, int, tuple]] = []
    extra_total = 0
    seen = set()
    overflow = False
    stopped: Optional[str] = None
    try:
        # Every stream must have its head before anything is taken, so these
        # opening reads run under the hard bound only.
        for index, category in enumerate(categories):
            cursor = conn.execute(sql, (max_extra_chars, category, lower_s, upper_s, max_rows + 1))
            cursors.append(cursor)
            head = cursor.fetchone()
            if head is not None:
                heapq.heappush(heap, (-float(head[1]), -int(head[0]), index, head))
        budget.arm(merge_budget_sec, merge_step_budget)
        while heap:
            _, _, index, head = heapq.heappop(heap)
            if len(rows) >= max_rows:
                overflow = True
                break
            # Bound what crosses into Python: the metadata text this row carries.
            read = len(head[21]) if isinstance(head[21], str) else 0
            if extra_total + read > max_extra_total:
                stopped = "byte_cap"
                break
            extra_total += read
            if head[2] not in seen:
                seen.add(head[2])
                rows.append(dict(zip(STREAM_COLUMNS, head)))
            try:
                following = cursors[index].fetchone()
            except sqlite3.OperationalError:
                if budget.tripped in ("time_budget", "step_budget"):
                    stopped = budget.tripped
                    budget.tripped = None
                    break
                raise
            if following is not None:
                heapq.heappush(heap, (-float(following[1]), -int(following[0]), index, following))
    finally:
        budget.disarm()
        for cursor in cursors:
            try:
                cursor.close()
            except sqlite3.Error:
                pass
    return overflow, stopped


def _loads(text: Any) -> Any:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _metadata(row: Dict[str, Any], json_functions: bool, max_extra_chars: int) -> None:
    """Replace the raw metadata columns with ``physical_context`` and a status.

    ``physical_context`` is the only key of ``extra`` this skill ever reads; the
    status is ``ok`` / ``absent`` when it was read, ``unread`` when the metadata
    was larger than the per-row bound and ``malformed`` when it was not a JSON
    object. Nothing else from ``extra`` survives this function.
    """
    length = row.pop("extra_len", None)
    shape = row.pop("extra_shape", None)
    text = row.pop("extra_text", None)
    context: Optional[Dict[str, Any]] = None
    status = "absent"
    if isinstance(length, int) and length > max_extra_chars:
        status = "unread"
    elif json_functions:
        if shape is not None and shape != "object":
            status = "malformed"
        else:
            parsed = _loads(text)
            if isinstance(parsed, dict):
                context, status = parsed, "ok"
    elif isinstance(text, str):
        parsed = _loads(text)
        if not isinstance(parsed, dict):
            status = "malformed"
        elif isinstance(parsed.get("physical_context"), dict):
            context, status = parsed["physical_context"], "ok"
    row["physical_context"] = context
    row["extra_status"] = status
