"""48-hour pooled Typical forecast and unified pooled future simulation.

Pure calculation over ``quota_summary`` data structures (stdlib only, no I/O).
Keeps the trailing-hour watched ``recent_pace`` (used in the summary tool,
context snapshot and Details) completely separate from this forecast.

Estimator:
    Reads ONLY the exact selected limit series (never mixing window lengths,
    request kinds or model scopes) over the common relevant interval
    ``[now - 48h - 2 * SIGHTING_GAP_SEC, now]`` (matching ``HistoryStore.read``
    Pass 1 so 24 h and 7 d spans always see the same runs, failed sweeps and
    edge continuity readings into ``[now - 48h, now]`` even when older chart
    history is capped).
    Attaches all fresh recordable live readings per source from ``state.norm``
    (falling back to ``calc.measured`` for synthetic states with empty
    ``norm.readings``) via ``quota_summary._with_live`` without bridging gaps.
    For each account and source, extracts valid comparable segments:
    - Same-timestamp superseded readings (e.g. a bad reading replaced at the
      same ``observed_at``) are retracted so they never count as burn or
      cross-source conflicts; when replaced inside one unbroken watch, the
      corrected reading stands in for the retracted sighting without bridging
      any gap or extrapolating hidden plateau timestamps.
    - Unchanged plateaus with at least two distinct fresh observations
      (``n_obs >= 2``), unbroken watch, no failed sweeps and an unsuperseded
      end contribute zero burn over their clipped observation span; a single
      cached observation (``n_obs == 1``) contributes zero measured time.
    - Consecutive runs of the same ``(account, source, plan, reset cycle)``
      with ``boundary(prev, nxt, view) == ""``, no failed sweeps in between,
      strictly advancing observation time inside ``[now - 48h, now]`` and
      non-negative delta contribute their positive ratio increase.
    - Multi-source accounts compute account-wide conflicts across every pair
      of distinct sources (active reset spans, segment compatibility,
      observation-vs-segment and simultaneous observation agreement within
      ``VALUE_TOLERANCE`` and ``RESET_TOLERANCE_SEC``, with no linear chord
      interpolation, no artificial padding, and no veto by a frozen ``n_obs == 1``
      old cached reading on newer fresh observations) and apply them to every
      source so an intermediate or resetless source cannot mask a real
      disagreement, then deduplicate clean same-source legs with a simple
      deterministic source choice on transition-atomic cells.

Pool coverage, capacity-bearing denominator and explicit limitations:
    An elementary interval is adequately covered only when at least
    ``ceil(0.80 * N)`` of the ``N`` currently measured accounts AND at least
    ``ceil(0.80 * M)`` of the active historical cohort have valid comparable
    measurements (100% for 1-4 accounts, >=80% for 5+). Within adequately
    covered time, only intervals where at least one observed account still had
    selected-limit reserve (``ratio < 1.0 - RATIO_EPS``) count toward
    ``valid_seconds`` / ``valid_hours`` and the rate denominator, requiring
    >=12 such hours before emitting ``rate_per_hour = observed_burn /
    valid_hours`` or ``state == "zero"``. When >=12h were covered overall but
    zero hours had reserve, returns ``insufficient`` /
    ``all_observed_at_limit``; mixed positive reserve hours below 12h return
    ``insufficient`` / ``few_hours`` reporting those hours and no rate.

    Explicit limitations:
    - No extrapolation over missing accounts under 80% coverage (5+ accounts).
    - Exhaustion censoring applies to the selected limit series only (not
      historical pauses caused by a sibling binding window).
    - Historical cohort presence comes from recorded sightings rather than a
      separate historical roster table.

Unified simulation and honesty:
    When Typical is unknown (fewer than 12 valid hours with available reserve,
    history unavailable or every account held), no numeric future line,
    checkpoints or schedule are emitted under Typical's name. When Typical is
    known (positive rate, or a measured zero with sufficient non-exhausted pool
    capacity), ONE pooled simulation produces the chart line, the
    pre-first-reset verdict, Details checkpoints, reset schedule and depletion
    instants. Each account refills at most once at its next reported reset (no
    repeated/unreported resets). Tracking runnable (unblocked) reserve
    separately from total selected-window reserve ensures that a temporary
    stall caused by another spent limit is surfaced openly rather than reported
    as uninterrupted availability.
"""

from __future__ import annotations

import bisect
import math
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

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
# 48-hour observed pool burn / valid capacity-bearing measured time


class _Seg(NamedTuple):
    t0: float
    t1: float
    burn: float
    r0: float
    r1: float
    resets_at: Optional[float]
    is_plateau: bool


def _has_failed_sweep(failed: Sequence[float], lo: float, hi: float) -> bool:
    if not failed:
        return False
    idx = bisect.bisect_left(failed, lo - 1e-6)
    return idx < len(failed) and failed[idx] <= hi + 1e-6


def _seg_has_cap(seg: _Seg) -> bool:
    return seg.r0 < 1.0 - qs.RATIO_EPS or seg.r1 < 1.0 - qs.RATIO_EPS


def _clean_source_runs(
    runs: Sequence["qs.Run"],
    now: float,
    view: "qs.HistoryView",
    failed: Sequence[float],
) -> List[Tuple["qs.Run", bool]]:
    """Chronological ``(run, last_superseded)`` pairs for one source.

    Retracts observations superseded at the same ``observed_at`` by a later
    sighting with changed content or ``after_correction``. When a retracted
    single-observation run is replaced within the same unbroken watch, the
    replacement inherits the original sighting start so a valid comparable
    change matches a clean control; a gapped correction still retracts the bad
    observation without bridging the gap.
    """
    ordered = sorted(
        (r for r in runs if r.first_seen <= now + 1e-6),
        key=lambda r: (r.first_seen, r.first_obs, int(r.after_correction)),
    )
    n = len(ordered)
    last_sup = [False] * n
    first_sup = [False] * n
    for i in range(n - 2, -1, -1):
        cur, nxt = ordered[i], ordered[i + 1]
        if abs(nxt.first_obs - cur.last_obs) <= 1e-6 and (
            first_sup[i + 1]
            or nxt.after_correction
            or nxt.ratio != cur.ratio
            or nxt.resets_at != cur.resets_at
            or nxt.plan != cur.plan
        ):
            last_sup[i] = True
            if abs(cur.first_obs - cur.last_obs) <= 1e-6:
                first_sup[i] = True

    out: List[Tuple[qs.Run, bool]] = []
    ret_start: Optional[qs.Run] = None
    ret_prev: Optional[qs.Run] = None
    ret_gap = False
    for i, run in enumerate(ordered):
        if first_sup[i] and last_sup[i]:
            if ret_start is None:
                ret_start = run
                ret_gap = False
            elif (qs.watched_gap(ret_prev, run, view)
                  or _has_failed_sweep(failed, ret_prev.first_seen, run.first_seen)):
                ret_gap = True
            ret_prev = run
            continue
        if ret_start is not None:
            broken = (
                ret_gap
                or qs.watched_gap(ret_prev, run, view)
                or _has_failed_sweep(failed, ret_prev.first_seen, run.first_seen)
            )
            if not broken and abs(run.first_obs - ret_start.first_obs) <= 1e-6:
                run = qs.replace(
                    run,
                    first_seen=ret_start.first_seen,
                    after_gap=ret_start.after_gap,
                    after_correction=ret_start.after_correction,
                )
            elif broken:
                run = qs.replace(run, after_gap=True)
            ret_start = ret_prev = None
            ret_gap = False
        out.append((run, last_sup[i]))
    return out


def _source_evidence(
    runs: Sequence["qs.Run"],
    win_start: float,
    now: float,
    view: "qs.HistoryView",
    failed: Sequence[float],
    *,
    need_spans: bool = False,
) -> Tuple[
    List[_Seg],
    List[Tuple[float, float, Optional[float]]],
    List[Tuple[float, float, float]],
    List[Tuple[float, float]],
]:
    """Extract disjoint comparable segments and non-superseded cross-source
    observations / active reset spans / single-sweep reset sightings for one
    ``(account, source)``."""
    cleaned = _clean_source_runs(runs, now, view, failed)
    segs: List[_Seg] = []
    watch_starts: List[float] = []
    watch_start = -math.inf
    for idx, (run, run_last_sup) in enumerate(cleaned):
        prev, prev_last_sup = cleaned[idx - 1] if idx > 0 else (None, False)
        if (prev is None
                or qs.watched_gap(prev, run, view)
                or _has_failed_sweep(failed, prev.first_seen, run.first_seen)):
            watch_start = run.first_seen
        if run.after_correction and not prev_last_sup:
            watch_start = max(watch_start, run.first_seen)
        watch_starts.append(watch_start)
        if run.first_obs > now + 1e-6:
            continue
        # 1. Constant plateau within a run: requires >= 2 distinct fresh
        #    observations, unbroken watch, no failed sweep and an unsuperseded
        #    end observation; never extends earlier than watch_start.
        if (run.n_obs >= 2
                and not run_last_sup
                and not _has_failed_sweep(failed, run.first_seen, run.last_seen)):
            obs_start = max(run.first_obs, watch_start)
            obs_end = min(run.last_obs, now)
            if run.resets_at is not None:
                obs_end = min(obs_end, run.resets_at)
            a, b = max(obs_start, win_start), min(obs_end, now)
            if b > a + 1e-6:
                segs.append(_Seg(a, b, 0.0, run.ratio, run.ratio, run.resets_at, True))
        # 2. Comparable transition from previous run of the SAME source.
        if prev is None or prev_last_sup or prev.first_obs > now + 1e-6:
            continue
        if qs.boundary(prev, run, view):
            continue
        if _has_failed_sweep(failed, prev.first_seen, run.first_seen):
            continue
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
        is_flat = delta <= qs.RATIO_EPS
        segs.append(_Seg(
            max(t0, win_start), min(t1, now),
            0.0 if is_flat else delta, prev.ratio, run.ratio,
            run.resets_at, is_flat,
        ))

    segs.sort(key=lambda s: (s.t0, s.t1))
    clean: List[_Seg] = []
    for seg in segs:
        if clean:
            if seg.t0 < clean[-1].t1 - 1e-6:
                continue
            if seg.t0 < clean[-1].t1:
                if seg.t1 <= clean[-1].t1 + 1e-6:
                    continue
                seg = seg._replace(t0=clean[-1].t1)
        clean.append(seg)

    if not need_spans:
        return clean, [], [], []

    same_t = qs.SAME_INSTANT_SEC
    obs: List[Tuple[float, float, Optional[float]]] = []
    reset_spans: List[Tuple[float, float, float]] = []
    reset_pts: List[Tuple[float, float]] = []
    spans = qs._source_spans([r for r, _sup in cleaned], now, view)
    for (run, end), (_r, run_last_sup), w_start in zip(spans, cleaned, watch_starts):
        if run.first_obs > now + 1e-6:
            continue
        if run.resets_at is not None:
            lo = max(run.first_seen, win_start)
            hi = min(end, now, run.last_obs) if run_last_sup else min(end, now)
            if hi > lo + 1e-6:
                reset_spans.append((lo, hi, run.resets_at))
            elif (not run_last_sup
                  and win_start - same_t <= run.first_seen <= now + 1e-6):
                reset_pts.append((run.first_seen, run.resets_at))
        obs_lo = max(win_start - same_t, w_start - qs.SIGHTING_GAP_SEC - 1e-6)
        if obs_lo <= run.first_obs <= now + 1e-6:
            obs.append((run.first_obs, run.ratio, run.resets_at))
        if (not run_last_sup
                and run.last_obs > run.first_obs + 1e-6
                and obs_lo <= run.last_obs <= now + 1e-6):
            obs.append((run.last_obs, run.ratio, run.resets_at))

    obs.sort(key=lambda o: o[0])
    reset_spans.sort(key=lambda r: (r[0], r[1]))
    reset_pts.sort(key=lambda p: p[0])
    return clean, obs, reset_spans, reset_pts


def _source_segments(
    runs: Sequence["qs.Run"],
    win_start: float,
    now: float,
    view: "qs.HistoryView",
    failed: Sequence[float],
) -> List[Tuple[float, float, float, float, float, Optional[float]]]:
    """Valid disjoint comparable segments ``(t0, t1, burn, r0, r1, resets_at)``
    for one ``(account, source)`` inside ``[win_start, now]``."""
    segs, _obs, _rspans, _rpts = _source_evidence(runs, win_start, now, view, failed)
    return [(s.t0, s.t1, s.burn, s.r0, s.r1, s.resets_at) for s in segs]


def _point_vs_seg_conflict(
    t: float, y: float, res_t: Optional[float], seg: _Seg
) -> bool:
    """True when observation ``(t, y, res_t)`` of another source contradicts
    ``seg`` under monotonic same-cycle semantics and ``quota_summary`` tolerances."""
    same_t = qs.SAME_INSTANT_SEC
    if t < seg.t0 - same_t or t > seg.t1 + same_t:
        return False
    # Ended cycles cannot veto a running cycle at the same instant (quota_summary).
    pt_ended = res_t is not None and res_t <= t + same_t
    seg_ended = seg.resets_at is not None and seg.resets_at <= t + same_t
    if pt_ended != seg_ended:
        return False
    if (seg.resets_at is not None
            and res_t is not None
            and abs(seg.resets_at - res_t) > qs.RESET_TOLERANCE_SEC):
        return True
    tol = qs.VALUE_TOLERANCE + qs.RATIO_EPS
    near0 = abs(t - seg.t0) <= same_t
    near1 = abs(t - seg.t1) <= same_t
    if near0 and not near1:
        return abs(y - seg.r0) > tol
    if near1 and not near0:
        return abs(y - seg.r1) > tol
    return y < seg.r0 - tol or y > seg.r1 + tol


def _segments_conflict(a: _Seg, b: _Seg) -> bool:
    """Symmetric monotonic + reset compatibility check for two segments of
    different sources without linear chord interpolation."""
    same_t = qs.SAME_INSTANT_SEC
    if a.t1 < b.t0 - same_t or b.t1 < a.t0 - same_t:
        return False
    if (min(a.t1, b.t1) > max(a.t0, b.t0) + 1e-6
            and a.resets_at is not None
            and b.resets_at is not None
            and abs(a.resets_at - b.resets_at) > qs.RESET_TOLERANCE_SEC):
        return True
    return (
        _point_vs_seg_conflict(a.t0, a.r0, a.resets_at, b)
        or _point_vs_seg_conflict(a.t1, a.r1, a.resets_at, b)
        or _point_vs_seg_conflict(b.t0, b.r0, b.resets_at, a)
        or _point_vs_seg_conflict(b.t1, b.r1, b.resets_at, a)
    )


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


def _subtract_intervals(
    a: float, b: float, exclusions: Sequence[Tuple[float, float]]
) -> List[Tuple[float, float]]:
    """Return disjoint sub-intervals of ``[a, b]`` remaining after removing sorted disjoint ``exclusions``."""
    if not exclusions:
        return [(a, b)]
    out: List[Tuple[float, float]] = []
    cur = a
    for lo, hi in exclusions:
        if hi <= cur + 1e-6:
            continue
        if lo >= b - 1e-6:
            break
        if lo > cur + 1e-6:
            out.append((cur, min(lo, b)))
        cur = max(cur, hi)
        if cur >= b - 1e-6:
            break
    if b > cur + 1e-6:
        out.append((cur, b))
    return out


def _in_intervals(t: float, intervals: Sequence[Tuple[float, float]], margin: float) -> bool:
    if not intervals:
        return False
    idx = bisect.bisect_right(intervals, (t + margin, math.inf)) - 1
    return idx >= 0 and intervals[idx][1] >= t - margin


def _account_conflicts(
    sources: Sequence[str],
    evidence: Dict[str, Tuple[
        List[_Seg], List[float],
        List[Tuple[float, float, Optional[float]]], List[float],
        List[Tuple[float, float, float]], List[float],
        List[Tuple[float, float]],
    ]],
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, bool]]]:
    """Account-wide cross-source value and reset conflicts across all source pairs.

    Returns ``(conflict_intervals, conflict_points)`` where any two distinct
    sources genuinely disagree — over active reset spans, overlapping segments,
    observation-vs-segment bounds, or simultaneous observations — so neither a
    middle-valued source nor a source lacking ``resets_at`` can mask a real
    conflict. Asynchronous monotone transitions and pre-watch cached readings
    are never flagged as conflicts. Each conflict point carries ``(t, ended)``
    so an ended-cycle conflict cannot veto a running cycle at a reset rollover.
    """
    same_t = qs.SAME_INSTANT_SEC
    tol = qs.RESET_TOLERANCE_SEC
    val_tol = qs.VALUE_TOLERANCE + qs.RATIO_EPS
    int_by_src: Dict[str, Tuple[List[Tuple[float, float, float]], List[float]]] = {}
    sweep_events: List[Tuple[float, int, str, float]] = []
    r_pts: List[Tuple[float, float, str]] = []
    all_obs: List[Tuple[float, float, Optional[float], str]] = []

    for src in sources:
        segs, _seg_ends, obs, _obs_ts, rspans, _max_hi, rpts = evidence[src]
        src_ints: List[Tuple[float, float, float]] = list(rspans)
        for s in segs:
            if s.resets_at is not None and s.t1 > s.t0 + 1e-6:
                src_ints.append((s.t0, s.t1, s.resets_at))
        src_ints.sort(key=lambda r: (r[0], r[1]))
        max_hi: List[float] = []
        hi_so_far = -math.inf
        for lo, hi, r in src_ints:
            hi_so_far = max(hi_so_far, hi)
            max_hi.append(hi_so_far)
            sweep_events.append((lo, 1, src, r))
            sweep_events.append((hi, 0, src, r))
        int_by_src[src] = (src_ints, max_hi)
        for ot, oy, ores in obs:
            all_obs.append((ot, oy, ores, src))
            if ores is not None:
                r_pts.append((ot, ores, src))
        for rt, rval in rpts:
            r_pts.append((rt, rval, src))

    # 1. Active reset-span conflicts (end events precede start events for exact rollovers).
    sweep_events.sort(key=lambda e: (e[0], e[1]))
    active: Dict[str, Dict[float, int]] = {}
    raw_intervals: List[Tuple[float, float]] = []
    n_ev = len(sweep_events)
    idx = 0
    while idx < n_ev:
        t_cur = sweep_events[idx][0]
        while idx < n_ev and sweep_events[idx][0] == t_cur:
            _at, kind, src, r = sweep_events[idx]
            bucket = active.setdefault(src, {})
            if kind == 1:
                bucket[r] = bucket.get(r, 0) + 1
            else:
                cnt = bucket.get(r, 0) - 1
                if cnt <= 0:
                    bucket.pop(r, None)
                    if not bucket:
                        active.pop(src, None)
                else:
                    bucket[r] = cnt
            idx += 1
        if idx >= n_ev:
            break
        t_nxt = sweep_events[idx][0]
        if t_nxt <= t_cur + 1e-6 or len(active) < 2:
            continue
        ranges = [(min(rs), max(rs)) for rs in active.values()]
        conflict = False
        for i in range(len(ranges)):
            lo_i, hi_i = ranges[i]
            for j in range(i + 1, len(ranges)):
                lo_j, hi_j = ranges[j]
                if hi_i - lo_j > tol or hi_j - lo_i > tol:
                    conflict = True
                    break
            if conflict:
                break
        if conflict:
            raw_intervals.append((t_cur, t_nxt))

    conflict_pts_set = set()

    # 2. Segment-vs-segment conflicts across every pair of distinct sources.
    for idx_a, src_a in enumerate(sources):
        segs_a = evidence[src_a][0]
        if not segs_a:
            continue
        for idx_b in range(idx_a + 1, len(sources)):
            segs_b, ends_b = evidence[sources[idx_b]][0], evidence[sources[idx_b]][1]
            if not segs_b:
                continue
            for seg_a in segs_a:
                j = bisect.bisect_left(ends_b, seg_a.t0 - same_t - 1e-6)
                while j < len(segs_b) and segs_b[j].t0 <= seg_a.t1 + same_t + 1e-6:
                    seg_b = segs_b[j]
                    if _segments_conflict(seg_a, seg_b):
                        o0, o1 = max(seg_a.t0, seg_b.t0), min(seg_a.t1, seg_b.t1)
                        if o1 > o0 + 1e-6:
                            raw_intervals.append((o0, o1))
                        else:
                            for pt_t, pt_y, pt_r, other_seg in (
                                (seg_a.t0, seg_a.r0, seg_a.resets_at, seg_b),
                                (seg_a.t1, seg_a.r1, seg_a.resets_at, seg_b),
                                (seg_b.t0, seg_b.r0, seg_b.resets_at, seg_a),
                                (seg_b.t1, seg_b.r1, seg_b.resets_at, seg_a),
                            ):
                                if _point_vs_seg_conflict(pt_t, pt_y, pt_r, other_seg):
                                    conflict_pts_set.add((pt_t, pt_r is not None and pt_r <= pt_t + same_t))
                    j += 1

    # 3. Simultaneous observation value conflicts and observation-vs-segment conflicts.
    all_obs.sort(key=lambda o: o[0])
    n_obs = len(all_obs)
    for i, (t_a, y_a, r_a, s_a) in enumerate(all_obs):
        ended_a = r_a is not None and r_a <= t_a + same_t
        j = i + 1
        while j < n_obs and all_obs[j][0] <= t_a + same_t + 1e-6:
            t_b, y_b, r_b, s_b = all_obs[j]
            if s_b != s_a:
                ended_b = r_b is not None and r_b <= t_b + same_t
                if ended_a == ended_b and abs(y_a - y_b) > val_tol:
                    conflict_pts_set.add((t_a, ended_a))
                    conflict_pts_set.add((t_b, ended_b))
            j += 1
        for other in sources:
            if other == s_a:
                continue
            osegs, oends = evidence[other][0], evidence[other][1]
            if not osegs:
                continue
            k = bisect.bisect_left(oends, t_a - same_t - 1e-6)
            while k < len(osegs) and osegs[k].t0 <= t_a + same_t + 1e-6:
                if _point_vs_seg_conflict(t_a, y_a, r_a, osegs[k]):
                    conflict_pts_set.add((t_a, ended_a))
                    break
                k += 1

    # 4. Known-reset point conflicts against other known-reset points and active reset spans.
    r_pts.sort(key=lambda p: p[0])
    n_pts = len(r_pts)
    for i, (t_a, r_a, s_a) in enumerate(r_pts):
        ended_a = r_a <= t_a + same_t
        j = i + 1
        while j < n_pts and r_pts[j][0] <= t_a + same_t + 1e-6:
            t_b, r_b, s_b = r_pts[j]
            if s_b != s_a:
                ended_b = r_b <= t_b + same_t
                if ended_a == ended_b and abs(r_a - r_b) > tol:
                    conflict_pts_set.add((t_a, ended_a))
                    conflict_pts_set.add((t_b, ended_b))
            j += 1
        for other in sources:
            if other == s_a:
                continue
            oints, omax_hi = int_by_src[other]
            if not oints:
                continue
            k = bisect.bisect_left(omax_hi, t_a - same_t - 1e-6)
            while k < len(oints) and oints[k][0] <= t_a + same_t + 1e-6:
                lo, hi, r_b = oints[k]
                if hi >= t_a - same_t - 1e-6:
                    ended_b = r_b <= t_a + same_t
                    if ended_a == ended_b and abs(r_a - r_b) > tol:
                        conflict_pts_set.add((t_a, ended_a))
                        break
                k += 1

    return _merge_intervals(raw_intervals), sorted(conflict_pts_set)


def _filter_clean_segments(
    segs: Sequence[_Seg],
    conflicts: Tuple[Sequence[Tuple[float, float]], Sequence[Tuple[float, bool]]] = ((), ()),
) -> List[_Seg]:
    """Keep segments free of account-wide cross-source value/reset conflicts.

    Transitions are atomic (dropped on any conflict). Plateaus carve out exact
    overlapping conflict intervals and drop on any standalone conflict point
    not covered by those exclusions, without artificial padding.
    """
    same_t = qs.SAME_INSTANT_SEC
    c_ints, c_pts = conflicts
    c_ends = [hi for _lo, hi in c_ints]
    c_pt_ts = [pt[0] for pt in c_pts]
    kept: List[_Seg] = []
    for seg in segs:
        exclusions: List[Tuple[float, float]] = []
        drop = False
        if c_ints:
            j = bisect.bisect_left(c_ends, seg.t0 + 1e-6)
            while j < len(c_ints) and c_ints[j][0] < seg.t1 - 1e-6:
                lo, hi = c_ints[j]
                if hi > seg.t0 + 1e-6:
                    if seg.is_plateau:
                        exclusions.append((max(seg.t0, lo), min(seg.t1, hi)))
                    else:
                        drop = True
                        break
                j += 1
        if drop:
            continue
        merged_excl = _merge_intervals(exclusions)
        if c_pts:
            j = bisect.bisect_left(c_pt_ts, seg.t0 - same_t - 1e-6)
            while j < len(c_pts) and c_pt_ts[j] <= seg.t1 + same_t + 1e-6:
                pt_t, pt_ended = c_pts[j]
                if seg.resets_at is not None and (seg.resets_at <= pt_t + same_t) != pt_ended:
                    j += 1
                    continue
                if not seg.is_plateau or not _in_intervals(pt_t, merged_excl, same_t + 1e-6):
                    drop = True
                    break
                j += 1
        if drop:
            continue
        if not merged_excl:
            kept.append(seg)
        else:
            for a, b in _subtract_intervals(seg.t0, seg.t1, merged_excl):
                kept.append(seg._replace(t0=a, t1=b))
    return kept


def _account_segments(
    runs: Sequence["qs.Run"],
    win_start: float,
    now: float,
    view: "qs.HistoryView",
    failed: Sequence[float],
) -> List[Tuple[float, float, float, bool]]:
    """Deduplicated valid intervals ``(u, v, rate_per_sec, has_capacity)`` for
    one account over ``[win_start, now]``."""
    by_src_runs: Dict[str, List[qs.Run]] = {}
    for r in runs:
        by_src_runs.setdefault(r.source, []).append(r)
    if len(by_src_runs) == 1:
        only_segs, _obs, _rspans, _rpts = _source_evidence(
            next(iter(by_src_runs.values())), win_start, now, view, failed
        )
        return [(s.t0, s.t1, s.burn / (s.t1 - s.t0), _seg_has_cap(s)) for s in only_segs]

    sources = sorted(by_src_runs)
    evidence: Dict[str, Tuple[
        List[_Seg], List[float],
        List[Tuple[float, float, Optional[float]]], List[float],
        List[Tuple[float, float, float]], List[float],
        List[Tuple[float, float]],
    ]] = {}
    for src in sources:
        segs, obs, rspans, rpts = _source_evidence(
            by_src_runs[src], win_start, now, view, failed, need_spans=True
        )
        max_hi: List[float] = []
        hi_so_far = -math.inf
        for _lo, hi, _res in rspans:
            hi_so_far = max(hi_so_far, hi)
            max_hi.append(hi_so_far)
        evidence[src] = (segs, [s.t1 for s in segs], obs, [o[0] for o in obs], rspans, max_hi, rpts)

    conflicts = _account_conflicts(sources, evidence)
    clean_by_src: Dict[str, List[_Seg]] = {}
    for src in sources:
        kept = _filter_clean_segments(evidence[src][0], conflicts)
        if kept:
            clean_by_src[src] = kept

    if not clean_by_src:
        return []
    if len(clean_by_src) == 1:
        only_segs = next(iter(clean_by_src.values()))
        return [(s.t0, s.t1, s.burn / (s.t1 - s.t0), _seg_has_cap(s)) for s in only_segs]

    # Simple deterministic source rank: longer clean coverage, more resolved
    # same-source legs (finer cadence), then source name. Transition-atomic
    # cells ensure no transition leg is ever split across sources.
    active_sources = sorted(clean_by_src)
    src_rank = {
        src: (-round(sum(s.t1 - s.t0 for s in segs), 3), -len(segs), src)
        for src, segs in clean_by_src.items()
    }
    all_pts = sorted({t for segs in clean_by_src.values() for s in segs for t in (s.t0, s.t1)})
    merged_trans: List[Tuple[float, float]] = []
    for lo, hi in sorted(
        (s.t0, s.t1) for segs in clean_by_src.values() for s in segs if not s.is_plateau
    ):
        if merged_trans and lo < merged_trans[-1][1] - 1e-6:
            merged_trans[-1] = (merged_trans[-1][0], max(merged_trans[-1][1], hi))
        else:
            merged_trans.append((lo, hi))

    cuts: List[float] = []
    t_idx = 0
    for p in all_pts:
        while t_idx < len(merged_trans) and merged_trans[t_idx][1] <= p + 1e-6:
            t_idx += 1
        if t_idx < len(merged_trans) and merged_trans[t_idx][0] + 1e-6 < p < merged_trans[t_idx][1] - 1e-6:
            continue
        if not cuts or p > cuts[-1] + 1e-6:
            cuts.append(p)

    cursors = {src: 0 for src in active_sources}
    out: List[Tuple[float, float, float, bool]] = []
    for c0, c1 in zip(cuts, cuts[1:]):
        if c1 - c0 <= 1e-6:
            continue
        best_key: Optional[Tuple[float, Tuple[float, int, str]]] = None
        best_segs: List[_Seg] = []
        for src in active_sources:
            segs = clean_by_src[src]
            pos = cursors[src]
            while pos < len(segs) and segs[pos].t1 <= c0 + 1e-6:
                pos += 1
            cursors[src] = pos
            j = pos
            cell_segs: List[_Seg] = []
            covered = 0.0
            while j < len(segs) and segs[j].t0 < c1 - 1e-6:
                seg = segs[j]
                lo, hi = max(seg.t0, c0), min(seg.t1, c1)
                if hi > lo + 1e-6:
                    cell_segs.append(seg._replace(t0=lo, t1=hi) if seg.is_plateau else seg)
                    covered += hi - lo
                if seg.t1 <= c1 + 1e-6:
                    j += 1
                else:
                    break
            if not cell_segs:
                continue
            cand_key = (-round(covered, 3), src_rank[src])
            if best_key is None or cand_key < best_key:
                best_key = cand_key
                best_segs = cell_segs
        for seg in best_segs:
            out.append((seg.t0, seg.t1, seg.burn / (seg.t1 - seg.t0), _seg_has_cap(seg)))
    return out


def estimate_typical_pace(calc: "qs.GroupCalc", state: "qs.SummaryState") -> Dict[str, Any]:
    """Observed pooled burn rate of one limit over the preceding 48 hours:
    ``observed_burn / valid_hours``.

    Never mixes different window durations or scopes. ``valid_seconds`` and
    ``valid_hours`` count adequately covered pool time (at least
    ``ceil(0.80 * N)`` of current measured accounts and ``ceil(0.80 * M)`` of
    the active historical cohort — 100% for 1-4 accounts) during which at least
    one observed account still had selected-limit reserve (not all pinned at
    100% used). Requires at least ``TYPICAL_MIN_VALID_SEC`` (12 such hours)
    before emitting a rate or measured zero.
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

    win_start = now - TYPICAL_LOOKBACK_SEC
    typical_since = qs.typical_history_since(now)
    if view.truncated_at.get(calc.key, -math.inf) >= typical_since:
        return _finish({
            "state": "unavailable", "reason": "history_truncated",
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

    raw_ts = list(view.sweeps)
    raw_ok = (list(view.sweep_ok) if view.sweep_ok is not None and len(view.sweep_ok) == len(raw_ts)
              else [True] * len(raw_ts))
    failed = sorted(t for t, ok in zip(raw_ts, raw_ok) if not ok and typical_since - 1e-6 <= t <= now + 1e-6)

    # Gather runs strictly for calc.key from the common relevant interval
    # [typical_since, now] (matching HistoryStore.read Pass 1, including edge
    # continuity readings into [now - 48h, now] while ignoring older Pass-2 /
    # left-edge rows that can differ between 24h and 7d under MAX_READ_ROWS).
    # Attach all fresh recordable live readings per source (same filter as the
    # collector), falling back to calc.measured for synthetic SummaryStates
    # with empty norm.readings.
    by_sub: Dict[str, List[qs.Run]] = {}
    for (sub, series), runs in view.runs.items():
        if series == calc.key and runs:
            rel = [r for r in runs if r.last_seen >= typical_since]
            if rel:
                by_sub[sub] = rel

    pseudo_by_subject: Dict[Tuple[str, str], str] = {
        (m.harness, m.subject_id): m.pseudo
        for m in calc.slots if m.pseudo
    }
    live_by_sub_src: Dict[Tuple[str, str], List[qs.Reading]] = {}
    for reading in qs.recordable(state.norm, now):
        if reading.key != calc.key:
            continue
        sub = pseudo_by_subject.get((reading.harness, reading.subject_id))
        if not sub and view.salt:
            sub = qs.pseudo_id(view.salt, reading.harness, reading.subject_id)
        if not sub:
            continue
        live_by_sub_src.setdefault((sub, reading.source), []).append(reading)
    for m in calc.measured:
        rd = m.reading
        if not m.pseudo or rd is None or not rd.fresh:
            continue
        if rd.observed_at is None or not math.isfinite(rd.observed_at):
            continue
        if qs.ratio_of(rd.ratio)[0] is None:
            continue
        if rd.observed_at > now + qs.FUTURE_SKEW_SEC:
            continue
        if rd.resets_at is not None and (not math.isfinite(rd.resets_at) or rd.resets_at <= now):
            continue
        live_by_sub_src.setdefault((m.pseudo, rd.source), [rd])

    for (sub, src), live_readings in live_by_sub_src.items():
        live_readings.sort(key=lambda r: r.observed_at)
        existing = by_sub.get(sub, [])
        same_src = sorted(
            (r for r in existing if r.source == src),
            key=lambda r: (r.first_seen, r.first_obs, int(r.after_correction)),
        )
        other_src = [r for r in existing if r.source != src]
        for reading in live_readings:
            same_src = qs._with_live(same_src, reading, now, view)
        by_sub[sub] = other_src + same_src

    if not by_sub:
        warming = view.collecting_since is None or (now - view.collecting_since) < TYPICAL_LOOKBACK_SEC
        return _finish({
            "state": "warming_up" if warming else "insufficient", "reason": "few_hours",
            "rate_per_hour": None, "valid_seconds": 0, "valid_hours": 0.0,
            "observed_burn": 0.0, "window_hours": window_hours,
            "min_valid_hours": min_hours,
        })

    # Sweep-line over disjoint valid measurement segments and merged cohort
    # presence spans inside [win_start, now].
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
    covered_sec = 0.0
    usable_sec = 0.0
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
        covered_sec += dt
        if cap_count > 0:
            usable_sec += dt
            if pos_rates:
                total_burn += sum(pos_rates.values()) * dt

    usable_hours = usable_sec / 3600.0
    if usable_sec < TYPICAL_MIN_VALID_SEC - 1e-6:
        if covered_sec >= TYPICAL_MIN_VALID_SEC - 1e-6:
            reason = "all_observed_at_limit" if usable_sec <= 1e-6 else "few_hours"
            return _finish({
                "state": "insufficient", "reason": reason,
                "rate_per_hour": None, "valid_seconds": round(usable_sec),
                "valid_hours": round(usable_hours, 2),
                "observed_burn": round(total_burn, 6),
                "window_hours": window_hours, "min_valid_hours": min_hours,
            })
        warming = view.collecting_since is None or (now - view.collecting_since) < TYPICAL_LOOKBACK_SEC
        return _finish({
            "state": "warming_up" if warming else "insufficient", "reason": "few_hours",
            "rate_per_hour": None, "valid_seconds": round(usable_sec),
            "valid_hours": round(usable_hours, 2),
            "observed_burn": round(total_burn, 6),
            "window_hours": window_hours, "min_valid_hours": min_hours,
        })

    if total_burn <= qs.RATIO_EPS:
        return _finish({
            "state": "zero", "reason": "", "rate_per_hour": 0.0,
            "valid_seconds": round(usable_sec),
            "valid_hours": round(usable_hours, 2),
            "observed_burn": 0.0, "window_hours": window_hours,
            "min_valid_hours": min_hours,
        })

    rate_per_hour = total_burn / usable_hours
    return _finish({
        "state": "ok", "reason": "", "rate_per_hour": rate_per_hour,
        "valid_seconds": round(usable_sec),
        "valid_hours": round(usable_hours, 2),
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
    unknown: Optional[Sequence[bool]] = None,
) -> Dict[str, Any]:
    """Single pooled simulation from ``now`` at ``rate`` win/h.

    Each item is ``(remaining, reset_at, blocked_until)``: ``blocked_until`` is
    None for an account that can run now, else the moment every restriction of
    the account ends (``math.inf``: no reported end). ``unknown`` marks the
    accounts whose availability is not known now (another limit that binds
    them, or its scope, was not read): like a restricted account each keeps
    its share in the total and is neither drained nor counted as usable, but
    what is left on it is said apart (``unknown``) — a condition, never a
    confirmed restriction. Runnable accounts share the pool burn, so demand
    continues when one account hits zero. Each account refills to 1.0 at most
    once, at its next reported reset.

    The simulation runs through every known event — each reported reset, each
    reported end of a restriction — and on to the depletion of what can still
    run. The span the chart draws is no event: it only cuts the output (the
    line at ``chart_end``, by interpolation between events, and the stops
    listed for it in ``runs_out``). So the 24-hour and the 7-day charts read
    one and the same future.

    A *stop* is a stretch with no usable reserve, ``{"start", "locked",
    "unknown", "resumes", "conditional"}``: what is left at its start on
    restricted accounts and on accounts of unknown availability, and the first
    event after which usable reserve is back (None: no known event brings it
    back). Every event of one instant is applied before a stop is recorded:
    capacity back at that same instant (within SAME_INSTANT_SEC) is no stop;
    any positive break is one — a stop may start at a reset whose refill lands
    only on accounts that cannot run, and what that instant leaves is what is
    left at its start. A stop is ``conditional`` when an account of unknown
    availability holds reserve at its start or refills before its known return:
    that account may be able to run. ``stops`` lists every stop in order;
    ``first_stop`` keeps the first as ``(start, locked + unknown, resumes)``.
    Each ``runs_out`` entry says ``again`` when an earlier stop was simulated
    before it, and ``unknown`` when its stop is conditional.
    """
    unk = {i for i, flag in enumerate(unknown or ()) if flag}
    left = [max(0.0, min(1.0, float(rem))) for rem, _res, _blk in items]
    vertices: List[Tuple[float, float]] = [(now, sum(left))]
    refill_adds: Dict[int, float] = {}
    runs_out: List[Dict[str, Any]] = []
    stops: List[Dict[str, Any]] = []

    def blocked_at(i: int, at: float) -> bool:
        blk = items[i][2]
        return blk is not None and blk > at + 1e-9

    def runnable(at: float) -> List[int]:
        return [i for i in range(len(items)) if i not in unk and not blocked_at(i, at)]

    def usable(at: float) -> float:
        return sum(left[i] for i in runnable(at))

    def unknown_ready(at: float) -> float:
        return sum(left[i] for i in unk if not blocked_at(i, at))

    def stop_from(start: float) -> Dict[str, Any]:
        on_unknown = unknown_ready(start)
        return {"start": start, "locked": max(0.0, sum(left) - on_unknown), "unknown": on_unknown,
                "resumes": None, "conditional": on_unknown > qs.RATIO_EPS}

    entry_of: Dict[int, Dict[str, Any]] = {}  # id(stop) -> its runs_out entry
    usable_before: Dict[float, float] = {}

    events = sorted(
        {res for _r, res, _b in items if res is not None and res > now}
        | {blk for _r, _res, blk in items if blk is not None and math.isfinite(blk) and blk > now}
    )
    open_stop: Optional[Dict[str, Any]] = stop_from(now) if usable(now) <= qs.RATIO_EPS else None
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
        zero: Optional[Dict[str, Any]] = None
        if at > t and open_stop is None and rate > qs.RATIO_EPS:
            run = runnable(t)
            run_total = sum(left[i] for i in run)
            if run_total > qs.RATIO_EPS:
                work = rate * (at - t) / 3600.0
                if work >= run_total - qs.RATIO_EPS:
                    zero_at = min(at, t + run_total / rate * 3600.0)
                    for i in run:
                        left[i] = 0.0
                    zero = stop_from(zero_at)
                    if zero_at < at - 1e-9:
                        vertices.append((zero_at, sum(left)))
                else:
                    factor = max(0.0, 1.0 - work / run_total)
                    for i in run:
                        left[i] *= factor
        vertices.append((at, sum(left)))
        # Immediately before ``at``: accounts whose hold ends at ``at`` are
        # still blocked until the unlock happens, so their balance is not part
        # of the pre-unlock margin.
        usable_before[at] = sum(
            left[i] for i in range(len(items))
            if i not in unk and not (items[i][2] is not None and items[i][2] >= at - 1e-9)
        )

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
        if zero is not None and zero["start"] >= at - SAME_INSTANT_SEC:
            # Empty at this very event: the stop starts with what the instant leaves, a refill landing now on
            # accounts that cannot (or may not) run included.
            zero = stop_from(zero["start"])
        if zero is not None and (not back or zero["start"] < at - SAME_INSTANT_SEC):
            zero_at = zero["start"]
            again = bool(stops)
            if back:
                zero["resumes"] = at
                stops.append(zero)
            else:
                open_stop = zero
            if now + 1e-9 < zero_at <= chart_end + 1e-9:
                restricted = zero["locked"] > qs.RATIO_EPS
                ahead = any(res is not None and res > zero_at for _r, res, _b in items) or (
                    restricted and any(blk is not None and math.isfinite(blk) and blk > zero_at
                                       for i, (_r, _res, blk) in enumerate(items) if i not in unk))
                entry: Dict[str, Any] = {"at": _stop_instant(zero_at, now), "reset_reported": ahead,
                                         "after_refill": refilled_before, "again": again}
                if restricted:
                    entry["blocked_only"] = True
                if zero["conditional"]:
                    entry["unknown"] = True
                runs_out.append(entry)
                entry_of[id(zero)] = entry
        elif open_stop is not None and back:
            open_stop["resumes"] = at
            stops.append(open_stop)
            open_stop = None
        if open_stop is not None and not open_stop["conditional"] and unknown_ready(at) > qs.RATIO_EPS:
            # An account of unknown availability refills or comes off a known hold before the known return: from
            # then on it may run, so the stop is a condition. What was left at its start is not changed.
            open_stop["conditional"] = True
            open_stop["unknown_refilled"] = any(i in unk and not blocked_at(i, at) for i in resetting)
            if id(open_stop) in entry_of:
                entry_of[id(open_stop)]["unknown"] = True
                entry_of[id(open_stop)]["unknown_later"] = True
        t = at
        if final:
            break

    if open_stop is not None:
        stops.append(open_stop)
    first = stops[0] if stops else None
    return {
        "vertices": vertices, "refill_adds": refill_adds, "runs_out": runs_out, "stops": stops,
        "usable_before": usable_before,
        "first_stop": (first["start"], first["locked"] + first["unknown"], first["resumes"]) if first else None,
    }


def _stop_instant(stamp: float, now: float) -> Any:
    """Exact stop timestamp (fractional second preserved via qs._instant; a stop
    at ``now`` uses ``qs._instant(now)`` to match the line's start without
    shifting past a sub-second resume)."""
    start = qs._instant(now)
    if abs(stamp - now) <= SAME_INSTANT_SEC:
        return start
    return max(qs._instant(stamp), start)


def _format_line(vertices: Sequence[Tuple[float, float]], now: float, end: float) -> List[List[float]]:
    """Slice ``vertices`` to ``[now, end]`` keeping exact timestamps via
    ``qs._instant`` (a little reserve stays positive, see _kept)."""
    if not vertices:
        return []
    first = qs._instant(now)
    last = qs._instant(end)
    out: List[List[float]] = [[first, _kept(vertices[0][1])]]
    for idx in range(1, len(vertices)):
        t, v = vertices[idx]
        if t > end + 1e-9:
            break
        at = last if abs(t - end) <= 1e-9 else max(qs._instant(t), first)
        rv = _kept(v)
        if out[-1][0] == at and out[-1][1] == rv:
            continue
        out.append([at, rv])
    if out[-1][0] < last:
        out.append([last, _kept(eval_polyline(vertices, end, right=True))])
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


def _verdict_stops(
    stops: Sequence[Dict[str, Any]], first_reset_at: Optional[float]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """The stop a verdict speaks of and the earlier one it keeps, among the
    stops that begin by the first reported reset (every stop when none is
    reported).

    The stop still on at the first reset — it reaches the reset, or begins
    right at it because the refill lands only on accounts that cannot run —
    decides whether reserve lasts to that reset; with none on there, the
    longest (the first of equal ones). A short break before it never stands
    in for it: the first stop before the one said is kept as ``earlier``."""
    pre = [s for s in stops if first_reset_at is None or s["start"] <= first_reset_at + SAME_INSTANT_SEC]
    if not pre:
        return None, None
    reaching = [s for s in pre if first_reset_at is not None
                and (s["resumes"] is None or s["resumes"] >= first_reset_at - SAME_INSTANT_SEC)]
    if reaching:
        said = reaching[-1]
    else:
        said = max(pre, key=lambda s: (math.inf if s["resumes"] is None else s["resumes"] - s["start"],
                                       -s["start"]))
    earlier = next((s for s in pre if s["start"] < said["start"] - SAME_INSTANT_SEC), None)
    return said, earlier


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
    unknown_accounts: int = 0,
    unknown: Optional[Sequence[bool]] = None,
    other_binding_labels: Sequence[str] = (),
    sim: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the pre-first-reset verdict from ``sim`` (or an unknown-state
    verdict without fake No-new-use numbers when ``sim is None``).

    Every figure is read off the one simulation: the totals just before and
    just after the first reported reset (at that instant), and the stop said
    (_verdict_stops) — up to the first event at which a reported reset brings
    usable reserve in, so an earlier reset on a disabled or blocked account
    never hides a stop on the accounts that can run. An earlier, shorter stop
    is kept apart (``earlier_empty_at``, ``earlier_resumes_at``,
    ``earlier_gap_seconds``, ``earlier_unknown``). A conditional stop is
    ``unknown_gap``, never a confirmed outage; ``unknown_at_empty`` is only
    what unknown-availability accounts hold ready at its start."""
    current = sum(max(0.0, min(1.0, float(rem))) for rem, _res, _blk in items)
    first_reset_at, first_group = _first_reset(items, now)
    first_reset_in_horizon = bool(first_reset_at is not None and first_reset_at <= end)
    unk_set = {i for i, flag in enumerate(unknown or ()) if flag}
    usable_resets = [
        max(res, blk) if blk is not None and blk > res else res
        for i, (_rem, res, blk) in enumerate(items)
        if i not in unk_set and res is not None and res > now and (blk is None or math.isfinite(blk))
    ]
    usable_reset_at = min(usable_resets) if usable_resets else None

    before_first: Optional[float] = None
    after_first: Optional[float] = None
    first_adds: Optional[float] = None
    usable_before_first: Optional[float] = None
    usable_before_usable: Optional[float] = None
    empty_at: Optional[float] = None
    resumes_at: Optional[float] = None
    gap_seconds: Optional[int] = None
    locked: Optional[float] = None
    on_unknown: Optional[float] = None
    conditional = False
    unknown_refilled: Optional[bool] = None
    earlier: Optional[Dict[str, Any]] = None

    if sim is not None:
        vertices = sim["vertices"]
        refill_adds = sim["refill_adds"]
        usable_before = sim.get("usable_before") or {}
        if first_reset_at is not None:
            before_first = eval_polyline(vertices, first_reset_at, right=False)
            after_first = eval_polyline(vertices, first_reset_at, right=True)
            first_adds = sum(refill_adds.get(i, 0.0) for i in first_group)
            usable_before_first = usable_before.get(first_reset_at, before_first)
        if usable_reset_at is not None:
            usable_before_usable = usable_before.get(usable_reset_at)
        if kind == "typical":
            said, earlier = _verdict_stops(sim["stops"], usable_reset_at)
            if said is not None:
                empty_at, locked, on_unknown, resumes_at = said["start"], said["locked"], said["unknown"], said["resumes"]
                conditional = bool(said.get("conditional"))
                if "unknown_refilled" in said:
                    unknown_refilled = bool(said["unknown_refilled"])
                if resumes_at is not None:
                    # A stop is a positive break: never published as a zero length.
                    gap_seconds = max(1, round(resumes_at - empty_at))

    has_locked = locked is not None and locked > qs.RATIO_EPS
    has_unknown = on_unknown is not None and on_unknown > qs.RATIO_EPS
    # The stop ends at the first reported reset, and nothing is on restricted
    # accounts: "runs out before the reset" says it whole.
    ends_at_reset = (first_reset_at is not None and resumes_at is not None
                     and abs(resumes_at - first_reset_at) <= 1e-6)
    tight_first = usable_before_first is not None and usable_before_first <= VERDICT_TIGHT_RESERVE
    tight_usable = usable_before_usable is not None and usable_before_usable <= VERDICT_TIGHT_RESERVE
    tight_reset_at: Optional[float] = None
    before_tight: Optional[float] = None
    usable_tight: Optional[float] = None
    if kind == "no_new_use":
        v_state = "no_new_use" if first_reset_at is not None else "no_new_use_no_reset"
    elif status == "held":
        v_state = "held"
    elif status == "unavailable":
        v_state = "unavailable"
    elif status in ("warming_up", "insufficient"):
        v_state = "insufficient"
    elif empty_at is not None:
        if conditional or has_unknown:
            # Whether work stops depends on accounts whose availability is not known.
            v_state = "unknown_gap"
        elif has_locked:
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
        v_state = "tight" if (tight_first or tight_usable) else "lasts"
        if v_state == "tight":
            if not tight_first and usable_reset_at is not None and sim is not None:
                tight_reset_at = usable_reset_at
                before_tight = eval_polyline(sim["vertices"], usable_reset_at, right=False)
                total_at_tight, cand_usable = before_tight, usable_before_usable
            else:
                total_at_tight, cand_usable = before_first, usable_before_first
            if (cand_usable is not None and total_at_tight is not None
                    and total_at_tight - cand_usable > qs.RATIO_EPS):
                usable_tight = cand_usable
    else:
        v_state = "lasts_no_reset"

    earlier_resumes = earlier["resumes"] if earlier is not None else None
    out: Dict[str, Any] = {
        "state": v_state,
        "reason": reason,
        "first_reset_at": qs._instant(first_reset_at) if first_reset_at is not None else None,
        "first_reset_accounts": len(first_group),
        "first_reset_in_horizon": first_reset_in_horizon,
        "before_first_reset": _kept(before_first),
        "after_first_reset": _kept(after_first),
        "first_reset_adds": _kept(first_adds),
        "current": _kept(current),
        "empty_at": _stop_instant(empty_at, now) if empty_at is not None else None,
        "resumes_at": qs._instant(resumes_at) if resumes_at is not None else None,
        "gap_seconds": gap_seconds,
        "locked_at_empty": _kept(locked) if has_locked else None,
        "unknown_at_empty": _kept(on_unknown) if has_unknown else None,
        "earlier_empty_at": _stop_instant(earlier["start"], now) if earlier is not None else None,
        "earlier_resumes_at": qs._instant(earlier_resumes) if earlier_resumes is not None else None,
        "earlier_gap_seconds": (max(1, round(earlier_resumes - earlier["start"]))
                                if earlier is not None and earlier_resumes is not None else None),
        "earlier_unknown": bool(earlier is not None and earlier.get("conditional")),
        "rate_per_hour": _kept(rate),
        "valid_seconds": int(valid_seconds),
        "valid_hours": round(float(valid_hours), 2),
        "min_valid_hours": int(min_valid_hours),
        "window_hours": int(window_hours),
        "held_reason": held_reason,
        "held_accounts": int(held_accounts),
        "blocked_accounts": int(blocked_accounts),
        "unknown_accounts": int(unknown_accounts),
        "other_binding_labels": list(other_binding_labels),
    }
    if unknown_refilled is not None:
        out["unknown_refilled"] = unknown_refilled
    if tight_reset_at is not None:
        out["tight_reset_at"] = qs._instant(tight_reset_at)
        out["before_tight_reset"] = _kept(before_tight)
    if usable_tight is not None:
        out["usable_before_reset"] = _kept(usable_tight)
    return out


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
    unknown: Optional[Sequence[bool]] = None,
    held_reason: str = "",
    other_binding_labels: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build the 48-hour Typical scenario from ONE pooled simulation.

    ``blocked_until`` gives each member's restriction end (see simulate_pool):
    another spent binding limit, or the account's own known state
    (Member.hold_until). ``unknown`` marks members whose availability is not
    known (simulate_pool): counted apart (``unknown_accounts``), never as
    restricted. When Typical is unknown (``warming_up``, ``insufficient``,
    ``unavailable``, or ``held``: no member can run, by a known hold or an
    unknown availability), emits ``line = []``, ``checkpoints = []``,
    ``schedule = []`` and no numeric ``before_first_reset``/``after_first_reset``
    so unknown Typical never masquerades as No new use; ``reason`` carries the
    estimator's own word (``all_observed_at_limit``: every account sat at its
    limit, so its use could not be seen).
    """
    blocks = list(blocked_until) if blocked_until is not None else [None] * len(members)
    unknowns = [bool(flag) for flag in unknown] if unknown is not None else [False] * len(members)
    items: List[Tuple[float, Optional[float], Optional[float]]] = [
        (m.remaining, m.reading.resets_at if m.reading is not None else None, blk)
        for m, blk in zip(members, blocks)
    ]
    unknown_count = sum(1 for flag in unknowns if flag)
    perm_held = sum(1 for (_rem, _res, blk), flag in zip(items, unknowns)
                    if not flag and blk is not None and not math.isfinite(blk))
    temp_blocked = sum(1 for (_rem, _res, blk), flag in zip(items, unknowns)
                       if not flag and blk is not None and math.isfinite(blk) and blk > now)
    runnable_count = len(items) - perm_held - unknown_count

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
        "unknown_accounts": unknown_count,
        "later": later_out,
    }
    verdict_args = dict(
        kind="typical", status=status, rate=raw_rate,
        valid_seconds=valid_seconds, valid_hours=valid_hours,
        min_valid_hours=min_hours, window_hours=window_hours, reason=reason,
        held_reason=held_reason, held_accounts=perm_held,
        blocked_accounts=temp_blocked, unknown_accounts=unknown_count,
        unknown=unknowns,
        other_binding_labels=other_binding_labels,
    )

    if status not in ("ok", "zero"):
        return dict(common, **{
            "line": [], "at_pace": 0, "held": len(members), "zero_growth": 0,
            "held_reason": held_reason if status == "held" else "",
            "schedule": [], "checkpoints": [], "runs_out": [],
            "verdict": _make_verdict(items, now, end, sim=None, **verdict_args),
        })

    eff_rate = float(raw_rate) if (status == "ok" and raw_rate is not None) else 0.0
    sim = simulate_pool(items, now, end, eff_rate, unknown=unknowns)
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
            "adds": _kept(sum(refill_adds.get(idx_by_id[id(m)], 0.0) for m in group)),
            "total_after": _kept(eval_polyline(vertices, last, right=True)),
        })

    checkpoints = []
    for offset in qs.CHECKPOINTS.get(horizon, qs.CHECKPOINTS["24h"]):
        moment = round(now + offset)
        if moment <= end:
            checkpoints.append({
                "after_seconds": round(offset),
                "at": moment,
                "value": _kept(eval_polyline(vertices, moment, right=True)),
            })

    active_drain = status == "ok"
    return dict(common, **{
        "line": _format_line(vertices, now, end),
        "at_pace": runnable_count if active_drain else 0,
        "held": perm_held + unknown_count if active_drain else len(members),
        "zero_growth": len(members) if status == "zero" else 0,
        "held_reason": "",
        "schedule": schedule,
        "checkpoints": checkpoints,
        "runs_out": sim["runs_out"],
        "verdict": _make_verdict(items, now, end, sim=sim, **verdict_args),
    })
