"""Incremental archive index; immutable query snapshots and no raw prompt storage.

The extension passes its existing event parser and evidence-rank function. Disk
and decoding work run in one owned thread, never in the widget's GET handler.
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import tempfile


class ArchiveIndex:
    VERSION = 1

    def __init__(self, path, parser_factory, rank):
        self.path = Path(path)
        self._parser_factory, self._rank = parser_factory, rank
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._loaded = False
        self._entries = {}
        self._dirty = False
        self._records, self._stamps, self._boots, self._boot_stamps = (), (), (), ()
        self._sessions = {}
        self._meta = {"state": "not_loaded", "archives_indexed": 0, "archives_done": 0,
                      "archives_total": 0, "read_error": None, "revision": 0}
        self._last_check = 0.0
        self._directory_stamp = None
        self._retry = False

    def close(self):
        with self._lock:
            self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def clear(self):
        # A stopped instance is never reused. Its thread may still be unwinding a
        # filesystem read; do not clear containers under that reader.
        self.close()

    def request(self, archive_dir, start, end, *, current=()):
        """Start/continue a refresh and return immediately with a dated snapshot."""
        with self._lock:
            if not self._stop.is_set() and (self._thread is None or not self._thread.is_alive()):
                if not self._loaded or time.monotonic() - self._last_check >= 30:
                    self._meta = {**self._meta, "state": "loading", "read_error": None}
                    self._thread = threading.Thread(target=self._refresh, args=(Path(archive_dir),),
                                                    name="cache-history-index", daemon=True)
                    self._thread.start()
            return self._select(start, end, current)

    def update(self, archive_dir, start, end):
        """Synchronous test/batch entry; the HTTP consumer uses request()."""
        self._refresh(Path(archive_dir))
        with self._lock:
            return self._select(start, end)

    def available(self, start, end, *, current=()):
        """Use the same already-published winners for short ranges and day detail."""
        with self._lock:
            if not self._meta["revision"]:
                return None
            return self._select(start, end, current)

    def _select(self, start, end, current=()):
        left = bisect_left(self._stamps, start) if start is not None else 0
        right = bisect_left(self._stamps, end)
        first_boot = bisect_left(self._boot_stamps, start) if start is not None else 0
        last_boot = bisect_left(self._boot_stamps, end)
        # The last preceding boot establishes whether the first in-range boot changed code.
        boots = self._boots[max(0, first_boot - 1):last_boot]
        # Compare the full live overlay with global session winners BEFORE slicing.
        # A stronger out-of-range row must also suppress a weaker in-range row.
        overrides = {}
        for record in current:
            if record.get("kind") == "subscription_session":
                identity = record["identity"]
                previous = overrides.get(identity, self._sessions.get(identity))
                if previous is None or self._rank(record) > self._rank(previous):
                    overrides[identity] = record
        selected = {r["identity"]: r for r in self._records[left:right]
                    if r["identity"] not in overrides}
        for record in (*overrides.values(), *(r for r in current if r.get("kind") != "subscription_session")):
            if (start is None or start <= record["ts"]) and record["ts"] < end:
                selected.setdefault(record["identity"], record)
        return list(selected.values()), list(boots), dict(self._meta)

    @staticmethod
    def _encode(record):
        result = dict(record)
        result["ts"] = record["ts"].isoformat()
        return result

    @staticmethod
    def _decode(record):
        result = dict(record)
        result["ts"] = datetime.fromisoformat(record["ts"]).astimezone(timezone.utc)
        if result.get("kind") == "attempt" and str(result.get("identity", "")).startswith("["):
            import hashlib
            result["identity"] = "request:" + hashlib.sha256(result["identity"].encode()).hexdigest()
        return result

    def _load(self):
        if self._loaded:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != self.VERSION:
                raise ValueError("unsupported history index")
            entries = data["archives"]
            if not isinstance(entries, dict):
                raise ValueError("invalid history archives")
            for entry in entries.values():
                if not isinstance(entry, dict) or not isinstance(entry.get("identity"), list):
                    raise ValueError("invalid history entry")
                for name in ("records", "sessions", "boots"):
                    if not isinstance(entry.get(name), list):
                        raise ValueError("invalid history records")
                    for record in entry[name]:
                        parsed = self._decode(record)
                        if not parsed.get("identity"):
                            raise ValueError("missing history identity")
                        if name != "boots" and not all(k in parsed for k in (
                                "prompt_tokens", "cached_tokens", "kind", "eligible", "measured")):
                            raise ValueError("invalid normalized measurement")
            self._entries = entries
        except FileNotFoundError:
            self._entries = {}
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            # Preserve the bad index for inspection; never overwrite its only copy.
            if self.path.exists():
                backup = self.path.with_name(self.path.name + ".unreadable-" + str(time.time_ns()))
                self.path.rename(backup)
            self._entries = {}
            self._meta["recovery"] = "Rebuilding unreadable index: " + type(exc).__name__
        self._loaded = True
        return True

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A cancelled worker can still be unwinding I/O after unload. It must
        # never share an open inode with a replacement worker's publication.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": self.VERSION, "archives": self._entries}, stream,
                          ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                if not self._stop.is_set():
                    os.replace(temporary, self.path)
                    self._dirty = False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _build_snapshot(self):
        records, sessions, boots = {}, {}, {}
        for key in sorted(self._entries):
            entry = self._entries[key]
            for raw in entry["records"]:
                record = self._decode(raw)
                records.setdefault(record["identity"], record)
            for raw in entry["sessions"]:
                record = self._decode(raw)
                current = sessions.get(record["identity"])
                if current is None or self._rank(record) > self._rank(current):
                    sessions[record["identity"]] = record
            for raw in entry["boots"]:
                record = self._decode(raw)
                boots.setdefault(record["identity"], record)
        ordered = tuple(sorted((*records.values(), *sessions.values()), key=lambda r: r["ts"]))
        ordered_boots = tuple(sorted(boots.values(), key=lambda r: r["ts"]))
        return ordered, tuple(r["ts"] for r in ordered), ordered_boots, tuple(r["ts"] for r in ordered_boots), sessions

    def _refresh(self, archive_dir):
        started = time.monotonic()
        changed, read_count, done, total = False, 0, 0, 0
        error = None
        try:
            loaded = self._load()
            # Rotation is rename + creation in this directory. Archives are immutable.
            try:
                directory_stamp = archive_dir.stat().st_mtime_ns
            except FileNotFoundError:
                directory_stamp = None
            if loaded or self._retry or directory_stamp != self._directory_stamp:
                candidates = sorted(archive_dir.glob("events_*.jsonl"))
                total = len(candidates)
                with self._lock:
                    self._meta.update(archives_done=0, archives_total=total,
                                      archive_directory="present" if directory_stamp is not None else "absent",
                                      retained_sources_absent=len(set(self._entries) - {str(p) for p in candidates}))
                for path in candidates:
                    if self._stop.is_set():
                        return
                    try:
                        stat = path.stat()
                        identity = [stat.st_size, stat.st_mtime_ns]
                        old = self._entries.get(str(path))
                        if not old or old["identity"] != identity:
                            parser = self._parser_factory()
                            # Process one line at a time; a large archive is not one allocation.
                            with path.open("rb") as stream:
                                for line in stream:
                                    if self._stop.is_set():
                                        return
                                    if not line.endswith(b"\n"):
                                        raise ValueError("incomplete archive line")
                                    parser._ingest_bytes(line)
                            after = path.stat()
                            if [after.st_size, after.st_mtime_ns] != identity:
                                raise ValueError("archive changed while reading")
                            self._entries[str(path)] = {
                                "identity": identity,
                                "records": [self._encode(r) for r in parser.records],
                                "sessions": [self._encode(r) for r in parser.sessions.values()],
                                "boots": [self._encode(r) for r in parser.boots],
                                "malformed": parser.raw_stats["malformed_lines"] + parser.raw_stats["unparsed_events"],
                            }
                            changed = True
                            read_count += 1
                    except (OSError, ValueError) as exc:
                        error = "archive_read_error:" + type(exc).__name__
                    done += 1
                    with self._lock:
                        self._meta.update(archives_done=done)
                self._directory_stamp = directory_stamp
            if changed:
                self._dirty = True
            snapshot = self._build_snapshot() if loaded or changed or self._dirty else None
            if self._dirty and not self._stop.is_set():
                self._save()
            malformed = sum(entry.get("malformed", 0) for entry in self._entries.values())
            if malformed:
                error = error or "malformed_usage_events"
            if self._stop.is_set():
                return
            with self._lock:
                if snapshot is not None:
                    self._records, self._stamps, self._boots, self._boot_stamps, self._sessions = snapshot
                    self._meta["revision"] += 1
                self._meta.update(
                    state="degraded" if error else "ready", read_error=error,
                    archives_indexed=len(self._entries), archives_read_this_call=read_count,
                    malformed_usage_events=malformed,
                    indexed_oldest_ts=self._stamps[0].isoformat() if self._stamps else None,
                    indexed_newest_ts=self._stamps[-1].isoformat() if self._stamps else None,
                    backfill_seconds=round(time.monotonic() - started, 3),
                    checked_at=datetime.now(timezone.utc).isoformat(),
                )
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            with self._lock:
                self._meta.update(state="degraded", read_error="history_index_error:" + type(exc).__name__)
            error = str(exc)
        finally:
            with self._lock:
                self._retry = bool(error or self._dirty)
                self._last_check = time.monotonic()
