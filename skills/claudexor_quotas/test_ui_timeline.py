"""The calm timeline of the real widget: one row of controls, the combined headline, model choice, folded details.

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
  const fx = JSON.parse(require('fs').readFileSync(process.env.UI_FIXTURE_FILE, 'utf8'));
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
    // The graph is the plate's continuation: the row of controls (span, future, Combined, model), the plot, the short
    // legend, then Details — in that order. It has no head of its own and, in the window mode, no headline: both would
    // say what the plate above already says.
    const order = ['tl-controls', 'chart-plot', 'calm-legend', 'chart-table'].map((c) => indexOf(block, c));
    assert.ok(order.every((i) => i >= 0), 'every part is there: ' + order);
    assert.deepEqual(order.slice().sort((a, b) => a - b), order, 'calm order, nothing long before the graph');
    assert.equal(indexOf(block, 'tl-head'), -1, 'no head (and no line under it) in the graph');
    assert.equal(indexOf(block, 'tl-hero'), -1, 'the window mode draws no headline: the plate has those figures');
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
    // One plate beside the rail of rings (not a stack of rows): one strip, of the picked limit; the rings are a group of pressed buttons.
    assert.equal(classes(env.root, 'q-bars').length, 1);
    assert.equal(classes(env.root, 'q-plate').length, 1);
    assert.equal(classes(env.root, 'q-rings')[0].getAttribute('role'), 'group');
    assert.equal(classes(env.root, 'q-ring').filter((r) => r.getAttribute('aria-pressed') === 'true').length, 1);
    // No tab strip in the graph (the window is picked in the row above, and no tab of it would be a second one); the one
    // thing it holds that the plate does not is Combined, a switch in its row of controls.
    assert.equal(classes(block, 'wt-tab').length, 0);
    assert.equal(classes(block, 'tab-mark-num').length, 0);
    assert.equal(classes(block, 'wt-bar').length, 0);
    assert.ok(!walk(block).some((n) => n.getAttribute('role') === 'tablist' || n.getAttribute('role') === 'tab'), 'no tablist, not even of one');
    const row = classes(block, 'tl-controls')[0];
    const inRow = walk(row).filter((n) => n.tagName === 'BUTTON').map((n) => n.getAttribute('data-focus'));
    assert.deepEqual(inRow, ['horizon:24h', 'horizon:7d', 'scenario:no_new_use', 'scenario:recent_pace', 'graph:joint', 'tl-model'],
      'the row: the span, the future, Combined, and the model choice last');
    env.disposeHooks[0]();
  } else if (mode === 'switch') {
    const env = await boot((url) => answer(url));
    const block = () => classes(env.root, 'tl-block')[0];
    const sw = () => byFocus(env.root, 'graph:joint');
    // Combined is one switch among the graph's controls: pressed or not, with the original words on it.
    assert.ok(sw(), 'Combined is a control of the graph');
    assert.equal(sw().tagName, 'BUTTON');
    assert.equal(text(sw()), 'Combined');
    assert.equal(sw().getAttribute('aria-pressed'), 'false', 'off: the graph draws the plate\'s window');
    assert.equal(sw().getAttribute('role'), null, 'a switch, not a tab');
    assert.match(sw().title, /^Per account the smaller of its binding limits, in units of its shortest window/);
    assert.ok(walk(classes(block(), 'tl-controls')[0]).includes(sw()), 'it stands in the row of controls, beside the span and the future');
    assert.equal(classes(block(), 'wt-tab').length, 0, 'no tab of the window in the graph');
    assert.equal(classes(block(), 'tab-mark-num').length, 0);
    assert.equal(classes(env.root, 'tl-hero').length, 0, 'the window mode draws no headline: the plate has those figures');
    const calls0 = env.calls.length;
    const shownKey = fx.default.reserve.chart.group_key;
    click(env, 'graph:joint');
    await sleep();
    assert.equal(sw().getAttribute('aria-pressed'), 'true', 'Combined on');
    const hero = text(classes(env.root, 'tl-hero')[0]);
    assert.match(hero, /equivalents left/);
    assert.match(hero, /counted/);
    assert.match(hero, /Next reset/);
    assert.equal(classes(block(), 'tl-hero').length, 1, 'Combined has its headline: its numbers are not the plate\'s');
    assert.equal(env.calls.length, calls0, 'Combined needs no new request: the chart on hand carries it');
    // Combined is the graph's own state: the rail keeps its pick.
    assert.deepEqual(classes(env.root, 'q-ring').filter((r) => r.getAttribute('aria-pressed') === 'true').map((r) => r.getAttribute('data-limit')), [shownKey],
      'the plate is still the same limit');
    // The same control turns it off again: the window, with no headline and no new request.
    click(env, 'graph:joint');
    await sleep();
    assert.equal(sw().getAttribute('aria-pressed'), 'false');
    assert.equal(classes(env.root, 'tl-hero').length, 0);
    assert.equal(env.calls.length, calls0, 'the window on the plate needs no new request');
    // Another window is picked in the window row: it asks for its own chart once, and the block follows it, back on the window.
    const other = fx.default.reserve.summary.groups.find((g) => g.key !== shownKey && g.window_seconds
      && byFocus(env.root, 'ring:' + g.harness + ':' + g.key)
      && g.window_seconds !== fx.default.reserve.summary.groups.find((x) => x.key === shownKey).window_seconds);
    assert.ok(other, 'another window has a ring of the rail');
    click(env, 'graph:joint');
    assert.equal(sw().getAttribute('aria-pressed'), 'true');
    click(env, 'ring:' + other.harness + ':' + other.key);
    await sleep(); await sleep();
    const asked = env.calls.filter((c) => c.url.includes('group=' + encodeURIComponent(other.key)));
    assert.equal(asked.length, 1, 'one chart request for the picked window');
    assert.equal(sw().getAttribute('aria-pressed'), 'false', 'a window picked above takes the graph back from Combined');
    env.disposeHooks[0]();
  } else if (mode === 'hidden_rows') {
    // The graph's controls are the same whatever the family's other windows are, or are hidden by the reader.
    const probe = await boot((url) => answer(url));
    const shown = fx.default.reserve.chart.group_key;
    const row = (env) => walk(classes(classes(env.root, 'tl-block')[0], 'tl-controls')[0]).filter((n) => n.tagName === 'BUTTON')
      .map((n) => n.getAttribute('data-focus'));
    const all = row(probe);
    assert.ok(all.includes('graph:joint') && all.includes('tl-model'), all.join());
    const gone = fx.default.reserve.summary.groups.find((g) => g.key !== shown && g.window_seconds).key;
    probe.disposeHooks[0]();
    const v = JSON.parse(JSON.stringify(fx.default));
    v.display = { hidden: [gone] };
    const env = await boot(() => v);
    assert.deepEqual(row(env), all, 'hiding a row changes nothing in the graph\'s controls');
    assert.equal(classes(classes(env.root, 'tl-block')[0], 'wt-tab').length, 0);
    assert.equal(classes(classes(env.root, 'tl-block')[0], 'tab-mark-num').length, 0);
    env.disposeHooks[0]();
  } else if (mode === 'model') {
    const env = await boot((url) => answer(url));
    const btn = byFocus(env.root, 'tl-model');
    assert.ok(btn, 'the model choice is a visible control, in the graph\'s row of controls');
    assert.ok(walk(classes(classes(env.root, 'tl-block')[0], 'tl-controls')[0]).includes(btn), 'the same row as the span and the future');
    assert.equal(classes(classes(env.root, 'tl-block')[0], 'tl-head').length, 0, 'not in a head of the graph: it has none');
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
    // The top account is already picked when the widget opens, so its row is pressed only when the pick is not there yet: a second press would clear it.
    if (byFocus(env.root, 'acct:' + acct.key).getAttribute('aria-pressed') !== 'true') click(env, 'acct:' + acct.key);
    await sleep();
    assert.equal(byFocus(env.root, 'acct:' + acct.key).getAttribute('aria-pressed'), 'true', 'the account is picked');
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
  } else if (mode === 'captions') {
    // Span and Future say what they are: each caption is quiet text in view at the head of its capsule, not a button.
    const en = I18N_ANSWER.en;
    const env = await boot((url) => answer(url));
    const row = classes(classes(env.root, 'tl-block')[0], 'tl-controls')[0];
    const labels = classes(row, 'seg-label');
    assert.deepEqual(labels.map(text), [en['chart.span'], en['chart.future']], 'the texts are the table\'s');
    assert.deepEqual(labels.map(text), ['Span', 'Future']);
    labels.forEach((label) => {
      assert.ok(!String(label.className).split(/\s+/).includes('sr-only'), 'in view, not for a screen reader only: ' + label.className);
      assert.equal(label.tagName, 'SPAN', 'a caption, not a button');
      assert.equal(label.getAttribute('data-focus'), null, 'no stop of the keyboard');
      assert.equal(label.getAttribute('tabindex'), null);
      assert.equal(label.getAttribute('role'), null);
    });
    // Each heads its own segment, which still names itself for a screen reader; the row's buttons are the same as before.
    const groups = classes(row, 'seg').filter((s) => s.getAttribute('role') === 'group');
    assert.deepEqual(groups.map((s) => s.getAttribute('aria-label')), ['Span', 'Future']);
    groups.forEach((s, i) => assert.ok(s.childNodes[0] === labels[i], 'the caption comes first in its segment'));
    assert.deepEqual(walk(row).filter((n) => n.tagName === 'BUTTON').map((n) => n.getAttribute('data-focus')),
      ['horizon:24h', 'horizon:7d', 'scenario:no_new_use', 'scenario:recent_pace', 'graph:joint', 'tl-model']);
    env.disposeHooks[0]();
  } else if (mode === 'idle') {
    // A pace nobody has measured draws what No new use draws: Recent pace is muted, says why, and still works.
    const PHRASE = 'No account has a measured pace yet, so this line is the same as No new use';
    const clone = (v) => JSON.parse(JSON.stringify(v));
    // where: the scenarios the plot draws, the window's or Combined's (Combined has none in the fixture: it gets a copy).
    const withPace = (view, where, atPace) => {
      const v = clone(view);
      const an = v.reserve.chart.analytics;
      if (where === 'joint' && !an.joint.scenarios) an.joint.scenarios = clone(an.window.scenarios);
      const scenarios = (where === 'window' ? an.window : an.joint).scenarios;
      scenarios.recent_pace.at_pace = atPace;
      if (where === 'window') v.reserve.chart.scenarios = clone(scenarios);
      return v;
    };
    const pace = (env) => byFocus(env.root, 'scenario:recent_pace');
    const idle = (node) => String(node.className).split(/\s+/).includes('idle');
    const legend = (env) => text(classes(env.root, 'calm-legend')[0]);

    let env = await boot(() => withPace(fx.default, 'window', 0));
    let b = pace(env);
    assert.ok(idle(b) && String(b.className).split(/\s+/).includes('seg-opt'), b.className);
    assert.equal(b.getAttribute('data-idle'), 'true');
    assert.equal(b.title, PHRASE);
    assert.equal(b.getAttribute('aria-label'), 'Recent pace — ' + PHRASE, 'the same phrase, appended to the name');
    assert.equal(text(b), 'Recent pace', 'not hidden and not renamed');
    const calm = byFocus(env.root, 'scenario:no_new_use');
    assert.ok(!idle(calm) && calm.getAttribute('data-idle') === null && calm.getAttribute('aria-label') === null);
    assert.equal(calm.title, 'Every account read now keeps its share and refills once at its next reported reset');
    assert.match(legend(env), /no new use: a condition, not a forecast/);
    // The button works: it picks the line, and the legend says that line is the same as no new use.
    click(env, 'scenario:recent_pace');
    await sleep();
    b = pace(env);
    assert.equal(b.getAttribute('aria-pressed'), 'true');
    assert.ok(idle(b), 'chosen it is still the idle one');
    assert.match(legend(env), /recent pace: no measured pace yet — same as no new use/);
    assert.doesNotMatch(legend(env), /a condition, not a forecast/);
    assert.ok(legend(env).length <= 140, legend(env));
    assert.ok(walk(env.root).some((n) => n.getAttribute('class') === 'chart-svg'), 'the plot stays');
    env.disposeHooks[0]();

    // A measured pace: nothing muted, the old title and legend.
    env = await boot(() => withPace(fx.default, 'window', 2));
    b = pace(env);
    assert.ok(!idle(b) && b.getAttribute('data-idle') === null && b.getAttribute('aria-label') === null);
    assert.equal(b.title, 'The accounts with a measured recent pace keep it; the others are held');
    click(env, 'scenario:recent_pace');
    await sleep();
    assert.match(legend(env), /recent pace: a condition, not a forecast/);
    assert.doesNotMatch(legend(env), /no measured pace yet/);
    env.disposeHooks[0]();

    // Combined reads the scenarios it draws, not the mode: a measured window beside an idle Combined, and the other way round.
    env = await boot(() => withPace(withPace(fx.default, 'window', 2), 'joint', 0));
    assert.ok(!idle(pace(env)), 'the window draws a measured pace');
    click(env, 'graph:joint');
    await sleep();
    assert.ok(idle(pace(env)), 'Combined draws none');
    assert.equal(pace(env).getAttribute('data-idle'), 'true');
    click(env, 'scenario:recent_pace');
    await sleep();
    assert.match(legend(env), /recent pace: no measured pace yet/);
    click(env, 'graph:joint');
    await sleep();
    assert.ok(!idle(pace(env)), 'back on the window, the pace is measured again');
    env.disposeHooks[0]();
    env = await boot(() => withPace(withPace(fx.default, 'window', 0), 'joint', 3));
    assert.ok(idle(pace(env)), 'the window draws none');
    click(env, 'graph:joint');
    await sleep();
    assert.ok(!idle(pace(env)), 'Combined draws a measured pace');
    assert.equal(pace(env).getAttribute('data-idle'), null);
    env.disposeHooks[0]();
  } else if (mode === 'how') {
    // Details, facts first: the method is one fold more, "How it is counted", and keeps its own state through a redraw.
    const env = await boot((url) => answer(url));
    const outer = () => classes(env.root, 'chart-table')[0];
    const how = () => classes(env.root, 'chart-how')[0];
    assert.equal(classes(env.root, 'chart-how').length, 1, 'one fold for the method');
    assert.equal(how().tagName, 'DETAILS');
    assert.ok(walk(outer()).includes(how()), 'inside the Details');
    const summary = byFocus(env.root, 'chart-how');
    assert.equal(summary.tagName, 'SUMMARY');
    assert.equal(text(summary), 'How it is counted');
    assert.ok(walk(how()).includes(summary) && walk(how())[1] === summary, 'its summary heads it');
    const all = walk(outer());
    // The facts stand outside the fold, before it; the notes and the data are inside it.
    for (const cls of ['chart-legend', 'tl-facts', 'schedule', 'reserve-detail']) {
      const nodes = classes(outer(), cls);
      if (cls !== 'tl-facts') assert.ok(nodes.length, cls + ' is there'); // the pace lines are drawn only at a measured pace
      nodes.forEach((n) => {
        assert.ok(!walk(how()).includes(n), cls + ' is not folded');
        assert.ok(all.indexOf(n) < all.indexOf(how()), cls + ' comes before the fold');
      });
    }
    assert.ok(classes(how(), 'chart-notes').length, 'the notes are in the fold');
    assert.equal(classes(outer(), 'chart-notes').filter((n) => !walk(how()).includes(n)).length, 0, 'none of them outside');
    const tables = all.filter((n) => n.tagName === 'TABLE' && !String(n.className).split(/\s+/).includes('schedule'));
    assert.ok(tables.length >= 1, 'the data table is there');
    assert.ok(tables.every((n) => walk(how()).includes(n)), 'and in the fold');
    assert.match(text(how()), /recorded changes/);
    // Open, it survives the redraw of the poll, with the keyboard on its summary; the Details around it are its own state.
    assert.ok(!how().open && !outer().open);
    how().open = true;
    how().listeners.toggle[0]();
    summary.focus();
    const before = how();
    env.interval()();
    await sleep(); await sleep();
    assert.notEqual(how(), before, 'the poll drew it again');
    assert.equal(how().open, true, 'and left it open');
    assert.ok(!outer().open, 'the Details are not opened by it');
    assert.equal(env.document.activeElement.getAttribute('data-focus'), 'chart-how');
    assert.ok(env.document.activeElement === byFocus(env.root, 'chart-how'));
    // Shut, it stays shut; the Details open on their own.
    how().open = false;
    how().listeners.toggle[0]();
    outer().open = true;
    outer().listeners.toggle[0]();
    env.interval()();
    await sleep(); await sleep();
    assert.ok(!how().open, 'shut stays shut');
    assert.equal(outer().open, true);
    env.disposeHooks[0]();
  } else if (mode === 'joint_unit') {
    // The combined view's unit is one phrase per length, so a language can decline it; in English it is today's phrase.
    const clone = (v) => JSON.parse(JSON.stringify(v));
    const seen = async (seconds) => {
      const v = clone(fx.default);
      v.reserve.chart.analytics.joint.unit.window_seconds = seconds;
      const env = await boot(() => v);
      click(env, 'graph:joint');
      await sleep();
      const out = { hero: text(classes(env.root, 'tl-unit')[0]), note: text(classes(env.root, 'tl-joint')[0]),
                    axis: text(walk(env.root).find((n) => n.getAttribute('class') === 'unit-text')) };
      env.disposeHooks[0]();
      return out;
    };
    const english = [[18000, '5-hour window equivalents left', '5-hour window equivalents now'],
      [604800, 'Weekly window equivalents left', 'Weekly window equivalents now'],
      [172800, '2-day window equivalents left', '2-day window equivalents now'],
      [1209600, '2-week window equivalents left', '2-week window equivalents now'],
      [86400, 'Daily window equivalents left', 'Daily window equivalents now'],
      [3600, 'Hourly window equivalents left', 'Hourly window equivalents now'],
      [120, '2-minute window equivalents left', '2-minute window equivalents now'],
      [60, 'Per-minute window equivalents left', 'Per-minute window equivalents now'],
      [0, 'No stated length window equivalents left', 'No stated length window equivalents now']];
    for (const [seconds, left, now] of english) {
      const out = await seen(seconds);
      assert.equal(out.hero, left, 'the headline, ' + seconds);
      assert.equal(out.axis, left, 'the axis caption says the same, ' + seconds);
      assert.ok(out.note.includes(now + ' · 0 of 3 accounts counted'), out.note);
    }
    // Russian: each length has its own phrase; the one the pack lacks stays English.
    Object.assign(I18N_ANSWER, { language: 'ru', revision: 1, plural_select: null, strings: {
      'joint.left_h': '{n}-часовых окон осталось',
      'joint.now_h': '{n}-часовых окон сейчас',
      'joint.left_week': 'Недельных окон осталось',
      'joint.counted': 'учтено {known}, всего {of}',
      'joint.next_reset': 'Ближайший сброс' } });
    const five = await seen(18000);
    assert.equal(five.hero, '5-часовых окон осталось');
    assert.equal(five.axis, five.hero);
    assert.ok(five.note.includes('5-часовых окон сейчас · учтено 0, всего 3 accounts'), five.note);
    assert.equal((await seen(604800)).hero, 'Недельных окон осталось');
    assert.equal((await seen(172800)).hero, '2-day window equivalents left', 'a phrase the pack lacks stays English');
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""


@pytest.fixture(scope="module")
def fixture_file(tmp_path_factory):
    directory = tmp_path_factory.mktemp("uitimeline")
    path = directory / "views.json"
    path.write_text(json.dumps(_views(directory)), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("case", ["composition", "switch", "hidden_rows", "model", "model_ignored", "no_choice", "money",
                                  "captions", "idle", "how", "joint_unit"])
def test_calm_timeline(fixture_file, case):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget = Path(os.environ.get("UI_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
    result = subprocess.run([str(node), "-e", harness + NODE_UI], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": str(widget), "UI_CASE": case, "UI_FIXTURE_FILE": fixture_file},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
