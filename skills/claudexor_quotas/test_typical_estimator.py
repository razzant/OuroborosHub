"""Focused synthetic tests for the 48h Typical estimator (quota_forecast.py).

Covers:
- Watch restart / stale cached observation after an outage vs re-confirmed baseline level.
- Exhaustion censoring (full-pool 100% excluded from denominator; all_observed_at_limit
  only at 0h reserve; mixed positive hours <12h returns insufficient/few_hours).
- Historical cohort boundary splitting (single sighting and brief invalid window).
- Same-timestamp provider corrections (persisted & uncollected refresh == clean control,
  gapped correction retracts without bridging, plan/cycle change, genuine later restore,
  multi-obs plateau with retracted end never extrapolates hidden timestamps).
- Multi-source agreement without linear chords or artificial padding: 0.4pp offset,
  decimal vs integer rounding, bursty 5m vs 30m, frozen n_obs==1 cached feeds (including
  first sighted after old timestamp / outside watch), live refresh across all fresh
  recordable sources (rejecting expired/junk), and real/3-source/reset conflicts.
- 80% coverage non-extrapolation and 22-account 48h synthetic pool.
- Account-level 3-source known-reset conflicts (never masked by a resetless 3rd source).
- Bounded history reads: 24h vs 7d Typical stability under MAX_READ_ROWS and honest
  unavailable/history_truncated reporting when the fresh 48h window itself is truncated.
"""
import math
import sqlite3

import plugin
import quota_forecast as qf
import quota_history
import quota_summary as qs
from test_reserve import FIVE_H, WEEK, payload, profile

H = 3600.0
NOW = 1_795_700_000.0
PLAN = '["plus","plus"]'


def _snap(sid, used_ratio, reset_at, *, observed_at, source="app", plan="plus", freshness="fresh"):
    return {
        "subject": {"harness": "codex", "subject_id": sid, "plan_label": plan},
        "constraints": [{
            "id": "primary",
            "label": "Weekly",
            "used_ratio": round(max(0.0, min(1.0, used_ratio)), 6) if isinstance(used_ratio, (int, float)) and 0.0 <= used_ratio <= 1.0 else used_ratio,
            "window_seconds": WEEK,
            "resets_at": qs.iso(reset_at) if reset_at is not None else None,
        }],
        "availability": {"state": "available"},
        "observed_at": qs.iso(observed_at),
        "freshness": freshness,
        "source": source,
    }


def _persist_typical(path, sweep_fn, times, *, sids=("a",), live_snaps=None, now=NOW):
    """Persist synthetic sweeps into SQLite and return the Typical scenario from reserve_view."""
    store = quota_history.HistoryStore(path)
    profs = [profile("codex", sid) for sid in sids]
    last_st = None
    for idx, t in enumerate(times):
        st = payload(sweep_fn(idx, t), profs, harnesses=("codex",))
        plugin.persist_sweep(store, st, "", t)
        last_st = st
    status = payload(live_snaps, profs, harnesses=("codex",)) if live_snaps is not None else last_st
    rv = plugin.reserve_view(store, status, now, now, harness="codex", horizon="7d", name_accounts=True)
    return rv["chart"]["scenarios"]["typical"], store, status


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
    assert res_empty["valid_seconds"] == 0
    assert res_empty["valid_hours"] == 0.0

    # Refilled just now (positive remaining, but all 24h observed were pinned at 100%)
    calc_refilled, state_refilled = _state_from_runs(
        {"a": [qs.Run("app", 1.0, NOW - 60, PLAN, NOW - 24 * H, NOW - 120, 288, NOW - 24 * H, NOW - 120)]},
        [("a", 0.0, NOW, reset_at, "app")],
    )
    res_refilled = qf.estimate_typical_pace(calc_refilled, state_refilled)
    assert res_refilled["state"] == "insufficient"
    assert res_refilled["reason"] == "all_observed_at_limit"
    assert res_refilled["rate_per_hour"] is None
    assert res_refilled["valid_hours"] == 0.0

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


def test_same_timestamp_correction_matches_clean_control_persisted_and_live(tmp_path):
    """Unbroken 20->90% corrected to 21% at the same observed_at counts the exact same
    20->21 change as a clean 21% control (both persisted in SQLite and on uncollected
    refresh); gapped corrections retract 90% without bridging; plan/cycle changes stay
    excluded; genuine later restores keep 20->90%; multi-obs retracted-end plateaus do
    not extrapolate hidden timestamps."""
    reset_at = NOW + 48 * H
    t0 = NOW - 16 * H
    t_corr = NOW - 8 * H
    times = [t0 + k * 300.0 for k in range(16 * 12 + 1)]

    # 1a. Persisted SQLite: unbroken 20->90->21 vs clean 21% control -> identical estimate.
    def _persisted_corr(_i, t):
        if t < t_corr - 1:
            return [_snap("a", 0.20, reset_at, observed_at=t)]
        if abs(t - t_corr) < 1:
            return [_snap("a", 0.90, reset_at, observed_at=t_corr)]
        if abs(t - (t_corr + 300.0)) < 1:
            return [_snap("a", 0.21, reset_at, observed_at=t_corr)]
        return [_snap("a", 0.21, reset_at, observed_at=t)]

    def _persisted_clean(_i, t):
        if t < t_corr - 1:
            return [_snap("a", 0.20, reset_at, observed_at=t)]
        obs = t_corr if abs(t - (t_corr + 300.0)) < 1 else t
        return [_snap("a", 0.21, reset_at, observed_at=obs)]

    typ_corr, store_corr, st_corr = _persist_typical(tmp_path / "p_corr", _persisted_corr, times)
    typ_clean, store_clean, st_clean = _persist_typical(tmp_path / "p_clean", _persisted_clean, times)
    assert typ_corr["status"] == typ_clean["status"] == "ok"
    assert typ_corr["valid_seconds"] == typ_clean["valid_seconds"] == round(16 * H)
    assert typ_corr["valid_hours"] == typ_clean["valid_hours"] == 16.0
    assert typ_corr["rate_per_hour"] == typ_clean["rate_per_hour"]
    assert math.isclose(typ_corr["rate_per_hour"], 0.01 / 16.0, abs_tol=1e-4)
    # Also confirm exact unrounded estimator agreement (0.01 burn / 16.0h = 0.000625/h).
    key = qs.group_key("codex", "primary", WEEK, ())
    s_corr = qs.compute(st_corr, store_corr.read(lambda s: {(qs.pseudo_id(s, "codex", "a"), key): NOW - 48 * H}, NOW), NOW)
    s_clean = qs.compute(st_clean, store_clean.read(lambda s: {(qs.pseudo_id(s, "codex", "a"), key): NOW - 48 * H}, NOW), NOW)
    est_corr = qf.estimate_typical_pace(s_corr.groups[0], s_corr)
    est_clean = qf.estimate_typical_pace(s_clean.groups[0], s_clean)
    assert est_corr == est_clean
    assert math.isclose(est_corr["observed_burn"], 0.01, abs_tol=1e-6)
    assert math.isclose(est_corr["rate_per_hour"], 0.01 / 16.0, abs_tol=1e-6)

    # 1b. Not-yet-collected refresh: 20->90 persisted up to NOW-300, live 21 at same obs vs clean control.
    t_last = NOW - 300.0
    pre_times = [t0 + k * 300.0 for k in range(16 * 12)]  # ends at t_last
    typ_live_corr, store_lc, st_lc = _persist_typical(
        tmp_path / "live_corr",
        lambda _i, t: [_snap("a", 0.90 if abs(t - t_last) < 1 else 0.20, reset_at, observed_at=t)],
        pre_times,
        live_snaps=[_snap("a", 0.21, reset_at, observed_at=t_last)],
    )
    typ_live_clean, store_lcl, st_lcl = _persist_typical(
        tmp_path / "live_clean",
        lambda _i, t: [_snap("a", 0.21 if abs(t - t_last) < 1 else 0.20, reset_at, observed_at=t)],
        pre_times,
        live_snaps=[_snap("a", 0.21, reset_at, observed_at=t_last)],
    )
    assert typ_live_corr["status"] == typ_live_clean["status"] == "ok"
    assert typ_live_corr["valid_seconds"] == typ_live_clean["valid_seconds"] == round(16 * H - 300.0)
    assert typ_live_corr["rate_per_hour"] == typ_live_clean["rate_per_hour"]
    s_lc = qs.compute(st_lc, store_lc.read(lambda s: {(qs.pseudo_id(s, "codex", "a"), key): NOW - 48 * H}, NOW), NOW)
    s_lcl = qs.compute(st_lcl, store_lcl.read(lambda s: {(qs.pseudo_id(s, "codex", "a"), key): NOW - 48 * H}, NOW), NOW)
    est_lc = qf.estimate_typical_pace(s_lc.groups[0], s_lc)
    est_lcl = qf.estimate_typical_pace(s_lcl.groups[0], s_lcl)
    assert est_lc == est_lcl
    assert math.isclose(est_lc["observed_burn"], 0.01, abs_tol=1e-6)
    assert math.isclose(est_lc["rate_per_hour"], 0.01 / (16.0 - 5 / 60), abs_tol=1e-6)

    # 1c. Gapped correction (persisted & live refresh) retracts 90% without bridging the gap.
    gapped_times = [t for t in times if not (t_corr < t < t_corr + 1800.0)]
    typ_gap, _, _ = _persist_typical(
        tmp_path / "p_gap",
        lambda _i, t: [_snap(
            "a",
            0.20 if t < t_corr - 1 else (0.90 if abs(t - t_corr) < 1 else 0.21),
            reset_at,
            observed_at=t_corr if abs(t - (t_corr + 1800.0)) < 1 else t,
        )],
        gapped_times,
    )
    assert typ_gap["status"] == "zero" and typ_gap["rate_per_hour"] == 0.0
    assert math.isclose(typ_gap["valid_hours"], 15.42, abs_tol=0.05)

    t_gap_last = NOW - 1800.0
    pre_gap_times = [t0 + k * 300.0 for k in range(15 * 12 + 7)]  # ends at NOW - 1800
    typ_live_gap, _, _ = _persist_typical(
        tmp_path / "live_gap",
        lambda _i, t: [_snap("a", 0.90 if abs(t - t_gap_last) < 1 else 0.20, reset_at, observed_at=t)],
        pre_gap_times,
        live_snaps=[_snap("a", 0.21, reset_at, observed_at=t_gap_last)],
    )
    assert typ_live_gap["status"] == "zero" and typ_live_gap["rate_per_hour"] == 0.0

    # Retracted 90% on s1 (even after a gap) neither counts burn nor vetoes clean s2 (20->21%).
    view = qs.HistoryView(state="ok", salt="a" * 32)
    dual_retract = [
        qs.Run("s1", 0.20, reset_at, PLAN, t0, t_corr - 300, 96, t0, t_corr - 300),
        qs.Run("s1", 0.90, reset_at, PLAN, t_corr, t_corr, 1, t_corr, t_corr),
        qs.Run("s1", 0.21, reset_at, PLAN, t_corr, NOW, 91, t_corr + 1800, NOW, after_gap=True),
        qs.Run("s2", 0.20, reset_at, PLAN, t0, t_corr - 300, 96, t0, t_corr - 300),
        qs.Run("s2", 0.21, reset_at, PLAN, t_corr, NOW, 97, t_corr, NOW),
    ]
    segs_dual = qf._account_segments(dual_retract, NOW - 48 * H, NOW, view, [])
    assert math.isclose(sum(v - u for u, v, _, _ in segs_dual), 16 * H, abs_tol=1.0)
    assert math.isclose(sum((v - u) * r for u, v, r, _ in segs_dual), 0.01, abs_tol=1e-5)

    # 1d. Unbroken correction with a plan change retracts 90% and does NOT bridge across plan change.
    typ_plan, _, _ = _persist_typical(
        tmp_path / "plan_corr",
        lambda _i, t: [_snap(
            "a",
            0.20 if t < t_corr - 1 else (0.90 if abs(t - t_corr) < 1 else 0.21),
            reset_at,
            observed_at=t_corr if abs(t - (t_corr + 300.0)) < 1 else t,
            plan="plus" if t <= t_corr + 1 else "pro",
        )],
        times,
    )
    assert typ_plan["status"] == "zero" and typ_plan["rate_per_hour"] == 0.0

    # 1e. Genuine 20->90% at t_corr followed by restore to 21% at LATER observed_at keeps 0.70 burn.
    typ_gen, _, _ = _persist_typical(
        tmp_path / "genuine",
        lambda _i, t: [_snap("a", 0.20 if t < t_corr - 1 else (0.90 if abs(t - t_corr) < 1 else 0.21), reset_at, observed_at=t)],
        times,
    )
    assert typ_gen["status"] == "ok"
    assert math.isclose(typ_gen["valid_hours"], 15 + 55 / 60, abs_tol=0.02)
    assert math.isclose(typ_gen["rate_per_hour"], 0.70 / (15 + 55 / 60), abs_tol=5e-4)

    # 1f. Multi-obs plateau whose terminal last_obs was corrected never extrapolates hidden timestamps.
    multi_end_corr = [
        qs.Run("app", 0.20, reset_at, PLAN, t0, t_corr, 97, t0, t_corr),
        qs.Run("app", 0.25, reset_at, PLAN, t_corr, NOW, 96, t_corr + 300, NOW, after_correction=True),
    ]
    segs_multi = qf._source_segments(multi_end_corr, NOW - 48 * H, NOW, view, [])
    assert len(segs_multi) == 1
    assert math.isclose(segs_multi[0][0], t_corr, abs_tol=1e-6)
    assert math.isclose(segs_multi[0][1], NOW, abs_tol=1e-6)
    assert segs_multi[0][2] == 0.0


def test_multisource_offset_bursty_and_frozen_cached_feeds(tmp_path):
    """Steady 0.4pp offset, decimal vs integer rounding, bursty 5m vs 30m, and frozen
    n_obs==1 cached feeds (including first sighted after old timestamp / outside watch)
    all count once without chord interpolation; real conflicts stay excluded."""
    reset_at = NOW + 48 * H
    t0 = NOW - 16 * H
    t_mid = NOW - 8 * H
    times = [t0 + k * 300.0 for k in range(16 * 12 + 1)]
    view = qs.HistoryView(state="ok", salt="a" * 32)

    # 1. Steady 0.4pp offset across 16h plateaus (20->25% vs 20.4->25.4%) -> counted once.
    typ_off, _, _ = _persist_typical(
        tmp_path / "offset",
        lambda _i, t: [
            _snap("a", 0.20 if t < t_mid else 0.25, reset_at, observed_at=t, source="s1"),
            _snap("a", (0.20 if t < t_mid else 0.25) + 0.004, reset_at, observed_at=t, source="s2"),
        ],
        times,
    )
    assert typ_off["status"] == "ok" and typ_off["valid_hours"] == 16.0
    assert math.isclose(typ_off["rate_per_hour"], 0.05 / 16.0, abs_tol=1e-4)

    # Decimal (+0.004/5m = 0.048/h) vs integer-percent rounded feed stays consistent.
    dec_runs = [qs.Run("dec", round(0.10 + 0.004 * k, 6), reset_at, PLAN, t, t, 1, t, t) for k, t in enumerate(times)]
    pct_runs = []
    for k, t in enumerate(times):
        u_int = round(0.10 + 0.004 * k, 2)
        if pct_runs and abs(pct_runs[-1].ratio - u_int) <= qs.RATIO_EPS:
            p = pct_runs[-1]
            pct_runs[-1] = qs.Run("pct", p.ratio, reset_at, PLAN, p.first_obs, t, p.n_obs + 1, p.first_seen, t)
        else:
            pct_runs.append(qs.Run("pct", u_int, reset_at, PLAN, t, t, 1, t, t))
    segs_dec = qf._account_segments(dec_runs + pct_runs, NOW - 48 * H, NOW, view, [])
    tot_h = sum(v - u for u, v, _, _ in segs_dec) / H
    assert math.isclose(tot_h, 16.0, abs_tol=1e-4)
    assert math.isclose(sum((v - u) * r for u, v, r, _ in segs_dec) / tot_h, 0.048, abs_tol=0.001)

    # Negative control: 1.5pp (> VALUE_TOLERANCE) offset across 16h plateaus -> excluded.
    assert qf._account_segments([
        qs.Run("s1", 0.200, reset_at, PLAN, t0, NOW, len(times), t0, NOW),
        qs.Run("s2", 0.215, reset_at, PLAN, t0, NOW, len(times), t0, NOW),
    ], NOW - 48 * H, NOW, view, []) == []

    # 2. Bursty 5m vs 30m agreeing (0.60 burn / 16h = 0.0375/h) counted once.
    burst_starts = {t0 + 2 * H, t0 + 7 * H, t0 + 12 * H}
    state_b = {"u": 0.10, "ru": 0.10, "robs": t0}

    def _burst_sweep(idx, t):
        if any(abs(t - (bs + 300.0)) < 1 for bs in burst_starts):
            state_b["u"] = round(state_b["u"] + 0.20, 6)
        if idx % 6 == 0:
            state_b["ru"], state_b["robs"] = state_b["u"], t
        return [
            _snap("a", state_b["u"], reset_at, observed_at=t, source="codex_app_server"),
            _snap("a", state_b["ru"], reset_at, observed_at=state_b["robs"], source="rollout_log"),
        ]

    typ_burst, _, _ = _persist_typical(tmp_path / "bursty", _burst_sweep, times)
    assert typ_burst["status"] == "ok" and typ_burst["valid_hours"] == 16.0
    assert math.isclose(typ_burst["rate_per_hour"], 0.60 / 16.0, abs_tol=1e-4)

    # Negative control: non-monotonic 5m jump (0.50) exceeding 30m endpoint (0.30) -> excluded.
    assert qf._account_segments([
        qs.Run("fast", 0.10, reset_at, PLAN, t0, t0, 1, t0, t0),
        qs.Run("fast", 0.50, reset_at, PLAN, t0 + 300, t0 + 1800, 6, t0 + 300, t0 + 1800),
        qs.Run("slow", 0.10, reset_at, PLAN, t0, t0, 1, t0, t0 + 1500),
        qs.Run("slow", 0.30, reset_at, PLAN, t0 + 1800, t0 + 1800, 1, t0 + 1800, t0 + 1800),
    ], NOW - 48 * H, NOW, view, []) == []

    # 3. Frozen n_obs==1 old cached feed (at t0 AND first sighted AFTER its old timestamp / outside watch).
    typ_frozen, _, _ = _persist_typical(
        tmp_path / "frozen_old",
        lambda _i, t: (
            [_snap("a", 0.10 + 0.03 * ((t - t0) / H), reset_at, observed_at=t, source="codex_app_server")]
            + ([_snap("a", 0.05, reset_at, observed_at=t0 - 3 * H, source="rollout_log")] if t >= t0 + 4 * H else [])
        ),
        times,
    )
    assert typ_frozen["status"] == "ok" and typ_frozen["valid_hours"] == 16.0
    assert math.isclose(typ_frozen["rate_per_hour"], 0.03, abs_tol=2e-4)

    app_runs = [
        qs.Run("codex_app_server", round(0.10 + 0.03 * ((t - t0) / H), 6), reset_at, PLAN, t, t, 1, t, t)
        for t in times
    ]
    # Cached at t0 (matching 0.10) first sighted 4h later -> does not veto fresh observations.
    segs_late_sight = qf._account_segments(
        app_runs + [qs.Run("rollout_log", 0.10, reset_at, PLAN, t0, t0, 1, t0 + 4 * H, NOW)],
        NOW - 48 * H, NOW, view, [],
    )
    assert math.isclose(sum(v - u for u, v, _, _ in segs_late_sight), 16 * H, abs_tol=1.0)
    # Negative controls: frozen reset conflict vetoes span; same-instant conflict vetoes touching step.
    assert qf._account_segments(
        app_runs + [qs.Run("rollout_log", 0.10, reset_at + 600.0, PLAN, t0, t0, 1, t0, NOW)],
        NOW - 48 * H, NOW, view, [],
    ) == []
    segs_bad_t0 = qf._account_segments(
        app_runs + [qs.Run("rollout_log", 0.50, reset_at, PLAN, t0, t0, 1, t0, NOW)],
        NOW - 48 * H, NOW, view, [],
    )
    assert segs_bad_t0 and segs_bad_t0[0][0] >= t0 + 300.0 - 1e-6
    assert math.isclose(sum(v - u for u, v, _, _ in segs_bad_t0), 16 * H - 300.0, abs_tol=1.0)


def test_live_refresh_attaches_all_fresh_sources_and_rejects_expired_or_junk(tmp_path):
    """On live refresh (now > last_sweep_at), all fresh recordable sources are attached via
    _with_live while expired, stale, or malformed readings remain rejected."""
    reset_at = NOW + 48 * H
    t0 = NOW - 16 * H
    pre_times = [t0 + k * 300.0 for k in range(16 * 12)]  # ends at NOW - 300s

    # 1. Both feeds report +0.15 at NOW -> counted once over 16h.
    typ_both, store, _ = _persist_typical(
        tmp_path / "refresh",
        lambda _i, t: [
            _snap("a", 0.10, reset_at, observed_at=t, source="codex_app_server"),
            _snap("a", 0.10, reset_at, observed_at=t, source="rollout_log"),
        ],
        pre_times,
        live_snaps=[
            _snap("a", 0.25, reset_at, observed_at=NOW, source="codex_app_server"),
            _snap("a", 0.25, reset_at, observed_at=NOW, source="rollout_log"),
        ],
    )
    assert typ_both["status"] == "ok" and typ_both["valid_hours"] == 16.0
    assert math.isclose(typ_both["rate_per_hour"], 0.15 / 16.0, abs_tol=1e-4)

    # 2. Only codex_app_server updated at NOW; rollout_log stays cached -> counted.
    profs = [profile("codex", "a")]
    rv_one = plugin.reserve_view(
        store,
        payload([
            _snap("a", 0.25, reset_at, observed_at=NOW, source="codex_app_server"),
            _snap("a", 0.10, reset_at, observed_at=NOW - 300.0, source="rollout_log"),
        ], profs, harnesses=("codex",)),
        NOW, NOW, harness="codex", horizon="7d", name_accounts=True,
    )
    typ_one = rv_one["chart"]["scenarios"]["typical"]
    assert typ_one["status"] == "ok" and typ_one["valid_hours"] == 16.0
    assert math.isclose(typ_one["rate_per_hour"], 0.15 / 16.0, abs_tol=1e-4)

    # 3. Expired (resets_at <= NOW) or malformed/stale live second feed is ignored cleanly.
    rv_junk = plugin.reserve_view(
        store,
        payload([
            _snap("a", 0.25, reset_at, observed_at=NOW, source="codex_app_server"),
            _snap("a", 0.90, NOW - 60.0, observed_at=NOW, source="expired_src"),
            _snap("a", 1.50, reset_at, observed_at=NOW, source="junk_src"),
            _snap("a", 0.90, reset_at, observed_at=NOW, source="stale_src", freshness="stale"),
        ], profs, harnesses=("codex",)),
        NOW, NOW, harness="codex", horizon="7d", name_accounts=True,
    )
    typ_junk = rv_junk["chart"]["scenarios"]["typical"]
    assert typ_junk["status"] == "ok" and typ_junk["valid_hours"] == 16.0
    assert math.isclose(typ_junk["rate_per_hour"], 0.15 / 16.0, abs_tol=1e-4)

    # 4. Real same-instant live conflict at NOW (0.25 vs 0.10) excludes only the live tail.
    live_conflict = payload([
        _snap("a", 0.25, reset_at, observed_at=NOW, source="codex_app_server"),
        _snap("a", 0.10, reset_at, observed_at=NOW, source="rollout_log"),
    ], profs, harnesses=("codex",))
    key = qs.group_key("codex", "primary", WEEK, ())
    st_conf = qs.compute(live_conflict, store.read(
        lambda salt: {(qs.pseudo_id(salt, "codex", "a"), key): NOW - 48 * H}, NOW,
    ), NOW)
    sub = qs.pseudo_id(st_conf.view.salt, "codex", "a")
    by_s = {}
    for r in st_conf.view.runs[(sub, key)]:
        by_s.setdefault(r.source, []).append(r)
    for rd in qs.recordable(st_conf.norm, NOW):
        by_s[rd.source] = qs._with_live(by_s.get(rd.source, []), rd, NOW, st_conf.view)
    segs_conf = qf._account_segments([r for rs in by_s.values() for r in rs], NOW - 48 * H, NOW, st_conf.view, [])
    assert math.isclose(sum(v - u for u, v, _, _ in segs_conf), 16 * H - 300.0, abs_tol=1.0)
    assert math.isclose(sum((v - u) * r for u, v, r, _ in segs_conf), 0.0, abs_tol=1e-6)


def test_multisource_conflicts_three_source_no_majority_and_reset_rollover():
    """Staggered incompatible feeds, exact plateau conflict carving vs standalone point
    veto (no artificial padding), 3-source no-majority exclusion, and exact reset rollover."""
    reset_at = NOW + 48 * H
    view = qs.HistoryView(state="ok", salt="a" * 32)
    t0 = NOW - 16 * H

    # 1. Staggered incompatible transitions -> excluded.
    assert qf._account_segments([
        qs.Run("s1", 0.10, reset_at, PLAN, NOW - 900, NOW - 900, 1, NOW - 900, NOW - 900),
        qs.Run("s1", 0.50, reset_at, PLAN, NOW - 300, NOW - 300, 1, NOW - 300, NOW - 300),
        qs.Run("s2", 0.60, reset_at, PLAN, NOW - 600, NOW - 600, 1, NOW - 600, NOW - 600),
        qs.Run("s2", 0.70, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
    ], NOW - 48 * H, NOW, view, []) == []

    # 2. Brief 1h conflicting segment carves out exact 1h (15h kept); standalone point drops plateau.
    plat_16h = qs.Run("s1", 0.20, reset_at, PLAN, t0, NOW, 193, t0, NOW)
    segs_brief = qf._account_segments([
        plat_16h,
        qs.Run("s2", 0.50, reset_at, PLAN, t0 + 7 * H, t0 + 8 * H, 13, t0 + 7 * H, t0 + 8 * H),
    ], NOW - 48 * H, NOW, view, [])
    assert math.isclose(sum(v - u for u, v, _, _ in segs_brief), 15 * H, abs_tol=1.0)
    assert qf._account_segments([
        plat_16h,
        qs.Run("s2", 0.50, reset_at, PLAN, t0 + 8 * H, t0 + 8 * H, 1, t0 + 8 * H, t0 + 8 * H),
    ], NOW - 48 * H, NOW, view, []) == []

    # 3. Three sources (2 agree, 1 disagrees) -> no majority vote.
    assert qf._account_segments([
        qs.Run("s1", 0.20, reset_at, PLAN, NOW - 300, NOW - 300, 1, NOW - 300, NOW - 300),
        qs.Run("s1", 0.30, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s2", 0.20, reset_at, PLAN, NOW - 300, NOW - 300, 1, NOW - 300, NOW - 300),
        qs.Run("s2", 0.30, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s3", 0.20, reset_at, PLAN, NOW - 300, NOW - 300, 1, NOW - 300, NOW - 300),
        qs.Run("s3", 0.60, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
    ], NOW - 48 * H, NOW, view, []) == []

    # 4. Exact reset rollover: ended cycle at r_mid never vetoes running cycle starting at r_mid.
    r_mid = NOW - 8 * H
    segs_roll = qf._account_segments([
        qs.Run("s1", 0.50, r_mid, PLAN, t0, r_mid, 97, t0, r_mid),
        qs.Run("s2", 0.00, r_mid + WEEK, PLAN, r_mid, NOW, 97, r_mid, NOW),
    ], NOW - 48 * H, NOW, view, [])
    assert math.isclose(sum(v - u for u, v, _, _ in segs_roll), 16 * H, abs_tol=1.0)


def test_exhaustion_censoring_and_mixed_below_threshold_reason(tmp_path):
    """Full-pool 100% hours are excluded from valid_hours and rate denominator; mixed
    positive reserve hours below 12h return insufficient/few_hours (not all_observed_at_limit)."""
    reset_at = NOW + 48 * H
    t_start = NOW - 48 * H
    times_48 = [t_start + k * 300.0 for k in range(48 * 12 + 1)]

    # 1. Mixed 28h @ 0.05/h (across a reset) + 20h @ 100% -> 0.05/h over 28h (no dilution).
    reset_mid = t_start + 20 * H
    typ_mix, _, _ = _persist_typical(
        tmp_path / "mix_28_20",
        lambda _i, t: [_snap(
            "a",
            min(1.0, 0.20 + 0.05 * ((t - t_start) / H)) if t < reset_mid else min(1.0, 0.40 + 0.05 * ((t - reset_mid) / H)),
            reset_mid if t < reset_mid else reset_at,
            observed_at=t,
        )],
        times_48,
    )
    assert typ_mix["status"] == "ok"
    assert math.isclose(typ_mix["valid_hours"], 28.0, abs_tol=0.1)
    assert math.isclose(typ_mix["rate_per_hour"], 0.05, abs_tol=5e-4)

    # 2. Mostly exhausted: 40h @ 100% + 8h @ 0.10/h (2 accts, covered=48h >= 12h, usable=8h < 12h) ->
    #    insufficient / few_hours reporting 8.0h and no rate (never misleading all_observed_at_limit).
    reset_8h = NOW - 8 * H
    typ_mostly, _, _ = _persist_typical(
        tmp_path / "mostly_40_8",
        lambda _i, t: [
            _snap("a", 1.0 if t < reset_8h else min(1.0, 0.05 * ((t - reset_8h) / H)), reset_8h if t < reset_8h else reset_at, observed_at=t),
            _snap("b", 1.0 if t < reset_8h else min(1.0, 0.05 * ((t - reset_8h) / H)), reset_8h if t < reset_8h else reset_at, observed_at=t),
        ],
        times_48,
        sids=("a", "b"),
    )
    assert typ_mostly["status"] == "insufficient"
    assert typ_mostly["reason"] == "few_hours"
    assert typ_mostly["rate_per_hour"] is None
    assert math.isclose(typ_mostly["valid_hours"], 8.0, abs_tol=0.1)
    assert typ_mostly["line"] == []

    # 3. 20h @ 100% + 16h genuinely quiet with capacity -> state="zero", valid_hours=16.0.
    r_split = NOW - 16 * H
    calc_q, state_q = _state_from_runs({
        "a": [
            qs.Run("app", 1.0, r_split, PLAN, NOW - 36 * H, r_split - 60, 240, NOW - 36 * H, r_split - 60),
            qs.Run("app", 0.30, reset_at, PLAN, r_split, NOW, 193, r_split, NOW),
        ],
    }, [("a", 0.30, NOW, reset_at, "app")])
    res_q = qf.estimate_typical_pace(calc_q, state_q)
    assert res_q["state"] == "zero" and res_q["rate_per_hour"] == 0.0 and res_q["valid_hours"] == 16.0

    # 4. Partial exhaustion (a at 100%, b burning 0.04/h for 16h) -> pool has capacity, 16h @ 0.04/h.
    t16 = NOW - 16 * H
    calc_p, state_p = _state_from_runs({
        "a": [qs.Run("app", 1.0, reset_at, PLAN, t16, NOW, 193, t16, NOW)],
        "b": [
            qs.Run("app", round(0.10 + 0.04 * (k / 12.0), 6), reset_at, PLAN,
                   t16 + k * 300.0, t16 + k * 300.0, 1, t16 + k * 300.0, t16 + k * 300.0)
            for k in range(16 * 12 + 1)
        ],
    }, [("a", 1.0, NOW, reset_at, "app"), ("b", 0.74, NOW, reset_at, "app")])
    res_p = qf.estimate_typical_pace(calc_p, state_p)
    assert res_p["state"] == "ok" and res_p["valid_hours"] == 16.0
    assert math.isclose(res_p["rate_per_hour"], 0.04, abs_tol=2e-4)

    # 5. Fewer than 12h covered overall (6h@100% + 4h@0.10/h) -> warming_up/few_hours, valid_hours=4.0.
    r_4h = NOW - 4 * H
    calc_s, state_s = _state_from_runs({
        "a": [qs.Run("app", 1.0, r_4h, PLAN, NOW - 10 * H, r_4h - 60, 72, NOW - 10 * H, r_4h - 60)] + [
            qs.Run("app", round(0.10 * (k / 12.0), 6), reset_at, PLAN,
                   r_4h + k * 300.0, r_4h + k * 300.0, 1, r_4h + k * 300.0, r_4h + k * 300.0)
            for k in range(4 * 12 + 1)
        ],
    }, [("a", 0.40, NOW, reset_at, "app")], collecting_since=NOW - 10 * H)
    res_s = qf.estimate_typical_pace(calc_s, state_s)
    assert res_s["state"] == "warming_up" and res_s["reason"] == "few_hours" and res_s["valid_hours"] == 4.0


def test_eighty_percent_coverage_does_not_extrapolate_over_missing_account_gap():
    """Under 80% coverage (4 of 5 accounts), an unread 5th account's reading gap is neither
    bridged nor extrapolated: only observed comparable segments contribute burn."""
    reset_at = NOW + 48 * H
    t0 = NOW - 24 * H
    sweeps = [t0 + k * 300.0 for k in range(24 * 12 + 1)]
    by_sub, measured = {}, []
    for i in range(4):
        sid = f"acct-{i}"
        runs = [qs.Run("app", round(0.10 + 0.01 * (k / 12.0), 6), reset_at, PLAN, t, t, 1, t, t) for k, t in enumerate(sweeps)]
        by_sub[sid] = runs
        measured.append((sid, runs[-1].ratio, runs[-1].last_obs, reset_at, "app"))
    by_sub["acct-4"] = [
        qs.Run("app", 0.10, reset_at, PLAN, t0, t0 + 9 * H, 109, t0, t0 + 9 * H),
        qs.Run("app", 0.22, reset_at, PLAN, t0 + 15 * H, NOW, 109, t0 + 15 * H, NOW, after_gap=True),
    ]
    measured.append(("acct-4", 0.22, NOW, reset_at, "app"))

    calc, state = _state_from_runs(by_sub, measured, sweeps=sweeps, sweep_ok=[True] * len(sweeps))
    res = qf.estimate_typical_pace(calc, state)
    assert res["state"] == "ok" and res["valid_hours"] == 24.0
    assert math.isclose(res["observed_burn"], 0.96, abs_tol=1e-4)
    assert math.isclose(res["rate_per_hour"], 0.04, abs_tol=2e-4)


def test_late_sighted_differing_stale_cache_does_not_veto_watched_series(tmp_path):
    """A late-sighted n_obs==1 cached reading whose observed_at precedes that source's
    unbroken watch must not veto a continuous series even when its cached ratio differs;
    reset conflicts over its sighting span and fresh in-watch conflicts remain enforced."""
    reset_at = NOW + 48 * H
    t0 = NOW - 16 * H
    t_jump = t0 + 1 * H
    t_late = t0 + 12 * H
    times = [t0 + k * 300.0 for k in range(16 * 12 + 1)]
    key = qs.group_key("codex", "primary", WEEK, ())

    # 1. Exact repro: app goes 0.10->0.25 at hour 1 and stays 0.25; second source first
    #    appears at hour 12 with n_obs=1, obs_at=hour 1, ratio=0.10, same reset -> 0.15/16h.
    typ, store, st = _persist_typical(
        tmp_path / "late_diff_same_reset",
        lambda _i, t: (
            [_snap("a", 0.10 if t < t_jump - 1 else 0.25, reset_at, observed_at=t, source="app")]
            + ([_snap("a", 0.10, reset_at, observed_at=t_jump, source="rollout_log")] if t >= t_late - 1 else [])
        ),
        times,
    )
    assert typ["status"] == "ok" and typ["valid_hours"] == 16.0
    assert math.isclose(typ["rate_per_hour"], 0.15 / 16.0, abs_tol=1e-4)
    state = qs.compute(st, store.read(lambda s: {(qs.pseudo_id(s, "codex", "a"), key): NOW - 48 * H}, NOW), NOW)
    est = qf.estimate_typical_pace(state.groups[0], state)
    assert est["state"] == "ok" and est["valid_hours"] == 16.0
    assert math.isclose(est["observed_burn"], 0.15, abs_tol=1e-6)
    assert math.isclose(est["rate_per_hour"], 0.15 / 16.0, abs_tol=1e-6)

    # 2. Same late-sighted stale cache with a DIFFERENT reported reset still conflicts on
    #    its sighting span [hour 12, hour 16], leaving the clean 12h + 0.15 burn intact.
    app_runs = [
        qs.Run("app", 0.10, reset_at, PLAN, t0, t_jump - 300.0, 12, t0, t_jump - 300.0),
        qs.Run("app", 0.25, reset_at, PLAN, t_jump, NOW, 181, t_jump, NOW),
    ]
    view = qs.HistoryView(state="ok", salt="a" * 32)
    segs_bad_reset = qf._account_segments(
        app_runs + [qs.Run("rollout_log", 0.10, reset_at + 600.0, PLAN, t_jump, t_jump, 1, t_late, NOW)],
        NOW - 48 * H, NOW, view, [],
    )
    assert math.isclose(sum(v - u for u, v, _, _ in segs_bad_reset), 12 * H, abs_tol=1.0)
    assert math.isclose(sum((v - u) * r for u, v, r, _ in segs_bad_reset), 0.15, abs_tol=1e-6)

    # 3. Fresh in-watch same-time endpoint conflict (observed 10s before sighting) still vetoes.
    segs_fresh_conflict = qf._account_segments(
        app_runs + [qs.Run("rollout_log", 0.10, reset_at, PLAN, t_jump, t_jump, 1, t_jump + 10.0, NOW)],
        NOW - 48 * H, NOW, view, [],
    )
    assert math.isclose(sum(v - u for u, v, _, _ in segs_fresh_conflict), 55 * 60.0, abs_tol=1.0)
    assert math.isclose(sum((v - u) * r for u, v, r, _ in segs_fresh_conflict), 0.0, abs_tol=1e-6)

    # 4. Multi-obs run whose first_obs is pre-watch and last_obs is in-watch ignores pre-watch
    #    first_obs (no fabricated timestamp) while still enforcing the in-watch conflict.
    segs_reconf = qf._account_segments(
        app_runs + [qs.Run("rollout_log", 0.10, reset_at, PLAN, t_jump, t_late + 300.0, 2, t_late, t_late + 300.0)],
        NOW - 48 * H, NOW, view, [],
    )
    assert math.isclose(sum(v - u for u, v, _, _ in segs_reconf), 16 * H - 300.0, abs_tol=1.0)
    assert math.isclose(sum((v - u) * r for u, v, r, _ in segs_reconf), 0.15, abs_tol=1e-6)


def test_three_source_reset_conflict_cannot_be_masked_by_resetless_source(tmp_path):
    """Account-wide known reset-date conflicts between two sources (> RESET_TOLERANCE_SEC)
    must exclude segments from a 3rd source lacking resets_at, while preserving clean
    controls (single reset + resetless, within-tolerance jitter, rollover, retracted reset)."""
    reset_at = NOW + 48 * H
    t0 = NOW - 16 * H
    times = [t0 + k * 300.0 for k in range(16 * 12 + 1)]
    view = qs.HistoryView(state="ok", salt="a" * 32, sweeps=times, sweep_ok=[True] * len(times))

    # 1a. Direct _account_segments: s1=R, s2=R+1h, s3=None over full 16h -> 0 segments.
    conflict_runs = [
        run
        for t in times
        for r in (round(0.10 + 0.02 * ((t - t0) / H), 6),)
        for run in (
            qs.Run("s1", r, reset_at, PLAN, t, t, 1, t, t),
            qs.Run("s2", r, reset_at + H, PLAN, t, t, 1, t, t),
            qs.Run("s3", r, None, PLAN, t, t, 1, t, t),
        )
    ]
    assert qf._account_segments(conflict_runs, NOW - 48 * H, NOW, view, []) == []

    # 1b. End-to-end through HistoryStore + reserve_view: s1=R, s2=R+1h, s3=None across
    #     historical sweeps (clean live reading at NOW so account is currently measured) ->
    #     s3 cannot mask the 16h conflict; Typical never emits a rate.
    typ_conf, _, _ = _persist_typical(
        tmp_path / "three_src_conflict",
        lambda _i, t: (
            [
                _snap("a", 0.10 + 0.02 * ((t - t0) / H), reset_at, observed_at=t, source="s1"),
                _snap("a", 0.10 + 0.02 * ((t - t0) / H), None, observed_at=t, source="s3"),
            ]
            + ([_snap("a", 0.10 + 0.02 * ((t - t0) / H), reset_at + H, observed_at=t, source="s2")]
               if t < NOW - 1e-6 else [])
        ),
        times,
    )
    assert typ_conf["status"] in ("warming_up", "insufficient")
    assert typ_conf["rate_per_hour"] is None
    assert typ_conf["valid_hours"] < 0.2
    assert typ_conf["line"] == []

    # 2. Partial-interval reset conflict [NOW-10h, NOW-6h] carves only those 4h from s3's 20h plateau.
    segs_partial = qf._account_segments([
        qs.Run("s3", 0.20, None, PLAN, NOW - 20 * H, NOW, 241, NOW - 20 * H, NOW),
        qs.Run("s1", 0.20, reset_at, PLAN, NOW - 10 * H, NOW - 6 * H, 49, NOW - 10 * H, NOW - 6 * H),
        qs.Run("s2", 0.20, reset_at + H, PLAN, NOW - 10 * H, NOW - 6 * H, 49, NOW - 10 * H, NOW - 6 * H),
    ], NOW - 48 * H, NOW, view, [])
    assert len(segs_partial) == 2
    assert math.isclose(sum(v - u for u, v, _, _ in segs_partial), 16 * H, abs_tol=1.0)

    # 3. Single-sweep reset conflict at NOW drops touching plateau/leg on s3=None,
    #    while leaving an earlier non-touching clean plateau intact.
    assert qf._account_segments([
        qs.Run("s3", 0.20, None, PLAN, t0, NOW, 193, t0, NOW),
        qs.Run("s1", 0.20, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s2", 0.20, reset_at + H, PLAN, NOW, NOW, 1, NOW, NOW),
    ], NOW - 48 * H, NOW, view, []) == []
    segs_tail_only = qf._account_segments([
        qs.Run("s3", 0.20, None, PLAN, t0, NOW - 300.0, 192, t0, NOW - 300.0),
        qs.Run("s3", 0.30, None, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s1", 0.30, reset_at, PLAN, NOW, NOW, 1, NOW, NOW),
        qs.Run("s2", 0.30, reset_at + H, PLAN, NOW, NOW, 1, NOW, NOW),
    ], NOW - 48 * H, NOW, view, [])
    assert math.isclose(sum(v - u for u, v, _, _ in segs_tail_only), 16 * H - 300.0, abs_tol=1.0)
    assert math.isclose(sum((v - u) * r for u, v, r, _ in segs_tail_only), 0.0, abs_tol=1e-6)

    # 4a. Paired clean control: s1=R + s3=None -> preserved (0.02/h over 16h).
    typ_pair, _, _ = _persist_typical(
        tmp_path / "pair_known_and_none",
        lambda _i, t: [
            _snap("a", 0.10 + 0.02 * ((t - t0) / H), reset_at, observed_at=t, source="s1"),
            _snap("a", 0.10 + 0.02 * ((t - t0) / H), None, observed_at=t, source="s3"),
        ],
        times,
    )
    assert typ_pair["status"] == "ok" and typ_pair["valid_hours"] == 16.0
    assert math.isclose(typ_pair["rate_per_hour"], 0.02, abs_tol=2e-4)

    # 4b. Paired clean control: s1=R + s2=R+60s (within 120s tolerance) + s3=None -> preserved.
    typ_jitter, _, _ = _persist_typical(
        tmp_path / "three_src_jitter_ok",
        lambda _i, t: [
            _snap("a", 0.10 + 0.02 * ((t - t0) / H), reset_at, observed_at=t, source="s1"),
            _snap("a", 0.10 + 0.02 * ((t - t0) / H), reset_at + 60.0, observed_at=t, source="s2"),
            _snap("a", 0.10 + 0.02 * ((t - t0) / H), None, observed_at=t, source="s3"),
        ],
        times,
    )
    assert typ_jitter["status"] == "ok" and typ_jitter["valid_hours"] == 16.0
    assert math.isclose(typ_jitter["rate_per_hour"], 0.02, abs_tol=2e-4)

    # 4c. Paired clean control: exact reset rollover at r_mid with s3=None -> no false conflict.
    r_mid = NOW - 8 * H
    segs_roll = qf._account_segments([
        qs.Run("s1", 0.40, r_mid, PLAN, t0, r_mid, 97, t0, r_mid),
        qs.Run("s2", 0.00, r_mid + WEEK, PLAN, r_mid, NOW, 97, r_mid, NOW),
        qs.Run("s3", 0.40, None, PLAN, t0, r_mid, 97, t0, r_mid),
        qs.Run("s3", 0.00, None, PLAN, r_mid, NOW, 97, r_mid, NOW),
    ], NOW - 48 * H, NOW, view, [])
    assert math.isclose(sum(v - u for u, v, _, _ in segs_roll), 16 * H, abs_tol=1.0)

    # 4d. Paired clean control: retracted wrong reset on s2 does not trigger a false conflict.
    segs_retracted = qf._account_segments([
        qs.Run("s1", 0.20, reset_at, PLAN, t0, NOW, 193, t0, NOW),
        qs.Run("s2", 0.20, reset_at + H, PLAN, NOW - 8 * H, NOW - 8 * H, 1, NOW - 8 * H, NOW - 8 * H),
        qs.Run("s2", 0.20, reset_at, PLAN, NOW - 8 * H, NOW - 8 * H, 1, NOW - 8 * H + 120.0, NOW - 8 * H + 120.0, after_correction=True),
        qs.Run("s3", 0.20, None, PLAN, t0, NOW, 193, t0, NOW),
    ], NOW - 48 * H, NOW, view, [])
    assert math.isclose(sum(v - u for u, v, _, _ in segs_retracted), 16 * H, abs_tol=1.0)


def test_horizon_switch_preserves_48h_typical_under_row_cap_and_discloses_48h_truncation(tmp_path, monkeypatch):
    """Under a bounded row cap, old chart/calibration history and former accounts must not
    crowd out the fresh 48h Typical window (24h and 7d agree identically with uncapped
    history), while truncation of the fresh 48h window itself returns unavailable/history_truncated."""
    store = quota_history.HistoryStore(tmp_path / "pool")
    key_7d = qs.group_key("codex", "primary", WEEK, ())
    key_5h = qs.group_key("codex", "secondary", FIVE_H, ())
    profs = [profile("codex", f"cur-{a}", plan="plus") for a in range(8)]

    def _dual_snap(sid, r7, res7, r5, res5, observed_at):
        return {
            "subject": {"harness": "codex", "subject_id": sid, "plan_label": "plus"},
            "constraints": [
                {"id": "primary", "label": "Weekly", "used_ratio": round(r7, 6),
                 "window_seconds": WEEK, "resets_at": qs.iso(res7)},
                {"id": "secondary", "label": "5h", "used_ratio": round(r5, 6),
                 "window_seconds": FIVE_H, "resets_at": qs.iso(res5)},
            ],
            "availability": {"state": "available"},
            "observed_at": qs.iso(observed_at),
            "freshness": "fresh",
            "source": "app",
        }

    # Initialize valid schema + salt via the real writer, then populate 7d history for
    # 8 current accounts + 14 former accounts (6-min unbroken sweeps: ~3.85k fresh 48h rows,
    # ~24k total rows across 7d).
    t_origin = NOW - 7 * 86400.0
    st_init = payload([_dual_snap("cur-0", 0.10, NOW + 3 * 86400.0, 0.10, NOW + 2 * H, t_origin)],
                      profs, harnesses=("codex",))
    plugin.persist_sweep(store, st_init, "", t_origin)

    conn = sqlite3.connect(str(store.path))
    try:
        salt = conn.execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
        conn.execute("DELETE FROM run")
        conn.execute("DELETE FROM sweep")
        step_sweep = 360.0  # 6m <= SIGHTING_GAP_SEC (420s): unbroken watch across 7d
        n_sweeps = int(7 * 86400.0 / step_sweep)
        conn.executemany(
            "INSERT INTO sweep (at, ok, reason) VALUES (?, 1, 'ok')",
            [(t_origin + i * step_sweep,) for i in range(n_sweeps + 1)],
        )
        run_rows = []
        # 8 current accounts: 0.005/h each (0.04/h pooled) in -48h..-16h,
        # 0.025/h each (0.20/h pooled) in -16h..0h -> exact 48h pooled rate = 4.48 / 48.
        for a in range(8):
            sub = qs.pseudo_id(salt, "codex", f"cur-{a}")
            for i in range(n_sweeps + 1):
                t = t_origin + i * step_sweep
                age_h = (NOW - t) / H
                if age_h > 48.0:
                    ratio = 0.05 + ((i % 120) * 0.002)
                    res = NOW - 48 * H
                elif age_h > 16.0:
                    ratio = 0.10 + (48.0 - age_h) * 0.005
                    res = NOW + 3 * 86400.0
                else:
                    ratio = 0.26 + (16.0 - age_h) * 0.025
                    res = NOW + 3 * 86400.0
                run_rows.append((sub, key_7d, "app", PLAN, round(ratio, 6), res, t, t, 1, t, t, 0, 0))
                if i % 4 == 0:
                    run_rows.append((sub, key_5h, "app", PLAN, round(0.10 + (i % 10) * 0.01, 6),
                                     NOW + 2 * H, t, t, 1, t, t, 0, 0))
        # 14 former accounts active only in -7d..-50h (14 * 590 = 8260 older rows).
        former_end = int((7 * 86400.0 - 50 * H) / step_sweep)
        for f in range(14):
            sub = qs.pseudo_id(salt, "codex", f"former-{f}")
            for i in range(0, former_end, 2):
                t = t_origin + i * step_sweep
                ratio = 0.10 + ((i % 90) * 0.003)
                run_rows.append((sub, key_7d, "app", PLAN, round(ratio, 6), NOW - 48 * H, t, t, 1, t, t, 0, 0))
        conn.executemany(
            "INSERT INTO run (subject, series, source, plan, ratio, resets_at, "
            "first_obs, last_obs, n_obs, first_seen, last_seen, after_gap, after_correction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            run_rows,
        )
        conn.commit()
    finally:
        conn.close()

    live_snaps = [_dual_snap(f"cur-{a}", 0.66, NOW + 3 * 86400.0, 0.15, NOW + 2 * H, NOW) for a in range(8)]
    status = payload(live_snaps, profs, harnesses=("codex",))
    expected_rate = round(4.48 / 48.0, 4)

    # 1. Uncapped normal-history control: 24h and 7d both produce full 48h rate, not truncated.
    ctrl_24 = plugin.reserve_view(store, status, NOW, NOW, harness="codex", group=key_7d, horizon="24h")
    ctrl_7d = plugin.reserve_view(store, status, NOW, NOW, harness="codex", group=key_7d, horizon="7d")
    assert ctrl_24["summary"]["history"]["truncated"] is False
    assert ctrl_7d["summary"]["history"]["truncated"] is False
    assert ctrl_24["chart"]["scenarios"]["typical"]["rate_per_hour"] == expected_rate
    assert ctrl_7d["chart"]["scenarios"]["typical"]["rate_per_hour"] == expected_rate
    assert ctrl_24["chart"]["scenarios"]["typical"]["valid_hours"] == 48.0
    assert ctrl_7d["chart"]["scenarios"]["typical"]["valid_hours"] == 48.0

    # 2. Tight cap (6000 rows: fits ~3900 fresh Pass-1 rows, truncates ~24k 7d rows):
    #    24h and 7d agree identically with the uncapped control (both analytics=False and True),
    #    while 7d discloses truncation and store.read strictly respects the cap.
    monkeypatch.setattr(quota_history, "MAX_READ_ROWS", 6000)
    for analytics in (False, True):
        rv_24 = plugin.reserve_view(store, status, NOW, NOW, harness="codex", group=key_7d, horizon="24h", analytics=analytics)
        rv_7d = plugin.reserve_view(store, status, NOW, NOW, harness="codex", group=key_7d, horizon="7d", analytics=analytics)
        t24 = rv_24["chart"]["scenarios"]["typical"]
        t7d = rv_7d["chart"]["scenarios"]["typical"]
        assert t24["status"] == t7d["status"] == "ok"
        assert t24["rate_per_hour"] == t7d["rate_per_hour"] == expected_rate
        assert t24["valid_hours"] == t7d["valid_hours"] == 48.0
        assert t24["valid_seconds"] == t7d["valid_seconds"]
        assert rv_7d["summary"]["history"]["truncated"] is True
        if analytics:
            a24 = rv_24["chart"]["analytics"]["window"]["scenarios"]["typical"]
            a7d = rv_7d["chart"]["analytics"]["window"]["scenarios"]["typical"]
            assert a24["rate_per_hour"] == a7d["rate_per_hour"] == expected_rate
            assert a24["valid_hours"] == a7d["valid_hours"] == 48.0
    bounded_view = store.read(
        lambda s: {(qs.pseudo_id(s, "codex", f"cur-{a}"), key_7d): qs.chart_history_since(NOW, "7d") for a in range(8)},
        NOW,
        chart_series=(key_7d, qs.chart_history_since(NOW, "7d")),
    )
    assert sum(len(rs) for rs in bounded_view.runs.values()) <= 6000
    assert bounded_view.truncated is True
    assert bounded_view.truncated_at[key_7d] < qs.typical_history_since(NOW)

    # 3. Fresh 48h overflow (1500 rows < ~3850 fresh 48h rows):
    #    Must NOT emit a biased partial-window rate; returns unavailable/history_truncated.
    monkeypatch.setattr(quota_history, "MAX_READ_ROWS", 1500)
    for horizon in ("24h", "7d"):
        rv_over = plugin.reserve_view(store, status, NOW, NOW, harness="codex", group=key_7d, horizon=horizon, analytics=True)
        typ_over = rv_over["chart"]["scenarios"]["typical"]
        an_over = rv_over["chart"]["analytics"]["window"]["scenarios"]["typical"]
        assert rv_over["summary"]["history"]["truncated"] is True
        for sc in (typ_over, an_over):
            assert sc["status"] == "unavailable"
            assert sc["reason"] == "history_truncated"
            assert sc["verdict"]["state"] == "unavailable"
            assert sc["verdict"]["reason"] == "history_truncated"
            assert sc["rate_per_hour"] is None
            assert sc["line"] == [] and sc["checkpoints"] == [] and sc["schedule"] == []

    # 4. Dropped long plateau spanning into 48h (first_seen < -48h, last_seen = NOW) is caught
    #    by exact omitted max(last_seen) and disclosed as unavailable/history_truncated.
    store_plat = quota_history.HistoryStore(tmp_path / "long_plateau")
    st_p0 = payload([_snap("a", 0.20, NOW + 3 * 86400.0, observed_at=NOW - 60 * H)],
                    [profile("codex", "a")], harnesses=("codex",))
    plugin.persist_sweep(store_plat, st_p0, "", NOW - 60 * H)
    conn_p = sqlite3.connect(str(store_plat.path))
    try:
        salt_p = conn_p.execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
        sub_a = qs.pseudo_id(salt_p, "codex", "a")
        conn_p.execute("DELETE FROM run")
        conn_p.execute("DELETE FROM sweep")
        conn_p.executemany(
            "INSERT INTO sweep (at, ok, reason) VALUES (?, 1, 'ok')",
            [(NOW - 60 * H + k * 300.0,) for k in range(60 * 12 + 1)],
        )
        conn_p.execute(
            "INSERT INTO run (subject, series, source, plan, ratio, resets_at, "
            "first_obs, last_obs, n_obs, first_seen, last_seen, after_gap, after_correction) "
            "VALUES (?, ?, 's_long', ?, 0.20, ?, ?, ?, 100, ?, ?, 0, 0)",
            (sub_a, key_7d, PLAN, NOW + 3 * 86400.0, NOW - 60 * H, NOW, NOW - 60 * H, NOW),
        )
        for k in range(20):
            t = NOW - (19 - k) * H
            conn_p.execute(
                "INSERT INTO run (subject, series, source, plan, ratio, resets_at, "
                "first_obs, last_obs, n_obs, first_seen, last_seen, after_gap, after_correction) "
                "VALUES (?, ?, 'app', ?, ?, ?, ?, ?, 1, ?, ?, 0, 0)",
                (sub_a, key_7d, PLAN, round(0.20 + 0.01 * k, 6), NOW + 3 * 86400.0, t, t, t, t),
            )
        conn_p.commit()
    finally:
        conn_p.close()
    monkeypatch.setattr(quota_history, "MAX_READ_ROWS", 20)
    st_plat = payload([_snap("a", 0.39, NOW + 3 * 86400.0, observed_at=NOW)],
                      [profile("codex", "a")], harnesses=("codex",))
    rv_plat = plugin.reserve_view(store_plat, st_plat, NOW, NOW, harness="codex", group=key_7d, horizon="7d")
    typ_plat = rv_plat["chart"]["scenarios"]["typical"]
    assert rv_plat["summary"]["history"]["truncated"] is True
    assert typ_plat["status"] == "unavailable"
    assert typ_plat["reason"] == "history_truncated"
    assert typ_plat["rate_per_hour"] is None
    assert typ_plat["line"] == []
