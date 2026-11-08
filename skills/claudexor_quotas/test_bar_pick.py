"""The picked bar: the top account picked at the opening and what keeps or lets go of the pick.

What is held here (Node, the fake DOM of the other widget tests):
- at the opening the top current bar of the window is picked (a bar hatched as last known, a held one and one at the limit are not taken at the top); with no account
  in the row nothing is picked; the widget's own pick stays through the poll, goes back to the top when its account is gone from the row, stays let go within a window
  and comes back with a change of window; a press picks and a second press lets go; the picked account's card stands beside the bars;
- a step of the ladder with one account is the pick of that bar; a step of several accounts lights their bars and picks none;
- with every limit hidden the widget's own pick goes with the plate, the reader's own stays; the billing-cycle budget (Cursor) picks its top account too.
The rectangles of the plate in real browsers are held by test_window_tabs.py.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

import test_quotas
from test_reserve import _node, _widget_fixture

WIDGET = Path(os.environ.get("BARS_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()

GROUP = "codex|primary|604800|-"
C1, C2, C3 = "codex:c1", "codex:c2", "codex:c3"


def bar(account, key=GROUP):
    return 'bar:%s:%s' % (key, account)


@pytest.fixture(scope="module")
def base_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("bars"))["view"]


def _variant(base, order=None, free=(), stale=(), no_account=False):
    """The fixture view with the bars of the first limit in the order `order` (accounts), `free` accounts without a hold, `stale` accounts last known."""
    view = json.loads(json.dumps(base))
    group = view["reserve"]["summary"]["groups"][0]
    bars = {b["account"]: b for b in group["bars"]}
    for account in free:
        bars[account].update(restricted=False, flags=[])
    for account in stale:
        bars[account].update(state="last_known", age_seconds=7200)
    group["bars"] = [bars[a] for a in (order or [C1, C3, C2])]
    if no_account:
        for b in group["bars"]:
            b.pop("account", None)
    return view


# ---------------------------------------------------------------------------
# Node: the choice itself, in the fake DOM.

NODE_BARS = r"""
const cls = (n) => String(n.className || '').split(/\s+/);
const has = (n, name) => cls(n).includes(name);
const click = (env, key) => {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
};
const settle2 = async () => { for (let i = 0; i < 6; i++) await settle(); };
const pressed = (env) => classes(env.root, 'q-bar').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.getAttribute('data-focus'));
const sels = (env) => classes(env.root, 'q-bar').filter((b) => has(b, 'sel')).map((b) => b.getAttribute('data-focus'));
const bar = (g, account) => 'bar:' + g.key + ':' + account;
// The card of the pick beside the bars (the stand-in "pick an account" is no card of an account).
const cards = (root) => classes(root, 'q-card').filter((c) => !has(c, 'q-card-none')).length;
function showLimit(env, g) {
  const ring = byFocus(env.root, 'ring:' + g.harness + ':' + g.key);
  assert.ok(ring, 'a ring for ' + g.key);
  if (ring.getAttribute('aria-pressed') !== 'true') ring.listeners.click[0]({ stopPropagation() {} });
  assert.equal(classes(env.root, 'q-plate').length, 1, 'the plate of ' + g.key + ' is shown');
}
const poll = async (env) => { env.interval()(); await settle2(); };
// "Rows": the boxes of the Rows menu (one for each limit of the family), the answer of the skill that echoes what was saved, a box unchecked.
const rowsMenu = (env) => classes(env.root, 'rows-menu')[0];
const rowBoxes = (env) => walk(rowsMenu(env)).filter((n) => n.tagName === 'INPUT');
const pressedRows = (env) => walk(env.root).filter((n) => /^acct:/.test(n.getAttribute('data-focus') || '') && n.getAttribute('aria-pressed') === 'true')
  .map((n) => n.getAttribute('data-focus'));
let saved = [];
const serveRows = (env, view) => {
  env.window.fetch = (url, options = {}) => {
    if (options.method === 'POST') { saved = JSON.parse(options.body).hidden; return Promise.resolve(response({ ok: true, display: { hidden: saved } })); }
    return Promise.resolve(response(Object.assign({}, view, { display: { hidden: saved } })));
  };
};
// Rule: while a plate is drawn the Rows menu lives in the head of the account list (the word over the bars opens it), no longer under the limits;
// so a reader opens the list first. With no plate drawn Rows stays under the limits, the one way back to a hidden row, and nothing needs opening.
const openRows = async (env) => {
  if (!rowsMenu(env)) { click(env, 'accounts'); await settle2(); }
  assert.ok(rowsMenu(env), 'the Rows menu is drawn');
};
const hideNext = async (env) => {
  const box = rowBoxes(env).find((b) => b.checked);
  assert.ok(box, 'a limit left to hide');
  box.checked = false;
  box.listeners.change[0]();
  await settle2();
};
const respond = (env, view) => {
  env.window.fetch = (url, options = {}) => Promise.resolve(response(options.method === 'POST' ? { ok: true } : view));
};

(async () => {
  const fx = JSON.parse(process.env.BARS_FIXTURE);
  const mode = process.env.BARS_CASE;
  const groups = (v) => v.reserve.summary.groups.filter((g) => (g.bars || []).some((b) => b.account));
  const env = await boot(fx.views.base);
  const [first, second] = groups(fx.views.base);
  assert.ok(first && second, 'the fixture has two limits with named bars');
  const FIRST_PICK = bar(first, 'codex:c2');
  let cleanup = () => env.disposeHooks[0]();

  if (mode === 'top_current_not_the_stale_above') {
    // The base row: c1 held, c3 hatched (last known), c2 current: c2 is picked, and it is the only one.
    assert.deepEqual(sels(env), [FIRST_PICK]);
    assert.deepEqual(pressed(env), [FIRST_PICK]);
    assert.equal(cards(env.root), 1, 'its card stands');
    // A stale bar on top with a current free one under it: the current one is picked.
    cleanup();
    const env2 = await boot(fx.views.fresh);
    const g2 = groups(fx.views.fresh)[0];
    assert.deepEqual(sels(env2), [bar(g2, 'codex:c1')]);
    assert.ok(!sels(env2).includes(bar(g2, 'codex:c3')), 'the stale bar above is not picked');
    cleanup = () => env2.disposeHooks[0]();
    // Nothing current: the first bar that names an account.
    cleanup();
    const env3 = await boot(fx.views.all_stale);
    const g3 = groups(fx.views.all_stale)[0];
    assert.deepEqual(sels(env3), [bar(g3, 'codex:c3')]);
    cleanup = () => env3.disposeHooks[0]();
  } else if (mode === 'no_accounts_no_pick') {
    cleanup();
    const bare = await boot(fx.views.bare);
    assert.deepEqual(sels(bare), []);
    assert.deepEqual(pressed(bare), []);
    assert.equal(cards(bare.root), 0, 'no card');
    assert.equal(classes(bare.root, 'q-card-none').length, 1, 'the card says to pick an account');
    await poll(bare);
    assert.deepEqual(sels(bare), []);
    cleanup = () => bare.disposeHooks[0]();
  } else if (mode === 'auto_pick_holds_at_the_poll_and_returns_to_the_top') {
    assert.deepEqual(sels(env), [FIRST_PICK]);
    await poll(env);
    await poll(env);
    assert.deepEqual(sels(env), [FIRST_PICK], 'the automatic pick holds through the poll');
    // c2 gone from the row: the top bar of what is left is picked (c1 is held, c3 hatched: the first that names an account, c1).
    const gone = JSON.parse(JSON.stringify(fx.views.base));
    gone.reserve.summary.groups[0].bars = gone.reserve.summary.groups[0].bars.filter((b) => b.account !== 'codex:c2');
    respond(env, gone);
    await poll(env);
    assert.equal(sels(env).length, 1, 'one bar is picked');
    assert.ok(!sels(env).includes(FIRST_PICK), 'the account that is gone is not picked');
    assert.equal(cards(env.root), 1);
  } else if (mode === 'let_go_stays_in_the_window_and_comes_back_with_the_window') {
    assert.deepEqual(sels(env), [FIRST_PICK]);
    click(env, FIRST_PICK);
    await settle2();
    assert.deepEqual(sels(env), [], 'the pick is let go');
    await poll(env);
    await poll(env);
    assert.deepEqual(sels(env), [], 'the automatic pick does not come back in this window');
    assert.equal(cards(env.root), 0);
    showLimit(env, second);
    await settle2();
    assert.equal(sels(env).length, 1, 'another window: its top bar is picked');
    showLimit(env, first);
    await settle2();
    assert.deepEqual(sels(env), [FIRST_PICK], 'the window is back: the top bar is picked again');
  } else if (mode === 'a_press_picks_and_a_second_press_lets_go') {
    // A press picks and a second press lets go, and the card follows.
    click(env, bar(first, 'codex:c1'));
    await settle2();
    assert.deepEqual(sels(env), [bar(first, 'codex:c1')]);
    assert.equal(cards(env.root), 1);
    click(env, bar(first, 'codex:c1'));
    await settle2();
    assert.deepEqual(sels(env), []);
    assert.equal(cards(env.root), 0);
  } else if (mode === 'a_step_of_one_account_picks_it_a_step_of_several_lights_them') {
    // The ladder of the limit on screen: a step of one account is the pick of its bar (pressed again, it lets the step go); a step of several lights their bars
    // (the others step back) and picks none.
    const steps = classes(env.root, 'q-step');
    assert.ok(steps.length >= 1, 'the ladder has steps');
    const one = steps.find((st) => !/ · \d+ acc$/.test(classes(st, 'q-step-back')[0].textContent));
    assert.ok(one, 'fixture supplies a single-account step');
    {
      one.listeners.click[0]({ stopPropagation() {} });
      await settle2();
      assert.equal(sels(env).length, 1, 'a step of one account picks its bar');
      assert.equal(cards(env.root), 1);
    }
    const lit = JSON.parse(JSON.stringify(fx.views.base));
    const g0 = lit.reserve.summary.groups[0];
    const at = g0.bars.find((b) => b.state === 'current' && b.resets_at).resets_at;
    g0.bars.forEach((b) => { if (b.state === 'current' && b.account) b.resets_at = at; });
    cleanup();
    const env2 = await boot(lit);
    cleanup = () => env2.disposeHooks[0]();
    click(env2, sels(env2)[0]);
    await settle2();
    assert.deepEqual(sels(env2), [], 'the pick let go first');
    const several = classes(env2.root, 'q-step').find((st) => / · \d+ acc$/.test(classes(st, 'q-step-back')[0].textContent));
    assert.ok(several, 'a step of several accounts');
    several.listeners.click[0]({ stopPropagation() {} });
    await settle2();
    const strip = classes(env2.root, 'q-bars')[0];
    assert.equal(strip.getAttribute('data-step'), '1', 'the row is lit');
    const lights = classes(strip, 'q-bar').filter((b) => has(b, 'lit')).map((b) => b.getAttribute('data-account'));
    assert.ok(lights.length >= 2, 'their bars are lit: ' + lights);
    assert.deepEqual(sels(env2), [], 'and none is picked');
    assert.equal(byFocus(env2.root, classes(env2.root, 'q-step').find((st) => st.getAttribute('aria-pressed') === 'true').getAttribute('data-focus')).getAttribute('aria-pressed'), 'true');
    env2.document.listeners.keydown[0]({ key: 'Escape' });
    await settle2();
    assert.equal(classes(env2.root, 'q-bars')[0].getAttribute('data-step'), null, 'Escape lets the step go');
  } else if (mode === 'every_limit_hidden_the_pick_of_the_widget_goes_with_the_plate') {
    // "Rows": the limits hidden one after the other. While a plate is shown the top bar of its window is picked; with every limit hidden there is no plate and no bar, and the
    // pick the widget made itself goes: no card of an account nobody chose under Accounts, no row of the list pressed (the view at rest).
    serveRows(env, fx.views.base);
    assert.deepEqual(sels(env), [FIRST_PICK]);
    await openRows(env);
    await hideNext(env);
    assert.ok(classes(env.root, 'q-plate').length > 0, 'another limit is still shown');
    assert.equal(sels(env).length, 1, 'its top bar is picked');
    while (rowBoxes(env).some((b) => b.checked)) await hideNext(env);
    const atRest = (label) => {
      assert.equal(classes(env.root, 'q-plate').length, 0, label + ': no plate');
      assert.deepEqual(sels(env), [], label + ': no bar is picked');
      assert.equal(cards(env.root) + classes(env.root, 'inspector').length, 0, label + ': no card of a pick nobody made');
      assert.deepEqual(pressedRows(env), [], label + ': no row of the list is pressed');
    };
    atRest('hidden');
    click(env, 'accounts');
    await settle2();
    atRest('the list toggled');
    click(env, 'accounts');
    await settle2();
    await poll(env);
    atRest('after the poll');
    // Show all: the plate is back and the top bar of its window is picked afresh.
    const all = walk(rowsMenu(env)).find((n) => n.tagName === 'BUTTON' && /Show all rows/.test(n.textContent));
    assert.ok(all, 'Show all rows');
    all.listeners.click[0]({ stopPropagation() {} });
    await settle2();
    assert.ok(classes(env.root, 'q-plate').length > 0, 'the plate is back');
    assert.equal(sels(env).length, 1, 'the top bar is picked again');
    assert.equal(cards(env.root), 1, 'with its card');
  } else if (mode === 'every_limit_hidden_the_pick_of_the_reader_stays') {
    // The reader's own pick is theirs: with every limit hidden its card stays (under its row of the list, or under the list's header); only the widget's own pick goes.
    serveRows(env, fx.views.base);
    click(env, bar(first, 'codex:c1'));
    await settle2();
    assert.deepEqual(sels(env), [bar(first, 'codex:c1')]);
    await openRows(env);
    while (rowBoxes(env).some((b) => b.checked)) await hideNext(env);
    assert.equal(classes(env.root, 'q-plate').length, 0, 'no plate');
    assert.equal(classes(env.root, 'inspector').length, 1, 'the card of the reader\'s pick stays, under the head of the list');
    await poll(env);
    assert.equal(classes(env.root, 'inspector').length, 1, 'and stays through the poll');
  } else if (mode === 'cursor_cycle_budget_picks_its_top_account_too') {
    // The budget of the billing cycle (Cursor) picks its top account at the opening, as a window does (the owner's composition has a card for every family);
    // the pick holds through the poll and a press on it lets it go.
    cleanup();
    for (const key of ['cursor_budget', 'cursor_mixed']) {
      const cur = await boot(fx.views[key]);
      assert.equal(classes(cur.root, 'q-plate-cycle').length, 1, key + ': the plate of the cycle');
      assert.ok(classes(cur.root, 'q-bar').length > 0, key + ': it has its bars');
      assert.equal(sels(cur).length, 1, key + ': its top bar is picked');
      assert.equal(cards(cur.root), 1, key + ': with its card');
      await poll(cur);
      assert.equal(sels(cur).length, 1, key + ': through the poll');
      click(cur, sels(cur)[0]);
      await settle2();
      assert.deepEqual(sels(cur), [], key + ': a press lets it go');
      cur.disposeHooks[0]();
    }
    cleanup = () => {};
  } else {
    throw new Error('unknown case ' + mode);
  }
  cleanup();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""

NODE_CASES = ["top_current_not_the_stale_above", "no_accounts_no_pick", "auto_pick_holds_at_the_poll_and_returns_to_the_top",
              "let_go_stays_in_the_window_and_comes_back_with_the_window", "a_press_picks_and_a_second_press_lets_go",
              "a_step_of_one_account_picks_it_a_step_of_several_lights_them",
              "every_limit_hidden_the_pick_of_the_widget_goes_with_the_plate", "every_limit_hidden_the_pick_of_the_reader_stays",
              "cursor_cycle_budget_picks_its_top_account_too"]


@pytest.fixture(scope="module")
def cursor_views(tmp_path_factory):
    """Cursor with a budget per billing cycle (cursor_budget, cursor_mixed), built the way test_window_tabs builds them."""
    from test_window_tabs import _cursor_views
    return _cursor_views(tmp_path_factory.mktemp("bars-cursor"))


@pytest.mark.parametrize("case", NODE_CASES)
def test_node_the_choice_of_the_top_bar(request, base_view, case):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    harness = harness.replace("    root: made.root,", "    window,\n    root: made.root,")
    assert "    window,\n    root: made.root," in harness
    views = {"base": base_view,
             "fresh": _variant(base_view, order=[C3, C1, C2], free=[C1], stale=[C3]),
             "all_stale": _variant(base_view, order=[C3, C1, C2], stale=[C3, C1, C2]),
             "bare": _variant(base_view, no_account=True)}
    if case.startswith("cursor_"):
        views.update(request.getfixturevalue("cursor_views"))
    result = subprocess.run(
        [str(node), "-e", harness + NODE_BARS], cwd=WIDGET.parent,
        env={**os.environ, "WIDGET_PATH": str(WIDGET), "BARS_CASE": case, "BARS_FIXTURE": json.dumps({"views": views})},
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
