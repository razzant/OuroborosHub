"""Reserve arithmetic for Claudexor Quotas — standard library only, no I/O.

One module owns every number the widget's overview draws and the model's
``quota_summary`` tool returns, so the two cannot disagree: both call
:func:`build_summary` on the same passive status projection and the same local
history view, and the chart comes from :func:`build_chart` over the same
intermediate result.

The unit is the *account-window*: one account's remaining share of one limit
counts 1, whatever its plan. Two accounts at 40% and 70% remaining are 1.10
account-windows. It is an arithmetic index of measured quota, never tokens,
hours, or a dispatch promise.

What this module refuses to do:

- add limits of different meaning, duration or model scope together (a 5-hour
  window and a weekly window bound the same work at the same time);
- read a duration out of the words ``primary`` / ``secondary`` — only
  ``window_seconds`` says how long a window is;
- count a stale, missing, malformed or out-of-range ratio as zero, and count a
  reading whose reported reset has already passed as current;
- merge two accounts because they share an e-mail address, or merge two
  sources of one account unless the subject, limit, duration, scope and reset
  agree — and when two sources measured the same moment, their values too;
- invent a reset the provider did not report, infer that a window "has not
  started" from how its numbers look, or call a carried display value a
  measurement of a moment this skill's collector was not watching.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

SUMMARY_SCHEMA = 1

# Two readings of one limit belong to the same cycle when their reported resets
# agree within this many seconds (vendors jitter by fractions of a second, and
# two sources of one account were seen a second apart).
RESET_TOLERANCE_SEC = 120.0
# A reading timestamped further than this in the future is a clock error.
FUTURE_SKEW_SEC = 300.0
# Two sources that measured one account within this many seconds measured the
# same moment; they must then agree on the value within one whole percent
# (two sources printing whole percents can differ by that much) or the
# account is "sources disagree" rather than one of them chosen.
SAME_INSTANT_SEC = 1.0
VALUE_TOLERANCE = 0.01
# The collector reads the passive status every COLLECT_INTERVAL_SEC. A reading
# is vouched for only while the collector keeps seeing it: from its first to
# its last sighting. Consecutive sightings further apart than this (three
# missed sweeps plus one status timeout), or with a failed sweep between them,
# are a gap in the record, never idle time — even when the same cached reading
# is reported again afterwards.
COLLECT_INTERVAL_SEC = 120.0
SIGHTING_GAP_SEC = 3 * COLLECT_INTERVAL_SEC + 60.0
# Recent pace: the last hour, at least a quarter of it actually covered.
RATE_WINDOW_SEC = 3600.0
MIN_RATE_SPAN_SEC = 900.0
# How far back the reader looks for the start of the collector's current
# unbroken watch. Enough for pace, which needs only the last hour; a watch
# that reaches past it is reported as "at least since", never as its start.
WATCH_LOOKBACK_SEC = RATE_WINDOW_SEC + 2 * SIGHTING_GAP_SEC
RATIO_EPS = 1e-9
# A last-known reading is carried into the display while its reported reset
# is still ahead. One with no reported reset is carried for at most its
# window's length, and one with no window either for at most this long. Past
# that it stays a dated historical fact and the account is "unknown".
LAST_KNOWN_NO_WINDOW_SEC = 86400.0
HORIZONS: Dict[str, float] = {"24h": 86400.0, "7d": 604800.0}
# The observed line keeps every change. Every vertex but a reported reset is a
# collector sighting, so a week holds about 5 000 of them; past this ceiling
# the oldest part is left out, and the chart says from when it is drawn.
MAX_PAST_VERTICES = 12_000
TABLE_PAST_ROWS = 8
FACETS = ("catalog", "accounts", "quota")

# Bounds on what one summary may carry, so a malformed or enormous status
# payload cannot turn into an enormous route or tool answer.
MAX_SNAPSHOTS = 2000
MAX_CONSTRAINTS = 64
MAX_GROUPS = 64
MAX_TEXT = 80
MAX_MODELS = 24

GLOBAL_NOTES = (
    "An account-window is one account's remaining share of one limit; every "
    "account counts 1 whatever its plan. It is not tokens or hours.",
    "Limits of different meaning, duration or model scope are never added "
    "together.",
    "Measured quota is not a dispatch guarantee: Claudexor decides routing, "
    "and an account can be disabled, signed out or cooling down, or a model "
    "of it reported out until its limit resets.",
    "Readings come from the host's passive status projection; a repeated "
    "cached reading is not a new measurement.",
)


# ---------------------------------------------------------------------------
# Small parsers


_FRACTION = re.compile(r"(\.\d+)(?=(Z|[+-]\d\d:?\d\d)?$)")


def parse_instant(value: Any) -> Optional[float]:
    """Epoch seconds for an ISO-8601 instant; None when it cannot be read.

    Naive instants are UTC (the engine speaks UTC). Fractions of any length
    are accepted — Python 3.10 only parses three or six digits on its own.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00").replace("z", "+00:00")
    found = _FRACTION.search(raw)
    if found:
        digits = found.group(1)[1:7].ljust(6, "0")
        raw = raw[:found.start(1)] + "." + digits + raw[found.end(1):]
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    try:
        stamp = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return stamp if math.isfinite(stamp) else None


def iso(stamp: Optional[float]) -> Optional[str]:
    """UTC ISO-8601 with a trailing Z, to the second; None stays None."""
    if stamp is None or not math.isfinite(stamp):
        return None
    moment = _dt.datetime.fromtimestamp(stamp, _dt.timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def iso_exact(stamp: float) -> str:
    """UTC ISO-8601 with a trailing Z and the fraction of a second kept, to
    the microsecond; a whole second is written exactly as iso() writes it.
    For a moment that must not be moved to its second (a reported reset in
    the chart's table), never in place of iso()."""
    return _dt.datetime.fromtimestamp(stamp, _dt.timezone.utc).isoformat().replace("+00:00", "Z")


def ratio_of(value: Any) -> Tuple[Optional[float], str]:
    """A used ratio in [0, 1], or None with the reason it was refused.

    Only a JSON number is a ratio. A string, a boolean, NaN, infinity or a
    value outside [0, 1] is refused rather than clamped: clamping 1.4 to 1 or
    -0.2 to 0 would print a number nobody reported.
    """
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "not_a_number"
    ratio = float(value)
    if not math.isfinite(ratio):
        return None, "not_finite"
    if ratio < 0.0 or ratio > 1.0:
        return None, "out_of_range"
    return ratio, ""


def window_of(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value <= 0:
        return None
    return int(value)


def duration_words(seconds: Optional[int]) -> str:
    """"5 hours", "week", "30 days"; the unit must divide the length exactly."""
    if not seconds:
        return "duration not reported"
    for size, word in ((604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds % size == 0:
            count = seconds // size
            return word if count == 1 else f"{count} {word}s"
    return f"{seconds} seconds"


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value if value is not None else "").strip()[:limit]


def pseudo_id(salt: str, harness: str, subject_id: str) -> str:
    """A local pseudonymous id for one account: stable per installation,
    meaningless without the store's own random salt, never the profile id."""
    digest = hashlib.sha256(f"{salt}\x00{harness}\x00{subject_id}".encode("utf-8"))
    return digest.hexdigest()[:20]


def meaning_of(harness: str, constraint_id: str, label: str) -> str:
    """What limit a constraint is, independent of which source reported it.

    Sources of one engine spell the same limit with and without the harness
    namespace (``codex:primary`` from the app server, ``primary`` from the
    rollout log). Only that exact ``<harness>:`` prefix is dropped; any other
    prefix (``base_model_inference:primary``) names a different pool.
    """
    cid = _text(constraint_id, 120)
    prefix = f"{harness}:"
    if cid.startswith(prefix) and len(cid) > len(prefix):
        cid = cid[len(prefix):]
    if not cid:
        cid = "label:" + _text(label, 60).lower()
    return cid


def _scope_hash(models: Sequence[str]) -> str:
    """A model scope's identity: the whole sorted list, every name in full.
    Only what is shown is bounded, never what tells two scopes apart.

    The list is serialized so that two different lists never give the same
    bytes. Joined by newlines it already is, as long as no name holds a
    newline — and every scope recorded so far is such a list, so it keeps the
    key its history is stored under. A list with a newline inside a name
    (``["a\\nb"]`` against ``["a", "b"]``) is serialized as JSON behind a
    leading newline instead: a joined list never starts with one, because
    every name is stripped (see _models_of) and never empty."""
    if not models:
        return "-"
    if any("\n" in name for name in models):
        joined = "\n" + json.dumps(list(models), ensure_ascii=False, separators=(",", ":"))
    else:
        joined = "\n".join(models)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:10]


def group_key(harness: str, meaning: str, window: Optional[int], models: Sequence[str]) -> str:
    return f"{harness}|{meaning}|{window or 0}|{_scope_hash(models)}"


def plan_evidence(quota_label: str, account_plan: Optional[str]) -> str:
    """The plan a reading is compared by: the quota reading's own plan label
    and the account list's plan (None when the account list was not read),
    both kept as reported. The account list can report a plan change that the
    quota reading does not (its label absent, or still the old one), so
    neither overrides the other: a change in either — a label appearing,
    disappearing or starting to disagree — cuts pace. Nothing is measured
    here; it only decides what is comparable."""
    return json.dumps([quota_label, account_plan], ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# History types (filled by quota_history; plain data here so this module
# stays free of I/O)


@dataclass
class Run:
    """Consecutive observations of one limit from one source whose value,
    reset cycle and plan did not change, watched without a gap.
    ``first_obs``/``last_obs`` are the source's own observation times;
    ``first_seen``/``last_seen`` are when the collector first and last saw the
    reading reported as fresh — the only stretch it vouches for. ``after_gap``
    says the collector was not watching between the previous run of this
    source and this one."""

    source: str
    ratio: float
    resets_at: Optional[float]
    plan: str
    first_obs: float
    last_obs: float
    n_obs: int
    first_seen: float
    last_seen: float
    after_gap: bool = False
    after_correction: bool = False


@dataclass
class HistoryView:
    state: str = "empty"  # ok | empty | unavailable
    salt: str = ""
    runs: Dict[Tuple[str, str], List[Run]] = field(default_factory=dict)
    # The earliest good sweep of the collector's current unbroken watch found
    # within WATCH_LOOKBACK_SEC, and whether that is where the watch began
    # (a break, or the first sweep kept, lies inside the look-back) or only a
    # lower bound (the watch goes on further back than the reader looked).
    watched_since: Optional[float] = None
    watched_since_exact: bool = False
    # Every sweep (good or failed) from ``sweeps_from`` on, sorted: a sweep
    # between two sightings of one source that did not see it is a recorded
    # hole in that source's watch, whichever writer stored the runs.
    sweeps: List[float] = field(default_factory=list)
    sweeps_from: Optional[float] = None
    collecting_since: Optional[float] = None
    oldest_at: Optional[float] = None
    last_sweep_at: Optional[float] = None
    last_sweep_ok: Optional[bool] = None
    last_sweep_reason: str = ""
    last_failed_at: Optional[float] = None
    truncated: bool = False
    capped_before: Optional[float] = None
    error: str = ""
    # The newest run kept per source for a (subject, series) pair, whatever
    # its age: where a last-known value comes from when the current answer
    # carries no reading of that account's limit at all. Sources are
    # resolved as a current reading's are (last_known_from_runs): two that
    # disagree give none, never the newest by chance.
    latest: Dict[Tuple[str, str], List[Run]] = field(default_factory=dict)
    # The (subject, series) pairs found for roster subjects (HistoryStore.read
    # ``roster``): the limits the history knows for accounts no reading names.
    roster_series: Set[Tuple[str, str]] = field(default_factory=set)


def same_cycle(prev_reset: Optional[float], reset: Optional[float]) -> bool:
    """Whether two readings report the same reset. Two readings that both
    report none cannot be told apart by it; a new cycle then shows only as a
    drop in use."""
    if prev_reset is None and reset is None:
        return True
    if prev_reset is None or reset is None:
        return False
    return abs(prev_reset - reset) <= RESET_TOLERANCE_SEC


def missed_between(view: Optional["HistoryView"], after: float, before: float) -> bool:
    """Whether a sweep the collector completed strictly between two sightings
    of one source did not see it. Every sweep that sees a source fresh and
    numeric marks a sighting of it, so a sweep in between is a recorded hole
    in that source's watch — a fact kept in the sweep table, whatever writer
    stored the runs. What a writer merged away cannot be read back."""
    if view is None or not view.sweeps or view.sweeps_from is None or after < view.sweeps_from:
        return False
    index = bisect.bisect_right(view.sweeps, after + 1e-6)
    return index < len(view.sweeps) and view.sweeps[index] < before - 1e-6


def unseen_since(view: Optional["HistoryView"], moment: float) -> bool:
    """Whether the newest sweep (good or failed) came after ``moment``: the
    collector looked again and did not see what it saw then."""
    return bool(view is not None and view.last_sweep_at is not None
                and view.last_sweep_at > moment + 1e-6)


def watched_gap(prev: Run, nxt: Run, view: Optional["HistoryView"] = None) -> bool:
    """Whether the collector stopped watching this source between two runs.

    Measured between sightings, never observation times: a cached reading
    that is reported again after an outage says nothing about the outage.
    A sweep in between that did not see the source is such a gap too, even
    when the rest of that sweep was healthy.
    """
    return (nxt.after_gap or nxt.first_seen - prev.last_seen > SIGHTING_GAP_SEC
            or missed_between(view, prev.last_seen, nxt.first_seen))


def boundary(prev: Run, nxt: Run, view: Optional["HistoryView"] = None) -> str:
    """'' when ``nxt`` continues ``prev``; otherwise why the series breaks.

    A break is never read as use or as idle time: a reset (or a manual limit
    restore) lowers the ratio, a plan change moves the ceiling, and a gap in
    the collector's watch may hide any change in between.
    """
    if watched_gap(prev, nxt, view):
        return "gap"
    if nxt.after_correction:
        return "correction"
    if nxt.plan != prev.plan:
        return "plan"
    if not same_cycle(prev.resets_at, nxt.resets_at):
        return "reset"
    if nxt.ratio < prev.ratio - RATIO_EPS:
        # Same cycle and less used: a manual limit restore, not negative use.
        return "ratio_drop"
    return ""


def extends(prev: Run, nxt: Run) -> bool:
    """A later observation joins ``prev`` when nothing about it changed."""
    return (
        not boundary(prev, nxt)
        and abs(nxt.ratio - prev.ratio) <= RATIO_EPS
        and nxt.first_obs > prev.last_obs
    )


def same_content(run: Run, reading: Any) -> bool:
    """A cached timestamp is a duplicate only when its content is unchanged."""
    return (run.ratio == reading.ratio and run.resets_at == reading.resets_at
            and run.plan == reading.plan_key)


# ---------------------------------------------------------------------------
# Normalization of the passive status projection


@dataclass
class Account:
    harness: str
    subject_id: str
    kind: str
    enabled: Optional[bool]
    signed_in: bool
    auth_failed: bool
    plan: str
    identity: str  # sign-in address, lowercased; compared in memory, never emitted


@dataclass
class Reading:
    harness: str
    subject_id: str
    source: str
    fresh: bool
    observed_at: Optional[float]
    key: str
    meaning: str
    label: str
    window: Optional[int]
    models: Tuple[str, ...]
    ratio: Optional[float]
    ratio_problem: str
    resets_at: Optional[float]
    plan: str  # the quota reading's own plan label
    # What history and pace compare by (plan_evidence); a run's ``plan``.
    plan_key: str = ""
    # The model scope's identity (_scope_hash of the whole list); ``models``
    # is the bounded list shown, ``models_omitted`` what it leaves out.
    scope: str = "-"
    models_omitted: int = 0


@dataclass
class Cooldown:
    """One reported cooldown that holds now: the account, or only the models
    of ``models``, is unavailable until ``until``. It is not a share used
    and not a reset — nothing refills when it ends."""
    harness: str
    subject_id: str
    models: Tuple[str, ...]  # the bounded list shown; () is the whole account
    scope: str  # _scope_hash of the whole list; "-" is the whole account
    until: Optional[float]  # None: unreadable, or no time reported
    until_text: str  # as reported, bounded; "" when none was
    kind: str  # "constraint" (a cooldown_until) | "availability" (the row's state)
    label: str
    source: str
    fresh: bool
    observed_at: Optional[float]
    models_omitted: int = 0  # names of the scope ``models`` leaves out


@dataclass
class Exhaustion:
    """One model-scoped exhaustion a snapshot's availability reports
    (``availability.model_scoped_exhaustions``): the engine says the limit
    ``constraint_id`` of these models ran out, until ``resets_at``. It is not
    a cooldown (it names the limit that ran out and that limit's reset), it
    never holds the whole account, and it carries no share of its own.

    ``live`` only while its reported reset parses and is still ahead. One
    with no reset, an unreadable one, or one whose reset has passed is
    disclosed as reported (``note``), never turned into a hold now."""
    harness: str
    subject_id: str
    constraint_id: str
    models: Tuple[str, ...]  # the bounded list shown; () when none was named
    models_omitted: int
    scope: str  # _scope_hash of the whole list; "-" when no model was named
    resets_at: Optional[float]
    resets_text: str  # as reported, bounded; "" when none was
    live: bool
    note: str  # "" (live) | "passed" | "not_reported" | "unreadable"
    source: str
    fresh: bool
    observed_at: Optional[float]


@dataclass
class Attributed:
    """Each snapshot of one status payload with the account it belongs to."""
    reads: Dict[str, str]
    unified: bool
    accounts: Dict[Tuple[str, str], Account]
    accounts_known: bool
    quota_known: bool
    rows: List[Tuple[str, str, Dict[str, Any]]]
    unattributed: Counter
    superseded: Counter
    malformed: int
    # How this answer's windows carry freshness (freshness_projection).
    freshness: str = "legacy"

    def rows_of(self, harness: str, subject_id: str) -> List[Dict[str, Any]]:
        return [row for hid, sid, row in self.rows if hid == harness and sid == subject_id]


@dataclass
class Normalized:
    reads: Dict[str, str]
    unified: bool
    harness_order: List[str]
    families: Dict[str, str]
    accounts: Dict[Tuple[str, str], Account]
    accounts_known: bool
    quota_known: bool
    readings: List[Reading]
    cooling_all: Set[Tuple[str, str]]
    cooling_scoped: Dict[Tuple[str, str], Set[str]]
    # Model scopes a live reported exhaustion holds, per account.
    exhausted_scoped: Dict[Tuple[str, str], Set[str]]
    unattributed: Counter
    superseded: Counter
    malformed: int
    # Facets this answer did not read, answered instead from the last answer
    # that did (facet -> when that one was read). Readings taken from a
    # cached quota facet are never fresh; a cached account list keeps the
    # roster, never the accounts' current state.
    cached: Dict[str, float] = field(default_factory=dict)
    # account -> keys of shared limits its current reading reports spent.
    spent: Dict[Tuple[str, str], Set[str]] = field(default_factory=dict)

    @property
    def accounts_cached(self) -> bool:
        return "accounts" in self.cached


def facet_reads(payload: Any) -> Dict[str, str]:
    reads = payload.get("reads") if isinstance(payload, dict) else None
    out: Dict[str, str] = {}
    for facet in FACETS:
        raw = str((reads or {}).get(facet) or "") if isinstance(reads, dict) else ""
        out[facet] = raw if raw in ("ok", "not_read", "failed") else "indeterminate"
    return out


# The engine's opted-in quota read (``GET /v2/quota?view=constraint_freshness``,
# passed on unchanged by the host's passive quota view) gives every window its
# own freshness: a weekly window can be fresh while its 5-hour sibling's reset
# has passed and the snapshot as a whole is stale. All or nothing per answer.
CONSTRAINT_FRESHNESS = ("fresh", "stale", "unknown")


def freshness_projection(snapshots: Any) -> str:
    """How one answer's snapshots speak for their windows' freshness:
    ``legacy`` (no constraint carries its own), ``explicit`` (every one does,
    exactly) or ``invalid`` (anything in between, or a word outside
    CONSTRAINT_FRESHNESS). Judged over the whole answer, the host's own rule:
    a snapshot without it beside one with it is a mixed answer."""
    rows = [row for row in snapshots if isinstance(row, dict)] if isinstance(snapshots, list) else []
    groups = [row.get("constraints", []) for row in rows]
    items = [item for group in groups if isinstance(group, list) for item in group]
    if not any(isinstance(item, dict) and "freshness" in item for item in items):
        return "legacy"
    if all(isinstance(group, list) for group in groups) and all(
            isinstance(item, dict) and item.get("freshness") in CONSTRAINT_FRESHNESS for item in items):
        return "explicit"
    return "invalid"


def constraint_freshness(row: Dict[str, Any], constraint: Any, projection: str) -> str:
    """The one freshness of one window of one snapshot, which every reader of
    a window asks: the reserve and the tool (normalize), the account view
    (plugin.quota_for), cooldowns, the reset-credit counter and the collector.

    In a legacy answer the snapshot's conservative word speaks for each of its
    windows: absence never makes a window fresh. In an explicit one each window
    has its own. In an invalid one no window is fresh, and none is stripped
    back to legacy. The engine keeps a snapshot's stale or unknown on every
    window and only ages a fresh one, so a fresh window under any other
    snapshot word contradicts it and is not fresh either."""
    aggregate = row.get("freshness")
    if projection == "legacy":
        return aggregate if aggregate in CONSTRAINT_FRESHNESS else "unknown"
    own = constraint.get("freshness") if projection == "explicit" and isinstance(constraint, dict) else None
    if own not in CONSTRAINT_FRESHNESS:
        return "unknown"
    if own == "fresh" and aggregate not in ("fresh", "stale"):
        return "unknown"
    return own


def stale_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    """A snapshot answered from a kept quota facet: never fresh, as a whole or
    in any window. A copy; the kept answer itself is never changed. A window's
    own word is only aged (fresh to stale), so the answer keeps its kind: a
    legacy one stays legacy and a malformed one stays malformed."""
    out = dict(row, freshness="stale")
    constraints = row.get("constraints")
    if isinstance(constraints, list):
        out["constraints"] = [dict(item, freshness="stale")
                              if isinstance(item, dict) and item.get("freshness") == "fresh" else item
                              for item in constraints]
    return out


def _models_of(raw: Any) -> Tuple[str, ...]:
    """Every model a limit applies to, sorted, each name in full: what the
    scope's identity is taken from. Display bounds are applied afterwards."""
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({item.strip() for item in raw if isinstance(item, str) and item.strip()}))


def _shown_models(scope_models: Tuple[str, ...]) -> Tuple[str, ...]:
    """The part of a scope that is shown: its first MAX_MODELS names, each cut
    to MAX_TEXT. The caller says how many names it leaves out."""
    return tuple(name[:MAX_TEXT] for name in scope_models[:MAX_MODELS])


def is_reset_credit(constraint: Dict[str, Any], harness: str) -> bool:
    """Only the engine's reset_credits constraint, with its optional namespace.

    A label mentioning credits does not turn another constraint into this
    counter. Its count has no relationship to a used ratio or a quota window.
    """
    return meaning_of(harness, _text(constraint.get("id"), 120), "") == "reset_credits"


_CREDIT_NOUN = r"(?:manual\s+)?reset\s+credits?"
_CREDIT_LABELS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rf"([0-9]+)\s+{_CREDIT_NOUN}(?:\s+(?:available|remaining|left))?",
    rf"{_CREDIT_NOUN}(?:\s+(?:available|remaining|left))?\s*:\s*([0-9]+)"
    r"(?:\s+(?:available|remaining|left))?",
    rf"{_CREDIT_NOUN}\s*\(\s*([0-9]+)\s+(?:available|remaining|left)\s*\)",
))


def reset_credit_count(label: Any) -> Optional[int]:
    """Read one explicit whole count from the provider's label, or no count.

    The wire constraint currently carries its count in text. Full matches
    avoid reading a price, a date, a fraction, a negative or one of several
    numbers as available credits. An unfamiliar label stays visible verbatim
    (bounded) with an unreadable count. No other constraint field is guessed.
    """
    if not isinstance(label, str) or len(label) > 512:
        return None
    for pattern in _CREDIT_LABELS:
        match = pattern.fullmatch(label.strip())
        if match:
            count = int(match.group(1))
            # Output travels through JavaScript; never publish a rounded count.
            return count if count <= 2 ** 53 - 1 else None
    return None


def reset_credits_of(rows: Sequence[Dict[str, Any]], harness: str, now: float,
                     quota_read: str = "ok", projection: Optional[str] = None) -> Dict[str, Any]:
    """One account's manual counter, apart from quota ratios and timed resets.

    Exact account attribution belongs to the caller (attribute/quota_for).
    Newest fresh evidence wins, otherwise newest stale evidence stays dated.
    Same-moment reports must agree exactly; a malformed newest label never
    falls back silently to an older readable one. An unknown count is None,
    never zero. Each report's freshness is its own counter's, as the engine
    reported it (constraint_freshness, over the whole answer's ``projection``;
    without one, over ``rows``).
    """
    if projection is None:
        projection = freshness_projection(list(rows))
    out: Dict[str, Any] = {
        "state": "unknown", "count": None, "observed_at": None,
        "age_seconds": None, "source": "", "label": "", "count_origin": None,
        "reason": "not_reported" if quota_read == "ok" else "quota_" + quota_read,
        "reports": [],
    }
    if quota_read != "ok":
        return out
    reports: List[Dict[str, Any]] = []
    for row in rows[:MAX_SNAPSHOTS]:
        if not isinstance(row, dict):
            continue
        constraints = row.get("constraints")
        observed = parse_instant(row.get("observed_at"))
        for constraint in (constraints if isinstance(constraints, list) else [])[:MAX_CONSTRAINTS]:
            if not isinstance(constraint, dict) or not is_reset_credit(constraint, harness):
                continue
            label = constraint.get("label")
            count = reset_credit_count(label)
            reports.append({
                "label": _text(label, 512),
                "source": _text(row.get("source"), 64) or "unnamed",
                "freshness": constraint_freshness(row, constraint, projection),
                "observed_at": iso_exact(observed) if observed is not None else None,
                "count": count, "count_origin": "label" if count is not None else None,
            })
    out["reports"] = reports
    if not reports:
        return out
    fresh = [r for r in reports if r["freshness"] == "fresh"]
    candidates = fresh or reports
    chosen = max(candidates, key=lambda r: (
        parse_instant(r["observed_at"]) if r["observed_at"] else -math.inf, r["source"], r["label"]))
    observed = parse_instant(chosen["observed_at"])
    out.update({key: chosen[key] for key in ("observed_at", "source", "label")})
    if observed is None:
        out["reason"] = "no_observation_time"
        return out
    if observed > now + FUTURE_SKEW_SEC:
        out["reason"] = "observed_in_future"
        return out
    out["age_seconds"] = max(0, round(now - observed))
    newest = [r for r in candidates if r["observed_at"]
              and observed - parse_instant(r["observed_at"]) <= SAME_INSTANT_SEC]
    if any(r["count"] is None for r in newest):
        out.update(state="unreadable", reason="count_unreadable")
    elif len({r["count"] for r in newest}) > 1:
        out.update(state="conflict", reason="sources_disagree")
    else:
        out.update(state="current" if fresh else "last_known", count=chosen["count"],
                   count_origin="label", reason="" if fresh else "not_fresh")
    return out


def family_reset_credits(credits: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """An attributed account counted once, with dated counts kept separate.

    Totals are counters reported by these profiles, not a promise of usable
    resets, distinct vendor pools, restored quota or currency value.
    """
    current = [c for c in credits if c["state"] == "current"]
    known = [c for c in credits if c["state"] == "last_known"]
    out: Dict[str, Any] = {
        "accounts": len(credits), "count": sum(c["count"] for c in current) if current else None,
        "current_accounts": len(current),
        "last_known_count": sum(c["count"] for c in known) if known else None,
        "last_known_accounts": len(known),
        "unknown_accounts": sum(c["state"] == "unknown" for c in credits),
        "unreadable_accounts": sum(c["state"] == "unreadable" for c in credits),
        "conflict_accounts": sum(c["state"] == "conflict" for c in credits),
    }
    for prefix, values in (("", current), ("last_known_", known)):
        observed = [parse_instant(c["observed_at"]) for c in values]
        out[prefix + "oldest_observed_at"] = iso(min(observed)) if observed else None
        out[prefix + "newest_observed_at"] = iso(max(observed)) if observed else None
    return out


def reading_of(constraint: Dict[str, Any], harness: str, subject_id: str, *, source: str,
               fresh: bool, observed: Optional[float], plan: str = "",
               plan_key: str = "") -> Optional[Reading]:
    """One quota window of one snapshot, read the one way this skill reads
    it — the reserve overview and the account view both call this. None for
    a constraint that is not a quota window (a cooldown or a credit counter:
    no window length and no ratio)."""
    if is_reset_credit(constraint, harness):
        return None
    window = window_of(constraint.get("window_seconds"))
    raw_ratio = constraint.get("used_ratio")
    if window is None and raw_ratio is None:
        return None
    scope_models = _models_of(constraint.get("applies_to_models"))
    models = _shown_models(scope_models)
    ratio, problem = ratio_of(raw_ratio)
    label = _text(constraint.get("label") or constraint.get("id")) or "limit"
    meaning = meaning_of(harness, _text(constraint.get("id"), 120), label)
    return Reading(
        harness=harness, subject_id=subject_id, source=source, fresh=fresh,
        observed_at=observed, key=group_key(harness, meaning, window, scope_models),
        meaning=meaning, label=label, window=window, models=models, ratio=ratio,
        ratio_problem=problem, resets_at=parse_instant(constraint.get("resets_at")),
        plan=plan, plan_key=plan_key, scope=_scope_hash(scope_models),
        models_omitted=len(scope_models) - len(models),
    )


def _accounts_of(payload: Dict[str, Any], unified: bool) -> Dict[Tuple[str, str], Account]:
    block = payload.get("profiles")
    block = block if isinstance(block, dict) else {}
    out: Dict[Tuple[str, str], Account] = {}
    natives = block.get("harnessAccounts")
    # A unified engine owns every account as a named row; a compatibility
    # native row on such an engine would count one login twice.
    for row in (natives if isinstance(natives, list) and not unified else []):
        if not isinstance(row, dict):
            continue
        hid = _text(row.get("harness_id"), 64)
        if not hid:
            continue
        identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
        out[(hid, "")] = Account(
            harness=hid, subject_id="", kind="native",
            enabled=row.get("native_credentials_enabled") is not False,
            signed_in=bool(row.get("native_login_detected")),
            auth_failed=False,
            plan=_text(identity.get("plan")),
            identity=_text(identity.get("email"), 200).lower(),
        )
    wrappers = block.get("profiles")
    for wrapper in (wrappers if isinstance(wrappers, list) else []):
        if not isinstance(wrapper, dict):
            continue
        profile = wrapper.get("profile") if isinstance(wrapper.get("profile"), dict) else {}
        status = wrapper.get("status") if isinstance(wrapper.get("status"), dict) else {}
        identity = wrapper.get("identity") if isinstance(wrapper.get("identity"), dict) else {}
        hid = _text(profile.get("harness_id"), 64)
        pid = _text(profile.get("profile_id"), 200)
        if not hid or not pid:
            continue
        verification = str(status.get("verification") or "")
        out[(hid, pid)] = Account(
            harness=hid, subject_id=pid, kind="profile",
            # Absent reads as enabled, as the host's own account list does:
            # a missing field is not the owner switching the account off.
            enabled=False if profile.get("enabled") is False else True,
            signed_in=verification == "passed" or status.get("availability") == "available",
            auth_failed=verification == "failed",
            plan=_text(identity.get("plan") or status.get("plan_label")),
            identity=_text(identity.get("email"), 200).lower(),
        )
    return out


def attribute(payload: Any) -> Attributed:
    """Which account each snapshot belongs to — one rule, which the reserve
    (:func:`normalize`) and the account view (plugin.quota_for) both follow.

    A reading belongs to the engine's exact subject ``(harness, subject_id)``
    when the account list holds it (or was not read). On a unified engine the
    reserved ``<harness>-default`` row inherits the legacy null subject its
    login was keyed by before migration — the host's own rule — only while
    the account list holds that row and it has no fresh reading of its own;
    otherwise a null-subject reading is superseded or unattributed, never
    given to an account by guess."""
    payload = payload if isinstance(payload, dict) else {}
    reads = facet_reads(payload)
    unified = payload.get("unified_accounts") is True
    accounts_known = reads["accounts"] == "ok"
    quota_known = reads["quota"] == "ok"
    accounts = _accounts_of(payload, unified) if accounts_known else {}

    snapshots = payload.get("quota") if quota_known else []
    projection = freshness_projection(snapshots)
    snapshots = [row for row in (snapshots if isinstance(snapshots, list) else [])
                 if isinstance(row, dict)][:MAX_SNAPSHOTS]
    malformed = 0

    def subject_of(row: Dict[str, Any]) -> Tuple[str, str]:
        subject = row.get("subject") if isinstance(row.get("subject"), dict) else {}
        raw = subject.get("subject_id")
        sid = "" if raw is None else _text(raw, 200)
        return _text(subject.get("harness"), 64), sid

    default_has_fresh: Set[str] = set()
    for row in snapshots:
        hid, sid = subject_of(row)
        if hid and sid == f"{hid}-default" and str(row.get("freshness") or "") == "fresh":
            default_has_fresh.add(hid)

    rows: List[Tuple[str, str, Dict[str, Any]]] = []
    unattributed: Counter = Counter()
    superseded: Counter = Counter()
    for row in snapshots:
        hid, sid = subject_of(row)
        if not hid:
            malformed += 1
            continue
        if sid == "":
            if unified:
                default_id = f"{hid}-default"
                if (hid, default_id) not in accounts and accounts_known:
                    unattributed[hid] += 1
                    continue
                if not accounts_known:
                    unattributed[hid] += 1
                    continue
                if hid in default_has_fresh:
                    superseded[hid] += 1
                    continue
                sid = default_id
            elif accounts_known and (hid, "") not in accounts:
                unattributed[hid] += 1
                continue
        elif accounts_known and (hid, sid) not in accounts:
            unattributed[hid] += 1
            continue
        rows.append((hid, sid, row))
    return Attributed(
        reads=reads, unified=unified, accounts=accounts, accounts_known=accounts_known,
        quota_known=quota_known, rows=rows, unattributed=unattributed,
        superseded=superseded, malformed=malformed, freshness=projection,
    )


def cooldowns_of(row: Dict[str, Any], harness: str, subject_id: str, now: float,
                 projection: Optional[str] = None) -> List[Cooldown]:
    """The cooldowns one attributed snapshot reports that hold at ``now`` —
    what the reserve's "cooling" restriction and the account view both read.

    A constraint's ``cooldown_until`` holds while it is ahead, or when it
    cannot be read (the conservative reading the host's own route verdict
    uses), from a fresh or a stale reading alike: the engine may still
    honour a live cooldown. One that has passed is history, not a
    restriction. An availability state of ``cooldown`` holds from a fresh
    reading only, by the same clock: while its ``resets_at`` is ahead, not
    reported or unreadable, never once a reported end has passed — a fresh
    reading carried past that end would otherwise hold the account after the
    engine let it go. Each is typed evidence of its own — never inferred from a
    share, and never a share or a reset itself. A model-scoped exhaustion the
    availability reports is not a cooldown: :func:`exhaustions_of` reads it.
    Which reading reported it: the snapshot's own word for its availability,
    the window's own freshness for a ``cooldown_until`` (constraint_freshness)."""
    fresh = str(row.get("freshness") or "") == "fresh"
    if projection is None:
        projection = freshness_projection([row])
    observed = parse_instant(row.get("observed_at"))
    source = _text(row.get("source"), 64) or "unnamed"
    out: List[Cooldown] = []

    def fact(raw: Any, scope_models: Tuple[str, ...], kind: str, label: str,
             fresh: bool = fresh) -> Cooldown:
        reported = raw not in (None, "")
        shown = _shown_models(scope_models)
        return Cooldown(
            harness=harness, subject_id=subject_id, models=shown,
            scope=_scope_hash(scope_models), until=parse_instant(raw) if reported else None,
            until_text=_text(raw, 64) if reported else "", kind=kind, label=label,
            source=source, fresh=fresh, observed_at=observed,
            models_omitted=len(scope_models) - len(shown))

    availability = row.get("availability") if isinstance(row.get("availability"), dict) else {}
    if fresh and str(availability.get("state") or "") == "cooldown":
        raw = availability.get("resets_at")
        until = parse_instant(raw) if raw not in (None, "") else None
        if until is None or until > now:
            out.append(fact(raw, (), "availability", "cooldown"))
    constraints = row.get("constraints")
    for constraint in (constraints if isinstance(constraints, list) else [])[:MAX_CONSTRAINTS]:
        if not isinstance(constraint, dict):
            continue
        raw = constraint.get("cooldown_until")
        if raw in (None, ""):
            continue
        until = parse_instant(raw)
        if until is not None and until <= now:
            continue
        label = _text(constraint.get("label") or constraint.get("id")) or "cooldown"
        out.append(fact(raw, _models_of(constraint.get("applies_to_models")), "constraint", label,
                        constraint_freshness(row, constraint, projection) == "fresh"))
    return out


def exhaustions_of(row: Dict[str, Any], harness: str, subject_id: str,
                   now: float) -> List[Exhaustion]:
    """Every model-scoped exhaustion one attributed snapshot's availability
    reports (``model_scoped_exhaustions``: ``{constraint_id,
    applies_to_models, resets_at}``), fresh or stale, each as reported.

    One is live only while its reported reset parses and is still ahead: the
    engine reported the limit out until a moment still to come, and that
    report is taken as it stands, whichever reading said so. One reported with
    no reset, with one that cannot be read, or with one that has passed says
    what the engine reported and nothing about now: it is disclosed, never
    made a hold, and no reset is invented for it. Nothing here is a share —
    the limit's own number, where one was read, stays that limit's reading."""
    fresh = str(row.get("freshness") or "") == "fresh"
    observed = parse_instant(row.get("observed_at"))
    source = _text(row.get("source"), 64) or "unnamed"
    availability = row.get("availability") if isinstance(row.get("availability"), dict) else {}
    entries = availability.get("model_scoped_exhaustions")
    out: List[Exhaustion] = []
    for entry in (entries if isinstance(entries, list) else [])[:MAX_CONSTRAINTS]:
        if not isinstance(entry, dict):
            continue
        scope_models = _models_of(entry.get("applies_to_models"))
        shown = _shown_models(scope_models)
        raw = entry.get("resets_at")
        reported = raw not in (None, "")
        resets_at = parse_instant(raw) if reported else None
        if not reported:
            note = "not_reported"
        elif resets_at is None:
            note = "unreadable"
        elif resets_at <= now:
            note = "passed"
        else:
            note = ""
        out.append(Exhaustion(
            harness=harness, subject_id=subject_id,
            constraint_id=_text(entry.get("constraint_id"), 120), models=shown,
            models_omitted=len(scope_models) - len(shown), scope=_scope_hash(scope_models),
            resets_at=resets_at, resets_text=_text(raw, 64) if reported else "",
            live=not note, note=note, source=source, fresh=fresh, observed_at=observed))
    return out


def normalize(payload: Any, now: float,
              cached: Optional[Dict[str, float]] = None) -> Normalized:
    """Readings, accounts and restrictions out of one status payload.

    ``cached`` names the facets the caller answered from an earlier read
    (facet -> when it was read); the payload then already carries that
    read's data, with every cached quota reading marked stale."""
    payload = payload if isinstance(payload, dict) else {}
    attributed = attribute(payload)
    reads = attributed.reads
    accounts = attributed.accounts

    harness_order: List[str] = []
    families: Dict[str, str] = {}
    harnesses = payload.get("harnesses")
    for row in (harnesses if isinstance(harnesses, list) else []):
        if isinstance(row, dict) and _text(row.get("id"), 64):
            hid = _text(row.get("id"), 64)
            if hid not in harness_order:
                harness_order.append(hid)
            if reads["catalog"] == "ok":
                families[hid] = _text(row.get("display_name") or row.get("displayName") or hid)
    for hid, _sid in accounts:
        if hid not in harness_order:
            harness_order.append(hid)

    malformed = attributed.malformed
    projection = attributed.freshness
    readings: List[Reading] = []
    cooling_all: Set[Tuple[str, str]] = set()
    cooling_scoped: Dict[Tuple[str, str], Set[str]] = {}
    exhausted_scoped: Dict[Tuple[str, str], Set[str]] = {}

    for hid, sid, row in attributed.rows:
        if hid not in harness_order:
            harness_order.append(hid)

        observed = parse_instant(row.get("observed_at"))
        source = _text(row.get("source"), 64) or "unnamed"
        plan = _text((row.get("subject") or {}).get("plan_label"))
        account = accounts.get((hid, sid))
        plan_key = plan_evidence(plan, account.plan if account is not None else None)
        for cooldown in cooldowns_of(row, hid, sid, now, projection):
            if cooldown.scope == "-":
                cooling_all.add((hid, sid))
            else:
                cooling_scoped.setdefault((hid, sid), set()).add(cooldown.scope)
        for exhaustion in exhaustions_of(row, hid, sid, now):
            # A model's limit, never the account: one that names no model
            # cannot hold any window.
            if exhaustion.live and exhaustion.scope != "-":
                exhausted_scoped.setdefault((hid, sid), set()).add(exhaustion.scope)
        constraints = row.get("constraints")
        for constraint in (constraints if isinstance(constraints, list) else [])[:MAX_CONSTRAINTS]:
            if not isinstance(constraint, dict):
                malformed += 1
                continue
            fresh = constraint_freshness(row, constraint, projection) == "fresh"
            reading = reading_of(constraint, hid, sid, source=source, fresh=fresh,
                                 observed=observed, plan=plan, plan_key=plan_key)
            if reading is not None:  # else a cooldown or a credit counter
                readings.append(reading)

    return Normalized(
        reads=reads, unified=attributed.unified, harness_order=harness_order,
        families=families, accounts=accounts, accounts_known=attributed.accounts_known,
        quota_known=attributed.quota_known, readings=readings, cooling_all=cooling_all,
        cooling_scoped=cooling_scoped, exhausted_scoped=exhausted_scoped,
        unattributed=attributed.unattributed,
        superseded=attributed.superseded, malformed=malformed,
        cached={k: float(v) for k, v in (cached or {}).items() if k in FACETS},
    )


def recordable(norm: Normalized, now: float) -> List[Reading]:
    """The readings the collector may keep: fresh, numeric, placed in time,
    of a cycle still running at the sweep. Every source is kept — which one to
    believe is decided at read time. Freshness is each window's own
    (constraint_freshness): a weekly window the engine still reports fresh is
    kept beside a 5-hour sibling whose own reset has passed, which is not.

    A reading whose own reported reset is at or before the sweep never vouches
    for that sweep, whatever freshness it carries: freshness was judged when
    the engine read it, and a reported reset ends the cycle that reading
    describes (the reserve's rule in resolve_member, the engine's own per-window
    rule). Kept, it would carry the ended cycle's value to the sweep."""
    out = []
    for reading in norm.readings:
        if not reading.fresh or reading.ratio is None or reading.observed_at is None:
            continue
        if reading.observed_at > now + FUTURE_SKEW_SEC:
            continue
        if reading.resets_at is not None and reading.resets_at <= now:
            continue
        out.append(reading)
    return out


# ---------------------------------------------------------------------------
# Per-group resolution


@dataclass
class Member:
    harness: str
    subject_id: str
    # measured | stale_only | invalid | conflicting | reset_passed, and
    # history_only for an account of the current roster whose limit this
    # answer carries no reading of, known only from the local history.
    status: str
    reading: Optional[Reading] = None
    remaining: float = 0.0
    reason: str = ""
    observed_at: Optional[float] = None
    plan: str = ""
    flags: Tuple[str, ...] = ()
    rate: Dict[str, Any] = field(default_factory=dict)
    pseudo: str = ""
    # The newest usable reading that is not current (see LastKnown), or None.
    last_known: Optional["LastKnown"] = None


@dataclass
class LastKnown:
    """A reading that is not current, kept as the dated fact it is.

    ``carried`` says whether the display may still count it, marked as last
    known: its reported reset is ahead, or — with none reported — it is no
    older than its window (LAST_KNOWN_NO_WINDOW_SEC without a window). A
    reading that is not carried stays here for the record; ``why`` says why
    it is not counted. ``observed_at`` is the source's own observation time,
    never when it was fetched. ``origin`` is where it was found: a stale
    reading in this answer (``payload``), the last answer that read the quota
    facet (``cached``), or the local history (``history``)."""
    ratio: float
    observed_at: float
    resets_at: Optional[float]
    source: str
    origin: str
    carried: bool
    why: str = ""


def carry_verdict(observed_at: float, resets_at: Optional[float], window: Optional[int],
                  now: float) -> Tuple[bool, str]:
    """Whether a last-known reading may still be carried at ``now`` — drawn
    dated, and counted only in the labelled last-known part of the widget's
    figure, never in ``measured`` — and why not. It is dated
    evidence of a cycle still running, not a bound on what is left now: use
    may have grown since, and a restore or an unreported reset may have
    lowered it. One whose reported reset has passed describes a cycle that
    has ended."""
    if resets_at is not None:
        return (True, "") if resets_at > now else (False, "reset_passed")
    limit = float(window) if window else LAST_KNOWN_NO_WINDOW_SEC
    return (True, "") if now - observed_at <= limit else (False, "too_old")


def last_known_of(readings: Sequence[Reading], now: float, origin: str
                  ) -> Tuple[Optional[LastKnown], str]:
    """The newest numeric, placed reading among ``readings`` that are not
    fresh, resolved by the same source policy as a current one (two sources
    that disagree give none). Readings of a cycle still running win over
    readings of an ended one. With none, why: ``missing`` (no usable stale
    reading at all) or the policy's own reason (``sources_disagree_on_…``) —
    evidence refused, which is not evidence absent."""
    usable = [r for r in readings if not r.fresh and r.ratio is not None
              and r.observed_at is not None and r.observed_at <= now + FUTURE_SKEW_SEC]
    if not usable:
        return None, "missing"
    running = [r for r in usable if r.resets_at is None or r.resets_at > now]
    pool = running or usable
    chosen, reason = _resolve_sources([(r.ratio, r.resets_at, r.observed_at, r.source, r) for r in pool])
    if reason or chosen is None:
        return None, reason or "missing"
    carried, why = carry_verdict(chosen.observed_at, chosen.resets_at, chosen.window, now)
    return LastKnown(ratio=chosen.ratio, observed_at=chosen.observed_at, resets_at=chosen.resets_at,
                     source=chosen.source, origin=origin, carried=carried, why=why), ""


def last_known_from_runs(runs: Sequence[Run], window: Optional[int], now: float) -> Optional[LastKnown]:
    """A last-known reading from the local history: the newest kept run of
    each source of one exact (subject, series), resolved by the policy a
    current reading's sources go through (_resolve_sources, as last_known_of
    does): runs of a cycle still running win over runs of an ended one, two
    sources that disagree on the reset or, at the same moment, on the value
    give none. The chosen run's own value and newest observation time. None
    for runs observed in the future."""
    usable = [run for run in runs if run.last_obs <= now + FUTURE_SKEW_SEC]
    if not usable:
        return None
    running = [run for run in usable if run.resets_at is None or run.resets_at > now]
    pool = running or usable
    chosen, reason = _resolve_sources([(run.ratio, run.resets_at, run.last_obs, run.source, run)
                                       for run in pool])
    if reason or chosen is None:
        return None
    carried, why = carry_verdict(chosen.last_obs, chosen.resets_at, window, now)
    return LastKnown(ratio=chosen.ratio, observed_at=chosen.last_obs, resets_at=chosen.resets_at,
                     source=chosen.source, origin="history", carried=carried, why=why)


@dataclass
class GroupCalc:
    key: str
    harness: str
    family: str
    meaning: str
    label: str
    window: Optional[int]
    models: Tuple[str, ...]
    members: List[Member]
    other_family_accounts: int
    possible_duplicates: int
    models_omitted: int = 0
    scope: str = "-"
    # Accounts of the current roster that report no reading of this limit
    # in this answer but whose last reading of it the local history keeps
    # (status history_only). Never counted as measured.
    extra: List[Member] = field(default_factory=list)

    @property
    def measured(self) -> List[Member]:
        return [m for m in self.members if m.status == "measured"]

    @property
    def slots(self) -> List[Member]:
        """Every account this limit is known to apply to: each one with a
        reading of it in this answer, fresh or not, and each one of the
        roster the history knows it for."""
        return list(self.members) + list(self.extra)


def _resolve_sources(candidates: List[Tuple[float, Optional[float], float, str, Any]]
                     ) -> Tuple[Any, str]:
    """One policy for valid, unexpired sources in current and historical views.

    Entries are (ratio, reset, observation time, source, original evidence).
    No source has priority. Reset conflicts concern all eligible sources;
    value conflicts concern the newest same-moment sources only.
    """
    if not candidates:
        return None, "missing"
    fixed = [r[1] for r in candidates if r[1] is not None]
    if fixed and max(fixed) - min(fixed) > RESET_TOLERANCE_SEC:
        return None, "sources_disagree_on_reset"
    observed = max(r[2] for r in candidates)
    newest = [r for r in candidates if observed - r[2] <= SAME_INSTANT_SEC]
    ratios = [r[0] for r in newest]
    if max(ratios) - min(ratios) > VALUE_TOLERANCE + RATIO_EPS:
        return None, "sources_disagree_on_value"
    return max(newest, key=lambda r: (r[0], r[2], r[3]))[4], ""


def resolve_member(readings: List[Reading], now: float) -> Member:
    """One account's current reading of one limit from every source that
    reports it, or why there is none (stale, unreadable, an ended cycle,
    sources that disagree). The reserve counts by it and the account view
    draws by it, so the two cannot call one reading two things."""
    first = readings[0]
    base = Member(harness=first.harness, subject_id=first.subject_id, status="stale_only")
    fresh = [r for r in readings if r.fresh]
    valid = []
    invalid_reason = ""
    for reading in fresh:
        if reading.observed_at is None:
            invalid_reason = invalid_reason or "no_observation_time"
        elif reading.observed_at > now + FUTURE_SKEW_SEC:
            invalid_reason = invalid_reason or "observed_in_future"
        elif reading.ratio is None:
            invalid_reason = invalid_reason or reading.ratio_problem or "missing"
        else:
            valid.append(reading)
    if not valid:
        if fresh:
            base.status = "invalid"
            base.reason = invalid_reason or "missing"
            base.observed_at = max((r.observed_at for r in fresh if r.observed_at), default=None)
            return base
        base.observed_at = max((r.observed_at for r in readings if r.observed_at), default=None)
        # Only stale readings: the newest usable one is the last known.
        base.last_known, refused = last_known_of(readings, now, "payload")
        if refused.startswith("sources_disagree"):
            # Said as such, and kept through every fallback (finish): an
            # older value elsewhere does not stand in for evidence refused.
            base.reason = "sources_disagree"
        return base
    # A reading whose reported reset has passed describes an ended cycle,
    # whichever source it came from; the same rule for every reading.
    current = [r for r in valid if r.resets_at is None or r.resets_at > now]
    if not current:
        base.status = "reset_passed"
        base.reason = "reported_reset_has_passed"
        base.observed_at = max(r.observed_at for r in valid)
        newest = max(valid, key=lambda r: r.observed_at)
        # Kept for the record only: its cycle has ended.
        base.last_known = LastKnown(ratio=newest.ratio, observed_at=newest.observed_at,
                                    resets_at=newest.resets_at, source=newest.source,
                                    origin="payload", carried=False, why="reset_passed")
        return base
    base.observed_at = max(r.observed_at for r in current)
    chosen, reason = _resolve_sources([
        (r.ratio, r.resets_at, r.observed_at, r.source, r) for r in current])
    if reason:
        base.status = "conflicting"
        base.reason = reason
        return base
    base.reading = chosen
    base.status = "measured"
    base.remaining = 1.0 - chosen.ratio
    return base


def _order_key(norm: Normalized, calc: GroupCalc) -> Tuple[Any, ...]:
    try:
        family_rank = norm.harness_order.index(calc.harness)
    except ValueError:
        family_rank = len(norm.harness_order)
    # Shorter windows first, shared limits before model-scoped ones, then the
    # limit most accounts report (every account that has any reading of it,
    # fresh or not, so a reading going stale does not reorder the list).
    return (
        family_rank, calc.harness,
        calc.window if calc.window else float("inf"),
        1 if (calc.models or calc.scope not in ("-", "")) else 0,
        -len(calc.members),
        calc.meaning, calc.models, calc.key,
    )


def resolve_groups(norm: Normalized, now: float) -> List[GroupCalc]:
    by_key: Dict[str, Dict[str, List[Reading]]] = {}
    for reading in norm.readings:
        by_key.setdefault(reading.key, {}).setdefault(reading.subject_id, []).append(reading)
    resolved = {key: [resolve_member(subjects[sid], now) for sid in sorted(subjects)]
                for key, subjects in by_key.items()}
    # A shared limit whose current reading is spent blocks the account's
    # other limits, whether or not its reset was reported: a missing reset is
    # unknown timing, not the absence of a restriction. A reading of an ended
    # cycle (reset passed), a stale or an unreadable one blocks nothing.
    spent: Dict[Tuple[str, str], Set[str]] = {}
    for key, members in resolved.items():
        for member in members:
            if member.status == "measured" and not member.reading.models \
                    and member.remaining <= RATIO_EPS:
                spent.setdefault((member.harness, member.subject_id), set()).add(key)
    norm.spent = spent
    groups: List[GroupCalc] = []
    for key, subjects in by_key.items():
        sample = next(iter(subjects.values()))[0]
        labels = Counter(r.label for rows in subjects.values() for r in rows if r.fresh) \
            or Counter(r.label for rows in subjects.values() for r in rows)
        top = max(labels.values())
        label = sorted(name for name, count in labels.items() if count == top)[0]
        members = resolved[key]
        for member in members:
            _decorate(norm, member, key, sample.scope, spent)
        family_accounts = [a for (h, _s), a in norm.accounts.items() if h == sample.harness]
        present = {m.subject_id for m in members}
        other = sum(1 for a in family_accounts if a.subject_id not in present)
        groups.append(GroupCalc(
            key=key, harness=sample.harness,
            family=norm.families.get(sample.harness, sample.harness),
            meaning=sample.meaning, label=label, window=sample.window,
            models=sample.models, members=members,
            other_family_accounts=other,
            possible_duplicates=_shared_identities(norm, members),
            models_omitted=sample.models_omitted,
            scope=sample.scope,
        ))
    groups.sort(key=lambda calc: _order_key(norm, calc))
    return groups[:MAX_GROUPS]


def _decorate(norm: Normalized, member: Member, key: str, scope: str,
              spent: Dict[Tuple[str, str], Set[str]]) -> None:
    subject = (member.harness, member.subject_id)
    account = norm.accounts.get(subject)
    reading = member.reading
    member.plan = (account.plan if account and account.plan else "") \
        or (reading.plan if reading else "")
    if not member.plan:
        member.plan = next((r.plan for r in norm.readings
                            if (r.harness, r.subject_id) == subject and r.fresh and r.plan), "")
    flags: List[str] = []
    if not norm.accounts_known or norm.accounts_cached:
        # A roster kept from an earlier answer says which accounts there are,
        # never whether they are enabled or signed in now.
        flags.append("account_state_unknown")
    elif account is not None:
        if account.enabled is False:
            flags.append("disabled")
        if not account.signed_in:
            flags.append("signed_out")
        if account.auth_failed:
            flags.append("auth_failed")
    if subject in norm.cooling_all or (scope != "-" and scope in norm.cooling_scoped.get(subject, set())):
        flags.append("cooling")
    # The engine reports exactly this model scope out until a reset still
    # ahead while the share counted here is not at the limit (another source,
    # a newer reading): the share is held back. A share already at the limit
    # says it itself, and is not counted a second time as a restriction.
    if scope != "-" and scope in norm.exhausted_scoped.get(subject, set()) \
            and member.status == "measured" and member.remaining > RATIO_EPS:
        flags.append("model_exhausted")
    if any(other != key for other in spent.get(subject, set())):
        flags.append("other_limit_spent")
    member.flags = tuple(flags)


def _shared_identities(norm: Normalized, members: List[Member]) -> int:
    """How many measured accounts share a sign-in with another measured one.

    Two profiles signed in to one vendor account may draw on one pool, and
    then the sum counts it twice. The address alone does not prove that, so
    nothing is merged: the count is disclosed instead, and the address itself
    never leaves this function.
    """
    seen: Counter = Counter()
    for member in members:
        if member.status != "measured":
            continue
        account = norm.accounts.get((member.harness, member.subject_id))
        if account and account.identity:
            seen[account.identity] += 1
    return sum(count for count in seen.values() if count > 1)


# ---------------------------------------------------------------------------
# Recent pace


def _history_runs(view: Optional[HistoryView], member: Member, key: str) -> List[Run]:
    if view is None or not member.pseudo:
        return []
    return list(view.runs.get((member.pseudo, key), []))


def _with_live(runs: List[Run], reading: Reading, now: float,
               view: Optional[HistoryView]) -> List[Run]:
    """The history of one source plus the reading just read, when it is newer
    than anything recorded: a real observation the collector has not stored
    yet, never an invented one. After a gap in the watch it starts a new run,
    even when the host still reports the same cached observation — and a
    sweep since the last sighting (failed, or one that did not see this
    source) is such a gap, before the next sweep has written it down."""
    last = runs[-1] if runs else None
    gap = last is not None and (now - last.last_seen > SIGHTING_GAP_SEC
                                or unseen_since(view, last.last_seen))
    live = Run(
        source=reading.source, ratio=reading.ratio, resets_at=reading.resets_at,
        plan=reading.plan_key, first_obs=reading.observed_at, last_obs=reading.observed_at,
        n_obs=1, first_seen=now, last_seen=now, after_gap=gap,
    )
    if last is None:
        return [live]
    if reading.observed_at < last.last_obs - 1e-6:
        return runs
    if gap:
        runs.append(live)
    elif abs(reading.observed_at - last.last_obs) <= 1e-6:
        if same_content(last, reading):
            runs[-1] = replace(last, last_seen=max(last.last_seen, now))
        else:
            runs.append(replace(live, after_correction=True))
    elif extends(last, live):
        runs[-1] = replace(last, last_obs=reading.observed_at, n_obs=last.n_obs + 1,
                           last_seen=now, resets_at=reading.resets_at)
    else:
        runs.append(live)
    return runs


def _quantized(values: Iterable[float]) -> bool:
    return all(abs(v * 100 - round(v * 100)) < 1e-6 for v in values)


def member_rate(view: Optional[HistoryView], member: Member, now: float) -> Dict[str, Any]:
    """Endpoint slope over the trailing comparable stretch inside the last hour.

    Only observations of the chosen source count: two sources of one account
    round differently, and mixing them would turn rounding into use. Only
    what the collector watched counts: an observation made before the
    collector's current unbroken run of good sweeps began — or before this
    source first appeared in its current uninterrupted watch segment — is not a
    starting point (unless the same value was observed again after it, which
    pins it there). So a pace never spans an outage, whatever cached reading
    the host reports afterwards, and never spans a sweep that did not see
    this source.
    """
    reading = member.reading
    if view is None or view.state == "unavailable":
        return {"state": "unavailable", "reason": "history_unavailable"}
    window_start = now - RATE_WINDOW_SEC
    # Only what the tool's narrower history read also sees, so the widget and
    # the tool compute the same pace whatever else the chart asked for.
    horizon = rate_since(now)
    runs = [run for run in _history_runs(view, member, reading.key)
            if run.source == reading.source and run.last_seen >= horizon]
    runs = _with_live(runs, reading, now, view)
    watch = len(runs) - 1
    while watch > 0 and not watched_gap(runs[watch - 1], runs[watch], view):
        watch -= 1
    watched_from = view.watched_since if view.watched_since is not None else now
    # A healthy collector does not imply that this individual source was
    # present. Bound every segment, including a source's first appearance.
    watched_from = max(watched_from, runs[watch].first_seen)
    lower = max(window_start, watched_from)
    stop_reason = ""
    first = len(runs) - 1
    while first > 0:
        why = boundary(runs[first - 1], runs[first], view)
        if why:
            stop_reason = why
            break
        if runs[first - 1].last_obs < lower:
            break
        first -= 1
    if runs[first].after_correction:
        # Corrected cached content was learned at this sighting. Its old
        # timestamp cannot manufacture a watched baseline before the correction.
        lower = max(lower, runs[first].first_seen)
    points: List[Tuple[float, float]] = []
    for run in runs[first:]:
        if run.last_obs < lower:
            continue
        start_at: Optional[float] = None
        if run.first_obs >= lower:
            start_at = run.first_obs
        else:
            # One unchanged value observed before and after the lower bound
            # is taken as the value there (the estimator's assumption: the
            # same reading on both sides, nothing observed between).
            start_at = lower
        if not points or start_at > points[-1][0]:
            points.append((start_at, run.ratio))
        if run.last_obs > points[-1][0]:
            points.append((run.last_obs, run.ratio))
    warming = view.watched_since is None or view.watched_since > window_start
    span = points[-1][0] - points[0][0] if len(points) >= 2 else 0.0
    if len(points) < 2 or span < MIN_RATE_SPAN_SEC:
        if stop_reason and runs[first].first_obs > window_start:
            reason = stop_reason
        elif watched_from > window_start and (watch > 0 or view.collecting_since is not None
                                              and view.collecting_since < watched_from):
            reason = "gap"
        elif len(points) < 2:
            reason = "few_observations"
        else:
            reason = "short_span"
        return {
            "state": "warming_up" if warming else "insufficient",
            "reason": reason,
            "span_seconds": round(span),
        }
    used = points[-1][1] - points[0][1]
    rate = max(0.0, used) / span * 3600.0
    out: Dict[str, Any] = {
        "state": "ok",
        "windows_per_hour": rate,
        "span_seconds": round(span),
        "observations": len(points),
        "zero_growth": used <= RATIO_EPS,
        # The observed net change of the used ratio over the span, as read:
        # what the pace is computed from, said exactly.
        "change": round(max(0.0, used), 6),
        "from": points[0][0],
        "to": points[-1][0],
    }
    if stop_reason and runs[first].first_obs > window_start:
        out["cut_by"] = stop_reason
    if _quantized(v for _t, v in points):
        out["resolution_windows_per_hour"] = 0.01 / span * 3600.0
    return out


# ---------------------------------------------------------------------------
# Summary


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def account_key(harness: str, subject_id: str) -> str:
    """The widget's own key for an account: ``<harness>:<profile id>``, or
    ``<harness>:native`` for a vendor CLI login. Route output only — the
    model tool never carries it."""
    return f"{harness}:{subject_id or 'native'}"


# Why an account of a limit has no value to show, from its member status.
_UNKNOWN_WHY = {
    "stale_only": "not_read",
    "invalid": "unreadable",
    "conflicting": "sources_disagree",
    "reset_passed": "reset_passed",
}


def reaches_limit(member: Member, now: float) -> Optional[Tuple[float, bool]]:
    """When one qualified account, continuing its observed net change, would
    reach the limit — and whether that is before a reported reset (True) or
    with no reset reported (False). None when it would not: no growth, none
    left, or the reported reset comes first. The one predicate behind the
    account sentence, the bar and the tool's counts."""
    rate = member.rate or {}
    per_hour = rate.get("windows_per_hour") or 0.0
    if rate.get("state") != "ok" or per_hour <= RATIO_EPS or member.remaining <= RATIO_EPS:
        return None
    at = now + member.remaining / per_hour * 3600.0
    reset = member.reading.resets_at if member.reading else None
    if reset is None:
        return at, False
    return (at, True) if at < reset else None


def _bar_pace(member: Member, now: float) -> Optional[Dict[str, Any]]:
    """One measured account's own recent pace, for the widget's account
    sentence: the observed net change and its span, and — only when that
    pace, continued, would reach the limit before the reported reset, or with
    no reset reported (``reset_reported`` says which) — when. Conditional on
    the pace continuing; not a forecast."""
    rate = member.rate or {}
    if rate.get("state") != "ok":
        return {"state": rate.get("state", "unavailable"), "reason": rate.get("reason", "")} if rate else None
    out: Dict[str, Any] = {
        "state": "ok",
        "windows_per_hour": _round(rate.get("windows_per_hour"), 4),
        "span_seconds": rate.get("span_seconds"),
        "change": rate.get("change"),
    }
    reach = reaches_limit(member, now)
    if reach is not None:
        out["reaches_limit_at"] = iso(reach[0])
        out["reset_reported"] = reach[1]
    return out


def _bars(calc: GroupCalc, now: float, named: bool = False) -> List[Dict[str, Any]]:
    """One entry per account this limit is known to apply to, fullest first.

    A measured account is ``current``; one whose carried last-known reading
    is shown instead is ``last_known`` (dated, never fresh); any other is
    ``unknown`` and stands after the rest — never a zero. Accounts with a
    value are ordered by the share left alone (then at the limit, then
    restricted, then the key), so an account flapping between a current and
    a last-known reading of the same value keeps its place. ``named`` adds
    the widget's account key (account_key); without it nothing names one."""
    known: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for member in calc.slots:
        entry: Dict[str, Any] = {
            "_key": account_key(member.harness, member.subject_id),
            "restricted": bool(member.flags),
            "flags": list(member.flags),
        }
        lk = member.last_known
        if member.status == "measured":
            entry.update({
                "state": "current", "left": round(member.remaining, 4),
                "at_limit": member.remaining <= RATIO_EPS,
                "observed_at": iso(member.observed_at),
                "resets_at": iso(member.reading.resets_at) if member.reading else None,
            })
            pace = _bar_pace(member, now)
            if pace:
                entry["pace"] = pace
            known.append(entry)
            continue
        if lk is not None and lk.carried:
            left = 1.0 - lk.ratio
            entry.update({
                "state": "last_known", "left": round(left, 4), "at_limit": left <= RATIO_EPS,
                "observed_at": iso(lk.observed_at), "resets_at": iso(lk.resets_at),
                "origin": lk.origin,
                "age_seconds": max(0, round(now - lk.observed_at)),
            })
            known.append(entry)
            continue
        entry.update({
            "state": "unknown", "left": None, "at_limit": False,
            "why": (lk.why if lk is not None and lk.why else
                    "sources_disagree" if member.reason == "sources_disagree" else
                    _UNKNOWN_WHY.get(member.status, member.reason or "not_read")),
            "observed_at": iso(member.observed_at),
        })
        if lk is not None:
            # Kept for the record: the value it last had, and when.
            entry["last_reading"] = {"left": round(1.0 - lk.ratio, 4), "observed_at": iso(lk.observed_at),
                                     "resets_at": iso(lk.resets_at), "origin": lk.origin}
        unknown.append(entry)
    known.sort(key=lambda b: (-b["left"], b["at_limit"], b["restricted"], b["_key"]))
    unknown.sort(key=lambda b: b["_key"])
    out = known + unknown
    for entry in out:
        key = entry.pop("_key")
        if named:
            entry["account"] = key
    return out


def _model_scope(calc: GroupCalc) -> str:
    """``none`` (a shared limit), ``named`` (tied to the models ``models``
    lists) or ``names_unknown`` (tied to models whose names this answer does
    not carry: only the history's scope id is known)."""
    if calc.models:
        return "named"
    return "none" if calc.scope in ("-", "") else "names_unknown"


def _group_output(calc: GroupCalc, now: float, named: bool = False) -> Dict[str, Any]:
    measured = calc.measured
    windows = sum(m.remaining for m in measured)
    counts = Counter(m.status for m in calc.members)
    restrictions: Dict[str, Dict[str, Any]] = {}
    unrestricted = 0.0
    for member in measured:
        if not member.flags:
            unrestricted += member.remaining
        for flag in member.flags:
            slot = restrictions.setdefault(flag, {"accounts": 0, "windows": 0.0})
            slot["accounts"] += 1
            slot["windows"] += member.remaining
    for slot in restrictions.values():
        slot["windows"] = _round(slot["windows"])

    plans: Dict[str, Dict[str, Any]] = {}
    for member in measured:
        slot = plans.setdefault(member.plan or "", {"accounts": 0, "windows": 0.0})
        slot["accounts"] += 1
        slot["windows"] += member.remaining
    breakdown = [
        {"plan": plan or "not reported", "accounts": slot["accounts"],
         "windows": _round(slot["windows"])}
        for plan, slot in sorted(plans.items(), key=lambda item: (item[0] == "", item[0]))
    ]

    observed = [m.observed_at for m in measured if m.observed_at is not None]
    stale_seen = [m.observed_at for m in calc.members
                  if m.status == "stale_only" and m.observed_at is not None]

    fixed = [(m.reading.resets_at, m) for m in measured
             if m.reading and m.reading.resets_at is not None and m.reading.resets_at > now]
    next_reset = None
    next_returns = None
    if fixed:
        soonest = min(at for at, _m in fixed)
        next_reset = {
            "at": iso(soonest),
            "accounts": sum(1 for at, _m in fixed if at - soonest <= RESET_TOLERANCE_SEC),
        }
        # What a full refill of those accounts would give back if nothing more
        # were used before it: their share used now. The chart's "no new use"
        # schedule says the same of its first event (reset_events) when that
        # event ends inside the chart's span (horizon_events).
        next_returns = round(sum(1.0 - m.remaining for at, m in fixed
                                 if at - soonest <= RESET_TOLERANCE_SEC), 4)
    pace_to_reset = None
    if fixed:
        pace_to_reset = {
            "windows_per_hour": _round(sum(m.remaining / ((at - now) / 3600.0) for at, m in fixed), 3),
            "accounts": len(fixed),
        }

    known = [m for m in measured if m.rate.get("state") == "ok"]
    reasons: Counter = Counter()
    for member in measured:
        if member.rate.get("state") != "ok":
            reasons[member.rate.get("state", "unavailable") + ":" + member.rate.get("reason", "")] += 1
    if measured and len(known) == len(measured):
        pace_state = "ok"
    elif known:
        pace_state = "partial"
    elif any(m.rate.get("state") == "warming_up" for m in measured):
        pace_state = "warming_up"
    elif any(m.rate.get("state") == "unavailable" for m in measured):
        pace_state = "unavailable"
    else:
        pace_state = "insufficient" if measured else "no_measured_accounts"
    # One predicate with the bars' (reaches_limit): before a reported reset,
    # or — counted apart, never as "before its reset" — with none reported.
    reaching = [(m, reaches_limit(m, now)) for m in known]
    exhaust_before = [r[0] for _m, r in reaching if r is not None and r[1]]
    no_reset_reach = [r[0] for _m, r in reaching if r is not None and not r[1]]
    spans = [m.rate["span_seconds"] for m in known]
    # What the total is made of, one entry per measured account and nothing
    # that names it: its share left, whether it is at the limit (decided on
    # the unrounded share, as `at_limit` below is — a share rounded to 0.0
    # is not an account at the limit) and whether a restriction touches it.
    # Sorted fullest first, so the list says the same thing in any order the
    # accounts were read in. The widget draws it; the sum is `windows` above.
    shares = sorted(
        ({"left": round(m.remaining, 4), "at_limit": m.remaining <= RATIO_EPS,
          "restricted": bool(m.flags)} for m in measured),
        key=lambda item: (-item["left"], item["at_limit"], item["restricted"]),
    )
    small = sum(1 for m, r in reaching if r is not None and r[1]
                and m.rate.get("change", 1.0) <= VALUE_TOLERANCE + RATIO_EPS)
    recent = {
        "state": pace_state,
        "windows_per_hour": _round(sum(m.rate["windows_per_hour"] for m in known), 3) if known else None,
        "accounts_known": len(known),
        "of": len(measured),
        "zero_growth": sum(1 for m in known if m.rate.get("zero_growth")),
        "span_min_seconds": min(spans) if spans else None,
        "span_max_seconds": max(spans) if spans else None,
        # Kept for compatibility: the pace one percentage point over each
        # account's span would give. A reference scale, not an error bound,
        # and not evidence of how a vendor rounds; nothing shows it as "±".
        "resolution_windows_per_hour": _round(
            sum(m.rate.get("resolution_windows_per_hour", 0.0) for m in known), 3) if known else None,
        "not_known": dict(sorted(reasons.items())),
        "exhaust_before_reset": len(exhaust_before),
        "earliest_exhaustion_at": iso(min(exhaust_before)) if exhaust_before else None,
        # Of those, how many rest on an observed change of one percentage
        # point or less: stated, not hidden, and not a verdict on direction.
        "exhaust_before_reset_small_change": small,
        # Accounts with no reported reset that would reach the limit at that
        # pace: said apart, since "before its reset" cannot be said of them.
        "reach_limit_no_reported_reset": len(no_reset_reach),
        "earliest_no_reset_reach_at": iso(min(no_reset_reach)) if no_reset_reach else None,
    }

    bars = _bars(calc, now, named)
    carried = [m.last_known for m in calc.slots
               if m.status != "measured" and m.last_known is not None and m.last_known.carried]
    lk_windows = sum(1.0 - lk.ratio for lk in carried)
    origins = Counter(lk.origin for lk in carried)
    unknown_bars = [b for b in bars if b["state"] == "unknown"]
    last_known = {
        "accounts": len(carried),
        "windows": _round(lk_windows),
        "oldest_observed_at": iso(min(lk.observed_at for lk in carried)) if carried else None,
        "newest_observed_at": iso(max(lk.observed_at for lk in carried)) if carried else None,
        "origins": dict(sorted(origins.items())),
    }
    return {
        "key": calc.key,
        "harness": calc.harness,
        "family": calc.family,
        "label": calc.label,
        "meaning": calc.meaning,
        "window_seconds": calc.window,
        "duration": duration_words(calc.window),
        "models": list(calc.models),
        "models_omitted": calc.models_omitted,
        # Additive since 0.7.0: whether the limit is tied to models, and
        # whether their names are known. A scoped limit restored from the
        # history keeps its scope but not its names (``names_unknown``):
        # still scoped, never read as the family's shared limit.
        "model_scope": _model_scope(calc),
        "measured": {
            "accounts": len(measured),
            "windows": _round(windows),
            "average_remaining_pct": _round(windows / len(measured) * 100.0, 1) if measured else None,
            "at_limit": sum(1 for m in measured if m.remaining <= RATIO_EPS),
        },
        "shares": shares,
        # Set by build_summary on the one group per family with the smallest
        # share left on average; never on a group with no measured account.
        "tightest": False,
        "coverage": {
            "measured": counts.get("measured", 0),
            "stale_only": counts.get("stale_only", 0),
            "invalid": counts.get("invalid", 0),
            "conflicting": counts.get("conflicting", 0),
            "reset_passed": counts.get("reset_passed", 0),
            "other_family_accounts": calc.other_family_accounts,
        },
        "unrestricted_windows": _round(unrestricted),
        "restrictions": dict(sorted(restrictions.items())),
        "plans": {"mixed": len(plans) > 1, "breakdown": breakdown},
        "observed": {
            "newest_at": iso(max(observed)) if observed else None,
            "oldest_at": iso(min(observed)) if observed else None,
            "stale_newest_at": iso(max(stale_seen)) if stale_seen else None,
        },
        "next_reset": next_reset,
        # Additive since 0.8.0 (widget only; the tool's next_reset is
        # unchanged): the share those accounts use now, which a full refill at
        # that reset gives back if nothing more is used before it.
        "next_reset_returns": next_returns,
        "reset_unknown": sum(1 for m in measured if m.reading and m.reading.resets_at is None),
        "pace_to_reset": pace_to_reset,
        "recent_pace": recent,
        "possible_duplicates": calc.possible_duplicates,
        # Additive since 0.7.0. ``measured`` above stays current readings
        # only; these say what the rest of the accounts are.
        "bars": bars,
        "slots": len(bars),
        "last_known": last_known,
        "unknown": {
            "accounts": len(unknown_bars),
            "reasons": dict(sorted(Counter(b["why"] for b in unknown_bars).items())),
        },
        # Roster accounts of the family with no reading of this limit in this
        # answer and none in the history: the limit may not apply to them.
        "applicability_unknown": max(0, calc.other_family_accounts - len(calc.extra)),
        # What the widget may show beside the current figure: current plus
        # carried last-known windows. Not "available now": it includes
        # readings that are not current, and says so.
        "with_last_known": {
            "windows": _round(windows + lk_windows),
            "accounts": len(measured) + len(carried),
        },
    }


def _unbroken_watch(view: HistoryView) -> Optional[Dict[str, Any]]:
    """When the collector's current unbroken watch began, as far as a bounded
    look-back can tell: ``exact`` when a break (or the first sweep kept) lies
    inside it, otherwise the watch began at or before ``since``. None when
    the collector is not watching now."""
    if view.watched_since is None:
        return None
    return {"since": iso(view.watched_since), "exact": bool(view.watched_since_exact),
            "lookback_seconds": round(WATCH_LOOKBACK_SEC)}


def _history_output(view: Optional[HistoryView]) -> Dict[str, Any]:
    if view is None:
        return {"state": "unavailable", "error": "no history reader"}
    return {
        "state": view.state,
        # The first sweep still kept: where the record begins, not where the
        # current unbroken watch began (an outage may lie in between).
        "collecting_since": iso(view.collecting_since),
        "unbroken_watch": _unbroken_watch(view),
        "oldest_at": iso(view.oldest_at),
        "last_sweep_at": iso(view.last_sweep_at),
        "last_sweep_ok": view.last_sweep_ok,
        "last_sweep_reason": view.last_sweep_reason,
        "last_failed_at": iso(view.last_failed_at),
        "truncated": view.truncated,
        "capped_before": iso(view.capped_before),
        "error": view.error,
    }


@dataclass
class SummaryState:
    """What :func:`build_summary` computed, kept so the chart reuses it."""

    norm: Normalized
    groups: List[GroupCalc]
    view: Optional[HistoryView]
    now: float


def rate_since(now: float) -> float:
    """The oldest last sighting a pace calculation may look at."""
    return now - RATE_WINDOW_SEC - 2 * SIGHTING_GAP_SEC


def prepare(payload: Any, now: float,
            cached: Optional[Dict[str, float]] = None) -> Tuple[Normalized, List[GroupCalc]]:
    """Everything that needs no history: readings, groups and members."""
    norm = normalize(payload, now, cached)
    return norm, resolve_groups(norm, now)


def history_requests(groups: Sequence[GroupCalc], salt: str, now: float,
                     chart: Optional[GroupCalc] = None,
                     horizon: str = "24h",
                     norm: Optional[Normalized] = None) -> Dict[Tuple[str, str], float]:
    """(pseudonymous subject, limit) -> oldest last sighting worth reading.

    Every measured account needs the last hour for its pace; every account
    the charted limit may apply to — whatever its reading now, and with
    ``norm`` each roster account that reports none of it — needs the whole
    horizon behind ``now``, so the record drawn does not change with what
    is fresh at this moment.
    """
    if not salt:
        return {}
    wanted: Dict[Tuple[str, str], float] = {}
    for calc in groups:
        for member in calc.measured:
            wanted[(pseudo_id(salt, member.harness, member.subject_id), calc.key)] = rate_since(now)
    if chart is not None:
        since = now - HORIZONS.get(horizon, HORIZONS["24h"]) - 2 * SIGHTING_GAP_SEC
        for member in chart.members:
            wanted[(pseudo_id(salt, member.harness, member.subject_id), chart.key)] = since
        for harness, subject_id in (_roster_missing(norm, chart) if norm is not None else []):
            wanted[(pseudo_id(salt, harness, subject_id), chart.key)] = since
    return wanted


def past_members(calc: GroupCalc, view: Optional[HistoryView], start: float) -> List[Member]:
    """Whose recorded total the chart draws behind ``now``: every account
    the limit is known to apply to (``slots``) that has a record in the
    chart's range, whether its reading now is current, last known or
    missing. Who the line sums depends on the record, never on what is fresh
    at this moment, so a reading going stale cannot change the past; where
    one of them has no value vouched for, the line has a gap instead of a
    smaller total. An account with no record in the range at all (new, or
    stale throughout) is left out and counted apart (``past_accounts`` of
    ``slots``), rather than blanking the whole range."""
    return [m for m in calc.slots
            if m.pseudo and any(run.last_seen >= start for run in _history_runs(view, m, calc.key))]


def _roster_missing(norm: Normalized, calc: GroupCalc) -> List[Tuple[str, str]]:
    """Accounts of the roster (current or kept) in this limit's family that
    report no reading of it at all in this answer."""
    present = {m.subject_id for m in calc.members}
    return sorted((h, sid) for (h, sid) in norm.accounts
                  if h == calc.harness and sid not in present)


def latest_requests(norm: Normalized, groups: Sequence[GroupCalc],
                    salt: str) -> List[Tuple[str, str]]:
    """(pseudonymous subject, limit) pairs whose newest kept runs may give a
    last-known value: every account of a limit whose readings in this answer
    are all stale, and every roster account that reports none at all. An
    unreadable or conflicting fresh reading is not replaced from the history.
    History is only ever asked about the exact recorded subject and series."""
    if not salt:
        return []
    wanted: List[Tuple[str, str]] = []
    for calc in groups:
        for member in calc.members:
            if member.status == "stale_only":
                wanted.append((pseudo_id(salt, member.harness, member.subject_id), calc.key))
        for harness, subject_id in _roster_missing(norm, calc):
            wanted.append((pseudo_id(salt, harness, subject_id), calc.key))
    return wanted


def roster_requests(norm: Normalized, salt: str) -> List[str]:
    """The pseudonymous subjects of the roster (current or kept) whose every
    recorded limit is wanted, so the history can say which limits those
    accounts had that no reading of this answer names: the quota facet
    unanswered, or answered without them (a quota answer that omits a limit
    is not proof its last reading was false). Removed accounts are not in the
    roster and are never asked about; no roster, nothing is asked."""
    if not salt:
        return []
    return [pseudo_id(salt, harness, subject_id) for (harness, subject_id) in sorted(norm.accounts)]


def _history_group(norm: Normalized, key: str, harness: str) -> Optional[GroupCalc]:
    """A limit known only from the history of a roster account (no reading
    of this answer names it): its identity is the recorded series key alone.
    The model names of a scoped limit are not recorded, only its scope id —
    the limit stays scoped, its names unknown (``model_scope``); the label is
    the limit's recorded meaning."""
    parts = key.split("|")
    if len(parts) < 4 or parts[0] != harness:
        return None
    meaning, window_text, scope = "|".join(parts[1:-2]), parts[-2], parts[-1]
    try:
        window = int(window_text) or None
    except ValueError:
        window = None
    return GroupCalc(key=key, harness=harness, family=norm.families.get(harness, harness),
                     meaning=meaning, label=meaning, window=window, models=(), members=[],
                     other_family_accounts=0, possible_duplicates=0, scope=scope)


def finish(norm: Normalized, groups: List[GroupCalc], view: Optional[HistoryView],
           now: float) -> SummaryState:
    salt = view.salt if view is not None else ""
    latest = view.latest if view is not None else {}
    restored: Set[str] = set()
    if salt and view is not None:
        # A limit no reading of this answer names: the history of the exact
        # roster subjects says which limits they had. A group is kept only
        # where some roster account has a usable last-known value of it
        # (below).
        known = {calc.key for calc in groups}
        roster = {pseudo_id(salt, h, sid): h for (h, sid) in norm.accounts}
        for subject, series in sorted(view.roster_series):
            if subject not in roster or series in known:
                continue
            calc = _history_group(norm, series, roster[subject])
            if calc is not None:
                groups.append(calc)
                known.add(series)
                restored.add(series)
    for calc in groups:
        for member in calc.members:
            member.pseudo = pseudo_id(salt, member.harness, member.subject_id) if salt else ""
            if member.status == "measured":
                member.rate = member_rate(view, member, now)
                continue
            if member.last_known is not None and "quota" in norm.cached:
                # Every quota reading of this answer was kept from an earlier one.
                member.last_known = replace(member.last_known, origin="cached")
            if member.status == "stale_only" and member.pseudo:
                runs = latest.get((member.pseudo, calc.key))
                found = last_known_from_runs(runs, calc.window, now) if runs else None
                # The history stands in only for a newer reading than this
                # answer's own evidence: its last known, or the stale
                # sources it refused because they disagree (resolve_member).
                # An older run never revives what was refused.
                newest = (member.last_known.observed_at if member.last_known is not None
                          else member.observed_at if member.reason == "sources_disagree" else None)
                if found is not None and (newest is None or found.observed_at > newest):
                    member.last_known = found
        # A roster account with no reading of this limit in this answer: the
        # history says whether it had one. None found keeps it out of the
        # limit — its applicability is unknown, never a zero.
        calc.extra = []
        if salt:
            for harness, subject_id in _roster_missing(norm, calc):
                pseudo = pseudo_id(salt, harness, subject_id)
                runs = latest.get((pseudo, calc.key))
                found = last_known_from_runs(runs, calc.window, now) if runs else None
                if found is None:
                    continue
                extra = Member(harness=harness, subject_id=subject_id, status="history_only",
                               reason="no_reading_in_answer", observed_at=found.observed_at,
                               pseudo=pseudo, last_known=found)
                _decorate(norm, extra, calc.key, calc.scope, norm.spent)
                calc.extra.append(extra)
        if calc.key in restored:
            # No account reads this limit in this answer: the whole family is
            # "other", as resolve_groups counts it; the accounts the history
            # restores are taken off once, in _group_output.
            calc.other_family_accounts = sum(1 for (h, _s) in norm.accounts if h == calc.harness)
    if restored:
        groups[:] = [calc for calc in groups if calc.key not in restored or calc.extra]
        groups.sort(key=lambda calc: _order_key(norm, calc))
        del groups[MAX_GROUPS:]
    return SummaryState(norm=norm, groups=groups, view=view, now=now)


def compute(payload: Any, view: Optional[HistoryView], now: float) -> SummaryState:
    norm, groups = prepare(payload, now)
    return finish(norm, groups, view, now)


def pick_chart_group(groups: Sequence[GroupCalc], key: str = "",
                     harness: str = "") -> Optional[GroupCalc]:
    calc = next((g for g in groups if g.key == key), None) if key else None
    return calc or default_group(groups, harness)


def roster_state(norm: Normalized) -> str:
    """Where the list of accounts comes from: this answer (``current``), the
    last answer that read it (``cached``), or nowhere (``unknown``)."""
    if norm.accounts_cached:
        return "cached"
    return "current" if norm.accounts_known else "unknown"


def build_summary(payload: Any, view: Optional[HistoryView], now: float,
                  *, status_read_at: Optional[float] = None,
                  state: Optional[SummaryState] = None,
                  reads: Optional[Dict[str, str]] = None,
                  name_accounts: bool = False) -> Dict[str, Any]:
    """The whole reserve overview; the widget and the tool both read this.

    ``reads`` are the facet states of the answer as the host gave it, when
    some facets were answered from an earlier read (``cached``)."""
    state = state or compute(payload, view, now)
    norm = state.norm
    groups = [_group_output(calc, now, name_accounts) for calc in state.groups]
    tight = set(tightest_keys(state.groups).values())
    for group in groups:
        group["tightest"] = group["key"] in tight
    return {
        "schema": SUMMARY_SCHEMA,
        "generated_at": iso(now),
        "status_read_at": iso(status_read_at),
        "reads": dict(reads) if reads is not None else norm.reads,
        # Facets this answer did not read, shown from the last answer that
        # did: facet -> when that answer was read.
        "cached": {facet: iso(at) for facet, at in sorted(norm.cached.items())},
        "roster": roster_state(norm),
        "unified_accounts": norm.unified,
        "history": _history_output(view),
        "unattributed_readings": dict(sorted(norm.unattributed.items())),
        "superseded_alias_readings": dict(sorted(norm.superseded.items())),
        "malformed_entries": norm.malformed,
        "groups": groups,
        "notes": list(GLOBAL_NOTES),
    }


def tightest_keys(groups: Sequence[GroupCalc]) -> Dict[str, str]:
    """family -> key of its tightest limit: the smallest share left on average
    among the limits with a measured account (more accounts at the limit, then
    the summary's own order, break a tie). It names a limit, not a verdict:
    limits of different length or scope are still never added together, and
    a model-scoped limit binds only its own models."""
    best: Dict[str, Tuple[float, int, int]] = {}
    out: Dict[str, str] = {}
    for index, calc in enumerate(groups):
        measured = calc.measured
        if not measured:
            continue
        average = sum(m.remaining for m in measured) / len(measured)
        at_limit = sum(1 for m in measured if m.remaining <= RATIO_EPS)
        rank = (round(average, 9), -at_limit, index)
        if calc.harness not in best or rank < best[calc.harness]:
            best[calc.harness] = rank
            out[calc.harness] = calc.key
    return out


def default_group(groups: Sequence[GroupCalc], harness: str = "") -> Optional[GroupCalc]:
    """The group a chart opens on: the family's tightest limit (the one the
    overview marks), else the first group of the family in the summary's own
    order. A short limit that happens to be nearly full is not what a chart
    of the family's reserve should open on."""
    pool = [g for g in groups if not harness or g.harness == harness]
    first = next((calc for calc in pool if calc.measured), pool[0] if pool else None)
    if first is None:
        return None
    tight = tightest_keys(pool).get(first.harness)
    return next((calc for calc in pool if calc.key == tight), first)


# ---------------------------------------------------------------------------
# Chart


Span = Tuple[Run, float]


def _source_spans(series: List[Run], now: float, view: Optional[HistoryView]) -> List[Span]:
    """Every run of one source, in order, with where the source is vouched
    for from its first sighting on: to the next run's first sighting (when
    the watch did not break between them) or to its own last sighting, and
    never past its reported reset. The last run reaches ``now`` only while
    the watch is still unbroken: no sweep since its last sighting, and that
    sighting recent. A run seen at one sweep and then not again spans no
    time at all — it is still a sighting (see :func:`_active`)."""
    series = sorted(series, key=lambda run: (run.first_seen, run.first_obs))
    spans: List[Span] = []
    for index, run in enumerate(series):
        end = run.last_seen
        if index + 1 < len(series):
            nxt = series[index + 1]
            if not watched_gap(run, nxt, view):
                end = max(end, nxt.first_seen)
        elif now - run.last_seen <= SIGHTING_GAP_SEC and not unseen_since(view, run.last_seen):
            end = now
        if run.resets_at is not None:
            end = min(end, run.resets_at)
        spans.append((run, end))
    return spans


Cover = List[Tuple[List[float], List[Span]]]


def _member_cover(runs: List[Run], now: float, view: Optional[HistoryView]) -> Cover:
    by_source: Dict[str, List[Run]] = {}
    for run in runs:
        by_source.setdefault(run.source, []).append(run)
    out = []
    for series in by_source.values():
        spans = _source_spans(series, now, view)
        out.append(([run.first_seen for run, _end in spans], spans))
    return out


def _active(cover: Cover, at: float, sighted: bool) -> List[Run]:
    """The runs that speak for one account at ``at``: those vouched for
    over a stretch that holds it (``at`` inside ``[first sighting, end)``)
    and — with ``sighted`` — also a run seen at ``at`` itself whose stretch
    ends there: the last sighting before a gap, or a reading seen at one
    sweep only. Such a sighting is evidence of that moment and of no
    moment after it."""
    active = []
    for starts, spans in cover:
        index = bisect.bisect_right(starts, at) - 1
        if index < 0:
            continue
        run, end = spans[index]
        if at < end or (sighted and at <= run.last_seen
                        and (run.resets_at is None or at < run.resets_at)):
            active.append(run)
    return active


def _member_ratio(cover: Cover, at: float, sighted: bool = False) -> Optional[float]:
    """Resolve the same source evidence policy as the current reserve.

    Compressed plateaus retain endpoint observation times, not every time
    in between. Where that could change which disagreeing source wins,
    leave coverage unknown instead of inventing an intermediate timestamp.
    """
    active = _active(cover, at, sighted)
    if (len({run.ratio for run in active}) > 1
            and any(run.first_obs != run.last_obs and at < run.last_seen for run in active)):
        return None
    chosen, _reason = _resolve_sources([
        (run.ratio, run.resets_at,
         run.first_obs if at < run.last_seen else run.last_obs, run.source, run)
        for run in active])
    return None if chosen is None else chosen.ratio


def _observed_past(members: List[Member], state: "SummaryState", key: str,
                   start: float) -> Tuple[List[List[Any]], List[List[Any]], Optional[float]]:
    """The observed total behind ``now``: step vertices and single points.
    ``[t, v]`` in the line holds ``v`` from ``t`` to the next vertex,
    ``[t, None]`` starts a gap. A point ``[t, v]`` is the total at one sweep
    that the line cannot show, because some account's reading was seen then
    and is not vouched for a moment longer; ``[t, None]`` is such a sweep
    whose sources disagree. Returns the vertices, the points and — only if
    the ceiling cut the oldest part away — the moment both are drawn from.

    Lossless: every moment at which any account's value can change is a
    vertex — each edge of what a source vouches for (a first sighting, a
    last one, a break in the watch, a sweep that did not see it, a reported
    reset) and each run's last sighting (where the same-moment source policy
    may choose differently). Between two such moments nothing changes, and
    only a vertex equal to the one before it is dropped. A value exists only
    while every account of the group is vouched for. Every sighting whose
    stretch ends where it was seen is resolved by the same source policy at
    that moment, and becomes a point unless the line already shows its
    total on one side of that moment; it is never stretched over time. A
    sweep whose total is unsettled only because a sighted account's sources
    disagree is a ``[t, None]`` point even where the line has no value
    there (the last sighting before a gap): seen, not unrecorded.
    """
    now = state.now
    current = round(sum(m.remaining for m in members), 4)
    if not members:
        return [[round(start), None], [round(now), None]], [], None
    covers = [_member_cover(_history_runs(state.view, m, key), now, state.view) for m in members]
    moments: Dict[float, Set[int]] = {}
    sightings: Dict[float, Set[int]] = {}
    for index, cover in enumerate(covers):
        for _starts, spans in cover:
            for run, end in spans:
                for moment in (run.first_seen, end, run.last_seen):
                    if start < moment < now:
                        moments.setdefault(moment, set()).add(index)
                # A sighting at the horizon's first instant is inside it: the
                # line starts from what is vouched for there, and the sighting
                # is still a point of its own.
                if end <= run.last_seen and start <= run.last_seen < now:
                    sightings.setdefault(run.last_seen, set()).add(index)
    ratios = [_member_ratio(cover, start) for cover in covers]

    def total(values: Sequence[Optional[float]]) -> Optional[float]:
        if any(ratio is None for ratio in values):
            return None
        return round(sum(1.0 - ratio for ratio in values), 4)

    past: List[List[Any]] = [[round(start), total(ratios)]]
    points: List[List[Any]] = []
    for moment in sorted(set(moments) | set(sightings)):
        before = past[-1][1]
        for index in moments.get(moment, ()):
            ratios[index] = _member_ratio(covers[index], moment)
        value = total(ratios)
        if value != before:
            past.append([round(moment), value])
        if moment in sightings:
            sighted = [_member_ratio(covers[index], moment, sighted=True)
                       if index in sightings[moment] else ratio
                       for index, ratio in enumerate(ratios)]
            seen = total(sighted)
            if seen != before and seen != value:
                points.append([round(moment), seen])
            elif seen is None and all(
                    ratio is not None
                    or (index in sightings[moment] and _active(covers[index], moment, True))
                    for index, ratio in enumerate(sighted)):
                # Every account is spoken for at this sweep, one by sources
                # that disagree: a sweep without a total, not one without a
                # record — so a point, even where the line has no value.
                points.append([round(moment), None])
    clipped = None
    if len(past) > MAX_PAST_VERTICES:
        past = past[-MAX_PAST_VERTICES:]
        clipped = past[0][0]
    if len(points) > MAX_PAST_VERTICES:
        edge = points[-MAX_PAST_VERTICES][0]
        if clipped is None or edge > clipped:
            # The line is cut at the same moment, holding what it held there.
            held = [vertex for vertex in past if vertex[0] <= edge]
            past = [[edge, held[-1][1]]] + [vertex for vertex in past if vertex[0] > edge]
            clipped = edge
    if clipped is not None:
        points = [point for point in points if point[0] >= clipped]
    past.append([round(now), current])
    return past, points, clipped


def _hold_value(member: Member, at: float, right: bool) -> float:
    reset = member.reading.resets_at
    if reset is not None and (at > reset or (right and at == reset)):
        return 1.0
    return member.remaining


def _last_known_history(calc: GroupCalc, state: SummaryState, start: float) -> Dict[str, Any]:
    """Display history only; never an input to measured pace or headlines.

    Each subject enters at its first recorded sighting. A failed/stale/missing
    reading carries its last resolved value, even beyond a reported reset,
    with dated provenance. No full refill is inferred. Source disagreement
    keeps the last resolved fact dated, or a gap if none has ever resolved.

    The database has no historical rosters. A subject absent from the known
    roster (current or kept) needs a sighting inside this window to enter it;
    an older seed alone cannot establish its membership here. In-window
    history remains, and a current roster confirms removal only at the read
    boundary, whose metadata says the actual removal time is unknown. An
    unanswered or cached roster never establishes a removal time.
    """
    now, view = state.now, state.view
    roster = {pseudo_id(view.salt, h, sid) for h, sid in state.norm.accounts} if view and view.salt else set()
    roster_known = roster_state(state.norm) != "unknown" and bool(view and view.salt)
    runs = {subject: list(values) for (subject, key), values in (view.runs.items() if view else [])
            if key == calc.key and values and (not roster_known or subject in roster
                                               or any(run.last_seen >= start for run in values))}
    slots = {m.pseudo or account_key(m.harness, m.subject_id): m for m in calc.slots}
    for subject in slots:
        runs.setdefault(subject, [])
    covers = {subject: _member_cover(values, now, view) for subject, values in runs.items()}
    changes: Dict[float, Set[str]] = {start: set(runs), now: set(runs)}
    for subject, cover in covers.items():
        for _starts, spans in cover:
            for run, end in spans:
                for at in (run.first_seen, run.last_seen, end, run.resets_at):
                    if at is not None and at <= now:
                        changes.setdefault(at, set()).add(subject)
    can_remove = roster_state(state.norm) == "current" and bool(view and view.salt)
    facts: Dict[str, Dict[str, Any]] = {}
    members: Set[str] = set()
    measured: Set[str] = set()
    unresolved: Dict[str, str] = {}
    line: List[List[Any]] = []
    details: List[Dict[str, Any]] = []
    previous_members: Set[str] = set()
    max_accounts = 0

    for at in sorted(changes):
        for subject in changes[at]:
            cover = covers[subject]
            available = []
            for starts, spans in cover:
                index = bisect.bisect_right(starts, at) - 1
                if index >= 0:
                    available.append(spans[index][0])
            if available:
                members.add(subject)
            active = _active(cover, at, True)
            candidates = active or available
            # Ended cycles cannot veto a newly observed current cycle.
            running = [r for r in candidates if r.resets_at is None or r.resets_at > at]
            candidates = running or candidates
            chosen, reason = _resolve_sources([
                (r.ratio, r.resets_at, r.first_obs if at < r.last_seen else r.last_obs,
                 r.source, r) for r in candidates])
            if active and _member_ratio(cover, at, True) is None:
                chosen, reason = None, "sources_disagree"
            if chosen is not None:
                observed = chosen.first_obs if at < chosen.last_seen else chosen.last_obs
                old = facts.get(subject)
                if old is None or observed >= old["observed"]:
                    facts[subject] = {"ratio": chosen.ratio, "observed": observed,
                                      "reset": chosen.resets_at, "source": chosen.source,
                                      "origin": "history"}
            fact = facts.get(subject)
            if (chosen is not None and fact is not None and fact["source"] == chosen.source
                    and fact["ratio"] == chosen.ratio
                    and fact["observed"] == (chosen.first_obs if at < chosen.last_seen else chosen.last_obs)
                    and _member_ratio(cover, at) == fact["ratio"]):
                measured.add(subject)
                unresolved.pop(subject, None)
            else:
                measured.discard(subject)
                unresolved[subject] = reason or "not_observed"
            if at == now and subject in slots:
                member = slots[subject]
                if member.status == "measured" and member.reading is not None:
                    reading = member.reading
                    facts[subject] = {"ratio": reading.ratio, "observed": reading.observed_at,
                                      "reset": reading.resets_at, "source": reading.source,
                                      "origin": "payload"}
                    members.add(subject)
                    measured.add(subject)
                    unresolved.pop(subject, None)
                else:
                    measured.discard(subject)
                    lk = member.last_known
                    if lk is not None and (subject not in facts or lk.observed_at >= facts[subject]["observed"]):
                        facts[subject] = {"ratio": lk.ratio, "observed": lk.observed_at,
                                          "reset": lk.resets_at, "source": lk.source, "origin": lk.origin}
                        members.add(subject)
                    unresolved[subject] = member.reason or member.status
        removed = set()
        if at == now and can_remove:
            removed = members - roster
            members -= removed
            measured -= removed
        if at < start:
            continue
        # At the left edge only the facts learned by then are used. Later
        # members cannot blank those hours or acquire invented earlier values.
        added = members - previous_members if line else set()
        lost = previous_members - members if line else set()
        changed = bool(added or lost)
        selected = [facts[s] for s in sorted(members) if s in facts]
        carried = members - measured
        carried_facts = [facts[s] for s in sorted(carried) if s in facts]
        unknown = len(members) - len(selected)
        value = round(sum(1 - fact["ratio"] for fact in selected), 4) if members and not unknown else None
        oldest = min((fact["observed"] for fact in carried_facts), default=None)
        detail = {
            "at": _instant(at), "value": value, "accounts": len(members),
            "measured": len(members & measured), "carried": len(carried_facts), "unknown": unknown,
            "oldest_observed_at": iso(oldest),
            "age_seconds": max(0, round(at - oldest)) if oldest is not None else None,
            "origins": dict(sorted(Counter(f["origin"] for f in carried_facts).items())),
            "sources": sorted({f["source"] for f in carried_facts}),
            "reset_passed": sum(1 for f in carried_facts if f["reset"] is not None and f["reset"] <= at),
            "reasons": dict(sorted(Counter(unresolved.get(s, "not_observed") for s in carried).items())),
            "change": ("membership_changed" if lost else "first_recorded") if changed else None,
            "added": len(added), "removed": len(lost),
            "removal_time_unknown": bool(removed),
        }
        if changed and line and line[-1][1] is not None:
            # A break, never a vertical consumption/refill step. Same-time
            # vertices keep the old segment all the way to the boundary.
            line.append([_instant(at), None])
            details.append(dict(detail, value=None, change=None))
        comparable = {k: v for k, v in detail.items() if k not in ("at", "age_seconds")}
        previous = {k: v for k, v in details[-1].items() if k not in ("at", "age_seconds")} if details else None
        if comparable != previous or at in (start, now):
            line.append([_instant(at), value])
            details.append(detail)
        max_accounts = max(max_accounts, len(members))
        previous_members = set(members)
    clipped = None
    if len(line) > MAX_PAST_VERTICES:
        line, details = line[-MAX_PAST_VERTICES:], details[-MAX_PAST_VERTICES:]
        clipped = line[0][0]
    table_rows = details[:-1][::max(1, len(details) // TABLE_PAST_ROWS)]
    table_rows += [d for d in details[:-1] if d["change"] and d not in table_rows][:TABLE_PAST_ROWS]
    table_rows.append(details[-1])
    table = [{"at": iso_exact(d["at"]), "observed": d["value"],
              "accounts": d["accounts"], "carried": d["carried"],
              "oldest_observed_at": d["oldest_observed_at"], "sources": d["sources"],
              "event": ("membership change; actual removal time unknown" if d["removed"] else
                        "first recorded member" if d["added"] else
                        "dated pre-reset value; refill not observed" if d["reset_passed"] else
                        "last known" if d["carried"] else "recorded")}
             for d in sorted(table_rows, key=lambda d: d["at"])]
    return {"line": line, "details": details, "max_accounts": max_accounts,
            "clipped_before": clipped, "table": table,
            "membership_note": "Accounts enter at their first recorded value, not an inferred creation time. "
            "Accounts absent from the known roster need a sighting inside this window; older seeds alone "
            "do not establish membership. "
            "Historical rosters were not recorded; a removal confirmed by the current roster is shown "
            "only at this read, with its actual time unknown. Missing quota readings never remove an account."}


def _pace_value(member: Member, now: float, at: float, right: bool) -> float:
    rate = member.rate["windows_per_hour"] / 3600.0
    reset = member.reading.resets_at
    if reset is not None and (at > reset or (right and at == reset)):
        return max(0.0, 1.0 - rate * (at - reset))
    return max(0.0, member.remaining - rate * (at - now))


def _vertices(members: List[Member], now: float, end: float, fn, breaks: Iterable[float]) -> List[List[float]]:
    times = sorted({t for t in breaks if now < t < end})
    out = [[now, sum(fn(m, now, True) for m in members)]]
    for t in times:
        left = sum(fn(m, t, False) for m in members)
        right = sum(fn(m, t, True) for m in members)
        out.append([t, left])
        if abs(right - left) > 1e-9:
            out.append([t, right])
    out.append([end, sum(fn(m, end, False) for m in members)])
    return [[round(t), round(v, 4)] for t, v in out]


# Where a scenario is read off in words: about an hour, a working day, half a
# day and the whole horizon; a day, three days and the week.
CHECKPOINTS: Dict[str, Tuple[float, ...]] = {
    "24h": (3600.0, 6 * 3600.0, 12 * 3600.0, 86400.0),
    "7d": (86400.0, 3 * 86400.0, 604800.0),
}


def reset_events(members: Sequence[Member], now: float) -> List[Tuple[float, List[Member]]]:
    """Each current account's next reported reset, in time order, grouped as
    the summary's ``next_reset`` groups them: a reset within
    RESET_TOLERANCE_SEC of the first one of a group belongs to that group.
    An account with no reported reset is in none — no refill is assumed."""
    fixed = sorted(((m.reading.resets_at, m) for m in members
                    if m.reading is not None and m.reading.resets_at is not None
                    and m.reading.resets_at > now), key=lambda pair: pair[0])
    events: List[Tuple[float, List[Member]]] = []
    for at, member in fixed:
        if events and at - events[-1][0] <= RESET_TOLERANCE_SEC:
            events[-1][1].append(member)
        else:
            events.append((at, [member]))
    return events


def horizon_events(members: Sequence[Member], now: float, end: float
                   ) -> Tuple[List[Tuple[float, List[Member]]], List[Tuple[float, List[Member]]]]:
    """The reset events up to ``end`` (inclusive) and those after it. The
    accounts are split at ``end`` before they are grouped (reset_events), so
    resets within RESET_TOLERANCE_SEC on either side of the horizon are never
    one event: what refills past it is never in a total inside it."""
    inside = [m for m in members if m.reading is not None
              and m.reading.resets_at is not None and m.reading.resets_at <= end]
    after = [m for m in members if m.reading is not None
             and m.reading.resets_at is not None and m.reading.resets_at > end]
    return reset_events(inside, now), reset_events(after, now)


def _instant(stamp: float) -> Union[int, float]:
    """A moment inside a scenario as published: a whole second as round()
    gives it, a fraction of a second kept — never moved to the nearest
    second, which could put a refill before its reported reset."""
    whole = round(stamp)
    return whole if whole == stamp else stamp


def _scenario_line(total, now: float, end: float, breaks: Iterable[float]) -> List[List[float]]:
    """A scenario's vertices from ``now`` to ``end``, both ends included: at
    every break a left and (where it differs) a right value, so a reset
    exactly at the horizon is drawn with its refill rather than left out.
    The ends are the whole seconds the chart publishes (``now`` rounded, the
    horizon); every break between them keeps its own time (_instant), so the
    line says what the checkpoints and the table read off the same functions
    at a whole second either side of a reset with a fraction. A break inside
    the half second ``now`` was rounded up by is drawn at ``now``."""
    times = sorted({t for t in breaks if now < t <= end})
    first = round(now)
    out = [[first, total(now, True)]]
    for t in times:
        left, right = total(t, False), total(t, True)
        at = max(_instant(t), first)
        out.append([at, left])
        if abs(right - left) > 1e-9:
            out.append([at, right])
    if not times or times[-1] < end:
        out.append([round(end), total(end, False)])
    return [[t, round(v, 4)] for t, v in out]


def _scenario_functions(members: List[Member], paced: List[Member], now: float):
    """(value, total) of one scenario: one account's share left at ``t``, and
    the sum over ``members`` — ``right`` asks for the value just after
    anything that happens exactly at ``t`` (a refill), else just before."""
    paced_ids = {id(m) for m in paced}

    def value(member: Member, t: float, right: bool) -> float:
        if id(member) in paced_ids:
            return _pace_value(member, now, t, right)
        return _hold_value(member, t, right)

    def total(t: float, right: bool) -> float:
        return sum(value(member, t, right) for member in members)

    return value, total


def _scenario(members: List[Member], paced: List[Member], now: float, end: float,
              horizon: str) -> Dict[str, Any]:
    """One conditional future of the accounts read now — the same accounts
    and the same figure as the row's headline, restricted ones included
    (measured quota is not a dispatch verdict) — drawn to the horizon.

    Every account keeps its share now and refills to one full window once,
    at its own next reported reset; an account with no reported reset is
    never refilled. The accounts in ``paced`` instead continue their own
    observed net change from now, staying at zero once they reach it, and
    after their refill continue it again from full. Nothing else is
    assumed: no additional reset, however short the window, and no pace for
    an account whose pace did not qualify. That is the stated condition, to
    the horizon; no accuracy is claimed for it. With ``paced`` empty this is
    "no new use". The schedule, the checkpoints and the moments an account
    runs out are read from the same functions the line is drawn from."""
    paced_ids = {id(m) for m in paced}
    value, total = _scenario_functions(members, paced, now)

    breaks: List[float] = []
    runs_out: List[Dict[str, Any]] = []
    for member in members:
        reset = member.reading.resets_at
        if reset is not None:
            breaks.append(reset)
        if id(member) not in paced_ids:
            continue
        rate = member.rate["windows_per_hour"] / 3600.0
        if rate <= 0:
            continue
        if member.remaining > RATIO_EPS:
            out_at = now + member.remaining / rate
            if reset is None or out_at < reset:
                breaks.append(out_at)
                if out_at <= end:
                    runs_out.append({"at": round(out_at), "reset_reported": reset is not None,
                                     "after_refill": False})
        if reset is not None:
            again = reset + 1.0 / rate
            breaks.append(again)
            if again <= end:
                runs_out.append({"at": round(again), "reset_reported": True, "after_refill": True})
    runs_out.sort(key=lambda row: row["at"])

    schedule: List[Dict[str, Any]] = []
    # Split at the horizon first (horizon_events): an event ends by ``end``,
    # and an account resetting just past it is only in ``later``.
    inside, later = horizon_events(members, now, end)
    for at, group in inside:
        # Resets reported within RESET_TOLERANCE_SEC are one event: it spans
        # from the first (``at``) to the last (``last``), and the total is the
        # one just after the last of them.
        last = max(member.reading.resets_at for member in group)
        schedule.append({
            # Their own times, as the line has them (_instant).
            "at": _instant(at), "last": _instant(last), "accounts": len(group),
            # What each refill gives back is what the account has used by
            # then in this scenario: never the "no new use" amount reused.
            "adds": round(sum(value(member, member.reading.resets_at, True)
                              - value(member, member.reading.resets_at, False) for member in group), 4),
            "total_after": round(total(last, True), 4),
        })
    checkpoints = []
    for offset in CHECKPOINTS.get(horizon, CHECKPOINTS["24h"]):
        # Read off at the whole second it is published at, as the table's
        # span rows are: the horizon's own checkpoint is the horizon's instant.
        # A reset later in that same second is not in it yet (_scenario_line).
        moment = round(now + offset)
        if moment <= end:
            checkpoints.append({"after_seconds": round(offset), "at": moment,
                                "value": round(total(moment, True), 4)})
    return {
        "line": _scenario_line(total, now, end, breaks),
        "accounts": len(members),
        "at_pace": len(paced),
        "held": len(members) - len(paced),
        "zero_growth": sum(1 for m in paced if (m.rate.get("windows_per_hour") or 0.0) <= RATIO_EPS),
        "no_reset": sum(1 for m in members if m.reading.resets_at is None),
        "schedule": schedule,
        # The first reported reset past the horizon, and how many more: when,
        # never how much — the scenario is not drawn that far.
        "later": ({"at": round(later[0][0]), "accounts": len(later[0][1]),
                   "more_events": len(later) - 1,
                   "more_accounts": sum(len(group) for _at, group in later[1:])} if later else None),
        "checkpoints": checkpoints,
        "runs_out": runs_out,
    }


def build_chart(state: SummaryState, key: str = "", harness: str = "",
                horizon: str = "24h") -> Optional[Dict[str, Any]]:
    """One group's chart: the recorded total behind ``now`` (``past``: every
    account of the limit with a record in the range, whatever is fresh now)
    and, ahead of it, the ``scenarios`` of the accounts read now — the row's
    figure — with their reset schedules (see _scenario). The two bases can
    differ; the line behind ``now`` never pretends to end at the figure.

    Kept for compatibility: ``no_new_use`` (the same line as the "no new use"
    scenario before the horizon's own instant, its times rounded to the
    second as they always were), the qualified cohort's own
    ``recent_pace`` up to its first reported reset with ``cohort_past``, and
    ``recent_pace_refill_scenario``.

    No later reset is assumed, however short the limit, and none is inferred
    from a window's length. That is a condition, said in ``assumptions``, not
    a forecast."""
    if horizon not in HORIZONS:
        horizon = "24h"
    calc = pick_chart_group(state.groups, key, harness)
    if calc is None:
        return None
    now = state.now
    span = HORIZONS[horizon]
    # The horizon is the whole second the chart publishes as ``end``: what
    # falls inside it (the reset events, both scenarios, their checkpoints,
    # the older lines and the table) is decided against that one instant,
    # never against a fraction of a second either side of it. ``now`` and
    # the figure at it stay as read.
    start, end = now - span, float(round(now + span))
    members = calc.measured
    current = sum(m.remaining for m in members)
    basis = past_members(calc, state.view, start)
    past, points, clipped = _observed_past(basis, state, calc.key, start)
    current_basis = [m for m in basis if m.status == "measured"]
    if len(current_basis) < len(basis):
        # Some of the accounts the record sums have no current reading: the
        # line ends where their record does, never at a value "now".
        past[-1] = [past[-1][0], None]
    elif not basis and members:
        # No record at all yet: the line is only the current figure, at now.
        past[-1] = [past[-1][0], round(current, 4)]
    past_basis = ("current" if members else "none") if not basis else (
        "current" if len(current_basis) == len(basis)
        else "last_known" if not current_basis else "recorded")
    history = _last_known_history(calc, state, start)

    # The reported resets inside the horizon, grouped one way for the marks,
    # the table and both scenarios' schedules (horizon_events: split at the
    # horizon, then grouped): first -> (the last reset of the event, how many
    # accounts).
    events = {at: (max(m.reading.resets_at for m in group), len(group))
              for at, group in horizon_events(members, now, end)[0]}
    resets: Dict[float, int] = {at: count for at, (_last, count) in events.items()}

    hold_breaks = [m.reading.resets_at for m in members if m.reading.resets_at is not None]
    hold = _vertices(members, now, end, lambda m, t, r: _hold_value(m, t, r), hold_breaks) if members else []

    # Recent pace: the accounts whose own pace qualified (the cohort), each
    # continuing its observed net change, summed over the cohort alone — an
    # account left out is not held at its value inside the line. The line
    # stops at the first reported reset among them: what a reading does after
    # its reset is not observed, so nothing is drawn as the estimate past it.
    # The refill a reported reset may bring is a separate scenario.
    pace = None
    pace_refill = None
    pace_note = ""
    cohort_past = None
    known = [m for m in members if m.rate.get("state") == "ok"]
    excluded: Counter = Counter(
        m.rate.get("state", "unavailable") + ":" + m.rate.get("reason", "")
        for m in members if m.rate.get("state") != "ok")
    resets_ahead = [m.reading.resets_at for m in known
                    if m.reading.resets_at is not None and now < m.reading.resets_at <= end]
    first_reset = min(resets_ahead) if resets_ahead else None
    until = first_reset if first_reset is not None else end
    if known:
        breaks: List[float] = []
        refill_breaks: List[float] = []
        for member in known:
            rate = member.rate["windows_per_hour"] / 3600.0
            reset = member.reading.resets_at
            if rate > 0:
                breaks.append(now + member.remaining / rate)
            if reset is not None:
                refill_breaks.append(reset)
                if rate > 0:
                    refill_breaks.append(reset + 1.0 / rate)
        pace = _vertices(known, now, until, lambda m, t, r: _pace_value(m, now, t, r), breaks)
        if first_reset is not None:
            pace_refill = _vertices(known, now, end, lambda m, t, r: _pace_value(m, now, t, r),
                                    breaks + refill_breaks)
        # The line the estimate continues is its own accounts' record, drawn
        # whenever they are not exactly the accounts the observed line sums
        # (the record also holds accounts with no current reading now).
        if {(m.harness, m.subject_id) for m in known} != {(m.harness, m.subject_id) for m in basis}:
            cohort_past, _cohort_points, _clip = _observed_past(known, state, calc.key, start)
        if len(known) < len(members):
            pace_note = (
                f"Recent pace known for {len(known)} of {len(members)} measured accounts; "
                f"the estimate line is the sum of those {len(known)} only."
            )
    elif members:
        pace_note = (
            f"Recent pace known for none of {len(members)} measured accounts; "
            "no estimate line is drawn."
        )

    # 0.8.0: the two futures the widget draws, both of the accounts read now
    # — the row's figure, from its own value at now — and both to the
    # horizon: no new use, and the qualified accounts at their recent pace
    # with the others held. With no current reading there is no future.
    scenarios = None
    if members:
        scenarios = {"no_new_use": _scenario(members, [], now, end, horizon),
                     "recent_pace": _scenario(members, known, now, end, horizon)}

    def at_time(line: Optional[List[List[float]]], moment: float) -> Optional[float]:
        if not line:
            return None
        value = None
        for (t0, v0), (t1, v1) in zip(line, line[1:]):
            if t0 <= moment <= t1:
                if t1 == t0:
                    value = v1
                else:
                    value = v0 + (v1 - v0) * (moment - t0) / (t1 - t0)
                if moment < t1:
                    break
        return None if value is None else round(value, 2)

    # The table is a sample of the observed line (the chart and its cursor
    # use every vertex): a stride of it, plus the beginnings of at most
    # TABLE_PAST_ROWS gaps the stride missed (the oldest; never every gap),
    # plus the newest single-sweep points — each marked as a sighting, since
    # a point holds for no stretch of time.
    table: List[Dict[str, Any]] = []
    stride = max(1, len(past) // TABLE_PAST_ROWS)
    rows = past[:-1][::stride]
    # A gap's beginning is said even where the stride does not land on it —
    # up to TABLE_PAST_ROWS of them, so the table stays a bounded sample.
    rows += [point for point in past[:-1] if point[1] is None and point not in rows][:TABLE_PAST_ROWS]
    for point in sorted(rows, key=lambda point: (point[0], point[1] is None)):
        table.append({"at": iso(point[0]), "observed": None if point[1] is None else round(point[1], 2)})
    for point in points[-TABLE_PAST_ROWS:]:
        table.append({"at": iso(point[0]), "observed": None if point[1] is None else round(point[1], 2),
                      "sighting": True,
                      "event": ("seen at this sweep only" if point[1] is not None
                                else "sources disagree at this sweep")})
    # The scenarios the widget draws (``scenario_no_new_use``,
    # ``scenario_recent_pace``) are read off their own functions just after
    # anything at that moment — after the last reset of an event, so a row
    # says the same as the schedule. ``no_new_use`` and ``recent_pace`` stay
    # the older lines, kept for compatibility.
    totals = ({"scenario_no_new_use": _scenario_functions(members, [], now)[1],
               "scenario_recent_pace": _scenario_functions(members, known, now)[1]} if members else {})

    def scenario_cells(moment: float) -> Dict[str, Optional[float]]:
        return {name: round(total(moment, True), 2) for name, total in totals.items()}

    table.append(dict({"at": iso(now), "observed": round(current, 2) if members else None,
                       "no_new_use": round(current, 2) if members else None,
                       "recent_pace": round(sum(m.remaining for m in known), 2) if pace else None,
                       "event": "now"}, **scenario_cells(now)))
    for moment in sorted(resets):
        count = resets[moment]
        last = events[moment][0]
        row = {
            # The reset's own time, its fraction of a second kept (iso_exact):
            # a row at the same whole second before it reads the total before it.
            "at": iso_exact(moment),
            "no_new_use": at_time(hold, moment + 1),
            "recent_pace": at_time(pace, moment + 1),
            "event": f"reported reset · {count} account{'s' if count != 1 else ''}",
        }
        if last != moment:
            row["until"] = iso_exact(last)
        row.update(scenario_cells(last))
        table.append(row)
    for fraction in (0.25, 0.5, 1.0):
        # Whole seconds, as the checkpoints are: the horizon's own row is the
        # horizon's instant, the last vertex.
        moment = round(now + span * fraction)
        table.append(dict({"at": iso(moment), "no_new_use": at_time(hold, moment),
                           "recent_pace": at_time(pace, moment)}, **scenario_cells(moment)))
    # In time order by the instant each row says, so a reset a fraction of a
    # second into a second follows the row at that whole second (rows of one
    # instant keep the order they were added in).
    table.sort(key=lambda row: parse_instant(row["at"]))

    return {
        "group_key": calc.key,
        "harness": calc.harness,
        "horizon": horizon,
        "start": round(start),
        "now": round(now),
        "end": round(end),
        # The scale is every account this limit is known to apply to (see
        # GroupCalc.slots), so it does not move when a reading goes stale.
        "y_max": max(len(calc.slots), len(members), history["max_accounts"]),
        "accounts": len(members),
        # The row's figure, unrounded: where both scenarios start at now.
        "current_windows": round(current, 4) if members else None,
        # 0.8.0: the futures the widget draws, of the accounts read now (see
        # _scenario). None with no current reading: nothing is projected
        # from last-known values.
        "scenarios": scenarios,
        # Display history. ``past`` below remains the legacy strict record;
        # neither carry nor changing membership enters the rate estimator.
        "history": history,
        "past": past,
        # How many recorded values the observed line has before ``now``,
        # and — only when the vertex ceiling cut the oldest part away — the
        # moment it is drawn from.
        "past_points": len(past) - 1,
        # Totals seen at one sweep that the line cannot hold over any time
        # (a reading seen once, the last one before a gap): ``[t, v]``, or
        # ``[t, None]`` where the sources seen then disagree. Drawn as
        # isolated marks, never joined to the line.
        "points": points,
        "past_clipped_before": clipped,
        # Whose total the past line is: every account of the limit with a
        # record in the range (``past_accounts`` of ``y_max``), the same set
        # whatever is fresh now — ``current`` when all of them have a current
        # reading, ``last_known`` when none does, ``recorded`` when some do
        # (the line then ends where their record does, never at a value now).
        # The estimate's own cohort is ``recent_pace_scope``/``cohort_past``.
        "past_basis": past_basis,
        "past_accounts": len(basis),
        "past_current_accounts": len(current_basis),
        "no_new_use": hold,
        "recent_pace": pace,
        "recent_pace_note": pace_note,
        # Who the estimate line sums and where it stops (see above);
        # ``cohort_past`` is the observed total of the same accounts, drawn
        # only when they are not exactly the accounts of ``past``.
        "recent_pace_scope": {
            "accounts": len(known),
            "of": len(members),
            "slots": max(len(calc.slots), len(members)),
            "until": round(until) if pace else None,
            "stops_at_reset": bool(pace) and first_reset is not None,
            "excluded": dict(sorted(excluded.items())),
        },
        "cohort_past": cohort_past,
        # The same accounts at the same pace, assuming each refills to a full
        # window at its next reported reset: a scenario, drawn only on request.
        "recent_pace_refill_scenario": pace_refill,
        "resets": [{"at": round(t), "accounts": n} for t, n in sorted(resets.items())],
        # The same events with when each ends (resets reported within
        # RESET_TOLERANCE_SEC of the first are one event).
        "reset_events": [{"at": round(t), "last": round(events[t][0]), "accounts": n}
                         for t, n in sorted(resets.items())],
        "table": table,
        # What the record and the two drawn ``scenarios`` assume — the notes
        # the widget shows. The older lines' own wording is apart, in
        # ``legacy_assumptions``: it describes fields that are not drawn.
        "assumptions": [
            "History: each account enters at its first recorded value. Temporary missing, stale or "
            "failed readings retain its dated last known value, dashed with age and source. "
            "Passing a reported reset retains the pre-reset value; it does not prove a refill. "
            "Membership changes break the line and are not consumption. The current figure and "
            "future use only fresh readings; carry never enters measured pace.",
            history["membership_note"],
            "No new use: each account keeps its current reading and refills to "
            "a full window only at its own next reported reset.",
            "Recent pace: every account read now, the ones whose pace "
            "qualified continuing it (refilled once at their next reported reset, "
            "then at the same pace), the others held at their share and refilled "
            "at their reset. A condition, not an estimate of use nobody observed.",
            "The scenarios assume no additional unreported resets: only each "
            "account's next reported reset is applied, so a limit shorter than "
            "the horizon is not refilled again in them, and an account with no "
            "reported reset is never refilled. Reset times are as reported now "
            "and may move. The horizon is how far the scenario is drawn, not how "
            "far it is reliable.",
        ],
        # The fields kept for compatibility, said by field: never drawn.
        "legacy_assumptions": {
            "recent_pace": (
                "Only the accounts whose own pace over the last hour qualified, "
                "each continuing its observed net change; one that runs out stays "
                "at zero. The line stops at the first reported reset among them. "
                "If the trend continues — not a promise, and an unchanged reading "
                "is not proof of zero use."),
            "recent_pace_refill_scenario": (
                "The same pace, with each account refilled to a full window at its "
                "next reported reset. A reported reset time is not evidence of a "
                "full refill."),
        },
    }


# ---------------------------------------------------------------------------
# Compact form for the model tool


def _compact_pace(pace: Dict[str, Any], detail: bool) -> Dict[str, Any]:
    """Unknowns and zero counts drop out; a known pace of zero stays, since
    "no growth observed" is an answer, not an absence."""
    out: Dict[str, Any] = {"state": pace.get("state"), "accounts_known": pace.get("accounts_known"),
                           "of": pace.get("of")}
    for key in ("windows_per_hour", "span_min_seconds", "span_max_seconds",
                "resolution_windows_per_hour", "earliest_exhaustion_at"):
        if pace.get(key) is not None:
            out[key] = pace[key]
    for key in ("zero_growth", "exhaust_before_reset", "exhaust_before_reset_small_change",
                "reach_limit_no_reported_reset", "earliest_no_reset_reach_at"):
        if pace.get(key):
            out[key] = pace[key]
    if detail and pace.get("not_known"):
        out["not_known"] = pace["not_known"]
    return out


def _headline(group: Dict[str, Any]) -> str:
    """One plain sentence per limit, from the same numbers as the fields:
    current windows of how many accounts, what is last known and how old,
    what is unknown, the next reported reset and how much of the pace is
    known. Words the model can quote; the fields stay the evidence."""
    m = group.get("measured") or {}
    slots = group.get("slots", m.get("accounts", 0))
    scope = ("; model-scoped, model names unavailable from history"
             if group.get("model_scope") == "names_unknown" else "")
    head = f"{group.get('family')} {group.get('label')} ({group.get('duration')}{scope}): "
    if m.get("accounts"):
        parts = [head + f"{m.get('windows')} account-windows left now across "
                        f"{m.get('accounts')} measured of {slots} accounts"]
    else:
        # Nothing measured is not "0 left": no current reading says nothing
        # about what is left now (remaining_windows keeps its own meaning).
        parts = [head + f"no current reading of any of {slots} account{'s' if slots != 1 else ''}"]
    lk = group.get("last_known") or {}
    if lk.get("accounts"):
        parts.append(f"plus {lk.get('windows')} last known from {lk.get('accounts')} "
                     f"account{'s' if lk.get('accounts') != 1 else ''} "
                     f"(oldest observed {lk.get('oldest_observed_at')}), not current")
    unknown = (group.get("unknown") or {}).get("accounts")
    if unknown:
        parts.append(f"{unknown} unknown, not counted")
    if m.get("at_limit"):
        parts.append(f"{m['at_limit']} at the limit")
    reset = (group.get("next_reset") or {}).get("at")
    if reset:
        parts.append(f"next reported reset {reset}")
    pace = group.get("recent_pace") or {}
    if pace.get("accounts_known"):
        parts.append(f"recent pace known for {pace['accounts_known']} of {pace.get('of')}")
        if pace.get("exhaust_before_reset"):
            parts.append(f"at that pace {pace['exhaust_before_reset']} would reach the limit before "
                         f"their reported reset, the first about {pace.get('earliest_exhaustion_at')}, "
                         "if the trend continued")
        if pace.get("reach_limit_no_reported_reset"):
            parts.append(f"{pace['reach_limit_no_reported_reset']} with no reported reset would reach it "
                         f"at that pace, the first about {pace.get('earliest_no_reset_reach_at')}")
    return "; ".join(parts) + "."


def compact(summary: Dict[str, Any], harness: str = "", detail: bool = False) -> Dict[str, Any]:
    """The tool's projection of :func:`build_summary`: the same numbers, fewer
    fields, zero counts dropped. ``detail`` adds plan breakdowns and the
    per-reason pace counts."""
    wanted = str(harness or "").strip().lower()
    groups = []
    for group in summary.get("groups", []):
        if wanted and str(group.get("harness", "")).lower() != wanted:
            continue
        coverage = {k: v for k, v in group["coverage"].items() if v}
        pace = group["recent_pace"]
        row: Dict[str, Any] = {
            "key": group["key"],
            "family": group["family"],
            "limit": group["label"],
            "duration": group["duration"],
            "remaining_windows": group["measured"]["windows"],
            "of_accounts": group["measured"]["accounts"],
            "average_remaining_pct": group["measured"]["average_remaining_pct"],
            "accounts_at_limit": group["measured"]["at_limit"],
            "unrestricted_windows": group["unrestricted_windows"],
            "coverage": coverage,
            "newest_observed_at": group["observed"]["newest_at"],
            "oldest_observed_at": group["observed"]["oldest_at"],
            "next_reset": group["next_reset"],
            "pace_to_reset": group["pace_to_reset"],
            "recent_pace": _compact_pace(pace, detail),
        }
        if group.get("tightest"):
            row["tightest_in_family"] = True
        if group["models"]:
            row["models"] = group["models"]
        if group.get("model_scope") == "names_unknown":
            # Restored from the history: tied to models, names not recorded.
            row["model_scope"] = "model-scoped; model names unavailable from history"
        if group.get("models_omitted"):
            row["models_omitted"] = group["models_omitted"]
        if group["restrictions"]:
            row["restrictions"] = group["restrictions"]
        if group["plans"]["mixed"]:
            row["mixed_plans"] = True
            if detail:
                row["plans"] = group["plans"]["breakdown"]
        if group["possible_duplicates"]:
            row["possible_duplicate_sign_ins"] = group["possible_duplicates"]
        if group["reset_unknown"]:
            row["reset_unknown"] = group["reset_unknown"]
        # Additive since 0.7.0: the accounts the current figure leaves out.
        # ``remaining_windows`` above stays current readings only.
        if group.get("slots") is not None:
            row["accounts_known_to_apply"] = group["slots"]
        last_known = group.get("last_known") or {}
        if last_known.get("accounts"):
            row["last_known"] = {
                "windows": last_known.get("windows"),
                "accounts": last_known.get("accounts"),
                "oldest_observed_at": last_known.get("oldest_observed_at"),
            }
            row["remaining_windows_with_last_known"] = (group.get("with_last_known") or {}).get("windows")
        unknown = group.get("unknown") or {}
        if unknown.get("accounts"):
            row["unknown_accounts"] = unknown["accounts"]
            if detail:
                row["unknown_reasons"] = unknown.get("reasons")
        if group.get("applicability_unknown"):
            row["applicability_unknown_accounts"] = group["applicability_unknown"]
        row["headline"] = _headline(group)
        groups.append(row)
    history = summary.get("history", {})
    extra: Dict[str, Any] = {}
    if summary.get("cached"):
        extra["cached_facets"] = summary["cached"]
    if summary.get("roster") and summary.get("roster") != "current":
        extra["roster"] = summary["roster"]
    return {
        "generated_at": summary.get("generated_at"),
        "status_read_at": summary.get("status_read_at"),
        "reads": summary.get("reads"),
        **extra,
        "history": {k: history.get(k) for k in ("state", "unbroken_watch", "oldest_at", "truncated", "error")
                    if history.get(k) not in (None, False, "")},
        "groups": groups,
        "unit": "account-windows: each account's remaining share of one limit counts 1; "
                "not tokens or hours; limits of different meaning/duration/scope are "
                "never added together.",
        "caveat": "Measured quota only. Not a dispatch guarantee and not a routing "
                  "instruction; it does not change model or account pins.",
    }
