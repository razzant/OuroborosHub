"""The picked bar and the line of its pick: the top account picked at the opening, the stem, the line, the stand-ins.

What is held here:
- at the opening the top current bar of the window is picked (a bar hatched as last known, a held one and one at the limit are not taken at the top); with no account
  in the row nothing is picked; the widget's own pick stays through the poll, goes back to the top when its account is gone from the row, stays let go within a window
  and comes back with a change of window; a press does not draw the bars anew (the nodes of `.bars` are the same);
- the picked bar is open and red, the chart stands in the plate right under `.bars` and the figure below it; a pick of another bar folds the chart and opens it
  again, the Details of the chart live through it; a step of the ladder with one account is the pick of that bar, a step of several accounts lights their bars and picks none;
- the stem is one (none with nothing picked), a bar of the line's width, no tip; the two halves of the line are of one length and one time and meet opposite the stem;
- the stand-ins are seen before the line has met, the real content is hidden then, and after the meeting no stand-in is left; the poll starts neither the stand-ins nor the line;
- quick presses: the last one wins and nothing is left hanging; reduced motion: the end state at once.
Real browsers (Chromium, and WebKit where Playwright has it) for the rectangles and the motion, Node (the fake DOM of the other widget tests) for the choice itself.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

import test_quotas
from test_reserve import _node, _widget_fixture, with_i18n

WIDGET = Path(os.environ.get("BARS_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
pytest.importorskip("playwright.sync_api")

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


def _same_reset(view, first, second):
    group = view["reserve"]["summary"]["groups"][0]
    at = next(b for b in group["bars"] if b["account"] == first)["resets_at"]
    for b in group["bars"]:
        if b["account"] == second:
            b["resets_at"] = at
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
const pressed = (env) => classes(env.root, 'bar').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.getAttribute('data-focus'));
const sels = (env) => classes(env.root, 'bar').filter((b) => has(b, 'sel')).map((b) => b.getAttribute('data-focus'));
const bar = (g, account) => 'bar:' + g.key + ':' + account;
function showLimit(env, g) {
  const tab = byFocus(env.root, 'win:' + g.harness + ':' + g.window_seconds);
  assert.ok(tab, 'a tab for the window of ' + g.key);
  if (tab.getAttribute('aria-selected') !== 'true') tab.listeners.click[0]({ stopPropagation() {} });
  const scope = byFocus(env.root, 'scope:' + g.key);
  if (scope && scope.getAttribute('aria-pressed') !== 'true') scope.listeners.click[0]({ stopPropagation() {} });
  assert.ok(byFocus(env.root, 'limit:' + g.key), 'the plate of ' + g.key + ' is shown');
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
    assert.equal(classes(env.root, 'inspector').length, 1, 'its card stands');
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
    assert.equal(classes(bare.root, 'inspector').length, 0, 'no card');
    assert.equal(classes(bare.root, 'bar-arrow').length, 0, 'no stem');
    assert.equal(classes(bare.root, 'wt-plate').filter((p) => p.childNodes.some((n) => has(n, 'tl-block') || has(n, 'tl-wrap'))).length, 0, 'the chart is not in the plate');
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
    assert.equal(classes(env.root, 'inspector').length, 1);
  } else if (mode === 'let_go_stays_in_the_window_and_comes_back_with_the_window') {
    assert.deepEqual(sels(env), [FIRST_PICK]);
    click(env, FIRST_PICK);
    await settle2();
    assert.deepEqual(sels(env), [], 'the pick is let go');
    await poll(env);
    await poll(env);
    assert.deepEqual(sels(env), [], 'the automatic pick does not come back in this window');
    assert.equal(classes(env.root, 'inspector').length, 0);
    showLimit(env, second);
    await settle2();
    assert.equal(sels(env).length, 1, 'another window: its top bar is picked');
    showLimit(env, first);
    await settle2();
    assert.deepEqual(sels(env), [FIRST_PICK], 'the window is back: the top bar is picked again');
  } else if (mode === 'a_press_picks_and_a_second_press_lets_go') {
    // The fake DOM has no `querySelector` on the root, so the widget draws the page anew for a press here (selectInPlace answers false): the identity of the `.bars` nodes
    // is held in the real browsers below. Here: a press picks and a second press lets go, and the card follows.
    click(env, bar(first, 'codex:c1'));
    await settle2();
    assert.deepEqual(sels(env), [bar(first, 'codex:c1')]);
    assert.equal(classes(env.root, 'inspector').length, 1);
    click(env, bar(first, 'codex:c1'));
    await settle2();
    assert.deepEqual(sels(env), []);
    assert.equal(classes(env.root, 'inspector').length, 0);
  } else if (mode === 'the_poll_starts_no_stand_ins_and_no_line') {
    const seen = () => ['tl-skel', 'plate-trace', 'tl-wait'].filter((c) => classes(env.root, c).length);
    await poll(env);
    await poll(env);
    assert.deepEqual(seen(), [], 'no stand-ins and no live line after the poll');
    assert.deepEqual(sels(env), [FIRST_PICK]);
  } else if (mode === 'every_limit_hidden_the_pick_of_the_widget_goes_with_the_plate') {
    // "Rows": the limits hidden one after the other. While a plate is shown the top bar of its window is picked; with every limit hidden there is no plate and no bar, and the
    // pick the widget made itself goes: no card of an account nobody chose under Accounts, no row of the list pressed (the view at rest).
    serveRows(env, fx.views.base);
    assert.deepEqual(sels(env), [FIRST_PICK]);
    await openRows(env);
    await hideNext(env);
    assert.ok(classes(env.root, 'wt-plate').length > 0, 'another limit is still shown');
    assert.equal(sels(env).length, 1, 'its top bar is picked');
    while (rowBoxes(env).some((b) => b.checked)) await hideNext(env);
    const atRest = (label) => {
      assert.equal(classes(env.root, 'wt-plate').length, 0, label + ': no plate');
      assert.deepEqual(sels(env), [], label + ': no bar is picked');
      assert.equal(classes(env.root, 'inspector').length, 0, label + ': no card of a pick nobody made');
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
    assert.ok(classes(env.root, 'wt-plate').length > 0, 'the plate is back');
    assert.equal(sels(env).length, 1, 'the top bar is picked again');
    assert.equal(classes(env.root, 'inspector').length, 1, 'with its card');
  } else if (mode === 'every_limit_hidden_the_pick_of_the_reader_stays') {
    // The reader's own pick is theirs: with every limit hidden its card stays (under its row of the list, or under the list's header); only the widget's own pick goes.
    serveRows(env, fx.views.base);
    click(env, bar(first, 'codex:c1'));
    await settle2();
    assert.deepEqual(sels(env), [bar(first, 'codex:c1')]);
    await openRows(env);
    while (rowBoxes(env).some((b) => b.checked)) await hideNext(env);
    assert.equal(classes(env.root, 'wt-plate').length, 0, 'no plate');
    assert.equal(classes(env.root, 'inspector').length, 1, 'the card of the reader\'s pick stays');
    await poll(env);
    assert.equal(classes(env.root, 'inspector').length, 1, 'and stays through the poll');
  } else if (mode === 'cursor_cycle_budget_has_no_top_account_pick_yet') {
    // AN EXCEPTION, NOT A CANON: the budget of the billing cycle (Cursor) picks no top account at the opening; the families with a window do. Whether Cursor should
    // is a product decision still open; until it is made, this holds the divergence in the open. If it is yes, this case is turned round (pickDecide for cyclePlate, selectInPlace for the plate of
    // the cycle).
    cleanup();
    for (const key of ['cursor_budget', 'cursor_mixed']) {
      const cur = await boot(fx.views[key]);
      assert.equal(classes(cur.root, 'wt-plate-cycle').length, 1, key + ': the plate of the cycle');
      assert.ok(classes(cur.root, 'bar').length > 0, key + ': it has its bars');
      assert.deepEqual(sels(cur), [], key + ': no bar is picked');
      assert.deepEqual(pressed(cur), [], key + ': none is pressed');
      assert.equal(classes(cur.root, 'inspector').length, 0, key + ': no card');
      assert.equal(classes(cur.root, 'bar-arrow').length, 0, key + ': no stem');
      await poll(cur);
      assert.deepEqual(sels(cur), [], key + ': none after the poll');
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
              "the_poll_starts_no_stand_ins_and_no_line",
              "every_limit_hidden_the_pick_of_the_widget_goes_with_the_plate", "every_limit_hidden_the_pick_of_the_reader_stays",
              "cursor_cycle_budget_has_no_top_account_pick_yet"]


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


# ---------------------------------------------------------------------------
# Real browsers.

STUB = with_i18n(r"""
(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true } : view) });
  };
  // The poll of the widget (its 30 s interval) is kept to be run by hand.
  window.__poll = [];
  const si = window.setInterval.bind(window);
  window.setInterval = (fn, ms, ...rest) => { if (ms >= 20000) window.__poll.push(fn); return si(fn, ms, ...rest); };
  // Every frame from here: what stands of the line, the stand-ins and the plate (the sampler of the motion).
  window.__frames = [];
  const sample = () => {
    const block = document.querySelector('.tl-block');
    const skel = document.querySelector('.tl-skel');
    const kid = block ? [...block.children].find((c) => !c.classList.contains('tl-skel')) : null;
    window.__frames.push({
      t: performance.now(), trace: document.querySelectorAll('.plate-trace').length, skel: document.querySelectorAll('.tl-skel').length,
      skelAria: skel ? skel.getAttribute('aria-hidden') : null, skelOp: skel ? parseFloat(getComputedStyle(skel).opacity) : null,
      off: skel ? skel.classList.contains('sk-off') : false, wait: !!block && block.classList.contains('tl-wait'),
      vis: kid ? getComputedStyle(kid).visibility : null, h: block ? block.getBoundingClientRect().height : null,
      traced: !!block && block.classList.contains('tl-traced'), arrows: document.querySelectorAll('.bar-arrow').length,
      wrap: (document.querySelector('.tl-wrap') || { getBoundingClientRect: () => ({ height: -1 }) }).getBoundingClientRect().height,
      inPlate: !!document.querySelector('.wt-plate .tl-block'),
    });
    requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
}
""")

STATE = """() => {
  const q = (s) => [...document.querySelectorAll(s)];
  const plate = document.querySelector('.wt-plate');
  return {
    sel: q('.bars > .sel').map((b) => b.getAttribute('data-focus')),
    pressed: q('.bars > .bar').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.getAttribute('data-focus')),
    barOn: q('.bars > .bar-on').length, arrows: q('.bar-arrow').length, arrowsOn: q('.bar-arrow.on').length,
    cards: q('.inspector').length, blocks: q('.tl-block').length, inPlate: q('.wt-plate .tl-block').length,
    traced: q('.tl-block.tl-traced').length, live: q('.plate-trace').length, skel: q('.tl-skel').length, wait: q('.tl-wait').length,
    reveal: q('.tl-reveal').length, run: q('.fold-run').length, shut: q('.fold-shut').length,
    kids: plate ? [...plate.children].map((c) => (c.getAttribute('class') || '').split(' ').slice(0, 2).join(' ')) : null,
    active: document.activeElement === document.body ? 'body' : document.activeElement.getAttribute('data-focus'),
  };
}"""

SETTLED = """() => !document.querySelector('.plate-trace, .tl-skel, .tl-wait, .tl-reveal, .fold-run, .fold-shut, .bar-arrow:not(.on)')
  && (!document.querySelector('.bars > .sel') || !!document.querySelector('.tl-block.tl-traced'))"""

ACCENT = """() => { const p = document.createElement('i'); p.style.color = 'var(--accent)'; document.body.appendChild(p);
  const c = getComputedStyle(p).color; p.remove(); return c; }"""


def _open(browser, view, width=1120, height=1000, settle=True, reduced=False, scale=1):
    page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce" if reduced else "no-preference", device_scale_factor=scale)
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(STUB, view)
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    page.wait_for_selector(".tl-block .chart-svg")
    if settle:
        _settle(page)
    return page


def _settle(page):
    page.wait_for_function(SETTLED, timeout=5000)


def _press(page, account, key=GROUP):
    page.click('[data-focus="%s"]' % bar(account, key))
    page.mouse.move(2, 2)


# --- the choice, in the rectangles -------------------------------------------------

def test_at_the_opening_the_top_current_bar_is_picked_the_stale_one_above_it_is_not(browser, base_view):
    cases = [(base_view, C2), (_variant(base_view, order=[C3, C1, C2], free=[C1], stale=[C3]), C1),
             (_variant(base_view, order=[C3, C1, C2], stale=[C3, C1, C2]), C3)]
    for view, expected in cases:
        page = _open(browser, view)
        state = page.evaluate(STATE)
        assert state["sel"] == [bar(expected)] and state["pressed"] == [bar(expected)], (expected, state)
        assert state["cards"] == 1 and state["blocks"] == 1 and state["inPlate"] == 1, state
        # the keyboard is not taken: the pick is the widget's own
        assert state["active"] == "body", state
        page.close()


def test_with_no_account_in_the_row_nothing_is_picked_and_the_view_is_the_view_at_rest(browser, base_view):
    page = _open(browser, _variant(base_view, no_account=True), settle=False)
    page.wait_for_timeout(700)
    state = page.evaluate(STATE)
    assert state["sel"] == [] and state["pressed"] == [] and state["arrows"] == 0 and state["cards"] == 0, state
    assert state["blocks"] == 1 and state["inPlate"] == 0 and state["traced"] == 0 and state["live"] == 0 and state["skel"] == 0, state
    assert state["kids"] and "tl-top" not in " ".join(state["kids"]), state
    page.close()


def test_the_picked_bar_is_open_and_red_and_stays_so_when_the_pointer_is_away(browser, base_view):
    page = _open(browser, base_view)
    accent = page.evaluate(ACCENT)
    info = page.evaluate("""() => [...document.querySelectorAll('.bars > .bar')].map((b) => ({
      key: b.getAttribute('data-focus'), sel: b.classList.contains('sel'), w: b.getBoundingClientRect().width,
      name: getComputedStyle(b.querySelector('.bar-n')).color }))""")
    picked = [b for b in info if b["sel"]]
    others = [b for b in info if not b["sel"]]
    assert len(picked) == 1 and picked[0]["key"] == bar(C2), info
    assert picked[0]["name"] == accent and all(o["name"] != accent for o in others), (accent, info)
    assert all(picked[0]["w"] > o["w"] + 20 for o in others), "the picked bar is open: " + json.dumps(info)
    # another is picked and the pointer leaves: the new one is open and red, the old one has shut
    _press(page, C1)
    _settle(page)
    page.wait_for_timeout(450)
    info = page.evaluate("""() => [...document.querySelectorAll('.bars > .bar')].map((b) => ({
      key: b.getAttribute('data-focus'), sel: b.classList.contains('sel'), w: b.getBoundingClientRect().width,
      name: getComputedStyle(b.querySelector('.bar-n')).color }))""")
    picked = [b for b in info if b["sel"]]
    others = [b for b in info if not b["sel"]]
    assert [b["key"] for b in picked] == [bar(C1)] and picked[0]["name"] == accent, info
    assert all(picked[0]["w"] > o["w"] + 20 and o["name"] != accent for o in others), info
    page.close()


def test_the_chart_stands_right_under_the_bars_in_the_plate_and_the_figure_below_it(browser, base_view):
    for width in (1120, 360):
        page = _open(browser, base_view, width=width)
        where = page.evaluate("""() => {
          const plate = document.querySelector('.wt-plate'), strip = plate.querySelector('.bars');
          const top = strip.nextElementSibling, block = top && top.querySelector('.tl-block'), fact = plate.querySelector('.wt-fact');
          const r = (n) => n.getBoundingClientRect(), p = r(plate);
          return { stripNext: top && top.className, blockParent: block && block.parentNode.parentNode.className, count: document.querySelectorAll('.tl-block').length,
                   gap: block && r(block).top - r(strip).bottom, factBelow: block && fact && r(fact).top - r(block).bottom,
                   blockLeft: block && r(block).left - p.left, blockRight: block && p.right - r(block).right,
                   order: [...plate.children].map((c) => (c.getAttribute('class') || '').split(' ')[0]),
                   overflow: document.documentElement.scrollWidth > window.innerWidth + 1 };
        }""")
        assert where["count"] == 1 and "tl-wrap" in where["stripNext"] and "tl-top" in where["stripNext"], where
        # (the figure stands in the summary of the account card under the plate, cardTake; its place below the chart is measured next)
        assert where["order"][:2] == ["bars", "tl-wrap"] and where["order"].index("ins-wrap") > 1 and "wt-fact" not in where["order"], where
        assert 0 <= where["gap"] <= 24, where
        assert where["factBelow"] >= -1, "the figure stands below the chart: %s" % where
        assert where["blockLeft"] <= 20 and where["blockRight"] <= 20 and not where["overflow"], where
        page.close()


def test_a_press_does_not_draw_the_bars_anew(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate("() => { window.__strip = document.querySelector('.bars'); window.__kids = [...window.__strip.children]; }")
    _press(page, C1)
    page.wait_for_timeout(150)
    same = page.evaluate("() => document.querySelector('.bars') === window.__strip && [...document.querySelector('.bars').children].every((k, i) => k === window.__kids[i])")
    assert same, "the same .bars and the same bars right after the press"
    _settle(page)
    assert page.evaluate("() => document.querySelector('.bars') === window.__strip && [...window.__strip.children].every((k, i) => k === window.__kids[i])")
    page.close()


# --- the switch ------------------------------------------------------------------------

def test_a_switch_folds_the_chart_and_opens_it_again_and_the_details_live(browser, base_view):
    page = _open(browser, base_view)
    page.click('[data-focus="chart-table"]')
    page.wait_for_selector(".checkpoints .cp-tile")
    page.mouse.move(2, 2)
    page.evaluate("""() => { window.__block = document.querySelector('.tl-block'); window.__tops = [];
      const t0 = performance.now(), wrap = document.querySelector('.tl-wrap');
      const f = () => { window.__tops.push(wrap.getBoundingClientRect().height); if (performance.now() - t0 < 1800) requestAnimationFrame(f); };
      requestAnimationFrame(f); }""")
    start = page.evaluate("() => document.querySelector('.tl-wrap').getBoundingClientRect().height")
    page.click('[data-focus="%s"]' % bar(C1))
    page.wait_for_timeout(1900)
    out = page.evaluate("() => ({ tops: window.__tops, same: document.querySelector('.tl-block') === window.__block, tiles: document.querySelectorAll('.checkpoints .cp-tile').length, open: document.querySelector('details.chart-table').open })")
    tops = out["tops"]
    assert min(tops) < start * 0.4, "the chart folds on the switch (smallest %.1f of %.1f)" % (min(tops), start)
    assert abs(tops[-1] - start) < 3, "and opens again to its height (%s -> %s)" % (start, tops[-1])
    assert out["same"] and out["tiles"] > 0 and out["open"], "one node, and the Details live: %s" % {k: v for k, v in out.items() if k != "tops"}
    state = page.evaluate(STATE)
    assert state["sel"] == [bar(C1)] and state["blocks"] == 1 and state["inPlate"] == 1 and state["arrows"] == 1, state
    page.close()


def test_a_pick_let_go_brings_the_chart_back_under_the_plate_by_the_same_node(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate("() => { window.__block = document.querySelector('.tl-block'); }")
    _press(page, C2)
    page.wait_for_function("() => !document.querySelector('.fold-run, .fold-shut, .bar-arrow') && !document.querySelector('.bars > .sel')", timeout=4000)
    state = page.evaluate(STATE)
    assert state["sel"] == [] and state["inPlate"] == 0 and state["blocks"] == 1 and state["arrows"] == 0 and state["cards"] == 0 and state["traced"] == 0, state
    assert page.evaluate("() => document.querySelector('.tl-block') === window.__block"), "the one node of the chart"
    page.close()


# --- the ladder -------------------------------------------------------------------------

def test_a_step_of_the_ladder_with_one_account_is_the_pick_of_its_bar(browser, base_view):
    page = _open(browser, base_view)
    step = page.locator('.rail-step[data-accounts="[\\"%s\\"]"]' % C1)
    assert step.count() == 1
    step.click()
    page.mouse.move(2, 2)
    _settle(page)
    state = page.evaluate(STATE)
    assert state["sel"] == [bar(C1)] and state["arrows"] == 1 and state["cards"] == 1 and state["inPlate"] == 1, state
    assert step.get_attribute("aria-pressed") == "true"
    step.click()
    page.mouse.move(2, 2)
    page.wait_for_function("() => !document.querySelector('.bars > .sel') && !document.querySelector('.bar-arrow')", timeout=4000)
    assert step.get_attribute("aria-pressed") == "false"
    page.close()


def test_a_step_of_several_accounts_lights_their_bars_opens_none_and_picks_none(browser, base_view):
    view = _same_reset(_variant(base_view, free=[C1]), C1, C2)
    page = _open(browser, view)
    accent = page.evaluate(ACCENT)
    # the top bar is picked at the opening: let it go, then press the step
    _press(page, C1)
    page.wait_for_function("() => !document.querySelector('.bars > .sel') && !document.querySelector('.bar-arrow') && !document.querySelector('.fold-run, .fold-shut')", timeout=4000)
    rest = page.evaluate("() => [...document.querySelectorAll('.bars > .bar')].map((b) => b.getBoundingClientRect().width)")
    step = page.locator('.rail-step[data-accounts*="%s"][data-accounts*="%s"]' % (C1, C2))
    assert step.count() == 1, "a step of two accounts"
    step.click()
    page.mouse.move(2, 2)
    page.wait_for_timeout(500)
    info = page.evaluate("""() => [...document.querySelectorAll('.bars > .bar')].map((b) => ({ key: b.getAttribute('data-focus'), on: b.classList.contains('bar-on'), lit: b.hasAttribute('data-step-lit'),
      w: b.getBoundingClientRect().width, name: getComputedStyle(b.querySelector('.bar-n')).color, opacity: parseFloat(getComputedStyle(b).opacity) }))""")
    lit = [b for b in info if b["lit"]]
    off = [b for b in info if not b["lit"]]
    assert sorted(b["key"] for b in lit) == sorted([bar(C1), bar(C2)], key=str), info
    # lit, not opened and not in the accent: the bars stand as wide as they did, the others step back
    assert [b["w"] for b in info] == rest and not any(b["on"] for b in info), (info, rest)
    assert all(b["name"] != accent for b in info), info
    assert all(b["opacity"] == 1 for b in lit) and all(b["opacity"] < 1 for b in off), info
    state = page.evaluate(STATE)
    assert state["sel"] == [] and state["arrows"] == 0 and state["cards"] == 0 and state["inPlate"] == 0 and state["blocks"] == 1 and state["barOn"] == 0, state
    page.close()


# --- the stem and the line --------------------------------------------------------------

def test_the_stem_is_one_with_a_pick_none_without_one_and_it_has_no_tip(browser, base_view):
    page = _open(browser, base_view)
    state = page.evaluate(STATE)
    assert state["arrows"] == 1 and state["arrowsOn"] == 1, state
    info = page.evaluate("""() => { const a = document.querySelector('.bar-arrow');
      const css = [...document.querySelectorAll('style')].map((s) => s.textContent).join('\\n') + [...document.styleSheets].map((s) => { try { return [...s.cssRules].map((r) => r.cssText).join(''); } catch (e) { return ''; } }).join('');
      return { kids: [...a.children].map((c) => c.tagName.toLowerCase()), tip: document.querySelectorAll('.trace-tip').length,
               css: /trace-tip|trace-rise/.test(css), w: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--trace-w')),
               width: a.getBoundingClientRect().width, doc: /trace-tip|trace-rise/.test(document.documentElement.outerHTML) }; }""")
    assert info["kids"] == ["rect"] and info["tip"] == 0 and not info["css"] and not info["doc"], info
    assert abs(info["width"] - info["w"]) < 0.01, info
    _press(page, C2)
    page.wait_for_function("() => !document.querySelector('.bar-arrow') && !document.querySelector('.bars > .sel')", timeout=4000)
    assert page.evaluate(STATE)["arrows"] == 0
    page.close()


@pytest.mark.parametrize("scale", [1, 2])
def test_the_stem_runs_from_inside_the_foot_of_the_bar_to_inside_the_top_line_of_the_plate_on_the_pixel_grid(browser, base_view, scale):
    page = _open(browser, base_view, scale=scale)
    g = page.evaluate("""() => { const a = document.querySelector('.bar-arrow'), s = document.querySelector('.bars > .sel'), b = document.querySelector('.tl-block'), strip = s.parentNode;
      const ar = a.getBoundingClientRect(), sr = s.getBoundingClientRect(), br = b.getBoundingClientRect();
      const w = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--trace-w'));
      const rest = parseFloat(strip.style.getPropertyValue('--bar-rest')) || sr.width;
      return { aTop: ar.top, aBottom: ar.bottom, aLeft: ar.left, aWidth: ar.width, sBottom: sr.bottom, sLeft: sr.left, bTop: br.top, rest, w, dpr: devicePixelRatio }; }""")
    d, w = g["dpr"], g["w"]
    assert d == scale
    assert abs(g["aTop"] - (g["sBottom"] - w)) <= 1 / d + 0.01, "starts inside the foot of the bar, no gap: %s" % g
    assert abs(g["aBottom"] - (g["bTop"] + w)) <= 1 / d + 0.01, "ends inside the top line of the plate, no gap and no spur: %s" % g
    assert abs(g["aLeft"] * d - round(g["aLeft"] * d)) < 0.02 and abs(g["aWidth"] * d - round(g["aWidth"] * d)) < 0.02, "on whole device pixels: %s" % g
    assert abs((g["aLeft"] + g["aWidth"] / 2) - (g["sLeft"] + g["rest"] / 2)) <= 1 / d + 0.51, "in the middle of the liquid: %s" % g
    page.close()


ARMS_WATCH = """() => { window.__live = null;
  new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes) {
    if (n.nodeType === 1 && n.matches && n.matches('.plate-trace') && !window.__live) {
      const arms = [...n.querySelectorAll('.trace-arm')], stem = n.querySelector('.trace-stem');
      const pr = document.querySelector('.wt-plate').getBoundingClientRect();
      const end = (p) => { const d = p.getAttribute('d'); const m = d.match(/H([-\\d.]+)$/); return m ? parseFloat(m[1]) : null; };
      window.__live = { arms: arms.length, len: arms.map((p) => p.getTotalLength()), dur: arms.map((p) => getComputedStyle(p).animationDuration), delay: arms.map((p) => getComputedStyle(p).animationDelay),
        ends: arms.map(end), stemDur: stem && getComputedStyle(stem).animationDuration, stemX: stem && parseFloat(stem.getAttribute('d').match(/^M([-\\d.]+)/)[1]),
        width: pr.width, left: n.getBoundingClientRect().left - pr.left, w: parseFloat(getComputedStyle(n.querySelector('path')).strokeWidth) };
    } } }).observe(document, { subtree: true, childList: true }); }"""


def test_the_two_halves_of_the_line_are_of_one_length_and_one_time_and_meet_opposite_the_stem(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate(ARMS_WATCH)
    _press(page, C1)  # the stem stands at the left of the plate: the meeting is not the middle of the foot
    page.wait_for_function("() => window.__live !== null", timeout=4000)
    live = page.evaluate("() => window.__live")
    assert live["arms"] == 2, live
    assert abs(live["len"][0] - live["len"][1]) <= 1.01, "the halves are of one length: %s" % live
    assert live["dur"][0] == live["dur"][1] and live["delay"][0] == live["delay"][1], "of one time, so one speed: %s" % live
    assert live["ends"][0] == live["ends"][1] and live["ends"][0] is not None, "and meet at one point: %s" % live
    middle = live["width"] / 2
    assert (live["ends"][0] - middle) * (live["stemX"] - middle) < 0 and abs(live["ends"][0] - middle) > 3, "the meeting is across the middle from the stem, not the middle of the foot: %s" % live
    assert abs(live["w"] - 1.5) < 1.01
    _settle(page)
    page.close()


# --- the stand-ins ----------------------------------------------------------------------

def test_the_stand_ins_stand_before_the_line_has_met_the_content_is_hidden_and_none_is_left_after(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate("() => { window.__frames.length = 0; }")
    _press(page, C1)
    page.wait_for_function("() => window.__frames.some((f) => f.trace) && !document.querySelector('.plate-trace')", timeout=5000)
    _settle(page)
    page.wait_for_timeout(120)
    frames = page.evaluate("() => window.__frames")
    live = [f for f in frames if f["trace"]]
    assert live, "the line was drawn"
    assert all(f["skel"] == 1 and f["skelAria"] == "true" and f["wait"] and f["vis"] == "hidden" for f in live), "stand-ins up, the real content hidden, while the line is drawn: %s" % live[:2]
    assert all(f["skelOp"] is not None and f["skelOp"] > 0.9 and not f["off"] for f in live), live[:2]
    first_skel = next(i for i, f in enumerate(frames) if f["skel"])
    first_live = next(i for i, f in enumerate(frames) if f["trace"])
    assert first_skel <= first_live, "the stand-ins come before the line"
    # the real content never shows with the stand-ins up and not yet fading
    assert not [f for f in frames if f["skel"] and not f["off"] and f["vis"] == "visible" and f["wait"]], "no half state"
    heights = [f["h"] for f in frames[first_live:] if f["h"] is not None]
    assert max(heights) - min(heights) <= 1.5, "the plate keeps its height under the stand-ins and after them: %s" % (max(heights) - min(heights))
    last = frames[-1]
    assert last["skel"] == 0 and not last["wait"] and last["vis"] == "visible" and last["traced"] and last["trace"] == 0, last
    ended = next(i for i, f in enumerate(frames) if i > first_live and f["trace"] == 0)
    gone = next(i for i, f in enumerate(frames) if i >= ended and f["skel"] == 0)
    assert not [f for f in frames[gone:] if f["skel"] or f["wait"]], "once gone, they do not come back"
    state = page.evaluate(STATE)
    assert state["skel"] == 0 and state["wait"] == 0 and state["reveal"] == 0 and state["live"] == 0, state
    page.close()


def test_the_stand_ins_are_made_of_the_layout_of_the_plate_and_hidden_from_a_reader(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate("() => { window.__shape = null; new MutationObserver(() => { const s = document.querySelector('.tl-skel'); if (s && !window.__shape) window.__shape = { aria: s.getAttribute('aria-hidden'), caps: s.querySelectorAll('.sk-cap').length, field: s.querySelectorAll('.sk-field').length, legend: s.querySelectorAll('.sk-legend').length, detail: s.querySelectorAll('.sk-detail').length, focusable: s.querySelectorAll('a,button,[tabindex],summary').length, pe: getComputedStyle(s).pointerEvents }; }).observe(document, { subtree: true, childList: true }); }")
    _press(page, C1)
    page.wait_for_function("() => window.__shape !== null", timeout=4000)
    shape = page.evaluate("() => window.__shape")
    assert shape == {"aria": "true", "caps": 4, "field": 1, "legend": 1, "detail": 1, "focusable": 0, "pe": "none"}, shape
    _settle(page)
    page.close()


def test_the_opening_draws_the_line_once_with_the_stand_ins_and_the_poll_draws_neither(browser, base_view):
    page = _open(browser, base_view, settle=False)
    _settle(page)
    opening = page.evaluate("() => window.__frames")
    assert any(f["trace"] and f["skel"] and f["vis"] == "hidden" for f in opening), "the line and the stand-ins at the opening"
    page.evaluate("() => { window.__frames.length = 0; window.__sawNew = []; new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes) if (n.nodeType === 1 && /plate-trace|tl-skel/.test(n.getAttribute('class') || '')) window.__sawNew.push(n.getAttribute('class')); }).observe(document, { subtree: true, childList: true }); }")
    before = page.evaluate(STATE)
    assert page.evaluate("() => window.__poll.length") >= 1, "the widget's poll was found"
    for _ in range(2):
        page.evaluate("() => window.__poll[0]()")
        page.wait_for_timeout(900)
    after = page.evaluate(STATE)
    assert page.evaluate("() => window.__sawNew") == [], "no line and no stand-ins at the poll"
    assert not [f for f in page.evaluate("() => window.__frames") if f["trace"] or f["skel"] or f["wait"]]
    for key in ("sel", "pressed", "arrows", "arrowsOn", "cards", "inPlate", "traced"):
        assert after[key] == before[key], (key, before, after)
    page.close()


def test_a_pick_made_by_the_widget_holds_through_the_poll_and_a_pick_let_go_does_not_come_back(browser, base_view):
    page = _open(browser, base_view)
    for _ in range(2):
        page.evaluate("() => window.__poll[0]()")
        page.wait_for_timeout(500)
    assert page.evaluate(STATE)["sel"] == [bar(C2)]
    _press(page, C2)
    page.wait_for_function("() => !document.querySelector('.bars > .sel') && !document.querySelector('.fold-run, .fold-shut')", timeout=4000)
    for _ in range(2):
        page.evaluate("() => window.__poll[0]()")
        page.wait_for_timeout(500)
    state = page.evaluate(STATE)
    assert state["sel"] == [] and state["inPlate"] == 0 and state["arrows"] == 0 and state["cards"] == 0, state
    page.close()


HIDE_ROWS = """async (once) => {
  const boxes = () => [...document.querySelectorAll('.rows-menu input[type=checkbox]')];
  while (boxes().some((b) => b.checked)) {
    const box = boxes().find((b) => b.checked);
    box.checked = false;
    box.dispatchEvent(new Event('change'));
    await new Promise((r) => setTimeout(r, 120));
    if (once) return;
  }
}"""

AT_REST = """() => ({
  plates: document.querySelectorAll('.wt-plate').length, sel: document.querySelectorAll('.bars > .sel').length,
  cards: document.querySelectorAll('.inspector').length, blocks: document.querySelectorAll('.tl-block').length,
  rows: [...document.querySelectorAll('[data-focus^="acct:"]')].filter((n) => n.getAttribute('aria-pressed') === 'true').map((n) => n.getAttribute('data-focus')),
})"""


def test_every_limit_hidden_the_pick_of_the_widget_goes_with_the_plate_and_no_card_hangs_under_accounts(browser, base_view):
    page = _open(browser, base_view)
    # Rule: with a plate drawn the Rows menu stands in the head of the account list, so the list is opened first (the word over the bars).
    page.click('[data-focus="accounts"]')
    page.wait_for_selector('.rows-menu input[type=checkbox]', state='attached', timeout=4000)
    # One limit hidden: another plate is shown and its top bar is picked (the card stands under that plate).
    page.evaluate(HIDE_ROWS, True)
    page.wait_for_function("() => !!document.querySelector('.bars > .sel') && !!document.querySelector('.wt-plate')", timeout=4000)
    # Every limit hidden: no plate, so no bar, so no card of a pick nobody made.
    page.evaluate(HIDE_ROWS, False)
    page.wait_for_function("() => !document.querySelector('.wt-plate')", timeout=4000)
    page.wait_for_timeout(700)
    state = page.evaluate(AT_REST)
    assert state["plates"] == 0 and state["sel"] == 0 and state["cards"] == 0 and state["rows"] == [], state
    page.click('[data-focus="accounts"]')
    page.wait_for_timeout(300)
    state = page.evaluate(AT_REST)
    assert state["plates"] == 0 and state["sel"] == 0 and state["cards"] == 0 and state["rows"] == [], state
    page.close()


# --- quick presses -----------------------------------------------------------------------

QUICK = """async ([keys, gap]) => {
  for (const k of keys) { document.querySelector('[data-focus="' + k + '"]').click(); await new Promise((r) => setTimeout(r, gap)); }
}"""


@pytest.mark.parametrize("gap", [0, 40, 130])
def test_quick_presses_the_last_one_wins_and_nothing_is_left_hanging(browser, base_view, gap):
    page = _open(browser, base_view)
    page.evaluate(QUICK, [[bar(C1), bar(C3), bar(C2), bar(C1)], gap])
    _settle(page)
    state = page.evaluate(STATE)
    assert state["sel"] == [bar(C1)] and state["pressed"] == [bar(C1)], state
    assert state["arrows"] == 1 and state["arrowsOn"] == 1 and state["cards"] == 1 and state["blocks"] == 1 and state["inPlate"] == 1 and state["traced"] == 1, state
    assert state["live"] == 0 and state["skel"] == 0 and state["wait"] == 0 and state["reveal"] == 0 and state["run"] == 0 and state["shut"] == 0, state
    page.wait_for_timeout(700)
    assert page.evaluate(STATE) == state, "and nothing comes after"
    page.close()


@pytest.mark.parametrize("gap", [0, 60, 200])
def test_quick_presses_on_one_bar_pick_and_let_go_and_the_last_wins(browser, base_view, gap):
    page = _open(browser, base_view)
    page.evaluate(QUICK, [[bar(C1), bar(C1)], gap])
    _settle(page)
    state = page.evaluate(STATE)
    assert state["sel"] == [bar(C1)] or state["sel"] == [], state
    # two presses: the bar is picked, then let go (the first pick is the widget's C2, so C1 picked then let go)
    assert state["sel"] == [], state
    assert state["arrows"] == 0 and state["cards"] == 0 and state["inPlate"] == 0 and state["blocks"] == 1, state
    assert state["live"] == 0 and state["skel"] == 0 and state["wait"] == 0 and state["reveal"] == 0 and state["run"] == 0 and state["traced"] == 0, state
    page.close()


# --- reduced motion --------------------------------------------------------------------

def test_reduced_motion_the_opening_and_a_pick_are_in_their_end_state_at_once(browser, base_view):
    page = _open(browser, base_view, settle=False, reduced=True)
    state = page.evaluate(STATE)
    assert state["sel"] == [bar(C2)] and state["traced"] == 1 and state["arrowsOn"] == 1 and state["inPlate"] == 1, state
    assert state["live"] == 0 and state["skel"] == 0 and state["wait"] == 0 and state["run"] == 0, state
    page.evaluate("() => { window.__frames.length = 0; document.querySelector('[data-focus=\"%s\"]').click(); }" % bar(C1))
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    state = page.evaluate(STATE)
    assert state["sel"] == [bar(C1)] and state["traced"] == 1 and state["arrowsOn"] == 1 and state["inPlate"] == 1 and state["cards"] == 1, state
    assert state["live"] == 0 and state["skel"] == 0 and state["wait"] == 0 and state["run"] == 0 and state["shut"] == 0 and state["reveal"] == 0, state
    page.click('[data-focus="%s"]' % bar(C1))
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    state = page.evaluate(STATE)
    assert state["sel"] == [] and state["inPlate"] == 0 and state["arrows"] == 0 and state["traced"] == 0 and state["run"] == 0 and state["shut"] == 0, state
    page.wait_for_timeout(700)
    frames = page.evaluate("() => window.__frames")
    assert not [f for f in frames if f["trace"] or f["skel"] or f["wait"] or f["vis"] == "hidden"], "no line and no stand-ins at any frame"
    page.close()


# --- the wait has an end: stand-ins, the fold's listener, the poll in the middle of a motion -------------------------------

HANG_CHART = """() => { const f = window.fetch; window.fetch = (url, o) => (/reuse=1/.test(url) ? new Promise(() => {}) : f(url, o)); }"""
FAIL_CHART = """() => { const f = window.fetch; window.fetch = (url, o) => (/reuse=1/.test(url) ? Promise.reject(new Error('no chart')) : f(url, o)); }"""


def _other_window(page):
    return page.query_selector('[data-focus^="win:"]:not([aria-selected="true"])')


@pytest.fixture(scope="module")
def two_windows_view(base_view):
    """The fixture view with a second window: its second limit (gpt-reserve) is a 5-hour one, so the family has two tabs (5H and 7D)."""
    view = json.loads(json.dumps(base_view))
    group = view["reserve"]["summary"]["groups"][1]
    assert "|604800|" in group["key"] and group["window_seconds"] == 604800, group["key"]
    group["window_seconds"] = 18000
    group["key"] = group["key"].replace("|604800|", "|18000|")
    return view


def test_the_stand_ins_do_not_hang_past_four_seconds_when_the_chart_does_not_come(browser, two_windows_view):
    page = _open(browser, two_windows_view)
    tab = _other_window(page)
    assert tab is not None, "the fixture has two windows"
    page.evaluate(HANG_CHART)
    tab.click()
    page.wait_for_function("() => document.querySelectorAll('.tl-skel').length === 1", timeout=2000)
    page.wait_for_timeout(2000)
    assert page.evaluate(STATE)["skel"] == 1, "they still stand at 2 s"
    page.wait_for_function("() => !document.querySelector('.tl-skel, .tl-wait, .tl-reveal')", timeout=3500)
    state = page.evaluate(STATE)
    assert state["skel"] == 0 and state["wait"] == 0 and state["live"] == 0 and state["traced"] == state["blocks"], state
    page.close()


def test_a_failed_read_of_the_chart_ends_the_stand_ins_at_once(browser, two_windows_view):
    page = _open(browser, two_windows_view)
    tab = _other_window(page)
    assert tab is not None, "the fixture has two windows"
    page.evaluate(FAIL_CHART)
    tab.click()
    page.wait_for_function("() => !document.querySelector('.tl-skel, .tl-wait, .tl-reveal, .plate-trace')", timeout=2500)
    page.close()


def test_the_fold_wait_leaves_no_listener_on_the_wrap_after_many_picks(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate("""() => {
      window.__most = 0;
      const add = EventTarget.prototype.addEventListener, del = EventTarget.prototype.removeEventListener;
      const wrap = (t) => t.classList && t.classList.contains('tl-wrap');
      EventTarget.prototype.addEventListener = function (n, f, o) { if (n === 'transitionend' && wrap(this)) { this.__n = (this.__n || 0) + 1; window.__most = Math.max(window.__most, this.__n); } return add.call(this, n, f, o); };
      EventTarget.prototype.removeEventListener = function (n, f, o) { if (n === 'transitionend' && wrap(this)) this.__n = Math.max(0, (this.__n || 0) - 1); return del.call(this, n, f, o); };
    }""")
    page.evaluate(QUICK, [[bar(C1), bar(C3)] * 6 + [bar(C1)], 130])
    _settle(page)
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__most") <= 2, "after 13 picks the wrap wore two listeners at most"
    left = page.evaluate("() => [...document.querySelectorAll('.tl-wrap')].map((w) => w.__n || 0)")
    assert all(n == 0 for n in left), left
    page.close()


def test_a_poll_in_the_middle_of_a_motion_is_drawn_after_it_within_a_second(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate("() => document.querySelector('.bars').setAttribute('data-probe', '1')")
    _press(page, C1)
    page.wait_for_function("() => !!document.querySelector('.fold-run')", timeout=2000)
    page.evaluate("() => window.__poll[0]()")
    page.wait_for_timeout(120)
    mid = page.evaluate("() => ({ probe: !!document.querySelector('.bars[data-probe]'), moving: !!document.querySelector('.fold-run, .plate-trace') })")
    assert mid["moving"] and mid["probe"], "while it moves the poll has not drawn the page anew: %s" % mid
    page.wait_for_function("() => !document.querySelector('.bars[data-probe]')", timeout=1100)
    _settle(page)
    assert page.evaluate(STATE)["sel"] == [bar(C1)], "the pick stands through the late drawing"
    page.close()


# --- the view does not jump ----------------------------------------------------------

def _dense(base, count):
    """The fixture view with `count` more bars in the first limit: the row is narrower than its bars rest at."""
    view = json.loads(json.dumps(base))
    group = view["reserve"]["summary"]["groups"][0]
    for k in range(count):
        group["bars"].append({**group["bars"][0], "account": "codex:d%d" % k})
    return view


OPEN_BAR = """() => new Promise((done) => {
  const out = [], t0 = performance.now();
  let polled = false;
  const f = () => {
    if (!polled) { polled = true; window.__poll[0](); }
    const open = document.querySelector('.bars > .sel');
    out.push(open ? open.getBoundingClientRect().width : -1);
    if (performance.now() - t0 < 900) requestAnimationFrame(f); else done(out);
  };
  requestAnimationFrame(f);
})"""


def test_a_drawing_anew_does_not_move_the_open_bar_in_a_row_that_rests_its_bars_narrower_than_the_most(browser, base_view):
    page = _open(browser, _dense(base_view, 12), width=520)
    rest = page.evaluate("() => parseFloat(document.querySelector('.bars').style.getPropertyValue('--bar-rest'))")
    most = page.evaluate("() => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--bar-max'))")
    assert 0 < rest < most - 8, "the row is dense: the bars rest at %s of at most %s" % (rest, most)
    for _ in range(2):
        before = page.evaluate("() => document.querySelector('.bars > .sel').getBoundingClientRect().width")
        sampled = page.evaluate(OPEN_BAR)
        assert min(sampled) > 0, "the bar stays open through the drawing"
        assert max(sampled) - min(sampled) < 0.5 and abs(sampled[0] - before) < 0.5, "no frame moves it: %s -> %s" % (before, sorted(set(round(w, 1) for w in sampled)))
        page.wait_for_timeout(200)
    assert not page.evaluate("() => document.querySelector('.bars-fitting')"), "and the row is let go"
    page.close()


def test_a_bar_still_opens_by_a_move_after_the_row_was_set(browser, base_view):
    page = _open(browser, _dense(base_view, 12), width=520)
    page.evaluate("() => window.__poll[0]()")
    page.wait_for_timeout(400)
    page.evaluate("() => { window.__w = []; const t0 = performance.now(); const f = () => { window.__w.push(document.querySelectorAll('.bars > .bar')[5].getBoundingClientRect().width); if (performance.now() - t0 < 700) requestAnimationFrame(f); }; requestAnimationFrame(f); document.querySelectorAll('.bars > .bar')[5].click(); }")
    page.wait_for_timeout(900)
    widths = page.evaluate("() => window.__w")
    assert max(widths) - widths[0] > 100 and len({round(w) for w in widths}) > 3, "the pick opens by a move, not at once: %s" % sorted({round(w) for w in widths})
    page.close()


HEIGHT_OF_THE_PAGE = """() => new Promise((done) => {
  const out = [], t0 = performance.now();
  const f = () => {
    out.push(Math.ceil(document.body.getBoundingClientRect().bottom));
    if (performance.now() - t0 < 1100) requestAnimationFrame(f); else done(out);
  };
  requestAnimationFrame(f);
})"""


@pytest.mark.parametrize("to", [C1, C3])
def test_a_pick_does_not_let_the_page_fall_to_the_plate_and_rise_again(browser, base_view, to):
    page = _open(browser, base_view)
    page.click('[data-focus="chart-table"]')
    page.wait_for_selector(".checkpoints .cp-tile")
    page.mouse.move(2, 2)
    for key in (to, to):
        start = page.evaluate("() => Math.ceil(document.body.getBoundingClientRect().bottom)")
        page.evaluate("(k) => setTimeout(() => document.querySelector('[data-focus=\"' + k + '\"]').click(), 0)", bar(key))
        heights = page.evaluate(HEIGHT_OF_THE_PAGE)
        _settle(page)
        assert min(heights) >= start * 0.85, "the page keeps its height through the switch (%s of %s)" % (min(heights), start)
        assert page.evaluate("() => document.querySelectorAll('.fold-held').length") == 0, "and the hold is let go"
        assert page.evaluate("() => document.querySelector('.lrows').style.getPropertyValue('--fold-hold')") == ""
        assert page.evaluate(STATE)["blocks"] == 1
    page.close()


def test_the_hold_of_the_page_is_let_go_after_quick_presses_and_a_poll_in_the_middle_of_the_motion(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate(QUICK, [[bar(C1), bar(C3), bar(C2), bar(C1), bar(C1)], 60])
    page.evaluate("() => window.__poll[0]()")
    _settle(page)
    page.wait_for_timeout(1200)
    state = page.evaluate("() => ({ held: document.querySelectorAll('.fold-held').length, least: [...document.querySelectorAll('.lrows')].map((n) => n.style.getPropertyValue('--fold-hold')) })")
    assert state["held"] == 0 and all(v == "" for v in state["least"]), state
    page.close()


COUNT_DRAWS = """() => {
  const root = document.getElementById('root'), set = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
  window.__draws = 0;
  Object.defineProperty(root, 'textContent', { configurable: true, get() { return set.get.call(this); }, set(v) { window.__draws += 1; set.set.call(this, v); } });
}"""


def test_a_pick_of_a_window_that_asks_for_its_chart_draws_the_page_twice_and_marks_the_read_on_the_screen_between(browser, two_windows_view):
    page = _open(browser, two_windows_view)
    page.evaluate(HANG_CHART)
    page.evaluate(COUNT_DRAWS)
    _other_window(page).click()
    page.wait_for_function("() => !!document.querySelector('.btn-word-dot.is-busy')", timeout=2000)
    page.wait_for_timeout(300)
    mark = page.evaluate("""() => { const r = document.querySelector('[data-focus="refresh"]');
      return { draws: window.__draws, disabled: r.disabled, busy: r.getAttribute('aria-busy'), dots: document.querySelectorAll('.btn-word-dot.is-busy').length }; }""")
    assert mark == {"draws": 1, "disabled": True, "busy": "true", "dots": 1}, "the click is drawn at once and the read in the air is marked on that screen: %s" % mark
    page.close()
    page = _open(browser, two_windows_view)
    page.evaluate(COUNT_DRAWS)
    _other_window(page).click()
    page.wait_for_timeout(1200)
    assert page.evaluate("() => window.__draws") == 2, "the click and the chart: two drawings"
    after = page.evaluate("() => { const r = document.querySelector('[data-focus=\"refresh\"]'); return { disabled: r.disabled, busy: r.getAttribute('aria-busy'), dots: document.querySelectorAll('.btn-word-dot.is-busy').length }; }")
    assert after == {"disabled": False, "busy": None, "dots": 0}, "and the chart's drawing takes the mark off: %s" % after
    page.close()
