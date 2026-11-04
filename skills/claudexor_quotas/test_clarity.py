"""0.6.3: literal names and terminal lifecycle in the actual widget script
(0.8.0: one screen — About instead of a settings panel, no saved display choice).

The fixtures pass through the existing backend projector. The same fake DOM as
other widget tests lets us control fetch settlement and stop at precise races.
Real opaque-frame browser QA is retained separately, outside the payload.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import plugin
import test_quotas
from test_reserve import _node, _widget_fixture, payload, profile, snap, WEEK

NODE_CLARITY = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  node.listeners.click[0]({stopPropagation() {}});
}
function deferredFetch(env) {
  const pending = [];
  env.window.fetch = (url, options = {}) => {
    env.calls.push({url, method:options.method || 'GET', body:options.body});
    return new Promise((resolve, reject) => pending.push({resolve, reject}));
  };
  return pending;
}
(async () => {
  const fx = JSON.parse(process.env.CLARITY_FIXTURE);
  const mode = process.env.CLARITY_CASE;
  if (mode === 'names') {
    // Labels and ids that coincide with Object.prototype names are literal keys.
    for (const view of fx.names) {
      const env = await boot(view);
      const account = view.groups[0].accounts[0];
      click(env, 'acct:' + account.key);
      const lines = classes(env.root, 'win-line');
      assert.equal(lines.length, 1);
      assert.match(lines[0].textContent, /25% used/);
      assert.equal(classes(lines[0], 'win-tag')[0].title, account.quota.constraints[0].label);
      assert.ok(byFocus(env.root, 'acct:' + account.key));
    }
    // A future harness id may coincide with Object.prototype too.
    const v = JSON.parse(JSON.stringify(fx.names[0]));
    v.groups[0].harness_id = 'constructor'; v.groups[0].family_label = 'Constructor';
    v.groups[0].accounts[0].key = 'constructor:sample';
    const env = await boot(v);
    assert.equal(classes(env.root, 'harness-initial').length, 1);
    assert.match(byFocus(env.root, 'harness:constructor').textContent, /Constructor/);
    click(env, 'acct:constructor:sample');
    assert.equal(classes(env.root, 'inspector').length, 1);
  } else if (mode === 'dispose') {
    let env = await boot(fx.summary);
    const count = env.calls.length;
    assert.equal(env.disposeHooks[0](), undefined, 'nothing is left to flush: no display choice is saved');
    assert.equal(env.intervalCleared(), true);
    assert.equal((env.document.listeners.click || []).length, 0);
    assert.equal(env.document.listeners.keydown.length, 0);
    assert.equal(env.document.listeners.pointermove.length, 0);
    env.interval()(); env.windowListeners.pageshow[0]();
    env.document.listeners.visibilitychange[0]();
    await settle();
    assert.equal(env.calls.length, count, 'terminal dispose must not revive');
    // A Refresh in the air at disposal: its answer starts no read after it
    // and draws nothing.
    env = await boot(fx.summary);
    const pending = deferredFetch(env);
    click(env, 'refresh');
    await settle();
    assert.equal(pending.length, 1);
    assert.equal(env.calls.at(-1).method, 'POST');
    const before = env.root.textContent;
    env.disposeHooks[0]();
    pending[0].resolve(response({ ok: true, quota_updates: [] }));
    await settle();
    assert.equal(pending.length, 1, 'no read follows a refresh answered after disposal');
    assert.equal(env.root.textContent, before);
  } else if (mode === 'queued_chart') {
    // A chart request the widget queues for itself (another limit's row
    // picked) cannot start once the frame is stopped.
    const other = fx.summary.reserve.summary.groups.find((g) => !g.tightest);
    for (const terminal of [false, true]) {
      const env = await boot(fx.summary); const count = env.calls.length;
      showKey(env, other.key);
      if (terminal) env.disposeHooks[0](); else env.windowListeners.pagehide[0]();
      await settle();
      assert.equal(env.calls.length, count, 'deferred chart request after stop');
    }
  } else if (mode === 'generation_success' || mode === 'generation_failure') {
    const env = await boot(fx.summary); const pending = deferredFetch(env);
    env.interval()(); await settle(); assert.equal(pending.length, 1);
    env.windowListeners.pagehide[0](); env.windowListeners.pageshow[0](); await settle();
    assert.equal(pending.length, 2, 'BFCache restoration starts one read');
    if (mode === 'generation_success') pending[0].resolve(response(fx.summary));
    else pending[0].reject(new Error('old generation failed'));
    await settle(); env.interval()(); await settle();
    assert.equal(pending.length, 2, 'old settlement cannot release new request lock');
    assert.equal(byFocus(env.root, 'refresh').disabled, true);
    pending[1].resolve(response(fx.summary)); await settle();
    assert.equal(byFocus(env.root, 'refresh').disabled, false);
    env.interval()(); await settle(); assert.equal(pending.length, 3);
    pending[2].resolve(response(fx.summary)); await settle(); env.disposeHooks[0]();
  } else if (mode === 'clarity') {
    const env = await boot(fx.summary);
    assert.ok(classes(env.root, 'tab-mark-name').every((n) => n.textContent));
    // Both are text actions (the kit's .btn-word): the name of each is the word that is seen, with no label and no title. Refresh has
    // the one word, busy or idle; what a reader hears leaves out a node that is aria-hidden (the dot between the two).
    const heard = (node) => (node.getAttribute('aria-hidden') === 'true' ? '' : (node._text || '') + node.childNodes.map(heard).join(''));
    assert.equal(heard(byFocus(env.root, 'refresh')), 'Refresh');
    assert.equal(heard(byFocus(env.root, 'about')), 'About');
    for (const key of ['refresh', 'about']) {
      assert.equal(byFocus(env.root, key).getAttribute('aria-label'), null);
      assert.equal(byFocus(env.root, key).getAttribute('title'), null);
    }
    // One screen: rows, the timeline open on its own, the account list folded;
    // the top account of the row picked on its own (its card stands under the plate) and no settings panel.
    assert.equal(classes(env.root, 'lrow').length, 1, 'one plate, of the picked window');
    assert.ok(walk(env.root).some((n) => n.getAttribute('class') === 'chart-svg'), 'the timeline is open');
    // The word "Accounts" is a pressed/unpressed word now (aria-pressed), not an expander.
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'false');
    assert.equal(classes(env.root, 'inspector').length, 1, 'the top account is picked by default');
    // The top is the first bar with today's reading and room left, not the held one (c1) nor the first by place.
    const picked = classes(env.root, 'bar').filter((b) => b.getAttribute('aria-pressed') === 'true');
    assert.equal(picked.length, 1);
    assert.match(picked[0].getAttribute('data-focus'), /:codex:c2$/);
    assert.match(classes(env.root, 'inspector')[0].textContent, /c2/);
    assert.ok(byFocus(env.root, 'settings') === undefined);
    // The cooling account says it once, with its scope and end.
    click(env, 'accounts');
    click(env, 'acct:codex:c1');
    const card = classes(env.root, 'inspector')[0];
    assert.equal((card.textContent.match(/Cooling down/g) || []).length, 1);
    assert.match(card.textContent, /whole account/);
    assert.match(card.textContent, /until/);
    assert.equal(byFocus(env.root, 'inspector-diag').getAttribute('aria-expanded'), 'false');
    click(env, 'inspector-diag');
    assert.ok(classes(env.root, 'win-line').length);
    // About opens in place, beside everything else; Escape closes it and
    // gives the keyboard back to its button. A second Escape clears the
    // selected account.
    click(env, 'about');
    assert.equal(byFocus(env.root, 'about').getAttribute('aria-expanded'), 'true');
    assert.ok(classes(env.root, 'about-panel').length && classes(env.root, 'lrow').length);
    env.document.listeners.keydown[0]({ key: 'Escape' });
    assert.equal(env.document.activeElement.getAttribute('data-focus'), 'about');
    assert.ok(!classes(env.root, 'about-panel').length);
    env.document.listeners.keydown[0]({ key: 'Escape' });
    assert.equal(classes(env.root, 'inspector').length, 0);
    env.disposeHooks[0]();
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""


@pytest.mark.parametrize('case', ['names', 'dispose', 'queued_chart', 'generation_success', 'generation_failure', 'clarity'])
def test_clarity_runtime(tmp_path, case):
    now = time.time()
    names = []
    for label in ['constructor primary', '__proto__ primary', 'toString primary']:
        data = payload([snap('codex', 'sample', [{'id':'primary', 'label':label,
            'window_seconds':WEEK, 'used_ratio':.25}], observed_abs=now - 10)],
            [profile('codex','sample')], harnesses=('codex',))
        view = plugin.build_view(data, '')
        assert view['groups'][0]['accounts'][0]['quota']['constraints'][0]['label'] == label
        names.append(view)
    fixture = {'names': names, 'summary': _widget_fixture(tmp_path)['view']}
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    harness = harness.replace('    root: made.root,', '    window,\n    root: made.root,')
    widget = Path(os.environ.get('CLARITY_WIDGET_PATH', Path(__file__).with_name('widget.js'))).resolve()
    node = _node()
    assert node is not None, 'Node is required'
    result = subprocess.run([str(node), '-e', harness + NODE_CLARITY], cwd=Path(__file__).parent,
        env={**os.environ, 'WIDGET_PATH':str(widget), 'CLARITY_CASE':case, 'CLARITY_FIXTURE':json.dumps(fixture)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_clarity_has_one_readable_style_system():
    source = Path(__file__).with_name('widget.js').read_text()
    css = source.split('var STYLE = [',1)[1].split("].join('');",1)[0]
    import re
    # (no gradient and no mask anywhere except the haze at the edge of a run that scrolls: the six --ladder-haze-* tokens are the only gradients, and
    # the masks that wear them are all under `.rail-scroll*`: the opened ladder window (.rail-scroll-start / -end, top and bottom) and the row of
    # the bars that goes sideways (.rail-scroll-x*, left and right); the narrow rail's row still wraps onto the next line, it does not fade)
    haze = re.findall(r'\.rail-scroll[^{]*\{[^}]*\}', css)
    assert haze and any('mask-image' in rule and 'var(--ladder-haze-y)' in rule for rule in haze)
    masks = re.findall(r'(?:-webkit-)?mask-image:([^;}]*)', ''.join(haze))
    assert masks and all(re.fullmatch(r'none|var\(--ladder-haze-[xy](?:-start|-end)?\)', value) for value in masks)
    tokens = re.findall(r'--ladder-haze-[xy](?:-start|-end)?:linear-gradient\([^;]*\);?', css)
    assert len(tokens) == 6, 'the haze is written once, as six tokens'
    rest = css
    for rule in haze + tokens:
        rest = rest.replace(rule, '')
    assert not re.search(r'gradient\(|mask-image|backdrop-filter|glass|font-size:(?:[0-9]|1[01])(?:\.\d+)?px', rest)
    assert not re.search(r'backdrop-filter|glass|font-size:(?:[0-9]|1[01])(?:\.\d+)?px', css)
    assert all(f'--type-{role}:{size}px' in css for role,size in [('meta',12),('body',14),('section',16)])
    assert '--row-h:32px' in css
    assert 'focus-visible' in css and 'prefers-reduced-motion' in css
