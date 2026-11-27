"""The joint view over a real history store: calibration, figure, futures.

Synthetic, hand-computable accounts driven through the real collector path
(persist_sweep) and the real reserve_view; the expected numbers are derived
on paper in each test. Nothing here measures a real account.
"""

import pytest

import plugin
import quota_analytics as qa
import quota_history
import quota_joint as qj
import quota_summary as qs
from test_reserve import FIVE_H, NOW, WEEK, constraint, payload, profile, snap

H = 3600.0
START = NOW - 8 * H
STEP = 120.0


def account_snap(sid, t, short_used, week_used, *, short_reset, week_reset=NOW + 6 * 86400):
    cons = []
    if short_used is not None:
        cons.append(constraint("five_hour", short_used, FIVE_H, reset=short_reset - NOW))
    if week_used is not None:
        cons.append(constraint("seven_day", week_used, WEEK, reset=week_reset - NOW))
    return snap("claude", sid, cons, observed_abs=t)


def at_time(t):
    """The payload of one sweep at ``t``: a works (both windows rise), b has
    no weekly window reported at all, c is exhausted weekly."""
    hours = (t - START) / H
    rows = [
        account_snap("a", t, 0.05 * hours, 0.82 + 0.01 * hours, short_reset=NOW + 1 * H),
        account_snap("b", t, 0.3, None, short_reset=NOW + 2 * H),
        account_snap("c", t, 0.1, 1.0, short_reset=NOW + 3 * H),
    ]
    return payload(rows, [profile("claude", s) for s in "abc"], harnesses=("claude",))


def fed_store(tmp_path, until=NOW, gap=None):
    store = quota_history.HistoryStore(tmp_path)
    t = START
    while t <= until + 1e-6:
        if gap is None or not (gap[0] <= t <= gap[1]):
            plugin.persist_sweep(store, at_time(t), "", t)
        else:  # the collector was not running: no sweep at all
            pass
        t += STEP
    return store


def joint_of(store, **extra):
    now_payload = at_time(NOW)
    result = plugin.reserve_view(store, now_payload, NOW, NOW, harness="claude", horizon="24h",
                                 analytics=True, **extra)
    return result, result["chart"]["analytics"]


def test_joint_figure_hours_and_who_is_left_out(tmp_path):
    store = fed_store(tmp_path)
    _result, analytics = joint_of(store)
    joint = analytics["joint"]
    assert joint["available"] and joint["unit"]["window_seconds"] == FIVE_H
    # a: short .6 left, weekly .10 left; the weekly moved .08 while the short moved .4 (k = 5):
    #    min(.6, .10 * 5) = .5. c: weekly exhausted -> 0 whatever else. b: no weekly reading -> unknown.
    assert joint["figure"] == pytest.approx(0.5, abs=1e-3)
    assert (joint["known"], joint["accounts"]) == (2, 3)
    assert joint["unknown_reasons"] == {"window_missing": 1}
    # hours at each account's own pace: a: min(.6/.05, .10/.01) = 10 h; c: exhausted = 0
    assert joint["hours"]["value"] == pytest.approx(10.0, abs=0.05)
    assert joint["hours"]["known"] == 2
    cal = joint["calibration"]
    assert cal["calibrated"] == 1 or cal["calibrated"] == 2  # a (and c, whose history exists)
    assert cal["common_seconds_min"] >= qa.MIN_COMMON_SEC


def test_the_recorded_joint_history_ends_at_the_same_figure(tmp_path):
    store = fed_store(tmp_path)
    _result, analytics = joint_of(store)
    line = analytics["joint"]["history"]["line"]
    assert line[-1][0] == round(NOW) and line[-1][1] == pytest.approx(0.5, abs=1e-3)
    values = [v for _t, v in line if v is not None]
    assert max(values) <= analytics["joint"]["y_max"]
    # At first sighting only c's observed exhaustion is a known zero. A cannot
    # borrow its later 8h calibration; it enters only after 6h common evidence.
    first = [v for t, v in line if v is not None][0]
    assert first == 0
    positive = [(t, v) for t, v in line if v is not None and v > 0]
    assert positive and positive[0][0] >= START + 6 * H


def test_no_new_use_future_keeps_the_weekly_bound_across_a_short_reset(tmp_path):
    store = fed_store(tmp_path)
    _result, analytics = joint_of(store)
    future = analytics["joint"]["scenarios"]["no_new_use"]
    start = future["line"][0][1]
    # a's short window resets (to full) in an hour, but the weekly still bounds it at .5
    after = [v for t, v in future["line"] if t > NOW + 1.5 * H][0]
    assert start == pytest.approx(0.5, abs=1e-3) and after == pytest.approx(0.5, abs=1e-3)
    assert future["schedule"] and future["schedule"][0]["adds"] == pytest.approx(0.0, abs=1e-6)


def test_pace_future_falls_at_the_unit_pace_then_freezes_with_the_binding_window_exhausted(tmp_path):
    store = fed_store(tmp_path)
    _result, analytics = joint_of(store)
    pace = analytics["joint"]["scenarios"]["recent_pace"]
    line = {round(t): v for t, v in pace["line"]}
    # weekly (.10 left at .01/h) runs out in 10 h; the joint capacity falls .05/h until then
    assert pace["line"][0][1] == pytest.approx(0.5, abs=1e-3)
    ten_hours = min(line, key=lambda t: abs(t - (NOW + 10 * H)))
    assert line[ten_hours] == pytest.approx(0.0, abs=2e-2)
    # nothing is drained afterwards: no work can run while the weekly window is exhausted
    assert pace["line"][-1][1] == pytest.approx(0.0, abs=1e-3)
    assert pace["at_pace"] >= 1 and pace["runs_out"]


def test_window_view_pace_does_not_drain_a_window_while_the_account_cannot_run(tmp_path):
    store = fed_store(tmp_path)
    _result, analytics = joint_of(store, group="claude|five_hour|18000|-")
    window = analytics["window"]
    assert window is not None and window["key"] == "claude|five_hour|18000|-"
    pace = window["scenarios"]["recent_pace"]
    values = [v for _t, v in pace["line"]]
    # c (weekly exhausted) holds its short share; b (unknown weekly pace) is held; only a ever drains
    assert pace["held"] >= 1
    assert min(values) >= 0.0 and values[-1] > 0.0


def test_one_window_family_has_no_joint_and_says_why(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    rows = [snap("claude", "a", [constraint("five_hour", 0.2, FIVE_H, reset=3600)], observed_abs=NOW)]
    data = payload(rows, [profile("claude", "a")], harnesses=("claude",))
    result = plugin.reserve_view(store, data, NOW, NOW, harness="claude", analytics=True)
    assert result["chart"]["analytics"]["joint"]["available"] is False
    assert result["chart"]["analytics"]["joint"]["reason"] == "one_window"


def test_no_history_means_unknown_never_positive(tmp_path):
    store = quota_history.HistoryStore(tmp_path)  # empty: nothing to calibrate on
    result, analytics = joint_of(store)
    joint = analytics["joint"]
    # a has two windows but no history -> not calibrated; c is exhausted -> a known zero
    assert joint["figure"] == pytest.approx(0.0, abs=1e-9)
    assert joint["known"] == 1
    assert joint["unknown_reasons"].get("no_history") == 1
    assert joint["history"]["line"] is not None  # drawn from what exists, never invented


def test_a_gap_cuts_the_calibration_period_and_is_not_bridged(tmp_path):
    # the collector was down for 5 of the 8 hours: the common watched period is too short to calibrate
    store = fed_store(tmp_path, gap=(START + 2 * H, START + 7 * H))
    _result, analytics = joint_of(store)
    joint = analytics["joint"]
    # a was watched together in both windows for only ~3 of the 8 hours (2 h before the gap, 1 h after):
    # below the 6 h minimum, so its ratio is not calibrated and it is left out — not bridged, not guessed.
    # c is still a known zero (exhausted weekly), b still lacks its weekly window.
    assert joint["unknown_reasons"] == {"window_missing": 1, "not_calibrated": 1}
    assert (joint["known"], joint["accounts"]) == (1, 3)
    assert joint["figure"] == pytest.approx(0.0, abs=1e-9)
    assert joint["calibration"]["common_seconds_max"] < qa.MIN_COMMON_SEC


def test_prefix_scoped_windows_stay_out_of_the_default_joint(tmp_path):
    store = fed_store(tmp_path)
    rows = at_time(NOW)["quota"]
    rows[0]["constraints"].append({"id": "gemini-weekly", "label": "Gemini weekly", "used_ratio": 0.5,
                                   "window_seconds": WEEK, "resets_at": qs.iso(NOW + 86400),
                                   "applies_to_models": [], "applies_to_model_prefixes": ["gemini-"]})
    data = dict(at_time(NOW), quota=rows)
    result = plugin.reserve_view(store, data, NOW, NOW, harness="claude", analytics=True)
    joint = result["chart"]["analytics"]["joint"]
    assert [w["label"] for w in joint["windows"]] == ["five_hour", "seven_day"]
    gemini = next(g for g in result["summary"]["groups"] if "gemini" in g["meaning"])
    assert gemini["model_scope"] == "prefixes" and gemini["scope"]["prefixes"]["names"] == ["gemini-"]
    # asked for a gemini model, the prefixed window binds, and the unscoped ones still do
    scoped = plugin.reserve_view(store, data, NOW, NOW, harness="claude", analytics=True, model="gemini-3")
    labels = [w["label"] for w in scoped["chart"]["analytics"]["joint"]["windows"]]
    assert "Gemini weekly" in labels and "five_hour" in labels


def test_combined_later_counts_distinct_accounts_not_windows():
    """In Combined (_scenario with joint=True), one account with two windows resetting
    simultaneously at +48h (past the 24h span) counts 1 account in later, matching in-span
    schedule counting by account identity rather than number of windows."""
    end_24h = NOW + 24 * H
    end_7d = NOW + 7 * 24 * H
    r48 = NOW + 48 * H
    w_short = qj.Win("claude|five_hour|18000|-", 0.5, r48, None, 1.0, "claude|five_hour|18000|-")
    w_week = qj.Win("claude|seven_day|604800|-", 0.6, r48, None, 1.0, "claude|five_hour|18000|-")
    tr_24 = qj.simulate([w_short, w_week], NOW, end_24h, False)
    tr_7d = qj.simulate([w_short, w_week], NOW, end_7d, False)

    sc_24 = qa._scenario([(tr_24, [w_short, w_week])], 0, True, NOW, end_24h, "24h")
    sc_7d = qa._scenario([(tr_7d, [w_short, w_week])], 0, True, NOW, end_7d, "7d")
    assert sc_7d["schedule"][0]["accounts"] == 1
    assert sc_24["later"] == {"at": round(r48), "accounts": 1, "more_events": 0, "more_accounts": 0}, sc_24["later"]

    # Two accounts each with two windows resetting simultaneously at +48h -> 2 accounts, 0 more.
    sc_two = qa._scenario([(tr_24, [w_short, w_week]), (tr_24, [w_short, w_week])], 0, True, NOW, end_24h, "24h")
    assert sc_two["later"] == {"at": round(r48), "accounts": 2, "more_events": 0, "more_accounts": 0}, sc_two["later"]

    # Account A resets at +30h and +72h; Account B resets at +72h -> first event 1 account, 1 more event, 1 more distinct account.
    r30, r72 = NOW + 30 * H, NOW + 72 * H
    a_wins = [qj.Win("k1", 0.5, r30, None, 1.0, "k1"), qj.Win("k2", 0.6, r72, None, 1.0, "k1")]
    b_wins = [qj.Win("k1", 0.5, r72, None, 1.0, "k1"), qj.Win("k2", 0.6, r72, None, 1.0, "k1")]
    sc_stag = qa._scenario(
        [(qj.simulate(a_wins, NOW, end_24h, False), a_wins),
         (qj.simulate(b_wins, NOW, end_24h, False), b_wins)],
        0, True, NOW, end_24h, "24h",
    )
    assert sc_stag["later"] == {"at": round(r30), "accounts": 1, "more_events": 1, "more_accounts": 1}, sc_stag["later"]
