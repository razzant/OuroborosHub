"""Meaning, sanitizing and projection for Context Lens; plus the legacy journal reader.

Pure module: no Ouroboros imports, no host imports, no network, no writes.
Everything the widget can ever see is produced here and passes through
``_point()`` / ``counters()`` / ``assemble()``, which are allowlists — a field
that is not named there cannot reach the browser.

Two sources, one projection
---------------------------
* **The usage store** (``state/usage.sqlite``) is the live usage authority.
  ``lens_store`` reads one bounded selection from it and returns plain column
  values; ``store_record()`` turns each row into the record shape below and
  ``store_snapshot()`` describes the selection exactly.
* **The retired journal** (``state/usage_attempts.jsonl``) is read by
  ``LedgerWindow`` ONLY when no store file exists at all. Core stopped appending
  to it at the one-time import, so its answer is labelled historical and never
  stands in for a store that exists but cannot be read.

Both sources become the same records, and ``assemble()`` builds one payload from
them, so points, counters, facets and the selection facts always describe one
population.

Row facts this module relies on (verified against the system repo)
------------------------------------------------------------------
* ``kind`` is one of ``attempt`` (default), ``external_unmetered``,
  ``subscription_session``, ``legacy_metadata``, ``legacy_delta``,
  ``usage_baseline``, ``usage_baseline_group``. Only ``attempt`` is a physical
  model request; the others are counted, never drawn.
* An attempt moves ``reserved`` → ``dispatched`` → ``settled`` / ``unresolved``,
  or ``released`` without being sent. The journal appends one row per
  transition (folded here by ``attempt_id``); the store UPDATEs one row.
* Token counts are read ONLY from a ``settled`` row. A missing count stays
  ``None`` — never zero — and an explicit ``0`` is a real measurement.
* ``cached_tokens`` / ``cache_write_tokens`` are what the provider reported.
  For some providers the input count already includes them, for others the
  relation is not established, so they are passed on separately and never
  added to the input or turned into a share.
* ``physical_context.rendered_mode`` (``max`` / ``low`` / ``nano``) is the only
  exact mode fact; its absence is Unknown and is never guessed.
* A compaction aggregate (``usage_baseline`` / ``usage_baseline_group``) sums
  many folded attempts; in the store a group row carries that count as its
  ``weight``. Aggregates are counted separately and never become points.
* The store's ``ts_last`` is the time of the latest accounting write to the row;
  a late receipt or a price refinement moves it. It is when usage was recorded
  or updated — not when a request was sent, and not a latency.
"""

from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import json
import math
import os
import re
import stat as _stat
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Fixed limits (documented in SKILL.md; no route may raise them)
# ---------------------------------------------------------------------------

LEDGER_REL_PARTS = ("state", "usage_attempts.jsonl")
MAX_BYTES_PER_REFRESH = 4 * 1024 * 1024          # <= 4 MiB read per refresh
MAX_RECORDS = 5000                               # cached journal attempt rows
DEFAULT_POINT_LIMIT = 1500
MAX_POINT_LIMIT = 4000                           # points sent in one answer
MAX_LABEL_LEN = 80

# A count larger than this cannot survive the browser's Number without silently
# changing value, so an absurd figure is reported as missing rather than as a
# number the widget would then draw wrongly.
MAX_SAFE_COUNT = 2 ** 53 - 1
# A timestamp outside this band is not a clock reading from this install; it is
# rejected instead of being plotted at the edge of the time axis.
MIN_EPOCH_MS = 1262304000000        # 2010-01-01T00:00:00Z
MAX_EPOCH_MS = 4102444800000        # 2100-01-01T00:00:00Z

ATTEMPT_STATES = ("reserved", "dispatched", "settled", "unresolved", "released")
# The ONE state whose row may carry token counts. `usage_ledger._validate_records`
# permits token fields on any row structurally, so the settled row is the only
# place this module will read them from — see `_absorb`.
SETTLED_STATE = "settled"

# Data horizons. The span is measured back from an explicit UTC anchor taken at
# projection time, and the cut is applied to the RETAINED records before any
# display limit, so points, counters, facets and coverage all describe the same
# selection. "available" means "everything this bounded reader still holds" —
# which is NOT the same as "all history"; see ``select_horizon``.
HORIZON_AVAILABLE = "available"
HORIZON_SPANS_MS = {
    "1h": 3600 * 1000,
    "6h": 6 * 3600 * 1000,
    "24h": 24 * 3600 * 1000,
    "7d": 7 * 24 * 3600 * 1000,
}
HORIZONS = ("1h", "6h", "24h", "7d", HORIZON_AVAILABLE)

# Buckets: exactly one per ledger row kind. Only ``attempt`` is plottable.
BUCKET_ATTEMPT = "attempt"
BUCKET_BASELINE = "baseline"                     # folded aggregate, not a request
BUCKET_SUBSCRIPTION = "subscription"             # harness session total, not a request
BUCKET_EXTERNAL = "external_unmetered"
BUCKET_LEGACY = "legacy_import"
BUCKET_UNKNOWN = "unknown_kind"

_KIND_BUCKETS = {
    "attempt": BUCKET_ATTEMPT,
    "": BUCKET_ATTEMPT,
    "usage_baseline": BUCKET_BASELINE,
    "usage_baseline_group": BUCKET_BASELINE,
    "subscription_session": BUCKET_SUBSCRIPTION,
    "external_unmetered": BUCKET_EXTERNAL,
    "legacy_metadata": BUCKET_LEGACY,
    "legacy_delta": BUCKET_LEGACY,
}

_LABEL_OK = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._:/+@=\[\]-]*\Z")
# The exact shape ``_opaque`` produces for a task id. A route key that does not
# match it is not echoed back in any form.
_OPAQUE_TASK_KEY = re.compile(r"\At-[0-9a-f]{12}\Z")
_MODES = ("max", "low", "nano")
_BASES = ("fresh_route_usage", "fresh_model_usage", "cold_estimate")
_PROFILES = ("owner_max", "owner_low", "owner_nano", "task_local_low", "task_local_nano")

SOURCE_STORE = "usage_store"
SOURCE_JOURNAL = "legacy_journal"


class LensUnavailable(Exception):
    """The source cannot be read. Carries a typed code, never a path or a body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Sanitizers — the allowlist boundary
# ---------------------------------------------------------------------------

def _label(value: Any, fallback: str = "") -> str:
    """A short display token, or ``fallback``. Never passes an odd string on."""
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if not text:
        return fallback
    if len(text) > MAX_LABEL_LEN or not _LABEL_OK.match(text):
        return "other"
    return text


def _opaque(value: Any, prefix: str) -> Optional[str]:
    """Stable opaque grouping key for an internal id.

    Task ids are host-internal and may carry owner-authored text, so the raw
    string never leaves the process. A digest keeps grouping exact while
    disclosing nothing.
    """
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if not text:
        return None
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _count(value: Any) -> Optional[int]:
    """A non-negative, browser-safe integer count, or ``None``.

    ``bool`` is not a count. A negative or absurdly large integer is reported as
    missing rather than passed on: an int wider than 2**53 cannot cross into a
    browser Number unchanged, and JSON-encoding an arbitrarily long integer from
    a corrupt row would inflate the response instead of failing honestly.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_SAFE_COUNT:
        return None
    return value


def _epoch_ms(ts: Any) -> Optional[int]:
    """Milliseconds since the epoch for an ISO timestamp, or ``None``.

    Every failure mode of a corrupt row is absorbed here — an unparsable string,
    a year outside ``datetime``'s range, a value that overflows the platform's
    ``timestamp()`` — so a malformed row can never raise out of a route.
    """
    text = ts if isinstance(ts, str) else ""
    if not text or len(text) > MAX_LABEL_LEN:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    try:
        milliseconds = int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None
    if milliseconds < MIN_EPOCH_MS or milliseconds > MAX_EPOCH_MS:
        return None
    return milliseconds


def _physical_context(raw: Any) -> Dict[str, Any]:
    """Allowlist the exact recorded fit metadata. Unknown values become None.

    ``rendered_mode`` is the ONLY exact Max/Low/Nano fact in the record. It
    exists only on rows whose round had a matching context-fit plan, so its
    absence is reported as Unknown and never guessed.
    """
    empty = {
        "mode": None, "profile": None, "basis": None,
        "target_total_tokens": None, "capacity_total_tokens": None,
        "target_miss": None, "auto_pass": None,
    }
    if not isinstance(raw, dict):
        return empty
    mode = raw.get("rendered_mode")
    profile = raw.get("profile")
    basis = raw.get("measurement_basis")
    return {
        "mode": mode if mode in _MODES else None,
        "profile": profile if profile in _PROFILES else None,
        "basis": basis if basis in _BASES else None,
        "target_total_tokens": _count(raw.get("target_total_tokens")),
        "capacity_total_tokens": _count(raw.get("capacity_total_tokens")),
        "target_miss": raw.get("context_target_miss") if isinstance(raw.get("context_target_miss"), bool) else None,
        "auto_pass": raw.get("automatic_pass_used") if isinstance(raw.get("automatic_pass_used"), bool) else None,
    }


# ---------------------------------------------------------------------------
# Horizon selection
# ---------------------------------------------------------------------------

def normalize_horizon(value: Any) -> str:
    """A known horizon token, or ``available``. Never raises, never echoes text."""
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in HORIZONS else HORIZON_AVAILABLE


def now_ms() -> int:
    """The UTC anchor a horizon is measured back from, in epoch milliseconds."""
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)


def record_ms(record: Dict[str, Any]) -> Optional[int]:
    """When the record was last written, in epoch ms, or ``None``.

    A store record carries ``t_ms`` from ``ts_last_epoch`` (the latest
    accounting write to the row). A journal record's ``ts`` is the timestamp of
    the last row folded into its chain, so for a settled attempt it is the
    settle time. Either is the instant ``_point()`` publishes as ``t``. A row
    whose timestamp is absent, unparsable or outside the admitted band has no
    position in time and is never placed at one.
    """
    if "t_ms" in record:
        return record["t_ms"]
    return _epoch_ms(record["ts"])


def select_horizon(
    records: List[Dict[str, Any]],
    horizon: str,
    anchor_ms: int,
    window: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cut the journal's retained records down to one horizon and describe the cut.

    Used for the legacy journal only — the store's selection is cut by its own
    indexed read. Honesty rules, all of which the widget restates:

    * The anchor is an explicit UTC instant, published as ``now_ms``; the cutoff
      is ``anchor - span`` and is published too. Nothing is "recent" by
      implication.
    * Records whose timestamp is unusable cannot be placed inside or outside a
      bounded span, so a bounded horizon leaves them out and counts them under
      ``unknown_timestamp`` instead of quietly keeping or dropping them.
    * ``reaches_cutoff`` is true ONLY when the oldest record still retained is
      at or before the cutoff. Eviction and the cold-tail prefix both drop the
      OLDEST records first, so what is retained is a contiguous suffix and that
      one comparison decides whether every journal row inside the span was read.
      That is ``selection_complete`` — completeness relative to the journal. It
      says nothing about whether the journal is still being written (it is not,
      on a host with a usage store); the source block says that separately.
    * ``available`` selects everything retained; it is complete only when the
      reader dropped nothing older.
    """
    horizon = normalize_horizon(horizon)
    span = HORIZON_SPANS_MS.get(horizon)
    cutoff = None if span is None else anchor_ms - span

    stamped: List[Tuple[int, Dict[str, Any]]] = []
    unknown: List[Dict[str, Any]] = []
    for record in records:
        moment = record_ms(record)
        if moment is None:
            unknown.append(record)
        else:
            stamped.append((moment, record))

    observed_from = min((moment for moment, _ in stamped), default=None)
    observed_to = max((moment for moment, _ in stamped), default=None)

    if cutoff is None:
        selected = list(records)
        older = 0
        kept_unknown = len(unknown)
    else:
        selected = [record for moment, record in stamped if moment >= cutoff]
        older = len(stamped) - len(selected)
        kept_unknown = 0

    selected_times = [moment for moment, record in stamped
                      if cutoff is None or moment >= cutoff]
    ahead = sum(1 for moment in selected_times if moment > anchor_ms)

    truncated = bool(window and (window.get("omitted_prefix_bytes")
                                 or window.get("evicted_records")
                                 or window.get("compaction_epoch")))
    if cutoff is None:
        reaches = None
        complete = not truncated
        covered_from = None if complete else observed_from
    else:
        reaches = observed_from is not None and observed_from <= cutoff
        complete = reaches
        covered_from = cutoff if complete else observed_from
    return {
        "records": selected,
        "selected": horizon,
        "options": list(HORIZONS),
        "span_ms": span,
        "now_ms": anchor_ms,
        "cutoff_ms": cutoff,
        "selection_complete": complete,
        "partial_reasons": [] if complete else ["journal_tail"],
        "covered_from_ms": covered_from,
        "observed_from_ms": observed_from,
        "observed_to_ms": observed_to,
        "selected_from_ms": min(selected_times) if selected_times else None,
        "selected_to_ms": max(selected_times) if selected_times else None,
        "records_retained": len(records),
        "records_selected": len(selected),
        "excluded_older_than_cutoff": older,
        "unknown_timestamp": len(unknown),
        "unknown_timestamp_kept": kept_unknown,
        "unknown_timestamp_capped": False,
        "ahead_of_anchor": ahead,
        "reaches_cutoff": reaches,
        "history_truncated_by_source": truncated,
    }


# ---------------------------------------------------------------------------
# Record folding
# ---------------------------------------------------------------------------

def _new_record(attempt_id: str, bucket: str, seq: int, ts: Any) -> Dict[str, Any]:
    return {
        "aid": attempt_id,
        "bucket": bucket,
        "first_seq": seq,
        "last_seq": seq,
        "first_ts": ts if isinstance(ts, str) else "",
        "ts": ts if isinstance(ts, str) else "",
        "state": "",
        "states": [],
        "model": "",
        "provider": "",
        "category": "",
        "source": "",
        "task": None,
        "root": None,
        "parent": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cached_tokens": None,
        "cache_write_tokens": None,
        "folded_attempt_count": None,
        "baseline": None,           # which compaction baseline a folded row belongs to
        "baseline_header": False,   # True only for the one `usage_baseline` row
        "weighted": False,          # a store aggregate whose weight IS its folded count
        "late_receipt": False,      # settled by a late receipt after being unresolved
        "fit": _physical_context(None),
    }


def _copy_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """A standalone copy: no nested object stays shared with the live cache.

    Projections run outside the lock, so they must not hold a reference a
    concurrent refresh can mutate underneath them.
    """
    clone = dict(record)
    clone["states"] = list(record["states"])
    clone["fit"] = dict(record["fit"])
    return clone


def _absorb(record: Dict[str, Any], row: Dict[str, Any], seq: int) -> None:
    """Fold one ledger row into its attempt record. Later rows win on identity."""
    record["last_seq"] = seq
    ts = row.get("ts")
    if isinstance(ts, str) and ts:
        record["ts"] = ts
        if not record["first_ts"]:
            record["first_ts"] = ts
    state = row.get("state")
    if isinstance(state, str) and state in ATTEMPT_STATES:
        record["state"] = state
        if state not in record["states"]:
            record["states"].append(state)
    record["model"] = _label(row.get("model"), record["model"])
    record["provider"] = _label(row.get("provider"), record["provider"])
    record["category"] = _label(row.get("category"), record["category"])
    record["source"] = _label(row.get("source"), record["source"])
    for slot, field, prefix in (
        ("task", "task_id", "t-"),
        ("root", "root_task_id", "r-"),
        ("parent", "parent_task_id", "p-"),
    ):
        key = _opaque(row.get(field), prefix)
        if key is not None:
            record[slot] = key
    # Tokens come from the terminal settled row and from nowhere else. A row
    # that is not settled is not evidence of a size even when a corrupt or
    # future chain puts token fields on it, and the settled row is authoritative
    # in BOTH directions: every one of the four is assigned from it, None
    # included, so an earlier stray value cannot survive a terminal absence and
    # be reported as a measured request.
    if row.get("state") == SETTLED_STATE:
        for field in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"):
            record[field] = _count(row.get(field))
    folded = _count(row.get("folded_attempt_count"))
    if folded is not None:
        record["folded_attempt_count"] = folded
    if row.get("settle_reason") == "late_receipt":
        record["late_receipt"] = True
    if isinstance(row.get("physical_context"), dict):
        record["fit"] = _physical_context(row.get("physical_context"))


# ---------------------------------------------------------------------------
# Bounded incremental reader
# ---------------------------------------------------------------------------

class _Rotated(Exception):
    """The path was replaced between the stat and the open. Read nothing."""


def _is_symlink(name: str, parent_fd: Optional[int]) -> bool:
    """Is this exact name a symlink? Asked without ever following it.

    Used only to classify an open failure into the right typed reason; nothing
    is read on the strength of the answer.
    """
    try:
        if parent_fd is None:
            info = os.lstat(name)
        else:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return _stat.S_ISLNK(info.st_mode)


class LedgerWindow:
    """A bounded, incremental, read-only view of the attempt ledger.

    One instance per serving root. All mutation happens under ``_lock`` so the
    threaded route dispatch (``asyncio.to_thread``) cannot interleave a refresh,
    and every projection copies what it returns while still holding it.
    """

    def __init__(
        self,
        data_dir: str,
        *,
        max_bytes: int = MAX_BYTES_PER_REFRESH,
        max_records: int = MAX_RECORDS,
    ) -> None:
        if not str(data_dir or "").strip():
            raise LensUnavailable("no_data_dir")
        self.data_dir = str(data_dir)
        self.path = os.path.join(str(data_dir), *LEDGER_REL_PARTS)
        self.max_bytes = max(4096, int(max_bytes))
        self.max_records = max(1, int(max_records))
        self._lock = threading.Lock()
        self._reset()

    # -- state ------------------------------------------------------------
    def _reset(self) -> None:
        self._fp: Optional[Tuple[int, int]] = None      # (st_dev, st_ino)
        self._offset = 0
        self._records: Dict[str, Dict[str, Any]] = {}
        self._lines = 0
        self._malformed = 0
        self._omitted_prefix_bytes = 0
        self._evicted = 0
        self._rotations = 0
        self._truncated_tail_bytes = 0
        self._resync = False        # mid-line: skip to the next newline first
        self._discarded_oversize_lines = 0
        self._baseline_epoch: Optional[int] = None
        self._baseline_folded: Optional[int] = None

    def reset(self) -> None:
        with self._lock:
            self._reset()

    # -- reading ----------------------------------------------------------
    def _open(self):
        """Open the fixed ledger through descriptors, never through a path.

        Checking the parents by name (``realpath``) and then opening by name is
        a race: between the check and the open, another process can swap a
        parent directory for a symlink, and ``O_NOFOLLOW`` only ever protects
        the FINAL component. So each component is opened by name RELATIVE to a
        descriptor already held — data dir, then ``state``, then the ledger —
        every hop with ``O_NOFOLLOW``. A parent swapped after its descriptor was
        obtained cannot move that descriptor, so the file this returns is
        provably inside the serving root, and every identity and size fact the
        caller uses comes from ``os.fstat`` on this descriptor.
        """
        if os.open not in os.supports_dir_fd:
            # Without dir_fd there is no way to open a child relative to a
            # verified parent, and the by-name fallback is exactly the race this
            # method exists to close. Refuse rather than read something else.
            raise LensUnavailable("ledger_platform_unsupported")
        # O_NONBLOCK so a FIFO left at this path cannot park the request thread
        # before the regular-file check below can refuse it; it has no effect on
        # reads from a regular file.
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        leaf_flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                      | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        parents = LEDGER_REL_PARTS[:-1]
        leaf = LEDGER_REL_PARTS[-1]
        def refuse(exc, name: str, parent_fd: Optional[int]) -> LensUnavailable:
            code = getattr(exc, "errno", None)
            # A symlink at any component refuses to open under O_NOFOLLOW, but
            # the errno differs: ELOOP on Linux, EMLINK on some BSDs, and — with
            # O_DIRECTORY also set — ENOTDIR on macOS, which is the same errno a
            # plain file would give. Ask what the name itself is, never following
            # it, so a directory swapped for a symlink is named as the steering
            # attempt it is rather than as an unreadable file.
            if code in (errno.ELOOP, errno.EMLINK):
                return LensUnavailable("ledger_not_confined")
            if code == errno.ENOTDIR and _is_symlink(name, parent_fd):
                return LensUnavailable("ledger_not_confined")
            return LensUnavailable("ledger_unreadable")

        opened_dirs: List[int] = []
        try:
            try:
                opened_dirs.append(os.open(self.data_dir, dir_flags))
            except FileNotFoundError:
                raise LensUnavailable("no_ledger") from None
            except OSError as exc:
                raise refuse(exc, self.data_dir, None) from None
            try:
                for part in parents:
                    parent_fd = opened_dirs[-1]
                    try:
                        opened_dirs.append(os.open(part, dir_flags, dir_fd=parent_fd))
                    except FileNotFoundError:
                        raise LensUnavailable("no_ledger") from None
                    except OSError as exc:
                        raise refuse(exc, part, parent_fd) from None
                descriptor = os.open(leaf, leaf_flags, dir_fd=opened_dirs[-1])
            except FileNotFoundError:
                raise LensUnavailable("no_ledger") from None
            except OSError as exc:
                raise refuse(exc, leaf, opened_dirs[-1]) from None
        finally:
            for handle in opened_dirs:
                try:
                    os.close(handle)
                except OSError:                       # pragma: no cover
                    pass
        try:
            opened = os.fstat(descriptor)
            if not _stat.S_ISREG(opened.st_mode):
                raise LensUnavailable("ledger_not_regular")
            return os.fdopen(descriptor, "rb"), opened
        except LensUnavailable:
            os.close(descriptor)
            raise
        except OSError:
            os.close(descriptor)
            raise LensUnavailable("ledger_unreadable") from None

    def refresh(self, *, force_cold: bool = False) -> Dict[str, Any]:
        """Read what is new (or a bounded cold tail) and return window facts."""
        with self._lock:
            for _ in range(3):
                try:
                    return self._read_locked(force_cold=force_cold)
                except _Rotated:
                    # The file was replaced under us; the retry starts cold from
                    # a settled path rather than trusting a stale byte offset.
                    force_cold = True
            raise LensUnavailable("ledger_unreadable")

    def _read_locked(self, *, force_cold: bool) -> Dict[str, Any]:
        # A pre-open stat, used ONLY as a rotation trip-wire: if the identity it
        # reports differs from the fstat of the descriptor `_open` returns, the
        # path was replaced mid-call and the byte offset we hold describes the
        # previous generation. Nothing is read on the strength of this stat, and
        # it is not the confinement check — `_open` owns that, by descriptor.
        try:
            pre = os.stat(self.path)
        except FileNotFoundError:
            raise LensUnavailable("no_ledger") from None
        except OSError:
            raise LensUnavailable("ledger_unreadable") from None

        handle, opened = self._open()
        with handle:
            fingerprint = (int(opened.st_dev), int(opened.st_ino))
            if fingerprint != (int(pre.st_dev), int(pre.st_ino)):
                # Compaction replaced the path between the stat and the open.
                # The offset we hold describes the previous generation, so
                # reading here would splice two files into one window. Nothing
                # is read from this handle at all.
                rotations = self._rotations + 1
                self._reset()
                self._rotations = rotations
                raise _Rotated()

            # Every size and identity decision below comes from the OPENED file,
            # so the bytes read are provably the bytes that were measured.
            size = int(opened.st_size)
            cold = force_cold or self._fp is None
            rotated = False
            if self._fp is not None and fingerprint != self._fp:
                # Compaction rewrites the ledger atomically onto a new inode.
                cold = rotated = True
            elif size < self._offset:
                # A shorter file under the same inode: a quarantined torn tail
                # was truncated away. Re-read rather than trust a stale offset.
                cold = rotated = True
            elif size - self._offset > self.max_bytes:
                # Too far behind to catch up inside the per-refresh bound: take
                # a bounded cold tail and disclose the prefix we skipped.
                cold = True

            if cold:
                rotations = self._rotations + (1 if rotated else 0)
                self._reset()
                self._rotations = rotations
                self._offset = max(0, size - self.max_bytes)
                self._omitted_prefix_bytes = self._offset
            self._fp = fingerprint
            start = self._offset

            try:
                handle.seek(self._offset)
                chunk = handle.read(self.max_bytes)
            except OSError:
                raise LensUnavailable("ledger_unreadable") from None

            if self._resync or (cold and start > 0):
                # We landed mid-line: drop the partial prefix, disclose it.
                cut = chunk.find(b"\n")
                if cut < 0:
                    # No terminator anywhere in this chunk. The line it belongs
                    # to may simply still be being written, so treat it as the
                    # torn tail it is: hold the offset and report the bytes,
                    # exactly as the incremental path does, instead of feeding a
                    # fragment to the parser and counting it malformed.
                    if len(chunk) >= self.max_bytes:
                        # ...unless waiting could never help: a single line at
                        # least as long as the whole per-refresh budget can
                        # never be read whole here, so retrying forever would
                        # stall the reader. DISCARD POLICY: such a line is
                        # skipped, its bytes are added to omitted_prefix_bytes
                        # and it is counted in discarded_oversize_lines. The
                        # skip continues on the next refresh until the newline
                        # that ends the line is finally reached.
                        self._offset += len(chunk)
                        self._omitted_prefix_bytes += len(chunk)
                        if not self._resync:
                            self._discarded_oversize_lines += 1   # once per line
                        self._resync = True
                        self._truncated_tail_bytes = 0
                    else:
                        self._offset = start
                        self._resync = True
                        self._truncated_tail_bytes = len(chunk)
                    self._evict()
                    return self._window()
                self._offset = start + cut + 1
                self._omitted_prefix_bytes += cut + 1
                self._resync = False
                chunk = chunk[cut + 1:]

            # A row is consumed only once its terminating newline is on disk;
            # a torn or still-being-written tail therefore costs nothing.
            end = chunk.rfind(b"\n")
            self._truncated_tail_bytes = len(chunk) - (end + 1)
            if end >= 0:
                self._ingest(chunk[: end + 1])
                self._offset += end + 1

            self._evict()
            return self._window()

    def _ingest(self, payload: bytes) -> None:
        for raw in payload.split(b"\n"):
            if not raw.strip():
                continue
            self._lines += 1
            try:
                row = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._malformed += 1
                continue
            if not isinstance(row, dict):
                self._malformed += 1
                continue
            attempt_id = row.get("attempt_id")
            seq = row.get("seq")
            if not isinstance(attempt_id, str) or not attempt_id:
                self._malformed += 1
                continue
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1 or seq > MAX_SAFE_COUNT:
                self._malformed += 1
                continue
            # An absent `kind` means `attempt` (usage_ledger's default). Anything
            # else that is not a known kind string — including a non-string kind
            # from a corrupt row — is an unknown kind, never an ordinary attempt.
            raw_kind = row.get("kind")
            if raw_kind is None:
                kind = "attempt"
            elif isinstance(raw_kind, str):
                kind = raw_kind.strip() or "attempt"
            else:
                kind = None
            bucket = BUCKET_UNKNOWN if kind is None else _KIND_BUCKETS.get(kind, BUCKET_UNKNOWN)
            if bucket == BUCKET_BASELINE and kind == "usage_baseline":
                self._baseline_epoch = _count(row.get("compaction_epoch"))
                self._baseline_folded = _count(row.get("folded_attempt_count"))
            record = self._records.get(attempt_id)
            if record is None:
                record = _new_record(attempt_id, bucket, seq, row.get("ts"))
                self._records[attempt_id] = record
            _absorb(record, row, seq)
            if bucket == BUCKET_BASELINE:
                # Which fold this row describes, so a header and its own group
                # rows are never counted as two separate populations.
                record["baseline"] = (
                    _opaque(row.get("baseline_id"), "b-")
                    or record["baseline"]
                    or _opaque(attempt_id, "b-")
                )
                if kind == "usage_baseline":
                    record["baseline_header"] = True

    def _evict(self) -> None:
        excess = len(self._records) - self.max_records
        if excess <= 0:
            return
        order = sorted(self._records.items(), key=lambda item: item[1]["last_seq"])
        for attempt_id, _ in order[:excess]:
            self._records.pop(attempt_id, None)
            self._evicted += 1

    # -- projections ------------------------------------------------------
    def _window(self) -> Dict[str, Any]:
        seqs = [record["last_seq"] for record in self._records.values()]
        return {
            "first_seq": min(seqs) if seqs else None,
            "last_seq": max(seqs) if seqs else None,
            "lines_read": self._lines,
            "malformed_lines": self._malformed,
            "omitted_prefix_bytes": self._omitted_prefix_bytes,
            "pending_tail_bytes": self._truncated_tail_bytes,
            "discarded_oversize_lines": self._discarded_oversize_lines,
            "evicted_records": self._evicted,
            "rotations_observed": self._rotations,
            "max_bytes_per_refresh": self.max_bytes,
            "max_records": self.max_records,
            "compaction_epoch": self._baseline_epoch,
            "compaction_folded_attempts": self._baseline_folded,
        }

    def _copy_locked(self) -> List[Dict[str, Any]]:
        """A coherent, bounded snapshot of the record cache. Call under the lock."""
        return sorted(
            (_copy_record(record) for record in self._records.values()),
            key=lambda record: record["last_seq"],
        )

    def records(self) -> List[Dict[str, Any]]:
        """Detached copies: a concurrent refresh cannot mutate what a caller holds."""
        with self._lock:
            return self._copy_locked()

    def snapshot(
        self,
        *,
        limit: int = DEFAULT_POINT_LIMIT,
        horizon: str = HORIZON_AVAILABLE,
        anchor_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """The whole browser-visible payload from the journal. Allowlisted.

        One record copy is taken under the lock and everything below is derived
        from it, so the horizon cut, the counters, the facets, the points and the
        selection facts all describe exactly the same population. The horizon is
        applied to the retained records FIRST; the display limit then trims only
        which of the selected points are sent, and says how many it dropped.
        """
        with self._lock:
            records = self._copy_locked()
            window = self._window()
        anchor = _anchor(anchor_ms)
        selection = select_horizon(records, horizon, anchor, window)
        selected = selection.pop("records")
        source = {
            "kind": SOURCE_JOURNAL,
            # Core stopped appending to the journal at the one-time import, so
            # its newest row says nothing about whether the install is idle.
            "current": False,
            "read_at_ms": anchor,
            "newest_record_ms": selection["observed_to_ms"],
            "schema_version": None,
            "lock_tier": None,
            "read_ms": None,
            "transaction_ms": None,
            "sql_steps": None,
            "metadata_read": None,
            "context_unread": 0,
        }
        limits = {"max_rows": self.max_records, "max_points": MAX_POINT_LIMIT,
                  "max_bytes_per_refresh": self.max_bytes}
        payload = assemble(selected, selection, source, limit=limit, limits=limits)
        payload["window"] = window
        return payload


def _anchor(anchor_ms: Any) -> int:
    if isinstance(anchor_ms, int) and not isinstance(anchor_ms, bool):
        return anchor_ms
    return now_ms()


# ---------------------------------------------------------------------------
# The usage store: one row per attempt -> the same record shape
# ---------------------------------------------------------------------------

_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")


def store_record(row: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """One store row as a record, plus how its ``extra`` metadata fared.

    The store keeps ONE current row per attempt, so there is no chain to fold:
    the row's own state is the attempt's state, and token counts are taken from
    it only when that state is ``settled`` (the same single authority the
    journal fold uses). Of the row's ``extra`` metadata ``lens_store`` keeps
    exactly one key — ``physical_context`` — and it passes the same allowlist as
    before; nothing else in ``extra`` (credential profiles, session digests,
    failure evidence, reasons) reaches this function. The second value is the
    metadata status: ``ok`` / ``absent`` when it was read, ``unread`` when it was
    larger than the read bound and ``malformed`` when it was not a JSON object —
    in both of those last cases the mode stays Unknown and the read says how many
    rows that affected.
    """
    attempt_id = row.get("attempt_id")
    raw_kind = row.get("kind")
    if raw_kind is None:
        kind: Optional[str] = "attempt"
    elif isinstance(raw_kind, str):
        kind = raw_kind.strip() or "attempt"
    else:
        kind = None
    bucket = BUCKET_UNKNOWN if kind is None else _KIND_BUCKETS.get(kind, BUCKET_UNKNOWN)
    record = _new_record(attempt_id if isinstance(attempt_id, str) else "", bucket, 0, row.get("ts_last"))
    epoch = row.get("ts_last_epoch")
    moment = None
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool) and math.isfinite(epoch):
        candidate = int(epoch * 1000)
        if MIN_EPOCH_MS <= candidate <= MAX_EPOCH_MS:
            moment = candidate
    record["t_ms"] = moment
    state = row.get("state")
    if isinstance(state, str) and state in ATTEMPT_STATES:
        record["state"] = state
        record["states"] = [state]
    for slot in ("model", "provider", "category", "source"):
        record[slot] = _label(row.get(slot))
    for slot, field, prefix in (("task", "task_id", "t-"), ("root", "root_task_id", "r-"),
                                ("parent", "parent_task_id", "p-")):
        record[slot] = _opaque(row.get(field), prefix)
    if record["state"] == SETTLED_STATE:
        for field in _TOKEN_FIELDS:
            record[field] = _count(row.get(field))
    if bucket == BUCKET_BASELINE:
        # An imported compaction aggregate: its weight IS the number of attempts
        # it folded (core `_derived`), and its token fields are sums.
        record["folded_attempt_count"] = _count(row.get("weight"))
        record["weighted"] = True
    flag = row.get("late_receipt")
    record["late_receipt"] = flag == 1 and not isinstance(flag, bool)
    context = row.get("physical_context")
    if isinstance(context, dict):
        record["fit"] = _physical_context(context)
    status = row.get("extra_status")
    return record, status if status in ("ok", "absent", "unread", "malformed") else "absent"


def store_snapshot(
    read: Dict[str, Any],
    *,
    horizon: str,
    anchor_ms: int,
    limit: int = DEFAULT_POINT_LIMIT,
    limits: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The browser-visible payload for one bounded store read.

    ``read`` is what ``lens_store.read_store`` returned: rows newest first and
    the facts of that read. Completeness is relative to enumerated category
    streams: legacy-only named categories can be absent. With core-maintained
    summaries these streams cover every timestamp-eligible physical attempt.
    No bound may bite for ``selection_complete`` to be true. Otherwise the
    newest-first prefix is within the streams read (not globally when categories
    are capped), and ``covered_from_ms`` is the oldest record in it.
    Freshness is stated separately (``source.read_at_ms`` / ``newest_record_ms``)
    because an idle install has an old newest record and is still current.
    """
    horizon = normalize_horizon(horizon)
    facts = read.get("facts") or {}
    records: List[Dict[str, Any]] = []
    unread = 0
    for row in reversed(read.get("rows") or []):          # oldest first, like the journal
        record, context = store_record(row)
        if context in ("unread", "malformed"):
            unread += 1
        records.append(record)
    span = HORIZON_SPANS_MS.get(horizon)
    cutoff = None if span is None else anchor_ms - span
    times = [record["t_ms"] for record in records if record["t_ms"] is not None]
    partial = [str(reason) for reason in facts.get("partial_reasons") or []]
    complete = not partial
    oldest = min(times) if times else None
    newest_s = facts.get("newest_epoch_s")
    newest = None
    if isinstance(newest_s, (int, float)) and not isinstance(newest_s, bool) and math.isfinite(newest_s):
        newest = int(newest_s * 1000)
    selection = {
        "selected": horizon,
        "options": list(HORIZONS),
        "span_ms": span,
        "now_ms": anchor_ms,
        "cutoff_ms": cutoff,
        "selection_scope": "enumerated_categories",
        "selection_complete": complete,
        "partial_reasons": partial,
        # Rows sharing that exact instant may sit on both sides of a row cap.
        "covered_from_ms": cutoff if complete else oldest,
        "observed_from_ms": None,
        "observed_to_ms": None,
        "selected_from_ms": oldest,
        "selected_to_ms": max(times) if times else None,
        "records_retained": None,
        "records_selected": len(records),
        "excluded_older_than_cutoff": None,
        "unknown_timestamp": int(facts.get("unknown_timestamp") or 0),
        "unknown_timestamp_kept": 0,
        "unknown_timestamp_capped": bool(facts.get("unknown_timestamp_capped")),
        "ahead_of_anchor": sum(1 for moment in times if moment > anchor_ms),
        "reaches_cutoff": None,
        "history_truncated_by_source": False,
    }
    source = {
        "kind": SOURCE_STORE,
        "current": True,
        "read_at_ms": anchor_ms,
        "newest_record_ms": newest,
        "schema_version": facts.get("schema_version"),
        "lock_tier": facts.get("lock_tier"),
        "read_ms": facts.get("read_ms"),
        "transaction_ms": facts.get("transaction_ms"),
        "sql_steps": facts.get("sql_steps"),
        "metadata_read": facts.get("metadata_read"),
        "category_enumeration": facts.get("category_enumeration"),
        "legacy_category_coverage": facts.get("legacy_category_coverage"),
        "context_unread": unread,
    }
    return assemble(records, selection, source, limit=limit, limits=dict(limits or {}))


# ---------------------------------------------------------------------------
# One projection for both sources
# ---------------------------------------------------------------------------

def assemble(
    records: List[Dict[str, Any]],
    horizon: Dict[str, Any],
    source: Dict[str, Any],
    *,
    limit: int,
    limits: Dict[str, Any],
) -> Dict[str, Any]:
    """One payload from one selection; ``records`` run oldest to newest.

    Counters cover every selected record; the display limit keeps the NEWEST
    attempts and says how many it did not send; facets describe what was sent.
    """
    attempts = [record for record in records if _is_attempt(record)]
    try:
        wanted = int(limit or DEFAULT_POINT_LIMIT)
    except (TypeError, ValueError):
        wanted = DEFAULT_POINT_LIMIT
    bounded = max(1, min(wanted, MAX_POINT_LIMIT))
    shown = attempts[-bounded:]
    horizon = dict(horizon)
    horizon["attempts_selected"] = len(attempts)
    horizon["points_sent"] = len(shown)
    return {
        "ok": True,
        "available": True,
        "source": source,
        "horizon": horizon,
        "limits": limits,
        "window": None,
        "counters": counters(records),
        "facets": facets(shown),
        "points": [_point(record) for record in shown],
        "points_omitted": max(0, len(attempts) - len(shown)),
    }


def _is_attempt(record: Dict[str, Any]) -> bool:
    """True for a physical attempt whose chain actually reached a known state.

    A row that carries no recognised ``state`` is not an ordinary request: it is
    a row this reader cannot place in the attempt lifecycle. It is counted under
    ``excluded.attempts_without_state`` and never drawn, listed or grouped.
    """
    return record["bucket"] == BUCKET_ATTEMPT and record["state"] in ATTEMPT_STATES


def _point(record: Dict[str, Any]) -> Dict[str, Any]:
    """The ONLY shape a physical attempt takes on its way to the browser.

    ``t`` is when usage was recorded or last updated (see ``record_ms``) — not
    when the request was sent. No elapsed time is derived: the gap between two
    accounting writes includes queueing and late accounting, so it is not a
    latency.
    """
    fit = record["fit"]
    return {
        # A digest of the WHOLE attempt id: a raw prefix is neither sanitized
        # nor collision-safe, and two ids sharing 16 characters would become one
        # selectable request in the widget.
        "id": "a-" + hashlib.sha256(record["aid"].encode("utf-8")).hexdigest()[:16],
        "t": record_ms(record),
        "state": record["state"],
        "model": record["model"] or "unknown",
        "provider": record["provider"] or "unknown",
        "category": record["category"] or "unknown",
        "source": record["source"] or "unknown",
        "task": record["task"],
        "root": record["root"],
        "parent": record["parent"],
        "prompt_tokens": record["prompt_tokens"],
        "completion_tokens": record["completion_tokens"],
        "cached_tokens": record["cached_tokens"],
        "cache_write_tokens": record["cache_write_tokens"],
        "mode": fit["mode"],
        "profile": fit["profile"],
        "basis": fit["basis"],
        "target_total_tokens": fit["target_total_tokens"],
        "capacity_total_tokens": fit["capacity_total_tokens"],
        "target_miss": fit["target_miss"],
        "auto_pass": fit["auto_pass"],
        "late_receipt": bool(record.get("late_receipt")),
    }


def counters(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Exact tallies over one selection. Derived, so never double-counted.

    Every number here is a count of DISTINCT attempt ids, so an incremental
    journal refresh that re-folds a known chain, or a store row UPDATEd between
    two reads, cannot inflate it.

    ``registered_attempts`` counts every attempt in a known state, including
    ones that were only reserved or were released without being sent;
    ``sent_attempts`` is the subset that was dispatched (``dispatched``,
    ``settled``, ``unresolved`` — core's physical-call rule). The two must never
    be shown under one label.

    Folded attempts are counted DISJOINTLY. In the journal one compaction
    writes a ``usage_baseline`` header whose ``folded_attempt_count`` is the
    total for that fold, plus ``usage_baseline_group`` rows whose counts
    partition the same attempts, so a fold is counted from its header when the
    header is in the window and from its group rows only when it is not
    (``folded_attempts_from_groups`` is then a lower bound). In the store the
    header lives outside the attempts table and each group row's weight IS its
    folded count (``folded_attempts_weighted``).
    """
    by_state = {state: 0 for state in ATTEMPT_STATES}
    registered = measured = settled_without_tokens = weighted = 0
    excluded = {
        "baseline_rows": 0, "baseline_header_rows": 0, "baseline_group_rows": 0,
        "folded_attempts": 0, "folded_attempts_from_headers": 0,
        "folded_attempts_from_groups": 0, "folded_attempts_weighted": 0,
        "baselines_without_header": 0,
        "subscription_sessions": 0, "external_unmetered": 0, "legacy_rows": 0,
        "unknown_kind": 0, "attempts_without_state": 0,
    }
    headers: Dict[str, int] = {}
    groups: Dict[str, int] = {}
    for record in records:
        bucket = record["bucket"]
        if bucket == BUCKET_ATTEMPT:
            if record["state"] not in by_state:
                excluded["attempts_without_state"] += 1
                continue
            registered += 1
            by_state[record["state"]] += 1
            if record["state"] == SETTLED_STATE:
                if record["prompt_tokens"] is None:
                    settled_without_tokens += 1
                else:
                    measured += 1
            continue
        if bucket == BUCKET_BASELINE:
            excluded["baseline_rows"] += 1
            folded = int(record["folded_attempt_count"] or 0)
            if record.get("weighted"):
                excluded["baseline_group_rows"] += 1
                weighted += folded
                continue
            key = record["baseline"] or record["aid"]
            if record["baseline_header"]:
                excluded["baseline_header_rows"] += 1
                headers[key] = headers.get(key, 0) + folded
            else:
                excluded["baseline_group_rows"] += 1
                groups[key] = groups.get(key, 0) + folded
        elif bucket == BUCKET_SUBSCRIPTION:
            excluded["subscription_sessions"] += 1
        elif bucket == BUCKET_EXTERNAL:
            excluded["external_unmetered"] += 1
        elif bucket == BUCKET_LEGACY:
            excluded["legacy_rows"] += 1
        else:
            excluded["unknown_kind"] += 1
    from_headers = sum(headers.values())
    orphaned = [key for key in groups if key not in headers]
    from_groups = sum(groups[key] for key in orphaned)
    excluded["folded_attempts_from_headers"] = from_headers
    excluded["folded_attempts_from_groups"] = from_groups
    excluded["folded_attempts_weighted"] = weighted
    excluded["baselines_without_header"] = len(orphaned)
    excluded["folded_attempts"] = from_headers + from_groups + weighted
    return {
        "registered_attempts": registered,
        "sent_attempts": by_state["dispatched"] + by_state["settled"] + by_state["unresolved"],
        "by_state": by_state,
        "measured": measured,
        "settled_without_tokens": settled_without_tokens,
        "excluded": excluded,
    }


def facets(records: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Filter options taken from the data, never from an invented taxonomy."""
    models, categories, sources, modes = set(), set(), set(), set()
    for record in records:
        models.add(record["model"] or "unknown")
        categories.add(record["category"] or "unknown")
        sources.add(record["source"] or "unknown")
        modes.add(record["fit"]["mode"] or "unknown")
    return {
        "models": sorted(models),
        "categories": sorted(categories),
        "sources": sorted(sources),
        "modes": [mode for mode in _MODES + ("unknown",) if mode in modes],
    }


# ---------------------------------------------------------------------------
# Trajectory (compatibility route)
# ---------------------------------------------------------------------------

def trajectory(
    points: Iterable[Dict[str, Any]],
    task_key: str,
    *,
    snapshot: Optional[str] = None,
    horizon: Optional[Dict[str, Any]] = None,
    points_omitted: int = 0,
) -> Dict[str, Any]:
    """One task's measured requests, taken from ONE snapshot's points.

    The widget derives its task focus from the overview it already holds; this
    answer exists for compatibility and is bounded the same way: it groups the
    snapshot's settled, measured, timed points of one task by (model, category)
    and lists other tasks of the same tree separately. Nothing is ``joined``: a
    shared task, model and category does not prove one growing context —
    parallel review slots, providers and sources can share all three — so no
    line is implied. Requests of this task outside the snapshot (before its
    cutoff, or older than its point cap) are NOT counted here, and
    ``own_outside_horizon`` says so with ``None`` rather than a false zero.

    The route key must be an opaque key this module itself produced; anything
    else answers with the empty shape and is never echoed back.
    """
    answer: Dict[str, Any] = {
        "task": "", "root": None, "snapshot": snapshot, "horizon": horizon,
        "groups": [], "related": [], "joined": False,
        "own_outside_horizon": None,
        "points_omitted_in_snapshot": int(points_omitted or 0),
    }
    wanted = task_key.strip() if isinstance(task_key, str) else ""
    if not _OPAQUE_TASK_KEY.match(wanted):
        return answer
    plottable = [point for point in points
                 if point.get("state") == SETTLED_STATE
                 and isinstance(point.get("prompt_tokens"), int)
                 and point.get("t") is not None]
    own: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    root = None
    for point in plottable:
        if point["task"] == wanted:
            root = root or point["root"]
            own.setdefault((point["model"], point["category"]), []).append(point)
    related: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    if root is not None:
        for point in plottable:
            # A point with no task cannot be attributed to a sibling task, so it
            # is left out rather than merged into one nameless group.
            if point["task"] is None or point["task"] == wanted or point["root"] != root:
                continue
            related.setdefault((point["task"], point["model"], point["category"]), []).append(point)

    def _series(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(items, key=lambda point: (point["t"], point["id"]))

    answer.update({
        "task": wanted,
        "root": root,
        "groups": [
            {"model": model, "category": category, "joined": False, "points": _series(items)}
            for (model, category), items in sorted(own.items())
        ],
        "related": [
            {"task": task, "model": model, "category": category, "joined": False,
             "points": _series(items)}
            for (task, model, category), items in sorted(related.items())
        ],
    })
    return answer


# ---------------------------------------------------------------------------
# Statistics (used by tests; the widget recomputes them per active filter)
# ---------------------------------------------------------------------------

def quantile(values: List[int], fraction: float) -> Optional[float]:
    """Linear-interpolated quantile over a non-empty sorted-able list."""
    numbers = sorted(int(value) for value in values)
    if not numbers:
        return None
    if len(numbers) == 1:
        return float(numbers[0])
    position = max(0.0, min(1.0, float(fraction))) * (len(numbers) - 1)
    low = int(position)
    high = min(low + 1, len(numbers) - 1)
    weight = position - low
    return numbers[low] * (1.0 - weight) + numbers[high] * weight


def spread(points: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    values = [point["prompt_tokens"] for point in points if isinstance(point.get("prompt_tokens"), int)]
    if not values:
        return {"count": 0, "median": None, "p95": None, "peak": None, "low": None}
    return {
        "count": len(values),
        "median": quantile(values, 0.5),
        "p95": quantile(values, 0.95),
        "peak": max(values),
        "low": min(values),
    }
