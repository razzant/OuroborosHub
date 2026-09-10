---
name: memory-atlas
description: Read-only atlas, document reader, authentic history browser, literal search, and evidence graph for allowlisted Ouroboros core memory, consolidated dialogue chronicles, and project knowledge. Opens on global memory; every link it draws is proven from stored bytes, with no model calls at runtime. Use to inspect memory provenance without modifying serving-process data.
version: 0.2.11
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [route, widget]
ui_tab:
  tab_id: atlas
  title: Memory Atlas
  icon: "◈"
  render:
    kind: module
    entry: widget.js
    start: manual
    height: 580
    span: 2
---

# Memory Atlas

Obtain the canonical serving-process data directory from
`api.get_runtime_info()["data_dir"]` and fail closed if it is invalid. Expose only
the bounded, read-only API in [docs/api-contract.md](docs/api-contract.md). Never
write, index, repair, or follow references found in memory content.

Treat [docs/source-policy.md](docs/source-policy.md) as the normative source and
security boundary. Render all returned source content as untrusted text.

## What this widget shows

It opens on **global memory** — projects are always an explicit choice, never a
default. Shown sources, each labelled with its real nature:

- **Identity** (`memory/identity.md`) with its rewrite journal.
- **Working memory** (`memory/scratchpad_blocks.json`, its generated Markdown
  projection, and the journal that preserves exact evicted blocks).
- **Knowledge topics** (`memory/knowledge/*.md`) with snapshot history. The
  Pattern Register is an ordinary topic; the improvement backlog is an ordinary
  path with merge-based writes and digest-only history, so it offers no diffs.
- **Dialogue chronicles** (`memory/dialogue_blocks.json`) — consolidated summary
  blocks, not raw chat. Summary, compressed *era* and durable *gap* blocks are
  rendered as visibly different kinds; era blocks are labelled lossy. A gap is
  recognised the way the writer actually marks one — a `gap_id`, or the
  `[MEMORY GAP]` marker in the block's own text — not by the record's `type`
  field, which the consolidator leaves as `"summary"` on real gaps.
  `memory/dialogue_meta.json` is surfaced as provenance only — it explains why
  the chronicle stops where it stops. `memory/dialogue_summary.md` appears only
  if it exists, labelled legacy, because nothing writes it any more.
- **Environment profile** (`memory/WORLD.md`) — generated, not authored.
- **Memory source map** (`memory/registry.md`).
- **Latest self-review** (`memory/deep_review.md`) — overwritten in place, so it
  has no evolution timeline.
- **Execution history** — global and per-project task reflections, distinguishing
  full rows from pointer rows. This is recorded process, not hidden reasoning.
- **Project memory** — knowledge, workpad and journal per project.

## What this widget deliberately does not show

This is a stated policy, not an oversight, and the interface says so:
raw `logs/chat.jsonl` and its archives (these are consolidator *inputs*, not
memory), the owner mailbox, control and queue state, `settings.json`, secrets,
and tool or execution logs.

## Relationship graph

The graph makes **no model calls**. Its canvas carries only links provable from
stored bytes: Markdown links and wiki links that resolve to an allowlisted
source, and scratchpad `source_ref` provenance. A link resolves the same way in
the reader and in the graph — by exact source id, or by a path resolved
lexically against the open document and matched to a catalogued source, which
may be a JSON or NDJSON source, not only a `.md` one — so the two views never
disagree about the same document. Shared-`task_id` edges appear
dotted and always state their literal basis. Structural relations — knowledge
index membership, project membership, history-to-document provenance — live in
navigation and timeline surfaces instead of the canvas. There is no inference
layer: a document with no provable links says so plainly.

A graph response's `revision` covers every file the graph can read — each
discovered source document *and* each history store the journal-reference and
shared-`task_id` scans consult, counted once even when many topics share one
store. Pinning that revision therefore rejects a corpus whose edges changed
because a journal changed, disappeared or reappeared, not only one whose
documents changed.

## Stated search bounds

Search pages are bounded work, and the bounds are reported rather than hidden:

- One request reads at most 2,000 files and 8 MiB in total. That single budget
  covers building the revision *and* building the response body — the two read
  the same files, and each file is read once — so the scan bound is a bound on
  work done, not only on bytes kept.
- A corpus larger than the budget is stated, never quietly digested: `search`
  reports `{"scope":"search","reason":"byte_scan_limit"}`, `catalog` and `graph`
  report `revision_scan_limit`, each with the number of entries the revision
  does not cover. Where no partial corpus is meaningful the request fails with
  `413 read_budget_exhausted` instead of returning a revision over part of it.
- The files a request may read are fixed once, as a set, at the start of the
  request, and that set is exactly what its revision covers. An entry left out
  is not read anywhere else in the request either — leftover budget cannot let
  a smaller later file into the answer behind the revision's back — so the
  omission is disclosed rather than served. The `graph` focus is always a
  member: a focus that does not fit is the typed `413`, never a graph answered
  without the document it is about.
- A cursor token longer than 4096 characters is rejected as `400 invalid_cursor`
  before it is decoded.
- The deepest page offset served is 10000. A cursor beyond it is
  `400 invalid_cursor`.
- When more matches exist but the next page would cross that offset, the
  response returns no `next_cursor` and states the truncation as a
  `{"scope":"search","reason":"page_limit_reached"}` gap, instead of handing out
  a token that could not be spent.
