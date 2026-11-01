"""0.7.0 targeted repair after the review (D2, D4, D5, D6, D7, D8, D9 and the
file cap's page reclaim).

Everything is synthetic: hand-built status payloads and simulated collector
sweeps against a temporary SQLite file; the widget runs in the bundled-Node
fake DOM of test_quotas with the scripted bridge of test_robust_widget.
"""

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
from test_last_known import codex, named_summary, only_group
from test_reserve import (FIVE_H, NOW, WEEK, _node, constraint, host_at, payload, profile, snap,
                          sweep)
from test_robust_widget import NODE_ROBUST

NOT_READ = {"catalog": "ok", "accounts": "ok", "quota": "not_read"}


def _recorded(store, sids=("a", "b"), ratios=(0.2, 0.6)):
    timelines = {sid: [(-3000, ratio, 2 * 86400)] for sid, ratio in zip(sids, ratios)}
    sweep(store, timelines, -3000, -600)
    return timelines


# ---------------------------------------------------------------------------
# D2: a cold start while the quota facet is not answered


@pytest.mark.parametrize("quota", ["not_read", "failed"])
def test_a_cold_start_shows_the_rosters_history_as_dated_last_known(tmp_path, quota):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store)
    data = codex([], "ab", reads={"catalog": "ok", "accounts": "ok", "quota": quota})
    summary, _ = named_summary(data, store, reads=plugin.facet_states(data))
    g = only_group(summary)
    assert g["measured"]["accounts"] == 0                  # nothing current is claimed
    assert [b["state"] for b in g["bars"]] == ["last_known", "last_known"]
    assert {b["origin"] for b in g["bars"]} == {"history"}
    assert g["last_known"]["windows"] == pytest.approx(0.8 + 0.4)
    assert g["last_known"]["oldest_observed_at"] == qs.iso(NOW - 3000)
    tool = qs.compact(summary)["groups"][0]
    assert tool["remaining_windows"] == g["measured"]["windows"]   # the field keeps its meaning
    assert "no current reading of any of 2 accounts" in tool["headline"]
    assert "0 account-windows left" not in tool["headline"]


def test_a_cold_start_never_brings_back_an_account_removed_since(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store)
    data = codex([], "a", reads=NOT_READ)          # b deleted in Claudexor since
    summary, _ = named_summary(data, store, reads=plugin.facet_states(data))
    assert [b["account"] for b in only_group(summary)["bars"]] == ["codex:a"]


def test_a_cold_start_with_an_empty_or_unknown_roster_restores_nothing(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store)
    empty = codex([], "", reads=NOT_READ)          # the account list says: none
    summary, _ = named_summary(empty, store, reads=plugin.facet_states(empty))
    assert summary["groups"] == []
    unknown = codex([], "ab", reads={"catalog": "ok", "accounts": "failed", "quota": "failed"})
    unknown["profiles"]["profiles"] = []
    summary, _ = named_summary(unknown, store, reads=plugin.facet_states(unknown))
    assert summary["groups"] == []


def test_a_quota_answer_keeps_a_recorded_limit_it_omits_as_dated_history(tmp_path):
    # r3 (review finding 2): an answered quota facet with no reading of a
    # recorded limit is not proof its last reading was false. It is shown as
    # the dated history value it is, never as current.
    store = quota_history.HistoryStore(tmp_path)
    sweep(store, {"a": [(-3000, 0.2, 86400)]}, -3000, -600, cid="secondary")
    data = codex([snap("codex", "a", [constraint("primary", 0.3, reset=86400)])], "a")
    summary, _ = named_summary(data, store)
    assert [g["key"].split("|")[1] for g in summary["groups"]] == ["primary", "secondary"]
    kept = only_group(summary, "|secondary|")
    assert kept["measured"]["accounts"] == 0
    assert [(b["state"], b["origin"]) for b in kept["bars"]] == [("last_known", "history")]


# ---------------------------------------------------------------------------
# D4: the recorded past does not depend on what is fresh now


def test_the_recorded_past_is_the_same_whatever_is_fresh_now(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timelines = {"a": [(-6000, 0.10, 2 * 86400), (-3000, 0.20, 2 * 86400)],
                 "b": [(-6000, 0.40, 2 * 86400)],
                 "c": [(-6000, 0.70, 2 * 86400), (-2000, 0.75, 2 * 86400)]}
    sweep(store, timelines, -6000, -600)
    charts = {}
    for name, stale in (("fresh", ()), ("one_stale", ("b",)), ("all_stale", ("a", "b", "c"))):
        data = host_at(NOW, timelines)
        for row in data["quota"]:
            if row["subject"]["subject_id"] in stale:
                row["freshness"] = "stale"
        charts[name] = plugin.reserve_view(store, data, NOW, NOW)["chart"]
    pasts = {name: chart["past"][:-1] for name, chart in charts.items()}
    assert pasts["fresh"] == pasts["one_stale"] == pasts["all_stale"]
    assert [v for _t, v in pasts["fresh"] if v is not None]          # a real record, not all gap
    assert {c["past_accounts"] for c in charts.values()} == {3}
    assert [charts[n]["past_basis"] for n in ("fresh", "one_stale", "all_stale")] == \
        ["current", "recorded", "last_known"]
    assert charts["fresh"]["past"][-1][1] == pytest.approx(0.8 + 0.6 + 0.25)
    assert charts["one_stale"]["past"][-1][1] is None                # no new total invented
    assert charts["all_stale"]["past"][-1][1] is None
    # The estimate's cohort stays its own: qualified current accounts only.
    assert charts["one_stale"]["recent_pace_scope"]["of"] == 2


# ---------------------------------------------------------------------------
# D8: history sources are resolved like a current reading's


def _two_sources(store, second_ratio, second_reset=2 * 86400):
    for k in range(6):
        t = NOW - 1500 + 120 * k
        data = codex([snap("codex", "a", [constraint("primary", 0.2, reset=2 * 86400)],
                           source="app", observed=-1500),
                      snap("codex", "a", [constraint("primary", second_ratio, reset=second_reset)],
                           source="rollout", observed=-1500),
                      snap("codex", "b", [constraint("primary", 0.5, reset=86400)], observed=-1500)], "ab")
        plugin.persist_sweep(store, data, "", t)


@pytest.mark.parametrize("second_ratio, second_reset, kept", [
    (0.6, 2 * 86400, False),          # same moment, values disagree: none
    (0.2, 2 * 86400 + 3600, False),   # resets disagree: none
    (0.2, 2 * 86400, True),           # agree: the value stands
])
def test_history_sources_that_disagree_give_no_last_known(tmp_path, second_ratio, second_reset, kept):
    store = quota_history.HistoryStore(tmp_path)
    _two_sources(store, second_ratio, second_reset)
    now_answer = codex([snap("codex", "b", [constraint("primary", 0.5, reset=86400)])], "ab")
    summary, _ = named_summary(now_answer, store)
    accounts = [b["account"] for b in only_group(summary)["bars"]]
    assert ("codex:a" in accounts) is kept
    if kept:
        bar = next(b for b in only_group(summary)["bars"] if b["account"] == "codex:a")
        assert bar["state"] == "last_known" and bar["origin"] == "history"


def test_the_reader_keeps_the_newest_run_of_every_source(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    _two_sources(store, 0.6)
    key = qs.group_key("codex", "primary", WEEK, ())
    view = store.read(lambda salt: {}, NOW, latest=lambda salt: [(qs.pseudo_id(salt, "codex", "a"), key)])
    runs = next(iter(view.latest.values()))
    assert sorted((r.source, r.ratio) for r in runs) == [("app", 0.2), ("rollout", 0.6)]


def test_an_unreadable_or_conflicting_reading_is_not_replaced_from_the_history(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store, ("a",), (0.2,))
    data = codex([snap("codex", "a", [constraint("primary", "lots", reset=86400)])], "a")
    norm, groups = qs.prepare(data, NOW)
    assert qs.latest_requests(norm, groups, "f" * 32) == []


# ---------------------------------------------------------------------------
# D7 and D9: what the tool and the widget say about reaching the limit


def test_no_reported_reset_is_counted_apart_from_before_its_reset(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    grow = [(-4080, 0.50), (-2040, 0.60), (-60, 0.70)]
    timelines = {"a": [(t, r, 3 * 86400) for t, r in grow], "b": [(t, r, None) for t, r in grow]}
    sweep(store, timelines, -4080, 0)
    out = plugin.reserve_view(store, host_at(NOW, timelines), NOW, NOW, chart=False, name_accounts=True)
    g = out["summary"]["groups"][0]
    pace = g["recent_pace"]
    assert pace["accounts_known"] == 2
    assert pace["exhaust_before_reset"] == 1 and pace["reach_limit_no_reported_reset"] == 1
    by = {b["account"]: b["pace"] for b in g["bars"]}
    assert by["codex:a"]["reset_reported"] is True and by["codex:b"]["reset_reported"] is False
    assert by["codex:a"]["reaches_limit_at"] and by["codex:b"]["reaches_limit_at"]
    headline = qs.compact(out["summary"])["groups"][0]["headline"]
    assert "1 would reach the limit before their reported reset" in headline
    assert "1 with no reported reset would reach it" in headline


def test_an_account_at_the_limit_is_not_said_to_reach_it(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    timelines = {"a": [(-4080, 0.90, 86400), (-60, 1.0, 86400)]}
    sweep(store, timelines, -4080, 0)
    out = plugin.reserve_view(store, host_at(NOW, timelines), NOW, NOW, chart=False, name_accounts=True)
    g = out["summary"]["groups"][0]
    assert g["measured"]["at_limit"] == 1
    assert g["recent_pace"]["exhaust_before_reset"] == 0
    assert "reaches_limit_at" not in g["bars"][0]["pace"]


# ---------------------------------------------------------------------------
# The file cap and the retention prune give every free page back


def test_a_prune_gives_every_free_page_back(tmp_path, monkeypatch):
    store = quota_history.HistoryStore(tmp_path)
    for i in range(40):
        timeline = {f"s{j}": [(-8000 + i * 120, 0.01 * i, 86400)] for j in range(20)}
        plugin.persist_sweep(store, host_at(NOW - 8000 + i * 120, timeline), "", NOW - 8000 + i * 120)
    monkeypatch.setattr(quota_history, "RETENTION_SEC", 1200.0)
    store._sweeps_until_prune = 0
    plugin.persist_sweep(store, host_at(NOW, {"s0": [(-10, 0.95, 86400)]}), "", NOW)
    conn = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM run").fetchone()[0] < 200
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# D5 and D6 in the real widget


NODE_REPAIR = r"""
(async () => {
  const fx = JSON.parse(process.env.REPAIR_FIXTURE);
  const url = (req) => new URL(req.url, 'http://x');
  const click = (env, key) => byFocus(env.root, key).listeners.click[0]({ stopPropagation() {} });

  // D5: the limit the chart opened on stays while an answer names another
  // one "lowest left"; a click, or its removal from a whole answer, moves it.
  // Every pending read (the poll, and the chart the widget then asks for) is
  // answered with the same view; the limit each one asked for is recorded.
  let env = bootControlled();
  const asked = [];
  async function drain(env, value) {
    for (let i = 0; i < 6; i++) {
      await settle();
      const open = env.requests.filter((r) => !r.done);
      if (!open.length) return;
      open.forEach((r) => { r.done = true; asked.push(url(r).searchParams.get('group')); r.resolve(answer(value)); });
    }
    await settle();
  }
  // 0.8.0: the timeline is open on every mount, so the first answer (which
  // carries no chart here) is followed by one request for the chart.
  await drain(env, fx.weekly_tightest);
  const opened = asked.at(-1);
  assert.match(opened, /\|secondary\|/, 'opens on the lowest-left limit');
  for (const next of [fx.five_hour_tightest_degraded, fx.five_hour_tightest, fx.five_hour_tightest]) {
    const from = asked.length;
    env.poll();
    await drain(env, next);
    assert.ok(asked.length > from);
    asked.slice(from).forEach((g) => assert.equal(g, opened, 'kept across answers'));
  }
  const fiveHour = fx.weekly_tightest.reserve.summary.groups.find((g) => /\|primary\|/.test(g.key)).key;
  let from = asked.length;
  click(env, 'limit:' + fiveHour);
  await drain(env, fx.weekly_tightest);
  assert.ok(asked.slice(from).includes(fiveHour), 'a click chooses: ' + asked.slice(from));
  from = asked.length;
  env.poll();
  await drain(env, fx.weekly_tightest);
  asked.slice(from).forEach((g) => assert.equal(g, fiveHour, 'the click is kept'));
  from = asked.length;
  env.poll();
  await drain(env, fx.weekly_only);
  env.poll();
  await drain(env, fx.weekly_only);
  assert.match(asked.at(-1), /\|secondary\|/, 'gone from a whole answer: ' + asked.slice(from));

  // D6: fresh -> degraded -> failed read keeps the degraded (newer) screen;
  // a semantic-empty answer is not drawn; the next whole answer recovers.
  // (The timeline is folded here: one read per poll, never a chart.)
  env = bootControlled();
  await drain(env, fx.weekly_tightest);
  // 0.8.1: the charted row's own control folds the timeline.
  const charted = fx.weekly_tightest.reserve.summary.groups.find((g) => g.tightest).key;
  click(env, 'limit:' + charted);
  assert.equal(byFocus(env.root, 'limit:' + charted).getAttribute('aria-pressed'), 'false');
  assert.equal(classes(env.root, 'tl-block').length, 0);
  env.poll();
  await settle();
  assert.match(env.requests.at(-1).url, /chart=0/, 'a folded timeline is not asked for');
  env.requests.at(-1).done = true;
  env.requests.at(-1).resolve(answer(fx.weekly_tightest));
  await settle();
  const lastBars = () => classes(env.root, 'bar').filter((b) => /\blast\b/.test(String(b.className))).length;
  assert.equal(lastBars(), 0);
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer(fx.five_hour_tightest_degraded));
  await settle();
  const degraded = lastBars();
  assert.ok(degraded > 0, 'the degraded answer shows last-known bars');
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer('not json', 502));
  await settle();
  assert.equal(lastBars(), degraded, 'a failed read keeps the newest screen, not an older whole one');
  assert.match(env.root.textContent, /Reading could not be refreshed \(HTTP 502\)/);
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer({}));
  await settle();
  assert.equal(lastBars(), degraded, 'an empty answer is not a screen');
  assert.match(env.root.textContent, /response reported itself incomplete/);
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer(fx.weekly_tightest));
  await settle();
  assert.equal(lastBars(), 0);
  assert.doesNotMatch(env.root.textContent, /could not be refreshed/);
  assert.equal(env.errors.length, 0, env.errors.join('\n'));
  console.log('ok');
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _repair_fixture(tmp_path):
    import time as _time
    now = float(int(_time.time()))

    def status(stale=False, reads=None):
        snaps = [snap("codex", sid, [constraint("primary", five, window=FIVE_H, reset=None),
                                      constraint("secondary", week, reset=None)],
                      observed_abs=now - 60, fresh=not stale)
                 for sid, five, week in (("a", 0.1, 0.7), ("b", 0.2, 0.8), ("c", 0.3, 0.6))]
        for row in snaps:
            row["constraints"][0]["resets_at"] = qs.iso(now + 3600)
            row["constraints"][1]["resets_at"] = qs.iso(now + 3 * 86400)
        return codex(snaps, "abc", reads=reads)

    store = quota_history.HistoryStore(tmp_path)

    def view(data, cached=None, reads=None):
        out = plugin.build_view(data, "", now, reads=reads, cached=cached)
        out["reserve"] = plugin.reserve_view(store, data, now, now, harness="codex", chart=False,
                                             cached=cached, reads=reads, name_accounts=True)
        return out

    weekly = view(status())
    keys = {g["key"].split("|")[1]: g for g in weekly["reserve"]["summary"]["groups"]}
    assert keys["secondary"]["tightest"] and not keys["primary"]["tightest"]

    def flipped(v):
        v = json.loads(json.dumps(v))
        for g in v["reserve"]["summary"]["groups"]:
            g["tightest"] = "|primary|" in g["key"]
        return v

    latest = plugin.LatestRead()
    latest.put(status(), "", now - 120)
    failed = status(reads={"catalog": "ok", "accounts": "ok", "quota": "failed"})
    failed["quota"] = []
    effective, cached = latest.effective(failed)
    degraded = view(effective, cached=cached, reads=plugin.facet_states(failed))
    assert degraded["complete"] is False
    weekly_only = json.loads(json.dumps(weekly))
    weekly_only["reserve"]["summary"]["groups"] = [
        g for g in weekly_only["reserve"]["summary"]["groups"] if "|secondary|" in g["key"]]
    return {"weekly_tightest": weekly, "five_hour_tightest": flipped(weekly),
            "five_hour_tightest_degraded": flipped(degraded), "weekly_only": weekly_only}


def test_real_widget_keeps_the_chart_limit_and_the_newest_screen(tmp_path):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    widget_path = Path(__file__).with_name("widget.js").resolve()
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    controlled = NODE_ROBUST.split("(async () => {")[0]
    result = subprocess.run(
        [str(node), "-e", harness + controlled + NODE_REPAIR],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path),
             "REPAIR_FIXTURE": json.dumps(_repair_fixture(tmp_path))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")
