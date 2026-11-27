"""Typical's future after the estimate, second round: which stop the verdict says, what an account of unknown
availability is, which holds bind a request, which resets the selected limit marks, and how the widget says each.
All readings are synthetic; the 48-hour rate is given (the estimator is tested elsewhere).

What is held here:
- a stop that begins at the first reset, because that refill lands only on accounts that cannot run, is said; a
  refill or an end of a restriction that brings usable reserve back at that very instant is no stop;
- the stop still on at the first reset is the one said, never a shorter break before it, which is kept apart as the
  earlier stop; with no stop on at the reset, the longest one;
- an account another limit of which is not known now is a condition (``unknown_gap``), counted apart and never
  "restricted with no reported end", also when it refills at the instant the stop begins or later in it, before the
  known return; every account unknown stays held, and a known hold with no end stays a hold;
- a model's cooldown or reported exhaustion binds that model's requests on a shared window although no window of
  that model is read; another model's requests, and the plain chart, are not held by it; of two holds of one scope
  the later end holds, while the snapshot's validity bound keeps the earliest;
- the selected window's No new use lists its own resets only, and names the next one past a short span;
- each stop is said once, and only one after an earlier stop is "again";
- the widget: the strip, its figures and its mark for these stops; "11.9h valid of 12h needed", never
  "12h valid of 12h needed"; a little reserve reads "<0.01" under the cursor; Combined names the next reset even
  when the 24 h span ends before it.
"""
import json
import math
import os
import subprocess
import time
from pathlib import Path

import pytest

import plugin
import quota_analytics as qa
import quota_forecast as qf
import quota_history
import quota_summary as qs
import test_quotas
import test_recent_pace_binding as rp
from test_reserve import _node, payload
from test_typical_events import FIVE_H, H, NOW, OK, RATE, WEEK, _both_spans, _member, _same_verdict

HERE = Path(__file__).parent
DAY = 86400.0


def _con(cid, used, window, reset_at, label, *, models=None, freshness=None, **extra):
    row = {"id": cid, "label": label, "used_ratio": used, "window_seconds": window,
           "resets_at": qs.iso(reset_at) if reset_at is not None else None}
    if models is not None:
        row["applies_to_models"] = list(models)
    if freshness is not None:
        row["freshness"] = freshness
    row.update(extra)
    return row


def _snap(sid, constraints, at, availability=None):
    return {"subject": {"harness": "codex", "subject_id": sid, "plan_label": "plus"}, "constraints": constraints,
            "availability": availability or {"state": "available"}, "observed_at": qs.iso(at - 10),
            "freshness": "fresh", "source": "app"}


def _profile(sid, enabled=True):
    return {"profile": {"harness_id": "codex", "profile_id": sid, "enabled": enabled, "display_name": sid},
            "status": {"verification": "passed", "availability": "available", "detail": ""},
            "identity": {"email": "", "plan": ""}}


def _state(snaps, disabled=(), rate=0.05):
    data = payload(snaps, [_profile(s["subject"]["subject_id"], s["subject"]["subject_id"] not in disabled)
                           for s in snaps], harnesses=("codex",))
    state = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    for calc in state.groups:
        state.typical_cache[calc.key] = dict(RATE, rate_per_hour=rate)
    return state


def _week(state, scoped=False):
    return next(g for g in state.groups if g.window == WEEK and bool(g.models) == scoped)


def _window(state, calc, horizon="24h", model=None):
    return qa.build(state, calc, horizon, model=model)["window"]


# ---------------------------------------------------------------------------
# the stop the verdict says


def test_a_stop_that_begins_at_a_refill_landing_on_accounts_that_cannot_run_is_said():
    """A runs out at +2h, the very instant B's selected window resets, but another limit holds B until +5h: the
    refill lands on an account that cannot run. No usable reserve from +2h to +5h, on both spans, never "lasts"."""
    a, b = _member(0.20, None, "a"), _member(0.30, NOW + 2 * H, "b")
    day, week = _both_spans([a, b], blocked_until=[None, NOW + 5 * H])
    for sc in (day, week):
        v = sc["verdict"]
        assert v["state"] == "blocked_gap", v
        assert (v["empty_at"], v["resumes_at"], v["gap_seconds"]) == (NOW + 2 * H, NOW + 5 * H, 3 * 3600), v
        assert v["first_reset_at"] == NOW + 2 * H and v["before_first_reset"] == 0.3 and v["after_first_reset"] == 1.0, v
        # What is left at its start is what that instant leaves: B refilled, on a restricted account.
        assert v["locked_at_empty"] == 1.0 and v["earlier_empty_at"] is None, v
        # Said first by what holds the rest; B, back at +5h, runs out again at +15h.
        assert [(r["at"], bool(r.get("blocked_only")), r["again"]) for r in sc["runs_out"]] == [
            (NOW + 2 * H, True, False), (NOW + 15 * H, False, True)], sc["runs_out"]
    assert _same_verdict(day["verdict"], week["verdict"])


@pytest.mark.parametrize("left,rate,at", [(0.20, 0.10, 2 * H), (0.10, 0.30, 1200.0)])
def test_capacity_back_at_the_refill_instant_is_no_stop(left, rate, at):
    """A runs out at the instant B both resets and may run again (the second case lands within float noise of it):
    the refill serves the demand at once. No stop, no mark, the margin before the reset is B's share."""
    a, b = _member(left, None, "a"), _member(0.30, NOW + at, "b")
    info = dict(OK, rate_per_hour=rate)
    for sc in _both_spans([a, b], info, blocked_until=[None, NOW + at]):
        v = sc["verdict"]
        assert v["empty_at"] is None and v["earlier_empty_at"] is None and v["state"] == "lasts", v
        assert math.isclose(v["before_first_reset"], 0.3, abs_tol=1e-4), v
        assert not [r for r in sc["runs_out"] if r["at"] <= NOW + at + 1], sc["runs_out"]


def test_a_short_earlier_stop_never_stands_in_for_the_stop_on_at_the_first_reset():
    """A runs out at +2h; another limit holds B one minute more; B then runs its 0.50 and the pool is out from
    +7h01m to the first reset (+20h). The verdict says that stop; the minute is kept as the earlier one."""
    a, b = _member(0.20, None, "a"), _member(0.50, NOW + 20 * H, "b")
    day, week = _both_spans([a, b], blocked_until=[None, NOW + 2 * H + 60])
    for sc in (day, week):
        v = sc["verdict"]
        assert v["state"] == "runs_out" and v["empty_at"] == NOW + 7 * H + 60 and v["resumes_at"] == NOW + 20 * H, v
        assert v["gap_seconds"] == 13 * 3600 - 60 and v["before_first_reset"] == 0.0, v
        assert (v["earlier_empty_at"], v["earlier_resumes_at"], v["earlier_gap_seconds"]) == (
            NOW + 2 * H, NOW + 2 * H + 60, 60), v
        # Details: each stop once, the first by what holds the rest, the next as "again" (on 7 d a third after the
        # reset, refilled, also "again").
        assert [(r["at"], bool(r.get("blocked_only")), r["again"]) for r in sc["runs_out"][:2]] == [
            (NOW + 2 * H, True, False), (NOW + 7 * H + 60, False, True)], sc["runs_out"]
        assert all(r["again"] for r in sc["runs_out"][1:]), sc["runs_out"]
    assert _same_verdict(day["verdict"], week["verdict"])


def test_with_no_stop_on_at_the_reset_the_longest_one_is_said():
    """A minute at +2h (B held), then five hours from +5h01m until C may run at +10h; the first reset (+15h) finds
    reserve. The five hours are said, the minute is the earlier stop."""
    a, b, c = _member(0.20, NOW + 15 * H, "a"), _member(0.30, None, "b"), _member(1.0, None, "c")
    sc = qf.build_typical_scenario([a, b, c], NOW, NOW + DAY, "24h", OK,
                                   blocked_until=[None, NOW + 2 * H + 60, NOW + 10 * H])
    v = sc["verdict"]
    assert v["state"] == "blocked_gap" and v["empty_at"] == NOW + 5 * H + 60 and v["resumes_at"] == NOW + 10 * H, v
    assert v["locked_at_empty"] == 1.0 and v["earlier_empty_at"] == NOW + 2 * H, v
    assert v["earlier_resumes_at"] == NOW + 2 * H + 60 and v["before_first_reset"] > 0.4, v


def test_a_stop_is_said_once_and_again_only_after_an_earlier_one():
    # The first stop comes after a refill, with no stop before it: not "again".
    sim = qf.simulate_pool([(0.50, NOW + H, None)], NOW, NOW + 7 * DAY, 0.10)
    assert [(r["at"], r["after_refill"], r["again"], r["reset_reported"]) for r in sim["runs_out"]] == [
        (NOW + 11 * H, True, False, False)], sim["runs_out"]
    # Out before the reset, then out again after it.
    sim = qf.simulate_pool([(0.05, NOW + H, None)], NOW, NOW + 7 * DAY, 0.10)
    assert [(r["at"], r["again"]) for r in sim["runs_out"]] == [(NOW + 1800, False), (NOW + 11 * H, True)]


def test_a_little_reserve_stays_positive_on_the_line_and_in_the_schedule():
    sc = qf.build_typical_scenario([_member(0.00004, NOW + 30 * H)], NOW, NOW + 7 * DAY, "7d",
                                   dict(OK, rate_per_hour=0.000001))
    assert sc["line"][0][1] > 0 and all(v > 0 for _t, v in sc["line"]), sc["line"]
    assert all(c["value"] > 0 for c in sc["checkpoints"]), sc["checkpoints"]


# ---------------------------------------------------------------------------
# accounts of unknown availability


def _unknown_pair(a_five="stale", b_five="fresh", disabled=()):
    """A: 0.40 weekly left; B: 0.20 weekly left; both reset at +60h. Each has a 5-hour window, ``stale`` (not known
    now) or ``fresh`` (10% used). Pool pace 0.05/h."""
    def snap(sid, left, five):
        return _snap(sid, [_con("primary", 1 - left, WEEK, NOW + 60 * H, "Weekly", freshness="fresh"),
                           _con("five_hour", 0.10, FIVE_H, NOW + 4 * H, "5 hour", freshness=five)], NOW)
    return _state([snap("a", 0.40, a_five), snap("b", 0.20, b_five)], disabled=disabled)


def test_an_account_whose_other_limit_is_unknown_is_a_condition_not_a_restriction():
    """A's 5-hour window is not known now: whether A can run is unknown. B runs out at +4h; that is no confirmed
    outage, the 0.40 on A is said as unknown, and A is never "restricted with no reported end"."""
    state = _unknown_pair()
    for horizon in ("24h", "7d"):
        sc = _window(state, _week(state), horizon)["scenarios"]["typical"]
        v = sc["verdict"]
        assert sc["status"] == "ok" and v["state"] == "unknown_gap", v
        assert v["empty_at"] == NOW + 4 * H and v["resumes_at"] == NOW + 60 * H, v
        assert v["unknown_at_empty"] == 0.4 and v["locked_at_empty"] is None, v
        assert (v["unknown_accounts"], v["held_accounts"], v["blocked_accounts"]) == (1, 0, 0), v
        assert sc["unknown_accounts"] == 1 and sc["runs_out"][0]["unknown"], sc["runs_out"]
        assert not sc["runs_out"][0].get("blocked_only"), sc["runs_out"]
        assert math.isclose(qf.eval_polyline(sc["line"], NOW + 5 * H), 0.4, abs_tol=1e-4), sc["line"]


@pytest.mark.parametrize("reset_in", [2 * H, 5 * H])
def test_a_refill_of_an_account_of_unknown_availability_makes_the_stop_a_condition(reset_in):
    """A can run, 0.20 left, reset at +10h; B is at its limit, its other limit not known now, reset at +2h (the very
    instant A runs out) or +5h (inside the stop). Once B refills it may be able to run: the stop to +10h is a
    condition, never a confirmed outage. The amount before the first reset stays exact, and B's refill is counted
    at the stop's start only when it lands at that instant."""
    a, b = _member(0.20, NOW + 10 * H, "a"), _member(0.0, NOW + reset_in, "b")
    for horizon, span in (("24h", DAY), ("7d", 7 * DAY)):
        sc = qf.build_typical_scenario([a, b], NOW, NOW + span, horizon, OK, unknown=[False, True])
        v = sc["verdict"]
        assert v["state"] == "unknown_gap" and v["empty_at"] == NOW + 2 * H and v["resumes_at"] == NOW + 10 * H, v
        assert v["first_reset_at"] == NOW + reset_in and v["before_first_reset"] == 0.0, v
        assert v["after_first_reset"] == 1.0 and v["locked_at_empty"] is None, v
        assert v["unknown_at_empty"] == (1.0 if reset_in == 2 * H else None), v
        assert v["unknown_accounts"] == 1 and v["held_accounts"] == 0 and v["blocked_accounts"] == 0, v
        assert sc["runs_out"][0]["at"] == NOW + 2 * H and sc["runs_out"][0]["unknown"], sc["runs_out"]
    sim = qf.simulate_pool([(0.20, NOW + 10 * H, None), (0.0, NOW + reset_in, None)], NOW, NOW + DAY, 0.10,
                           unknown=[False, True])
    first = sim["stops"][0]
    assert first["conditional"] and first["resumes"] == NOW + 10 * H, first
    # Nothing unknown refills inside a stop: it stays confirmed (B can run, held to +5h by a known limit).
    sim = qf.simulate_pool([(0.20, NOW + 10 * H, None), (0.0, NOW + 2 * H, NOW + 5 * H)], NOW, NOW + DAY, 0.10)
    assert not sim["stops"][0]["conditional"], sim["stops"][0]


def test_every_account_unknown_stays_held_and_a_known_endless_hold_stays_a_hold():
    state = _unknown_pair(a_five="stale", b_five="stale")
    sc = _window(state, _week(state))["scenarios"]["typical"]
    assert sc["status"] == "held" and sc["held_reason"] == "window_unknown" and sc["line"] == [], sc
    assert sc["verdict"]["state"] == "held" and sc["verdict"]["unknown_accounts"] == 2, sc["verdict"]
    # A is disabled as well: what is known holds it for good, whatever else is unknown.
    state = _unknown_pair(disabled=("a",))
    v = _window(state, _week(state))["scenarios"]["typical"]["verdict"]
    assert v["state"] == "blocked_gap" and v["locked_at_empty"] == 0.4 and v["unknown_at_empty"] is None, v
    assert (v["held_accounts"], v["unknown_accounts"]) == (1, 0), v
    # Mixed held pool (2 disabled + 1 with stale 5h window): never "restricted" as one blanket reason.
    snaps = [
        _snap("a", [_con("primary", 0.5, WEEK, NOW + 60 * H, "Weekly", freshness="fresh"),
                    _con("five_hour", 0.1, FIVE_H, NOW + 4 * H, "5 hour", freshness="fresh")], NOW),
        _snap("b", [_con("primary", 0.5, WEEK, NOW + 60 * H, "Weekly", freshness="fresh"),
                    _con("five_hour", 0.1, FIVE_H, NOW + 4 * H, "5 hour", freshness="fresh")], NOW),
        _snap("c", [_con("primary", 0.5, WEEK, NOW + 60 * H, "Weekly", freshness="fresh"),
                    _con("five_hour", 0.1, FIVE_H, NOW + 4 * H, "5 hour", freshness="stale")], NOW),
    ]
    mixed = _state(snaps, disabled=("a", "b"))
    sc = _window(mixed, _week(mixed))["scenarios"]["typical"]
    assert sc["status"] == "held" and sc["held_reason"] == "mixed", sc
    assert (sc["verdict"]["held_accounts"], sc["verdict"]["unknown_accounts"]) == (2, 1), sc["verdict"]


def test_a_known_cooldown_outlasting_a_stop_stays_blocked_despite_an_unread_sibling_window():
    """A (0.20 left, runnable) empties at +2h and resets at +5h; B (0.40 left) is cooling until +8h AND has a stale
    5-hour reading. During +2h..+5h B is provably blocked by the +8h cooldown, so the stop is blocked_gap, not
    unknown_gap. If A instead resets at +10h (after +8h), B's cooldown lifts during the stop and makes it conditional."""
    def state_for(a_reset):
        return _state([
            _snap("a", [_con("primary", 0.80, WEEK, a_reset, "Weekly", freshness="fresh"),
                        _con("five_hour", 0.10, FIVE_H, NOW + 4 * H, "5 hour", freshness="fresh")], NOW),
            _snap("b", [_con("primary", 0.60, WEEK, NOW + 60 * H, "Weekly", freshness="fresh"),
                        _con("five_hour", 0.10, FIVE_H, NOW + 4 * H, "5 hour", freshness="stale")], NOW,
                  availability={"state": "cooldown", "resets_at": qs.iso(NOW + 8 * H)}),
        ], rate=0.10)

    s5 = state_for(NOW + 5 * H)
    sc5 = _window(s5, _week(s5))["scenarios"]["typical"]
    v5 = sc5["verdict"]
    assert v5["state"] == "blocked_gap" and v5["empty_at"] == NOW + 2 * H and v5["resumes_at"] == NOW + 5 * H, v5
    assert v5["locked_at_empty"] == 0.4 and v5["unknown_at_empty"] is None, v5
    assert sc5["runs_out"][0].get("blocked_only") and not sc5["runs_out"][0].get("unknown"), sc5["runs_out"]

    s10 = state_for(NOW + 10 * H)
    sc10 = _window(s10, _week(s10))["scenarios"]["typical"]
    v10 = sc10["verdict"]
    assert v10["state"] == "unknown_gap" and v10["empty_at"] == NOW + 2 * H and v10["resumes_at"] == NOW + 10 * H, v10
    assert v10["locked_at_empty"] == 0.4 and v10["unknown_at_empty"] is None and v10["unknown_refilled"] is False, v10
    assert sc10["runs_out"][0].get("unknown_later") is True, sc10["runs_out"]

    # Same class via a fresh exhausted model-scoped sibling window (+8h) alongside a stale 5h window:
    # for model="m-a", B provably cannot run before +8h (+2h..+5h is blocked_gap; +2h..+10h turns unknown_gap after +8h).
    def scoped_sibling_state(a_reset):
        return _state([
            _snap("a", [_con("primary", 0.80, WEEK, a_reset, "Weekly", freshness="fresh")], NOW),
            _snap("b", [_con("primary", 0.60, WEEK, NOW + 60 * H, "Weekly", freshness="fresh"),
                        _con("five_hour", 0.10, FIVE_H, NOW + 4 * H, "5 hour", freshness="stale"),
                        _con("daily_ma", 1.0, DAY, NOW + 8 * H, "Daily m-a", models=["m-a"], freshness="fresh")], NOW),
        ], rate=0.10)

    m5 = scoped_sibling_state(NOW + 5 * H)
    scm5 = _window(m5, _week(m5), model="m-a")["scenarios"]["typical"]
    vm5 = scm5["verdict"]
    assert vm5["state"] == "blocked_gap" and vm5["empty_at"] == NOW + 2 * H and vm5["resumes_at"] == NOW + 5 * H, vm5
    assert vm5["locked_at_empty"] == 0.4 and vm5["unknown_at_empty"] is None, vm5
    assert scm5["runs_out"][0].get("blocked_only") and not scm5["runs_out"][0].get("unknown"), scm5["runs_out"]
    assert qa.future_table(_window(m5, _week(m5), model="m-a")["scenarios"], NOW)[0]["scenario_typical"] == 0.6

    m10 = scoped_sibling_state(NOW + 10 * H)
    scm10 = _window(m10, _week(m10), model="m-a")["scenarios"]["typical"]
    vm10 = scm10["verdict"]
    assert vm10["state"] == "unknown_gap" and vm10["empty_at"] == NOW + 2 * H and vm10["resumes_at"] == NOW + 10 * H, vm10
    assert vm10["locked_at_empty"] == 0.4 and vm10["unknown_at_empty"] is None and vm10["unknown_refilled"] is False, vm10
    assert scm10["runs_out"][0].get("unknown_later") is True, scm10["runs_out"]
    # Unspecified model is not bound by daily_ma, so B's stale five_hour makes +2h..+5h conditional right away.
    v_unspec = _window(m5, _week(m5), model=None)["scenarios"]["typical"]["verdict"]
    assert v_unspec["state"] == "unknown_gap" and v_unspec["unknown_at_empty"] == 0.4, v_unspec


def test_single_untimed_limit_keeps_typical_when_applicable_while_combined_requires_comparable_units():
    """A governing limit without window_seconds still gets its Typical forecast in _window_view when its scope applies,
    stays hidden when its scope is unreadable, and prevents Combined when paired with another governing limit."""
    untimed = _state([_snap("a", [_con("shared_cap", 0.30, None, NOW + 5 * H, "Shared cap")], NOW)], rate=0.01)
    built = qa.build(untimed, untimed.groups[0], "24h")
    assert built["window"]["scope_applies"] is True
    assert built["window"]["scenarios"]["typical"]["status"] == "ok" and built["window"]["scenarios"]["typical"]["line"]
    assert built["joint"]["available"] is False and built["joint"]["reason"] == "one_window"

    paired = _state([_snap("a", [_con("shared_cap", 0.30, None, NOW + 5 * H, "Shared cap"),
                                 _con("primary", 0.20, WEEK, NOW + 60 * H, "Weekly")], NOW)], rate=0.01)
    both = qa.build(paired, _week(paired), "24h")
    assert both["window"]["scope_applies"] is True and both["window"]["scenarios"]["typical"]["status"] == "ok"
    assert both["joint"]["available"] is False and both["joint"]["reason"] == "incomparable_units"

    unread = _state([_snap("a", [_con("odd_cap", 0.30, None, NOW + 5 * H, "Odd cap",
                                      applies_to_models="unreadable")], NOW)], rate=0.01)
    held = qa.build(unread, unread.groups[0], "24h")
    assert held["window"]["scope_applies"] is None
    assert held["window"]["scenarios"]["typical"]["status"] == "held"
    assert held["window"]["scenarios"]["typical"]["held_reason"] == "scope_unknown"


# ---------------------------------------------------------------------------
# holds of the request


def _shared_with(extra, availability=None):
    """One account: the shared week has 0.30 left (reset +60h), plus ``extra`` constraints; pace 0.10/h."""
    return _state([_snap("a", [_con("seven_day", 0.70, WEEK, NOW + 60 * H, "7 day")] + extra, NOW, availability)],
                  rate=0.10)


@pytest.mark.parametrize("hold", ["cooldown", "exhaustion"])
def test_a_models_hold_binds_its_requests_without_a_window_of_that_model(hold):
    """model-a is cooling down (a constraint of model-a with only a cooldown) or reported out (an exhaustion of a
    model-a limit no window reports) until +24h. A model-a request cannot use the shared week before then; a model-b
    request, a request naming no model and the plain chart are not held."""
    until = NOW + 24 * H
    if hold == "cooldown":
        state = _shared_with([{"id": "alpha", "label": "alpha", "applies_to_models": ["model-a"],
                               "cooldown_until": qs.iso(until)}])
    else:
        state = _shared_with([], {"state": "available", "model_scoped_exhaustions": [
            {"constraint_id": "alpha_week", "applies_to_models": ["model-a"], "resets_at": qs.iso(until)}]})
    shared = _week(state)
    v = _window(state, shared, model="model-a")["scenarios"]["typical"]["verdict"]
    assert (v["earlier_empty_at"], v["earlier_resumes_at"]) == (round(NOW), until), v
    assert v["state"] == "runs_out" and v["empty_at"] == NOW + 27 * H and v["resumes_at"] == NOW + 60 * H, v
    for model in ("model-b", None):
        v = _window(state, shared, model=model)["scenarios"]["typical"]["verdict"]
        assert v["state"] == "runs_out" and v["empty_at"] == NOW + 3 * H and v["earlier_empty_at"] is None, (model, v)
    assert qs.build_chart(state, shared.key, "codex", "24h")["scenarios"]["typical"]["verdict"]["empty_at"] == NOW + 3 * H


def test_of_two_holds_of_one_scope_the_later_end_holds():
    """Two whole-account cooldowns, to +2h and to +6h: the account runs again at +6h, never at +2h. The snapshot's
    validity bound is still the earliest end of the evidence. A cooldown with no end beside one with an end holds
    for good."""
    first, second = ({"id": f"c{i}", "label": f"c{i}", "cooldown_until": qs.iso(NOW + h * H)} for i, h in ((1, 2), (2, 6)))
    state = _shared_with([first, second])
    member = _week(state).measured[0]
    assert member.hold_until == NOW + 6 * H and member.restriction_boundary == NOW + 2 * H, member
    v = _window(state, _week(state))["scenarios"]["typical"]["verdict"]
    assert (v["earlier_empty_at"], v["earlier_resumes_at"], v["empty_at"]) == (round(NOW), NOW + 6 * H, NOW + 9 * H), v
    state = _shared_with([first, {"id": "c3", "label": "c3", "cooldown_until": "not a time"}])
    member = _week(state).measured[0]
    assert member.hold_until == math.inf, member.hold_until
    sc = qs.build_chart(state, _week(state).key, "codex", "24h")["scenarios"]["typical"]
    assert sc["status"] == "held" and sc["held_reason"] == "restricted", sc


# ---------------------------------------------------------------------------
# the selected window's own resets


def test_the_selected_window_marks_its_own_resets_only():
    """The weekly limit's No new use, with a 5-hour window beside it: the 5-hour refill at +3h gives the week
    nothing and is no reset of it. On 24 h there is none inside; the next one is named past the span."""
    state = _state([_snap("a", [_con("primary", 0.40, WEEK, NOW + 60 * H, "Weekly"),
                                _con("five_hour", 0.50, FIVE_H, NOW + 3 * H, "5 hour")], NOW)])
    week = _week(state)
    nnu = _window(state, week, "7d")["scenarios"]["no_new_use"]
    assert [(r["at"], r["adds"]) for r in nnu["schedule"]] == [(NOW + 60 * H, 0.4)], nnu["schedule"]
    nnu = _window(state, week, "24h")["scenarios"]["no_new_use"]
    assert nnu["schedule"] == [] and nnu["later"]["at"] == NOW + 60 * H, nnu
    assert nnu["no_reset"] == 0, nnu


# ---------------------------------------------------------------------------
# the widget says it (fake DOM over views built by the skill's own routes)


def _now():
    return float(int(time.time() // 3600) * 3600 + 600)  # a whole hour + 10 min: the 24 h end is not an axis hour


def _route_views(folder, snaps_at, info, disabled=()):
    """The weekly limit's view on each span from three synthetic sweeps, with the given 48-hour estimate."""
    now = _now()
    store = quota_history.HistoryStore(folder)
    status = None
    for t in (now - 600.0, now - 300.0, now):
        snaps = snaps_at(t, now)
        status = payload(snaps, [_profile(s["subject"]["subject_id"], s["subject"]["subject_id"] not in disabled)
                                 for s in snaps], harnesses=("codex",))
        plugin.persist_sweep(store, status, "", t)
    views = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(qf, "estimate_typical_pace", lambda calc, state: dict(info))
        for horizon in ("24h", "7d"):
            view = plugin.build_view(status, "")
            first = plugin.reserve_view(store, status, now, now, harness="codex", horizon=horizon, name_accounts=True)
            week = next(g["key"] for g in first["summary"]["groups"] if g["window_seconds"] == WEEK)
            view["reserve"] = plugin.reserve_view(store, status, now, now, harness="codex", group=week,
                                                  horizon=horizon, name_accounts=True, analytics=True)
            views[horizon] = view
    return views


def _rate(value):
    return {"state": "ok", "reason": "", "rate_per_hour": value, "valid_seconds": 48 * 3600, "valid_hours": 48.0,
            "observed_burn": value * 48, "window_hours": 48, "min_valid_hours": 12}


def _joint_views(folder):
    """Combined over 16 synthetic hours: two accounts whose weekly and 30-day windows both rise, so each is
    calibrated and counted; the weekly reset is three days ahead, the monthly ten, both past the 24 h span."""
    now = _now()
    saved = {name: getattr(rp, name) for name in ("NOW", "START", "WEEK_RESET", "CUT", "STEP")}
    rp.NOW, rp.START, rp.WEEK_RESET, rp.CUT, rp.STEP = now, now - 16 * rp.H, now + 3 * DAY, now - 600, 300.0
    try:
        def both(sid, week_base, month_base):
            week, month = rp.rising(week_base, 0.02), rp.rising(month_base, 0.005)
            return lambda t: [rp.snapshot(sid, t, [rp.limit(week(t)), rp.limit(month(t), rp.MONTH, reset=now + 10 * DAY)])]
        accounts = {"a": both("a", 0.15, 0.10), "b": both("b", 0.25, 0.20)}
        store = rp.feed(folder, accounts)
        status = rp.status_at(now, accounts)
        views = {}
        for horizon in ("24h", "7d"):
            view = plugin.build_view(status, "")
            view["reserve"] = plugin.reserve_view(store, status, now, now, harness="codex", group=rp.WEEK_KEY,
                                                  horizon=horizon, name_accounts=True, analytics=True)
            views[horizon] = view
    finally:
        for name, value in saved.items():
            setattr(rp, name, value)
    joint = views["24h"]["reserve"]["chart"]["analytics"]["joint"]
    assert joint["available"] and joint["scenarios"]["no_new_use"]["schedule"] == [], joint.get("scenarios")
    assert joint["scenarios"]["no_new_use"]["later"]["at"] == round(now + 3 * DAY), joint["scenarios"]["no_new_use"]
    return views


@pytest.fixture(scope="module")
def fixture_file(tmp_path_factory):
    root = tmp_path_factory.mktemp("typical-stops")

    def week(sid, left, reset, t, five=None):
        rows = [_con("primary", round(1 - left, 6), WEEK, reset, "Weekly")]
        if five is not None:
            rows.append(_con("five_hour", five[0], FIVE_H, five[1], "5 hour"))
        return _snap(sid, rows, t)

    def explicit(snap, stale=()):
        for row in snap["constraints"]:
            row["freshness"] = "stale" if row["id"] in stale else "fresh"
        return snap

    fixtures = {
        # A out at +2h; B held one minute more by its 5-hour window, then out from +7h01m to its reset at +20h.
        "masked": _route_views(root / "masked", lambda t, now: [
            week("acct-a", 0.20, None, t, (0.10, now + 4 * H)),
            week("acct-b", 0.50, now + 20 * H, t, (1.0, now + 2 * H + 60))], _rate(0.10)),
        # A out at +2h, the instant B's week resets; B's 5-hour window holds B until +5h.
        "refill": _route_views(root / "refill", lambda t, now: [
            week("acct-a", 0.20, None, t, (0.10, now + 4 * H)),
            week("acct-b", 0.30, now + 2 * H, t, (1.0, now + 5 * H))], _rate(0.10)),
        # A's 5-hour window is not known now; B runs out at +4h.
        "unknown": _route_views(root / "unknown", lambda t, now: [
            explicit(week("acct-a", 0.40, now + 60 * H, t, (0.10, now + 4 * H)), stale=("five_hour",)),
            explicit(week("acct-b", 0.20, now + 60 * H, t, (0.10, now + 4 * H)))], _rate(0.05)),
        # Just under the 12-hour minimum, published rounded to 12.0.
        "insufficient": _route_views(root / "insufficient", lambda t, now: [week("acct-a", 0.60, now + 30 * H, t)], {
            "state": "insufficient", "reason": "few_hours", "rate_per_hour": None, "valid_seconds": 43200,
            "valid_hours": 12.0, "observed_burn": 0.1, "window_hours": 48, "min_valid_hours": 12}),
        # A out at +2h, the instant B — at its limit, its 5-hour window not known now — refills.
        "coincident": _route_views(root / "coincident", lambda t, now: [
            explicit(week("acct-a", 0.20, now + 10 * H, t, (0.10, now + 4 * H))),
            explicit(week("acct-b", 0.0, now + 2 * H, t, (0.10, now + 4 * H)), stale=("five_hour",))], _rate(0.10)),
        # A out at +2h with the whole pool empty; B (5-hour not known now) refills later at +5h before A's +10h reset.
        "late_unknown": _route_views(root / "late_unknown", lambda t, now: [
            explicit(week("acct-a", 0.20, now + 10 * H, t, (0.10, now + 4 * H))),
            explicit(week("acct-b", 0.0, now + 5 * H, t, (0.10, now + 4 * H)), stale=("five_hour",))], _rate(0.10)),
        # Mixed held pool: 2 disabled accounts + 1 active account with a stale 5-hour window.
        "mixed_held": _route_views(root / "mixed_held", lambda t, now: [
            explicit(week("acct-a", 0.50, now + 60 * H, t, (0.10, now + 4 * H))),
            explicit(week("acct-b", 0.50, now + 60 * H, t, (0.10, now + 4 * H))),
            explicit(week("acct-c", 0.50, now + 60 * H, t, (0.10, now + 4 * H)), stale=("five_hour",))],
            _rate(0.05), disabled=("acct-a", "acct-b")),
        # A little reserve: 0.004 left.
        "tiny": _route_views(root / "tiny", lambda t, now: [week("acct-a", 0.004, now + 30 * H, t)], _rate(0.0001)),
        # 0.50 left, refilled at +1h, out at +11h with no reset ahead: one stop, not "again".
        "refilled": _route_views(root / "refilled", lambda t, now: [week("acct-a", 0.50, now + H, t)], _rate(0.10)),
        "joint": _joint_views(root / "joint"),
    }
    path = root / "views.json"
    path.write_text(json.dumps(fixtures), encoding="utf-8")
    return str(path)


NODE = r"""
const text = (n) => (n ? n.textContent : '');
const cls = (n) => String((n && n.getAttribute('class')) || n && n.className || '');
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
const block = (env) => classes(env.root, 'tl-block')[0];
const strip = (env) => classes(block(env), 'tl-verdict')[0];
const head = (env) => classes(strip(env), 'tl-verdict-head')[0];
const figs = (env) => classes(strip(env), 'tl-vfig').map(text);
const marks = (env) => walk(block(env)).filter((n) => /deplete-dot/.test(cls(n)));
const facts = (env) => text(classes(block(env), 'tl-facts')[0]);
(async () => {
  const fx = JSON.parse(require('fs').readFileSync(process.env.STOPS_FIXTURE, 'utf8'));
  const open = async (name) => {
    const env = await boot((url) => String(url).indexOf('horizon=24h') >= 0 ? fx[name]['24h'] : fx[name]['7d']);
    await settle();
    return env;
  };

  // The stop on at the first reset is said; the minute before it is the earlier stop. Its mark stands on the line.
  let env = await open('masked');
  assert.match(text(head(env)), /^Weekly runs out 12h 59m before the \d\d:\d\d reset$/, text(head(env)));
  assert.ok(cls(head(env)).split(/\s+/).includes('bad'), cls(head(env)));
  assert.match(text(strip(env)), /earlier stop ~\d\d:\d\d to ~\d\d:\d\d/, text(strip(env)));
  assert.ok(figs(env).some((f) => /^Before \d\d:\d\d0\.00 win$/.test(f)), figs(env).join(' | '));
  assert.equal(marks(env).length, 1);
  // Details: all simulated stops in the span are listed once each, subsequent ones as "again".
  assert.match(facts(env), /usable reserve runs out while restricted accounts hold the rest \([^)]*\); the pool runs out again \([^)]*\); the pool runs out again after refilling \(/, facts(env));
  assert.doesNotMatch(facts(env), /before the next reported reset/, facts(env));
  const week = text(head(env));
  click(env, 'horizon:24h');
  await settle();
  assert.equal(text(head(env)), week, 'one future on both spans');
  assert.doesNotMatch(facts(env), /after refilling/, facts(env));
  env.disposeHooks[0]();

  // A stop that begins at a refill landing on an account that cannot run: from when to when, and the reset's figures.
  env = await open('refill');
  assert.match(text(head(env)), /^No usable Weekly from ~\d\d:\d\d to ~\d\d:\d\d$/, text(head(env)));
  const f = figs(env);
  assert.ok(f.some((x) => x.startsWith('Usable empty')) && f.some((x) => x.startsWith('Resumes')), f.join(' | '));
  assert.ok(f.some((x) => /^Before \d\d:\d\d0\.30 win$/.test(x)), f.join(' | '));
  assert.equal(marks(env).length, 1);
  env.disposeHooks[0]();

  // An account of unknown availability: a condition, said with a warning, never a restriction or "no usable".
  env = await open('unknown');
  assert.match(text(head(env)), /^Known usable Weekly runs out ~\d\d:\d\d$/, text(head(env)));
  const tone = cls(head(env)).split(/\s+/);
  assert.ok(tone.includes('warn') && !tone.includes('bad'), tone.join(' '));
  assert.match(text(strip(env)), /0\.40 win is on accounts whose other limit is not known now/, text(strip(env)));
  assert.doesNotMatch(text(strip(env)), /restricted with no reported end|No usable/, text(strip(env)));
  assert.ok(figs(env).some((x) => x.startsWith('Known usable empty')), figs(env).join(' | '));
  assert.equal(marks(env).length, 1);
  assert.ok(!cls(marks(env)[0]).split(/\s+/).includes('bad'), cls(marks(env)[0]));
  assert.match(facts(env), /accounts whose other limit is not known now hold the rest/, facts(env));
  env.disposeHooks[0]();

  // An account of unknown availability refilled the instant the pool runs out: still a condition, with what it holds.
  env = await open('coincident');
  assert.match(text(head(env)), /^Known usable Weekly runs out ~\d\d:\d\d$/, text(head(env)));
  assert.match(text(strip(env)), /1\.00 win is on accounts whose other limit is not known now/, text(strip(env)));
  assert.ok(figs(env).some((x) => /^Before \d\d:\d\d0\.00 win$/.test(x)), figs(env).join(' | '));
  assert.match(facts(env), /^At typical pace, known usable reserve runs out while accounts whose other limit is not known now hold the rest/, facts(env));
  env.disposeHooks[0]();

  // Unknown account empty at the stop's start (+2h) and refilling later (+5h): first stop never says it held reserve at +2h;
  // at +20h (after A's +10h reset runs out) B's +5h refill IS held at the second stop's start.
  env = await open('late_unknown');
  assert.match(text(strip(env)), /an account whose other limit is not known now refills during this stop/, text(strip(env)));
  const parts = facts(env).split('; ');
  assert.match(parts[0], /^At typical pace, usable reserve runs out \([^)]*\)$/, facts(env));
  assert.doesNotMatch(parts[0], /hold the rest/, facts(env));
  assert.equal(parts[1], 'earlier return depends on an account whose other limit is not known now');
  assert.match(parts[2], /^known usable reserve runs out again while accounts whose other limit is not known now hold the rest \([^)]*\)\.$/, facts(env));
  env.disposeHooks[0]();

  // Mixed held pool (2 disabled + 1 unknown other limit): separate counts, never "Every ... account is restricted/held".
  env = await open('mixed_held');
  assert.equal(text(head(env)), 'No Weekly account is known able to run');
  assert.doesNotMatch(text(block(env)), /Every .* account is restricted/, text(block(env)));
  assert.equal(byFocus(env.root, 'scenario:typical').title, 'No current account is known able to run, so no Typical line is drawn');
  assert.doesNotMatch(byFocus(env.root, 'scenario:typical').title, /Every current account is held/);
  assert.match(text(strip(env)), /2 accounts held with no reported end · 1 account with another limit not known now/, text(strip(env)));
  assert.match(text(classes(block(env), 'chart-legend')[0]), /2 accounts held with no reported end, 1 account with another limit not known now/, text(classes(block(env), 'chart-legend')[0]));
  env.disposeHooks[0]();

  // The words of a conditional stop carry into the earlier note and the Details rows (answers edited to each case).
  const variant = (name, edit) => {
    const copy = JSON.parse(JSON.stringify(fx[name]));
    for (const span of ['24h', '7d']) edit(copy[span].reserve.chart.analytics.window.scenarios.typical, copy[span].reserve.chart.now);
    return copy;
  };
  const openVariant = async (copy) => {
    const env = await boot((url) => String(url).indexOf('horizon=24h') >= 0 ? copy['24h'] : copy['7d']);
    await settle();
    return env;
  };
  env = await openVariant(variant('masked', (typ, now) => {
    typ.verdict.earlier_unknown = true;
  }));
  assert.match(text(strip(env)), /earlier, known usable reserve out ~\d\d:\d\d to ~\d\d:\d\d/, text(strip(env)));
  assert.doesNotMatch(text(strip(env)), /earlier stop/, text(strip(env)));
  env.disposeHooks[0]();
  env = await openVariant(variant('masked', (typ, now) => {
    Object.assign(typ.verdict, { earlier_empty_at: now, earlier_resumes_at: now + 3600, earlier_unknown: true });
    typ.runs_out = [{ at: now + 1800, reset_reported: true, after_refill: true, again: true, unknown: true }];
  }));
  assert.match(text(strip(env)), /known usable reserve out now to ~\d\d:\d\d/, text(strip(env)));
  assert.match(facts(env), /^At typical pace, known usable reserve runs out again while accounts whose other limit is not known now hold the rest \([^)]*\)\.$/, facts(env));
  env.disposeHooks[0]();
  env = await openVariant(variant('masked', (typ, now) => {
    typ.runs_out = [{ at: now + 1800, reset_reported: true, after_refill: false, again: false, blocked_only: true },
                    { at: now + 7200, reset_reported: true, after_refill: false, again: true, unknown: true }];
  }));
  assert.match(facts(env), /restricted accounts hold the rest \([^)]*\); known usable reserve runs out again while accounts whose other limit is not known now hold the rest/, facts(env));
  env.disposeHooks[0]();

  // Just under the minimum: never "12h valid of 12h needed".
  env = await open('insufficient');
  assert.match(text(strip(env)), /11\.9h valid of 12h needed/, text(strip(env)));
  assert.doesNotMatch(text(block(env)), /(?<![\d.])12h valid/, text(strip(env)));
  env.disposeHooks[0]();

  // A little reserve under the cursor, in checkpoint cards, and in the Details table: "<0.01", never 0.00.
  env = await open('tiny');
  click(env, 'horizon:24h');
  await settle();
  const plot = byFocus(env.root, 'chart-plot');
  plot.listeners.focus[0]();
  const values = classes(env.root, 'tip-val').map(text);
  assert.ok(values.length >= 1 && values.every((v) => v === '<0.01'), values.join(' | '));
  assert.match(text(classes(env.root, 'chart-readout')[0]), /<0\.01 of/, text(classes(env.root, 'chart-readout')[0]));
  const cpNums = classes(block(env), 'cp-num').map(text);
  assert.ok(cpNums.length >= 1 && cpNums.every((v) => v === '<0.01'), cpNums.join(' | '));
  const tbls = classes(block(env), 'tbl');
  const dataTbl = tbls[tbls.length - 1];
  assert.match(text(dataTbl), /<0\.01/, text(dataTbl));
  assert.doesNotMatch(text(dataTbl), /(?<![\d.])0\.00(?!\s+of)/, text(dataTbl));
  env.disposeHooks[0]();

  // The first stop after a refill, with none before it: said once, never "again".
  env = await open('refilled');
  assert.match(facts(env), /^At typical pace, the pool runs out with no reported reset \([^)]*\)\.$/, facts(env));
  env.disposeHooks[0]();

  // Combined on 24 h names the next known reset although the span ends before it.
  env = await open('joint');
  click(env, 'horizon:24h');
  await settle();
  click(env, 'graph:joint');
  await settle();
  const next = classes(block(env), 'tl-next-v')[0];
  assert.ok(next && !cls(next).split(/\s+/).includes('dim'), cls(next));
  assert.notEqual(text(next), 'none reported');
  env.disposeHooks[0]();
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
"""


def test_the_widget_says_each_stop_once_with_its_condition(fixture_file):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run([str(node), "-e", harness + NODE], cwd=HERE,
                            env={**os.environ, "WIDGET_PATH": str((HERE / "widget.js").resolve()),
                                 "STOPS_FIXTURE": fixture_file},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
