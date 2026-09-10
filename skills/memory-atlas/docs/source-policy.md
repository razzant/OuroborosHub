# Memory Atlas source policy

The only root is the serving process value `D = api.get_runtime_info()["data_dir"]`.
Registration fails closed when it is empty, relative, missing, or a symlink. No
`HOME` fallback exists. Callers select generated IDs, never paths.

## Included sources — what each one actually is

| ID | Path | Nature, in plain English | History/activity |
|---|---|---|---|
| `identity` | `memory/identity.md` | The authored identity document. | `memory/identity_journal.jsonl` |
| `scratchpad` | `memory/scratchpad.md` | Short-lived working notes. Journal rows are *block* snapshots, never versions of the whole document. | `scratchpad_blocks.json`, `scratchpad_journal.jsonl` |
| `dialogue` | `memory/dialogue_blocks.json` | **Consolidated summary blocks, not raw chat.** Each block covers roughly 100 chat entries. `summary` blocks are one consolidated stretch of conversation. `era` blocks are **lossy**: once more than `MAX_SUMMARY_BLOCKS` blocks exist, the `ERA_COMPRESS_COUNT` oldest blocks are folded into one era block and the individual summaries it replaced are gone for good. `gap` blocks are **durable** markers of a stretch that was never consolidated, and an era never bridges or absorbs them. The writer does *not* label these with `type: "gap"`: the consolidator records a discontinuity as `type: "summary"` carrying a `gap_id` and a `[MEMORY GAP]` content marker, so the discriminator this reader uses is `gap_id` first and the `[MEMORY GAP]` content marker second — matching the host's own predicates — and the record's `type` field is consulted only afterwards. A block with neither marker whose `type` is absent or unrecognised is reported as `unknown`, which is an honest observation about the file, not a defect. | none (served by the `dialogue` route) |
| *(not a source)* `dialogue_meta` | `memory/dialogue_meta.json` | **Provenance only, never a readable document and never catalogued.** It records how far consolidation got (`last_consolidated_offset`, `last_consolidated_at`) and against which chat log (`chat_log_signature`, which nests a generation signature including `first_line_sha256`). It exists to explain honestly why the chronicle stops where it stops. Returned in the `dialogue` route's `meta` field; when it is missing, unreadable, or not a JSON object, `available` is `false` with `reason` in `missing`/`unreadable`/`malformed_json` and every value stays null. | n/a |
| `dialogue_legacy` | `memory/dialogue_summary.md` | **Legacy.** A reader for this file still exists in the codebase, but nothing writes it any more. It must never be presented as current consolidation output. It is shown only when the file happens to exist. | none |
| `world` | `memory/WORLD.md` | A **generated** environment profile, not authored knowledge. No history store exists for it and none is claimed. | none |
| `registry` | `memory/registry.md` | The memory-source map with its trust and gap annotations. | none |
| `deep_review` | `memory/deep_review.md` | The **latest** self-review only. Each review overwrites it in place, so no evolution timeline for it exists anywhere. | none |
| `reflections` | `logs/task_reflections.jsonl` | **Recorded execution history** of completed tasks — the process/"evolution" source. A *full row* carries `ts, task_id, task_type, goal, rounds, cost_usd, error_count, key_markers, review_evidence, reflection, backlog_candidates, memory_actions` and is summarised as `task reflection`. The canonical global log may instead hold a *pointer row* (`type: "project_reflection_pointer"`) whose full row lives in that project's own log; it is summarised as `project reflection pointer` and exposes `project_id`/`reflection_path`. The two are always distinguishable, and `reflection_path` is reported as recorded text — it is never followed as a filesystem capability. | itself |
| `project:<p>:reflections` | `projects/<p>/logs/task_reflections.jsonl` | The same recorded execution history, per project. | itself |
| `knowledge:<topic>` | `memory/knowledge/<topic>.md` | Authored knowledge topics. `patterns` is an ordinary topic whose evolution is `knowledge/patterns_history.jsonl`. | `knowledge_history.jsonl`, `knowledge_journal.jsonl`; plus `knowledge/patterns_history.jsonl` for `patterns` |
| `project:<p>:knowledge:<topic>` | project `knowledge/<topic>.md` | Per-project authored knowledge. | adjacent knowledge history/journal |
| `project:<p>:workpad` | project `workpad.md` | Per-project working document. | none established |
| `project:<p>:journal` | project `journal.jsonl` | Per-project activity journal. | that activity journal |

Every optional source above is listed only when `_safe_file` accepts it. A missing
file is simply absent from the catalogue — never an error and never a gap, because
"this installation does not have that file" is not a failure.

`catalog` items carry `compare_supported`. It is `false` for
`knowledge:improvement-backlog` and `true` for everything else: that document is
written by MERGE and keeps digest-only history, so two full retained versions of it
never exist and offering a version diff for it would promise a comparison this
reader cannot honestly produce.

## Excluded — and why, so "not shown" reads as a decision

| Excluded | Reason |
|---|---|
| `logs/chat.jsonl` | Raw chat. It is the **input** the dialogue consolidator reads; the consolidated blocks are the source, the transcript is not. |
| `archive/chat_*.jsonl` | Rotated raw chat, same reason. |
| `memory/owner_mailbox*` | Owner correspondence, not memory. |
| Control/queue state (task queues, run control, locks) | Live operational state, not memory, and reading it invites treating a viewer as a controller. |
| `settings.json` and other configuration | Configuration, not memory. |
| Secret-shaped names (`.env*`, `*credential*`, `*secret*`, `*token*`, `*auth*`, `*cookie*`, `*session*`, `*private_key*`, `*.pem/.key/.p12/.pfx`) | Credentials must never be served by a read-only viewer. |
| Tool and execution logs | Machine transcripts of tool calls, not memory. Task *reflections* are included instead: those are the recorded outcome of execution. |
| `memory/knowledge/index-full.md` (global and per project) | Derived: it is regenerated from the topics that are already listed, so serving it would duplicate them under a name that looks like a document. Reported as a `derived_excluded` gap. |
| Anything outside `D`, symlinks, non-regular files, traversal, ancestor swaps | Fail closed. |

All text and JSON fields are untrusted data.

## Record semantics

Records are discriminated on the writer's `type` field, with `kind`/`event` accepted
as legacy spellings. Identity rows expose complete `old_content` and `new_content` as
separate snapshot events unless `content_digested` is literally `true`; the string
`"false"` and the integer `1` are not that claim and never suppress retained content.
Digested rows expose only authentic `digest_preview`, `old_preview`, `new_preview`,
and SHA fields; previews never become snapshots. Knowledge shared stores match
`topic` exactly before legacy attribution fields and preserve complete old/new
snapshots. Other topics are simply unrelated, not gaps.

Scratchpad journal records follow the native block writer: an append carries the
stored text in nested `block.content` beside `content_len`/`source`/`metadata`; an
eviction carries the retired text in flat `evicted_block_content` beside
`evicted_block_ts`/`evicted_block_source`/`source_ref`; a failed append carries a
`block` that was never stored. The first two are exposed as **block** snapshots,
labelled `snapshot_scope: block`, alongside `scratchpad_blocks.json` entries. Failed
appends are activity, and a well-formed record whose `type` this reader does not know
is activity too — not a malformed record. The scratchpad document is never
reconstructed from block records, and no scratchpad record is called a full-document
version.

## Graph edges: provable only

There is no language model anywhere in this skill and none may be added. The graph
emits exactly four edge kinds, each carrying a `basis` string and a
`source_excerpt`:

Both link kinds accept the same two written forms — the exact, whole-string `id`
of a catalog source, or a path that lexically resolves to an allowlisted catalog
source path — so the reader and the Links tab cannot disagree about the same
document. The resolved path does **not** have to end in `.md`: the catalogue
holds JSON and NDJSON sources (`dialogue`, `reflections`,
`project:<p>:journal`, `project:<p>:reflections`) and an authored link to one of
them is a real edge. The id form is whole-string equality against a discovered
id, never a prefix or fuzzy match, and applies only to text the author actually
wrote inside a link.

- `markdown_link` — an inline `[label](target)` in the focus document written in
  one of those two forms: `[notes](knowledge:tooling)`, `[see](patterns.md)`,
  `[chronicle](../dialogue_blocks.json)`.
- `wiki_link` — a `[[…]]` in the focus document written in one of those two
  forms (`[[knowledge:patterns]]`, `[[knowledge/tooling.md]]`), plus the
  wiki-only convention that a bare name may mean the sibling `.md` file
  (`[[tooling]]`).
- `journal_source_ref` — recorded provenance from a journal row's
  `source_ref.read.arguments.path`, mapped onto an allowlisted source path relative
  to `D`. An `entry_id` is shown in the excerpt when present, but an entry id alone
  never creates an edge.
- `shared_task_id` — an equal, non-empty `task_id` recorded on both sides. The basis
  is the literal `shared task_id <id>` so a reader can check the claim.

Link and `source_ref` targets are resolved **lexically only** and are never opened:
a written reference can never become a filesystem capability. Mentioning a source ID
or a topic word in prose is not a link and produces no edge. Structural relations —
knowledge-index membership, same-project membership, history-row provenance — are
not authored links and are not edges. A focus document with nothing to point at
returns an empty edge list; that is the correct answer.

## Bounds and honesty

Every open is read-only and no cache, lock, repair, migration, or touch occurs in
`D`. Descriptor and path stats are checked around reads; cursors/revisions reject
drift with 409. Missing history is `unavailable` with an explicit gap, while a source
with no established history remains `none`.

Bounds: 2,000 total discovery entries/sources per request; 4 MiB/file; 10,000
history records/file; JSONL line 64 KiB; search 200 files/8 MiB; graph 60
history/journal files per request and 8 MiB, each store read at most once and then
attributed per source — including when the focus source *is* its own history
store (`reflections`, `project:<p>:reflections`, `project:<p>:journal`), where
the bytes read for the link scan seed the record cache instead of being read and
charged a second time; dialogue block content 8,192 bytes trimmed back to a UTF-8
character boundary with `truncated: true`; document/event pages 1–65,536 bytes; and
serialized JSON 256 KiB. A record carrying a value the response serializer cannot write —
a lone UTF-16 surrogate, or a non-finite number such as `NaN`/`Infinity`, both
of which `json.loads` accepts — is reported as a `malformed_record` gap rather
than being allowed to break the whole response. Unreadable, invalid, excluded,
oversized, or bounded-away material is reported in `gaps` where a safe partial
response exists — a truncated
graph scan reports `{"scope":"graph","reason":"scan_limit"}`. Malformed UTF-8 is
rejected, never replacement-decoded and falsely marked complete: a source whose
bytes are not valid UTF-8 gets no revision digest at all and is catalogued with
`revision: null`, `read_error: invalid_utf8`, and a matching gap.
