"""0.7.0 repair r4, after the independent review of r3: one kept projection
for every fallback screen, the read-out of a chart with no record, a Refresh
answer that cannot be read, the tool's own reads, and stale sources that
disagree.

Everything is synthetic: hand-built status payloads against a temporary
SQLite file; the widget runs in the bundled-Node fake DOM of test_quotas with
a scripted bridge and a clock the test moves.
"""

import json
import os
import subprocess
import time as _time
from pathlib import Path

import pytest

import plugin
import quota_history
import quota_summary as qs
import test_quotas
from test_last_known import codex, named_summary, only_group
from test_repair_r3 import NODE_R3, _Api, _view
from test_reserve import NOW, WEEK, _node, constraint, payload, profile, snap, sweep

ENVELOPE = {"snapshots": [], "absences": [], "refreshed_at": "2026-09-21T13:33:20+00:00"}


class _Pinned:
    """plugin's clock: wall time pinned to NOW, monotonic real."""

    def time(self):
        return NOW

    def __getattr__(self, name):
        return getattr(_time, name)


# ---------------------------------------------------------------------------
# 3: a success status whose body is not a refresh envelope


@pytest.mark.parametrize("body", [{}, {"snapshots": []}, {"snapshots": "x", "absences": [], "refreshed_at": "t"}])
def test_a_refresh_answer_that_is_not_an_envelope_has_an_unknown_outcome(tmp_path, monkeypatch, body):
    calls = []

    def fake(_port, path, method="GET", timeout_sec=0):
        calls.append((method, path))
        return body, "", 200

    monkeypatch.setattr(plugin, "_request_json", fake)
    api = _Api(tmp_path)
    plugin.register(api)
    result = api.routes["refresh"]({})
    assert calls == [("POST", plugin.REFRESH_PATH)]          # sent once, never again on its own
    assert result["ok"] is False and result["outcome_unknown"] is True
    assert result["compatibility_error"] is False
    assert "unknown" in result["message"] and "failed" not in result["message"]


def test_a_refresh_envelope_is_still_a_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_request_json", lambda *_a, **_k: (dict(ENVELOPE), "", 200))
    api = _Api(tmp_path)
    plugin.register(api)
    result = api.routes["refresh"]({})
    assert result["ok"] is True and "outcome_unknown" not in result


# ---------------------------------------------------------------------------
# 4: the tool keeps its own reads, as the widget route does


def _one(used):
    return codex([snap("codex", "a", [constraint("primary", used, reset=86400)])], "a")


@pytest.mark.parametrize("older", [True, False])
def test_a_tool_read_answers_the_next_failed_tool_read(tmp_path, monkeypatch, older):
    monkeypatch.setattr(plugin, "time", _Pinned())
    api = _Api(tmp_path)
    latest = plugin.LatestRead()
    if older:
        latest.put(_one(0.3), "", NOW - 600)              # 0.70 left, read by an earlier route call
        latest.invalidate()                                # not reusable: a Refresh returned since
    first = plugin.tool_answer(api, latest, fetch=lambda: (_one(0.8), ""), now=NOW)
    assert first["groups"][0]["remaining_windows"] == pytest.approx(0.2)
    latest.invalidate()
    failed = plugin.tool_answer(api, latest, fetch=lambda: (None, "HTTP 503 from /api/claudexor/status"),
                                now=NOW)
    assert failed["status_error"]
    assert failed["groups"], "the tool's own read is the last known, not nothing"
    g = failed["groups"][0]
    assert g["remaining_windows"] == 0                     # nothing current is claimed
    assert g["last_known"]["windows"] == pytest.approx(0.2)  # the tool's read, never the older 0.70
    assert set(failed["cached_facets"]) == {"catalog", "accounts", "quota"}


def test_a_tool_read_in_the_air_when_a_refresh_returns_is_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "time", _Pinned())
    latest = plugin.LatestRead()

    def racing():
        latest.invalidate()                                # the Refresh returned meanwhile
        return _one(0.8), ""

    plugin.tool_answer(_Api(tmp_path), latest, fetch=racing, now=NOW)
    assert latest.get(3600) is None
    assert latest.effective(None) == (None, {})


# ---------------------------------------------------------------------------
# 5: stale sources that disagree stay refused through the history fallback


def _conflict(observed):
    return codex([snap("codex", "a", [constraint("primary", 0.2, reset=2 * 86400)], source="x",
                       fresh=False, observed=observed),
                  snap("codex", "a", [constraint("primary", 0.8, reset=2 * 86400)], source="y",
                       fresh=False, observed=observed)], "a")


def test_older_history_never_revives_stale_sources_that_disagree(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    sweep(store, {"a": [(-3000, 0.2, 2 * 86400)]}, -3000, -600)    # recorded 0.80 left, seen until -600
    summary, _ = named_summary(_conflict(-60), store)
    g = only_group(summary)
    bar = g["bars"][0]
    assert (bar["state"], bar["left"], bar["why"]) == ("unknown", None, "sources_disagree")
    assert "last_reading" not in bar
    assert g["last_known"]["accounts"] == 0 and g["with_last_known"]["windows"] == 0
    tool = qs.compact(summary)["groups"][0]
    assert tool["remaining_windows"] == 0 and "last_known" not in tool


def test_a_newer_recorded_reading_still_stands_in_for_older_disagreeing_stale_sources(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    sweep(store, {"a": [(-3000, 0.2, 2 * 86400)]}, -3000, -600)
    summary, _ = named_summary(_conflict(-4000), store)
    bar = only_group(summary)["bars"][0]
    assert (bar["state"], bar["left"], bar["origin"]) == ("last_known", pytest.approx(0.8), "history")


def test_absent_stale_evidence_is_still_filled_from_the_history(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    sweep(store, {"a": [(-3000, 0.2, 2 * 86400)]}, -3000, -600)
    absent = codex([snap("codex", "a", [constraint("primary", None, reset=2 * 86400)], fresh=False,
                         observed=-60)], "a")
    bar = only_group(named_summary(absent, store)[0])["bars"][0]
    assert (bar["state"], bar["origin"]) == ("last_known", "history")


# ---------------------------------------------------------------------------
# 1, on the skill's side: an account list not read now states no routing


def test_an_account_list_not_read_now_states_no_next_up():
    data = codex([snap("codex", "a", [constraint("primary", 0.3, reset=86400)])], "ab")
    data["profiles"]["accountPools"] = [{"harness_id": "codex", "next_up": {"kind": "profile", "profileId": "a"}}]
    live = plugin.build_view(data, "", NOW)["groups"][0]
    assert live["routing_read"] is True and [a["next_up"] for a in live["accounts"]] == [True, False]
    latest = plugin.LatestRead()
    latest.put(data, "", NOW - 120)
    effective, cached = latest.effective(None)          # the status read failed: the list is kept
    kept = plugin.build_view(effective, "HTTP 503 from /api/claudexor/status", NOW,
                             reads=plugin.facet_states(None), cached=cached)["groups"][0]
    assert kept["routing_read"] is False
    assert [a["next_up"] for a in kept["accounts"]] == [False, False]
    assert all(a["verification"]["label"].endswith("— last known") for a in kept["accounts"])


# ---------------------------------------------------------------------------
# 1, 2 and 3 in the real widget


def _row(now, sid, used, *, plan=None, fresh=True, observed=-60.0, reset=86400.0):
    return {"subject": {"harness": "codex", "subject_id": sid, "plan_label": plan},
            "constraints": [{"id": "primary", "label": "primary", "used_ratio": used, "window_seconds": WEEK,
                             "resets_at": qs.iso(now + reset)}],
            "availability": {"state": "available"}, "observed_at": qs.iso(now + observed),
            "freshness": "fresh" if fresh else "stale", "source": "app"}


def _r4_fixture(tmp_path):
    now = float(int(_time.time()))
    empty = quota_history.HistoryStore(tmp_path / "empty")
    # Reset in 2 h: a screen kept 3 h later is past it.
    soon = payload([_row(now, "a", 0.7, reset=7200), _row(now, "b", 0.6, reset=7200)],
                   [profile("codex", "a"), profile("codex", "b")], harnesses=("codex",))
    good = _view(empty, soon, now, True)
    broken = json.loads(json.dumps(good))
    broken["groups"][0]["accounts"] = [None]
    # Verified live and next up; mixed plans; one stale; one switched off.
    k1 = payload([_row(now, "a", 0.3, plan="plus"), _row(now, "e", 0.5, plan="pro"),
                  _row(now, "s", 0.6, fresh=False, observed=-1200)],
                 [profile("codex", "a"), profile("codex", "e"), profile("codex", "s"),
                  profile("codex", "d", enabled=False, display_name="lumen")], harnesses=("codex",))
    k1["profiles"]["profiles"][0]["status"]["verification_source"] = "vendor"
    k1["profiles"]["accountPools"] = [{"harness_id": "codex", "next_up": {"kind": "profile", "profileId": "a"}}]
    k1_view, k1_chart = _view(empty, k1, now, True), _view(empty, k1, now, True)
    g = k1_view["reserve"]["summary"]["groups"][0]
    assert g["plans"]["mixed"] and g["coverage"]["stale_only"] == 1
    chart = k1_chart["reserve"]["chart"]
    assert (chart["past_accounts"], chart["accounts"], chart["past"][-1][1]) == (0, 2, pytest.approx(1.2))
    a = k1_view["groups"][0]["accounts"][0]
    assert a["verification"]["label"] == "Verified live" and a["next_up"] is True
    # Held by a cooldown alone, ending in 1 h.
    cool = payload([{"subject": {"harness": "codex", "subject_id": "c", "plan_label": None}, "constraints": [],
                     "availability": {"state": "cooldown", "resets_at": qs.iso(now + 3600)},
                     "observed_at": qs.iso(now - 60), "freshness": "fresh", "source": "app"}],
                   [profile("codex", "c")], harnesses=("codex",))
    k2 = _view(empty, cool, now, False)
    assert k2["groups"][0]["accounts"][0]["quota"]["state"] == "cooling"
    return {"good": good, "broken": broken, "k1": k1_view, "k1_chart": k1_chart, "k1_key": g["key"], "k2": k2}


NODE_R4 = r"""
(async () => {
  const fx = JSON.parse(process.env.R4_FIXTURE);
  const section = process.env.R4_SECTION;
  const nowLabels = (env) => walk(env.root).filter((n) => n.getAttribute('class') === 'axis-text now')
    .map((n) => n.textContent);
  // 0.8.0: the About button carries the pip that says a facet did not answer.
  const problem = (env) => /\bhas-problem\b/.test(String(byFocus(env.root, 'about').className));
  const text = (env) => env.root.textContent;
  let env;

  if (section === 'draw') {
    // 1. An answer that cannot be drawn, 3 h after the last one (1 h past
    // its reported reset): the screen before it comes back kept, never current.
    env = bootClock();
    await respond(env, fx.good);
    assert.ok(barStates(env).every((s) => s === 'current'));
    assert.match(text(env), /0\.70 of 2/);
    env.clock.shift = 3 * 3600 * 1000;
    env.poll();
    await respond(env, fx.broken);
    assert.match(text(env), /The latest answer could not be drawn \(TypeError/);
    assert.ok(barStates(env).length >= 2 && barStates(env).every((s) => s === 'unknown'), barStates(env).join());
    assert.doesNotMatch(text(env), /0\.70 of 2/);
    assert.doesNotMatch(spoken(env), /2 current of 2 accounts/);
    assert.doesNotMatch(text(env), /next reset/);
    assert.match(text(env), /Claudexor not read now/);
    assert.doesNotMatch(text(env), /Reading could not be refreshed/, 'one banner at a time');
    // A second answer that cannot be drawn: still one banner, still kept.
    env.poll();
    await respond(env, fx.broken);
    assert.match(text(env), /The latest answer could not be drawn \(TypeError/);
    assert.doesNotMatch(text(env), /Reading could not be refreshed/, 'one banner at a time');
    assert.ok(barStates(env).every((s) => s === 'unknown'));
    // The next redraw still says what the screen is.
    click(env, 'about');
    await settle();
    click(env, 'about');
    await settle();
    assert.doesNotMatch(text(env), /could not be drawn \(TypeError/);
    assert.match(text(env), /Reading could not be refreshed \(the latest answer could not be drawn\)/);
    assert.match(text(env), /nothing in it is current/);
    assert.ok(barStates(env).every((s) => s === 'unknown'));
    env.poll();
    await respond(env, fx.good);
    assert.doesNotMatch(text(env), /could not be drawn/);
  }

  if (section === 'readout') {
    // 2. With no older record there is no historical line yet. The reading
    // at now names its own accounts (including carried), separately from the fresh-only future.
    env = bootClock();
    await respond(env, fx.k1_chart);
    byFocus(env.root, 'chart-plot').listeners.focus[0]();
    const readout = classes(env.root, 'chart-readout')[0].textContent;
    const nowHistory = fx.k1_chart.reserve.chart.history.details.at(-1);
    assert.match(readout, new RegExp('recorded ' + nowHistory.value.toFixed(2) + ' of '
      + nowHistory.accounts + ' accounts'), readout);
    assert.match(readout, /no new use 1\.20 of 2 current accounts/, readout);
    assert.doesNotMatch(readout, /of 0 accounts/);
    // The legend says there is no record rather than imply a line.
    assert.match(classes(env.root, 'chart-legend')[0].textContent, /no record in this span yet/);
    assert.match(text(env), /No record in this span yet: the future starts from the row’s figure at now/);
  }

  if (section === 'kept') {
    // 1. One kept projection: facets, daemon, checks, routing, the
    // switched-off line, plans and counts of the answer's moment, the
    // chart's moment.
    env = bootClock();
    await respond(env, fx.k1);
    click(env, 'accounts');
    click(env, 'acct:codex:a');
    await settle();
    assert.match(text(env), /Verified live/);
    assert.match(text(env), /next up/);
    assert.match(text(env), /lumen is switched off in Claudexor — not counted/);
    assert.match(text(env), /mixed plans/);
    assert.match(text(env), /1 stale · /);
    assert.ok(!problem(env), 'the About pip says nothing is wrong');
    assert.deepEqual(nowLabels(env), ['now']);
    click(env, 'about');
    await settle();
    assert.match(text(env), /What the daemon answered on this read/);
    assert.match(text(env), /daemon running/);
    assert.doesNotMatch(text(env), /when last read/);
    click(env, 'about');
    await settle();

    env.clock.shift = 2 * 3600 * 1000;
    env.poll();
    await respond(env, 'bad gateway', 502);
    assert.match(text(env), /Reading could not be refreshed \(HTTP 502\)/);
    assert.ok(problem(env), 'the About pip says nothing was read');
    assert.match(text(env), /Verified live — last known/);
    assert.doesNotMatch(text(env), /Verified live(?! — last known)/);
    assert.doesNotMatch(text(env), /next up/);
    assert.match(text(env), /lumen was switched off in Claudexor when last read \(2h ago\) — not counted/);
    assert.doesNotMatch(text(env), /is switched off/);
    assert.doesNotMatch(text(env), /mixed plans/);
    assert.doesNotMatch(text(env), / stale · /);
    assert.match(text(env), /3 shown as last known, not current/);
    assert.equal(nowLabels(env).length, 1);
    assert.match(nowLabels(env)[0], /^read \d\d:\d\d$/);
    assert.match(classes(env.root, 'chart-legend')[0].textContent, /no future · nothing read since \d\d:\d\d/);
    click(env, 'about');
    await settle();
    assert.match(text(env), /Nothing was read on the latest attempt: nothing below is current/);
    assert.match(text(env), /daemon running when last read \(2h ago\)/);
    assert.doesNotMatch(text(env), /What the daemon answered on this read/);
    assert.match(text(env), /catalog indeterminate/);
    assert.match(text(env), /rotation not reported for Codex/);
    click(env, 'about');
    await settle();
    // The next whole answer is drawn as it is.
    env.poll();
    await respond(env, fx.k1);
    assert.match(text(env), /Verified live(?! — last known)/);
    assert.ok(!problem(env), 'the About pip says nothing is wrong');
  }

  if (section === 'cooldown') {
    // 1. A cooldown alone: past its end a kept screen does not say
    // "Cooling down"; before it, the cooldown stays, from the reading
    // that reported it.
    env = bootClock();
    await respond(env, fx.k2);
    click(env, 'acct:codex:c');
    assert.match(text(env), /Cooling down/);
    env.clock.shift = 2 * 3600 * 1000;
    env.poll();
    await respond(env, 'bad gateway', 502);
    assert.doesNotMatch(text(env), /Cooling down/);
    assert.match(text(env), /No fresh reading — the cooldown last reported has ended/);
    env = bootClock();
    await respond(env, fx.k2);
    click(env, 'acct:codex:c');
    env.clock.shift = 30 * 60 * 1000;
    env.poll();
    await respond(env, 'bad gateway', 502);
    assert.match(text(env), /Cooling down · whole account/);
    assert.match(text(env), /reported by a stale reading/);
  }

  if (section === 'refresh') {
    // 3. Refresh: a success status whose body breaks off or is not the
    // route's answer has an unknown outcome; an answer that says it failed,
    // or an HTTP error, is a failure. Never sent again on its own.
    env = bootClock();
    await respond(env, fx.good);
    const cases = [
      ['unknown', () => ({ ok: true, status: 200, text: () => Promise.reject(new Error('body stream interrupted')) })],
      ['unknown', () => Promise.reject(new TypeError('Failed to fetch'))],
      ['unknown', () => answer({})],
      ['unknown', () => answer({ quota_updates: [] })],
      ['failed', () => answer({ ok: false, compatibility_error: false, message: 'Live quota refresh failed' })],
      ['failed', () => ({ ok: false, status: 500, text: () => Promise.reject(new Error('connection reset')) })],
    ];
    for (const [want, make] of cases) {
      byFocus(env.root, 'refresh').listeners.click[0]();
      await settle();
      const post = env.requests.at(-1);
      assert.equal(post.method, 'POST');
      post.done = true;
      post.resolve(make());
      await settle();
      if (want === 'unknown') {
        assert.match(text(env), /whether it ran is unknown/);
        assert.doesNotMatch(text(env), /Live quota refresh failed/);
      } else {
        assert.match(text(env), /Live quota refresh failed/);
        assert.doesNotMatch(text(env), /whether it ran is unknown/);
      }
      assert.ok(!byFocus(env.root, 'refresh').disabled);
    }
    env.poll();
    await respond(env, fx.good);
    assert.equal(env.requests.filter((r) => r.method === 'POST').length, cases.length);
  }
  assert.ok(env, 'a known section');
  // Only each answer that could not be drawn is logged, once.
  assert.equal(env.errors.length, section === 'draw' ? 2 : 0, env.errors.join('\n'));
  if (section === 'draw') assert.match(env.errors[0], /the answer could not be drawn/);
  console.log('ok');
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def r4_fixture(tmp_path_factory):
    return json.dumps(_r4_fixture(tmp_path_factory.mktemp("r4")))


@pytest.mark.parametrize("section", ["draw", "kept", "readout", "cooldown", "refresh"])
def test_real_widget_keeps_one_projection_and_reads_unknown_outcomes(r4_fixture, section):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    widget_path = Path(__file__).with_name("widget.js").resolve()
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    helpers = NODE_R3.split("(async () => {")[0]
    result = subprocess.run(
        [str(node), "-e", harness + helpers + NODE_R4],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path), "R4_FIXTURE": r4_fixture,
             "R4_SECTION": section},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")
