---
name: context-lens
description: Read-only context-size charts for model requests over a chosen horizon, with honest telemetry coverage.
version: 1.1.2
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [route, widget]
timeout_sec: 60
when_to_use: The owner wants to understand reported input sizes, their spread and growth within tasks.
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
    start: auto
    span: 2
    height: 760
---

# Context Lens

A Widgets card that answers one question about this install: **how large were
the inputs of recent model requests, how much do they vary, and where does the
record stop being able to tell you?**

It reads one fixed local file, shows what that file actually contains, and says
plainly where the record runs out. It never calls a model, never touches the
network, never writes anything, and never shows prompt text.

## What it shows

* **A horizon selector — 1h / 6h / 24h / 7d / Available.** The span is measured
  back from an explicit UTC anchor taken when the answer is built, and the cut
  is applied on the server, to the **retained records, before the display
  limit** — so the chart, the four figures, the filter options, the list and the
  coverage line all describe one and the same selection. Under the control, the
  card says what the horizon *actually* covered: the anchor, and either "the
  whole span is inside the read window" or the shorter range that was really
  observed. **A selected period is never presented as delivered history**: this
  reader keeps a bounded tail of one file, so `Available` means "everything
  still in the read window", not "all history", and a 7-day request over a
  two-hour tail says exactly that.
* **A scatter of the selected requests.** One dot is one physical model request that
  finished and reported an input size. The dots are deliberately **not joined**:
  two neighbouring requests can belong to completely unrelated tasks, so a line
  through them would draw a trend that never happened.
* **Typical input (median), 95% below (p95), peak and data coverage**, all four
  recomputed from exactly the points the chart is drawing — so a tile never
  describes a different population than the graph beside it. The median and the
  p95 are also drawn as reference rules, so spread is visible rather than
  implied. Each tile carries the precise definition as its tooltip.
* **Filters** for model (`row.model`), work kind (`row.category`), origin
  (`row.source`) and mode. Every option comes from the data in view; nothing is
  a hard-coded taxonomy. The one exception is **Unknown** under mode, which is
  always offered: "no context-fit measurement was recorded" is a real answer
  about this install, not an artefact of what happens to be in view.
* **A keyboard-reachable list** of requests carrying the same facts as the dots.
  It opens compact — six rows — and pages to **every** request matching the
  filter through Show more / Show less, inside a bounded scroll area. The chart
  says so in its accessible name rather than claiming the list already holds
  everything.
* **A detail panel** for one request: its reported input, model and mode first,
  then that task's **trajectory**, then the recorded internals behind a
  *Technical detail* disclosure. The trajectory groups the task's requests by
  model and work kind; a line joins points **only inside one such group**.
  Requests belonging to other tasks in the same tree — children, review slots —
  are drawn as loose points and are never joined into the same line. The
  trajectory carries the **same horizon** as the overview and says how many of
  that task's own measured requests fall outside it, because "this task did not
  grow" and "the growth is before the cutoff" are different answers.

The card is one screen — a fixed 760 px frame that scrolls itself. Every
explanation — *How to read this*, *Coverage details*, *Technical detail* — is
one disclosure away rather than unrolled by default, and the graph keeps the
space.

## What it deliberately does not show

No arbitrary alarm thresholds, no "context is X % full", no cost, no per-document
contribution, and no claim that a particular request caused compaction. Each of
those would require a fact the ledger does not hold; see "Exact gaps" below.

---

## Source of truth

Everything comes from one file:

```
<PluginAPI.get_runtime_info()["data_dir"]>/state/usage_attempts.jsonl
```

That relative path is fixed by the core substrate — `ouroboros/usage_ledger.py`
declares `LEDGER_REL = pathlib.Path("state/usage_attempts.jsonl")`. `plugin.py`
holds no path parameter of any kind, so no request can steer a read anywhere
else. The archive segments under `archive/usage_ledger/`, the quarantine file,
observability artifacts, settings and credential stores are all **never opened**.

The file is opened `"rb"` and only ever read. Nothing writes, renames,
truncates, locks or archives it.

### Row facts this skill depends on

| Fact | Where it is fixed in the system repo |
|---|---|
| One JSON object per line with a dense 1-based `seq` and an ISO `ts` | `usage_ledger._append_rows_locked` |
| Kinds: `attempt`, `external_unmetered`, `subscription_session`, `legacy_metadata`, `legacy_delta`, `usage_baseline`, `usage_baseline_group` | `usage_ledger._validate_records` |
| An attempt is a chain of rows sharing `attempt_id`: `reserved` → `dispatched` → `settled` / `unresolved` / `released` | `usage_ledger._validate_records` |
| Every later row re-carries `model`, `provider`, `task_id`, `root_task_id`, `parent_task_id`, `category`, `source`, `physical_context` | `usage_accounting._transition` |
| Token counts exist **only** on the terminal `settled` row — and *only* a `settled` row is read for them, in both directions (see "The settled row is the only token authority") | `usage_accounting.settle_attempt` |
| `prompt_tokens` is the provider-reported input count, and for Anthropic-native responses it **already includes** `cache_read_input_tokens` + `cache_creation_input_tokens` | `_usage_response.usage_from_response` |
| A reported count that is absent stays `None`, distinct from an explicit `0` | `_usage_response._reported_token_count` |
| `physical_context.rendered_mode` is `"max"` or `"low"`, written from the round's context-fit measurement | `usage_accounting.PhysicalAttemptContext`, `loop_model_call._physical_context_for_fit` |
| Compaction rewrites the whole file with a leading `usage_baseline` header plus `usage_baseline_group` rows whose token fields are **sums over many folded attempts** | `usage_compaction` (`_group_key`, group-row builder) |

### Counting rules that follow from those facts

* **`cached_tokens` is a subset of `prompt_tokens`, never an addend.** The
  widget shows it as "of which cache reads" and adds nothing. Adding it again
  would double-count the same tokens.
* **A horizon selects records, and says what it could not reach.** The cut uses
  the attempt's **settled timestamp** — the `ts` of its terminal row, the same
  instant a point is drawn at — against an explicit UTC anchor published as
  `horizon.now_ms`, with the boundary at `now_ms - span` published as
  `horizon.cutoff_ms`. A record whose timestamp is absent, unparsable or outside
  the admitted band cannot be placed inside or outside a span, so a bounded
  horizon leaves it out and counts it under `horizon.unknown_timestamp` rather
  than keeping or dropping it silently; `available` keeps it and it stays
  undrawable. `horizon.covers_selected_span` is true **only** when the oldest
  retained record is at or before the cutoff — eviction and the cold-tail read
  both drop the oldest records first, so what is retained is a contiguous suffix
  and that single comparison decides it. When it is false the widget shows the
  observed range instead of the requested one and says so in words.
* **Missing is not zero.** A settled request that reported no size is counted
  under "finished without one" and is not drawn.
* **The settled row is the only token authority.** `usage_accounting` writes
  `prompt_tokens`, `completion_tokens`, `cached_tokens` and `cache_write_tokens`
  on the `settled` row alone, but `usage_ledger._validate_records` permits those
  fields *structurally* on any row — so a corrupt or future chain could carry a
  token count on a `reserved`/`dispatched` row and omit it on the settled row.
  `lens_core._absorb` therefore reads those four fields **only** from a row whose
  `state` is `settled`, and on that row assigns **all four**, `None` included.
  A count on a pre-terminal row is ignored, and a terminal *absence* clears any
  earlier value rather than letting it survive — so a request is reported as
  measured only when its own settled row stated a size.
* **A reserved request carries no token estimate.** The reserved row records a
  monetary upper bound, not a token count, so nothing is estimated for
  in-flight work — it is reported as in flight and left out of every statistic.
* **Aggregates are never points.** `usage_baseline` / `usage_baseline_group`
  rows are folded summaries of many attempts; a group row's `prompt_tokens` is
  a sum. They are excluded from the chart and reported separately, together
  with the compaction epoch and the number of attempts folded away.
* **A fold is counted once, not twice.** One compaction writes a
  `usage_baseline` header whose `folded_attempt_count` is the total for that
  fold, plus `usage_baseline_group` rows whose counts *partition the same
  attempts*. Adding the two would report every folded attempt twice, so each
  fold is counted from its header when the header is in the window and from its
  group rows only when it is not. Which basis was used is disclosed:
  `folded_attempts_from_groups` is a lower bound, because a window that lost the
  header may have lost group rows as well.
* **A row that cannot be placed is not a request.** A row whose `kind` is not a
  known kind string — including a non-string `kind` from a corrupt line — is
  counted as `unknown_kind`, never defaulted to `attempt`. A row that looks like
  an attempt but carries no recognised `state` is counted as
  `attempts_without_state` and is left out of the points, the facets, the
  statistics and every trajectory.
* **An impossible number is missing, not wrong.** A negative count, a count
  wider than 2**53 (which cannot cross into a browser `Number` unchanged) and a
  timestamp that will not parse or falls outside 2010–2100 are all reported as
  absent. A corrupt line degrades one field; it never raises out of a route.
* **A harness subscription session total is not a physical request.** A
  `subscription_session` row is one aggregate for a whole delegated session.
  It is excluded from the chart and from every statistic, and the widget says
  so in plain words when such rows exist.
* **Legacy imports and external unmetered dispatches** are likewise excluded
  and counted on their own lines.
* An unrecognised future `kind` is excluded and counted as `unknown_kind`
  rather than guessed into the chart.

---

## Exact gaps in the telemetry

These are properties of the record, verified in the system repo, not of this
widget. They are stated in the widget itself as well.

1. **No compaction attribution.** The ledger has no field that ties an attempt
   to a context-compaction pass. `category` and `source` come from the ambient
   `UsageScope` (`usage_accounting._merge_scope`), so a compaction summarizer
   call made inside a task inherits that task's scope. The compaction path's own
   discriminator is `call_type="context_compaction_map" / "_fold"`
   (`context_compaction._call_summarizer`), and that value is written to
   observability artifacts, **not** to `usage_attempts.jsonl`. Evidence about a
   compaction may therefore exist elsewhere in this install; it is simply not
   available from this ledger, and the two records share no key this skill could
   match on — so no such join is attempted or implied anywhere. A plateau or a
   drop in a task's request sizes is
   consistent with compaction, but equally with a shorter round, a model
   change, a branch of work ending, or a request that reported nothing.
   Context Lens says this and never claims a cause.
2. **No token estimate for in-flight attempts.** As above — the reserved row
   has no token field at all.
3. **No document or context-section contribution.** The ledger records one
   total input count per request. Nothing in it attributes that total to
   individual documents, tool results or context sections.
4. **No window-fill percentage.** `physical_context.capacity_total_tokens` and
   `target_total_tokens` are exact figures *recorded with that one request*,
   so the detail panel shows them as recorded numbers. No percentage is
   derived from them: the route and its capacity can differ between requests,
   and the ledger holds no historical proof that a given capacity applied to
   any request other than the one it is written on.
5. **Mode is often genuinely unknown.** `physical_context` is attached only
   where the round had a matching context-fit plan
   (`loop_model_call._measure_round_main_fit` returns `None` otherwise), so
   many rows carry no mode. Those appear under an explicit **Unknown** option,
   never bucketed into Max or Low.
6. **History before a compaction is unreachable here.** Folded attempts move
   into `archive/usage_ledger/`, which this skill does not read.
7. **The reserved→settled elapsed time is not provider latency.** It is the
   wall-clock gap between two ledger rows and includes queuing.
8. **A horizon cannot extend the record.** Selecting 7 days does not read more
   of the file: the reader holds at most a 4 MiB tail and 5000 attempt records,
   and anything compacted away is in an archive this skill does not open. A
   horizon therefore selects *within* what is retained, and the card states the
   range it truly observed rather than the range that was asked for.

---

## Backend

Two GET routes, both under this skill's own namespace, both parameterless
except for bounded scalars:

| Route | Parameters | Answer |
|---|---|---|
| `GET /api/extensions/context-lens/data` | `limit` (1…5000), `refresh` (forces a cold tail read), `horizon` (`1h`/`6h`/`24h`/`7d`/`available`) | window facts, the horizon block, exact counters, filter facets, sanitized points |
| `GET /api/extensions/context-lens/trajectory` | `task` (an opaque key this skill minted, `t-` + 12 hex), `horizon` | that task's groups plus loose related groups, cut by the same horizon |

`horizon` is normalized against that fixed set of tokens: an unknown value falls
back to `available` instead of being trusted or reflected, and every answer
states the horizon it actually applied, its anchor, its cutoff and how much of
the span the read window could really cover.

The `task` parameter is validated against the exact shape `lens_core._opaque()`
produces. Anything else — a longer string, a root key, arbitrary text — is
answered with the empty trajectory, and the supplied text is **never reflected
back** in the response.

No tool, no WebSocket handler, no settings section, no companion process, no
scheduled task, no dependency. `permissions: [route, widget]` is the whole
surface.

### Bounds

| Bound | Value | Where |
|---|---|---|
| Bytes read per refresh | ≤ 4 MiB | `lens_core.MAX_BYTES_PER_REFRESH` |
| Cached attempt records | ≤ 5000 | `lens_core.MAX_RECORDS` |
| Points returned per request | ≤ 5000, default 1500 | `lens_core.DEFAULT_POINT_LIMIT` |
| Label length admitted | ≤ 80 chars, restricted charset | `lens_core.MAX_LABEL_LEN` |
| Count admitted | 0 … 2**53−1 | `lens_core.MAX_SAFE_COUNT` |
| Timestamp admitted | 2010-01-01 … 2100-01-01 | `lens_core.MIN_EPOCH_MS` / `MAX_EPOCH_MS` |

Reading is incremental: the reader keeps a byte offset and an `(st_dev, st_ino)`
fingerprint and consumes only new bytes. Counters are derived from the deduped
record map keyed by `attempt_id`, so re-reading or re-folding a chain cannot
inflate any number.

### How the one file is opened

The path is fixed, and each refresh proves it is still the same fixed file
before trusting the byte offset it holds:

* `os.stat` first, then the open, then `os.fstat` on the **opened descriptor**.
  Every size and identity decision comes from that `fstat`. If the two identities
  differ, compaction replaced the path in between: nothing is read from that
  handle, the rotation is counted, and the retry starts cold. Two generations of
  the ledger can therefore never be spliced into one window.
* **Confinement is by descriptor, not by path.** `O_NOFOLLOW` only ever protects
  the *final* component, so resolving the parents by name and then opening by
  name is a race: between the check and the open, another process can swap
  `state` for a symlink into another tree and the descriptor lands outside the
  data directory while every check still passes. Instead, each component is
  opened by name **relative to a descriptor already held** — the data directory
  (`O_RDONLY|O_DIRECTORY|O_NOFOLLOW`), then `state` (same flags, via `dir_fd`),
  then `usage_attempts.jsonl` (`O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, via `dir_fd`).
  A parent renamed after its descriptor was taken cannot move that descriptor.
  A symlink at any component is refused as `ledger_not_confined`; so is a
  symlinked data directory itself. The directory descriptors are closed in a
  `finally`, and the `os.stat` above is only a rotation trip-wire — nothing is
  read on its strength.
* If the platform has no `dir_fd` support (`os.open not in os.supports_dir_fd`)
  the read **fails closed** with `ledger_not_confined`. There is no by-name
  fallback, because the fallback is the race.
* The opened descriptor must be a regular file (`ledger_not_regular`), decided —
  like every size and identity fact — from `os.fstat` on that descriptor.
* Projections (`records()`, `snapshot()`) copy every record, including its
  nested state list and fit mapping, **while still holding the lock**. A refresh
  running on another thread cannot mutate a payload that is already being
  projected.

Every bound that bites is disclosed in the widget's window line rather than
being silently applied:

* a cold tail read reports `omitted_prefix_bytes` — the older part of the file
  it did not read;
* cache eviction reports `evicted_records`;
* the point cap reports `points_omitted`, and it applies **after** the horizon
  cut, never instead of it;
* the horizon reports its anchor, cutoff, observed range,
  `records_selected` of `records_retained`, `excluded_older_than_cutoff`,
  `unknown_timestamp` and `covers_selected_span`;
* unreadable lines report `malformed_lines`;
* an unterminated final line reports `pending_tail_bytes` and is left on disk
  for the next refresh, so a torn or still-being-written tail costs nothing.
  This holds at the cold-tail boundary too: when a bounded tail read lands
  inside a line and finds no terminator at all, the offset is **held** and the
  bytes are reported as `pending_tail_bytes` — the fragment is never handed to
  the parser and never counted malformed;
* **the one discard.** Retrying cannot help a line at least as long as the whole
  per-refresh cap (4 MiB), because such a line can never be read whole in one
  refresh, so waiting for it would stall the reader forever. That line is
  skipped: its bytes are added to `omitted_prefix_bytes`, it is counted once in
  `discarded_oversize_lines`, and the reader keeps skipping to the next newline
  on later refreshes so no piece of it is ever parsed as a row. This is the only
  case in which the reader drops data it could otherwise have read, and it is
  always counted;
* a replaced or truncated ledger (compaction rewrites the file onto a new
  inode) is detected, re-read from scratch, and reported as
  `rotations_observed`.

### What the browser receives

Only the allowlist built by `lens_core._point()`. Concretely:

* Internal task ids **never leave the process**. They are replaced by a
  12-hex-character digest with a `t-` / `r-` / `p-` prefix, which keeps grouping
  exact while disclosing nothing about the task.
* The `attempt_id` never leaves either. A point's `id` is `a-` plus a 16-hex
  digest of the **whole** attempt id, so selection stays exact: a raw prefix is
  neither sanitized nor collision-safe, and two ids sharing a prefix would
  otherwise become one selectable request.
* Model, provider, category and source pass a strict charset and length check;
  anything else becomes the literal `"other"`.
* Money, candidate hashes, review-slot attribution, budget limits, cache-TTL
  and route fingerprints are not in the allowlist and cannot be reached.
* Failures answer with a typed code and an owner-readable sentence. No path,
  no exception text, no ledger line, no traceback.
* Each code says only what is actually known. In particular, when
  `PluginAPI.get_runtime_info()` itself raises, registration still completes but
  the routes answer `runtime_info_unavailable` — **not** `no_data_dir`, which
  would assert that the host reported no data directory when the truth is that
  the host did not answer at all. The failure is not swallowed: it is written to
  the host log at `warning` (no permission needed), naming only the exception
  **type**, because an exception message can carry a path or a credential and
  this skill discloses neither. A later successful registration clears it.

## Widget

`widget.js` is a single dependency-free classic script drawing to a `<canvas>`.
It runs in the host's opaque-origin sandboxed frame, so it declares its own
styles (mirroring `docs/DESIGN.md` tokens by value — 12/14/16/24 px type,
`--text-meta` ink, one quiet brand red) and uses `OuroborosWidget.fetch`
exclusively against this skill's own prefix.

* `start: auto` — the card is a cheap instrument: it draws from one bounded
  local read and does no work while the Widgets page is hidden.
* `span: 2` asks the host for a two-column card, which is the host's own width
  contract (`extension_surface_names._widget_span_from_render` normalizes it to
  1 or 2, and the Widgets masonry falls back to one column when the page is too
  narrow). The widget does not try to be wide through its own CSS.
* **A fixed `height: 760`, deliberately.** The host mounts a module frame with
  its auto-height bridge **only** when no `render.height` is declared
  (`web/modules/widget_module.js`: `autoHeight = render.height === undefined ||
  render.height === null`); that bridge is a `ResizeObserver` on `#root` which
  posts a height to the parent, resizes the card, and is then notified again by
  the re-layout it caused. In WebKit that round trip surfaced as
  `ResizeObserver loop completed with undelivered notifications` — a red *Widget
  script error* on the Widgets page. Declaring a fixed height removes that
  observer entirely, so the loop cannot form. Nothing is caught, filtered or
  suppressed: no `error` handler, no `unhandledrejection` handler, no host edit
  and no custom `postMessage`. The one observer left is this widget's own width
  watcher, which redraws **only when the observed box actually changed size**, so
  it cannot re-enter its own notification either.
* **The frame is the single scrolling surface.** `body` scrolls; the request
  list no longer has an inner `max-height` pane, so there is never a second
  scrollbar competing for the same gesture and the last row of the card is
  always reachable. `#root` remains the single owner of padding under
  `box-sizing: border-box`.
* Type is the 12 / 14 / 16 / 24 px scale only — 12 px for meta and labels, 14 px
  for body text and every control, 16 px for card titles, 24 px for the four
  figures.
* It lays out from 360 px upward with no horizontal overflow: the tiles and the
  filters are `auto-fit` grids, the two columns collapse to one below 660 px,
  and every grid and flex child is `min-width: 0` so a long model name
  ellipsises instead of widening the document.
* Fetches are single-flight per kind and every one of them carries a generation.
  **Only the current generation may write to state**, so a late answer can never
  undo a newer click — and each superseded request is aborted through its own
  `AbortController`, so pressing through a list or a horizon row cannot stack
  live fetches.
  * The horizon row stays enabled while an answer is loading, because a span
    that returns nothing must still be changeable. Changing it therefore
    **supersedes** the overview request in flight — that answer describes a span
    the owner has already left — and the newer selection is what is requested.
    An overview payload is adopted only while its `horizon.selected` still
    agrees with what is selected now; anything else would silently overwrite a
    live click.
  * A trajectory answer is dropped unless it is still the newest one requested,
    so changing the selection — or pressing Reload twice — cannot paint a stale
    task. The sequence guard and the abort are kept together, on purpose: the
    abort stops the request, the guard is the defence if an answer arrives
    anyway.
* Keyboard focus survives a rerender: every control carries a stable
  `data-focus` key and focus is restored to the same control afterwards. The
  60-second poll skips a redraw entirely while focus is inside the card, so it
  cannot close an open dropdown under the owner's hands.
* Every draw runs in a cancellable animation frame, coalesced to one per frame
  per chart; both the main chart and the trajectory chart are `ResizeObserver`d
  through the same debounced, size-guarded path.
* Refresh is manual; a conservative 60-second poll runs **only** while
  `document.visibilityState === "visible"`.
* `window.__ouroWidgetOnDispose` is registered (never assigned over) and clears
  the timer, cancels every pending animation frame, disconnects every
  `ResizeObserver`, removes every listener and aborts every in-flight request.
  The view holds no durable state — everything it shows is re-derivable from the
  ledger.
* Work kinds and origins are humanised for display only (`skill_review` →
  “Skill review”); the exact recorded token stays in the tooltip and in the
  request's technical detail.

## Tests

```
python3 -m unittest discover -s tests
```

Standard-library `unittest` only; fixtures are written to a temporary
directory. `tests/test_lens_core.py` covers numeric aggregation and spread,
empty and missing ledgers, malformed / non-UTF-8 / torn-tail rows, record
uniqueness across repeated and incremental refreshes, cache bounds and
eviction, the 4 MiB cold-tail bound, ledger replacement and truncation, the
"the reader never mutates the file" invariant, aggregate exclusion, the
redaction allowlist, mode classification, and trajectory grouping. It also
covers, specifically:

* folded attempts counted **once** — from the header, from group rows when the
  header is outside the window (disclosed as a lower bound), and across two
  independent compactions;
* a malformed `kind` and a missing or unknown `state` excluded rather than
  admitted as ordinary requests;
* oversized counts, unparsable and out-of-range timestamps and an oversized
  `seq` degrading to "missing" while the payload stays serialisable;
* a symlinked ledger, a symlinked `state` directory and a non-regular file all
  refused;
* a ledger replaced **between the stat and the open** (driven by a racing
  `os.stat`) producing exactly one generation of points;
* a `state` directory swapped for a symlink into another tree **after its
  descriptor was taken** (driven by a racing `os.open`) still reading the ledger
  inside the serving root, and a platform without `dir_fd` failing closed;
* a cold tail landing inside an unterminated line: held and retried when the
  line can still complete, discarded and counted in `discarded_oversize_lines`
  when it is at least as long as the per-refresh cap — never parsed in pieces,
  never counted malformed;
* projections staying byte-identical while a later refresh mutates the cache;
* point ids that are digests of the whole attempt id, stable across refreshes
  and collision-safe on a shared prefix;
* trajectory keys validated, never reflected, and groups ordered temporally with
  only settled, measured, timestamped points taking part.

`tests/test_plugin_routes.py` drives the real handlers through a fake
`PluginAPI` that fails on any undeclared permission, checks the registered
widget geometry and `span` against this manifest, proves that a
`get_runtime_info()` that raises is answered `runtime_info_unavailable` and
logged by exception type only — with no path and no exception text reaching
either the log line or the browser — plus a threaded double-count check and — when Starlette is importable — the same handlers through a real
`Request` and `TestClient`. `tests/test_widget_contract.py` checks the widget
source against the promises made here — its own route prefix only, no forbidden
browser capability, compact-by-default paging, focus restoration, cancellable
frames, the guarded trajectory answer and the honest wording.

When a Node runtime is installed (both checks skip cleanly when it is not) that
file also parses `widget.js` with `node --check` and runs
`tests/widget_smoke.cjs`, which **executes** the widget in a VM context against
a minimal DOM stub and a recording canvas: first paint, a loaded payload, the
compact list paging out to every filtered request and back, the coverage tile
reading 24 of 27 for the current view, selection loading a trajectory, a
superseded trajectory answer being discarded, a horizon click made while an
overview request is still in flight winning over the answer that lands after it,
focus surviving a filter change,
Unknown staying offered, and disposal leaving no pending animation frame. It is
not a browser — no layout, no painting — but a render path that throws or draws
the wrong population fails there rather than in the card. The harness is
`.cjs` deliberately: the host captures every sibling `.js` / `.mjs` into the
served module bundle, and a test harness has no business being in it.
