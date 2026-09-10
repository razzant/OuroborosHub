"""Bounded, read-only backend for Memory Atlas.

Only paths discovered by :class:`MemoryReader` are accepted. The module has no
framework dependency and performs no filesystem writes.
"""
from __future__ import annotations

import base64
import bisect
import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

MAX_FILE = 4 * 1024 * 1024
MAX_LINE = 64 * 1024
MAX_SOURCES = 2000
MAX_DISCOVERY_ENTRIES = 2000
MAX_HISTORY = 10_000
MAX_RESPONSE = 256 * 1024
MAX_SEARCH_FILES = 200
MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_GRAPH_FILES = 60
MAX_GRAPH_EDGES = 300
# The cumulative read budget of one request: every file this reader opens is
# charged against it, whether it is opened to build a revision or to build the
# response body. Both quantities are bounds on work actually done, not on bytes
# retained: a budget that capped only retention left revision construction free
# to open and hash the whole corpus (2,000 sources of up to MAX_FILE each) while
# still advertising an 8 MiB scan.
MAX_READ_FILES = MAX_SOURCES
MAX_READ_BYTES = MAX_SEARCH_BYTES
# Deepest container nesting this reader will walk in a parsed record. Real
# memory records are a handful of levels deep; a value nested past this is
# reported as a malformed record rather than walked, which keeps the walk (and
# the serializer that would follow it) off the interpreter's recursion limit.
MAX_JSON_DEPTH = 64
# A cursor this reader issues is far shorter than this; the bound exists so a
# hostile token is rejected before any base64 or JSON work is done on it.
MAX_CURSOR_CHARS = 4096
# Deepest search page this reader will serve. Beyond it the scan cost of
# skipping earlier matches stops being bounded work, so the cap is stated
# rather than hidden behind a cursor that would fail on use.
MAX_SEARCH_OFFSET = 10_000
DIALOGUE_CONTENT_BYTES = 8 * 1024
DIALOGUE_BLOCK_TYPES = ("summary", "era", "gap")
# The consolidator writes a discontinuity as type "summary" carrying a gap_id;
# this literal is the writer's secondary marker for the same fact.
DIALOGUE_GAP_MARKER = "[MEMORY GAP]"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SECRET_RE = re.compile(
    r"(^\.env(?:\.|$)|credential|secret|token|private[_-]?key|"
    r"\.(?:pem|key|p12|pfx)$|auth|cookie|session)",
    re.I,
)
MD_REL_LINK_RE = re.compile(
    r"\[(?:[^\]\\]|\\.)*\]\(\s*<?([^>\s)]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
# The one knowledge topic written by MERGE with digest-only history: no pair of
# retained full versions ever exists for it, so a version diff cannot be offered.
NO_COMPARE_IDS = frozenset(("knowledge:improvement-backlog",))


class AtlasError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@dataclass(frozen=True)
class Source:
    id: str
    family: str
    title: str
    path: Path
    history: str
    history_paths: tuple[Path, ...] = ()


@dataclass
class ScanBudget:
    remaining: int = MAX_DISCOVERY_ENTRIES

    def take(self, count: int = 1) -> bool:
        if self.remaining < count:
            self.remaining = 0
            return False
        self.remaining -= count
        return True


def _gap(scope: str, reason: str, count: int = 1) -> dict[str, Any]:
    return {"scope": scope, "reason": reason, "count": count}


class ReadBudgetExhausted(AtlasError):
    """One request tried to read past its cumulative file/byte budget.

    This is a typed refusal, not a silent truncation: a route that cannot build
    a complete byte-bound revision within the budget says so instead of serving
    a digest computed over part of the corpus, and instead of falling back to
    stat metadata, which cannot carry a content claim at all.
    """

    def __init__(self) -> None:
        super().__init__(413, "read_budget_exhausted",
                         "request read budget exhausted")


class _ReadPhase:
    """One in-flight request's cumulative read budget and the bytes it holds.

    ``files``/``bytes`` count every file this request has opened, for a revision
    or for a response body alike, and ``_read_all`` refuses the read that would
    cross ``max_files``/``max_bytes``. Because the charge is taken at the open
    and not at the cache, the budget bounds hashing volume, which is the work
    the routes' advertised limits are about. Everything charged is also
    retained, so a later read of the same file is a cache hit that costs
    nothing further, and the retained bytes stay bounded by ``max_bytes``.

    ``selection`` is the second half of the bound and the one that ties the
    served revision to the response: a route calls ``select`` once, fixing the
    exact set of files the request may open, and ``_read_all`` then refuses
    every path outside it. Leftover budget no longer decides anything, so a
    file skipped for size cannot be followed by a smaller one that slips into
    the answer without being in the revision the answer is served under.
    ``None`` means the phase has selected nothing and is unrestricted; that is
    only the closing drift check, which reads exactly the entries it is handed.
    """

    __slots__ = ("cache", "files", "bytes", "max_files", "max_bytes", "selection")

    def __init__(self, max_files: int | None = None,
                 max_bytes: int | None = None) -> None:
        self.cache: dict[str, bytes | AtlasError] = {}
        self.files = 0
        self.bytes = 0
        self.selection: frozenset[str] | None = None
        # Resolved per phase rather than bound as a default, so the module
        # constants are the single source of the bound.
        self.max_files = MAX_READ_FILES if max_files is None else max_files
        self.max_bytes = MAX_READ_BYTES if max_bytes is None else max_bytes

    def reserve(self, path: Path) -> int:
        """Charge one open against the budget, using the file's on-disk size."""
        try:
            size = path.lstat().st_size
        except OSError:
            size = 0
        if self.files >= self.max_files or self.bytes + size > self.max_bytes:
            raise ReadBudgetExhausted()
        self.files += 1
        self.bytes += size
        return size

    def settle(self, reserved: int, actual: int) -> None:
        """Correct the charge to the bytes the read actually produced."""
        self.bytes += actual - reserved
        if self.bytes > self.max_bytes:
            raise ReadBudgetExhausted()

    def select(self, paths: Iterable[Path], *,
               required: Iterable[Path] = ()) -> frozenset[str]:
        """Fix, once, the exact set of files this request is allowed to read.

        Sizes come from ``lstat``, so a route chooses its corpus *before*
        hashing it and never opens more than it is allowed to. A path that
        cannot be stat'd costs nothing and is still selected: absence is
        recorded by attempting the read, not by excluding the entry.

        The result is a *set*, not a leading run. Optional ``paths`` are taken
        in order and stop at the first one that does not fit — so which files a
        route covers is unchanged — but membership is what ``_read_all``
        enforces afterwards, which is what makes a later, smaller file
        structurally unreachable instead of merely unlikely to fit.

        ``required`` entries are charged first and are not optional: a required
        file that does not fit the budget raises ``ReadBudgetExhausted``, so a
        route whose answer is meaningless without a particular file (the focus
        of ``graph``, the document and stores of ``history``) returns the typed
        bounded outcome rather than an answer built over part of its corpus.
        """
        selected: set[str] = set()
        files, used = self.files, self.bytes

        def take(path: Path) -> bool:
            nonlocal files, used
            key = str(path)
            if key in selected:
                return True
            try:
                size = path.lstat().st_size
            except OSError:
                selected.add(key)
                return True
            if files >= self.max_files or used + size > self.max_bytes:
                return False
            files, used = files + 1, used + size
            selected.add(key)
            return True

        for path in required:
            if not take(path):
                raise ReadBudgetExhausted()
        for path in paths:
            if not take(path):
                break
        self.selection = frozenset(selected)
        return self.selection


# The open phase belongs to the request, not to the reader: one reader serves
# every request and ``plugin.py`` dispatches each of them onto its own worker
# thread, so instance state would let concurrent requests share (and outlive)
# one cache. ``asyncio.to_thread`` copies the calling context per call, so a
# value set inside the worker is private to that request and disappears with
# it. ``None`` means "no request phase is open"; see ``_bound_reads``.
_READ_PHASE: contextvars.ContextVar[_ReadPhase | None] = contextvars.ContextVar(
    "memory_atlas_read_phase", default=None)


class MemoryReader:
    def __init__(self, data_dir: str | os.PathLike[str]):
        raw = os.fspath(data_dir) if data_dir is not None else ""
        if not raw or not os.path.isabs(raw):
            raise ValueError("canonical data_dir must be a non-empty absolute path")
        root = Path(raw)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("canonical data_dir must be an existing non-symlink directory")
        self.root = root.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | \
            getattr(os, "O_NOFOLLOW", 0)
        self._root_fd = os.open(self.root, flags)

    def close(self) -> None:
        fd = getattr(self, "_root_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._root_fd = -1

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    # ---- public API -----------------------------------------------------
    def catalog(self, *, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        """List the discovered sources, bound to a revision over their bytes.

        The revision corpus is selected from on-disk sizes *before* it is
        hashed, so the catalogue never hashes more than the request's read
        budget allows; a corpus larger than that budget is reported as a
        ``catalog``/``revision_scan_limit`` gap naming how many sources the
        revision does not cover, rather than passed off as a complete digest.
        That selection is also a membership bound on the items: a source the
        revision does not cover cannot be read for its item either, so it
        carries ``read_error: "read_budget_exhausted"`` and no digest instead
        of a digest for bytes outside the revision the page is served under.
        The same digest is recomputed after the phase, so a source mutated
        while the page was being built is rejected as ``409 revision_drift``
        instead of being served as a mixed page under a stale cursor binding.
        """
        limit = self._limit(limit, 1, 200)
        sources, gaps = self._sources()
        with self._bound_reads() as phase:
            selection = phase.select([s.path for s in sources])
            bound = [s for s in sources if str(s.path) in selection]
            if len(bound) < len(sources):
                gaps.append(_gap("catalog", "revision_scan_limit",
                                 len(sources) - len(bound)))
            revision = self._aggregate_revision(bound)
            offset = self._cursor(cursor, "catalog", revision, "") if cursor else 0
            items = [self._catalog_item(s, gaps)
                     for s in sources[offset:offset + limit]]
        with self._bound_reads():
            observed = self._aggregate_revision(bound)
        if observed != revision:
            raise AtlasError(409, "revision_drift", "catalog corpus changed",
                             revision=observed)
        nxt = self._make_cursor("catalog", revision, "", offset + len(items)) \
            if offset + len(items) < len(sources) else None
        return self._ok({"items": items, "next_cursor": nxt}, gaps)

    def document(self, source_id: str, *, cursor: str | None = None,
                 limit: int = 16_384, revision: str | None = None) -> dict[str, Any]:
        limit = self._limit(limit, 1, 65_536)
        source, gaps = self._source(source_id)
        raw, current = self._read_versioned(source.path, source.id)
        offset = self._paged_offset("document", source_id, current, cursor, revision)
        content, used, complete = self._page_bytes(raw, offset, limit)
        nxt = self._make_cursor("document", current, source_id, offset + used) \
            if not complete else None
        return self._ok({"id": source.id, "revision": current, "offset": offset,
                         "content": content, "content_bytes": used,
                         "complete": complete, "next_cursor": nxt}, gaps)

    def history(self, source_id: str, *, cursor: str | None = None,
                limit: int = 50, revision: str | None = None) -> dict[str, Any]:
        limit = self._limit(limit, 1, 200)
        source, gaps = self._source(source_id)
        if source.history == "unavailable":
            gaps.append(_gap(source.id, "history_unavailable"))
        with self._bound_reads() as phase:
            # No bounded prefix of a document and its stores is meaningful, so
            # every one of them is required: the set is the whole binding, and
            # a corpus that does not fit is the typed 413 rather than a partial
            # timeline under a revision that claims to cover it.
            phase.select((), required=(source.path, *source.history_paths))
            current = self._history_revision(source)
            if revision and revision != current:
                raise AtlasError(409, "revision_drift", "history changed",
                                 revision=current)
            offset = self._cursor(cursor, "history", current, source_id) \
                if cursor else 0
            events, parse_gaps = self._history_events(source)
        gaps.extend(parse_gaps)
        with self._bound_reads():
            observed = self._history_revision(source)
        if observed != current:
            raise AtlasError(409, "revision_drift", "history changed", revision=observed)
        items = [self._event_public(e) for e in events[offset:offset + limit]]
        nxt = self._make_cursor("history", current, source_id, offset + len(items)) \
            if offset + len(items) < len(events) else None
        return self._ok({"id": source.id, "revision": current, "items": items,
                         "next_cursor": nxt}, gaps)

    def history_event(self, source_id: str, event_id: str, *,
                      cursor: str | None = None, limit: int = 16_384,
                      revision: str | None = None) -> dict[str, Any]:
        limit = self._limit(limit, 1, 65_536)
        source, gaps = self._source(source_id)
        with self._bound_reads() as phase:
            phase.select((), required=(source.path, *source.history_paths))
            current = self._history_revision(source)
            offset = self._paged_offset("history_event",
                                        source_id + "\0" + event_id,
                                        current, cursor, revision)
            events, parse_gaps = self._history_events(source)
        gaps.extend(parse_gaps)
        with self._bound_reads():
            observed = self._history_revision(source)
        if observed != current:
            raise AtlasError(409, "revision_drift", "history changed", revision=observed)
        event = next((e for e in events if e["event_id"] == event_id), None)
        if event is None:
            raise AtlasError(404, "event_not_found", "history event not found")
        if event["representation"] != "snapshot" or "_content" not in event:
            raise AtlasError(409, "no_snapshot", "event is not a historic snapshot")
        raw = event["_content"].encode("utf-8")
        text, used, complete = self._page_bytes(raw, offset, limit)
        nxt = self._make_cursor("history_event", current,
                                source_id + "\0" + event_id, offset + used) \
            if not complete else None
        return self._ok({"id": source_id, "event_id": event_id,
                         "representation": "snapshot", "revision": current,
                         "offset": offset, "content": text,
                         "content_bytes": used, "complete": complete,
                         "next_cursor": nxt}, gaps)

    def search(self, query: str, *, cursor: str | None = None, limit: int = 25,
               case_sensitive: bool = False,
               revision: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str) or not 1 <= len(query) <= 128:
            raise AtlasError(400, "invalid_query", "q must contain 1 to 128 characters")
        limit = self._limit(limit, 1, 100)
        sources, gaps = self._sources()
        selected = sources[:MAX_SEARCH_FILES]
        if len(sources) > len(selected):
            gaps.append(_gap("search", "file_scan_limit", len(sources) - len(selected)))
        with self._bound_reads() as phase:
            # The scanned corpus is chosen from on-disk sizes before anything is
            # opened, and the revision then covers exactly the files the scan
            # reads. Selecting after hashing (as this route used to) meant every
            # selected file was opened and hashed regardless of the byte bound,
            # so the advertised 8 MiB scan could cost several times that.
            selection = phase.select([s.path for s in selected])
            covered = [s for s in selected if str(s.path) in selection]
            if len(covered) < len(selected):
                gaps.append(_gap("search", "byte_scan_limit",
                                 len(selected) - len(covered)))
                selected = covered
            current = self._aggregate_revision(selected)
            if revision and revision != current:
                raise AtlasError(409, "revision_drift", "search corpus changed",
                                 revision=current)
            offset = self._cursor(cursor, "search", current,
                                  query + "\0" + str(case_sensitive)) if cursor else 0
            if offset > MAX_SEARCH_OFFSET:
                raise AtlasError(400, "invalid_cursor", "search offset is out of range")
            needle = query if case_sensitive else query.casefold()
            # Earlier pages are counted and skipped, never retained: at most
            # ``limit + 1`` hits (the page plus the one that proves there is a
            # next page) are ever held, so a deep cursor costs no extra memory.
            page: list[dict[str, Any]] = []
            found, stop = 0, offset + limit + 1
            for source in selected:
                # Already read and charged for the revision, so this is a cache
                # hit: the scan and the revision cover one and the same bytes.
                try:
                    raw = self._read_all(source.path)
                except AtlasError as exc:
                    gaps.append(_gap(source.id, exc.code))
                    continue
                try:
                    text = raw.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    gaps.append(_gap(source.id, "invalid_utf8"))
                    continue
                for line_no, line in enumerate(text.splitlines(), 1):
                    hay, index_map = self._fold_with_map(line, case_sensitive)
                    start = 0
                    last_span: tuple[int, int] | None = None
                    while True:
                        pos = hay.find(needle, start)
                        if pos < 0:
                            break
                        folded_end = pos + len(needle)
                        # The match ends at the last original character any folded
                        # position covers. ``index_map[folded_end]`` would be the
                        # next character, and equals ``orig_start`` when the match
                        # stops inside one character's expansion (``ß`` -> ``ss``).
                        orig_start = index_map[pos]
                        orig_end = index_map[folded_end - 1] + 1
                        # A hit that lands inside the same expansion is the same
                        # original span, not a second occurrence.
                        if (orig_start, orig_end) != last_span:
                            last_span = (orig_start, orig_end)
                            if found >= offset:
                                left = max(0, orig_start - 80)
                                right = min(len(line), orig_end + 80)
                                page.append({"id": source.id, "line": line_no,
                                             "column": orig_start + 1,
                                             "excerpt": line[left:right],
                                             "match_start": orig_start - left,
                                             "match_end": orig_end - left})
                            found += 1
                        # Resume past the whole expansion of the last matched
                        # character so an expanded character cannot re-match.
                        start = max(pos + 1, self._folded_index_at(index_map, orig_end))
                        if found >= stop:
                            break
                    if found >= stop:
                        break
                if found >= stop:
                    break
        has_more = len(page) > limit
        del page[limit:]
        with self._bound_reads():
            observed = self._aggregate_revision(selected)
        if observed != current:
            raise AtlasError(409, "revision_drift", "search corpus changed",
                             revision=observed)
        nxt = None
        if has_more:
            if offset + len(page) > MAX_SEARCH_OFFSET:
                # A cursor past the offset bound would be rejected on use, so
                # report the truncation instead of handing out a dead token.
                gaps.append(_gap("search", "page_limit_reached"))
            else:
                nxt = self._make_cursor("search", current,
                                        query + "\0" + str(case_sensitive),
                                        offset + len(page))
        return self._ok({"query": query, "revision": current, "items": page,
                         "next_cursor": nxt}, gaps)

    def dialogue(self, *, cursor: str | None = None, limit: int = 10,
                 revision: str | None = None) -> dict[str, Any]:
        """Page the consolidated dialogue chronicle.

        ``memory/dialogue_blocks.json`` holds the consolidator's output, not raw
        chat: each block summarizes roughly 100 chat entries. Three block types
        are authentic and they do not mean the same thing:

        * ``summary`` — one consolidated block of that stretch of conversation.
        * ``era`` — LOSSY. Once more than MAX_SUMMARY_BLOCKS blocks exist, the
          ERA_COMPRESS_COUNT oldest blocks are folded into a single era block.
          The individual summaries it replaced no longer exist anywhere; an era
          block is a compression of them, not a container for them.
        * ``gap`` — a durable marker for a stretch that was never consolidated.
          Gap blocks are never bridged or absorbed by an era; they stay. The
          writer does not label these with ``type: "gap"``: it writes ``type:
          "summary"`` plus a ``gap_id`` and a ``[MEMORY GAP]`` content marker,
          so ``gap_id`` is the discriminator this reader uses and the ``type``
          field is only consulted afterwards.

        A record with a missing or unrecognised type is reported as ``unknown``,
        which is an authentic observation about the file, not a malformed record.
        The raw ``logs/chat.jsonl`` this is derived from is the consolidator's
        input and stays outside this skill entirely.
        """
        limit = self._limit(limit, 1, 50)
        gaps: list[dict[str, Any]] = []
        blocks_path = self.root / "memory" / "dialogue_blocks.json"
        meta_path = self.root / "memory" / "dialogue_meta.json"
        if not self._safe_file(blocks_path):
            raise AtlasError(404, "source_not_found", "dialogue chronicle not found")
        binding = (("dialogue_blocks.json", blocks_path),
                   ("dialogue_meta.json", meta_path))
        with self._bound_reads() as phase:
            phase.select((), required=(blocks_path, meta_path))
            current = self._revision(binding)
            if revision and revision != current:
                raise AtlasError(409, "revision_drift", "dialogue chronicle changed",
                                 revision=current)
            offset = self._cursor(cursor, "dialogue", current, "") if cursor else 0
            try:
                records = self._json_array(blocks_path, gaps)
            except AtlasError as exc:
                gaps.append(_gap("dialogue_blocks.json", exc.code))
                records = []
            blocks = []
            for index, record in enumerate(records):
                if not self._unicode_scalar_safe(record):
                    gaps.append(_gap("dialogue_blocks.json", "malformed_record"))
                    continue
                block = self._dialogue_block(index, record, gaps)
                if block is not None:
                    blocks.append(block)
            meta = self._dialogue_meta(meta_path)
        with self._bound_reads():
            observed = self._revision(binding)
        if observed != current:
            raise AtlasError(409, "revision_drift", "dialogue chronicle changed",
                             revision=observed)
        page = blocks[offset:offset + limit]
        nxt = self._make_cursor("dialogue", current, "", offset + len(page)) \
            if offset + len(page) < len(blocks) else None
        return self._ok({"revision": current, "blocks": page,
                         "next_cursor": nxt, "meta": meta}, gaps)

    def _dialogue_block(self, index: int, record: Any,
                        gaps: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            gaps.append(_gap("dialogue_blocks.json", "malformed_record"))
            return None
        content = record.get("content")
        if not isinstance(content, str):
            gaps.append(_gap("dialogue_blocks.json", "malformed_record"))
            return None
        ts = self._scalar(record.get("ts"))
        block_type = self._dialogue_block_type(record, content)
        identity = f"dialogue_blocks:{index}:{ts}:{block_type}"
        block_id = hashlib.sha256(identity.encode()).hexdigest()[:20]
        raw = content.encode("utf-8")
        truncated = len(raw) > DIALOGUE_CONTENT_BYTES
        end = len(raw)
        if truncated:
            # Trim back to a character boundary so the cap is never exceeded and
            # the text returned is never a broken sequence.
            end = DIALOGUE_CONTENT_BYTES
            while end > 0 and raw[end] & 0xC0 == 0x80:
                end -= 1
        page = raw[:end]
        return {"block_id": block_id, "ts": ts, "type": block_type,
                "range": self._json_safe(record.get("range")),
                "message_count": self._json_safe(record.get("message_count")),
                "gap_id": self._json_safe(record.get("gap_id")),
                "content": page.decode("utf-8", "strict"),
                "content_bytes": len(page), "truncated": truncated}

    @staticmethod
    def _dialogue_block_type(record: dict[str, Any], content: str) -> str:
        """Resolve the block kind the way the writer actually marks it.

        The consolidator does not write ``type: "gap"``. A durable discontinuity
        is written as ``{"type": "summary", "gap_id": "gap:...", "content":
        "[MEMORY GAP] ..."}``, and the host's own predicates are ``bool(block
        .get("gap_id"))`` (consolidator) and ``gap_id`` present or the
        ``[MEMORY GAP]`` content marker (memory). Classifying on the ``type``
        field alone would therefore label every real gap a summary — the exact
        opposite of what the block records. The ``gap_id`` discriminator is
        checked first, the content marker second, and only then does the
        record's own ``type`` field decide between ``era`` and ``summary``.
        """
        gap_id = record.get("gap_id")
        if isinstance(gap_id, (str, int, float)) and not isinstance(gap_id, bool) \
                and str(gap_id).strip():
            return "gap"
        if DIALOGUE_GAP_MARKER in content:
            return "gap"
        raw_type = record.get("type")
        return raw_type if raw_type in DIALOGUE_BLOCK_TYPES else "unknown"

    def _dialogue_meta(self, path: Path) -> dict[str, Any]:
        """Read ``memory/dialogue_meta.json`` as provenance only.

        This file is not a readable document and is never catalogued. It records
        how far consolidation got and against which chat log, which is the only
        honest explanation for why the chronicle stops where it stops. Nothing
        here is invented: when the file is absent or cannot be parsed the values
        stay null and ``reason`` names the failure.
        """
        meta: dict[str, Any] = {"available": False, "reason": None,
                                "last_consolidated_offset": None,
                                "chat_log_signature": None,
                                "last_consolidated_at": None}
        if not self._safe_file(path):
            meta["reason"] = "missing"
            return meta
        try:
            raw = self._read_all(path)
        except AtlasError:
            meta["reason"] = "unreadable"
            return meta
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            meta["reason"] = "malformed_json"
            return meta
        if not isinstance(value, dict):
            meta["reason"] = "malformed_json"
            return meta
        if not self._unicode_scalar_safe(value):
            meta["reason"] = "malformed_json"
            return meta
        meta["available"] = True
        for key in ("last_consolidated_offset", "chat_log_signature",
                    "last_consolidated_at"):
            meta[key] = self._json_safe(value.get(key))
        return meta

    def graph(self, focus: str, *, limit: int = 50,
              revision: str | None = None) -> dict[str, Any]:
        """Return only links this reader can prove from recorded bytes.

        Every edge is authored or recorded evidence: a Markdown link, a wiki
        link, a journal read reference, or an identical recorded ``task_id``.
        There is no language model anywhere in this skill and no similarity
        heuristic: a bare identifier or topic word appearing in prose is not a
        link, and a focus document with nothing to point at correctly returns an
        empty edge list rather than manufactured neighbours.
        """
        limit = self._limit(limit, 1, 100)
        sources, gaps = self._sources()
        by_id = {s.id: s for s in sources}
        if focus not in by_id:
            raise AtlasError(404, "source_not_found", "source ID not found")
        # The graph reads history stores as well as documents, so it binds to
        # a revision that covers both; see ``_graph_entries``. The entries are
        # trimmed to what the request's read budget can actually open before any
        # of them is hashed — hashing every discovered source and store first
        # and applying the 60-store/8 MiB scan budget afterwards made those
        # bounds untrue — and an entry the revision does not cover is disclosed.
        # The selection is then the request's read permission as well: a store
        # the revision does not cover is refused by ``_read_all`` and reported
        # as a truncated scan, so no edge can rest on bytes outside the served
        # revision. The focus is required rather than optional — an answer
        # "about" a document that was never read is not a bounded answer but a
        # wrong one — so a focus that does not fit the budget is the typed 413.
        focus_source = by_id[focus]
        with self._bound_reads() as phase:
            entries = self._graph_entries(sources)
            selection = phase.select([path for _, path in entries],
                                     required=(focus_source.path,))
            covered = [e for e in entries if str(e[1]) in selection]
            if len(covered) < len(entries):
                gaps.append(_gap("graph", "revision_scan_limit",
                                 len(entries) - len(covered)))
                entries = covered
            current = self._revision(entries)
            if revision and revision != current:
                raise AtlasError(409, "revision_drift", "graph corpus changed",
                                 revision=current)
            focus_raw = self._read_all(focus_source.path)
            try:
                focus_text = focus_raw.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise AtlasError(409, "invalid_utf8", "focus source is not valid UTF-8") from exc
            by_rel: dict[str, str] = {}
            for source in sources:
                try:
                    by_rel[source.path.relative_to(self.root).as_posix()] = source.id
                except ValueError:
                    continue
            budget = {"files": 0, "bytes": len(focus_raw), "truncated": False}
            cache: dict[str, list[Any]] = {}
            # For reflections, project reflections and project journals the history
            # store IS the focus file. Its bytes are already read and already
            # charged, so seed the cache with them: reading it again would charge
            # the same file twice against the scan budget, contradicting the
            # documented "each graph store is read at most once" and prematurely
            # reporting scan_limit while omitting edges that are actually provable.
            if focus_source.path in focus_source.history_paths:
                budget["files"] += 1
                seed_gaps: list[dict[str, Any]] = []
                name = focus_source.path.name
                cache[str(focus_source.path)] = (
                    self._json_array_raw(name, focus_raw, seed_gaps)
                    if name == "scratchpad_blocks.json"
                    else self._json_lines_raw(name, focus_raw, seed_gaps))
                gaps.extend(seed_gaps)
            candidates = list(self._link_edges(focus_source, focus_text, by_rel,
                                               set(by_id)))
            candidates.extend(self._journal_ref_edges(focus_source, by_rel, cache,
                                                      budget, gaps))
            candidates.extend(self._shared_task_edges(focus_source, sources, cache,
                                                      budget, gaps))
            if budget["truncated"]:
                gaps.append(_gap("graph", "scan_limit"))
            edges, chosen, seen = [], {focus}, set()
            for target, kind, basis, excerpt in candidates:
                if target == focus or target not in by_id:
                    continue
                key = (target, kind, basis)
                if key in seen:
                    continue
                if target not in chosen and len(chosen) >= limit:
                    continue
                seen.add(key)
                chosen.add(target)
                edges.append({"source": focus, "target": target, "kind": kind,
                              "basis": basis, "evidence": {"source_excerpt": excerpt}})
            nodes = [{"id": s.id, "title": s.title, "family": s.family}
                     for s in sources if s.id in chosen]
        with self._bound_reads():
            observed = self._revision(entries)
        if observed != current:
            raise AtlasError(409, "revision_drift", "graph corpus changed",
                             revision=observed)
        omitted = max(0, len(edges) - MAX_GRAPH_EDGES)
        if omitted:
            gaps.append(_gap("graph", "edge_limit", omitted))
        return self._ok({"focus": focus, "revision": current, "nodes": nodes,
                         "edges": edges[:MAX_GRAPH_EDGES]}, gaps)

    # ---- discovery and safe reads --------------------------------------
    def _sources(self) -> tuple[list[Source], list[dict[str, Any]]]:
        out: list[Source] = []
        gaps: list[dict[str, Any]] = []
        budget = ScanBudget()
        mem = self.root / "memory"
        self._add(out, "identity", "identity", "Identity", mem / "identity.md",
                  "mixed", (mem / "identity_journal.jsonl",))
        self._add(out, "scratchpad", "scratchpad", "Scratchpad",
                  mem / "scratchpad.md", "mixed",
                  (mem / "scratchpad_blocks.json", mem / "scratchpad_journal.jsonl"))
        # Consolidated dialogue memory. ``dialogue_blocks.json`` holds summary
        # blocks written by the consolidator; the raw ``logs/chat.jsonl`` it
        # reads from stays excluded. ``dialogue_meta.json`` is deliberately not
        # catalogued: it is provenance about where consolidation stopped, not a
        # readable document, and it is served through the ``dialogue`` route.
        self._add(out, "dialogue", "dialogue", "Dialogue chronicle",
                  mem / "dialogue_blocks.json", "none")
        # Legacy: a reader exists for this file but nothing writes it any more.
        self._add(out, "dialogue_legacy", "dialogue_legacy",
                  "Dialogue summary (legacy)", mem / "dialogue_summary.md", "none")
        # Generated environment profile, not authored knowledge; no history store.
        self._add(out, "world", "world", "World profile", mem / "WORLD.md", "none")
        self._add(out, "registry", "registry", "Memory source registry",
                  mem / "registry.md", "none")
        # Overwritten in place by each review, so no evolution timeline exists.
        self._add(out, "deep_review", "deep_review", "Latest self-review",
                  mem / "deep_review.md", "none")
        # Recorded execution history of completed tasks.
        reflections = self.root / "logs" / "task_reflections.jsonl"
        self._add(out, "reflections", "reflections", "Task reflections",
                  reflections, "activity", (reflections,))
        kdir = mem / "knowledge"
        for path in self._md_files(kdir, gaps, budget):
            if self._skip_derived_index(path, gaps):
                continue
            slug = path.stem
            histories = [mem / "knowledge_history.jsonl",
                         mem / "knowledge_journal.jsonl"]
            if slug == "patterns":
                histories.append(kdir / "patterns_history.jsonl")
            self._add(out, "knowledge:" + slug, "knowledge", slug, path, "mixed",
                      tuple(histories))
        pdir = self.root / "projects"
        for project_dir in self._dirs(pdir, gaps, budget):
            project = project_dir.name
            for path in self._md_files(project_dir / "knowledge", gaps, budget):
                if self._skip_derived_index(path, gaps):
                    continue
                slug = path.stem
                self._add(out, f"project:{project}:knowledge:{slug}",
                          "project_knowledge", f"{project} / {slug}", path, "mixed",
                          (project_dir / "knowledge_history.jsonl",
                           project_dir / "knowledge_journal.jsonl"))
            self._add(out, f"project:{project}:workpad", "project_workpad",
                      f"{project} / workpad", project_dir / "workpad.md", "none")
            self._add(out, f"project:{project}:journal", "project_journal",
                      f"{project} / journal", project_dir / "journal.jsonl", "activity",
                      (project_dir / "journal.jsonl",))
            project_reflections = project_dir / "logs" / "task_reflections.jsonl"
            self._add(out, f"project:{project}:reflections", "project_reflections",
                      f"{project} / task reflections", project_reflections, "activity",
                      (project_reflections,))
        out.sort(key=lambda s: s.id)
        if len(out) > MAX_SOURCES:
            gaps.append(_gap("catalog", "source_limit", len(out) - MAX_SOURCES))
            out = out[:MAX_SOURCES]
        return out, gaps

    def _add(self, out: list[Source], sid: str, family: str, title: str,
             path: Path, history: str, histories: tuple[Path, ...] = ()) -> None:
        if self._safe_file(path):
            safe_histories = tuple(p for p in histories if self._safe_file(p))
            actual = history if safe_histories else ("unavailable" if histories else "none")
            out.append(Source(sid, family, title, path, actual, safe_histories))

    def _skip_derived_index(self, path: Path, gaps: list[dict[str, Any]]) -> bool:
        if path.name != "index-full.md":
            return False
        try:
            scope = path.relative_to(self.root).as_posix()
        except ValueError:
            scope = path.name
        gaps.append(_gap(scope, "derived_excluded"))
        return True

    def _dirs(self, path: Path, gaps: list[dict[str, Any]],
              budget: ScanBudget) -> Iterable[Path]:
        if not self._safe_dir(path):
            return ()
        try:
            entries = []
            with os.scandir(path) as scan:
                for entry in scan:
                    if not budget.take():
                        gaps.append(_gap("discovery", "entry_scan_limit"))
                        break
                    entries.append(entry)
            entries.sort(key=lambda e: e.name)
        except OSError:
            gaps.append(_gap(str(path.relative_to(self.root)), "unreadable"))
            return ()
        accepted = []
        for entry in entries:
            if (NAME_RE.fullmatch(entry.name) and not SECRET_RE.search(entry.name)
                    and not entry.is_symlink() and entry.is_dir(follow_symlinks=False)):
                accepted.append(Path(entry.path))
            else:
                gaps.append(_gap(str(path.relative_to(self.root)), "entry_excluded"))
        return accepted

    def _md_files(self, path: Path, gaps: list[dict[str, Any]],
                  budget: ScanBudget) -> Iterable[Path]:
        if not self._safe_dir(path):
            return ()
        try:
            entries = []
            with os.scandir(path) as scan:
                for entry in scan:
                    if not budget.take():
                        gaps.append(_gap("discovery", "entry_scan_limit"))
                        break
                    entries.append(entry)
            entries.sort(key=lambda e: e.name)
        except OSError:
            gaps.append(_gap(str(path.relative_to(self.root)), "unreadable"))
            return ()
        accepted = []
        for entry in entries:
            if (entry.name.endswith(".md") and NAME_RE.fullmatch(entry.name[:-3])
                    and not SECRET_RE.search(entry.name) and not entry.is_symlink()
                    and entry.is_file(follow_symlinks=False)):
                accepted.append(Path(entry.path))
            else:
                gaps.append(_gap(str(path.relative_to(self.root)), "entry_excluded"))
        return accepted

    def _safe_dir(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self.root)
            current = self.root
            for part in rel.parts:
                if SECRET_RE.search(part):
                    return False
                current = current / part
                st = current.lstat()
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    return False
            return True
        except (OSError, ValueError):
            return False

    def _safe_file(self, path: Path) -> bool:
        if SECRET_RE.search(path.name):
            return False
        try:
            if not self._safe_dir(path.parent):
                return False
            st = path.lstat()
            return stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode)
        except OSError:
            return False

    @contextlib.contextmanager
    def _bound_reads(self) -> Iterator[_ReadPhase]:
        """Read each bound file once, within one cumulative request budget.

        A byte-bound revision reads exactly the files the response body reads,
        so without this the same store would be opened for the revision and
        again for the content. Inside the phase the bytes are read once and
        reused, and every open — revision or body — is charged against the one
        MAX_READ_FILES/MAX_READ_BYTES budget the phase carries, so a route
        cannot hash more than it advertises that it reads.

        Each route opens the phase and immediately calls ``select`` to fix the
        set of files it may read; ``_read_all`` refuses everything outside it.
        That is the single choke point for the rule the revisions depend on —
        every file whose bytes can influence a response is a member of the set
        the response's revision is computed over — so it holds for a route's
        body reads as much as for its digest, without per-route checks.

        The closing drift check runs in a *fresh* phase, never in this one: an
        empty cache means it re-reads the files for real, which is what makes
        it a check, while the new budget keeps that second pass bounded too.
        It selects nothing because it reads nothing but the entries it is
        handed, which are exactly the ones the opening phase selected.

        The phase lives in a ``ContextVar``, not on the reader: requests run
        concurrently on worker threads, and their phases neither nest nor exit
        in order, so instance state would leak one request's bytes into
        another's revision and survive past every phase.
        """
        phase = _ReadPhase()
        token = _READ_PHASE.set(phase)
        try:
            yield phase
        finally:
            _READ_PHASE.reset(token)

    def _read_all(self, path: Path) -> bytes:
        phase = _READ_PHASE.get()
        if phase is None:
            return self._read_uncached(path)
        key = str(path)
        if phase.selection is not None and key not in phase.selection:
            # Outside the set this request selected, so it is not served, full
            # stop. Enforcing membership here — one choke point, ahead of the
            # cache and the budget — is what keeps every byte behind a response
            # inside the revision that response is served under: leftover budget
            # after a file was skipped for size can no longer admit a smaller
            # one that the revision does not cover. The caller sees the same
            # typed bounded outcome it would see for an exhausted budget, and
            # each route discloses the omission in its own gap vocabulary.
            raise ReadBudgetExhausted()
        hit = phase.cache.get(key)
        if hit is not None:
            if isinstance(hit, AtlasError):
                raise hit
            return hit
        try:
            reserved = phase.reserve(path)
            raw = self._read_uncached(path)
            phase.settle(reserved, len(raw))
        except AtlasError as exc:
            phase.cache[key] = exc
            raise
        phase.cache[key] = raw
        return raw

    def _read_uncached(self, path: Path) -> bytes:
        fd, before = self._open_readonly(path)
        if before.st_size > MAX_FILE:
            os.close(fd)
            raise AtlasError(413, "file_too_large", "source exceeds 4 MiB bound")
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise AtlasError(409, "revision_drift", "source changed before read")
            chunks, remaining = [], MAX_FILE + 1
            while remaining:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if len(raw) > MAX_FILE:
            raise AtlasError(413, "file_too_large", "source exceeds 4 MiB bound")
        if self._stat_tuple(before) != self._stat_tuple(after):
            raise AtlasError(409, "revision_drift", "source changed during read")
        return raw

    def _read_versioned(self, path: Path, name: str) -> tuple[bytes, str]:
        """Bind a revision to the exact stable bytes returned by the read.

        The bytes are validated as UTF-8 here, so a source that cannot be
        served as text never receives a revision digest: the catalogue reports
        ``revision: null`` with an ``invalid_utf8`` read error instead of
        advertising a digest for content no page can return.
        """
        raw = self._read_all(path)
        try:
            raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise AtlasError(409, "invalid_utf8", "source is not valid UTF-8") from exc
        digest = hashlib.sha256()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        return raw, digest.hexdigest()

    def _open_readonly(self, path: Path) -> tuple[int, os.stat_result]:
        """Open beneath the retained root fd without following any path symlink."""
        try:
            parts = path.relative_to(self.root).parts
        except ValueError as exc:
            raise AtlasError(404, "source_not_found", "source is outside data_dir") from exc
        if not parts or any(not NAME_RE.fullmatch(p.rsplit(".", 1)[0])
                            or SECRET_RE.search(p) for p in parts):
            raise AtlasError(404, "source_not_found", "unsafe source path")
        directory = os.dup(self._root_fd)
        try:
            dflags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | \
                getattr(os, "O_NOFOLLOW", 0)
            for part in parts[:-1]:
                nxt = os.open(part, dflags, dir_fd=directory)
                os.close(directory)
                directory = nxt
            before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise AtlasError(404, "source_not_found", "source is not regular")
            fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                         dir_fd=directory)
            return fd, before
        except AtlasError:
            raise
        except OSError as exc:
            raise AtlasError(409, "revision_drift",
                             "source path changed before read") from exc
        finally:
            os.close(directory)

    @staticmethod
    def _page_bytes(raw: bytes, offset: int, limit: int) -> tuple[str, int, bool]:
        if offset < 0 or offset > len(raw):
            raise AtlasError(400, "invalid_cursor", "page offset is out of range")
        if offset < len(raw) and raw[offset] & 0xC0 == 0x80:
            raise AtlasError(400, "invalid_cursor", "page offset is not a UTF-8 boundary")
        end = min(len(raw), offset + limit)
        if end < len(raw):
            while end < len(raw) and raw[end] & 0xC0 == 0x80:
                end += 1
        page = raw[offset:end]
        try:
            text = page.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise AtlasError(409, "invalid_utf8", "source is not valid UTF-8") from exc
        return text, len(page), end == len(raw)

    # ---- history -------------------------------------------------------
    def _history_events(self, source: Source) -> tuple[list[dict[str, Any]],
                                                        list[dict[str, Any]]]:
        events, gaps = [], []
        for path in source.history_paths:
            try:
                if path.name == "scratchpad_blocks.json":
                    records = self._json_array(path, gaps)
                    store = "scratchpad_blocks"
                else:
                    records = self._json_lines(path, gaps)
                    store = path.name
            except AtlasError as exc:
                gaps.append(_gap(path.name, exc.code))
                continue
            for index, record in enumerate(records):
                if not self._unicode_scalar_safe(record):
                    gaps.append(_gap(path.name, "malformed_record"))
                    continue
                if not self._record_matches(source, store, record):
                    continue
                normalized = self._normalize_events(source, store, index, record)
                if not normalized:
                    gaps.append(_gap(path.name, "malformed_record"))
                else:
                    events.extend(normalized)
                if len(events) >= MAX_HISTORY:
                    gaps.append(_gap("history", "record_limit"))
                    return events, gaps
        return events, gaps

    @staticmethod
    def _unicode_scalar_safe(value: Any) -> bool:
        """Return false when a parsed record cannot survive strict JSON output.

        Two failures are caught here, both of which ``json.loads`` accepts and
        the response serializer does not:

        * a lone UTF-16 surrogate, which is not a Unicode scalar value;
        * a non-finite number. ``json.loads`` accepts the ``NaN``/``Infinity``
          extensions, but Starlette's ``JSONResponse`` serializes with
          ``allow_nan=False``, so letting one through turns an otherwise valid
          request into a generic 500. The record is reported as a
          ``malformed_record`` gap instead, which is the honest observation
          about the file and keeps the rest of the response intact.

        A third failure is bounded rather than caught: containers are walked
        iteratively to an explicit MAX_JSON_DEPTH. Walking a deeply nested
        value recursively raised ``RecursionError``, which no caller expected
        and which escaped as a generic 500 instead of the documented gap.
        """
        stack: list[tuple[Any, int]] = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if isinstance(item, str):
                if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                    return False
            elif isinstance(item, float):
                if not math.isfinite(item):
                    return False
            elif isinstance(item, dict):
                if depth >= MAX_JSON_DEPTH:
                    return False
                for key, sub in item.items():
                    stack.append((key, depth + 1))
                    stack.append((sub, depth + 1))
            elif isinstance(item, (list, tuple)):
                if depth >= MAX_JSON_DEPTH:
                    return False
                for sub in item:
                    stack.append((sub, depth + 1))
        return True

    @staticmethod
    def _record_matches(source: Source, store: str, record: Any) -> bool:
        """Reject shared-store records that explicitly name a different source."""
        if not isinstance(record, dict) or source.family not in (
                "knowledge", "project_knowledge"):
            return True
        slug = source.id.rsplit(":", 1)[-1]
        claimed = record.get("topic", record.get("slug", record.get("name",
                  record.get("knowledge_id", record.get("document_id",
                  record.get("source_id"))))))
        if claimed is not None:
            return str(claimed) in (slug, source.id)
        claimed_path = record.get("path")
        if claimed_path is not None:
            normalized = str(claimed_path).replace("\\", "/")
            return normalized.endswith("/" + slug + ".md") or normalized == slug + ".md"
        return source.id == "knowledge:patterns" and store == "patterns_history.jsonl"

    def _normalize_events(self, source: Source, store: str, index: int,
                          record: Any) -> list[dict[str, Any]]:
        if not isinstance(record, dict):
            return []
        ts = self._scalar(record.get("ts"))
        # The native writers discriminate on "type"; "kind"/"event" are legacy
        # spellings kept for stores that predate it.
        raw_kind = record.get("type")
        if raw_kind is None or raw_kind == "":
            raw_kind = (record.get("kind") or record.get("event")
                        or record.get("source_type") or "event")
        kind = self._scalar(raw_kind)
        identity = f"{store}:{index}:{ts}:{kind}"
        eid = hashlib.sha256(identity.encode()).hexdigest()[:20]
        base = {"event_id": eid, "ts": ts, "kind": kind}
        digested = self._digest_preview_event(base, record)
        if digested:
            return digested
        if source.id == "identity":
            common = {k: self._json_safe(record[k]) for k in
                      ("source_type", "old_sha256", "new_sha256")
                      if k in record}
            result = []
            for side in ("old", "new"):
                content = record.get(side + "_content")
                if isinstance(content, str):
                    fields = dict(common)
                    fields["snapshot_side"] = side
                    item = {**base, "event_id": eid + "-" + side,
                            "representation": "snapshot", "summary": kind + " " + side,
                            "fields": fields, "_content": content}
                    result.append(item)
            return result
        if source.family in ("reflections", "project_reflections"):
            return self._reflection_event(base, record)
        if source.id == "scratchpad" and store == "scratchpad_journal.jsonl":
            return self._scratchpad_journal_event(base, kind, record)
        if store == "scratchpad_blocks":
            content = record.get("content")
            if not isinstance(content, str):
                return []
            fields = {k: self._json_safe(record[k]) for k in ("source", "metadata")
                      if k in record}
            fields["snapshot_scope"] = "block"
            return [{**base, "representation": "snapshot",
                     "summary": "scratchpad block (block snapshot)",
                     "fields": fields, "_content": content}]
        if store.endswith("history.jsonl") and source.family in (
                "knowledge", "project_knowledge"):
            result = []
            for side in ("old", "new"):
                content = record.get(side + "_content")
                if isinstance(content, str):
                    fields = {k: self._scalar(record[k]) for k in
                              ("old_sha256", "new_sha256", "mode") if k in record}
                    fields["snapshot_side"] = side
                    result.append({**base, "event_id": eid + "-" + side,
                                   "representation": "snapshot", "summary": kind + " " + side,
                                   "fields": fields, "_content": content})
            if result:
                return result
        allowed = ("old_sha256", "new_sha256", "mode", "source_type",
                   "content_digested", "source", "metadata",
                   "task_id", "text")
        fields = {k: self._json_safe(record[k]) for k in allowed if k in record}
        return [{**base, "representation": "activity", "summary": kind,
                 "fields": fields}]

    def _reflection_event(self, base: dict[str, Any],
                          record: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize one task-reflection row.

        These rows are recorded execution history of finished tasks: what ran,
        how many rounds it took, what it cost, what it concluded. The canonical
        global log may hold a *pointer* row instead of the full row, when the
        full row lives in that project's own log. Both are activity, and the two
        shapes stay distinguishable so a reader never mistakes a pointer for the
        reflection itself. ``reflection_path`` is reported as recorded text only
        and is never followed as a filesystem capability.
        """
        if record.get("type") == "project_reflection_pointer":
            fields = {k: self._json_safe(record[k]) for k in
                      ("project_id", "reflection_path") if k in record}
            return [{**base, "representation": "activity",
                     "summary": "project reflection pointer", "fields": fields}]
        allowed = ("task_id", "task_type", "rounds", "cost_usd", "error_count")
        fields = {k: self._json_safe(record[k]) for k in allowed if k in record}
        if "goal" in record:
            # Free text: bounded with the same rule the other summaries use.
            fields["goal"] = self._scalar(record["goal"])[:2048]
        return [{**base, "representation": "activity",
                 "summary": "task reflection", "fields": fields}]

    def _scratchpad_journal_event(self, base: dict[str, Any], kind: str,
                                  record: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize one native scratchpad journal record.

        The writer emits an append as a nested ``block`` object holding the
        stored ``content``, an eviction as flat ``evicted_block_*`` fields
        holding the retired ``content``, and a failed append as a nested
        ``block`` that was never stored. Only the first two carry an authentic
        block payload, and each is exposed as one *block* snapshot: the
        scratchpad document itself is never reconstructed from them.
        """
        nested = record.get("block")
        nested = nested if isinstance(nested, dict) else {}
        if kind in ("block_appended", "block_evicted"):
            flat = record.get("evicted_block_content") if kind == "block_evicted" else None
            content = flat if isinstance(flat, str) else nested.get("content")
            if isinstance(content, str):
                fields = {k: self._json_safe(nested[k]) for k in
                          ("ts", "source", "metadata") if k in nested}
                fields.update({k: self._json_safe(record[k]) for k in
                               ("source", "metadata", "content_len", "evicted_block_ts",
                                "evicted_block_source", "source_ref")
                               if k in record})
                fields["snapshot_scope"] = "block"
                return [{**base, "representation": "snapshot",
                         "summary": kind + " (block snapshot)",
                         "fields": fields, "_content": content}]
        # A failed append stored nothing, and a record type this reader has
        # never seen is still an authentic well-formed record: both are
        # activity, not malformed data and not a document version.
        fields = {k: self._json_safe(record[k]) for k in
                  ("source", "metadata", "content_len", "source_ref") if k in record}
        return [{**base, "representation": "activity", "summary": kind,
                 "fields": fields}]

    def _digest_preview_event(self, base: dict[str, Any],
                              record: dict[str, Any]) -> list[dict[str, Any]] | None:
        # Only a literal ``true`` means the writer digested the content. The
        # string "false" and the integer 1 are not that claim, and treating
        # them as true would hide real retained content behind a preview.
        if record.get("content_digested") is not True:
            return None
        fields = {k: self._json_safe(record[k]) for k in
                  ("source_type", "content_digested", "old_sha256", "new_sha256")
                  if k in record}
        for key in ("digest_preview", "old_preview", "new_preview"):
            if key in record:
                fields[key] = self._scalar(record[key])[:2048]
        return [{**base, "representation": "digest_preview", "summary": base["kind"],
                 "fields": fields}]

    def _json_lines(self, path: Path, gaps: list[dict[str, Any]]) -> list[Any]:
        return self._json_lines_raw(path.name, self._read_all(path), gaps)

    def _json_lines_raw(self, name: str, raw: bytes,
                        gaps: list[dict[str, Any]]) -> list[Any]:
        """Parse NDJSON from bytes already in hand, so no store is read twice.

        ``RecursionError`` is a parse failure like any other here: a line far
        below MAX_LINE can still nest containers deeply enough to exhaust the
        decoder's stack, and that is an observation about the file, so it is
        reported as ``malformed_json`` rather than escaping as a 500.
        """
        records = []
        for line in raw.splitlines()[:MAX_HISTORY]:
            if len(line) > MAX_LINE:
                gaps.append(_gap(name, "line_too_large"))
                continue
            try:
                record = json.loads(line)
                if not self._unicode_scalar_safe(record):
                    gaps.append(_gap(name, "malformed_record"))
                    continue
                records.append(record)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                gaps.append(_gap(name, "malformed_json"))
        if len(raw.splitlines()) > MAX_HISTORY:
            gaps.append(_gap(name, "record_scan_limit"))
        return records

    def _json_array(self, path: Path, gaps: list[dict[str, Any]]) -> list[Any]:
        return self._json_array_raw(path.name, self._read_all(path), gaps)

    def _json_array_raw(self, name: str, raw: bytes,
                        gaps: list[dict[str, Any]]) -> list[Any]:
        """Parse a JSON array from bytes already in hand.

        Nesting deep enough to exhaust the decoder's stack is reported as
        ``malformed_json`` for the same reason a bad token is; see
        ``_json_lines_raw``.
        """
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            gaps.append(_gap(name, "malformed_json"))
            return []
        if not isinstance(value, list):
            gaps.append(_gap(name, "malformed_array"))
            return []
        safe = []
        for record in value[:MAX_HISTORY]:
            if self._unicode_scalar_safe(record):
                safe.append(record)
            else:
                gaps.append(_gap(name, "malformed_record"))
        if len(value) > MAX_HISTORY:
            gaps.append(_gap(name, "record_scan_limit",
                             len(value) - MAX_HISTORY))
        return safe

    # ---- revisions/cursors/serialization ------------------------------
    def _source(self, source_id: str) -> tuple[Source, list[dict[str, Any]]]:
        if not isinstance(source_id, str) or len(source_id) > 400:
            raise AtlasError(400, "invalid_source_id", "invalid source ID")
        sources, gaps = self._sources()
        found = next((s for s in sources if s.id == source_id), None)
        if found is None:
            raise AtlasError(404, "source_not_found", "source ID not found")
        return found, gaps

    def _catalog_item(self, source: Source,
                      gaps: list[dict[str, Any]]) -> dict[str, Any]:
        """Catalogue one source.

        ``revision`` is the digest of the exact bytes a ``document`` read would
        return. When the content cannot be read — too large, mid-write, not
        UTF-8-decodable at read time — the item stays in the catalogue with
        ``revision: null`` and a ``read_error`` code, and the same reason is
        added to ``gaps``. No substitute digest is invented: a stat-derived hash
        would be a different quantity presented under the same name, and a
        client would compare it against a content digest.
        """
        media = {".md": "text/markdown", ".json": "application/json"}.get(
            source.path.suffix, "application/x-ndjson")
        try:
            rel = source.path.relative_to(self.root).as_posix()
        except ValueError:      # not reachable for discovered sources
            rel = None
        item: dict[str, Any] = {
            "id": source.id, "family": source.family, "title": source.title,
            # Root-relative location, so a reader can resolve an authored
            # relative link the same way the graph does. It is an identifier for
            # link resolution only: the widget never opens a path.
            "path": rel,
            "media_type": media, "bytes": None, "modified_ns": None,
            "revision": None, "history": source.history, "read_error": None,
            # ``improvement-backlog`` is written by MERGE and keeps digest-only
            # history: two full retained versions of it never exist, so offering
            # a version comparison for it would promise a diff we cannot honestly
            # produce. Every other source can be compared across its snapshots.
            "compare_supported": source.id not in NO_COMPARE_IDS,
        }
        try:
            st = source.path.lstat()
        except OSError:
            item["read_error"] = "unreadable"
            gaps.append(_gap(source.id, "unreadable"))
            return item
        item["bytes"] = st.st_size
        item["modified_ns"] = st.st_mtime_ns
        try:
            item["revision"] = self._read_versioned(source.path, source.id)[1]
        except AtlasError as exc:
            item["read_error"] = exc.code
            gaps.append(_gap(source.id, exc.code))
        return item

    def _history_revision(self, source: Source) -> str:
        return self._revision([(source.id, source.path)] +
                              [(p.name, p) for p in source.history_paths])

    def _aggregate_revision(self, sources: Iterable[Source]) -> str:
        return self._revision((s.id, s.path) for s in sources)

    def _graph_revision(self, sources: Iterable[Source]) -> str:
        return self._revision(self._graph_entries(sources))

    def _graph_entries(self, sources: Iterable[Source]) -> list[tuple[str, Path]]:
        """Every file a graph read can actually consult, each named once.

        ``_aggregate_revision`` covers only the source documents, but graph
        edges also come from the history stores read by ``_journal_ref_edges``
        and ``_shared_task_edges``. Binding the graph to the document digest
        alone let a journal change (or a deleted journal) silently alter the
        edge set at an unchanged revision. Every discovered source path and
        every history path is included exactly once — a store shared by many
        topics is not counted repeatedly — so a change, a deletion or an
        appearance of any of them moves the digest.
        """
        entries: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for source in sources:
            for name, path in ((source.id, source.path),
                               *((source.id + "\0" + p.name, p)
                                 for p in source.history_paths)):
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                entries.append((name, path))
        return entries

    def _revision(self, entries: Iterable[tuple[str, Path]]) -> str:
        """Digest the bound files' bytes, never their stat metadata.

        A revision is a claim about content: a client that pins one and is told
        the revision is unchanged has been told the bytes behind the response
        did not change. Stat metadata cannot carry that claim — an in-place
        edit of the same length with ``st_mtime_ns`` restored leaves device,
        inode, size and mtime identical — so every entry contributes the
        SHA-256 of the bytes ``_read_all`` would serve, under the same MAX_FILE
        bound as the routes that serve them.

        Absence and unreadability are recorded explicitly instead of being
        skipped, so a missing file, a file this reader cannot read and an empty
        file are three different digests. Within one request the bytes are read
        once and reused; see ``_bound_reads``.

        Running out of the request's read budget is *not* one of those recorded
        outcomes: a file left unread for want of budget would silently turn the
        result into a digest over a truncated corpus, so ``ReadBudgetExhausted``
        propagates. Routes that can legitimately serve part of a corpus select
        the covered entries before calling this (and disclose the omission as a
        gap); the rest surface the typed 413.
        """
        digest = hashlib.sha256()
        for name, path in entries:
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            if not self._safe_file(path):
                digest.update(b"absent\0")
                continue
            try:
                raw = self._read_all(path)
            except ReadBudgetExhausted:
                raise
            except AtlasError as exc:
                digest.update(b"unreadable:")
                digest.update(exc.code.encode("utf-8"))
                digest.update(b"\0")
                continue
            digest.update(b"present:")
            digest.update(hashlib.sha256(raw).digest())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _stat_tuple(st: os.stat_result) -> tuple[int, int, int, int]:
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns

    def _paged_offset(self, endpoint: str, binding: str, current: str,
                      cursor: str | None, revision: str | None) -> int:
        if cursor and not revision:
            raise AtlasError(400, "revision_required",
                             "revision is required after the first page")
        if revision and revision != current:
            raise AtlasError(409, "revision_drift", "source changed", revision=current)
        return self._cursor(cursor, endpoint, current, binding) if cursor else 0

    @staticmethod
    def _make_cursor(endpoint: str, revision: str, binding: str, offset: int) -> str:
        raw = json.dumps({"e": endpoint, "r": revision, "b": binding, "o": offset},
                         separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _cursor(token: str, endpoint: str, revision: str, binding: str) -> int:
        # Bound the token before decoding it: an oversized cursor is rejected
        # without allocating its decoded form.
        if not isinstance(token, str) or len(token) > MAX_CURSOR_CHARS:
            raise AtlasError(400, "invalid_cursor", "cursor token is too long")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            value = json.loads(raw)
            if value["e"] != endpoint or value["b"] != binding:
                raise ValueError
            if value["r"] != revision:
                raise AtlasError(409, "revision_drift", "cursor revision is stale",
                                 revision=revision)
            offset = value["o"]
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ValueError
            return offset
        except AtlasError:
            raise
        except Exception as exc:
            raise AtlasError(400, "invalid_cursor", "invalid cursor") from exc

    @staticmethod
    def _limit(value: Any, low: int, high: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise AtlasError(400, "invalid_limit", f"limit must be {low}..{high}")
        return value

    @staticmethod
    def _scalar(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))[:2048]

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        """Pass a value through only if the response serializer will accept it.

        ``allow_nan=False`` matches how the response is actually written, so a
        non-finite number is folded to its literal text here rather than
        breaking the whole response later.
        """
        try:
            if not cls._unicode_scalar_safe(value):
                raise ValueError("value is not safe for strict JSON output")
            json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
            return value
        except (TypeError, ValueError, UnicodeEncodeError):
            fallback = cls._scalar(value)
            if cls._unicode_scalar_safe(fallback):
                return fallback
            try:
                return json.dumps(value, ensure_ascii=True, sort_keys=True,
                                  separators=(",", ":"))[:2048]
            except (TypeError, ValueError):
                return "<unserializable>"

    @staticmethod
    def _event_public(event: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in event.items() if not k.startswith("_")}

    @staticmethod
    def _fold_with_map(text: str, case_sensitive: bool) -> tuple[str, list[int]]:
        if case_sensitive:
            return text, list(range(len(text)))
        folded: list[str] = []
        mapping: list[int] = []
        for index, char in enumerate(text):
            value = char.casefold()
            folded.append(value)
            mapping.extend([index] * len(value))
        return "".join(folded), mapping

    @staticmethod
    def _folded_index_at(mapping: list[int], original_index: int) -> int:
        """First folded offset that belongs to ``original_index`` or later."""
        return bisect.bisect_left(mapping, original_index)

    # ---- provable graph edges ------------------------------------------
    def _link_edges(self, focus: Source, text: str, by_rel: dict[str, str],
                    ids: set[str]) -> Iterable[tuple[str, str, str, str]]:
        """Authored links written in the focus document itself.

        Markdown and wiki links are separate authoring acts and stay separate
        edge kinds. Targets are resolved lexically against the catalogue
        allowlist only: a link target is never opened, so a link can never be
        used as a filesystem capability.

        Both link kinds accept the same two written forms, so the reader and the
        graph share one vocabulary: the exact ``id`` of a catalogued source, or
        a path that lexically resolves to one. The exact id form is a
        whole-string equality against a discovered id, never a prefix or fuzzy
        match, and only text the author actually wrote inside a link is
        considered, so a bare topic word in prose is still not a link. The
        resolved path is not required to end in ``.md``: any allowlisted
        catalogue source qualifies, including the JSON and NDJSON ones.
        """
        try:
            parent = focus.path.parent.relative_to(self.root).as_posix()
        except ValueError:
            return
        link_text = self._mask_non_link_markdown(text)
        for match in MD_REL_LINK_RE.finditer(link_text):
            sid = self._link_target(match.group(1), parent, by_rel, ids, focus)
            if sid:
                yield (sid, "markdown_link", "Markdown link in this document",
                       self._span_excerpt(text, match.start(), match.end()))
        for match in WIKI_LINK_RE.finditer(link_text):
            sid = self._link_target(match.group(1), parent, by_rel, ids, focus,
                                    bare_name_fallback=True)
            if sid:
                yield (sid, "wiki_link", "Wiki link in this document",
                       self._span_excerpt(text, match.start(), match.end()))

    @staticmethod
    def _mask_non_link_markdown(text: str) -> str:
        """Blank Markdown regions whose link-shaped text is not a link.

        Length and newlines are preserved so edge evidence still uses offsets
        from the original document.
        """
        masked = list(text)

        def blank(start: int, end: int) -> None:
            for index in range(start, end):
                if masked[index] not in ("\n", "\r"):
                    masked[index] = " "

        lines = text.splitlines(keepends=True)
        offset = 0
        fence_char: str | None = None
        fence_len = 0
        for line in lines:
            body = line.rstrip("\r\n")
            fence = re.match(r"^ {0,3}(`{3,}|~{3,})", body)
            if fence_char is not None:
                blank(offset, offset + len(line))
                closing = re.match(r"^ {0,3}(%s{%d,})\s*$" %
                                   (re.escape(fence_char), fence_len), body)
                if closing:
                    fence_char = None
                offset += len(line)
                continue
            if fence:
                fence_char, fence_len = fence.group(1)[0], len(fence.group(1))
                blank(offset, offset + len(line))
            elif re.match(r"^(?: {4}|\t)", body):
                blank(offset, offset + len(line))
            offset += len(line)

        visible = "".join(masked)
        for match in re.finditer(r"(`+)([\s\S]*?)\1(?!`)", visible):
            blank(match.start(), match.end())

        visible = "".join(masked)
        image_re = re.compile(r"!" + MD_REL_LINK_RE.pattern)
        for match in image_re.finditer(visible):
            blank(match.start(), match.end())

        for index, char in enumerate(text):
            if char != "[" or masked[index] == " ":
                continue
            slashes = 0
            cursor = index - 1
            while cursor >= 0 and text[cursor] == "\\":
                slashes += 1
                cursor -= 1
            if slashes % 2:
                masked[index] = " "
        return "".join(masked)

    def _link_target(self, written: str, parent: str, by_rel: dict[str, str],
                     ids: set[str], focus: Source,
                     bare_name_fallback: bool = False) -> str | None:
        """Resolve one authored link target to a catalogue id, or to nothing.

        One vocabulary for both link kinds: an exact catalogue id, or a path
        that lexically resolves to an allowlisted catalogue source path. Nothing
        is opened. ``bare_name_fallback`` only adds the wiki convention that
        ``[[patterns]]`` may mean ``patterns.md`` next door.
        """
        target = written.strip()
        if not target:
            return None
        if target in ids and target != focus.id:
            return target
        rel = self._lexical_md_target(parent, target)
        sid = by_rel.get(rel) if rel else None
        if sid or not bare_name_fallback or target.endswith(".md"):
            return sid
        rel = self._lexical_md_target(parent, target + ".md")
        return by_rel.get(rel) if rel else None

    def _journal_ref_edges(self, focus: Source, by_rel: dict[str, str],
                           cache: dict[str, list[Any]], budget: dict[str, Any],
                           gaps: list[dict[str, Any]]
                           ) -> list[tuple[str, str, str, str]]:
        """Provenance recorded by the writer: ``source_ref.read.arguments.path``.

        Only a path that lexically resolves to a catalogued source under the
        data_dir root becomes an edge; anything else is simply not an edge. An
        ``entry_id`` is shown in the excerpt when the record carries one, but an
        entry id alone never creates an edge, and the recorded path is never
        opened.
        """
        out: list[tuple[str, str, str, str]] = []
        for path in focus.history_paths:
            for record in self._store_records(focus, path, cache, budget, gaps):
                if not isinstance(record, dict):
                    continue
                ref = record.get("source_ref")
                if not isinstance(ref, dict):
                    continue
                read = ref.get("read")
                arguments = read.get("arguments") if isinstance(read, dict) else None
                raw = arguments.get("path") if isinstance(arguments, dict) else None
                if not isinstance(raw, str):
                    continue
                rel = self._lexical_path_target("", raw)
                sid = by_rel.get(rel) if rel else None
                if not sid:
                    continue
                entry_id = ref.get("entry_id")
                excerpt = f"{path.name}: source_ref read path {raw}"
                if isinstance(entry_id, (str, int)):
                    excerpt += f" (entry_id {entry_id})"
                out.append((sid, "journal_source_ref", "Journal read reference",
                            excerpt[:2048]))
        return out

    def _shared_task_edges(self, focus: Source, sources: Iterable[Source],
                           cache: dict[str, list[Any]], budget: dict[str, Any],
                           gaps: list[dict[str, Any]]
                           ) -> list[tuple[str, str, str, str]]:
        """Equal, non-empty ``task_id`` values recorded on both sides.

        This is a literal equality of two recorded identifiers, not a guess: the
        same task wrote to both stores. The basis names the shared id so a reader
        can check it.
        """
        focus_ids = self._task_ids(focus, cache, budget, gaps)
        if not focus_ids:
            return []
        out: list[tuple[str, str, str, str]] = []
        for target in sources:
            if target.id == focus.id or not target.history_paths:
                continue
            shared = sorted(focus_ids & self._task_ids(target, cache, budget, gaps))
            for task_id in shared[:1]:
                out.append((target.id, "shared_task_id",
                            "shared task_id " + task_id,
                            f"task_id {task_id} recorded for both "
                            f"{focus.id} and {target.id}"[:2048]))
        return out

    def _task_ids(self, source: Source, cache: dict[str, list[Any]],
                  budget: dict[str, Any],
                  gaps: list[dict[str, Any]]) -> set[str]:
        ids: set[str] = set()
        for path in source.history_paths:
            for record in self._store_records(source, path, cache, budget, gaps):
                if not isinstance(record, dict):
                    continue
                value = record.get("task_id")
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    text = str(value).strip()
                    if text:
                        ids.add(text)
        return ids

    def _store_records(self, source: Source, path: Path,
                       cache: dict[str, list[Any]], budget: dict[str, Any],
                       gaps: list[dict[str, Any]]) -> list[Any]:
        """Read one history/journal store at most once per request, within bounds.

        Records are cached by path and then attributed per source, so a store
        shared by many knowledge topics is parsed once and still only yields the
        rows that name the source being examined.
        """
        key = str(path)
        if key not in cache:
            if budget["truncated"]:
                return []
            try:
                size = path.lstat().st_size
            except OSError:
                gaps.append(_gap(path.name, "unreadable"))
                cache[key] = []
                return []
            if (budget["files"] >= MAX_GRAPH_FILES
                    or budget["bytes"] + size > MAX_SEARCH_BYTES):
                budget["truncated"] = True
                return []
            budget["files"] += 1
            budget["bytes"] += size
            store_gaps: list[dict[str, Any]] = []
            try:
                if path.name == "scratchpad_blocks.json":
                    cache[key] = self._json_array(path, store_gaps)
                else:
                    cache[key] = self._json_lines(path, store_gaps)
            except ReadBudgetExhausted:
                # The request cannot open this store within its cumulative read
                # budget. That is the same fact the 60-store/8 MiB budget above
                # reports, so it is reported the same way: a truncated scan,
                # not an edge set presented as complete.
                budget["truncated"] = True
                cache[key] = []
            except AtlasError as exc:
                gaps.append(_gap(path.name, exc.code))
                cache[key] = []
            except UnicodeDecodeError:
                gaps.append(_gap(path.name, "invalid_utf8"))
                cache[key] = []
            gaps.extend(store_gaps)
        store = "scratchpad_blocks" if path.name == "scratchpad_blocks.json" \
            else path.name
        return [r for r in cache[key] if self._record_matches(source, store, r)]

    @classmethod
    def _lexical_md_target(cls, parent_rel: str, href: str) -> str | None:
        """Resolve an authored href to a root-relative path, lexically only.

        There is no ``.md`` gate: the contract defines a link edge as any target
        that lexically resolves to an allowlisted catalogue source path, and the
        catalogue holds JSON and NDJSON sources (the dialogue chronicle, task
        reflections, project journals) that an author can legitimately link to.
        The allowlist lookup at the call site is what makes the result safe;
        ``_lexical_path_target`` still refuses schemes, absolute paths,
        backslashes, colons and traversal above the root.
        """
        href = href.split("#", 1)[0].strip()
        if not href:
            return None
        return cls._lexical_path_target(parent_rel, href)

    @staticmethod
    def _lexical_path_target(parent_rel: str, href: str) -> str | None:
        """Resolve a written reference to a root-relative path, lexically only.

        No filesystem call is made and nothing is opened here: the result is
        looked up in the catalogue allowlist and discarded when it misses.
        Schemes, absolute paths, backslashes, colons, and traversal above the
        root are refused outright.
        """
        href = href.split("#", 1)[0].strip()
        lowered = href.casefold()
        if lowered.startswith(("http:", "https:", "mailto:", "data:", "file:")):
            return None
        if href.startswith("/") or "\\" in href or ":" in href:
            return None
        parts: list[str] = []
        for part in (*parent_rel.split("/"), *href.split("/")):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
                continue
            stem = part.rsplit(".", 1)[0] if "." in part[1:] else part
            if not NAME_RE.fullmatch(stem) or SECRET_RE.search(part):
                return None
            parts.append(part)
        if not parts:
            return None
        return "/".join(parts)

    @staticmethod
    def _span_excerpt(text: str, start: int, end: int) -> str:
        return text[max(0, start - 60):min(len(text), end + 60)]

    @staticmethod
    def _ok(data: dict[str, Any], gaps: list[dict[str, Any]]) -> dict[str, Any]:
        combined: dict[tuple[str, str], int] = {}
        for gap in gaps:
            key = (str(gap["scope"]), str(gap["reason"]))
            combined[key] = combined.get(key, 0) + int(gap.get("count", 1))
        compact = [{"scope": scope, "reason": reason, "count": count}
                   for (scope, reason), count in sorted(combined.items())[:100]]
        if len(combined) > 100:
            compact.append(_gap("response", "gap_category_limit",
                                len(combined) - 100))
        result = {"ok": True, "data": data, "gaps": compact}
        # ``allow_nan=False`` matches the response serializer, so the size check
        # measures exactly what will be written and never under-reports a value
        # the writer would reject.
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode()) > MAX_RESPONSE:
            raise AtlasError(413, "response_too_large",
                             "serialized response exceeds 256 KiB")
        return result


def error_response(exc: Exception) -> tuple[int, dict[str, Any]]:
    if isinstance(exc, AtlasError):
        error = {"code": exc.code, "message": exc.message}
        error.update(exc.extra)
        return exc.status, {"ok": False, "error": error}
    return 500, {"ok": False, "error": {"code": "internal_error",
                                        "message": "internal error"}}
