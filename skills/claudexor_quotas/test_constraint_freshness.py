"""Per-constraint freshness from the engine's opted-in quota read.

The host's passive quota view passes on ``GET /v2/quota?view=constraint_freshness``:
every constraint then carries its own ``freshness``. One resolver
(quota_summary.constraint_freshness) answers it for the reserve, the tool, the
account view, cooldowns, reset credits and the collector. All responses and
accounts are synthetic; registered handlers run against a stubbed HTTP
boundary and no running host is contacted.
"""

import copy
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import plugin
import quota_history
import quota_summary as qs
import test_quotas
from test_passive_quota_read import route, stub
from test_reserve import FIVE_H, NOW, WEEK, _Host, _node, constraint, profile, snap

FIVE_KEY, WEEK_KEY = "codex|secondary|18000|-", "codex|primary|604800|-"


def engine_snapshot(sid="fixture-a", *, aggregate="stale", five="stale", week="fresh",
                    credits="fresh", observed=-150.0, five_reset=-30):
    """The contract's example: observed 150 s ago, the 5-hour reset has just
    passed (the snapshot as a whole is stale), the weekly reset is tomorrow.
    ``None`` leaves a constraint without its own word."""
    row = snap("codex", sid, [constraint("secondary", .7, FIVE_H, reset=five_reset),
                              constraint("primary", .4, WEEK, reset=86400),
                              {"id": "reset_credits", "label": "2 reset credits",
                               "used_ratio": None, "window_seconds": None}],
               observed=observed)
    row["freshness"] = aggregate
    for item, word in zip(row["constraints"], (five, week, credits)):
        if word is not None:
            item["freshness"] = word
    return row


def legacy(row):
    out = copy.deepcopy(row)
    for item in out["constraints"]:
        item.pop("freshness", None)
    return out


def envelope(rows, subjects=("fixture-a",), quota="ok"):
    return {
        "view": "quota",
        "reads": {"catalog": "not_read", "accounts": "ok", "quota": quota},
        "profiles": {"profiles": [profile("codex", sid) for sid in subjects], "accountPools": []},
        "unified_accounts": True,
        "quota": rows if quota == "ok" else [],
        "quota_absences": [],
        "timings_ms": {}, "read_errors": {},
    }


def group(summary, key):
    found = [g for g in summary["groups"] if g["key"] == key]
    assert len(found) == 1, [g["key"] for g in summary["groups"]]
    return found[0]


def tool_group(answer, key):
    return next(g for g in answer["groups"] if g["key"] == key)


def account_quota(view, sid="fixture-a"):
    return next(a for g in view["groups"] for a in g["accounts"] if a["subject_id"] == sid)["quota"]


def read_everything(tmp_path, monkeypatch, data):
    """The registered widget route and model tool on one passive answer."""
    original = copy.deepcopy(data)
    stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    view = route(host)
    answer = json.loads(host.tools["quota_summary"]["handler"](None, harness="codex"))
    assert data == original, "the received answer is never changed"
    return view, answer


# ---------------------------------------------------------------------------
# The resolver


def test_the_projection_is_judged_over_the_whole_answer():
    explicit = engine_snapshot()
    empty = dict(engine_snapshot("fixture-b"), constraints=[])
    assert qs.freshness_projection([legacy(explicit)]) == "legacy"
    assert qs.freshness_projection([]) == qs.freshness_projection(None) == "legacy"
    assert qs.freshness_projection([empty]) == "legacy", "an empty list carries no evidence"
    assert qs.freshness_projection([explicit]) == "explicit"
    assert qs.freshness_projection([explicit, empty]) == "explicit", "no invented annotation needed"
    assert qs.freshness_projection([engine_snapshot(five=None)]) == "invalid", "partial"
    assert qs.freshness_projection([explicit, legacy(engine_snapshot("fixture-b"))]) == "invalid", "mixed"
    for word in ("FRESH", "", None, 1, "expired", ["fresh"]):
        assert qs.freshness_projection([engine_snapshot(week=word)]) == "invalid", word
    with_item = engine_snapshot()
    with_item["constraints"].append("not an object")
    assert qs.freshness_projection([with_item]) == "invalid"
    for listed in (None, {}, "fresh"):
        assert qs.freshness_projection([explicit, dict(legacy(explicit), constraints=listed)]) == "invalid"


def test_one_resolver_answers_each_window():
    row = engine_snapshot()
    five, week, credits = row["constraints"]
    assert [qs.constraint_freshness(row, c, "explicit") for c in (five, week, credits)] == \
        ["stale", "fresh", "fresh"]
    # Legacy: the snapshot's conservative word for every window, whatever else it carries.
    assert [qs.constraint_freshness(row, c, "legacy") for c in (five, week)] == ["stale", "stale"]
    assert qs.constraint_freshness(dict(row, freshness="fresh"), {}, "legacy") == "fresh"
    assert qs.constraint_freshness(dict(row, freshness="expired"), {}, "legacy") == "unknown"
    # Invalid: nothing is fresh, and nothing is stripped back to legacy.
    assert qs.constraint_freshness(dict(row, freshness="fresh"), week, "invalid") == "unknown"
    # A fresh window under a snapshot word the engine never ages into it.
    for aggregate in ("unknown", None, "", "FRESH"):
        assert qs.constraint_freshness(dict(row, freshness=aggregate), week, "explicit") == "unknown"
    assert qs.constraint_freshness(dict(row, freshness="fresh"), five, "explicit") == "stale"


# ---------------------------------------------------------------------------
# Summary, tool, account view and reset credits on one answer


def test_a_fresh_weekly_window_counts_beside_its_stale_five_hour_sibling(tmp_path, monkeypatch):
    view, answer = read_everything(tmp_path, monkeypatch, envelope([engine_snapshot()]))
    summary = view["reserve"]["summary"]
    week, five = group(summary, WEEK_KEY), group(summary, FIVE_KEY)
    assert (week["measured"]["accounts"], week["measured"]["windows"]) == (1, 0.6)
    assert five["measured"]["accounts"] == 0
    assert not (five.get("last_known") or {}).get("accounts"), "an ended cycle is never carried"
    assert tool_group(answer, WEEK_KEY)["remaining_windows"] == 0.6
    assert tool_group(answer, FIVE_KEY)["remaining_windows"] == 0
    assert "no current reading" in tool_group(answer, FIVE_KEY)["headline"]
    assert "0.6 account-windows left now" in tool_group(answer, WEEK_KEY)["headline"]

    quota = account_quota(view)
    assert (quota["state"], quota["label"]) == ("ok", "40% used")
    assert [c["id"] for c in quota["constraints"]] == ["primary", "reset_credits"]
    assert [(s["freshness"], [c["id"] for c in s["constraints"]]) for s in quota["stale"]] == \
        [("stale", ["secondary"])]
    credits = quota["reset_credits"]
    assert (credits["state"], credits["count"], credits["reason"]) == ("current", 2, "")
    assert credits["reports"][0]["freshness"] == "fresh"
    assert view["groups"][0]["reset_credits"]["current_accounts"] == 1


@pytest.mark.parametrize("counter_only", [True, False], ids=["credits", "cooldown"])
def test_fresh_non_window_facts_do_not_hide_stale_quota_windows(tmp_path, monkeypatch, counter_only):
    row = engine_snapshot(five="stale", week="stale", credits="fresh")
    if not counter_only:
        row["constraints"][-1] = {"id": "cooldown", "label": "Cooling down",
                                   "used_ratio": None, "window_seconds": None,
                                   "cooldown_until": qs.iso(NOW + 60), "freshness": "fresh"}
    view, answer = read_everything(tmp_path, monkeypatch, envelope([row]))
    quota = account_quota(view)
    assert quota["state"] == ("no_fresh_window" if counter_only else "cooling")
    assert quota["constraints"] == []
    assert quota["stale"], "reported stale percentages are retained"
    assert group(view["reserve"]["summary"], WEEK_KEY)["measured"]["accounts"] == 0
    assert tool_group(answer, WEEK_KEY)["remaining_windows"] == 0
    if counter_only:
        assert quota["reset_credits"]["state"] == "current"
    else:
        assert quota["cooldowns"]


def test_absent_nested_metadata_keeps_the_conservative_legacy_answer(tmp_path, monkeypatch):
    view, answer = read_everything(tmp_path, monkeypatch, envelope([legacy(engine_snapshot())]))
    week = group(view["reserve"]["summary"], WEEK_KEY)
    assert week["measured"]["accounts"] == 0
    assert week["last_known"]["accounts"] == 1, "dated and carried, never current"
    assert tool_group(answer, WEEK_KEY)["remaining_windows"] == 0
    quota = account_quota(view)
    assert quota["state"] == "no_fresh_window" and quota["constraints"] == []
    assert [c["id"] for c in quota["stale"][0]["constraints"]] == ["secondary", "primary", "reset_credits"]
    assert (quota["reset_credits"]["state"], quota["reset_credits"]["reason"]) == ("last_known", "not_fresh")


@pytest.mark.parametrize("case", ["partial", "unknown_word", "mixed_snapshots", "contradiction"])
def test_partial_or_malformed_metadata_is_never_fresh(tmp_path, monkeypatch, case):
    rows = {
        "partial": [engine_snapshot(five=None)],
        "unknown_word": [engine_snapshot(five="expired")],
        "mixed_snapshots": [engine_snapshot(), legacy(engine_snapshot("fixture-b", aggregate="fresh"))],
        "contradiction": [engine_snapshot(aggregate="unknown")],
    }[case]
    view, answer = read_everything(tmp_path, monkeypatch, envelope(rows, ("fixture-a", "fixture-b")))
    assert "freshness" in rows[0]["constraints"][1], "nothing is stripped from the answer"
    week = group(view["reserve"]["summary"], WEEK_KEY)
    assert week["measured"]["accounts"] == 0
    assert tool_group(answer, WEEK_KEY)["remaining_windows"] == 0
    for sid in ("fixture-a", "fixture-b"):
        quota = account_quota(view, sid)
        assert quota["constraints"] == [] and quota["state"] != "ok", (sid, quota["state"])
        assert quota["reset_credits"]["state"] != "current"
        assert all(s["freshness"] != "fresh" for s in quota["stale"])


# ---------------------------------------------------------------------------
# A failed read answered from the kept quota facet


def test_a_failed_read_ages_every_window_and_leaves_the_kept_answer_intact(tmp_path, monkeypatch):
    kept = envelope([engine_snapshot(aggregate="fresh", five="fresh", five_reset=3600)])
    original = copy.deepcopy(kept)
    latest = plugin.LatestRead()
    latest.put(kept, "", NOW - 120)
    failed = envelope([], quota="failed")
    effective, cached = latest.effective(failed)
    assert cached == {"quota": NOW - 120}
    assert kept == original, "the kept answer is never changed"
    row = effective["quota"][0]
    assert row is not kept["quota"][0] and row["freshness"] == "stale"
    assert [c.get("freshness") for c in row["constraints"]] == ["stale", "stale", "stale"]
    assert all(c is not o for c, o in zip(row["constraints"], kept["quota"][0]["constraints"]))
    assert qs.freshness_projection(effective["quota"]) == "explicit"

    view = plugin.build_view(effective, "", NOW, reads=plugin.facet_states(failed), cached=cached)
    quota = account_quota(view)
    assert quota["state"] == "no_fresh_window" and quota["constraints"] == []
    assert quota["reset_credits"]["state"] == "last_known"
    reserve = plugin.reserve_view(None, effective, NOW, NOW, chart=False, cached=cached,
                                  reads=plugin.facet_states(failed))["summary"]
    week = group(reserve, WEEK_KEY)
    assert week["measured"]["accounts"] == 0 and week["last_known"]["accounts"] == 1
    assert week["last_known"]["origins"] == {"cached": 1}

    # The tool reuses the newer read, whose quota facet failed: no request is sent.
    stub(monkeypatch, [])
    latest.put(failed, "", NOW)
    answer = plugin.tool_answer(_Host(tmp_path), latest, "codex")
    assert answer["cached_facets"] == {"quota": qs.iso(NOW - 120)}
    assert tool_group(answer, WEEK_KEY)["remaining_windows"] == 0
    assert tool_group(answer, WEEK_KEY)["last_known"]["accounts"] == 1
    assert kept == original


@pytest.mark.parametrize(("rows", "kind"), [
    ([legacy(engine_snapshot(aggregate="fresh"))], "legacy"),
    ([engine_snapshot(aggregate="fresh", five=None)], "invalid"),
])
def test_a_kept_answer_keeps_its_kind_when_aged(rows, kind):
    original = copy.deepcopy(rows)
    aged = [qs.stale_snapshot(row) for row in rows]
    assert rows == original
    assert qs.freshness_projection(aged) == kind
    assert all(qs.constraint_freshness(r, c, kind) != "fresh" for r in aged for c in r["constraints"])


# ---------------------------------------------------------------------------
# The collector


def _runs(store):
    with sqlite3.connect(store.path) as conn:
        return {series: (n_obs, first_seen, last_seen) for series, n_obs, first_seen, last_seen in
                conn.execute("SELECT series, n_obs, first_seen, last_seen FROM run ORDER BY series")}


def test_a_window_whose_own_reset_passed_cannot_vouch_but_its_weekly_sibling_can(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    before = envelope([engine_snapshot(aggregate="fresh", five="fresh")])  # reset still ahead then
    plugin.persist_sweep(store, before, "", NOW - 120)
    assert set(_runs(store)) == {FIVE_KEY, WEEK_KEY}
    plugin.persist_sweep(store, envelope([engine_snapshot()]), "", NOW)
    runs = _runs(store)
    assert runs[WEEK_KEY] == (1, NOW - 120, NOW), "the same observation, still vouched for"
    assert runs[FIVE_KEY] == (1, NOW - 120, NOW - 120), "its own reset passed: not vouched for"

    legacy_store = quota_history.HistoryStore(tmp_path / "legacy")
    plugin.persist_sweep(legacy_store, envelope([legacy(engine_snapshot())]), "", NOW)
    assert _runs(legacy_store) == {}, "a stale snapshot without its own words keeps nothing"
    partial_store = quota_history.HistoryStore(tmp_path / "partial")
    plugin.persist_sweep(partial_store, envelope([engine_snapshot(aggregate="fresh", five=None)]), "", NOW)
    assert _runs(partial_store) == {}, "a partial answer vouches for nothing"


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "legacy"])
def test_a_reading_still_called_fresh_never_vouches_at_or_after_its_own_reset(tmp_path, explicit):
    """Freshness is judged when the engine reads; it does not stay true past
    the window's own reported reset. The unchanged reading (observed 150 s ago,
    5-hour reset 30 s ago) is called fresh by every answer here, yet from the
    reset on it vouches for no sweep. Its weekly sibling still does."""
    row = engine_snapshot(aggregate="fresh", five="fresh", five_reset=-30)
    answer = envelope([row if explicit else legacy(row)])
    store = quota_history.HistoryStore(tmp_path)
    for swept in (NOW - 120, NOW - 31, NOW - 30, NOW):  # reset ahead, 1 s ahead, at it, after it
        plugin.persist_sweep(store, answer, "", swept)
    runs = _runs(store)
    assert runs[WEEK_KEY] == (1, NOW - 120, NOW)
    assert runs[FIVE_KEY] == (1, NOW - 120, NOW - 31), "never confirmed at or after its reset"
    norm = qs.normalize(answer, NOW)
    assert [r.key for r in norm.readings if r.fresh] == [FIVE_KEY, WEEK_KEY], "still called fresh"
    assert [r.key for r in qs.recordable(norm, NOW)] == [WEEK_KEY]
    assert [r.key for r in qs.recordable(norm, NOW - 31)] == [FIVE_KEY, WEEK_KEY]


# ---------------------------------------------------------------------------
# Attribution and cooldowns follow the same resolver


def _null(**words):
    row = engine_snapshot("", **words)
    row["subject"]["subject_id"] = None
    return row


def _default_and_null(default, null):
    data = envelope([default, null], subjects=("codex-default",))
    return data, qs.attribute(data), qs.normalize(data, NOW)


def test_window_words_never_widen_the_default_profile_claim_on_null_subject_readings():
    """Supersession keeps the snapshot's own word (the host's alias rule): a
    default profile fresh only in some windows does not erase a legacy
    null-subject reading, whose own fresh windows still count for it."""
    # Split windows: the default's weekly is fresh, the null reading's 5-hour is.
    data, attributed, norm = _default_and_null(
        engine_snapshot("codex-default"),
        _null(aggregate="stale", five="fresh", week="stale", five_reset=3600))
    assert attributed.superseded["codex"] == 0
    assert len(attributed.rows_of("codex", "codex-default")) == 2
    fresh = sorted(r.key for r in norm.readings if r.fresh and r.subject_id == "codex-default")
    assert fresh == sorted([FIVE_KEY, WEEK_KEY]), "the null reading's fresh 5-hour window is not erased"
    summary = qs.build_summary(data, qs.HistoryView(state="empty"), NOW)
    assert [group(summary, key)["measured"]["accounts"] for key in (FIVE_KEY, WEEK_KEY)] == [1, 1]

    # Credit-only: a fresh counter alone does not erase a wholly fresh null reading.
    _data, attributed, norm = _default_and_null(
        engine_snapshot("codex-default", five="stale", week="stale", credits="fresh"),
        _null(aggregate="fresh", five="fresh", week="fresh", five_reset=3600))
    assert attributed.superseded["codex"] == 0
    assert sorted(r.key for r in norm.readings if r.fresh) == sorted([FIVE_KEY, WEEK_KEY])

    # A default fresh as a whole supersedes it, as before, with or without window words.
    whole = engine_snapshot("codex-default", aggregate="fresh", five="fresh", five_reset=3600)
    null = _null(aggregate="fresh", five="fresh", week="fresh", five_reset=3600)
    assert _default_and_null(whole, null)[1].superseded["codex"] == 1
    assert _default_and_null(legacy(whole), legacy(null))[1].superseded["codex"] == 1


def test_a_constraint_cooldown_is_reported_by_its_own_window():
    row = engine_snapshot()
    row["constraints"][1]["cooldown_until"] = qs.iso(NOW + 600)
    explicit = plugin.quota_for([row], "codex", "fixture-a", "ok", now=NOW)
    assert [(c["kind"], c["freshness"]) for c in explicit["cooldowns"]] == [("constraint", "fresh")]
    as_legacy = plugin.quota_for([legacy(row)], "codex", "fixture-a", "ok", now=NOW)
    assert [(c["kind"], c["freshness"]) for c in as_legacy["cooldowns"]] == [("constraint", "stale")]


def test_a_refresh_envelope_stays_legacy():
    """The foreground POST keeps the legacy shape: its snapshot's word stands."""
    update = plugin.build_quota_updates({"snapshots": [legacy(engine_snapshot(aggregate="fresh"))],
                                         "absences": [], "refreshed_at": qs.iso(NOW)})
    quota = update["quota_updates"][0]["quota"]
    assert quota["state"] in ("ok", "not_current")
    assert quota["reset_credits"]["state"] == "current"


# ---------------------------------------------------------------------------
# The widget draws the route's answer


NODE_FRESHNESS = r"""
(async () => {
  // Unmodified registered-route output for the same reading with and without
  // the engine's per-window words.
  const fixtures = JSON.parse(process.env.FRESHNESS_WIDGET_FIXTURE);
  const click=(env,key)=>byFocus(env.root,key).listeners.click[0]({stopPropagation(){}});
  for (const name of ['explicit','legacy','partial']) {
    const view = fixtures[name];
    const env = await boot(view);
    assert.equal(classes(env.root,'banner').length,0,name);
    click(env,'accounts');
    click(env,'acct:'+view.groups[0].accounts[0].key);
    const inspector = classes(env.root,'inspector')[0].textContent;
    const credits = classes(env.root,'account-credits')[0].textContent;
    assert.match(inspector,/its window reset after the last reading/,name);
    if (name === 'explicit') {
      assert.doesNotMatch(inspector,/No fresh reading|last known/,name);
      assert.match(credits,/Manual reset credits · 2 · observed/,name);
      assert.doesNotMatch(credits,/last known|not current/,name);
    } else {
      assert.match(inspector,/No fresh reading — last reading is stale/,name);
      assert.match(inspector,/last known/,name);
      assert.match(credits,/last known 2.*not current/,name);
    }
  }
  assert.ok(fixtures.explicit.groups[0].accounts[0].quota.constraints.length,'received answer intact');
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
"""


def test_the_widget_draws_each_window_by_its_own_freshness(tmp_path, monkeypatch):
    fixtures = {}
    for name, row in (("explicit", engine_snapshot()), ("legacy", legacy(engine_snapshot())),
                      ("partial", engine_snapshot(five=None))):
        view, _answer = read_everything(tmp_path / name, monkeypatch, envelope([row]))
        fixtures[name] = view
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run(
        [str(node), "-e", harness + NODE_FRESHNESS], cwd=Path(__file__).parent,
        env={**os.environ, "WIDGET_PATH": str(Path(__file__).with_name("widget.js").resolve()),
             "FRESHNESS_WIDGET_FIXTURE": json.dumps(fixtures)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
