"""Typical's future after the estimate: one simulation of known events, the verdict read off it, and the account's
own restrictions — all synthetic, no real accounts.

What is held here (the estimator's 48-hour rate is quota_forecast.estimate_typical_pace, tested elsewhere; these
tests take a rate as given — directly, or through the state's own cache of that estimate):
- one future for both spans: the simulation runs through every reported reset and every reported end of a
  restriction, whatever span is drawn — the span cuts the output only; usable reserve comes back at the first event
  that really brings it back (an account refilled while still restricted does not);
- every event of one instant is applied before a stop is recorded: capacity back at that very instant is no stop,
  while any positive break is a stop with a positive length, never "almost";
- the value after the first reset is the line's own vertex at that instant: a reset a minute later is not in it;
- a small positive reserve or pace stays positive in the answer; the estimator's reason reaches the verdict;
- an account that cannot run now — disabled, signed out, cooling down, the selected model reported out, or another
  spent limit — keeps its share in the total but is neither drained nor counted as usable until that ends, in the
  plain chart and in the window view alike; an account state that was not read holds nothing.
"""
import math

import pytest

import quota_analytics as qa
import quota_forecast as qf
import quota_summary as qs

H = 3600.0
WEEK = 604800
FIVE_H = 18000
NOW = 1_900_000_000.0  # a fixed synthetic instant, a whole second
OK = {"state": "ok", "rate_per_hour": 0.10, "valid_hours": 24.0, "valid_seconds": 86400, "min_valid_hours": 12}


def _member(rem, reset_at, sid="a"):
    reading = qs.Reading(
        harness="codex", subject_id=sid, source="app", fresh=True, observed_at=NOW - 10,
        key=f"codex|primary|{WEEK}|-", meaning="primary", label="Weekly", window=WEEK, models=(),
        ratio=max(0.0, min(1.0, 1.0 - rem)), ratio_problem="", resets_at=reset_at, plan="plus", plan_key="plus",
    )
    return qs.Member(harness="codex", subject_id=sid, status="measured", reading=reading,
                     remaining=rem, observed_at=reading.observed_at)


def _both_spans(members, info=OK, blocked_until=None):
    return [qf.build_typical_scenario(members, NOW, NOW + span, horizon, info, blocked_until=blocked_until)
            for horizon, span in (("24h", 86400.0), ("7d", 7 * 86400.0))]


def _same_verdict(a, b):
    """Every field of the verdict but the span's own flag."""
    drop = {"first_reset_in_horizon"}
    return {k: v for k, v in a.items() if k not in drop} == {k: v for k, v in b.items() if k not in drop}


# ---------------------------------------------------------------------------
# one future for both spans


def test_usable_reserve_comes_back_at_the_event_that_brings_it_back_on_both_spans():
    """A runs out at +2h; B's selected window resets at +30h, but another limit holds B until +40h: the refill at
    +30h lands on an account that cannot run, so usable reserve is back at +40h — on the 24 h and the 7 d chart."""
    a, b = _member(0.20, None, "a"), _member(0.80, NOW + 30 * H, "b")
    day, week = _both_spans([a, b], blocked_until=[None, NOW + 40 * H])
    for sc in (day, week):
        v = sc["verdict"]
        assert v["state"] == "blocked_gap", v
        assert v["empty_at"] == NOW + 2 * H and v["resumes_at"] == NOW + 40 * H, v
        assert v["gap_seconds"] == 38 * 3600 and v["locked_at_empty"] == 0.8, v
    assert _same_verdict(day["verdict"], week["verdict"])
    assert day["verdict"]["first_reset_in_horizon"] is False and week["verdict"]["first_reset_in_horizon"] is True
    # The span cuts only what is drawn: the 24 h line ends at its own end, with the value the 7 d line has there.
    assert day["line"][-1][0] == NOW + 86400
    assert math.isclose(day["line"][-1][1], qf.eval_polyline(week["line"], NOW + 86400), abs_tol=1e-4)


def test_the_span_end_is_no_event_a_quarter_second_break_is_one_stop_on_both_spans():
    """The pool empties exactly at +24h — the 24 h chart's own end — and refills 0.25 s later: one positive stop on
    both spans, published with a positive length (never 0) and said as running out, not as "almost"."""
    m = _member(0.25, NOW + 86400.25)
    info = dict(OK, rate_per_hour=0.25 / 24)
    day, week = _both_spans([m], info)
    for sc in (day, week):
        v = sc["verdict"]
        assert v["state"] == "runs_out", v
        assert v["empty_at"] == NOW + 86400 and v["resumes_at"] == NOW + 86400.25, v
        assert v["gap_seconds"] == 1 and v["before_first_reset"] == 0.0, v
    assert _same_verdict(day["verdict"], week["verdict"])
    for end, horizon in ((NOW + 86400.0, "24h"), (NOW + 7 * 86400.0, "7d")):
        sim = qf.simulate_pool([(0.25, NOW + 86400.25, None)], NOW, end, 0.25 / 24)
        assert sim["first_stop"] == (NOW + 86400.0, 0.0, NOW + 86400.25), horizon


def test_capacity_back_at_the_same_instant_is_no_stop_and_a_one_minute_break_is():
    a, b = _member(0.20, None, "a"), _member(0.80, None, "b")
    # A reaches zero at +2h, the instant B's other limit lets it run with 0.80: demand is served without a break.
    sc = qf.build_typical_scenario([a, b], NOW, NOW + 86400, "24h", OK, blocked_until=[None, NOW + 2 * H])
    v = sc["verdict"]
    assert v["state"] == "runs_out_no_reset" and v["empty_at"] == NOW + 10 * H and v["locked_at_empty"] is None, v
    assert [r["at"] for r in sc["runs_out"]] == [NOW + 10 * H], sc["runs_out"]
    assert not any(r.get("blocked_only") for r in sc["runs_out"])
    # B a minute later: a real one-minute stop, short (under the bad-tone threshold) but a stop. It is kept as the
    # earlier stop; the verdict says the pool running out with no reset reported, which that minute never hides.
    sc = qf.build_typical_scenario([a, b], NOW, NOW + 86400, "24h", OK, blocked_until=[None, NOW + 2 * H + 60])
    v = sc["verdict"]
    assert v["earlier_empty_at"] == NOW + 2 * H and v["earlier_resumes_at"] == NOW + 2 * H + 60, v
    assert v["earlier_gap_seconds"] == 60 < qf.VERDICT_GAP_BAD_SEC, v
    assert v["state"] == "runs_out_no_reset" and v["empty_at"] == NOW + 10 * H + 60 and v["resumes_at"] is None, v
    sim = qf.simulate_pool([(0.20, None, None), (0.80, None, NOW + 2 * H + 60)], NOW, NOW + 86400, 0.10)
    assert sim["first_stop"] == (NOW + 2 * H, 0.8, NOW + 2 * H + 60), sim["first_stop"]
    assert [(s["locked"], s["resumes"]) for s in sim["stops"]] == [(0.8, NOW + 2 * H + 60), (0.0, None)]
    # Reaching zero at the very instant of the own reset: no stop, the margin is zero — "tight", not "runs out".
    sc = qf.build_typical_scenario([_member(0.10, NOW + H)], NOW, NOW + 86400, "24h", OK)
    v = sc["verdict"]
    assert v["state"] == "tight" and v["empty_at"] is None and v["before_first_reset"] == 0.0, v


def test_a_short_real_stop_is_runs_out_and_a_small_margin_is_tight():
    # 0.10 left at 0.10/h, reset in 65 min: five minutes with nothing left.
    v = qf.build_typical_scenario([_member(0.10, NOW + 65 * 60)], NOW, NOW + 86400, "24h", OK)["verdict"]
    assert v["state"] == "runs_out" and v["empty_at"] == NOW + H and v["gap_seconds"] == 300, v
    # 0.13 left: about 0.02 still there at the reset — a margin, not a stop.
    v = qf.build_typical_scenario([_member(0.13, NOW + 65 * 60)], NOW, NOW + 86400, "24h", OK)["verdict"]
    assert v["state"] == "tight" and v["empty_at"] is None and v["gap_seconds"] is None, v
    assert 0.0 < v["before_first_reset"] <= qf.VERDICT_TIGHT_RESERVE, v
    # Adding 0.90 on an account blocked past the reset keeps the total on the chart (0.901) without hiding "tight".
    a, b = _member(0.201, NOW + 2 * H, "a"), _member(0.90, NOW + 24 * H, "b")
    v = qf.build_typical_scenario([a, b], NOW, NOW + 86400, "24h", OK, blocked_until=[None, NOW + 24 * H])["verdict"]
    assert v["state"] == "tight" and math.isclose(v["before_first_reset"], 0.901, abs_tol=1e-4), v
    assert math.isclose(v["usable_before_reset"], 0.001, abs_tol=1e-4), v


def test_fractional_stop_seconds_stay_before_a_subsecond_refill():
    """Depletion at +3600.72 s with refill at +3600.88 s, and inside the very first fractional second (now=N+0.51,
    empty=N+0.72, reset=N+0.82): exact instants and the initial polyline vertex stay ordered via qs._instant."""
    m = _member(0.10002, NOW + 3600.88)
    day, week = _both_spans([m], OK)
    for sc in (day, week):
        v = sc["verdict"]
        assert v["state"] == "runs_out" and v["gap_seconds"] == 1, v
        assert v["empty_at"] == pytest.approx(NOW + 3600.72, abs=1e-6), v
        assert v["empty_at"] < v["resumes_at"] == pytest.approx(NOW + 3600.88, abs=1e-6), v
        assert sc["runs_out"][0]["at"] == pytest.approx(NOW + 3600.72, abs=1e-6), sc["runs_out"]

    now_f = NOW + 0.51
    m_f = _member(0.1 * 0.21 / 3600.0, NOW + 0.82)
    sc_f = qf.build_typical_scenario([m_f], now_f, round(now_f + 86400), "24h", OK)
    vf = sc_f["verdict"]
    assert vf["state"] == "runs_out" and vf["gap_seconds"] == 1, vf
    assert vf["empty_at"] == pytest.approx(NOW + 0.72, abs=1e-6), vf
    assert vf["empty_at"] < vf["resumes_at"] == pytest.approx(NOW + 0.82, abs=1e-6), vf
    assert sc_f["runs_out"][0]["at"] == pytest.approx(NOW + 0.72, abs=1e-6), sc_f["runs_out"]
    assert sc_f["line"][0][0] == pytest.approx(NOW + 0.51, abs=1e-6) and sc_f["line"][0][1] > 0, sc_f["line"][:4]
    assert [(round(t - NOW, 2), val) for t, val in sc_f["line"][1:4]] == [(0.72, 0.0), (0.82, 0.0), (0.82, 1.0)], sc_f["line"][:5]


def test_an_earlier_reset_on_an_unusable_account_never_hides_a_later_outage_or_tight_margin():
    """Disabled B resets at +2h; runnable A (0.30 left, reset +100h) runs out at +3h. The strip keeps Before +2h (0.60)
    and still reports the +3h..+100h outage (and continuous +2h06m outage when B is temporarily held until +5h)."""
    a, b = _member(0.30, NOW + 100 * H, "a"), _member(0.50, NOW + 2 * H, "b")
    for sc in _both_spans([a, b], OK, blocked_until=[None, math.inf]):
        v = sc["verdict"]
        assert v["state"] == "blocked_gap" and v["empty_at"] == NOW + 3 * H and v["resumes_at"] == NOW + 100 * H, v
        assert v["locked_at_empty"] == 1.0 and v["first_reset_at"] == NOW + 2 * H and v["before_first_reset"] == 0.6, v
    # B held by another limit until +5h while resetting at +2h, A emptying 6 min after +2h: continuous with +2h00m.
    a2 = _member(0.21, NOW + 100 * H, "a")
    v = qf.build_typical_scenario([a2, b], NOW, NOW + 86400, "24h", OK, blocked_until=[None, NOW + 5 * H])["verdict"]
    assert v["state"] == "blocked_gap" and v["empty_at"] == NOW + 2 * H + 360 and v["resumes_at"] == NOW + 5 * H, v
    assert v["locked_at_empty"] == 1.0 and v["first_reset_at"] == NOW + 2 * H and v["before_first_reset"] == 0.51, v
    # Disabled B resets at +2h while runnable A dips to 0.02 usable right before A's own +5h reset: warns "tight".
    a3 = _member(0.52, NOW + 5 * H, "a")
    v = qf.build_typical_scenario([a3, b], NOW, NOW + 86400, "24h", OK, blocked_until=[None, math.inf])["verdict"]
    assert v["state"] == "tight" and v["first_reset_at"] == NOW + 2 * H and v["before_first_reset"] == 0.82, v
    assert v["tight_reset_at"] == NOW + 5 * H and math.isclose(v["before_tight_reset"], 1.02, abs_tol=1e-4), v
    assert math.isclose(v["usable_before_reset"], 0.02, abs_tol=1e-4), v


def test_the_value_after_the_first_reset_is_the_lines_vertex_at_that_instant():
    """A resets 30 s before the 24 h chart's end, B 30 s after it: after A's reset the pool holds 1.40, on both
    spans, as the 24 h schedule and the line say; B's refill is a later vertex, never counted at A's moment."""
    end = NOW + 86400
    a, b = _member(0.40, end - 30, "a"), _member(0.40, end + 30, "b")
    zero = dict(OK, state="zero", rate_per_hour=0.0)
    day, week = _both_spans([a, b], zero)
    for sc in (day, week):
        v = sc["verdict"]
        assert v["first_reset_at"] == end - 30 and v["first_reset_accounts"] == 1, v
        assert v["before_first_reset"] == 0.8 and v["after_first_reset"] == 1.4 and v["first_reset_adds"] == 0.6, v
        assert v["after_first_reset"] == round(qf.eval_polyline(sc["line"], end - 30, right=True), 4)
    assert day["schedule"][0]["total_after"] == 1.4 and day["later"]["at"] == end + 30
    for sc in (qf.build_no_new_use_verdict([a, b], NOW, end), qf.build_no_new_use_verdict([a, b], NOW, NOW + 604800)):
        assert sc["after_first_reset"] == 1.4 and sc["first_reset_accounts"] == 1, sc


def test_a_small_positive_reserve_or_pace_stays_positive_and_a_real_zero_stays_zero():
    info = dict(OK, rate_per_hour=0.00004)
    sc = qf.build_typical_scenario([_member(0.00004, NOW + 30 * H)], NOW, NOW + 86400, "24h", info)
    v = sc["verdict"]
    assert v["current"] > 0 and v["rate_per_hour"] > 0 and sc["rate_per_hour"] > 0, (v, sc["rate_per_hour"])
    assert math.isclose(v["current"], 0.00004, rel_tol=0.05) and math.isclose(v["rate_per_hour"], 0.00004, rel_tol=0.05)
    v = qf.build_typical_scenario([_member(0.0, NOW + 30 * H)], NOW, NOW + 86400, "24h", OK)["verdict"]
    assert v["current"] == 0.0 and v["state"] == "empty_now", v


def test_the_estimators_reason_reaches_the_scenario_and_the_verdict_with_no_numbers_under_typical():
    info = {"state": "insufficient", "reason": "all_observed_at_limit", "rate_per_hour": None,
            "valid_seconds": 13 * 3600, "valid_hours": 13.0, "min_valid_hours": 12}
    # Every account at its limit now, too: the verdict is still the estimator's word, not "empty now".
    sc = qf.build_typical_scenario([_member(0.0, NOW + 3 * H, "a"), _member(0.0, NOW + 5 * H, "b")],
                                   NOW, NOW + 86400, "24h", info)
    assert sc["status"] == "insufficient" and sc["reason"] == "all_observed_at_limit"
    assert sc["verdict"]["state"] == "insufficient" and sc["verdict"]["reason"] == "all_observed_at_limit"
    assert sc["line"] == [] and sc["checkpoints"] == [] and sc["schedule"] == [] and sc["runs_out"] == []
    assert sc["verdict"]["before_first_reset"] is None and sc["verdict"]["after_first_reset"] is None


# ---------------------------------------------------------------------------
# the account's own restrictions, in the plain chart and in the window view


def _con(cid, used, window, reset_at, label, models=None):
    row = {"id": cid, "label": label, "used_ratio": used, "window_seconds": window,
           "resets_at": qs.iso(reset_at) if reset_at is not None else None}
    if models is not None:
        row["applies_to_models"] = list(models)
    return row


def _snap(sid, constraints, availability=None):
    return {"subject": {"harness": "codex", "subject_id": sid, "plan_label": "plus"}, "constraints": constraints,
            "availability": availability or {"state": "available"}, "observed_at": qs.iso(NOW - 10),
            "freshness": "fresh", "source": "app"}


def _profile(sid, enabled=True, verification="passed", availability="available"):
    return {"profile": {"harness_id": "codex", "profile_id": sid, "enabled": enabled, "display_name": sid},
            "status": {"verification": verification, "availability": availability, "detail": ""},
            "identity": {"email": "", "plan": ""}}


def _payload(snaps, profiles, accounts_read="ok"):
    return {"reads": {"catalog": "ok", "accounts": accounts_read, "quota": "ok"}, "daemon": {"state": "running"},
            "unified_accounts": False,
            "harnesses": [{"id": "codex", "display_name": "Codex", "status": "ok", "enabled": True}],
            "profiles": {"profiles": profiles, "harnessAccounts": []}, "quota": snaps, "quota_absences": []}


RATE = {"state": "ok", "reason": "", "rate_per_hour": 0.05, "valid_seconds": 14 * 3600, "valid_hours": 14.0,
        "observed_burn": 0.7, "window_hours": 48, "min_valid_hours": 12}


def _typical_both_ways(data, models=None):
    """The Typical verdict of the weekly limit as the plain chart and as the window view give it, with the 48-hour
    rate taken as given (the state's own cache of the estimate)."""
    state = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    calc = next(g for g in state.groups if g.window == WEEK)
    state.typical_cache[calc.key] = dict(RATE)
    chart = qs.build_chart(state, calc.key, "codex", "24h")["scenarios"]["typical"]
    window = qa.build(state, calc, "24h", model=models[0] if models else None)["window"]["scenarios"]["typical"]
    return chart, window


def _two_accounts(a_profile=None, a_availability=None, accounts_read="ok", models=None):
    """A: 0.20 weekly left. B: 0.40 weekly left, its 5-hour window spent until +5h. Pool pace 0.05/h."""
    week_a = _con("primary", 0.80, WEEK, NOW + 60 * H, "Weekly", models)
    week_b = _con("primary", 0.60, WEEK, NOW + 60 * H, "Weekly", models)
    return _payload([
        _snap("a", [week_a, _con("five_hour", 0.10, FIVE_H, NOW + 4 * H, "5 hour")], a_availability),
        _snap("b", [week_b, _con("five_hour", 1.0, FIVE_H, NOW + 5 * H, "5 hour")]),
    ], [a_profile or _profile("a"), _profile("b")], accounts_read)


def test_with_every_account_able_to_run_the_pool_runs_out_then_waits_for_the_other_limit():
    """A runs out at +4h while B's other limit holds B until +5h: a one-hour stop. B then runs 0.40 at 0.05/h and
    the pool is out from +13h to the reset at +60h. The verdict says the stop still on at the first reset; the
    earlier hour is kept beside it, never in its place."""
    for sc in _typical_both_ways(_two_accounts()):
        v = sc["verdict"]
        assert sc["status"] == "ok" and v["state"] == "runs_out", v
        assert v["empty_at"] == NOW + 13 * H and v["resumes_at"] == NOW + 60 * H and v["locked_at_empty"] is None, v
        assert v["earlier_empty_at"] == NOW + 4 * H and v["earlier_resumes_at"] == NOW + 5 * H, v
        assert v["earlier_gap_seconds"] == 3600 and v["before_first_reset"] == 0.0, v
        assert v["blocked_accounts"] == 1 and v["held_accounts"] == 0 and v["unknown_accounts"] == 0, v


@pytest.mark.parametrize("case", ["disabled", "signed_out", "cooling", "model_exhausted"])
def test_an_account_that_cannot_run_now_is_held_apart_from_its_share(case):
    """A cannot run now: no usable reserve from now until B's other limit lets B run at +5h — never "4 h of work
    left" on A's 0.20. The balance still counts A's share (0.20 + 0.40 = 0.60). That stop from now is kept as the
    earlier one; the verdict says the stop still on at the reset (+60h): A held for good, B spent from +13h; A
    back (+8h, +6h) and both spent from +17h."""
    models = ["m1"] if case == "model_exhausted" else None
    data = _two_accounts(
        a_profile=(_profile("a", enabled=False) if case == "disabled"
                   else _profile("a", verification="failed", availability="unavailable") if case == "signed_out"
                   else None),
        a_availability=({"state": "cooldown", "resets_at": qs.iso(NOW + 8 * H)} if case == "cooling"
                        else {"state": "available", "model_scoped_exhaustions": [
                            {"constraint_id": "primary", "applies_to_models": models,
                             "resets_at": qs.iso(NOW + 6 * H)}]} if case == "model_exhausted" else None),
        models=models)
    held_for_good = case in ("disabled", "signed_out")
    for sc in _typical_both_ways(data, models):
        v = sc["verdict"]
        assert v["earlier_empty_at"] == round(NOW) and v["earlier_resumes_at"] == NOW + 5 * H, (case, v)
        assert v["current"] == 0.6 and v["resumes_at"] == NOW + 60 * H, (case, v)
        if held_for_good:
            assert sc["status"] == "ok" and v["state"] == "blocked_gap", (case, v)
            assert v["empty_at"] == NOW + 13 * H and v["locked_at_empty"] == 0.2, (case, v)
        else:
            assert sc["status"] == "ok" and v["state"] == "runs_out", (case, v)
            assert v["empty_at"] == NOW + 17 * H and v["locked_at_empty"] is None, (case, v)
        assert (v["held_accounts"], v["blocked_accounts"]) == ((1, 1) if held_for_good else (0, 2)), (case, v)
        sim = qf.simulate_pool([(0.20, NOW + 60 * H, math.inf if held_for_good else
                                 NOW + (8 if case == "cooling" else 6) * H),
                                (0.40, NOW + 60 * H, NOW + 5 * H)], NOW, NOW + 86400, 0.05)
        start, locked, resumes = sim["first_stop"]
        assert (start, resumes) == (round(NOW), NOW + 5 * H) and math.isclose(locked, 0.6), (case, sim["first_stop"])


def test_an_account_state_that_was_not_read_holds_nothing():
    data = _two_accounts(a_profile=_profile("a", enabled=False), accounts_read="failed")
    for sc in _typical_both_ways(data):
        v = sc["verdict"]
        assert v["earlier_empty_at"] == NOW + 4 * H and v["earlier_resumes_at"] == NOW + 5 * H, v
        assert v["state"] == "runs_out" and v["empty_at"] == NOW + 13 * H and v["resumes_at"] == NOW + 60 * H, v
        assert v["held_accounts"] == 0, v


def test_every_account_restricted_with_no_reported_end_is_held_for_that_reason():
    data = _payload([_snap("a", [_con("primary", 0.80, WEEK, NOW + 60 * H, "Weekly")]),
                     _snap("b", [_con("primary", 0.60, WEEK, NOW + 60 * H, "Weekly")])],
                    [_profile("a", enabled=False), _profile("b", verification="failed", availability="unavailable")])
    for sc in _typical_both_ways(data):
        assert sc["status"] == "held" and sc["held_reason"] == "restricted", sc
        assert sc["verdict"]["state"] == "held" and sc["verdict"]["held_reason"] == "restricted"
        assert sc["line"] == [] and sc["checkpoints"] == [] and sc["schedule"] == []


def test_a_model_reported_out_on_its_own_window_stops_that_models_use_of_the_shared_week():
    """One account: the shared week has 0.30 left, the model-a week 0.60, and model-a is reported out until +24h.
    For a model-a request the shared week cannot be used before +24h (a known hold of another window that binds the
    request), and then it runs out at +27h, before the reset at +60h; with no model chosen the shared week runs out at
    its own pace, as before (0.30 at 0.10/h: +3h)."""
    models = ["model-a"]
    data = _payload([_snap("a", [
        _con("seven_day", 0.70, WEEK, NOW + 60 * H, "7 day"),
        _con("weekly_scoped", 0.40, WEEK, NOW + 60 * H, "Weekly model-a", models),
    ], {"state": "available", "model_scoped_exhaustions": [
        {"constraint_id": "weekly_scoped", "applies_to_models": models, "resets_at": qs.iso(NOW + 24 * H)}]})],
        [_profile("a")])
    state = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    shared = next(g for g in state.groups if g.window == WEEK and not g.models)
    scoped = next(g for g in state.groups if g.window == WEEK and g.models)
    rate = dict(RATE, rate_per_hour=0.10)
    for calc in (shared, scoped):
        state.typical_cache[calc.key] = dict(rate)

    def verdict(calc, model):
        return qa.build(state, calc, "24h", model=model)["window"]["scenarios"]["typical"]["verdict"]

    v = verdict(shared, "model-a")
    assert v["earlier_empty_at"] == round(NOW) and v["earlier_resumes_at"] == NOW + 24 * H, v
    assert v["state"] == "runs_out" and v["empty_at"] == NOW + 27 * H and v["resumes_at"] == NOW + 60 * H, v
    v = verdict(scoped, "model-a")
    assert v["earlier_empty_at"] == round(NOW) and v["earlier_resumes_at"] == NOW + 24 * H, v
    assert v["state"] == "runs_out" and v["resumes_at"] == NOW + 60 * H, v
    v = verdict(shared, None)
    assert v["state"] == "runs_out" and v["empty_at"] == NOW + 3 * H, v
    # The shared week's own summary and plain chart keep its own state: no model, no hold.
    assert not [m for m in shared.measured if m.hold_until is not None]
    assert qs.build_chart(state, shared.key, "codex", "24h")["scenarios"]["typical"]["verdict"]["empty_at"] == NOW + 3 * H


def test_a_spent_scoped_window_keeps_a_longer_model_exhaustion_hold_across_its_own_refill():
    """Selected model-scoped window is at 0 now (resets at +2h) while a model_scoped_exhaustion on the same scope lasts
    until +6h: summary restrictions do not double-count the spent window, yet hold_until stays +6h so the +2h refill is
    not burned until +6h."""
    models = ["model-a"]
    data = _payload([_snap("a", [_con("weekly_scoped", 1.0, WEEK, NOW + 2 * H, "Weekly model-a", models)],
                           {"state": "available", "model_scoped_exhaustions": [
                               {"constraint_id": "other_model_cap", "applies_to_models": models,
                                "resets_at": qs.iso(NOW + 6 * H)}]})], [_profile("a")])
    state = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    scoped = state.groups[0]
    member = scoped.measured[0]
    assert "model_exhausted" not in member.flags and member.hold_until == NOW + 6 * H, (member.flags, member.hold_until)
    state.typical_cache[scoped.key] = dict(RATE)
    for sc in (qs.build_chart(state, scoped.key, "codex", "7d")["scenarios"]["typical"],
               qa.build(state, scoped, "7d", model="model-a")["window"]["scenarios"]["typical"]):
        v = sc["verdict"]
        assert v["state"] == "blocked_gap" and v["empty_at"] == round(NOW) and v["resumes_at"] == NOW + 6 * H, v
        assert sc["runs_out"][0]["at"] == NOW + 26 * H, sc["runs_out"]
        assert qf.eval_polyline(sc["line"], NOW + 4 * H) == 1.0 and qf.eval_polyline(sc["line"], NOW + 6 * H) == 1.0


def test_pre_unlock_margin_excludes_just_unlocked_balance_and_tiny_no_new_use_stays_positive():
    """1. At 0.05 win/h, refilled B unlocks at +4h while runnable A dips to 0.02 just before +4h:
    the pre-unlock margin is 0.02 (tight) reported under tight_unlock_at (never tight_reset_at),
    preserving B's real +2h refill in first_reset_at / before_first_reset / after_first_reset.
    2. Selected-window No new use keeps 0.00004 positive in line and checkpoints while true 0.0 stays 0.0."""
    a, b = _member(0.22, NOW + 100 * H, "a"), _member(0.50, NOW + 2 * H, "b")
    sc = qf.build_typical_scenario([a, b], NOW, NOW + 86400, "24h", RATE, blocked_until=[None, NOW + 4 * H])
    v = sc["verdict"]
    assert v["state"] == "tight" and v["first_reset_at"] == NOW + 2 * H, v
    assert math.isclose(v["before_first_reset"], 0.62, abs_tol=1e-4), v
    assert math.isclose(v["after_first_reset"], 1.12, abs_tol=1e-4), v
    assert "tight_reset_at" not in v and "usable_before_reset" not in v, v
    assert v["tight_unlock_at"] == NOW + 4 * H and math.isclose(v["before_tight_unlock"], 1.02, abs_tol=1e-4), v
    assert math.isclose(v["usable_before_unlock"], 0.02, abs_tol=1e-4), v

    # Selected-window No new use: 0.00004 stays positive in line and checkpoints; 0.0 stays 0.0.
    tiny_data = _payload([_snap("a", [_con("primary", 0.99996, WEEK, NOW + 30 * H, "Weekly")])], [_profile("a")])
    tiny_state = qs.compute(tiny_data, qs.HistoryView(state="empty"), NOW)
    tiny_calc = tiny_state.groups[0]
    for nnu in (
        qs.build_chart(tiny_state, tiny_calc.key, "codex", "24h")["scenarios"]["no_new_use"],
        qa.build(tiny_state, tiny_calc, "24h")["window"]["scenarios"]["no_new_use"],
    ):
        assert nnu["line"] and all(val > 0 and math.isclose(val, 0.00004, rel_tol=0.05) for _t, val in nnu["line"]), nnu["line"]
        assert nnu["checkpoints"] and all(
            cp["value"] > 0 and math.isclose(cp["value"], 0.00004, rel_tol=0.05) for cp in nnu["checkpoints"]
        ), nnu["checkpoints"]
        assert math.isclose(nnu["verdict"]["current"], 0.00004, rel_tol=0.05), nnu["verdict"]

    zero_data = _payload([_snap("a", [_con("primary", 1.0, WEEK, NOW + 30 * H, "Weekly")])], [_profile("a")])
    zero_state = qs.compute(zero_data, qs.HistoryView(state="empty"), NOW)
    zero_calc = zero_state.groups[0]
    for nnu in (
        qs.build_chart(zero_state, zero_calc.key, "codex", "24h")["scenarios"]["no_new_use"],
        qa.build(zero_state, zero_calc, "24h")["window"]["scenarios"]["no_new_use"],
    ):
        assert nnu["line"] and all(val == 0.0 for _t, val in nnu["line"]), nnu["line"]
        assert nnu["checkpoints"] and all(cp["value"] == 0.0 for cp in nnu["checkpoints"]), nnu["checkpoints"]
        assert nnu["verdict"]["current"] == 0.0, nnu["verdict"]


@pytest.mark.parametrize("reported_reset", [True, False], ids=["known-end", "unknown-end"])
def test_offscreen_binding_spent_window_beyond_max_groups_still_holds_selected_request(reported_reset):
    """When 64 other groups fill MAX_GROUPS, an offscreen exhausted window that binds the
    selected request (model-scoped, prefix-scoped, or request-kind-specific) still holds
    the account with its exact reset deadline or endless other_limit_spent reason, without
    affecting unrelated requests, raising MAX_GROUPS, or widening history reads."""
    assert qs.MAX_GROUPS == 64
    reset_at = NOW + 8 * H if reported_reset else None
    visible = _con("visible_shared", 0.70, FIVE_H, NOW + 60 * H, "Visible 5h")
    hidden_model = _con("hidden_model", 1.0, WEEK, reset_at, "Hidden model-a", models=["model-a"])
    hidden_prefix = dict(_con("hidden_prefix", 1.0, WEEK, reset_at, "Hidden gem"),
                         applies_to_models=[], applies_to_model_prefixes=["gem-"])
    hidden_text = dict(_con("hidden_text", 1.0, WEEK, reset_at, "Hidden text"),
                       applies_to_request_kinds=["text"])
    hidden_image = dict(_con("hidden_image", 1.0, WEEK, NOW + 2 * H, "Hidden image"),
                        applies_to_request_kinds=["image"])
    # Put filler groups on account b so per-account constraint caps do not drop account a's windows.
    fillers = [_con(f"filler_{i:02d}", 0.10, 30000, NOW + 50 * H, f"Filler {i:02d}")
               for i in range(qs.MAX_GROUPS - 1)]

    # 1. Model-scoped & prefix-scoped offscreen spent windows (norm.spent stays empty for scoped limits).
    data_scoped = _payload([
        _snap("a", [visible, hidden_model, hidden_prefix]),
        _snap("b", fillers),
    ], [_profile("a"), _profile("b")])
    state = qs.compute(data_scoped, qs.HistoryView(state="empty"), NOW)
    assert len(state.groups) == qs.MAX_GROUPS
    assert not any(g.meaning.startswith("hidden_") for g in state.groups)
    assert not state.norm.spent  # scoped limits never pollute unscoped norm.spent
    vis = next(g for g in state.groups if g.meaning == "visible_shared")
    state.typical_cache[vis.key] = dict(RATE, rate_per_hour=0.10)

    # History reads stay bounded to displayed groups only (never widened for offscreen groups).
    wanted = {}
    qa.extend_requests(wanted, state.groups, vis, "salt", NOW, model="model-a", kind="text")
    assert all("hidden_" not in series for _sub, series in wanted)

    for matching_model in ("model-a", "gem-pro"):
        built = qa.build(state, vis, "24h", model=matching_model, kind="text")
        typ = built["window"]["scenarios"]["typical"]
        v = typ["verdict"]
        if reported_reset:
            assert typ["status"] == "ok" and v["state"] == "runs_out", (matching_model, v)
            assert (v["earlier_empty_at"], v["earlier_resumes_at"]) == (round(NOW), NOW + 8 * H), (matching_model, v)
            assert v["empty_at"] == NOW + 11 * H and v["blocked_accounts"] == 1, (matching_model, v)
        else:
            assert typ["status"] == "held" and typ["held_reason"] == "other_limit_spent", (matching_model, typ)
            assert v["state"] == "held" and v["held_reason"] == "other_limit_spent", (matching_model, v)
        # Legacy recent_pace is also held rather than draining an exhausted account.
        assert built["window"]["scenarios"]["recent_pace"]["at_pace"] == 0

    # Unrelated models and unspecified-model requests remain completely unaffected.
    for other_model in ("model-b", None):
        v_free = qa.build(state, vis, "24h", model=other_model, kind="text")["window"]["scenarios"]["typical"]["verdict"]
        assert v_free["state"] == "runs_out" and v_free["empty_at"] == NOW + 3 * H, (other_model, v_free)
        assert v_free["earlier_empty_at"] is None and v_free["blocked_accounts"] == 0, (other_model, v_free)

    # 2. Request-kind-specific offscreen spent window: binds text on an all-kinds window, never image.
    data_kind = _payload([
        _snap("a", [visible, hidden_text, hidden_image]),
        _snap("b", fillers),
    ], [_profile("a"), _profile("b")])
    state_k = qs.compute(data_kind, qs.HistoryView(state="empty"), NOW)
    assert len(state_k.groups) == qs.MAX_GROUPS
    assert not any(g.meaning.startswith("hidden_") for g in state_k.groups)
    vis_k = next(g for g in state_k.groups if g.meaning == "visible_shared")
    state_k.typical_cache[vis_k.key] = dict(RATE, rate_per_hour=0.10)

    v_text = qa.build(state_k, vis_k, "24h", model=None, kind="text")["window"]["scenarios"]["typical"]["verdict"]
    if reported_reset:
        assert v_text["earlier_empty_at"] == round(NOW) and v_text["earlier_resumes_at"] == NOW + 8 * H, v_text
        assert v_text["empty_at"] == NOW + 11 * H, v_text
    else:
        assert v_text["state"] == "held" and v_text["held_reason"] == "other_limit_spent", v_text

    v_img = qa.build(state_k, vis_k, "24h", model=None, kind="image")["window"]["scenarios"]["typical"]["verdict"]
    assert v_img["earlier_empty_at"] == round(NOW) and v_img["earlier_resumes_at"] == NOW + 2 * H, v_img
    assert v_img["empty_at"] == NOW + 5 * H, v_img

    # 3. Combined (joint) view with two visible windows + offscreen spent binding window reports 0.0, not positive.
    vis2 = _con("visible_day", 0.50, 24000, NOW + 60 * H, "Visible 2")
    fillers_j = [_con(f"filler_{i:02d}", 0.10, 30000, NOW + 50 * H, f"Filler {i:02d}")
                 for i in range(qs.MAX_GROUPS - 2)]
    data_joint = _payload([
        _snap("a", [visible, vis2, hidden_model]),
        _snap("b", fillers_j),
    ], [_profile("a"), _profile("b")])
    state_j = qs.compute(data_joint, qs.HistoryView(state="empty"), NOW)
    assert len(state_j.groups) == qs.MAX_GROUPS
    assert not any(g.meaning == "hidden_model" for g in state_j.groups)
    vis_j = next(g for g in state_j.groups if g.meaning == "visible_shared")
    joint = qa.build(state_j, vis_j, "24h", model="model-a", kind="text")["joint"]
    assert joint["available"] is True and joint["known"] == 1 and joint["figure"] == 0.0, joint
    assert joint["hours"]["value"] == 0.0, joint["hours"]
