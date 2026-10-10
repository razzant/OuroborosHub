"""Bounded local history of passive quota readings — SQLite, stdlib only.

The one writer is the skill's supervised collector; routes and the model tool
only read. The file lives in the skill's own state directory and holds, per
account and limit, runs of consecutive fresh readings from one source that
the collector watched without a gap:

    subject     a salted pseudonymous id (never the profile id, name or e-mail)
    series      harness | limit meaning | window seconds | model-scope digest
    source      the engine's reading source (e.g. codex_app_server)
    plan        plan evidence: the reading's own plan label and the account
                list's plan, both as reported (quota_summary.plan_evidence)
    ratio       the raw used ratio, unrounded
    resets_at   the reported reset (UTC epoch seconds) or NULL
    first_obs / last_obs    the source's own observation times
    n_obs       how many distinct observations the run holds
    first_seen / last_seen  when the collector first/last saw it reported fresh
    after_gap   1 when the watch broke between the previous run and this one
    after_correction   1 when content changed at the same observation time

and one row per collector sweep (time, ok, a short reason). Nothing else is
written: no raw response, no detail text, no identity, no credential.

A repeated cached reading (same source observation time and content) only moves
``last_seen`` while the watch of that source is unbroken; after a gap
(sightings further apart than SIGHTING_GAP_SEC, or any sweep since the last
sighting — a failed one, or one that did not see this source fresh and
numeric) the same reading opens a new run, so a returning cached timestamp
never bridges an outage or an individual stale, missing or unreadable
reading. Healthy sources in the same sweep are not affected.

Bounds, exactly: every PRUNE_EVERY_SWEEPS sweeps, runs last sighted more than
RETENTION_SEC ago are deleted and a longer run is trimmed to that window (its
first sighting, and its first observation when a later one repeats the value,
move up to the cutoff), as are sweeps; then at most MAX_RUNS runs and
MAX_SWEEPS sweeps are kept. After every sweep a file (with its write-ahead
log) above MAX_FILE_BYTES loses its oldest quarter of runs and gives the
freed pages back: a soft ceiling, checked after the sweep has written, and a
file still above it shrinks again at the next sweep. What a cap removed is
recorded as ``capped_before`` so a reader can say the history is shorter
than 14 days.

Disk work belongs to the collector worker, with cooperative stop checks and
explicit atomic-operation admission. Stop request is not settlement; the
collector acknowledges settlement only after connections and leases close.

A file this skill cannot read is not moved, deleted or rebuilt over, and the
history is reported unavailable until the owner removes it. A file that is
not a SQLite database at all is never even opened: SQLite would delete the
write-ahead log beside it. Before each read and write only the schema and
salt are checked; no full integrity scan is run, so a damaged page is found
only when a query reaches it (that read reports the history unavailable, that
sweep is rolled back) and a sweep that never reaches it may still be written.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:  # the host loads plugin.py as a package: siblings are relative imports
    from . import quota_summary as qs
except ImportError:  # imported directly from the skill directory (tests)
    import quota_summary as qs  # type: ignore[no-redef]

HISTORY_FILE = "quota_history.sqlite3"
SCHEMA_VERSION = 2
RETENTION_SEC = 14 * 86400.0
MAX_RUNS = 200_000
MAX_SWEEPS = 20_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_READ_ROWS = 50_000
# Sweep times a read may carry for continuity checks: the whole sweep cap,
# so a week's chart (about 5 000 sweeps) is never cut short.
MAX_READ_SWEEPS = 20_000
# Pairs one read may ask the newest run of (a last-known value each).
MAX_LATEST_PAIRS = 2_000
# Sources one pair may hold a newest run for: each is resolved like a
# current reading's sources (they may disagree), never "newest wins".
MAX_SOURCES_PER_PAIR = 8
# Limits one roster account may be found to have in the history.
MAX_SERIES_PER_SUBJECT = 64
BUSY_TIMEOUT_MS = 250
PRUNE_EVERY_SWEEPS = 30

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE run (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    series TEXT NOT NULL,
    source TEXT NOT NULL,
    plan TEXT NOT NULL,
    ratio REAL NOT NULL,
    resets_at REAL,
    first_obs REAL NOT NULL,
    last_obs REAL NOT NULL,
    n_obs INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    after_gap INTEGER NOT NULL,
    after_correction INTEGER NOT NULL
);
CREATE INDEX run_lookup ON run (subject, series, source, last_seen);
CREATE INDEX run_seen ON run (last_seen);
CREATE TABLE sweep (at REAL PRIMARY KEY, ok INTEGER NOT NULL, reason TEXT NOT NULL);
"""

_RUN_FIELDS = ("source, ratio, resets_at, plan, first_obs, last_obs, "
               "n_obs, first_seen, last_seen, after_gap, after_correction")

# SQLite words for a file that is not (or no longer) a readable history. Any
# other failure — a lock held by another writer, a full disk — is transient
# and must not cost the owner their history.
_CORRUPT_WORDS = ("malformed", "not a database", "no such table", "no such column",
                  "has no column named", "unknown history schema", "file is encrypted", "corrupt")


SQLITE_HEADER = b"SQLite format 3\x00"


class HistoryCorrupt(Exception):
    """The file exists but is not a history this skill can read. It is left
    untouched; the collector keeps nothing until the owner removes it."""


class HistoryStopped(Exception):
    """Stop requested before the next atomic disk operation was admitted."""


class StopControl:
    """Memory-only stop/commit admission. No lock is held during disk I/O.

    Stop is a request. An already admitted SQLite commit/maintenance operation
    may finish afterwards; settled is signalled by the collector only after
    its worker has closed the connection and released the cycle lease.

    A control made with a parent is stopped when either is, and setting it
    stops only itself: the collector gives each run one under its
    registration's. Both admit through the parent's gate, so a stop of
    either is ordered against every admission, as with one control.
    """
    def __init__(self, event: Optional[threading.Event] = None, *,
                 parent: Optional["StopControl"] = None):
        self.event = event or threading.Event()
        self._parent = parent
        self._gate = parent._gate if parent is not None else threading.Lock()
        self.settled = threading.Event()
        self.settled.set()

    def is_set(self) -> bool:
        return self.event.is_set() or (self._parent is not None and self._parent.is_set())

    def set(self) -> None:
        with self._gate:
            self.event.set()

    def check(self) -> None:
        if self.is_set():
            raise HistoryStopped()

    def admit(self) -> None:
        with self._gate:
            self.check()


def _commit(conn: sqlite3.Connection, control: StopControl) -> None:
    conn.set_progress_handler(None, 0)
    control.admit()
    conn.execute("COMMIT")


def _reclaim_free_pages(conn: sqlite3.Connection) -> None:
    """Give every free page back to the file system. The pragma frees one
    page per result row of zero columns, and CPython 3.11's cursor stops at
    the first such row (3.9 and 3.14 step on), so ``execute().fetchall()``
    would free a single page. ``executescript`` steps to the end everywhere;
    callers are in autocommit here, so its implicit COMMIT never applies."""
    conn.executescript("PRAGMA incremental_vacuum;")


def _is_corrupt(exc: BaseException) -> bool:
    if isinstance(exc, HistoryCorrupt):
        return True
    text = str(exc).lower()
    return isinstance(exc, sqlite3.DatabaseError) and any(word in text for word in _CORRUPT_WORDS)


def _run_of(row: Tuple[Any, ...]) -> qs.Run:
    return qs.Run(
        source=str(row[0]), ratio=float(row[1]),
        resets_at=None if row[2] is None else float(row[2]),
        plan=str(row[3]), first_obs=float(row[4]), last_obs=float(row[5]),
        n_obs=int(row[6]), first_seen=float(row[7]), last_seen=float(row[8]),
        after_gap=bool(row[9]),
        after_correction=bool(row[10]),
    )


class HistoryStore:
    def __init__(self, directory: Path):
        self.path = Path(directory) / HISTORY_FILE
        self._sweeps_until_prune = 0

    # -- connections -------------------------------------------------------

    def _not_a_database(self) -> str:
        """Why the file must not be handed to SQLite at all, or ''.

        Opening a file that is not a database read-write makes SQLite delete
        the write-ahead log beside it — evidence the owner may want to keep.
        So a file without the SQLite header, or an empty one with a log
        beside it, is refused before any connection is made.
        """
        try:
            with open(self.path, "rb") as handle:
                head = handle.read(len(SQLITE_HEADER))
        except FileNotFoundError:
            return ""
        if head == SQLITE_HEADER:
            return ""
        if not head and not os.path.exists(str(self.path) + "-wal"):
            return ""  # an empty file: nothing to keep, a new history may start
        return "history unreadable: not a SQLite database"

    def _connect(self, create: bool, *, preflight: bool = False) -> sqlite3.Connection:
        mode = "rwc" if create else "ro"
        uri = f"file:{urllib.parse.quote(str(self.path))}?mode={mode}"
        # Schema-only preflight of a checkpointed file must not create WAL/SHM
        # sidecars merely to reject it. Never use immutable for the actual
        # history view: a concurrent writer may have opened a WAL meanwhile.
        if preflight and not os.path.exists(str(self.path) + "-wal"):
            uri += "&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0,
                               isolation_level=None, check_same_thread=True)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if not create:
            conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _validate(conn: sqlite3.Connection) -> Dict[str, str]:
        """Validate the whole schema and salt, even for an empty request/sweep."""
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise HistoryCorrupt("unknown history schema version")
        expected = {
            "meta": [("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)],
            "run": [("id", "INTEGER", 0, 1)] + [
                (name, kind, 0 if name == "resets_at" else 1, 0)
                for name, kind in (
                    ("subject", "TEXT"), ("series", "TEXT"), ("source", "TEXT"),
                    ("plan", "TEXT"), ("ratio", "REAL"), ("resets_at", "REAL"),
                    ("first_obs", "REAL"), ("last_obs", "REAL"), ("n_obs", "INTEGER"),
                    ("first_seen", "REAL"), ("last_seen", "REAL"),
                    ("after_gap", "INTEGER"), ("after_correction", "INTEGER"))],
            "sweep": [("at", "REAL", 0, 1), ("ok", "INTEGER", 1, 0),
                      ("reason", "TEXT", 1, 0)],
        }
        tables = set(conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                  "AND name NOT LIKE 'sqlite_%'"))
        if tables != {(name,) for name in expected}:
            raise HistoryCorrupt("unknown history schema tables")
        for table, fields in expected.items():
            actual = [(row[1], row[2].upper(), row[3], row[5])
                      for row in conn.execute(f"PRAGMA table_info({table})")]
            if actual != fields:
                raise HistoryCorrupt("unknown history schema columns")
        for index, fields in (("run_lookup", ["subject", "series", "source", "last_seen"]),
                              ("run_seen", ["last_seen"])):
            if [row[2] for row in conn.execute(f"PRAGMA index_info({index})")] != fields:
                raise HistoryCorrupt("unknown history schema indexes")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise HistoryCorrupt("unknown history schema triggers")
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        salt = meta.get("salt", "")
        if not isinstance(salt, str) or len(salt) != 32 or any(c not in "0123456789abcdef" for c in salt):
            raise HistoryCorrupt("history file has invalid salt")
        return meta

    def _open_writer(self, control: StopControl) -> sqlite3.Connection:
        control.check()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        refused = self._not_a_database()
        if refused:
            raise HistoryCorrupt(refused)
        new = not self.path.exists() or self.path.stat().st_size == 0
        if not new:
            # Refuse unsupported files before opening them read-write. A ro
            # connection reads committed WAL too; immutable=1 would miss it.
            reader = self._connect(create=False, preflight=True)
            try:
                new = (reader.execute("PRAGMA user_version").fetchone()[0] == 0
                       and not reader.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone())
                if not new:
                    self._validate(reader)
            finally:
                reader.close()
        control.admit()
        conn = self._connect(create=True)
        try:
            if new:
                tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
                if tables:
                    raise HistoryCorrupt("unknown history schema (tables without a version)")
                # auto_vacuum only takes effect before the first table exists.
                control.admit()
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                control.admit()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            control.check()
                            conn.execute(statement)
                    conn.execute("INSERT INTO meta (key, value) VALUES ('salt', ?)",
                                 (secrets.token_hex(16),))
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    _commit(conn, control)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            self._validate(conn)
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except BaseException:
            conn.close()
            raise

    # -- writer ------------------------------------------------------------

    def record_sweep(self, readings: Iterable[qs.Reading], swept_at: float,
                     ok: bool, reason: str, *, control: Optional[StopControl] = None) -> Dict[str, Any]:
        """Store one collector sweep in one transaction. Returns counters.

        Any failure propagates and the sweep is simply not recorded (a gap,
        never an invented point). A file that is not a readable history
        raises :class:`HistoryCorrupt` and is left untouched.
        """
        rows = list(readings)
        try:
            return self._record(rows, swept_at, ok, reason, control or StopControl())
        except HistoryCorrupt:
            raise
        except Exception as exc:
            if _is_corrupt(exc):
                raise HistoryCorrupt(f"history unreadable: {type(exc).__name__}") from exc
            raise

    def _record(self, readings: List[qs.Reading], swept_at: float, ok: bool,
                reason: str, control: StopControl) -> Dict[str, Any]:
        counts = {"inserted": 0, "extended": 0, "seen": 0, "out_of_order": 0, "pruned": 0}
        conn = self._open_writer(control)
        try:
            salt = conn.execute("SELECT value FROM meta WHERE key='salt'").fetchone()
            if not salt or not salt[0]:
                raise HistoryCorrupt("history file has no salt")
            salt = str(salt[0])
            conn.execute("BEGIN IMMEDIATE")
            conn.set_progress_handler(lambda: int(control.is_set()), 1000)
            try:
                # The newest earlier sweep, good or failed. A run whose last
                # sighting is older than it was not seen by that sweep.
                earlier = conn.execute(
                    "SELECT max(at) FROM sweep WHERE at<?", (swept_at,),
                ).fetchone()[0]
                last_sweep = None if earlier is None else float(earlier)
                for reading in readings:
                    control.check()
                    self._upsert(conn, salt, reading, swept_at, last_sweep, counts)
                conn.execute("INSERT OR REPLACE INTO sweep (at, ok, reason) VALUES (?, ?, ?)",
                             (swept_at, 1 if ok else 0, str(reason or "")[:64]))
                self._sweeps_until_prune -= 1
                if self._sweeps_until_prune <= 0:
                    counts["pruned"] = self._prune(conn, swept_at)
                    self._sweeps_until_prune = PRUNE_EVERY_SWEEPS
                _commit(conn, control)
            except BaseException:
                conn.set_progress_handler(None, 0)
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                self._sweeps_until_prune = 0
                raise
            if counts["pruned"]:
                control.admit()
                _reclaim_free_pages(conn)
            if self._file_bytes() > MAX_FILE_BYTES:
                counts["pruned"] += self._shrink(conn, control)
        finally:
            conn.close()
        return counts

    def _upsert(self, conn: sqlite3.Connection, salt: str, reading: qs.Reading,
                swept_at: float, last_sweep: Optional[float], counts: Dict[str, int]) -> None:
        subject = qs.pseudo_id(salt, reading.harness, reading.subject_id)
        found = conn.execute(
            f"SELECT id, {_RUN_FIELDS} FROM run WHERE subject=? AND series=? AND source=? "
            "ORDER BY last_seen DESC, id DESC LIMIT 1",
            (subject, reading.key, reading.source),
        ).fetchone()
        observed = float(reading.observed_at)
        gap = False
        correction = False
        if found is not None:
            run_id, prev = found[0], _run_of(found[1:])
            # A break in this source's watch: sightings too far apart, or a
            # sweep since its last sighting (failed, or one that did not see
            # it fresh and numeric) — even when the rest of that sweep was
            # healthy and the host now reports the very same cached reading.
            gap = (swept_at - prev.last_seen > qs.SIGHTING_GAP_SEC
                   or (last_sweep is not None and last_sweep > prev.last_seen + 1e-6))
            if observed < prev.last_obs - 1e-6:
                counts["out_of_order"] += 1
                return
            correction = abs(observed - prev.last_obs) <= 1e-6 and not qs.same_content(prev, reading)
            if not gap and not correction and abs(observed - prev.last_obs) <= 1e-6:
                # The same observation read again while the watch is
                # unbroken: the host still vouches for it. Not a new point.
                conn.execute("UPDATE run SET last_seen=max(last_seen, ?) WHERE id=?",
                             (swept_at, run_id))
                counts["seen"] += 1
                return
            nxt = qs.Run(
                source=reading.source, ratio=float(reading.ratio), resets_at=reading.resets_at,
                plan=reading.plan_key, first_obs=observed, last_obs=observed, n_obs=1,
                first_seen=swept_at, last_seen=swept_at, after_gap=gap, after_correction=correction,
            )
            if qs.extends(prev, nxt):
                conn.execute(
                    "UPDATE run SET last_obs=?, n_obs=n_obs+1, last_seen=?, resets_at=? WHERE id=?",
                    (observed, swept_at, reading.resets_at, run_id),
                )
                counts["extended"] += 1
                return
        conn.execute(
            "INSERT INTO run (subject, series, source, plan, ratio, resets_at, "
            "first_obs, last_obs, n_obs, first_seen, last_seen, after_gap, after_correction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (subject, reading.key, reading.source, reading.plan_key, float(reading.ratio),
             reading.resets_at, observed, observed, swept_at, swept_at, int(gap), int(correction)),
        )
        counts["inserted"] += 1

    def _prune(self, conn: sqlite3.Connection, now: float) -> int:
        cutoff = now - RETENTION_SEC
        removed = conn.execute("DELETE FROM run WHERE last_seen < ?", (cutoff,)).rowcount
        # A run still sighted keeps no older sighting than the cutoff, and no
        # older observation when a later one repeats its value (the value at
        # the cutoff is then known). A single observation keeps its own time.
        conn.execute("UPDATE run SET first_obs = max(first_obs, ?) "
                     "WHERE first_obs < ? AND last_obs >= ?", (cutoff, cutoff, cutoff))
        conn.execute("UPDATE run SET first_seen = ? WHERE first_seen < ?", (cutoff, cutoff))
        conn.execute("DELETE FROM sweep WHERE at < ?", (cutoff,))
        total = conn.execute("SELECT count(*) FROM run").fetchone()[0]
        if total > MAX_RUNS:
            removed += self._drop_oldest(conn, total - MAX_RUNS)
        sweeps = conn.execute("SELECT count(*) FROM sweep").fetchone()[0]
        if sweeps > MAX_SWEEPS:
            conn.execute(
                "DELETE FROM sweep WHERE at IN (SELECT at FROM sweep ORDER BY at ASC LIMIT ?)",
                (sweeps - MAX_SWEEPS,),
            )
        return max(0, removed)

    def _drop_oldest(self, conn: sqlite3.Connection, count: int) -> int:
        edge = conn.execute(
            "SELECT max(last_seen) FROM (SELECT last_seen FROM run ORDER BY last_seen ASC LIMIT ?)",
            (count,),
        ).fetchone()[0]
        removed = conn.execute(
            "DELETE FROM run WHERE id IN (SELECT id FROM run ORDER BY last_seen ASC LIMIT ?)",
            (count,),
        ).rowcount
        if edge is not None:
            previous = conn.execute("SELECT value FROM meta WHERE key='capped_before'").fetchone()
            value = max(float(edge), float(previous[0])) if previous else float(edge)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('capped_before', ?)",
                         (repr(value),))
        return removed

    def _file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return total

    def _shrink(self, conn: sqlite3.Connection, control: StopControl) -> int:
        """The file cap: drop the oldest quarter of the runs and give the
        pages back. Rare by construction — the row cap normally binds first.
        A soft ceiling: it acts after a sweep has written, and a file still
        above the cap (a reader holding the log) shrinks again next sweep."""
        control.check()
        conn.execute("BEGIN IMMEDIATE")
        conn.set_progress_handler(lambda: int(control.is_set()), 1000)
        try:
            total = conn.execute("SELECT count(*) FROM run").fetchone()[0]
            removed = self._drop_oldest(conn, max(1, total // 4)) if total else 0
            _commit(conn, control)
        except BaseException:
            conn.set_progress_handler(None, 0)
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        control.admit()
        _reclaim_free_pages(conn)
        control.admit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed

    # -- reader ------------------------------------------------------------

    def read(self, requests: Callable[[str], Dict[Tuple[str, str], float]],
             now: float, *, max_rows: int = MAX_READ_ROWS,
             latest: Optional[Callable[[str], Iterable[Tuple[str, str]]]] = None,
             roster: Optional[Callable[[str], Iterable[str]]] = None,
             chart_series: Optional[Tuple[str, float]] = None) -> qs.HistoryView:
        """A read-only view. ``requests(salt)`` names the (subject, series)
        pairs wanted and the oldest last sighting worth reading for each;
        ``latest(salt)`` names pairs whose newest kept run per source is
        wanted, whatever its age; ``roster(salt)`` names subjects whose every
        recorded series is wanted that way (the current roster when no
        reading names its limits). Both are bounded: at most MAX_LATEST_PAIRS
        pairs, MAX_SOURCES_PER_PAIR sources each, MAX_SERIES_PER_SUBJECT
        series per roster subject, every row an indexed lookup.
        Never creates, repairs or writes the file."""
        if not self.path.is_file():
            return qs.HistoryView(state="empty")
        try:
            refused = self._not_a_database()
        except OSError as exc:
            return qs.HistoryView(state="unavailable", error=f"history unreadable: {type(exc).__name__}")
        if refused:
            return qs.HistoryView(state="unavailable", error=refused)
        try:
            preflight = self._connect(create=False, preflight=True)
            try:
                self._validate(preflight)
            finally:
                preflight.close()
        except (sqlite3.Error, HistoryCorrupt) as exc:
            return qs.HistoryView(state="unavailable", error=f"history unreadable: {type(exc).__name__}")
        try:
            conn = self._connect(create=False)
        except sqlite3.Error as exc:
            return qs.HistoryView(state="unavailable", error=f"history unreadable: {type(exc).__name__}")
        try:
            meta = self._validate(conn)
            salt = str(meta.get("salt") or "")
            view = qs.HistoryView(state="ok", salt=salt)
            if meta.get("capped_before"):
                try:
                    view.capped_before = float(meta["capped_before"])
                except ValueError:
                    view.capped_before = None
            last = conn.execute("SELECT at, ok, reason FROM sweep ORDER BY at DESC LIMIT 1").fetchone()
            if last is None:
                view.state = "empty"
            else:
                view.last_sweep_at = float(last[0])
                view.last_sweep_ok = bool(last[1])
                view.last_sweep_reason = str(last[2] or "")
            first = conn.execute("SELECT min(at) FROM sweep").fetchone()[0]
            view.collecting_since = None if first is None else float(first)
            failed = conn.execute("SELECT at FROM sweep WHERE ok=0 ORDER BY at DESC LIMIT 1").fetchone()
            view.last_failed_at = None if failed is None else float(failed[0])
            oldest = conn.execute("SELECT min(first_obs) FROM run").fetchone()[0]
            view.oldest_at = None if oldest is None else float(oldest)
            view.watched_since, view.watched_since_exact = self._unbroken_watch(conn, now)
            wanted = requests(salt) if salt else {}
            # The displayed history includes previously recorded subjects,
            # even when the current roster no longer contains them. This
            # says nothing about when they were removed: the schema stores
            # quota observations, not historical rosters. Bounded read only;
            # neither a schema change nor a new writer/index is needed.
            if chart_series is not None:
                series, since = chart_series
                subjects = conn.execute(
                    "SELECT DISTINCT subject FROM run WHERE series=? LIMIT ?",
                    (series, MAX_LATEST_PAIRS + 1)).fetchall()
                if len(subjects) > MAX_LATEST_PAIRS:
                    view.truncated = True
                for (subject,) in subjects[:MAX_LATEST_PAIRS]:
                    pair = (str(subject), series)
                    wanted[pair] = min(wanted.get(pair, float(since)), float(since))
            if wanted:
                # Read before the runs: a sweep committed in between can only
                # make a run newer than this list, never older.
                view.sweeps_from = min(float(since) for since in wanted.values())
                view.sweeps = [float(at) for (at,) in conn.execute(
                    "SELECT at FROM sweep WHERE at>=? ORDER BY at DESC LIMIT ?",
                    (view.sweeps_from, MAX_READ_SWEEPS))][::-1]
                if len(view.sweeps) >= MAX_READ_SWEEPS:
                    # The newest are kept; nothing is claimed before them.
                    view.sweeps_from = view.sweeps[0]
                    view.truncated = True
            budget = max(1, int(max_rows))
            for (subject, series), since in sorted(wanted.items()):
                if budget <= 0:
                    view.truncated = True
                    break
                rows = conn.execute(
                    f"SELECT {_RUN_FIELDS} FROM run WHERE subject=? AND series=? AND last_seen>=? "
                    "ORDER BY first_seen DESC, id DESC LIMIT ?",
                    (subject, series, float(since), budget + 1),
                ).fetchall()
                if len(rows) > budget:
                    view.truncated = True
                    rows = rows[:budget]
                budget -= len(rows)
                view.runs[(subject, series)] = [_run_of(row) for row in reversed(rows)]
                if chart_series is not None and series == chart_series[0] and budget > 0:
                    # Seed a carry at the left edge from the newest older
                    # run of each source, never from an observation after it.
                    sources = conn.execute(
                        "SELECT DISTINCT source FROM run WHERE subject=? AND series=? LIMIT ?",
                        (subject, series, MAX_SOURCES_PER_PAIR + 1)).fetchall()
                    if len(sources) > MAX_SOURCES_PER_PAIR:
                        view.truncated = True
                    seeds = []
                    for (source,) in sources[:MAX_SOURCES_PER_PAIR]:
                        if budget <= 0:
                            view.truncated = True
                            break
                        seed = conn.execute(
                            f"SELECT {_RUN_FIELDS} FROM run WHERE subject=? AND series=? "
                            "AND source=? AND last_seen<? ORDER BY last_seen DESC, id DESC LIMIT 1",
                            (subject, series, source, float(since))).fetchone()
                        if seed is not None:
                            seeds.append(_run_of(seed))
                            budget -= 1
                    view.runs[(subject, series)] = seeds + view.runs[(subject, series)]
            pairs = set(latest(salt)) if (latest is not None and salt) else set()
            for subject in sorted(set(roster(salt))) if (roster is not None and salt) else []:
                found = [str(s) for (s,) in conn.execute(
                    "SELECT DISTINCT series FROM run WHERE subject=? LIMIT ?",
                    (subject, MAX_SERIES_PER_SUBJECT + 1))]
                if len(found) > MAX_SERIES_PER_SUBJECT:
                    found = found[:MAX_SERIES_PER_SUBJECT]
                    view.truncated = True
                view.roster_series.update((subject, series) for series in found)
                pairs.update((subject, series) for series in found)
            pairs = sorted(pairs)
            if len(pairs) > MAX_LATEST_PAIRS:
                pairs = pairs[:MAX_LATEST_PAIRS]
                view.truncated = True
            for subject, series in pairs:
                sources = [str(s) for (s,) in conn.execute(
                    "SELECT DISTINCT source FROM run WHERE subject=? AND series=? LIMIT ?",
                    (subject, series, MAX_SOURCES_PER_PAIR + 1))]
                if len(sources) > MAX_SOURCES_PER_PAIR:
                    sources = sources[:MAX_SOURCES_PER_PAIR]
                    view.truncated = True
                newest = []
                for source in sources:
                    row = conn.execute(
                        f"SELECT {_RUN_FIELDS} FROM run WHERE subject=? AND series=? AND source=? "
                        "ORDER BY last_seen DESC, id DESC LIMIT 1",
                        (subject, series, source),
                    ).fetchone()
                    if row is not None:
                        newest.append(_run_of(row))
                if newest:
                    view.latest[(subject, series)] = newest
            return view
        except (sqlite3.Error, HistoryCorrupt) as exc:
            return qs.HistoryView(state="unavailable", error=f"history unreadable: {type(exc).__name__}")
        finally:
            conn.close()

    @staticmethod
    def _unbroken_watch(conn: sqlite3.Connection, now: float) -> Tuple[Optional[float], bool]:
        """The collector's current unbroken run of good sweeps, looked for
        only as far back as WATCH_LOOKBACK_SEC: (its earliest sweep found,
        whether that is where the run began). (None, False) when it is not
        running now — no good sweep within one gap, or the last sweep failed.
        A failed sweep or a gap breaks the run. When the look-back ends first,
        one index seek for the sweep just before it tells whether the run
        goes on further back ("at least since") or begins there; nothing
        older is read on a request."""
        floor = now - qs.WATCH_LOOKBACK_SEC
        rows = [(float(at), bool(ok)) for at, ok in conn.execute(
            "SELECT at, ok FROM sweep WHERE at>=? ORDER BY at DESC", (floor,))]
        if not rows or not rows[0][1] or now - rows[0][0] > qs.SIGHTING_GAP_SEC:
            return None, False
        start = rows[0][0]
        for at, ok in rows[1:]:
            if not ok or start - at > qs.SIGHTING_GAP_SEC:
                return start, True
            start = at
        before = conn.execute(
            "SELECT at, ok FROM sweep WHERE at<? ORDER BY at DESC LIMIT 1", (floor,)).fetchone()
        exact = before is None or not before[1] or start - float(before[0]) > qs.SIGHTING_GAP_SEC
        return start, exact
