"""Display carry has its own semantics; measured pace never consumes it.

Only synthetic subjects and private temporary stores are used here.
"""
import sqlite3

import pytest

import plugin
import quota_history
import quota_summary as qs
from test_reserve import NOW, constraint, payload, profile, snap


def data(offset, values, *, roster=None, stale=(), reset=86400, reads=None):
    return payload([
        snap("codex", sid, [constraint("primary", ratio, reset=reset)],
             observed=offset, fresh=sid not in stale)
        for sid, ratio in values.items()
    ], [profile("codex", sid) for sid in (values if roster is None else roster)],
        reads=reads, harnesses=("codex",))


def record(store, offset, values, **kw):
    reading = data(offset, values, **kw)
    plugin.persist_sweep(store, reading, "", NOW + offset)
    return reading


def result(store, reading, **kw):
    return plugin.reserve_view(store, reading, NOW, NOW, **kw)


def detail_at(history, offset):
    return [d for d in history["details"] if d["at"] <= NOW + offset][-1]


def test_later_first_observation_does_not_blank_old_hours_or_create_past_values(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -7200, {"atlas": .2})
    record(store, -600, {"atlas": .3, "birch": .4})
    out = result(store, data(0, {"atlas": .4, "birch": .5}))
    history = out["chart"]["history"]
    assert detail_at(history, -7201)["value"] is None
    earlier = detail_at(history, -3600)
    assert earlier["value"] == .8 and earlier["accounts"] == 1
    entered = detail_at(history, -600)
    assert entered["value"] == 1.3 and entered["accounts"] == 2
    assert entered["added"] == 1 and entered["change"] == "first_recorded"
    assert [NOW - 600, None] in history["line"]


@pytest.mark.parametrize("failure", ["missing", "stale", "failed"])
def test_temporary_loss_retains_same_subject_sum_with_dated_provenance(tmp_path, failure):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -1200, {"atlas": .2, "birch": .6})
    if failure == "failed":
        plugin.persist_sweep(store, None, "synthetic unavailable", NOW - 600)
    else:
        record(store, -600, {"atlas": .3, **({"birch": .6} if failure == "stale" else {})},
               roster=("atlas", "birch"), stale=("birch",))
    out = result(store, data(0, {"atlas": .4}, roster=("atlas", "birch")))
    history = out["chart"]["history"]
    during = detail_at(history, -500)
    assert during["accounts"] == 2 and during["carried"] >= 1
    assert during["value"] == pytest.approx(1.2 if failure == "failed" else 1.1)
    end = history["details"][-1]
    assert end["value"] == 1 and end["carried"] == 1 and end["measured"] == 1
    assert end["age_seconds"] == 1200 and end["sources"] == ["app"]
    assert end["origins"] == {"history": 1}
    assert not end["removed"]
    # Fresh-only headline and scenario do not absorb the carry.
    assert out["summary"]["groups"][0]["measured"]["windows"] == .6
    assert out["chart"]["scenarios"]["no_new_use"]["line"][0][1] == .6


def test_passed_reported_reset_retains_pre_reset_value_never_measured_full(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -1200, {"atlas": .8}, reset=-600)
    out = result(store, data(-1200, {"atlas": .8}, stale=("atlas",), reset=-600))
    history = out["chart"]["history"]
    assert {v for t, v in history["line"] if v is not None} == {.2}
    after = detail_at(history, -300)
    assert after["carried"] == after["reset_passed"] == 1
    assert history["details"][-1]["age_seconds"] == 1200
    assert out["chart"]["scenarios"] is None
    assert out["summary"]["groups"][0]["measured"]["accounts"] == 0


def test_removed_subject_history_remains_removal_time_is_unknown(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -3600, {"atlas": .2, "birch": .6})
    # This latest successful roster excludes birch; last_seen is not its
    # removal time. Its known value remains in the earlier historical total.
    out = result(store, data(0, {"atlas": .3}))
    history = out["chart"]["history"]
    assert detail_at(history, -1800)["value"] == 1.2
    assert history["details"][-1]["value"] == .7
    assert history["details"][-1]["removed"] == 1
    assert history["details"][-1]["removal_time_unknown"]
    assert history["max_accounts"] == 2 and out["chart"]["y_max"] == 2
    assert [NOW, None] in history["line"]


@pytest.mark.parametrize("roster_read", ["failed", "cached"])
def test_unread_roster_never_confirms_removal(tmp_path, roster_read):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -1200, {"atlas": .2, "birch": .6})
    reading = data(0, {"atlas": .3}, reads={"catalog": "ok", "accounts": "failed", "quota": "ok"})
    kw = {}
    if roster_read == "cached":
        reading["reads"]["accounts"] = "ok"
        kw["cached"] = {"accounts": NOW - 1200}
    out = result(store, reading, **kw)
    end = out["chart"]["history"]["details"][-1]
    assert end["accounts"] == 2 and end["value"] == 1.1
    assert end["removed"] == 0 and not end["removal_time_unknown"]


def test_left_edge_uses_only_older_retained_seed(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -90000, {"atlas": .2})
    out = result(store, data(0, {"atlas": .7}))
    history = out["chart"]["history"]
    assert history["line"][0] == [NOW - 86400, .8]
    assert history["details"][0]["carried"] == 1
    assert history["details"][0]["age_seconds"] == 3600
    assert history["details"][-1]["value"] == .3
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "run", "sweep", "meta"}


@pytest.mark.parametrize("roster_read", ["current", "cached", "unknown"])
@pytest.mark.parametrize("last_seen", [-90000, -86401])
def test_pre_window_subject_absent_from_known_roster_is_not_carried(tmp_path, roster_read, last_seen):
    store = quota_history.HistoryStore(tmp_path)
    record(store, last_seen, {"atlas": .2, "deleted": .6})
    reading = data(0, {"atlas": .3})
    kw = {}
    if roster_read == "cached":
        kw["cached"] = {"accounts": NOW - 600}
    elif roster_read == "unknown":
        reading["reads"]["accounts"] = "failed"
    chart = result(store, reading, **kw)["chart"]
    history = chart["history"]
    known = roster_read != "unknown"
    assert history["max_accounts"] == chart["y_max"] == (1 if known else 2)
    assert detail_at(history, -3600)["value"] == (.8 if known else 1.2)
    assert history["details"][-1]["value"] == (.7 if known else 1.1)
    assert all(not d["removed"] for d in history["details"])


@pytest.mark.parametrize("last_seen", [-86400, -86399])
def test_absent_subject_seen_inside_window_keeps_its_real_history(tmp_path, last_seen):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -90000, {"atlas": .2, "deleted": .6})
    # The observation is older than the window, but the collector still saw
    # that same reading inside it. Membership depends on last_seen.
    plugin.persist_sweep(store, data(-90000, {"atlas": .2, "deleted": .6}), "", NOW + last_seen)
    history = result(store, data(0, {"atlas": .3}))["chart"]["history"]
    assert detail_at(history, -3600)["value"] == 1.2
    assert history["max_accounts"] == 2
    assert history["details"][-1]["value"] == .7
    assert history["details"][-1]["removed"] == 1
    assert history["details"][-1]["removal_time_unknown"]


def test_known_default_alias_keeps_its_pre_window_seed_as_one_subject(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    roster = [profile("codex", "codex-default")]
    # The existing unified-account contract proves this attribution at
    # collection time; neither an email nor a historical name is guessed.
    legacy = payload([snap("codex", None, [constraint("primary", .2, reset=86400)],
                           observed=-90000)], roster, unified=True, harnesses=("codex",))
    plugin.persist_sweep(store, legacy, "", NOW - 90000)
    current = payload([snap("codex", "codex-default", [constraint("primary", .3, reset=86400)],
                            observed=0)], roster, unified=True, harnesses=("codex",))
    chart = result(store, current)["chart"]
    assert chart["history"]["line"][0] == [NOW - 86400, .8]
    assert chart["history"]["max_accounts"] == chart["y_max"] == 1
    assert chart["history"]["details"][-1]["value"] == .7
    assert not any(d["added"] or d["removed"] for d in chart["history"]["details"])


def test_pre_window_native_key_is_not_a_second_current_default_account(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    native = {"harness_id": "codex", "native_login_detected": True}
    old = payload([snap("codex", None, [constraint("primary", .2, reset=86400)],
                        observed=-90000)], natives=[native], harnesses=("codex",))
    plugin.persist_sweep(store, old, "", NOW - 90000)
    current = payload([snap("codex", None, [constraint("primary", .3, reset=86400)],
                            observed=-1200)], [profile("codex", "codex-default")],
                      unified=True, harnesses=("codex",))
    plugin.persist_sweep(store, current, "", NOW - 1200)
    chart = result(store, current)["chart"]
    # The stored native id has no in-window evidence. Do not revive it as
    # another account, or invent proof to move its value to the named id.
    assert chart["history"]["line"][0] == [NOW - 86400, None]
    assert detail_at(chart["history"], -3600)["accounts"] == 0
    assert chart["history"]["max_accounts"] == chart["y_max"] == 1
    assert chart["history"]["details"][-1]["value"] == .7


@pytest.mark.parametrize("requested_since", [-90000, -85000])
def test_chart_discovery_keeps_the_earliest_requested_lookback(tmp_path, requested_since):
    store = quota_history.HistoryStore(tmp_path)
    for offset, ratio in [(-89000, .1), (-88500, .2), (-87000, .3), (-86000, .4)]:
        record(store, offset, {"atlas": ratio})
    key = qs.group_key("codex", "primary", 604800, ())
    view = store.read(
        lambda salt: {(qs.pseudo_id(salt, "codex", "atlas"), key): NOW + requested_since}, NOW,
        chart_series=(key, NOW - 86400),
    )
    runs = view.runs[(qs.pseudo_id(view.salt, "codex", "atlas"), key)]
    assert view.sweeps_from == NOW + min(requested_since, -86400)
    assert [r.ratio for r in runs] == ([.1, .2, .3, .4] if requested_since == -90000 else [.3, .4])


def test_conflicting_new_sources_keep_old_fact_dated_not_smaller_sum(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -1200, {"atlas": .2, "birch": .6})
    reading = data(-600, {"atlas": .4, "birch": .7})
    reading["quota"].append(snap("codex", "atlas", [constraint("primary", .8, reset=86400)],
                                 source="second", observed=-600))
    plugin.persist_sweep(store, reading, "", NOW - 600)
    history = result(store, reading)["chart"]["history"]
    end = history["details"][-1]
    assert end["accounts"] == 2 and end["value"] == 1.1
    assert end["carried"] >= 1


def test_no_resolved_first_value_leaves_gap_not_partial_sum(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    reading = data(-600, {"atlas": .4, "birch": .7})
    reading["quota"].append(snap("codex", "atlas", [constraint("primary", .8, reset=86400)],
                                 source="second", observed=-600))
    plugin.persist_sweep(store, reading, "", NOW - 600)
    end = result(store, reading)["chart"]["history"]["details"][-1]
    assert end["accounts"] == 2 and end["unknown"] == 1 and end["value"] is None


def test_carry_does_not_teach_pace_and_tool_chart_rates_agree(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -3600, {"atlas": .2})
    record(store, -600, {"atlas": .6})
    reading = data(0, {"atlas": .7})
    out = result(store, reading)
    tool = result(store, reading, chart=False)
    assert out["summary"]["groups"][0]["recent_pace"] == tool["summary"]["groups"][0]["recent_pace"]
    assert out["summary"]["groups"][0]["recent_pace"]["accounts_known"] == 0
    assert detail_at(out["chart"]["history"], -1800)["value"] == .8


def test_display_and_legacy_history_are_separate_and_display_table_is_dated(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    record(store, -1200, {"atlas": .2})
    out = result(store, data(-1200, {"atlas": .2}, stale=("atlas",)))
    chart = out["chart"]
    assert chart["past"][-1][1] is None
    assert chart["history"]["line"][-1][1] == .8
    assert chart["history"]["table"][-1]["carried"] == 1
    assert chart["history"]["table"][-1]["oldest_observed_at"] == qs.iso(NOW - 1200)


def test_older_still_active_source_does_not_make_newer_carry_measured(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    for offset in (-1200, -1080, -960, -840, -720, -600):
        reading = data(-1200, {"atlas": .2})
        if offset in (-1080, -960):
            reading["quota"].append(snap("codex", "atlas", [constraint("primary", .4, reset=86400)],
                                         source="second", observed=-1080))
        plugin.persist_sweep(store, reading, "", NOW + offset)
    history = result(store, data(0, {"atlas": .5}))["chart"]["history"]
    during = detail_at(history, -700)
    assert during["value"] == .6 and during["carried"] == 1
    assert during["measured"] == 0 and during["sources"] == ["second"]
