"""Same-account joint capacity and its conditional futures — pure algebra.

Standard library only, no I/O, no import of the rest of the skill: every rule
here is testable on numbers alone, and the widget, the model tool and the
context snapshot all read the same answers.

One account is bound by several windows at once (a 5-hour window and a
weekly window both limit the same work). Three questions have an exact
answer on the account's own numbers and nothing else:

``joint_value`` — *the same-account intersection*, in units of the account's
shortest window (its ``unit`` window: "5-hour-window equivalents").  A window
``w`` with share ``r_w`` left is worth ``r_w * k_w`` unit-window shares, where
``k_w = (unit-window share used) / (w share used)`` over a period when both
were observed on the same account (``quota_analytics`` calibrates it
retrospectively; here it is an input).  The account's capacity is
``min(r_U, min_w r_w * k_w)``.  A unit is never taken from unlike percents:
no ``k``, no value. ``k`` is a retrospective ratio of consumption, not a
compute or throughput figure.

``work_hours`` — *conditional account-hours*: at the account's own measured
pace ``rho_w`` (share used per hour, same period), the hours until its first
binding window runs out, ``min_w r_w / rho_w``. It is a conditional on the
pace staying what it was, never a task duration.

``simulate`` — the account's conditional future: *no new use* (nothing
consumed; each window refilled once at its own next reported reset) or *at
pace* (work runs at the measured pace while every binding window has share,
and stops, for every window at once, as soon as one is exhausted; it resumes
when a reset gives that window share back). Only the next reported reset of
each window is applied, once; no later reset is assumed. An account whose
pace is not known for every binding window is held (no new use): nothing is
drained from a window while no work can run.

Order of facts, exactly: a known exhausted window gives ``0`` whatever else is
unknown (nothing can run) — a zero beats a missing ``k`` or a missing
reading; otherwise any unknown window share or missing ``k`` leaves the value
unknown, never positive; ``rate == 0`` is an observed zero (the window is not
binding at the observed pace) while ``rate is None`` is not observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

EPS = 1e-9
HOUR = 3600.0


@dataclass(frozen=True)
class Win:
    """One binding window of one account at one moment."""

    key: str
    # Share left in [0, 1]; None = not known now (stale, missing, unreadable).
    remaining: Optional[float]
    # The next reported reset (epoch seconds), or None when none is reported.
    reset: Optional[float] = None
    # Share of this window used per SECOND while work runs; None = not observed.
    rate: Optional[float] = None
    # Unit-window shares per share of this window; None = not calibrated.
    # The unit window itself is 1.0.
    k: Optional[float] = None
    unit: bool = False


@dataclass(frozen=True)
class Joint:
    value: Optional[float]
    reason: str  # "" | exhausted | window_unknown | not_calibrated | no_window
    binding: str = ""  # key of the window that binds (or is unknown / uncalibrated)


def joint_value(wins: Sequence[Win]) -> Joint:
    """The account's capacity in unit-window shares, or why it is unknown."""
    if not wins:
        return Joint(None, "no_window")
    spent = [w for w in wins if w.remaining is not None and w.remaining <= EPS]
    if spent:
        return Joint(0.0, "exhausted", spent[0].key)
    unknown = [w for w in wins if w.remaining is None]
    if unknown:
        return Joint(None, "window_unknown", unknown[0].key)
    best: Optional[Tuple[float, str]] = None
    for w in wins:
        k = 1.0 if (w.unit or len(wins) == 1) else w.k
        if k is None:
            return Joint(None, "not_calibrated", w.key)
        value = float(w.remaining) * k  # type: ignore[arg-type]
        if best is None or value < best[0]:
            best = (value, w.key)
    assert best is not None
    return Joint(best[0], "", best[1])


def work_hours(wins: Sequence[Win]) -> Joint:
    """Hours of work at the measured pace until the first binding window runs
    out: ``min r_w / rho_w`` over windows that are being used. ``None`` when
    a share or a pace is not known, and when nothing is being used at all
    (no limit is reached at the observed pace)."""
    if not wins:
        return Joint(None, "no_window")
    spent = [w for w in wins if w.remaining is not None and w.remaining <= EPS]
    if spent:
        return Joint(0.0, "exhausted", spent[0].key)
    for w in wins:
        if w.remaining is None:
            return Joint(None, "window_unknown", w.key)
    for w in wins:
        if w.rate is None:
            return Joint(None, "pace_unknown", w.key)
    used = [(float(w.remaining) / w.rate / HOUR, w.key) for w in wins if w.rate > EPS]  # type: ignore[arg-type,operator]
    if not used:
        return Joint(None, "no_use_observed")
    best = min(used)
    return Joint(best[0], "", best[1])


# ---------------------------------------------------------------------------
# The account's conditional future


@dataclass
class Segment:
    """One stretch of an account's future: every window moves linearly from
    ``start`` at ``t0`` with ``slope`` (share per second) to ``t1``."""

    t0: float
    t1: float
    start: Dict[str, float]
    slope: Dict[str, float]


@dataclass
class Jump:
    """A reported reset applied once: window ``key`` goes from ``left`` to
    ``right`` (a full share) at ``t``."""

    t: float
    key: str
    left: float
    right: float


@dataclass
class Trajectory:
    segments: List[Segment] = field(default_factory=list)
    jumps: List[Jump] = field(default_factory=list)
    paced: bool = False  # work was allowed to run (pace known for every window)
    # The moments the account could not run (a binding window at zero) after
    # having been able to, in order: (t, key of the exhausted window).
    stops: List[Tuple[float, str]] = field(default_factory=list)

    def value(self, key: str, t: float, right: bool = True) -> Optional[float]:
        """Share left of window ``key`` at ``t``; ``right`` asks for the
        value just after anything that happens exactly at ``t`` (a refill)."""
        segs = self.segments
        if not segs:
            return None
        lo, hi = 0, len(segs) - 1
        found = 0
        while lo <= hi:  # last segment starting at or before t (right) / before t (left)
            mid = (lo + hi) // 2
            start = segs[mid].t0
            if start < t or (right and start <= t):
                found, lo = mid, mid + 1
            else:
                hi = mid - 1
        seg = segs[found]
        if key not in seg.start:
            return None
        at = min(max(t, seg.t0), seg.t1)
        return max(0.0, min(1.0, seg.start[key] + seg.slope.get(key, 0.0) * (at - seg.t0)))


def simulate(wins: Sequence[Win], now: float, end: float, pace: bool) -> Trajectory:
    """The account's windows from ``now`` to ``end`` — see the module note.

    ``pace`` False is *no new use*. ``pace`` True lets work run at the
    windows' own ``rate`` while every window has share left, but only when
    every window's share and rate are known and at least one window is
    actually being used; otherwise the account is held and the result is the
    no-new-use one (``paced`` False says so)."""
    traj = Trajectory()
    if not wins or any(w.remaining is None for w in wins):
        return traj
    rem: Dict[str, float] = {w.key: max(0.0, min(1.0, float(w.remaining))) for w in wins}  # type: ignore[arg-type]
    rate: Dict[str, float] = {w.key: (w.rate if w.rate is not None and w.rate > 0 else 0.0) for w in wins}
    traj.paced = bool(pace and all(w.rate is not None for w in wins) and any(rate[k] > EPS for k in rate))
    pending = sorted((w.reset, w.key) for w in wins
                     if w.reset is not None and now < w.reset <= end)
    t = float(now)
    guard = 8 * len(wins) + 16
    was_working = False
    while guard > 0:
        guard -= 1
        working = traj.paced and all(rem[k] > EPS for k in rem)
        if was_working and not working:
            blocked = sorted(k for k in rem if rem[k] <= EPS)
            traj.stops.append((t, blocked[0] if blocked else ""))
        was_working = working
        slope = {k: (-rate[k] if working else 0.0) for k in rem}
        candidates = [end]
        if pending:
            candidates.append(pending[0][0])
        if working:
            candidates.extend(t + rem[k] / rate[k] for k in rem if rate[k] > EPS)
        t_next = max(t, min(candidates))
        traj.segments.append(Segment(t, t_next, dict(rem), slope))
        for k in rem:
            rem[k] = max(0.0, rem[k] + slope[k] * (t_next - t))
            if rem[k] <= 1e-7:
                rem[k] = 0.0
        t = t_next
        while pending and pending[0][0] <= t + 1e-6:
            moment, k = pending.pop(0)
            traj.jumps.append(Jump(moment, k, rem[k], 1.0))
            rem[k] = 1.0
        if t >= end - 1e-9 and not pending:
            break
    traj.segments.append(Segment(end, end, dict(rem), {k: 0.0 for k in rem}))
    return traj


def contribution(traj: Trajectory, wins: Sequence[Win], t: float, right: bool = True,
                 joint: bool = True) -> Optional[float]:
    """What one account adds to a total at ``t``: its joint capacity (joint)
    or the share of its one window that is the first of ``wins`` (not joint).
    None when the trajectory does not exist (a window share is unknown)."""
    if not traj.segments:
        return None
    vals = {w.key: traj.value(w.key, t, right) for w in wins}
    if any(v is None for v in vals.values()):
        return None
    if not joint:
        return vals[wins[0].key]
    # The same precedence as now: exhaustion is a known zero even without
    # calibration, but once it refills the missing k becomes unknown again.
    return joint_value([Win(w.key, vals[w.key], k=w.k, unit=w.unit) for w in wins]).value


def kinks(traj: Trajectory, wins: Sequence[Win]) -> List[float]:
    """The times inside a trajectory where its joint capacity changes slope
    without a segment boundary: crossings of two windows' unit values."""
    out: List[float] = []
    if len(wins) < 2:
        return out
    ks = {w.key: (1.0 if (w.unit or len(wins) == 1) else w.k) for w in wins}
    if any(k is None for k in ks.values()):
        return out
    for seg in traj.segments:
        if seg.t1 - seg.t0 <= 1e-9:
            continue
        keys = [w.key for w in wins]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                va, vb = seg.start[a] * ks[a], seg.start[b] * ks[b]  # type: ignore[operator]
                sa, sb = seg.slope[a] * ks[a], seg.slope[b] * ks[b]  # type: ignore[operator]
                if abs(sa - sb) <= 1e-15:
                    continue
                cross = seg.t0 + (vb - va) / (sa - sb)
                if seg.t0 + 1e-9 < cross < seg.t1 - 1e-9:
                    out.append(cross)
    return out


def total_line(trajs: Sequence[Tuple[Trajectory, Sequence[Win]]], now: float, end: float,
               joint: bool, extra: Iterable[float] = ()) -> List[List[Optional[float]]]:
    """The sum of every account's contribution from ``now`` to ``end`` as a
    piecewise-linear polyline: at each break a left and, where it differs, a
    right vertex. Accounts without a trajectory add nothing and are not in
    the sum (the caller says how many there are)."""
    times = {float(now), float(end)}
    for traj, wins in trajs:
        for seg in traj.segments:
            times.update((seg.t0, seg.t1))
        for jump in traj.jumps:
            times.add(jump.t)
        times.update(kinks(traj, wins))
    times.update(t for t in extra if now <= t <= end)
    out: List[List[Optional[float]]] = []

    def total(t: float, right: bool) -> Optional[float]:
        known = [v for v in (contribution(traj, wins, t, right, joint) for traj, wins in trajs)
                 if v is not None]
        return sum(known) if known else None

    for t in sorted(x for x in times if now - 1e-9 <= x <= end + 1e-9):
        if abs(t - now) <= 1e-9:
            out.append([t, total(t, True)])  # the figure at now, after anything exactly at now
            continue
        left, right = total(t, False), total(t, True)
        out.append([t, left])
        if (right is None) != (left is None) or (right is not None and left is not None
                                               and abs(right - left) > 1e-9):
            out.append([t, right])
    return out
