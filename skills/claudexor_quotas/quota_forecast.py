"""48-hour pooled Typical forecast and unified pooled future simulation.

Pure calculation over ``quota_summary`` data structures (stdlib only, no I/O).
Keeps the trailing-hour watched ``recent_pace`` (used in the summary tool,
context snapshot and Details) completely separate from this forecast.

Estimator:
    Reads ONLY the exact selected limit series (never mixing window lengths,
    request kinds or model scopes) over the preceding 48 hours ``[now - 48h, now]``.
    For each account and source, extracts valid comparable intervals:
    - Unchanged plateaus with at least two distinct fresh observations
      (``n_obs >= 2``) and unbroken watch contribute zero burn over their
      clipped observation span; a single cached observation (``n_obs == 1``)
      contributes zero measured time, never a fake zero hour.
    - Consecutive runs of the same ``(account, source, plan, reset cycle)``
      with ``boundary(prev, nxt, view) == ""``, no failed collector sweeps in
      between, strictly advancing observation time inside ``[now - 48h, now]``,
      and non-negative delta contribute their positive ratio increase.
      Plan changes, resets beyond ``RESET_TOLERANCE_SEC``, ratio drops /
      restores, corrections, missing readings, collector gaps and failed
      sweeps are never counted as burn or as zero-burn time.
    - Multi-source accounts are deduplicated to one deterministic source per
      elementary interval and rejected wherever concurrent sources disagree on
      value or reset; cross-source differences are never treated as burn.

Pool coverage and minimum duration:
    An elementary interval counts toward the pool's valid measured time only
    when at least ``ceil(0.80 * N)`` of the ``N`` currently measured accounts
    in the pool AND at least ``ceil(0.80 * M)`` of the historically active
    cohort at that moment have valid comparable measurements. For 1-4 fully
    visible accounts ``ceil(0.80 * N) == N`` (100%, no min-5 account floor),
    while partial history (e.g. 1 of 22 current accounts) yields 0 valid time
    and stays unknown rather than inventing zero pace. At least 12 valid hours
    are required before emitting a rate: ``total_observed_burn / valid_hours``.

Unified simulation and honesty:
    When Typical is unknown (fewer than 12 valid hours, history unavailable or
    every account held), no numeric future line, checkpoints or schedule are
    emitted under Typical's name. When Typical is known (positive rate, or a
    measured zero with sufficient non-exhausted pool capacity), ONE pooled
    simulation produces the chart line, the pre-first-reset verdict, Details
    checkpoints, reset schedule and depletion instants. Each account refills at
    most once at its next reported reset (no repeated/unreported resets).
    Tracking runnable (unblocked) reserve separately from total selected-window
    reserve ensures that a temporary stall caused by another spent limit is
    surfaced openly rather than reported as uninterrupted availability.
"""

from __future__ import annotations

import bisect
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:  # the host loads the skill as a package: siblings are relative imports
    from . import quota_summary as qs
except ImportError:  # imported directly from the skill directory (tests)
    import quota_summary as qs  # type: ignore[no-redef]

TYPICAL_LOOKBACK_SEC = 48 * 3600.0
TYPICAL_MIN_VALID_SEC = 12 * 3600.0
TYPICAL_COVERAGE = 0.80
VERDICT_GAP_BAD_SEC = 900.0
VERDICT_TIGHT_RESERVE = 0.05


# ---------------------------------------------------------------------------
# 48-hour observed pool burn / valid measured time


def _has_failed_sweep(failed: Sequence[float], lo: float, hi: float) -> bool:
    if not failed:
        return False
    idx = bisect.bisect_left(failed, lo - 1e-6)
    return idx < len(failed) and failed[idx] <= hi + 1e-6


def _source_segments(
    runs: Sequence["qs.Run"],
    win_start: float,
    now: float,
    view: "qs.HistoryView",
    failed: Sequence[float],
) -> List[Tuple[float, float, float, float, float, Optional[float]]]:
    """Valid disjoint comparable segments ``(t0, t1, burn, r0, r1, resets_at)``
    for one ``(account, source)`` inside ``[win_start, now]``."""
    ordered = sorted(runs, key=lambda r: (r.first_seen, r.first_obs))
    segs: List[Tuple[float, float, float, float, float, Optional[float]]] = []
    watch_start = -math.inf
    for idx, run in enumerate(ordered):
        prev = ordered[idx - 1] if idx > 0 else None
        if (prev is None
                or qs.watched_gap(prev, run, view)
                or _has_failed_sweep(failed, prev.first_seen, run.first_seen)):
            watch_start = run.first_seen
        if run.after_correction:
            watch_start = max(watch_start, run.first_seen)
        if run.first_obs > now + 1e-6:
            continue
        # 1. Constant plateau within a run: requires >= 2 distinct fresh
        #    observations so a frozen cached reading (n_obs == 1) never counts,
        #    and never extends earlier than this source's current uninterrupted
        #    watch segment (or correction sighting).
        if run.n_obs >= 2 and not _has_failed_sweep(failed, run.first_seen, run.last_seen):
            obs_start = max(run.first_obs, watch_start)
            obs_end = min(run.last_obs, now)
            if run.resets_at is not None:
                obs_end = min(obs_end, run.resets_at)
            a = max(obs_start, win_start)
            b = min(obs_end, now)
            if b > a + 1e-6:
                segs.append((a, b, 0.0, run.ratio, run.ratio, run.resets_at))
        # 2. Comparable transition from previous run of the SAME source.
        if prev is None:
            continue
        if prev.first_obs > now + 1e-6:
            continue
        if qs.boundary(prev, run, view):
            continue
        if _has_failed_sweep(failed, prev.first_seen, run.first_seen):
            continue
        # A cached observation timestamped before this source's current
        # uninterrupted watch segment (or correction) cannot open a transition
        # unless that level was re-observed at or after watch_start.
        if prev.last_obs < watch_start - 1e-6:
            continue
        t0, t1 = prev.last_obs, run.first_obs
        if t1 <= t0 + 1e-6:
            continue
        if prev.resets_at is not None and t1 > prev.resets_at + 1e-6:
            continue
        if run.resets_at is not None and t1 > run.resets_at + 1e-6:
            continue
        if t0 < win_start - 1e-6 or t1 > now + 1e-6:
            continue
        delta = max(0.0, run.ratio - prev.ratio)
        segs.append((max(t0, win_start), min(t1, now), delta, prev.ratio, run.ratio, run.resets_at))
    segs.sort(key=lambda s: (s[0], s[1]))
    # Guard against any overlap from clock jitter without inventing data.
    clean: List[Tuple[float, float, float, float, float, Optional[float]]] = []
    for seg in segs:
        if clean:
            if seg[0] < clean[-1][1] - 1e-6:
                continue
            if seg[0] < clean[-1][1]:
                seg = (clean[-1][1], seg[1], seg[2], seg[3], seg[4], seg[5])
                if seg[1] <= seg[0] + 1e-6:
                    continue
        clean.append(seg)
    return clean


def _seg_ratio_at(
    seg: Tuple[float, float, float, float, float, Optional[float]],
    at: float,
) -> float:
    t0, t1, _burn, r0, r1, _res = seg
    if t1 <= t0:
        return r0
    return r0 + (r1 - r0) * ((at - t0) / (t1 - t0))


def _sources_disagree(
    covering: Dict[str, Tuple[float, float, float, float, float, Optional[float]]],
    cover: "qs.Cover",
    u: float,
    v: float,
) -> bool:
    """True when concurrent sources for the same account disagree on value or
    reset across ``[u, v]`` (or at either end of a shared covering overlap)."""
    tol = qs.VALUE_TOLERANCE + qs.RATIO_EPS
    segs = list(covering.values())
    ratios_u: List[float] = []
    ratios_v: List[float] = []
    resets: List[float] = []
    for seg in segs:
        ratios_u.append(_seg_ratio_at(seg, u))
        ratios_v.append(_seg_ratio_at(seg, v))
        if seg[5] is not None:
            resets.append(seg[5])
    # Check both endpoints of every shared covering overlap so two crossing
    # transitions (e.g. 10->22% vs 15->17%) cannot agree around their crossing.
    for i in range(len(segs)):
        a = segs[i]
        for j in range(i + 1, len(segs)):
            b = segs[j]
            o0 = max(a[0], b[0])
            o1 = min(a[1], b[1])
            if o1 > o0 + 1e-6:
                if abs(_seg_ratio_at(a, o0) - _seg_ratio_at(b, o0)) > tol:
                    return True
                if abs(_seg_ratio_at(a, o1) - _seg_ratio_at(b, o1)) > tol:
                    return True
    mid = (u + v) * 0.5
    active = qs._active(cover, mid, False)
    for run in active:
        if run.source not in covering:
            ratios_u.append(run.ratio)
            ratios_v.append(run.ratio)
            if run.resets_at is not None:
                resets.append(run.resets_at)
    if (len(active) > 1
            and len({r.ratio for r in active}) > 1
            and any(r.first_obs != r.last_obs and mid < r.last_seen for r in active)):
        return True
    if resets and max(resets) - min(resets) > qs.RESET_TOLERANCE_SEC:
        return True
    if len(ratios_u) > 1 and max(ratios_u) - min(ratios_u) > tol:
        return True
    if len(ratios_v) > 1 and max(ratios_v) - min(ratios_v) > tol:
        return True
    return False


def _merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Merge overlapping or touching ``(lo, hi)`` intervals into disjoint sorted spans."""
    if not intervals:
        return []
    intervals.sort()
    merged: List[Tuple[float, float]] = [intervals[0]]
    for lo, hi in intervals[1:]:
        if lo <= merged[-1][1] + 1e-6:
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    return merged


def _account_segments(
    runs: Sequence["qs.Run"],
    win_start: float,
    now: float,
    view: "qs.HistoryView",
    failed: Sequence[float],
) -> List[Tuple[float, float, float, bool]]:
    """Deduplicated valid intervals ``(u, v, rate_per_sec, has_capacity)`` for
    one account over ``[win_start, now]``."""
    by_src: Dict[str, List[qs.Run]] = {}
    for r in runs:
        by_src.setdefault(r.source, []).append(r)
    src_segs = {
        src: _source_segments(srs, win_start, now, view, failed)
        for src, srs in by_src.items()
    }
    if len(by_src) == 1:
        only = next(iter(src_segs.values()))
        return [
            (t0, t1, burn / (t1 - t0), (r0 < 1.0 - qs.RATIO_EPS or r1 < 1.0 - qs.RATIO_EPS))
            for t0, t1, burn, r0, r1, _res in only
        ]

    cover = qs._member_cover(list(runs), now, view)
    pts: Set[float] = set()
    for segs in src_segs.values():
        for t0, t1, *_rest in segs:
            pts.add(t0)
            pts.add(t1)
    for _starts, spans in cover:
        for run, end in spans:
            for at in (run.first_seen, run.last_seen, end):
                if win_start <= at <= now:
                    pts.add(at)
    if len(pts) < 2:
        return []

    ordered = sorted(pts)
    sources = sorted(src_segs)
    cursors = {src: 0 for src in sources}
    out: List[Tuple[float, float, float, bool]] = []
    sticky: Optional[str] = None
    for u, v in zip(ordered, ordered[1:]):
        if v - u <= 1e-6:
            continue
        covering: Dict[str, Tuple[float, float, float, float, float, Optional[float]]] = {}
        for src in sources:
            segs = src_segs[src]
            pos = cursors[src]
            while pos < len(segs) and segs[pos][1] <= u + 1e-6:
                pos += 1
            cursors[src] = pos
            if pos < len(segs) and segs[pos][0] <= u + 1e-6 and segs[pos][1] >= v - 1e-6:
                covering[src] = segs[pos]
        if not covering:
            sticky = None
            continue
        if _sources_disagree(covering, cover, u, v):
            sticky = None
            continue
        pick = sticky if sticky in covering else min(covering)
        sticky = pick
        t0, t1, burn, r0, r1, _res = covering[pick]
        has_cap = r0 < 1.0 - qs.RATIO_EPS or r1 < 1.0 - qs.RATIO_EPS
        out.append((u, v, burn / (t1 - t0), has_cap))
    return out


def estimate_typical_pace(calc: "qs.GroupCalc", state: "qs.SummaryState") -> Dict[str, Any]:
    """Observed pooled burn rate of one limit over the preceding 48 hours:
    ``total_observed_pool_burn / valid_measured_hours``.

    Never mixes different window durations or scopes. Requires at least
    ``TYPICAL_MIN_VALID_SEC`` (12 valid hours) of intervals where both the
    current measured pool and the historically active cohort meet
    ``TYPICAL_COVERAGE`` (80%, i.e. 100% for 1-4 accounts and >=80% for 5+).
    """
    cached = state.typical_cache.get(calc.key)
    if cached is not None:
        return dict(cached)

    now = state.now
    view = state.view
    window_hours = round(TYPICAL_LOOKBACK_SEC / 3600.0)
    min_hours = round(TYPICAL_MIN_VALID_SEC / 3600.0)

    def _finish(result: Dict[str, Any]) -> Dict[str, Any]:
        state.typical_cache[calc.key] = result
        return dict(result)

    if view is None or view.state == "unavailable":
        return _finish({
            "state": "unavailable", "reason": "history_unavailable",
            "rate_per_hour": None, "valid_seconds": 0, "valid_hours": 0.0,
            "observed_burn": 0.0, "window_hours": window_hours,
            "min_valid_hours": min_hours,
        })

    current_subs = {m.pseudo for m in calc.measured if m.pseudo}
    if not current_subs:
        return _finish({
            "state": "insufficient", "reason": "no_current_accounts",
            "rate_per_hour": None, "valid_seconds": 0, "valid_hours": 0.0,
            "observed_burn": 0.0, "window_hours": window_hours,
            "min_valid_hours": min_hours,
        })

    win_start = now - TYPICAL_LOOKBACK_SEC
    raw_ts = list(view.sweeps)
    raw_ok = (list(view.sweep_ok) if view.sweep_ok is not None and len(view.sweep_ok) == len(raw_ts)
              else [True] * len(raw_ts))
    failed = sorted(t for t, ok in zip(raw_ts, raw_ok) if not ok and t <= now + 1e-6)

    # Gather runs strictly for calc.key and attach live current readings
    # without bridging gaps (same rule as member_rate).
    by_sub: Dict[str, List[qs.Run]] = {}
    for (sub, series), runs in view.runs.items():
        if series == calc.key and runs:
            by_sub[sub] = list(runs)
    for m in calc.measured:
        if not m.pseudo or m.reading is None or m.reading.observed_at is None:
            continue
        if m.reading.observed_at > now + qs.FUTURE_SKEW_SEC:
            continue
        existing = by_sub.get(m.pseudo, [])
        same_src = sorted(
            (r for r in existing if r.source == m.reading.source),
            key=lambda r: (r.first_seen, r.first_obs),
        )
        other_src = [r for r in existing if r.source != m.reading.source]
        by_sub[m.pseudo] = other_src + qs._with_live(same_src, m.reading, now, view)

    if not by_sub:
        warming = view.collecting_since is None or (now - view.collecting_since) < TYPICAL_LOOKBACK_SEC
        return _finish({
            "state": "warming_up" if warming else "insufficient", "reason": "few_hours",
            "rate_per_hour": None, "valid_seconds": 0, "valid_hours": 0.0,
            "observed_burn": 0.0, "window_hours": window_hours,
            "min_valid_hours": min_hours,
        })

    # Sweep-line over disjoint valid measurement segments and merged cohort
    # presence spans inside [win_start, now]. Splitting by account participation
    # boundaries keeps a brief or single sighting of another account local, and
    # avoids re-scanning runs per elementary interval.
    # Event tuple: (at, kind, sub, is_current, rate_sec, has_cap)
    # kind order at equal timestamp: 0=seg_end, 1=pres_end, 2=pres_start, 3=seg_start.
    events: List[Tuple[float, int, str, bool, float, bool]] = []
    for sub, runs in by_sub.items():
        is_cur = sub in current_subs
        segs = _account_segments(runs, win_start, now, view, failed)
        cap_end = now if is_cur else max(r.last_seen for r in runs)
        raw_pres: List[Tuple[float, float]] = []
        for r in runs:
            lo = max(r.first_seen, win_start)
            hi = min(r.last_seen + 86400.0, cap_end, now)
            if hi > lo + 1e-6:
                raw_pres.append((lo, hi))
        for u, v, rate_sec, has_cap in segs:
            if v <= u + 1e-6:
                continue
            events.append((u, 3, sub, is_cur, rate_sec, has_cap))
            events.append((v, 0, sub, is_cur, rate_sec, has_cap))
            raw_pres.append((u, v))
        for lo, hi in _merge_intervals(raw_pres):
            events.append((lo, 2, sub, is_cur, 0.0, False))
            events.append((hi, 1, sub, is_cur, 0.0, False))

    events.sort(key=lambda e: (e[0], e[1]))
    need_current = math.ceil(TYPICAL_COVERAGE * len(current_subs))
    valid_sec = 0.0
    unblocked_valid_sec = 0.0
    total_burn = 0.0

    valid_total = 0
    valid_current = 0
    hist_count = 0
    cap_count = 0
    pos_rates: Dict[str, float] = {}

    n_ev = len(events)
    idx = 0
    while idx < n_ev:
        t_cur = events[idx][0]
        while idx < n_ev and events[idx][0] == t_cur:
            _at, kind, sub, is_cur, rate_sec, has_cap = events[idx]
            if kind == 0:
                valid_total -= 1
                if is_cur:
                    valid_current -= 1
                if has_cap:
                    cap_count -= 1
                pos_rates.pop(sub, None)
            elif kind == 1:
                hist_count -= 1
            elif kind == 2:
                hist_count += 1
            else:
                valid_total += 1
                if is_cur:
                    valid_current += 1
                if has_cap:
                    cap_count += 1
                if rate_sec > 0.0:
                    pos_rates[sub] = rate_sec
            idx += 1
        if idx >= n_ev:
            break
        dt = events[idx][0] - t_cur
        if dt <= 1e-6:
            continue
        if valid_current < need_current:
            continue
        if hist_count > 0 and valid_total < math.ceil(TYPICAL_COVERAGE * hist_count):
            continue
        valid_sec += dt
        if pos_rates:
            total_burn += sum(pos_rates.values()) * dt
        if cap_count > 0:
            unblocked_valid_sec += dt

    valid_hours = valid_sec / 3600.0
    if valid_sec < TYPICAL_MIN_VALID_SEC - 1e-6:
        warming = view.collecting_since is None or (now - view.collecting_since) < TYPICAL_LOOKBACK_SEC
        return _finish({
            "state": "warming_up" if warming else "insufficient", "reason": "few_hours",
            "rate_per_hour": None, "valid_seconds": round(valid_sec),
            "valid_hours": round(valid_hours, 2), "observed_burn": round(total_burn, 6),
            "window_hours": window_hours, "min_valid_hours": min_hours,
        })

    if total_burn <= qs.RATIO_EPS:
        # Observing 0 burn while every observed account was pinned at 100% used
        # cannot prove zero demand, even when the pool is still empty now.
        if unblocked_valid_sec < TYPICAL_MIN_VALID_SEC - 1e-6:
            return _finish({
                "state": "insufficient", "reason": "all_observed_at_limit",
                "rate_per_hour": None, "valid_seconds": round(valid_sec),
                "valid_hours": round(valid_hours, 2), "observed_burn": 0.0,
                "window_hours": window_hours, "min_valid_hours": min_hours,
            })
        return _finish({
            "state": "zero", "reason": "", "rate_per_hour": 0.0,
            "valid_seconds": round(valid_sec), "valid_hours": round(valid_hours, 2),
            "observed_burn": 0.0, "window_hours": window_hours,
            "min_valid_hours": min_hours,
        })

    rate_per_hour = total_burn / valid_hours
    return _finish({
        "state": "ok", "reason": "", "rate_per_hour": rate_per_hour,
        "valid_seconds": round(valid_sec), "valid_hours": round(valid_hours, 2),
        "observed_burn": round(total_burn, 6), "window_hours": window_hours,
        "min_valid_hours": min_hours,
    })


# ---------------------------------------------------------------------------
# Unified pooled simulation, chart line, schedule, checkpoints and verdict

# Two moments this close are one instant (float noise of an epoch timestamp):
# capacity that comes back at the very instant the runnable reserve reaches zero
# serves the demand without a break. Any longer break is a stop, however short.
SAME_INSTANT_SEC = 1e-6
# Flags of an account's own state that hold it (Member.hold_until); any other
# hold of a measured account comes from another spent limit.
ACCOUNT_HOLD_FLAGS = ("disabled", "signed_out", "cooling", "model_exhausted")


def eval_polyline(vertices: Sequence[Sequence[float]], moment: float, right: bool = True) -> float:
    """Evaluate a piecewise-linear ``(t, v)`` polyline at ``moment``."""
    if not vertices:
        return 0.0
    if moment < vertices[0][0]:
        return float(vertices[0][1])
    if moment > vertices[-1][0]:
        return float(vertices[-1][1])
    if not right:
        for idx, (t, v) in enumerate(vertices):
            if abs(t - moment) <= 1e-12:
                return float(v)
            if t > moment and idx > 0:
                t0, v0 = vertices[idx - 1]
                return float(v0 if t == t0 else v0 + (v - v0) * (moment - t0) / (t - t0))
        return float(vertices[-1][1])
    value = float(vertices[0][1])
    for (t0, v0), (t1, v1) in zip(vertices, vertices[1:]):
        if t0 <= moment <= t1:
            if t1 == t0 or moment == t1:
                value = float(v1)
            elif moment == t0:
                value = float(v0)
            else:
                value = float(v0 + (v1 - v0) * (moment - t0) / (t1 - t0))
            if moment < t1:
                break
    return value


def _kept(value: Optional[float], digits: int = 4) -> Optional[float]:
    """``round(value, digits)``, except that a positive value below that
    precision keeps two significant digits: a little reserve or a slow pace is
    never published as a zero that is not there (the widget says "<0.01")."""
    if value is None:
        return None
    out = round(float(value), digits)
    if out == 0.0 and value > qs.RATIO_EPS:
        return float(f"{float(value):.2g}")
    return out


def held_why(member: "qs.Member") -> str:
    """Why a measured account that cannot run, with no reported end, is held:
    its own state (``restricted``: disabled, signed out, cooling down, the
    selected model reported out) or another spent limit."""
    return "restricted" if any(flag in member.flags for flag in ACCOUNT_HOLD_FLAGS) else "other_limit_spent"


def later_of(*ends: Optional[float]) -> Optional[float]:
    """The later of known hold ends (None: no hold; ``math.inf``: no reported end)."""
    known = [end for end in ends if end is not None]
    return max(known) if known else None


def simulate_pool(
    items: Sequence[Tuple[float, Optional[float], Optional[float]]],
    now: float,
    chart_end: float,
    rate: float,
) -> Dict[str, Any]:
    """Single pooled simulation from ``now`` at ``rate`` win/h.

    Each item is ``(remaining, reset_at, blocked_until)``: ``blocked_until`` is
    None for an account that can run now, else the moment every restriction of
    the account ends (``math.inf``: no reported end). Runnable accounts share
    the pool burn, so demand continues when one account hits zero; a restricted
    account keeps its share in the total and is not drained while restricted.
    Each account refills to 1.0 at most once, at its next reported reset.

    The simulation runs through every known event — each reported reset, each
    reported end of a restriction — and on to the depletion of what can still
    run. The span the chart draws is no event: it only cuts the output (the
    line at ``chart_end``, by interpolation between events, and the stops
    listed for it in ``runs_out``). So the 24-hour and the 7-day charts read
    one and the same future.

    A *stop* is a stretch with no usable (runnable) reserve, ``(start, locked,
    resumes)``: ``locked`` is the total still on restricted accounts at its
    start, ``resumes`` the first event after which runnable reserve is back
    (None: no known event brings it back). Every event of one instant is
    applied before a stop is recorded: capacity back at that same instant
    (within SAME_INSTANT_SEC) is no stop; any positive break is one.
    """
    left = [max(0.0, min(1.0, float(rem))) for rem, _res, _blk in items]
    vertices: List[Tuple[float, float]] = [(now, sum(left))]
    refill_adds: Dict[int, float] = {}
    runs_out: List[Dict[str, Any]] = []
    stops: List[Tuple[float, float, Optional[float]]] = []

    def runnable(at: float) -> List[int]:
        return [i for i, (_r, _res, blk) in enumerate(items) if blk is None or blk <= at + 1e-9]

    def usable(at: float) -> float:
        return sum(left[i] for i in runnable(at))

    events = sorted(
        {res for _r, res, _b in items if res is not None and res > now}
        | {blk for _r, _res, blk in items if blk is not None and math.isfinite(blk) and blk > now}
    )
    open_stop: Optional[Tuple[float, float]] = (now, sum(left)) if usable(now) <= qs.RATIO_EPS else None
    refilled_any = False
    t = now
    idx = 0
    while True:
        final = idx >= len(events)
        if not final:
            at = events[idx]
            idx += 1
        else:
            # Past the last known event: on to the depletion of what can still run.
            rest = usable(t)
            if rate <= qs.RATIO_EPS or open_stop is not None or rest <= qs.RATIO_EPS:
                break
            at = t + rest / rate * 3600.0
        zero_at: Optional[float] = None
        locked = 0.0
        if at > t and open_stop is None and rate > qs.RATIO_EPS:
            run = runnable(t)
            run_total = sum(left[i] for i in run)
            if run_total > qs.RATIO_EPS:
                work = rate * (at - t) / 3600.0
                if work >= run_total - qs.RATIO_EPS:
                    zero_at = min(at, t + run_total / rate * 3600.0)
                    for i in run:
                        left[i] = 0.0
                    locked = sum(left)
                    if zero_at < at - 1e-9:
                        vertices.append((zero_at, locked))
                else:
                    factor = max(0.0, 1.0 - work / run_total)
                    for i in run:
                        left[i] *= factor
        vertices.append((at, sum(left)))

        refilled_before = refilled_any
        resetting = [i for i, (_r, res, _b) in enumerate(items) if res is not None and res == at]
        if resetting:
            before_step = sum(left)
            for i in resetting:
                refill_adds[i] = 1.0 - left[i]
                left[i] = 1.0
            refilled_any = True
            after_step = sum(left)
            if abs(after_step - before_step) > 1e-9:
                vertices.append((at, after_step))

        back = usable(at) > qs.RATIO_EPS
        if zero_at is not None and (not back or zero_at < at - SAME_INSTANT_SEC):
            if back:
                stops.append((zero_at, locked, at))
            else:
                open_stop = (zero_at, locked)
            if now + 1e-9 < zero_at <= chart_end + 1e-9:
                if locked > qs.RATIO_EPS:
                    ahead = any((res is not None and res > zero_at)
                                or (blk is not None and math.isfinite(blk) and blk > zero_at)
                                for _r, res, blk in items)
                    runs_out.append({"at": round(zero_at), "reset_reported": ahead,
                                     "after_refill": refilled_before, "blocked_only": True})
                else:
                    ahead = any(res is not None and res > zero_at for _r, res, _b in items)
                    runs_out.append({"at": round(zero_at), "reset_reported": ahead,
                                     "after_refill": refilled_before})
        elif open_stop is not None and back:
            stops.append((open_stop[0], open_stop[1], at))
            open_stop = None
        t = at
        if final:
            break

    if open_stop is not None:
        stops.append((open_stop[0], open_stop[1], None))
    return {
        "vertices": vertices, "refill_adds": refill_adds, "runs_out": runs_out,
        "first_stop": stops[0] if stops else None,
    }


def _format_line(vertices: Sequence[Tuple[float, float]], now: float, end: float) -> List[List[float]]:
    """Slice ``vertices`` to ``[now, end]`` using the chart's rounding rules."""
    if not vertices:
        return []
    first = round(now)
    out: List[List[float]] = [[first, round(vertices[0][1], 4)]]
    for idx in range(1, len(vertices)):
        t, v = vertices[idx]
        if t > end + 1e-9:
            break
        at = round(end) if abs(t - end) <= 1e-9 else max(qs._instant(t), first)
        rv = round(v, 4)
        if out[-1][0] == at and out[-1][1] == rv:
            continue
        out.append([at, rv])
    if out[-1][0] < round(end):
        out.append([round(end), round(eval_polyline(vertices, end, right=True), 4)])
    return out


def _first_reset(
    items: Sequence[Tuple[float, Optional[float], Optional[float]]], now: float
) -> Tuple[Optional[float], List[int]]:
    """The first reported reset ahead and the accounts that reset at that very
    instant. A reset even a second later is a later vertex of the simulation:
    the value after the first reset never counts it."""
    future = [res for _rem, res, _blk in items if res is not None and res > now]
    if not future:
        return None, []
    first_at = min(future)
    return first_at, [i for i, (_rem, res, _blk) in enumerate(items) if res is not None and res == first_at]


def _make_verdict(
    items: Sequence[Tuple[float, Optional[float], Optional[float]]],
    now: float,
    end: float,
    *,
    kind: str,
    status: str,
    rate: Optional[float],
    valid_seconds: int = 0,
    valid_hours: float = 0.0,
    min_valid_hours: int = 12,
    window_hours: int = 48,
    reason: str = "",
    held_reason: str = "",
    held_accounts: int = 0,
    blocked_accounts: int = 0,
    other_binding_labels: Sequence[str] = (),
    sim: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the pre-first-reset verdict from ``sim`` (or an unknown-state
    verdict without fake No-new-use numbers when ``sim is None``).

    Every figure is read off the one simulation: the totals just before and
    just after the first reported reset (at that instant), and the first stop
    that begins before it — when usable reserve runs out (``empty_at``), what
    is still on restricted accounts then (``locked_at_empty``) and the first
    event that brings runnable reserve back (``resumes_at``, ``gap_seconds``;
    None: no known event does)."""
    current = sum(max(0.0, min(1.0, float(rem))) for rem, _res, _blk in items)
    first_reset_at, first_group = _first_reset(items, now)
    first_reset_in_horizon = bool(first_reset_at is not None and first_reset_at <= end)

    before_first: Optional[float] = None
    after_first: Optional[float] = None
    first_adds: Optional[float] = None
    empty_at: Optional[float] = None
    resumes_at: Optional[float] = None
    gap_seconds: Optional[int] = None
    locked: Optional[float] = None

    if sim is not None:
        vertices = sim["vertices"]
        refill_adds = sim["refill_adds"]
        if first_reset_at is not None:
            before_first = eval_polyline(vertices, first_reset_at, right=False)
            after_first = eval_polyline(vertices, first_reset_at, right=True)
            first_adds = sum(refill_adds.get(i, 0.0) for i in first_group)
        stop = sim["first_stop"]
        if kind == "typical" and stop is not None and (
                first_reset_at is None or stop[0] < first_reset_at - 1e-6):
            empty_at, locked, resumes_at = stop
            if resumes_at is not None:
                # A stop is a positive break: never published as a zero length.
                gap_seconds = max(1, round(resumes_at - empty_at))

    has_locked = locked is not None and locked > qs.RATIO_EPS
    # The stop ends at the first reported reset, and nothing is on restricted
    # accounts: "runs out before the reset" says it whole.
    ends_at_reset = (first_reset_at is not None and resumes_at is not None
                     and abs(resumes_at - first_reset_at) <= 1e-6)
    if kind == "no_new_use":
        v_state = "no_new_use" if first_reset_at is not None else "no_new_use_no_reset"
    elif status == "held":
        v_state = "held"
    elif status == "unavailable":
        v_state = "unavailable"
    elif status in ("warming_up", "insufficient"):
        v_state = "insufficient"
    elif empty_at is not None:
        if has_locked:
            v_state = "blocked_gap"
        elif current <= qs.RATIO_EPS and (ends_at_reset or first_reset_at is None):
            v_state = "empty_now"
        elif first_reset_at is None:
            v_state = "runs_out_no_reset"
        elif ends_at_reset:
            # Any real stop, however short: its length sets the tone, not the word.
            v_state = "runs_out"
        else:
            # The reset does not bring it back (that account is restricted then).
            v_state = "blocked_gap"
    elif current <= qs.RATIO_EPS:
        v_state = "empty_now"
    elif status == "zero":
        v_state = "zero_pace"
    elif first_reset_at is not None:
        v_state = "tight" if before_first is not None and before_first <= VERDICT_TIGHT_RESERVE else "lasts"
    else:
        v_state = "lasts_no_reset"

    return {
        "state": v_state,
        "reason": reason,
        "first_reset_at": qs._instant(first_reset_at) if first_reset_at is not None else None,
        "first_reset_accounts": len(first_group),
        "first_reset_in_horizon": first_reset_in_horizon,
        "before_first_reset": _kept(before_first),
        "after_first_reset": _kept(after_first),
        "first_reset_adds": _kept(first_adds),
        "current": _kept(current),
        "empty_at": round(empty_at) if empty_at is not None else None,
        "resumes_at": qs._instant(resumes_at) if resumes_at is not None else None,
        "gap_seconds": gap_seconds,
        "locked_at_empty": _kept(locked) if has_locked else None,
        "rate_per_hour": _kept(rate),
        "valid_seconds": int(valid_seconds),
        "valid_hours": round(float(valid_hours), 2),
        "min_valid_hours": int(min_valid_hours),
        "window_hours": int(window_hours),
        "held_reason": held_reason,
        "held_accounts": int(held_accounts),
        "blocked_accounts": int(blocked_accounts),
        "other_binding_labels": list(other_binding_labels),
    }


def build_no_new_use_verdict(
    members: Sequence["qs.Member"], now: float, end: float
) -> Dict[str, Any]:
    """Pre-first-reset verdict for No new use from the same single simulation."""
    items: List[Tuple[float, Optional[float], Optional[float]]] = [
        (m.remaining, m.reading.resets_at if m.reading is not None else None, None)
        for m in members
    ]
    sim = simulate_pool(items, now, end, 0.0)
    return _make_verdict(items, now, end, kind="no_new_use", status="no_new_use", rate=0.0, sim=sim)


def build_typical_scenario(
    members: Sequence["qs.Member"],
    now: float,
    end: float,
    horizon: str,
    typical_info: Dict[str, Any],
    *,
    blocked_until: Optional[Sequence[Optional[float]]] = None,
    held_reason: str = "",
    other_binding_labels: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build the 48-hour Typical scenario from ONE pooled simulation.

    ``blocked_until`` gives each member's restriction end (see simulate_pool):
    another spent binding limit, or the account's own known state
    (Member.hold_until). When Typical is unknown (``warming_up``,
    ``insufficient``, ``unavailable``, or ``held``), emits ``line = []``,
    ``checkpoints = []``, ``schedule = []`` and no numeric
    ``before_first_reset``/``after_first_reset`` so unknown Typical never
    masquerades as No new use; ``reason`` carries the estimator's own word
    (``all_observed_at_limit``: every account sat at its limit, so its use
    could not be seen).
    """
    blocks = list(blocked_until) if blocked_until is not None else [None] * len(members)
    items: List[Tuple[float, Optional[float], Optional[float]]] = [
        (m.remaining, m.reading.resets_at if m.reading is not None else None, blk)
        for m, blk in zip(members, blocks)
    ]
    perm_held = sum(1 for _rem, _res, blk in items if blk is not None and not math.isfinite(blk))
    temp_blocked = sum(1 for _rem, _res, blk in items if blk is not None and math.isfinite(blk) and blk > now)
    runnable_count = len(items) - perm_held

    raw_state = str(typical_info.get("state") or "insufficient")
    raw_rate = typical_info.get("rate_per_hour")
    reason = str(typical_info.get("reason") or "")
    valid_seconds = int(typical_info.get("valid_seconds") or 0)
    valid_hours = float(typical_info.get("valid_hours") or 0.0)
    window_hours = int(typical_info.get("window_hours") or round(TYPICAL_LOOKBACK_SEC / 3600.0))
    min_hours = int(typical_info.get("min_valid_hours") or round(TYPICAL_MIN_VALID_SEC / 3600.0))

    if runnable_count == 0 and items:
        status = "held"
    elif raw_state == "ok" and raw_rate is not None and raw_rate > qs.RATIO_EPS:
        status = "ok"
    elif raw_state == "zero":
        status = "zero"
    elif raw_state in ("unavailable", "warming_up", "insufficient"):
        status = raw_state
    else:
        status = "insufficient"

    no_reset_count = sum(1 for m in members if m.reading is None or m.reading.resets_at is None)
    inside, later = qs.horizon_events(list(members), now, end)
    later_out = ({
        "at": round(later[0][0]), "accounts": len(later[0][1]),
        "more_events": len(later) - 1,
        "more_accounts": sum(len(group) for _at, group in later[1:]),
    } if later else None)
    rate_out = _kept(float(raw_rate)) if raw_rate is not None else None
    common = {
        "accounts": len(members),
        "no_reset": no_reset_count,
        "kind": "typical",
        "status": status,
        "reason": reason,
        "rate_per_hour": rate_out,
        "valid_seconds": valid_seconds,
        "valid_hours": round(valid_hours, 2),
        "window_hours": window_hours,
        "min_valid_hours": min_hours,
        "blocked_accounts": temp_blocked,
        "later": later_out,
    }
    verdict_args = dict(
        kind="typical", status=status, rate=raw_rate,
        valid_seconds=valid_seconds, valid_hours=valid_hours,
        min_valid_hours=min_hours, window_hours=window_hours, reason=reason,
        held_reason=held_reason, held_accounts=perm_held,
        blocked_accounts=temp_blocked, other_binding_labels=other_binding_labels,
    )

    if status not in ("ok", "zero"):
        return dict(common, **{
            "line": [], "at_pace": 0, "held": len(members), "zero_growth": 0,
            "held_reason": held_reason if status == "held" else "",
            "schedule": [], "checkpoints": [], "runs_out": [],
            "verdict": _make_verdict(items, now, end, sim=None, **verdict_args),
        })

    eff_rate = float(raw_rate) if (status == "ok" and raw_rate is not None) else 0.0
    sim = simulate_pool(items, now, end, eff_rate)
    vertices = sim["vertices"]
    refill_adds = sim["refill_adds"]

    idx_by_id = {id(m): i for i, m in enumerate(members)}
    schedule: List[Dict[str, Any]] = []
    for at, group in inside:
        last = max(m.reading.resets_at for m in group if m.reading and m.reading.resets_at is not None)
        schedule.append({
            "at": qs._instant(at),
            "last": qs._instant(last),
            "accounts": len(group),
            "adds": round(sum(refill_adds.get(idx_by_id[id(m)], 0.0) for m in group), 4),
            "total_after": round(eval_polyline(vertices, last, right=True), 4),
        })

    checkpoints = []
    for offset in qs.CHECKPOINTS.get(horizon, qs.CHECKPOINTS["24h"]):
        moment = round(now + offset)
        if moment <= end:
            checkpoints.append({
                "after_seconds": round(offset),
                "at": moment,
                "value": round(eval_polyline(vertices, moment, right=True), 4),
            })

    active_drain = status == "ok"
    return dict(common, **{
        "line": _format_line(vertices, now, end),
        "at_pace": runnable_count if active_drain else 0,
        "held": perm_held if active_drain else len(members),
        "zero_growth": len(members) if status == "zero" else 0,
        "held_reason": "",
        "schedule": schedule,
        "checkpoints": checkpoints,
        "runs_out": sim["runs_out"],
        "verdict": _make_verdict(items, now, end, sim=sim, **verdict_args),
    })
