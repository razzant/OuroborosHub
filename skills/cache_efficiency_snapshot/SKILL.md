---
name: cache_efficiency_snapshot
version: 0.8.1
title: Cache Efficiency Snapshot
description: Token and cache dashboard with exact intraday intervals, daily Week/Month/All bars, Custom dates, weighted cache hit rate, cache writes, measurement coverage and automatic local archive indexing.
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
timeout_sec: 30
permissions:
  - route
  - widget
env_from_settings: []
ui_tab:
  tab_id: cache_efficiency
  title: Cache Efficiency Snapshot
  icon: activity
  render:
    kind: module
    entry: widget.js
    appearance: host
    span: 2
---

# Cache Efficiency Snapshot

## Host appearance

This module opts into the host-resolved Light/Dark theme through the optional `OuroborosWidget.onTheme` bridge. Older hosts keep the existing dark palette. Theme changes update presentation in place and disposal unsubscribes the listener.

A read-only dashboard over Ouroboros's own event log: `logs/events.jsonl` and
its rotated `archive/events_*.jsonl` files. Per-call `llm_usage` events are
requests. An `llm_usage` event carrying a Claudexor run receipt is one whole
harness session. The core also writes a `delegate_run_ledger_recorded` event for
every session it records; a session met only there counts with its route and
time and unknown tokens. `worker_boot` events show when new code started. It
reports input tokens, cache reads and cache writes with explicit measurement coverage. It makes
no model calls and changes no task-card accounting. Its only writes are its own
normalized history index and recoverable copies of an unreadable index.

From Ouroboros 7.7.0 the usage ledger lives in the core's SQLite store
(`state/usage.sqlite`). The store keeps ordinary readers off its attempts table,
and its journal holds writers back while a reader is inside a query, so the
dashboard never opens it. Cores before it (7.6.0 and older) keep session tokens
in `state/usage_attempts.jsonl` and write no session usage events; that file is
read while it changes within the archive window, and its session rows replace the
same run's tokenless sessions. The store's import leaves the file unchanged, so
it stops being read once its last change leaves the window.

## Measurements

The cache-read rate is the sum of cache-read tokens divided by the sum of input
tokens from the **same measured records**. Both counts must be known, input
must be positive, and reads cannot exceed input. Missing cache-write data does
not invalidate an independently measured input total and cache-read count.

A usage event carries inclusive `prompt_tokens`, `cached_tokens` and
`cache_write_tokens` for one physical request; a missing count stays unknown,
and cache reads above the input total make the read unknown rather than
clipped. A session event's counters are its run summary's. A normalized
`input_token_usage` split (nullable `total_tokens`, `cache_read_tokens` and
`cache_write_tokens`), when present, is authoritative, and null fields are never
filled from other fields. Codex sessions report inclusive input and read-only
counters. Other harnesses' session counters do not establish an inclusive total
(Claude's input excludes cache reads and names no cache writes), so those
sessions stay unknown, as does a session recorded without a usage event. There
is no guess based on model names or on which count is larger.

A measured record that reports a cache-write count splits the input it did
not read from the cache into cache writes and uncached input (neither read from
the cache nor written to it). Without such a count, or with a write that with
the known reads exceeds the input total, that unread input is unclassified, like
input whose reads are unknown: no write is guessed and nothing unknown is called
uncached. The volume bars stack unclassified input, cache reads, cache writes
and uncached input, so the parts add up to the input total. The headline shows
"—" for cache writes when no record reports a usable write count and for
uncached input when no measured record does; each of the two cards names how
many of the window's records it covers when that is not all of them.
Known cache writes count every write a record reports and its input can hold,
like known cache reads, so a write of a record whose reads or input total are
unknown is among them but is not drawn (the bars hold the writes of measured
records only); unknown cache writes count the records they do not cover. Records
written by a core older than 7.7.0 may carry 0 for a count their provider did
not report; such a 0 counts as reported, so that record's unread input is drawn
as uncached input. Writes are shown in tokens only; the money is on the core's Costs page,
which counts single model calls by their start time (not harness sessions), so
its token figures differ from these.

Known-token totals exclude unknown records, whose counts remain visible. The
coverage card counts records with both valid measurements, including measured
zero-volume records. Zero input has no rate; positive input with zero cache
reads is **0%**. Idle and unavailable periods are gaps in the trend chart.
No monetary savings or estimated prices are calculated.

Requests and whole sessions remain different units. Rows group by exact
provider, model and record kind. A session aggregate may span attempts and
models; its displayed model is only the final reported model, not per-model
attribution of every token in that session.

## Timeframes and retained history

- `1H`: trailing hour in five-minute bins.
- `6H`: trailing six hours in thirty-minute bins.
- `24H` (default): trailing day in hourly bins.
- **Week** (`7D`): seven calendar dates including today, in daily bins.
- **Month** (`MONTH`): thirty calendar dates including today, in daily bins.
- **All** (`ALL`): available indexed observations, in daily bins; not a claim of lifetime completeness.
- **Custom**: inclusive dates in the viewer's IANA time zone, committed with Apply.
  Expanding into old history automatically starts a background backfill. The
  previous successful view keeps its own dates while loading or after a failure.

The API accepts `timeframe`, `timezone` (UTC when omitted), `daily=1`, and for
Custom `from`/`to`. Date-only `to` includes that local date; an explicit ISO
instant is exclusive. The widget's day detail uses exact ISO boundaries and
`granularity=hour`, including 23/25-hour DST days. Without `daily=1`, 7D/All
retain adaptive intraday bins for older API consumers.

Intervals are half-open and have no artificial empty endpoint. Bins are
labelled in the viewer's local time, and the chart names that time zone. The live
event log is read incrementally: incomplete trailing lines wait for their
newline, a rotated or truncated live file restarts at offset zero, and each
rotated archive is read once. Records are keyed by call identity, so a request
met in the live tail and again inside the archive it rotated into counts once.
Sessions are keyed by run, so a run met as a usage event and as a ledger event
counts once, with the usage event's tokens.
A run may emit several session usage events (for example a failed review, then a
recovered one). Each run keeps one session, chosen by evidence: a normalized
`input_token_usage` object (nulls included) beats a legacy-counter usage event,
which beats a ledger event without tokens. A later event replaces the kept one only
with strictly higher evidence; fields are never merged, summed or filled across
events, and equal evidence keeps the first event. Two different normalized splits
of one run are not reconciled: the first is kept and the other is counted as a
skipped duplicate.
The recent reader keeps seven days of archives plus the active log's observations
(even if it has not rotated for longer), and a preceding worker start for version
comparison. Month, All and older Custom ranges use
`state/cache_efficiency_snapshot/history-index.json`. One owned background thread
loads, incrementally parses and atomically saves normalized archive entries. GET
reads a parsed timestamp-sorted snapshot, resolving the live overlay's session
evidence before the range filter. Short ranges and selected-day detail use the
same available historical winners and can recover that index after a transient
read error on the existing background cadence. Unchanged polls do not decode and deduplicate
all of history again. Changed archives still rebuild and persist the normalized
index in the background; All aggregation remains proportional to its observations.
Discovery checks for rotation every thirty seconds; raw
archives are immutable under the core's rotation contract. Indexed measurements
survive raw-source retirement, with absent-source counts disclosed. A damaged
index is preserved beside its replacement and its recovery is shown.

The index contains no raw prompts; request/session identities are hashes, with
only times, source/model, counters and code-start facts retained. A saved index
is a local projection, not a backup of the full logs. Backfill progress counts
processed archives; observed extrema never prove continuous daily coverage.
Unavailable, unobserved and not-yet-indexed dates are not zero consumption.
Diagnostics disclose measurement coverage, malformed records and display limits:
25 model/source rows and 60 code marks (with omitted counts). Daily charts page
all dates without dropping them; only the legacy intraday API is limited to the
latest 120 bins. Headline totals cover the full selected interval.

## Code-version marks

A short dashed tick marks the first worker start on a commit different from the
previous start's commit. The earliest retained start marks nothing, and restarts
on the same commit mark nothing. Inspecting a bin names the code that started
inside it. A mark is a time, not a cause: a change next to it may come from a
model, task mix or provider change.

## Widget lifecycle

The standalone module uses the owning extension fetch bridge, with no external
CDN. Refresh is explicit and also runs every thirty seconds while mounted.
SVG token-volume and hit-rate views support pointer, touch and keyboard inspection;
coverage, absolute known cache reads and unit explanations remain visible outside tooltips. Known input whose
class is unknown (unknown cache reads, or unread input without a usable cache-write count) is hatched, not silently
dropped. Selected intraday bins use
their own totals; a selected day requests its own hourly data, models and marks.
Refreshing an open day invalidates and reloads its detail; current bucket totals
remain visible meanwhile. Keyboard focus follows the exact chart/bin through
asynchronous redraws. Usable partial measurements retain their own dates and an
explicit warning, separately from the last successful snapshot. Transport failures
keep the displayed reading. Retry and the existing thirty-second polling cadence
both recover the failed selection. Native disclosure state survives refresh. Disposal stops polling, detaches
the width observer and theme listener; plugin unload stops its archive worker.
The existing host/owner launch policy is unchanged.
