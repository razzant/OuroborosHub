"""Regression tests for the reserve overview, the history collector and the
quota_summary tool (v0.6.0).

Everything here is synthetic: hand-built status payloads and simulated
collector sweeps against a temporary SQLite file. None of it measures a real
account, and a passing chart test says nothing about forecast accuracy.

The host is MOCKED in the lifecycle tests: ``_Host`` records registrations the
way a worker process does and never runs a supervised task by itself. The
real publication/cancellation path is the parent's live check.
"""

import asyncio
import json
import math
import os
import shutil
import sqlite3
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest

import plugin
import quota_history
import quota_summary as qs

NOW = 1_790_000_000.0  # a fixed synthetic "now" (2026-09-21T13:33:20Z)
WEEK = 604800
FIVE_H = 18000


def at(offset):
    return qs.iso(NOW + offset)


def constraint(cid, ratio, window=WEEK, reset=None, models=None, label=None, cooldown=None):
    row = {"id": cid, "label": label or cid, "used_ratio": ratio, "window_seconds": window,
           "resets_at": None if reset is None else at(reset)}
    if models is not None:
        row["applies_to_models"] = models
    if cooldown is not None:
        row["cooldown_until"] = cooldown
    return row


def snap(harness, sid, constraints, *, source="app", fresh=True, observed=-60.0,
         plan=None, availability="available", observed_abs=None):
    return {
        "subject": {"harness": harness, "subject_id": sid, "plan_label": plan},
        "constraints": constraints,
        "availability": {"state": availability},
        "observed_at": qs.iso(observed_abs) if observed_abs is not None else at(observed),
        "freshness": "fresh" if fresh else "stale",
        "source": source,
    }


def profile(harness, pid, *, enabled=True, verification="passed", email="", plan="",
            display_name=None, detail=""):
    return {
        "profile": {"harness_id": harness, "profile_id": pid, "enabled": enabled,
                    "display_name": display_name or pid},
        "status": {"verification": verification, "availability": "available", "detail": detail},
        "identity": {"email": email, "plan": plan},
    }


def payload(snaps, profiles=(), natives=(), *, unified=False, reads=None, harnesses=("claude", "codex")):
    return {
        "reads": reads or {"catalog": "ok", "accounts": "ok", "quota": "ok"},
        "daemon": {"state": "running"},
        "unified_accounts": unified,
        "harnesses": [{"id": h, "display_name": h.title(), "status": "ok", "enabled": True}
                      for h in harnesses],
        "profiles": {"profiles": list(profiles), "harnessAccounts": list(natives)},
        "quota": list(snaps),
        "quota_absences": [],
    }


def summary_of(data, view=None, now=NOW):
    state = qs.compute(data, view or qs.HistoryView(state="empty"), now)
    return qs.build_summary(data, state.view, now, state=state), state


def group(summary, key_part, harness=None):
    found = [g for g in summary["groups"]
             if key_part in g["key"] and (harness is None or g["harness"] == harness)]
    assert len(found) == 1, [g["key"] for g in summary["groups"]]
    return found[0]


# ---------------------------------------------------------------------------
# Sum, average, coverage, identity


def test_equal_weight_sum_average_and_coverage():
    data = payload(
        [snap("codex", f"a{i}", [constraint("primary", used, reset=86400)])
         for i, used in enumerate((0.8, 0.5, 0.2))],
        [profile("codex", f"a{i}") for i in range(4)],
    )
    summary, _ = summary_of(data)
    g = group(summary, "|primary|")
    assert g["measured"] == {"accounts": 3, "windows": 1.5, "average_remaining_pct": 50.0, "at_limit": 0}
    assert g["coverage"]["measured"] == 3
    assert g["coverage"]["other_family_accounts"] == 1  # a3: no reading, not assumed
    assert g["unrestricted_windows"] == 1.5


def test_duration_comes_from_window_seconds_never_from_primary_or_secondary():
    data = payload([
        snap("codex", "a", [constraint("primary", 0.1, WEEK, reset=3600),
                            constraint("secondary", 0.2, FIVE_H, reset=600)]),
        snap("codex", "b", [constraint("primary", 0.3, None)]),
    ], [profile("codex", "a"), profile("codex", "b")])
    summary, _ = summary_of(data)
    durations = {(g["meaning"], g["duration"]) for g in summary["groups"]}
    assert ("primary", "week") in durations
    assert ("secondary", "5 hours") in durations
    assert ("primary", "duration not reported") in durations
    # A primary without a length is its own group, not folded into the week.
    assert group(summary, "|primary|604800|")["measured"]["accounts"] == 1


def test_five_hour_week_and_model_scoped_limits_are_never_added():
    data = payload([snap("claude", "p1", [
        constraint("five_hour", 0.5, FIVE_H, reset=3600),
        constraint("seven_day", 0.4, WEEK, reset=86400),
        constraint("weekly_scoped:Fable", 1.0, WEEK, reset=86400, models=["fable"], label="7 day (Fable)"),
    ])], [profile("claude", "p1")])
    summary, _ = summary_of(data)
    assert [g["duration"] for g in summary["groups"]] == ["5 hours", "week", "week"]
    assert [g["measured"]["windows"] for g in summary["groups"]] == [0.5, 0.6, 0.0]
    assert summary["groups"][2]["measured"]["at_limit"] == 1
    assert summary["groups"][2]["models"] == ["fable"]
    # The scoped cap being spent restricts nothing else on the account.
    assert summary["groups"][1]["restrictions"] == {}


def test_two_sources_of_one_limit_count_once_and_newest_wins():
    data = payload([
        snap("codex", "c1", [constraint("primary", 0.03, reset=90000)], source="codex_rollout",
             observed=-30),
        snap("codex", "c1", [constraint("codex:primary", 0.04, reset=90000.6, label="codex primary")],
             source="codex_app_server", observed=-200, plan="pro"),
    ], [profile("codex", "c1")])
    summary, state = summary_of(data)
    g = group(summary, "|primary|")
    assert g["measured"]["accounts"] == 1
    assert g["measured"]["windows"] == 0.97  # the newer rollout reading
    assert state.groups[0].members[0].reading.source == "codex_rollout"
    assert g["plans"]["breakdown"] == [{"plan": "pro", "accounts": 1, "windows": 0.97}]


def test_sources_that_disagree_on_the_reset_are_named_not_guessed():
    data = payload([
        snap("codex", "c1", [constraint("primary", 0.7, reset=3 * 86400)], source="rollout", observed=-30),
        snap("codex", "c1", [constraint("codex:primary", 0.1, reset=6 * 86400)], source="app", observed=-60),
        snap("codex", "c2", [constraint("primary", 0.5, reset=86400)]),
    ], [profile("codex", "c1"), profile("codex", "c2")])
    summary, _ = summary_of(data)
    g = group(summary, "|primary|")
    assert g["coverage"]["conflicting"] == 1
    assert g["measured"] == {"accounts": 1, "windows": 0.5, "average_remaining_pct": 50.0, "at_limit": 0}


def test_sources_measuring_the_same_moment_must_agree_on_the_value():
    for low_source, high_source in (("aaa", "zzz"), ("zzz", "aaa")):
        data = payload([
            snap("codex", "a", [constraint("primary", 0.1, reset=86400)], source=low_source),
            snap("codex", "a", [constraint("primary", 0.9, reset=86400)], source=high_source),
            snap("codex", "b", [constraint("primary", 0.5, reset=86400)]),
        ], [profile("codex", "a"), profile("codex", "b")])
        summary, state = summary_of(data)
        g = group(summary, "|primary|")
        # Neither source outranks the other: the account is "sources disagree",
        # whatever the names sort as.
        assert g["coverage"]["conflicting"] == 1
        assert g["measured"] == {"accounts": 1, "windows": 0.5, "average_remaining_pct": 50.0, "at_limit": 0}
        member = next(m for m in state.groups[0].members if m.subject_id == "a")
        assert member.reason == "sources_disagree_on_value"
    # Within whole-percent rounding they agree, and the larger use is counted.
    data = payload([
        snap("codex", "a", [constraint("primary", 0.4049, reset=86400)], source="exact"),
        snap("codex", "a", [constraint("primary", 0.40, reset=86400)], source="rounded"),
    ], [profile("codex", "a")])
    g = group(summary_of(data)[0], "|primary|")
    assert g["measured"]["accounts"] == 1 and g["measured"]["windows"] == 0.6


def test_only_the_own_harness_namespace_is_dropped():
    assert qs.meaning_of("codex", "codex:primary", "") == "primary"
    assert qs.meaning_of("codex", "base_model_inference:primary", "") == "base_model_inference:primary"
    assert qs.meaning_of("claude", "codex:primary", "") == "codex:primary"


@pytest.mark.parametrize("bad, reason", [
    (None, "missing"), ("0.5", "not_a_number"), (True, "not_a_number"),
    (float("nan"), "not_finite"), (float("inf"), "not_finite"), (1.4, "out_of_range"), (-0.1, "out_of_range"),
])
def test_unusable_ratio_is_never_a_zero(bad, reason):
    assert qs.ratio_of(bad) == (None, reason)
    data = payload([
        snap("codex", "good", [constraint("primary", 0.25, reset=86400)]),
        snap("codex", "bad", [constraint("primary", bad, reset=86400)]),
        snap("codex", "old", [constraint("primary", 0.1, reset=86400)], fresh=False, observed=-7200),
    ], [profile("codex", n) for n in ("good", "bad", "old")])
    summary, _ = summary_of(data)
    g = group(summary, "|primary|")
    assert g["measured"]["windows"] == 0.75
    assert g["coverage"]["invalid"] == 1
    assert g["coverage"]["stale_only"] == 1
    assert g["observed"]["stale_newest_at"] == at(-7200)


def test_reading_whose_reset_passed_or_without_time_is_not_current():
    no_time = snap("codex", "t", [constraint("primary", 0.1, reset=86400)])
    no_time["observed_at"] = None
    future = snap("codex", "f", [constraint("primary", 0.1, reset=86400)], observed=3600)
    data = payload([
        snap("codex", "r", [constraint("primary", 0.9, reset=-60)], observed=-600),
        no_time, future,
    ], [profile("codex", n) for n in ("r", "t", "f")])
    summary, state = summary_of(data)
    g = group(summary, "|primary|")
    assert g["measured"]["accounts"] == 0
    assert g["coverage"]["reset_passed"] == 1
    assert g["coverage"]["invalid"] == 2
    reasons = sorted(m.reason for m in state.groups[0].members if m.status == "invalid")
    assert reasons == ["no_observation_time", "observed_in_future"]


def test_unified_default_alias_needs_the_unified_flag_and_no_fresh_reading_of_its_own():
    legacy = snap("claude", None, [constraint("seven_day", 0.4, reset=86400)])
    own = snap("claude", "claude-default", [constraint("seven_day", 0.1, reset=86400)])
    accounts = [profile("claude", "claude-default")]
    # Unified, default row has no reading of its own: the legacy key is its.
    s1, _ = summary_of(payload([legacy], accounts, unified=True))
    assert group(s1, "seven_day")["measured"]["windows"] == 0.6
    # Unified and the default row has a fresh reading: the legacy key is superseded.
    s2, _ = summary_of(payload([legacy, own], accounts, unified=True))
    assert group(s2, "seven_day")["measured"] == {"accounts": 1, "windows": 0.9,
                                                   "average_remaining_pct": 90.0, "at_limit": 0}
    assert s2["superseded_alias_readings"] == {"claude": 1}
    # Legacy engine: the null subject is the native login, a separate account.
    native = {"harness_id": "claude", "native_login_detected": True, "identity": {}}
    s3, _ = summary_of(payload([legacy, own], accounts, [native]))
    assert group(s3, "seven_day")["measured"]["accounts"] == 2
    # Unified without a reserved default row: nobody to attribute it to.
    s4, _ = summary_of(payload([legacy], [profile("claude", "work")], unified=True))
    assert s4["groups"] == []
    assert s4["unattributed_readings"] == {"claude": 1}


def test_subjects_outside_the_account_list_and_unread_accounts():
    data = payload([snap("codex", "ghost", [constraint("primary", 0.2, reset=86400)]),
                    snap("codex", "real", [constraint("primary", 0.2, reset=86400)])],
                   [profile("codex", "real")])
    summary, _ = summary_of(data)
    assert group(summary, "|primary|")["measured"]["accounts"] == 1
    assert summary["unattributed_readings"] == {"codex": 1}
    data["reads"]["accounts"] = "failed"
    summary, _ = summary_of(data)
    g = group(summary, "|primary|")
    assert g["measured"]["accounts"] == 2
    assert g["restrictions"]["account_state_unknown"]["accounts"] == 2
    assert g["unrestricted_windows"] == 0.0


def test_restrictions_are_counted_but_not_hidden_in_the_total():
    cooling = (qs.iso(NOW + 3600))
    data = payload([
        snap("codex", "ok", [constraint("primary", 0.5, reset=86400)]),
        snap("codex", "off", [constraint("primary", 0.5, reset=86400)]),
        snap("codex", "out", [constraint("primary", 0.5, reset=86400)]),
        snap("codex", "cool", [constraint("primary", 0.5, reset=86400),
                               {"id": "cooldown", "label": "Cooldown", "used_ratio": None,
                                "window_seconds": None, "cooldown_until": cooling}]),
        snap("codex", "short", [constraint("primary", 0.5, reset=86400),
                                constraint("burst", 1.0, FIVE_H, reset=1800)]),
    ], [profile("codex", "ok"), profile("codex", "off", enabled=False),
        profile("codex", "out", verification="failed"), profile("codex", "cool"),
        profile("codex", "short")])
    data["profiles"]["profiles"][2]["status"]["availability"] = "unavailable"
    summary, _ = summary_of(data)
    g = group(summary, "|primary|604800")
    assert g["measured"]["windows"] == 2.5
    assert g["unrestricted_windows"] == 0.5
    assert set(g["restrictions"]) == {"disabled", "auth_failed", "signed_out", "cooling", "other_limit_spent"}
    assert g["restrictions"]["other_limit_spent"] == {"accounts": 1, "windows": 0.5}
    # A cooldown row is not a quota window of its own.
    assert not any("cooldown" in x["key"] for x in summary["groups"])


def test_a_spent_shared_limit_restricts_even_without_a_reported_reset():
    def data(burst_reset, observed=-60):
        return payload([snap("codex", "a", [constraint("primary", 0.2, WEEK, reset=86400),
                                             constraint("burst", 1.0, FIVE_H, reset=burst_reset)],
                             observed=observed)], [profile("codex", "a")])

    # Spent now, reset time not reported: unknown timing, still a restriction.
    summary, _ = summary_of(data(None))
    week = group(summary, "|primary|")
    assert week["unrestricted_windows"] == 0.0
    assert week["restrictions"] == {"other_limit_spent": {"accounts": 1, "windows": 0.8}}
    tool = next(row for row in qs.compact(summary)["groups"] if row["key"] == week["key"])
    assert tool["restrictions"] == week["restrictions"] and tool["unrestricted_windows"] == 0.0
    # An unreadable reset reads the same way.
    raw = data(None)
    raw["quota"][0]["constraints"][1]["resets_at"] = "soon"
    assert group(summary_of(raw)[0], "|primary|")["unrestricted_windows"] == 0.0
    # The spent reading belongs to a cycle whose reported reset has passed:
    # it is not current, and restricts nothing.
    summary, _ = summary_of(data(-60, observed=-600))
    week = group(summary, "|primary|")
    assert week["restrictions"] == {} and week["unrestricted_windows"] == 0.8
    assert group(summary, "|burst|")["coverage"]["reset_passed"] == 1
    # A stale spent reading restricts nothing either.
    stale = data(None)
    stale["quota"][0]["freshness"] = "stale"
    assert summary_of(stale)[0]["groups"][0]["measured"]["accounts"] == 0


def test_shared_sign_in_is_disclosed_never_merged_and_never_emitted():
    data = payload([snap("claude", p, [constraint("seven_day", 0.5, reset=86400)]) for p in ("a", "b", "c")],
                   [profile("claude", "a", email="Same@Example.com"),
                    profile("claude", "b", email="same@example.com"),
                    profile("claude", "c", email="other@example.com")])
    summary, _ = summary_of(data)
    g = group(summary, "seven_day")
    assert g["measured"]["accounts"] == 3
    assert g["possible_duplicates"] == 2
    text = json.dumps(summary) + json.dumps(qs.compact(summary, detail=True))
    assert "example.com" not in text.lower()


def test_mixed_plans_are_flagged_with_a_breakdown():
    data = payload([snap("codex", "a", [constraint("primary", 0.5, reset=86400)], plan="pro"),
                    snap("codex", "b", [constraint("primary", 0.2, reset=86400)], plan="prolite"),
                    snap("codex", "c", [constraint("primary", 0.2, reset=86400)])],
                   [profile("codex", n) for n in "abc"])
    g = group(summary_of(data)[0], "|primary|")
    assert g["plans"]["mixed"] is True
    assert g["plans"]["breakdown"] == [
        {"plan": "pro", "accounts": 1, "windows": 0.5},
        {"plan": "prolite", "accounts": 1, "windows": 0.8},
        {"plan": "not reported", "accounts": 1, "windows": 0.8},
    ]


def test_unread_quota_facet_sums_nothing_and_junk_never_raises():
    data = payload([snap("codex", "a", [constraint("primary", 0.5, reset=86400)])], [profile("codex", "a")],
                   reads={"catalog": "ok", "accounts": "ok", "quota": "failed"})
    summary, _ = summary_of(data)
    assert summary["groups"] == [] and summary["reads"]["quota"] == "failed"
    for junk in (None, [], "x", {"quota": "nope", "reads": []},
                 {"reads": {"quota": "ok"}, "quota": [1, None, {"subject": "x"},
                                                      {"subject": {"harness": "c"}, "constraints": "x"},
                                                      {"subject": {"harness": "c"}, "constraints": [1, {"used_ratio": {}}]}]}):
        summary, state = summary_of(junk)
        assert isinstance(summary["groups"], list)
        assert qs.build_chart(state) is None or isinstance(qs.build_chart(state), dict)


def test_synthetic_multi_source_fleet_counts_accounts_not_snapshots():
    """Shaped like the dated 41-account / 45-snapshot evidence (synthetic):
    most accounts report through two sources, some only stale ones."""
    snaps, profiles = [], []
    for i in range(23):
        sid = f"codex-{i:02d}"
        profiles.append(profile("codex", sid, plan="pro" if i % 2 else "prolite"))
        snaps.append(snap("codex", sid, [constraint("codex:primary", 0.02 * (i % 5), reset=5 * 86400 + i,
                                                    label="codex primary")], source="codex_app_server"))
        if i % 3 == 0:
            snaps.append(snap("codex", sid, [constraint("primary", 0.02 * (i % 5), reset=5 * 86400 + i)],
                              source="codex_rollout", observed=-20))
    for i in range(15):
        profiles.append(profile("claude", f"claude-{i:02d}"))
        if i < 3:
            snaps.append(snap("claude", f"claude-{i:02d}", [constraint("seven_day", 0.3, reset=86400)],
                              fresh=False, observed=-4000))
    assert len(snaps) > len([p for p in profiles if p["profile"]["harness_id"] == "codex"])
    summary, _ = summary_of(payload(snaps, profiles))
    codex = group(summary, "|primary|", harness="codex")
    assert codex["measured"]["accounts"] == 23
    assert codex["measured"]["windows"] == round(sum(1 - 0.02 * (i % 5) for i in range(23)), 2)
    claude = group(summary, "seven_day")
    assert claude["measured"]["accounts"] == 0 and claude["coverage"]["stale_only"] == 3


# ---------------------------------------------------------------------------
# History, recent pace and boundaries (simulated collector sweeps)


def host_at(t, timelines, *, harness="codex", cid="primary", window=WEEK, source="app",
            plans=None, stale_after=None):
    """The status payload a host would serve at time ``t``: for each account
    the newest observation not later than ``t``, marked fresh."""
    snaps, profiles = [], []
    for sid, points in timelines.items():
        profiles.append(profile(harness, sid))
        seen = [p for p in points if NOW + p[0] <= t + 1e-9]
        if not seen:
            continue
        obs, ratio, reset = seen[-1][:3]
        plan = (plans or {}).get(sid)
        fresh = stale_after is None or t - (NOW + obs) <= stale_after
        snaps.append(snap(harness, sid, [constraint(cid, ratio, window, reset)], source=source,
                          observed_abs=NOW + obs, plan=plan, fresh=fresh))
    return payload(snaps, profiles, harnesses=(harness,))


def sweep(store, timelines, start, end, step=120.0, skip=(), **kw):
    t = NOW + start
    while t <= NOW + end + 1e-6:
        if not any(a <= t - NOW < b for a, b in skip):
            plugin.persist_sweep(store, host_at(t, timelines, **kw), "", t)
        t += step
    return store


def summarize(store, timelines, now=NOW, **kw):
    data = host_at(now, timelines, **kw)
    result = plugin.reserve_view(store, data, now, now)
    return result, data


def rate_of(result, key="|primary|"):
    return group(result["summary"], key)["recent_pace"]


def observed(past, offset):
    """The observed line at NOW + offset, read the way the widget draws it:
    each vertex holds until the next; None is a gap."""
    value = "before the chart"
    for t, v in past:
        if t <= NOW + offset:
            value = v
    return value


def test_repeated_cached_reading_is_one_point_and_extends_last_seen(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-1000, 0.2, 86400)]}
    sweep(store, timeline, -1000, 0)
    rows = sqlite3.connect(store.path).execute(
        "SELECT n_obs, first_obs, last_obs, first_seen, last_seen FROM run").fetchall()
    assert len(rows) == 1
    n_obs, first_obs, last_obs, first_seen, last_seen = rows[0]
    assert (n_obs, first_obs, last_obs) == (1, NOW - 1000, NOW - 1000)
    assert last_seen > first_seen


def test_unequal_intervals_use_the_endpoint_slope_not_an_average_of_rates(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3600, 0.10, 3 * 86400), (-3300, 0.11, 3 * 86400), (0, 0.17, 3 * 86400)]}
    sweep(store, timeline, -4200, 0)
    result, _ = summarize(store, timeline)
    pace = rate_of(result)
    assert pace["state"] == "ok"
    assert pace["windows_per_hour"] == pytest.approx(0.07, abs=1e-9)
    assert pace["span_min_seconds"] == 3600
    assert pace["windows_per_hour"] != pytest.approx(0.0927, abs=1e-3)
    assert pace["resolution_windows_per_hour"] == pytest.approx(0.01, abs=1e-9)


def test_zero_growth_is_a_known_zero_not_an_endless_reserve(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-5000, 0.3, 2 * 86400), (-2000, 0.3, 2 * 86400), (-100, 0.3, 2 * 86400)]}
    sweep(store, timeline, -5000, 0)
    result, _ = summarize(store, timeline)
    pace = rate_of(result)
    assert pace["state"] == "ok" and pace["windows_per_hour"] == 0 and pace["zero_growth"] == 1
    assert pace["exhaust_before_reset"] == 0
    chart = result["chart"]
    assert chart["recent_pace"] == chart["no_new_use"]
    # The tool keeps a known zero; only unknowns drop out.
    tool_pace = qs.compact(result["summary"])["groups"][0]["recent_pace"]
    assert tool_pace["windows_per_hour"] == 0 and tool_pace["zero_growth"] == 1


@pytest.mark.parametrize("second, reason", [
    ((-600, 0.05, 6 * 86400), "reset"),       # the provider reset: new cycle
    ((-600, 0.40, 2 * 86400), "ratio_drop"),  # a manual restore, same reset
])
def test_reset_or_drop_cuts_the_series_and_is_never_negative_use(tmp_path, second, reason):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3000, 0.80, 2 * 86400), second]}
    sweep(store, timeline, -4000, 0)
    result, _ = summarize(store, timeline)
    pace = rate_of(result)
    assert pace["state"] == "insufficient"
    assert pace["not_known"] == {f"insufficient:{reason}": 1}
    assert pace["windows_per_hour"] is None


def test_plan_change_cuts_the_series(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3000, 0.20, 2 * 86400), (-600, 0.25, 2 * 86400)]}
    t = NOW - 4000
    while t <= NOW:
        plan = "pro" if t < NOW - 600 else "max"
        plugin.persist_sweep(store, host_at(t, timeline, plans={"a": plan}), "", t)
        t += 120
    data = host_at(NOW, timeline, plans={"a": "max"})
    pace = group(plugin.reserve_view(store, data, NOW, NOW)["summary"], "|primary|")["recent_pace"]
    assert pace["not_known"] == {"insufficient:plan": 1}


def test_a_gap_in_the_record_is_not_idle_time(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    # Ouroboros closed from -3000 to -600; the source measured at -2000 while
    # nobody watched. Neither the pace nor the chart crosses the outage.
    timeline = {"a": [(-3500, 0.10, 2 * 86400), (-2000, 0.20, 2 * 86400), (-300, 0.22, 2 * 86400)]}
    sweep(store, timeline, -3600, 0, skip=[(-3000, -600)])
    result, _ = summarize(store, timeline)
    pace = rate_of(result)
    # The -2000 reading was first seen at -600, after the outage: it is not a
    # starting point, and one watched reading since is not a pace.
    assert pace["state"] == "warming_up"
    assert pace["not_known"] == {"warming_up:gap": 1}
    past = result["chart"]["past"]
    assert observed(past, -3490) is None               # not yet seen (first sweep after it: -3480)
    assert observed(past, -3400) == pytest.approx(0.9)
    assert [round(NOW) - 3120, None] in past            # the gap starts at the last sighting
    for moment in (-3000, -2600, -2000, -1700, -700):
        assert observed(past, moment) is None           # nobody watched
    assert observed(past, -500) == pytest.approx(0.8)   # seen again from -600
    assert observed(past, -100) == pytest.approx(0.78)


def test_a_cached_reading_returning_after_an_outage_does_not_heal_the_gap(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    # Paused from -3000 to -1000; afterwards the host still reports the -3500
    # reading. That repeat says nothing about the outage.
    timeline = {"a": [(-3500, 0.10, 2 * 86400), (-400, 0.30, 2 * 86400)]}
    sweep(store, timeline, -3600, 0, skip=[(-3000, -1000)])
    rows = sqlite3.connect(store.path).execute(
        "SELECT ratio, first_obs, last_seen, after_gap FROM run ORDER BY id").fetchall()
    assert [(r[0], r[1] - NOW, r[3]) for r in rows[:2]] == [(0.1, -3500, 0), (0.1, -3500, 1)]
    assert rows[0][2] - NOW == -3120  # the run before the outage is not stretched over it
    result, _ = summarize(store, timeline)
    pace = rate_of(result)
    assert pace["state"] == "warming_up" and pace["not_known"] == {"warming_up:gap": 1}
    assert observed(result["chart"]["past"], -2000) is None


def test_offline_return_of_the_same_cached_observation_is_not_a_pace(tmp_path):
    """The parent's independent probe, as a regression: one reading an hour
    ago, a long outage, the same cached reading two minutes ago, a new one
    now. Before the fix this was a 0.1 windows/h pace over a full hour."""
    store = quota_history.HistoryStore(tmp_path)
    for obs, seen, used in [(-3600, -3600, 0.1), (-3600, -120, 0.1), (0, 0, 0.2)]:
        data = host_at(NOW + seen, {"a": [(obs, used, 86400)]})
        plugin.persist_sweep(store, data, "", NOW + seen)
    result, _ = summarize(store, {"a": [(0, 0.2, 86400)]})
    pace = rate_of(result)
    assert pace["state"] == "warming_up" and pace["windows_per_hour"] is None
    assert pace["not_known"] == {"warming_up:gap": 1}
    assert pace["exhaust_before_reset"] == 0


def test_a_failed_sweep_breaks_the_watch_even_inside_the_tolerance(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3500, 0.10, 2 * 86400), (-200, 0.30, 2 * 86400)]}
    t = NOW - 3600
    while t <= NOW + 1e-6:
        if abs(t - (NOW - 2400)) < 1:
            plugin.persist_sweep(store, None, "URLError: refused", t)  # one failed read
        else:
            plugin.persist_sweep(store, host_at(t, timeline), "", t)
        t += 120
    rows = sqlite3.connect(store.path).execute(
        "SELECT first_obs, last_seen, after_gap FROM run ORDER BY id").fetchall()
    # The same cached reading on both sides of the failed read: two runs.
    assert [(r[0] - NOW, r[1] - NOW, r[2]) for r in rows] == [(-3500, -2520, 0), (-3500, -240, 1), (-200, 0, 0)]
    result, _ = summarize(store, timeline)
    # The unbroken watch begins after the failed read, and that is its start.
    assert result["summary"]["history"]["unbroken_watch"] == {
        "since": at(-2280), "exact": True, "lookback_seconds": 4440}
    pace = rate_of(result)
    assert pace["state"] == "warming_up" and pace["not_known"] == {"warming_up:gap": 1}
    past = result["chart"]["past"]
    assert observed(past, -2400) is None
    assert observed(past, -2600) == pytest.approx(0.9)


def test_nothing_is_drawn_before_the_collector_first_saw_a_reading(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    # Collection began two minutes ago; the host's reading is thirty minutes old.
    timeline = {"a": [(-1800, 0.3, 86400)]}
    sweep(store, timeline, -120, 0)
    result, _ = summarize(store, timeline)
    past = result["chart"]["past"]
    for moment in (-1800, -1700, -800, -200):
        assert observed(past, moment) is None
    assert observed(past, -60) == pytest.approx(0.7)
    assert rate_of(result)["state"] == "warming_up"


def test_the_chart_keeps_a_short_outage_between_two_samples(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    # Ten hours watched with a 30-minute outage that falls between two of the
    # 7-day chart's two-hourly samples (-15200 and -8000).
    timeline = {"a": [(-36000, 0.10, 3 * 86400), (-20000, 0.20, 3 * 86400), (-100, 0.30, 3 * 86400)]}
    sweep(store, timeline, -36000, 0, skip=[(-15000, -13200)])
    data = host_at(NOW, timeline)
    chart = plugin.reserve_view(store, data, NOW, NOW, horizon="7d")["chart"]
    past = chart["past"]
    assert observed(past, -15200) == pytest.approx(0.8)
    assert observed(past, -14000) is None                 # the hole survives sampling
    assert observed(past, -12000) == pytest.approx(0.8)
    assert any(v is None for t, v in past if NOW - 15200 < t < NOW - 8000)
    assert len(past) <= 100
    # The table names where the gap begins, whatever its stride.
    assert any(row.get("observed", 0) is None and row["at"] > at(-15200) and row["at"] < at(-8000)
               for row in chart["table"])


def test_a_reported_reset_is_an_edge_of_the_observed_line(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    # A 5-hour window resets at -3060, between two sweeps; the new cycle's first
    # reading (-3030) is seen by the very next sweep (-3000): the watch is unbroken.
    timeline = {"a": [(-7200, 0.60, -3060), (-3030, 0.05, -3060 + FIVE_H)]}
    sweep(store, timeline, -7200, 0, window=FIVE_H)
    data = host_at(NOW, timeline, window=FIVE_H)
    past = plugin.reserve_view(store, data, NOW, NOW)["chart"]["past"]
    assert observed(past, -3061) == pytest.approx(0.4)
    assert [round(NOW) - 3060, None] in past               # the old cycle ends at its reset
    assert observed(past, -3001) is None                   # not a slope from 0.4 to 0.95
    assert observed(past, -3000) == pytest.approx(0.95)
    assert observed(past, -2800) == pytest.approx(0.95)


def test_a_sweep_at_its_reset_never_vouches_for_the_ended_cycle(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    # The 5-hour window resets at -3000, exactly on a sweep. This host still
    # calls the old reading fresh there (an engine ages it at its own reset):
    # that sweep cannot vouch for it, so the line ends at the last sighting
    # before the reset and the ended cycle's value is never carried to it.
    timeline = {"a": [(-7200, 0.60, -3000), (-2900, 0.05, -3000 + FIVE_H)]}
    sweep(store, timeline, -7200, 0, window=FIVE_H)
    data = host_at(NOW, timeline, window=FIVE_H)
    past = plugin.reserve_view(store, data, NOW, NOW)["chart"]["past"]
    assert observed(past, -3121) == pytest.approx(0.4)
    assert [round(NOW) - 3120, None] in past               # vouched to its last sighting before the reset
    assert observed(past, -3001) is None
    assert observed(past, -2950) is None                   # not a slope from 0.4 to 0.95
    assert observed(past, -2800) == pytest.approx(0.95)
    last_seen = sqlite3.connect(store.path).execute(
        "SELECT max(last_seen) FROM run WHERE ratio = 0.6").fetchone()[0]
    assert last_seen == NOW - 3120


def test_pace_needs_fifteen_minutes_within_the_trailing_hour(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-600, 0.10, 2 * 86400), (0, 0.12, 2 * 86400)]}
    sweep(store, timeline, -600, 0)
    result, _ = summarize(store, timeline)
    pace = rate_of(result)
    assert pace["state"] == "warming_up"
    assert pace["not_known"] == {"warming_up:short_span": 1}
    assert result["summary"]["history"]["unbroken_watch"] == {
        "since": at(-600), "exact": True, "lookback_seconds": 4440}
    # Nothing before the collector started is invented.
    assert all(v is None for t, v in result["chart"]["past"] if t < NOW - 600)
    # Twenty minutes watched is a pace over twenty minutes, said as such.
    store2 = quota_history.HistoryStore(tmp_path / "b")
    timeline2 = {"a": [(-1200, 0.10, 2 * 86400), (0, 0.12, 2 * 86400)]}
    sweep(store2, timeline2, -1200, 0)
    pace2 = rate_of(summarize(store2, timeline2)[0])
    assert pace2["state"] == "ok" and pace2["span_min_seconds"] == 1200


def test_an_empty_store_is_warming_up_and_the_chart_starts_at_now(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    result, _ = summarize(store, {"a": [(-60, 0.4, 86400)]})
    assert rate_of(result)["state"] == "warming_up"
    assert result["summary"]["history"]["state"] == "empty"
    past = result["chart"]["past"]
    assert [v for _t, v in past[:-1]] == [None] * (len(past) - 1)
    assert past[-1] == [round(NOW), 0.6]
    assert not store.path.exists()  # readers never create the file


def test_zero_use_is_not_declared_unstarted_and_resets_stay_as_reported():
    data = payload([
        # Nothing used and a reset exactly one window after the reading: the
        # shape once read as "not started". It is a reported reset.
        snap("codex", "a", [constraint("primary", 0.0, reset=WEEK - 60)], observed=-60),
        snap("codex", "b", [constraint("primary", 0.0, reset=None)]),          # reset unknown
        snap("codex", "c", [constraint("primary", 0.0, reset=-30)], observed=-600),  # ended cycle
        snap("codex", "d", [constraint("primary", 0.2, reset=3600)]),
    ], [profile("codex", n) for n in "abcd"])
    summary, state = summary_of(data)
    g = group(summary, "|primary|")
    assert g["measured"]["accounts"] == 3 and g["measured"]["windows"] == 2.8
    assert g["coverage"]["reset_passed"] == 1              # the expiry rule holds for zero too
    assert g["reset_unknown"] == 1
    assert g["next_reset"] == {"at": at(3600), "accounts": 1}
    assert g["pace_to_reset"]["accounts"] == 2             # a's reported reset is kept
    assert "not_started" not in json.dumps(summary)
    tool = qs.compact(summary)["groups"][0]
    assert tool["reset_unknown"] == 1 and "not_started" not in tool
    chart = qs.build_chart(state, horizon="7d")
    assert [r["at"] for r in chart["resets"]] == [round(NOW + 3600), round(NOW + WEEK - 60)]


def test_partial_pace_is_a_lower_bound_and_draws_the_named_cohort_only(tmp_path):
    # 0.7.0: the all-accounts draw gate is gone. The line sums the qualified
    # cohort alone (b is not held constant inside it) and says whom it covers.
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3600, 0.10, 2 * 86400), (0, 0.14, 2 * 86400)],
                "b": [(-3600, 0.50, 2 * 86400), (-100, 0.20, 2 * 86400)]}  # b dropped: a restore
    # Align the first source observation with a watched sweep.
    sweep(store, timeline, -4080, 0)
    result, _ = summarize(store, timeline)
    pace = rate_of(result)
    assert pace["state"] == "partial"
    assert pace["accounts_known"] == 1 and pace["of"] == 2
    assert pace["windows_per_hour"] == pytest.approx(0.04, abs=1e-9)
    chart = result["chart"]
    line = chart["recent_pace"]
    assert line[0][1] == pytest.approx(0.86)  # a alone, not a + b
    assert line[-1][0] == chart["end"]  # the reset is beyond the horizon
    assert min(v for _t, v in line) == 0.0
    scope = chart["recent_pace_scope"]
    assert scope["accounts"] == 1 and scope["of"] == 2 and not scope["stops_at_reset"]
    assert sum(scope["excluded"].values()) == 1
    assert chart["cohort_past"] is not None
    assert "1 of 2" in chart["recent_pace_note"]
    assert chart["recent_pace_refill_scenario"] is None


def test_membership_is_every_account_of_the_limit_whatever_is_fresh_now(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3000, 0.10, 2 * 86400)], "b": [(-3000, 0.50, 2 * 86400)]}
    sweep(store, timeline, -3000, -1500)
    # b goes stale before now. 0.7.0 repair (D4): the past line still sums
    # both accounts — who it sums never depends on what is fresh now — and
    # where b has no value vouched for, it has a gap, never a smaller total.
    data = host_at(NOW, timeline)
    data["quota"][1]["freshness"] = "stale"
    result = plugin.reserve_view(store, data, NOW, NOW)
    chart = result["chart"]
    assert chart["y_max"] == 2 and chart["accounts"] == 1
    assert chart["past_accounts"] == 2 and chart["past_basis"] == "recorded"
    values = [v for t, v in chart["past"] if v is not None]
    assert values and all(v == pytest.approx(1.4) for v in values)
    assert chart["past"][-1] == [round(NOW), None]      # not every account has a value now


# ---------------------------------------------------------------------------
# Chart arithmetic (synthetic; the research example, recomputed)


def forecast_state():
    rows = [("x", 0.80, 8), ("y", 0.50, 24), ("z", 0.20, 72)]
    data = payload([snap("codex", sid, [constraint("primary", used, reset=hours * 3600)])
                    for sid, used, hours in rows], [profile("codex", sid) for sid, _u, _h in rows])
    state = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    for member, rate in zip(sorted(state.groups[0].members, key=lambda m: m.subject_id), (0.04, 0.02, 0.01)):
        member.rate = {"state": "ok", "windows_per_hour": rate, "span_seconds": 3600}
    return state


def line_at(points, t, right=False):
    best = None
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            best = v1 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
            if t < t1 or (t == t1 and not right):
                break
    return best


def test_chart_steps_only_at_reported_resets_and_matches_the_research_example():
    chart = qs.build_chart(forecast_state(), horizon="24h")
    # 0.7.0: the estimate stops at the first reported reset in the cohort
    # (x, 8 h); the research example's refills are the separate scenario.
    hold, pace = chart["no_new_use"], chart["recent_pace_refill_scenario"]
    assert chart["recent_pace"][-1][0] == round(NOW + 8 * 3600)
    assert chart["recent_pace_scope"]["stops_at_reset"] is True
    assert line_at(chart["recent_pace"], NOW + 5 * 3600) == pytest.approx(1.15)
    assert hold[0] == [round(NOW), 1.5]
    # The research example's table: h=5 1.15; h=8 2.06 (after x's reset);
    # h=16 1.50 / 2.30; h=24 1.92 / 2.80 once y's reset at 24 h is counted.
    assert line_at(pace, NOW + 5 * 3600) == pytest.approx(1.15)
    assert line_at(pace, NOW + 8 * 3600, right=True) == pytest.approx(2.06)
    assert line_at(pace, NOW + 16 * 3600) == pytest.approx(1.50)
    assert line_at(hold, NOW + 16 * 3600) == pytest.approx(2.30)
    # The 24-hour chart ends exactly at y's reset and shows the value before it.
    assert line_at(pace, NOW + 24 * 3600) == pytest.approx(0.94)
    week = qs.build_chart(forecast_state(), horizon="7d")
    assert line_at(week["recent_pace_refill_scenario"], NOW + 24 * 3600, right=True) == pytest.approx(1.92)
    assert line_at(week["no_new_use"], NOW + 24 * 3600, right=True) == pytest.approx(2.8)
    # Resets inside the horizon only; the 72-hour one is not drawn, and no
    # second 8-hour cycle is invented.
    assert [r["at"] for r in chart["resets"]] == [round(NOW + 8 * 3600), round(NOW + 24 * 3600)]
    steps = [t for (t0, _v0), (t, _v) in zip(hold, hold[1:]) if t == t0]
    assert steps == [round(NOW + 8 * 3600)]
    # An account at zero stays at zero until its reset.
    assert line_at(pace, NOW + 7 * 3600) == pytest.approx(0.0 + 0.36 + 0.73, abs=1e-6)


# ---------------------------------------------------------------------------
# 0.8.0: the futures the widget draws — every account read now, the row's
# own figure, to the horizon — and the reset schedule they are made of


def _schedule(sc):
    return [(round((e["at"] - NOW) / 3600, 3), e["accounts"], e["adds"], e["total_after"]) for e in sc["schedule"]]


def test_both_scenarios_start_at_the_figure_and_reach_the_horizon():
    for horizon in ("24h", "7d"):
        chart = qs.build_chart(forecast_state(), horizon=horizon)
        for name, paced in (("no_new_use", 0), ("recent_pace", 3)):
            sc = chart["scenarios"][name]
            assert sc["line"][0] == [round(NOW), 1.5] == [chart["now"], chart["current_windows"]]
            assert sc["line"][-1][0] == chart["end"]
            assert (sc["accounts"], sc["at_pace"], sc["held"]) == (3, paced, 3 - paced)


def test_what_a_reset_gives_back_depends_on_the_scenario():
    """The research example: x 80% used resets in 8 h, y 50% in 24 h, z 20%
    in 72 h, at 4, 2 and 1 percentage points an hour. With no new use a reset
    gives back what is used now; at the recent pace, what has been used by
    then — the same functions the lines are drawn from."""
    chart = qs.build_chart(forecast_state(), horizon="24h")
    assert _schedule(chart["scenarios"]["no_new_use"]) == [(8.0, 1, 0.8, 2.3), (24.0, 1, 0.5, 2.8)]
    assert _schedule(chart["scenarios"]["recent_pace"]) == [(8.0, 1, 1.0, 2.06), (24.0, 1, 0.98, 1.92)]
    # Past the horizon: when, and for how many accounts — never how much.
    later = chart["scenarios"]["no_new_use"]["later"]
    assert (later["at"], later["accounts"], later["more_events"]) == (round(NOW + 72 * 3600), 1, 0)
    week = qs.build_chart(forecast_state(), horizon="7d")
    assert _schedule(week["scenarios"]["no_new_use"])[-1] == (72.0, 1, 0.2, 3.0)
    assert _schedule(week["scenarios"]["recent_pace"])[-1] == (72.0, 1, 0.92, 1.04)
    assert week["scenarios"]["recent_pace"]["later"] is None
    # x runs out at 5 h, before its reset; after their refills x and y run
    # out again inside the week (z does not: 72 h + 100 h is past it).
    runs = [(round((r["at"] - NOW) / 3600, 3), r["after_refill"]) for r in week["scenarios"]["recent_pace"]["runs_out"]]
    assert runs == [(5.0, False), (33.0, True), (74.0, True)]
    assert chart["scenarios"]["no_new_use"]["runs_out"] == []
    # The summary's next reset gives back what the "no new use" schedule's
    # first event does; the tool's next_reset itself is unchanged.
    data = payload([snap("codex", sid, [constraint("primary", used, reset=hours * 3600)])
                    for sid, used, hours in (("x", .8, 8), ("y", .5, 24), ("z", .2, 72))],
                   [profile("codex", sid) for sid in "xyz"])
    summary, _ = summary_of(data)
    g = summary["groups"][0]
    assert g["next_reset_returns"] == 0.8
    assert g["next_reset"] == {"at": at(8 * 3600), "accounts": 1}


def test_a_reset_exactly_at_the_horizon_is_drawn_with_its_refill():
    """y resets exactly at the 24-hour horizon. Its refill is in the schedule,
    so the line shows it too — the left value and then the right one at the
    horizon's instant — and the checkpoint there agrees. (The older
    no_new_use line kept only the value before it, as before.)"""
    chart = qs.build_chart(forecast_state(), horizon="24h")
    end = chart["end"]
    for name, before, after in (("no_new_use", 2.3, 2.8), ("recent_pace", 0.94, 1.92)):
        sc = chart["scenarios"][name]
        assert sc["line"][-2:] == [[end, before], [end, after]], name
        assert sc["checkpoints"][-1] == {"after_seconds": 86400, "at": end, "value": after}, name
        assert sc["schedule"][-1]["total_after"] == after, name
    assert chart["no_new_use"][-1] == [end, 2.3]
    assert [r["at"] for r in chart["resets"]] == [round(NOW + 8 * 3600), end]


def test_resets_reported_within_two_minutes_are_one_event_with_its_span():
    state = scenario_state([("a", 0.4, 3600, 0.1), ("c", 0.9, 3660, 0.2), ("b", 0.0, None, 0.0)])
    chart = qs.build_chart(state, horizon="24h")
    for name in ("no_new_use", "recent_pace"):
        sc = chart["scenarios"][name]
        assert len(sc["schedule"]) == 1, name
        event = sc["schedule"][0]
        assert (event["at"], event["last"], event["accounts"]) == (round(NOW + 3600), round(NOW + 3660), 2)
        # The total is the one after the last of them, as the table says.
        row = next(r for r in chart["table"] if r.get("event", "").startswith("reported reset"))
        assert row["until"] == qs.iso(NOW + 3660)
        assert row["scenario_" + name] == round(event["total_after"], 2), name
    # With no use both refill fully; at the pace c has run out (0.1 left at
    # 0.2 an hour) and a used 0.1 more by then: a different amount back.
    assert chart["scenarios"]["no_new_use"]["schedule"][0]["adds"] == pytest.approx(0.4 + 0.9)
    assert chart["scenarios"]["recent_pace"]["schedule"][0]["adds"] == pytest.approx(0.5 + 1.0)
    assert chart["reset_events"] == [{"at": round(NOW + 3600), "last": round(NOW + 3660), "accounts": 2}]


def test_a_reset_cluster_straddling_the_horizon_is_split_at_it():
    """Two half-full accounts report resets a minute either side of the
    24-hour horizon — within RESET_TOLERANCE_SEC, so one event if nothing cut
    it. Split at the horizon first: the schedule, the marks and the table
    count only the refill inside the span, so the total after it is the 1.5
    the line ends at, never 2; the other is only ``later``, when and for
    how many."""
    state = scenario_state([("a", 0.5, 86400 - 60, 0.0), ("b", 0.5, 86400 + 60, 0.0)], window=WEEK)
    chart = qs.build_chart(state, horizon="24h")
    inside, past = round(NOW + 86400 - 60), round(NOW + 86400 + 60)
    for name in ("no_new_use", "recent_pace"):
        sc = chart["scenarios"][name]
        assert [(e["at"], e["last"], e["accounts"], e["adds"], e["total_after"]) for e in sc["schedule"]] \
            == [(inside, inside, 1, 0.5, 1.5)], name
        assert sc["line"][-1] == [chart["end"], 1.5], name
        assert sc["checkpoints"][-1]["value"] == 1.5, name
        assert sc["later"] == {"at": past, "accounts": 1, "more_events": 0, "more_accounts": 0}, name
    assert chart["resets"] == [{"at": inside, "accounts": 1}]
    assert chart["reset_events"] == [{"at": inside, "last": inside, "accounts": 1}]
    row = next(r for r in chart["table"] if r.get("event", "").startswith("reported reset"))
    assert row["event"] == "reported reset · 1 account" and "until" not in row
    assert row["scenario_no_new_use"] == row["scenario_recent_pace"] == 1.5
    assert all(qs.parse_instant(r["at"]) <= chart["end"] for r in chart["table"])
    # On the week span both are inside: one event, as before.
    week = qs.build_chart(state, horizon="7d")
    hold = week["scenarios"]["no_new_use"]
    assert [(e["at"], e["last"], e["accounts"], e["total_after"]) for e in hold["schedule"]] \
        == [(inside, past, 2, 2.0)]
    assert hold["later"] is None


def test_the_shown_assumptions_are_the_drawn_scenarios_and_the_legacy_ones_are_apart():
    """The notes the widget shows describe what it draws: the recent-pace
    future runs every account read now to the horizon. The older cohort line's
    wording (stopping at the first reset) and the refill scenario's are kept
    only under ``legacy_assumptions``, by field."""
    chart = qs.build_chart(forecast_state(), horizon="24h")
    shown = " ".join(chart["assumptions"])
    assert "Recent pace: every account read now" in shown
    assert "stops at the first reported reset" not in shown
    assert "Refill scenario" not in shown and "(mixed)" not in shown
    legacy = chart["legacy_assumptions"]
    assert set(legacy) == {"recent_pace", "recent_pace_refill_scenario"}
    assert "stops at the first reported reset" in legacy["recent_pace"]
    assert all(field in chart for field in legacy)


def test_an_account_with_no_reported_reset_is_never_refilled():
    state = scenario_state([("a", 0.3, None, 0.05), ("b", 0.0, None, 0.0)])
    for horizon in ("24h", "7d"):
        chart = qs.build_chart(state, horizon=horizon)
        hold, pace = chart["scenarios"]["no_new_use"], chart["scenarios"]["recent_pace"]
        assert hold["schedule"] == pace["schedule"] == [] and hold["later"] is None
        assert hold["no_reset"] == pace["no_reset"] == 2
        assert {v for _t, v in hold["line"]} == {1.7}
        # 0.7 left at 5%/h runs out after 14 hours and stays out: no refill.
        assert pace["runs_out"] == [{"at": round(NOW + 14 * 3600), "reset_reported": False, "after_refill": False}]
        assert pace["line"][-1] == [chart["end"], 1.0]
        assert pace["zero_growth"] == 1


def test_accounts_with_no_qualified_pace_are_held_in_the_pace_scenario():
    state = scenario_state([("a", 0.4, 3600, 0.1), ("b", 0.6, 7200, 0.3)])
    member = next(m for m in state.groups[0].members if m.subject_id == "b")
    member.rate = {"state": "warming_up", "reason": "short_span"}
    chart = qs.build_chart(state, horizon="24h")
    pace = chart["scenarios"]["recent_pace"]
    assert (pace["accounts"], pace["at_pace"], pace["held"]) == (2, 1, 1)
    # b is held at 0.4 and refills at its reset (+0.6); a burns 0.1 by its reset.
    assert _schedule(pace) == [(1.0, 1, 0.5, 1.4), (2.0, 1, 0.6, 1.9)]
    # With no account at pace the scenario is the same as no new use.
    member_a = next(m for m in state.groups[0].members if m.subject_id == "a")
    member_a.rate = {"state": "insufficient", "reason": "gap"}
    chart = qs.build_chart(state, horizon="24h")
    assert chart["scenarios"]["recent_pace"]["at_pace"] == 0
    assert chart["scenarios"]["recent_pace"]["line"] == chart["scenarios"]["no_new_use"]["line"]


def test_restricted_accounts_stay_in_the_scenarios():
    """Measured quota is not a dispatch verdict: a cooling account's share is
    in the row's figure, so it is in both futures too."""
    cooling = constraint("cooldown", None, window=None, cooldown=at(900))
    data = payload([snap("codex", "a", [constraint("primary", 0.2, reset=3600), cooling]),
                    snap("codex", "b", [constraint("primary", 0.6, reset=7200)])],
                   [profile("codex", sid) for sid in "ab"], harnesses=("codex",))
    state = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    summary = qs.build_summary(data, state.view, NOW, state=state)
    assert summary["groups"][0]["restrictions"]["cooling"]["accounts"] == 1
    chart = qs.build_chart(state, horizon="24h")
    for sc in chart["scenarios"].values():
        assert sc["accounts"] == 2
        assert sc["line"][0][1] == summary["groups"][0]["measured"]["windows"] == 1.2


def test_with_no_current_reading_no_future_is_drawn(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timelines = {"a": [(-3000, 0.10, 2 * 86400)], "b": [(-3000, 0.50, 2 * 86400)]}
    sweep(store, timelines, -3000, -600)
    data = host_at(NOW, timelines)
    for row in data["quota"]:
        row["freshness"] = "stale"
    chart = plugin.reserve_view(store, data, NOW, NOW)["chart"]
    assert chart["scenarios"] is None and chart["current_windows"] is None
    assert chart["past_basis"] == "last_known" and chart["past"][-1][1] is None
    assert [v for _t, v in chart["past"][:-1] if v is not None]          # the dated record stays


def test_a_moving_reported_reset_is_read_as_reported_now(tmp_path):
    """A rolling window reports a later reset at each reading. The schedule
    is the reset reported now: the earlier time is not promised."""
    store = quota_history.HistoryStore(tmp_path)
    sweep(store, {"a": [(-3000, 0.30, 3 * 3600)]}, -3000, -1500)
    now_data = host_at(NOW, {"a": [(-60, 0.32, 5 * 3600)]})
    chart = plugin.reserve_view(store, now_data, NOW, NOW)["chart"]
    for sc in chart["scenarios"].values():
        assert [e["at"] for e in sc["schedule"]] == [round(NOW + 5 * 3600)]
    assert "Reset times are as reported now and may move" in " ".join(chart["assumptions"])


def test_the_tool_answer_is_unchanged_by_the_widget_futures():
    """The tool's fields stay what they were for the same state: the
    scenarios and next_reset_returns are the widget's, additive, and never
    in the compact answer."""
    data = payload([snap("codex", sid, [constraint("primary", used, reset=hours * 3600)])
                    for sid, used, hours in (("x", .8, 8), ("y", .5, 24))],
                   [profile("codex", sid) for sid in "xy"])
    summary, _ = summary_of(data)
    row = qs.compact(summary)["groups"][0]
    assert set(row) == {"key", "family", "limit", "duration", "remaining_windows", "of_accounts",
                        "average_remaining_pct", "accounts_at_limit", "unrestricted_windows", "coverage",
                        "newest_observed_at", "oldest_observed_at", "next_reset", "pace_to_reset",
                        "recent_pace", "tightest_in_family", "accounts_known_to_apply", "headline"}
    assert row["next_reset"] == {"at": at(8 * 3600), "accounts": 1}
    assert "scenario" not in json.dumps(qs.compact(summary)) and "returns" not in json.dumps(qs.compact(summary))


def test_chart_table_and_bounds():
    chart = qs.build_chart(forecast_state(), horizon="7d")
    assert chart["horizon"] == "7d" and chart["end"] - chart["now"] == 7 * 86400
    assert len(chart["past"]) <= 85
    events = [row for row in chart["table"] if row.get("event")]
    assert events[0]["event"] == "now"
    assert len(chart["table"]) <= qs.TABLE_PAST_ROWS + 3 + 3 + 3
    # The horizon's own row carries both scenario values (a fractional "now"
    # once left it empty).
    fractional = qs.build_chart(qs.compute(payload(
        [snap("codex", "a", [constraint("primary", .4, reset=86400)])], [profile("codex", "a")]),
        qs.HistoryView(state="empty"), NOW + 0.4), horizon="7d")
    last = fractional["table"][-1]
    assert last["at"] == qs.iso(round(NOW + 0.4 + 7 * 86400)) and last["no_new_use"] == 1.0
    assert any("no additional unreported resets" in a for a in chart["assumptions"])


# ---------------------------------------------------------------------------
# Store: retention, caps, corruption, privacy


def test_retention_deletes_readings_older_than_fourteen_days(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    old = {"a": [(-15 * 86400, 0.1, -14 * 86400)]}
    plugin.persist_sweep(store, host_at(NOW - 15 * 86400, old), "", NOW - 15 * 86400)
    store._sweeps_until_prune = 0
    plugin.persist_sweep(store, host_at(NOW, {"b": [(-10, 0.2, 86400)]}), "", NOW)
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT count(*) FROM run").fetchone()[0] == 1
    assert conn.execute("SELECT min(at) FROM sweep").fetchone()[0] == NOW


def test_retention_trims_a_long_unchanged_run_to_fourteen_days(tmp_path, monkeypatch):
    # One reading, re-observed daily with the same value for thirty days and
    # watched throughout (the watch tolerance is widened so daily sweeps are
    # one unbroken run without thirty days of two-minute sweeps).
    monkeypatch.setattr(qs, "SIGHTING_GAP_SEC", 2 * 86400.0)
    monkeypatch.setattr(quota_history, "PRUNE_EVERY_SWEEPS", 1)
    store = quota_history.HistoryStore(tmp_path)
    for day in range(30, -1, -1):
        t = NOW - day * 86400
        plugin.persist_sweep(store, host_at(t, {"a": [(-day * 86400, 0.3, 40 * 86400)]}), "", t)
    conn = sqlite3.connect(store.path)
    rows = conn.execute("SELECT first_obs, last_obs, first_seen, last_seen FROM run").fetchall()
    cutoff = NOW - quota_history.RETENTION_SEC
    assert rows == [(cutoff, NOW, cutoff, NOW)]
    assert conn.execute("SELECT min(at) FROM sweep").fetchone()[0] >= cutoff
    assert store.read(lambda salt: {}, NOW).oldest_at == cutoff


def test_row_cap_drops_the_oldest_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(quota_history, "MAX_RUNS", 5)
    monkeypatch.setattr(quota_history, "PRUNE_EVERY_SWEEPS", 1)
    store = quota_history.HistoryStore(tmp_path)
    for i in range(12):
        timeline = {"a": [(-10000 + i * 100, 0.01 * i, 86400)]}
        plugin.persist_sweep(store, host_at(NOW - 10000 + i * 100, timeline), "", NOW - 10000 + i * 100)
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT count(*) FROM run").fetchone()[0] == 5
    view = store.read(lambda salt: {}, NOW)
    assert view.capped_before is not None
    assert view.oldest_at == pytest.approx(NOW - 10000 + 7 * 100)
    summary = plugin.reserve_view(store, host_at(NOW, {"a": [(-10, 0.2, 86400)]}), NOW, NOW)["summary"]
    assert summary["history"]["capped_before"] is not None


def test_file_cap_shrinks_the_store(tmp_path, monkeypatch):
    store = quota_history.HistoryStore(tmp_path)
    for i in range(40):
        timeline = {f"s{j}": [(-5000 + i * 100, 0.01 * (i % 50), 86400)] for j in range(10)}
        plugin.persist_sweep(store, host_at(NOW - 5000 + i * 100, timeline), "", NOW - 5000 + i * 100)
    before = sqlite3.connect(store.path).execute("SELECT count(*) FROM run").fetchone()[0]
    monkeypatch.setattr(quota_history, "MAX_FILE_BYTES", 1)
    plugin.persist_sweep(store, host_at(NOW, {"s0": [(-10, 0.9, 86400)]}), "", NOW)
    after = sqlite3.connect(store.path).execute("SELECT count(*) FROM run").fetchone()[0]
    assert after < before


def test_file_cap_is_a_soft_ceiling_the_file_returns_under(tmp_path, monkeypatch):
    store = quota_history.HistoryStore(tmp_path)
    for i in range(60):
        timeline = {f"s{j}": [(-8000 + i * 120, 0.01 * (i % 90), 86400)] for j in range(20)}
        plugin.persist_sweep(store, host_at(NOW - 8000 + i * 120, timeline), "", NOW - 8000 + i * 120)
    size = store._file_bytes()
    cap = int(size * 0.9)
    monkeypatch.setattr(quota_history, "MAX_FILE_BYTES", cap)
    plugin.persist_sweep(store, host_at(NOW, {"s0": [(-10, 0.95, 86400)]}), "", NOW)
    assert store._file_bytes() <= cap
    assert store.read(lambda salt: {}, NOW).capped_before is not None


def test_bounded_read_is_marked_truncated(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3000 + i * 150, 0.01 * i, 86400) for i in range(20)]}
    sweep(store, timeline, -3000, 0)
    salt_pairs = lambda salt: {(qs.pseudo_id(salt, "codex", "a"), qs.group_key("codex", "primary", WEEK, ())): 0.0}
    view = store.read(salt_pairs, NOW, max_rows=3)
    assert view.truncated is True
    runs = next(iter(view.runs.values()))
    assert len(runs) == 3 and runs[-1].ratio == pytest.approx(0.19)


def test_an_unreadable_store_is_left_exactly_as_it_is(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    junk = b"this is not a database at all" * 100
    store.path.write_bytes(junk)
    wal = tmp_path / (quota_history.HISTORY_FILE + "-wal")
    wal.write_bytes(b"write-ahead evidence")
    backup = tmp_path / (quota_history.HISTORY_FILE + ".corrupt")
    backup.write_bytes(b"an older, unique backup")
    names = sorted(p.name for p in tmp_path.iterdir())
    assert store.read(lambda salt: {}, NOW).state == "unavailable"
    for _ in range(2):
        with pytest.raises(quota_history.HistoryCorrupt):
            plugin.persist_sweep(store, host_at(NOW, {"a": [(-10, 0.2, 86400)]}), "", NOW)
    assert store.path.read_bytes() == junk
    assert wal.read_bytes() == b"write-ahead evidence"
    assert backup.read_bytes() == b"an older, unique backup"
    assert sorted(p.name for p in tmp_path.iterdir()) == names
    view = store.read(lambda salt: {}, NOW)
    assert view.state == "unavailable" and "unreadable" in view.error
    summary = plugin.reserve_view(store, host_at(NOW, {"a": [(-10, 0.2, 86400)]}), NOW, NOW)["summary"]
    assert summary["history"]["state"] == "unavailable"
    assert group(summary, "|primary|")["recent_pace"]["state"] == "unavailable"
    assert group(summary, "|primary|")["measured"]["windows"] == 0.8  # the reserve itself stands


def test_a_store_of_another_shape_is_unavailable_not_rebuilt(tmp_path):
    # Tables without a version, and a versioned file of an older shape.
    for sql, version in (("CREATE TABLE something (x INTEGER)", 0),
                         ("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                          "INSERT INTO meta VALUES ('salt', 'abc');"
                          "CREATE TABLE run (id INTEGER PRIMARY KEY, subject TEXT, series TEXT, source TEXT,"
                          " plan TEXT, ratio REAL, resets_at REAL, floating INTEGER, first_obs REAL,"
                          " last_obs REAL, n_obs INTEGER, first_seen REAL, last_seen REAL);"
                          "CREATE TABLE sweep (at REAL PRIMARY KEY, ok INTEGER, reason TEXT)", 1)):
        folder = tmp_path / f"v{version}"
        folder.mkdir()
        store = quota_history.HistoryStore(folder)
        conn = sqlite3.connect(store.path)
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
        conn.close()
        before = store.path.read_bytes()
        salt = lambda s: {(qs.pseudo_id(s, "codex", "a"), qs.group_key("codex", "primary", WEEK, ())): 0.0}
        assert store.read(salt, NOW).state == "unavailable"
        with pytest.raises(quota_history.HistoryCorrupt):
            plugin.persist_sweep(store, host_at(NOW, {"a": [(-10, 0.2, 86400)]}), "", NOW)
        assert store.path.read_bytes() == before


def test_a_locked_store_skips_the_sweep_and_keeps_the_history(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    plugin.persist_sweep(store, host_at(NOW - 120, {"a": [(-130, 0.2, 86400)]}), "", NOW - 120)
    holder = sqlite3.connect(store.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            plugin.persist_sweep(store, host_at(NOW, {"a": [(-10, 0.3, 86400)]}), "", NOW)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert sorted(p.name for p in tmp_path.iterdir() if "corrupt" in p.name) == []
    assert sqlite3.connect(store.path).execute("SELECT count(*) FROM run").fetchone()[0] == 1


def test_no_identity_or_raw_text_reaches_the_store_or_the_answers(tmp_path):
    secret_email = "person@example.org"
    secret_name = "Personal Work Account"
    secret_detail = "/Users/someone/.config/token-path"
    secret_pid = "alice-profile-7"
    data = payload(
        [snap("claude", secret_pid, [constraint("seven_day", 0.3, reset=86400)], plan="max")],
        [profile("claude", secret_pid, email=secret_email, display_name=secret_name, detail=secret_detail)],
    )
    data["quota"][0]["detail"] = secret_detail
    store = quota_history.HistoryStore(tmp_path)
    plugin.persist_sweep(store, data, "", NOW)
    dump = "\n".join(sqlite3.connect(store.path).iterdump())
    for secret in (secret_email, secret_name, secret_detail, secret_pid, "example.org"):
        assert secret not in dump
    answers = json.dumps(plugin.reserve_view(store, data, NOW, NOW))
    for secret in (secret_email, secret_name, secret_detail, secret_pid):
        assert secret not in answers


# ---------------------------------------------------------------------------
# Plugin lifecycle (MOCKED host), collector cancellation, tool parity


class _Host:
    """MOCKED host: records registrations like a worker process; nothing runs."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.routes, self.tools, self.tasks, self.unload, self.tabs, self.logs = {}, {}, [], [], {}, []

    def get_state_dir(self):
        return str(self.state_dir)

    def get_runtime_info(self):
        return {"server_port": 8765}

    def register_route(self, name, handler, methods=("GET",)):
        self.routes[name] = handler

    def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
        self.tools[name] = {"handler": handler, "description": description, "schema": schema,
                            "timeout_sec": timeout_sec}

    def register_supervised_task(self, name, factory, *, restart_policy="on_failure",
                                 max_restarts=5, backoff_seconds=2.0):
        self.tasks.append((name, factory, restart_policy, max_restarts))

    def on_unload(self, callback):
        self.unload.append(callback)

    def register_ui_tab(self, tab_id, title, *, icon="extension", render=None):
        self.tabs[tab_id] = {"icon": icon, "render": render}

    def log(self, level, message, **fields):
        self.logs.append((level, message))


def test_registration_records_one_collector_and_starts_nothing(tmp_path, monkeypatch):
    def no_network(*_a, **_k):
        raise AssertionError("register() must not touch the network")

    monkeypatch.setattr(plugin, "_request_json", no_network)
    threads = threading.active_count()
    host = _Host(tmp_path)
    plugin.register(host)
    assert [name for name, *_ in host.tasks] == ["quota_collector"]
    assert host.tasks[0][2] == "on_failure" and host.tasks[0][3] == 3
    assert len(host.unload) == 1
    assert threading.active_count() == threads
    assert not any(tmp_path.iterdir())  # nothing written at registration
    tool = host.tools["quota_summary"]
    assert tool["schema"] == plugin.TOOL_SCHEMA
    static = tool["description"] + json.dumps(tool["schema"])
    assert not any(ch.isdigit() for ch in static)  # numbers only ever in results
    assert host.tabs["quotas"]["render"]["start"] == "auto"
    assert len(host.tabs["quotas"]["icon"]) == 1 and host.tabs["quotas"]["icon"] != "gauge"


def test_many_widget_frames_do_not_add_collectors(tmp_path, monkeypatch):
    calls = []
    data = host_at(NOW, {"a": [(-10, 0.2, 86400)]})
    monkeypatch.setattr(plugin, "_request_json",
                        lambda port, path, method="GET", timeout_sec=0: (calls.append((method, path)) or (data, "", 200)))
    host = _Host(tmp_path)
    plugin.register(host)
    threads = threading.active_count()
    for _ in range(5):
        view = host.routes["quotas"]({})
        assert view["reserve"]["summary"]["groups"]
    assert len(host.tasks) == 1
    assert threading.active_count() == threads
    assert set(calls) == {("GET", plugin.STATUS_PATH)}
    assert not (tmp_path / quota_history.HISTORY_FILE).exists()  # routes never write history


def test_reuse_param_answers_from_a_recent_read(tmp_path, monkeypatch):
    calls = []
    data = host_at(NOW, {"a": [(-10, 0.2, 86400)]})
    monkeypatch.setattr(plugin, "_request_json",
                        lambda port, path, method="GET", timeout_sec=0: (calls.append(path) or (data, "", 200)))
    host = _Host(tmp_path)
    plugin.register(host)
    host.routes["quotas"]({})
    view = host.routes["quotas"]({"query_params": {"reuse": "1", "horizon": "7d", "harness": "codex"}})
    assert calls == [plugin.STATUS_PATH]
    assert view["reserve"]["chart"]["horizon"] == "7d"
    host.routes["quotas"]({"query_params": {"horizon": "bogus"}})
    assert len(calls) == 2


def test_reserve_failure_leaves_the_account_view_intact(tmp_path, monkeypatch):
    data = host_at(NOW, {"a": [(-10, 0.2, 86400)]})
    monkeypatch.setattr(plugin, "_request_json", lambda *a, **k: (data, "", 200))
    monkeypatch.setattr(qs, "prepare", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    host = _Host(tmp_path)
    plugin.register(host)
    view = host.routes["quotas"]({})
    assert view["groups"] and view["reserve"]["summary"] is None
    assert "ValueError" in view["reserve"]["error"]


def run_collector(factory, until, timeout=5.0):
    async def main():
        task = asyncio.ensure_future(factory())
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return task

    loop = asyncio.new_event_loop()
    try:
        task = loop.run_until_complete(main())
        return loop, task
    except BaseException:
        loop.close()
        raise


def test_cancellation_during_a_read_prevents_any_later_write(tmp_path):
    host = _Host(tmp_path)
    started, release = threading.Event(), threading.Event()
    data = host_at(NOW, {"a": [(-10, 0.2, 86400)]})

    def slow_read():
        started.set()
        release.wait(5)
        return data, ""

    factory = plugin.make_collector(host, plugin.LatestRead(), threading.Event(), read=slow_read,
                                    interval_sec=0.05, first_delay_sec=0)
    loop, task = run_collector(factory, started.is_set)
    try:
        assert started.is_set()
        task.cancel()
        loop.run_until_complete(asyncio.sleep(0.02))
        assert not task.done()  # stop requested, worker has not settled yet
        assert not factory.control.settled.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(task)
        assert factory.control.settled.is_set()
    finally:
        loop.close()
    assert not (tmp_path / quota_history.HISTORY_FILE).exists()


def test_stop_flag_during_a_read_prevents_the_write(tmp_path):
    host = _Host(tmp_path)
    started, release, stop = threading.Event(), threading.Event(), threading.Event()
    data = host_at(NOW, {"a": [(-10, 0.2, 86400)]})

    def slow_read():
        started.set()
        release.wait(5)
        return data, ""

    factory = plugin.make_collector(host, plugin.LatestRead(), stop, read=slow_read,
                                    interval_sec=0.05, first_delay_sec=0)
    loop, task = run_collector(factory, started.is_set)
    try:
        stop.set()  # what on_unload does
        release.set()
        loop.run_until_complete(asyncio.wait_for(task, 5))
    finally:
        loop.close()
    assert not (tmp_path / quota_history.HISTORY_FILE).exists()


def test_collector_records_failures_as_gaps_and_reuses_a_recent_route_read(tmp_path):
    host = _Host(tmp_path)
    latest = plugin.LatestRead()
    reads = []
    data = host_at(time.time(), {"a": [(-10, 0.2, 86400)]})
    outcomes = [(None, "URLError: refused"), (data, "")]

    def read():
        reads.append(1)
        return outcomes[min(len(reads) - 1, 1)]

    factory = plugin.make_collector(host, latest, threading.Event(), read=read,
                                    interval_sec=0.05, first_delay_sec=0)
    store = quota_history.HistoryStore(tmp_path)

    def sweeps():
        if not store.path.exists():
            return 0
        # SQLite creates the file before the collector commits its schema.
        # File existence alone is not a completed first sweep.
        conn = sqlite3.connect(store.path)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sweep'").fetchone():
                return 0
            return conn.execute("SELECT count(*) FROM sweep").fetchone()[0]
        finally:
            conn.close()

    loop, task = run_collector(factory, lambda: sweeps() >= 2)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        loop.close()
    rows = sqlite3.connect(store.path).execute("SELECT ok, reason FROM sweep ORDER BY at").fetchall()
    assert rows[0] == (0, "status_unreadable")
    assert (1, "") in rows
    # A route read moments ago is recorded instead of a second daemon read.
    latest.put(data, "", time.time())
    before = len(reads)
    factory = plugin.make_collector(host, latest, threading.Event(), read=read,
                                    interval_sec=10.0, first_delay_sec=0)
    count = sweeps()
    loop, task = run_collector(factory, lambda: sweeps() > count)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        loop.close()
    assert len(reads) == before


def test_collector_fails_closed_on_an_unreadable_store_and_says_so_once(tmp_path):
    host = _Host(tmp_path)
    junk = b"not a history" * 50
    (tmp_path / quota_history.HISTORY_FILE).write_bytes(junk)
    reads = []
    data = host_at(time.time(), {"a": [(-10, 0.2, 86400)]})

    def read():
        reads.append(1)
        return data, ""

    factory = plugin.make_collector(host, plugin.LatestRead(), threading.Event(), read=read,
                                    interval_sec=0.01, first_delay_sec=0)
    loop, task = run_collector(factory, lambda: len(reads) >= 5)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        loop.close()
    assert len(reads) >= 5
    assert (tmp_path / quota_history.HISTORY_FILE).read_bytes() == junk
    assert sorted(p.name for p in tmp_path.iterdir()) == ["quota_collector.lock", quota_history.HISTORY_FILE]
    said = [message for level, message in host.logs if "unreadable" in message]
    assert len(said) == 1 and "left as is" in said[0]


def test_ui_and_tool_read_the_same_numbers_and_the_tool_never_refreshes(tmp_path, monkeypatch):
    store = quota_history.HistoryStore(tmp_path)
    timeline = {"a": [(-3600, 0.10, 3 * 86400), (0, 0.16, 3 * 86400)],
                "b": [(-3600, 0.40, 2 * 86400), (-60, 0.40, 2 * 86400)]}
    # Align the first source observation with a watched sweep.
    sweep(store, timeline, -4080, 0)
    data = host_at(NOW, timeline)
    calls = []
    monkeypatch.setattr(plugin, "_request_json",
                        lambda port, path, method="GET", timeout_sec=0: (calls.append((method, path, timeout_sec)) or (data, "", 200)))
    monkeypatch.setattr(plugin.time, "time", lambda: NOW)
    host = _Host(tmp_path)
    plugin.register(host)
    ui = host.routes["quotas"]({})["reserve"]["summary"]
    plugin_latest_cleared = plugin.LatestRead()
    tool = json.loads(host.tools["quota_summary"]["handler"](None, harness="codex"))
    assert all(method == "GET" and path == plugin.STATUS_PATH for method, path, _t in calls)
    assert len(tool["groups"]) == len(ui["groups"])
    for u, t in zip(ui["groups"], tool["groups"]):
        assert t["key"] == u["key"]
        assert t["remaining_windows"] == u["measured"]["windows"]
        assert t["of_accounts"] == u["measured"]["accounts"]
        assert t["average_remaining_pct"] == u["measured"]["average_remaining_pct"]
        assert t["next_reset"] == u["next_reset"]
        assert t["pace_to_reset"] == u["pace_to_reset"]
        assert t["recent_pace"]["windows_per_hour"] == u["recent_pace"]["windows_per_hour"]
        assert t["recent_pace"]["state"] == u["recent_pace"]["state"] == "ok"
    # A fresh process (a worker) reads for itself, with the tool's shorter bound.
    calls.clear()
    fresh = plugin.tool_answer(host, plugin_latest_cleared, "codex", False, now=NOW)
    assert calls == [("GET", plugin.STATUS_PATH, plugin.TOOL_STATUS_TIMEOUT_SEC)]
    assert fresh["groups"][0]["remaining_windows"] == tool["groups"][0]["remaining_windows"]


def test_tool_answer_is_bounded_filtered_and_honest_about_a_failed_read(tmp_path):
    host = _Host(tmp_path)
    snaps = [snap("codex", "a", [constraint(f"pool{i}", 0.1, WEEK, reset=86400)]) for i in range(40)]
    data = payload(snaps, [profile("codex", "a")])
    answer = plugin.tool_answer(host, plugin.LatestRead(), "", False, fetch=lambda: (data, ""), now=NOW)
    assert len(answer["groups"]) == plugin.TOOL_MAX_GROUPS
    assert answer["groups_omitted"] == 40 - plugin.TOOL_MAX_GROUPS
    assert plugin.tool_answer(host, plugin.LatestRead(), "claude", False,
                              fetch=lambda: (data, ""), now=NOW)["groups"] == []
    failed = plugin.tool_answer(host, plugin.LatestRead(), "", True,
                                fetch=lambda: (None, "URLError: refused"), now=NOW)
    assert failed["groups"] == [] and "nothing is claimed" in failed["status_error"]
    assert len(json.dumps(answer)) < 40_000


# ---------------------------------------------------------------------------
# The real widget, driven in-process by bundled Node (fake DOM, no browser)


def _node():
    candidates = [
        os.environ.get("OUROBOROSHUB_NODE", ""),
        str(Path.home() / ".claudexor" / "node" / "bin" / "node"),
        "/Applications/Claudexor.app/Contents/Resources/node",
        shutil.which("node") or "",
    ]
    return next((Path(item) for item in candidates if item and Path(item).is_file()), None)


def widget_reserve(store, data, read_at, now, **kw):
    """The reserve as the widget's first read gets it (0.8.0): the timeline is
    open on every mount, so the answer carries the chart of the family's
    default limit over that limit's own span — a week for a limit longer than
    a day, as the widget asks — and the widget asks for nothing more."""
    out = plugin.reserve_view(store, data, read_at, now, horizon="24h", **kw)
    chart = out.get("chart")
    if chart:
        default = next((g for g in out["summary"]["groups"] if g["key"] == chart["group_key"]), None)
        if default and (default["window_seconds"] or 0) > 86400:
            out = plugin.reserve_view(store, data, read_at, now, horizon="7d", **kw)
    return out


NODE_RESERVE_MATRIX = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
const hasSvg = (root) => walk(root).some((n) => n.getAttribute('class') === 'chart-svg');
// 0.8.0: a limit is one .lrow; its name button (data-focus "limit:<key>")
// carries the whole spoken summary and shows the limit in the timeline.
const rowOf = (env, key) => byFocus(env.root, 'limit:' + key).parentNode;
const has = (c, name) => String(c.className).split(/\s+/).includes(name);
const paths = (env) => walk(env.root).filter((n) => n.tagName === 'PATH').map((n) => n.getAttribute('class'));

(async () => {
  const fixture = JSON.parse(process.env.RESERVE_FIXTURE);
  const PREFIX = process.env.WIDGET_ROUTE_PREFIX;
  const groups = fixture.view.reserve.summary.groups.filter((x) => x.harness === 'codex');
  const tight = groups.find((x) => x.tightest);
  const other = groups.find((x) => x.key !== tight.key);
  assert.ok(tight && other, 'fixture has a tightest limit and another one');
  const chart = fixture.view.reserve.chart;
  assert.equal(chart.group_key, tight.key, 'the skill charts the tightest limit by default');

  // 1. The first screen: every limit as one row with the skill's own figures,
  // the tightest marked and in the timeline, which is open and drawn from the
  // first answer — nothing more is asked.
  let env = await boot(fixture.view);
  let text = env.root.textContent;
  assert.match(text, /Reserve · Codex/);
  groups.forEach((g) => {
    // The figure is the skill's own current windows alone, of the accounts it
    // applies to; last-known windows are said under it, never added in.
    const row = rowOf(env, g.key);
    // 0.8.1: the figure says its unit itself — "of N accounts" — in place of
    // the unit line the overview no longer carries above the rows.
    assert.equal(classes(row, 'l-fig')[0].textContent,
                 (g.measured.accounts ? g.measured.windows.toFixed(2) : '—') + ' of ' + g.slots
                 + (g.slots === 1 ? ' account' : ' accounts'));
    if (g.last_known && g.last_known.accounts) {
      assert.match(classes(row, 'l-sub')[0].textContent, new RegExp('^Last known ' + g.last_known.windows.toFixed(2) + ' · '));
    }
  });
  assert.match(text, /\d+ current · 1 last known \(2 h\)/);
  const stamp = classes(env.root, 'reserve-age')[0];
  assert.match(stamp.title, /provider readings observed \d\d:\d\d/);
  assert.match(stamp.title, /status read \d\d:\d\d/);
  assert.match(stamp.title, /times in /);
  assert.ok(hasSvg(env.root), 'the timeline is open on every mount');
  // 0.8.1: the timeline has one toggle, the charted row's own control; no
  // second one inside the timeline, and no unit line above the rows.
  assert.equal(byFocus(env.root, 'chart-toggle'), undefined);
  assert.equal(classes(env.root, 'chart-toggle').length, 0);
  assert.equal(classes(env.root, 'reserve-unit').length, 0);
  assert.doesNotMatch(text, /a full account counts 1/);
  assert.match(classes(env.root, 'reserve-title')[0].title, /a full account counts 1/);
  assert.equal(classes(env.root, 'l-chart').length, groups.length, 'one control per row');
  assert.equal(classes(env.root, 'l-chart').filter((b) => has(b, 'on')).length, 1);
  assert.equal(byFocus(env.root, 'limit:' + tight.key).textContent, 'Hide chart');
  assert.equal(byFocus(env.root, 'limit:' + other.key).textContent, 'Show chart');
  assert.equal(env.calls.length, 1, env.calls.map((c) => c.url).join());
  assert.ok(env.calls[0].url.startsWith(PREFIX + 'quotas?'));
  assert.doesNotMatch(env.calls[0].url, /chart=0|reuse=1/);
  // The tightest row wears the mark, in words too, and is the one in the timeline.
  const tightName = byFocus(env.root, 'limit:' + tight.key);
  assert.equal(classes(rowOf(env, tight.key), 'rs-tight').length, 1);
  assert.match(tightName.getAttribute('aria-label'), /lowest average share left in this family/);
  assert.equal(tightName.getAttribute('aria-pressed'), 'true');
  assert.ok(has(rowOf(env, tight.key), 'sel'));
  // The timeline stands right under the selected limit's row.
  const after = rowOf(env, tight.key).parentNode.childNodes;
  assert.ok(has(after[after.indexOf(rowOf(env, tight.key)) + 1], 'tl-block'));
  assert.equal(classes(rowOf(env, other.key), 'rs-tight').length, 0);
  // One bar per account the limit applies to, as the skill sends them:
  // restricted ones marked, unknown ones outlined; the row says it in words.
  const cells = classes(rowOf(env, tight.key), 'bar');
  assert.equal(cells.length, tight.bars.length);
  assert.equal(cells.filter((c) => has(c, 'unknown')).length, tight.bars.filter((b) => b.state === 'unknown').length);
  assert.equal(cells.filter((c) => has(c, 'last')).length, tight.bars.filter((b) => b.state === 'last_known').length);
  assert.equal(cells.filter((c) => has(c, 'restricted')).length, tight.bars.filter((b) => b.restricted).length);
  assert.ok(cells.filter((c) => has(c, 'restricted')).length > 0);
  assert.match(tightName.getAttribute('aria-label'), /1 stale/);
  assert.match(tightName.getAttribute('aria-label'), /1 cooldown reported \(0\.86\)/);
  assert.match(text, /cooling down/);
  // The next reported reset stands in the row, with what it gives back.
  assert.match(classes(rowOf(env, tight.key), 'l-tail')[0].textContent,
               new RegExp('next reset .*\\+' + tight.next_reset_returns.toFixed(2) + ' if unused'));

  // 2. The timeline: the record behind now and one future ahead of it, from
  // the row's own figure, with its reset schedule and checkpoints; no estimate
  // cohort, no refill toggle, no point marks or hatching, no "no new use"
  // line hidden away.
  assert.ok(paths(env).includes('line-observed'));
  assert.ok(paths(env).includes('line-scenario'));
  assert.ok(!paths(env).some((c) => /line-pace|line-hold|line-cohort|line-refill|gap-band/.test(c)), paths(env).join());
  assert.equal(walk(env.root).filter((n) => /point-mark|rail-line/.test(String(n.getAttribute('class') || ''))).length, 0);
  text = env.root.textContent;
  // 0.8.1: the future's assumption is said once, in its legend line.
  assert.match(classes(env.root, 'chart-legend')[0].textContent,
               /no new use · 2 current accounts keep their share, each refilled once at its next reported reset/);
  assert.equal((text.match(/refilled once at its next reported reset/g) || []).length, 1, 'said once on the open screen');
  assert.equal(classes(env.root, 'lead').length, 0);
  assert.match(text, /Reported resets · no new use/);
  assert.match(text, /Left if nothing more is used: in 24 h ≈ /);
  assert.match(text, /Details, notes and data/);
  assert.match(text, /Recorded since /);
  assert.match(text, /times in /);
  const schedule = classes(env.root, 'schedule')[0];
  const sc = chart.scenarios.no_new_use;
  const rowsShown = Math.min(2, sc.schedule.length);
  assert.equal(classes(schedule, 'n').filter((n) => n.tagName === 'TD').length, rowsShown * 3);
  if (sc.schedule.length) {
    assert.match(schedule.textContent, new RegExp('\\+' + sc.schedule[0].adds.toFixed(2) + sc.schedule[0].total_after.toFixed(2)
      + ' of ' + chart.y_max));
  }
  // History explains its carry style; the future names its fresh-only basis.
  assert.match(classes(env.root, 'chart-legend')[0].textContent,
               new RegExp('history · solid: recorded; dashed: carried.*no new use · ' + chart.accounts + ' current'));

  // The keyboard cursor and the spoken read-out say what the chart says, for
  // the limit on screen: history may carry accounts excluded from the fresh-only figure.
  const plot = byFocus(env.root, 'chart-plot');
  const readout = classes(env.root, 'chart-readout')[0];
  plot.listeners.focus[0]();
  const name = classes(rowOf(env, tight.key), 'l-name-text')[0].textContent;
  assert.ok(readout.textContent.includes(name), readout.textContent);
  const historyNow = chart.history.details.at(-1);
  assert.match(readout.textContent, new RegExp('recorded ' + historyNow.value.toFixed(2) + ' of '
    + historyNow.accounts + ' accounts?\\b'));
  assert.match(readout.textContent, /carried|recorded/);
  assert.match(readout.textContent, new RegExp('no new use ' + tight.measured.windows.toFixed(2) + ' of '
    + chart.accounts + ' current accounts?'));
  const tipText = classes(env.root, 'chart-tip')[0].textContent;
  assert.ok(tipText.includes(name + ' · account-windows left, scale ' + chart.y_max), tipText);
  plot.listeners.keydown[0]({ key: 'End', preventDefault() {} });
  assert.match(readout.textContent, /· scenario — .*no new use \d+\.\d\d of \d+ current accounts?/);
  plot.listeners.keydown[0]({ key: 'Home', preventDefault() {} });
  assert.match(readout.textContent, /recorded /);

  // The future is a choice beside the chart: recent pace is the same chart's
  // other scenario — drawn at once, nothing asked — with its own schedule.
  click(env, 'scenario:recent_pace');
  assert.equal(env.calls.length, 1);
  assert.match(env.root.textContent, /Reported resets · recent pace/);
  const pace = chart.scenarios.recent_pace;
  assert.match(classes(env.root, 'chart-legend')[0].textContent,
               new RegExp('recent pace · ' + pace.at_pace + ' of ' + pace.accounts + ' current'));
  assert.match(env.root.textContent, new RegExp(pace.at_pace + ' of ' + pace.accounts + ' current at their pace of the last '));
  click(env, 'scenario:no_new_use');

  // Choosing another limit (its row's name) asks once, with reuse, for that
  // limit's chart, and its timeline stands under its own row.
  click(env, 'limit:' + other.key);
  assert.match(env.root.textContent, /Loading the timeline for this limit/);
  await settle();
  assert.equal(env.calls.length, 2);
  assert.match(env.calls[1].url, /reuse=1/);
  assert.match(env.calls[1].url, new RegExp('group=' + encodeURIComponent(other.key).replace(/[|]/g, '\\|')));
  await settle();
  assert.equal(env.calls.length, 2, 'the fake host answers without it: not asked again');
  click(env, 'limit:' + tight.key);
  await settle();
  click(env, 'horizon:24h');
  await settle();
  assert.match(env.calls.at(-1).url, /horizon=24h/);

  // 3. Folding the timeline: the next timed read leaves the chart out (and
  // the host answers without one); unfolding it asks once, with reuse.
  const noChart = JSON.parse(JSON.stringify(fixture.view));
  delete noChart.reserve.chart;
  env = await boot((url) => (/chart=0/.test(url) ? noChart : fixture.view));
  // 0.8.1: the charted row's own control folds it; folded, nothing stands
  // under the row and no row is drawn as charted.
  click(env, 'limit:' + tight.key);
  assert.ok(!hasSvg(env.root));
  assert.equal(classes(env.root, 'tl-block').length, 0);
  assert.equal(byFocus(env.root, 'limit:' + tight.key).textContent, 'Show chart');
  assert.equal(byFocus(env.root, 'limit:' + tight.key).getAttribute('aria-pressed'), 'false');
  assert.equal(classes(env.root, 'sel').length, 0);
  assert.equal(classes(env.root, 'l-chart').filter((b) => has(b, 'on')).length, 0);
  env.interval()();
  await settle();
  assert.match(env.calls.at(-1).url, /chart=0/);
  click(env, 'limit:' + tight.key);
  await settle();
  assert.match(env.calls.at(-1).url, /reuse=1/);
  assert.ok(hasSvg(env.root));
  assert.ok(has(rowOf(env, tight.key), 'sel'));

  // 4. The limit's details, notes and data table sit under the timeline;
  // open, they survive the timed redraw, with the keyboard on them. About
  // carries the unit and the caveats, folded until asked.
  env = await boot(fixture.view);
  const details = classes(env.root, 'chart-table')[0];
  text = details.textContent;
  assert.match(text, /mixed plans/);
  assert.match(text, /pro: 0\.86 account-windows across 1 account/);
  assert.match(text, /prolite: 0\.48 account-windows across 1 account/);
  assert.match(text, /1 stale/);
  assert.match(text, /1 cooldown reported \(0\.86\)/);
  assert.match(text, /Recent pace: /);
  assert.match(text, /Even use/);
  assert.match(text, /to each account’s reported reset/);
  assert.match(text, /Only each account’s next reported reset is applied/);
  // The notes describe the futures drawn, never the older cohort line that
  // stopped at the first reset (kept apart, by field, and not shown).
  assert.match(text, /Recent pace: every account read now/);
  assert.doesNotMatch(text, /stops at the first reported reset|Refill scenario|\(mixed\)/);
  assert.ok(!details.open);
  details.open = true;
  details.listeners.toggle[0]();
  byFocus(env.root, 'chart-table').focus();
  const callsBefore = env.calls.length;
  env.interval()();
  await settle();
  assert.ok(env.calls.length > callsBefore, 'the poll read again');
  assert.doesNotMatch(env.calls.at(-1).url, /chart=0/, 'an open timeline is read with the poll');
  assert.ok(hasSvg(env.root));
  const reopened = classes(env.root, 'chart-table')[0];
  assert.notEqual(reopened, details);
  assert.equal(reopened.open, true);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'chart-table');
  reopened.open = false;
  reopened.listeners.toggle[0]();
  env.interval()();
  await settle();
  assert.ok(!classes(env.root, 'chart-table')[0].open);
  assert.doesNotMatch(env.root.textContent, /not tokens or hours|Not tokens or hours/);
  click(env, 'about');
  assert.match(env.root.textContent, /Not tokens or hours/);
  assert.match(env.root.textContent, /never added together/);
  assert.match(env.root.textContent, /not a dispatch guarantee/);

  // 5. Warming up and an unavailable overview are said in words; the account
  // list stands open when there is no overview to fold it under.
  env = await boot(fixture.warming);
  assert.match(classes(env.root, 'chart-table')[0].textContent, /warming up/);
  assert.match(classes(env.root, 'chart-table')[0].textContent, /at least 15 minutes.*trailing hour/);
  click(env, 'scenario:recent_pace');
  assert.match(classes(env.root, 'chart-legend')[0].textContent,
               /recent pace · 2 current accounts held at their shares; each refilled once at its next reported reset; no later reset is assumed\. No account has a measured pace yet \(warming up: .*\): the same as no new use/);
  env = await boot(fixture.failed);
  assert.match(env.root.textContent, /Reserve overview unavailable/);
  assert.match(env.root.textContent, /The account list below is unaffected/);
  assert.equal(byFocus(env.root, 'accounts'), undefined);
  assert.equal(classes(env.root, 'acc-row').length, 3);

  // 6. Two limits with one label, "7 day (Fable)", scoped to different models
  // stay two limits on screen, in words and in what a screen reader hears.
  env = await boot(fixture.scoped);
  const rows = classes(env.root, 'lrow');
  assert.equal(rows.length, 2);
  // 0.8.1: the row's spoken summary is on its one control (.l-chart).
  const heard = rows.map((r) => classes(r, 'l-chart')[0].getAttribute('aria-label'));
  assert.match(heard[0], /models: fable-a, fable-b/);
  assert.match(heard[1], /models: fable-c, fable-d/);
  const names = rows.map((r) => classes(r, 'l-name-text')[0].textContent);
  assert.notEqual(names[0], names[1]);
  // Family accounts without a reading of the limit are said, not counted as zero.
  assert.match(env.root.textContent, /0\.70 of 1/);
  assert.match(heard[0], /2 other accounts: no reading, limit may not apply/);
  const scopedTight = fixture.scoped.reserve.summary.groups.find((x) => x.tightest);
  assert.match(classes(env.root, 'tl-scope')[0].textContent, new RegExp('models: ' + scopedTight.models.join(', ')));
  // The outage is carried as a dashed interval, while the solid intervals
  // remain separate. No band or repeated marker rail is added.
  const observedLine = walk(env.root).find((n) => n.getAttribute('class') === 'line-observed' && n.tagName === 'PATH');
  const pieces = observedLine.getAttribute('d').split('M').filter(Boolean);
  assert.ok(pieces.length >= 2);
  assert.match(pieces[0], /L/);
  assert.ok(paths(env).includes('line-carried'));
  assert.ok(!walk(env.root).some((n) => /gap-band/.test(String(n.getAttribute('class') || ''))));

  // 7. The legend names a recorded line only where one is drawn: a value
  // held over a stretch of time. A value alone at its instant, a gap at once
  // after it, draws nothing — the legend says there is no record.
  const lone = JSON.parse(JSON.stringify(fixture.view));
  const lc = lone.reserve.chart;
  delete lc.history; // Legacy answer compatibility: strict measured-only series.
  const mid = lc.now - 600;
  lc.past = [[lc.start, null], [mid, 1.2], [mid, null], [lc.now, null]];
  env = await boot(lone);
  assert.match(classes(env.root, 'chart-legend')[0].textContent, /no record in this span yet/);
  assert.doesNotMatch(classes(env.root, 'chart-legend')[0].textContent, /recorded · /);
  lc.past = [[lc.start, null], [mid, 1.2], [mid + 300, null], [lc.now, null]];
  env = await boot(lone);
  assert.match(classes(env.root, 'chart-legend')[0].textContent,
               new RegExp('recorded · ' + lc.past_accounts + ' accounts?'));

  // Zero qualified pace still draws a future of all current accounts. Its
  // visible legend must name that count and the held/refill assumptions even
  // when a different cohort produced the recorded line.
  for (const count of [1, 2]) {
    const noPace = JSON.parse(JSON.stringify(fixture.warming));
    const c = noPace.reserve.chart;
    delete c.history; // Older API response still renders its named cohort.
    c.scenarios.recent_pace.accounts = count;
    c.scenarios.recent_pace.held = count;
    assert.equal(c.scenarios.recent_pace.at_pace, 0);
    c.past_accounts = count + 1;
    c.past = [[c.now - 600, 1], [c.now, 1]];
    env = await boot(noPace);
    click(env, 'scenario:recent_pace');
    const legend = classes(env.root, 'chart-legend')[0].textContent;
    assert.ok(legend.includes('recorded · ' + (count + 1) + ' accounts'), legend);
    assert.ok(legend.includes('recent pace · ' + count + ' current account'
      + (count === 1 ? ' held at its share' : 's held at their shares')), legend);
    assert.match(legend, /each refilled once at its next reported reset; no later reset is assumed/);
    assert.match(legend, /No account has a measured pace yet/);
    assert.equal((legend.match(/refilled once/g) || []).length, 1);
  }
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _widget_fixture(tmp_path):
    now = float(int(time.time()))
    base = plugin.build_view(None, "")
    rows = {
        "c1": [(-3600, 0.10, 3 * 86400), (-30, 0.14, 3 * 86400)],
        "c2": [(-3600, 0.50, 2 * 86400), (-40, 0.52, 2 * 86400)],
    }
    store = quota_history.HistoryStore(tmp_path)
    for t in [now - 4080 + 120 * k for k in range(35)] + [now]:
        snaps = []
        for sid, points in rows.items():
            seen = [p for p in points if now + p[0] <= t]
            if seen:
                o, r, reset = seen[-1]
                snaps.append({"subject": {"harness": "codex", "subject_id": sid,
                                          "plan_label": "pro" if sid == "c1" else "prolite"},
                              "constraints": [{"id": "codex:primary", "label": "codex primary", "used_ratio": r,
                                               "window_seconds": WEEK, "resets_at": qs.iso(now + reset)}],
                              "availability": {"state": "available"}, "observed_at": qs.iso(now + o),
                              "freshness": "fresh", "source": "app"})
        data = payload(snaps, [profile("codex", "c1"), profile("codex", "c2"), profile("codex", "c3")],
                       harnesses=("codex",))
        plugin.persist_sweep(store, data, "", t)
    data["quota"].append({"subject": {"harness": "codex", "subject_id": "c3"},
                          "constraints": [{"id": "primary", "used_ratio": 0.3, "window_seconds": WEEK,
                                           "resets_at": qs.iso(now + 86400)},
                                          {"id": "cooldown", "used_ratio": None, "window_seconds": None,
                                           "cooldown_until": qs.iso(now + 600)}],
                          "availability": {"state": "available"}, "observed_at": qs.iso(now - 7200),
                          "freshness": "stale", "source": "rollout"})
    data["quota"].append({"subject": {"harness": "codex", "subject_id": "c1"},
                          "constraints": [{"id": "base_model_inference:primary", "label": "gpt-reserve primary",
                                           "used_ratio": 0.0, "window_seconds": WEEK,
                                           "resets_at": qs.iso(now - 20 + WEEK)}],
                          "availability": {"state": "available"}, "observed_at": qs.iso(now - 20),
                          "freshness": "fresh", "source": "app2"})
    data["quota"][0]["constraints"].append({"id": "cooldown", "used_ratio": None, "window_seconds": None,
                                            "cooldown_until": qs.iso(now + 900)})
    view = plugin.build_view(data, "")
    # As the widget route answers its first read: account keys on the bars,
    # and the chart of the default limit over its own span (widget_reserve).
    view["reserve"] = widget_reserve(store, data, now, now, harness="codex", name_accounts=True)
    warming = plugin.build_view(data, "")
    warming["reserve"] = widget_reserve(quota_history.HistoryStore(tmp_path / "empty"), data, now, now,
                                        harness="codex", name_accounts=True)
    failed = plugin.build_view(data, "")
    failed["reserve"] = {"summary": None, "chart": None, "error": "reserve summary failed (ValueError)"}
    assert view["reserve"]["chart"]["recent_pace"], view["reserve"]["summary"]["groups"]
    return {"view": view, "warming": warming, "failed": failed, "base": base,
            "scoped": _scoped_view(tmp_path / "scoped", now)}


def _scoped_view(folder, now):
    """One account with two model-scoped limits that share a label, two
    family accounts with no reading of them, and a collector outage."""
    store = quota_history.HistoryStore(folder)

    def status():
        scoped = [{"id": "weekly_scoped:Fable", "label": "7 day (Fable)", "used_ratio": used,
                   "window_seconds": WEEK, "resets_at": qs.iso(now + 2 * 86400),
                   "applies_to_models": models}
                  for used, models in ((0.3, ["fable-b", "fable-a"]), (0.6, ["fable-c", "fable-d"]))]
        return payload([{"subject": {"harness": "codex", "subject_id": "c1"}, "constraints": scoped,
                         "availability": {"state": "available"}, "observed_at": qs.iso(now - 7300),
                         "freshness": "fresh", "source": "app"}],
                       [profile("codex", sid) for sid in ("c1", "c2", "c3")], harnesses=("codex",))

    t = now - 7200
    while t <= now:
        if not (now - 5400 < t < now - 3000):  # Ouroboros closed in between
            plugin.persist_sweep(store, status(), "", t)
        t += 120
    view = plugin.build_view(status(), "")
    view["reserve"] = widget_reserve(store, status(), now, now, harness="codex", name_accounts=True)
    groups = view["reserve"]["summary"]["groups"]
    assert [g["models"] for g in groups] == [["fable-a", "fable-b"], ["fable-c", "fable-d"]]
    assert groups[0]["coverage"]["other_family_accounts"] == 2
    return view


def test_real_widget_renders_the_skill_numbers_and_asks_for_charts_once(tmp_path):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_RESERVE_MATRIX)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path),
             "WIDGET_ROUTE_PREFIX": test_quotas._widget_route_prefix(widget_path),
             "RESERVE_FIXTURE": json.dumps(_widget_fixture(tmp_path))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 0.6.1: what the overview is drawn from, the tightest limit, and where a
# scenario line has to stop


def test_shares_are_the_measured_accounts_sorted_and_add_up_to_the_figure():
    cooling = constraint("cooldown", None, window=None, cooldown=at(900))
    data = payload(
        [snap("codex", "a", [constraint("primary", 0.8, reset=86400)]),
         snap("codex", "b", [constraint("primary", 0.2, reset=86400), cooling]),
         snap("codex", "c", [constraint("primary", 0.5, reset=86400)]),
         snap("codex", "d", [constraint("primary", 0.1, reset=86400)], fresh=False)],
        [profile("codex", sid) for sid in "abcd"],
    )
    summary, _ = summary_of(data)
    g = group(summary, "|primary|")
    # Fullest first; a stale reading is not a share (it is coverage), and a
    # restricted account is still counted, marked as such.
    assert g["shares"] == [{"left": 0.8, "at_limit": False, "restricted": True},
                           {"left": 0.5, "at_limit": False, "restricted": False},
                           {"left": 0.2, "at_limit": False, "restricted": False}]
    assert sum(s["left"] for s in g["shares"]) == pytest.approx(g["measured"]["windows"])
    assert g["coverage"]["stale_only"] == 1
    # Nothing that names an account travels with it, and the tool stays compact.
    assert set(g["shares"][0]) == {"left", "at_limit", "restricted"}
    assert "shares" not in json.dumps(qs.compact(summary))


def test_the_tightest_limit_is_the_smallest_average_share_per_family():
    five, week, fable = [], [], []
    for i in range(9):
        five.append(constraint("five_hour", 0.0 if i < 6 else 0.25, window=FIVE_H,
                               reset=None if i < 6 else 3600, label="5 hour"))
        week.append(constraint("seven_day", 0.53, reset=3 * 86400, label="7 day"))
        fable.append(constraint("seven_day_fable", 1.0 if i < 5 else 0.44, reset=3 * 86400,
                                label="7 day (Fable)", models=["fable", "claude-fable-5"]))
    snaps = [snap("claude", f"c{i}", [five[i], week[i], fable[i]]) for i in range(9)]
    snaps.append(snap("codex", "x", [constraint("primary", 0.9, reset=86400)]))
    snaps.append(snap("codex", "x", [constraint("base_model_inference:primary", 0.95, reset=86400,
                                                label="gpt-reserve primary")], source="app2"))
    data = payload(snaps, [profile("claude", f"c{i}") for i in range(9)] + [profile("codex", "x")])
    summary, state = summary_of(data)
    claude = [g for g in summary["groups"] if g["harness"] == "claude"]
    # A nearly full short limit does not stand for the family: the weekly
    # Fable limit, with five accounts at it, is the tightest.
    assert [g["meaning"] for g in claude if g["tightest"]] == ["seven_day_fable"]
    assert group(summary, "five_hour")["measured"]["average_remaining_pct"] > 90
    assert [g["meaning"] for g in summary["groups"] if g["harness"] == "codex" and g["tightest"]] \
        == ["base_model_inference:primary"]
    # One per family; the tool carries the same mark on the same limit.
    tool = qs.compact(summary)["groups"]
    assert [row["limit"] for row in tool if row.get("tightest_in_family")] == \
        ["7 day (Fable)", "gpt-reserve primary"]
    # The chart opens on it when no limit is asked for.
    assert qs.build_chart(state, harness="claude")["group_key"] == group(summary, "seven_day_fable")["key"]
    assert qs.build_chart(state, harness="codex")["group_key"] == \
        group(summary, "base_model_inference")["key"]


def test_tightest_ties_prefer_more_accounts_at_the_limit_and_skip_unmeasured_limits():
    data = payload(
        [snap("codex", "a", [constraint("one", 0.5, reset=86400), constraint("two", 1.0, reset=86400),
                             constraint("three", 0.2, reset=86400)]),
         snap("codex", "b", [constraint("one", 0.5, reset=86400), constraint("two", 0.0, reset=86400)]),
         snap("codex", "c", [constraint("three", 0.1, reset=86400)], fresh=False)],
        [profile("codex", sid) for sid in "abc"],
    )
    summary, _ = summary_of(data)
    # "one" and "two" both average 50% left; "two" has an account at the limit.
    assert [g["meaning"] for g in summary["groups"] if g["tightest"]] == ["two"]
    stale_only = payload([snap("codex", "c", [constraint("x", 0.9, reset=86400)], fresh=False)],
                         [profile("codex", "c")])
    summary, _ = summary_of(stale_only)
    assert not any(g["tightest"] for g in summary["groups"])


def scenario_state(rows, window=FIVE_H):
    """Members (sid, used, reset offset or None, pace per hour) of one limit."""
    data = payload([snap("claude", sid, [constraint("five_hour", used, window=window, reset=reset)])
                    for sid, used, reset, _rate in rows],
                   [profile("claude", sid) for sid, *_ in rows])
    state = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    rates = {sid: rate for sid, _u, _r, rate in rows}
    for member in state.groups[0].members:
        member.rate = {"state": "ok", "windows_per_hour": rates[member.subject_id], "span_seconds": 3600}
    return state


def test_scenarios_run_to_the_horizon_with_only_the_next_reported_reset():
    """0.6.1 repair: no cycle is inferred. The scenario lines reach the chosen
    horizon; each account refills once, at its own next reported reset, and
    nothing refills it again, as the chart's own words say."""
    # a: 40% used, resets in an hour, keeps using 10%/h; b: unused, no reset.
    state = scenario_state([("a", 0.4, 3600, 0.1), ("b", 0.0, None, 0.0)])
    chart = qs.build_chart(state, horizon="24h")
    # 0.7.0: the refill is the separate scenario; the estimate itself stops
    # at a's reported reset, an hour ahead.
    for line in ("no_new_use", "recent_pace_refill_scenario"):
        assert chart[line][-1][0] == chart["end"]
    assert chart["recent_pace"][-1][0] == round(NOW + 3600)
    assert line_at(chart["recent_pace"], NOW + 3600) == pytest.approx(0.5 + 1.0)
    assert "no_new_use_until" not in chart and "recent_pace_until" not in chart
    pace, hold = chart["recent_pace_refill_scenario"], chart["no_new_use"]
    # a refills at its reported reset, then keeps its pace until it runs out
    # (ten hours later) and stays out: no second, unreported refill.
    assert line_at(pace, NOW + 3600, right=True) == pytest.approx(1.0 + 1.0)
    assert line_at(pace, NOW + 3600 + 5 * 3600) == pytest.approx(0.5 + 1.0, abs=1e-3)
    assert line_at(pace, NOW + 3600 + 10 * 3600) == pytest.approx(1.0, abs=1e-3)
    assert line_at(pace, NOW + 86400) == pytest.approx(1.0, abs=1e-3)
    after = [v for t, v in pace if t > NOW + 3600]
    assert all(later <= earlier + 1e-9 for earlier, later in zip(after, after[1:]))
    assert line_at(hold, NOW + 86400) == pytest.approx(2.0)
    events = [row["event"] for row in chart["table"] if row.get("event")]
    assert not any("stops" in e or "cycle" in e for e in events), events
    words = " ".join(chart["assumptions"])
    assert "no additional unreported resets" in words
    assert "not how far it is reliable" in words
    assert "unreported cycle would begin" not in words


def test_a_used_window_with_no_reported_reset_holds_or_drains_without_an_invented_reset():
    state = scenario_state([("a", 0.3, None, 0.05), ("b", 0.0, None, 0.0)])
    for horizon in ("24h", "7d"):
        chart = qs.build_chart(state, horizon=horizon)
        assert chart["no_new_use"][-1] == [chart["end"], 1.7]
        # 0.7 left at 5%/h runs out after 14 hours and stays out.
        assert line_at(chart["recent_pace"], NOW + 14 * 3600) == pytest.approx(1.0, abs=1e-3)
        assert chart["recent_pace"][-1] == [chart["end"], 1.0]
        assert chart["resets"] == []


def test_weekly_lines_reach_both_horizons_uncut():
    for horizon in ("24h", "7d"):
        chart = qs.build_chart(forecast_state(), horizon=horizon)
        assert chart["recent_pace_refill_scenario"][-1][0] == chart["end"]
        assert chart["no_new_use"][-1][0] == chart["end"]
        # The estimate proper ends at the first reported reset (x, 8 h).
        assert chart["recent_pace"][-1][0] == round(NOW + 8 * 3600)


def test_the_route_leaves_the_chart_out_when_the_widget_has_it_folded(tmp_path, monkeypatch):
    real_now = time.time()  # the route reads the clock itself
    limit = {"id": "primary", "label": "primary", "used_ratio": 0.4, "window_seconds": WEEK,
             "resets_at": qs.iso(real_now + 86400)}
    data = payload([snap("codex", "a", [limit], observed_abs=real_now - 60)],
                   [profile("codex", "a")], harnesses=("codex",))
    monkeypatch.setattr(plugin, "_request_json", lambda *_a, **_k: (data, "", 200))
    host = _Host(tmp_path)
    plugin.register(host)
    folded = host.routes["quotas"]({"query_params": {"harness": "codex", "chart": "0"}})
    assert "chart" not in folded["reserve"]
    assert folded["reserve"]["summary"]["groups"][0]["measured"]["windows"] == 0.6
    opened = host.routes["quotas"]({"query_params": {"harness": "codex", "horizon": "7d"}})
    assert opened["reserve"]["chart"]["horizon"] == "7d"
    # The same figures either way, and the tool's: one calculation.
    assert opened["reserve"]["summary"]["groups"] == folded["reserve"]["summary"]["groups"]
    tool = json.loads(host.tools["quota_summary"]["handler"](None, "codex"))
    assert tool["groups"][0]["remaining_windows"] == 0.6


# ---------------------------------------------------------------------------
# 0.6.1 repair: a lossless observed line, source-specific continuity and the
# bounded meaning of the collector's unbroken watch


def _scripted_sweeps(store, script, offsets, sids=("a", "b"), harness="codex"):
    """One collector sweep per offset; ``script(offset)`` lists the snapshots
    the host reports then. Returns the last payload."""
    data = None
    for offset in offsets:
        data = payload(script(offset), [profile(harness, sid) for sid in sids], harnesses=(harness,))
        plugin.persist_sweep(store, data, "", NOW + offset)
    return data


def _held_after(offset, offsets, seen):
    """An independent oracle for the observed line just after one sweep.

    ``seen(offset)`` maps account -> {source: (observed, ratio)} for what the
    host reported fresh and numeric at that sweep. A source holds its reading
    from a sweep until the next sweep only when that next sweep sees the
    source too (for the last sweep: until now); the newest observation among
    the held sources is the account's value; an account with none held is a
    gap. Readings here never report a reset inside the stretch checked."""
    later = [o for o in offsets if o > offset]
    now_side = seen(offset)
    next_side = seen(later[0]) if later else now_side
    total = 0.0
    for account, sources in now_side.items():
        held = [(obs, ratio) for source, (obs, ratio) in sources.items()
                if source in next_side.get(account, {})]
        if not held:
            return None
        total += 1.0 - max(held)[1]
    return round(total, 4)


@pytest.mark.parametrize("horizon", ["24h", "7d"])
def test_every_recorded_change_is_a_vertex_even_inside_one_sample_bucket(tmp_path, horizon):
    """Finding 1: the line used to keep only the first change after each
    15-minute (24 h) or 2-hour (7 d) sample and held a stale value after it."""
    store = quota_history.HistoryStore(tmp_path)
    # Account a changes at six sweeps in a row: use, a manual restore (a drop)
    # and use again; b holds. All inside one 2-hour and across 15-minute buckets.
    changes = [(-1320, .12), (-1200, .15), (-1080, .11), (-960, .20), (-840, .21), (-720, .26)]

    def ratio_a(offset):
        return [r for o, r in [(-10 ** 6, .10)] + changes if o <= offset][-1]

    def seen(offset):
        return {"a": {"app": (offset - 5, ratio_a(offset))}, "b": {"app": (-3600, .5)}}

    def script(offset):
        return [snap("codex", "a", [constraint("primary", ratio_a(offset), reset=86400)],
                     observed_abs=NOW + offset - 5),
                snap("codex", "b", [constraint("primary", .5, reset=86400)], observed=-3600)]

    offsets = list(range(-3600, 1, 120))
    data = _scripted_sweeps(store, script, offsets)
    past = plugin.reserve_view(store, data, NOW, NOW, horizon=horizon)["chart"]["past"]
    for offset in offsets:
        assert observed(past, offset + 1) == pytest.approx(_held_after(offset, offsets, seen)), offset
    # Only equal neighbours are dropped; the last vertex is the live "now".
    values = [v for _t, v in past[:-1]]
    assert all(x != y for x, y in zip(values, values[1:])), past
    assert observed(past, -3601) is None


def test_source_selection_and_expiry_inside_one_bucket_are_vertices(tmp_path):
    """Finding 1 with two sources: a newer source taking over, going stale for
    one sweep and returning with the same cached reading, then changing and
    disappearing — every change of the chosen value and every edge is drawn."""
    store = quota_history.HistoryStore(tmp_path)

    def rollout(offset):
        if -1440 <= offset <= -1320 or -1080 <= offset <= -960:
            return (-1500, .33)
        if offset == -840:
            return (-845, .36)
        return None

    def seen(offset):
        a = {"app": (-3000, .30)}
        if rollout(offset):
            a["rollout"] = rollout(offset)
        return {"a": a, "b": {"app": (-3000, .5)}}

    def script(offset):
        rows = [snap("codex", "a", [constraint("primary", .30, reset=86400)], observed=-3000),
                snap("codex", "b", [constraint("primary", .5, reset=86400)], observed=-3000)]
        if offset == -1200:  # the newer source goes stale for one sweep
            rows.append(snap("codex", "a", [constraint("primary", .33, reset=86400)], source="rollout",
                             observed=-1500, fresh=False))
        elif rollout(offset):
            obs, ratio = rollout(offset)
            rows.append(snap("codex", "a", [constraint("primary", ratio, reset=86400)], source="rollout",
                             observed=obs))
        return rows

    offsets = list(range(-2400, 1, 120))
    data = _scripted_sweeps(store, script, offsets)
    for horizon in ("24h", "7d"):
        past = plugin.reserve_view(store, data, NOW, NOW, horizon=horizon)["chart"]["past"]
        for offset in offsets:
            assert observed(past, offset + 1) == pytest.approx(_held_after(offset, offsets, seen)), \
                (horizon, offset, past)
    assert observed(past, -1439) == pytest.approx(1.17)  # the newer source is chosen
    assert observed(past, -1300) == pytest.approx(1.2)   # not vouched past its last sighting
    assert observed(past, -1079) == pytest.approx(1.17)  # back, with the same cached reading
    assert observed(past, -839) == pytest.approx(1.2)    # seen once, then gone: never held


@pytest.mark.parametrize("obs_mode", ["cached", "moving"])
@pytest.mark.parametrize("kind", ["stale", "missing", "unreadable"])
def test_a_sweep_that_does_not_see_one_source_breaks_only_that_source(tmp_path, kind, obs_mode):
    """Finding 2: a whole-sweep success used to carry an individual stale,
    missing or unreadable reading over, and the same cached reading returning
    two minutes later extended the old run across the hole."""
    store = quota_history.HistoryStore(tmp_path)

    def obs(offset):
        return NOW - 4000 if obs_mode == "cached" else NOW + offset - 5

    def script(offset):
        a = snap("codex", "a", [constraint("primary", .2, reset=86400)], observed_abs=obs(offset))
        b = snap("codex", "b", [constraint("primary", .4, reset=86400)], observed_abs=obs(offset))
        if offset == -1800:
            if kind == "stale":
                b["freshness"] = "stale"
            elif kind == "missing":
                return [a]
            else:
                b["constraints"][0]["used_ratio"] = 1.4
        return [a, b]

    data = _scripted_sweeps(store, script, range(-3600, 1, 120))
    with sqlite3.connect(store.path) as conn:
        salt = conn.execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
        runs = conn.execute("SELECT subject, first_seen, last_seen, after_gap FROM run ORDER BY id").fetchall()
    of = {qs.pseudo_id(salt, "codex", sid): sid for sid in "ab"}
    a_runs = [(r[1] - NOW, r[2] - NOW, r[3]) for r in runs if of[r[0]] == "a"]
    b_runs = [(r[1] - NOW, r[2] - NOW, r[3]) for r in runs if of[r[0]] == "b"]
    assert a_runs == [(-3600, 0, 0)]                    # the healthy neighbour is one run
    assert b_runs == [(-3600, -1920, 0), (-1680, 0, 1)]  # b restarts after the hole
    result = plugin.reserve_view(store, data, NOW, NOW)
    past = result["chart"]["past"]
    assert observed(past, -2000) == pytest.approx(1.4)
    for moment in (-1919, -1850, -1700):
        assert observed(past, moment) is None, moment    # b is not vouched there
    assert observed(past, -1679) == pytest.approx(1.4)
    if obs_mode == "moving":
        pace_a = next(m for g in result["summary"]["groups"] for m in [g["recent_pace"]])
        assert pace_a["state"] == "ok" and pace_a["accounts_known"] == 2
        state = qs.compute(data, store.read(lambda salt: qs.history_requests(
            qs.prepare(data, NOW)[1], salt, NOW), NOW), NOW)
        rates = {m.subject_id: m.rate for m in state.groups[0].members}
        assert rates["a"]["from"] == pytest.approx(NOW - 3600) and "cut_by" not in rates["a"]
        assert rates["b"]["from"] >= NOW - 1680 and rates["b"]["cut_by"] == "gap"


def test_a_live_read_does_not_bridge_a_miss_the_last_sweep_recorded(tmp_path):
    """Finding 2, read side: the last sweep did not see b; before the next
    sweep a route read sees b fresh again with the same cached reading. The
    live overlay and the chart must not carry b across that known hole."""
    store = quota_history.HistoryStore(tmp_path)

    def script(offset):
        rows = [snap("codex", "a", [constraint("primary", .2, reset=86400)], observed_abs=NOW + offset - 5)]
        if offset != -120:
            rows.append(snap("codex", "b", [constraint("primary", .4, reset=86400)],
                             observed_abs=NOW + min(offset, -240) - 5))
        return rows

    _scripted_sweeps(store, script, range(-3600, -119, 120))
    live = payload([snap("codex", "a", [constraint("primary", .2, reset=86400)], observed=-5),
                    snap("codex", "b", [constraint("primary", .4, reset=86400)], observed=-245)],
                   [profile("codex", "a"), profile("codex", "b")], harnesses=("codex",))
    result = plugin.reserve_view(store, live, NOW, NOW)
    past = result["chart"]["past"]
    assert observed(past, -300) == pytest.approx(1.4)
    for moment in (-239, -200, -60):
        assert observed(past, moment) is None, moment
    assert past[-1] == [round(NOW), 1.4]                # the live reading, at now only
    state = qs.compute(live, store.read(lambda salt: qs.history_requests(
        qs.prepare(live, NOW)[1], salt, NOW), NOW), NOW)
    member_b = next(m for m in state.groups[0].members if m.subject_id == "b")
    merged = qs._with_live(qs._history_runs(state.view, member_b, member_b.reading.key),
                           member_b.reading, NOW, state.view)
    assert merged[-1].after_gap and merged[-1].first_seen == NOW
    rates = {m.subject_id: m.rate for m in state.groups[0].members}
    assert rates["a"]["state"] == "ok"
    assert rates["b"]["state"] in ("warming_up", "insufficient") and rates["b"]["reason"] == "gap"


def test_a_miss_recorded_by_an_older_writer_is_still_read_as_a_gap(tmp_path):
    """History written before this repair has no after_gap mark for such a
    hole, but its sweep row is there: a completed sweep between two sightings
    of one source that did not see it. The reader uses that recorded fact; it
    never invents a hole where the old writer merged the sightings."""
    store = quota_history.HistoryStore(tmp_path)

    def script(offset):
        rows = [snap("codex", "a", [constraint("primary", .2, reset=86400)], observed=-4000)]
        if offset != -1800:
            ratio = .4 if offset < -1800 else .45
            rows.append(snap("codex", "b", [constraint("primary", ratio, reset=86400)],
                             observed_abs=NOW + offset - 5))
        return rows

    data = _scripted_sweeps(store, script, range(-3600, 1, 120))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE run SET after_gap=0")      # as the 0.6.0 writer left it
    past = plugin.reserve_view(store, data, NOW, NOW)["chart"]["past"]
    assert observed(past, -1850) is None and observed(past, -1700) is None
    assert observed(past, -1679) == pytest.approx(1.35)
    assert observed(past, -2000) == pytest.approx(1.4)


def _sweeps_every(store, start, end, step=120, skip=(), failed=()):
    t = start
    while t <= end + 1e-6:
        if not any(a <= t < b for a, b in skip):
            if t in failed:
                plugin.persist_sweep(store, None, "URLError: refused", NOW + t)
            else:
                plugin.persist_sweep(store, host_at(NOW + t, {"a": [(-20000, .2, 86400)]}), "", NOW + t)
        t += step


def test_the_unbroken_watch_is_bounded_and_says_when_it_is_only_a_lower_bound(tmp_path, monkeypatch):
    """Finding 3: the field named session_start was the first sweep of a
    74-minute look-back, called the start of the whole unbroken collection."""
    statements = []
    real_connect = quota_history.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    lookback = qs.RATE_WINDOW_SEC + 2 * qs.SIGHTING_GAP_SEC
    # Three hours unbroken: the look-back ends before the watch does.
    long = quota_history.HistoryStore(tmp_path / "long")
    _sweeps_every(long, -10800, 0)
    monkeypatch.setattr(quota_history.sqlite3, "connect", traced)
    result, _ = summarize(long, {"a": [(-20000, .2, 86400)]})
    history = result["summary"]["history"]
    assert "session_start" not in history
    assert history["unbroken_watch"] == {"since": at(-4440), "exact": False,
                                         "lookback_seconds": round(lookback)}
    assert history["collecting_since"] == at(-10800)
    tool = qs.compact(result["summary"])
    assert tool["history"]["unbroken_watch"] == history["unbroken_watch"]
    assert "session_start" not in json.dumps(tool)
    # No read of the sweeps is unbounded: a time bound, a LIMIT or an aggregate.
    sweeps = [s for s in statements if "FROM sweep" in s and not s.startswith(("INSERT", "DELETE"))]
    assert sweeps and all("at>=" in s.replace(" ", "") or "LIMIT" in s or "min(" in s or "max(" in s
                          for s in sweeps), sweeps
    monkeypatch.setattr(quota_history.sqlite3, "connect", real_connect)
    # An outage 30 minutes ago: the watch since is exactly its end.
    broken = quota_history.HistoryStore(tmp_path / "broken")
    _sweeps_every(broken, -10800, 0, skip=[(-2400, -1800)])
    watch = summarize(broken, {"a": [(-20000, .2, 86400)]})[0]["summary"]["history"]["unbroken_watch"]
    assert watch == {"since": at(-1800), "exact": True, "lookback_seconds": round(lookback)}
    # A failed read ten minutes ago breaks it too.
    failed = quota_history.HistoryStore(tmp_path / "failed")
    _sweeps_every(failed, -10800, 0, failed={-600})
    watch = summarize(failed, {"a": [(-20000, .2, 86400)]})[0]["summary"]["history"]["unbroken_watch"]
    assert watch["since"] == at(-480) and watch["exact"] is True
    # A collector that began inside the look-back: its first sweep, exactly.
    young = quota_history.HistoryStore(tmp_path / "young")
    _sweeps_every(young, -4320, 0)
    watch = summarize(young, {"a": [(-20000, .2, 86400)]})[0]["summary"]["history"]["unbroken_watch"]
    assert watch == {"since": at(-4320), "exact": True, "lookback_seconds": round(lookback)}
    # Not watching now: no claim at all.
    stopped = quota_history.HistoryStore(tmp_path / "stopped")
    _sweeps_every(stopped, -10800, -1200)
    history = summarize(stopped, {"a": [(-20000, .2, 86400)]})[0]["summary"]["history"]
    assert history["unbroken_watch"] is None


# ---------------------------------------------------------------------------
# 0.6.1 final repair: a reading seen at one sweep only is a point of its own
# (review finding 1), resolved by the reserve's own source policy


def _settle(rows):
    """Independent resolution of one account's sources at one moment, from
    the documented policy (no skill code): every source must agree on the
    reset within 120 s; the newest observation wins, and sources observed
    within one second of it must agree within 0.01 (then the most used of
    them counts); otherwise the moment is not settled (None)."""
    if not rows:
        return None
    resets = [reset for _obs, _ratio, reset in rows if reset is not None]
    if resets and max(resets) - min(resets) > 120:
        return None
    newest = max(obs for obs, _ratio, _reset in rows)
    top = [ratio for obs, ratio, _reset in rows if newest - obs <= 1.0]
    if max(top) - min(top) > 0.01 + 1e-9:
        return None
    return max(top)


def _group_total(values):
    if any(value is None for value in values):
        return None
    return round(sum(1.0 - value for value in values), 4)


def _held_between(offset, offsets, seen):
    """The line just after a sweep: a source holds its reading until the
    next sweep only when that sweep sees it again (the last sweep: until now)."""
    later = [o for o in offsets if o > offset]
    here, there = seen(offset), (seen(later[0]) if later else seen(offset))
    return _group_total([_settle([row for source, row in sources.items() if source in there.get(account, {})])
                         for account, sources in here.items()])


def _sighted_at(offset, seen):
    """The total at the sweep itself: every source seen fresh and numeric then."""
    return _group_total([_settle(list(sources.values())) for sources in seen(offset).values()])


def _same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 1e-6)


def _expected_points(offsets, seen):
    """A sweep's own total is a point exactly when the line shows it on
    neither side of that sweep."""
    out, before = [], None
    for offset in offsets:
        after = _held_between(offset, offsets, seen)
        here = _sighted_at(offset, seen)
        if offset < 0 and not _same(here, before) and not _same(here, after):
            out.append([round(NOW + offset), here])
        before = after
    return out


def _run_point_scenario(tmp_path, seen):
    """Sweeps every 120 s over the last 40 minutes; the host reports exactly
    what ``seen(offset)`` lists: account -> {source: (observed, ratio, reset)}
    (offsets from NOW), nothing stale."""
    store = quota_history.HistoryStore(tmp_path)
    offsets = list(range(-2400, 1, 120))

    def script(offset):
        return [snap("codex", account, [constraint("primary", ratio, reset=reset)], source=source,
                     observed_abs=NOW + obs)
                for account, sources in seen(offset).items() for source, (obs, ratio, reset) in sources.items()]

    data = _scripted_sweeps(store, script, offsets)
    return store, data, offsets, script


def _newer_once(value=.36, obs=-845.0, reset=86400):
    """Account a: an older source at 30% throughout, and a newer source seen
    at one sweep (-840) only. Account b: 50% throughout."""
    def seen(offset):
        a = {"app": (-3000.0, .30, 86400)}
        if offset == -840:
            a["rollout"] = (obs, value, reset)
        return {"a": a, "b": {"app": (-3000.0, .50, 86400)}}
    return seen


def _first_sighting_once(offset):
    """Account a seen at the very first sweep of the record, missed at the
    next one, then watched again at a new value."""
    a = {} if offset == -2280 else {"app": ((-2405.0, .20, 86400) if offset == -2400
                                             else (offset - 5.0, .25, 86400))}
    return {"a": a, "b": {"app": (-3000.0, .40, 86400)}}


def _last_before_a_gap(offset):
    """Account a changes at its last sighting before a missed sweep."""
    if offset <= -1560:
        a = {"app": (offset - 5.0, .20, 86400)}
    elif offset == -1440:
        a = {"app": (-1445.0, .26, 86400)}
    elif offset == -1320:
        a = {}
    else:
        a = {"app": (offset - 5.0, .30, 86400)}
    return {"a": a, "b": {"app": (-3000.0, .40, 86400)}}


def _unchanged_before_a_gap(offset):
    """Account a missed at one sweep, the same reading before and after: the
    last sighting before the gap is where the line ends, not a point."""
    a = {} if offset == -1440 else {"app": (offset - 5.0, .20, 86400)}
    return {"a": a, "b": {"app": (-3000.0, .40, 86400)}}


POINT_CASES = {
    # oldA .30 / newA .36 at one sweep / B .50: that sweep's total is 1.14;
    # after it the older source is still vouched for: 1.20, never the 1.14.
    "newer_source_once": (_newer_once(), [[round(NOW - 840), 1.14]]),
    "first_sighting_once": (_first_sighting_once, [[round(NOW - 2400), 1.4]]),
    "last_before_a_gap": (_last_before_a_gap, [[round(NOW - 1440), 1.34]]),
    # Observed within one second of the older source, at a different value:
    # the sources disagree at that sweep — a point with no value.
    "same_instant_disagreement": (_newer_once(obs=-2999.6), [[round(NOW - 840), None]]),
    "reset_disagreement": (_newer_once(value=.30, reset=90000), [[round(NOW - 840), None]]),
    # Controls: a sighting the line already shows is not a point.
    "agreeing_once": (_newer_once(value=.30), []),
    "older_once": (_newer_once(obs=-3500.0), []),
    "unchanged_before_a_gap": (_unchanged_before_a_gap, []),
}


@pytest.mark.parametrize("horizon", ["24h", "7d"])
@pytest.mark.parametrize("case", sorted(POINT_CASES))
def test_a_reading_seen_at_one_sweep_is_a_point_never_held_or_erased(tmp_path, case, horizon):
    seen, want = POINT_CASES[case]
    store, data, offsets, _script = _run_point_scenario(tmp_path, seen)
    chart = plugin.reserve_view(store, data, NOW, NOW, horizon=horizon)["chart"]
    # The independent oracle says where the points are and what they hold ...
    assert _expected_points(offsets, seen) == want, case
    assert chart["points"] == want, (case, chart["points"])
    # ... and the line is still exactly the supported intervals: a point is
    # never stretched over time, before or after its sweep.
    for offset in offsets:
        assert observed(chart["past"], offset + 1) == pytest.approx(_held_between(offset, offsets, seen)), \
            (case, offset)
    if case == "newer_source_once":
        assert observed(chart["past"], -841) == pytest.approx(1.2)
        assert observed(chart["past"], -839) == pytest.approx(1.2)
        rows = [row for row in chart["table"] if row.get("sighting")]
        assert rows == [{"at": at(-840), "observed": 1.14, "sighting": True, "event": "seen at this sweep only"}]
    if want and want[0][1] is None:
        assert [row["event"] for row in chart["table"] if row.get("sighting")] == ["sources disagree at this sweep"]


def test_a_point_is_what_the_reserve_said_at_that_sweep(tmp_path):
    """The point resolves sources exactly as the reserve overview did when
    that sweep's status was current: the same total, from the same policy."""
    store, data, _offsets, script = _run_point_scenario(tmp_path, _newer_once())
    point = plugin.reserve_view(store, data, NOW, NOW)["chart"]["points"]
    then = payload(script(-840), [profile("codex", sid) for sid in ("a", "b")], harnesses=("codex",))
    summary, _ = summary_of(then, now=NOW - 840)
    assert point == [[round(NOW - 840), group(summary, "|primary|")["measured"]["windows"]]]
    assert qs.compact(summary)["groups"][0]["remaining_windows"] == 1.14


def test_points_share_the_vertex_ceiling_and_its_disclosure(tmp_path, monkeypatch):
    """Many single-sweep readings: the newest are kept, the line is cut at the
    same moment holding what it held there, and the cut is disclosed."""
    def flapping(offset):
        a = {"app": (-3000.0, .30, 86400)}
        if (offset // 120) % 2 == 0:
            a["rollout"] = (offset - 5.0, .31 + ((offset // 240) % 5) / 100.0, 86400)
        return {"a": a, "b": {"app": (-3000.0, .50, 86400)}}

    store, data, offsets, _script = _run_point_scenario(tmp_path, flapping)
    full = plugin.reserve_view(store, data, NOW, NOW)["chart"]
    assert len(full["points"]) >= 8 and full["past_clipped_before"] is None
    monkeypatch.setattr(qs, "MAX_PAST_VERTICES", 4)
    cut = plugin.reserve_view(store, data, NOW, NOW)["chart"]
    assert cut["points"] == full["points"][-4:]
    assert cut["past_clipped_before"] == cut["points"][0][0]
    assert cut["past"][0] == [cut["points"][0][0], observed(full["past"], cut["points"][0][0] - NOW)]
    assert all(t >= cut["past_clipped_before"] for t, _v in cut["past"])


# ---------------------------------------------------------------------------
# 0.6.1 final repair: the account view, the overview and the tool describe
# one reading the same way (review finding 2)


def _consistency_status():
    """Six Claude accounts, each with a healthy weekly window, and a 5-hour
    window that is: 99.6% used, exactly full, out of range (1.4), fresh but
    of a cycle whose reported reset passed, from two sources that disagree
    at one moment, and healthy."""
    def five(ratio, reset=3600):
        return constraint("five_hour", ratio, window=FIVE_H, reset=reset, label="5 hour")

    week = constraint("seven_day", 0.40, reset=3 * 86400, label="7 day")
    snaps = [snap("claude", "near", [five(0.996), week]),
             snap("claude", "full", [five(1.0), week]),
             snap("claude", "bad", [five(1.4), week]),
             snap("claude", "ended", [five(0.5, reset=-300), week]),
             snap("claude", "split", [five(0.2), week], source="a"),
             snap("claude", "split", [five(0.5)], source="b"),
             snap("claude", "ok", [five(0.3), week]),
             # A last-known reading at exactly 100%: a known fact, printed as such.
             snap("claude", "ok", [five(1.0)], source="old", fresh=False, observed=-7200)]
    return payload(snaps, [profile("claude", sid) for sid in ("near", "full", "bad", "ended", "split", "ok")],
                   harnesses=("claude",))


def _route_and_tool(tmp_path, monkeypatch, data):
    monkeypatch.setattr(plugin, "_request_json", lambda *a, **k: (data, "", 200))
    monkeypatch.setattr(plugin.time, "time", lambda: NOW)
    host = _Host(tmp_path)
    plugin.register(host)
    view = host.routes["quotas"]({})
    tool = json.loads(host.tools["quota_summary"]["handler"](None, harness="claude", detail=True))
    return view, tool


def test_route_accounts_summary_and_tool_agree_on_every_reading(tmp_path, monkeypatch):
    view, tool = _route_and_tool(tmp_path, monkeypatch, _consistency_status())
    accounts = {a["subject_id"]: a["quota"] for g in view["groups"] for a in g["accounts"]}

    def current(sid, key_part):
        return [c for c in accounts[sid]["constraints"] if c["id"] == key_part]

    near, full = current("near", "five_hour")[0], current("full", "five_hour")[0]
    assert (near["used_text"], near["at_limit"], accounts["near"]["label"]) == ("99.6", False, "99.6% used")
    assert (full["used_text"], full["at_limit"], accounts["full"]["label"]) == ("100", True, "Limit reached")
    assert accounts["full"]["state"] == "exhausted" and accounts["near"]["state"] == "ok"
    for sid, why in (("bad", "ratio outside 0–100%"), ("ended", "its reported reset has passed"),
                     ("split", "its sources disagree")):
        quota = accounts[sid]
        assert current(sid, "five_hour") == [], sid          # no current bar
        assert quota["state"] == "ok" and quota["label"] == "40% used", sid  # the healthy weekly stands
        assert {entry["why"] for entry in quota["stale"]} == {why}, sid
        assert all(not c["at_limit"] for entry in quota["stale"] for c in entry["constraints"]), sid
    assert accounts["bad"]["stale"][0]["constraints"][0]["used_pct"] is None
    assert len(accounts["split"]["stale"]) == 2
    assert accounts["ok"]["label"] == "40% used" and current("ok", "five_hour")[0]["used_text"] == "30"
    last_known = accounts["ok"]["stale"][0]
    assert last_known["freshness"] == "stale" and "why" not in last_known
    assert (last_known["constraints"][0]["used_text"], last_known["constraints"][0]["at_limit"]) == ("100", False)

    # The overview counts exactly the current windows the accounts show.
    summary = view["reserve"]["summary"]
    five = group(summary, "|five_hour|")
    shown = [c for q in accounts.values() for c in q["constraints"] if c["id"] == "five_hour"]
    assert five["measured"]["accounts"] == len(shown) == 3
    assert five["measured"]["at_limit"] == sum(c["at_limit"] for c in shown) == 1
    assert sum(1 for s in five["shares"] if s["at_limit"]) == 1
    assert five["measured"]["windows"] == round(sum(1 - c["used_pct"] / 100 for c in shown), 2) == 0.7
    assert (five["coverage"]["invalid"], five["coverage"]["reset_passed"], five["coverage"]["conflicting"]) \
        == (1, 1, 1)
    week = group(summary, "|seven_day|")
    assert week["measured"]["accounts"] == 6 and week["measured"]["at_limit"] == 0
    # The tool says the same.
    row = next(r for r in tool["groups"] if r["key"] == five["key"])
    assert (row["of_accounts"], row["accounts_at_limit"], row["remaining_windows"]) == (3, 1, 0.7)
    assert row["coverage"] == {"measured": 3, "invalid": 1, "conflicting": 1, "reset_passed": 1}
    assert "Limit reached" not in json.dumps(accounts["near"]) + json.dumps(accounts["bad"])


def _final_fixture(tmp_path, monkeypatch):
    """What the Node matrix renders: the registered route's answer for the
    six accounts above, and a codex chart with every kind of point."""
    route, _tool = _route_and_tool(tmp_path / "route", monkeypatch, _consistency_status())
    monkeypatch.undo()

    def seen(offset):
        a = {"app": (-3000.0, .30, 86400)}
        if offset == -840:
            a["rollout"] = (-845.0, .36, 86400)
        if offset == -1440:
            a["rollout"] = (-2999.6, .36, 86400)
        c = {} if offset == -2280 else {"app": ((-2405.0, .10, 86400) if offset == -2400
                                                 else (offset - 5.0, .12, 86400))}
        return {"a": a, "b": {"app": (-3000.0, .50, 86400)}, "c": c}

    store = quota_history.HistoryStore(tmp_path / "chart")

    def script(offset):
        return [snap("codex", account, [constraint("primary", ratio, reset=reset)], source=source,
                     observed_abs=NOW + obs)
                for account, sources in seen(offset).items() for source, (obs, ratio, reset) in sources.items()]

    data = _scripted_sweeps(store, script, list(range(-2400, 1, 120)), sids=("a", "b", "c"))
    chart_view = plugin.build_view(data, "", NOW)
    chart_view["reserve"] = widget_reserve(store, data, NOW, NOW, harness="codex", name_accounts=True)
    points = chart_view["reserve"]["chart"]["points"]
    assert points == [[round(NOW - 2400), 2.1], [round(NOW - 1440), None], [round(NOW - 840), 2.02]], points
    return {"route": route, "chart": chart_view}


NODE_FINAL_MATRIX = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
function only(route, sid) {
  const view = JSON.parse(JSON.stringify(route));
  view.groups.forEach((g) => { g.accounts = g.accounts.filter((a) => a.subject_id === sid); });
  return view;
}
// 0.8.0: what the account itself says — its card (the inspector), with its
// windows under Diagnostics, and its row in the account list.
const card = (root) => classes(root, 'inspector')[0];
const dotOf = (root) => classes(card(root), 'state-dot')[0].className;
const stateOf = (root, sid) => classes(byFocus(root, 'acct:claude:' + sid), 'acc-state')[0].textContent;
async function opened(view, sid) {
  const env = await boot(view);
  click(env, 'accounts');
  click(env, 'acct:claude:' + sid);
  return env;
}

(async () => {
  const fx = JSON.parse(process.env.FINAL_FIXTURE);

  // Finding 2: every account word, tone and bar reads the unrounded verdict
  // the overview counts by. 99.6% is not the limit; 1.4, an ended cycle and
  // disagreeing sources are no current bar and no spent verdict.
  let env = await opened(only(fx.route, 'near'), 'near');
  assert.doesNotMatch(allSpoken(card(env.root)), /Limit reached|100% used|\bspent\b/);
  assert.doesNotMatch(dotOf(env.root), /\bbad\b/);
  assert.notEqual(stateOf(env.root, 'near'), 'limit reached');
  assert.equal(classes(card(env.root), 'win-line').length, 0, 'windows wait under Diagnostics');
  click(env, 'inspector-diag');
  assert.match(classes(card(env.root), 'win-line').map((t) => t.textContent).join(' '), /99\.6% used/);

  env = await opened(only(fx.route, 'full'), 'full');
  assert.match(allSpoken(card(env.root)), /Limit reached/);
  assert.equal(stateOf(env.root, 'full'), 'limit reached');
  assert.match(dotOf(env.root), /\bbad\b/);

  const asides = { bad: /ratio outside 0–100%/, ended: /its reported reset has passed/, split: /its sources disagree/ };
  for (const sid of Object.keys(asides)) {
    env = await opened(only(fx.route, sid), sid);
    const brief = card(env.root).textContent;
    assert.match(brief, asides[sid], sid);
    assert.match(brief, /not counted now/, sid);
    assert.doesNotMatch(allSpoken(card(env.root)), /Limit reached|100% used|\bspent\b/, sid);
    assert.equal(stateOf(env.root, sid), 'ready', sid);
    click(env, 'inspector-diag');
    const text = card(env.root).textContent;
    assert.match(text, /week40% used/, sid);
    assert.match(text, new RegExp('Not current — ' + asides[sid].source), sid);
    const staleLines = classes(card(env.root), 'win-line').filter((t) => /\bstale\b/.test(String(t.className)));
    assert.ok(staleLines.length >= 1, sid);
    if (sid === 'bad') {
      // No number, no share: no bar at all — an empty one would read as 0% left.
      const unreadable = staleLines.filter((t) => /Unreadable ratio/.test(t.textContent));
      assert.ok(unreadable.length);
      unreadable.forEach((t) => assert.equal(classes(t, 'meter').length, 0));
    }
    if (sid === 'ended') assert.match(staleLines.map((t) => t.textContent).join(' '), /50% used/);
    if (sid === 'split') assert.match(staleLines.map((t) => t.textContent).join(' '), /20% used.*50% used|50% used.*20% used/);
    assert.equal(walk(env.root).filter((n) => /\bmeter\b.*\bspent\b/.test(String(n.className))).length, 0, sid);
  }

  // A last-known reading at exactly 100% reads "100% used", muted, not a verdict.
  env = await opened(only(fx.route, 'ok'), 'ok');
  click(env, 'inspector-diag');
  const lastKnown = classes(card(env.root), 'quota-last-known').map((b) => b.textContent).join(' ');
  assert.match(lastKnown, /Last known · observed .*100% used/);
  assert.doesNotMatch(allSpoken(card(env.root)), /<100|Limit reached/);

  // The account list reads the same verdicts, row by row.
  env = await boot(fx.route);
  click(env, 'accounts');
  const said = (sid) => byFocus(env.root, 'acct:claude:' + sid).getAttribute('aria-label');
  assert.doesNotMatch(said('near'), /limit reached|alert/);
  assert.match(said('full'), /limit reached/);
  ['bad', 'ended', 'split'].forEach((sid) => assert.doesNotMatch(said(sid), /100%|limit reached|alert/, sid));
  // The overview row agrees: one account at the limit, named, never a rounded "0%".
  const five = fx.route.reserve.summary.groups.find((g) => g.key.includes('|five_hour|'));
  const row = byFocus(env.root, 'limit:' + five.key);
  assert.match(row.getAttribute('aria-label'), /full at the limit until /);
  assert.equal(classes(row.parentNode, 'bar').filter((c) => String(c.className).includes('spent')).length, 1);

  // Finding 1, 0.8.0: a total seen at one sweep only holds for no stretch of
  // time. It is not drawn as a mark (no symbols on the chart); the record
  // stays a line with breaks, and every such sighting is listed in the data
  // table under Details, the disagreeing one as "not settled".
  env = await boot(fx.chart);
  const svgClasses = (name) => walk(env.root).filter((n) =>
    String(n.getAttribute('class') || '').split(/\s+/).includes(name));
  assert.ok(svgClasses('chart-svg').length, 'the timeline is drawn');
  assert.equal(svgClasses('point-mark').length + svgClasses('point-seen').length + svgClasses('point-unsettled').length, 0);
  assert.ok(svgClasses('line-observed').some((n) => n.tagName === 'PATH'));
  const legend = classes(env.root, 'chart-legend')[0].textContent;
  assert.doesNotMatch(legend, /one sweep|≠/);
  const plot = byFocus(env.root, 'chart-plot');
  plot.focus();
  plot.listeners.focus[0]();
  plot.listeners.keydown[0]({ key: 'Home', preventDefault() {} });
  for (let i = 0; i < 70; i++) {
    plot.listeners.keydown[0]({ key: 'ArrowRight', preventDefault() {} });
    assert.doesNotMatch(classes(env.root, 'chart-readout')[0].textContent, /this sweep|disagree/);
  }
  const table = classes(env.root, 'chart-table')[0].textContent;
  assert.match(table, /3 totals seen at one sweep only are listed separately below/);
  assert.match(table, /2\.10seen at this sweep only/);
  assert.match(table, /not settled.*sources disagree at this sweep/);
  assert.match(table, /2\.02seen at this sweep only/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_final_repair_matrix(tmp_path, monkeypatch):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    fixture = _final_fixture(tmp_path, monkeypatch)
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_FINAL_MATRIX)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path), "FINAL_FIXTURE": json.dumps(fixture)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


NODE_REPAIR_MATRIX = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
const hasSvg = (root) => walk(root).some((n) => n.getAttribute('class') === 'chart-svg');
const rowOf = (env, key) => byFocus(env.root, 'limit:' + key).parentNode;

// The host's appearance bridge, so a theme change can be sent to the widget.
async function bootThemed(view) {
  const made = makeDocument();
  made.document.documentElement = made.document.createElement('html');
  made.document.documentElement.dataset = {};
  const calls = [];
  let intervalCallback = null;
  let themeListener = null;
  const window = {
    fetch(url, options = {}) {
      calls.push({ url, method: options.method || 'GET' });
      return Promise.resolve(response(view));
    },
    setInterval(callback) { intervalCallback = callback; return 17; },
    clearInterval() {},
    addEventListener() {},
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    clearTimeout: (id) => clearTimeout(id),
    __ouroWidgetOnDispose() {},
    OuroborosWidget: { onTheme(fn) { themeListener = fn; fn('light'); return () => { themeListener = null; }; } },
  };
  const context = vm.createContext({ window, document: made.document, console, Date, Math, Object,
    Array, String, Number, RegExp, Promise, setImmediate });
  vm.runInContext(instrumentedWidgetSource, context, { filename: 'widget.js' });
  await settle();
  return { root: made.root, document: made.document, calls, interval: () => intervalCallback,
    theme: (name) => { themeListener(name); return made.document.documentElement.dataset.theme; } };
}

(async () => {
  const fixture = JSON.parse(process.env.RESERVE_FIXTURE);
  const view = fixture.view;
  const groups = view.reserve.summary.groups.filter((g) => g.harness === 'codex');
  const tight = groups.find((g) => g.tightest);

  // 1. Each new mount (0.8.0 defaults, never saved): the limits, the timeline
  // of the lowest-left limit open under its row with "no new use" and the
  // limit's own span, the account list folded, no account selected, About shut.
  let env = await bootThemed(view);
  assert.ok(hasSvg(env.root), 'timeline open by default');
  assert.doesNotMatch(env.calls[0].url, /chart=0/);
  assert.equal(byFocus(env.root, 'scenario:no_new_use').getAttribute('aria-pressed'), 'true');
  assert.equal(byFocus(env.root, 'horizon:' + (tight.window_seconds > 86400 ? '7d' : '24h')).getAttribute('aria-pressed'), 'true');
  assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-expanded'), 'false');
  assert.equal(byFocus(env.root, 'about').getAttribute('aria-expanded'), 'false');
  assert.equal(classes(env.root, 'inspector').length, 0);
  assert.equal(classes(env.root, 'win-line').length, 0);

  // Coverage in every row: the figure is of every account the limit applies
  // to, and the row says how many of them are current.
  groups.forEach((g) => {
    const name = byFocus(env.root, 'limit:' + g.key);
    assert.match(classes(name.parentNode, 'l-fig')[0].textContent, new RegExp(' of ' + g.slots + ' accounts?$'));
    assert.match(name.getAttribute('aria-label'), new RegExp(g.measured.accounts + ' current of ' + g.slots + ' accounts'));
  });
  // "lowest left": a ranking of averages, and said so.
  const badge = classes(rowOf(env, tight.key), 'rs-tight')[0];
  assert.equal(badge.textContent, 'lowest left');
  assert.match(badge.title, /lowest average share left/i);
  assert.match(badge.title, /a ranking, not a verdict/);
  assert.match(byFocus(env.root, 'limit:' + tight.key).getAttribute('aria-label'), /lowest average share left in this family/);
  assert.doesNotMatch(allSpoken(env.root), /tightest/i);
  // What is not counted is said in short under the rows — never as zero —
  // and the link opens the account list, which says it by name.
  const cover = byFocus(env.root, 'cover');
  assert.match(cover.getAttribute('aria-label'), /not counted/);
  assert.match(cover.getAttribute('aria-label'), /never as zero/);
  click(env, 'cover');
  assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-expanded'), 'true');
  assert.match(classes(env.root, 'acc-cover').map((n) => n.textContent).join(' '), /not counted, never as zero/);
  click(env, 'accounts');

  // About: bars are not aligned across rows; a scoped cap binds its models.
  click(env, 'about');
  let about = classes(env.root, 'about-panel')[0].textContent;
  assert.match(about, /a column does not follow one account/);
  assert.match(about, /binds only the models it names/);
  assert.match(about, /watched without a break since \d\d:\d\d/);
  click(env, 'about');

  // 2. A selected account stays selected through a poll and a theme change,
  // its diagnostics open, the keyboard on them.
  click(env, 'accounts');
  click(env, 'acct:codex:c1');
  click(env, 'inspector-diag');
  assert.ok(classes(env.root, 'win-line').length > 0);
  byFocus(env.root, 'inspector-diag').focus();
  env.interval()();
  await settle();
  assert.equal(byFocus(env.root, 'inspector-diag').getAttribute('aria-expanded'), 'true');
  assert.ok(classes(env.root, 'win-line').length > 0);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'inspector-diag');
  assert.equal(env.theme('dark'), 'dark');
  await settle();
  assert.ok(classes(env.root, 'inspector').length);

  // 3. The timeline survives a poll and a theme change too, and says what it
  // assumes — in one line on the face, every caveat under its details.
  env.interval()();
  await settle();
  assert.ok(hasSvg(env.root));
  assert.doesNotMatch(env.calls.at(-1).url, /chart=0/);
  env.theme('light');
  assert.ok(hasSvg(env.root));
  const block = classes(env.root, 'tl-block')[0].textContent;
  assert.match(block, /no later reset is assumed/);
  assert.match(block, /no additional unreported resets/);
  assert.match(block, /not how far it is reliable/);
  assert.doesNotMatch(block, /Stops:|ended \d|unreported cycle|cycle nobody/);
  assert.equal(walk(env.root).filter((n) => /^line-end|region-text/.test(String(n.getAttribute('class') || ''))).length, 0);
  // The table is a sample of the line and says so.
  assert.match(classes(env.root, 'chart-table')[0].textContent, /of \d+ recorded changes/);

  // 4. A new mount starts with the defaults again: nothing of the last
  // session is kept.
  env = await bootThemed(view);
  assert.ok(hasSvg(env.root));
  assert.equal(classes(env.root, 'inspector').length, 0);
  assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-expanded'), 'false');

  // 5. With no reserve overview the account list is all there is: open, and
  // a selected account shows its windows without a Diagnostics fold.
  env = await boot(fixture.failed);
  click(env, 'acct:codex:c1');
  assert.ok(classes(env.root, 'win-line').length > 0);
  assert.ok(!byFocus(env.root, 'inspector-diag'));

  // 6. The unbroken watch is said as what it is: a start, or a lower bound.
  const bounded = JSON.parse(JSON.stringify(view));
  bounded.reserve.summary.history.unbroken_watch.exact = false;
  env = await boot(bounded);
  click(env, 'about');
  about = classes(env.root, 'about-panel')[0].textContent;
  assert.match(about, /watched without a break at least since \d\d:\d\d/);
  assert.doesNotMatch(allSpoken(env.root), /for at least the last|74 min/);
  assert.doesNotMatch(allSpoken(env.root), /collecting since|session start|start of the session/i);

  // 7. A redraw that blurs the focused plot as it removes it (Chromium does)
  // keeps the keyboard cursor where the reader moved it.
  const text = Object.getOwnPropertyDescriptor(Element.prototype, 'textContent');
  Object.defineProperty(Element.prototype, 'textContent', {
    configurable: true,
    get: text.get,
    set(value) {
      const active = this.ownerDocument.activeElement;
      if (active && active !== this && walk(this).includes(active)) {
        (active.listeners.blur || []).forEach((listener) => listener());
      }
      text.set.call(this, value);
    },
  });
  try {
    env = await boot(view);
    const plot = byFocus(env.root, 'chart-plot');
    plot.focus();
    plot.listeners.focus[0]();
    plot.listeners.keydown[0]({ key: 'ArrowRight', preventDefault() {} });
    plot.listeners.keydown[0]({ key: 'ArrowRight', preventDefault() {} });
    const moved = classes(env.root, 'chart-readout')[0].textContent;
    assert.match(moved, /· scenario — /);
    env.interval()();
    await settle();
    assert.equal(env.document.activeElement.getAttribute('data-focus'), 'chart-plot');
    assert.equal(classes(env.root, 'chart-readout')[0].textContent, moved);
  } finally {
    Object.defineProperty(Element.prototype, 'textContent', text);
  }
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_repair_matrix(tmp_path):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_REPAIR_MATRIX)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path),
             "RESERVE_FIXTURE": json.dumps(_widget_fixture(tmp_path))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 0.6.1 account closure (delta review F2 1-3, F1 horizon edge): the account
# view reads accounts and cooldowns by the reserve's own rules, and a
# cooldown is "Cooling down" with its own facts — never "Limit reached",
# never a reset.


def _bare_cooldown(offset):
    """A cooldown as the engine reports one (the real shape): no ratio, no
    window, and its own end repeated as ``resets_at``."""
    return {"id": "cooldown", "label": "Cooldown", "used_ratio": None, "window_seconds": None,
            "resets_at": at(offset), "cooldown_until": at(offset), "applies_to_models": None}


FABLE = ["claude-fable-5", "fable"]


def _closure_status():
    """Claude accounts whose cooldowns and numbers come from different
    readings. Every seven_day reset is the same (3 days ahead)."""
    week = 3 * 86400

    def seven(ratio, **extra):
        return constraint("seven_day", ratio, reset=week, label="7 day", **extra)

    def fable(ratio, **extra):
        return constraint("weekly_scoped:Fable", ratio, reset=week, label="7 day (Fable)",
                          models=FABLE, **extra)

    snaps = [
        # (2) The older source carries a live cooldown, the newer one the number.
        snap("claude", "cool-split", [seven(.30, cooldown=at(3600))], source="a", observed=-600),
        snap("claude", "cool-split", [seven(.40)], source="b", observed=-60),
        # The same, on a model-scoped window.
        snap("claude", "cool-scoped", [fable(.30, cooldown=at(3600))], source="a", observed=-600),
        snap("claude", "cool-scoped", [seven(.20), fable(.40)], source="b", observed=-60),
        # An unreadable cooldown on the losing source still holds.
        snap("claude", "cool-unreadable", [seven(.30, cooldown="not-a-date")], source="a", observed=-600),
        snap("claude", "cool-unreadable", [seven(.40)], source="b", observed=-60),
        # A live cooldown on a stale reading beside a fresh number.
        snap("claude", "cool-stale", [seven(.40)], source="b", observed=-60),
        snap("claude", "cool-stale", [seven(.90, cooldown=at(1800))], source="old", fresh=False,
             observed=-7200),
        # An expired cooldown is history: no restriction anywhere.
        snap("claude", "cool-expired", [seven(.30, cooldown=at(-60))], source="a", observed=-600),
        snap("claude", "cool-expired", [seven(.40)], source="b", observed=-60),
        # (3) Partial share + cooldown; full share + cooldown; refused share + cooldown.
        snap("claude", "partial", [seven(.40, cooldown=at(3600))]),
        snap("claude", "full", [seven(1.0), _bare_cooldown(7200)]),
        snap("claude", "invalid", [seven(1.4, cooldown=at(3600))]),
        # The real shape: a bare cooldown constraint and the row's own state.
        dict(snap("claude", "bare", [seven(.20), _bare_cooldown(5400)], availability="cooldown"),
             availability={"state": "cooldown", "blocking_constraints": ["cooldown"],
                           "resets_at": at(5400)}),
        # The engine's availability state alone, no end reported.
        snap("claude", "avail-only", [seven(.20)], availability="cooldown"),
        snap("claude", "plain", [seven(.50)]),
        # No fresh reading at all, and a live cooldown on the stale one.
        snap("claude", "stale-only", [seven(.50, cooldown=at(2400))], source="old", fresh=False,
             observed=-7200),
    ]
    sids = ("cool-split", "cool-scoped", "cool-unreadable", "cool-stale", "cool-expired",
            "partial", "full", "invalid", "bare", "avail-only", "plain", "stale-only")
    return payload(snaps, [profile("claude", sid) for sid in sids], harnesses=("claude",))


def _accounts_of_route(view):
    return {a["subject_id"]: a["quota"] for g in view["groups"] for a in g["accounts"]}


def _cooling_by_account_view(accounts, calc):
    """Independently of the reserve: which accounts the ACCOUNT VIEW says
    are measured in this limit (a current bar of it) and held by a cooldown
    that covers it (the whole account, or exactly this limit's models)."""
    meaning, models = calc["meaning"], sorted(calc["models"])
    measured, cooling = {}, {}
    for sid, quota in accounts.items():
        bars = [c for c in quota["constraints"] if c["id"] == meaning and c["used_pct"] is not None]
        if not bars:
            continue
        left = 1 - bars[0]["used_pct"] / 100
        measured[sid] = left
        if any(c["scope"] == "account" or sorted(c["models"]) == models for c in quota["cooldowns"]):
            cooling[sid] = left
    return measured, cooling


def test_cooldowns_from_any_source_or_freshness_agree_across_route_accounts_summary_and_tool(
        tmp_path, monkeypatch):
    view, tool = _route_and_tool(tmp_path, monkeypatch, _closure_status())
    accounts = _accounts_of_route(view)
    summary = view["reserve"]["summary"]
    week, fable = group(summary, "|seven_day|"), group(summary, "|weekly_scoped:Fable|")

    # The overview: every cooldown that holds, whichever number is summed.
    assert week["measured"] == {"accounts": 10, "windows": 5.9, "average_remaining_pct": 59.0, "at_limit": 1}
    assert week["restrictions"]["cooling"] == {"accounts": 7, "windows": 4.0}
    assert week["unrestricted_windows"] == 1.9 and week["coverage"]["invalid"] == 1
    assert week["coverage"]["stale_only"] == 1
    assert fable["measured"]["windows"] == 0.6
    assert fable["restrictions"] == {"cooling": {"accounts": 1, "windows": 0.6}}
    # The account view says the same, limit by limit, from its own fields.
    for calc in (week, fable):
        measured, cooling = _cooling_by_account_view(accounts, calc)
        assert len(measured) == calc["measured"]["accounts"], calc["key"]
        assert round(sum(measured.values()), 2) == calc["measured"]["windows"], calc["key"]
        assert len(cooling) == calc["restrictions"]["cooling"]["accounts"], calc["key"]
        assert round(sum(cooling.values()), 2) == calc["restrictions"]["cooling"]["windows"], calc["key"]
    # The tool too.
    rows = {row["key"]: row for row in tool["groups"]}
    assert rows[week["key"]]["restrictions"] == week["restrictions"]
    assert rows[fable["key"]]["restrictions"] == fable["restrictions"]
    assert (rows[week["key"]]["remaining_windows"], rows[week["key"]]["accounts_at_limit"]) == (5.9, 1)

    def only(sid):
        return accounts[sid]

    # (2) The drawn number is the newer source's; the older source's cooldown is kept.
    split = only("cool-split")
    assert (split["state"], split["label"], split["resets_at"]) == ("cooling", "Cooling down", "")
    assert [(c["id"], c["used_text"], c["cooldown_until"]) for c in split["constraints"]] == \
        [("seven_day", "40", "")]
    assert split["cooling_until"] == at(3600)
    assert [(c["scope"], c["source"], c["freshness"], c["until"], c["until_note"], c["kind"])
            for c in split["cooldowns"]] == [("account", "a", "fresh", at(3600), "", "constraint")]
    scoped = only("cool-scoped")
    assert (scoped["state"], scoped["label"], scoped["note"]) == ("ok", "20% used", "")
    assert [(c["scope"], c["models"]) for c in scoped["cooldowns"]] == [("models", FABLE)]
    unreadable = only("cool-unreadable")
    assert (unreadable["state"], unreadable["cooling_until"]) == ("cooling", "")
    assert [(c["until"], c["until_note"]) for c in unreadable["cooldowns"]] == [("", "unreadable")]
    stale = only("cool-stale")
    assert stale["state"] == "cooling" and stale["label"] == "Cooling down"
    assert [(c["source"], c["freshness"], c["until"]) for c in stale["cooldowns"]] == \
        [("old", "stale", at(1800))]
    assert [e["freshness"] for e in stale["stale"]] == ["stale"]  # the stale number stays last-known
    for sid in ("cool-expired", "plain"):
        assert only(sid)["state"] == "ok" and only(sid)["cooldowns"] == [], sid
    # (3) A cooldown is not the limit and its end is not a reset.
    partial = only("partial")
    assert (partial["state"], partial["label"], partial["resets_at"], partial["cooling_until"]) == \
        ("cooling", "Cooling down", "", at(3600))
    full = only("full")
    assert (full["state"], full["label"]) == ("exhausted", "Limit reached")
    assert full["resets_at"] == at(3 * 86400)  # the window's own reset, not the cooldown's end
    assert [(c["scope"], c["until"]) for c in full["cooldowns"]] == [("account", at(7200))]
    invalid = only("invalid")
    assert (invalid["state"], invalid["label"], invalid["resets_at"]) == ("cooling", "Cooling down", "")
    assert invalid["constraints"] == [] and invalid["stale"][0]["why"] == "ratio outside 0–100%"
    assert invalid["stale"][0]["constraints"][0]["used_pct"] is None
    bare = only("bare")
    assert (bare["state"], bare["cooling_until"], bare["availability"]) == ("cooling", at(5400), "cooldown")
    assert len(bare["cooldowns"]) == 1  # the row's state and its constraint: one cooldown
    avail = only("avail-only")
    assert (avail["state"], avail["cooling_until"], avail["availability"]) == ("cooling", "", "cooldown")
    assert [(c["kind"], c["until_note"]) for c in avail["cooldowns"]] == [("availability", "not_reported")]
    stale_only = only("stale-only")
    assert (stale_only["state"], stale_only["label"], stale_only["cooling_until"]) == \
        ("cooling", "Cooling down", at(2400))
    assert stale_only["constraints"] == [] and [e["freshness"] for e in stale_only["stale"]] == ["stale"]
    for sid in ("cool-split", "cool-unreadable", "cool-stale", "partial", "invalid", "bare", "avail-only",
                "cool-scoped", "stale-only"):
        assert "Limit reached" not in json.dumps(only(sid)), sid


def _alias_case(name):
    legacy = snap("claude", None, [constraint("seven_day", .40, reset=86400, label="7 day")])
    work = snap("claude", "work", [constraint("seven_day", .20, reset=86400, label="7 day")])
    default = profile("claude", "claude-default")
    if name == "alias":
        return payload([legacy, work], [default, profile("claude", "work")], unified=True, harnesses=("claude",))
    if name == "alias_cooling":
        cooling = dict(legacy, constraints=legacy["constraints"] + [_bare_cooldown(3600)])
        return payload([cooling, work], [default, profile("claude", "work")], unified=True,
                       harnesses=("claude",))
    if name == "named_wins":
        own = snap("claude", "claude-default", [constraint("seven_day", .10, reset=86400, label="7 day")])
        return payload([legacy, own, work], [default, profile("claude", "work")], unified=True,
                       harnesses=("claude",))
    if name == "stale_own_fresh_legacy":
        own = snap("claude", "claude-default", [constraint("seven_day", .90, reset=86400, label="7 day")],
                   fresh=False, observed=-7200)
        return payload([legacy, own, work], [default, profile("claude", "work")], unified=True,
                       harnesses=("claude",))
    if name == "no_default_row":
        return payload([legacy, work], [profile("claude", "work")], unified=True, harnesses=("claude",))
    if name == "accounts_unread":
        data = payload([legacy, work], [default, profile("claude", "work")], unified=True, harnesses=("claude",))
        data["reads"]["accounts"] = "failed"
        return data
    if name == "legacy_engine":
        native = {"harness_id": "claude", "native_login_detected": True, "native_credentials_enabled": True,
                  "identity": {}}
        return payload([legacy, work], [default, profile("claude", "work")], [native], harnesses=("claude",))
    raise AssertionError(name)


# name -> (label of the account the legacy reading may belong to, that
# account's key, overview windows / accounts, unattributed, superseded)
ALIAS_CASES = {
    "alias": ("40% used", "claude-default", (1.4, 2), {}, {}),
    "alias_cooling": ("Cooling down", "claude-default", (1.4, 2), {}, {}),
    "named_wins": ("10% used", "claude-default", (1.7, 2), {}, {"claude": 1}),
    "stale_own_fresh_legacy": ("40% used", "claude-default", (1.4, 2), {}, {}),
    "no_default_row": (None, None, (0.8, 1), {"claude": 1}, {}),
    "accounts_unread": ("No quota window reported for this account", "claude-default", (0.8, 1),
                        {"claude": 1}, {}),
    "legacy_engine": ("40% used", "", (1.4, 2), {}, {}),
}


@pytest.mark.parametrize("case", sorted(ALIAS_CASES))
def test_the_account_view_attributes_readings_by_the_reserves_own_rule(tmp_path, monkeypatch, case):
    label, owner, (windows, measured), unattributed, superseded = ALIAS_CASES[case]
    view, tool = _route_and_tool(tmp_path, monkeypatch, _alias_case(case))
    summary = view["reserve"]["summary"]
    week = group(summary, "|seven_day|")
    assert (week["measured"]["windows"], week["measured"]["accounts"]) == (windows, measured)
    assert summary["unattributed_readings"] == unattributed
    assert summary["superseded_alias_readings"] == superseded
    row = next(r for r in tool["groups"] if r["key"] == week["key"])
    assert (row["remaining_windows"], row["of_accounts"]) == (windows, measured)
    by_key = {a["subject_id"] if a["kind"] == "profile" else "": a["quota"]
              for g in view["groups"] for a in g["accounts"]}
    # The account view draws exactly the accounts the overview measures.
    drawn = {sid for sid, q in by_key.items()
             if any(c["id"] == "seven_day" and c["used_pct"] is not None for c in q["constraints"])}
    assert len(drawn) == measured, (case, drawn)
    assert by_key["work"]["label"] == "20% used"
    if owner is not None:
        assert by_key[owner]["label"] == label, (case, by_key[owner])
    for sid, quota in by_key.items():
        if sid != "work" and sid != owner:
            assert quota["state"] in ("no_data", "not_checked"), (case, sid, quota["label"])
    if case == "alias_cooling":
        assert week["restrictions"]["cooling"] == {"accounts": 1, "windows": 0.6}
        assert [c["until"] for c in by_key["claude-default"]["cooldowns"]] == [at(3600)]
    if case == "named_wins":
        # The superseded legacy reading is nobody's: not even a last-known one.
        assert by_key["claude-default"]["stale"] == []
    if case == "stale_own_fresh_legacy":
        assert [e["freshness"] for e in by_key["claude-default"]["stale"]] == ["stale"]


def test_attribution_is_one_rule_for_the_reserve_and_the_account_view():
    """quota_summary.normalize and the account view take their readings from
    the one attribute(); nothing else decides which account a row is."""
    for case in sorted(ALIAS_CASES):
        data = _alias_case(case)
        norm = qs.normalize(data, NOW)
        attributed = qs.attribute(data)
        subjects = {(r.harness, r.subject_id) for r in norm.readings}
        assert subjects == {(h, s) for h, s, _row in attributed.rows}, case
        assert (norm.unattributed, norm.superseded) == (attributed.unattributed, attributed.superseded), case


@pytest.mark.parametrize("horizon", ["24h", "7d"])
def test_a_single_sighting_at_the_horizons_first_instant_is_a_point(tmp_path, horizon):
    """Delta review F1 (P3): a reading seen once, exactly where the chart's
    horizon starts, is inside it — a point, never dropped and never held."""
    span = qs.HORIZONS[horizon]
    store = quota_history.HistoryStore(tmp_path)
    first, missed = -span, -span + 120
    offsets = [first, missed, -240, -120, 0]

    def script(offset):
        rows = [snap("codex", "b", [constraint("primary", .40, reset=86400)], observed=-span - 600)]
        if offset == first:
            rows.append(snap("codex", "a", [constraint("primary", .20, reset=86400)], observed_abs=NOW + first - 5))
        elif offset != missed:
            rows.append(snap("codex", "a", [constraint("primary", .25, reset=86400)],
                             observed_abs=NOW + offset - 5))
        return rows

    data = _scripted_sweeps(store, script, offsets)
    chart = plugin.reserve_view(store, data, NOW, NOW, horizon=horizon)["chart"]
    assert chart["start"] == round(NOW - span)
    assert chart["points"] == [[round(NOW + first), 1.4]]
    # Nothing is stretched from it: no value is vouched for right after it.
    assert observed(chart["past"], first) is None
    assert observed(chart["past"], first + 1) is None
    assert observed(chart["past"], -1) == pytest.approx(1.35)


NODE_CLOSURE_MATRIX = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
function only(route, sid) {
  const view = JSON.parse(JSON.stringify(route));
  view.groups.forEach((g) => { g.accounts = g.accounts.filter((a) => a.subject_id === sid); });
  return view;
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = (n) => (n < 10 ? '0' : '') + n;
// The widget's own date words (formatResetAt), so the test does not depend
// on the machine's time zone.
const fmt = (iso) => { const d = new Date(iso); return d.getDate() + ' ' + MONTHS[d.getMonth()] + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes()); };
// 0.8.0: the account's own card (the inspector), its windows under
// Diagnostics, and its row in the account list.
const card = (root) => classes(root, 'inspector')[0];
const dotOf = (root) => classes(card(root), 'state-dot')[0].className;
const verdictOf = (root) => classes(card(root), 'quota-primary-row')[0];
const cooldownLines = (root) => classes(card(root), 'quota-cooldown').map((n) => n.textContent);
async function opened(view, sid, diagnostics) {
  const env = await boot(view);
  click(env, 'accounts');
  click(env, 'acct:claude:' + sid);
  if (diagnostics) click(env, 'inspector-diag');
  return env;
}

(async () => {
  const fx = JSON.parse(process.env.CLOSURE_FIXTURE);
  // The widget judges a window's own cooldown against its clock: this process
  // runs at the moment the route answered (plus five seconds).
  Date.now = () => fx.now_ms;
  const route = fx.route;
  const q = (sid) => route.groups[0].accounts.find((a) => a.subject_id === sid).quota;

  // (3) A cooldown is "Cooling down … until", never "Limit reached" nor
  // "Resets"; (2) whichever reading reported it.
  const cooling = {
    'cool-split': fmt(q('cool-split').cooling_until),
    'partial': fmt(q('partial').cooling_until),
    'cool-stale': fmt(q('cool-stale').cooling_until),
    'bare': fmt(q('bare').cooling_until),
    'invalid': fmt(q('invalid').cooling_until),
    'stale-only': fmt(q('stale-only').cooling_until),
    'cool-unreadable': null,
    'avail-only': null,
  };
  for (const sid of Object.keys(cooling)) {
    for (const details of [false, true]) {
      const env = await opened(only(route, sid), sid, details);
      // One cooldown sentence carries scope, end and provenance; no duplicate verdict.
      assert.equal(verdictOf(env.root), undefined, sid + ': no duplicate cooling verdict');
      const lines = cooldownLines(env.root);
      assert.equal(lines.length, 1, sid + ': ' + lines.join(' | '));
      assert.match(lines[0], /^Cooling down · whole account/, sid);
      if (cooling[sid]) assert.ok(lines[0].includes('until ' + cooling[sid]), sid + ' ' + lines[0]);
      else assert.doesNotMatch(lines[0], /until/, sid);
      assert.doesNotMatch(allSpoken(card(env.root)), /Limit reached|Resets/, sid);
      assert.match(dotOf(env.root), /\bbad\b/, sid);
      // The account list says it in the same words, amber: a hold, not a spent share.
      const state = classes(byFocus(env.root, 'acct:claude:' + sid), 'acc-state')[0];
      assert.equal(state.textContent, 'cooling down', sid);
      assert.match(state.className, /\bwarn\b/, sid);
    }
  }
  let env = await opened(only(route, 'cool-split'), 'cool-split', true);
  assert.match(card(env.root).textContent, /week40% used/);
  env = await opened(only(route, 'cool-stale'), 'cool-stale');
  assert.match(cooldownLines(env.root)[0], /reported by a stale reading observed/);
  env = await opened(only(route, 'cool-unreadable'), 'cool-unreadable');
  assert.match(cooldownLines(env.root)[0], /end time unreadable/);
  env = await opened(only(route, 'avail-only'), 'avail-only');
  assert.match(cooldownLines(env.root)[0], /no end time reported/);
  env = await opened(only(route, 'invalid'), 'invalid');
  assert.match(card(env.root).textContent, /not counted now \(ratio outside 0–100%\)/);

  // At the limit and cooling: two facts, each with its own time.
  env = await opened(only(route, 'full'), 'full');
  const verdict = verdictOf(env.root).textContent;
  assert.match(verdict, /^Limit reached/);
  assert.ok(verdict.includes('Resets ' + fmt(q('full').resets_at)), verdict);
  assert.ok(!verdict.includes(fmt(q('full').cooldowns[0].until)), verdict);
  assert.deepEqual(cooldownLines(env.root).map((l) => l.includes('until ' + fmt(q('full').cooldowns[0].until))), [true]);
  assert.match(dotOf(env.root), /\bbad\b/);
  // The bare cooldown's own end, repeated by the engine as its resets_at, is
  // said once, as the cooldown's end — never as a reset; the week's reset stays.
  click(env, 'inspector-diag');
  const lines = classes(card(env.root), 'win-line').map((t) => t.textContent);
  const bareLine = lines.find((t) => /Cooldown/.test(t));
  assert.ok(bareLine && bareLine.includes(fmt(q('full').cooldowns[0].until)), lines.join(' | '));
  assert.doesNotMatch(bareLine, /resets/);
  assert.ok(lines.find((t) => /100% used/.test(t)).includes(fmt(q('full').resets_at)), lines.join(' | '));

  // A model's cooldown marks the model, not the account.
  env = await opened(only(route, 'cool-scoped'), 'cool-scoped');
  assert.equal(verdictOf(env.root), undefined, 'state ok: the windows speak');
  assert.match(cooldownLines(env.root).join(' '), /^Cooling down · Fable · until /);
  assert.match(dotOf(env.root), /\bwarn\b/);
  assert.doesNotMatch(allSpoken(card(env.root)), /Limit reached|Resets/);

  // The reserve and the account read that cooldown one way, window by window.
  // It came from the older source, the Fable share from the newer one: the
  // reserve holds the share back, and so does the window's line. The shared
  // week, which no cooldown covers, stays neutral everywhere.
  const keyOf = (part) => route.reserve.summary.groups.find((g) => g.key.includes(part)).key;
  const heldCells = classes(byFocus(env.root, 'limit:' + keyOf('|weekly_scoped:Fable|')).parentNode, 'bar')
    .filter((c) => !/\bunknown\b/.test(c.className));
  assert.equal(heldCells.length, 1);
  assert.match(heldCells[0].className, /\brestricted\b/);
  assert.match(heldCells[0].title, /: 60% left — restricted now( · |$)/);
  click(env, 'inspector-diag');
  const meterOf = (node) => classes(node, 'meter')[0];
  const lineOf = (label) => classes(card(env.root), 'win-line').find((l) => classes(l, 'win-tag')[0].title === label);
  assert.match(meterOf(lineOf('7 day (Fable)')).className, /\brestricted\b/);
  assert.equal(meterOf(lineOf('7 day (Fable)')).title, '60% left — held back now');
  assert.doesNotMatch(meterOf(lineOf('7 day')).className, /\brestricted\b/);
  // The bar is the share left, the figure beside it the share used, and each
  // says which: never a bare "40%" beside a bar 60% long.
  for (const [label, used] of [['7 day (Fable)', '40'], ['7 day', '20']]) {
    const line = lineOf(label);
    assert.equal(classes(line, 'win-used')[0].textContent, used + '% used', label);
    assert.match(line.getAttribute('aria-label'), new RegExp(used + '% used'), label);
    assert.equal(meterOf(line).title.split(' — ')[0], (100 - used) + '% left', label);
  }

  // Controls: an expired cooldown and no cooldown say nothing of one.
  for (const sid of ['cool-expired', 'plain']) {
    env = await opened(only(route, sid), sid);
    assert.deepEqual(cooldownLines(env.root), [], sid);
    assert.doesNotMatch(allSpoken(card(env.root)), /Cooling|\bcooldown\b|Limit reached/, sid);
    assert.doesNotMatch(dotOf(env.root), /\bbad\b/, sid);
  }

  // The account list reads the same facts.
  env = await boot(route);
  click(env, 'accounts');
  const said = (sid) => byFocus(env.root, 'acct:claude:' + sid).getAttribute('aria-label');
  ['cool-split', 'cool-stale', 'cool-unreadable', 'partial', 'bare', 'avail-only', 'invalid', 'stale-only']
    .forEach((sid) => assert.match(said(sid), /cooling down/, sid));
  ['cool-expired', 'plain'].forEach((sid) => assert.doesNotMatch(said(sid), /cooling down|cooldown|alert/, sid));

  // (1) The unified engine's default row shows the legacy reading the
  // overview counts for it.
  env = await opened(only(fx.alias, 'claude-default'), 'claude-default', true);
  assert.match(card(env.root).textContent, /40% used/);
  assert.doesNotMatch(allSpoken(card(env.root)), /No quota window reported/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_account_closure_matrix(tmp_path, monkeypatch):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    route, _tool = _route_and_tool(tmp_path / "route", monkeypatch, _closure_status())
    alias, _tool = _route_and_tool(tmp_path / "alias", monkeypatch, _alias_case("alias"))
    monkeypatch.undo()
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_CLOSURE_MATRIX)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path),
             "CLOSURE_FIXTURE": json.dumps({"route": route, "alias": alias, "now_ms": (NOW + 5) * 1000})},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Skill-review closure: a sweep whose sources disagree has no total. Where the
# line has no value either (the last sighting before a gap, a sighting inside
# one), it is still a point of the record. 0.8.0: the widget lists every such
# point in the data table and draws none (no marks, no rail on the chart).

def _gap_singleton_script(offset):
    """Account a: vouched to -1800, where its last sighting disagrees; a gap
    after it, with a disagreeing sighting at -1440 and a settled one at
    -1200; vouched again from -960. Account b holds throughout."""
    a = {}
    if offset < -1800:
        a = {"app": (-3000.0, .30, 86400)}
    elif offset == -1800:
        a = {"app": (-3000.0, .30, 86400), "rollout": (-2999.6, .36, 86400)}
    elif offset == -1440:
        a = {"app": (-1445.0, .30, 86400), "rollout": (-1444.6, .36, 86400)}
    elif offset == -1200:
        a = {"app": (-1205.0, .36, 86400)}
    elif offset >= -960:
        a = {"app": (offset - 5.0, .40, 86400)}
    return {"a": a, "b": {"app": (-3000.0, .50, 86400)}}


def _gap_singleton_view(directory, script=_gap_singleton_script):
    store = quota_history.HistoryStore(directory)

    def snaps(offset):
        return [snap("codex", account, [constraint("primary", ratio, reset=reset)], source=source,
                     observed_abs=NOW + obs)
                for account, sources in script(offset).items()
                for source, (obs, ratio, reset) in sources.items()]

    data = _scripted_sweeps(store, snaps, list(range(-2400, 1, 120)), sids=("a", "b"))
    view = plugin.build_view(data, "", NOW)
    view["reserve"] = widget_reserve(store, data, NOW, NOW, harness="codex", name_accounts=True)
    return view


def test_a_disagreeing_sighting_where_the_line_has_no_value_is_still_a_point(tmp_path):
    chart = _gap_singleton_view(tmp_path / "gap")["reserve"]["chart"]
    t = lambda offset: round(NOW + offset)
    # The line is what it was: nothing is drawn across the gap.
    assert [v for v in chart["past"] if v[0] > t(-3000)] == [
        [t(-2400), 1.2], [t(-1800), None], [t(-960), 1.1], [t(0), 1.1]]
    assert chart["points"] == [[t(-1800), None], [t(-1440), None], [t(-1200), 1.14]]
    said = [row["event"] for row in chart["table"] if row.get("sighting")]
    assert said == ["sources disagree at this sweep"] * 2 + ["seen at this sweep only"]

    # A sweep that simply did not see another account has no total and is
    # not a disagreement: no point.
    def b_missing(offset):
        seen = _gap_singleton_script(offset)
        if offset == -1440:
            seen["b"] = {}
        return seen

    other = _gap_singleton_view(tmp_path / "other", b_missing)["reserve"]["chart"]
    assert [p for p in other["points"] if p[0] == t(-1440)] == []


NODE_GAP_SINGLETON = r"""
(async () => {
  const view = JSON.parse(process.env.GAP_FIXTURE);
  delete view.reserve.chart.history; // Legacy strict-series fallback remains supported.
  const env = await boot(view);
  const svg = walk(env.root).find((n) => n.getAttribute('class') === 'chart-svg');
  assert.ok(svg, 'the chart is drawn');
  const cls = (n) => String(n.getAttribute('class') || '').split(/\s+/);
  const inSvg = (name) => walk(svg).filter((n) => cls(n).includes(name));
  // 0.8.0: a sweep whose sources disagree has no total, and a total seen at
  // one sweep holds for no stretch of time: neither is drawn — no marks, no
  // rail, no symbol. The record stays a line that stops at the gap.
  assert.equal(inSvg('point-mark').length + inSvg('point-rail').length + inSvg('rail-line').length, 0);
  assert.equal(walk(svg).filter((n) => n.tagName === 'TEXT' && /≠/.test(n.textContent)).length, 0);
  const line = inSvg('line-observed').find((n) => n.tagName === 'PATH');
  assert.ok(line && /M/.test(line.getAttribute('d')));
  assert.equal((line.getAttribute('d').match(/M/g) || []).length, 2, 'two stretches: before and after the gap');
  assert.doesNotMatch(classes(env.root, 'chart-legend')[0].textContent, /≠|one sweep/);

  // The keyboard never lands on a value that is not there: in the gap the
  // record says it has none, never a zero.
  const plot = byFocus(env.root, 'chart-plot');
  plot.focus();
  plot.listeners.focus[0]();
  plot.listeners.keydown[0]({ key: 'Home', preventDefault() {} });
  const heard = [];
  for (let i = 0; i < 70; i++) {
    plot.listeners.keydown[0]({ key: 'ArrowRight', preventDefault() {} });
    heard.push(classes(env.root, 'chart-readout')[0].textContent);
  }
  heard.forEach((said) => assert.doesNotMatch(said, /this sweep|\b0(\.00)? of 2/));
  assert.ok(heard.some((said) => /recorded no record \(gap\)/.test(said)));

  // Every such sweep is still on record: the details say so, and the table
  // lists each — the two that disagree as "not settled", the settled one with
  // its total.
  const table = classes(env.root, 'chart-table')[0].textContent;
  assert.match(table, /3 totals seen at one sweep only .* are listed in the table, not drawn/);
  assert.equal((table.match(/sources disagree at this sweep/g) || []).length, 2);
  assert.match(table, /1\.14seen at this sweep only/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_lists_single_sweep_sightings_and_never_draws_them(tmp_path):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    view = _gap_singleton_view(tmp_path)
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_GAP_SINGLETON)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path), "GAP_FIXTURE": json.dumps(view)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Final repairs: the horizon is the one whole second the chart publishes as
# ``end``, whatever fraction of a second ``now`` was read at; and the widget
# lists every single-sweep point, a page at a time, not the table's newest few.


def _iso_exact(stamp):
    """An instant with its fraction of a second, as a vendor may report it."""
    import datetime as _dt
    moment = _dt.datetime.fromtimestamp(stamp, _dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _chart_read_at(now, resets, horizon):
    """One account per reported reset (absolute, fraction kept), each 40%
    used, read at ``now``: nothing recorded, so no pace."""
    snaps = []
    for index, reset in enumerate(resets):
        row = constraint("primary", .4)
        row["resets_at"] = _iso_exact(reset)
        snaps.append(snap("codex", f"a{index}", [row], observed_abs=now - 60))
    data = payload(snaps, [profile("codex", f"a{index}") for index in range(len(resets))])
    return qs.build_chart(qs.compute(data, qs.HistoryView(state="empty"), now), horizon=horizon)


def _published_at_horizon(chart, now, horizon_end, total):
    """Everything the chart says at its horizon says ``total`` there."""
    assert chart["end"] == horizon_end
    for sc in chart["scenarios"].values():
        assert sc["line"][-1] == [horizon_end, total]
        assert sc["checkpoints"][-1]["at"] == horizon_end and sc["checkpoints"][-1]["value"] == total
        # Every checkpoint is the whole second of the original now plus its offset.
        assert [cp["at"] for cp in sc["checkpoints"]] == [round(now + cp["after_seconds"])
                                                          for cp in sc["checkpoints"]]
    rows = [row for row in chart["table"] if row["at"] == qs.iso(horizon_end)]
    assert rows and all(row["scenario_no_new_use"] == total and row["scenario_recent_pace"] == total
                        for row in rows)


@pytest.mark.parametrize("horizon", ["24h", "7d"])
def test_fractional_now_above_half_a_reset_at_the_published_horizon_is_inside(horizon):
    """Read at .75 of a second: ``end`` rounds up to the next whole second.
    A reset exactly then used to fall past the unrounded horizon — in
    ``later``, with no refill on the line, the checkpoint or the table —
    while the chart published that very second as its end."""
    span = qs.HORIZONS[horizon]
    now = NOW + 0.75
    end = round(NOW + span) + 1
    chart = _chart_read_at(now, [end], horizon)
    # The original now and the figure at it stay as read.
    assert chart["now"] == round(now) and chart["current_windows"] == 0.6
    for sc in chart["scenarios"].values():
        assert sc["line"][0] == [round(now), 0.6]
        assert [(e["at"], e["accounts"], e["total_after"]) for e in sc["schedule"]] == [(end, 1, 1.0)]
        assert sc["later"] is None
        assert sc["line"][-2:] == [[end, 0.6], [end, 1.0]]     # refilled at the horizon, not before
    _published_at_horizon(chart, now, end, 1.0)
    assert chart["resets"] == [{"at": end, "accounts": 1}]
    assert [row["event"] for row in chart["table"] if row["at"] == qs.iso(end) and row.get("event")] \
        == ["reported reset · 1 account"]


@pytest.mark.parametrize("horizon", ["24h", "7d"])
def test_fractional_now_below_half_a_reset_past_the_published_horizon_is_not_applied(horizon):
    """Read at .25 of a second: ``end`` rounds down. A reset exactly at that
    second is inside and refilled there; one a fifth of a second later —
    before the unrounded horizon, after the published one — is only in
    ``later`` and refills nothing: never applied before its own time on a
    line that ends at the published second."""
    span = qs.HORIZONS[horizon]
    now = NOW + 0.25
    end = round(NOW + span)
    chart = _chart_read_at(now, [end, end + 0.2], horizon)
    assert chart["now"] == round(now) and chart["current_windows"] == 1.2
    for sc in chart["scenarios"].values():
        assert sc["line"][0] == [round(now), 1.2]
        assert [(e["at"], e["accounts"], e["total_after"]) for e in sc["schedule"]] == [(end, 1, 1.6)]
        # Its own time, to the second it is published at (here the same
        # second as the horizon): when, never applied.
        assert sc["later"] == {"at": round(end + 0.2), "accounts": 1, "more_events": 0, "more_accounts": 0}
        assert max(v for _t, v in sc["line"]) == 1.6
    _published_at_horizon(chart, now, end, 1.6)
    assert chart["resets"] == [{"at": end, "accounts": 1}]
    # A whole-second now is unchanged by any of it.
    whole = _chart_read_at(NOW, [end, end + 0.2], horizon)
    assert whole["end"] == end and [e["at"] for e in whole["scenarios"]["no_new_use"]["schedule"]] == [end]
    _published_at_horizon(whole, NOW, end, 1.6)


# A reset inside the span keeps its fraction of a second: never applied
# before it happens. What the widget's cursor reads off the line (lineAt),
# the checkpoints and the table say the same at a whole second either side.


def _cursor_at(points, t):
    """widget.js lineAt: inside the line, the value just after anything at
    ``t`` (a step there reads its right side); outside it, nothing."""
    if not points or t < points[0][0] or t > points[-1][0]:
        return None
    return line_at(points, t, right=True)


# (now's fraction, the reset from NOW, whether the 6-hour checkpoint comes
# before it). Read at .25 that checkpoint is NOW + 21600; at .75, NOW + 21601.
FRACTIONAL_RESETS = [
    (0.25, 21600.2, True),     # a fifth of a second after the checkpoint
    (0.25, 21600.0, False),    # at it
    (0.25, 21599.8, False),    # a fifth of a second before it
    (0.75, 21601.3, True),     # in the second the checkpoint rounds up to
    (0.75, 21600.6, False),    # in the half second it rounds up over
]


@pytest.mark.parametrize("fraction, offset, before", FRACTIONAL_RESETS)
def test_a_reset_with_a_fraction_of_a_second_is_never_applied_before_it(fraction, offset, before):
    now = NOW + fraction
    reset = qs.parse_instant(_iso_exact(NOW + offset))      # as the chart reads it
    checkpoint = round(now + 6 * 3600)
    expected = 0.6 if before else 1.0
    chart = _chart_read_at(now, [NOW + offset], "24h")
    for name, sc in chart["scenarios"].items():
        # The reset at its own time, in the schedule and on the line.
        assert [(e["at"], e["last"]) for e in sc["schedule"]] == [(reset, reset)], name
        assert [[t, v] for t, v in sc["line"] if t == reset] == [[reset, 0.6], [reset, 1.0]], name
        times = [t for t, _v in sc["line"]]
        assert times == sorted(times) and (times[0], times[-1]) == (chart["now"], chart["end"]), name
        six = next(cp for cp in sc["checkpoints"] if cp["after_seconds"] == 6 * 3600)
        assert (six["at"], six["value"]) == (checkpoint, expected), name
        for cp in sc["checkpoints"]:
            assert _cursor_at(sc["line"], cp["at"]) == cp["value"], (name, cp)
        assert (_cursor_at(sc["line"], reset - 0.05), _cursor_at(sc["line"], reset)) == (0.6, 1.0), name

    # The table: its reset row at the reset's own instant, every row in time
    # order by the instant it says — the 6-hour row of the same second before
    # a reset a fraction into it — and each saying what the cursor reads there.
    table = chart["table"]
    instants = [qs.parse_instant(row["at"]) for row in table]
    assert instants == sorted(instants)
    reset_row = next(row for row in table if row.get("event", "").startswith("reported reset"))
    assert qs.parse_instant(reset_row["at"]) == reset and "until" not in reset_row
    assert reset_row["at"] == qs.iso_exact(reset)
    span_row = next(row for row in table if row["at"] == qs.iso(checkpoint) and not row.get("event"))
    assert span_row["scenario_no_new_use"] == span_row["scenario_recent_pace"] == expected
    if before:
        assert table.index(span_row) < table.index(reset_row)
    for row in table:
        if "scenario_no_new_use" not in row or row.get("event") == "now":
            continue
        at = qs.parse_instant(row.get("until") or row["at"])
        for name, sc in chart["scenarios"].items():
            assert row["scenario_" + name] == round(_cursor_at(sc["line"], at), 2), (name, row)


def test_one_event_of_resets_a_fraction_apart_keeps_both_instants():
    """Two resets in one second, a checkpoint at its whole second before
    both: one event from the first to the last, each at its own instant."""
    now = NOW + 0.25
    first, last = (qs.parse_instant(_iso_exact(NOW + offset)) for offset in (21600.2, 21600.7))
    chart = _chart_read_at(now, [NOW + 21600.2, NOW + 21600.7], "24h")
    for name, sc in chart["scenarios"].items():
        assert [(e["at"], e["last"], e["accounts"], e["adds"], e["total_after"]) for e in sc["schedule"]] \
            == [(first, last, 2, 0.8, 2.0)], name
        line = sc["line"]
        assert [_cursor_at(line, t) for t in (NOW + 21600, first, last)] == [1.2, 1.6, 2.0], name
        assert next(cp for cp in sc["checkpoints"] if cp["after_seconds"] == 6 * 3600)["value"] == 1.2
    row = next(row for row in chart["table"] if row.get("event", "").startswith("reported reset"))
    assert (row["at"], row["until"]) == (qs.iso_exact(first), qs.iso_exact(last))
    assert row["scenario_no_new_use"] == 2.0
    # Whole seconds are written as iso() writes them, and iso() is unchanged.
    assert qs.iso_exact(NOW) == qs.iso(NOW) and qs.iso(first) == qs.iso(NOW + 21600)


def test_the_widgets_own_lineat_agrees_with_the_checkpoints_around_a_fractional_reset():
    """The real lineAt of widget.js, run on the published lines: at each
    checkpoint it reads the checkpoint, a hair before the reset the total
    before it, at the reset the total after it."""
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    head = "    function lineAt(points, t) {"
    assert source.count(head) == 1
    body = "function lineAt(points, t) {" + source.split(head, 1)[1].split("\n    }\n", 1)[0] + "\n}"
    cases = []
    for fraction, offset, _before in FRACTIONAL_RESETS:
        reset = qs.parse_instant(_iso_exact(NOW + offset))
        for sc in _chart_read_at(NOW + fraction, [NOW + offset], "24h")["scenarios"].values():
            checks = [[cp["at"], cp["value"]] for cp in sc["checkpoints"]] + [[reset - 0.05, 0.6], [reset, 1.0]]
            cases.append({"line": sc["line"], "checks": checks})
    script = body + textwrap.dedent("""
        const cases = JSON.parse(process.env.LINE_CASES);
        console.log(JSON.stringify(cases.map((c) => c.checks.map((check) => lineAt(c.line, check[0])))));
    """)
    result = subprocess.run([str(node), "-e", script], env={**os.environ, "LINE_CASES": json.dumps(cases)},
                            text=True, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [[value for _t, value in case["checks"]] for case in cases]


def _sightings_view(directory, first, horizon=None):
    """Sweeps every 120 s from ``first`` to now. Account a: one source held
    throughout and, at every other sweep, a reading seen at that sweep only
    — every fourth of them beside another source observed within a second of
    it at another value (the sources disagree). Account b holds throughout."""
    store = quota_history.HistoryStore(directory)

    def seen(offset):
        a = {"app": (first - 100.0, .30, 86400)}
        step = offset // 120
        if step % 2 == 0:
            a["rollout"] = (offset - 5.0, .31 + (step // 2 % 5) / 100.0, 86400)
            if step // 2 % 4 == 0:
                a["cli"] = (offset - 5.4, .40, 86400)
        return {"a": a, "b": {"app": (first - 100.0, .50, 86400)}}

    def snaps(offset):
        return [snap("codex", account, [constraint("primary", ratio, reset=reset)], source=source,
                     observed_abs=NOW + obs)
                for account, sources in seen(offset).items() for source, (obs, ratio, reset) in sources.items()]

    data = _scripted_sweeps(store, snaps, list(range(first, 1, 120)))
    view = plugin.build_view(data, "", NOW)
    view["reserve"] = (plugin.reserve_view(store, data, NOW, NOW, horizon=horizon, harness="codex",
                                           name_accounts=True) if horizon
                       else widget_reserve(store, data, NOW, NOW, harness="codex", name_accounts=True))
    return view


def test_the_chart_keeps_every_single_sweep_point_the_table_only_its_newest(tmp_path):
    chart = _sightings_view(tmp_path, -10800)["reserve"]["chart"]
    points = chart["points"]
    assert len(points) == 45 and chart["past_clipped_before"] is None
    assert sum(1 for _t, v in points if v is None) == 11
    assert [t for t, _v in points] == sorted(t for t, _v in points)
    # The payload's table is unchanged: a sample of the newest few only.
    sampled = [[qs.parse_instant(row["at"]), row["observed"]] for row in chart["table"] if row.get("sighting")]
    assert sampled == points[-qs.TABLE_PAST_ROWS:]


NODE_SIGHTINGS_PAGES = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
const details = (env) => classes(env.root, 'chart-table')[0];
function page(env) {
  const box = classes(details(env), 'sightings')[0];
  assert.ok(box, 'the sightings are listed under Details');
  return {
    caption: walk(box).find((n) => n.tagName === 'CAPTION').textContent,
    rows: walk(box).filter((n) => n.tagName === 'TR' && n.parentNode.tagName === 'TBODY')
      .map((tr) => tr.childNodes.map((td) => td.textContent)),
  };
}
const said = (p) => (p[1] === null ? ['not settled', 'sources disagree at this sweep']
                                    : [p[1].toFixed(2), 'seen at this sweep only']);
// The page shows points[lo, hi) in time order, each once in all of Details.
function expectPage(env, points, lo, hi) {
  const shown = page(env);
  assert.equal(shown.caption, 'Seen at one sweep only · ' + (lo + 1) + '–' + hi + ' of ' + points.length);
  assert.deepEqual(shown.rows.map((r) => r.slice(1)), points.slice(lo, hi).map(said));
  shown.rows.forEach((r) => assert.ok(r[0].length > 4, 'a time on every row'));
  const text = details(env).textContent;
  const valued = points.slice(lo, hi).filter((p) => p[1] !== null).length;
  assert.equal((text.match(/seen at this sweep only/g) || []).length, valued);
  assert.equal((text.match(/sources disagree at this sweep/g) || []).length, hi - lo - valued);
  assert.match(text, new RegExp(points.length + ' totals seen at one sweep only are listed separately below'));
}
const at = (env, key) => byFocus(env.root, key);
const disabled = (env, key) => at(env, key).getAttribute('aria-disabled') === 'true';

(async () => {
  const fx = JSON.parse(process.env.SIGHTINGS_FIXTURE);

  // More than the table's newest 8, fewer than a page: every one, no pages.
  const few = fx.small.reserve.chart.points;
  assert.ok(few.length > 8 && few.length <= 20, String(few.length));
  let env = await boot(fx.small);
  expectPage(env, few, 0, few.length);
  assert.equal(at(env, 'sightings:older'), undefined);
  // The recorded sample keeps its own count and lists no sighting.
  assert.match(details(env).textContent, /The table lists \d+ of \d+ recorded changes/);

  // More than a page: the newest page first, in time order.
  const points = fx.view.reserve.chart.points;
  const N = points.length;
  assert.ok(N > 40, String(N));
  let current = fx.view;
  env = await boot((url) => (/horizon=24h/.test(url) ? fx.day : current));
  const open = details(env);
  open.open = true;
  open.listeners.toggle[0]();
  expectPage(env, points, N - 20, N);
  assert.ok(disabled(env, 'sightings:newer') && !disabled(env, 'sightings:older'));

  // Older moves a page back, the keyboard stays on the button.
  click(env, 'sightings:older');
  expectPage(env, points, N - 40, N - 20);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'sightings:older');
  assert.ok(!disabled(env, 'sightings:newer'));

  // A poll keeps Details open, the page and the focus.
  env.interval()();
  await settle();
  assert.equal(details(env).open, true);
  expectPage(env, points, N - 40, N - 20);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'sightings:older');

  // A new sighting at the next poll: the same page back from the newest,
  // each older page on by one row.
  const grown = JSON.parse(JSON.stringify(fx.view));
  const more = grown.reserve.chart.points;
  more.push([grown.reserve.chart.now - 30, 1.11]);
  current = grown;
  env.interval()();
  await settle();
  expectPage(env, more, more.length - 40, more.length - 20);

  // To the oldest page; there Older says so, does nothing and keeps focus.
  click(env, 'sightings:older');
  expectPage(env, more, 0, more.length - 40);
  assert.ok(disabled(env, 'sightings:older'));
  click(env, 'sightings:older');
  expectPage(env, more, 0, more.length - 40);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'sightings:older');

  // Fewer sightings at a poll (older ones out of the span): the page is
  // the last there is, not an empty one.
  const shrunk = JSON.parse(JSON.stringify(fx.view));
  shrunk.reserve.chart.points = shrunk.reserve.chart.points.slice(-25);
  current = shrunk;
  env.interval()();
  await settle();
  expectPage(env, shrunk.reserve.chart.points, 0, 5);
  assert.ok(disabled(env, 'sightings:older'));
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'sightings:older');
  click(env, 'sightings:newer');
  expectPage(env, shrunk.reserve.chart.points, 5, 25);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'sightings:newer');
  click(env, 'sightings:older');

  // Another span is another list: it starts at its newest page.
  click(env, 'horizon:24h');
  await settle();
  const day = fx.day.reserve.chart;
  assert.equal(day.horizon, '24h');
  expectPage(env, day.points, day.points.length - 20, day.points.length);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_pages_through_every_single_sweep_point(tmp_path):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    fixture = {"small": _sightings_view(tmp_path / "small", -3000),
               "view": _sightings_view(tmp_path / "week", -10800),
               "day": _sightings_view(tmp_path / "day", -10800, horizon="24h")}
    assert fixture["view"]["reserve"]["chart"]["horizon"] == "7d"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_SIGHTINGS_PAGES)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path), "SIGHTINGS_FIXTURE": json.dumps(fixture)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Owner, 8 Oct: the 5-hour chart could not be found. A limit's name is the
# control that charts it, and now looks like one: the chart mark and "Show
# chart" inside the same button, one handler, its spoken label unchanged.


def _five_hour_and_weekly(directory):
    """Two Claude accounts with a 5-hour and a weekly limit; the 5-hour one
    has the lowest share left, so it is the family's default chart."""
    store = quota_history.HistoryStore(directory)

    def snaps(offset):
        return [snap("claude", sid, [constraint("five_hour", five, FIVE_H, reset=3600, label="5 hour"),
                                     constraint("seven_day", week, WEEK, reset=3 * 86400, label="7 day")],
                     observed_abs=NOW + offset - 5)
                for sid, five, week in (("p", .70, .20), ("q", .60, .30))]

    data = _scripted_sweeps(store, snaps, list(range(-2400, 1, 120)), sids=("p", "q"), harness="claude")
    base = plugin.build_view(data, "", NOW)

    def answer(group_key, horizon):
        view = json.loads(json.dumps(base))
        view["reserve"] = plugin.reserve_view(store, data, NOW, NOW, harness="claude", group=group_key,
                                              horizon=horizon, name_accounts=True)
        return view

    first = answer("", "24h")
    keys = {g["key"].split("|")[1]: g for g in first["reserve"]["summary"]["groups"]}
    five, week = keys["five_hour"], keys["seven_day"]
    assert five["tightest"] and not week["tightest"]
    assert first["reserve"]["chart"]["group_key"] == five["key"] and first["reserve"]["chart"]["horizon"] == "24h"
    weekly = answer(week["key"], "7d")
    assert weekly["reserve"]["chart"]["group_key"] == week["key"]
    return {"five": first, "week": weekly, "five_key": five["key"], "week_key": week["key"]}


NODE_FIVE_HOUR_TO_WEEKLY = r"""
const nameOf = (env, key) => byFocus(env.root, 'limit:' + key);
const rowOf = (env, key) => nameOf(env, key).parentNode;
const cueOf = (env, key) => classes(rowOf(env, key), 'l-chart')[0];
const has = (c, name) => String(c.className).split(/\s+/).includes(name);
const below = (env, key) => {
  const row = rowOf(env, key);
  const next = row.parentNode.childNodes[row.parentNode.childNodes.indexOf(row) + 1];
  return next && String(next.className).split(/\s+/).includes('tl-block') ? next : null;
};
function expectCharted(env, shown, other) {
  assert.equal(nameOf(env, shown).getAttribute('aria-pressed'), 'true');
  assert.equal(nameOf(env, other).getAttribute('aria-pressed'), 'false');
  assert.equal(cueOf(env, shown).textContent, 'Hide chart');
  assert.equal(cueOf(env, other).textContent, 'Show chart');
  assert.ok(has(cueOf(env, shown), 'on') && !has(cueOf(env, other), 'on'));
  assert.ok(has(rowOf(env, shown), 'sel') && !has(rowOf(env, other), 'sel'));
  assert.match(nameOf(env, shown).getAttribute('aria-label'), / — its timeline is shown below$/);
  assert.match(nameOf(env, other).getAttribute('aria-label'), / — show its timeline$/);
  assert.ok(nameOf(env, shown).getAttribute('aria-label').startsWith(cueOf(env, shown).textContent + ' — '));
  assert.ok(nameOf(env, other).getAttribute('aria-label').startsWith(cueOf(env, other).textContent + ' — '));
  assert.ok(below(env, shown), 'the timeline stands under the charted limit');
  assert.equal(below(env, other), null);
  assert.equal(classes(env.root, 'tl-block').length, 1);
}

(async () => {
  const fx = JSON.parse(process.env.SWITCH_FIXTURE);
  const asked = (url) => decodeURIComponent(url);
  const env = await boot((url) => (asked(url).includes('group=' + fx.week_key) ? fx.week : fx.five));
  const five = fx.five_key, week = fx.week_key;

  // 0.8.1: the row's one control is the chart toggle in its own slot — a
  // button with the chart mark and two words, one handler, the spoken
  // summary — a direct child of the row beside the name, which is text.
  [five, week].forEach((key) => {
    const btn = nameOf(env, key);
    assert.equal(btn.tagName, 'BUTTON');
    assert.equal(btn.listeners.click.length, 1);
    assert.equal(cueOf(env, key), btn, 'the control is the cue');
    assert.ok(has(btn.parentNode, 'lrow'));
    assert.equal(classes(btn.parentNode, 'l-chart').length, 1, 'one control per row');
    assert.equal(walk(btn).filter((n) => n.tagName === 'SVG').length, 1, 'the chart mark');
    assert.equal(walk(btn).filter((n) => n !== btn && n.tagName === 'BUTTON').length, 0);
    assert.equal(classes(btn, 'l-name-text').length, 0, 'the name is not inside the control');
    const name = classes(btn.parentNode, 'l-name')[0];
    assert.notEqual(name.tagName, 'BUTTON');
    assert.equal(name.listeners.click, undefined);
    assert.match(classes(name, 'l-name-text')[0].textContent, key === five ? /^5-hour$/ : /^Weekly$/);
  });
  assert.equal(classes(env.root, 'chart-toggle').length, 0, 'no second toggle inside the timeline');
  // The family's lowest-left limit, the 5-hour one, is charted first.
  expectCharted(env, five, week);
  assert.equal(env.calls.length, 1);

  // 5 hour -> weekly: the weekly name asks once, with reuse, for its chart
  // over its own span, and its timeline replaces the 5-hour one.
  nameOf(env, week).focus();
  nameOf(env, week).listeners.click[0]({ stopPropagation() {} });
  assert.match(below(env, week).textContent, /Loading the timeline for this limit/);
  await settle();
  assert.equal(env.calls.length, 2);
  const url = asked(env.calls[1].url);
  assert.ok(url.includes('group=' + week), url);
  assert.match(url, /horizon=7d/);
  assert.match(url, /reuse=1/);
  expectCharted(env, week, five);
  assert.ok(walk(below(env, week)).some((n) => n.getAttribute('class') === 'chart-svg'));
  assert.equal(byFocus(env.root, 'horizon:7d').getAttribute('aria-pressed'), 'true');
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'limit:' + week);

  // ... and back: the 5-hour chart over 24 hours.
  nameOf(env, five).focus();
  nameOf(env, five).listeners.click[0]({ stopPropagation() {} });
  await settle();
  assert.match(asked(env.calls.at(-1).url), /horizon=24h/);
  expectCharted(env, five, week);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'limit:' + five);

  // On the charted limit the same button folds the timeline, and says so;
  // folded, nothing stands under any row and no row is drawn as charted.
  nameOf(env, five).listeners.click[0]({ stopPropagation() {} });
  assert.equal(cueOf(env, five).textContent, 'Show chart');
  assert.match(nameOf(env, five).getAttribute('aria-label'), /^Show chart — /);
  assert.equal(nameOf(env, five).getAttribute('aria-pressed'), 'false');
  assert.ok(!has(cueOf(env, five), 'on'));
  assert.equal(walk(env.root).filter((n) => n.getAttribute('class') === 'chart-svg').length, 0);
  assert.equal(below(env, five), null);
  assert.equal(classes(env.root, 'tl-block').length, 0);
  assert.equal(classes(env.root, 'sel').length, 0);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_switches_from_the_five_hour_chart_to_the_weekly_one(tmp_path):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    fixture = _five_hour_and_weekly(tmp_path)
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_FIVE_HOUR_TO_WEEKLY)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path), "SWITCH_FIXTURE": json.dumps(fixture)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# A focused page button whose pages go (45 sightings, then 20 or fewer, then
# none) hands the keyboard to the Details summary; a span left while its
# chart had no sightings yet is still another list.

NODE_SIGHTINGS_FOCUS = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
const details = (env) => classes(env.root, 'chart-table')[0];
const listed = (env) => classes(details(env), 'sightings')[0];
const caption = (env) => walk(listed(env)).find((n) => n.tagName === 'CAPTION').textContent;
const focused = (env) => env.document.activeElement;
const inTree = (env, node) => walk(env.root).includes(node);
// Nodes are compared by identity only: a failed deepEqual would print the tree.
function onSummary(env) {
  const summary = byFocus(env.root, 'chart-table');
  const at = focused(env);
  assert.ok(summary && at === summary,
    'the keyboard is on the Details summary, not on ' + (at && at.getAttribute('data-focus')));
  assert.ok(inTree(env, at), 'the focus is on a node on the page');
}
function withPoints(view, points) {
  const out = JSON.parse(JSON.stringify(view));
  out.reserve.chart.points = points;
  return out;
}
async function poll(env) {
  const before = env.calls.length;
  env.interval()();
  await settle();
  return env.calls.slice(before);
}

(async () => {
  const fx = JSON.parse(process.env.SIGHTINGS_FIXTURE);
  const points = fx.view.reserve.chart.points;
  const N = points.length;
  assert.equal(N, 45);

  // 45 → 20 or fewer → none, the keyboard on Older throughout.
  let current = fx.view;
  let env = await boot(() => current);
  details(env).open = true;
  details(env).listeners.toggle[0]();
  click(env, 'sightings:older');
  assert.equal(caption(env), 'Seen at one sweep only · 6–25 of 45');
  assert.equal(focused(env).getAttribute('data-focus'), 'sightings:older');
  const asked = await poll(env);
  assert.equal(asked.length, 1, JSON.stringify(asked));
  assert.equal(focused(env).getAttribute('data-focus'), 'sightings:older');
  assert.ok(inTree(env, focused(env)), 'the focus is on a node on the page');

  current = withPoints(fx.view, points.slice(-15));
  assert.deepEqual(await poll(env), asked, 'no other request');
  assert.equal(details(env).open, true);
  assert.ok(!byFocus(env.root, 'sightings:older'), 'no page buttons');
  assert.equal(caption(env), 'Seen at one sweep only · 1–15 of 15');
  onSummary(env);

  current = withPoints(fx.view, []);
  assert.deepEqual(await poll(env), asked, 'no other request');
  assert.equal(details(env).open, true);
  assert.ok(!listed(env), 'no sightings listed');
  onSummary(env);
  assert.deepEqual(await poll(env), asked);
  onSummary(env);

  // They come back: the newest page, the keyboard left on the summary.
  current = fx.view;
  assert.deepEqual(await poll(env), asked);
  assert.equal(caption(env), 'Seen at one sweep only · 26–45 of 45');
  onSummary(env);
  assert.equal(env.calls.filter((c) => c.method !== 'GET').length, 0);

  // Away to a span with no sightings yet and back: the newest page again.
  let day = withPoints(fx.day, []);
  current = fx.view;
  env = await boot((url) => (/horizon=24h/.test(url) ? day : current));
  details(env).open = true;
  details(env).listeners.toggle[0]();
  click(env, 'sightings:older');
  assert.equal(caption(env), 'Seen at one sweep only · 6–25 of 45');
  click(env, 'horizon:24h');
  await settle();
  assert.equal(details(env).open, true);
  assert.ok(!listed(env), 'no sightings listed');
  click(env, 'horizon:7d');
  await settle();
  assert.equal(caption(env), 'Seen at one sweep only · 26–45 of 45');

  // And when the empty span's own sightings arrive, at its newest page.
  click(env, 'sightings:older');
  click(env, 'horizon:24h');
  await settle();
  assert.ok(!listed(env), 'no sightings listed');
  day = fx.day;
  await poll(env);
  const M = fx.day.reserve.chart.points.length;
  assert.ok(M > 20, String(M));
  assert.equal(caption(env), 'Seen at one sweep only · ' + (M - 19) + '–' + M + ' of ' + M);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_keeps_the_keyboard_when_the_sightings_pages_go(tmp_path):
    import test_quotas

    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    fixture = {"view": _sightings_view(tmp_path / "week", -10800),
               "day": _sightings_view(tmp_path / "day", -10800, horizon="24h")}
    assert fixture["view"]["reserve"]["chart"]["horizon"] == "7d"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_SIGHTINGS_FOCUS)],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path), "SIGHTINGS_FIXTURE": json.dumps(fixture)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
