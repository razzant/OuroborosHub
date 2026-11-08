"""The widget is the app's shell in small (capsule: shell): the head - the families, About and Refresh - stands on
the page's ground, and everything under it, whatever the screen is, stands in one card (the --shell-* tokens); the windows of the limits stand in a rail at the card's side."""
import os
import re
import subprocess
from pathlib import Path

import test_quotas
from test_reserve import _node

NODE_SHELL = r"""
const kinds = (node) => node.childNodes.map((n) => String(n.className || '').split(/\s+/)[0]);
const has = (node, name) => String((node && node.className) || '').split(/\s+/).includes(name);
const click = (env, key) => byFocus(env.root, key).listeners.click[0]({ stopPropagation() {} });
(async () => {
  const good = view([account('a', quota()), account('b', quota())]);
  const broken = view([null]);

  // 1. The head and the frame, nothing else in the root. The families, About and Refresh are the head's; the banners, the limits
  // and the accounts are the card's. A family whose limits have no window to list (the reserve is not available) has no rail: the
  // frame is the card alone, whole width, and does not say has-rail. (The frame with a rail is held in test_window_tabs.)
  let env = await boot(good);
  assert.deepEqual(kinds(env.root), ['control-bar', 'shell']);
  let [head, frame] = env.root.childNodes;
  assert.deepEqual(kinds(frame), ['shell-card']);
  assert.ok(!has(frame, 'has-rail'), 'no rail, no class');
  let card = frame.childNodes[0];
  assert.ok(classes(head, 'tab-mark').length && byFocus(head, 'about') && byFocus(head, 'refresh'));
  assert.equal(classes(card, 'tab-mark').length + classes(card, 'btn-word').length + classes(card, 'btn-word-dot').length, 0,
    'the head (the families, the words About and Refresh and the dot between them) is not in the card');
  assert.equal(classes(env.root, 'rail').length, 0);
  for (const name of ['banners', 'reserve', 'accounts']) {
    assert.equal(classes(card, name).length, 1, name + ' stands in the card');
    assert.equal(classes(head, name).length, 0, name + ' does not stand in the head');
  }
  click(env, 'about');
  [head, frame] = env.root.childNodes;
  card = frame.childNodes[frame.childNodes.length - 1];
  assert.deepEqual(kinds(env.root), ['control-bar', 'shell']);
  assert.equal(classes(card, 'about-panel').length, 1, 'About opens in the card');
  assert.equal(classes(head, 'about-panel').length, 0);

  // 2. A family with no account: its empty state is the card's too; the rail is there exactly when the frame says so.
  env = await boot(view([]));
  [head, frame] = env.root.childNodes;
  card = frame.childNodes[frame.childNodes.length - 1];
  assert.deepEqual(kinds(env.root), ['control-bar', 'shell']);
  assert.ok(has(card, 'shell-card') && (kinds(frame)[0] === 'rail') === has(frame, 'has-rail'));
  assert.equal(classes(card, 'empty-card').length, 1, 'the empty state stands in the card');
  assert.equal(classes(head, 'empty-card').length, 0);

  // 3. An answer that cannot be drawn: the screen before it stays (in its frame), the fault is the first banner of its card,
  // under the head, not above it.
  let bad = false;
  env = await boot(() => (bad ? broken : good));
  bad = true;
  env.interval()();
  await settle();
  [head, frame] = env.root.childNodes;
  card = frame.childNodes[frame.childNodes.length - 1];
  assert.deepEqual(kinds(env.root), ['control-bar', 'shell']);
  assert.ok(has(card, 'shell-card') && has(card.childNodes[0], 'draw-fault'), 'the fault is first in the card');
  assert.equal(classes(head, 'draw-fault').length, 0);
  assert.ok(classes(card, 'q-plate').length + classes(card, 'accounts').length, 'the screen before it is kept in the card');
  assert.deepEqual(kinds(frame), ['shell-card'], 'in its frame');

  // 4. A first answer that cannot be drawn: the waiting screen stays, the frame has no rail (the card has the whole width), and the
  // fault and its Retry are the card's.
  env = await boot(broken);
  [head, frame] = env.root.childNodes;
  assert.deepEqual(kinds(env.root), ['control-bar', 'shell']);
  assert.deepEqual(kinds(frame), ['shell-card'], 'no rail');
  assert.ok(!has(frame, 'has-rail'));
  card = frame.childNodes[0];
  assert.ok(has(card.childNodes[0], 'draw-fault'));
  assert.ok(byFocus(card, 'retry') && !byFocus(head, 'retry'));
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
"""


def _style():
    source = Path(__file__).with_name('widget.js').read_text(encoding='utf-8')
    return source.split('var STYLE = [', 1)[1].split("].join('');", 1)[0]


def test_the_head_stands_on_the_ground_and_everything_under_it_in_one_card():
    css = _style()
    # The tokens: the ground, the card's colour and its glow in both themes; the corner and the gap once, as the
    # shell has them; the card's rule reads them all and writes no value of its own.
    dark, light = css.split(':root[data-theme=light]{', 1)
    light = light.split('/* base */', 1)[0]
    for token in ('--shell-bg', '--shell-card-bg', '--shell-card-shadow'):
        assert re.search(re.escape(token) + r':[^;]+;', dark) and re.search(re.escape(token) + r':[^;]+;', light), token
    assert '--shell-card-radius:20px;' in dark and '--shell-card-gap:var(--space-2);' in dark
    assert not re.search(r'--shell-card-(?:radius|gap):', light), 'the corner and the gap are the same in both themes'
    assert re.search(r'"body\{(?:[^"\\]|\\.)*background:var\(--shell-bg\)', css), 'the page is the shell\'s ground'
    rule = re.search(r'"\.shell-card\{([^"]*)\}"', css).group(1)
    for declaration in ('background:var(--shell-card-bg)', 'border-radius:var(--shell-card-radius)',
                        'box-shadow:var(--shell-card-shadow)'):
        assert declaration in rule, declaration
    # The gap stands only between the top row and the card: a card alone (no top row) keeps the page's own edge.
    assert '".control-bar+.shell{margin-top:var(--shell-card-gap)}"' in css
    assert 'margin-top' not in rule
    # The frame: one column without a rail; the rail of rings (--q-rail-w) and the card side by side with it, a block on a phone (520px and under).
    assert '".shell{display:grid;grid-template-columns:minmax(0,1fr);min-width:0}"' in css
    assert '".shell.has-rail{grid-template-columns:var(--q-rail-w) minmax(0,1fr);column-gap:var(--space-2);align-items:start}"' in css
    assert '--q-rail-w:96px;' in dark, 'the rail is as wide as the ring capsule with its longest short name (Claude and GPT 7d) asks'
    assert '"@media(max-width:520.98px){.shell.has-rail{display:block}.has-rail>.shell-card{margin-top:var(--space-2)}}"' in css
    assert not re.search(r'"[^"]*\.(?:shell|rail)[^"]*:has\(', css), 'the frame is told by a class the script sets, not by :has'
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|gradient\(|backdrop-filter|font-size', rule), rule
    # The capsule stands after the kit and before the others.
    assert css.index('/* kit: inputs */') < css.index('/* capsule: shell */') < css.index('/* capsule: head */')
    assert css.index('/* kit: ring */') < css.index('/* capsule: shell */')

    # The markup, in the widget's own run: the head outside the card, everything else inside it.
    widget = Path(__file__).with_name('widget.js').resolve()
    node = _node()
    assert node is not None, 'Node is required'
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    result = subprocess.run([str(node), '-e', harness + NODE_SHELL], cwd=Path(__file__).parent,
                            env={**os.environ, 'WIDGET_PATH': str(widget)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
