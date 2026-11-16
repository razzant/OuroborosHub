"""Incremental history index over rotated event archives, owned by this skill.

Storage is one SQLite database in the skill's own state directory (or memory when the
host gives none), WAL with durable (FULL) commits. Each archive is parsed once and written in one transaction: its
normalized requests (hashed identity fingerprint, time, provider/model/kind, counts and
legacy-zero bits; never prompts or task ids), its session candidates, its code starts
and honest content facts (torn tail, malformed lines). Requests also add to additive
15-minute cells per provider/model/kind, which is all a long range reads; an edge
between two records of one cell is refined from that cell's rows. Work is proportional
to the new archive, and one owned thread does all disk and decoding work. A published
view is immutable; GET handlers read it and never wait for a pass. Every write transaction
advances one commit count, and a view carries the count it shows: a reading reads
the database rows it needs (edge cells, identity checks) inside one read transaction at that
same count, or starts again on the newer view.

The earlier JSON index (``state/cache_efficiency_snapshot/history-index.json`` of
0.8.x) is read once, never renamed or written: its entries whose archive is no longer
on disk are imported as restored sources (their zeros carry no contract stamp, so they
are unknown); archives still on disk are parsed again, exactly.
"""
from __future__ import annotations

import copy
import json
import mmap
import os
import re
import sqlite3
import threading
import time
from array import array
from bisect import bisect_left, bisect_right, insort
from datetime import datetime, timedelta, timezone
from pathlib import Path

CELL_US = 15 * 60 * 1_000_000  # every modern UTC offset and DST edge falls on this grid
DAY_US = 96 * CELL_US
FORMAT, VERSION = "cache-efficiency-history", "2"
VECTOR_VERSION = "1"  # bump with the plugin's VECTOR_FIELDS: cells are then rebuilt from rows
RECHECK_SECONDS = 30
VERIFY_SECONDS = 6 * 3600  # how often every indexed archive's identity is checked again (also at each load)
PUBLISH_SECONDS = 0.5
BATCH_ARCHIVES = 64  # archives per transaction during a backfill (also bounded by PUBLISH_SECONDS)
READ_BLOCK = 8 << 20
_MARKERS = (b'"llm_usage"', b'"delegate_run_ledger_recorded"', b'"worker_boot"')
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ROW_MIGRATED, SESSION_NORMALIZED, SESSION_FACTS, SESSION_MIGRATED = 8, 8, 16, 32
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS segments(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,"
    " size INTEGER, mtime_ns INTEGER, low INTEGER, high INTEGER, stats TEXT NOT NULL, fps BLOB)",
    "CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,"
    " kind TEXT NOT NULL, UNIQUE(provider, model, kind))",
    "CREATE TABLE IF NOT EXISTS rows(seg INTEGER NOT NULL, ord INTEGER NOT NULL, fp INTEGER NOT NULL,"
    " digest BLOB NOT NULL, ts INTEGER NOT NULL, gid INTEGER NOT NULL, p INTEGER, c INTEGER, w INTEGER,"
    " flags INTEGER NOT NULL, counted INTEGER NOT NULL, PRIMARY KEY(seg, ord)) WITHOUT ROWID",
    "CREATE INDEX IF NOT EXISTS rows_counted ON rows(ts, gid) WHERE counted = 1",
    # The few duplicate copies, so a retracted source's requests find their next copy without a scan of all rows.
    "CREATE INDEX IF NOT EXISTS rows_duplicates ON rows(ts, gid) WHERE counted = 0",
    "CREATE TABLE IF NOT EXISTS cells(cell INTEGER NOT NULL, gid INTEGER NOT NULL, vector BLOB NOT NULL,"
    " low INTEGER NOT NULL, high INTEGER NOT NULL, PRIMARY KEY(cell, gid)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS sessions(seg INTEGER NOT NULL, ord INTEGER NOT NULL, identity TEXT NOT NULL,"
    " ts INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, p INTEGER, c INTEGER, w INTEGER,"
    " flags INTEGER NOT NULL, PRIMARY KEY(seg, ord)) WITHOUT ROWID",
    # Every source's copy of a code start (one per identity and source), so retracting one source never drops a
    # start that another source still holds. Same columns as the first 0.8.1 layout, which _connect migrates.
    "CREATE TABLE IF NOT EXISTS boots(identity TEXT NOT NULL, ts INTEGER NOT NULL, sha TEXT NOT NULL,"
    " branch TEXT NOT NULL, seg INTEGER NOT NULL, PRIMARY KEY(identity, seg)) WITHOUT ROWID",
)
READER_POOL = 4  # idle read-only connections kept for readings
_FACT_KEYS = ("lines", "malformed_lines", "unparsed_events", "boots_without_code", "torn_tails", "duplicates",
              "fingerprint_collisions")


def _us(stamp):
    delta = stamp - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _stamp(value):
    return _EPOCH + timedelta(microseconds=value)


def _pack(values):
    """Integers as one little-endian int64 blob (cell vectors, a source's counted fingerprints)."""
    packed = array("q", values)
    if packed.itemsize != 8:
        raise OverflowError("int64 arrays unavailable")
    return packed.tobytes()


def _unpack(blob):
    values = array("q")
    values.frombytes(blob or b"")
    return values


def _table(db, table, columns):
    """Every row of one of this module's own tables (fixed names, never input) from ONE SQLite step, as JSON. A cursor
    gives up the GIL around each row; while other server threads run Python, each row then waits for it again. SQLite
    without JSON functions fails here (OperationalError): the load degrades, nothing is read another way."""
    return json.loads(db.execute("SELECT json_group_array(json_array(" + columns + ")) FROM " + table).fetchone()[0])


def _in_sorted(values, item):
    index = bisect_left(values, item)
    return index < len(values) and values[index] == item


class StaleView(Exception):
    """The index committed after this view was published: the reading starts again on a newer view."""

    def __init__(self, commits):
        super().__init__(commits)
        self.commits = commits


class HistoryView:
    """One published, immutable reading of the index. Its parts are shared copy-on-write with the index, so a
    publication costs what changed (days touched, new sessions), never a copy of all history.

    The rows a reading still reads from the database (cells an edge cuts, identity checks) must be the rows of this
    same commit: ``HistoryIndex.bind`` gives a reading a copy whose reads share one read transaction at it."""

    _query = None  # set on a bound copy: that reading's read transaction

    def __init__(self, index, groups, days, sessions, session_order, boots, boot_stamps, fp_base, fp_chunks,
                 oldest, newest, total, commits=0):
        self._index, self.groups, self._days = index, groups, days
        self._fingerprints_ready = not index._fp_pending
        self.commits = commits  # committed write transactions this view shows
        self._day_keys = sorted(days)
        self._group_ids = {key: gid for gid, key in enumerate(groups) if key is not None}
        self.memo, self._memo_lock = {}, threading.Lock()  # derived readings of this view (plugin-owned)
        self.sessions, self._session_order = sessions, session_order
        if session_order:
            # Sessions bound the span too: a reading's end past every request may still cut a session off.
            first, last = _us(session_order[0][0]), _us(session_order[-1][0])
            oldest = first if oldest is None else min(oldest, first)
            newest = last if newest is None else max(newest, last)
        self._boots, self._boot_stamps = boots, boot_stamps
        self._fp_base, self._fp_chunks = fp_base, fp_chunks
        self._oldest, self._newest, self._total = oldest, newest, total

    def remember(self, key, part):
        """Keep a derived reading of this view (at most 16, oldest dropped first); concurrent readings share it."""
        with self._memo_lock:
            while len(self.memo) >= 16:
                self.memo.pop(next(iter(self.memo)))
            self.memo[key] = part

    def _read(self, sql, args):
        if self._query is not None:
            return self._query(sql, args)
        with self._index.bind(self) as bound:  # an unbound view reads at its own commit too, one call at a time
            return bound._query(sql, args)

    def contains(self, fingerprint):
        return any(fingerprint in chunk for chunk in self._fp_chunks) or _in_sorted(self._fp_base, fingerprint)

    def holds(self, record, fingerprint):
        """Whether the index counts exactly this request: the fingerprint rules most records out, and a match is
        confirmed against the stored full identity digest (a fingerprint collision is never a duplicate)."""
        if self._fingerprints_ready and not self.contains(fingerprint):
            return False
        gid = self._group_ids.get((record["provider"], record["model"], record["kind"]))
        digest = bytes.fromhex(record["identity"].split(":", 1)[-1])
        return gid is not None and bool(self._read(
            "SELECT 1 FROM rows WHERE counted = 1 AND ts = ? AND gid = ? AND digest = ? LIMIT 1",
            (_us(record["ts"]), gid, digest)))

    def holds_boot(self, boot):
        """Whether this view has exactly this code start."""
        stamps, index = self._boot_stamps, bisect_left(self._boot_stamps, boot["ts"])
        while index < len(stamps) and stamps[index] == boot["ts"]:
            if self._boots[index]["identity"] == boot["identity"]:
                return True
            index += 1
        return False

    def oldest_us(self):
        return self._oldest

    def newest_us(self):
        return self._newest

    def total_records(self):
        return self._total

    def cells(self, start_us, end_us):
        keys = self._day_keys
        for day in keys[bisect_left(keys, (start_us - CELL_US) // DAY_US):bisect_right(keys, (end_us - 1) // DAY_US)]:
            for cell, inner in self._days[day].items():
                if start_us - CELL_US < cell < end_us:
                    for gid, (vector, low, high) in inner.items():
                        yield cell, gid, vector, low, high

    def rows(self, cell, gid):
        """The counted rows of one cell and group at this view's commit, for an edge that falls between them."""
        found = self._read("SELECT r.ts, g.kind, r.p, r.c, r.w, r.flags FROM rows r JOIN groups g ON g.id = r.gid"
                           " WHERE r.counted = 1 AND r.ts >= ? AND r.ts < ? AND r.gid = ? ORDER BY r.ts",
                           (cell, cell + CELL_US, gid))
        return [(ts, self._index._row_vector(kind, p, c, w, flags)) for ts, kind, p, c, w, flags in found]

    def sessions_between(self, start, end, overrides=()):
        order = self._session_order
        left = bisect_left(order, (start,)) if start is not None else 0
        right = bisect_left(order, (end,))
        return [self.sessions[identity] for _, identity in order[left:right] if identity not in overrides]

    def boots_between(self, start, end):
        first = bisect_left(self._boot_stamps, start) if start is not None else 0
        # The last preceding start establishes whether the first in-range start changed code.
        return list(self._boots[max(0, first - 1):bisect_left(self._boot_stamps, end)])


class HistoryIndex:
    def __init__(self, directory, *, legacy_path, parse_line, rank, classify, vector, fingerprint, empty_stats):
        self.directory = Path(directory) if directory is not None else None
        self.path = self.directory / "history.sqlite3" if self.directory is not None else None
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self._parse_line, self._rank, self._classify, self._vector = parse_line, rank, classify, vector
        self._fingerprint, self._empty_stats = fingerprint, dict(empty_stats)
        self._lock = threading.Lock()      # published state and meta
        self._published = threading.Condition(self._lock)  # a new view: readings waiting for its commit go on
        # The writer connection. The worker holds it for a whole write transaction, so a reading of the in-memory
        # database (one shared connection) never sees an uncommitted row; such a reading holds it while it reads.
        self._db_lock = threading.RLock()
        self._readers, self._reader_lock = [], threading.Lock()  # idle read-only connections (WAL readings)
        self._commits = 0                  # the commit count the in-memory state (and so the next view) shows
        self._ready = threading.Event()    # a stored view is published, or the first pass ended/failed
        self._kept = {}                    # archive name -> changed identity whose bytes did not replace the indexed copy
        self._stop = threading.Event()
        self._thread = None
        self._db = None
        self._loaded = False
        self._view = None
        self._last_check = 0.0
        self._last_verify = 0.0
        self._directory_stamp = None
        self._retry = False
        self._again = False                # set during a pass: the next request starts another one at once
        self._reset_state()
        self._meta = {"state": "not_loaded", "complete": False, "refreshing": False, "revision": 0,
                      "storage": "sqlite" if self.path else "memory", "archives_total": 0, "archives_done": 0,
                      "archives_indexed": 0, "retained_sources_absent": 0, "restored_sources": 0,
                      "read_error": None, "recovery": None, "recovery_incomplete": False, "legacy_import": None}
        self._rebuilt = None               # why this index was rebuilt from an unreadable file, if it was
        self._legacy = None                # what reading the earlier JSON index found (kept in meta 'legacy')
        self._consistent = False           # memory holds a whole loaded state (see _load_tables)

    def _reset_state(self):
        self._groups, self._group_ids = [], {}
        self._days, self._oldest, self._newest, self._total = {}, None, None, 0  # UTC day -> cell -> group
        self._sessions = {}          # identity -> (order key, record): the winner of each run
        self._session_order = []     # sorted (time, identity) of the winners, kept by insertion
        self._boots, self._boot_list, self._boot_stamps = {}, [], []
        self._published_sessions = self._published_boots = None  # copied only after a change
        self._fp_base, self._fp_chunks, self._fp_recent = array("q"), [], 0  # sorted base + per-batch frozensets
        self._segments = {}          # name -> {"id", "kind", "size", "mtime_ns", "facts"}
        self._facts = dict.fromkeys(_FACT_KEYS, 0)
        self._published_at, self._loading, self._fp_pending = 0.0, False, False

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        with self._lock:
            self._stop.set()
            self._published.notify_all()
        self._ready.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._drop_readers()
        if thread is None or not thread.is_alive():
            self._close_db()

    def wait_ready(self, timeout):
        """Wait for a stored view, or the empty index's first pass (cold start); later passes never block here."""
        return self._ready.wait(timeout)

    def wait_published(self, commits, timeout):
        """Wait up to ``timeout`` seconds for a published view at ``commits`` or later (one follows each commit)."""
        with self._published:
            return self._published.wait_for(
                lambda: self._stop.is_set() or (self._view is not None and self._view.commits >= (commits or 0)), timeout)

    def bind(self, view):
        """A reading of ``view``: its database reads share one read transaction at the view's commit.

        Raises StaleView when the database has moved past the view (a commit not yet published, or another writer);
        the reading then starts again on the newer view, so it never mixes rows of two commits."""
        return _Bound(self, view)

    def note_stale(self):
        """A reading found the database ahead of every published view: the next request starts a pass, which loads
        the database again when another writer committed to it (a pass already running is followed by one)."""
        with self._lock:
            self._retry = self._again = True

    def _lease(self):
        with self._reader_lock:
            if self._stop.is_set():
                raise sqlite3.ProgrammingError("history index closed")
            if self._readers:
                return self._readers.pop()
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15, isolation_level=None,
                               check_same_thread=False)

    def _drop_readers(self):
        """Idle read connections go (a reopened index may be a new file); leased ones close on release."""
        with self._reader_lock:
            idle, self._readers = self._readers, []
        for connection in idle:
            connection.close()

    def _release(self, connection):
        with self._reader_lock:
            if not self._stop.is_set() and len(self._readers) < READER_POOL:
                self._readers.append(connection)
                return
        connection.close()

    def _stored_commits(self, db):
        found = db.execute("SELECT value FROM meta WHERE key = 'commits'").fetchone()
        return int(found[0]) if found else 0

    def _bump(self, db, expected=None):
        """Inside an open write transaction: the commit count once this transaction commits.

        A transaction staged from memory (``expected``: the count memory shows) commits only on top of that same
        state: if another writer committed since memory was loaded, it rolls back and the index is loaded again,
        so a published view's count always names exactly the rows its memory holds."""
        stored = self._stored_commits(db)
        if expected is not None and stored != expected:
            raise sqlite3.IntegrityError("another writer committed to the history index")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('commits', ?)", (str(stored + 1),))
        return stored + 1

    def _rollback(self):
        db = self._db
        if db is not None and db.in_transaction:
            db.execute("ROLLBACK")

    def _close_db(self):
        with self._db_lock:
            if self._db is not None:
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass
                self._db = None

    def request(self, archive_dir):
        """Start or continue a background pass; never waits for it."""
        with self._lock:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return
            if self._loaded and self._last_check and not self._retry and time.monotonic() - self._last_check < RECHECK_SECONDS:
                return
            self._meta.update(refreshing=True, state=self._meta["state"] if self._view else "loading")
            self._thread = threading.Thread(target=self._run, args=(Path(archive_dir),),
                                            name="cache-history-index", daemon=True)
            self._thread.start()

    def update(self, archive_dir):
        """Synchronous pass for tests and batch use; the HTTP consumer uses request()."""
        self._run(Path(archive_dir))
        return self.view()

    def view(self):
        with self._lock:
            return self._view

    def meta(self):
        with self._lock:
            return dict(self._meta)

    # -- worker ------------------------------------------------------------

    def _run(self, archive_dir):
        started, error = time.monotonic(), None
        with self._lock:
            self._again = False
        try:
            if not self._loaded:
                with self._lock:
                    # Until the first listing every older reading may still gain records.
                    self._meta["complete_after_us"] = _us(datetime.now(timezone.utc))
                self._open()
                self._migrate_legacy(archive_dir)
                self._loaded = True
                if self._publish(force=True) and self._segments:
                    # Stored history is shown at once, as loading until the first listing (complete_after_us); only
                    # an empty index lets a reading wait briefly for a small first backfill.
                    self._release_readers()
            elif str((self._legacy or {}).get("status", "")).startswith("pending:"):
                # Keep the good index; retry only on the normal background cadence, without reopening it.
                self._migrate_legacy(archive_dir)
                self._publish(force=True)
            error = self._pass(archive_dir)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
            error = "history_index_error:" + type(exc).__name__
        finally:
            if not self._consistent:
                self._loaded = False  # a load that failed half way is done again, from the database, next time
            self._ready.set()
            with self._lock:
                self._retry = bool(error) or self._again
                self._last_check = time.monotonic()
                if not self._stop.is_set():
                    self._meta.update(refreshing=False, read_error=error,
                                      state="degraded" if error else "ready" if self._loaded else "loading",
                                      pass_seconds=round(time.monotonic() - started, 3),
                                      checked_at=datetime.now(timezone.utc).isoformat())
            if self._stop.is_set():
                self._close_db()

    def _open(self):
        recovery = None
        self._close_db()  # a failed earlier attempt leaves no second connection behind
        self._drop_readers()
        if self.path is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                self._connect()
                self._load(fingerprints=False)
            except sqlite3.DatabaseError as exc:
                if isinstance(exc, (sqlite3.OperationalError, sqlite3.IntegrityError, sqlite3.DataError)):
                    # Busy, full, read-only or an I/O error says nothing about the file: it stays the index, the
                    # pass degrades and is tried again. Only a corrupt or foreign file is set aside below.
                    self._close_db()
                    raise
                # Keep the unreadable database for inspection; rebuild from archives and the earlier index.
                self._close_db()
                suffix = ".unreadable-" + str(time.time_ns())
                for extra in ("", "-wal", "-shm"):
                    source = Path(str(self.path) + extra)
                    if source.exists():
                        source.rename(Path(str(self.path) + suffix + extra))
                recovery = ("Rebuilt an unreadable index (" + type(exc).__name__ + ") from the archives on disk and the earlier"
                            " JSON index; observations kept only in the old file are not included (it is kept as "
                            + self.path.name + suffix + ").")
                self._reset_state()
                self._connect()
                with self._db_lock:
                    self._db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery', ?)", (recovery,))
                self._load(fingerprints=False)
        else:
            self._connect()
            self._load(fingerprints=False)
        with self._db_lock:
            kept = self._db.execute("SELECT value FROM meta WHERE key = 'recovery'").fetchone()
        # An incomplete rebuild stays disclosed on every later load, never presented as the whole history.
        self._rebuilt = recovery or (kept[0] if kept else None)
        self._disclose_recovery()

    def _connect(self):
        target = str(self.path) if self.path is not None else ":memory:"
        db = sqlite3.connect(target, timeout=15, isolation_level=None, check_same_thread=False)
        try:
            if self.path is not None:
                # As the core's own store: WAL, a durable commit (FULL) and a bounded journal after checkpoints.
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA journal_size_limit=33554432")
            for statement in _SCHEMA:
                db.execute(statement)
            found = dict(db.execute("SELECT key, value FROM meta").fetchall())
            if found.get("format", FORMAT) != FORMAT or found.get("version", VERSION) != VERSION:
                raise sqlite3.DatabaseError("unsupported history index " + str(found.get("version")))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('format', ?), ('version', ?)", (FORMAT, VERSION))
            self._keep_boots_per_source(db)
        except Exception:
            db.close()
            raise
        with self._db_lock:
            self._db = db

    def _keep_boots_per_source(self, db):
        """The first 0.8.1 layout kept one row per code start (the first source's); from now on every source's copy
        is kept. Migrated in place, in one transaction: every stored row stays, nothing else changes, and the same
        columns stay readable by that earlier version. A failure leaves the old table and is tried at the next load."""
        old_key = {name: key for _, name, _, _, _, key in db.execute("PRAGMA table_info(boots)")}.get("seg") == 0
        if not old_key and db.execute("SELECT 1 FROM meta WHERE key = 'boot_sources'").fetchone():
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            if old_key:
                db.execute("CREATE TABLE boots_by_source(identity TEXT NOT NULL, ts INTEGER NOT NULL, sha TEXT NOT NULL,"
                           " branch TEXT NOT NULL, seg INTEGER NOT NULL, PRIMARY KEY(identity, seg)) WITHOUT ROWID")
                db.execute("INSERT INTO boots_by_source SELECT identity, ts, sha, branch, seg FROM boots")
                db.execute("DROP TABLE boots")
                db.execute("ALTER TABLE boots_by_source RENAME TO boots")
            # Earlier layouts discarded duplicate boot sources. Replay just code starts from still-present
            # archives once, BEFORE retracting any source. A cursor makes interruption resumable; all previously
            # retained observations stay in place, including sources whose raw archives have been retired.
            state = {"after": ""} if db.execute("SELECT 1 FROM segments LIMIT 1").fetchone() else {"done": True}
            db.execute("INSERT OR IGNORE INTO meta VALUES ('boot_sources', ?)", (json.dumps(state),))
            self._bump(db)
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def _restore_boot_sources(self, archive_dir):
        with self._db_lock:
            state = json.loads(self._db.execute("SELECT value FROM meta WHERE key = 'boot_sources'").fetchone()[0])
        if state.get("done"):
            return
        names = sorted(name for name in self._segments if name > state.get("after", ""))
        for name in names:
            if self._stop.is_set():
                raise InterruptedError("closed during boot migration")
            boots, path = [], archive_dir / name
            try:
                stat = path.stat()
            except FileNotFoundError:
                pass  # stored starts remain; a retired raw source cannot supply its discarded duplicates
            else:
                def consume(raw):
                    if b'"worker_boot"' in raw:
                        parsed = self._parse_line(raw, dict(self._empty_stats))
                        if parsed is not None and parsed[0] == "boot":
                            boots.append(parsed[1])
                if self._read_lines(path, (stat.st_size, stat.st_mtime_ns), consume) is None:
                    raise InterruptedError("closed during boot migration")
            batch = _Batch()
            batch.boots = boots
            with self._db_lock:
                db = self._db
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.executemany("INSERT OR IGNORE INTO boots VALUES (?, ?, ?, ?, ?)",
                                   ((b["identity"], _us(b["ts"]), b["sha"], b["branch"], self._segments[name]["id"])
                                    for b in boots))
                    db.execute("UPDATE meta SET value = ? WHERE key = 'boot_sources'", (json.dumps({"after": name}),))
                    batch.commits = self._bump(db, self._commits)
                    db.execute("COMMIT")
                except BaseException:
                    self._rollback()
                    raise
                self._apply(batch)
            self._publish(force=True)
        with self._db_lock:
            self._db.execute("UPDATE meta SET value = ? WHERE key = 'boot_sources'", (json.dumps({"done": True}),))

    def _load(self, fingerprints=True):
        """Everything a view needs, read once per process: cells, sessions, code starts; then the fingerprints the
        worker deduplicates with (deferred on a cold open until the first view is out)."""
        with self._db_lock:
            db = self._db
            vector = db.execute("SELECT value FROM meta WHERE key = 'vector'").fetchone()
            if vector is not None and vector[0] != VECTOR_VERSION:
                self._rebuild_cells(db)
            elif vector is None:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('vector', ?)", (VECTOR_VERSION,))
            db.execute("BEGIN")  # one consistent reading of every table, with the commit count it shows
            try:
                self._load_tables(db)
                # Dedup runs in memory: each source keeps its counted fingerprints as one packed blob, so a load reads
                # a few kilobytes per archive instead of every row (and no index on random keys slows each write).
                self._fp_pending = True
                if fingerprints:
                    self._read_fingerprints(db)
            finally:
                if db.in_transaction:
                    db.execute("COMMIT")

    def _load_tables(self, db):
        """Every table a view needs, inside the caller's read transaction, with the commit count it shows. Until it
        completes, memory is not a state to publish (a failed load is loaded again by the next pass)."""
        self._consistent = False
        self._reset_state()
        self._commits = self._stored_commits(db)
        for gid, provider, model, kind in db.execute("SELECT id, provider, model, kind FROM groups ORDER BY id"):
            self._remember_group(gid, (provider, model, kind))
        names = {}
        for seg, name, kind, size, mtime_ns, stats in _table(db, "segments", "id, name, kind, size, mtime_ns, stats"):
            self._segments[name] = {"id": seg, "kind": kind, "size": size, "mtime_ns": mtime_ns,
                                    "facts": json.loads(stats)}
            names[seg] = name
        stored = dict(db.execute("SELECT key, value FROM meta").fetchall())
        self._kept = json.loads(stored.get("kept") or "{}")
        for facts in (segment["facts"] for segment in self._segments.values()):
            for key in _FACT_KEYS:
                self._facts[key] += facts.get(key, 0)
        days = self._days
        for cell, gid, vector, low, high in _table(db, "cells", "cell, gid, hex(vector), low, high"):
            vector = tuple(_unpack(bytes.fromhex(vector)))
            days.setdefault(cell // DAY_US, {}).setdefault(cell, {})[gid] = (vector, low, high)
            self._total += vector[0]
            self._oldest = low if self._oldest is None else min(self._oldest, low)
            self._newest = high if self._newest is None else max(self._newest, high)
        self._loading = True  # winners first, one sort after
        for seg, order, identity, ts, provider, model, p, c, w, flags in _table(
                db, "sessions", "seg, ord, identity, ts, provider, model, p, c, w, flags"):
            self._offer_session((names.get(seg, ""), order), self._session_record(identity, ts, provider, model, p, c, w, flags))
        self._loading = False
        self._session_order = sorted((record["ts"], identity) for identity, (_, record) in self._sessions.items())
        for identity, ts, sha, branch in _table(db, "boots", "identity, ts, sha, branch"):
            # One code start per identity, whichever sources still hold a copy of it.
            self._boots[identity] = {"ts": _stamp(ts), "sha": sha, "branch": branch, "identity": identity}
        self._boot_list = sorted(self._boots.values(), key=lambda boot: boot["ts"])
        self._boot_stamps = [boot["ts"] for boot in self._boot_list]
        self._consistent = True

    def _load_fingerprints(self):
        with self._db_lock:
            self._read_fingerprints(self._db)

    def _read_fingerprints(self, db):
        """Each source's counted fingerprints (one packed blob per archive) as a sorted base, and the last days'
        as a set for the live reader's rotated requests; neither reads the rows table."""
        fingerprints, recent = array("q"), array("q")
        recent_from = _us(datetime.now(timezone.utc) - timedelta(days=9))
        for blob, high in db.execute("SELECT fps, high FROM segments"):
            fingerprints.frombytes(blob or b"")
            if high is not None and high >= recent_from:
                recent.frombytes(blob or b"")
        self._fp_base = array("q", sorted(fingerprints))
        self._fp_chunks = [frozenset(recent)]
        self._fp_pending = False

    def _rebuild_cells(self, db):
        """The cell vectors follow the plugin's arithmetic; a new definition recomputes them from the rows."""
        cells = {}
        for ts, gid, kind, p, c, w, flags in db.execute(
                "SELECT r.ts, r.gid, g.kind, r.p, r.c, r.w, r.flags FROM rows r JOIN groups g ON g.id = r.gid WHERE r.counted = 1"):
            vector = self._row_vector(kind, p, c, w, flags)
            key = (ts - ts % CELL_US, gid)
            entry = cells.get(key)
            cells[key] = (vector, ts, ts) if entry is None else (
                [a + b for a, b in zip(entry[0], vector)], min(entry[1], ts), max(entry[2], ts))
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM cells")
            db.executemany("INSERT INTO cells VALUES (?, ?, ?, ?, ?)",
                           ((cell, gid, _pack(v), low, high) for (cell, gid), (v, low, high) in cells.items()))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('vector', ?)", (VECTOR_VERSION,))
            self._bump(db)
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def _recount_cells(self, db, keys):
        """Inside an open write transaction: these cells (cell, group) recomputed from their counted rows, exactly
        the arithmetic of a whole rebuild, so a retraction costs the cells it touched, not all history."""
        for cell, gid in keys:
            vector = low = high = None
            for ts, kind, p, c, w, flags in db.execute(
                    "SELECT r.ts, g.kind, r.p, r.c, r.w, r.flags FROM rows r JOIN groups g ON g.id = r.gid"
                    " WHERE r.counted = 1 AND r.ts >= ? AND r.ts < ? AND r.gid = ?", (cell, cell + CELL_US, gid)):
                row = self._row_vector(kind, p, c, w, flags)
                vector = row if vector is None else [a + b for a, b in zip(vector, row)]
                low = ts if low is None else min(low, ts)
                high = ts if high is None else max(high, ts)
            if vector is None:
                db.execute("DELETE FROM cells WHERE cell = ? AND gid = ?", (cell, gid))
            else:
                db.execute("INSERT OR REPLACE INTO cells VALUES (?, ?, ?, ?, ?)", (cell, gid, _pack(vector), low, high))

    # -- in-memory state ---------------------------------------------------

    def _remember_group(self, gid, key):
        while len(self._groups) <= gid:
            self._groups.append(None)
        self._groups[gid] = key
        self._group_ids[key] = gid

    def _cell(self, cell, gid):
        return self._days.get(cell // DAY_US, {}).get(cell, {}).get(gid)

    def _set_cell(self, cell, gid, vector, low, high):
        day = cell // DAY_US
        cells = dict(self._days.get(day, {}))  # copy on write: a published view keeps its own dicts
        inner = dict(cells.get(cell, {}))
        previous = inner.get(gid)
        inner[gid] = (vector, low, high)
        cells[cell] = inner
        self._days[day] = cells
        self._total += vector[0] - (previous[0][0] if previous else 0)
        self._oldest = low if self._oldest is None else min(self._oldest, low)
        self._newest = high if self._newest is None else max(self._newest, high)

    def _row_vector(self, kind, p, c, w, flags):
        measured = p is not None and c is not None
        return self._vector({"kind": kind, "prompt_tokens": p, "cached_tokens": c, "cache_write_tokens": w,
                             "measured": measured, "eligible": measured and p > 0, "legacy_zeros": flags & 7})

    def _session_record(self, identity, ts, provider, model, p, c, w, flags):
        measured = p is not None and c is not None
        record = {"ts": _stamp(ts), "kind": "subscription_session", "provider": provider, "model": model,
                  "display_name": str(model or "unknown").rsplit("/", 1)[-1].rsplit("::", 1)[-1],
                  "prompt_tokens": p, "cached_tokens": c, "cache_write_tokens": w, "measured": measured,
                  "eligible": measured and p > 0, "uncached_tokens": p - c if measured else None,
                  "legacy_zeros": flags & 7, "identity": identity, "token_facts": bool(flags & SESSION_FACTS),
                  "normalized": bool(flags & SESSION_NORMALIZED)}
        record["_vector"] = tuple(self._vector(record))
        return record

    def _offer_session(self, order, record):
        """Evidence rank first; equal evidence keeps the earliest source (archive name, then line)."""
        current = self._sessions.get(record["identity"])
        rank = self._rank(record)
        if current is None or rank > self._rank(current[1]) or (rank == self._rank(current[1]) and order < current[0]):
            self._sessions[record["identity"]] = (order, record)
            self._published_sessions = None
            if not self._loading:
                if current is not None:
                    index = bisect_left(self._session_order, (current[1]["ts"], record["identity"]))
                    if index < len(self._session_order) and self._session_order[index] == (current[1]["ts"], record["identity"]):
                        del self._session_order[index]
                insort(self._session_order, (record["ts"], record["identity"]))

    def _contains(self, fingerprint):
        return any(fingerprint in chunk for chunk in self._fp_chunks) or _in_sorted(self._fp_base, fingerprint)

    def _release_readers(self):
        """Readers waiting after a load go on; from here the index is ready, a background check is `refreshing`."""
        with self._lock:
            if self._meta["state"] in ("not_loaded", "loading") and self._view is not None:
                self._meta["state"] = "ready"
        self._ready.set()

    def _publish(self, force=False):
        """Swap in a new view, at most every PUBLISH_SECONDS unless forced; its cost follows what changed."""
        now = time.monotonic()
        if not self._consistent or (not force and now - self._published_at < PUBLISH_SECONDS):
            return False
        self._published_at = now
        if self._published_sessions is None:
            self._published_sessions = ({identity: record for identity, (_, record) in self._sessions.items()},
                                        list(self._session_order))
        if self._published_boots is None:
            self._published_boots = (list(self._boot_list), list(self._boot_stamps))
        view = HistoryView(self, tuple(self._groups), dict(self._days), *self._published_sessions,
                           *self._published_boots, self._fp_base, tuple(self._fp_chunks), self._oldest,
                           self._newest, self._total, self._commits)
        present = getattr(self, "_present", None)
        facts = self._facts
        with self._lock:
            if self._stop.is_set():
                return False
            self._view = view
            self._published.notify_all()
            self._meta["revision"] += 1
            self._meta.update(
                archives_indexed=len(self._segments),
                restored_sources=sum(s["kind"] == "restored" for s in self._segments.values()) if force else
                self._meta.get("restored_sources", 0),
                retained_sources_absent=getattr(self, "_absent", 0) if present is not None else 0,
                indexed_oldest_ts=_stamp(view.oldest_us()).isoformat() if view.oldest_us() is not None else None,
                indexed_newest_ts=_stamp(view.newest_us()).isoformat() if view.newest_us() is not None else None,
                torn_tails=facts["torn_tails"], malformed_lines=facts["malformed_lines"],
                unparsed_events=facts["unparsed_events"], boots_without_code=facts["boots_without_code"],
                duplicate_requests=facts["duplicates"], fingerprint_collisions=facts["fingerprint_collisions"],
                changed_sources_kept=sorted(self._kept)[:20], changed_sources_kept_count=len(self._kept))
        return True

    # -- passes ------------------------------------------------------------

    def _pass(self, archive_dir):
        """Index new archives, newest first, publishing as they land; then, on a verifying pass, check the identity
        of every indexed archive. Readers waiting after a load are released between the two."""
        with self._lock:
            self._meta["archives_read_last_pass"] = 0
        with self._db_lock:
            foreign = self._stored_commits(self._db) != self._commits
        if foreign:
            # Another writer (an instance still unloading) committed: show the database as it is now.
            self._load()
            self._publish(force=True)
        self._restore_boot_sources(archive_dir)
        try:
            directory_stamp = archive_dir.stat().st_mtime_ns
        except FileNotFoundError:
            directory_stamp = None
        if (directory_stamp == self._directory_stamp and self._last_verify and not self._retry and self._meta["complete"]
                and time.monotonic() - self._last_verify < VERIFY_SECONDS):
            return None
        candidates = {}
        if directory_stamp is not None:
            with os.scandir(archive_dir) as entries:
                for entry in entries:
                    if entry.name.startswith("events_") and entry.name.endswith(".jsonl") and entry.is_file():
                        candidates[entry.name] = entry
        self._present = set(candidates)
        self._absent = sum(1 for name in self._segments if name not in self._present)
        # The names list is the one step per rotation that sees every archive; only new names are examined. The
        # first pass after loading, and one every VERIFY_SECONDS, also checks every indexed archive's identity
        # (archives are immutable by the core's contract) after the new ones are in.
        verify = not self._last_verify or not self._meta["complete"] or time.monotonic() - self._last_verify >= VERIFY_SECONDS
        first = not self._meta["complete"]
        fresh, stat_error = [], None
        for name in sorted(candidates, reverse=True):
            if name not in self._segments:
                try:
                    fresh.append((name, candidates[name].stat()))
                except FileNotFoundError:
                    self._present.discard(name)  # retention raced discovery; keep any indexed observations
                    continue
                except OSError as exc:
                    stat_error = "history_stat_error:" + type(exc).__name__
                    continue
        with self._lock:
            self._meta.update(archives_total=len(fresh), archives_done=0,
                              complete_after_us=fresh[0][1].st_mtime_ns // 1000 if first and fresh else None)
            if stat_error:
                # Readers released below see the indexed history as it is, degraded: an archive could not be examined.
                self._meta.update(read_error=stat_error, state="degraded")
        if not fresh:
            self._release_readers()  # a reload with nothing new: the loaded history is the whole reading
        elif self._fp_pending:
            self._load_fingerprints()  # dedup of the new archives needs them; otherwise they load after readers go
        error = self._index_list(archive_dir, fresh, first)
        if error == "interrupted":
            return None
        self._publish_changed()
        with self._lock:
            self._meta["complete_after_us"] = None
        self._release_readers()  # readers waiting after a load see every archive on disk from here on
        if self._fp_pending:
            self._load_fingerprints()
            self._publish(force=True)  # from here a reading's view tells every indexed request from a new one
        if verify:
            self._last_verify = time.monotonic()
            changed = []
            for name in sorted(candidates, reverse=True):
                known = self._segments.get(name)
                if known is None:
                    continue
                try:
                    stat = candidates[name].stat()
                except FileNotFoundError:
                    self._present.discard(name)  # retention raced discovery; keep any indexed observations
                    continue
                except OSError as exc:
                    stat_error = "history_stat_error:" + type(exc).__name__
                    continue
                identity = [stat.st_size, stat.st_mtime_ns]
                if (known["kind"] != "archive" or [known["size"], known["mtime_ns"]] != identity) and self._kept.get(name) != identity:
                    changed.append((name, stat))
            if changed:
                with self._lock:
                    self._meta.update(archives_total=len(changed), archives_done=0)
                failed = self._index_list(archive_dir, changed, False)
                if failed == "interrupted":
                    return None
                if failed:
                    self._last_verify = 0  # a replacement that could not be saved is tried again by the next pass
                error = failed or error
                self._publish_changed()
        if stat_error:
            self._last_verify = 0  # an unreadable identity was not verified; the next pass must examine it again
        else:
            self._directory_stamp = directory_stamp
        absent = sum(1 for name in self._segments if name not in self._present)
        if absent != self._absent:
            self._absent = absent
            self._publish(force=True)
        with self._lock:
            # The first pass has ended. An identity that could not be read is examined again by the next pass
            # (_last_verify and the returned error), which is never a first backfill: no reading turns back to loading.
            self._meta["complete"] = True
        return stat_error or error

    def _publish_changed(self):
        """Publish only what changed: a pass that committed nothing and saw no source leave (an error retried at
        every request, an unchanged listing) keeps the published view and the readings derived from it."""
        with self._lock:
            same = (self._view is not None and self._view.commits == self._commits
                    and self._meta.get("retained_sources_absent") == self._absent)
        if not same:
            self._publish(force=True)

    def _index_list(self, archive_dir, pending, bounded):
        """Parse and write these archives, several per transaction; a changed one replaces its indexed copy only
        when it reads cleanly, in a transaction of its own. Returns the last error, or "interrupted" when the index
        was closed. Memory and the published view follow a commit, never an open transaction."""
        error, batch, handled = None, None, 0

        def publish():
            # Every commit is published at once, so a reading never waits long for the view its rows belong to. Only
            # with no batch open does the completeness bound move: everything handled so far is then visible.
            if self._publish(force=True) and bounded and batch is None:
                with self._lock:
                    self._meta["complete_after_us"] = pending[handled][1].st_mtime_ns // 1000 if handled < len(pending) else None

        def begin():
            # The worker holds the writer connection for the whole transaction (see _db_lock).
            nonlocal batch
            if batch is None:
                self._db_lock.acquire()
                try:
                    self._db.execute("BEGIN IMMEDIATE")
                except BaseException:
                    self._db_lock.release()
                    raise
                batch = _Batch()

        def commit():
            # Several archives share one transaction during a backfill; memory follows only a committed batch.
            nonlocal batch
            if batch is None:
                return
            staged, batch = batch, None
            try:
                if self._stop.is_set():
                    raise InterruptedError("index closed")
                staged.commits = self._bump(self._db, self._commits)
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
            finally:
                self._db_lock.release()
            self._apply(staged)
            with self._lock:
                self._meta["archives_read_last_pass"] += len(staged.segments)
            publish()

        def abandon():
            nonlocal batch
            if batch is not None:
                batch = None
                try:
                    self._rollback()
                finally:
                    self._db_lock.release()

        try:
            for done, (name, stat) in enumerate(pending, 1):
                if self._stop.is_set():
                    abandon()
                    return "interrupted"
                try:
                    parsed = self._parse_archive(archive_dir / name, stat)
                    if parsed is not None and name in self._segments:
                        if not (parsed[4]["torn_tails"] or parsed[4]["malformed_lines"]):
                            # The usage prefilter never hands unmarked lines to the parser: before the only indexed
                            # copy goes, every nonempty line of the new bytes must be a whole JSON object.
                            invalid = self._invalid_lines(archive_dir / name, parsed[0])
                            if invalid is None:
                                abandon()
                                return "interrupted"
                            parsed[4]["malformed_lines"] += invalid
                        if parsed[4]["torn_tails"] or parsed[4]["malformed_lines"]:
                            # A changed archive that does not read cleanly (cut short, damaged) never replaces the
                            # only indexed copy of its rows: the earlier copy stays counted and the change is disclosed.
                            begin()
                            kept = dict(self._kept if batch.kept is None else batch.kept)
                            kept[name] = list(parsed[0])
                            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('kept', ?)", (json.dumps(kept),))
                            batch.kept = kept
                        else:
                            commit()  # what this pass staged before is saved first
                            self._replace(name, parsed)
                            publish()
                        parsed = None
                    if parsed is not None:
                        begin()
                        self._stage(self._db, batch, name, "archive", *parsed, migrated=False)
                    if batch is not None and (len(batch.segments) >= BATCH_ARCHIVES or time.monotonic() - batch.started >= PUBLISH_SECONDS):
                        commit()
                except (OSError, ValueError) as exc:
                    if isinstance(exc, InterruptedError):
                        raise
                    error = "archive_read_error:" + type(exc).__name__
                except sqlite3.IntegrityError:
                    abandon()
                    # Another writer (an unloading instance) committed first: take its rows as they are, and look at
                    # what this list left behind again at the next request, new and changed archives alike.
                    self._load()
                    publish()
                    self._again, self._last_verify = True, 0
                except sqlite3.Error as exc:
                    abandon()  # the whole batch is attempted again by the next pass
                    error = "history_save_error:" + type(exc).__name__
                handled = done
                with self._lock:
                    self._meta["archives_done"] = done
                time.sleep(0)  # a long backfill yields to the server's threads between archives
            commit()
        except sqlite3.Error as exc:
            abandon()
            error = "history_save_error:" + type(exc).__name__
        except InterruptedError:
            return "interrupted"
        finally:
            abandon()  # whatever was raised, no transaction (and no hold on the writer connection) outlives the list
        return error

    def _read_lines(self, path, identity, consume):
        """Hand each complete line that may hold a usage fact to ``consume``, in order, block by block (a large
        archive is never held whole); return the content facts of the file, or None when the index closed."""
        facts = dict.fromkeys(_FACT_KEYS, 0)
        with path.open("rb") as stream:
            carry = b""
            while True:
                if self._stop.is_set():
                    return None
                block = stream.read(READ_BLOCK)
                data = carry + block
                cut = data.rfind(b"\n") + 1 if block else len(data)
                complete, carry = data[:cut], data[cut:]
                facts["lines"] += complete.count(b"\n")
                if not block and complete and not complete.endswith(b"\n"):
                    facts["torn_tails"] += 1  # a last line without its newline: indexed if it is a whole record
                    facts["lines"] += 1
                starts = set()
                for marker in _MARKERS:
                    position = complete.find(marker)
                    while position >= 0:
                        starts.add(complete.rfind(b"\n", 0, position) + 1)
                        end = complete.find(b"\n", position)
                        position = complete.find(marker, end) if end >= 0 else -1
                for start in sorted(starts):
                    end = complete.find(b"\n", start)
                    consume(complete[start:end if end >= 0 else len(complete)])
                if not block:
                    break
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != identity:
            raise ValueError("archive changed while reading")
        return facts

    def _invalid_lines(self, path, identity):
        """How many nonempty lines of a changed archive are not one whole JSON object (None when the index closed).
        Read only for an archive about to replace its indexed copy, never on a reading's path."""
        invalid = 0
        with path.open("rb") as stream:
            carry = b""
            while True:
                if self._stop.is_set():
                    return None
                block = stream.read(READ_BLOCK)
                data = carry + block
                cut = data.rfind(b"\n") + 1 if block else len(data)
                complete, carry = data[:cut], data[cut:]
                for line in complete.split(b"\n"):
                    if line.strip():
                        try:
                            invalid += not isinstance(json.loads(line), dict)
                        except ValueError:  # JSON and UTF-8 decoding errors alike
                            invalid += 1
                if not block:
                    break
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != tuple(identity):
            raise ValueError("archive changed while reading")
        return invalid

    def _parse_archive(self, path, stat):
        identity = (stat.st_size, stat.st_mtime_ns)
        stats = dict(self._empty_stats)
        rows, sessions, boots = [], [], []

        def consume(raw):
            parsed = self._parse_line(raw, stats)
            if parsed is not None:
                kind, record = parsed
                (boots if kind == "boot" else sessions if kind == "session" else rows).append(record)

        facts = self._read_lines(path, identity, consume)
        if facts is None:
            return None
        facts["malformed_lines"] = stats.get("malformed_lines", 0)
        facts["unparsed_events"] = stats.get("unparsed_events", 0)
        facts["boots_without_code"] = stats.get("boots_without_code", 0)
        return identity, rows, sessions, boots, facts

    def _write_segment(self, name, kind, identity, rows, sessions, boots, facts, *, migrated):
        """One source in its own transaction (the earlier index's entries); memory changes after the commit."""
        batch = _Batch()
        with self._db_lock:
            db = self._db
            if db is None:
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                self._stage(db, batch, name, kind, identity, rows, sessions, boots, facts, migrated=migrated)
                if self._stop.is_set():
                    raise InterruptedError("index closed")
                batch.commits = self._bump(db, self._commits)
                db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
            self._apply(batch)

    def _stage(self, db, batch, name, kind, identity, rows, sessions, boots, facts, *, migrated, returning=frozenset()):
        """Write one source inside the open transaction and stage what memory gains once it commits. ``returning``:
        the requests (time, group, digest) a replaced copy of this same source counted, whose fingerprints memory
        still holds until the replacement commits; meeting one again is not a fingerprint collision."""
        seg = db.execute("INSERT INTO segments(name, kind, size, mtime_ns, stats) VALUES (?, ?, ?, ?, ?)",
                         (name, kind, identity[0], identity[1], json.dumps(facts))).lastrowid

        def group_id(key):
            gid = self._group_ids.get(key, batch.groups.get(key))
            if gid is None:
                gid = db.execute("INSERT INTO groups(provider, model, kind) VALUES (?, ?, ?)", key).lastrowid
                batch.groups[key] = gid
            return gid

        low = high = None
        row_values, session_values, touched, counted_fps = [], [], set(), []
        for order, record in enumerate(rows):
            stamp = _us(record["ts"])
            fp = self._fingerprint(record["identity"])
            digest = bytes.fromhex(record["identity"].split(":", 1)[-1])
            gid = group_id((record["provider"], record["model"], record["kind"]))
            counted = True
            if fp in batch.fps or self._contains(fp):
                # A fingerprint match is only a candidate: the stored full digest decides (same time and group).
                counted = not (digest in batch.digests.get((stamp, gid), ()) or db.execute(
                    "SELECT 1 FROM rows WHERE counted = 1 AND ts = ? AND gid = ? AND digest = ? LIMIT 1",
                    (stamp, gid, digest)).fetchone())
                if counted and (stamp, gid, digest) not in returning:
                    facts["fingerprint_collisions"] = facts.get("fingerprint_collisions", 0) + 1
            flags = (record.get("legacy_zeros") or 0) | (ROW_MIGRATED if migrated else 0)
            row_values.append((seg, order, fp, digest, stamp, gid, record["prompt_tokens"], record["cached_tokens"],
                               record["cache_write_tokens"], flags, int(counted)))
            low = stamp if low is None else min(low, stamp)
            high = stamp if high is None else max(high, stamp)
            if not counted:
                facts["duplicates"] = facts.get("duplicates", 0) + 1
                continue
            batch.fps.add(fp)
            counted_fps.append(fp)
            batch.digests.setdefault((stamp, gid), set()).add(digest)
            key = (stamp - stamp % CELL_US, gid)
            vector = self._vector(record)
            entry = batch.cells.get(key)
            if entry is None:
                current = self._cell(key[0], gid)
                entry = (list(current[0]), current[1], current[2]) if current else ([0] * len(vector), stamp, stamp)
            batch.cells[key] = ([a + b for a, b in zip(entry[0], vector)], min(entry[1], stamp), max(entry[2], stamp))
            touched.add(key)
        db.executemany("INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row_values)
        for order, record in enumerate(sessions):
            stamp = _us(record["ts"])
            flags = ((record.get("legacy_zeros") or 0) | (SESSION_NORMALIZED if record.get("normalized") else 0)
                     | (SESSION_FACTS if record.get("token_facts", True) else 0) | (SESSION_MIGRATED if migrated else 0))
            session_values.append((seg, order, record["identity"], stamp, record["provider"], record["model"],
                                   record["prompt_tokens"], record["cached_tokens"], record["cache_write_tokens"], flags))
            low = stamp if low is None else min(low, stamp)
            high = stamp if high is None else max(high, stamp)
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", session_values)
        db.executemany("INSERT OR IGNORE INTO boots VALUES (?, ?, ?, ?, ?)",
                       ((b["identity"], _us(b["ts"]), b["sha"], b["branch"], seg) for b in boots))
        db.executemany("INSERT OR REPLACE INTO cells VALUES (?, ?, ?, ?, ?)",
                       ((cell, gid, _pack(batch.cells[(cell, gid)][0]), *batch.cells[(cell, gid)][1:])
                        for cell, gid in touched))
        db.execute("UPDATE segments SET low = ?, high = ?, stats = ?, fps = ? WHERE id = ?",
                   (low, high, json.dumps(facts), _pack(counted_fps), seg))
        batch.sessions.extend((name, values) for values in session_values)
        batch.boots.extend(boots)
        batch.segments.append((name, {"id": seg, "kind": kind, "size": identity[0], "mtime_ns": identity[1], "facts": facts}))

    def _apply(self, batch):
        """A committed batch becomes visible in memory: the same facts the database now holds."""
        if batch.commits is not None:
            self._commits = batch.commits
        if batch.kept is not None:
            self._kept = batch.kept
        for key, gid in batch.groups.items():
            self._remember_group(gid, key)
        for (cell, gid), (vector, low, high) in batch.cells.items():
            self._set_cell(cell, gid, tuple(vector), low, high)
        if batch.fps:
            # One immutable chunk per batch: a view shares the chunks instead of copying a growing set.
            self._fp_chunks.append(frozenset(batch.fps))
            self._fp_recent += len(batch.fps)
            if self._fp_recent > 400_000:
                self._fp_base = array("q", sorted({*self._fp_base, *(fp for chunk in self._fp_chunks for fp in chunk)}))
                self._fp_chunks, self._fp_recent = [], 0
            elif len(self._fp_chunks) > 32:
                self._fp_chunks = [frozenset().union(*self._fp_chunks)]
        for name, (_, order, identity, stamp, provider, model, p, c, w, flags) in batch.sessions:
            self._offer_session((name, order), self._session_record(identity, stamp, provider, model, p, c, w, flags))
        for boot in batch.boots:
            if boot["identity"] not in self._boots:
                self._boots[boot["identity"]] = boot
                index = bisect_right(self._boot_stamps, boot["ts"])
                self._boot_stamps.insert(index, boot["ts"])
                self._boot_list.insert(index, boot)
                self._published_boots = None
        for name, info in batch.segments:
            self._segments[name] = info
            for key in _FACT_KEYS:
                self._facts[key] += info["facts"].get(key, 0)

    def _replace(self, name, parsed):
        """A source whose bytes changed (read cleanly), or a restored source whose archive is back, in ONE transaction:
        its rows, sessions and code starts leave, another source's copy of each request it counted takes over (the
        same call: time, group and full digest), the new bytes are indexed under the same name, and exactly the cells
        all of that touched are recomputed from their rows. Nothing shows before the commit; a failure leaves the
        database and the published view as they were, and the next pass tries again."""
        identity, rows, sessions, boots, facts = parsed
        with self._db_lock:
            db, seg = self._db, self._segments[name]["id"]
            db.execute("BEGIN IMMEDIATE")
            try:
                lost = db.execute("SELECT ts, gid, digest FROM rows WHERE seg = ? AND counted = 1", (seg,)).fetchall()
                touched = {(ts - ts % CELL_US, gid) for ts, gid, _ in lost}
                for table in ("rows", "sessions", "boots"):
                    db.execute("DELETE FROM " + table + " WHERE seg = ?", (seg,))
                db.execute("DELETE FROM segments WHERE id = ?", (seg,))
                heirs = set()
                for ts, gid, digest in lost:
                    found = db.execute("SELECT seg, ord FROM rows WHERE counted = 0 AND ts = ? AND gid = ? AND digest = ?"
                                       " ORDER BY seg, ord LIMIT 1", (ts, gid, digest)).fetchone()
                    if found is not None:
                        db.execute("UPDATE rows SET counted = 1 WHERE seg = ? AND ord = ?", found)
                        heirs.add(found[0])
                for other in heirs:
                    stats = json.loads(db.execute("SELECT stats FROM segments WHERE id = ?", (other,)).fetchone()[0])
                    stats["duplicates"] = db.execute("SELECT COUNT(*) FROM rows WHERE seg = ? AND counted = 0", (other,)).fetchone()[0]
                    db.execute("UPDATE segments SET stats = ? WHERE id = ?", (json.dumps(stats), other))
                    db.execute("UPDATE segments SET fps = ? WHERE id = ?", (_pack(fp for (fp,) in db.execute(
                        "SELECT fp FROM rows WHERE seg = ? AND counted = 1", (other,))), other))
                kept = dict(self._kept)
                kept.pop(name, None)
                db.execute("INSERT OR REPLACE INTO meta VALUES ('kept', ?)", (json.dumps(kept),))
                staged = _Batch()
                self._stage(db, staged, name, "archive", identity, rows, sessions, boots, facts, migrated=False,
                            returning=frozenset(lost))
                self._recount_cells(db, touched | set(staged.cells))
                if self._stop.is_set():
                    raise InterruptedError("index closed")
                self._bump(db, self._commits)
                db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
            self._load()  # memory (cells, fingerprints, session winners, code starts) follows the committed state

    # -- the earlier JSON index -------------------------------------------

    def _migrate_legacy(self, archive_dir):
        """Import, once and read-only, the 0.8.x entries whose archive is gone; extant archives are parsed again.

        The once-marker is written only after a complete read or for a format this version cannot read; an
        interrupted or failed read is tried again on the background cadence, and imported sources are never doubled. What the
        import could not read is kept with the marker and disclosed at every load."""
        with self._db_lock:
            done = self._db.execute("SELECT value FROM meta WHERE key = 'legacy'").fetchone()
        if done is not None:
            self._legacy = json.loads(done[0])
            self._disclose_recovery()
            return
        if self.legacy_path is None:
            return
        report = {"path": str(self.legacy_path), "imported": 0, "extant": 0, "unreadable": 0}
        try:
            try:
                stat = self.legacy_path.stat()
            except FileNotFoundError:
                report["status"] = "absent"
            else:
                report.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
                if self._fp_pending:
                    self._load_fingerprints()
                self._import_legacy(archive_dir, report)
                report["status"] = "read"
        except _LegacyFormatError as exc:
            report["status"] = "unreadable:" + str(exc)
        except (OSError, ValueError) as exc:
            self._legacy = dict(report, status="pending:" + type(exc).__name__)
            self._disclose_recovery()
            return
        if self._stop.is_set():
            return
        with self._db_lock:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('legacy', ?)", (json.dumps(report),))
        self._legacy = report
        self._disclose_recovery()

    def _disclose_recovery(self):
        """What the history cannot include, shown on every load: an unreadable index this one was rebuilt without,
        and any part of the earlier JSON index that could not be read. Neither is presented as the whole history."""
        notes, incomplete = [], False
        if self._rebuilt:
            notes.append(self._rebuilt)
            incomplete = True
        legacy = self._legacy or {}
        status = str(legacy.get("status") or "")
        imported, unreadable = int(legacy.get("imported") or 0), int(legacy.get("unreadable") or 0)
        if status.startswith("pending:"):
            notes.append("The earlier index could not be read yet (" + status.partition(":")[2] + "); it is tried again"
                         " later, and until then observations kept only in it are not included.")
            incomplete = True
        elif status.startswith("unreadable"):
            notes.append("The earlier index (" + os.path.basename(str(legacy.get("path") or "")) + ") could not be read ("
                         + status.partition(":")[2] + "): observations kept only in it are not included.")
            incomplete = True
        elif status == "read":
            if imported:
                notes.append("Restored " + str(imported) + " sources from the earlier index whose archives are no longer"
                             " on disk.")
            if unreadable:
                notes.append(str(unreadable) + " entries of the earlier index could not be read: observations kept only"
                             " in them are not included.")
                incomplete = True
        with self._lock:
            self._meta.update(recovery=" ".join(notes) or None, recovery_incomplete=incomplete,
                              rebuild_incomplete=bool(self._rebuilt),
                              legacy_import={key: legacy.get(key) for key in ("status", "imported", "unreadable", "extant")}
                              if legacy else None)

    def _import_legacy(self, archive_dir, report):
        with self.legacy_path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size < 64:
                # Small enough to read whole: an index that names no archive restores nothing; anything else here is
                # not the earlier format.
                try:
                    small = json.loads(stream.read() or b"null")
                except ValueError:
                    small = None
                if isinstance(small, dict) and small.get("version") == 1 and small.get("archives") == {}:
                    return
                raise _LegacyFormatError("unsupported" if size else "empty")
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                head = data[:64]
                if not re.match(rb'\{"version":\s*1,\s*"archives":\s*\{', head):
                    raise _LegacyFormatError("unsupported")
                # One entry per archive key; each entry ends where the next key starts. The keys are found one at a
                # time (a Python-level loop), so a large earlier index never holds the interpreter in one long call.
                previous = None
                for match in re.finditer(rb'"((?:[^"\\]|\\.)*)":\{"identity":\[(\d+),(\d+)\]', data):
                    if previous is not None:
                        self._import_legacy_entry(archive_dir, data, previous, match.start() - 1, report)
                    previous = match
                if previous is None:
                    raise _LegacyFormatError("no readable entry")  # larger than any index without archives
                self._import_legacy_entry(archive_dir, data, previous, len(data) - 2, report)

    def _import_legacy_entry(self, archive_dir, data, match, end, report):
        if self._stop.is_set():
            raise InterruptedError("closed during import")
        name = os.path.basename(json.loads(b'"' + match.group(1) + b'"'))
        if (archive_dir / name).exists():
            report["extant"] += 1
            return
        if name in self._segments:
            # A partial import may already have committed this retained source.
            report["imported" if self._segments[name]["kind"] == "restored" else "extant"] += 1
            return
        try:
            entry = json.loads(data[match.start(0) + len(match.group(1)) + 3:end])
            self._import_entry(name, entry)
            report["imported"] += 1
        except (ValueError, TypeError, KeyError, OverflowError, sqlite3.IntegrityError):
            report["unreadable"] += 1

    def _import_entry(self, name, entry):
        def stamp(record):
            return datetime.fromisoformat(record["ts"]).astimezone(timezone.utc)

        rows, sessions, boots = [], [], []
        for record in entry.get("records") or []:
            identity = str(record.get("identity") or "")
            if identity.startswith("["):
                import hashlib
                identity = "request:" + hashlib.sha256(identity.encode()).hexdigest()
            p, c, w, legacy = self._classify(record.get("prompt_tokens"), record.get("cached_tokens"),
                                             record.get("cache_write_tokens"), False)
            measured = p is not None and c is not None
            if not re.fullmatch(r"request:[0-9a-f]{64}", identity):
                raise ValueError("unexpected request identity")
            rows.append({"ts": stamp(record), "identity": identity, "kind": "attempt",
                         "provider": str(record.get("provider") or "unknown"), "model": str(record.get("model") or "unknown"),
                         "prompt_tokens": p, "cached_tokens": c, "cache_write_tokens": w, "legacy_zeros": legacy,
                         "measured": measured, "eligible": measured and p > 0})
        for record in entry.get("sessions") or []:
            normalized = bool(record.get("normalized"))
            p, c, w, legacy = self._classify(record.get("prompt_tokens"), record.get("cached_tokens"),
                                             record.get("cache_write_tokens"), normalized)
            sessions.append({"ts": stamp(record), "identity": str(record["identity"]), "kind": "subscription_session",
                             "provider": str(record.get("provider") or "unknown"), "model": str(record.get("model") or "unknown"),
                             "prompt_tokens": p, "cached_tokens": c, "cache_write_tokens": w, "legacy_zeros": legacy,
                             "normalized": normalized, "token_facts": bool(record.get("token_facts", True))})
        for record in entry.get("boots") or []:
            if str(record.get("sha") or "").strip():
                boots.append({"ts": stamp(record), "sha": str(record["sha"]), "branch": str(record.get("branch") or ""),
                              "identity": str(record["identity"])})
        facts = dict.fromkeys(_FACT_KEYS, 0)
        facts["malformed_lines"] = int(entry.get("malformed") or 0)
        identity = entry.get("identity") or [None, None]
        self._write_segment(name, "restored", (identity[0], identity[1]), rows, sessions, boots, facts, migrated=True)


class _Batch:
    """What one open transaction has written, waiting for its commit before memory shows it."""

    def __init__(self):
        self.groups, self.cells, self.fps, self.digests = {}, {}, set(), {}
        self.sessions, self.boots, self.segments = [], [], []
        self.kept, self.commits = None, None
        self.started = time.monotonic()


class _Bound:
    """One reading's database reads at one view's commit (``HistoryIndex.bind``)."""

    def __init__(self, index, view):
        self._index, self._view, self._connection, self._locked, self._open = index, view, None, False, False

    def __enter__(self):
        index = self._index
        try:
            if index.path is None:
                # The in-memory database has one connection: hold it, so no commit lands while this reading reads.
                index._db_lock.acquire()
                self._locked = True
                if index._db is None:
                    raise sqlite3.ProgrammingError("history index closed")
                commits = index._stored_commits(index._db)
            else:
                self._connection = index._lease()
                self._connection.execute("BEGIN")
                commits = index._stored_commits(self._connection)  # this first read fixes the snapshot
            if commits != self._view.commits:
                raise StaleView(commits)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        self._open = True
        bound = copy.copy(self._view)  # shares the view's parts and memo; only its reads are this reading's
        bound._query = self._query
        return bound

    def _query(self, sql, args):
        if not self._open:
            raise sqlite3.ProgrammingError("reading already ended")
        db = self._connection if self._connection is not None else self._index._db
        return db.execute(sql, args).fetchall()

    def __exit__(self, *exc):
        self._open = False
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.execute("ROLLBACK")  # ends the read transaction
            except sqlite3.Error:
                connection.close()
            else:
                self._index._release(connection)
        if self._locked:
            self._locked = False
            self._index._db_lock.release()
        return False


class _LegacyFormatError(ValueError):
    """The earlier index is not a format this version reads; retrying would not change that."""
