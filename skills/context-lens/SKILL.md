---
name: context-lens
description: Read-only chart of model-request input sizes from the local usage store, with honest freshness and coverage.
version: 1.2.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [route, widget]
timeout_sec: 60
when_to_use: The owner wants to understand reported input sizes, their spread and which requests belong to one task.
model_experience:
  what_model_sees: No tool schema and no WebSocket handler, because this skill registers neither; it adds two owner-facing HTTP routes and one Widgets card. What can reach a model's context is the small manifest metadata every installed skill carries — this name and this one-line description — wherever the host lists installed skills.
  token_effect: >-
    Constant and small: the manifest metadata only. The skill never calls a model and adds nothing per request or per round.
ui_tab:
  tab_id: lens
  title: Context Lens
  icon: "◉"
  render:
    kind: module
    entry: widget.js
    appearance: host
    start: auto
    span: 2
---

# Context Lens

A Widgets card that answers one question about this install: **how large were
the inputs of recent model requests, how much do they vary, and how much of the
record does the answer actually cover?** It never calls a model, never touches
the network, never writes anything and never shows prompt text.

## What the card shows

* **One status line**: the source (the usage store, or the retired journal
  labelled historical), when it was read, and how long before that read the
  newest record was written. An old newest record can simply mean nothing ran,
  so freshness is stated, never inferred from it.
* **Horizon** (1h / 6h / 24h / 7d / All, default 24h) and native **filters** for
  model, work kind, origin and mode. Options come from the data; *Unknown* mode
  is always offered, and *Nano* appears when recorded.
* **One coverage line**: requests in view, how many have a measured input, and —
  only when it is so — that the selection is not the whole span (row cap,
  retained journal tail, a read bound), plus unplaceable rows and the point cap.
* **The chart** (260 px; 220 below 640 px, 200 below 400 px): one dot per
  physical request that finished with a reported input size, against the time
  usage was recorded or last updated. Median and p95 of exactly the dots in view
  are quiet reference rules. Hover shows the request; click, or Arrow keys on
  the focused chart, selects one.
* **Selection**: the request's size, model, work kind, mode and time, and the
  other measured requests of **the same task highlighted** on the same chart —
  derived from the answer already held, with the same filters, never joined by a
  line. A *Technical detail* disclosure holds the recorded internals.
* **Requests** (closed): the keyboard-reachable list of everything in view,
  newest first, six rows then *Show more* in steps of twelve.
* **About this data** (closed): source, selection, counters with *registered*
  and *sent* kept apart, what is counted but never drawn, how to read the
  chart, and the bounds.

Empty, error and stale states are distinct: a first read that fails shows the
typed reason and *Try again*; a failed refresh keeps the last answer on screen,
labelled with its read time and the failure; a span with no requests says so
with the newest record and offers *Show everything*; a view emptied by filters
offers *Clear filters*; requests without any measured size are named as such.

## What it deliberately does not show

No alarm thresholds, no cost, no "context is X % full", no per-document or
per-section contribution, no claim that a request caused or followed a
compaction, and no line through requests. A drop in size is consistent with
compaction, a shorter round, another model or a branch of work ending alike;
the record holds no fact that tells them apart. One task can run parallel
review slots, providers and sources, so a shared task, model and work kind does
not prove one growing context.

---

## Source of truth

Paths are fixed relative to `PluginAPI.get_runtime_info()["data_dir"]`; no route
takes a path, and none is ever sent to the browser.

1. **`state/usage.sqlite`** — core's usage store (`ouroboros/usage_store.py`,
   `docs/USAGE_STORE.md`, schema 1). One row per physical attempt, UPDATEd in
   place on every transition.
2. **`state/usage_attempts.jsonl`** — the retired journal, read **only when no
   file exists at the store's name**, and then labelled historical
   (`source.current = false`). Core stopped appending to it at the one-time
   import. A store that exists but is busy, corrupt, unsupported or refused is
   reported as such and is **never** replaced by journal data.

### How the store is read (`lens_store.py`)

* **Never with `open()`.** Closing any descriptor on a SQLite file drops every
  POSIX lock the process holds on it, and this skill may share a process with
  core's own store connections. The path is checked with `lstat` only: the data
  directory and `state` are real directories, the store a regular file, with
  symlinks rejected at those three checked paths (`store_not_confined` /
  `store_not_regular`). Ancestors of the data directory are not checked. A `-wal`
  sibling is refused before anything opens the store.
* **Strictly read-only.** SQLite URI `mode=ro`, `PRAGMA query_only = ON`, a
  0.25 s busy wait, one deferred read transaction. Never `immutable`, never
  `nolock`, never `usage_store.read()` / `hold()` / `read_usage_records()`,
  init, migration or any write.
* **Validated first.** `PRAGMA application_id` is checked before anything else:
  the *name* lock tier (`0x4F55534E`) is refused as `store_name_tier`, because
  every access there must run inside core's name-protocol money lock, which a
  read-only widget cannot take. Then: rollback-journal mode, the `attempts`,
  `summaries`, `meta` tables and the `attempts_category_time (category,
  ts_last_epoch)` index, `meta.schema_version = 1`, `meta.lock_tier =
  "enforced"`, `meta.import.status = "completed"` (`store_not_ready`), and the
  columns read.
* **Bounded and index-ordered.** A global `ORDER BY ts_last_epoch` is a full
  scan plus a temporary B-tree, so category values are taken from `summaries`
  (scope `category`, plus separate NULL and empty streams) and each category is
  read newest-first over the index; the streams are merged until **4 000 rows in
  total** (row 4 001 marks the selection partial). Of the `extra` metadata only
  `physical_context` is extracted (in SQLite when its JSON functions exist, else
  from bounded text after the transaction); nothing else in `extra` is read.
* **Enumeration scope.** Core summarises physical-attempt categories, but files
  legacy imports as unattributed instead of creating category summaries for
  them. A legacy-only named category can therefore be absent from the streams.
  `source.category_enumeration = "summary_keys_plus_null_and_empty"` and
  `source.legacy_category_coverage = "not_guaranteed"` disclose this without a
  new index or a history scan. Counts (including excluded and untimed rows) and
  `newest_record_ms` refer only to these streams; missing legacy rows are not
  estimated. Legacy rows in shared categories, NULL or empty categories can
  still be read and counted.
* **Bounds** — rows 4 000; category streams 64 (+2); metadata 16 384 chars per
  row and 8 Mi characters per read; merge 0.4 s / 40 M SQLite steps (the merge stops and
  the selection is the exact newest prefix of the enumerated streams, marked
  partial); whole read 2 s
  (`store_slow`). Every bound that bites is named in `horizon.partial_reasons`.
* **Released before any projection.** The transaction ends, then metadata is
  parsed and statistics are built. The read reports how long it held the shared
  lock (`source.transaction_ms`). The path's identity is checked again after
  the transaction; a store replaced meanwhile is `store_replaced`.

Every read is a fresh selection — there is no append order to resume from, and
an updated row (a late receipt, a price refinement) simply moves; it is never
counted twice.

### Counting rules

* **Points** are `kind = attempt` rows in state `settled` with a reported
  `prompt_tokens` (an explicit `0` is a measurement) and an admitted timestamp
  (2010–2100). Token counts are read only from a settled row; `NULL` stays
  missing, never zero. Among the rows read, session totals, compaction aggregates (whose `weight` is
  their folded count), external dispatches, legacy rows and unknown kinds are
  counted under `counters.excluded`, never drawn.
* **Registered vs sent.** `registered_attempts` counts every attempt in a known
  state; `sent_attempts` only `dispatched` + `settled` + `unresolved`. Reserved
  and released requests were not sent.
* **Time** is `ts_last` — when usage was recorded or last updated. A late
  receipt or price refinement moves it, so it is neither send time nor latency,
  and no elapsed time is derived.
* **Cache** counts are passed on as reported and never added to the input or
  turned into a share: for some providers they are part of the input number,
  for others that is not established.
* **Mode** is `physical_context.rendered_mode` (`max` / `low` / `nano`); absent
  or unreadable metadata is Unknown (`source.context_unread` counts the latter).
* **Selection facts are separate**: `source.read_at_ms` (read instant = anchor),
  `source.newest_record_ms`, `horizon.selection_complete` /
  `partial_reasons` / `covered_from_ms`, the event range
  (`selected_from_ms` … `selected_to_ms`), `unknown_timestamp` (rows that no
  horizon can place), `points_omitted` (the point cap) and, per view, measured
  vs total in the widget.
* For the store, `horizon.selection_scope = "enumerated_categories"` qualifies
  `selection_complete`: every timestamp-eligible row of the span in those
  streams was read, with no bound reached. Core-maintained summaries enumerate
  physical-attempt categories, so a complete selection covers eligible physical
  attempts; this does **not** prove that all store records or legacy rows were
  read. A category cap also prevents a completeness claim for physical attempts.
  The point cap and missing measurements remain separate from read completeness.
* The journal path keeps its original reader and invariants (descriptor-
  confined open, 4 MiB tail per refresh, 5 000 cached records, torn-tail and
  rotation handling, a fold counted once from its header or its groups). Platforms
  without descriptor-relative open support receive `ledger_platform_unsupported`,
  not a false claim that their journal path is outside the data directory; its
  completeness means "every retained journal row of the span was read"
  (`reaches_cutoff`), and the source line says it is historical.

## Routes

| Route | Parameters | Answer |
|---|---|---|
| `GET /api/extensions/context-lens/data` | `horizon` (`1h`/`6h`/`24h`/`7d`/`available`; anything else → `available`), `limit` (points, 1…4 000, default 1 500), `refresh` (journal only: cold re-read) | `source`, `snapshot.id`, `horizon` selection facts, `counters`, `facets`, `points`, `points_omitted`, `limits` (and `window` for the journal) |
| `GET /api/extensions/context-lens/trajectory` | `task` (an opaque key this skill minted), `snapshot` (an id from `/data`) or `horizon` | that task's measured points from **one** snapshot, grouped by model and work kind, other tasks of the same tree listed apart; `joined: false` everywhere, `own_outside_horizon: null` (not counted) |

The widget derives task focus from `/data` itself; the trajectory route keeps
its URL and task-grouping purpose. With `snapshot` it answers from that snapshot's points or
says `snapshot_expired` (the last two are held); without it, it takes a fresh
read and states that read's own snapshot id and anchor. A key or snapshot id
that this skill did not mint is answered empty and never reflected.

### 1.2 interface changes from 1.1.3

The route URLs remain, but **the response shape is not backward compatible**.
Consumers of either route must use the 1.2 fields and their stated scope:

* `counters.physical_attempts` is replaced by `counters.registered_attempts`;
  `counters.sent_attempts` separately counts requests sent to a provider.
* `horizon.covers_selected_span` is replaced by `horizon.reaches_cutoff`, a
  journal-retention fact; it is `null` for the store. Store selection coverage
  uses `selection_complete`, `selection_scope` and `partial_reasons` above.
* Point fields `elapsed_sec`, `states` and `seq`, and the `in_flight` counter,
  are removed. A point has its current `state`; counters expose `by_state`.
  No elapsed duration, transition history or journal ordering is reconstructed
  from the store's latest accounting write, and no replacement zero is invented.

The journal reader retains its internal folding and retention behavior; this
does not preserve the old HTTP response shape.

What reaches the browser is only the allowlist in `lens_core._point()`:
attempt and task ids become `a-`/`t-`/`r-`/`p-` digests; labels pass a strict
charset (else `other`); money, credential profiles, session digests, review
slots, failure evidence, route fingerprints and every other `extra` field are
unreachable. Failures answer a typed `reason` and an owner-readable sentence —
no path, exception text or row. A `get_runtime_info()` that raises is
`runtime_info_unavailable` (logged by exception type only), never
`no_data_dir`.

## Widget

`widget.js` is one dependency-free classic script drawing to a `<canvas>` in
the host's opaque-origin frame. It uses `OuroborosWidget.fetch` only, against
its own prefix, every request with an `AbortController`, `timeoutMs` and a
generation guard: a superseded or late answer can never overwrite a newer
horizon. No storage, no `postMessage`, no WebSocket, no subscriptions.

* **Theme** (first shipped in Hub 1.1.3; kept and extended): `appearance: host`
  in the manifest and the registration; `OuroborosWidget.onTheme` sets
  `data-theme`, rereads the named `--lens-*` tokens (dark and light values from
  `web/ui.css`, with `color-scheme`) and repaints the canvas — no remount, no
  control rebuild, disclosures and focus untouched. Older hosts keep the dark
  palette. The outer background is transparent so the host card shows through.
* **Height follows content**: no `render.height`, so the host's auto-height
  bridge measures `#root`. The card keeps that loop-free — the chart box has a
  fixed CSS height, nothing is sized from the viewport, and the widget's own
  `ResizeObserver` only schedules a canvas repaint when the box really changed
  size. (1.1.x pinned 760 px to sidestep a WebKit observer loop; that pin is
  gone.)
* Type is 12 / 13 (controls) / 14 / 16 px; one focus ring; lays out from 360 px
  with no horizontal overflow.
* Status changes repaint two nodes in place; a rebuild waits while a native
  dropdown is focused. A successful answer stays pending until that rebuild,
  so the displayed read time, chart, counters and theme repaints describe the
  same answer. A new horizon discards the old pending answer. Focus (`data-focus` keys), the selection, paging and
  open disclosures survive every refresh. Refresh is never disabled.
* A 60 s poll runs only while the page is visible, and returning to a page
  hidden longer than that reads at once. `__ouroWidgetOnDispose` clears timers,
  frames, observers, listeners, request timers, aborts reads and unsubscribes
  the theme.

## Limits worth knowing

* A selection holds at most 4 000 rows; a busy install's 24 h can exceed that,
  and the card then says it shows only the newest rows and from when. The soft
  wall-time budget can stop a read earlier under load: a longer selected period
  can therefore return fewer rows than a shorter one. Refresh takes a new
  bounded snapshot; neither selection promises complete retained history.
* Native Windows and PyWebView have not been exercised for this release.
  The SQLite file-URI spelling on Windows remains unverified.
* The name lock tier and WAL-mode stores are refused, not read around. SQLite
  itself would create WAL side files when opening a WAL store; the `-wal` check
  catches a live one first, and core never runs the store in WAL mode.
* The path checks use `lstat` and SQLite opens by name, so a symlink swapped in
  between and back by a process that can already write the data directory is
  not excluded; the post-read identity check catches a replaced store.
* Legacy-only named categories may be absent from enumeration. About and the
  response's scope fields disclose that legacy counts and newest-record time
  need not cover all store rows.

## Tests

```
python3 -m unittest discover -s tests
```

Standard-library `unittest` only; every fixture is written into a fresh
temporary directory and nothing reads a real install. `tests/store_fixture.py`
builds a store with core's schema 1 column for column and mirrors category-summary
membership (legacy kinds do not create category summaries); it does not maintain
accounting totals or the other summary scopes.

* `test_lens_store.py` — selection and exact newest prefix across streams, NULL
  and empty categories, missing legacy-only named categories and scoped
  completeness, inclusive cutoff, unplaceable timestamps, category /
  row / byte / step / time / hard bounds, `EXPLAIN QUERY PLAN` (index range in
  index order, no temporary B-tree) and work that does not grow with history;
  settled-only tokens, NULL vs 0, cache pass-through, Max/Low/Nano/Unknown, an
  UPDATE in place and a late refinement counted once, excluded kinds and
  weights; `extra` allowlisting and the SQLite-JSON vs Python fallback;
  byte-identical store and directory after reads, `mode=ro` / `query_only` /
  never `immutable`, the lock released before return, busy writers, name tier
  refused from the header alone, unsupported schema / tier / index / column /
  WAL, unfinished import, corrupt file, symlinks, and a store replaced mid-read.
* `test_lens_core.py` — the journal reader's original invariants (bounds,
  rotation, torn tails, descriptor confinement, fold counting, redaction,
  horizons) plus the shared projection and the trajectory route.
* `test_plugin_routes.py` — real handlers through a fake `PluginAPI` that fails
  on any undeclared permission: registration and manifest agreement, store
  first, journal only when no store exists, no fallback over a broken store,
  typed busy, snapshot-bound and expired trajectories, no leakage; with
  Starlette importable, the same handlers through a real `Request`.
* `test_widget_contract.py` — the widget source against these promises; with a
  working Node (`CONTEXT_LENS_NODE` or `node` on `PATH`) it also runs
  `node --check` and `tests/widget_smoke.cjs`, which executes the widget in a VM
  against a DOM stub and a recording canvas: first paint, loaded answer, no
  joining lines, paging, task focus without a fetch, filters, keyboard, a stale
  refresh while a SELECT is focused (distinct answers, theme repaint, subsequent
  failure and superseding horizon), theme repaint without rebuild, superseded
  horizons, empty / filter / unmeasured / error states, narrow-width and all-zero
  label collisions, the hidden poll and
  disposal. It is not a browser; real layout is checked in the Widgets page.
  The harness is `.cjs` so the host does not serve it with the module bundle.
