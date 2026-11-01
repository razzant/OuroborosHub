"""0.7.0: last-known values, facet fallback, the roster, bars and the partial
pace cohort.

Synthetic payloads and simulated collector sweeps only; nothing here measures
a real account, and a passing chart test says nothing about forecast accuracy.
"""

import json

import pytest

import plugin
import quota_history
import quota_summary as qs
from test_reserve import (FIVE_H, NOW, WEEK, _Host, at, constraint, host_at, payload, profile, snap,
                          summary_of, sweep)


def codex(snaps, sids, **kw):
    return payload(snaps, [profile("codex", sid) for sid in sids], harnesses=("codex",), **kw)


def only_group(summary, part="|primary|"):
    found = [g for g in summary["groups"] if part in g["key"]]
    assert len(found) == 1, [g["key"] for g in summary["groups"]]
    return found[0]


def named_summary(data, store, now=NOW, cached=None, reads=None):
    out = plugin.reserve_view(store, data, now, now, chart=True, cached=cached, reads=reads,
                              name_accounts=True)
    return out["summary"], out["chart"]


# ---------------------------------------------------------------------------
# Carry rules: what a reading that is not current may still count for


def test_a_stale_reading_is_carried_dated_and_never_current(tmp_path):
    data = codex([snap("codex", "a", [constraint("primary", 0.2, reset=86400)]),
                  snap("codex", "b", [constraint("primary", 0.6, reset=86400)], fresh=False, observed=-1200)],
                 "ab")
    summary, _chart = named_summary(data, quota_history.HistoryStore(tmp_path))
    g = only_group(summary)
    # The current figure is current readings only — unchanged meaning.
    assert g["measured"] == {"accounts": 1, "windows": 0.8, "average_remaining_pct": 80.0, "at_limit": 0}
    assert g["coverage"]["stale_only"] == 1
    # The stale one is a dated last-known value, counted beside it, not in it.
    assert g["last_known"]["accounts"] == 1 and g["last_known"]["windows"] == 0.4
    assert g["last_known"]["oldest_observed_at"] == at(-1200)
    assert g["with_last_known"] == {"windows": 1.2, "accounts": 2}
    states = [(b["account"], b["state"], b["left"]) for b in g["bars"]]
    assert states == [("codex:a", "current", 0.8), ("codex:b", "last_known", 0.4)]
    assert g["bars"][1]["origin"] == "payload" and g["bars"][1]["age_seconds"] == 1200
    tool = qs.compact(summary)["groups"][0]
    assert tool["remaining_windows"] == 0.8
    assert tool["last_known"] == {"windows": 0.4, "accounts": 1, "oldest_observed_at": at(-1200)}
    assert tool["remaining_windows_with_last_known"] == 1.2
    assert "not current" in tool["headline"]


def test_a_reading_of_an_ended_cycle_is_unknown_and_kept_for_the_record(tmp_path):
    data = codex([snap("codex", "a", [constraint("primary", 0.2, reset=86400)]),
                  snap("codex", "b", [constraint("primary", 0.3, reset=-60)], fresh=False, observed=-7200)],
                 "ab")
    summary, _ = named_summary(data, quota_history.HistoryStore(tmp_path))
    g = only_group(summary)
    bar = g["bars"][-1]
    assert bar["state"] == "unknown" and bar["left"] is None and bar["why"] == "reset_passed"
    assert bar["last_reading"] == {"left": 0.7, "observed_at": at(-7200), "resets_at": at(-60),
                                   "origin": "payload"}
    assert g["last_known"]["accounts"] == 0 and g["with_last_known"]["windows"] == 0.8
    assert g["unknown"] == {"accounts": 1, "reasons": {"reset_passed": 1}}


def test_without_a_reported_reset_a_reading_is_carried_for_its_window_only(tmp_path):
    def status(age):
        return codex([snap("codex", "a", [constraint("primary", 0.5, window=FIVE_H)], fresh=False,
                           observed=-age)], "a")
    within, _ = named_summary(status(FIVE_H - 60), quota_history.HistoryStore(tmp_path / "1"))
    beyond, _ = named_summary(status(FIVE_H + 60), quota_history.HistoryStore(tmp_path / "2"))
    assert within["groups"][0]["bars"][0]["state"] == "last_known"
    assert beyond["groups"][0]["bars"][0]["state"] == "unknown"
    assert beyond["groups"][0]["bars"][0]["why"] == "too_old"


def test_stale_sources_that_disagree_give_no_last_known_value(tmp_path):
    data = codex([snap("codex", "a", [constraint("primary", 0.2, reset=86400)], source="x",
                       fresh=False, observed=-600),
                  snap("codex", "a", [constraint("primary", 0.6, reset=86400)], source="y",
                       fresh=False, observed=-600)], "a")
    summary, _ = named_summary(data, quota_history.HistoryStore(tmp_path))
    bar = summary["groups"][0]["bars"][0]
    assert bar["state"] == "unknown" and bar["left"] is None


def test_unknown_is_never_zero_anywhere(tmp_path):
    data = codex([snap("codex", "a", [constraint("primary", 0.2, reset=86400)]),
                  snap("codex", "b", [constraint("primary", 1.4, reset=86400)])], "ab")
    summary, chart = named_summary(data, quota_history.HistoryStore(tmp_path))
    g = only_group(summary)
    unknown = [b for b in g["bars"] if b["state"] == "unknown"]
    assert len(unknown) == 1 and unknown[0]["why"] == "unreadable" and unknown[0]["left"] is None
    assert g["measured"]["windows"] == 0.8 and g["with_last_known"]["windows"] == 0.8
    assert chart["y_max"] == 2 and chart["accounts"] == 1


# ---------------------------------------------------------------------------
# Facet fallback: each facet on its own, never the whole payload


def _healthy():
    return codex([snap("codex", sid, [constraint("primary", used, reset=86400)])
                  for sid, used in (("a", 0.2), ("b", 0.5), ("c", 0.9))], "abc")


def test_a_failed_quota_facet_shows_the_last_quota_answer_as_last_known(tmp_path):
    latest = plugin.LatestRead()
    latest.put(_healthy(), "", NOW - 300)
    now_read = codex([], "abc", reads={"catalog": "ok", "accounts": "ok", "quota": "failed"})
    effective, cached = latest.effective(now_read)
    assert cached == {"quota": NOW - 300}
    assert all(row["freshness"] == "stale" for row in effective["quota"])
    reads = plugin.facet_states(now_read)
    summary, _ = named_summary(effective, quota_history.HistoryStore(tmp_path), cached=cached, reads=reads)
    g = only_group(summary)
    assert summary["reads"]["quota"] == "failed"          # what was read stays said
    assert summary["cached"] == {"quota": qs.iso(NOW - 300)}
    assert g["measured"]["accounts"] == 0                  # nothing current is claimed
    assert [b["state"] for b in g["bars"]] == ["last_known"] * 3
    assert {b["origin"] for b in g["bars"]} == {"cached"}
    assert g["last_known"]["windows"] == pytest.approx(1.4)
    assert qs.compact(summary)["cached_facets"] == {"quota": qs.iso(NOW - 300)}
    # The account view shows the same readings as stale, never "not checked".
    view = plugin.build_view(effective, "", NOW, reads=reads, cached=cached)
    assert view["facets"]["quota"] == "failed" and view["complete"] is False
    assert view["cached"] == {"quota": qs.iso(NOW - 300)}
    quotas = [a["quota"] for a in view["groups"][0]["accounts"]]
    assert all(q["state"] == "no_fresh_window" for q in quotas)


def test_a_failed_status_read_keeps_every_facet_from_the_last_answer(tmp_path):
    latest = plugin.LatestRead()
    latest.put(_healthy(), "", NOW - 120)
    effective, cached = latest.effective(None)
    assert set(cached) == {"catalog", "accounts", "quota"}
    view = plugin.build_view(effective, "HTTP 503 from /api/claudexor/status", NOW,
                             reads=plugin.facet_states(None), cached=cached)
    assert view["ok"] is False and view["complete"] is False
    assert set(view["facets"].values()) == {"indeterminate"}
    assert [a["subject_id"] for a in view["groups"][0]["accounts"]] == ["a", "b", "c"]
    assert all(a["verification"]["label"].endswith("— last known") for a in view["groups"][0]["accounts"])
    assert view["daemon"]["state"] == ""                    # not claimed either way
    summary, chart = named_summary(effective, quota_history.HistoryStore(tmp_path), cached=cached,
                                   reads=plugin.facet_states(None))
    assert summary["roster"] == "cached"
    g = only_group(summary)
    # A kept roster says which accounts there are, never their state now.
    assert all("account_state_unknown" in b["flags"] for b in g["bars"])
    assert chart["past_basis"] in ("last_known", "none")


def test_without_an_earlier_answer_nothing_is_invented(tmp_path):
    latest = plugin.LatestRead()
    effective, cached = latest.effective(None)
    assert effective is None and cached == {}
    now_read = codex([], (), reads={"catalog": "ok", "accounts": "failed", "quota": "failed"})
    effective, cached = latest.effective(now_read)
    assert cached == {}
    summary, _ = named_summary(effective, quota_history.HistoryStore(tmp_path), reads=plugin.facet_states(now_read))
    assert summary["roster"] == "unknown" and summary["groups"] == []


def test_a_current_roster_wins_over_kept_quota_readings(tmp_path):
    latest = plugin.LatestRead()
    latest.put(_healthy(), "", NOW - 300)
    # Account c was deleted in Claudexor; the quota facet failed this time.
    now_read = codex([], "ab", reads={"catalog": "ok", "accounts": "ok", "quota": "failed"})
    effective, cached = latest.effective(now_read)
    assert "accounts" not in cached
    summary, _ = named_summary(effective, quota_history.HistoryStore(tmp_path), cached=cached,
                               reads=plugin.facet_states(now_read))
    g = only_group(summary)
    assert [b["account"] for b in g["bars"]] == ["codex:a", "codex:b"]   # c is not brought back
    assert summary["unattributed_readings"] == {"codex": 1}


def test_a_confirmed_empty_roster_is_not_replaced_by_a_kept_one(tmp_path):
    latest = plugin.LatestRead()
    latest.put(_healthy(), "", NOW - 300)
    now_read = codex([], (), reads={"catalog": "ok", "accounts": "ok", "quota": "failed"})
    effective, cached = latest.effective(now_read)
    assert "accounts" not in cached and effective["profiles"]["profiles"] == []
    summary, _ = named_summary(effective, quota_history.HistoryStore(tmp_path), cached=cached,
                               reads=plugin.facet_states(now_read))
    assert summary["groups"] == [] and summary["roster"] == "current"


def test_fresh_account_state_still_holds_over_kept_quota(tmp_path):
    latest = plugin.LatestRead()
    latest.put(_healthy(), "", NOW - 300)
    roster = [profile("codex", "a"), profile("codex", "b", enabled=False), profile("codex", "c")]
    now_read = payload([], roster, harnesses=("codex",),
                       reads={"catalog": "ok", "accounts": "ok", "quota": "failed"})
    effective, cached = latest.effective(now_read)
    summary, _ = named_summary(effective, quota_history.HistoryStore(tmp_path), cached=cached,
                               reads=plugin.facet_states(now_read))
    bars = {b["account"]: b for b in only_group(summary)["bars"]}
    assert "disabled" in bars["codex:b"]["flags"] and bars["codex:b"]["restricted"]
    assert not bars["codex:a"]["restricted"]


def test_a_failed_read_is_never_answered_from_an_older_reusable_read():
    latest = plugin.LatestRead()
    latest.put(_healthy(), "", NOW - 30)
    assert latest.get(45) is not None
    latest.put(None, "HTTP 503", NOW)
    assert latest.get(45) is None                     # reuse does not skip a newer failure
    effective, cached = latest.effective(None)
    assert set(cached) == {"catalog", "accounts", "quota"}  # the dated facets stay
    # An older read landing late does not replace a newer facet.
    latest.put(_healthy(), "", NOW + 60)
    older = _healthy()
    older["quota"][0]["constraints"][0]["used_ratio"] = 0.99
    latest.put(older, "", NOW + 10)
    _, cached = latest.effective(codex([], "abc", reads={"catalog": "ok", "accounts": "ok", "quota": "failed"}))
    assert cached["quota"] == NOW + 60


# ---------------------------------------------------------------------------
# The history: last-known by exact subject and series, for the current roster


def test_a_roster_account_without_a_reading_gets_its_history_value(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timelines = {"a": [(-3000, 0.10, 2 * 86400)], "b": [(-3000, 0.40, 2 * 86400)]}
    sweep(store, timelines, -3000, -1800)
    # Now the answer carries a reading of a only; b is in the roster.
    data = host_at(NOW, {"a": timelines["a"]})
    data["profiles"]["profiles"].append(profile("codex", "b"))
    data["profiles"]["profiles"].append(profile("codex", "c"))
    summary, chart = named_summary(data, store)
    g = only_group(summary)
    bars = {b["account"]: b for b in g["bars"]}
    assert bars["codex:b"]["state"] == "last_known" and bars["codex:b"]["origin"] == "history"
    assert bars["codex:b"]["left"] == pytest.approx(0.6)
    assert bars["codex:b"]["observed_at"] == at(-3000)     # the source's own observation time
    assert "codex:c" not in bars                           # no evidence: applicability unknown
    assert g["slots"] == 2 and g["applicability_unknown"] == 1
    assert g["coverage"]["other_family_accounts"] == 2     # the old count keeps its meaning
    assert chart["y_max"] == 2


def test_history_attaches_by_exact_subject_never_by_e_mail(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    sweep(store, {"a": [(-3000, 0.10, 2 * 86400)]}, -3000, -1800)
    data = codex([], ())
    data["profiles"]["profiles"] = [profile("codex", "a2", email="same@example.test")]
    summary, _ = named_summary(data, store)
    assert summary["groups"] == []


def test_the_history_reader_finds_the_newest_run_and_writes_nothing(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    sweep(store, {"a": [(-3000, 0.10, 2 * 86400), (-1500, 0.30, 2 * 86400)]}, -3000, -600)
    before = store.path.read_bytes()
    key = qs.group_key("codex", "primary", WEEK, ())
    view = store.read(lambda salt: {}, NOW,
                      latest=lambda salt: [(qs.pseudo_id(salt, "codex", "a"), key),
                                           (qs.pseudo_id(salt, "codex", "zz"), key)])
    # The newest run per source of each pair found (one source here).
    assert len(view.latest) == 1
    runs = next(iter(view.latest.values()))
    assert len(runs) == 1 and runs[0].ratio == pytest.approx(0.30)
    assert store.path.read_bytes() == before


# ---------------------------------------------------------------------------
# Bars: one order, by value; a freshness flap does not move an account


def test_a_freshness_flap_with_the_same_value_keeps_the_order(tmp_path):
    def status(stale_b):
        return codex([snap("codex", "a", [constraint("primary", 0.30, reset=86400)]),
                      snap("codex", "b", [constraint("primary", 0.50, reset=86400)], fresh=not stale_b),
                      snap("codex", "c", [constraint("primary", 0.70, reset=86400)])], "abc")
    one, _ = named_summary(status(False), quota_history.HistoryStore(tmp_path / "1"))
    two, _ = named_summary(status(True), quota_history.HistoryStore(tmp_path / "2"))
    order = lambda s: [b["account"] for b in s["groups"][0]["bars"]]
    assert order(one) == order(two) == ["codex:a", "codex:b", "codex:c"]
    assert [b["state"] for b in two["groups"][0]["bars"]] == ["current", "last_known", "current"]
    assert one["groups"][0]["slots"] == two["groups"][0]["slots"] == 3


def test_bars_carry_account_keys_only_for_the_widget_route(tmp_path):
    data = _healthy()
    plain = plugin.reserve_view(quota_history.HistoryStore(tmp_path), data, NOW, NOW, chart=False)
    assert all("account" not in b for b in plain["summary"]["groups"][0]["bars"])
    tool = qs.compact(plain["summary"], detail=True)
    text = json.dumps(tool)
    assert "codex:a" not in text and '"bars"' not in text


# ---------------------------------------------------------------------------
# The partial pace cohort


def test_the_estimate_sums_the_qualified_cohort_and_stops_at_its_first_reset(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timelines = {"a": [(-3600, 0.10, 5 * 3600), (0, 0.16, 5 * 3600)],
                 "b": [(-3600, 0.40, 2 * 86400), (-60, 0.46, 2 * 86400)],
                 "c": [(-600, 0.50, 2 * 86400)]}  # appeared ten minutes ago: warming up
    sweep(store, timelines, -4080, 0)
    data = host_at(NOW, timelines)
    out = plugin.reserve_view(store, data, NOW, NOW, horizon="24h", name_accounts=True)
    chart, g = out["chart"], out["summary"]["groups"][0]
    assert g["recent_pace"]["state"] == "partial"
    scope = chart["recent_pace_scope"]
    assert (scope["accounts"], scope["of"], scope["slots"]) == (2, 3, 3)
    assert scope["stops_at_reset"] and scope["until"] == round(NOW + 5 * 3600)
    # c is not held constant inside the line: it starts at a + b only.
    assert chart["recent_pace"][0][1] == pytest.approx(0.84 + 0.54)
    assert chart["recent_pace"][-1][0] == round(NOW + 5 * 3600)
    assert chart["cohort_past"] is not None
    assert chart["recent_pace_refill_scenario"][-1][0] == chart["end"]
    # Per account: its own pace, and when it would reach the limit only if
    # before its reported reset — conditional on the pace continuing.
    bars = {b["account"]: b for b in g["bars"]}
    assert bars["codex:a"]["pace"]["state"] == "ok"
    assert bars["codex:c"]["pace"]["state"] in ("warming_up", "insufficient")
    assert all("reaches_limit_at" not in b.get("pace", {}) or b["pace"]["state"] == "ok" for b in g["bars"])


def test_a_small_observed_change_is_counted_and_said(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timelines = {"a": [(-3600, 0.90, 30 * 86400), (0, 0.91, 30 * 86400)]}
    sweep(store, timelines, -4080, 0)
    out = plugin.reserve_view(store, host_at(NOW, timelines), NOW, NOW, chart=False, name_accounts=True)
    g = out["summary"]["groups"][0]
    pace = g["recent_pace"]
    assert pace["exhaust_before_reset"] == 1 and pace["exhaust_before_reset_small_change"] == 1
    assert g["bars"][0]["pace"]["change"] == pytest.approx(0.01)
    assert "reaches_limit_at" in g["bars"][0]["pace"]
    tool = qs.compact(out["summary"])["groups"][0]
    assert tool["recent_pace"]["exhaust_before_reset_small_change"] == 1
    assert "if the trend continued" in tool["headline"]


def test_during_an_outage_the_chart_draws_the_record_of_the_last_known_accounts(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timelines = {"a": [(-3000, 0.10, 2 * 86400)], "b": [(-3000, 0.40, 2 * 86400)]}
    sweep(store, timelines, -3000, -600)
    latest = plugin.LatestRead()
    latest.put(host_at(NOW - 600, timelines), "", NOW - 600)
    effective, cached = latest.effective(None)
    out = plugin.reserve_view(store, effective, None, NOW, cached=cached, reads=plugin.facet_states(None),
                              name_accounts=True)
    chart = out["chart"]
    assert chart["past_basis"] == "last_known" and chart["past_accounts"] == 2
    values = [v for _t, v in chart["past"] if v is not None]
    assert values and values[0] == pytest.approx(1.5)
    assert chart["past"][-1][1] is None                 # never a value "now"
    assert chart["recent_pace"] is None


# ---------------------------------------------------------------------------
# The route and the tool, end to end over the registered handlers


def test_the_route_after_a_failed_read_answers_with_dated_last_known_values(tmp_path, monkeypatch):
    answers = [(_healthy(), "", 200), (None, "HTTP 503 from /api/claudexor/status", 503)]
    monkeypatch.setattr(plugin, "_request_json", lambda *a, **k: answers.pop(0) if answers else (None, "down", 0))
    monkeypatch.setattr(plugin.time, "time", lambda: NOW)
    host = _Host(tmp_path)
    plugin.register(host)
    first = host.routes["quotas"]({"query_params": {"chart": "0"}})
    assert first["complete"] is True and first["cached"] == {}
    second = host.routes["quotas"]({"query_params": {"chart": "0"}})
    assert second["ok"] is False and second["complete"] is False
    assert set(second["cached"]) == {"catalog", "accounts", "quota"}
    g = second["reserve"]["summary"]["groups"][0]
    assert [b["state"] for b in g["bars"]] == ["last_known"] * 3
    assert all(b["account"].startswith("codex:") for b in g["bars"])
    tool = json.loads(host.tools["quota_summary"]["handler"](None))
    assert "status_error" in tool and set(tool["cached_facets"]) == {"catalog", "accounts", "quota"}
    assert tool["groups"][0]["remaining_windows"] == 0           # nothing current is claimed
    assert tool["groups"][0]["last_known"]["accounts"] == 3
