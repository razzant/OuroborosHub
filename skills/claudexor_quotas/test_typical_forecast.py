"""48-hour pooled Typical forecast (total observed pool burn / valid measured time),
No new use, binding-window gaps, honest unknown/Combined states, and real-browser tests.

Every fixture here is synthetic — no real accounts, identifiers, or external databases.
What is held here:
- Pooled demand persists when one account hits zero instead of decaying per-account; one
  unified simulation (`quota_forecast.simulate_pool`) produces the chart line, verdict,
  checkpoints, schedule, table, and runs_out identically.
- Estimator uses total observed comparable pool burn over the preceding 48h divided by
  valid measured pool time (NOT median):
  (a) rare positive burst across many zero hours yields a positive typical rate;
  (b) known measured zero with sufficient coverage yields `status == "zero"` and draws a line,
      whereas a frozen single observation (`n_obs == 1`) stays `insufficient`;
  (c) 1-of-22 current-only pool stays `insufficient` (never falsely `zero_pace`), while
      1-4 fully visible accounts work cleanly without an artificial min-5 floor;
  (d) unknown Typical (`insufficient`/`warming_up`/`unavailable`/`held`) emits NO numeric
      `No new use` curve, checkpoints, schedule, or table values under Typical's name.
- Data hygiene: reset jitter (<= 120s) is preserved, real resets, ratio drops, plan changes,
  collector gaps, failed sweeps, backward corrections, multi-source deduplication,
  multi-source disagreements, and sibling/model-scoped limits are handled cleanly.
- Binding windows & `blocked_gap`: exact A=0.10 (reset +3h) + B=0.40 (reset +5h, 5h spent
  until +2h) at pooled 0.10/h reports `blocked_gap` at +1h..+2h with locked 0.40 (NEVER
  "Lasts" or promising 0.30 before +3h); other governing binding windows show an open
  caveat directly on the card with Details closed.
- Combined (`joint`) keeps all existing measured numbers, emits no Typical scenario, draws
  no future line while Typical is selected, and works cleanly on `No new use`.
- `quota_summary` and `quota_context` retain trailing-hour `recent_pace` untouched.
- Real browser (Chromium + WebKit) renders Typical `runs_out`, `blocked_gap`, unknown
  `insufficient`, `Combined`, `No new use`, dark/light themes, and narrow mobile widths
  with zero console/page errors and zero horizontal overflow.
"""
import math
import time
from pathlib import Path

import pytest

import plugin
import quota_context as qc
import quota_forecast as qf
import quota_history
import quota_summary as qs
from test_reserve import FIVE_H, WEEK, payload, profile, with_i18n

WIDGET = Path(__file__).with_name("widget.js").resolve()
H = 3600.0


def _aligned_now() -> float:
    """A whole-hour-aligned timestamp near the current wall clock."""
    return float(int(time.time() // 3600) * 3600 + 600)


def _snap(harness, sid, constraints, *, observed_at, plan="plus", source="app", availability="available"):
    return {
        "subject": {"harness": harness, "subject_id": sid, "plan_label": plan},
        "constraints": constraints,
        "availability": {"state": availability},
        "observed_at": qs.iso(observed_at),
        "freshness": "fresh",
        "source": source,
    }


def _con(cid, used_ratio, window_sec, reset_at, *, label=None, models=None, request_kinds=None):
    row = {
        "id": cid,
        "label": label or cid,
        "used_ratio": round(max(0.0, min(1.0, used_ratio)), 6),
        "window_seconds": window_sec,
        "resets_at": qs.iso(reset_at) if reset_at is not None else None,
    }
    if models is not None:
        row["applies_to_models"] = list(models)
    if request_kinds is not None:
        row["applies_to_request_kinds"] = list(request_kinds)
    return row


def _record_sweeps(tmp_path, harness, sweep_fn, times, *, failed_at=()):
    """Persist synthetic sweeps into a fresh HistoryStore and return (store, last_status)."""
    store = quota_history.HistoryStore(tmp_path)
    failed = set(failed_at)
    last_status = None
    for t in times:
        if t in failed:
            plugin.persist_sweep(store, None, "collector timeout", t)
            continue
        snaps, active_sids = sweep_fn(t)
        profs = [profile(harness, sid) for sid in active_sids]
        st = payload(snaps, profs, harnesses=(harness,))
        plugin.persist_sweep(store, st, "", t)
        last_status = st
    assert last_status is not None
    return store, last_status


def _reserve(store, status, now, *, harness="codex", group="", horizon="7d", analytics=True, model=None):
    return plugin.reserve_view(
        store, status, now, now,
        harness=harness, group=group, horizon=horizon, name_accounts=True, analytics=analytics, model=model,
    )


# ---------------------------------------------------------------------------
# 1. Pooled demand continues across individual account exhaustion + unified simulation


def test_pooled_demand_continues_when_one_account_hits_zero(tmp_path):
    """Account a has 0.10 left and b has 0.40 left (0.50 total), both resetting at +20h.
    At pooled 0.10 win/h, old per-account decay would empty a at +1.67h and stretch b to +10h;
    pooled Typical keeps burning 0.10 win/h across the pool and empties at +5h (15h before reset)."""
    now = _aligned_now()
    reset_at = now + 20 * H
    times = [now - 14 * H + k * 300.0 for k in range(14 * 12 + 1)]

    def sweep(t):
        dt = (t - (now - 14 * H)) / H
        u_a = 0.06 + 0.06 * dt  # 0.06 -> 0.90 (0.10 left at now)
        u_b = 0.04 + 0.04 * dt  # 0.04 -> 0.60 (0.40 left at now)
        return [
            _snap("codex", "a", [_con("primary", u_a, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
            _snap("codex", "b", [_con("primary", u_b, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
        ], ("a", "b")

    store, status = _record_sweeps(tmp_path, "codex", sweep, times)
    rv = _reserve(store, status, now, horizon="24h")
    chart = rv["chart"]
    typ = chart["scenarios"]["typical"]
    rp = chart["scenarios"]["recent_pace"]

    assert typ["status"] == "ok"
    assert typ["valid_hours"] >= 13.5
    assert math.isclose(typ["rate_per_hour"], 0.10, abs_tol=0.002)

    v = typ["verdict"]
    assert v["state"] == "runs_out"
    assert abs((v["empty_at"] - now) - 5 * H) < 180
    assert abs(v["gap_seconds"] - 15 * H) < 180
    assert v["before_first_reset"] == 0.0
    assert math.isclose(v["after_first_reset"], 2.0, abs_tol=0.01)

    # Unified simulation invariant: checkpoint at +6h, line at +6h, and table all agree on 0.0.
    cp6_typ = next(c["value"] for c in typ["checkpoints"] if c["after_seconds"] == 6 * 3600)
    cp6_rp = next(c["value"] for c in rp["checkpoints"] if c["after_seconds"] == 6 * 3600)
    assert cp6_typ == 0.0
    assert cp6_rp > 0.15
    assert math.isclose(qf.eval_polyline(typ["line"], now + 6 * H), 0.0, abs_tol=1e-6)
    future_rows = [
        r for r in chart["table"]
        if (qs.parse_instant(r["at"]) or 0) >= now + 6 * H and (qs.parse_instant(r["at"]) or 0) < reset_at
    ]
    assert future_rows and all(r["scenario_typical"] == 0.0 for r in future_rows)


# ---------------------------------------------------------------------------
# 2. Contrastive (a): rare positive burst across many zero hours


def test_rare_positive_burst_across_many_zero_hours_gives_positive_rate(tmp_path):
    """20 valid hours where 18 hours are completely quiet (0 burn) and 2 burst hours burn 0.20 each
    (= 0.40 total burn). A median over 20 hours would falsely collapse to 0.0; total observed
    pool burn / valid measured time yields 0.40 / 20 = 0.020 win/h."""
    now = _aligned_now()
    base = now - 20 * H
    reset_at = now + 72 * H
    times = [base + k * 300.0 for k in range(20 * 12 + 1)]

    def cumulative_use(dt_h):
        total = 0.05
        for h_idx in range(20):
            lo, hi = float(h_idx), float(h_idx + 1)
            if dt_h <= lo:
                break
            frac = min(dt_h, hi) - lo
            if h_idx in (5, 14):
                total += 0.20 * frac
        return total

    def sweep(t):
        dt = (t - base) / H
        return [
            _snap("codex", "a", [_con("primary", cumulative_use(dt), WEEK, reset_at, label="Weekly")], observed_at=t - 10),
        ], ("a",)

    store, status = _record_sweeps(tmp_path, "codex", sweep, times)
    rv = _reserve(store, status, now, horizon="7d")
    typ = rv["chart"]["scenarios"]["typical"]
    assert typ["status"] == "ok"
    assert typ["valid_hours"] >= 19.5
    assert math.isclose(typ["rate_per_hour"], 0.020, abs_tol=0.002)
    assert typ["verdict"]["state"] == "runs_out"


# ---------------------------------------------------------------------------
# 3. Contrastive (b) & (c): known zero vs frozen cache vs 1-of-22 bug vs small 1-4 pools vs <12h


def test_known_zero_vs_frozen_cache_vs_1_of_22_coverage_bug_and_small_pools(tmp_path):
    now = _aligned_now()
    reset_at = now + 48 * H
    times_16 = [now - 16 * H + k * 300.0 for k in range(16 * 12 + 1)]

    # (b1) Known measured zero with full coverage (2 accounts, fresh moving observed_at) -> status="zero"
    def sweep_zero(t):
        return [
            _snap("codex", "a", [_con("primary", 0.30, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
            _snap("codex", "b", [_con("primary", 0.40, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
        ], ("a", "b")

    store_z, st_z = _record_sweeps(tmp_path / "zero", "codex", sweep_zero, times_16)
    rv_z = _reserve(store_z, st_z, now, horizon="7d")
    typ_z = rv_z["chart"]["scenarios"]["typical"]
    assert typ_z["status"] == "zero"
    assert typ_z["rate_per_hour"] == 0.0
    assert typ_z["valid_hours"] >= 15.5
    assert typ_z["verdict"]["state"] == "zero_pace"
    assert typ_z["line"] == rv_z["chart"]["scenarios"]["no_new_use"]["line"]

    # (b2) Frozen single cached reading (observed_at never advances, n_obs == 1) -> MUST stay insufficient!
    frozen_obs = now - 16 * H - 30

    def sweep_frozen(t):
        return [
            _snap("codex", "a", [_con("primary", 0.30, WEEK, reset_at, label="Weekly")], observed_at=frozen_obs),
            _snap("codex", "b", [_con("primary", 0.40, WEEK, reset_at, label="Weekly")], observed_at=frozen_obs),
        ], ("a", "b")

    store_f, st_f = _record_sweeps(tmp_path / "frozen", "codex", sweep_frozen, times_16)
    rv_f = _reserve(store_f, st_f, now, horizon="7d")
    typ_f = rv_f["chart"]["scenarios"]["typical"]
    assert typ_f["status"] in ("warming_up", "insufficient")
    assert typ_f["valid_hours"] == 0.0
    assert typ_f["line"] == [] and typ_f["checkpoints"] == [] and typ_f["schedule"] == []

    # (b3) Regression test for the 1-of-22 bug: 1 account watched flat for 14h + 21 current-only accounts.
    # Coverage is 1/22 (far below 80%), so MUST stay insufficient — NEVER zero_pace!
    times_14 = [now - 14 * H + k * 300.0 for k in range(14 * 12 + 1)]
    all_22 = tuple(f"acc-{i:02d}" for i in range(22))

    def sweep_1_of_22(t):
        if t < now - 1:
            return [
                _snap("codex", "acc-00", [_con("primary", 0.25, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
            ], ("acc-00",)
        return [
            _snap("codex", sid, [_con("primary", 0.25, WEEK, reset_at, label="Weekly")], observed_at=t - 10)
            for sid in all_22
        ], all_22

    store_22, st_22 = _record_sweeps(tmp_path / "one_of_22", "codex", sweep_1_of_22, times_14)
    rv_22 = _reserve(store_22, st_22, now, horizon="7d")
    typ_22 = rv_22["chart"]["scenarios"]["typical"]
    assert typ_22["status"] in ("warming_up", "insufficient")
    assert typ_22["verdict"]["state"] == "insufficient"
    assert typ_22["line"] == [] and typ_22["checkpoints"] == [] and typ_22["schedule"] == []
    assert all(r["scenario_typical"] is None for r in rv_22["chart"]["table"] if (qs.parse_instant(r["at"]) or 0) >= now)

    # (b4) Small pools of 1, 2, 3, and 4 fully visible accounts all work cleanly (no min-5 floor).
    for pool_size in (1, 2, 3, 4):
        sids = tuple(f"s{i}" for i in range(pool_size))

        def sweep_small(t, _sids=sids):
            dt = (t - (now - 14 * H)) / H
            return [
                _snap("codex", sid, [_con("primary", 0.10 + 0.02 * dt, WEEK, reset_at, label="Weekly")], observed_at=t - 10)
                for sid in _sids
            ], _sids

        store_s, st_s = _record_sweeps(tmp_path / f"small_{pool_size}", "codex", sweep_small, times_14)
        rv_s = _reserve(store_s, st_s, now, horizon="7d")
        typ_s = rv_s["chart"]["scenarios"]["typical"]
        assert typ_s["status"] == "ok", (pool_size, typ_s)
        assert math.isclose(typ_s["rate_per_hour"], 0.02 * pool_size, abs_tol=0.002)

    # (c) < 12 valid hours (11h) -> unknown Typical with empty line, checkpoints, schedule, and None in table.
    times_11 = [now - 11 * H + k * 300.0 for k in range(11 * 12 + 1)]

    def sweep_11(t):
        dt = (t - (now - 11 * H)) / H
        return [
            _snap("codex", "a", [_con("primary", 0.05 + 0.02 * dt, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
        ], ("a",)

    store_11, st_11 = _record_sweeps(tmp_path / "h11", "codex", sweep_11, times_11)
    rv_11 = _reserve(store_11, st_11, now, horizon="7d")
    typ_11 = rv_11["chart"]["scenarios"]["typical"]
    assert typ_11["status"] in ("warming_up", "insufficient")
    assert typ_11["valid_hours"] < 12.0
    assert typ_11["verdict"]["state"] == "insufficient"
    assert typ_11["verdict"]["before_first_reset"] is None
    assert typ_11["line"] == [] and typ_11["checkpoints"] == [] and typ_11["schedule"] == []
    assert all(r.get("scenario_typical") is None for r in rv_11["chart"]["table"])


# ---------------------------------------------------------------------------
# 4. Data hygiene: jitter, resets, drops, plans, gaps, failed sweeps, multi-source, isolation


def test_estimator_hygiene_resets_jitter_drops_plans_gaps_failed_multisource_and_isolation(tmp_path):
    """Feed 24h of history containing:
    - reset jitter (+45s oscillation <= 120s): MUST stay comparable (not dropped)
    - collector gap (> 420s) at hour 3..4
    - failed sweep at hour 6.5
    - real reset (new resets_at + ratio drop) on account a at hour 9.5
    - ratio drop without reset on account b at hour 11.5
    - plan change on account b at hour 13.5
    - dual agreeing sources on account a (must NOT double-count!)
    - brief source disagreement on account b at hour 16..17 (must exclude that span!)
    - sibling 5h window and model-scoped window burning 10x faster (must stay isolated!)
    Every clean hour burns 0.02/h per account (0.04/h pooled across a+b)."""
    now = _aligned_now()
    base = now - 24 * H
    reset_old = now + 10 * H
    reset_new = now + 178 * H
    five_reset = now + 2 * H

    times = []
    failed_at = set()
    for k in range(24 * 12 + 1):
        t = base + k * 300.0
        if base + 3 * H < t < base + 4 * H:
            continue  # collector gap
        times.append(t)
        if abs(t - (base + 6.5 * H)) < 1.0:
            failed_at.add(t)

    def sweep(t):
        dt = (t - base) / H
        # Reset jitter <= 120s on every alternate sweep: must not break comparability.
        jitter = 45.0 if int(round(dt * 12)) % 2 == 0 else 0.0
        if dt < 9.5:
            u_a, r_a = 0.10 + 0.02 * dt, reset_old + jitter
        else:
            u_a, r_a = 0.02 + 0.02 * (dt - 9.5), reset_new + jitter
        if dt < 11.5:
            u_b = 0.25 + 0.02 * dt
        else:
            u_b = 0.15 + 0.02 * (dt - 11.5)  # ratio drop without reset
        plan_b = "plus" if dt < 13.5 else "pro"

        snaps = [
            # Account a reported by TWO agreeing sources -> must NOT double-count!
            _snap("codex", "a", [
                _con("primary", u_a, WEEK, r_a, label="Weekly"),
                _con("five_hour", min(0.95, (dt * 0.25) % 0.9), FIVE_H, five_reset, label="5 hour"),
                _con("scoped", min(0.95, 0.05 + 0.04 * dt), WEEK, reset_new, label="Scoped", models=["m-fast"]),
            ], observed_at=t - 10, source="app"),
            _snap("codex", "a", [
                _con("primary", u_a, WEEK, r_a, label="Weekly"),
            ], observed_at=t - 10, source="cli"),
            _snap("codex", "b", [
                _con("primary", u_b, WEEK, reset_new + jitter, label="Weekly"),
                _con("five_hour", min(0.95, (dt * 0.25) % 0.9), FIVE_H, five_reset, label="5 hour"),
            ], observed_at=t - 10, plan=plan_b, source="app"),
        ]
        # Between hour 16 and 17, a second source on b disagrees sharply -> excluded!
        if 16.0 <= dt <= 17.0:
            snaps.append(_snap("codex", "b", [
                _con("primary", min(0.99, u_b + 0.30), WEEK, reset_new, label="Weekly"),
            ], observed_at=t - 10, plan=plan_b, source="cli"))
        return snaps, ("a", "b")

    store, status = _record_sweeps(tmp_path, "codex", sweep, times, failed_at=failed_at)
    rv0 = _reserve(store, status, now, horizon="7d")
    weekly_key = next(
        g["key"] for g in rv0["summary"]["groups"]
        if g["window_seconds"] == WEEK and not g.get("models")
    )
    rv = _reserve(store, status, now, group=weekly_key, horizon="7d")
    typ = rv["chart"]["scenarios"]["typical"]
    assert typ["status"] == "ok", typ
    assert typ["valid_hours"] >= 16.0, typ
    # True pooled rate is 0.04 win/h (NOT 0.06 from double-counting a, NOT polluted by 5h or resets).
    assert math.isclose(typ["rate_per_hour"], 0.04, abs_tol=0.004), typ


# ---------------------------------------------------------------------------
# 5. Span independence (24h vs 7d) & first reset beyond 24h


def test_24h_and_7d_compute_same_48h_typical_rate_and_verdict(tmp_path):
    now = _aligned_now()
    reset_at = now + 48 * H  # past 24h span, inside 7d span
    times = [now - 18 * H + k * 300.0 for k in range(18 * 12 + 1)]

    def sweep(t):
        dt = (t - (now - 18 * H)) / H
        return [
            _snap("codex", "a", [_con("primary", 0.10 + 0.02 * dt, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
            _snap("codex", "b", [_con("primary", 0.15 + 0.01 * dt, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
        ], ("a", "b")

    store, status = _record_sweeps(tmp_path, "codex", sweep, times)
    rv24 = _reserve(store, status, now, horizon="24h")
    rv7d = _reserve(store, status, now, horizon="7d")
    t24 = rv24["chart"]["scenarios"]["typical"]
    t7d = rv7d["chart"]["scenarios"]["typical"]

    assert t24["status"] == t7d["status"] == "ok"
    assert t24["rate_per_hour"] == t7d["rate_per_hour"]
    assert t24["valid_hours"] == t7d["valid_hours"]
    assert t24["verdict"]["state"] == t7d["verdict"]["state"] == "runs_out"
    assert t24["verdict"]["before_first_reset"] == t7d["verdict"]["before_first_reset"] == 0.0
    assert t24["verdict"]["empty_at"] == t7d["verdict"]["empty_at"]
    assert t24["verdict"]["first_reset_in_horizon"] is False
    assert t7d["verdict"]["first_reset_in_horizon"] is True


# ---------------------------------------------------------------------------
# 6. Exact binding-window blocked_gap scenario (A=0.10/+3h, B=0.40/+5h 5h-spent-+2h, rate=0.10/h)


def test_blocked_gap_when_runnable_account_empties_while_sibling_is_5h_blocked(tmp_path):
    """Supervisor's exact test case:
    - Account A: 0.10 weekly left (reset +3h), 5h window healthy.
    - Account B: 0.40 weekly left (reset +5h), 5h window exhausted until +2h.
    - Pooled weekly rate: 0.10 win/h.
    Between +1h and +2h there is NO usable/unblocked reserve even though weekly still holds 0.40 on B.
    Must report `blocked_gap` (empty_at=+1h, resumes_at=+2h, locked_at_empty=0.40) and NEVER claim 'lasts'."""
    now = _aligned_now()
    reset_a = now + 3 * H
    reset_b = now + 5 * H
    five_unblock_b = now + 2 * H
    five_reset_a = now + 4 * H
    times = [now - 14 * H + k * 300.0 for k in range(14 * 12 + 1)]

    def sweep(t):
        dt = (t - (now - 14 * H)) / H
        u_a = 0.06 + 0.06 * dt  # 0.06 -> 0.90 (0.10 left at now)
        u_b = 0.04 + 0.04 * dt  # 0.04 -> 0.60 (0.40 left at now)
        five_b = 1.0 if t >= now - 60 else 0.25
        return [
            _snap("codex", "a", [
                _con("primary", u_a, WEEK, reset_a, label="Weekly"),
                _con("five_hour", 0.20, FIVE_H, five_reset_a, label="5 hour"),
            ], observed_at=t - 10),
            _snap("codex", "b", [
                _con("primary", u_b, WEEK, reset_b, label="Weekly"),
                _con("five_hour", five_b, FIVE_H, five_unblock_b, label="5 hour"),
            ], observed_at=t - 10),
        ], ("a", "b")

    store, status = _record_sweeps(tmp_path / "gap", "codex", sweep, times)
    rv0 = _reserve(store, status, now, horizon="24h")
    week_key = next(g["key"] for g in rv0["summary"]["groups"] if g["window_seconds"] == WEEK)
    rv = _reserve(store, status, now, group=week_key, horizon="24h", analytics=True)
    typ = rv["chart"]["analytics"]["window"]["scenarios"]["typical"]
    v = typ["verdict"]

    assert typ["status"] == "ok"
    assert math.isclose(typ["rate_per_hour"], 0.10, abs_tol=0.002)
    assert v["state"] == "blocked_gap", v
    assert abs((v["empty_at"] - now) - 1 * H) < 120, v
    assert abs((v["resumes_at"] - now) - 2 * H) < 120, v
    assert abs(v["gap_seconds"] - 1 * H) < 120, v
    assert math.isclose(v["locked_at_empty"], 0.40, abs_tol=0.01), v
    assert any("5" in lbl.lower() for lbl in (v.get("other_binding_labels") or [])), v
    # Total pool reserve is flat at 0.40 during the blocked gap [+1h..+2h], then burns 0.10/h until +3h (0.30).
    assert math.isclose(qf.eval_polyline(typ["line"], now + 1.5 * H), 0.40, abs_tol=0.01)
    assert math.isclose(v["before_first_reset"], 0.30, abs_tol=0.01)
    assert any(r.get("blocked_only") for r in typ["runs_out"])

    # Also test all-held (other limit spent with no reset) & non-governing scope -> status="held", empty line.
    def sweep_all_blocked(t):
        dt = (t - (now - 14 * H)) / H
        u = 0.08 + 0.02 * dt
        five_u = 1.0 if t >= now - 60 else 0.30
        five_r = None if t >= now - 60 else five_reset_a
        return [
            _snap("codex", "a", [
                _con("primary", u, WEEK, reset_b, label="Weekly"),
                _con("five_hour", five_u, FIVE_H, five_r, label="5 hour"),
            ], observed_at=t - 10),
            _snap("codex", "b", [
                _con("primary", u, WEEK, reset_b, label="Weekly"),
                _con("five_hour", five_u, FIVE_H, five_r, label="5 hour"),
            ], observed_at=t - 10),
        ], ("a", "b")

    store2, st2 = _record_sweeps(tmp_path / "all_held", "codex", sweep_all_blocked, times)
    rv2 = _reserve(store2, st2, now, group=week_key, horizon="7d", analytics=True)
    typ2 = rv2["chart"]["analytics"]["window"]["scenarios"]["typical"]
    assert typ2["status"] == "held"
    assert typ2["held_reason"] == "other_limit_spent"
    assert typ2["verdict"]["state"] == "held"
    assert typ2["line"] == [] and typ2["checkpoints"] == [] and typ2["schedule"] == []

    def sweep_scoped(t):
        dt = (t - (now - 14 * H)) / H
        u = 0.08 + 0.02 * dt
        return [
            _snap("claude", "a", [
                _con("seven_day", u, WEEK, reset_b, label="7 day"),
                _con("scoped", u, WEEK, reset_b, label="Scoped", models=["only-m"]),
            ], observed_at=t - 10),
        ], ("a",)

    store3, st3 = _record_sweeps(tmp_path / "scoped", "claude", sweep_scoped, times)
    rv3_0 = _reserve(store3, st3, now, harness="claude", horizon="7d")
    scoped_key = next(g["key"] for g in rv3_0["summary"]["groups"] if g.get("models") == ["only-m"])
    rv3 = _reserve(store3, st3, now, harness="claude", group=scoped_key, horizon="7d", analytics=True, model="other-m")
    sc_typ = rv3["chart"]["analytics"]["window"]["scenarios"]["typical"]
    assert sc_typ["status"] == "held" and sc_typ["held_reason"] == "scope_not"
    assert sc_typ["line"] == [] and sc_typ["checkpoints"] == [] and sc_typ["schedule"] == []


# ---------------------------------------------------------------------------
# 7. Verdict states & thresholds directly on quota_forecast


def _member(rem: float, reset_at, *, sid: str = "a") -> qs.Member:
    reading = qs.Reading(
        harness="codex", subject_id=sid, source="app", fresh=True,
        observed_at=_aligned_now() - 10, key=f"codex|primary|{WEEK}|-",
        meaning="primary", label="Weekly", window=WEEK,
        models=(), ratio=max(0.0, min(1.0, 1.0 - rem)), ratio_problem="",
        resets_at=reset_at, plan="plus", plan_key="plus",
    )
    return qs.Member(
        harness="codex", subject_id=sid, status="measured",
        reading=reading, remaining=rem, observed_at=reading.observed_at,
    )


def test_verdict_states_and_unified_simulation():
    now = _aligned_now()
    end = now + 24 * H

    # Tight: <= 0.05 left before reset (0.03 left)
    sc_tight = qf.build_typical_scenario(
        [_member(0.43, now + 4 * H)], now, end, "24h",
        {"state": "ok", "rate_per_hour": 0.10, "valid_hours": 24.0, "valid_seconds": 86400, "min_valid_hours": 12},
    )
    assert sc_tight["verdict"]["state"] == "tight"
    assert math.isclose(sc_tight["verdict"]["before_first_reset"], 0.03, abs_tol=1e-3)

    # Tight: empties 5 min before reset (< 15m gap threshold)
    sc_tight_gap = qf.build_typical_scenario(
        [_member(0.395, now + 4 * H)], now, end, "24h",
        {"state": "ok", "rate_per_hour": 0.10, "valid_hours": 24.0, "valid_seconds": 86400, "min_valid_hours": 12},
    )
    assert sc_tight_gap["verdict"]["state"] == "tight"
    assert sc_tight_gap["verdict"]["gap_seconds"] < qf.VERDICT_GAP_BAD_SEC

    # Lasts comfortably: 0.60 left before reset
    sc_lasts = qf.build_typical_scenario(
        [_member(1.0, now + 4 * H)], now, end, "24h",
        {"state": "ok", "rate_per_hour": 0.10, "valid_hours": 24.0, "valid_seconds": 86400, "min_valid_hours": 12},
    )
    assert sc_lasts["verdict"]["state"] == "lasts"
    assert math.isclose(sc_lasts["verdict"]["before_first_reset"], 0.60, abs_tol=1e-3)
    assert sc_lasts["verdict"]["empty_at"] is None

    # Empty now with and without reset
    sc_empty = qf.build_typical_scenario(
        [_member(0.0, now + 3 * H)], now, end, "24h",
        {"state": "ok", "rate_per_hour": 0.10, "valid_hours": 24.0, "valid_seconds": 86400, "min_valid_hours": 12},
    )
    assert sc_empty["verdict"]["state"] == "empty_now"
    assert sc_empty["verdict"]["after_first_reset"] == 1.0

    sc_empty_nr = qf.build_typical_scenario(
        [_member(0.0, None)], now, end, "24h",
        {"state": "ok", "rate_per_hour": 0.10, "valid_hours": 24.0, "valid_seconds": 86400, "min_valid_hours": 12},
    )
    assert sc_empty_nr["verdict"]["state"] == "empty_now"
    assert sc_empty_nr["verdict"]["first_reset_at"] is None

    # Runs out with no reported reset
    sc_nr = qf.build_typical_scenario(
        [_member(0.50, None)], now, end, "24h",
        {"state": "ok", "rate_per_hour": 0.10, "valid_hours": 24.0, "valid_seconds": 86400, "min_valid_hours": 12},
    )
    assert sc_nr["verdict"]["state"] == "runs_out_no_reset"
    assert abs((sc_nr["verdict"]["empty_at"] - now) - 5 * H) < 5

    # Unavailable -> NO future line, NO fake before_first_reset
    sc_unav = qf.build_typical_scenario(
        [_member(0.80, now + 4 * H)], now, end, "24h",
        {"state": "unavailable", "rate_per_hour": None, "valid_hours": 0.0, "valid_seconds": 0, "min_valid_hours": 12},
    )
    assert sc_unav["verdict"]["state"] == "unavailable"
    assert sc_unav["verdict"]["before_first_reset"] is None
    assert sc_unav["line"] == [] and sc_unav["checkpoints"] == [] and sc_unav["schedule"] == []


# ---------------------------------------------------------------------------
# 8. Combined honesty & untouched quota_summary / quota_context surfaces


def test_combined_emits_no_typical_and_tool_context_stay_separate(tmp_path):
    now = _aligned_now()
    week_reset = now + 48 * H
    five_reset = now + 3 * H
    times = [now - 16 * H + k * 300.0 for k in range(16 * 12 + 1)]

    def sweep(t):
        dt = (t - (now - 16 * H)) / H
        return [
            _snap("claude", "a", [
                _con("five_hour", 0.10 + 0.03 * dt, FIVE_H, five_reset, label="5 hour"),
                _con("seven_day", 0.10 + 0.02 * dt, WEEK, week_reset, label="7 day"),
            ], observed_at=t - 10),
            _snap("claude", "b", [
                _con("five_hour", 0.15 + 0.02 * dt, FIVE_H, five_reset, label="5 hour"),
                _con("seven_day", 0.15 + 0.02 * dt, WEEK, week_reset, label="7 day"),
            ], observed_at=t - 10),
        ], ("a", "b")

    store, status = _record_sweeps(tmp_path, "claude", sweep, times)
    rv = _reserve(store, status, now, harness="claude", horizon="7d", analytics=True)

    # Combined preserves all measured fields and emits NO typical scenario.
    joint = rv["chart"]["analytics"]["joint"]
    assert joint["available"] is True
    assert joint["known"] == 2 and joint["accounts"] == 2 and joint["figure"] is not None
    assert "no_new_use" in joint["scenarios"] and "typical" not in joint["scenarios"]

    # quota_summary tool & quota_context snapshot stay strictly on trailing-hour recent_pace.
    tool = qs.compact(rv["summary"])
    for g in tool["groups"]:
        assert "recent_pace" in g
        assert "typical" not in g
        assert "verdict" not in g
    brief = qc.brief_text(rv["summary"])
    details = qc.snapshot_text(rv["summary"])
    assert "48h" not in brief and "typical" not in brief.lower()
    assert "48h" not in details and "typical" not in details.lower()


# ---------------------------------------------------------------------------
# 9. Real browser (Chromium + WebKit) visual & DOM verification


pytest.importorskip("playwright.sync_api")

STUB = with_i18n(r"""
(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true } : view) });
  };
}
""")


def _browser_view_runs_out(tmp_path):
    now = _aligned_now()
    week_reset = now + 24 * H
    five_reset = now + 4 * H
    times = [now - 16 * H + k * 300.0 for k in range(16 * 12 + 1)]

    def sweep(t):
        dt = (t - (now - 16 * H)) / H
        return [
            _snap("claude", "acct-1", [
                _con("five_hour", 0.10 + 0.02 * dt, FIVE_H, five_reset, label="5 hour"),
                _con("seven_day", 0.15 + 0.04 * dt, WEEK, week_reset, label="7 day"),
            ], observed_at=t - 10),
            _snap("claude", "acct-2", [
                _con("five_hour", 0.10 + 0.02 * dt, FIVE_H, five_reset, label="5 hour"),
                _con("seven_day", 0.10 + 0.04 * dt, WEEK, week_reset, label="7 day"),
            ], observed_at=t - 10),
        ], ("acct-1", "acct-2")

    store, status = _record_sweeps(tmp_path, "claude", sweep, times)
    view = plugin.build_view(status, "")
    rv0 = _reserve(store, status, now, harness="claude", horizon="7d")
    week_key = next(g["key"] for g in rv0["summary"]["groups"] if g["window_seconds"] == WEEK)
    view["reserve"] = _reserve(store, status, now, harness="claude", group=week_key, horizon="7d", analytics=True)
    return view


def _browser_view_blocked_gap(tmp_path):
    now = _aligned_now()
    reset_a = now + 3 * H
    reset_b = now + 5 * H
    five_unblock_b = now + 2 * H
    five_reset_a = now + 4 * H
    times = [now - 14 * H + k * 300.0 for k in range(14 * 12 + 1)]

    def sweep(t):
        dt = (t - (now - 14 * H)) / H
        five_b = 1.0 if t >= now - 60 else 0.25
        return [
            _snap("codex", "acct-a", [
                _con("primary", 0.06 + 0.06 * dt, WEEK, reset_a, label="Weekly"),
                _con("five_hour", 0.20, FIVE_H, five_reset_a, label="5 hour"),
            ], observed_at=t - 10),
            _snap("codex", "acct-b", [
                _con("primary", 0.04 + 0.04 * dt, WEEK, reset_b, label="Weekly"),
                _con("five_hour", five_b, FIVE_H, five_unblock_b, label="5 hour"),
            ], observed_at=t - 10),
        ], ("acct-a", "acct-b")

    store, status = _record_sweeps(tmp_path, "codex", sweep, times)
    view = plugin.build_view(status, "")
    rv0 = _reserve(store, status, now, harness="codex", horizon="7d")
    week_key = next(g["key"] for g in rv0["summary"]["groups"] if g["window_seconds"] == WEEK)
    view["reserve"] = _reserve(store, status, now, harness="codex", group=week_key, horizon="7d", analytics=True)
    return view


def _browser_view_insufficient(tmp_path):
    now = _aligned_now()
    reset_at = now + 24 * H
    times = [now - 4 * H + k * 300.0 for k in range(4 * 12 + 1)]

    def sweep(t):
        dt = (t - (now - 4 * H)) / H
        return [
            _snap("codex", "acct-1", [_con("primary", 0.20 + 0.02 * dt, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
            _snap("codex", "acct-2", [_con("primary", 0.30 + 0.02 * dt, WEEK, reset_at, label="Weekly")], observed_at=t - 10),
        ], ("acct-1", "acct-2")

    store, status = _record_sweeps(tmp_path, "codex", sweep, times)
    view = plugin.build_view(status, "")
    view["reserve"] = _reserve(store, status, now, harness="codex", horizon="7d", analytics=True)
    return view


@pytest.mark.parametrize("width,theme", [(1120, "dark"), (1120, "light"), (360, "dark")])
def test_real_browser_typical_verdict_binding_caveat_and_modes(browser, tmp_path_factory, width, theme):
    view = _browser_view_runs_out(tmp_path_factory.mktemp(f"typ-{width}-{theme}"))
    page = browser.new_page(viewport={"width": width, "height": 1300})
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    try:
        page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
        page.evaluate(STUB, view)
        page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
        page.wait_for_selector(".tl-block .chart-svg")
        if theme == "light":
            page.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
            page.wait_for_timeout(100)

        # Future control has ONLY Typical (default pressed) and No new use — no Fast pace, no Recent pace.
        fut_buttons = page.evaluate("""() => [...document.querySelectorAll('[data-focus^="scenario:"]')].map((b) => ({
            focus: b.getAttribute('data-focus'), text: b.textContent,
            pressed: b.getAttribute('aria-pressed'), idle: b.getAttribute('data-idle')
        }))""")
        assert fut_buttons == [
            {"focus": "scenario:typical", "text": "Typical", "pressed": "true", "idle": None},
            {"focus": "scenario:no_new_use", "text": "No new use", "pressed": "false", "idle": None},
        ], fut_buttons

        # Typical runs out before the 24h reset; open binding-window caveat is visible with Details closed!
        typ_state = page.evaluate("""() => ({
            scenarioAttr: document.querySelector('.tl-block').getAttribute('data-scenario'),
            detailsOpen: document.querySelector('.chart-table').open,
            verdict: document.querySelector('.tl-verdict') ? document.querySelector('.tl-verdict').textContent : '',
            depleteDots: document.querySelectorAll('.tl-block .chart-svg .deplete-dot').length,
            depleteLines: document.querySelectorAll('.tl-block .chart-svg .deplete-line').length,
            futurePaths: document.querySelectorAll('.tl-block .chart-svg path.line-scenario').length,
            legend: document.querySelector('.tl-block .calm-legend').textContent,
            overflow: document.documentElement.scrollWidth > window.innerWidth,
            allText: document.body.textContent,
        })""")
        assert typ_state["scenarioAttr"] == "typical", typ_state
        assert typ_state["detailsOpen"] is False
        assert "runs out" in typ_state["verdict"].lower() and "48h pool avg" in typ_state["verdict"], typ_state
        assert "other binding limit" in typ_state["verdict"], "open binding-window caveat visible with Details closed"
        assert "win" in typ_state["verdict"] and "Empty at" in typ_state["verdict"], typ_state
        assert typ_state["depleteDots"] == 1 and typ_state["depleteLines"] == 1 and typ_state["futurePaths"] == 1
        assert "48h typical" in typ_state["legend"], typ_state
        assert "MSK" not in typ_state["allText"], "no hardcoded MSK timezone"
        assert not typ_state["overflow"], f"horizontal overflow at {width}px"

        # Switch to No new use -> neutral line, no depletion marker, refill-only verdict.
        page.click('[data-focus="scenario:no_new_use"]')
        page.wait_for_function("() => document.querySelector('.tl-block').getAttribute('data-scenario') === 'no_new_use'")
        nnu_state = page.evaluate("""() => ({
            verdict: document.querySelector('.tl-verdict').textContent,
            depleteDots: document.querySelectorAll('.tl-block .chart-svg .deplete-dot').length,
            futurePaths: document.querySelectorAll('.tl-block .chart-svg path.line-scenario').length,
            legend: document.querySelector('.tl-block .calm-legend').textContent,
        })""")
        assert "if idle" in nnu_state["verdict"] and "win" in nnu_state["verdict"], nnu_state
        assert nnu_state["depleteDots"] == 0 and nnu_state["futurePaths"] == 1
        assert "no new use · refill only" in nnu_state["legend"], nnu_state

        # Switch back to Typical, then turn on Combined -> Typical becomes idle, NO future line drawn,
        # measured Combined figure & Next reset stay intact, and No new use still works cleanly.
        page.click('[data-focus="scenario:typical"]')
        page.click('[data-focus="graph:joint"]')
        page.wait_for_function("() => document.querySelector('[data-focus=\"graph:joint\"]').getAttribute('aria-pressed') === 'true'")
        joint_typ = page.evaluate("""() => ({
            hero: document.querySelector('.tl-hero').textContent,
            idle: document.querySelector('[data-focus="scenario:typical"]').getAttribute('data-idle'),
            verdict: document.querySelector('.tl-verdict').textContent,
            futurePaths: document.querySelectorAll('.tl-block .chart-svg path.line-scenario').length,
            depleteDots: document.querySelectorAll('.tl-block .chart-svg .deplete-dot').length,
            resets: [...document.querySelectorAll('.tl-block .chart-svg .reset-line, .tl-block .chart-svg .reset-dot')]
                .map((e) => e.getAttribute('x1') || e.getAttribute('cx')),
            legend: document.querySelector('.tl-block .calm-legend').textContent,
        })""")
        assert "Next reset" in joint_typ["hero"] and "2 of 2 accounts counted" in joint_typ["hero"], joint_typ
        assert joint_typ["idle"] == "true", joint_typ
        assert joint_typ["futurePaths"] == 0 and joint_typ["depleteDots"] == 0, joint_typ
        assert len(joint_typ["resets"]) > 0, joint_typ
        assert "No pooled 48h typical forecast for Combined" in joint_typ["verdict"], joint_typ
        assert "not drawn" in joint_typ["legend"], joint_typ

        page.click('[data-focus="scenario:no_new_use"]')
        page.wait_for_selector(".tl-block .chart-svg path.line-scenario")
        assert page.locator(".tl-block .chart-svg path.line-scenario").count() == 1
        joint_nnu_resets = page.evaluate("""() => [...document.querySelectorAll('.tl-block .chart-svg .reset-line, .tl-block .chart-svg .reset-dot')]
            .map((e) => e.getAttribute('x1') || e.getAttribute('cx'))""")
        assert joint_nnu_resets == joint_typ["resets"]
        assert not errors, errors
    finally:
        page.close()


def test_real_browser_blocked_gap_and_unknown_typical_honesty(browser, tmp_path_factory):
    # 1. Blocked-gap scene (A=0.10/+3h, B=0.40/+5h 5h-spent-+2h, rate=0.10/h)
    gap_view = _browser_view_blocked_gap(tmp_path_factory.mktemp("gap-browser"))
    page = browser.new_page(viewport={"width": 1120, "height": 1300})
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    try:
        page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
        page.evaluate(STUB, gap_view)
        page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        page.wait_for_selector(".tl-block .chart-svg")
        got = page.evaluate("""() => ({
            verdict: document.querySelector('.tl-verdict').textContent,
            head: document.querySelector('.tl-verdict-head').textContent,
            dotCy: document.querySelector('.deplete-dot') ? parseFloat(document.querySelector('.deplete-dot').getAttribute('cy')) : null,
            baseY: parseFloat(document.querySelector('line.grid.base').getAttribute('y1')),
            overflow: document.documentElement.scrollWidth > window.innerWidth,
        })""")
        assert "lasts" not in got["head"].lower(), got
        assert "unblocked" in got["head"].lower() and "blocked until" in got["head"].lower(), got
        assert "0.40 win sits on blocked accounts" in got["verdict"], got
        assert "Usable empty" in got["verdict"] and "Resumes" in got["verdict"], got
        # Depletion dot stands at y(0.40) strictly ABOVE the 0 baseline!
        assert got["dotCy"] is not None and got["dotCy"] < got["baseY"] - 5, got
        assert not got["overflow"]
        assert not errors, errors
    finally:
        page.close()

    # 2. Unknown Typical (4h < 12h) draws NO future line, NO checkpoints, NO schedule,
    #    while preserving known reported reset-time marks identically to No new use.
    ins_view = _browser_view_insufficient(tmp_path_factory.mktemp("ins-browser"))
    page = browser.new_page(viewport={"width": 1120, "height": 1300})
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    try:
        page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
        page.evaluate(STUB, ins_view)
        page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        page.wait_for_selector(".tl-block .chart-svg")
        page.click('[data-focus="chart-table"]')
        ins = page.evaluate("""() => ({
            idle: document.querySelector('[data-focus="scenario:typical"]').getAttribute('data-idle'),
            futurePaths: document.querySelectorAll('.tl-block .chart-svg path.line-scenario').length,
            resetDots: document.querySelectorAll('.tl-block .chart-svg .reset-dot').length,
            resetLines: document.querySelectorAll('.tl-block .chart-svg .reset-line').length,
            resets: [...document.querySelectorAll('.tl-block .chart-svg .reset-line, .tl-block .chart-svg .reset-dot')]
                .map((e) => e.getAttribute('x1') || e.getAttribute('cx')),
            checkpoints: document.querySelectorAll('.tl-block .checkpoints').length,
            schedules: document.querySelectorAll('.tl-block .schedule').length,
            verdict: document.querySelector('.tl-verdict').textContent,
            legend: document.querySelector('.tl-block .calm-legend').textContent,
        })""")
        assert ins["idle"] == "true"
        assert ins["futurePaths"] == 0
        assert ins["resetDots"] > 0 and ins["resetLines"] == ins["resetDots"]
        assert ins["checkpoints"] == 0 and ins["schedules"] == 0
        assert "Not enough 48h history" in ins["verdict"] and "no forecast drawn" in ins["verdict"]
        assert "not drawn" in ins["legend"]
        # Switching to No new use renders the refill-only curve, checkpoints, and schedule,
        # while keeping the exact same reported reset marks on the chart.
        page.click('[data-focus="scenario:no_new_use"]')
        page.wait_for_selector(".tl-block .chart-svg path.line-scenario")
        nnu_resets = page.evaluate("""() => [...document.querySelectorAll('.tl-block .chart-svg .reset-line, .tl-block .chart-svg .reset-dot')]
            .map((e) => e.getAttribute('x1') || e.getAttribute('cx'))""")
        assert nnu_resets == ins["resets"]
        assert page.locator(".tl-block .checkpoints .cp-tile").count() >= 1
        assert page.locator(".tl-block .schedule").count() == 1
        # Switching back to Typical hides the curve again and keeps the same reset marks.
        page.click('[data-focus="scenario:typical"]')
        page.wait_for_function("() => document.querySelectorAll('.tl-block .chart-svg path.line-scenario').length === 0")
        back_resets = page.evaluate("""() => [...document.querySelectorAll('.tl-block .chart-svg .reset-line, .tl-block .chart-svg .reset-dot')]
            .map((e) => e.getAttribute('x1') || e.getAttribute('cx'))""")
        assert back_resets == ins["resets"]
        assert not errors, errors
    finally:
        page.close()
