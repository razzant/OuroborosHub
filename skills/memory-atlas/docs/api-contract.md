# Memory Atlas HTTP API

Prefix: `/api/extensions/memory-atlas/`. All seven routes are bounded `GET` handlers
returning Starlette JSON responses with `Cache-Control: no-store`. Success is
`{"ok":true,"data":...,"gaps":[]}`; errors are
`{"ok":false,"error":{"code":"...","message":"..."}}` with 400, 404, 409,
413, or 500. Unknown parameters are rejected. Opaque cursors bind route, inputs,
offset, and revision. A cursor token longer than 4096 characters is rejected as
`400 invalid_cursor` before it is base64-decoded.

Every `revision` in this contract is bound to bytes, never to filesystem
metadata. A route digests each file its response is built from as `sha256` of
that file's content, under the same 4 MiB per-file bound the routes serve, with
a missing file, an unreadable one and an empty one contributing three different
markers. So an unchanged revision means unchanged content: an in-place edit that
keeps the same length and restores `st_mtime_ns` leaves device, inode, size and
mtime identical and still moves the revision. Each file is read once for the
phase that produces a response and re-read for the closing drift check, which is
what lets that check reject a file mutated mid-scan (`409 revision_drift`). Every
route that binds a revision performs that closing check — `catalog` included.

Revision construction is read work, and it is charged against the same bound as
the body it belongs to. One request may open at most `MAX_READ_FILES` (currently
2,000) files totalling `MAX_READ_BYTES` (currently 8 MiB); the budget is
cumulative across the whole request and is enforced at the open, so digesting a
corpus can never cost more than the route says it reads. Each phase (the response
phase and the closing check) carries that budget once, and within a phase a file
opened for the revision is the same read the body uses.

The set a request may read is fixed once, at the start of the phase, and it is
exactly the set its revision covers. Every file whose bytes can influence a
response is a member of that set, and a path outside it is refused for the rest
of the request rather than admitted by whatever budget happens to be left: a
file skipped because it did not fit cannot be followed by a smaller one that
reaches the response body without being in the revision the body is served
under. So a client that pins a revision and is told it has not changed has been
told that every byte behind that response is unchanged.

A corpus that does not fit is disclosed, never silently truncated into a digest
and never replaced by stat metadata:

- Where a partial corpus is meaningful the covered entries are chosen from
  on-disk sizes *before* anything is hashed, the revision is complete for those
  entries, and the shortfall is a gap: `{"scope":"catalog","reason":"revision_scan_limit","count":n}`,
  `{"scope":"search","reason":"byte_scan_limit","count":n}`, or
  `{"scope":"graph","reason":"revision_scan_limit","count":n}`, where `count` is
  the number of entries the revision does not cover. Those entries are then not
  read at all: they are absent from the answer, not present through a second
  route into it.
- Otherwise the request fails with `413 read_budget_exhausted`. This includes a
  file the route's answer is meaningless without — the `graph` focus document,
  and the document and stores a `history` page binds — which is a required
  member of the set rather than an optional one. A `catalog` item outside the
  selected set is listed with `revision: null` and
  `read_error: "read_budget_exhausted"`, like any other read failure, and the
  same reason appears as a per-source gap.

- `catalog?cursor&limit`: `data={items,next_cursor}`. An item is
  `{id,path,family,title,media_type,bytes,modified_ns,revision,history,read_error,compare_supported}`;
  `path` is the source's root-relative location (e.g.
  `memory/knowledge/patterns.md`), present so a client can resolve an authored
  *relative* link exactly as the graph does and the reader and the Links tab
  cannot disagree about the same document. It is an identifier for link
  resolution only: no route accepts a path and the widget never opens one.
  history is `none|unavailable|activity|mixed`. Item `revision` is the same
  content digest `document` returns for that source, and `modified_ns` is the
  file's latest modification time — a single marker, not a historic revision or
  a retained lifespan. If the content cannot be read for the digest, the item is
  still listed with `revision: null` and a `read_error` code (and `bytes`/
  `modified_ns` also null when even `lstat` fails), plus a matching `gaps` entry;
  no substitute digest is ever synthesized. `read_error` is null on success.
  `compare_supported` is `false` only for `knowledge:improvement-backlog` — that
  document is written by MERGE and keeps digest-only history, so two full retained
  versions never exist and a version diff would be a lie — and `true` otherwise.
  `media_type` is `text/markdown`, `application/json`, or `application/x-ndjson`.
  The page is bound to a revision over the discovered sources (bounded as above)
  and that revision is recomputed after the page is built, so a source changed
  while the page was assembled is `409 revision_drift` rather than a mixed page
  under a cursor bound to a revision that no longer describes the corpus.
- `document?id&cursor&limit&revision`:
  `data={id,revision,offset,content,content_bytes,complete,next_cursor}`.
- `history?id&cursor&limit&revision`:
  `data={id,revision,items,next_cursor}`. Items are
  `{event_id,ts,kind,representation,summary,fields}` where representation is
  `snapshot`, `digest_preview`, or `activity`.
- `history/event?id&event_id&cursor&limit&revision`:
  `data={id,event_id,representation,revision,offset,content,content_bytes,complete,next_cursor}`.
  Non-snapshots return `409 no_snapshot`.
- `search?q&cursor&limit&case_sensitive&revision`:
  `data={query,revision,items,next_cursor}`. Literal hits are
  `{id,line,column,excerpt,match_start,match_end}` with offsets in original Unicode
  characters even when case folding expands a character. At most
  `MAX_SEARCH_FILES` (currently 200) sources are selected, and that selection is
  then trimmed to what the request's read budget covers *before* any of it is
  opened, so the scanned corpus, the hashed corpus and the revision are one and
  the same set of bytes; sources dropped by either bound are reported as
  `file_scan_limit` and `byte_scan_limit` gaps. A match that ends inside an
  expansion (`ß` → `ss`) covers that whole character, and one expansion is reported
  once. Paging is bounded work in memory as well as in scan: a page holds at most
  `limit + 1` hits, and matches before the cursor offset are counted and skipped,
  never retained. The deepest offset served is `MAX_SEARCH_OFFSET` (currently
  10000); a cursor past it is `400 invalid_cursor`. When further matches exist but
  the next page would cross that offset, the response omits `next_cursor` and
  states the truncation as `{"scope":"search","reason":"page_limit_reached"}`
  rather than issuing a token that would be rejected on use.
- `graph?focus&limit&revision`:
  `data={focus,revision,nodes,edges}`. The graph `revision` digests every file a
  graph read can consult — each discovered source path and each history path the
  journal-reference and shared-`task_id` scans read, deduplicated so a store
  shared by many topics counts once, and trimmed to what the request's read
  budget covers before any of it is hashed — an entry left out is a
  `graph`/`revision_scan_limit` gap, and it is then not read either: a store
  outside the revision cannot produce an edge, and the scan that would have
  consulted it reports `{"scope":"graph","reason":"scan_limit"}`. The focus
  document is always a member, so a focus that does not fit the budget is
  `413 read_budget_exhausted` rather than a graph answered from everything
  except the document it is about. A history-only change, deletion or
  reappearance therefore moves the revision, and a stale requested revision is
  `409 revision_drift`; the same digest is recomputed after the scan, so a store
  mutated mid-scan is rejected instead of being served as a half-old edge set. Nodes are `{id,title,family}`; edges are
  `{source,target,kind,basis,evidence:{source_excerpt}}`. There is no language
  model in this skill and no similarity heuristic: every edge is provable from
  recorded bytes, and `kind` is exactly one of
  Both link kinds accept the same two written forms, so the reader and the
  graph share one vocabulary: the exact, whole-string `id` of a catalog source,
  or a path that lexically resolves to an allowlisted catalog source path. The
  resolved path does **not** have to end in `.md` — the catalog contains JSON
  and NDJSON sources (`dialogue`, `reflections`, `project:<p>:journal`,
  `project:<p>:reflections`) and an authored link to one of them is a real edge.
  - `markdown_link` — an inline `[label](target)` in the focus document whose
    target is one of those two forms — e.g. `[notes](knowledge:tooling)`,
    `[see](patterns.md)`, `[chronicle](../dialogue_blocks.json)`
    (`basis: "Markdown link in this document"`);
  - `wiki_link` — a `[[…]]` in the focus document whose target is one of those
    two forms (`[[knowledge:patterns]]`, `[[knowledge/tooling.md]]`), plus the
    wiki-only convention that a bare name may mean the sibling `.md` file
    (`[[tooling]]`). `basis: "Wiki link in this document"`. The id form is
    equality against a discovered id only — never a prefix, substring, or fuzzy
    match — and only text inside `[[…]]` counts, so the same id written bare in
    prose is still not an edge. No form is opened as a path;
  - `journal_source_ref` — a journal row's `source_ref.read.arguments.path`
    mapped onto an allowlisted source path relative to `data_dir`
    (`basis: "Journal read reference"`); `entry_id` appears in the excerpt when
    recorded but never creates an edge by itself;
  - `shared_task_id` — an equal, non-empty `task_id` recorded on both sides,
    with `basis` the literal `"shared task_id <id>"`.

  Link and `source_ref` targets are resolved lexically and never opened. A bare
  source ID or topic word in prose is not a link. At most one edge exists per
  `(target, kind, basis)`, `limit` bounds the number of distinct nodes, and a
  truncated store scan reports `{"scope":"graph","reason":"scan_limit"}`, which
  also covers a store the cumulative read budget cannot open. Responses contain at most `MAX_GRAPH_EDGES` (currently 300) edges. If more
  deduplicated edges would otherwise be reported, `gaps` includes
  `{"scope":"graph","reason":"edge_limit","count":<omitted>}`, where `count`
  is the exact number omitted by that response bound. A focus
  document with no provable links returns an empty edge list.
- `dialogue?cursor&limit&revision`:
  `data={revision,blocks,next_cursor,meta}`. Blocks are
  `{block_id,ts,type,range,message_count,gap_id,content,content_bytes,truncated}`.
  These are the consolidator's summary blocks, not raw chat: each covers roughly
  100 chat entries, and raw `logs/chat.jsonl` is its input and stays excluded.
  `type` is `summary`, `era`, `gap`, or `unknown`, and it is a **resolved** kind,
  not a copy of the record's `type` field. The consolidator never writes
  `type: "gap"`: it writes a discontinuity as `type: "summary"` carrying a
  `gap_id` and a `[MEMORY GAP]` content marker. The discriminator for `gap` is
  therefore `gap_id` (non-empty scalar), with the `[MEMORY GAP]` content marker
  as the secondary signal; only when neither is present does the record's own
  `type` field decide between `era` and `summary`. This matches the host's own
  predicates (`bool(block["gap_id"])` in `consolidator.py`, `gap_id` or the
  content marker in `memory.py`). An `era` block is **lossy** — once more than
  `MAX_SUMMARY_BLOCKS` blocks exist the `ERA_COMPRESS_COUNT` oldest are folded
  into one era block and the summaries it replaced are gone. A `gap` block is
  durable and is never bridged by an era. `unknown` means the record's type is
  absent or unrecognised and it carries no gap marker, which is authentic, not
  malformed. `content` is
  capped at 8,192 bytes on a UTF-8 character boundary with `truncated: true` and
  `content_bytes` the bytes actually returned. `404 source_not_found` when
  `memory/dialogue_blocks.json` is not an accepted safe file; a non-list or
  unparseable file yields `gaps` and an empty block list. `meta` is provenance
  only, read from `memory/dialogue_meta.json`:
  `{available,reason,last_consolidated_offset,chat_log_signature,last_consolidated_at}`
  with `available:false` and `reason` in `missing`/`unreadable`/`malformed_json`
  and null values when it cannot be read — nothing is invented. The revision binds
  both `dialogue_blocks.json` and `dialogue_meta.json`.

Limits default to catalog 100, document/event 16,384 bytes, history 50, search 25,
graph 50, and dialogue 10. Maxima are respectively 200, 65,536, 200, 100, 100, and 50. Revision is
required after the first document/event page. UTF-8 paging rejects non-boundary
offsets and every non-final page advances. `complete=true` means only that this event
or document's final page was reached; it does not claim that one response contains
the whole document. `gaps` entries are `{scope,reason,count}`. Clients must render
returned strings as text, never HTML.

## Backend audit record (2026-09-06)

Independent synthetic-fixture review confirmed the fixed generated-ID surface,
no-follow descriptor reads, literal search, default-off inference, UTF-8 page
continuity, and the documented PluginAPI 2.0 registration calls. It also found that
a malformed `scratchpad_blocks.json` entry without string `content` was being
serialized as an invented snapshot. That is rejected as `malformed_record`; only an
authentic string block payload may be exposed as a snapshot. Catalog and document
now share one content digest; `content_digested` history rows keep
`digest_preview` hashes/previews; project `index-full.md` is excluded with global
knowledge; async routes offload the synchronous reader; explicit graph edges also
recognize lexical Markdown/wiki `.md` links onto catalog sources.

## Integration repair (2026-09-06)

`catalog` no longer swallows a read failure behind a stat-derived hash. The item
keeps `revision: null` with an explicit `read_error` and gap, so a client can
still list and open the source while knowing the digest is unavailable rather
than being handed a different quantity under the `revision` name. The widget
labels `modified_ns` as a last-modification marker (never a historic revision or
lifespan) and describes explicit graph edges as identifier mentions *or* resolved
relative Markdown/wiki links, matching `_explicit_targets`.

## Review repair (2026-09-06, second pass)

- `plugin.py` imports its sibling relatively (`from .memory_reader import …`). The
  host loads the entry file as a package whose `__path__` is the payload directory
  and never extends `sys.path`, so an absolute sibling import would not resolve
  live. The tests load it exactly that way — `spec_from_file_location(unique_name,
  plugin.py, submodule_search_locations=[payload])` with the module registered in
  `sys.modules` before execution — instead of a `sys.path` shim.
- History records are discriminated on the native `type` field and the scratchpad
  journal's real shapes (nested append block, flat `evicted_block_*` eviction, never
  stored failed block) are read as documented in `source-policy.md`.
- `content_digested` is honoured only as a literal `true`.
- Bytes that are not valid UTF-8 never receive a revision digest; the catalogue
  reports `revision: null` with `read_error: invalid_utf8` and a gap.
- `graph` reports a target that disappears between discovery and its `lstat` as an
  `unreadable` gap instead of failing the request.
- The widget labels catalogue `history: "unavailable"` as a missing history store
  (distinct from `none`), keeps the backend's own gap for it, and binds the *first*
  `history/event` page to the revision the history listing was read at, rejecting a
  body served at another revision and refusing to diff two snapshots read at
  different revisions.

## Graph and dialogue rework (2026-09-06, third pass)

- Consolidated dialogue memory is first class again. New catalog sources
  `dialogue`, `dialogue_legacy`, `world`, `registry`, `deep_review`,
  `reflections`, and `project:<p>:reflections`; each appears only when the file
  exists and passes the safe-file gate, and a missing file is absence, not an
  error. `memory/dialogue_meta.json` is deliberately *not* catalogued: it is
  provenance about where consolidation stopped, and it is served in the new
  `dialogue` route's `meta` field.
- New bounded `dialogue` route (seventh route) with block-type semantics spelled
  out above: `era` is compressed and lossy, `gap` is durable and never bridged.
- Task reflection rows are recorded execution history. Full rows and
  `project_reflection_pointer` rows are both `activity` but stay distinguishable
  by `summary` and `fields`; `reflection_path` is reported as text and is never
  followed as a filesystem capability.
- The graph is deterministic and provable. The `inferred` parameter, the
  shared-term scan, and the "an exact source ID appears in the prose" edge class
  were all removed; a request that still passes `inferred` now fails
  `400 unknown_parameter`. Markdown and wiki links are now separate kinds, and
  recorded `source_ref` reads and equal `task_id`s are the only non-link edges.
  Stores are read at most once per request under a 60-file / 8 MiB budget, and a
  truncated scan is reported as a `graph`/`scan_limit` gap.
- `catalog` items gained `compare_supported`, false only for
  `knowledge:improvement-backlog`, whose digest-only history makes a version diff
  impossible to produce honestly. `knowledge:patterns` remains an ordinary topic
  with `knowledge/patterns_history.jsonl` as its evolution.

## Correctness repair (2026-09-06, fourth pass)

Four defects found by re-reading this contract against the code and against the
host writers. Each is now covered by a named test.

- **Dialogue gap discriminator.** The consolidator does not write
  `type: "gap"`; it writes `type: "summary"` with a `gap_id` and a
  `[MEMORY GAP]` content marker. Classifying on the `type` field alone labelled
  every real discontinuity a summary — the opposite of what the block records,
  in the one distinction this skill promises most loudly. The resolved kind now
  uses `gap_id` first and the content marker second.
  (`test_dialogue_block_types_and_truncation`.)
- **One link vocabulary.** The exact-catalog-id form is accepted for
  `markdown_link` as it always was for `wiki_link`, and the widget resolves a
  relative href against the open document's `path` (new catalog field) with the
  same lexical refusals the backend applies, including for `[[wiki]]` links in
  the reader. A link that yields an edge now also opens in the reader, and one
  that opens is one the backend reported.
  (`test_graph_markdown_link_accepts_an_exact_catalog_source_id`,
  `test_the_reader_and_the_links_tab_agree_about_the_same_document`.)
- **Non-Markdown link targets.** The `.md` suffix gate is gone: any target that
  lexically resolves to an allowlisted catalog source path is an edge, so the
  JSON and NDJSON sources this contract lists can actually be linked to. The
  `.md` suffix survives only as the bare-name wiki fallback.
  (`test_graph_links_resolve_to_non_markdown_catalog_sources`.)
- **Awkward-but-valid data.** A non-finite number (`NaN`/`Infinity`, which
  `json.loads` accepts and the response serializer refuses) is reported as a
  `malformed_record` gap instead of turning the route into a 500. And when a
  focus source's history store *is* the focus file (`reflections`,
  `project:<p>:reflections`, `project:<p>:journal`), the already-read bytes seed
  the graph record cache, so the file is read and charged once as documented
  rather than twice — which used to produce a premature `scan_limit` that
  omitted provable edges.
  (`test_non_finite_numbers_are_malformed_records_not_a_broken_response`,
  `test_real_registration_and_all_seven_request_handlers`,
  `test_graph_reads_a_focus_that_is_its_own_history_store_once`.)

## Publication repair (2026-09-09, 0.2.9)

- **Revisions are byte-bound.** `_revision` — and with it the graph, history,
  search/catalog and dialogue revisions built on it — hashed each file's name
  and its `(st_dev, st_ino, st_size, st_mtime_ns)` tuple, not its content. An
  in-place edit of the same length with the mtime restored left all four fields
  identical, so a changed edge set, history, search corpus or dialogue block
  could be served under a pinned revision with both the before and the after
  drift check agreeing nothing had moved — which contradicted the guarantee
  stated above. Each entry now contributes `sha256` of the bytes the routes
  serve, with explicit `absent` and `unreadable` markers, and the allowlist,
  response shapes, field names and drift vocabulary are unchanged. Within one
  response phase a file is read once and reused; the closing check re-reads.
  (`test_every_revision_follows_content_not_stat_metadata`,
  `test_revision_separates_a_missing_file_from_an_empty_one`,
  `test_graph_reads_a_focus_that_is_its_own_history_store_once`.)
- **Deep nesting is a gap, not a 500.** The walk over a parsed record is
  iterative and bounded by `MAX_JSON_DEPTH`, and a decoder that gives up on
  nesting is caught alongside the other parse failures. A value nested far
  below `MAX_FILE` used to raise `RecursionError` and escape as a generic
  `internal_error`; it is now the documented `malformed_json`/`malformed_record`
  gap, and `dialogue_meta.json` reports `reason: "malformed_json"`.
  (`test_deeply_nested_dialogue_records_are_a_gap_not_a_crash`,
  `test_deeply_nested_history_records_are_a_gap_not_a_crash`.)
- **Inherited names are not sources.** The widget's catalogue maps are
  prototype-less. As plain object literals they answered `constructor`,
  `toString`, `valueOf`, `hasOwnProperty` and `__proto__` from
  `Object.prototype`, so an authored `[note](constructor)` or `[[toString]]`
  rendered as an active cross-reference the backend never reported an edge for
  and, when clicked, drew a header for a non-source and issued a 404.
  (`test_the_reader_and_the_links_tab_agree_about_the_same_document`.)

## Bounded-read repair (2026-09-09, 0.2.10)

- **`catalog` had no closing drift check.** It computed `_aggregate_revision`
  and every catalogue item inside the read phase and returned, while `history`,
  `history/event`, `search`, `graph` and `dialogue` all recompute their digest
  after the phase. A source changed after its opening read could therefore be
  served as a mixed page, and its `next_cursor` handed out bound to a revision
  that no longer described the corpus, with no `409 revision_drift`. `catalog`
  now recomputes the same aggregate digest outside the phase and rejects a
  mismatch with the existing drift vocabulary.
  (`test_catalog_rejects_a_source_mutated_while_the_page_was_built`,
  `test_catalog_still_rejects_drift_inside_a_budget_bounded_corpus`.)
- **The read budget capped retention, not reads.** `_revision` called
  `_read_all` for every entry, and the phase budget decided only which bytes
  were *kept*: once spent, reads kept opening and hashing. So `catalog` could
  hash 2,000 files of up to 4 MiB, `search` hashed all 200 selected files before
  its 8 MiB scan bound applied, and `graph` hashed every discovered source and
  history store before its 60-store/8 MiB budget applied — measured at roughly
  5x to 10x the advertised bound for one request, and enough to spend the
  60-second request budget. There is now one cumulative per-request budget
  (`MAX_READ_FILES` files, `MAX_READ_BYTES` bytes) shared by revision
  construction and body reads and enforced inside `_read_all` at the open.
  `catalog`, `search` and `graph` select their corpus from on-disk sizes before
  hashing it and disclose the shortfall (`revision_scan_limit`,
  `byte_scan_limit`); anything else surfaces the typed `413
  read_budget_exhausted` rather than a digest over a truncated corpus. The
  revision stays byte-bound for the corpus it covers, no route falls back to
  stat metadata, and response shapes, field names, the allowlist and the
  `revision_drift` vocabulary are unchanged.
  (`test_catalog_revision_work_stays_inside_the_read_budget`,
  `test_catalog_file_count_stays_inside_the_read_budget`,
  `test_search_revision_work_stays_inside_the_scan_budget`,
  `test_graph_revision_work_stays_inside_the_read_budget`,
  `test_a_bounded_corpus_is_disclosed_and_never_served_as_complete`.)

## Read-set repair (2026-09-09, 0.2.11)

- **The selected corpus was a prefix, but reads were not confined to it.**
  `catalog` and `graph` chose their revision corpus as a leading run of entries
  that fitted the read budget, and then read on: `catalog` built an item for
  every source on the page, and `graph` read the focus document and scanned
  history stores for every source. When the run stopped at a file too big to
  fit, budget remained, so a *smaller* file later in the corpus was still read
  and still shaped the response — an edge, or an item digest — while sitting
  outside the revision that response was served under. On a mixed-size corpus
  the 0.2.10 claim that the revision and the response cover one byte set was
  therefore false, and those bytes could change without moving the pinned
  revision. The phase now fixes its read set once (`_ReadPhase.select`) as a
  *set*, not a prefix, and `_read_all` refuses any path outside it, so leftover
  budget admits nothing: the closing digest runs over exactly the files that
  were read. An entry outside the set is disclosed, not served — `catalog`
  reports `revision: null` with `read_error: "read_budget_exhausted"`, `graph`
  reports `revision_scan_limit` and, for a store it cannot consult,
  `scan_limit`. The `graph` focus and the `history` binding are required
  members, so a focus that does not fit is the typed `413
  read_budget_exhausted` rather than a partial answer. Response shapes, field
  names, the allowlist, `revision_drift` and the gap vocabulary are unchanged.
  (`test_catalog_never_reads_a_small_source_past_a_source_that_did_not_fit`,
  `test_catalog_still_drifts_on_any_source_the_page_actually_read`,
  `test_graph_never_reads_a_store_past_an_entry_that_did_not_fit`,
  `test_graph_revision_covers_exactly_the_files_the_response_read`,
  `test_graph_focus_after_a_non_fitting_entry_is_still_covered`,
  `test_graph_focus_that_does_not_fit_is_a_typed_bounded_outcome`.)
