"""Recent pace of one window: an account is held only by the binding windows it reports.

0.13.0 held every account of a per-window pace future by every binding window of its
family, so one account's stale 30-day window held all the weekly accounts and Recent
pace drew the No new use line beside measured paces (14 Nov 2026). Here synthetic
accounts go through the real collector path (persist_sweep) and the real route path
(reserve_view with analytics, as the widget's chart read); every expected figure is
derived from the accounts' own inputs in the test. Nothing here reads a real account.
"""
import pytest

import plugin
import quota_history
import quota_summary as qs
from test_joint_view import fed_store, joint_of
from test_reserve import FIVE_H, NOW, WEEK, payload, profile

H = 3600.0
MONTH = 30 * 86400
STEP = 120.0
START = NOW - 1.5 * H
WEEK_KEY = "codex|primary|604800|-"
MONTH_KEY = "codex|primary|2592000|-"
WEEK_RESET = NOW + 3 * 86400
A_RATE, B_RATE = 0.02, 0.04
CUT = NOW - 600  # a sweep of the collector's grid ten minutes before now


def limit(used, window=WEEK, reset="week", cid="primary", **extra):
    reset = WEEK_RESET if reset == "week" else reset  # read when called: a widget test moves the clock
    row = {"id": cid, "label": cid, "used_ratio": used, "window_seconds": window,
           "resets_at": None if reset is None else qs.iso(reset)}
    row.update(extra)
    return row


def snapshot(sid, t, constraints, *, harness="codex", fresh=True, source="app", observed=None, plan=None):
    return {"subject": {"harness": harness, "subject_id": sid, "plan_label": plan},
            "constraints": constraints, "availability": {"state": "available"},
            "observed_at": qs.iso(t - 20 if observed is None else observed),
            "freshness": "fresh" if fresh else "stale", "source": source}


def rising(base, per_hour):
    return lambda t: base + per_hour * (t - START) / H


def weekly(sid, used):
    return lambda t: [snapshot(sid, t, [limit(used(t) if callable(used) else used)])]


def z_stale_month(t):
    """The owner's case: one account still reports a stale 30-day window (and no weekly one)."""
    return [snapshot("z", t, [limit(0.0, MONTH, reset=None)], fresh=False, source="rollout",
                     observed=NOW - 2 * 86400)]


def codex_accounts(**override):
    """a and b rise (measured positive paces), c is measured with no growth, d is at the
    limit with no growth; z reports only its own stale 30-day window."""
    accounts = {"a": weekly("a", rising(0.50, A_RATE)), "b": weekly("b", rising(0.30, B_RATE)),
                "c": weekly("c", 0.60), "d": weekly("d", 1.0), "z": z_stale_month}
    accounts.update(override)
    return {sid: make for sid, make in accounts.items() if make is not None}


def status_at(t, accounts, harness="codex"):
    return payload([row for make in accounts.values() for row in make(t)],
                   [profile(harness, sid) for sid in accounts], harnesses=(harness,))


def feed(folder, accounts, *, start=None, fail=(), harness="codex"):
    folder.mkdir(parents=True, exist_ok=True)
    store = quota_history.HistoryStore(folder)
    t = START if start is None else start
    while t <= NOW + 1e-6:
        if any(abs(t - when) < 1 for when in fail):
            plugin.persist_sweep(store, None, "status_unreadable", t)  # the collector read nothing
        else:
            plugin.persist_sweep(store, status_at(t, accounts, harness), "", t)
        t += STEP
    return store


def chart_of(store, accounts, group, *, harness="codex", horizon="7d", **extra):
    result = plugin.reserve_view(store, status_at(NOW, accounts, harness), NOW, NOW, harness=harness,
                                 group=group, horizon=horizon, analytics=True, **extra)
    return result, result["chart"]["analytics"]


def value_at(line, t):
    """The future line at ``t`` as the widget draws it: linear between vertices."""
    for (t0, v0), (t1, v1) in zip(line, line[1:]):
        if t0 <= t <= t1 and t1 > t0:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    raise AssertionError(f"{t} is outside the line")


def split(sc):
    return {k: sc[k] for k in ("accounts", "at_pace", "held", "zero_growth", "pace_held", "pace_unknown")}


def group(result, window, meaning="primary"):
    found = [g for g in result["summary"]["groups"] if g["window_seconds"] == window and g["meaning"] == meaning]
    assert len(found) == 1, [g["key"] for g in result["summary"]["groups"]]
    return found[0]


START_LEFT = 0.47 + 0.64 + 0.40 + 0.0  # a, b, c, d at now


# ---------------------------------------------------------------------------
# The owner's case


def test_measured_paces_drain_beside_another_accounts_stale_30_day_window(tmp_path):
    accounts = codex_accounts()
    store = feed(tmp_path, accounts)
    result, analytics = chart_of(store, accounts, WEEK_KEY)
    week, month = group(result, WEEK), group(result, MONTH)
    assert (week["key"], month["key"]) == (WEEK_KEY, MONTH_KEY)
    # z alone has the 30-day window, as a stale reading; no weekly account reports it at all
    assert month["measured"]["accounts"] == 0 and month["slots"] == 1
    pace = week["recent_pace"]
    assert (pace["accounts_known"], pace["of"], pace["zero_growth"]) == (4, 4, 2)
    assert pace["windows_per_hour"] == pytest.approx(A_RATE + B_RATE, abs=1e-6)
    assert analytics["scope"]["unreadable_windows"] == 0
    window = analytics["window"]
    assert window["key"] == WEEK_KEY and window["scope_applies"] is True
    assert window["held_to_other_windows"] == 0 and window["holds"] == {}
    sc = window["scenarios"]["recent_pace"]
    assert split(sc) == {"accounts": 4, "at_pace": 2, "held": 2, "zero_growth": 2, "pace_held": 0, "pace_unknown": 0}
    # a: .47 left at .02/h, b: .64 at .04/h; c (.40) and d (0) keep their share at their measured zero
    assert sc["line"][0][1] == pytest.approx(START_LEFT, abs=1e-4)
    assert value_at(sc["line"], NOW + H) == pytest.approx(START_LEFT - A_RATE - B_RATE, abs=1e-4)
    # b runs out at +16 h, a at +23.5 h, both before their reported reset (+72 h); no later reset is assumed
    assert value_at(sc["line"], NOW + 16 * H) == pytest.approx(0.47 - 16 * A_RATE + 0.40, abs=1e-3)
    assert value_at(sc["line"], NOW + 30 * H) == pytest.approx(0.40, abs=1e-3)
    before = sorted(r["at"] for r in sc["runs_out"] if not r["after_refill"])
    assert before == [round(NOW + 16 * H), round(NOW + 23.5 * H)]
    # No new use is untouched: every account keeps its share until its reset
    keep = window["scenarios"]["no_new_use"]
    assert value_at(keep["line"], NOW + 30 * H) == pytest.approx(START_LEFT, abs=1e-4)
    assert (keep["at_pace"], keep["held"], keep["zero_growth"]) == (0, 4, 0) and "pace_held" not in keep


def test_combined_never_adds_a_weekly_share_to_a_monthly_one_and_says_unavailable_not_zero(tmp_path):
    accounts = codex_accounts()
    _result, analytics = chart_of(feed(tmp_path / "with-d", accounts), accounts, WEEK_KEY)
    joint = analytics["joint"]
    assert [w["window_seconds"] for w in joint["windows"]] == [WEEK, MONTH]
    assert joint["unit"]["window_seconds"] == WEEK
    # No account has both windows: nobody is calibrated. d's exhausted weekly window is a known
    # zero; a, b, c (weekly only) and z (30-day only) are left out — their shares are never summed.
    assert (joint["known"], joint["accounts"], joint["figure"]) == (1, 5, 0.0)
    assert joint["unknown_reasons"] == {"window_missing": 4}
    assert joint["calibration"]["calibrated"] == 0
    # Without the exhausted account Combined has no figure at all: unavailable, never 0.
    accounts = codex_accounts(d=None)
    _result, analytics = chart_of(feed(tmp_path / "no-d", accounts), accounts, WEEK_KEY)
    joint = analytics["joint"]
    assert (joint["known"], joint["figure"], joint["scenarios"]) == (0, None, None)
    assert joint["unknown_reasons"] == {"window_missing": 4}
    assert joint["hours"]["value"] is None
    # ... while the selected weekly window still drains the measured paces
    assert analytics["window"]["scenarios"]["recent_pace"]["at_pace"] == 2


# ---------------------------------------------------------------------------
# An account's own windows still hold it


def own_month(kind):
    """a's own 30-day window: a stale reading, an unreadable one, or one known only from its kept history."""
    rise = rising(0.50, A_RATE)
    if kind == "stale":
        return lambda t: [snapshot("a", t, [limit(rise(t))]),
                          snapshot("a", t, [limit(0.1, MONTH, reset=NOW + 10 * 86400)], fresh=False,
                                   source="rollout", observed=NOW - 86400)]
    if kind == "unreadable":
        return lambda t: [snapshot("a", t, [limit(rise(t)), limit("n/a", MONTH, reset=NOW + 10 * 86400)])]
    # history_only: read fresh in the first twenty minutes, absent from every later answer
    return lambda t: [snapshot("a", t, [limit(rise(t))] + (
        [limit(0.1, MONTH, reset=NOW + 10 * 86400)] if t < START + 1200 else []))]


@pytest.mark.parametrize("kind,status", [("stale", "stale_only"), ("unreadable", "invalid"),
                                         ("history_only", "history_only")])
def test_an_accounts_own_unknown_window_still_holds_its_measured_pace(tmp_path, kind, status):
    accounts = codex_accounts(a=own_month(kind))
    result, analytics = chart_of(feed(tmp_path, accounts), accounts, WEEK_KEY)
    month = group(result, MONTH)
    assert month["slots"] == 2 and month["measured"]["accounts"] == 0  # a and z
    assert analytics["window"]["holds"] == {"window_unknown": 1}
    assert analytics["window"]["held_to_other_windows"] == 1
    sc = analytics["window"]["scenarios"]["recent_pace"]
    # a's own pace is measured (.02/h) but its own 30-day window is not known now: held, said apart
    assert split(sc) == {"accounts": 4, "at_pace": 1, "held": 3, "zero_growth": 2, "pace_held": 1, "pace_unknown": 0}
    assert value_at(sc["line"], NOW + H) == pytest.approx(START_LEFT - B_RATE, abs=1e-4)
    assert value_at(sc["line"], NOW + 30 * H) == pytest.approx(0.47 + 0.40, abs=1e-3)
    if kind == "history_only":  # a's from its kept history, z's stale reading from this answer
        assert month["last_known"]["origins"] == {"history": 1, "payload": 1}


def claude_accounts():
    """e: its five-hour window reached the limit 30 min ago and resets in 2 h, its weekly window
    rose; f reports no five-hour window at all and its weekly window rises."""
    return {
        "e": lambda t: [snapshot("e", t, [
            limit(min(1.0, 0.6 + 0.4 * (t - START) / H), FIVE_H, reset=NOW + 2 * H, cid="five_hour"),
            limit(0.45 + 0.05 * (t - START) / H, WEEK, reset=NOW + 5 * 86400, cid="seven_day")],
            harness="claude")],
        "f": lambda t: [snapshot("f", t, [limit(0.20 + 0.03 * (t - START) / H, WEEK, reset=NOW + 5 * 86400,
                                                cid="seven_day")], harness="claude")],
    }


def test_an_accounts_exhausted_second_window_blocks_until_its_one_reported_reset(tmp_path):
    accounts = claude_accounts()
    store = feed(tmp_path, accounts, harness="claude")
    result, analytics = chart_of(store, accounts, "claude|seven_day|604800|-", harness="claude", horizon="24h")
    five = group(result, FIVE_H, "five_hour")
    r5 = five["recent_pace"]["windows_per_hour"]
    assert five["measured"]["at_limit"] == 1 and r5 > 0.1  # measured while it rose to the limit
    window = analytics["window"]
    assert window["holds"] == {}
    sc = window["scenarios"]["recent_pace"]
    assert split(sc) == {"accounts": 2, "at_pace": 2, "held": 0, "zero_growth": 0, "pace_held": 0, "pace_unknown": 0}
    e0, f0 = 1 - 0.525, 1 - 0.245
    line = sc["line"]
    # e cannot run until its five-hour reset (+2 h): its weekly share stays; f, held by no
    # five-hour window it does not have, drains at its own .03/h from now
    assert value_at(line, NOW + H) == pytest.approx(e0 + f0 - 0.03, abs=1e-3)
    assert value_at(line, NOW + 2 * H) == pytest.approx(e0 + f0 - 0.06, abs=1e-3)
    # after the reset e works until its five-hour window is spent again, then stops: no second reset
    out = NOW + 2 * H + H / r5
    e_end = e0 - 0.05 / r5
    assert value_at(line, out + H) == pytest.approx(e_end + f0 - 0.03 * (out + H - NOW) / H, abs=2e-3)
    assert value_at(line, NOW + 24 * H - 1) == pytest.approx(e_end + f0 - 0.03 * 24, abs=2e-3)
    stops = [r for r in sc["runs_out"] if r["after_refill"]]
    assert len(stops) == 1 and abs(stops[0]["at"] - out) <= 60 and stops[0]["reset_reported"] is False


# ---------------------------------------------------------------------------
# Measured zero, no measured pace, measured but held: three different facts


def test_measured_zero_is_not_unknown(tmp_path):
    accounts = codex_accounts(a=weekly("a", 0.53), b=weekly("b", 0.36))
    _result, analytics = chart_of(feed(tmp_path, accounts), accounts, WEEK_KEY)
    sc = analytics["window"]["scenarios"]["recent_pace"]
    assert split(sc) == {"accounts": 4, "at_pace": 0, "held": 4, "zero_growth": 4, "pace_held": 0, "pace_unknown": 0}
    assert sc["line"] == analytics["window"]["scenarios"]["no_new_use"]["line"]


def test_no_measured_pace_is_unknown_not_zero(tmp_path):
    accounts = codex_accounts()
    # the collector has watched for ten minutes: every pace is still warming up
    result, analytics = chart_of(feed(tmp_path, accounts, start=NOW - 600), accounts, WEEK_KEY)
    assert group(result, WEEK)["recent_pace"]["state"] == "warming_up"
    sc = analytics["window"]["scenarios"]["recent_pace"]
    assert split(sc) == {"accounts": 4, "at_pace": 0, "held": 4, "zero_growth": 0, "pace_held": 0, "pace_unknown": 4}
    assert sc["line"] == analytics["window"]["scenarios"]["no_new_use"]["line"]


def cut(kind):
    """a's readings with one break in the last fifteen minutes, as the estimator defines a break."""
    rise = rising(0.50, A_RATE)
    if kind == "source_gap":  # one healthy sweep did not see a
        return lambda t: [] if abs(t - CUT) < 1 else [snapshot("a", t, [limit(rise(t))])]
    if kind == "short":  # a first appears 14 minutes ago
        return lambda t: [] if t < NOW - 840 else [snapshot("a", t, [limit(rise(t))])]
    if kind == "reset":  # a new cycle began ten minutes ago
        return lambda t: [snapshot("a", t, [limit(rise(t) - 0.4, reset=WEEK_RESET + 86400)])] if t >= CUT \
            else [snapshot("a", t, [limit(rise(t))])]
    if kind == "plan":
        return lambda t: [snapshot("a", t, [limit(rise(t))], plan="pro" if t >= CUT else "plus")]
    if kind == "source":
        return lambda t: [snapshot("a", t, [limit(rise(t))], source="rollout" if t >= CUT else "app")]
    if kind == "correction":  # the same observation time with another value: a correction, not use
        return lambda t: [snapshot("a", t, [limit(rise(t) + 0.01)], observed=t - STEP - 20)] if abs(t - CUT) < 1 \
            else [snapshot("a", t, [limit(rise(t))])]
    raise ValueError(kind)


@pytest.mark.parametrize("kind,reasons", [
    ("source_gap", {"gap"}), ("short", {"few_observations", "short_span", "gap"}), ("reset", {"reset"}),
    ("plan", {"plan"}), ("source", {"gap"}), ("correction", {"correction", "ratio_drop"})])
def test_the_estimators_cuts_leave_a_pace_unknown_and_undrained(tmp_path, kind, reasons):
    accounts = codex_accounts(a=cut(kind))
    result, analytics = chart_of(feed(tmp_path, accounts), accounts, WEEK_KEY)
    pace = group(result, WEEK)["recent_pace"]
    assert pace["accounts_known"] == 3  # b, c, d
    (why, count), = pace["not_known"].items()
    assert count == 1 and why.split(":")[1] in reasons, pace["not_known"]
    sc = analytics["window"]["scenarios"]["recent_pace"]
    assert split(sc) == {"accounts": 4, "at_pace": 1, "held": 3, "zero_growth": 2, "pace_held": 0, "pace_unknown": 1}
    left = sc["line"][0][1]
    assert value_at(sc["line"], NOW + H) == pytest.approx(left - B_RATE, abs=1e-4)  # only b drains


def test_a_failed_sweep_restarts_every_pace(tmp_path):
    accounts = codex_accounts()
    result, analytics = chart_of(feed(tmp_path, accounts, fail=(CUT,)), accounts, WEEK_KEY)
    assert group(result, WEEK)["recent_pace"]["accounts_known"] == 0
    sc = analytics["window"]["scenarios"]["recent_pace"]
    assert split(sc) == {"accounts": 4, "at_pace": 0, "held": 4, "zero_growth": 0, "pace_held": 0, "pace_unknown": 4}


# ---------------------------------------------------------------------------
# Scope: what does not govern the request, and what cannot be read


def chart_group(result, meaning="primary"):
    return group(result, WEEK, meaning)["key"]


def test_a_window_that_does_not_govern_the_request_drains_nothing(tmp_path):
    accounts = codex_accounts(
        a=lambda t: [snapshot("a", t, [limit(rising(0.50, A_RATE)(t), applies_to_request_kinds=["text"])])],
        b=lambda t: [snapshot("b", t, [limit(rising(0.30, B_RATE)(t), applies_to_request_kinds=["text"])])],
        c=lambda t: [snapshot("c", t, [limit(0.60, applies_to_request_kinds=["text"])])],
        d=lambda t: [snapshot("d", t, [limit(1.0, applies_to_request_kinds=["text"])])])
    store = feed(tmp_path, accounts)
    probe, _ = chart_of(store, accounts, WEEK_KEY)
    key = chart_group(probe)
    _result, analytics = chart_of(store, accounts, key, kind="image")
    window = analytics["window"]
    assert window["key"] == key and window["scope_applies"] is False and window["holds"] == {"scope_not": 4}
    sc = window["scenarios"]["recent_pace"]
    assert split(sc) == {"accounts": 4, "at_pace": 0, "held": 4, "zero_growth": 2, "pace_held": 2, "pace_unknown": 0}
    assert sc["line"] == window["scenarios"]["no_new_use"]["line"]


def test_the_selected_windows_own_unreadable_scope_holds_every_account(tmp_path):
    bad = {"applies_to_models": {"bad": True}}
    accounts = codex_accounts(
        a=lambda t: [snapshot("a", t, [limit(rising(0.50, A_RATE)(t), **bad)])],
        b=lambda t: [snapshot("b", t, [limit(rising(0.30, B_RATE)(t), **bad)])],
        c=lambda t: [snapshot("c", t, [limit(0.60, **bad)])], d=lambda t: [snapshot("d", t, [limit(1.0, **bad)])])
    store = feed(tmp_path, accounts)
    probe, _ = chart_of(store, accounts, WEEK_KEY)
    key = chart_group(probe)
    _result, analytics = chart_of(store, accounts, key)
    window = analytics["window"]
    assert window["key"] == key and window["scope_applies"] is None and window["holds"] == {"scope_unknown": 4}
    assert split(window["scenarios"]["recent_pace"])["at_pace"] == 0


def test_an_unreadable_scope_holds_the_accounts_that_have_that_window_only(tmp_path):
    rise = rising(0.50, A_RATE)
    odd = limit(0.2, cid="odd", applies_to_models={"bad": True})
    accounts = codex_accounts(a=lambda t: [snapshot("a", t, [limit(rise(t)), odd])])
    _result, analytics = chart_of(feed(tmp_path, accounts), accounts, WEEK_KEY)
    assert analytics["scope"]["unreadable_windows"] == 1
    window = analytics["window"]
    assert window["scope_applies"] is True and window["holds"] == {"scope_unknown": 1}
    sc = window["scenarios"]["recent_pace"]
    # a has the window whose scope cannot be read: held, its measured pace said; b is not
    assert split(sc) == {"accounts": 4, "at_pace": 1, "held": 3, "zero_growth": 2, "pace_held": 1, "pace_unknown": 0}
    assert value_at(sc["line"], NOW + H) == pytest.approx(START_LEFT - B_RATE, abs=1e-4)


def test_an_unrelated_history_only_window_of_unknown_scope_holds_nobody_else(tmp_path):
    # Request kinds give the window a key of its own (quota_scope.key_token): restored from the
    # history, its scope is not read (a bare model digest would prove it binds no unnamed model).
    gemini = limit(0.5, reset=NOW + 86400, cid="gemini-weekly", applies_to_models=[],
                   applies_to_model_prefixes=["gemini-"], applies_to_request_kinds=["text"])
    accounts = codex_accounts(z=lambda t: z_stale_month(t) + (
        [snapshot("z", t, [gemini])] if t < START + 1200 else []))
    result, analytics = chart_of(feed(tmp_path, accounts), accounts, WEEK_KEY)
    restored = [g for g in result["summary"]["groups"] if "gemini" in g["key"]]
    assert len(restored) == 1 and restored[0]["model_scope"] == "names_unknown"
    assert analytics["scope"]["unreadable_windows"] == 1  # z's restored window: its scope is not read
    window = analytics["window"]
    assert window["scope_applies"] is True and window["holds"] == {}
    assert split(window["scenarios"]["recent_pace"])["at_pace"] == 2


# ---------------------------------------------------------------------------
# Combined keeps its figures; its pace future says the same split


def test_combined_figures_are_unchanged_and_its_pace_future_counts_a_measured_zero(tmp_path):
    store = fed_store(tmp_path)
    _result, analytics = joint_of(store)
    joint = analytics["joint"]
    # the figures test_joint_view derives on paper
    assert joint["figure"] == pytest.approx(0.5, abs=1e-3) and joint["unknown_reasons"] == {"window_missing": 1}
    sc = joint["scenarios"]["recent_pace"]
    # a runs at its common-period pace; c (exhausted weekly, both windows flat over the common
    # period) has a measured zero there: counted as such, never as a missing pace
    assert split(sc) == {"accounts": 2, "at_pace": 1, "held": 1, "zero_growth": 1, "pace_held": 0, "pace_unknown": 0}
    assert "pace_held" not in joint["scenarios"]["no_new_use"]
