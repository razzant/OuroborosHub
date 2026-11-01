"""0.7.0 repair r3, after the independent review: a recorded limit an answered
quota facet omits, the cold chart, model scope from the history, the counts,
the estimate's own record, the Refresh outcome, and the widget's fallback,
chart narration, pace details and last-known zeros.

Everything is synthetic: hand-built status payloads and simulated collector
sweeps against a temporary SQLite file; the widget runs in the bundled-Node
fake DOM of test_quotas with a scripted bridge and a clock the test moves.
"""

import json
import os
import socket
import subprocess
import threading
import time as _time
from pathlib import Path

import pytest

import plugin
import quota_history
import quota_summary as qs
import test_quotas
from test_last_known import codex, named_summary, only_group
from test_reserve import NOW, WEEK, _node, constraint, host_at, payload, profile, snap, sweep, widget_reserve

NOT_READ = {"catalog": "ok", "accounts": "ok", "quota": "not_read"}
NO_ANSWER = getattr(plugin, "NO_ANSWER_STATUS", -1)


def _recorded(store, sids=("a", "b"), ratios=(0.2, 0.6), **kw):
    timelines = {sid: [(-3000, ratio, 2 * 86400)] for sid, ratio in zip(sids, ratios)}
    sweep(store, timelines, -3000, -600, **kw)
    return timelines


def _recorded_two_limits(store):
    """a and b, each with two weekly limits, seen from -3000 to -600."""
    for k in range(21):
        t = NOW - 3000 + 120 * k
        data = codex([snap("codex", sid, [constraint("primary", p, reset=2 * 86400),
                                          constraint("secondary", s, reset=3 * 86400)],
                           observed_abs=NOW - 3000)
                      for sid, p, s in (("a", 0.2, 0.5), ("b", 0.6, 0.1))], "ab")
        plugin.persist_sweep(store, data, "", t)


# ---------------------------------------------------------------------------
# 2: an answered quota facet that omits a recorded limit does not hide it


@pytest.mark.parametrize("answered", ["empty", "another_limit"])
def test_an_answered_quota_facet_keeps_a_recorded_limit_it_omits(tmp_path, answered):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store)                                   # a and b: primary
    snaps = [] if answered == "empty" else [snap("codex", "a", [constraint("secondary", 0.3, reset=86400)])]
    data = codex(snaps, "ab")                          # quota: ok
    summary, _ = named_summary(data, store)
    g = only_group(summary)
    assert g["measured"]["accounts"] == 0              # nothing current is claimed
    assert [(b["account"], b["state"], b["origin"]) for b in g["bars"]] == \
        [("codex:a", "last_known", "history"), ("codex:b", "last_known", "history")]
    assert g["last_known"]["windows"] == pytest.approx(0.8 + 0.4)
    tool = next(t for t in qs.compact(summary)["groups"] if "|primary|" in t["key"])
    assert tool["remaining_windows"] == 0
    assert "no current reading of any of 2 accounts" in tool["headline"]
    if answered == "another_limit":
        assert only_group(summary, "|secondary|")["measured"]["accounts"] == 1


def test_an_answered_quota_facet_never_brings_back_a_removed_account(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store)
    summary, _ = named_summary(codex([], "a"), store)          # b deleted since
    assert [b["account"] for b in only_group(summary)["bars"]] == ["codex:a"]
    summary, _ = named_summary(codex([], ""), store)           # a confirmed empty roster
    assert summary["groups"] == []


# ---------------------------------------------------------------------------
# 3: the chart of a limit only the history knows reads that limit's record


@pytest.mark.parametrize("asked", ["", "codex|primary|604800|-"])
def test_a_cold_start_charts_the_restored_limit_from_its_record(tmp_path, asked):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store)                                   # a and b recorded
    data = codex([], "abc", reads=NOT_READ)
    out = plugin.reserve_view(store, data, NOW, NOW, group=asked, reads=plugin.facet_states(data))
    chart = out["chart"]
    assert chart["group_key"] == qs.group_key("codex", "primary", WEEK, ())
    assert (chart["past_accounts"], chart["past_current_accounts"], chart["past_basis"]) == \
        (2, 0, "last_known")
    recorded = [v for _t, v in chart["past"][:-1] if v is not None]
    assert recorded and all(v == pytest.approx(0.8 + 0.4) for v in recorded)
    assert chart["past"][-1][1] is None                # no total "now" invented


@pytest.mark.parametrize("reads", [NOT_READ, None])
def test_the_chart_of_an_asked_restored_limit_that_is_not_the_default(tmp_path, reads):
    store = quota_history.HistoryStore(tmp_path)
    _recorded_two_limits(store)
    data = codex([], "ab", reads=reads)
    key = qs.group_key("codex", "secondary", WEEK, ())
    out = plugin.reserve_view(store, data, NOW, NOW, group=key, reads=plugin.facet_states(data))
    chart = out["chart"]
    assert chart["group_key"] == key
    assert chart["past_accounts"] == 2
    recorded = [v for _t, v in chart["past"][:-1] if v is not None]
    assert recorded and all(v == pytest.approx(0.5 + 0.9) for v in recorded)
    # The summary is the same whichever limit the chart shows.
    plain = plugin.reserve_view(store, data, NOW, NOW, chart=False, reads=plugin.facet_states(data))
    assert out["summary"]["groups"] == plain["summary"]["groups"]


# ---------------------------------------------------------------------------
# 4: a scoped limit restored from the history stays scoped


def test_a_restored_scoped_limit_says_it_is_scoped_with_names_unknown(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    models = ["fable", "best"]
    # The series key the collector stores carries the scope of the model list.
    for k in range(21):
        t = NOW - 3000 + 120 * k
        plugin.persist_sweep(store, codex([snap("codex", "a", [constraint("weekly_scoped:Fable", 0.4,
                                                                          reset=2 * 86400, models=models)],
                                                observed_abs=NOW - 3000)], "a"), "", t)
    cold = codex([], "a", reads=NOT_READ)
    summary, _ = named_summary(cold, store, reads=plugin.facet_states(cold))
    scoped = [g for g in summary["groups"] if g["key"].endswith("|" + qs._scope_hash(tuple(sorted(models))))]
    assert len(scoped) == 1
    g = scoped[0]
    assert g["models"] == [] and g["model_scope"] == "names_unknown"
    tool = next(t for t in qs.compact(summary)["groups"] if t["key"] == g["key"])
    assert "model_scope" in tool and "names" in tool["model_scope"]
    assert "model-scoped" in tool["headline"]
    # A scope read now names its models; a shared limit has none.
    warm = codex([snap("codex", "a", [constraint("weekly_scoped:Fable", 0.4, reset=2 * 86400, models=models),
                                      constraint("primary", 0.1, reset=86400)])], "a")
    summary, _ = named_summary(warm, store)
    scopes = {g["key"].split("|")[1]: g["model_scope"] for g in summary["groups"]}
    assert scopes == {"weekly_scoped:Fable": "named", "primary": "none"}


# ---------------------------------------------------------------------------
# 5: each roster account is counted once


def test_a_cold_start_counts_each_roster_account_once(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    _recorded(store)                                   # roster a, b, c; history a, b
    cold = codex([], "abc", reads=NOT_READ)
    summary, _ = named_summary(cold, store, reads=plugin.facet_states(cold))
    g = only_group(summary)
    assert (g["slots"], g["coverage"]["other_family_accounts"], g["applicability_unknown"]) == (2, 3, 1)
    assert qs.compact(summary)["groups"][0]["applicability_unknown_accounts"] == 1
    # The same roster when a quota answer reads only a: the same split.
    warm = codex([snap("codex", "a", [constraint("primary", 0.2, reset=2 * 86400)])], "abc")
    summary, _ = named_summary(warm, store)
    g = only_group(summary)
    assert (g["slots"], g["applicability_unknown"]) == (2, 1)
    # Two roster accounts, one recorded: one unknown, not none.
    store2 = quota_history.HistoryStore(tmp_path / "2")
    _recorded(store2, ("a",), (0.2,))
    cold = codex([], "ab", reads=NOT_READ)
    summary, _ = named_summary(cold, store2, reads=plugin.facet_states(cold))
    assert only_group(summary)["applicability_unknown"] == 1


# ---------------------------------------------------------------------------
# 6: the estimate is continued from the record of its own accounts


def test_the_estimate_has_the_record_of_its_own_cohort(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    grow = [(-4080, 0.50), (-2040, 0.60), (-60, 0.70)]
    timelines = {"a": [(t, r, 3 * 86400) for t, r in grow], "b": [(-4080, 0.20, 3 * 86400)]}
    sweep(store, timelines, -4080, 0)
    data = host_at(NOW, timelines)
    for row in data["quota"]:
        if row["subject"]["subject_id"] == "b":
            row["freshness"] = "stale"
    chart = plugin.reserve_view(store, data, NOW, NOW)["chart"]
    scope = chart["recent_pace_scope"]
    assert (chart["past_accounts"], scope["accounts"], scope["of"]) == (2, 1, 1)
    assert chart["cohort_past"] is not None
    own = [v for _t, v in chart["cohort_past"][:-1] if v is not None]
    assert own and max(own) <= 0.5 + 1e-9               # a alone, never a + b
    assert chart["cohort_past"][-1][1] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# 10: a Refresh with no answer read has an unknown outcome


class _Api:
    def __init__(self, tmp_path):
        self.routes, self.tools, self.logs = {}, {}, []
        self._dir = tmp_path

    def get_state_dir(self):
        return str(self._dir)

    def get_runtime_info(self):
        return {"server_port": 1}

    def log(self, level, message):
        self.logs.append((level, message))

    def register_route(self, name, handler, methods=()):
        self.routes[name] = handler

    def register_tool(self, name, handler, **kw):
        self.tools[name] = handler

    def register_supervised_task(self, *a, **k):
        pass

    def on_unload(self, fn):
        pass

    def register_ui_tab(self, *a, **k):
        pass


@pytest.mark.parametrize("status, error, unknown, compat", [
    (NO_ANSWER, "TimeoutError: timed out", True, False),
    (NO_ANSWER, "RemoteDisconnected: Remote end closed connection without response", True, False),
    (200, "response was not JSON", True, False),
    (0, "URLError: <urlopen error [Errno 61] Connection refused>", False, False),
    (500, "HTTP 500 from /api/claudexor/quota/refresh", False, False),
    (404, "HTTP 404 from /api/claudexor/quota/refresh", False, True),
])
def test_a_refresh_with_no_answer_read_has_an_unknown_outcome(tmp_path, monkeypatch, status, error,
                                                              unknown, compat):
    calls = []

    def fake(_port, path, method="GET", timeout_sec=0):
        calls.append((method, path))
        return None, error, status

    monkeypatch.setattr(plugin, "_request_json", fake)
    api = _Api(tmp_path)
    plugin.register(api)
    result = api.routes["refresh"]({})
    assert calls == [("POST", plugin.REFRESH_PATH)]          # sent once, never again on its own
    assert result["ok"] is False
    assert result["compatibility_error"] is compat
    assert bool(result.get("outcome_unknown")) is unknown
    if unknown:
        assert "unknown" in result["message"] and "failed" not in result["message"]
    elif not compat:
        assert result["message"] == "Live quota refresh failed"


def test_the_transport_tells_a_request_never_sent_from_one_with_no_answer():
    def serve(behaviour):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        held = []

        def run():
            conn, _ = srv.accept()
            held.append(conn)
            conn.settimeout(2)
            try:
                conn.recv(65536)
            except OSError:
                pass
            if behaviour == "close":
                conn.close()

        threading.Thread(target=run, daemon=True).start()
        return srv, held

    for behaviour in ("hang", "close"):
        srv, held = serve(behaviour)
        try:
            out, error, status = plugin._request_json(srv.getsockname()[1], plugin.REFRESH_PATH,
                                                      "POST", timeout_sec=0.5)
        finally:
            for conn in held:
                conn.close()
            srv.close()
        assert out is None and error and status == NO_ANSWER, (behaviour, error, status)
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    out, error, status = plugin._request_json(port, plugin.REFRESH_PATH, "POST", timeout_sec=0.5)
    assert out is None and error and status == 0              # refused: never reached a host


# ---------------------------------------------------------------------------
# 8 and the advisories: what the documentation says


def test_the_documentation_matches_the_figure_and_claims_no_rounding_bound():
    text = Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8")
    assert "beside the figure, never in it" not in text
    assert "stated resolution" not in text
    assert "outcome_unknown" in text


# ---------------------------------------------------------------------------
# 1, 7, 8, 9, 10 and the advisories in the real widget


NODE_R3 = r"""
function bootClock() {
  const made = makeDocument();
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  let intervalCallback = null;
  const disposeHooks = [];
  const errors = [];
  const clock = { shift: 0 };
  const RealDate = Date;
  class ShiftedDate extends RealDate {
    constructor(...args) { if (args.length === 0) super(RealDate.now() + clock.shift); else super(...args); }
    static now() { return RealDate.now() + clock.shift; }
  }
  const window = {
    fetch(url, init = {}) {
      const req = { url, method: init.method || 'GET', init };
      req.promise = new Promise((resolve, reject) => { req.resolve = resolve; req.reject = reject; });
      requests.push(req);
      return req.promise;
    },
    setInterval(cb) { intervalCallback = cb; return 17; },
    clearInterval() {},
    setTimeout(cb, ms) { const id = ++timerId; timers.set(id, { cb, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener() {},
    __ouroWidgetOnDispose(fn) { disposeHooks.push(fn); },
  };
  const quietConsole = { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} };
  const context = vm.createContext({
    window, document: made.document, console: quietConsole, Date: ShiftedDate, Math, Object, Array,
    String, Number, RegExp, Promise, setImmediate, JSON,
  });
  vm.runInContext(instrumentedWidgetSource, context, { filename: 'widget.js' });
  return { root: made.root, requests, errors, clock, poll: () => intervalCallback() };
}
const answer = (value, status = 200) => ({ ok: status >= 200 && status < 300, status,
  text: async () => (typeof value === 'string' ? value : JSON.stringify(value)) });
const click = (env, key) => byFocus(env.root, key).listeners.click[0]({ stopPropagation() {} });
async function respond(env, value, status) {
  await settle();
  const open = env.requests.filter((r) => !r.done);
  open.forEach((r) => { r.done = true; r.resolve(answer(value, status)); });
  await settle();
}
const barStates = (env) => walk(env.root).filter((n) => n.getAttribute('data-state') !== null)
  .map((n) => n.getAttribute('data-state'));
const spoken = (env) => allSpoken(env.root);
// 0.8.0: the future drawn is one scenario line, from the row's own figure.
const paceLines = (env) => walk(env.root).filter((n) => n.tagName === 'PATH'
  && /\bline-scenario\b/.test(n.getAttribute('class') || '')).length;

(async () => {
  const fx = JSON.parse(process.env.R3_FIXTURE);

  // 1. A failed read after the reset: nothing current, no old reset, no
  // forecast; each value dated or "?" with its last reading kept.
  let env = bootClock();
  await respond(env, fx.good);
  assert.ok(barStates(env).every((s) => s === 'current'), barStates(env).join());
  assert.match(env.root.textContent, /0\.70 of 2/);
  // The timeline is open on every mount: its chart is asked for and drawn.
  await respond(env, fx.good_chart);
  assert.ok(paceLines(env) > 0, 'the future is drawn while current');
  env.clock.shift = 3 * 3600 * 1000;                   // 1 h past the reported reset
  env.poll();
  await respond(env, 'bad gateway', 502);
  assert.match(env.root.textContent, /Reading could not be refreshed \(HTTP 502\)/);
  assert.ok(barStates(env).length >= 2 && barStates(env).every((s) => s === 'unknown'), barStates(env).join());
  assert.doesNotMatch(env.root.textContent, /0\.70 of 2/);
  assert.doesNotMatch(spoken(env), /2 current of 2 accounts/);
  assert.doesNotMatch(env.root.textContent, /next reset/);
  assert.match(spoken(env), /last read/);                // the record stays
  assert.equal(paceLines(env), 0, 'no estimate on a kept screen');
  assert.doesNotMatch(env.root.textContent, /Cached data from/);
  assert.match(env.root.textContent, /nothing in it is current/);
  // Before the reset the same failure keeps the values, dated, never current.
  env = bootClock();
  await respond(env, fx.good);
  env.clock.shift = 30 * 60 * 1000;
  env.poll();
  await respond(env, 'bad gateway', 502);
  assert.deepEqual(barStates(env), ['last_known', 'last_known']);
  // The figure is current only: "—", never 0; the kept values are the dated
  // "Last known" line and the hatched bars.
  assert.match(env.root.textContent, /— of 2/);
  assert.match(env.root.textContent, /Last known 0\.70 · /);
  assert.doesNotMatch(env.root.textContent, /0\.70 of 2|0\.00 of 2|incl\./);
  assert.match(spoken(env), /0 current of 2 accounts/);
  assert.doesNotMatch(spoken(env), /restricted now/);
  // The next whole answer is drawn as it is.
  env.poll();
  await respond(env, fx.good);
  assert.ok(barStates(env).every((s) => s === 'current'));

  // 7. The chart narrates the record by its own accounts, not the current count.
  env = bootClock();
  await respond(env, fx.hist);
  click(env, 'horizon:24h');
  await respond(env, fx.hist_chart);
  const plot = byFocus(env.root, 'chart-plot');
  plot.listeners.focus[0]();
  plot.listeners.keydown[0]({ key: 'ArrowLeft', preventDefault() {} });
  const readout = classes(env.root, 'chart-readout')[0].textContent;
  assert.match(readout, /recorded 1\.80 of 3 accounts/, readout);
  assert.doesNotMatch(readout, /of 0 measured/);

  // 8 and the advisory: pace details claim no whole-percent bound, and say
  // the accounts with no reported reset apart.
  env = bootClock();
  await respond(env, fx.paced);
  // The limit's details sit under the timeline's "Details, notes and data".
  const details = classes(env.root, 'chart-table')[0];
  assert.ok(details, 'the timeline carries its details');
  assert.match(details.textContent, /Recent pace/);
  assert.doesNotMatch(env.root.textContent, /±/);
  assert.doesNotMatch(env.root.textContent, /whole-percent/);
  assert.match(details.textContent, /with no reported reset would reach the limit/);

  // 9. A last-known zero is dated and muted, never current exhaustion.
  env = bootClock();
  await respond(env, fx.lastzero);
  const zero = walk(env.root).find((n) => n.getAttribute('data-state') === 'last_known');
  assert.ok(zero, 'a last-known bar');
  assert.doesNotMatch(String(zero.className), /\bspent\b/);
  assert.doesNotMatch(env.root.textContent, /at the limit until/);
  assert.match(env.root.textContent, /at the limit when last read/);
  assert.match(zero.getAttribute('aria-label'), /last known/);

  // 10. The skill says the Refresh outcome is unknown: said so, never "failed",
  // and the POST is not sent again.
  env = bootClock();
  await respond(env, fx.good);
  await respond(env, fx.good_chart);      // the chart the open timeline asks for
  byFocus(env.root, 'refresh').listeners.click[0]();
  await settle();
  const post = env.requests.at(-1);
  assert.equal(post.method, 'POST');
  post.done = true;
  post.resolve(answer({ ok: false, outcome_unknown: true, compatibility_error: false,
                        message: 'Live refresh got no readable answer; whether it ran is unknown.' }));
  await settle();
  assert.match(env.root.textContent, /whether it ran is unknown/);
  assert.doesNotMatch(env.root.textContent, /Live quota refresh failed/);
  env.poll();
  await respond(env, fx.good);
  assert.equal(env.requests.filter((r) => r.method === 'POST').length, 1);
  assert.equal(env.errors.length, 0, env.errors.join('\n'));
  console.log('ok');
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _live(store, base, timelines, start, end, step=120.0):
    """Collector sweeps at real times around ``base``: timelines are
    sid -> [(offset, ratio)], every account resetting at base + 2 h."""
    t = base + start
    while t <= base + end + 1e-6:
        plugin.persist_sweep(store, _status(base, timelines, t), "", t)
        t += step


def _status(base, timelines, t, stale=(), reset=7200.0):
    snaps = []
    for sid, points in timelines.items():
        seen = [p for p in points if base + p[0] <= t + 1e-9]
        if not seen:
            continue
        obs, ratio = seen[-1]
        snaps.append({
            "subject": {"harness": "codex", "subject_id": sid, "plan_label": None},
            "constraints": [{"id": "primary", "label": "primary", "used_ratio": ratio, "window_seconds": WEEK,
                             "resets_at": qs.iso(base + reset)}],
            "availability": {"state": "available"},
            "observed_at": qs.iso(base + obs), "freshness": "stale" if sid in stale else "fresh",
            "source": "app"})
    return payload(snaps, [profile("codex", sid) for sid in timelines], harnesses=("codex",))


def _view(store, data, now, chart):
    out = plugin.build_view(data, "", now)
    if chart:
        # As the widget's first read gets it: the default limit's own span.
        out["reserve"] = widget_reserve(store, data, now, now, harness="codex", name_accounts=True)
    else:
        out["reserve"] = plugin.reserve_view(store, data, now, now, harness="codex", chart=False,
                                             name_accounts=True)
    return out


def _r3_fixture(tmp_path):
    now = float(int(_time.time()))
    live = quota_history.HistoryStore(tmp_path / "live")
    grow = {"a": [(-4080, 0.50), (-2040, 0.60), (-60, 0.70)], "b": [(-4080, 0.60)]}
    _live(live, now, grow, -4080, 0)
    good = _view(live, _status(now, grow, now), now, False)
    good_chart = _view(live, _status(now, grow, now), now, True)
    assert good_chart["reserve"]["chart"]["recent_pace"], "the fixture needs a drawn estimate"
    paced = json.loads(json.dumps(good_chart))
    g = paced["reserve"]["summary"]["groups"][0]
    g["recent_pace"].update({"resolution_windows_per_hour": 0.012, "reach_limit_no_reported_reset": 1,
                             "earliest_no_reset_reach_at": qs.iso(now + 5 * 3600)})
    hist_store = quota_history.HistoryStore(tmp_path / "hist")
    three = {"a": [(-6000, 0.10)], "b": [(-6000, 0.40)], "c": [(-6000, 0.70)]}
    _live(hist_store, now, three, -6000, -600)
    stale = _status(now, three, now, stale=("a", "b", "c"))
    hist = _view(hist_store, stale, now, False)
    # The reader picks the 24-hour span: the record lies in the last two hours.
    hist_chart = plugin.build_view(stale, "", now)
    hist_chart["reserve"] = plugin.reserve_view(hist_store, stale, now, now, harness="codex", horizon="24h",
                                                name_accounts=True)
    assert hist_chart["reserve"]["chart"]["past_accounts"] == 3
    zero = _status(now, {"a": [(-60, 0.3)], "b": [(-1200, 1.0)]}, now, stale=("b",))
    lastzero = _view(quota_history.HistoryStore(tmp_path / "zero"), zero, now, False)
    return {"good": good, "good_chart": good_chart, "paced": paced, "paced_key": g["key"],
            "hist": hist, "hist_chart": hist_chart, "lastzero": lastzero}


def test_real_widget_withdraws_current_claims_and_narrates_the_record(tmp_path):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    widget_path = Path(__file__).with_name("widget.js").resolve()
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run(
        [str(node), "-e", harness + NODE_R3],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path),
             "R3_FIXTURE": json.dumps(_r3_fixture(tmp_path))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")


def test_a_narrow_retry_does_not_wrap():
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    rule = next(line for line in source.splitlines() if ".banner .banner-retry{" in line)
    assert "white-space:nowrap" in rule
