"""The calm timeline of the real widget: tabs, headline, model choice, folded details.

The fixtures pass through the real backend (reserve_view with analytics over a real
HistoryStore); the same fake DOM as the other widget tests lets us see the DOM order,
the requests the widget sends and the redraws, without a browser. Real-browser
keyboard, poll and failure behaviour at 18 accounts is covered by _qa-ui/ui_qa.py.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import plugin
import quota_history
import quota_summary as qs
import test_quotas
from test_reserve import _node, payload, profile, WEEK, FIVE_H

LONG_MODEL = "m" * 80  # as long as the skill's display bound: possibly cut, never offered


def _snap(sid, now, kinds=False, at=None):
    # Use drifts with time, so the record has stretches of its own (a constant value draws nothing).
    drift = 0.0 if at is None else (at - now) / 6 / 3600 * 0.05

    def row(cid, ratio, window, models=None, label=None, request_kinds=None):
        out = {"id": cid, "label": label or cid, "used_ratio": round(min(0.95, ratio + drift), 4), "window_seconds": window,
               "resets_at": qs.iso(now + (3600 if window == FIVE_H else 2 * 86400))}
        if models is not None:
            out["applies_to_models"] = models
        if request_kinds is not None:
            out["applies_to_request_kinds"] = request_kinds
        return out
    cons = [row("five_hour", 0.2, FIVE_H), row("seven_day", 0.4, WEEK),
            row("weekly_one", 0.3, WEEK, models=["model-one"], label="Weekly model-one"),
            row("weekly_long", 0.1, WEEK, models=[LONG_MODEL], label="Weekly long")]
    if kinds:
        cons.append(row("image_daily", 0.2, 86400, request_kinds=["image"], label="Image daily"))
    return {"subject": {"harness": "claude", "subject_id": sid, "plan_label": None}, "constraints": cons,
            "availability": {"state": "available"}, "observed_at": qs.iso((now if at is None else at) - 20), "freshness": "fresh", "source": "app"}


def _views(tmp_path, kinds=True):
    now = float(int(time.time()))
    store = quota_history.HistoryStore(tmp_path)
    ids = ("a", "b", "c")

    def status(at=None):
        return payload([_snap(s, now, kinds, at) for s in ids], [profile("claude", s) for s in ids], harnesses=("claude",))
    t = now - 6 * 3600
    while t <= now:
        plugin.persist_sweep(store, status(t), "", t)
        t += 600
    out = {}
    for tag, model, kind in (("default", None, "text"), ("one", "model-one", "text"), ("image", None, "image")):
        view = plugin.build_view(status(), "")
        # As the widget's first read gets it: the chart of the default limit over that limit's own span.
        for horizon in ("24h", "7d"):
            view["reserve"] = plugin.reserve_view(store, status(), now, now, harness="claude", horizon=horizon,
                                                  name_accounts=True, analytics=True, model=model, kind=kind)
            chart = view["reserve"]["chart"]
            own = next(g for g in view["reserve"]["summary"]["groups"] if g["key"] == chart["group_key"])
            if (own["window_seconds"] or 0) <= 86400 or horizon == "7d":
                break
        out[tag] = view
    assert out["default"]["reserve"]["chart"]["analytics"]["joint"]["available"]
    return out


NODE_UI = r"""
const sleep = () => settle();
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  node.listeners.click[0]({stopPropagation() {}});
}
const text = (n) => n.textContent;
function indexOf(root, cls) { return walk(root).findIndex((n) => String(n.className || '').split(/\s+/).includes(cls)); }
(async () => {
  const fx = JSON.parse(process.env.UI_FIXTURE);
  const mode = process.env.UI_CASE;
  // Answers by what the URL asks for, as the route does.
  function answer(url) {
    const m = /[?&]model=([^&]*)/.exec(url);
    const k = /[?&]kind=([^&]*)/.exec(url);
    if (k && decodeURIComponent(k[1]) === 'image') return fx.image;
    if (m && decodeURIComponent(m[1]) === 'model-one') return fx.one;
    return fx.default;
  }
  if (mode === 'composition') {
    const env = await boot((url) => answer(url));
    const block = classes(env.root, 'tl-block')[0];
    assert.ok(block, 'a timeline block under the charted row');
    // head (tabs + model), hero, controls, plot, short legend, then Details — in that order.
    const order = ['tl-head', 'tl-hero', 'tl-controls', 'chart-plot', 'calm-legend', 'chart-table'].map((c) => indexOf(block, c));
    assert.ok(order.every((i) => i >= 0), 'every part is there: ' + order);
    assert.deepEqual(order.slice().sort((a, b) => a - b), order, 'calm order, nothing long before the graph');
    // Everything the calm view folds away lives under Details: the schedule, the long legend, the joint paragraph.
    const details = classes(block, 'chart-table')[0];
    for (const cls of ['schedule', 'chart-legend']) {
      const nodes = classes(block, cls);
      assert.ok(nodes.length, cls + ' is kept');
      assert.ok(nodes.every((n) => walk(details).includes(n)), cls + ' only under Details');
    }
    assert.ok(classes(block, 'tl-joint').every((n) => walk(details).includes(n)), 'combined figures only under Details');
    // The short legend names only what is drawn, in few words.
    const legend = text(classes(block, 'calm-legend')[0]);
    assert.ok(legend.length <= 140, legend);
    assert.match(legend, /recorded/);
    assert.match(legend, /no new use: a condition, not a forecast/);
    // One curve: nothing in the block is hatched.
    assert.ok(!walk(block).some((n) => n.tagName === 'PATTERN'));
    // The row bars are still there, one strip per limit.
    assert.equal(classes(env.root, 'bars').length, fx.default.reserve.summary.groups.length);
    // The tab strip and the headline.
    const tabs = classes(block, 'tl-tab').map(text);
    assert.equal(tabs[0], 'Combined');
    assert.ok(tabs.length >= 3, tabs.join('|'));
    const hero = text(classes(block, 'tl-hero')[0]);
    assert.match(hero, /Next reset/);
    env.disposeHooks[0]();
  } else if (mode === 'tabs') {
    const env = await boot((url) => answer(url));
    const block = classes(env.root, 'tl-block')[0];
    const tabs = classes(block, 'tl-tab');
    assert.equal(tabs.filter((t) => t.getAttribute('aria-selected') === 'true').length, 1);
    assert.equal(tabs.filter((t) => t.getAttribute('tabindex') === '0').length, 1, 'one tab stop in the tablist');
    assert.equal(classes(block, 'tl-tabs')[0].getAttribute('role'), 'tablist');
    assert.ok(classes(block, 'tl-tabs')[0].listeners.keydown.length, 'arrow keys are handled on the strip');
    const calls0 = env.calls.length;
    click(env, 'tab:joint');
    await sleep();
    const now = classes(env.root, 'tl-tab');
    assert.equal(now[0].getAttribute('aria-selected'), 'true', 'Combined chosen');
    assert.match(text(classes(env.root, 'tl-hero')[0]), /equivalents left/);
    assert.match(text(classes(env.root, 'tl-hero')[0]), /counted/);
    assert.equal(env.calls.length, calls0, 'Combined needs no new request: the chart on hand carries it');
    // A window tab asks for its own chart once, and the block follows it.
    const other = fx.default.reserve.summary.groups.find((g) => g.key !== fx.default.reserve.chart.group_key && g.window_seconds
      && classes(env.root, 'tl-tab').some((t) => t.getAttribute('data-focus') === 'tab:' + g.key));
    assert.ok(other, 'another window is a tab');
    click(env, 'tab:' + other.key);
    await sleep(); await sleep();
    const asked = env.calls.filter((c) => c.url.includes('group=' + encodeURIComponent(other.key)));
    assert.equal(asked.length, 1, 'one chart request for the picked window');
    env.disposeHooks[0]();
  } else if (mode === 'hidden_rows') {
    const probe = await boot((url) => answer(url));
    const tabKeys = classes(probe.root, 'tl-tab').map((t) => t.getAttribute('data-focus').slice(4)).filter((k) => k !== 'joint');
    const gone = tabKeys.find((k) => k !== fx.default.reserve.chart.group_key);
    assert.ok(gone);
    probe.disposeHooks[0]();
    const v = JSON.parse(JSON.stringify(fx.default));
    v.display = { hidden: [gone] };
    const env = await boot(() => v);
    assert.ok(!byFocus(env.root, 'tab:' + gone), 'a window the reader hid from the rows is not a tab either');
    env.disposeHooks[0]();
  } else if (mode === 'model') {
    const env = await boot((url) => answer(url));
    const btn = byFocus(env.root, 'tl-model');
    assert.ok(btn, 'the model choice is a visible control beside the tabs');
    assert.equal(btn.getAttribute('aria-expanded'), 'false');
    assert.ok(!classes(env.root, 'tl-model-panel').length);
    click(env, 'tl-model');
    await sleep();
    const panel = classes(env.root, 'tl-model-panel')[0];
    assert.ok(panel);
    const opts = classes(panel, 'seg-opt').map(text);
    // A request naming no model (not every model) + reported names; cut names are not offered.
    assert.deepEqual(opts.filter((o) => o !== 'Text' && o !== 'Image'), ['No model named', 'model-one']);
    assert.match(btn.getAttribute('aria-label'), /a request naming no model/);
    assert.match(text(panel), /Names too long to be sure of in full are not offered/);
    assert.ok(opts.includes('Image'), 'an image-only window exists, so the request kind is a choice');
    // Choosing a model asks once, with joint=1 and the exact name.
    const before = env.calls.length;
    click(env, 'tl-model:model-one');
    await sleep(); await sleep();
    const asked = env.calls.slice(before).filter((c) => c.url.includes('model=model-one'));
    assert.equal(asked.length, 1, env.calls.slice(before).map((c) => c.url).join('\n'));
    assert.ok(asked[0].url.includes('joint=1'));
    assert.ok(!asked[0].url.includes('kind='), 'text is the default and is not sent');
    assert.match(text(byFocus(env.root, 'tl-model')), /model-one/);
    assert.ok(classes(env.root, 'tl-model-panel').length, 'the list survives the redraw');
    assert.ok(!classes(env.root, 'is-updating').length, 'the answer matches the choice: nothing dimmed');
    // Escape closes the list, and the keyboard goes back to its button.
    env.document.listeners.keydown[0]({ key: 'Escape' });
    assert.ok(!classes(env.root, 'tl-model-panel').length);
    assert.equal(env.document.activeElement.getAttribute('data-focus'), 'tl-model');
    // The request kind rides the same request.
    click(env, 'tl-model');
    click(env, 'tl-model:');
    await sleep(); await sleep();
    click(env, 'tl-kind:image');
    await sleep(); await sleep();
    assert.ok(env.calls.some((c) => c.url.includes('kind=image')));
    env.disposeHooks[0]();
  } else if (mode === 'model_ignored') {
    // A route that answers for another scope than the one asked: the screen says so, dims the graph, and
    // asks once — never in a loop, and a poll does not turn it into a claim.
    const env = await boot(() => fx.default);
    click(env, 'tl-model');
    click(env, 'tl-model:model-one');
    await sleep(); await sleep(); await sleep();
    const asks = env.calls.filter((c) => c.url.includes('model=model-one') && c.url.includes('reuse=1'));
    assert.equal(asks.length, 1, 'asked once for the scope, then left alone: ' + asks.length);
    assert.ok(classes(env.root, 'is-updating').length, 'the graph on screen is of the previous choice and is said to be');
    assert.match(text(classes(env.root, 'tl-updating')[0]), /previous choice/);
    assert.ok(walk(env.root).some((n) => n.getAttribute('class') === 'chart-svg'), 'the graph stays');
    env.interval()(); await sleep(); await sleep();
    assert.ok(classes(env.root, 'tl-model-panel').length, 'a poll leaves the open list alone');
    assert.match(text(byFocus(env.root, 'tl-model')), /model-one/, 'a poll keeps the chosen model');
    env.disposeHooks[0]();
  } else if (mode === 'money') {
    // Amounts are decimal strings: unit "minor" is scaled by decimal_places inside the digits, never as a float;
    // another unit is shown as sent with its unit and scale, never read as a major-unit amount.
    const v = JSON.parse(JSON.stringify(fx.default));
    const acct = v.groups[0].accounts[0];
    const now = new Date().toISOString();
    const bal = (id, amount, extra = {}) => Object.assign({ id, label: id, amount, unit: 'minor', currency: 'USD', decimal_places: 2,
      has_balance: true, unlimited: false, scope: 'personal', usage_kind: 'total', period: null, fee_included: true, note: '' }, extra);
    acct.resources = {
      balances: { state: 'current', observed_at: now, rows: [
        bal('main', '2345678'), bal('zero', '0'), bal('cent', '5'), bal('huge', '123456789012345678901234567890'),
        bal('whole', '42', { decimal_places: 0 }), bal('noscale', '777', { decimal_places: null }),
        bal('old', '2345678', { unit: 'cents' }), bal('nounit', '9', { unit: '' }),
        bal('missing', null), bal('team', '250', { scope: 'team', note: 'team figure, not this profile\'s personal quota' }),
        bal('unl', null, { unlimited: true }), bal('neg', '-1250') ] },
      spending: { state: 'current', observed_at: now, rows: [
        { id: 's', label: 'spend', enabled: true, used: '250', limit: '10000', unit: 'minor', currency: 'USD', decimal_places: 2,
          scope: null, usage_kind: null, period: null, fee_included: null, note: '' },
        { id: 'n', label: 'unused', enabled: true, used: null, limit: null, unit: 'minor', currency: 'USD', decimal_places: 2,
          scope: null, usage_kind: null, period: null, fee_included: null, note: '' } ] },
      resets: { state: 'not_reported', observed_at: null, rows: [] },
      diagnostics: { state: 'current', observed_at: now, rows: [] },
    };
    const env = await boot(() => v);
    click(env, 'accounts');
    click(env, 'acct:' + acct.key);
    await sleep();
    const said = classes(env.root, 'account-resources').map(text).join('\n');
    const lines = classes(env.root, 'tl-scope').map(text);
    const has = (s) => assert.ok(lines.some((l) => l.includes(s)), 'missing: ' + s + '\n' + lines.join('\n'));
    has('main: 23,456.78 USD');
    has('zero: 0.00 USD');
    has('cent: 0.05 USD');
    has('huge: 1,234,567,890,123,456,789,012,345,678.90 USD');
    has('whole: 42 USD');
    has('noscale: 777 USD minor units (scale not reported)');
    has('old: 2345678 cents \u00b7 USD \u00b7 2 decimal places');
    has('nounit: 9 unit not reported \u00b7 USD \u00b7 2 decimal places');
    has('missing: amount not reported');
    has('team: 2.50 USD');
    has('unl: unlimited');
    has('neg: -12.50 USD');
    has('spend: 2.50 USD of 100.00 USD');
    has('unused: not reported');
    assert.ok(!lines.some((l) => /2345678 USD|23,456\.78 cents/.test(l)), 'cents are never read as dollars, minor never as major');
    assert.ok(lines.some((l) => l.includes('team figure')), 'a team figure says it is the team\'s');
    env.disposeHooks[0]();
  } else if (mode === 'no_choice') {
    // A family whose limits name no model and no request kind has nothing to choose.
    const v = JSON.parse(JSON.stringify(fx.default));
    v.reserve.summary.groups.forEach((g) => { g.models = []; g.models_omitted = 0; g.scope = null; });
    const env = await boot(() => v);
    assert.ok(!byFocus(env.root, 'tl-model'), 'no model button when there is nothing to pick');
    env.disposeHooks[0]();
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""


@pytest.fixture(scope="module")
def fixture_json(tmp_path_factory):
    return json.dumps(_views(tmp_path_factory.mktemp("uitimeline")))


@pytest.mark.parametrize("case", ["composition", "tabs", "hidden_rows", "model", "model_ignored", "no_choice", "money"])
def test_calm_timeline(fixture_json, case):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget = Path(os.environ.get("UI_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
    result = subprocess.run([str(node), "-e", harness + NODE_UI], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": str(widget), "UI_CASE": case, "UI_FIXTURE": fixture_json},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
