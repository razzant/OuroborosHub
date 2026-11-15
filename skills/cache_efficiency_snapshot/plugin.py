"""Multi-timeframe cache efficiency analytics for Ouroboros.

Everything comes from the event log (the live ``logs/events.jsonl`` plus its rotated
``archive/events_*.jsonl`` files): a bounded in-memory reader serves the recent seven
days exactly; Month, All and older Custom ranges add the skill's own incremental history
index (``history.py``) kept in the host-assigned state directory. Per-call ``llm_usage``
events are requests; an ``llm_usage`` event carrying a Claudexor run receipt is a whole
harness session, and a ``delegate_run_ledger_recorded`` event without one is a session
whose tokens the log does not hold; ``worker_boot`` events mark when new code started.
Cores before the usage store (7.6.0 and older) still keep session tokens in a live
``state/usage_attempts.jsonl``, which is read while it keeps changing. Measurements keep
explicit coverage and follow the host record contract: a zero in a row not stamped
``record_contract_version: 2`` is ambiguous and counts as unknown.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import operator
import sqlite3
import threading
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

TERMINAL_STATE = "settled"
MAX_MODELS_RETURNED = 25
MAX_BUCKETS_RETURNED = 120
MAX_MARKERS_RETURNED = 60
MAX_CACHE_RETAIN_DAYS = 30
# Interval bounds before any list is built: about ten years of days, or of six-hour bins.
MAX_DAILY_INTERVALS = 3700
MAX_INTRADAY_INTERVALS = 15000
BASELINE_KINDS = frozenset({"usage_baseline", "usage_baseline_group"})
USAGE_EVENT_TYPE = "llm_usage"
SESSION_LEDGER_EVENT_TYPE = "delegate_run_ledger_recorded"
BOOT_EVENT_TYPE = "worker_boot"
_EVENT_MARKERS = tuple(f'"{name}"'.encode() for name in (USAGE_EVENT_TYPE, SESSION_LEDGER_EVENT_TYPE, BOOT_EVENT_TYPE))
NO_USAGE_EVENT_MODEL = "no usage event"
USAGE_ARCHIVE_WINDOW_DAYS = 7
FIRST_PASS_WAIT_SECONDS = 2.0  # a cold reading waits at most this long for the index's first pass
# A reading that finds the index committed past its view waits at most this long, in all, for the newer view.
STALE_WAIT_SECONDS = 3.0
CONTRACT_STAMP = 2  # record_contract.RECORD_CONTRACT_VERSION: rows that distinguish null from 0
LEGACY_TOTAL, LEGACY_READ, LEGACY_WRITE = 1, 2, 4
_EMPTY_EVENT_STATS = {
    "total_lines_read": 0,
    "malformed_lines": 0,
    "non_usage_lines": 0,
    "usage_events": 0,
    "session_usage_events": 0,
    "session_ledger_events": 0,
    "worker_boots": 0,
    "boots_without_code": 0,
    "unparsed_events": 0,
    "duplicate_events_skipped": 0,
}

_EMPTY_LEDGER_STATS = {
    "total_lines_read": 0,
    "malformed_lines": 0,
    "non_settled_lines": 0,
    "valid_records": 0,
    "compacted_records_skipped": 0,
    "request_rows_skipped": 0,
    "unsupported_rows_skipped": 0,
}

_DATA_DIR: Optional[Path] = None


class LedgerCache:
    """Thread-safe incremental cache for whole-session rows of a live JSONL ledger.

    Cores before the usage store keep session tokens only here. Physical request
    rows are NOT taken from it: compaction folds them into ``usage_baseline_group``
    summaries within hours, so the per-call ``llm_usage`` events are the request
    source (``UsageEventsCache``). Only identified ``subscription_session`` rows are
    sessions; every other settled kind (``external_unmetered`` and the like) is
    counted as unsupported and skipped.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.file_identity: Optional[Tuple[int, int]] = None  # (inode, dev)
        self.last_mtime_ns: int = 0
        self.last_size: int = 0
        self.records: List[Dict[str, Any]] = []
        self.raw_stats: Dict[str, int] = dict(_EMPTY_LEDGER_STATS)

    def clear(self) -> None:
        with self._lock:
            self.file_identity = None
            self.last_mtime_ns = 0
            self.last_size = 0
            self.records = []
            self.raw_stats = dict(_EMPTY_LEDGER_STATS)

    def update(self, ledger_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        with self._lock:
            meta: Dict[str, Any] = {
                "exists": ledger_path.is_file(),
                "ledger_path": str(ledger_path),
                "read_error": None,
                "raw_stats": dict(self.raw_stats),
            }
            if not meta["exists"]:
                meta["read_error"] = "file_not_found"
                return list(self.records), meta

            try:
                stat = ledger_path.stat()
            except OSError as exc:
                meta["read_error"] = f"stat_error:{type(exc).__name__}"
                return list(self.records), meta

            current_ident = (stat.st_ino, stat.st_dev)
            current_mtime = stat.st_mtime_ns
            current_size = stat.st_size
            meta["file_size_bytes"] = current_size

            # If file replaced or truncated, do a full reload
            if (
                self.file_identity != current_ident
                or current_size < self.last_size
                or self.file_identity is None
            ):
                self.records = []
                self.last_size = 0
                self.raw_stats = dict(_EMPTY_LEDGER_STATS)
                self.file_identity = current_ident

            # If no change in size/mtime and we already have records, return cached copy
            if current_size == self.last_size and current_mtime == self.last_mtime_ns and self.records:
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            # Read only new bytes
            start_offset = self.last_size
            try:
                with ledger_path.open("rb") as f:
                    if start_offset > 0:
                        f.seek(start_offset)
                    new_bytes = f.read()
            except OSError as exc:
                meta["read_error"] = f"read_error:{type(exc).__name__}"
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            if not new_bytes:
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            # Find the last newline so we never parse a torn trailing line
            cut = new_bytes.rfind(b"\n")
            if cut < 0:
                # Incomplete partial line: do not advance offset yet
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            complete_bytes = new_bytes[: cut + 1]
            self.last_size = start_offset + len(complete_bytes)
            self.last_mtime_ns = current_mtime

            lines = complete_bytes.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                if not line.strip():
                    continue
                self.raw_stats["total_lines_read"] += 1
                try:
                    row = json.loads(line)
                except Exception:
                    self.raw_stats["malformed_lines"] += 1
                    continue

                if not isinstance(row, dict):
                    self.raw_stats["malformed_lines"] += 1
                    continue

                state = str(row.get("state") or "").lower()
                if state != TERMINAL_STATE:
                    self.raw_stats["non_settled_lines"] += 1
                    continue

                kind = str(row.get("kind") or "attempt")  # the core ledger reader's default
                # Compaction rows (the settled ``usage_baseline`` header and its
                # folded ``usage_baseline_group`` rows) carry compaction time,
                # not per-request timestamps; the core reader skips them too.
                if kind in BASELINE_KINDS:
                    self.raw_stats["compacted_records_skipped"] += 1
                    continue
                # Physical requests are counted from the per-call usage events
                # (they outlive compaction there); counting the ledger row too
                # would double the request.
                if kind == "attempt":
                    self.raw_stats["request_rows_skipped"] += 1
                    continue
                # A session is keyed by its run; a row without that key, or of
                # another kind, has no session to replace or count.
                parsed = _normalize_record(row) if kind == "subscription_session" and row.get("session_id_sha256") else None
                if parsed is None:
                    self.raw_stats["unsupported_rows_skipped"] += 1
                    continue
                self.records.append(parsed)
                self.raw_stats["valid_records"] += 1

            # Bounded memory retention: prune records older than MAX_CACHE_RETAIN_DAYS
            if len(self.records) > 20000:
                cutoff_dt = datetime.now(timezone.utc) - timedelta(days=MAX_CACHE_RETAIN_DAYS)
                self.records = [r for r in self.records if r["ts"] >= cutoff_dt]

            meta["raw_stats"] = dict(self.raw_stats)
            return list(self.records), meta


_CACHE = LedgerCache()

# Every field that distinguishes one reported call from another: a byte-identical replay
# (a rotation seam) hashes the same; rows differing in source, provider or any counter do not.
_REQUEST_IDENTITY_FIELDS = ("ts", "task_id", "model", "source", "provider", "api_key_type", "prompt_tokens",
                            "completion_tokens", "cached_tokens", "cache_write_tokens", "input_token_usage",
                            "record_contract_version", "llm_call_id", "ledger_attempt_ids")


def _usage_event_identity(row: Mapping[str, Any]) -> str:
    """One physical call = one event; a byte-identical replay (rotation seam) is the same call."""
    facts = [row.get(key) for key in _REQUEST_IDENTITY_FIELDS]
    encoded = json.dumps(facts, sort_keys=True, default=str, separators=(",", ":"))
    return "request:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def identity_fingerprint(identity: str) -> int:
    """The 63-bit key (a signed SQLite integer) the history index stores for a hashed identity, never a raw value."""
    digest = identity.split(":", 1)[-1]
    return int(digest[:16], 16) >> 1


def _stamped(row: Mapping[str, Any]) -> bool:
    return row.get("record_contract_version") == CONTRACT_STAMP


def _normalize_usage_event(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A per-call ``llm_usage`` event: inclusive input, cache reads, cache writes.

    An event carrying a Claudexor run receipt is one whole harness session, never a
    request: its counters are the run summary's, read by the session rules."""
    receipt = row.get("claudexor")
    run_id = receipt.get("run_id") if isinstance(receipt, Mapping) else None
    if run_id:
        return _session_from_usage_event(row, receipt, str(run_id))
    stamp = _parse_timestamp(row)
    if stamp is None:
        return None
    provider = str(row.get("provider") or row.get("api_key_type") or "unknown")
    model = str(row.get("model") or "unknown")
    record = _measurement("attempt", provider, model, _token_count(row.get("prompt_tokens")),
                          _token_count(row.get("cached_tokens")), _token_count(row.get("cache_write_tokens")),
                          stamped=_stamped(row))
    record.update(ts=stamp, identity=_usage_event_identity(row))
    return record


def _session_identity(run_id: str) -> str:
    """The ledger keys a session by the sha256 of its run id; every source uses that key."""
    return "session:" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _session_from_usage_event(row: Mapping[str, Any], receipt: Mapping[str, Any],
                              run_id: str) -> Optional[Dict[str, Any]]:
    """The session row the ledger would hold, read by the same rules as a ledger session."""
    route = str(receipt.get("delegated_route") or receipt.get("requested_route") or "unknown")
    shaped = {"ts": row.get("ts"), "kind": "subscription_session", "provider": route,
              "subscription_route": route, "model": row.get("model"),
              "prompt_tokens": row.get("prompt_tokens"), "cached_tokens": row.get("cached_tokens"),
              "cache_write_tokens": row.get("cache_write_tokens"),
              "record_contract_version": row.get("record_contract_version")}
    if "input_token_usage" in row:
        shaped["input_token_usage"] = row["input_token_usage"]
    record = _normalize_record(shaped)
    if record is not None:
        # ``normalized``: the event carries the core's input-token split (an object, nulls
        # included), the only evidence that outranks legacy counters; see ``_evidence_rank``.
        record.update(identity=_session_identity(run_id), token_facts=True,
                      normalized=isinstance(row.get("input_token_usage"), Mapping))
    return record


def _evidence_rank(session: Mapping[str, Any]) -> int:
    """Evidence of one run: normalized split (2) > legacy usage event (1) > ledger event, tokenless (0)."""
    return 2 if session.get("normalized") else 1 if session.get("token_facts") else 0


def _session_from_ledger_event(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A session the core recorded without a usage event: route and time, tokens unknown."""
    stamp, run_id = _parse_timestamp(row), row.get("run_id")
    if stamp is None or not run_id:
        return None
    record = _measurement("subscription_session", str(row.get("route") or "unknown"), NO_USAGE_EVENT_MODEL,
                          None, None, None, stamped=True)
    record.update(ts=stamp, identity=_session_identity(str(run_id)), token_facts=False)
    return record


def _boot_from_event(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    stamp, sha = _parse_timestamp(row), str(row.get("git_sha") or "").strip()
    if stamp is None or not sha:
        return None
    return {"ts": stamp, "sha": sha, "branch": str(row.get("git_branch") or ""),
            "identity": f"boot:{row.get('ts')}:{row.get('pid')}:{sha}"}


def parse_event_line(raw: bytes, stats: Dict[str, int]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """One complete event-log line: ``(kind, record)`` for a usage, session or boot fact, else None.

    ``stats`` receives the same counters for every reader (recent window and history).
    A code start without a commit is not a mark and not a malformed event."""
    if not raw.strip():
        return None
    stats["total_lines_read"] += 1
    if not any(marker in raw for marker in _EVENT_MARKERS):
        stats["non_usage_lines"] += 1
        return None
    try:
        row = json.loads(raw)
    except Exception:
        stats["malformed_lines"] += 1
        return None
    kind = row.get("type") if isinstance(row, dict) else None
    if kind == USAGE_EVENT_TYPE:
        record = _normalize_usage_event(row)
    elif kind == SESSION_LEDGER_EVENT_TYPE:
        record = _session_from_ledger_event(row)
    elif kind == BOOT_EVENT_TYPE:
        if _parse_timestamp(row) is not None and not str(row.get("git_sha") or "").strip():
            stats["boots_without_code"] += 1
            return None
        record = _boot_from_event(row)
    else:
        stats["non_usage_lines"] += 1
        return None
    if record is None:
        stats["unparsed_events"] += 1
        return None
    if kind == BOOT_EVENT_TYPE:
        return "boot", record
    return ("session" if record["kind"] == "subscription_session" else "request"), record


class UsageEventsCache:
    """Thread-safe incremental reader of the live ``logs/events.jsonl`` (usage, session and boot events).

    The live file is read incrementally (identity, offset, torn trailing line). The core
    rotates it by renaming it to ``archive/events_<stamp>.jsonl``, which the history index
    owns: a GET never reads archives. Requests, sessions and code starts already read stay
    until the committed index holds them (``forget``), whatever their age, so a rotation
    never opens a gap or counts a request twice; only while the index cannot take them
    (``retain_rotated`` false: degraded, disclosed) do rotated ones older than seven days
    leave. Requests are keyed by call identity. Sessions are keyed
    by run and kept by evidence rank (``_evidence_rank``): a later event replaces the kept
    one only with strictly higher evidence, never the reverse and never by merging fields;
    equal rank keeps the first event.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.file_identity: Optional[Tuple[int, int]] = None
        self.last_size: int = 0
        self.records: List[Dict[str, Any]] = []
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.boots: List[Dict[str, Any]] = []
        self.live_identities: set = set()
        self.seen: set = set()
        self.raw_stats: Dict[str, int] = dict(_EMPTY_EVENT_STATS)

    def clear(self) -> None:
        with self._lock:
            self.file_identity = None
            self.last_size = 0
            self.records = []
            self.sessions = {}
            self.boots = []
            self.live_identities = set()
            self.seen = set()
            self.raw_stats = dict(_EMPTY_EVENT_STATS)

    def boot_snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.boots)

    def update(self, live_path: Path, *, now: Optional[datetime] = None,
               retain_rotated: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        with self._lock:
            now = now or datetime.now(timezone.utc)
            horizon = now - timedelta(days=USAGE_ARCHIVE_WINDOW_DAYS)
            meta: Dict[str, Any] = {"exists": live_path.is_file(), "events_path": str(live_path),
                                    "archive_window_days": USAGE_ARCHIVE_WINDOW_DAYS, "read_error": None}
            if meta["exists"]:
                self._ingest_live(live_path, meta)
            else:
                meta["read_error"] = "file_not_found"
            if not retain_rotated:
                self._prune(horizon)
            meta["raw_stats"] = dict(self.raw_stats)
            return list(self.records) + list(self.sessions.values()), meta

    def forget(self, records: Iterable[Dict[str, Any]] = (), sessions: Iterable[Dict[str, Any]] = (),
               boots: Iterable[Dict[str, Any]] = ()) -> None:
        """Requests, sessions and code starts of an earlier (rotated) live file that the committed history index now
        holds: the same request, a session of at least the same evidence, the same code start."""
        with self._lock:
            gone = {id(item) for item in [*records, *sessions, *boots] if item["identity"] not in self.live_identities}
            if gone:
                self.records = [r for r in self.records if id(r) not in gone]
                self.sessions = {key: s for key, s in self.sessions.items() if id(s) not in gone}
                self.boots = [b for b in self.boots if id(b) not in gone]
                self.seen = {r["identity"] for r in [*self.records, *self.boots]}

    def _ingest_live(self, live_path: Path, meta: Dict[str, Any]) -> None:
        try:
            stat = live_path.stat()
        except OSError as exc:
            meta["read_error"] = f"stat_error:{type(exc).__name__}"
            return
        current_ident = (stat.st_ino, stat.st_dev)
        meta["file_size_bytes"] = stat.st_size
        # A replaced (rotated) or truncated live file starts over at offset 0;
        # records already read stay, the identity set absorbs any replay.
        if self.file_identity != current_ident or stat.st_size < self.last_size:
            self.file_identity = current_ident
            self.last_size = 0
            self.live_identities = set()
        if stat.st_size == self.last_size:
            return
        try:
            with live_path.open("rb") as handle:
                if self.last_size:
                    handle.seek(self.last_size)
                new_bytes = handle.read()
        except OSError as exc:
            meta["read_error"] = f"read_error:{type(exc).__name__}"
            return
        cut = new_bytes.rfind(b"\n")
        if cut < 0:
            return  # Incomplete partial line: wait for its newline.
        complete = new_bytes[: cut + 1]
        self._ingest_bytes(complete)
        self.last_size += len(complete)

    def _ingest_bytes(self, data: bytes) -> None:
        for raw in data.splitlines():
            parsed = parse_event_line(raw, self.raw_stats)
            if parsed is None:
                continue
            kind, record = parsed
            self.live_identities.add(record["identity"])
            if kind == "boot":
                self._add_boot(record)
            elif kind == "session":
                self._add_session(record, from_usage=record.get("token_facts", True))
            elif record["identity"] in self.seen:
                self.raw_stats["duplicate_events_skipped"] += 1
            else:
                self.seen.add(record["identity"])
                self.records.append(record)
                self.raw_stats["usage_events"] += 1

    def _add_session(self, record: Dict[str, Any], *, from_usage: bool) -> None:
        current = self.sessions.get(record["identity"])
        if current is not None and _evidence_rank(record) <= _evidence_rank(current):
            self.raw_stats["duplicate_events_skipped"] += 1
            return
        self.sessions[record["identity"]] = record
        self.raw_stats["session_usage_events" if from_usage else "session_ledger_events"] += 1

    def _add_boot(self, boot: Dict[str, Any]) -> None:
        if boot["identity"] in self.seen:
            self.raw_stats["duplicate_events_skipped"] += 1
            return
        self.seen.add(boot["identity"])
        self.boots.append(boot)
        self.raw_stats["worker_boots"] += 1

    def _prune(self, horizon: datetime) -> None:
        if any(r["ts"] < horizon for r in [*self.records, *self.boots, *self.sessions.values()]):
            self.records = [r for r in self.records if r["ts"] >= horizon or r["identity"] in self.live_identities]
            preceding = max((b for b in self.boots if b["ts"] < horizon), key=lambda b: b["ts"], default=None)
            self.boots = [b for b in self.boots if b is preceding or b["ts"] >= horizon or b["identity"] in self.live_identities]
            self.sessions = {key: s for key, s in self.sessions.items() if s["ts"] >= horizon or key in self.live_identities}
            self.seen = {r["identity"] for r in [*self.records, *self.boots]}


_EVENTS = UsageEventsCache()


# Load the sibling without depending on a process-wide sys.path or plugin package name.
_history_spec = importlib.util.spec_from_file_location(__name__ + "_history", Path(__file__).with_name("history.py"))
_history_module = importlib.util.module_from_spec(_history_spec)
_history_spec.loader.exec_module(_history_module)


def _clean_model_display_name(model_name: str) -> str:
    """Short label only; exact provider/model/kind remain the grouping identity."""
    return str(model_name or "unknown").rsplit("/", 1)[-1].rsplit("::", 1)[-1]


def _parse_timestamp(row: Mapping[str, Any]) -> Optional[datetime]:
    for key in ("ts", "settled_at", "finished_at", "updated_at", "timestamp"):
        val = row.get(key)
        if isinstance(val, (int, float)):
            try:
                return datetime.fromtimestamp(val, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                continue
        if isinstance(val, str) and val.strip():
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def _token_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _consistent(prompt: Optional[int], cached: Optional[int],
                write: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    """Counts the input total cannot hold are unknown, never clipped or guessed: reads above the input, or a write
    that with the known reads exceeds it."""
    if prompt is not None and cached is not None and cached > prompt:
        cached = None
    if prompt is not None and write is not None and write + (cached or 0) > prompt:
        write = None
    return cached, write


def classify_counts(prompt: Optional[int], cached: Optional[int], write: Optional[int],
                    stamped: bool) -> Tuple[Optional[int], Optional[int], Optional[int], int]:
    """Reported counts as the dashboard reads them, with the legacy-zero bits.

    The host record contract (``record_contract.LEGACY_RULE``): a zero counter in a row
    not stamped ``record_contract_version: 2`` may be an old unknown-to-zero projection,
    so it is unknown, never a measured zero, and is disclosed per counter."""
    legacy = 0
    if not stamped:
        if prompt == 0:
            prompt, legacy = None, legacy | LEGACY_TOTAL
        if cached == 0:
            cached, legacy = None, legacy | LEGACY_READ
        if write == 0:
            write, legacy = None, legacy | LEGACY_WRITE
    cached, write = _consistent(prompt, cached, write)
    return prompt, cached, write, legacy


def _measurement(kind: str, provider: str, model: str, prompt: Optional[int], cached: Optional[int],
                 write: Optional[int], *, stamped: bool) -> Dict[str, Any]:
    prompt, cached, write, legacy = classify_counts(prompt, cached, write, stamped)
    measured = prompt is not None and cached is not None
    return {
        "kind": kind, "provider": provider, "model": model,
        "display_name": _clean_model_display_name(model),
        "prompt_tokens": prompt, "cached_tokens": cached, "cache_write_tokens": write,
        "measured": measured, "eligible": measured and prompt > 0,
        "uncached_tokens": prompt - cached if measured else None,
        "legacy_zeros": legacy,
    }


def _normalize_record(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    stamp = _parse_timestamp(row)
    if stamp is None:
        return None
    kind = str(row.get("kind") or "attempt")  # the core ledger reader's default
    provider = str(row.get("provider") or "unknown")
    model = str(row.get("model") or row.get("resolved_model") or "unknown")

    # The additive object is authoritative, including its unknown fields; its
    # nullable split names an unknown as null, so its zeros are reported zeros.
    if "input_token_usage" in row:
        usage = row["input_token_usage"]
        usage = usage if isinstance(usage, Mapping) else {}
        counts = (_token_count(usage.get("total_tokens")), _token_count(usage.get("cache_read_tokens")),
                  _token_count(usage.get("cache_write_tokens")))
        stamped = True
    elif kind == "attempt" or (
        kind == "subscription_session" and provider == "codex"
        and row.get("subscription_route") == "codex"
    ):
        # Physical attempts are already canonical. Codex sessions (a ledger row or
        # a run summary) report inclusive input and cache reads; other harnesses'
        # session counters do not form an inclusive total, so they stay unknown.
        usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
        counts = (_token_count(row.get("prompt_tokens", usage.get("prompt_tokens"))),
                  _token_count(row.get("cached_tokens", usage.get("cached_tokens"))),
                  _token_count(row.get("cache_write_tokens", usage.get("cache_write_tokens"))))
        stamped = _stamped(row)
    else:
        counts, stamped = (None, None, None), True

    record = _measurement(kind, provider, model, *counts, stamped=stamped)
    record["ts"] = stamp
    if row.get("session_id_sha256"):
        record.update(identity=f"session:{row['session_id_sha256']}", token_facts=True,
                      normalized=isinstance(row.get("input_token_usage"), Mapping))
    return record


# ---------------------------------------------------------------------------
# Arithmetic: one additive vector per record; every figure shown is a sum of them
# ---------------------------------------------------------------------------

VECTOR_FIELDS = ("records", "requests", "sessions", "others", "total_sum", "total_known", "read_sum",
                 "read_known", "write_sum", "write_known", "rate_prompt", "rate_read", "rate_records",
                 "written", "unread", "write_records", "measured", "zero_volume", "legacy_total",
                 "legacy_read", "legacy_write")
VECTOR_SIZE = len(VECTOR_FIELDS)


def zero_vector() -> List[int]:
    return [0] * VECTOR_SIZE


def add_vector(total: List[int], vector: Iterable[int]) -> None:
    total[:] = map(operator.add, total, vector)


def record_vector(record: Mapping[str, Any]) -> List[int]:
    """One record's additive counters, read from its normalized fields.

    A write counts as known when the input can hold it (or the input is unknown); a
    measured record with such a write splits its unread input into writes and uncached
    input. An index may carry a precomputed ``_vector`` for records it owns."""
    cached_vector = record.get("_vector")
    if cached_vector is not None:
        return list(cached_vector)
    prompt, cached, write = record["prompt_tokens"], record["cached_tokens"], record.get("cache_write_tokens")
    kind, measured, eligible = record["kind"], record["measured"], record["eligible"]
    fits = write is not None and (prompt is None or write + (cached or 0) <= prompt)
    counted = measured and write is not None and cached + write <= prompt
    legacy = record.get("legacy_zeros") or 0
    return [1, int(kind == "attempt"), int(kind == "subscription_session"),
            int(kind not in ("attempt", "subscription_session")),
            prompt or 0, int(prompt is not None), cached or 0, int(cached is not None),
            write if fits else 0, int(fits),
            prompt if eligible else 0, cached if eligible else 0, int(bool(eligible)),
            write if counted else 0, prompt - cached if counted else 0, int(bool(counted)),
            int(bool(measured)), int(bool(measured) and prompt == 0),
            legacy & LEGACY_TOTAL and 1, legacy & LEGACY_READ and 1, legacy & LEGACY_WRITE and 1]


def finish_vector(vector: List[int]) -> Dict[str, Any]:
    """The displayed figures of a summed vector: headlines, route rows and time buckets alike."""
    (count, requests, sessions, others, total_sum, total_known, read_sum, read_known, write_sum, write_known,
     rate_prompt, rate_read, rate_records, written, unread, write_records, measured, zero_volume,
     legacy_total, legacy_read, legacy_write) = vector
    return {
        "total_records": count, "request_count": requests, "session_count": sessions, "other_count": others,
        "prompt_tokens": total_sum if total_known else None,
        "cached_tokens": read_sum if read_known else None,
        "cache_write_tokens": write_sum if write_known else None,  # every known write, as known reads count every read
        "eligible_prompt_tokens": rate_prompt,
        "eligible_cached_tokens": rate_read,
        "uncached_tokens": rate_prompt - rate_read,
        # A measured record that reports a write count its input can hold splits its unread input into cache writes
        # and uncached input. Without such a count that input is unclassified, as input with unknown reads is: no
        # write is guessed and nothing unknown is called uncached.
        "eligible_write_tokens": written if write_records else None,
        "uncached_input_tokens": unread - written if write_records else None,
        "unclassified_tokens": total_sum - rate_read - unread,
        "write_records": write_records,
        "unknown_write_records": count - write_known,  # the records known cache writes do not cover
        "unknown_prompt_tokens": total_sum - rate_prompt,
        "cache_read_rate": round(rate_read / rate_prompt * 100, 2) if rate_prompt else None,
        "measured_records": measured,
        "rate_records": rate_records,
        "unknown_total_records": count - total_known,
        "unknown_read_records": count - read_known,
        "zero_volume_records": zero_volume,
        "legacy_zero_total_records": legacy_total,
        "legacy_zero_read_records": legacy_read,
        "legacy_zero_write_records": legacy_write,
    }


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One arithmetic owner for headlines, route rows and time buckets."""
    total = zero_vector()
    for record in records:
        add_vector(total, record_vector(record))
    return finish_vector(total)


def _get_timeframe_bounds(tf: str, now: datetime) -> Tuple[datetime, timedelta]:
    if tf == "1H":
        return now - timedelta(hours=1), timedelta(minutes=5)
    if tf == "6H":
        return now - timedelta(hours=6), timedelta(minutes=30)
    if tf == "7D":
        return now - timedelta(days=7), timedelta(hours=6)
    return now - timedelta(hours=24), timedelta(hours=1)


def _version_markers(boots: List[Dict[str, Any]], start: datetime, now: datetime,
                     intervals: List[Tuple[datetime, datetime]]) -> Tuple[List[Dict[str, Any]], int]:
    """Code changes in the requested window, positioned in the actual returned bins.

    The earliest retained boot has no predecessor, so it marks nothing. Omitted
    changes include both off-chart bins and the marker limit, each counted once."""
    markers, previous, changes = [], None, 0
    begins = [begin for begin, _ in intervals]
    for boot in sorted(boots, key=lambda item: item["ts"]):
        if previous is not None and boot["sha"] != previous and start <= boot["ts"] < now:
            changes += 1
            index = bisect_right(begins, boot["ts"]) - 1
            if index >= 0:
                begin, end = intervals[index]
                position = index + (boot["ts"] - begin) / (end - begin)
                # Rounding must not put the last in-window mark beyond the final bin.
                markers.append({"ts": boot["ts"].isoformat(), "code": boot["sha"][:9],
                                "branch": boot["branch"], "position": min(round(position, 4), len(intervals) - 0.0001)})
        previous = boot["sha"]
    markers = markers[-MAX_MARKERS_RETURNED:]
    return markers, changes - len(markers)


def _custom_bound(value: Any, *, end: bool = False, zone: str = "UTC") -> Optional[datetime]:
    """Parse a Custom date in the owner's display zone, or an explicit ISO instant."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        local_zone = ZoneInfo(zone)
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            utc = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        else:
            local = datetime.fromisoformat(text).replace(tzinfo=local_zone)
            if end:
                local += timedelta(days=1)
            utc = local.astimezone(timezone.utc)
        utc.astimezone(local_zone)  # both the instant and its display date must be representable
        return utc
    except (TypeError, ValueError, OverflowError, KeyError, OSError):
        return None


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_us(stamp: datetime) -> int:
    """Microseconds since the epoch, exact (the history index's time unit)."""
    delta = stamp - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_us(value: int) -> datetime:
    return EPOCH + timedelta(microseconds=value)


def _local_midnight(date, zone: ZoneInfo) -> datetime:
    return datetime.combine(date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)


def _interval_count(start: datetime, end: datetime, step: timedelta, daily: bool, zone: ZoneInfo) -> int:
    if end <= start:
        return 0
    if daily:
        return (end.astimezone(zone).date() - start.astimezone(zone).date()).days + 1
    return math.ceil((end - start) / step)


def _history_part(history: Any, start: datetime, end: datetime, intervals: List[Tuple[datetime, datetime]],
                  overrides: frozenset) -> Dict[str, Any]:
    """The index's share of one reading (request cells and session winners), kept on the immutable view.

    Polls between rotations reuse it. An end after every indexed record (requests and sessions alike) does not
    change the share; the identities a stronger recent session overrides are part of the key."""
    end_us = to_us(end)
    newest = history.newest_us()
    key = (to_us(start), None if newest is not None and end_us > newest else end_us,
           tuple(to_us(begin) for begin, _ in intervals), overrides)
    part = history.memo.get(key)
    if part is None:
        part = {"total": zero_vector(), "buckets": [zero_vector() for _ in intervals], "groups": {},
                "bounds": [None, None], "without_usage": 0}
        part["count"] = _add_history(history, start, end, intervals, part["total"], part["buckets"],
                                     part["groups"], part["bounds"])
        begins = [begin for begin, _ in intervals]
        for record in history.sessions_between(start, end, overrides):
            vector = record_vector(record)
            add_vector(part["total"], vector)
            index = bisect_right(begins, record["ts"]) - 1
            if index >= 0:
                add_vector(part["buckets"][index], vector)
            group_key = (record["provider"], record["model"], record["kind"])
            group = part["groups"].get(group_key)
            if group is None:
                part["groups"][group_key] = group = zero_vector()
            add_vector(group, vector)
            stamp_us = to_us(record["ts"])
            low, high = part["bounds"]
            part["bounds"] = [stamp_us if low is None else min(low, stamp_us), stamp_us if high is None else max(high, stamp_us)]
            part["without_usage"] += not record.get("token_facts", True)
            part["count"] += 1
        history.remember(key, part)
    return part


def _add_history(history: Any, start: datetime, end: datetime, intervals: List[Tuple[datetime, datetime]],
                 total: List[int], buckets: List[List[int]], groups: Dict[Tuple[str, str, str], List[int]],
                 bounds: List[Optional[int]]) -> int:
    """Index cells, each wholly inside the window and one interval, else counted row by row.

    A cell's records decide (its min/max time), not its 15-minute span, so an edge between
    two of its records refines only that cell from the index's own rows."""
    start_us, end_us = to_us(start), to_us(end)
    begins = [to_us(begin) for begin, _ in intervals]
    keys, count = history.groups, 0

    def place(stamp_us: int, gid: int, vector: List[int], low: int, high: int) -> None:
        add_vector(total, vector)
        group = groups.get(keys[gid])
        if group is None:
            groups[keys[gid]] = group = zero_vector()
        add_vector(group, vector)
        index = bisect_right(begins, stamp_us) - 1
        if index >= 0:
            add_vector(buckets[index], vector)
        bounds[0] = low if bounds[0] is None else min(bounds[0], low)
        bounds[1] = high if bounds[1] is None else max(bounds[1], high)

    for cell_start, gid, vector, low, high in history.cells(start_us, end_us):
        if low >= start_us and high < end_us and bisect_right(begins, low) == bisect_right(begins, high):
            place(low, gid, vector, low, high)
            count += vector[0]
            continue
        for stamp_us, row_vector in history.rows(cell_start, gid):
            if start_us <= stamp_us < end_us:
                place(stamp_us, gid, row_vector, stamp_us, stamp_us)
                count += 1
    return count


def calculate_analytics(
    records: List[Dict[str, Any]], timeframe: str = "24H", meta: Optional[Dict[str, Any]] = None,
    *, now: Optional[datetime] = None, boots: Optional[List[Dict[str, Any]]] = None,
    start_override: Optional[datetime] = None, end_override: Optional[datetime] = None,
    calendar_daily: bool = False, zone: str = "UTC", hourly: bool = False, history: Any = None,
    history_overrides: frozenset = frozenset(),
) -> Dict[str, Any]:
    """All consumers receive exact half-open intervals, never regrouped coarse bins.

    ``records`` are exact normalized records; ``history`` (optional) adds the history index's
    request cells and its session winners except ``history_overrides``, none of which
    ``records`` duplicates."""
    tf = str(timeframe or "24H").upper().strip()
    if tf not in ("1H", "6H", "24H", "7D", "MONTH", "ALL", "CUSTOM"):
        tf = "24H"
    now = now or datetime.now(timezone.utc)
    end = min(end_override or now, now)
    start, step = _get_timeframe_bounds(tf, end)

    def first_observation() -> Optional[datetime]:
        first = min((r["ts"] for r in records if r["ts"] < end), default=None)
        oldest = history.oldest_us() if history is not None else None
        if oldest is not None and oldest < to_us(end):
            first = min(first, from_us(oldest)) if first is not None else from_us(oldest)
        return first

    if tf == "CUSTOM":
        start = start_override or end - timedelta(hours=24)
        span = max(0.0, (end - start).total_seconds())
        step = timedelta(minutes=5 if span <= 3600 else 30 if span <= 21600 else 60 if span <= 86400 else 360)
        if hourly:
            step = timedelta(hours=1)
    elif tf == "MONTH":
        start, step = end - timedelta(days=30), timedelta(days=1)
    elif tf == "ALL":
        start = first_observation() or end
        span = (end - start).total_seconds()
        step = timedelta(hours=1 if span <= 86400 else 6 if span <= 7 * 86400 else 12 if span <= 30 * 86400 else 24)
    local_zone = ZoneInfo(zone)
    daily = calendar_daily or tf == "MONTH"
    # Explicit ISO bounds stay exact. Calendar bin edges may be clipped at either end.
    if daily and tf in ("7D", "MONTH", "ALL"):
        date = start.astimezone(local_zone).date() if tf == "ALL" else end.astimezone(local_zone).date() - timedelta(days=6 if tf == "7D" else 29)
        start = _local_midnight(date, local_zone)
    # A requested range far older than any observation is charted from the first one;
    # nothing is built beyond a fixed number of intervals.
    limit = MAX_DAILY_INTERVALS if daily else MAX_INTRADAY_INTERVALS
    clamped_from = None
    if _interval_count(start, end, step, daily, local_zone) > limit:
        first = first_observation()
        if first is not None and first > start:
            clamped_from = start
            start = _local_midnight(first.astimezone(local_zone).date(), local_zone) if daily else max(start, first)
        if _interval_count(start, end, step, daily, local_zone) > limit:
            return {"status": "degraded", "read_error": "custom_range_too_long", "timeframe": tf, "timezone": zone}
    intervals = []
    cursor = start
    while cursor < end:
        if daily:
            boundary = _local_midnight(cursor.astimezone(local_zone).date() + timedelta(days=1), local_zone)
        else:
            boundary = cursor + step
        stop = min(boundary, end)
        intervals.append((cursor, stop))
        cursor = stop
    # Preserve all daily observations; the widget pages the plot, never silently drops history.
    omitted = 0 if daily else max(0, len(intervals) - MAX_BUCKETS_RETURNED)
    intervals = intervals[omitted:]
    begins = [begin for begin, _ in intervals]
    total = zero_vector()
    bucket_vectors = [zero_vector() for _ in intervals]
    groups: Dict[Tuple[str, str, str], List[int]] = {}
    bounds: List[Optional[int]] = [None, None]
    window_records = sessions_without_usage = 0
    for record in records:
        stamp = record["ts"]
        if not (start <= stamp < end):
            continue
        vector = record_vector(record)
        add_vector(total, vector)
        window_records += 1
        if record["kind"] == "subscription_session" and not record.get("token_facts", True):
            sessions_without_usage += 1
        index = bisect_right(begins, stamp) - 1
        if index >= 0 and stamp < intervals[index][1]:
            add_vector(bucket_vectors[index], vector)
        key = (record["provider"], record["model"], record["kind"])
        group = groups.get(key)
        if group is None:
            groups[key] = group = zero_vector()
        add_vector(group, vector)
        stamp_us = to_us(stamp)
        bounds[0] = stamp_us if bounds[0] is None else min(bounds[0], stamp_us)
        bounds[1] = stamp_us if bounds[1] is None else max(bounds[1], stamp_us)
    if history is not None and end > start:
        part = _history_part(history, start, end, intervals, history_overrides)
        window_records += part["count"]
        sessions_without_usage += part["without_usage"]
        add_vector(total, part["total"])
        for vector, share in zip(bucket_vectors, part["buckets"]):
            add_vector(vector, share)
        for key, share in part["groups"].items():
            group = groups.get(key)
            if group is None:
                groups[key] = group = zero_vector()
            add_vector(group, share)
        for index, pick in ((0, min), (1, max)):
            if part["bounds"][index] is not None:
                bounds[index] = part["bounds"][index] if bounds[index] is None else pick(bounds[index], part["bounds"][index])
    summary = finish_vector(total)
    markers, markers_omitted = _version_markers(boots or [], start, end, intervals)
    buckets = []
    for (begin, stop), vector in zip(intervals, bucket_vectors):
        aggregate = finish_vector(vector)
        local = begin.astimezone(local_zone)
        buckets.append({**aggregate, "ts": begin.isoformat(), "end_ts": stop.isoformat(),
                        "day": local.date().isoformat(),
                        "label": local.strftime("%Y-%m-%d" if daily else "%Y-%m-%d %H:%M" if tf in ("ALL", "CUSTOM") else "%m-%d %H:%M"),
                        "partial": daily and (local.time() != datetime.min.time() or stop.astimezone(local_zone).time() != datetime.min.time()),
                        "rate": aggregate["cache_read_rate"], "prompt": aggregate["prompt_tokens"],
                        "cached": aggregate["eligible_cached_tokens"], "uncached": aggregate["uncached_tokens"],
                        "unknown": aggregate["unknown_prompt_tokens"]})
    models = [{**finish_vector(vector), "provider": provider, "model": model, "kind": kind,
               "display_name": _clean_model_display_name(model)}
              for (provider, model, kind), vector in groups.items()]
    for model in models:
        model["rate"] = model["cache_read_rate"]
    models.sort(key=lambda item: item["prompt_tokens"] or 0, reverse=True)
    quality = dict(meta or {})
    quality.update({"settled_records_total": len(records) + (history.total_records() + len(history.sessions) - len(history_overrides) if history is not None else 0),
                    "window_records": window_records,
                    "oldest_ts": from_us(bounds[0]).isoformat() if bounds[0] is not None else None,
                    "newest_ts": from_us(bounds[1]).isoformat() if bounds[1] is not None else None,
                    "models_omitted": max(0, len(models) - MAX_MODELS_RETURNED),
                    "buckets_omitted": omitted, "markers_omitted": markers_omitted,
                    "sessions_without_usage_event": sessions_without_usage,
                    "coverage": "Available event log observations; empty intervals mean no recorded usage, not proof of zero consumption. Harness sessions are attributed to their recorded observation time, not spread over their duration."
                                + (" A live JSONL ledger adds its session tokens." if quality.get("ledger_live") else "")})
    if clamped_from is not None:
        quality["range_clamped_from"] = clamped_from.isoformat()
    history_meta = quality.get("history_index") or {}
    error = quality.get("read_error") or quality.get("ledger_read_error") or history_meta.get("read_error")
    if history_meta.get("state") in ("degraded", "unavailable"):
        error = error or "history_unavailable"
    status = "degraded" if error else "loading" if quality.get("history_pending") else "ok"
    return {"status": status, "read_error": error, "timeframe": tf, "timezone": zone,
            "start": start.isoformat(), "end": end.isoformat(), "daily": daily,
            "generated_at": now.isoformat(), "summary": summary, "buckets": buckets,
            "models": models[:MAX_MODELS_RETURNED], "quality": quality, "markers": markers}


def _merge_ledger_sessions(records: List[Dict[str, Any]], ledger_rows: List[Dict[str, Any]],
                           horizon: datetime) -> List[Dict[str, Any]]:
    """Every session source by evidence rank: a ledger row replaces the run's kept session only with strictly
    stronger evidence (a normalized split's nulls are never filled), and equal evidence keeps the event."""
    winners: Dict[str, Dict[str, Any]] = {r["identity"]: r for r in records if r["kind"] == "subscription_session"}
    for row in ledger_rows:
        identity = row.get("identity")
        if row["ts"] < horizon or row["kind"] != "subscription_session" or not identity:
            continue
        current = winners.get(identity)
        if current is None or _evidence_rank(row) > _evidence_rank(current):
            winners[identity] = row
    return [r for r in records if r["kind"] != "subscription_session"] + list(winners.values())


def _ledger_is_live(ledger_path: Path, horizon: datetime) -> bool:
    """Cores with the usage store stopped writing the JSONL ledger; a file changed inside the window is live."""
    try:
        return datetime.fromtimestamp(ledger_path.stat().st_mtime, tz=timezone.utc) >= horizon
    except OSError:
        return False


def _session_winners(history_sessions: Mapping[str, Dict[str, Any]],
                     current: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Global winners: the index's sessions, replaced by a recent session only with strictly stronger evidence."""
    winners: Dict[str, Dict[str, Any]] = {}
    for record in current:
        if record["kind"] != "subscription_session":
            continue
        identity = record["identity"]
        previous = winners.get(identity) or history_sessions.get(identity)
        if previous is None or _evidence_rank(record) > _evidence_rank(previous):
            winners[identity] = record
    return winners


HistoryIndex = _history_module.HistoryIndex
_HISTORY: Optional[Any] = None
_HISTORY_KEY: Optional[Tuple[Optional[str], Optional[str]]] = None


def _runtime_paths(api: Any) -> Tuple[Optional[Path], Optional[Path]]:
    """The host's data directory (read) and this skill's own state directory (written)."""
    data_dir = state_dir = None
    try:
        info = api.get_runtime_info()
        raw = info.get("data_dir") if isinstance(info, Mapping) else None
        data_dir = Path(raw) if isinstance(raw, str) and raw.strip() else None
    except Exception:
        data_dir = None
    try:
        raw_state = api.get_state_dir() if callable(getattr(api, "get_state_dir", None)) else None
        state_dir = Path(raw_state) if isinstance(raw_state, (str, Path)) and str(raw_state).strip() else None
    except Exception:
        state_dir = None
    return data_dir, state_dir


def make_history_index(directory: Optional[Path], legacy_path: Optional[Path] = None) -> Any:
    """The history index with this module's parser, evidence rank and arithmetic."""
    return HistoryIndex(directory, legacy_path=legacy_path, parse_line=parse_event_line, rank=_evidence_rank,
                        classify=classify_counts, vector=record_vector, fingerprint=identity_fingerprint,
                        empty_stats=_EMPTY_EVENT_STATS)


def _history_for(data_dir: Optional[Path], state_dir: Optional[Path]) -> Any:
    """One index per (data, state) directory pair; without a state directory it is kept in memory only.

    The 0.8.x JSON index under the data directory is a read-only migration source."""
    global _HISTORY, _HISTORY_KEY
    key = (str(data_dir) if data_dir else None, str(state_dir) if state_dir else None)
    if _HISTORY is None or _HISTORY_KEY != key:
        if _HISTORY is not None:
            _HISTORY.close()
        legacy = data_dir / "state" / "cache_efficiency_snapshot" / "history-index.json" if data_dir else None
        _HISTORY_KEY = key
        _HISTORY = make_history_index(state_dir / "history" if state_dir else None, legacy)
    return _HISTORY


def register(api: Any) -> None:
    global _DATA_DIR
    data_dir, state_dir = _runtime_paths(api)
    _DATA_DIR = data_dir
    history = _history_for(data_dir, state_dir)

    def route_handler(request: Any = None) -> Dict[str, Any]:
        query = getattr(request, "query_params", None)
        if query is None:
            query = request.get("query_params", {}) if isinstance(request, Mapping) else {}
        if _DATA_DIR is None:
            return {"status": "degraded", "read_error": "data_dir_unavailable"}
        events_path = _DATA_DIR / "logs" / "events.jsonl"
        archive_dir = _DATA_DIR / "archive"
        ledger_path = _DATA_DIR / "state" / "usage_attempts.jsonl"
        timeframe = str(query.get("timeframe", "24H")).upper().strip()
        zone = str(query.get("timezone", "UTC"))
        try:
            local_zone = ZoneInfo(zone)
        except (KeyError, ValueError, OSError):
            return {"status": "degraded", "read_error": "invalid_timezone"}
        custom_from = _custom_bound(query.get("from"), zone=zone)
        custom_to = _custom_bound(query.get("to"), end=True, zone=zone)
        daily = str(query.get("daily", "0")).lower() in ("1", "true", "yes")
        now = datetime.now(timezone.utc)
        if timeframe == "CUSTOM" and (not custom_from or not custom_to or custom_from >= min(custom_to, now)):
            return {"status": "degraded", "read_error": "invalid_custom_range"}
        # Rotated observations stay with the live reader until the committed index holds them, whatever their age;
        # a failed archive write cannot discard the only remaining observation either. A rotation therefore
        # never drops a reading's figures while its archive is indexed or retried.
        records, meta = _EVENTS.update(events_path, now=now, retain_rotated=True)
        horizon = now - timedelta(days=USAGE_ARCHIVE_WINDOW_DAYS)
        meta["ledger_path"] = str(ledger_path)
        meta["ledger_live"] = _ledger_is_live(ledger_path, horizon)
        if meta["ledger_live"]:
            sessions, ledger_meta = _CACHE.update(ledger_path)
            records = _merge_ledger_sessions(records, sessions, horizon)
            meta["ledger_read_error"] = ledger_meta.get("read_error")
            meta["ledger_raw_stats"] = ledger_meta.get("raw_stats")
        # Rotated archives belong to the history index; it starts with the first reading of any range and then
        # checks for rotation at most every thirty seconds. A GET never reads or decodes an archive. Only the
        # first pass after loading may hold a reading, briefly (a cold start sees its history at once).
        history.request(archive_dir)
        history.wait_ready(FIRST_PASS_WAIT_SECONDS)
        if timeframe == "MONTH":
            selected_start = _local_midnight(now.astimezone(local_zone).date() - timedelta(days=29), local_zone)
        elif timeframe == "ALL":
            selected_start = None
        elif timeframe == "CUSTOM":
            selected_start = custom_from
        elif timeframe == "7D" and daily:
            selected_start = _local_midnight(now.astimezone(local_zone).date() - timedelta(days=6), local_zone)
        else:
            selected_start = _get_timeframe_bounds(timeframe, now)[0]
        selected_end = min(custom_to or now, now) if timeframe == "CUSTOM" else now

        def reading(view: Any, history_meta: Dict[str, Any]) -> Dict[str, Any]:
            """One reading of one published view; its database reads (edge rows, identity checks) are that view's."""
            meta["history_index"] = history_meta
            winners = _session_winners(view.sessions if view is not None else {}, records)
            requests = [r for r in records if r["kind"] != "subscription_session"]
            boots = _EVENTS.boot_snapshot()
            if view is not None:
                # Recent requests join exactly when the index does not count them yet (fingerprint, then full
                # digest). What of a rotated live file the committed index now holds leaves the recent reader:
                # the same request, a session of at least the same evidence, the same code start.
                held = [r for r in requests if view.holds(r, identity_fingerprint(r["identity"]))]
                indexed = view.sessions
                _EVENTS.forget(held, [s for s in records if s["kind"] == "subscription_session" and s["identity"] in indexed
                                      and _evidence_rank(indexed[s["identity"]]) >= _evidence_rank(s)],
                               [b for b in boots if view.holds_boot(b)])
                if held:
                    held_ids = {id(r) for r in held}
                    requests = [r for r in requests if id(r) not in held_ids]
                boots = list({b["identity"]: b for b in [*view.boots_between(selected_start, selected_end), *boots]}.values())
            sessions = list(winners.values())  # global winners; calculate_analytics applies the selected window
            # Older days may still be added only before the first complete pass, and only to a reading that reaches
            # back past the newest archive not indexed yet. A rotation after that never turns a reading into loading.
            complete_after = history_meta.get("complete_after_us")
            meta["history_pending"] = view is None or (not history_meta.get("complete") and complete_after is not None and (
                selected_start is None or to_us(selected_start) <= complete_after))
            return calculate_analytics([*requests, *sessions], timeframe=timeframe, meta=meta, now=now, boots=boots,
                                       start_override=custom_from, end_override=custom_to,
                                       calendar_daily=daily, zone=zone,
                                       hourly=str(query.get("granularity", "")) == "hour", history=view,
                                       history_overrides=frozenset(winners).intersection(view.sessions) if view is not None else frozenset())

        # The index's cells, its rows at an edge and its identity checks belong to one committed state: a reading
        # binds to the published view's commit, and if the index has committed since, it reads the newer view.
        deadline = time.monotonic() + STALE_WAIT_SECONDS
        for _ in range(8):
            history_meta = history.meta()  # before the view: a newer view never meets older completeness facts
            view = history.view()
            try:
                if view is None:
                    return reading(None, history_meta)
                with history.bind(view) as bound:
                    return reading(bound, history_meta)
            except _history_module.StaleView as stale:
                if not history.wait_published(stale.commits, min(0.25, max(0.0, deadline - time.monotonic()))):
                    history.note_stale()  # committed but not published (another writer): a pass loads it now
                    history.request(archive_dir)
                    if not history.wait_published(stale.commits, max(0.0, deadline - time.monotonic())):
                        break
            except sqlite3.Error as exc:
                # The index's rows could not be read (a removed or unreadable file): the recent reading, disclosed.
                return reading(None, dict(history_meta, state="degraded", read_error="history_read_error:" + type(exc).__name__))
        # No consistent reading in time (a reload of the whole index is under way): nothing partial is drawn.
        meta.update(history_index=history.meta(), history_pending=True)
        return {"status": "loading", "read_error": None, "timeframe": timeframe, "timezone": zone, "quality": meta}

    api.register_route("data", route_handler, methods=("GET",))
    api.register_ui_tab(
        "cache_efficiency",
        "Cache Efficiency Snapshot",
        icon="activity",
        render={"kind": "module", "entry": "widget.js", "appearance": "host", "span": 2},
    )
    if hasattr(api, "on_unload") and callable(api.on_unload):
        try:
            def cleanup() -> None:
                global _HISTORY, _HISTORY_KEY
                history.close()
                _EVENTS.clear()
                _CACHE.clear()
                if _HISTORY is history:
                    _HISTORY, _HISTORY_KEY = None, None
            api.on_unload(cleanup)
        except Exception:
            pass
