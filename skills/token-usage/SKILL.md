---
name: token-usage
description: Token Observatory — Liquid Glass dashboard for recorded Ouroboros token usage, tasks and models.
version: 1.2.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [route, widget]
when_to_use: Inspect token usage across Ouroboros models, harnesses, projects and tasks over the retained history.
model_experience:
  what_model_sees: No tools are registered; this skill adds a Widgets dashboard and its own HTTP routes.
  token_effect: Small constant manifest metadata only; the dashboard calls no language model.
contributes:
  - id: usage
    placement: composer.chips
    label: Usage
    source: {route: chip, refresh_s: 120}
    action: {verb: open.tab}
ui_tab:
  tab_id: observatory
  title: Token Observatory
  icon: "◉"
  render:
    kind: module
    entry: widget.js
    appearance: host
    start: manual
    height: 560
    span: 2
---

# Token Observatory

The composer usage chip opens this widget. `GET chip` queries the same
in-process `data?period=24h&page_size=1` summary, showing recorded confirmed cost
when present, otherwise reported physical-call tokens. The title gives calls,
tokens, confirmed cost and missing coverage. Subscription sessions, estimates
and holds are not added to these totals. Loading or failed reads show “—”.


## Host appearance

This module opts into the host-resolved Light/Dark theme through the optional `OuroborosWidget.onTheme` bridge. Older hosts keep the existing dark palette. Theme changes update presentation in place and disposal unsubscribes the listener.

Independent Widgets dashboard for recorded Ouroboros usage. Start **Token Observatory** in Widgets after the host has reviewed, granted and enabled this extension. The dashboard does not call a language model.

Owner-approved scope: Ouroboros and its launched agents only; all retained numeric history with presets and calendar; Liquid Glass visual design; root task own usage and aggregate descendant usage; secondary recorded costs. No external application chats, prompts or secrets. No public publication.

Data sources are read-only. The skill must never acquire the monetary lock or repair/write the usage ledger. Display preferences and any disposable derived numeric index belong only to the skill state directory. Missing token counts are unknown, never zero. Cache counts are not added to input totals, and legacy input semantics must be disclosed.

## Runtime

`plugin.py` uses the exact PluginAPI 2.0 contract: `register_route(path, handler, methods=[...])`, one `/preferences` handler for GET/PUT, `register_ui_tab` with the manual module render specification, and `on_unload` cleanup. The host prefixes routes with `/api/extensions/token-usage`; the widget uses that exact prefix. Fixed `data_dir` and skill-owned `state_dir` come from `api.get_runtime_info()`. Starlette Request/JSONResponse are required host dependencies; missing imports fail explicitly. `widget.js` has no CDN, framework or localStorage. The only persisted runtime document is `state_dir/token-observatory/display-preferences.json`; the numeric projection is disposable memory. Source reading starts lazily on the first query, with cancellation on plugin unload.

Source access is limited to `data_dir/state/usage_attempts.jsonl`, archives linked by its leading baseline header, and the two fixed project metadata files. Do not expand these paths from client input. Do not read task result bodies, source_text, prompts, chats, account IDs, or credentials. The narrow metadata parser discards fields other than task/project identities and project names.

## Accounting and coverage

Main totals include physical attempts in dispatched, settled and unresolved states. Subscription sessions are separate aggregate counters, never additional physical calls. Legacy totals, baseline groups and reserved/released attempts do not enter the main token totals. Latest journal order wins across transitions and archive generations; sequence numbers are not compared across epochs.

Subscription sessions preserve the optional `input_token_usage` object independently of legacy counters. It is valid only with exactly `total_tokens`, `cache_read_tokens`, `cache_write_tokens`, each null or a non-boolean nonnegative integer. A valid object's input/cache fields replace legacy fields for session totals; null stays unknown. An invalid or absent object uses legacy fields. Both observations, known/missing counts, and exact integer strings remain inspectable. Physical calls and sessions have separate chart/ranking views and are never summed together.

Input and output are recorded fields. Cache read/write are separate non-additive counters; uncached input and reasoning are never inferred. Recorded harness/route, provider and model remain distinct labels. Missing project/route facts remain Unassigned/Unknown. Confirmed, estimated, active-call holds and undispatched reservation holds are recorded amounts with distinct meanings.

See [README.md](README.md) for data contracts, API, tests and integration assumptions. See [HANDOFF.md](HANDOFF.md) for the Russian delivery report and remaining host acceptance. This private payload has not been applied, reviewed, granted or enabled by its author.
