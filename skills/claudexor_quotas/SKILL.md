---
name: claudexor_quotas
description: "Claudexor quota widget and read-only quota_summary tool: one bar per account and limit (current, dated last-known, or unknown — never zero), reserve in account-windows with coverage, restrictions and reported resets, and under the chosen limit a timeline — its recorded past and one conditional future (no new use, or the recent pace) of the accounts read now, with its reset schedule — from a bounded local history of passive readings."
version: 0.9.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [net, route, widget, tool, supervised_task]
env_from_settings: []
when_to_use: "The owner wants quota windows, resets and remaining reserve of the authorized Claudexor accounts; or, before choosing executors, the model wants measured reserve by family and limit. Advisory only: never a dispatch guarantee or a pin change."
model_experience:
  what_model_sees: "One tool, quota_summary, with a fixed schema. Only when called does compact JSON (account-windows per family and limit, current and dated last-known apart, coverage, restrictions, resets, pace, and a one-line headline per limit) arrive as a tool result; nothing is auto-inserted."
  token_effect: "Fixed schema and description while enabled; each voluntary call adds about 1-8 KB of JSON, less per family. No automatic call or cache benefit is promised."
timeout_sec: 180
ui_tab:
  tab_id: quotas
  title: Claudexor Quotas
  icon: "◔"
  span: 2
  render:
    kind: module
    entry: widget.js
    appearance: host
    start: auto
---

# Claudexor Quotas (v0.9.0)

A projection of the host's own account surface, plus a small planner on top of
it: how much measured quota is left per agent family and limit, when it is
reported to reset, and what happens if the recent pace continues. Cached
projection reads remain read-only. The owner's explicit Refresh button invokes
the host's dedicated foreground quota action; the skill reads no daemon token
and owns no quota freshness, routing, retry, pacing, or vendor policy.

The skill keeps these local files inside the state directory the host hands
it (`api.get_state_dir()`):

- `prefs.json` — legacy since 0.8.0: the display choices of the 0.7 account
  list (how much of a row to unfold, which windows a row shows per agent
  family, which kinds of account fold away). The 0.8 widget neither reads
  nor saves them — its timeline's span and future are choices for one visit,
  and each mount opens on its defaults — but the file, the route and the save
  ordering below are kept for an older widget. The widget cannot keep choices
  itself: module widgets run in an
  opaque-origin sandbox where every browser store throws, so a preference kept
  there is silently forgotten. The route stores those three values and
  nothing else. It reads and writes the file off the event loop, one save at
  a time per load of the skill, each through a temporary file of its own
  moved over `prefs.json`. Of two saves that cross, the reader's later choice
  is kept: the widget numbers its saves per load of the frame (`frame`, `seq`,
  read and never stored), the route does not write a save that arrives after
  a newer one from the same frame, and the widget draws only its newest
  save's answer. A save without that number keeps the later arrival.
- `quota_history.sqlite3` — a bounded history of passive quota readings,
  written only by the skill's one supervised collector (see below).
- `quota_collector.lock` — an empty lease file, never removed while the skill
  runs. Its OS lock serializes collector cycles across unload/reload; it holds
  no account or quota data. SQLite may also keep its standard WAL/SHM files.

## What changed in 0.9.0: last-known history and manual reset credits

0.9.0 includes the presentation work prototyped privately as 0.8.1. It adds display history (`chart.history`) alongside the
unchanged strict measurement series (`chart.past`, compatibility only).
Temporary missing, failed or stale readings carry each account's last resolved
value, dashed with observation age and source on hover. Passing a reported
reset keeps the dated pre-reset value; it never manufactures a full refill.
The headline, future scenarios and measured pace remain fresh-only.

Each member begins at its first recorded sighting, with a line break when
the set changes. The chart reads retained subjects of the selected series,
including those absent from the current roster, so removing an account does
not erase its in-window past. A subject absent from the current or retained
known roster needs an in-window sighting; an older seed alone cannot include
it. Current roster members can still carry older seeds. The existing history
schema has no historical rosters:
a removal confirmed by a fresh roster is shown only at the current read,
explicitly with its actual time unknown. An omitted quota reading or a failed
or cached roster never proves removal. A wholly removed limit is not restored
as a current row solely from history. No database migration is introduced.

Manual reset credits appear in the family overview and selected account.
Only `reset_credits` (optionally with its own harness prefix) identifies the
counter. An explicit single integer in a recognized label is shown as
reported; unfamiliar or ambiguous labels remain unreadable with their original
label in the account context. Zero, not reported, unreadable, disagreeing
sources, current and dated last-known counts stay distinct. Family totals
separate current and last-known counts with account coverage. No currency,
reset schedule, restored capacity or provider semantics are inferred. The
existing in-memory facet cache can retain a dated counter; no persistent
credit history is added.

The skill requests the host's passive quota view with
`GET /api/claudexor/status?view=quota`. Only an exact `view: "quota"` response
marker identifies that dedicated path. An older core may ignore the query;
that answer is handled as legacy status, without a second GET or any claim
that it avoided diagnostics. Catalog `not_read` is intentional on the marked
path, not promoted to `ok`; accounts and quota still need successful reads.
The dedicated envelope omits daemon health too; absence is not a daemon-down
verdict. Known display names use the host's existing identity presentation
when the catalog is absent; meaningful cached catalog names still take priority.
Failed or unread rosters never become authoritative empty rosters. Safe phase
errors and timings remain available in the projection. Source freshness
remains the engine's authority. This work does not establish or fix the cause
of a previously reported native `Script error`. Core and engine updates are separate.

**Freshness per window.** A core that opts in to the engine's
`GET /v2/quota?view=constraint_freshness` passes each constraint's own
`freshness` (`fresh`, `stale`, `unknown`) through the quota view: a weekly
window can stay fresh while its 5-hour sibling's reset has passed and the
snapshot as a whole is stale. One resolver (`constraint_freshness`) answers
every reader of a window — the reserve and the tool, the account view,
cooldowns, reset credits and the collector — judged over the whole answer.
With the field absent from every constraint the snapshot's conservative
`freshness` speaks for all its windows, as before; absence never makes a
window fresh. Present on some constraints but not all, a word outside the
three, a mixed answer, or a fresh window under a snapshot that is neither
fresh nor stale is never fresh, and nothing is stripped back to legacy. A
snapshot with no window, and its availability state, keep the snapshot's own
word, as does the reserved default profile's claim on legacy null-subject
readings: per-window words never widen who a reading belongs to. A quota facet
answered from the kept read ages every window too, on a copy; the kept answer
is never changed. The collector keeps a fresh weekly window beside a 5-hour
one the engine reports stale, and never records a window at or after its own
reported reset, whatever freshness it still carries: freshness is judged when
the engine reads, not forever after. The foreground Refresh envelope keeps the
legacy shape.

## Presentation changes shipping in 0.9.0 (private prototype 0.8.1)

Presentation only — the widget's rows and timeline, nothing in the numbers,
the routes, the tool, the collector or the history:

- **One bar track.** Every row's bars stand on one track height (40 px),
  charted or not: the same share left is the same height in every row. (In
  0.8.0 the charted row's bars were 46 px and the others 28 px.)
- **One control per row, in one slot.** The chart toggle ("Show chart"; on
  the charted row "Hide chart", which folds the timeline) is a button of one
  width at the right edge of every row, so the toggles stand in one column.
  It carries the row's spoken summary, as the name button did in 0.8.0. The
  limit's name is text again. The timeline has no second toggle of its own
  ("Hide timeline" is gone); folded, nothing stands under the rows and no row
  is drawn as charted.
- **Said once.** The unit line above the rows is gone: the figure says it
  itself ("10.35 of 19 accounts"), the title's tooltip and About say it in
  full. The future's assumption ("no new use · 2 current accounts keep their
  share, each refilled once at its next reported reset; no later reset is
  assumed") is its legend line
  under the chart, not a paragraph of its own as well; with no future drawn
  the legend says why. The charted row and its timeline share one shading
  with no rule between them.
- **Narrow cards.** At widget viewport widths of 640 px or less, the bars
  take a full row and the caption sits below the figure, leaving room for
  dense account strips below the figure and fixed control column. In a card
  narrower than about 390 px (the widget's own viewport at or under 360 px) the figure and its caption each take a line of
  their own under the name, and a long name or figure wraps instead of
  running into its neighbour; the control keeps its slot.

## What changed in 0.8.0

One screen, one story per limit, instead of four panels side by side:

- **The limits.** One row per limit of the chosen family: one bar per account
  (as in 0.7.0), the current figure ("10.35 of 19", "—" with no current
  reading, never 0) with the average or the dated "Last known" line under it,
  and its tail — who is at the limit or held back, unknown accounts, and the
  **next reported reset with what it gives back if nothing more is used**
  ("next reset Thu 8 Oct 22:59 · 3 accounts · +1.80 if unused";
  `next_reset_returns`). A limit's name selects it: it is underlined and
  carries the timeline's chart mark with "Show chart" (on the limit in the
  timeline, "Hide chart", which folds it) — one button, its spoken label
  unchanged.
- **The timeline**, open on every mount, stands directly under the selected
  limit's row (by default the family's "lowest left"): the recorded past and
  **one** future, chosen beside the chart — *No new use* (the default) or
  *Recent pace* — both of the accounts read now, starting from the row's own
  figure. Under the chart, in this order: where that future stands a few
  moments ahead, one line saying what it assumes, the **reported resets**
  (when, how many accounts, what each gives back in that scenario, the total
  after it — the first two, the rest on request), and the limit's details,
  notes and data table folded. The span is the limit's own by default (a
  week for a limit longer than a day) and can be switched to 24 hours or 7
  days. The 0.7.0 cohort overlay, the refill toggle, the single-sweep marks,
  the "≠" rail, the gap hatching and the large legend are gone from the
  chart; gaps stay breaks in the line, and every sighting stays listed in
  the data table.
- **One selected account.** Nothing is selected until the reader clicks a
  bar or a row of the account list; that account's bars are then marked in
  every limit, and its card shows its share, reset and pace in each limit,
  its holds, absences and readings refused, with every window and last-known
  reading under Diagnostics; Clear (or Escape) drops it and gives the
  keyboard back to the bar or row that selected it (gone since: to the
  account's row, the account list or the family button).
- **The account list** is one folded table of the family's accounts — their
  share left in each limit and their state — with the accounts that cannot
  run anything (switched off, signed out, failing their check) at the bottom
  under their reasons, never counted as alarms. Who stands in no row is said
  there by name; under the rows a short link counts them and opens it.
- **About** replaces the settings panel: the system state (daemon, facets,
  rotation) and how to read the screen, opened in place. The family buttons
  carry no colour for their worst account; a harness that is down says so.
- **Refresh reads the whole projection after it**: one POST, and once the
  host says it ran, one ordinary read — so the rows, the timeline and the
  accounts all come from a reading made after it. Nothing from the POST's
  answer is merged into an older screen.

Carried from 0.7.0 unchanged: nothing vanishes and nothing becomes 0 (a
reading not current is a dated last-known bar or a "?"); a limit an answer
does not name keeps its record; a facet that did not answer is answered from
the last read that did, dated; and every request ends, bounded (60 s reads,
210 s Refresh), never re-sending a Refresh on its own.

## Host appearance

This module opts into the host-resolved Light/Dark theme through the optional
`OuroborosWidget.onTheme` bridge. Older hosts keep the widget’s dark palette.
Theme changes update presentation in place and disposal unsubscribes the
listener. Both themes use the same neutral surfaces, 12/14/16px type scale,
32px basic controls and visible keyboard focus. Family buttons have visible
names, and Refresh is explicitly labelled. There are no glass, canvas pools,
plan gradients or tiny-text exceptions.

Host disposal stops polling and document listeners immediately, while an
already queued preference save can finish within the existing bounded flush.
A terminal disposal cannot restart through `pageshow`; ordinary pagehide /
back-forward-cache restoration can. Late replies from an old generation cannot
release a newer request’s in-flight lock, and deferred chart requests cannot
start after stop. Pool names and family ids are treated as literal keys even
when they match JavaScript prototype names (for example `constructor`). These
are regression-tested repairs; they do not establish the cause of a previously
reported native desktop `Script error` whose stack was unavailable.

The card declares `start: auto`: it is a cheap dashboard that starts
when the Widgets page is shown and stops when the owner leaves it (the owner's
per-card mode still wins). Its icon is one glyph, `◔`.

## Reserve overview (quota_summary.py)

One module computes every reserve number. The widget's overview and chart and
the model's `quota_summary` tool call the same functions on the same passive
status projection and the same history view; the widget only formats.

**Unit — account-windows.** One account's remaining share of one limit counts
1, whatever its plan: accounts with 40% and 70% left are 1.10 account-windows,
shown with the average (55%) and the number of measured accounts. It is an
arithmetic index of measured quota, not tokens, hours or work. When the
measured accounts carry different plan labels the group is marked "mixed
plans" and both the overview and detailed tool output show per-plan account
counts and account-windows; no weights are invented.

**What is one group.** A group is one agent family (harness) and one limit:
the limit's meaning (its constraint id, with only the engine's own
`<harness>:` namespace prefix dropped, so the app-server's `codex:primary` and
the rollout log's `primary` are one limit, while `base_model_inference:primary`
stays its own pool), its duration from `window_seconds` (never from the words
primary/secondary), and its model scope (`applies_to_models`, told apart by
the whole list with every name in full, serialized so that a newline inside a
name is never read as a second name — every scope without one keeps the key
its history is stored under; a list longer than 24 names is shown in part,
saying how many names it leaves out). A 5-hour limit,
a weekly limit and a model-scoped limit are separate groups and are never
added together: they bound the same work at the same time. A constraint with
neither a ratio nor a window (a cooldown, a reset-credit counter) is not a
quota window; a cooldown becomes a restriction instead.

**Who is counted, once.** Accounts are matched by the engine's exact subject
`(harness, subject_id)`. On a unified engine (`unified_accounts: true`) the
reserved `<harness>-default` profile may inherit legacy null-subject readings,
the host's own rule, and only while it has no fresh reading of its own;
otherwise a null-subject reading is left out and counted as unattributed, as
is a reading for a subject the account list does not contain (and, with the
account list unread, a null-subject reading on a unified engine). The account
view takes each account's readings from the same rule (`attribute`), so the
default row shows the legacy reading exactly when the overview counts it for
that row. A Refresh answer carries no account list: its per-account update
matches readings by exact subject until the next status read. When one account
has fresh readings from several sources, they are one limit only if their
reported resets agree (within 120 s); the newest observation is used. Sources
that observed the same moment (within one second) must also agree on the
value, within one whole percent (the larger use is then counted); no source
outranks another. If sources disagree about the reset or about a same-moment
value, the account is shown as "sources disagree" and left out of the total
rather than guessed. Accounts are never merged because they share an e-mail
address; the number of measured accounts sharing a sign-in is disclosed
instead, because two profiles of one vendor account may draw on one pool.

**Only fresh numbers are summed.** A reading contributes only when the host
marks that window fresh (its own `freshness`, or the snapshot's when no
constraint carries one), it has an observation time, and its `used_ratio` is a JSON
number in [0, 1]. A stale-only, missing, non-numeric, NaN, infinite or
out-of-range ratio is never a zero and never clamped: the account is shown as
stale or unreadable in the coverage line. A fresh reading whose reported reset
has already passed describes an ended cycle and is shown as "reset since
reading", not counted — the same rule for every reading, whatever its value.
Accounts of the family that report no reading of a limit are named as "N other
accounts: no reading, limit may not apply": not counted, not zero, and not
assumed to have that limit.

**Last known: dated, labelled, never current.** An account of a limit with no
current reading keeps its newest usable value as a dated last-known fact
(`LastKnown`): from a stale reading in this answer, from the quota facet of the
last answer that read it (when this one did not), or — for an account of the
current roster whose limit this answer carries no reading of at all — from the
newest run the local history keeps for exactly that pseudonymous subject and
series (never by e-mail). The source's own observation time is kept, never
when it was fetched; stale sources that disagree give none — said as "its
sources disagree", and kept through every fallback: the history stands in
for this answer's stale evidence only with a newer reading, never an older
run in place of evidence refused — and the newest
kept run of every source of a history series goes through the same policy
(two sources that disagree there give none either, never the newest by
chance). It is *carried* (drawn hatched and dated, and said under the
widget's row figure as its own line "Last known … · age" — never in the
figure, `measured`, `shares` or the tool's `remaining_windows`) while
its reported reset is still ahead or, with no reset reported, for at most its
window's length (24 hours without a window). A carried value is dated
evidence, not a bound on what is left now and not available reserve: use may
have grown since, and a restore or an unreported reset may have lowered it. Past that, or once its reset has passed, it stays on record for the
account ("last read …") and the account is unknown. Each group adds `bars`
(one per account the limit is known to apply to — `current`, `last_known` with
`origin` and `age_seconds`, or `unknown` with `why` — fullest first by share
alone, so an account flapping between a current and a last-known reading of
one value keeps its place; unknown ones last), `slots` (their count: the scale
of the row and of the chart), `last_known` (accounts, windows, oldest and newest
observation, origins), `unknown`, `applicability_unknown` (roster accounts with
no reading of the limit here or in the history) and `with_last_known` (current
plus carried windows — for the tool's `remaining_windows_with_last_known`; the
widget does not show this sum, and it is never available now). `measured`,
`coverage` and `shares` keep their meaning. Account keys travel on the bars
only in the widget route's answer, beside the account list it already sends;
the tool and every other answer carry no account identity.

**A facet that did not answer.** The process keeps, in memory only, each facet
(catalog, accounts, quota) of the last status read that answered it. When a
later read fails a facet — or fails altogether — that facet is answered from
the kept one, and the answer says which and from when (`cached`, per facet),
while `facets`/`reads` keep saying what was actually read. Kept quota readings
are all stale, so they can only be last known. A kept account list keeps the
roster, never the accounts' state: every account is then flagged
`account_state_unknown` (and the widget says once that the account list was not
read now, rather than painting every bar amber). A facet read now — an empty
roster included — is never replaced: a deleted account is not brought back,
and fresh disable/sign-out/auth state still holds over kept quota numbers.
With no earlier read in this process nothing is invented: the roster is
`unknown` and only what the answer carries is shown. A limit the history
records for a roster subject (read now or kept) that no reading of the answer
names — a cold start with the quota facet not answered (`not_read` or
`failed`, and none kept), or a quota answer that omits it (an answered facet
with no reading of a limit is not proof its last reading was false) — comes
from the local history of exactly those roster subjects: each recorded limit
of theirs with a usable last-known value is shown as that dated `history`
value (sources resolved as above), never as current, so `measured` stays 0
for it and the headline says "no current reading". An account not in the
roster now is never restored as a current row, and an empty or unknown roster
restores nothing. Display history separately retains removed subjects'
observations. Such a limit's coverage counts the whole family as `other` and takes
the restored accounts off once (`applicability_unknown`). History alone does
not record a scoped limit's model names, only its scope: such a limit is
labelled by its recorded meaning and stays scoped — `model_scope:
"names_unknown"` (else `named` or `none`), the tool's `model_scope` note and
headline say "model-scoped, model names unavailable from history", and the
widget names it "· model-scoped" — never the family's shared limit. When the
chart opens on such a limit (asked for, or the default), the route reads once
more, bounded the same way, with that limit as the charted one, so its past
is its own record. A status read that fails
also stops a chart or family switch (`reuse=1`) from answering from an older
successful read, so the screen never jumps back to "fine" over a newer failure.

**The account view reads the same way.** An account's current windows are the
readings the reserve counts, read and resolved by the same code
(`reading_of`, `resolve_member`): one window per limit — from the source the
same-moment policy chooses, not one per source — and only from a fresh,
numeric reading observed at a known, not future, time of a cycle whose
reported reset is still ahead. A fresh reading the reserve refuses (a ratio
out of range or not a number, no or a future observation time, a reported
reset that has passed, sources that disagree) is never a current bar or a
"Limit reached" verdict: it stays in view as "Not current — <reason>", beside
the stale readings, as the known fact it is. A window reported with no ratio
at all stays a window without a bar. Percentages are carried unrounded
(`used_pct`) with their words (`used_text`): whole percents as reported, a
finer reading to one decimal, and never "100" for a window that is not at its
limit nor "0" for one that has been used ("<100", ">0"). Whether a window is
at its limit (`at_limit`) is decided on the unrounded share, exactly as the
overview's `at_limit` is — 99.6% used is not the limit — and every label, bar
colour, dot, chip, window line, list row and verdict takes it from there.

**A cooldown is not the limit.** Cooldowns are read apart from the numbers,
by the one rule the overview's "cooling" restriction uses (`cooldowns_of`):
a `cooldown_until` that is ahead or unreadable, from any source of the account
and from fresh or stale readings alike, and a fresh reading's availability
state `cooldown` while its `resets_at` is ahead, unreadable or not reported;
one whose time has passed — either kind — is history. So a cooldown
reported by a source whose number is not the one drawn, or by a stale
reading, is not lost. The account view carries each as a fact of its own
(`cooldowns`: the whole account or the models it holds, until when — or that
the end is unreadable, not reported, or already past — and whether a stale
reading reported it). An account held by one reads "Cooling down" until the
last one ends (`cooling_until`, empty when an end is unknown) — never "Limit
reached", and its end is never shown as a reset: nothing refills then. A
window at its limit still reads "Limit reached" with that window's own reset,
and a cooldown beside it stays a separate line. A model's cooldown marks the
model, not the account. The engine's own `availability` word is passed on as
reported.

**A model's limit reported out is not a cooldown.** The engine's
`availability.model_scoped_exhaustions` (`{constraint_id, applies_to_models,
resets_at}`) is read apart from cooldowns (`exhaustions_of`), from fresh and
stale readings alike, and carried in the account view as facts of their own
(`model_exhaustions`: the limit, the whole model scope by `scope_key`, the
first 24 names and how many more, the reported reset, which reading reported
it, and `live`). One holds its models only while its reported reset parses and
is still ahead, as the engine reported it. One with no reset, an
unreadable one, or one whose reset has passed is disclosed as reported
(`reset_note`: `not_reported`, `unreadable`, `passed`) and never made a hold;
no reset is invented for it. It never holds the whole account or changes the
account's state, it is never a share, and nothing is forecast from it: the
limit's own number, where one was read, stays that limit's reading.

**Reserve is not availability.** Measured accounts that are disabled, signed
out, failing verification, cooling down (a live or unreadable
`cooldown_until`, fresh or stale evidence, since the engine may still honour
it), or blocked by another spent shared limit are counted in the total and
listed as restrictions with their account-windows; beside them the rest is
shown as unrestricted. A shared limit blocks as soon as its current measured reading is
spent, whether or not its reset time was reported (unknown timing is not the
absence of a restriction); a reading whose reported reset has passed, a stale
or an unreadable one blocks nothing. A live reported model exhaustion
restricts (`model_exhausted`) only the account's share of the limit with
exactly that model scope, and only while the share counted there is not
already at the limit (which says it itself, and is not counted twice); a
disclosed one, or one naming no model, restricts nothing. None of this is a
dispatch guarantee: Claudexor decides routing.

**Lowest left (`tightest`).** In each family, the limit with the smallest share
left on average among those with a measured account (more accounts at the
limit, then the summary's order, break a tie) is marked `tightest` — in the
overview (worded "lowest left"), in the tool (`tightest_in_family`), and as the
chart's default. It is a ranking of averages, not a verdict on what can run: a
nearly full 5-hour limit does not stand for a family whose weekly or
model-scoped limit is running out, nothing is added across limits to find it,
and a model-scoped limit (such as "Weekly · Fable") binds only the models it
names, never the family's other models. Each group also lists `shares`: the
measured accounts' share left, fullest first, with whether each is at the
limit (on the unrounded share) and whether a restriction touches it — no
identity — which the widget draws one bar per account; they add up to the
figure. Each row is sorted on its own, so a column of bars does
not follow one account from row to row.

**Reported resets and even pace.** The next reported reset is shown with the
number of accounts resetting then. "Even use to each account's reported reset"
is the sum over accounts with a reported future reset of remaining share /
hours to that reset. Every reported reset is taken as reported, whatever the
usage: nothing is inferred about whether a window "has started", since the
status API does not say so. An account whose reading carries no reset is
counted in the reserve and named as "with no reported reset"; no refill is
drawn for it.

## History and the collector (quota_history.py)

A single server-owned supervised task (`api.register_supervised_task`,
permission `supervised_task`) reads the same passive `GET
/api/claudexor/status?view=quota` every 120 seconds, with the 25-second read bound, on a
worker in the event loop's shared, bounded executor. State-directory and port
lookup, the network read, normalization, SQLite writes, pruning, vacuum and
connection closure all run there, outside the host's event-bus loop. It
never calls the refresh endpoint and never asks a provider for a new reading.
Two minutes bounds local history growth; the engine refreshes quota on its own
schedule. The marked quota path omits Ouroboros's outer subsystem diagnostics;
the engine's credential-profile read can still call Harnesses doctor on a cold
read. It is not a guarantee of independence from CLI probes. A legacy core
may ignore the query and still perform the older, slower status work. A
status read a widget route or the tool made within the last minute is recorded
instead of issuing another; the tool keeps its own reads exactly as the route
does (the facets they answered are what a later failed read is answered from). Any Refresh request drops that remembered read, whatever its
outcome, and a read still in the air when it returns is not remembered: the
next chart or family switch, tool call or sweep reads the status anew rather
than reuse a read from before the Refresh. No Refresh is ever issued for it.

- The host starts the task only when the server publishes the registration;
  worker processes merely record it. Disable, unload, delete or shutdown
  cancels it, and `on_unload` sets a stop flag as a second guard. Widget
  frames never start a collector, however many are open.
- A run that ends on an error stops only itself, so the host's `on_failure`
  policy (at most three restarts, 30 s apart) starts a new run. Unload and
  cancellation stop the registration itself: a run started after either
  ends before any I/O.
- Each coroutine submits at most one blocking cycle at a time. An OS file
  lock on `quota_collector.lock`, tried without waiting on that worker,
  covers the whole cycle: read, write and connection closure. It is `flock`
  on macOS/Linux and a lock of the file's first byte through
  `msvcrt.locking` (`LK_NBLCK`, then `LK_UNLCK`) on Windows. Both belong to
  the open file, not the process, so an old and a new registration in one
  process exclude each other, as do two processes sharing the state
  directory, and the OS drops the lock if its holder dies. A refused lock
  (`EWOULDBLOCK`; on Windows `EACCES`/`EDEADLOCK`) means another worker holds
  it: the new registration skips that cycle while the previous one settles.
  Any other lock error skips the sweep with a warning. Unload/re-enable
  creates no private executor, thread or unbounded writer pool. Where
  neither lock exists the collector fails closed: every cycle is skipped and
  logged, and nothing is read or written. The Windows branch is verified
  only against a model of the documented `msvcrt.locking` behaviour, not on
  Windows itself. A network file system that emulates `flock` with
  process-owned locks would not separate two generations in one process;
  the state directory is expected on a local disk.
- **Stop request versus settled:** `on_unload` sets a stop flag through a
  memory-only lock and returns immediately, without waiting for disk. Host
  task cancellation requests the same stop. A queued cycle is cancelled; a
  running read may return, but any result returned after stop is discarded.
  The port is looked up (runtime info, a file read) before the status
  request, which is then admitted right before it starts: a stop requested
  before admission prevents the request. Pending transactions check stop
  while processing and before commit admission, and roll back if stopped.
  Admission uses the same short lock; no lock is held during network or disk
  I/O. A status request, commit (or initialization/maintenance operation)
  admitted before the stop request may start or finish afterwards; such a
  request's answer is discarded. An already committed atomic write cannot be
  cancelled.
- The coroutine drains an active worker asynchronously, including rollback or
  admitted commit, connection closure and lease release, then sets its
  internal `control.settled` event. Repeated cancellation does not detach a
  writer. **The public host unload API does not await or report this internal
  acknowledgement:** callback return means stop requested, not persistence
  settled. A stalled filesystem can delay settlement, but never makes this
  callback wait on disk. Actual installed Stop/Panic behavior remains a host
  integration check.
- Each sweep writes, in one SQLite transaction, the fresh numeric readings it
  saw (every source) whose own reported reset is still ahead of the sweep,
  plus one sweep row (time, ok, short reason). A reading
  the host keeps reporting with the same observation time **and unchanged
  content** is not a new point: while the watch of that source is unbroken it
  only extends `last_seen`. A changed value, reset or plan evidence starts a
  new run even if its timestamp is unchanged; that correction is marked and
  cuts pace, rather than treating the correction as new spend. A break in the
  watch of a source also starts a new run — sightings more than 7 minutes apart
  (three missed sweeps plus one status timeout), or any sweep since its last
  sighting: a failed one, or one that did not see this source fresh and
  numeric (stale, missing, unreadable) while the rest of that sweep was
  healthy — even when the host reports the very same cached reading
  afterwards. The run after a break is marked, so neither an outage nor one
  source's short hole is bridged by a returning cached timestamp, and healthy
  neighbouring sources keep their runs.
- Readers apply the same rule from the sweep rows themselves: a completed
  sweep between two sightings of a source (or after its last sighting) that
  did not see it is a hole in that source's line, in the chart, in pace and in
  the reading just made by a route before the next sweep. History written by
  0.6.0 kept those sweep rows, so its recorded holes are read as holes too;
  where 0.6.0 merged a hole into one run (the same cached reading returning),
  nothing is left to read and nothing is reconstructed.
- Stored per run: a salted pseudonymous subject id (random salt kept in the
  store; not the profile id, name or e-mail), the limit key, source name, plan
  evidence (the reading's own plan label and the account list's plan, both as
  reported), raw ratio, reported reset, first/last source observation times,
  first/last collector sightings, break and correction marks. No raw response,
  detail text, identity or credential is written.
- Bounds, exactly: every 30 sweeps, runs last sighted more than 14 days ago
  and older sweeps are deleted, and a run still sighted is trimmed to the last
  14 days (its first sighting, and its first observation when a later one
  repeats the value, move up to the cutoff; a single observation keeps its own
  time); then at most 200 000 runs and 20 000 sweeps are kept. After every
  sweep a file (with its write-ahead log) over 64 MiB drops its oldest quarter
  of runs and returns the freed pages: a soft ceiling, checked after the
  sweep has written, and a file still above it shrinks again next sweep. A
  cap that removed data is recorded (`capped_before`) and the widget says the
  history is shorter than 14 days. Reads are capped at 50 000 rows per call
  (and at the 20 000 sweep rows a continuity check can need). The widget says
  where the record begins: the first sweep still kept.
- A selected chart additionally reads at most 2 000 retained subjects of its
  exact series, plus at most eight older source runs per subject to seed the
  left edge. Those seeds count against the same 50 000-row budget. No value
  observed after the left edge seeds an earlier point. The series-wide subject
  lookup may scan the bounded run table because schema 2 has no series-first
  index; the database schema and collector writes are unchanged. Truncation is
  disclosed. The model tool does not request this chart expansion.
- A history file the skill cannot read (not a SQLite database, an unknown
  shape or version) fails closed: it is not moved, deleted or rebuilt,
  nothing more is written, the collector logs it once, and the overview and
  tool report the history as unavailable (the reserve itself is unaffected).
  Before each read and write only the schema and salt are checked; no full
  integrity scan is run. A damaged page is therefore found only when a query
  reaches it: that read reports the history unavailable and that sweep is
  rolled back (the file is still not moved, deleted or rebuilt), while a sweep
  that never reaches it may still be written. A file that is not a SQLite database is never even opened,
  because SQLite would delete the write-ahead log beside it. Removing
  `quota_history.sqlite3` from the skill's state directory starts a new
  history. Schema version 2 is intentionally distinct from uninstalled R1/R2;
  there is no migration. Every read and write validates tables, columns,
  indexes and salt, including empty requests and failed/empty sweeps. A locked
  or unwritable file only skips that sweep. Readers use SQLite `mode=ro` and
  `query_only`, never initialize or repair the database, and see committed WAL
  content. A checkpointed file receives an immutable **schema-only** preflight
  to reject unsupported files without creating sidecars; actual history reads
  never use immutable mode. SQLite may maintain SHM/read-lock bookkeeping on
  ordinary read-only WAL connections. Database and existing WAL preservation
  are tested; a blanket no-filesystem-write claim is not made.
- While Ouroboros is closed, offline, or the daemon or quota facet does not
  answer, nothing is recorded: that is a gap, never idle time and never a
  zero. Pace warms up until at least 15 minutes of comparable watched
  observations exist within the trailing hour; an hour of history is not
  required. No history is generated.

## Recent pace and the timeline

**Recent pace** is, per account, the endpoint slope over the trailing
comparable stretch of one source inside the last hour: (last ratio − first
ratio) / elapsed time between the two source observations, with that span
shown. Comparable means the same source, the same reported reset (two readings
that both report none are told apart only by a drop), a non-decreasing ratio,
the same plan evidence and no break in the watch; a reset, a ratio drop
(including a manual limit restore), a plan change or a break cuts the stretch,
and a cut is never read as negative or idle use. Plan evidence is the quota
reading's own plan label and the account list's plan together, as reported:
the account list may report a change the reading's label does not (absent, or
still the old one), so a change in either — one appearing, disappearing or
starting to disagree, or the account list going unread — cuts, and neither
overrides the other. The plan shown remains the account list's, else the
reading's label. Only watched time counts: an observation
made before the collector's current unbroken run of good sweeps began (or
before the individual source first appeared in its current uninterrupted
watch segment, including its first-ever appearance and its return after a
sweep that did not see it) is not a starting point, unless the same value was
observed again after that moment, which the estimator takes as its value
there (its assumption: the same reading on both sides, nothing observed
between). At least 15 minutes
between the two observations is required. Zero growth is reported as "no growth observed in that
span", never as an unlimited reserve, and an unchanged reading is not proof
of zero use. `resolution_windows_per_hour` is kept for compatibility: the pace
one percentage point over the span would give — a reference scale, not an
error bound and not evidence of how a vendor rounds; nothing shows it as "±".
The group pace is the sum over accounts whose pace is known; with
some unknown it is "at least" that and marked partial.

**The timeline** stands under the selected limit's row and is open on every
mount (the widget then asks the route for it; folded with the row's "Hide
chart", it sends `chart=0` and the chart and its longer history read are not
computed). It shows one group (by default the family's "lowest left" limit,
which is then
kept as a pick is: an answer during an outage that names another limit
"lowest left" does not move it — only a click, or the limit's absence from a
whole answer, chooses again) over 24 hours or 7 days back and ahead — the
limit's own span by default — one y-axis in account-windows whose scale is
the larger of current `slots` and the displayed history's largest membership,
on local clock times. The time
axis is the chosen range in whole hours (it moves once an hour, not with
every reading), never where the record happens to begin, and is never
fitted to the lines.

- *history* (`history.line`, with aligned `history.details`) — solid while
  the recorded value is vouched for, dashed when any contributor is carried.
  A temporary loss keeps the latest resolved value of that same account;
  conflicting evidence keeps that fact dated, or leaves a gap if no value
  ever resolved. The hover gives the point's account count, carried count,
  oldest carried observation, age, source and origin. Compressed plateaus
  retain only endpoint observation times, so an interior age uses the older
  known endpoint rather than inventing a measurement time. No member has a
  value before its first sighting. First sightings and confirmed roster
  removals create breaks, not consumption steps; historical removal timing
  remains unknown as described above. After a reported reset the pre-reset
  value remains dated and says that refill was not observed. At most 12 000
  vertices are returned; `history.clipped_before` discloses clipping. The
  detailed table samples this same history. Carry is a display convention,
  never a new observation stored in SQLite or taught to the pace estimator.
- *the future* (`scenarios`) — of the accounts read now, the row's own
  figure (`current_windows`), restricted ones included (measured quota is not
  a dispatch verdict), drawn from now to the horizon:
  - *no new use* (`no_new_use`, the default) — each account keeps its
    current share and refills to one full window once, at its own next
    reported reset;
  - *recent pace* (`recent_pace`) — the accounts whose own pace qualified
    continue their observed net change (staying at zero once they reach it),
    refill once at their next reported reset and then continue it from full;
    the other accounts are held as with no new use (`at_pace` of `accounts`,
    `held`). A condition, not an estimate of use nobody observed.

  An account with no reported reset is never refilled (`no_reset`), and **no
  additional unreported resets** are assumed: only each account's next
  reported reset is applied, as reported now — it may move — and no later
  reset is inferred from a window's length. The horizon is how far a line is
  drawn, not how far it is reliable; no accuracy is claimed. Each scenario
  carries its `schedule` — one event per reported reset inside the span
  (resets reported within 120 s of the first of an event are one event, from
  `at` to `last`; the accounts are split at the horizon before they are
  grouped, so resets a minute either side of it are never one event — the
  one past it is only in `later`), with how many accounts reset then, what
  the refills give back **in that scenario** (`adds`: with no new use the
  share used now, at
  the recent pace what has been used by then) and the total just after the
  last of them (`total_after`) — `later` (the first reported reset past the
  span: when and for how many, never how much), `checkpoints` (the total an
  hour, 6 and 12 hours and a day ahead; a day, 3 days and a week on the
  7-day span) and, at the recent pace, `runs_out` (when accounts reach zero
  inside the span: before their reported reset, with none reported, or again
  after their refill). Every one of them is read off the same functions the
  line is drawn from; a reset exactly at the horizon is drawn with its
  refill. The horizon is one instant: the whole second published as `end`
  (now plus the span, rounded to the second). Which resets fall inside it,
  both lines and the older ones, `later`, `runs_out`, the checkpoints (each
  read off at the whole second it is published at) and the table's rows are
  all decided against that instant, never a fraction of a second either side
  of it: a reset at that second is inside and refilled there, one even a
  fraction later is only in `later`. `now` and the figure at it stay as read.
  A reset inside the span keeps its own time, fraction of a second included,
  on both drawn lines, in the schedule (`at`, `last`) and in the table's
  reset rows (`at`, `until`, then written to the microsecond) — never moved
  to a whole second, so it is never applied before it happens: a checkpoint
  or a table row at the whole second just before it reads the total before
  it, and the table lists its rows in time order by the instant each says.
  With no current reading there is no future (`scenarios` is null).

The record and the future need not share their accounts: where they do not,
or where their totals differ at now, the two lines are not joined — the
legend distinguishes solid recorded and dashed carried history from the
current-only future; the hover gives the account count at each moment.
Kept for compatibility, unchanged and not drawn by this widget: the strict
`past`, `points`, `past_basis`, `past_accounts` and their sampled `table`;
`no_new_use` (the
same line, its times rounded to the second, without a refill exactly at the
horizon), the qualified cohort's own `recent_pace` up to its first reported
reset with `cohort_past`, and `recent_pace_refill_scenario`. The display's
`history.table` is a separate bounded sample with carry counts and provenance.
The chart's `assumptions` — the notes the widget shows — describe the record
and the two futures drawn; the older lines' own wording is kept apart, by
field, in `legacy_assumptions` and never shown.

A cursor carries the numbers: a hairline, a dot on each line and one tooltip,
driven by the pointer or by arrow keys on the focused plot, with a spoken
read-out of the historical total and its membership at that moment, carried
counts and age/source, plus the separate fresh-only scenario. Under the chart:
one compact legend explains solid/dashed history and what the future assumes,
including its current account count when no pace qualifies;
then, at the recent pace, who runs out and when; the checkpoints; the first
two reported resets with the rest on request, and "Details, notes and data" —
the limit's details (scope, restrictions with the unrestricted windows, what
is not counted, plans, shared sign-ins, resets, even use and recent pace), every
caveat, the record and watch, the time zone, and a bounded table of the same
display history with carried counts and provenance, followed by the scenarios
at reported resets and checkpoints. The older strict-series table and paged
single-sweep sightings remain only as a renderer fallback for answers without
`chart.history`. The timeline, span, future, details, keyboard focus and cursor
survive the automatic 30-second redraw.
Refresh is disabled while a read is in the air, and a disabled button cannot
keep focus; a keyboard reader on it gets it back when the read ends, unless
they have moved elsewhere or left the frame. Times are shown in the frame's
local time zone, with their day and month.

## Model tool: quota_summary

`quota_summary(harness?: string, detail?: boolean)` returns compact JSON: per
group the family, limit, duration, remaining account-windows and measured
accounts, average, unrestricted windows, coverage, restrictions (by kind:
`cooling`, `model_exhausted`, `other_limit_spent`, `disabled`, `signed_out`,
`auth_failed`, `account_state_unknown`), observation
times, next reported reset, even pace to reset and recent pace, and
`tightest_in_family` on the limit the overview marks "lowest left"; plus the
history state. Additive `passive_read` reports the selected view and read diagnostics.
Its `unbroken_watch` is `{since, exact, lookback_seconds}`:
where the collector's current unbroken watch began (`exact: true`: a break,
or the first sweep kept, lies within the look-back) or only a lower bound
(`exact: false`: it began at or before `since`, further back than the
74-minute look-back pace needs). The widget says "watched without a break
since HH:MM" or "at least since HH:MM" — never "for the last 74 minutes":
the earliest sweep the look-back finds can lie up to a sweep inside it. It is found with a bounded query and one
index seek, never a scan of the whole history; 0.6.0's `session_start`, which
called that bounded boundary the start of the whole collection, is gone. `harness` limits the answer to one family; `detail` adds plan
breakdowns and why pace is unknown. Since 0.7.0 each group also says
`accounts_known_to_apply` (the slots), `last_known` (windows, accounts,
oldest observation) with `remaining_windows_with_last_known` when any is
carried, `unknown_accounts`, `applicability_unknown_accounts`, and a one-line
`headline` built from the same numbers; the answer carries `cached_facets` and
`roster` when a facet was answered from an earlier read. `remaining_windows`
stays current readings only; with no measured account the headline leads with
"no current reading of any of N accounts", never "0 account-windows left".
`exhaust_before_reset` counts the accounts whose recent pace, continued, would
reach the limit before their reported reset; those with no reported reset that
would reach it are counted apart (`reach_limit_no_reported_reset`,
`earliest_no_reset_reach_at`) — the same predicate as the widget's account
sentence (`reaches_limit`, with `reset_reported` on each bar). It performs one passive status read
(20-second bound) unless a read from the last 45 seconds, made after the last
Refresh, is at hand, reads the history, and computes the same numbers the
widget shows. It never refreshes a
provider, never changes model or account pins or routing, and never injects
chat or writes memory.

The tool's schema and description are static; numbers appear only in the tool
result, at the end of the conversation, when the model chooses to call it.
Nothing calls it automatically and nothing guarantees a prompt-cache hit.

## What it reads

The automatic 30-second visibility poll uses one existing passive endpoint,
through the host's own authenticated fetch:

    GET /api/claudexor/status?view=quota

The snapshot and roster fields retain the legacy envelope (whose exact wire
names were previously checked against engine 3.3.15). The dedicated endpoint
contract is exercised with synthetic handler responses. That qualification
is not a deployed engine/host test. Fields consumed:

- `reads` — `ClaudexorStatusReads`: `catalog` / `accounts` / `quota`, each
  `ok` | `not_read` | `failed`. This is the provenance authority.
- `view: "quota"` — the dedicated passive path marker. On that path catalog
  is intentionally `not_read`; successful account and quota reads are enough
  for a complete quota view. Missing or different markers retain legacy rules.
  `read_errors` contains safe phase codes and optional status codes;
  `timings_ms` records discovery, accounts, quota and total timings. The widget
  projection identifies its passive mode without inventing absent diagnostics.
- `daemon` — `state`, `engine_version`, `self_started`, `runtime.last_error`.
- `harnesses[]` — `id`, `display_name`, `status`, `enabled`, `provider_family`.
  One agent family per card.
- `profiles.harnessAccounts[]` — the per-harness native login:
  `harness_id`, `native_credentials_enabled`, `native_login_detected`,
  `identity.{email,plan}`, `next_up.{kind,route,profile_id}`.
- `profiles.profiles[]` — named credential profiles as wrapper objects:
  `profile.{profile_id,harness_id,display_name,credential_kind,enabled}`,
  `status.{availability,verification,verification_source,last_verified_at,detail}`,
  `identity.{email,plan}`.
- `quota[]` — snapshots: `subject.{harness,subject_id,plan_label,credential_route}`,
  `constraints[].{id,label,used_ratio,window_seconds,resets_at,cooldown_until,applies_to_models,freshness}`
  (`freshness` only from a core that opts in to per-window freshness, then on every constraint),
  `availability.{state,blocking_constraints,model_scoped_exhaustions}`,
  `observed_at`, `freshness`.
- `quota_absences[]` — typed missing-snapshot evidence. Visible copy is always
  generic; only supported owner actions (`Sign-in required`, `Retry after Xm`,
  `No live quota source`) are projected, never raw reason/detail text.

One explicit owner action is separate from cached reads:

    POST /api/claudexor/quota/refresh

The extension calls that host action only from the Refresh button. The host
retains the Claudexor bearer token and returns the exact foreground quota
envelope (its route still projects it by exact `(harness, subject_id)` as
`quota_updates`). When it says the refresh ran, the widget reads the whole
projection once — the ordinary status read, never reused from before the
Refresh — and draws that; nothing from the envelope is merged into an older
screen. If that read fails, the screen stays the one it was, kept and dated,
with a note that the refresh ran but its result could not be read.

`subject_id` is `null` for the native login and the profile id for a named
account; matching is EXACT on `(harness, subject_id)` so a named profile's
exhausted window is never reported as the default login's.

## Honesty rules (the point of this widget)

1. **Per-facet provenance, never a global verdict.** Each facet is labeled from
   its own `reads` value. A refused or unread facet is rendered as
   "not checked" / "unavailable". The About button carries a red pip whenever
   one of them did not answer, the system state inside it names which, and a
   banner above everything names it again in the open — so a failure is never
   only one click away from being invisible. It is never rendered as
   "no quota", `0`, or an empty list.
   The dedicated quota view's intentional `catalog: not_read` is labeled
   "not requested" without a failure pip or banner.
2. **No invented number.** A missing `used_ratio` is "no usage numbers
   reported", not `0%` and not "unlimited". A ratio out of range or not a
   number is an "unreadable ratio", never clamped to 100% or 0%. Rounding
   never turns a share that is not at its limit into "100%", nor a used one
   into "0%". A missing `resets_at` prints nothing rather than a fabricated
   time.
3. **Stale is disclosed, not silently dropped.** A carried last-known value is a hatched bar in its
   row with its age on hover and its own labelled, dated line under the row figure ("Last known
   0.40 · 21 min"), never in the current number (the row figure, `measured`, `remaining_windows`);
   with no current reading the row figure is "—", never 0, while a measured 0 stays 0. A last-known
   value at the limit stays hatched and dated ("at the limit when last read (20m ago)"): never the
   red base, "at the limit until …" or any other current-exhaustion wording. In the selected account's
   card it stays a last-known reading: an amber "Last known" block (only when the reading has windows — an empty one is not drawn) with a muted
   (translucent neutral) share, observation age and an explicit
   statement that stale percentages are not used to grant routing. They never
   look like fresh red exhaustion, nor like an amber share held back now. A still-live cooldown carried by stale
   evidence may still deny or rank a route, so the widget does not claim the
   engine ignores that evidence.
4. **Per-model caps stay per-model.** A constraint with a non-empty
   `applies_to_models` never marks the whole account exhausted; it becomes a
   scoped note. A present `cooldown_until` in the future (or one that cannot be
   parsed) is a cooldown — the window or model is out for now, "cooling down"
   in amber, never "Limit reached", never the red of a spent share and never a
   reset. Red is only a measured share at its limit; a cooldown, a reported
   model exhaustion and any other hold are amber — on window lines, chips,
   pool chips and their times alike. A hold reported apart from the
   windows (a cooldown on some models, a model limit reported out) is tied to
   a window only by the skill's `scope_key` of the whole scope, never by the
   printed names; a model's pool on the account's card is grouped by the same
   key, so two scopes that print one name ("Fable", "M00 +24") stay two pools,
   each coloured only by its own hold (the chip's hover lists its scope). On
   the account's window lines, a model window its scope's hold covers reads
   "cooling down" or "limit reported" in amber, with that hold's end or
   reset; the account's shared windows stay neutral. In the account list a
   model hold is named with its model and kind ("model cooldown: Opus"),
   several are counted ("2 model holds") with each scope named in the title.
   A live exhaustion that names no model holds nothing, as in the reserve: it
   is disclosed under Diagnostics only ("Reported model limit reached · models
   not named · reported until … · holds no window", muted) and colours no dot,
   chip, line or pool.
5. **`local_store` verification is honest both ways.** It reads
   "Signed in — local session, not verified live"; only `verification_source:
   vendor` earns "Verified live". Neither is treated as an absent account.
6. **Degraded accounts keep their rows as "last known"** and lose any green
   verified claim; rotation wording counts only accounts actually signed in,
   and with the account list not read now no account is "next up"
   (`routing_read: false`): a kept list's routing verdict is last known.

## Interactive Features

- **Families, About, Refresh**: visible family names with their vendor marks
  (or an initial) and the number of accounts switched on select the family;
  About opens the system state and how to read the screen in place; Refresh
  is explicitly labelled. A family's button carries no colour for its worst
  account; a harness that is down or switched off says so.
- **The limits**: beside the title a status line ("All 19 read · observed 1
  min ago", "15 current · 4 last known (21 min)", or "Claudexor not read now ·
  all as read 3 min ago"; exact times, the status read and the time zone on
  hover); the title's tooltip and About explain account-windows (a full
  account counts 1; limits are never added). Then one row per limit — its
  name ("5-hour", "Weekly · Fable"), one bar per account the limit applies to,
  as tall as its share left on the same 40 px track and 0–100% scale in every
  row, fullest first, one fixed strip width per slot count: solid for a
  current reading, hatched for a dated last-known value, amber when a restriction holds it back, a red base at the
  limit, an outlined "?" (never an empty bar or a zero) after the rest for an
  account with no usable value. Each bar names its account, share, reset and
  age on hover and aloud, and selects that account. On the right the figure —
  current readings only ("10.35 of 19 accounts"; "— of 19 accounts" when none
  is current; a measured 0 stays "0.00") — and under it the average, or the
  dated "Last known 2.12 · 21 min" line, never added to the figure. The tail
  names one account at the limit or held back by name, else counts them, then unknown
  accounts, then the next reported reset with how many accounts and what it
  gives back if unused; the limit of lowest average share is marked "lowest
  left". Each row has one "Show chart" button in the same right-hand column;
  the charted row's "Hide chart" folds its timeline. Its shading continues
  into the timeline; its bar track stays the same height as every other row.
  Who stands in no row (switched off, or with no reading of these
  limits here or in the history) is counted in one short link that opens the
  account list, where they are named.
- **The timeline** under the selected row: its span (24 h, 7 days) and
  future (No new use, Recent pace) controls; the chart; then the legend
  with each line's accounts and the future's assumptions, the checkpoints,
  the first two reported resets (the rest on request), and "Details, notes
  and data" folded. See "The timeline" above.
- **One selected account**: a bar or a row of the account list selects it;
  its bars are outlined in every row. Its card: state dot, name, plan, "next
  up", its e-mail, login kind, check and the age of its quota reading; its own
  verdict where the lines do not already say it ("Limit reached · Resets …",
  "Cooling down until …"); a line per limit (share left, reported reset, "last
  known, read 21 min ago" or why it is unknown, a hold, and — only when its
  own observed pace continued would reach the limit before that reset —
  "would reach the limit ~Fri 9 Oct 08:53"); typed quota absences, cooldowns
  (one sentence each: scope, end, provenance), model exhaustions that may
  hold now, and fresh readings not counted with their reason. Diagnostics
  unfolds every window as a line (its pool's chip, share left, share used in
  words, why it is out and until when, or its reset), each last-known or
  not-current reading, and every reported model exhaustion. Clear or Escape
  drops the selection, the keyboard back on the bar or row that made it.
  With no overview there is no Diagnostics: the card shows everything it
  would hold — the windows whole, every reported model exhaustion (a passed
  one and one naming no model too) and the credential.
- **The account list**, folded under "Accounts · 19 accounts · 2 not running":
  one row per account in the engine's order — its share left in each limit
  (from the same bars; underlined when last known, "?" when unknown, "—" when
  it has no reading of that limit) and its state ("ready", "nearly used",
  "limit reached", "cooling down", "model cooldown: Opus", "quota unavailable ·
  Sign-in required", "no current reading"); then, under "Not running", the
  accounts switched off, signed out or failing their check, with that reason
  — never counted as alarms. The engine answers a failed check with
  `signed_in: false`: such an account stands under its first reason and its
  row still says the failure, red ("not signed in · verification failed"),
  before anyone selects it. While the account list is not read now nothing
  is said not to run.
- **One bar language**: every bar is as long (or tall) as the share LEFT on
  one 0–100% scale, in one neutral ink, over a hairline base; amber is a share
  held back now, a share at its limit has no fill but a red base, a last-known
  reading is muted and claims neither, a window with no usable ratio has no
  bar at all. The number beside a window's bar is the share used and says so
  ("N% used"); the exact share left is on the bar's hover.
- **Reset Times**: the moment a window resets and a cooldown ends, printed as
  a date and hour in tabular numerals — no per-second ticking.
- **Honest live Refresh**: the explicit button performs one host POST and,
  once the host says it ran, one read of the whole projection, inside the
  same in-flight lifecycle. Automatic polling remains passive GET. An older
  host reports that a newer Ouroboros is required instead of silently
  substituting a cached reload. Cached reads use a 25-second network bound;
  the foreground refresh may wait up to 180 seconds for the host's bounded
  handshake and sequential vendor work.
- **Every request ends**: the widget asks the bridge to bound each read at 60 s
  and the Refresh at 210 s (`init.timeoutMs`, above the route's own bounds) and
  stops waiting itself 5 s later on a host that does not. Headers or a body that
  never come, an abort, a bridge error, a body that is not a JSON object, an
  HTTP error and an answer that cannot be drawn all end the request and free
  Refresh; a late answer, or one after the frame was stopped or disposed,
  changes nothing. A Refresh with no answer in time is said to have an unknown
  outcome and is never sent again on its own — whether the widget's own bound
  ran out, or the skill's route read no answer from the host (its 180 s bound,
  a connection closed after the request went out, an answer that broke off or
  could not be read, a success answer that is not a refresh envelope such as
  `{}`: the route answers `outcome_unknown: true`, never "failed"; so does the
  widget for a success status whose body breaks off or is not the route's
  answer, and for a transport rejection before any status is available (which
  does not prove the POST was never delivered); only an answer that says it
  failed, or a request that never reached the host, is a failure). Passive
  retries are the ordinary poll, and a Retry button reads now. A read that
  fails keeps the newest screen re-read at that moment: every current bar
  becomes the dated last-known value it now is, or — past its reported reset,
  or with none past its window — a "?" with its last reading kept; the current
  figure, the next reset, pace and every future are withdrawn, no account is
  shown held back now, and each account's windows become last-known readings.
  The rest of the screen is read the same way, as the skill words a status it
  could not read: no facet is shown as read now (the About pip says so), the
  daemon's state is "when last read", each account's check is "— last known"
  and none is "next up", a cooldown alone whose end has passed no longer reads
  "Cooling down", a switched-off account is "switched off when last read",
  plan splits and the answer's own stale/unreadable counts are withdrawn, and
  the chart's moment is labelled by the clock time it was read ("read 14:05"),
  not "now", with no future drawn. The banner says when the last answer was
  received, and that each value is dated by its own observation. When an
  answer cannot be drawn the screen before it stays, re-read the same way,
  with the error and a Retry (which never wraps in a narrow frame); the error
  is logged, not swallowed. Raw transport text stays off the screen.

## Owner-controlled steps

The skill declares no secrets (`env_from_settings: []`). Its permissions are
`net` + `route` + `widget` + `tool` + `supervised_task`:

- `net` — the routes, the tool and the collector read the host's own endpoints
  over loopback with `urllib` (no external host and no proxy handler): passive
  status GET with a 25-second bound (20 seconds for the tool), and the explicit
  foreground quota POST with 180 seconds, only from the Refresh button;
- `route` + `widget` — the widget, its three routes (the prefs route kept
  for an older widget) and its tab;
- `tool` — the read-only `quota_summary` tool;
- `supervised_task` — the one history collector described above.

No secret key grant is required. Everything the skill writes lives in its own
state directory: `prefs.json` with three display choices, and the bounded
history, which holds no account name, address, credential or raw response.
Enabling a reviewed skill remains the owner's action in Skills.
