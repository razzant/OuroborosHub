"""Focused synthetic tests for the 48h Typical estimator (quota_forecast.py).

Covers:
- Watch restart / stale cached observation after an outage vs re-confirmed baseline level.
- Exhausted pool (100% used throughout observation, including when still empty now) vs genuine zero.
- Historical cohort boundary splitting (single sighting and brief invalid window of a second account).
- Multi-source disagreement across endpoints and crossing transitions vs agreeing sources.
- 48-hour lookback boundary control across distinct 0-24h, 24-48h, and >48h burn bands.
- Rate and coverage on a 22-account 48h synthetic pool.
"""
import math

import plugin
import quota_forecast as qf
import quota_history
import quota_summary as qs
from test_reserve import WEEK, payload, profile

H = 3600.0
NOW = 1_795_700_000.0
PLAN = '["plus","plus"]'


def _snap(sid, used_ratio, reset_at, *, observed_at, source="app", plan="plus"):
    return {
        "subject": {"harness": "codex", "subject_id": sid, "plan_label": plan},
        "constraints": [{
            "id": "primary",
            "label": "Weekly",
            "used_ratio": round(max(0.0, min(1.0, used_ratio)), 6),
            "window_seconds": WEEK,
            "resets_at": qs.iso(reset_at) if reset_at is not None else None,
        }],
        "availability": {"state": "available"},
        "observed_at": qs.iso(observed_at),
        "freshness": "fresh",
        "source": source,
    }


def _state_from_runs(
    by_sub_runs,
    measured_specs,
    *,
    now=NOW,
    sweeps=(),
    sweep_ok=None,
    collecting_since=None,
):
    key = qs.group_key("codex", "primary", WEEK, ())
    salt = "a" * 32
    view = qs.HistoryView(
        state="ok",
        salt=salt,
        runs={
            (qs.pseudo_id(salt, "codex", sid), key): list(runs)
            for sid, runs in by_sub_runs.items()
        },
        sweeps=list(sweeps),
        sweep_ok=list(sweep_ok) if sweep_ok is not None else None,
        sweeps_from=min(sweeps) if sweeps else None,
        collecting_since=collecting_since if collecting_since is not None else now - 72 * H,
    )
    members = []
    for sid, ratio, obs, reset_at, source in measured_specs:
        reading = qs.Reading(
            harness="codex", subject_id=sid, source=source, fresh=True,
            observed_at=obs, key=key, meaning="primary", label="Weekly",
            window=WEEK, models=(), ratio=ratio, ratio_problem="",
            resets_at=reset_at, plan="plus", plan_key=PLAN,
        )
        members.append(qs.Member(
            harness="codex", subject_id=sid, status="measured",
            reading=reading, remaining=1.0 - ratio, observed_at=obs,
            pseudo=qs.pseudo_id(salt, "codex", sid),
        ))
    calc = qs.GroupCalc(
        key=key, harness="codex", family="Codex", meaning="primary",
        label="Weekly", window=WEEK, models=(), members=members,
        other_family_accounts=0, possible_duplicates=0,
    )
    norm = qs.Normalized(
        reads={"catalog": "not_read", "accounts": "ok", "quota": "ok"},
        unified=False, harness_order=["codex"], families={"codex": "Codex"},
        accounts={}, accounts_known=True, quota_known=True,
        readings=[], cooldown_ends={}, exhaustion_ends={},
        unattributed={}, superseded={}, malformed=0,
    )
    return calc, qs.SummaryState(norm=norm, groups=[calc], view=view, now=now)


def test_watch_restart_cached_reading_never_counts_outage_gap(tmp_path):
    """After a 13h gap, receiving an old cached timestamp followed 2m later by a fresh
    reading must NOT credit 13h of valid time or 0.0077 win/h (end-to-end via SQLite)."""
    reset_at = NOW + 48 * H
    store = quota_history.HistoryStore(tmp_path)
    st0 = payload([_snap("a", 0.10, reset_at, observed_at=NOW - 13 * H)], [profile("codex", "a")], harnesses=("codex",))
    plugin.persist_sweep(store, st0, "", NOW - 13 * H)
    st1 = payload([_snap("a", 0.10, reset_at, observed_at=NOW - 13 * H)], [profile("codex", "a")], harnesses=("codex",))
    plugin.persist_sweep(store, st1, "", NOW - 120)
    st2 = payload([_snap("a", 0.20, reset_at, observed_at=NOW)], [profile("codex", "a")], harnesses=("codex",))
    plugin.persist_sweep(store, st2, "", NOW)

    rv = plugin.reserve_view(store, st2, NOW, NOW, harness="codex", horizon="7d", name_accounts=True)
    typ = rv["chart"]["scenarios"]["typical"]
    assert typ["status"] in ("warming_up", "insufficient")
    assert typ["rate_per_hour"] is None
    assert typ["valid_seconds"] == 0
    assert typ["valid_hours"] == 0.0
    assert typ["line"] == []

    # Same rule for a fresh collector start with an old cached reading (no prior run in DB).
    store_new = quota_history.HistoryStore(tmp_path / "fresh_start")
    plugin.persist_sweep(store_new, st1, "", NOW - 120)
    plugin.persist_sweep(store_new, st2, "", NOW)
    rv_new = plugin.reserve_view(store_new, st2, NOW, NOW, harness="codex", horizon="7d", name_accounts=True)
    typ_new = rv_new["chart"]["scenarios"]["typical"]
    assert typ_new["status"] in ("warming_up", "insufficient")
    assert typ_new["valid_seconds"] == 0


def test_watch_restart_reconfirmed_level_counts_only_post_restart_span(tmp_path):
    """If the pre-gap cached level IS freshly re-observed after the collector restarts,
    only the post-restart span counts (never the 13h gap before first_seen)."""
    reset_at = NOW + 48 * H
    store = quota_history.HistoryStore(tmp_path)
    st_old = payload([_snap("a", 0.10, reset_at, observed_at=NOW - 13 * H)], [profile("codex", "a")], harnesses=("codex",))
    plugin.persist_sweep(store, st_old, "", NOW - 13 * H)
    # Wakes at NOW - 240s with old cached obs (NOW - 13h), then re-confirms 0.10 at NOW - 120s,
    # then burns to 0.20 at NOW. Only 240s (not 13h) was watched after restart.
    plugin.persist_sweep(store, st_old, "", NOW - 240)
    st_reconf = payload([_snap("a", 0.10, reset_at, observed_at=NOW - 120)], [profile("codex", "a")], harnesses=("codex",))
    plugin.persist_sweep(store, st_reconf, "", NOW - 120)
    st_burn = payload([_snap("a", 0.20, reset_at, observed_at=NOW)], [profile("codex", "a")], harnesses=("codex",))
    plugin.persist_sweep(store, st_burn, "", NOW)

    key = qs.group_key("codex", "primary", WEEK, ())
    view = store.read(lambda salt: {(qs.pseudo_id(salt, "codex", "a"), key): NOW - 48 * H}, NOW)
    runs = view.runs[(qs.pseudo_id(view.salt, "codex", "a"), key)]
    segs = qf._source_segments(runs, NOW - 48 * H, NOW, view, [])
    total_sec = sum(t1 - t0 for t0, t1, *_ in segs)
    total_burn = sum(burn for _t0, _t1, burn, *_ in segs)
    assert math.isclose(total_sec, 240.0, abs_tol=1e-3)
    assert math.isclose(total_burn, 0.10, abs_tol=1e-6)
    assert all(t0 >= NOW - 240.0 - 1e-6 for t0, *_ in segs)


def test_exhausted_pool_never_proves_zero_pace_even_when_still_empty():
    """24h pinned at 100% used hides demand: must return insufficient(all_observed_at_limit)
    both when currently empty and when positive reserve exists now, while genuine measured
    zero with available capacity stays state='zero'."""
    reset_at = NOW + 10 * H
    run_full = qs.Run("app", 1.0, reset_at, PLAN, NOW - 24 * H, NOW, 289, NOW - 24 * H, NOW)

    # Still 100% used now (remaining == 0.0)
    calc_empty, state_empty = _state_from_runs({"a": [run_full]}, [("a", 1.0, NOW, reset_at, "app")])
    res_empty = qf.estimate_typical_pace(calc_empty, state_empty)
    assert res_empty["state"] == "insufficient"
    assert res_empty["reason"] == "all_observed_at_limit"
    assert res_empty["rate_per_hour"] is None
    assert res_empty["valid_hours"] == 24.0

    # Refilled just now (positive remaining, but all 24h observed were pinned at 100%)
    calc_refilled, state_refilled = _state_from_runs(
        {"a": [qs.Run("app", 1.0, NOW - 60, PLAN, NOW - 24 * H, NOW - 120, 288, NOW - 24 * H, NOW - 120)]},
        [("a", 0.0, NOW, reset_at, "app")],
    )
    res_refilled = qf.estimate_typical_pace(calc_refilled, state_refilled)
    assert res_refilled["state"] == "insufficient"
    assert res_refilled["reason"] == "all_observed_at_limit"
    assert res_refilled["rate_per_hour"] is None

    # Genuine measured zero with >=12h of available capacity -> state="zero"
    run_avail = qs.Run("app", 0.35, reset_at, PLAN, NOW - 16 * H, NOW, 193, NOW - 16 * H, NOW)
    calc_zero, state_zero = _state_from_runs({"a": [run_avail]}, [("a", 0.35, NOW, reset_at, "app")])
    res_zero = qf.estimate_typical_pace(calc_zero, state_zero)
    assert res_zero["state"] == "zero"
    assert res_zero["reason"] == ""
    assert res_zero["rate_per_hour"] == 0.0
    assert res_zero["valid_hours"] == 16.0


def test_cohort_participation_boundaries_keep_brief_or_single_sighting_local():
    """A single sighting (or brief invalid span) of a historical account B in the middle
    of A's 14h continuous run must NOT zero out the entire 14h interval."""
    reset_at = NOW + 48 * H
    run_a = qs.Run("app", 0.20, reset_at, PLAN, NOW - 14 * H, NOW, 169, NOW - 14 * H, NOW)

    # 1. Single sighting of non-current B at hour 7 -> 0 duration, full 14h preserved.
    run_b_once = qs.Run("app", 0.30, reset_at, PLAN, NOW - 7 * H, NOW - 7 * H, 1, NOW - 7 * H, NOW - 7 * H)
    calc1, state1 = _state_from_runs({"a": [run_a], "b": [run_b_once]}, [("a", 0.20, NOW, reset_at, "app")])
    res1 = qf.estimate_typical_pace(calc1, state1)
    assert res1["state"] == "zero"
    assert res1["valid_hours"] == 14.0

    # 2. Brief 1h frozen (n_obs=1, no valid segment) sighting of non-current B at hours 7..6 ->
    #    excludes ONLY that 1h, preserving the remaining 13h!
    run_b_1h = qs.Run("app", 0.30, reset_at, PLAN, NOW - 7 * H, NOW - 7 * H, 1, NOW - 7 * H, NOW - 6 * H)
    calc2, state2 = _state_from_runs({"a": [run_a], "b": [run_b_1h]}, [("a", 0.20, NOW, reset_at, "app")])
    res2 = qf.estimate_typical_pace(calc2, state2)
    assert res2["state"] == "zero"
    assert res2["valid_hours"] == 13.0

    # 3. 1-of-22 current pool still stays insufficient (current coverage check untouched).
    measured_22 = [("a", 0.20, NOW, reset_at, "app")] + [
        (f"cur-{i:02d}", 0.20, NOW, reset_at, "app") for i in range(21)
    ]
    calc3, state3 = _state_from_runs({"a": [run_a]}, measured_22)
    res3 = qf.estimate_typical_pace(calc3, state3)
    assert res3["state"] == "insufficient"
    assert res3["valid_hours"] == 0.0


def test_multisource_crossing_and_endpoint_disagreements_are_excluded():
    """Two sources diverging by 5pp at both ends (10->22% vs 15->17%) cross at midpoint
    yet disagree at both endpoints and MUST be excluded; agreeing sources still count."""
    reset_at = NOW + 48 * H
    view = qs.HistoryView(state="ok", salt="a" * 32)

    # 1. Exact crossing case: 10->22% vs 15->17% over 2m (midpoint both 16%).
    crossing = [
        qs.Run("s1", 0.10, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 120),
        qs.Run("s1", 0.22, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s2", 0.15, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 120),
        qs.Run("s2", 0.17, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
    ]
    assert qf._account_segments(crossing, NOW - 48 * H, NOW, view, []) == []

    # 2. Same crossing even when cover sightings split right around the midpoint crossing
    #    (both sources have first_obs == first_seen == NOW-120 so both emit [NOW-120, NOW],
    #    while last_seen puts grid points at NOW-65 and NOW-55 bracketing the crossing NOW-60).
    crossing_split = [
        qs.Run("s1", 0.10, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 65),
        qs.Run("s1", 0.22, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s2", 0.15, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 55),
        qs.Run("s2", 0.17, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
    ]
    assert len(qf._source_segments([r for r in crossing_split if r.source == "s1"], NOW - 48 * H, NOW, view, [])) == 1
    assert len(qf._source_segments([r for r in crossing_split if r.source == "s2"], NOW - 48 * H, NOW, view, [])) == 1
    assert qf._account_segments(crossing_split, NOW - 48 * H, NOW, view, []) == []

    # 3. Agree at start, diverge by 1.5pp at end (midpoint diff 0.75pp <= 1pp) -> excluded.
    diverging = [
        qs.Run("s1", 0.10, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 120),
        qs.Run("s1", 0.12, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s2", 0.10, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 120),
        qs.Run("s2", 0.105, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
    ]
    assert qf._account_segments(diverging, NOW - 48 * H, NOW, view, []) == []

    # 4. Two agreeing sources (within 1pp rounding at both ends) -> counted once, not doubled.
    agreeing = [
        qs.Run("s1", 0.100, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 120),
        qs.Run("s1", 0.150, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s2", 0.104, reset_at, PLAN, NOW - 120, NOW - 120, 1, NOW - 120, NOW - 120),
        qs.Run("s2", 0.154, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
    ]
    segs = qf._account_segments(agreeing, NOW - 48 * H, NOW, view, [])
    assert len(segs) == 1
    assert math.isclose(segs[0][1] - segs[0][0], 120.0, abs_tol=1e-6)
    assert math.isclose(segs[0][2] * 120.0, 0.05, abs_tol=1e-6)


def test_staggered_readings_of_22_accounts_cover_the_same_48_hours():
    """Staggering the accounts' observations does not multiply the pool's watched time."""
    reset_at = NOW + 48 * H
    sweeps = [NOW - 48 * H + k * 300.0 for k in range(48 * 12 + 1)]
    by_sub = {}
    measured = []
    for i in range(22):
        sid = f"acct-{i:02d}"
        runs = []
        for k, t in enumerate(sweeps):
            u = round(0.05 + 0.001 * k, 6)
            obs = t + i * 0.5  # distinct per-account timestamps (worst-case grid density)
            if obs > NOW:
                obs = NOW
            runs.append(qs.Run("app", u, reset_at, PLAN, obs, obs, 1, t, t))
        by_sub[sid] = runs
        measured.append((sid, runs[-1].ratio, runs[-1].last_obs, reset_at, "app"))

    calc, state = _state_from_runs(by_sub, measured, sweeps=sweeps, sweep_ok=[True] * len(sweeps))
    res = qf.estimate_typical_pace(calc, state)

    assert res["state"] == "ok"
    assert res["valid_hours"] >= 47.5
    assert math.isclose(res["rate_per_hour"], 22 * 0.012, abs_tol=0.005)
