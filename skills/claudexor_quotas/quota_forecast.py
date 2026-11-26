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
    for idx, run in enumerate(ordered):
        if run.first_obs > now + 1e-6:
            continue
        # 1. Constant plateau within a run: requires >= 2 distinct fresh
        #    observations so a frozen cached reading (n_obs == 1) never counts.
        if run.n_obs >= 2 and not _has_failed_sweep(failed, run.first_seen, run.last_seen):
            obs_start = max(run.first_obs, run.first_seen) if run.after_correction else run.first_obs
            obs_end = min(run.last_obs, now)
            if run.resets_at is not None:
                obs_end = min(obs_end, run.resets_at)
            a = max(obs_start, win_start)
            b = min(obs_end, now)
            if b > a + 1e-6:
                segs.append((a, b, 0.0, run.ratio, run.ratio, run.resets_at))
        # 2. Comparable transition from previous run of the SAME source.
        if idx == 0:
            continue
        prev = ordered[idx - 1]
        if prev.first_obs > now + 1e-6:
            continue
        if qs.boundary(prev, run, view):
            continue
        if _has_failed_sweep(failed, prev.last_seen, run.first_seen):
            continue
        # A corrected cached observation carries an old timestamp learned only
        # at first_seen: never manufacture watched span before first_seen.
        if prev.after_correction and prev.last_obs < prev.first_seen - 1e-6:
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
        if clean and seg[0] < clean[-1][1] - 1e-6:
            continue
        clean.append(seg)
    return clean


def _sources_disagree(
    covering: Dict[str, Tuple[float, float, float, float, float, Optional[float]]],
    cover: "qs.Cover",
    mid: float,
) -> bool:
    """True when concurrent sources for the same account disagree on value or
    reset around ``mid``."""
    ratios: List[float] = []
    resets: List[float] = []
    for t0, t1, _burn, r0, r1, res in covering.values():
        frac = (mid - t0) / (t1 - t0) if t1 > t0 else 0.0
        ratios.append(r0 + (r1 - r0) * frac)
        if res is not None:
            resets.append(res)
    active = qs._active(cover, mid, False)
    if len(active) > 1:
        for run in active:
            if run.source not in covering:
                ratios.append(run.ratio)
                if run.resets_at is not None:
                    resets.append(run.resets_at)
        if (len({r.ratio for r in active}) > 1
                and any(r.first_obs != r.last_obs and mid < r.last_seen for r in active)):
            return True
    if resets and max(resets) - min(resets) > qs.RESET_TOLERANCE_SEC:
        return True
    if len(ratios) > 1 and max(ratios) - min(ratios) > qs.VALUE_TOLERANCE + qs.RATIO_EPS:
        return True
    return False


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
        mid = (u + v) * 0.5
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
        if _sources_disagree(covering, cover, mid):
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
        same_src = [r for r in existing if r.source == m.reading.source]
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

    acct_segs: Dict[str, List[Tuple[float, float, float, bool]]] = {}
    sub_first: Dict[str, float] = {}
    sub_last: Dict[str, float] = {}
    grid_pts: Set[float] = {win_start, now}
    for sub, runs in by_sub.items():
        sub_first[sub] = min(r.first_seen for r in runs)
        sub_last[sub] = max(r.last_seen for r in runs)
        segs = _account_segments(runs, win_start, now, view, failed)
        if segs:
            acct_segs[sub] = segs
            for u, v, _rate, _cap in segs:
                grid_pts.add(u)
                grid_pts.add(v)

    ordered = sorted(grid_pts)
    seg_subs = sorted(acct_segs)
    cursors = {sub: 0 for sub in seg_subs}
    need_current = math.ceil(TYPICAL_COVERAGE * len(current_subs))
    valid_sec = 0.0
    unblocked_valid_sec = 0.0
    total_burn = 0.0

    for u, v in zip(ordered, ordered[1:]):
        dt = v - u
        if dt <= 1e-6:
            continue
        valid_here: List[Tuple[str, float, bool]] = []
        valid_current = 0
        for sub in seg_subs:
            segs = acct_segs[sub]
            pos = cursors[sub]
            while pos < len(segs) and segs[pos][1] <= u + 1e-6:
                pos += 1
            cursors[sub] = pos
            if pos < len(segs) and segs[pos][0] <= u + 1e-6 and segs[pos][1] >= v - 1e-6:
                _s0, _s1, rate_sec, has_cap = segs[pos]
                valid_here.append((sub, rate_sec, has_cap))
                if sub in current_subs:
                    valid_current += 1
        if valid_current < need_current:
            continue
        valid_set = {sub for sub, _r, _c in valid_here}
        active_hist = [
            sub for sub, runs in by_sub.items()
            if sub_first[sub] <= v + 1e-6
            and (sub in current_subs or sub_last[sub] >= u - 1e-6)
            and any(r.first_seen <= v + 1e-6 and r.last_seen >= u - 86400.0 for r in runs)
        ]
        if active_hist:
            need_hist = math.ceil(TYPICAL_COVERAGE * len(active_hist))
            if sum(1 for sub in active_hist if sub in valid_set) < need_hist:
                continue
        valid_sec += dt
        total_burn += sum(rate_sec * dt for _sub, rate_sec, _cap in valid_here)
        if any(has_cap for _sub, _r, has_cap in valid_here):
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
        # cannot prove zero demand when positive reserve exists now.
        if unblocked_valid_sec < TYPICAL_MIN_VALID_SEC - 1e-6 and sum(m.remaining for m in calc.measured) > qs.RATIO_EPS:
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


def simulate_pool(
    items: Sequence[Tuple[float, Optional[float], Optional[float]]],
    now: float,
    sim_end: float,
    chart_end: float,
    rate: float,
) -> Dict[str, Any]:
    """Single pooled simulation over ``[now, sim_end]`` at ``rate`` win/h.

    Each item is ``(remaining, reset_at, blocked_until)``. Runnable accounts
    share the pool burn so demand continues when one account hits zero; blocked
    or held accounts are not drained while blocked. Each account refills to 1.0
    at most once at its next reported reset; no later reset is assumed.

    Tracks BOTH total selected-window reserve (``vertices``) and usable
    (unblocked) reserve so a stall where runnable accounts hit zero while
    blocked accounts still hold selected-window share is recorded explicitly
    as ``first_usable_empty = (empty_at, locked_total, resumes_at)``.
    """
    left = [max(0.0, min(1.0, float(rem))) for rem, _res, _blk in items]
    total0 = sum(left)
    vertices: List[Tuple[float, float]] = [(now, total0)]
    refill_adds: Dict[int, float] = {}
    runs_out: List[Dict[str, Any]] = []
    first_total_empty_at: Optional[float] = now if total0 <= qs.RATIO_EPS else None
    first_usable_empty: Optional[Tuple[float, float, Optional[float]]] = None
    usable_empty_start: Optional[Tuple[float, float]] = None

    runnable0 = sum(
        left[i] for i, (_r, _res, blk) in enumerate(items) if blk is None or blk <= now + 1e-9
    )
    if total0 > qs.RATIO_EPS and runnable0 <= qs.RATIO_EPS:
        usable_empty_start = (now, total0)

    if sim_end <= now:
        if usable_empty_start is not None:
            first_usable_empty = (usable_empty_start[0], usable_empty_start[1], None)
        return {
            "vertices": vertices, "refill_adds": refill_adds, "runs_out": runs_out,
            "first_total_empty_at": first_total_empty_at,
            "first_usable_empty": first_usable_empty,
        }

    events: Set[float] = {sim_end}
    if now < chart_end < sim_end:
        events.add(chart_end)
    for _rem, res, blk in items:
        if res is not None and now < res <= sim_end:
            events.add(res)
        if blk is not None and math.isfinite(blk) and now < blk <= sim_end:
            events.add(blk)

    refilled_any = False
    t = now
    for at in sorted(events):
        dt = at - t
        if dt > 0:
            runnable = [i for i, (_r, _res, blk) in enumerate(items) if blk is None or blk <= t + 1e-9]
            runnable_total = sum(left[i] for i in runnable)
            if usable_empty_start is not None and runnable_total > qs.RATIO_EPS:
                if first_usable_empty is None:
                    first_usable_empty = (usable_empty_start[0], usable_empty_start[1], t)
                usable_empty_start = None
            if rate > qs.RATIO_EPS and runnable_total > qs.RATIO_EPS:
                work = rate * dt / 3600.0
                if work >= runnable_total - qs.RATIO_EPS:
                    t_zero = min(at, max(t, t + (runnable_total / rate) * 3600.0))
                    for i in runnable:
                        left[i] = 0.0
                    zero_total = sum(left)
                    if t_zero < at - 1e-9:
                        vertices.append((t_zero, zero_total))
                    if zero_total <= qs.RATIO_EPS:
                        if first_total_empty_at is None:
                            first_total_empty_at = t_zero
                        if usable_empty_start is None and first_usable_empty is None:
                            usable_empty_start = (t_zero, 0.0)
                        if t_zero <= chart_end + 1e-9:
                            reset_ahead = any(res is not None and res > t_zero for _r, res, _b in items)
                            runs_out.append({
                                "at": round(t_zero), "reset_reported": reset_ahead,
                                "after_refill": refilled_any,
                            })
                    else:
                        if usable_empty_start is None and first_usable_empty is None:
                            usable_empty_start = (t_zero, zero_total)
                        if t_zero <= chart_end + 1e-9:
                            resume_ahead = any(
                                (res is not None and res > t_zero)
                                or (blk is not None and math.isfinite(blk) and blk > t_zero)
                                for _r, res, blk in items
                            )
                            runs_out.append({
                                "at": round(t_zero), "reset_reported": resume_ahead,
                                "after_refill": refilled_any, "blocked_only": True,
                            })
                else:
                    factor = max(0.0, 1.0 - work / runnable_total)
                    for i in runnable:
                        left[i] *= factor
            vertices.append((at, sum(left)))

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

        if usable_empty_start is not None:
            runnable_after = sum(
                left[i] for i, (_r, _res, blk) in enumerate(items) if blk is None or blk <= at + 1e-9
            )
            if runnable_after > qs.RATIO_EPS:
                if first_usable_empty is None:
                    first_usable_empty = (usable_empty_start[0], usable_empty_start[1], at)
                usable_empty_start = None
        t = at

    if usable_empty_start is not None and first_usable_empty is None:
        first_usable_empty = (usable_empty_start[0], usable_empty_start[1], None)

    return {
        "vertices": vertices, "refill_adds": refill_adds, "runs_out": runs_out,
        "first_total_empty_at": first_total_empty_at,
        "first_usable_empty": first_usable_empty,
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


def _first_reset_cluster(
    items: Sequence[Tuple[float, Optional[float], Optional[float]]], now: float
) -> Tuple[Optional[float], Optional[float], List[int]]:
    future = [res for _rem, res, _blk in items if res is not None and res > now]
    if not future:
        return None, None, []
    first_at = min(future)
    group = [
        i for i, (_rem, res, _blk) in enumerate(items)
        if res is not None and res > now and res - first_at <= qs.RESET_TOLERANCE_SEC
    ]
    last_at = max(items[i][1] for i in group if items[i][1] is not None)
    return first_at, last_at, group


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
    held_reason: str = "",
    held_accounts: int = 0,
    blocked_accounts: int = 0,
    other_binding_labels: Sequence[str] = (),
    sim: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the pre-first-reset verdict from ``sim`` (or an unknown-state
    verdict without fake No-new-use numbers when ``sim is None``)."""
    current = sum(max(0.0, min(1.0, float(rem))) for rem, _res, _blk in items)
    first_reset_at, first_reset_last, first_group = _first_reset_cluster(items, now)
    first_reset_in_horizon = bool(first_reset_at is not None and first_reset_at <= end)

    before_first: Optional[float] = None
    after_first: Optional[float] = None
    first_adds: Optional[float] = None
    empty_at: Optional[float] = None
    resumes_at: Optional[float] = None
    gap_seconds: Optional[int] = None
    locked_at_empty: Optional[float] = None

    if sim is not None:
        vertices = sim["vertices"]
        refill_adds = sim["refill_adds"]
        if first_reset_at is not None and first_reset_last is not None:
            before_first = eval_polyline(vertices, first_reset_at, right=False)
            after_first = eval_polyline(vertices, first_reset_last, right=True)
            first_adds = sum(refill_adds.get(i, 0.0) for i in first_group)

        total_empty = sim["first_total_empty_at"]
        usable_empty = sim["first_usable_empty"]  # (t_empty, locked_total, t_resumes)

        if kind == "typical":
            if current <= qs.RATIO_EPS:
                empty_at = now
                resumes_at = first_reset_at
                if first_reset_at is not None:
                    gap_seconds = max(0, round(first_reset_at - now))
            elif (usable_empty is not None
                  and usable_empty[1] > qs.RATIO_EPS
                  and (first_reset_at is None or usable_empty[0] < first_reset_at - 1e-6)):
                empty_at, locked_at_empty, resumes_at = usable_empty
                if resumes_at is not None:
                    gap_seconds = max(0, round(resumes_at - empty_at))
                elif first_reset_at is not None:
                    gap_seconds = max(0, round(first_reset_at - empty_at))
            elif total_empty is not None and (first_reset_at is None or total_empty <= first_reset_at + 1e-9):
                empty_at = total_empty
                resumes_at = first_reset_at
                if first_reset_at is not None:
                    gap_seconds = max(0, round(first_reset_at - empty_at))

    if kind == "no_new_use":
        v_state = "no_new_use" if first_reset_at is not None else "no_new_use_no_reset"
    elif status == "held":
        v_state = "held"
    elif status == "unavailable":
        v_state = "unavailable"
    elif status in ("warming_up", "insufficient"):
        v_state = "insufficient"
    elif current <= qs.RATIO_EPS:
        v_state = "empty_now"
    elif locked_at_empty is not None and locked_at_empty > qs.RATIO_EPS:
        v_state = "blocked_gap"
    elif status == "zero":
        v_state = "zero_pace"
    elif first_reset_at is not None:
        if empty_at is not None and gap_seconds is not None and gap_seconds >= VERDICT_GAP_BAD_SEC:
            v_state = "runs_out"
        elif empty_at is not None or (before_first is not None and before_first <= VERDICT_TIGHT_RESERVE):
            v_state = "tight"
        else:
            v_state = "lasts"
    else:
        v_state = "runs_out_no_reset" if empty_at is not None else "lasts_no_reset"

    return {
        "state": v_state,
        "first_reset_at": qs._instant(first_reset_at) if first_reset_at is not None else None,
        "first_reset_accounts": len(first_group),
        "first_reset_in_horizon": first_reset_in_horizon,
        "before_first_reset": round(before_first, 4) if before_first is not None else None,
        "after_first_reset": round(after_first, 4) if after_first is not None else None,
        "first_reset_adds": round(first_adds, 4) if first_adds is not None else None,
        "current": round(current, 4),
        "empty_at": round(empty_at) if empty_at is not None else None,
        "resumes_at": qs._instant(resumes_at) if resumes_at is not None else None,
        "gap_seconds": gap_seconds,
        "locked_at_empty": round(locked_at_empty, 4) if locked_at_empty is not None else None,
        "rate_per_hour": round(rate, 4) if rate is not None else None,
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
    _first_at, first_last, _group = _first_reset_cluster(items, now)
    sim_end = max(end, first_last) if first_last is not None else end
    sim = simulate_pool(items, now, sim_end, end, 0.0)
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

    When Typical is unknown (``warming_up``, ``insufficient``, ``unavailable``,
    or ``held``), emits ``line = []``, ``checkpoints = []``, ``schedule = []``
    and no numeric ``before_first_reset``/``after_first_reset`` so unknown
    Typical never masquerades as No new use.
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

    known = status in ("ok", "zero")
    if not known:
        verdict = _make_verdict(
            items, now, end, kind="typical", status=status, rate=raw_rate,
            valid_seconds=valid_seconds, valid_hours=valid_hours,
            min_valid_hours=min_hours, window_hours=window_hours,
            held_reason=held_reason, held_accounts=perm_held,
            blocked_accounts=temp_blocked, other_binding_labels=other_binding_labels,
            sim=None,
        )
        return {
            "line": [],
            "accounts": len(members),
            "at_pace": 0,
            "held": len(members),
            "zero_growth": 0,
            "no_reset": no_reset_count,
            "kind": "typical",
            "status": status,
            "held_reason": held_reason if status == "held" else "",
            "rate_per_hour": round(float(raw_rate), 4) if raw_rate is not None else None,
            "valid_seconds": valid_seconds,
            "valid_hours": round(valid_hours, 2),
            "window_hours": window_hours,
            "min_valid_hours": min_hours,
            "blocked_accounts": temp_blocked,
            "schedule": [],
            "later": later_out,
            "checkpoints": [],
            "runs_out": [],
            "verdict": verdict,
        }

    eff_rate = float(raw_rate) if (status == "ok" and raw_rate is not None) else 0.0
    first_at, first_last, _group = _first_reset_cluster(items, now)
    sim_end = end
    if first_last is not None:
        sim_end = max(sim_end, first_last)
    elif eff_rate > qs.RATIO_EPS:
        # Extend once past the visible span to find depletion when no reset is reported.
        runnable_rem = sum(rem for rem, _res, blk in items if blk is None or math.isfinite(blk))
        if runnable_rem > qs.RATIO_EPS:
            max_blk = max(
                (blk for _rem, _res, blk in items if blk is not None and math.isfinite(blk) and blk > now),
                default=now,
            )
            sim_end = max(sim_end, max_blk + (runnable_rem / eff_rate) * 3600.0 + 1.0)
    else:
        # Even at zero burn, extend past the latest temporary unblock so a
        # currently blocked pool knows when usable reserve resumes.
        max_blk = max(
            (blk for _rem, _res, blk in items if blk is not None and math.isfinite(blk) and blk > now),
            default=now,
        )
        sim_end = max(sim_end, max_blk)

    sim = simulate_pool(items, now, sim_end, end, eff_rate)
    vertices = sim["vertices"]
    refill_adds = sim["refill_adds"]
    line = _format_line(vertices, now, end)

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

    verdict = _make_verdict(
        items, now, end, kind="typical", status=status, rate=raw_rate,
        valid_seconds=valid_seconds, valid_hours=valid_hours,
        min_valid_hours=min_hours, window_hours=window_hours,
        held_reason=held_reason, held_accounts=perm_held,
        blocked_accounts=temp_blocked, other_binding_labels=other_binding_labels,
        sim=sim,
    )
    active_drain = status == "ok"
    return {
        "line": line,
        "accounts": len(members),
        "at_pace": runnable_count if active_drain else 0,
        "held": perm_held if active_drain else len(members),
        "zero_growth": len(members) if status == "zero" else 0,
        "no_reset": no_reset_count,
        "kind": "typical",
        "status": status,
        "held_reason": "",
        "rate_per_hour": round(float(raw_rate), 4) if raw_rate is not None else None,
        "valid_seconds": valid_seconds,
        "valid_hours": round(valid_hours, 2),
        "window_hours": window_hours,
        "min_valid_hours": min_hours,
        "blocked_accounts": temp_blocked,
        "schedule": schedule,
        "later": later_out,
        "checkpoints": checkpoints,
        "runs_out": sim["runs_out"],
        "verdict": verdict,
    }
