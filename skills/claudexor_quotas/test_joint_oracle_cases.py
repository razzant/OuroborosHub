"""The parent's independent rational reference cases (conditional same-account
capacity), replayed against quota_joint. The expected values come from an
exact-Fraction reference that shares no code with this skill; they are
conditional-work scenarios, not provider evidence.

(remaining, rate per hour, one-off reported resets as (hour, window), the
moment read in hours, expected remaining, expected joint in unit shares)
"""

from fractions import Fraction as F

import pytest

import quota_joint as qj

H = 3600.0
CASES = [
    ("tight_week_now", ["0.6", "0.1"], ["0.2", "0.05"], [], 0, ["3/5", "1/10"], "2/5"),
    ("short_reset_not_week", ["0.6", "0.1"], ["0.2", "0.05"], [[1, 0]], 1, ["1", "1/20"], "1/5"),
    ("week_reset_after_short", ["0.6", "0.1"], ["0.2", "0.05"], [[1, 0], [2, 1]], 2, ["4/5", "1"], "4/5"),
    ("blocked_does_not_burn", ["0.6", "0"], ["0.2", "0.05"], [], 1, ["3/5", "0"], "0"),
    ("short_reset_does_not_unlock", ["0.6", "0"], ["0.2", "0.05"], [[1, 0], [2, 1]], 1, ["1", "0"], "0"),
    ("week_unlocks_preserved_short", ["0.6", "0"], ["0.2", "0.05"], [[1, 0], [2, 1]], 2, ["1", "1"], "1"),
    ("post_unlock_burn", ["0.6", "0"], ["0.2", "0.05"], [[1, 0], [2, 1]], 3, ["4/5", "19/20"], "4/5"),
    ("no_fabricated_recurrence", ["0.6", "0"], ["0.2", "0.05"], [[1, 0], [2, 1]], 10, ["0", "3/4"], "0"),
    ("zero_rate_exhaustion_binds", ["0.6", "0"], ["0.2", "0"], [], 0, ["3/5", "0"], "0"),
    ("positive_zero_rate_unqualified", ["0.6", "0.1"], ["0.2", "0"], [], 0, ["3/5", "1/10"], None),
    ("fractional_event_exact", ["0.25", "0.5"], ["0.5", "0.2"], [[0.5, 0]], "0.5", ["1", "2/5"], "1"),
]


@pytest.mark.parametrize("name,remaining,rates,resets,at,expected_rem,expected_joint", CASES)
def test_reference_cases(name, remaining, rates, resets, at, expected_rem, expected_joint):
    keys = ["short", "week"]
    at_s = float(F(str(at))) * H
    resets_by = {i: float(F(str(t))) * H for t, i in resets}
    rate_ps = [float(F(r)) / H for r in rates]
    base = [qj.Win(keys[i], float(F(remaining[i])), reset=resets_by.get(i), rate=rate_ps[i], unit=(i == 0))
            for i in range(2)]
    # k = unit rate / the window's rate where both are observed and positive; else not calibrated
    wins = [qj.Win(w.key, w.remaining, w.reset, w.rate, (1.0 if i == 0 else (rate_ps[0] / rate_ps[i] if rate_ps[i] > 0 and rate_ps[0] > 0 else None)), w.unit)
            for i, w in enumerate(base)]
    traj = qj.simulate(wins, 0.0, max(at_s, 1.0), pace=True)
    got = [traj.value(w.key, at_s, right=True) for w in wins]
    for value, text in zip(got, expected_rem):
        assert value == pytest.approx(float(F(text)), abs=1e-6), (name, got, expected_rem)
    now_wins = [qj.Win(w.key, v, None, w.rate, w.k, w.unit) for w, v in zip(wins, got)]
    joint = qj.joint_value(now_wins).value
    if expected_joint is None:
        assert joint is None  # a positive window with no measured rate is not infinity
    else:
        assert joint == pytest.approx(float(F(expected_joint)), abs=1e-6), (name, joint, expected_joint)


def test_pool_first_is_not_the_algorithm():
    # two accounts, each bound by one of its windows: capacities add AFTER the per-account min
    a = qj.joint_value([qj.Win("short", 1.0, unit=True), qj.Win("week", 0.0, k=4.0)]).value
    b = qj.joint_value([qj.Win("short", 0.0, unit=True), qj.Win("week", 1.0, k=4.0)]).value
    assert a + b == 0.0  # pooling the windows first (min(1, 1*4) = 1) would have said 1
