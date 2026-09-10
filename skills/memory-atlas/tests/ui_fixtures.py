"""Synthetic Memory Atlas API fixtures for the widget UI tests.

Nothing here is real memory: every source, timestamp and body is invented. The
fixture list is matched by *exact* path and *exact* query parameter set, so a
widget that renamed or dropped a contract parameter fails to match instead of
silently passing against a lenient mock.
"""
from __future__ import annotations

from typing import Any

REVISION_PATTERNS = "a1" * 32
REVISION_PATTERNS_HISTORY = "b2" * 32
REVISION_TOOLING = "c3" * 32
REVISION_JOURNAL = "d4" * 32
REVISION_AGGREGATE = "e5" * 32
REVISION_DRIFTED = "f6" * 32
REVISION_DIALOGUE = "d1" * 32

# 2026-02-01T09:00:00 local-ish; only ordering matters for the UI.
NS = 1_000_000_000


def _ns(seconds: int) -> int:
    return seconds * NS


# `compare_supported` is False only for improvement-backlog, whose writer keeps
# digest-only history, so a version diff would be a fiction.
CATALOG_PAGE_ONE = [
    {"id": "identity",
     "path": "memory/identity.md", "family": "identity", "title": "Identity",
     "media_type": "text/markdown", "bytes": 4096, "modified_ns": _ns(1_770_000_000),
     "revision": "11" * 32, "history": "mixed", "read_error": None,
     "compare_supported": True},
    {"id": "scratchpad",
     "path": "memory/scratchpad.md", "family": "scratchpad", "title": "Scratchpad",
     "media_type": "text/markdown", "bytes": 2048, "modified_ns": _ns(1_770_100_000),
     "revision": "22" * 32, "history": "mixed", "read_error": None,
     "compare_supported": True},
    {"id": "knowledge:patterns",
     "path": "memory/knowledge/patterns.md", "family": "knowledge", "title": "patterns",
     "media_type": "text/markdown", "bytes": 8192, "modified_ns": _ns(1_770_200_000),
     "revision": REVISION_PATTERNS, "history": "mixed", "read_error": None,
     "compare_supported": True},
    {"id": "knowledge:tooling",
     "path": "memory/knowledge/tooling.md", "family": "knowledge", "title": "tooling",
     "media_type": "text/markdown", "bytes": 1536, "modified_ns": _ns(1_770_050_000),
     "revision": REVISION_TOOLING, "history": "unavailable", "read_error": None,
     "compare_supported": True},
    {"id": "knowledge:improvement-backlog",
     "path": "memory/knowledge/improvement-backlog.md", "family": "knowledge",
     "title": "improvement-backlog", "media_type": "text/markdown",
     "bytes": 1024, "modified_ns": _ns(1_770_075_000),
     "revision": "1b" * 32, "history": "unavailable", "read_error": None,
     "compare_supported": False},
    {"id": "knowledge:undated",
     "path": "memory/knowledge/undated.md", "family": "knowledge", "title": "undated",
     "media_type": "text/markdown", "bytes": 512, "modified_ns": 0,
     "revision": "33" * 32, "history": "unavailable", "read_error": None,
     "compare_supported": True},
]

# The families added after the first release. Every one of them is a real
# catalogue family the widget must name in plain words.
CATALOG_NEW_FAMILIES = [
    {"id": "dialogue",
     "path": "memory/dialogue_blocks.json", "family": "dialogue",
     "title": "Dialogue chronicle",
     "media_type": "application/json", "bytes": 5120,
     "modified_ns": _ns(1_770_400_000), "revision": REVISION_DIALOGUE,
     "history": "none", "read_error": None, "compare_supported": True},
    {"id": "dialogue_legacy",
     "path": "memory/dialogue_summary.md", "family": "dialogue_legacy",
     "title": "Dialogue summary (legacy)", "media_type": "text/markdown",
     "bytes": 800, "modified_ns": _ns(1_769_000_000), "revision": "99" * 32,
     "history": "none", "read_error": None, "compare_supported": True},
    {"id": "world",
     "path": "memory/WORLD.md", "family": "world", "title": "World profile",
     "media_type": "text/markdown", "bytes": 1100,
     "modified_ns": _ns(1_770_310_000), "revision": "aa" * 32,
     "history": "none", "read_error": None, "compare_supported": True},
    {"id": "registry",
     "path": "memory/registry.md", "family": "registry", "title": "Memory source registry",
     "media_type": "text/markdown", "bytes": 1300,
     "modified_ns": _ns(1_770_320_000), "revision": "bb" * 32,
     "history": "none", "read_error": None, "compare_supported": True},
    {"id": "deep_review",
     "path": "memory/deep_review.md", "family": "deep_review", "title": "Latest self-review",
     "media_type": "text/markdown", "bytes": 2600,
     "modified_ns": _ns(1_770_330_000), "revision": "cc" * 32,
     "history": "none", "read_error": None, "compare_supported": True},
    {"id": "reflections",
     "path": "logs/task_reflections.jsonl", "family": "reflections",
     "title": "Task reflections",
     "media_type": "application/x-ndjson", "bytes": 4400,
     "modified_ns": _ns(1_770_340_000), "revision": "dd" * 32,
     "history": "activity", "read_error": None, "compare_supported": True},
]

CATALOG_PAGE_TWO = [
    {"id": "project:atlas:knowledge:design",
     "path": "projects/atlas/knowledge/design.md", "family": "project_knowledge",
     "title": "atlas / design", "media_type": "text/markdown", "bytes": 3072,
     "modified_ns": _ns(1_770_300_000), "revision": "44" * 32, "history": "mixed",
     "read_error": None, "compare_supported": True},
    {"id": "project:atlas:workpad",
     "path": "projects/atlas/workpad.md", "family": "project_workpad",
     "title": "atlas / workpad", "media_type": "text/markdown", "bytes": 900,
     "modified_ns": _ns(1_770_250_000), "revision": "55" * 32, "history": "none",
     "read_error": None, "compare_supported": True},
    {"id": "project:atlas:journal",
     "path": "projects/atlas/journal.jsonl", "family": "project_journal",
     "title": "atlas / journal", "media_type": "application/x-ndjson", "bytes": 1200,
     "modified_ns": _ns(1_770_260_000), "revision": REVISION_JOURNAL,
     "history": "activity", "read_error": None, "compare_supported": True},
    {"id": "project:atlas:reflections",
     "path": "projects/atlas/logs/task_reflections.jsonl", "family": "project_reflections",
     "title": "atlas / task reflections", "media_type": "application/x-ndjson", "bytes": 640,
     "modified_ns": _ns(1_770_265_000), "revision": "ee" * 32, "history": "activity",
     "read_error": None, "compare_supported": True},
    {"id": "project:beacon:knowledge:notes",
     "path": "projects/beacon/knowledge/notes.md", "family": "project_knowledge",
     "title": "beacon / notes", "media_type": "text/markdown", "bytes": 700,
     "modified_ns": _ns(1_770_270_000), "revision": "66" * 32, "history": "unavailable",
     "read_error": None, "compare_supported": True},
    {"id": "knowledge:drifting",
     "path": "memory/knowledge/drifting.md", "family": "knowledge", "title": "drifting",
     "media_type": "text/markdown", "bytes": 640, "modified_ns": _ns(1_770_280_000),
     "revision": "77" * 32, "history": "unavailable", "read_error": None,
     "compare_supported": True},
    # The backend could not read the bytes, so it reports no revision digest at
    # all rather than substituting a stat-derived hash under the same name.
    # The policy establishes a history store for knowledge, but this one could
    # not be read: `unavailable` is a different fact from `none`.
    {"id": "knowledge:vanished",
     "path": "memory/knowledge/vanished.md", "family": "knowledge", "title": "vanished",
     "media_type": "text/markdown", "bytes": 480,
     "modified_ns": _ns(1_770_290_000), "revision": "88" * 32,
     "history": "unavailable", "read_error": None, "compare_supported": True},
    {"id": "knowledge:unreadable",
     "path": "memory/knowledge/unreadable.md", "family": "knowledge", "title": "unreadable",
     "media_type": "text/markdown", "bytes": None, "modified_ns": None,
     "revision": None, "history": "unavailable", "read_error": "file_too_large",
     "compare_supported": True},
]

CATALOG_ITEMS = CATALOG_PAGE_ONE + CATALOG_NEW_FAMILIES + CATALOG_PAGE_TWO

# The reader exercise document: every Markdown construct the plan requires,
# plus hostile text that must survive as literal characters.
PATTERNS_PAGE_ONE = """# Retention patterns

Working notes about **retention**, _decay_ and ~~eviction~~ with `inline code`.
A safe link to [the spec](https://example.invalid/spec), an internal reference to
[tooling notes](knowledge:tooling), and a [dangling pointer](javascript:alert(1))
that must stay inert.

Every authored link form the backend turns into an edge also has to open in the
reader: a sibling path [the undated notes](undated.md), a non-Markdown source
[the chronicle](../dialogue_blocks.json), and a wiki link to
[[../registry]]. A [missing sibling](nowhere.md) resolves to nothing on both
sides. So do the two inherited names [note](constructor) and [[toString]],
which are ordinary authored text and not catalogue entries.

These are not authored links: ![diagram](tooling.md),
`[inline example](tooling.md)`, and \\[escaped example](tooling.md).

```md
[fenced example](tooling.md)
```

## Checklist

- [x] Describe the retention window
- [ ] Measure the eviction rate
- Ordinary bullet
  - Nested bullet with `code`

1. First numbered step
2. Second numbered step

## Comparison table

| Stratum | Retention | Reconstructible |
| --- | :---: | ---: |
| Identity | long | digest only |
| Knowledge | medium | yes |
| Journal | short | no |

> Retention is a property of the store, not a claim about the document.

```python
def retain(block, ttl):
    return block if ttl > 0 else None
```

Literal markup that must not become HTML:
<img src=x onerror="window.__pwned = true"> and <script>window.__pwned = true;</script>
"""

PATTERNS_PAGE_TWO = """
## Continued after paging

This paragraph only exists on the second page of the document, so it proves that
"Load more" really appends the rest of the source.

---

Final line.
"""

SNAPSHOT_OLD = """# Retention patterns

Retention window: 7 days.
Eviction: oldest first.
Unchanged tail line.
"""

SNAPSHOT_NEW = """# Retention patterns

Retention window: 30 days.
Eviction: oldest first.
Added guarantee line.
Unchanged tail line.
"""

JOURNAL_TEXT = (
    '{"ts": "2026-02-01T09:00:00", "kind": "note", "text": "journal entry one"}\n'
    '{"ts": "2026-02-01T10:00:00", "kind": "note", "text": "journal entry two"}\n'
)

HISTORY_PAGE_ONE = [
    {"event_id": "ev-new", "ts": "2026-02-03T11:15:00", "kind": "knowledge_updated",
     "representation": "snapshot", "summary": "knowledge updated",
     "fields": {"sha": "9f" * 4, "source_type": "knowledge"}},
    {"event_id": "ev-old", "ts": "2026-02-01T08:05:00", "kind": "knowledge_created",
     "representation": "snapshot", "summary": "knowledge created",
     "fields": {"sha": "1a" * 4, "source_type": "knowledge"}},
    {"event_id": "ev-digest", "ts": "2026-01-28T22:40:00", "kind": "identity_write",
     "representation": "digest_preview", "summary": "identity write",
     "fields": {"content_digested": "true",
                "new_content_preview": "Only the first characters were retained",
                "new_content_preview_truncated": True}},
]

HISTORY_PAGE_TWO = [
    {"event_id": "ev-stale", "ts": "2026-01-22T09:30:00", "kind": "knowledge_updated",
     "representation": "snapshot", "summary": "knowledge updated (stale)",
     "fields": {"sha": "5c" * 4, "source_type": "knowledge"}},
    {"event_id": "ev-activity", "ts": "2026-01-20T07:00:00", "kind": "reindexed",
     "representation": "activity", "summary": "reindexed",
     "fields": {"task_id": "t-0042"}},
]

SEARCH_HITS = [
    {"id": "knowledge:patterns", "line": 3, "column": 22,
     "excerpt": "Working notes about retention, decay and eviction",
     "match_start": 20, "match_end": 29},
    {"id": "project:beacon:knowledge:notes", "line": 2, "column": 5,
     "excerpt": "beacon retention notes", "match_start": 7, "match_end": 16},
]

# The only four link kinds the contract allows, each with the backend's own
# literal `basis` string, plus one edge under an unknown kind so the widget's
# honest handling of it is covered.
#
# Every link edge here is a form `_link_edges` really emits against
# PATTERNS_PAGE_ONE, resolved against `memory/knowledge/` (the directory of
# `memory/knowledge/patterns.md`): an exact catalogue id, a sibling relative
# path, a relative path to a non-Markdown catalogue source, and a wiki link
# resolved as a path. `nowhere.md`, `constructor` and `toString` resolve to no
# catalogue source, so the backend emits nothing for them and the reader leaves
# them unresolved. The last two are names every plain JavaScript object
# inherits: the backend looks them up in a real set and reports no edge, and
# the reader must agree.
GRAPH_EDGES = [
    {"source": "knowledge:patterns", "target": "knowledge:tooling",
     "kind": "markdown_link", "basis": "Markdown link in this document",
     "evidence": {
         "source_excerpt": "an internal reference to [tooling notes](knowledge:tooling)"}},
    {"source": "knowledge:patterns", "target": "knowledge:undated",
     "kind": "markdown_link", "basis": "Markdown link in this document",
     "evidence": {
         "source_excerpt": "a sibling path [the undated notes](undated.md)"}},
    {"source": "knowledge:patterns", "target": "dialogue",
     "kind": "markdown_link", "basis": "Markdown link in this document",
     "evidence": {"source_excerpt":
                  "[the chronicle](../dialogue_blocks.json)"}},
    {"source": "knowledge:patterns", "target": "registry",
     "kind": "wiki_link", "basis": "Wiki link in this document",
     "evidence": {"source_excerpt": "a wiki link to [[../registry]]"}},
    {"source": "knowledge:patterns", "target": "project:atlas:journal",
     "kind": "journal_source_ref", "basis": "Journal read reference",
     "evidence": {"source_excerpt": "journal entry recorded reading knowledge:patterns"}},
    {"source": "knowledge:patterns", "target": "project:atlas:knowledge:design",
     "kind": "shared_task_id", "basis": "shared task_id abc123",
     "evidence": {"source_excerpt": "task_id abc123"}},
    {"source": "knowledge:patterns", "target": "knowledge:drifting",
     "kind": "speculative_guess", "basis": "not a contract kind",
     "evidence": {"source_excerpt": "never rendered as a link"}},
]

GRAPH_NODES = [
    {"id": "knowledge:tooling", "title": "tooling", "family": "knowledge"},
    {"id": "knowledge:undated", "title": "undated", "family": "knowledge"},
    {"id": "dialogue", "title": "Dialogue chronicle", "family": "dialogue"},
    {"id": "registry", "title": "Memory source registry", "family": "registry"},
    {"id": "project:atlas:journal", "title": "atlas / journal",
     "family": "project_journal"},
    {"id": "project:atlas:knowledge:design", "title": "atlas / design",
     "family": "project_knowledge"},
]

DIALOGUE_SUMMARY = ("## Retention talk\n\nWe agreed to keep **summaries**, not "
                    "transcripts.\n")
DIALOGUE_ERA = "Early conversations about the reader, folded together.\n"
DIALOGUE_GAP = "This stretch was never consolidated.\n"
DIALOGUE_ODD = "Stored under a block kind this reader does not know.\n"
DIALOGUE_LATER = "A further summary block, fetched by paging.\n"

DIALOGUE_BLOCKS_PAGE_ONE = [
    {"block_id": "blk-summary", "ts": "2026-02-02T10:00:00", "type": "summary",
     "range": "messages 100-140", "message_count": 41, "gap_id": None,
     "content": DIALOGUE_SUMMARY, "content_bytes": len(DIALOGUE_SUMMARY.encode()),
     "truncated": False},
    {"block_id": "blk-era", "ts": "2026-01-15T08:00:00", "type": "era",
     "range": "messages 1-99", "message_count": 99, "gap_id": None,
     "content": DIALOGUE_ERA, "content_bytes": len(DIALOGUE_ERA.encode()),
     "truncated": True},
    {"block_id": "blk-gap", "ts": "2026-01-30T12:00:00", "type": "gap",
     "range": "messages 141-160", "message_count": 20, "gap_id": "gap-7",
     "content": DIALOGUE_GAP, "content_bytes": len(DIALOGUE_GAP.encode()),
     "truncated": False},
    {"block_id": "blk-odd", "ts": "2026-02-04T09:00:00", "type": "spiral",
     "range": None, "message_count": None, "gap_id": None,
     "content": DIALOGUE_ODD, "content_bytes": len(DIALOGUE_ODD.encode()),
     "truncated": False},
]

DIALOGUE_BLOCKS_PAGE_TWO = [
    {"block_id": "blk-later", "ts": "2026-02-05T10:00:00", "type": "summary",
     "range": "messages 161-180", "message_count": 20, "gap_id": None,
     "content": DIALOGUE_LATER, "content_bytes": len(DIALOGUE_LATER.encode()),
     "truncated": False},
]

DIALOGUE_SIGNATURE_PLAIN = "a1" * 32
DIALOGUE_SIGNATURE_TOP_LEVEL = {"first_line_sha256": "b2" * 32,
                                "line_count": 160}
DIALOGUE_SIGNATURE_NESTED = {"generation": {"first_line_sha256": "ab" * 16,
                                             "last_line_sha256": "cd" * 16},
                             "line_count": 160}
DIALOGUE_SIGNATURE_OTHER = {"checkpoint": {"digest": "c3" * 32},
                            "line_count": 160}

DIALOGUE_META = {"available": True, "reason": None,
                 "last_consolidated_offset": 160,
                 "chat_log_signature": DIALOGUE_SIGNATURE_NESTED,
                 "last_consolidated_at": "2026-02-04T09:30:00"}

DIALOGUE_META_UNAVAILABLE = {"available": False, "reason": "state_file_missing",
                             "last_consolidated_offset": None,
                             "chat_log_signature": None,
                             "last_consolidated_at": None}

DIALOGUE_RAW_JSON = (
    '{"blocks": [{"block_id": "blk-summary", "type": "summary", '
    '"content": "We agreed to keep summaries, not transcripts."}]}\n')

SIMPLE_DOCS = {
    "identity": "# Identity\n\nWho I am, in my own words.\n",
    "dialogue_legacy": "# Old dialogue summary\n\nSuperseded, kept only because it "
                       "exists.\n",
    "world": "# World profile\n\nGenerated description of this machine.\n",
    "registry": "# Memory source registry\n\nThe map of sources and their trust "
                "notes.\n",
    "deep_review": "# Latest self-review\n\nOnly the latest text exists.\n",
    "knowledge:improvement-backlog": "# Improvement backlog\n\nPending repair.\n",
    # NDJSON as the live reader serves it: the global log may hold a pointer
    # whose full row lives in the project file. The two shapes stay distinct.
    "reflections": (
        '{"ts":"10","task_id":"t-global","task_type":"build","goal":"ship it",'
        '"rounds":3,"cost_usd":0.5,"error_count":1,"reflection":"went fine"}\n'
        '{"ts":"11","task_id":"t2","type":"project_reflection_pointer",'
        '"project_id":"atlas",'
        '"reflection_path":"projects/atlas/logs/task_reflections.jsonl"}\n'
    ),
    "project:atlas:reflections": (
        '{"ts":"11","task_id":"t2","task_type":"fix","goal":"repair atlas",'
        '"rounds":1,"cost_usd":0.1,"error_count":0,'
        '"reflection":"project reflection body"}\n'
    ),
    "project:atlas:knowledge:design": "# atlas design\n\nDesign decisions about "
                                      "retention windows.\n",
    "project:atlas:workpad": "# atlas workpad\n\nWork in progress.\n",
    "project:beacon:knowledge:notes": "# beacon notes\n\nbeacon retention notes.\n",
}

IDENTITY_HISTORY = [
    {"event_id": "id-new", "ts": "2026-02-03T11:15:00", "kind": "identity_write",
     "representation": "snapshot", "summary": "identity written",
     "fields": {"sha": "ab" * 4}},
    {"event_id": "id-old", "ts": "2026-02-01T08:05:00", "kind": "identity_write",
     "representation": "snapshot", "summary": "identity written earlier",
     "fields": {"sha": "cd" * 4}},
]


def _ok(data: dict[str, Any], gaps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data, "gaps": gaps or []}


def _entry(path: str, query: dict[str, str], body: dict[str, Any],
           status: int = 200) -> dict[str, Any]:
    return {"path": path, "query": query, "status": status, "body": body}


def fixtures(*, catalog_page_one: list[dict[str, Any]] | None = None,
             catalog_page_two: list[dict[str, Any]] | None = None,
             dialogue_meta: dict[str, Any] | None = None,
             dialogue_blocks: list[dict[str, Any]] | None = None
             ) -> list[dict[str, Any]]:
    """Exact (path, query) -> response fixtures for the harness fetch stub."""
    page_one = CATALOG_PAGE_ONE + CATALOG_NEW_FAMILIES if catalog_page_one is None \
        else catalog_page_one
    page_two = CATALOG_PAGE_TWO if catalog_page_two is None else catalog_page_two
    meta = DIALOGUE_META if dialogue_meta is None else dialogue_meta
    blocks = DIALOGUE_BLOCKS_PAGE_ONE if dialogue_blocks is None else dialogue_blocks
    doc_gap = [{"scope": "knowledge", "reason": "unreadable", "count": 1},
               {"scope": "knowledge:unreadable", "reason": "file_too_large",
                "count": 1}]
    entries = [
        _entry("/catalog", {"limit": "100"},
               _ok({"items": page_one, "next_cursor": "catalog-page-2"})),
        _entry("/catalog", {"cursor": "catalog-page-2", "limit": "100"},
               _ok({"items": page_two, "next_cursor": None}, doc_gap)),

        _entry("/document", {"id": "knowledge:patterns", "limit": "16384"},
               _ok({"id": "knowledge:patterns", "revision": REVISION_PATTERNS,
                    "offset": 0, "content": PATTERNS_PAGE_ONE,
                    "content_bytes": len(PATTERNS_PAGE_ONE.encode()),
                    "complete": False, "next_cursor": "document-page-2"})),
        _entry("/document", {"id": "knowledge:patterns", "cursor": "document-page-2",
                             "limit": "16384", "revision": REVISION_PATTERNS},
               _ok({"id": "knowledge:patterns", "revision": REVISION_PATTERNS,
                    "offset": len(PATTERNS_PAGE_ONE.encode()),
                    "content": PATTERNS_PAGE_TWO,
                    "content_bytes": len(PATTERNS_PAGE_TWO.encode()),
                    "complete": True, "next_cursor": None})),
        _entry("/document", {"id": "knowledge:tooling", "limit": "16384"},
               _ok({"id": "knowledge:tooling", "revision": REVISION_TOOLING, "offset": 0,
                    "content": "# Tooling\n\nShort and complete.\n",
                    "content_bytes": 31, "complete": True, "next_cursor": None})),
        _entry("/document", {"id": "project:atlas:journal", "limit": "16384"},
               _ok({"id": "project:atlas:journal", "revision": REVISION_JOURNAL,
                    "offset": 0, "content": JOURNAL_TEXT,
                    "content_bytes": len(JOURNAL_TEXT.encode()),
                    "complete": True, "next_cursor": None})),
        _entry("/document", {"id": "knowledge:drifting", "limit": "16384"},
               {"ok": False, "error": {"code": "revision_drift",
                                       "message": "source changed",
                                       "revision": REVISION_DRIFTED}},
               status=409),

        _entry("/document", {"id": "knowledge:unreadable", "limit": "16384"},
               {"ok": False, "error": {"code": "file_too_large",
                                       "message": "source exceeds 4 MiB bound"}},
               status=413),

        # The dialogue chronicle's own "Source text" view: the raw stored JSON.
        _entry("/document", {"id": "dialogue", "limit": "16384"},
               _ok({"id": "dialogue", "revision": REVISION_DIALOGUE, "offset": 0,
                    "content": DIALOGUE_RAW_JSON,
                    "content_bytes": len(DIALOGUE_RAW_JSON.encode()),
                    "complete": True, "next_cursor": None})),

        _entry("/dialogue", {"limit": "10"},
               _ok({"revision": REVISION_DIALOGUE, "blocks": blocks,
                    "next_cursor": "dialogue-page-2", "meta": meta})),
        _entry("/dialogue", {"cursor": "dialogue-page-2", "limit": "10",
                             "revision": REVISION_DIALOGUE},
               _ok({"revision": REVISION_DIALOGUE,
                    "blocks": DIALOGUE_BLOCKS_PAGE_TWO,
                    "next_cursor": None, "meta": meta})),

        _entry("/history", {"id": "knowledge:patterns", "limit": "50"},
               _ok({"id": "knowledge:patterns", "revision": REVISION_PATTERNS_HISTORY,
                    "items": HISTORY_PAGE_ONE, "next_cursor": "history-page-2"})),
        _entry("/history", {"id": "knowledge:patterns", "cursor": "history-page-2",
                            "limit": "50", "revision": REVISION_PATTERNS_HISTORY},
               _ok({"id": "knowledge:patterns", "revision": REVISION_PATTERNS_HISTORY,
                    "items": HISTORY_PAGE_TWO, "next_cursor": None},
                   [{"scope": "history", "reason": "malformed_record", "count": 2}])),

        # Two full snapshots exist, but the catalogue says this source keeps no
        # earlier text, so the widget must not offer to diff them.
        _entry("/history", {"id": "identity", "limit": "50"},
               _ok({"id": "identity", "revision": REVISION_AGGREGATE,
                    "items": IDENTITY_HISTORY, "next_cursor": None})),
        _entry("/history", {"id": "knowledge:improvement-backlog", "limit": "50"},
               _ok({"id": "knowledge:improvement-backlog",
                    "revision": REVISION_AGGREGATE,
                    "items": IDENTITY_HISTORY, "next_cursor": None})),

        _entry("/history/event", {"id": "knowledge:patterns", "event_id": "ev-new",
                                  "limit": "16384",
                                  "revision": REVISION_PATTERNS_HISTORY},
               _ok({"id": "knowledge:patterns", "event_id": "ev-new",
                    "representation": "snapshot",
                    "revision": REVISION_PATTERNS_HISTORY, "offset": 0,
                    "content": SNAPSHOT_NEW,
                    "content_bytes": len(SNAPSHOT_NEW.encode()),
                    "complete": True, "next_cursor": None})),
        _entry("/history/event", {"id": "knowledge:patterns", "event_id": "ev-old",
                                  "limit": "16384",
                                  "revision": REVISION_PATTERNS_HISTORY},
               _ok({"id": "knowledge:patterns", "event_id": "ev-old",
                    "representation": "snapshot",
                    "revision": REVISION_PATTERNS_HISTORY, "offset": 0,
                    "content": SNAPSHOT_OLD,
                    "content_bytes": len(SNAPSHOT_OLD.encode()),
                    "complete": True, "next_cursor": None})),

        # Served from a different state of the store than the listing: the
        # widget must report drift instead of stitching the body in.
        _entry("/history/event", {"id": "knowledge:patterns", "event_id": "ev-stale",
                                  "limit": "16384",
                                  "revision": REVISION_PATTERNS_HISTORY},
               _ok({"id": "knowledge:patterns", "event_id": "ev-stale",
                    "representation": "snapshot", "revision": REVISION_DRIFTED,
                    "offset": 0, "content": SNAPSHOT_OLD,
                    "content_bytes": len(SNAPSHOT_OLD.encode()),
                    "complete": True, "next_cursor": None})),

        _entry("/document", {"id": "knowledge:vanished", "limit": "16384"},
               _ok({"id": "knowledge:vanished", "revision": "88" * 32, "offset": 0,
                    "content": "# Vanished\n\nThe current file still reads.\n",
                    "content_bytes": 42, "complete": True, "next_cursor": None})),
        _entry("/history", {"id": "knowledge:vanished", "limit": "50"},
               _ok({"id": "knowledge:vanished", "revision": REVISION_AGGREGATE,
                    "items": [], "next_cursor": None},
                   [{"scope": "knowledge:vanished", "reason": "history_unavailable",
                     "count": 1}])),
        _entry("/history", {"id": "knowledge:tooling", "limit": "50"},
               _ok({"id": "knowledge:tooling", "revision": REVISION_AGGREGATE,
                    "items": [], "next_cursor": None},
                   [{"scope": "knowledge:tooling", "reason": "history_unavailable",
                     "count": 1}])),

        _entry("/search", {"q": "retention", "limit": "25", "case_sensitive": "false"},
               _ok({"query": "retention", "revision": REVISION_AGGREGATE,
                    "items": SEARCH_HITS, "next_cursor": None},
                   [{"scope": "search", "reason": "byte_scan_limit", "count": 1}])),
        _entry("/search", {"q": "nothingmatches", "limit": "25",
                           "case_sensitive": "false"},
               _ok({"query": "nothingmatches", "revision": REVISION_AGGREGATE,
                    "items": [], "next_cursor": None})),

        # `inferred` is gone from the contract: the graph takes focus and limit.
        _entry("/graph", {"focus": "knowledge:patterns", "limit": "50"},
               _ok({"focus": "knowledge:patterns", "revision": REVISION_AGGREGATE,
                    "nodes": GRAPH_NODES, "edges": GRAPH_EDGES})),
        _entry("/graph", {"focus": "knowledge:tooling", "limit": "50"},
               _ok({"focus": "knowledge:tooling", "revision": REVISION_AGGREGATE,
                    "nodes": [], "edges": []})),
        _entry("/graph", {"focus": "project:atlas:knowledge:design", "limit": "50"},
               _ok({"focus": "project:atlas:knowledge:design",
                    "revision": REVISION_AGGREGATE, "nodes": [], "edges": []})),
    ]
    for source_id, body in SIMPLE_DOCS.items():
        entries.append(_entry(
            "/document", {"id": source_id, "limit": "16384"},
            _ok({"id": source_id, "revision": "0f" * 32, "offset": 0,
                 "content": body, "content_bytes": len(body.encode()),
                 "complete": True, "next_cursor": None})))
    return entries


def fixtures_without_project(project: str) -> list[dict[str, Any]]:
    """The catalogue as it looks after a whole project has disappeared."""
    kept = [item for item in CATALOG_PAGE_TWO
            if not str(item["id"]).startswith("project:" + project + ":")]
    return fixtures(catalog_page_two=kept)


def fixtures_without_legacy_dialogue() -> list[dict[str, Any]]:
    """No legacy dialogue file exists at all, so the section must be absent."""
    kept = [item for item in CATALOG_NEW_FAMILIES
            if item["family"] != "dialogue_legacy"]
    return fixtures(catalog_page_one=CATALOG_PAGE_ONE + kept)


def fixtures_dialogue_unavailable() -> list[dict[str, Any]]:
    """The consolidation record could not be read; no position may be invented."""
    return fixtures(dialogue_meta=DIALOGUE_META_UNAVAILABLE, dialogue_blocks=[])


def fixtures_dialogue_signature(signature: Any) -> list[dict[str, Any]]:
    """A chronicle whose host pass-through used a particular signature shape."""
    meta = dict(DIALOGUE_META, chat_log_signature=signature)
    return fixtures(dialogue_meta=meta)


MALFORMED_BLOCKS = [
    # No `type`, no `content`: the widget must show it as stored without
    # inventing a kind or a body.
    {"block_id": "blk-broken", "ts": None, "range": None, "message_count": None,
     "gap_id": None, "content": None, "content_bytes": None, "truncated": None},
    # A block whose content is not a string at all.
    {"block_id": "blk-wrong", "ts": "not-a-timestamp", "type": 17,
     "range": {"from": 1}, "message_count": "many", "gap_id": None,
     "content": {"unexpected": "shape"}, "content_bytes": None, "truncated": True},
]

LONG_BLOCK_TEXT = "Consolidated line about retention.\n\n" * 400

LONG_BLOCKS = [
    {"block_id": "blk-long", "ts": "2026-02-06T10:00:00", "type": "summary",
     "range": "messages 1-4000", "message_count": 4000, "gap_id": None,
     "content": LONG_BLOCK_TEXT, "content_bytes": len(LONG_BLOCK_TEXT.encode()),
     "truncated": True},
]


def fixtures_dialogue_malformed() -> list[dict[str, Any]]:
    """Blocks with missing and wrongly typed fields."""
    entries = fixtures(dialogue_blocks=MALFORMED_BLOCKS)
    dialogue = next(entry for entry in entries
                    if entry["path"] == "/dialogue"
                    and entry["query"] == {"limit": "10"})
    dialogue["body"]["gaps"] = [
        {"scope": "dialogue_blocks.json", "reason": "malformed_record", "count": 2}]
    return entries


def fixtures_search_incomplete() -> list[dict[str, Any]]:
    """An empty search whose byte bound prevented a complete scan."""
    entries = fixtures()
    search = next(entry for entry in entries
                  if entry["path"] == "/search"
                  and entry["query"].get("q") == "nothingmatches")
    search["body"]["gaps"] = [
        {"scope": "search", "reason": "byte_scan_limit", "count": 1}]
    return entries


def fixtures_empty_graph_incomplete() -> list[dict[str, Any]]:
    """An empty graph whose recorded-source scan hit its bound."""
    entries = fixtures()
    graph = next(entry for entry in entries
                 if entry["path"] == "/graph"
                 and entry["query"].get("focus") == "knowledge:tooling")
    graph["body"]["gaps"] = [
        {"scope": "graph", "reason": "scan_limit", "count": 1}]
    return entries


def fixtures_dialogue_long() -> list[dict[str, Any]]:
    """One very long block, to prove the reader scrolls internally."""
    return fixtures(dialogue_blocks=LONG_BLOCKS)
