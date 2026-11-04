"""Where the selected account's card stands.

The card of an account a bar selected stands right under the limit that bar is in — before that limit's own
timeline, whichever limit is charted; the card of an account a row of the account list selected stands right
under that row. If the place it came from is no longer drawn (a hidden limit, a folded list) it stands where
the account lives: under its row of the list, or under the folded list's header. Exactly one card, never at the
far end of the screen — which is where the first 0.9.0 candidate left it, 1086 px from the strip that opened it.

Two real consumers of widget.js: the fake DOM of the other widget tests (the order of the nodes, the keyboard's
place across the 30-second read, a failed read and the next) and, where Playwright has a browser, Chromium and
WebKit (the rectangles the reader sees). The opaque production host is exercised by _qa-inspector/, not here.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

import test_quotas
from conftest import launch_engine
from test_reserve import _node, _widget_fixture, with_i18n

WIDGET = Path(os.environ.get("INSPECTOR_PLACEMENT_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()

NODE_PLACEMENT = r"""
const cls = (n) => String(n.className || '').split(/\s+/);
const has = (n, name) => cls(n).includes(name);
const click = (env, key) => {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
};
const active = (env) => env.document.activeElement;
// A press on a bar of the window on screen. The top bar is picked there already (the widget's own pick: open, red, its card under the
// plate); a press on a picked bar lets it go, so a second press is the reader's own pick of it.
const pick = (env, key) => {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  if (node.getAttribute('aria-pressed') === 'true') click(env, key);
  click(env, key);
};
// The window row shows one limit: pick its window, then its scope (the plate of the stack's old rows).
function showLimit(env, g) {
  const tab = byFocus(env.root, 'win:' + g.harness + ':' + g.window_seconds);
  assert.ok(tab, 'a tab for the window of ' + g.key);
  if (tab.getAttribute('aria-selected') !== 'true') tab.listeners.click[0]({ stopPropagation() {} });
  const scope = byFocus(env.root, 'scope:' + g.key);
  if (scope && scope.getAttribute('aria-pressed') !== 'true') scope.listeners.click[0]({ stopPropagation() {} });
  assert.ok(byFocus(env.root, 'limit:' + g.key), 'the plate of ' + g.key + ' is shown');
}
// The stub keeps focus on a node a redraw removed; the keyboard is where it should be only on a live node.
const keyboardOn = (env, key) => walk(env.root).includes(active(env)) && active(env).getAttribute('data-focus') === key;
const cards = (env) => classes(env.root, 'inspector');
// The card stands in a fold of one row (.ins-wrap > .ins-fold). Under a chart it is the plate's own child (the plate is the row of the limits, .lrow); where no chart is over it
// (Chart folded) it is a row of the limits itself. Either way it is among the limits.
const inRows = (card) => { const holder = card.parentNode.parentNode.parentNode; return has(holder, 'lrows') || (has(holder, 'lrow') && has(holder.parentNode, 'lrows')); };
const limitKey = (row) => classes(row, 'l-chart')[0].getAttribute('data-focus').slice(6);
// The children of a list, one word each: a limit row, the card (alone, or in its fold), a chart (alone, or in its fold: it stands
// there only where nothing is picked; a picked bar lifts it into the plate), an account row.
const seq = (parent) => parent.childNodes.map((n) => has(n, 'inspector') || has(n, 'ins-wrap') ? 'card'
  : has(n, 'tl-block') || has(n, 'tl-wrap') ? 'chart'
  : has(n, 'lrow') ? 'row:' + limitKey(n) : has(n, 'acc-row') ? n.getAttribute('data-focus') : cls(n)[0]);
// The plate of a limit (its row) and whether its chart stands in it: with a bar picked the chart is the plate's own child.
const plateOf = (env, key) => classes(env.root, 'lrow').find((r) => limitKey(r) === key);
const chartIn = (env, key) => { const r = plateOf(env, key); return !!r && classes(r, 'tl-block').length === 1; };
// Where the card of a limit stands: 'plate' (the plate's own child, right after the chart: the plate's area `card`, a second card that goes under the chart's plate), 'below' (the next row
// of the limits: where no chart is over it, Chart folded, it stands whole in its own place), 'plate-no-chart' (in the plate but not after a chart: wrong) or '' (nowhere).
const placeOf = (env, key) => {
  const r = plateOf(env, key);
  if (!r) return '';
  const kids = r.childNodes;
  const w = kids.findIndex((n) => has(n, 'ins-wrap'));
  if (w >= 0) return w > 0 && has(kids[w - 1], 'tl-wrap') ? 'plate' : 'plate-no-chart';
  const sibs = r.parentNode.childNodes;
  const next = sibs[sibs.indexOf(r) + 1];
  return next && has(next, 'ins-wrap') ? 'below' : '';
};
const rowsSeq = (env) => seq(classes(env.root, 'lrows')[0]);
const tableSeq = (env) => seq(classes(env.root, 'acc-table')[0]);
const after = (list, item) => list[list.indexOf(item) + 1];
const barKey = (g, i = 0) => 'bar:' + g.key + ':' + g.bars[i].account;

(async () => {
  const fx = JSON.parse(process.env.PLACEMENT_FIXTURE);
  const mode = process.env.PLACEMENT_CASE;
  const groups = fx.view.reserve.summary.groups.filter((g) => (g.bars || []).some((b) => b.account));
  assert.ok(groups.length >= 2, 'the fixture has two limits with named bars');
  const env = await boot(fx.view);
  const charted = () => classes(env.root, 'lrow').filter((r) => has(r, 'sel')).map(limitKey);
  const first = groups[0], second = groups[1];
  // The screen's own children: the root holds the head and the one card (capsule: shell), and the screen stands in the card.
  const rootKinds = () => classes(env.root, 'shell-card')[0].childNodes.map((n) => cls(n)[0]);
  const oneCard = () => {
    assert.equal(cards(env).length, 1, 'exactly one card');
    assert.ok(!rootKinds().includes('inspector'), 'never at the far end of the screen');
  };

  if (mode === 'bar_card_under_its_limit') {
    // Every limit with a named bar: its bar's card stands in the plate of that limit, under the chart (the chart stands right under the bars, a bar is picked).
    for (const g of groups) {
      showLimit(env, g);
      pick(env, barKey(g));
      oneCard();
      const rows = rowsSeq(env);
      // One plate: the shown limit's row, the chart in it and the card in it, right under the chart (the card is no more a row of its own).
      assert.deepEqual(rows, ['row:' + g.key], 'one plate: ' + rows.join(' '));
      assert.equal(placeOf(env, g.key), 'plate', 'the card is under the chart of ' + g.key);
      assert.ok(chartIn(env, g.key), 'the chart of ' + g.key + ' stands in its plate');
      assert.deepEqual(charted(), [g.key]);
      assert.ok(keyboardOn(env, barKey(g)), 'the keyboard stays on the bar');
      click(env, 'inspector-clear');
      assert.equal(cards(env).length, 0);
    }
  } else if (mode === 'chart_moves_card_stays') {
    showLimit(env, first);
    pick(env, barKey(first));
    showLimit(env, second);
    assert.deepEqual(charted(), [second.key]);
    let rows = rowsSeq(env);
    // Another window is shown: a pick stands in every window (the reader's follows where its account has a bar, else the top bar is
    // picked), so the card is under the plate shown and the chart in it; the limit of the first window is not drawn.
    assert.deepEqual(rows, ['row:' + second.key], rows.join(' '));
    assert.equal(placeOf(env, second.key), 'plate', 'the card is under the chart of the plate shown');
    assert.ok(chartIn(env, second.key), 'the chart stands in the plate shown');
    oneCard();
    assert.ok(inRows(cards(env)[0]), 'the card is among the limits');
    // Back to the origin window: the card is under its plate, the chart in it.
    showLimit(env, first);
    rows = rowsSeq(env);
    assert.deepEqual(charted(), [first.key]);
    assert.deepEqual(rows, ['row:' + first.key]);
    assert.equal(placeOf(env, first.key), 'plate');
    assert.ok(chartIn(env, first.key));
    // Folded (the plate's word Chart): the chart leaves the plate, the card goes out of it and stands whole in its own place under the plate (no chart is left to go under).
    click(env, 'limit:' + first.key);
    rows = rowsSeq(env);
    assert.ok(!rows.includes('chart') && !chartIn(env, first.key));
    assert.equal(after(rows, 'row:' + first.key), 'card');
    assert.equal(placeOf(env, first.key), 'below');
    oneCard();
  } else if (mode === 'another_selection_moves_the_card') {
    // Two bars of two different accounts in two limits (a bar of the selected account clears it instead).
    let i1 = -1, i2 = -1;
    first.bars.forEach((a, i) => second.bars.forEach((b, j) => {
      if (i1 < 0 && a.account && b.account && a.account !== b.account) { i1 = i; i2 = j; }
    }));
    assert.ok(i1 >= 0, 'the fixture has two accounts in two limits');
    showLimit(env, first);
    pick(env, barKey(first, i1));
    assert.equal(placeOf(env, first.key), 'plate');
    showLimit(env, second);
    pick(env, barKey(second, i2));
    oneCard();
    const rows = rowsSeq(env);
    assert.equal(placeOf(env, second.key), 'plate');
    assert.ok(!rows.includes('row:' + first.key), 'one plate: the first limit is not drawn');
    // A bar of the same account again clears it (the existing toggle).
    click(env, barKey(second, i2));
    assert.equal(cards(env).length, 0);
  } else if (mode === 'list_row_card_under_its_row') {
    // Rule: the list opens in the place of the bars, the chart and the card, and a row of it picks its
    // account and shuts the list. The card is never a row of the table now: it stands in the plate under the chart, like the card of a pressed bar.
    click(env, 'accounts');
    oneCard();
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'true', 'the word over the bars is pressed while the list stands');
    assert.ok(!tableSeq(env).includes('card'), 'the card is not a row of the table: ' + tableSeq(env).join(' '));
    click(env, 'acct:codex:c2');
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'false', 'a row of the list shuts the list');
    oneCard();
    const shown = rowsSeq(env)[0].slice(4);
    const plateGroup = groups.find((g) => g.key === shown);
    assert.equal(placeOf(env, shown), 'plate', 'the card of the row stands in the plate, under the chart');
    const c2 = plateGroup.bars.findIndex((b) => b.account === 'codex:c2');
    assert.ok(c2 >= 0 && byFocus(env.root, barKey(plateGroup, c2)).getAttribute('aria-pressed') === 'true', 'the bar of the row account is picked');
    // Opened again the list does not take the card out of the plate; the word shuts it.
    click(env, 'accounts');
    oneCard();
    assert.equal(placeOf(env, shown), 'plate');
    assert.ok(!tableSeq(env).includes('card'), 'still not a row of the table');
    click(env, 'accounts');
    // A bar of another account takes the card to its own bar (the list is shut: no table with a card in it).
    showLimit(env, first);
    pick(env, barKey(first));
    oneCard();
    assert.ok(!classes(env.root, 'acc-table').length || !tableSeq(env).includes('card'));
    assert.equal(placeOf(env, first.key), 'plate');
  } else if (mode === 'hidden_origin_limit') {
    // The limits are hidden from the rows (display only): with a plate still shown the pick moves to it (another window, the card under
    // that plate); with no plate left to stand under, the card goes to the account's own row of the list — or under the list's header —
    // and returns under the limit when the limits do.
    const rowsMenu = () => classes(env.root, 'rows-menu')[0];
    // The Rows menu lists the family's limits in the order of the answer; its labels carry the names.
    const family = fx.view.reserve.summary.groups.filter((g) => g.harness === first.harness);
    const limitName = (g) => walk(rowsMenu()).filter((n) => n.tagName === 'INPUT')[family.indexOf(g)]
      .getAttribute('aria-label').slice(5);
    const hide = (g) => {
      const box = walk(rowsMenu()).find((n) => n.tagName === 'INPUT' && n.getAttribute('aria-label') === 'Show ' + limitName(g));
      assert.ok(box, 'the Rows menu offers the limit');
      box.checked = false;
      box.listeners.change[0]();
    };
    click(env, 'accounts');
    showLimit(env, first);
    pick(env, barKey(first));
    const account = first.bars[0].account;
    hide(first);
    assert.ok(!rowsSeq(env).includes('row:' + first.key), 'the limit is hidden');
    oneCard();
    if (family.length > 1) {
      // Another limit is shown: the card stands in its plate (or under it).
      assert.ok(inRows(cards(env)[0]), 'under the plate of the limit that is shown');
      family.filter((g) => g !== first).forEach(hide);
    }
    assert.equal(classes(env.root, 'lrow').length, 0, 'no limit is drawn');
    oneCard();
    assert.equal(after(tableSeq(env), 'acct:' + account), 'card', tableSeq(env).join(' '));
    // The word shuts the list: with no plate to carry it the card stays in the list while the fold plays (the list has no header of its own now:
    // it is the table's child inside the accounts section, no more the section's child right under the header).
    click(env, 'accounts');
    oneCard();
    assert.ok(has(cards(env)[0].parentNode.parentNode, 'accounts') || has(cards(env)[0].parentNode, 'accounts'), 'in the accounts section');
    const all = walk(rowsMenu()).find((n) => n.tagName === 'BUTTON' && /Show all rows/.test(n.textContent));
    assert.ok(all, 'Show all rows');
    all.listeners.click[0]({ stopPropagation() {} });
    showLimit(env, first);
    assert.equal(placeOf(env, first.key), 'plate', 'the card is back under the chart of its limit once it is shown');
    oneCard();
  } else if (mode === 'the_reading_keeps_the_place') {
    // A timed read, a read that fails, and the read after it: one card, in the same place, the keyboard on
    // the same control — the 30-second redraw is not a reason for the card to move or the focus to be lost.
    showLimit(env, second);
    pick(env, barKey(second));
    byFocus(env.root, 'inspector-diag').focus();
    click(env, 'inspector-diag');
    const where = () => rowsSeq(env).join(' ') + ' | ' + placeOf(env, second.key);
    const placed = where();
    assert.equal(placeOf(env, second.key), 'plate');
    const state = (label) => {
      assert.equal(cards(env).length, 1, label + ': one card');
      assert.equal(where(), placed, label + ': same place');
      assert.ok(keyboardOn(env, 'inspector-diag'), label + ': keyboard on Diagnostics, on a live node');
      assert.ok(classes(cards(env)[0], 'insp-diag-body').length, label + ': Diagnostics still open');
    };
    state('before any read');
    env.interval()(); await settle();
    state('timed read');
    const good = env.window.fetch;
    env.window.fetch = () => Promise.reject(new Error('QA: the read fails'));
    env.interval()(); await settle();
    assert.match(env.root.textContent, /could not be refreshed/);
    state('failed read');
    env.window.fetch = good;
    env.interval()(); await settle();
    assert.doesNotMatch(env.root.textContent, /could not be refreshed/);
    state('the read after');
  } else if (mode === 'the_bar_gone_keeps_the_limit') {
    // The next reading no longer has the opening bar: the account is still selected, its card still under the
    // limit it was opened from — the limit is still drawn.
    const only = groups.find((g) => g.bars.length === 1) || second;
    const account = only.bars[0].account;
    showLimit(env, only);
    pick(env, 'bar:' + only.key + ':' + account);
    const gone = JSON.parse(JSON.stringify(fx.view));
    gone.reserve.summary.groups.forEach((g) => { if (g.key === only.key) g.bars = g.bars.filter((b) => b.account !== account); });
    env.window.fetch = (url, options = {}) => Promise.resolve(response(options.method === 'POST' ? { ok: true } : gone));
    env.interval()(); await settle();
    assert.ok(byFocus(env.root, 'bar:' + only.key + ':' + account) === undefined, 'the opening bar is gone');
    oneCard();
    assert.equal(after(rowsSeq(env), 'row:' + only.key), 'card', rowsSeq(env).join(' '));
  } else {
    throw new Error('unknown case ' + mode);
  }
  env.disposeHooks[0]();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""

CASES = ["bar_card_under_its_limit", "chart_moves_card_stays", "another_selection_moves_the_card",
         "list_row_card_under_its_row", "hidden_origin_limit", "the_reading_keeps_the_place",
         "the_bar_gone_keeps_the_limit"]


@pytest.fixture(scope="module")
def widget_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("fixture"))["view"]


@pytest.mark.parametrize("case", CASES)
def test_the_card_stands_where_its_selection_came_from(widget_view, case):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    harness = harness.replace("    root: made.root,", "    window,\n    root: made.root,")
    assert "    window,\n    root: made.root," in harness
    result = subprocess.run(
        [str(node), "-e", harness + NODE_PLACEMENT], cwd=WIDGET.parent,
        env={**os.environ, "WIDGET_PATH": str(WIDGET), "PLACEMENT_CASE": case,
             "PLACEMENT_FIXTURE": json.dumps({"view": widget_view})},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# In a real browser: the rectangles the reader sees.

REAL_STUB = r"""
(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true } : view) });
  };
}
"""

REAL_STUB = with_i18n(REAL_STUB)

# Under a chart the card stands in the plate (.lrow > .ins-wrap > .ins-fold), right after the chart (.tl-wrap > .tl-block) and tucked under the chart's own plate: 16px of its top go under
# it (the chart is the higher layer), 16px in from each side of it. In the account list it stands alone, one row of the table. `unit` is the card's fold (the row it stands as).
# The chart of a picked limit is the plate's own child (it stands right under the bars), so the card's `prev` is that chart.
WHERE = """() => {
  const cards = [...document.querySelectorAll('.inspector')];
  const c = cards[0] || null;
  const unit = c && (c.closest('.ins-wrap') || c);
  const prev = unit && unit.previousElementSibling;
  const plate = unit && unit.parentElement && unit.parentElement.classList.contains('lrow') ? unit.parentElement : null;
  const own = plate && plate.querySelector('[data-focus^="limit:"]');
  const block = prev && prev.querySelector('.tl-block');
  const R = (n) => n.getBoundingClientRect();
  const gap = unit && prev ? R(unit).top - R(prev).bottom : null;
  // What the reader sees at a point of the card's top edge: the chart's plate over it (tucked under) or the card's own button.
  const hit = c ? document.elementFromPoint(R(c).left + R(c).width / 2, R(c).top + 4) : null;
  return { count: cards.length, parent: unit ? unit.parentElement.className : null,
           plateLimit: own ? own.getAttribute('data-focus').slice(6) : null,
           prevLimit: own ? own.getAttribute('data-focus').slice(6) : null,
           prevIsChart: !!(prev && prev.classList.contains('tl-wrap') && block),
           rowGap: plate ? (parseFloat(getComputedStyle(plate).rowGap) || 0) + (parseFloat(getComputedStyle(prev).marginBottom) || 0) : null,
           left: block && c ? R(c).left - R(block).left : null, right: block && c ? R(block).right - R(c).right : null,
           under: block && c ? R(block).bottom - R(c).top : null,
           chartOver: !!(hit && block && block.contains(hit)),
           prevFocus: prev && prev.getAttribute ? prev.getAttribute('data-focus') : null,
           chartInPlate: !!(prev && prev.querySelector('.tl-block')), gap,
           active: document.activeElement && document.activeElement.getAttribute
             ? document.activeElement.getAttribute('data-focus') : null,
           overflow: document.documentElement.scrollWidth > window.innerWidth + 1 };
}"""


def _pick(page, selector):
    """A press on a bar or a row for the reader's own pick: the top one is picked already, and a press on a picked one lets it go."""
    if page.get_attribute(selector, "aria-pressed") == "true":
        page.click(selector)
    page.click(selector)
    # A pick that follows a pick let go goes in two moves (the first card and the chart shut, then the new ones open: --dur-5 and a little): the card is awaited.
    page.wait_for_function("document.querySelectorAll('.inspector').length === 1", timeout=3000)


# The card and the chart over it open in folds (--dur-5): the rectangles are read where the card has stood still for twenty frames.
STILL = """() => new Promise((done) => {
  const at = () => { const c = document.querySelector('.inspector'); const t = document.querySelector('.tl-top') || document.body;
    return c ? [c.getBoundingClientRect().top, c.getBoundingClientRect().height, t.getBoundingClientRect().bottom].join(':') : ''; };
  let last = at(), same = 0;
  const tick = () => { const now = at(); same = now === last && now ? same + 1 : 0; last = now; if (same >= 20) done(true); else requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
})"""


@pytest.mark.parametrize("engine, width", [("chromium", 1120), ("chromium", 390), ("webkit", 390)])
def test_real_browser_the_card_touches_the_row_it_came_from(widget_view, engine, width):
    sync_api = pytest.importorskip("playwright.sync_api")
    groups = [g for g in widget_view["reserve"]["summary"]["groups"] if any(b.get("account") for b in g.get("bars") or [])]
    assert len(groups) >= 2
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
            page.evaluate(REAL_STUB, widget_view)
            page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
            page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            page.wait_for_selector(".tl-block .chart-svg")
            for g in groups:
                key = "bar:%s:%s" % (g["key"], g["bars"][0]["account"])
                # One plate: show the limit first (its window, then its scope).
                page.click('[data-focus="win:%s:%s"]' % (g["harness"], g["window_seconds"]))
                if page.locator('[data-focus="scope:%s"]' % g["key"]).count():
                    page.click('[data-focus="scope:%s"]' % g["key"])
                _pick(page, '[data-focus="%s"]' % key)
                page.evaluate(STILL)
                where = page.evaluate(WHERE)
                # The card is the plate's own child, right after the chart, in the plate of the limit its bar is in.
                assert where["count"] == 1 and "lrow" in where["parent"].split() and where["plateLimit"] == g["key"], (g["key"], where)
                assert where["prevIsChart"] and where["chartInPlate"], ("the card stands right after the chart of its own limit", g["key"], where)
                if width > 520:
                    # Wide: a second, narrower card whose top goes 16px under the chart's plate (the plate is the higher layer), 16px in from each side.
                    assert abs(where["under"] - 16) <= 1.5, ("16px of the card go under the chart", g["key"], where)
                    assert abs(where["left"] - 16) <= 1.5 and abs(where["right"] - 16) <= 1.5, ("16px narrower on each side", g["key"], where)
                    assert where["chartOver"], ("the chart's plate is over the card", g["key"], where)
                else:
                    # Narrow (520px and under): the card is as wide as the chart and stands under it, not tucked: one row of the plate below it.
                    assert abs(where["left"]) <= 1.5 and abs(where["right"]) <= 1.5, ("as wide as the chart", g["key"], where)
                    assert abs(where["gap"] - where["rowGap"]) <= 1.5, ("under the chart, the gap the plate keeps between its rows", g["key"], where)
                assert where["active"] == key and not where["overflow"], where
                page.keyboard.press("Escape")
                # The pick let go, the card shuts by the height of its fold (--dur-5) and leaves with the end of it: nothing is left behind.
                page.wait_for_function("document.querySelectorAll('.inspector').length === 0", timeout=3000)
                assert page.evaluate(WHERE)["count"] == 0
            # A row of the account list. Rule: the list stands in the place of the bars, the chart and the card, and a row
            # picks its account and shuts the list; the card is no more the row's next sibling, it stands in the plate right after the chart, and the bar of the account is picked.
            # The plate shown is the first limit. A row that is not the picked one is pressed (the picked one is the plate's own pick already).
            page.click('[data-focus="win:%s:%s"]' % (groups[0]["harness"], groups[0]["window_seconds"]))
            if page.locator('[data-focus="scope:%s"]' % groups[0]["key"]).count():
                page.click('[data-focus="scope:%s"]' % groups[0]["key"])
            page.click('[data-focus="accounts"]')
            row = page.locator('.acc-row:not(.active)').first.get_attribute("data-focus")
            page.click('[data-focus="%s"]' % row)
            page.wait_for_function("document.querySelector('[data-focus=\"accounts\"]').getAttribute('aria-pressed') === 'false'", timeout=3000)
            page.wait_for_function("document.querySelectorAll('.inspector').length === 1", timeout=3000)
            page.evaluate(STILL)
            where = page.evaluate(WHERE)
            assert where["count"] == 1 and where["prevFocus"] != row, ("the card is not the row's next sibling", row, where)
            if "lrow" in where["parent"].split():
                assert where["prevIsChart"], where
                assert page.locator('[data-focus^="bar:"][data-focus$=":%s"][aria-pressed="true"]' % row[len("acct:"):]).count() == 1, ("the bar of the row account is picked", where)
            else:
                # No bar of that account in the plate shown: the card stands in the shut section of the accounts, as the one place left for it.
                assert where["parent"] == "accounts", where
            # The word over the bars, from the keyboard: the list opens, the card stays in its plate and the keyboard stays on the word.
            page.focus('[data-focus="accounts"]')
            page.keyboard.press("Enter")
            page.wait_for_function("document.querySelector('[data-focus=\"accounts\"]').getAttribute('aria-pressed') === 'true'", timeout=3000)
            where = page.evaluate(WHERE)
            assert where["count"] == 1 and "lrow" in where["parent"].split() and where["active"] == "accounts", where
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_narrow_accounts_keep_the_whole_long_model_label_inside_the_row(tmp_path, engine):
    from test_ui_timeline import _views, LONG_MODEL
    sync_api = pytest.importorskip("playwright.sync_api")
    view = _views(tmp_path)["default"]
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
            page.evaluate(REAL_STUB, view)
            page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
            page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            page.click('[data-focus="accounts"]')
            cells = page.locator('.acc-row .acc-cell').evaluate_all("""nodes => nodes
              .filter(n => (n.dataset.limit || '').toLowerCase().includes('m'.repeat(80)))
              .map(n => { const b=n.getBoundingClientRect(), row=n.closest('.acc-row').getBoundingClientRect();
                return {label:n.dataset.limit, spoken:n.closest('.acc-row').getAttribute('aria-label'),
                  left:b.left, right:b.right, rowLeft:row.left, rowRight:row.right,
                  scroll:n.scrollWidth, client:n.clientWidth, wrap:getComputedStyle(n).overflowWrap}; })""")
            assert cells, "the real backend fixture must contain the excluded long model label"
            for cell in cells:
                assert LONG_MODEL in cell["label"].lower() and LONG_MODEL in cell["spoken"].lower(), cell
                assert cell["wrap"] == "anywhere", cell
                assert cell["left"] >= cell["rowLeft"] - 1 and cell["right"] <= cell["rowRight"] + 1, cell
                assert cell["scroll"] <= cell["client"] + 1, cell
        finally:
            browser.close()
