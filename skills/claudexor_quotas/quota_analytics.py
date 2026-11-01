"""Same-account joint view of a family's windows, over the skill's own history.

One calculation for the widget, the model tool and the context snapshot: it
reads the same :class:`quota_summary.SummaryState` the reserve is built from
(accounts, windows, local history) and answers, per family and request scope:

* ``joint`` — the same-account intersection in units of the shortest window
  ("5-hour-window equivalents"): per account ``min(r_U, min_w r_w * k_w)`` with
  ``k_w`` the retrospective ratio of consumption of the unit window to ``w``
  over the account's *common watched period* (the stretches where every
  binding window of that account was observed in one cycle, unbroken). Its
  recorded history, the figure now, conditional hours at the measured pace,
  and the two conditional futures (no new use; at the measured pace, work
  frozen while any binding window is exhausted).
* ``window`` — the same two futures for the selected window alone, but with
  that window's account held to the other binding windows too, so nothing is
  drained from a window while no work can run.

The algebra is quota_joint; this module only reads history into it. Nothing
here is stored, written or sent; it never refreshes a provider.

Honesty rules this module keeps: an account with an unknown window, no
history to calibrate on, or too little common watched time is *unknown*
(``not_calibrated``), counted apart and left out of the sum — never a
positive guess; the figure says how many accounts it covers; the history
carries the last resolved value of each window dated (never a new
observation) and a gap where none ever resolved; only each window's next
reported reset is applied, once.
"""

from __future__ import annotations

import bisect
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:  # the host loads the skill as a package: siblings are relative imports
    from . import quota_joint as qj
    from . import quota_scope
    from . import quota_summary as qs
except ImportError:  # imported directly from the skill directory (tests)
    import quota_joint as qj  # type: ignore[no-redef]
    import quota_scope  # type: ignore[no-redef]
    import quota_summary as qs  # type: ignore[no-redef]

CAL_LOOKBACK_SEC = 7 * 86400.0
# An account is calibrated only on a period it was watched through in every
# binding window for at least this long, and only where each window moved at
# least MIN_DELTA of a share: smaller movements are rounding, not a ratio.
MIN_COMMON_SEC = 6 * 3600.0
MIN_DELTA = 0.02
UNIT_NOTE = "shares of the shortest window (its length names the unit)"


# ---------------------------------------------------------------------------
# Which windows bind, which history is needed


def binding_groups(state: "qs.SummaryState", harness: str, model: Optional[str],
                   kind: str) -> Tuple[List["qs.GroupCalc"], int]:
    """The family's windows that govern a request ``(model, kind)`` by the
    engine's own rule (quota_scope.applies), shortest first, and how many
    windows' scope could not be read (never counted as vendor-wide)."""
    out: List[qs.GroupCalc] = []
    unreadable = 0
    for calc in state.groups:
        if calc.harness != harness or not calc.window:
            continue
        if calc.scope_info is None:
            # Old series retain their scope token, not the lost model list.
            # A legacy model digest without a new-field suffix proves just
            # one thing: it never governed an unspecified-model request.
            # Keep it for display; named requests and new tokens stay unknown.
            legacy_model_digest = (len(calc.scope) == 10
                                   and all(c in "0123456789abcdef" for c in calc.scope))
            if model is None and legacy_model_digest:
                continue
            unreadable += 1
            continue
        verdict = quota_scope.applies(calc.scope_info, model, kind)
        if verdict is None:
            unreadable += 1
        elif verdict:
            out.append(calc)
    out.sort(key=lambda c: (c.window or 0, c.key))
    return out, unreadable


def extend_requests(wanted: Dict[Tuple[str, str], float], groups: Sequence["qs.GroupCalc"],
                    chart: Optional["qs.GroupCalc"], salt: str, now: float,
                    model: Optional[str] = None, kind: str = "text") -> None:
    """Add, in place, the history reads the joint view needs: every binding
    window of every account of the chart's family, the calibration lookback
    behind ``now``. Nothing else is asked, and no read is widened for others."""
    if not salt or chart is None:
        return
    since = now - CAL_LOOKBACK_SEC - 2 * qs.SIGHTING_GAP_SEC
    for calc in groups:
        if calc.harness != chart.harness or calc.scope_info is None or not calc.window:
            continue
        if quota_scope.applies(calc.scope_info, model, kind) is not True and calc.key != chart.key:
            continue
        for member in calc.slots:
            pair = (qs.pseudo_id(salt, member.harness, member.subject_id), calc.key)
            wanted[pair] = min(wanted.get(pair, since), since)


# ---------------------------------------------------------------------------
# Reading history into per-window states


def _state_at(cover: "qs.Cover", at: float) -> Optional[Tuple[float, Optional[float], "qs.Run"]]:
    """(share used, reported reset) one window was vouched for at ``at``, by
    the reserve's own source policy; None where it was not vouched for."""
    active = qs._active(cover, at, True)
    if not active:
        return None
    if (len({run.ratio for run in active}) > 1
            and any(run.first_obs != run.last_obs and at < run.last_seen for run in active)):
        return None
    chosen, _reason = qs._resolve_sources([
        (run.ratio, run.resets_at, run.first_obs if at < run.last_seen else run.last_obs, run.source, run)
        for run in active])
    if chosen is None or chosen.first_obs > at:
        return None
    return chosen.ratio, chosen.resets_at, chosen


def calibration_curve(covers: Dict[str, "qs.Cover"], unit: str, start: float, now: float) -> Dict[str, Any]:
    """One account's common watched period over ``[start, now]`` and, on it,
    how much each window moved: ``delta`` (share used), ``rate`` (per second;
    None when the period is too short to say) and ``k`` (unit-window shares
    per share of the window; None unless both moved at least MIN_DELTA).

    A stretch counts only when *every* window was vouched for at both ends of
    consecutive moments, in one reset cycle each, with no drop in any ratio:
    a reset, a gap or a restore is cut out of all windows alike, so the
    ratios describe one workload of one account over one period."""
    keys = sorted(covers)
    times: Set[float] = {start, now}
    for key in keys:
        for _starts, spans in covers[key]:
            for run, end in spans:
                for at in (run.first_seen, run.last_seen, end, run.resets_at):
                    if at is not None and start <= at <= now:
                        times.add(at)
    ordered = sorted(times)
    # One state per elementary interval, judged in its interior: a window is
    # watched over an interval only if the collector vouched for it there, so
    # a gap is never bridged by the sightings on either side of it.
    mids = [(ordered[i] + ordered[i + 1]) / 2 for i in range(len(ordered) - 1)]
    states = {key: [_state_at(covers[key], at) for at in mids] for key in keys}
    endpoints = {key: [_state_at(covers[key], at) for at in ordered[1:]] for key in keys}
    common = 0.0
    delta = {key: 0.0 for key in keys}
    curve = {"times": [start], "common": [0.0], "delta": [{key: 0.0 for key in keys}], "unit": unit}
    for i in range(len(mids)):
        step: Dict[str, float] = {}
        if all(states[key][i] is not None for key in keys):
            common += ordered[i + 1] - ordered[i]
            for key in keys:
                a, b = states[key][i], endpoints[key][i]
                if (b is None or not qs.same_cycle(a[1], b[1]) or b[0] < a[0] - 1e-9
                        or a[2].source != b[2].source or a[2].plan != b[2].plan
                        or (a[2].first_seen != b[2].first_seen
                            and (b[2].after_gap or b[2].after_correction))):
                    step = {}
                    break
                step[key] = b[0] - a[0]
        for key, value in step.items():
            delta[key] += value
        # Prefixes make history causal without re-scanning all runs for every
        # displayed point: a ratio is available only after the common period
        # and consumption supporting it were observed, never borrowed from now.
        curve["times"].append(ordered[i + 1])
        curve["common"].append(common)
        curve["delta"].append(dict(delta))
    return curve


def calibration_at(curve: Dict[str, Any], at: float) -> Dict[str, Any]:
    index = max(0, bisect.bisect_right(curve["times"], at) - 1)
    common, delta, unit = curve["common"][index], curve["delta"][index], curve["unit"]
    keys = sorted(delta)
    rate = {key: (delta[key] / common if common >= MIN_COMMON_SEC else None) for key in keys}
    k: Dict[str, Optional[float]] = {}
    for key in keys:
        if key == unit:
            k[key] = 1.0
        elif delta[key] >= MIN_DELTA and delta[unit] >= MIN_DELTA and common >= MIN_COMMON_SEC:
            k[key] = delta[unit] / delta[key]
        else:
            k[key] = None
    return {"common_seconds": common, "delta": delta, "rate": rate, "k": k}


def calibrate(covers: Dict[str, "qs.Cover"], unit: str, start: float, now: float) -> Dict[str, Any]:
    return calibration_at(calibration_curve(covers, unit, start, now), now)


# ---------------------------------------------------------------------------
# One account's inputs now


class _Account:
    """One account's binding windows: members, covers, calibration, inputs."""

    def __init__(self, subject: Tuple[str, str], pseudo: str):
        self.subject = subject
        self.pseudo = pseudo
        self.members: Dict[str, Optional["qs.Member"]] = {}
        self.covers: Dict[str, "qs.Cover"] = {}
        self.calib: Optional[Dict[str, Any]] = None
        self.calibration_curve: Optional[Dict[str, Any]] = None
        self.unit = ""

    def wins(self, keys: Sequence[str], pace_rates: bool = True, *, recent: bool = False) -> List[qj.Win]:
        out: List[qj.Win] = []
        for key in keys:
            m = self.members.get(key)
            current = m is not None and m.status == "measured" and m.reading is not None
            calib = self.calib or {}
            rate = (calib.get("rate") or {}).get(key)
            if recent or len(keys) == 1:
                value = m.rate.get("windows_per_hour") if m is not None else None
                rate = value / qj.HOUR if value is not None else None
            out.append(qj.Win(
                key=key, remaining=m.remaining if current else None,
                reset=m.reading.resets_at if current else None,
                rate=rate if pace_rates else None,
                k=(calib.get("k") or {}).get(key), unit=(key == self.unit)))
        return out


def _accounts(state: "qs.SummaryState", keys: Sequence[str], groups: Dict[str, "qs.GroupCalc"],
              unit: str, now: float) -> List[_Account]:
    view = state.view
    seen: Dict[Tuple[str, str], _Account] = {}
    for key in keys:
        for member in groups[key].slots:
            subject = (member.harness, member.subject_id)
            acct = seen.setdefault(subject, _Account(subject, member.pseudo))
            acct.members[key] = member
    out = sorted(seen.values(), key=lambda a: a.subject)
    start = now - CAL_LOOKBACK_SEC
    for acct in out:
        acct.unit = unit
        for key in keys:
            member = acct.members.get(key)
            runs = qs._history_runs(view, member, key) if member is not None and member.pseudo else []
            acct.covers[key] = qs._member_cover(runs, now, view) if runs else []
        have = {key: acct.covers[key] for key in keys if acct.covers[key]}
        if len(keys) > 1 and len(have) == len(keys) and unit in have:
            # A still-cached endpoint is not another observation of zero use.
            # Do not lengthen its measured period merely because time passed
            # before the next collector sighting/source observation arrived.
            observed_end = min(max(run.last_obs for _starts, spans in have[key]
                                   for run, _end in spans) for key in keys)
            acct.calibration_curve = calibration_curve(have, unit, start, max(start, min(now, observed_end)))
            acct.calib = calibration_at(acct.calibration_curve, now)
    return out


def _reason_of(acct: _Account, keys: Sequence[str]) -> str:
    """Why this account has no joint value (``""`` when it has one now)."""
    wins = acct.wins(keys)
    joint = qj.joint_value(wins)
    if joint.value is not None:
        return ""
    for key in keys:
        if acct.members.get(key) is None:
            return "window_missing"
    if joint.reason == "not_calibrated" and acct.calib is None:
        return "no_history"
    return joint.reason


# ---------------------------------------------------------------------------
# History line (display only: recorded + carried last known, dated)


def _fact_timeline(acct: _Account, key: str, times: Sequence[float], now: float
                   ) -> List[Optional[Dict[str, Any]]]:
    """The window's resolved fact at each moment — the last resolved value,
    carried when the window is not vouched for just then — as
    ``_last_known_history`` resolves one account, so the two histories carry
    the same value at the same moment."""
    cover = acct.covers.get(key) or []
    out: List[Optional[Dict[str, Any]]] = []
    fact: Optional[Dict[str, Any]] = None
    for at in times:
        available = []
        for starts, spans in cover:
            index = bisect.bisect_right(starts, at) - 1
            if index >= 0:
                available.append(spans[index][0])
        active = qs._active(cover, at, True)
        candidates = active or available
        running = [r for r in candidates if r.resets_at is None or r.resets_at > at]
        candidates = running or candidates
        chosen, _reason = qs._resolve_sources([
            (r.ratio, r.resets_at, r.first_obs if at < r.last_seen else r.last_obs, r.source, r)
            for r in candidates])
        if active and qs._member_ratio(cover, at, True) is None:
            chosen = None
        measured = False
        if chosen is not None:
            observed = chosen.first_obs if at < chosen.last_seen else chosen.last_obs
            if fact is None or observed >= fact["observed"]:
                fact = {"ratio": chosen.ratio, "observed": observed, "reset": chosen.resets_at,
                        "source": chosen.source, "origin": "history"}
            measured = (fact["source"] == chosen.source and fact["ratio"] == chosen.ratio
                        and fact["observed"] == observed and qs._member_ratio(cover, at) == fact["ratio"])
        out.append(dict(fact, measured=measured) if fact is not None else None)
    return out


def _joint_history(accounts: Sequence[_Account], keys: Sequence[str], start: float, now: float
                   ) -> Dict[str, Any]:
    cal = list(accounts)
    cal = [a for a in cal if all(a.members.get(k) is not None for k in keys)]
    changes: Set[float] = {start, now}
    for acct in cal:
        for key in keys:
            for _starts, spans in acct.covers.get(key, []):
                for run, end in spans:
                    for at in (run.first_seen, run.last_seen, end, run.resets_at):
                        if at is not None and start <= at <= now:
                            changes.add(at)
    times = sorted(changes)
    timeline = {(a.subject, k): _fact_timeline(a, k, times, now) for a in cal for k in keys}
    line: List[List[Any]] = []
    details: List[Dict[str, Any]] = []
    previous: Set[Tuple[str, str]] = set()
    max_accounts = 0
    for i, at in enumerate(times):
        members: List[Tuple[_Account, List[Dict[str, Any]], float]] = []
        for acct in cal:
            facts = [timeline[(acct.subject, k)][i] for k in keys]
            if at == now:  # now: the figure the reserve counts, else the dated last known
                facts = []
                for k in keys:
                    m = acct.members[k]
                    if m is not None and m.status == "measured" and m.reading is not None:
                        facts.append({"ratio": m.reading.ratio, "observed": m.reading.observed_at or now,
                                      "reset": m.reading.resets_at, "source": m.reading.source,
                                      "origin": "payload", "measured": True})
                    elif m is not None and m.last_known is not None:
                        lk = m.last_known
                        facts.append({"ratio": lk.ratio, "observed": lk.observed_at, "reset": lk.resets_at,
                                      "source": lk.source, "origin": lk.origin, "measured": False})
                    else:
                        facts.append(None)
            if any(f is None for f in facts):
                continue
            past_calib = (calibration_at(acct.calibration_curve, at)
                          if acct.calibration_curve is not None else None)
            wins = [qj.Win(k, 1.0 - f["ratio"], k=(past_calib["k"].get(k) if past_calib else None),
                           unit=(k == acct.unit)) for k, f in zip(keys, facts)]
            value = qj.joint_value(wins).value
            if value is None:
                continue
            members.append((acct, facts, value))
        ids = {(a.subject) for a, _f, _v in members}
        carried = [(a, f) for a, fs, _v in members for f in fs if not f["measured"]]
        oldest = min((f["observed"] for _a, f in carried), default=None)
        total = round(sum(v for _a, _f, v in members), 4) if members else None
        added, lost = ids - previous, previous - ids
        changed = bool(added or lost) and bool(line)
        detail = {
            "at": qs._instant(at), "value": total, "accounts": len(members),
            "measured": sum(1 for _a, fs, _v in members if all(f["measured"] for f in fs)),
            "carried": len({a.subject for a, _f in carried}), "unknown": 0,
            "oldest_observed_at": qs.iso(oldest),
            "age_seconds": max(0, round(at - oldest)) if oldest is not None else None,
            "origins": dict(sorted(Counter(f["origin"] for _a, f in carried).items())),
            "sources": sorted({f["source"] for _a, f in carried}),
            "reset_passed": sum(1 for _a, f in carried if f["reset"] is not None and f["reset"] <= at),
            "reasons": {}, "change": ("membership_changed" if lost else "first_recorded") if changed else None,
            "added": len(added) if line else 0, "removed": len(lost) if line else 0,
            "removal_time_unknown": False,
        }
        if changed and line and line[-1][1] is not None:
            line.append([qs._instant(at), None])  # a break, never a consumption step
            details.append(dict(detail, value=None, change=None))
        comparable = {k: v for k, v in detail.items() if k not in ("at", "age_seconds")}
        last = {k: v for k, v in details[-1].items() if k not in ("at", "age_seconds")} if details else None
        if comparable != last or at in (start, now):
            line.append([qs._instant(at), total])
            details.append(detail)
        max_accounts = max(max_accounts, len(members))
        previous = ids
    clipped = None
    if len(line) > qs.MAX_PAST_VERTICES:
        line, details = line[-qs.MAX_PAST_VERTICES:], details[-qs.MAX_PAST_VERTICES:]
        clipped = line[0][0]
    return {"line": line, "details": details, "max_accounts": max_accounts, "clipped_before": clipped,
            "table": [], "membership_note":
            "Accounts enter at their first recorded value; an account joins the combined figure only once "
            "every binding window of it has a recorded value and its ratio is calibrated."}


# ---------------------------------------------------------------------------
# Scenarios from trajectories


def _scenario(trajs: Sequence[Tuple[qj.Trajectory, List[qj.Win]]], held: int, joint: bool,
              now: float, end: float, horizon: str) -> Dict[str, Any]:
    """One conditional future in the shape the widget already draws: its
    line, its schedule of reported resets (what each gives back in this
    scenario), checkpoints and — at pace — when accounts run out."""
    line = qj.total_line(trajs, now, end, joint)
    def values(t, right):
        return [qj.contribution(tr, w, t, right, joint) for tr, w in trajs]

    def total(t, right):
        known = [v for v in values(t, right) if v is not None]
        return sum(known) if known else None

    def rounded(value):
        return round(value, 4) if value is not None else None
    jumps = sorted(((j.t, i) for i, (tr, _w) in enumerate(trajs) for j in tr.jumps), key=lambda p: p[0])
    schedule: List[Dict[str, Any]] = []
    events: List[List[Tuple[float, int]]] = []
    for at, i in jumps:
        if events and at - events[-1][0][0] <= qs.RESET_TOLERANCE_SEC:
            events[-1].append((at, i))
        else:
            events.append([(at, i)])
    for group in events:
        first, last = group[0][0], group[-1][0]
        who = {i for _t, i in group}
        before = [qj.contribution(trajs[i][0], trajs[i][1], first, False, joint) for i in who]
        after = [qj.contribution(trajs[i][0], trajs[i][1], last, True, joint) for i in who]
        adds = None if any(v is None for v in before + after) else sum(after) - sum(before)
        schedule.append({"at": qs._instant(first), "last": qs._instant(last), "accounts": len(who),
                         "adds": rounded(adds), "total_after": rounded(total(last, True)),
                         "known_after": sum(v is not None for v in values(last, True)),
                         "unknown_after": sum(v is None for v in values(last, True))})
    later_times = sorted(w.reset for _tr, ws in trajs for w in ws if w.reset is not None and w.reset > end)
    later = None
    if later_times:
        first = later_times[0]
        near = [t for t in later_times if t - first <= qs.RESET_TOLERANCE_SEC]
        later = {"at": qs._instant(first), "accounts": len(near), "more_events": 0,
                 "more_accounts": len(later_times) - len(near)}
    checkpoints = []
    for offset in qs.CHECKPOINTS.get(horizon, qs.CHECKPOINTS["24h"]):
        moment = round(now + offset)
        if moment <= end:
            checkpoints.append({"after_seconds": round(offset), "at": moment,
                                "value": rounded(total(moment, True)),
                                "known": sum(v is not None for v in values(moment, True)),
                                "unknown": sum(v is None for v in values(moment, True))})
    runs_out = []
    for tr, ws in trajs:
        for at, key in tr.stops:
            refilled = any(j.key == key and j.t <= at for j in tr.jumps)
            reset_ahead = any(j.key == key and j.t > at for j in tr.jumps)
            runs_out.append({"at": round(at), "reset_reported": reset_ahead, "after_refill": refilled})
    runs_out.sort(key=lambda row: row["at"])
    return {
        "line": [[qs._instant(t), rounded(v)] for t, v in line], "accounts": len(trajs),
        "coverage": [{"at": qs._instant(t), "known": sum(v is not None for v in values(t, True)),
                      "unknown": sum(v is None for v in values(t, True))} for t in sorted({t for t, _v in line})],
        "at_pace": sum(1 for tr, _w in trajs if tr.paced), "held": held + sum(1 for tr, _w in trajs if not tr.paced),
        "zero_growth": 0,
        "no_reset": sum(1 for _tr, ws in trajs if not any(w.reset is not None for w in ws)),
        "schedule": schedule,
        "later": later,
        "checkpoints": checkpoints, "runs_out": runs_out,
    }


def _futures(accounts: Sequence[Tuple[_Account, List[qj.Win]]], joint: bool, now: float, end: float,
             horizon: str, held_extra: int = 0) -> Optional[Dict[str, Any]]:
    out: Dict[str, Any] = {}
    for name, pace in (("no_new_use", False), ("recent_pace", True)):
        trajs = []
        for _acct, wins in accounts:
            traj = qj.simulate(wins, now, end, pace)
            if traj.segments and (not joint or qj.joint_value(wins).value is not None):
                trajs.append((traj, wins))
        if not trajs:
            return None
        out[name] = _scenario(trajs, held_extra, joint, now, end, horizon)
    return out


# ---------------------------------------------------------------------------
# The whole view


def _hours_now(accounts: Sequence[_Account], keys: Sequence[str]) -> Dict[str, Any]:
    total, known, unknown = 0.0, 0, 0
    for acct in accounts:
        hours = qj.work_hours(acct.wins(keys))
        if hours.value is None:
            unknown += 1
        else:
            known += 1
            total += hours.value
    return {"value": total if known else None, "known": known, "unknown": unknown,
            "pace_basis": "common_watched_period", "pace_lookback_seconds": CAL_LOOKBACK_SEC,
            "note": "account-hours at each account's own measured pace, if that pace holds — "
                    "never the duration of a task"}


def future_table(scenarios: Optional[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """Samples of precisely the displayed futures, not the legacy chart.
    Right sides at resets; a missing value stays missing, never interpolated
    through a gap. History samples remain in the view's own history."""
    if not scenarios:
        return []
    times = {now}
    for sc in scenarios.values():
        times.update(c["at"] for c in sc["checkpoints"])
        times.update(c["last"] for c in sc["schedule"])
    rows = []
    for at in sorted(times):
        row = {"at": qs.iso_exact(at), "event": "now" if at == now else ""}
        for name, sc in scenarios.items():
            line = sc["line"]
            index = bisect.bisect_right([t for t, _v in line], at) - 1
            value = line[index][1] if index >= 0 else None
            if index >= 0 and index + 1 < len(line) and at > line[index][0]:
                t0, v0 = line[index]
                t1, v1 = line[index + 1]
                value = None if v0 is None or v1 is None else v0 + (v1 - v0) * (at - t0) / (t1 - t0)
            row["scenario_" + name] = value
        rows.append(row)
    return rows


def build(state: "qs.SummaryState", chart: Optional["qs.GroupCalc"], horizon: str = "24h", *,
          model: Optional[str] = None, kind: str = "text", echo: Optional[Dict[str, Any]] = None,
          light: bool = False) -> Optional[Dict[str, Any]]:
    """The family's analytics for the selected window's family (see module).
    ``light`` computes only the figures now (the model tool and the context
    snapshot): no history line, no futures."""
    if chart is None:
        return None
    now = state.now
    span = qs.HORIZONS.get(horizon, qs.HORIZONS["24h"])
    start, end = now - span, float(round(now + span))
    wins, unreadable = binding_groups(state, chart.harness, model, kind)
    by_key = {g.key: g for g in wins}
    scope = {"model": model, "request_kind": kind, "basis": "quota_block",
             "echo": bool(echo and echo.get("stated")), "unreadable_windows": unreadable,
             "note": ("windows that govern " + ("an unspecified-model request" if model is None else f"model {model}")
                      + f", {kind} requests (the engine's own applicability rule)")}
    out: Dict[str, Any] = {"version": 1, "scope": scope, "unit_note": UNIT_NOTE, "window": None, "joint": None}
    keys = [g.key for g in wins]
    # --- the selected window alone, coupled to its account's binding windows
    out["window"] = None if light else _window_view(state, chart, wins, by_key, now, end, horizon,
                                                  scope_unknown=bool(unreadable))
    if len(keys) < 2:
        out["joint"] = {"available": False, "reason": "scope_unknown" if unreadable else "one_window" if keys else "no_window",
                        "note": "only one window governs this scope: the window's own graph is the "
                                "whole story" if keys and not unreadable else
                                "some window applicability is unknown" if unreadable else
                                "no window with a known length governs this scope"}
        return out
    unit = keys[0]
    accounts = _accounts(state, keys, by_key, unit, now)
    reasons = Counter()
    joinable: List[Tuple[_Account, List[qj.Win]]] = []
    for acct in accounts:
        why = _reason_of(acct, keys)
        if why == "no_history" and state.view is not None and state.view.truncated:
            why = "history_truncated"
        if unreadable and qj.joint_value(acct.wins(keys)).value != 0:
            why = "scope_unknown"
        if why:
            reasons[why] += 1
        if not why:
            joinable.append((acct, acct.wins(keys)))
    calibrated = [a for a in accounts if a.calib is not None]
    hist = None if light else _joint_history(accounts, keys, start, now)
    futures = None if light else _futures(joinable, True, now, end, horizon, 0)
    figure = round(sum(qj.joint_value(w).value for _a, w in joinable), 4) if joinable else None
    periods = [a.calib["common_seconds"] for a in calibrated]
    out["joint"] = {
        "available": True, "unit": {"key": unit, "window_seconds": by_key[unit].window,
                                    "label": by_key[unit].label},
        "windows": [{"key": g.key, "label": g.label, "window_seconds": g.window,
                     "scoped": bool(g.scope_info and g.scope_info.scoped)} for g in wins],
        "accounts": len(accounts), "known": len(joinable), "unknown_reasons": dict(sorted(reasons.items())),
        "figure": figure,
        "hours": _hours_now([a for a, _w in joinable], keys),
        "history": hist, "scenarios": futures, "table": future_table(futures, now),
        "y_max": float(max(1, len(joinable), hist["max_accounts"] if hist else 0)),
        "calibration": {"lookback_seconds": CAL_LOOKBACK_SEC, "min_common_seconds": MIN_COMMON_SEC,
                        "min_delta": MIN_DELTA, "calibrated": len(calibrated),
                        "common_seconds_min": min(periods) if periods else None,
                        "common_seconds_max": max(periods) if periods else None,
                        "history_truncated": bool(state.view is not None and state.view.truncated)},
        "limitations": [
            "A combined figure is a retrospective ratio of consumption per account (how much of the shortest "
            "window one share of the other window cost), not compute, tokens or hours of work.",
            "Hours are conditional on the measured pace holding; a future line is a condition, not a forecast.",
            "Accounts with an unknown window, no history to calibrate on or too little common watched time are "
            "left out and counted apart — never counted as positive.",
        ],
    }
    return out


def _window_view(state: "qs.SummaryState", chart: "qs.GroupCalc", wins: Sequence["qs.GroupCalc"],
                 by_key: Dict[str, "qs.GroupCalc"], now: float, end: float, horizon: str,
                 scope_unknown: bool = False) -> Optional[Dict[str, Any]]:
    """The selected window's own conditional futures. Each of its current
    accounts is held to the family's other binding windows as well (and
    calibrated with them), so the window is never drained while the account
    could not run."""
    members = chart.measured
    if not members:
        return None
    # A selected per-window chart remains visible for inspection even when a
    # model/kind query does not bind it. Such work cannot consume that window.
    scope_applies = chart.key in by_key
    keys = [chart.key] + [g.key for g in wins if g.key != chart.key]
    groups = dict(by_key)
    groups[chart.key] = chart
    unit_key = min(keys, key=lambda key: (groups[key].window or 0, key))
    accounts = [a for a in _accounts(state, keys, groups, unit_key, now)
                if (a.subject in {(m.harness, m.subject_id) for m in members})]
    pairs: List[Tuple[_Account, List[qj.Win]]] = []
    held = 0
    for acct in accounts:
        w = acct.wins(keys, recent=True)
        if scope_unknown or not scope_applies or any(x.remaining is None for x in w):
            # Another binding window is not known now: the account's future
            # cannot be judged against it, so it is held at its own share of
            # this window (no pace, nothing drained).
            own = w[0]
            w = [qj.Win(own.key, own.remaining, own.reset, None, own.k, own.unit)]
            held += 1
        pairs.append((acct, w))
    futures: Dict[str, Any] = {}
    for name, pace in (("no_new_use", False), ("recent_pace", True)):
        trajs = [(qj.simulate(w, now, end, pace), w) for _a, w in pairs]
        trajs = [(t, w) for t, w in trajs if t.segments]
        if not trajs:
            return None
        sc = _scenario(trajs, 0, False, now, end, horizon)
        futures[name] = sc
    return {"key": chart.key, "scenarios": futures, "table": future_table(futures, now),
            "held_to_other_windows": held,
            "pace_basis": "trailing_hour", "pace_lookback_seconds": qs.RATE_WINDOW_SEC,
            "scope_applies": None if scope_unknown else scope_applies,
            "accounts": len(pairs),
            "note": "the window's accounts at its own share; a pace future stops draining every window of "
                    "an account while any of its binding windows is exhausted"}
