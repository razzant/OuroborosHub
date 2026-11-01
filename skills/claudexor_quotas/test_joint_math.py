"""The algebra of same-account joint capacity, on numbers alone.

Independent of the widget, the history and the route: every case here is
derivable by hand, and the conditional futures are cross-checked against a
brute-force time-stepping simulator that shares no code with quota_joint.
"""

import math

import pytest

import quota_joint as qj

H = 3600.0


def approx(value):
    return pytest.approx(value, abs=1e-6)


# --- joint_value ------------------------------------------------------------


def test_the_analytic_case_short_unit_capacity():
    # short has .6 left; weekly has .1 left and weekly use runs at 0.05/h while
    # short use runs at 0.2/h: one weekly share is worth .2/.05 = 4 short shares.
    short = qj.Win("5h", 0.6, unit=True, rate=0.2 / H)
    week = qj.Win("7d", 0.1, k=0.2 / 0.05, rate=0.05 / H)
    joint = qj.joint_value([short, week])
    assert joint.value == approx(0.4)
    assert joint.binding == "7d"
    # the same answer as durations: min(.6/.2, .1/.05) = 2 h, times .2/h = .4
    hours = qj.work_hours([short, week])
    assert hours.value == approx(2.0)
    assert hours.value * 0.2 == approx(joint.value)


def test_a_short_reset_alone_does_not_unlock_more_than_the_weekly_allows():
    short = qj.Win("5h", 1.0, unit=True)  # just reset to full
    week = qj.Win("7d", 0.1, k=4.0)
    assert qj.joint_value([short, week]).value == approx(0.4)


def test_known_exhaustion_is_zero_even_beside_unknowns():
    week = qj.Win("7d", 0.0, k=None)  # exhausted, never calibrated
    short = qj.Win("5h", None, unit=True)  # share not known
    joint = qj.joint_value([short, week])
    assert joint.value == 0.0 and joint.reason == "exhausted" and joint.binding == "7d"
    # and a zero rate does not turn it into something else
    assert qj.work_hours([qj.Win("5h", 0.5, rate=0.0), qj.Win("7d", 0.0, rate=0.0)]).value == 0.0


def test_unknown_or_uncalibrated_is_never_positive():
    short = qj.Win("5h", 0.6, unit=True)
    missing = qj.joint_value([short, qj.Win("7d", None, k=4.0)])
    assert missing.value is None and missing.reason == "window_unknown"
    uncalibrated = qj.joint_value([short, qj.Win("7d", 0.9, k=None)])
    assert uncalibrated.value is None and uncalibrated.reason == "not_calibrated"
    assert qj.joint_value([]).value is None


def test_one_window_is_its_own_share_and_needs_no_k():
    assert qj.joint_value([qj.Win("5h", 0.37)]).value == approx(0.37)


def test_three_windows_take_the_smallest_unit_value_never_a_min_of_unlike_percents():
    # 5h .5 left; weekly .3 left worth 2 units each (.6); model weekly .9 left worth .5 each (.45)
    wins = [qj.Win("5h", 0.5, unit=True), qj.Win("7d", 0.3, k=2.0), qj.Win("7d-m", 0.9, k=0.5)]
    joint = qj.joint_value(wins)
    assert joint.value == approx(0.45) and joint.binding == "7d-m"
    # a plain min of the raw shares would have said .3 — it is not what is computed
    assert min(w.remaining for w in wins) == approx(0.3)


def test_work_hours_unbounded_and_unknown_pace_are_not_numbers():
    only_idle = [qj.Win("5h", 0.5, rate=0.0), qj.Win("7d", 0.5, rate=0.0)]
    assert qj.work_hours(only_idle).value is None
    assert qj.work_hours(only_idle).reason == "no_use_observed"
    assert qj.work_hours([qj.Win("5h", 0.5, rate=None)]).reason == "pace_unknown"
    # a window being used at nothing does not bind; the used one does
    mix = [qj.Win("5h", 0.5, rate=0.0), qj.Win("7d", 0.2, rate=0.1 / H)]
    assert qj.work_hours(mix).value == approx(2.0)


# --- the conditional future ---------------------------------------------------


def brute_force(wins, now, end, pace, dt=1.0):
    """Time-stepping reference: shares move by rate*dt while every window has
    share, refill once at their reset. Shares no code with quota_joint."""
    rem = {w.key: w.remaining for w in wins}
    rate = {w.key: w.rate or 0.0 for w in wins}
    resets = {w.key: w.reset for w in wins if w.reset is not None}
    done = set()
    out = {}
    t = now
    can = pace and all(w.rate is not None for w in wins)
    while t <= end + 1e-9:
        for key, at in resets.items():
            if key not in done and at <= t + 1e-9 and at > now:
                rem[key] = 1.0
                done.add(key)
        out[round(t, 6)] = dict(rem)
        if can and all(v > 1e-9 for v in rem.values()):
            for key in rem:
                rem[key] = max(0.0, rem[key] - rate[key] * dt)
        t += dt
    return out


def check(wins, now, end, pace, tol=2e-3):
    traj = qj.simulate(wins, now, end, pace)
    dt = max(1.0, (end - now) / 4000.0)
    ref = brute_force(wins, now, end, pace, dt=dt)
    resets = [w.reset for w in wins if w.reset is not None]
    for t, state in ref.items():
        if t >= end or any(abs(t - at) <= 2 * dt for at in resets):
            continue  # a jump instant is asserted exactly by the tests, not stepped
        for key, value in state.items():
            got = traj.value(key, t, right=True)
            assert got == pytest.approx(value, abs=tol), (key, t, got, value)
    return traj


def test_no_new_use_refills_each_window_once_at_its_own_reset():
    now, end = 0.0, 48 * H
    wins = [qj.Win("5h", 0.2, reset=2 * H, unit=True), qj.Win("7d", 0.5, reset=30 * H, k=1.0)]
    traj = check(wins, now, end, pace=False)
    assert traj.value("5h", 1 * H, False) == approx(0.2)
    assert traj.value("5h", 2 * H, False) == approx(0.2)  # just before the reset
    assert traj.value("5h", 2 * H, True) == approx(1.0)  # just after
    assert traj.value("5h", 40 * H, True) == approx(1.0)  # no second reset is invented
    assert traj.value("7d", 29 * H, True) == approx(0.5)
    assert traj.value("7d", 30 * H, True) == approx(1.0)
    assert not traj.paced


def test_pace_freezes_every_window_while_one_is_exhausted_then_resumes_after_its_reset():
    # weekly is already 0, short has .6; short would burn at .2/h if it ran.
    now, end = 0.0, 24 * H
    wins = [qj.Win("5h", 0.6, reset=3 * H, rate=0.2 / H, unit=True),
            qj.Win("7d", 0.0, reset=10 * H, rate=0.05 / H, k=4.0)]
    traj = check(wins, now, end, pace=True)
    # nothing can run, so the otherwise-available short window must not drain
    assert traj.value("5h", 2 * H, True) == approx(0.6)
    # the short reset (to full) still happens, and work still cannot run
    assert traj.value("5h", 3 * H, True) == approx(1.0)
    assert traj.value("5h", 9 * H, True) == approx(1.0)
    # the weekly reset gives share back: work resumes from there at pace
    assert traj.value("7d", 10 * H, True) == approx(1.0)
    assert traj.value("5h", 10 * H + 1 * H, True) == approx(0.8)
    assert traj.value("7d", 10 * H + 1 * H, True) == approx(0.95)


def test_short_reset_before_weekly_with_short_above_half():
    # the falsifier: short > .5 and its reset comes before the weekly reset
    now, end = 0.0, 36 * H
    wins = [qj.Win("5h", 0.7, reset=1 * H, rate=0.1 / H, unit=True),
            qj.Win("7d", 0.04, reset=20 * H, rate=0.02 / H, k=5.0)]
    traj = check(wins, now, end, pace=True)
    # weekly runs out after 2 h (.04 / .02); the short window, having started at .7, stops at .6 + refill
    assert traj.value("7d", 2 * H, False) == approx(0.0)
    # short: -0.1 in the first hour, refilled to 1.0, then -0.1 again till weekly runs out at 2 h
    assert traj.value("5h", 2 * H, False) == approx(0.9)
    # frozen until the weekly reset at 20 h; short stays at .9 meanwhile
    assert traj.value("5h", 19 * H, True) == approx(0.9)
    assert traj.stops and traj.stops[0][1] == "7d"


def test_unknown_pace_holds_the_account_instead_of_draining_it():
    wins = [qj.Win("5h", 0.6, reset=3 * H, rate=0.2 / H, unit=True),
            qj.Win("7d", 0.5, rate=None, k=2.0)]
    traj = qj.simulate(wins, 0.0, 24 * H, pace=True)
    assert not traj.paced
    assert traj.value("5h", 2 * H, True) == approx(0.6)


def test_a_window_with_unknown_share_has_no_trajectory():
    traj = qj.simulate([qj.Win("5h", None, unit=True), qj.Win("7d", 0.5, k=1.0)], 0.0, H, True)
    assert traj.segments == []
    assert qj.contribution(traj, [qj.Win("5h", None)], 0.0) is None


def test_resets_at_the_horizon_are_applied_once_and_beyond_it_never():
    wins = [qj.Win("5h", 0.3, reset=24 * H, unit=True), qj.Win("7d", 0.4, reset=24 * H + 1, k=1.0)]
    traj = qj.simulate(wins, 0.0, 24 * H, pace=False)
    assert traj.value("5h", 24 * H, False) == approx(0.3)
    assert traj.value("5h", 24 * H, True) == approx(1.0)
    assert traj.value("7d", 24 * H, True) == approx(0.4)  # one second past the horizon


def test_joint_line_keeps_the_binding_window_under_a_short_reset():
    # joint (no new use) of two accounts; one short reset must not lift the total past the weekly bound
    wins_a = [qj.Win("5h", 0.2, reset=2 * H, unit=True), qj.Win("7d", 0.1, k=4.0)]
    wins_b = [qj.Win("5h", 0.9, unit=True), qj.Win("7d", 0.9, k=4.0)]
    trajs = [(qj.simulate(w, 0.0, 6 * H, False), w) for w in (wins_a, wins_b)]
    line = qj.total_line(trajs, 0.0, 6 * H, joint=True)
    start, after = line[0][1], [v for t, v in line if t > 2 * H][0]
    assert start == approx(0.2 + 0.9)  # min(.2, .4) + min(.9, 3.6)
    assert after == approx(0.4 + 0.9)  # reset lifts a's short to 1, but weekly .1*4 = .4 binds


def test_pace_joint_declines_at_the_unit_rate_when_k_matches_the_rates():
    # with k = rho_unit / rho_w the joint capacity falls at rho_unit while work runs
    unit = qj.Win("5h", 0.8, rate=0.2 / H, unit=True)
    week = qj.Win("7d", 0.3, rate=0.05 / H, k=4.0)
    traj = qj.simulate([unit, week], 0.0, 4 * H, pace=True)
    assert qj.contribution(traj, [unit, week], 0.0) == approx(min(0.8, 1.2))
    assert qj.contribution(traj, [unit, week], 1 * H) == approx(0.6)
    assert qj.contribution(traj, [unit, week], 3 * H) == approx(0.2)
    # weekly runs out at 6 h and short at 4 h: stays within the horizon, no stop yet
    assert not traj.stops


def test_total_line_matches_a_brute_force_sum():
    now, end = 0.0, 30 * H
    accounts = [
        [qj.Win("5h", 0.6, reset=4 * H, rate=0.1 / H, unit=True), qj.Win("7d", 0.3, reset=26 * H, rate=0.03 / H, k=3.3333333)],
        [qj.Win("5h", 0.25, reset=1 * H, rate=0.15 / H, unit=True), qj.Win("7d", 0.7, rate=0.03 / H, k=5.0)],
    ]
    trajs = [(qj.simulate(w, now, end, True), w) for w in accounts]
    line = qj.total_line(trajs, now, end, joint=True)
    refs = [brute_force(w, now, end, True, dt=30.0) for w in accounts]
    for t in [x for x in refs[0] if 0 < x < end and abs(x % 3600) > 31 and abs(x % 3600) < 3569]:
        expect = sum(min(state[w.key] * (1.0 if w.unit else w.k) for w in wins)
                     for state, wins in ((r[t], w) for r, w in zip(refs, accounts)))
        got = _interp(line, t)
        assert got == pytest.approx(expect, abs=2e-3), (t, got, expect)


def _interp(line, t):
    for (t0, v0), (t1, v1) in zip(line, line[1:]):
        if t0 <= t <= t1 and t1 > t0:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return None
