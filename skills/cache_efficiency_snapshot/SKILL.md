---
name: cache_efficiency_snapshot
version: 0.6.1
title: Cache Efficiency Snapshot
description: Interactive cache observability dashboard over the event log with token-weighted read rates, separate request and session counts, measurement coverage, code-version marks, and Canvas charts.
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
reports input tokens and cache reads with explicit measurement coverage. It makes
no model calls and changes no runtime state or task-card accounting.

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
- `7D`: trailing week in six-hour bins.
- `ALL`: all retained records, with adaptive hourly, six-hour, half-day or daily bins.

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
Requests, sessions and starts are kept for the last seven days (the archive
window). `ALL` therefore means all retained records, not a replay of older
history. Diagnostics disclose archives read, duplicates skipped, sessions
without a usage event and display limits (25 model/route rows, 120 latest time
bins and the 60 latest code marks, each with an omitted count); headline totals
still cover the selected retained window.

## Code-version marks

A dashed violet line marks the first worker start on a commit different from the
previous start's commit. The earliest retained start marks nothing, and restarts
on the same commit mark nothing. Inspecting a bin names the code that started
inside it. A mark is a time, not a cause: a change next to it may come from a
model, task mix or provider change.

## Widget lifecycle

The standalone module uses the owning extension fetch bridge, with no external
CDN. Refresh is explicit and also runs every thirty seconds while mounted.
Trend and token-volume views support pointer, touch and arrow-key inspection;
coverage and unit explanations remain visible outside tooltips. Read failures
show a warning while retaining the last successfully loaded measurements.
The host disposal registrar stops polling and removes the resize listener.
The existing host/owner launch policy is unchanged.
