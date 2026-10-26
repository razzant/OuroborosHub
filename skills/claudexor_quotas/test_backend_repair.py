"""Bounded backend repair regressions; exercise real answers, not imagined wire."""
import copy
import json

import pytest
import plugin
import quota_context as qc
import quota_scope as scope
import quota_joint as qj
import quota_history
import quota_summary as qs
import quota_analytics as qa
from test_scope_resources_context import RESOURCES, _answer, _Api
from test_reserve import FIVE_H, WEEK, NOW, constraint, payload, profile, snap
from test_joint_view import fed_store, at_time, START


@pytest.fixture(scope="module")
def watched_store(tmp_path_factory):
    # Immutable readers share one actual collector-fed store; don't fsync the
    # same 241 synthetic sweeps independently for every new read regression.
    return fed_store(tmp_path_factory.mktemp("backend-watched"))


def test_reset_caps_snapshot_even_inside_one_sweep():
    body = qc.envelope("x", 1000, 1060.125, lambda t: t)
    assert body["valid_until"] == 1060.125
    # A passed reset must not renew a fresh claim, nor produce an invalid envelope.
    with pytest.raises(ValueError):
        qc.envelope("x", 1000, 1000, lambda t: t)


def test_real_answer_snapshot_prints_numeric_pace_and_partial_cohort(watched_store):
    data = at_time(NOW)
    answer = plugin.answer_of(watched_store, data, {}, data, NOW, NOW)
    text = qc.snapshot_text(answer)
    row = next(r for r in answer["groups"] if r["recent_pace"]["accounts_known"])
    pace = row["recent_pace"]
    assert f"{qc._rate(pace['windows_per_hour'])} account-windows/h" in text  # derived resolution, one decimal
    assert f"{pace['accounts_known']} of {pace['of']}" in text
    assert "watched" in text
    assert "request scope not stated" in text  # no echo is not an all-model guarantee


def test_resource_only_family_is_in_real_answer_and_dated_snapshot(tmp_path):
    data = payload([], [profile("cursor", "c1")], harnesses=("cursor",))
    data.update(copy.deepcopy(RESOURCES))
    data["availability_scope"]["basis"] = "quota_block"
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    effective, cached = latest.effective(data)
    answer = plugin.answer_of(None, effective, cached, data, NOW, NOW)
    assert not answer["groups"]
    assert answer["resources"]["cursor"]["accounts"] == 1
    text = qc.snapshot_text(answer)
    assert "Cursor" in text or "cursor" in text
    assert "no quota-window reading" in text
    assert "Included 0 USD" in text and "team" in text
    assert "2026-10-15T10:00:00Z" in text and "last known" in text
    assert "spending not reported" in text
    assert "c1" not in text


def test_small_positive_hours_do_not_print_as_zero():
    assert qc.rounded_hours(0.25) not in ("0", "0.0", "0.00")
    assert qc.rounded_hours(0.001) not in ("0", "0.0", "0.00")


@pytest.mark.parametrize("aliases,prefixes,model,expected", [
    (["model-a"], [], "model-a", True),
    (["model-a"], [], "model-a-future", True),
    (["model-a"], [], "model-a[1m]", True),
    (["prefix-model-a"], [], "model-a", True),
    (["Model-A"], [], "model-a", True),
    (["model-a"], [], "different", False),
    ([], ["model-"], "model-future", True),
    ([], ["Model-"], "model-future", False),  # prefixes remain literal, unlike aliases
    ([], [], "model-a", True),
])
def test_scope_matches_quota_block_goldens(aliases, prefixes, model, expected):
    given = scope.scope_of({"applies_to_models": aliases, "applies_to_model_prefixes": prefixes})
    assert scope.applies(given, model) is expected


def test_unknown_scope_never_allows_positive_joint_or_pace(watched_store):
    data = at_time(NOW)
    data["quota"][0]["constraints"].append(dict(constraint("unreadable", .2, WEEK),
                                                 applies_to_models={"bad": True}))
    result = plugin.reserve_view(watched_store, data, NOW, NOW, harness="claude", analytics=True)
    analytics = result["chart"]["analytics"]
    assert analytics["scope"]["unreadable_windows"] == 1
    assert analytics["joint"]["figure"] in (None, 0)
    assert analytics["window"]["scenarios"]["recent_pace"]["at_pace"] == 0


def test_history_does_not_use_later_calibration_for_an_earlier_point(watched_store):
    result = plugin.reserve_view(watched_store, at_time(NOW), NOW, NOW, harness="claude", analytics=True)
    hist = result["chart"]["analytics"]["joint"]["history"]
    # a's positive capacity cannot be reconstructed from the 8h ratio at its first sighting.
    # c's observed weekly exhaustion is independently a known zero.
    first = next((v for t, v in hist["line"] if t == START), None)
    assert first in (None, 0)


def test_no_rate_and_known_exhaustion_order_and_burn_freeze():
    assert qj.work_hours([qj.Win("s", .6, rate=None, unit=True),
                          qj.Win("w", 0, rate=0)]).value == 0
    assert qj.work_hours([qj.Win("s", .6, rate=.1), qj.Win("w", .1, rate=None)]).value is None
    tr = qj.simulate([qj.Win("s", .6, rate=.1, unit=True), qj.Win("w", 0, reset=2, rate=.1, k=1)], 0, 3, True)
    assert tr.value("s", 1) == .6 and tr.value("s", 3) == pytest.approx(.5)


def test_earliest_reset_compares_instants_and_publish_cannot_extend_one(tmp_path):
    answer = {"groups": [{"next_reset": {"at": "2026-10-15T11:00:00+02:00"}},
                          {"next_reset": {"at": "2026-10-15T10:00:00Z"}}]}
    assert qc.earliest_reset(answer) == "2026-10-15T11:00:00+02:00"
    assert qc.publish(tmp_path, "same", 1000, 1060.125, qs.iso_exact, qs.parse_instant) == "written"
    body = json.loads((tmp_path/qc.SNAPSHOT_FILE).read_text())
    assert qs.parse_instant(body["valid_until"]) == 1060.125
    assert qc.publish(tmp_path, "same", 1060.125, 1060.125, qs.iso_exact, qs.parse_instant) == "skipped"
    assert json.loads((tmp_path/qc.SNAPSHOT_FILE).read_text()) == body


def test_single_window_uses_its_measured_rate_not_a_nonexistent_joint_calibration():
    acct = qa._Account(("x", "a"), "p")
    acct.unit = "w"
    reading = qs.reading_of(constraint("w", .2, FIVE_H), "x", "a", source="s", fresh=True, observed=NOW)
    acct.members["w"] = qs.Member("x", "a", "measured", reading, .8,
                                   rate={"windows_per_hour": .1})
    wins = acct.wins(["w"])
    assert wins[0].rate == pytest.approx(.1 / 3600)
    assert qj.simulate(wins, NOW, NOW+3600, True).value("w", NOW+3600) == pytest.approx(.7)


def test_unknown_offer_eligibility_is_not_reported_usable():
    import quota_resources as qr
    data = copy.deepcopy(RESOURCES)
    data["resources"][0]["resets"]["freshness"] = "fresh"
    data["resources"][0]["resets"]["value"][0]["usable_now"] = None
    row = qr.family_resets(data, "cursor")
    assert row["available"] == 0 and row["offers_uncounted"] == 1


def test_selected_nonbinding_window_does_not_burn_for_another_request_kind():
    data = payload([snap("claude", "a", [dict(constraint("text-only", .2, FIVE_H, reset=7200),
                                               applies_to_request_kinds=["text"])])],
                   [profile("claude", "a")], harnesses=("claude",))
    norm, groups = qs.prepare(data, NOW)
    state = qs.finish(norm, groups, qs.HistoryView(state="empty"), NOW)
    # Explicit observed pace as algebra input: no calibration is needed for a
    # single window. The query is for image work, which cannot consume it.
    groups[0].members[0].rate = {"windows_per_hour": .1}
    result = qa.build(state, groups[0], kind="image")
    own = result["window"]
    pace = own["scenarios"]["recent_pace"]
    assert pace["at_pace"] == 0
    assert pace["checkpoints"][0]["value"] == pytest.approx(.8)
    assert own["scope_applies"] is False


def test_resource_cents_are_not_relabelled_as_whole_currency():
    import quota_resources as qr
    data = copy.deepcopy(RESOURCES)
    raw = data["resources"][0]["balances"]["value"][0]
    raw.update(amount="2345678", unit="cents", currency="USD", decimal_places=2)
    answer = {"resources": qr.family_resources(data)}
    got = answer["resources"]["cursor"]["facets"]["balances"][0]["rows"][0]
    assert got["decimal_places"] == 2 and got["unit"] == "cents"
    text = qc.snapshot_text(answer)
    assert "2345678 cents [USD]" in text and "2345678 USD" not in text


@pytest.mark.parametrize("raw,scale,expected", [
    ("2345678", 2, "23456.78 USD"),
    ("9007199254740993", 2, "90071992547409.93 USD"),
    ("1", 2, "0.01 USD"),
    ("0", 2, "0.00 USD"),
    ("-15", 2, "-0.15 USD"),
])
def test_declared_minor_units_are_scaled_without_float(raw, scale, expected):
    import quota_resources as qr
    data = copy.deepcopy(RESOURCES)
    data["resources"][0]["balances"]["value"][0].update(
        amount=raw, unit="minor", currency="USD", decimal_places=scale)
    text = qc.snapshot_text({"resources": qr.family_resources(data)})
    assert expected in text


@pytest.mark.parametrize("facet,row", [
    ("balances", {"period": {"ends_at": "2026-10-15T13:05:58.125Z"}}),
    ("spending", {"resets_at": "2026-10-15T13:05:58.125Z"}),
    ("resets", {"resets_at": "2026-10-15T13:05:58.125Z"}),
])
def test_current_resource_cycle_also_caps_snapshot_validity(facet, row):
    answer = {"resources": {"cursor": {"facets": {facet: [{"state": "current", "rows": [row]}]}}}}
    assert qc.earliest_reset(answer) == "2026-10-15T13:05:58.125Z"
    # A dated last-known period is information only, not a current claim.
    answer["resources"]["cursor"]["facets"][facet][0]["state"] = "last_known"
    assert qc.earliest_reset(answer) is None
