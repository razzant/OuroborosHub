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
  const ring = byFocus(env.root, 'ring:' + g.harness + ':' + g.key);
  assert.ok(ring, 'a ring for ' + g.key);
  if (ring.getAttribute('aria-pressed') !== 'true') ring.listeners.click[0]({ stopPropagation() {} });
  assert.ok(classes(env.root, 'q-plate').some((p) => p.getAttribute('data-limit') === g.key), 'the plate of ' + g.key + ' is shown');
}
// The stub keeps focus on a node a redraw removed; the keyboard is where it should be only on a live node.
const keyboardOn = (env, key) => walk(env.root).includes(active(env)) && active(env).getAttribute('data-focus') === key;
// The cards that are seen: the opened account's card beside the bars of the plate (not the stand-in that asks for a pick, and not while the list of
// accounts stands in the plate's place: then the plate is folded away and inert) and the account's full card under its row of the list.
const shut = (n) => { for (let up = n.parentNode; up; up = up.parentNode) if (up.inert === true) return true; return false; };
const cards = (env) => classes(env.root, 'q-card').filter((c) => !has(c, 'q-card-none') && !shut(c)).concat(classes(env.root, 'inspector'));
const limitKey = (plate) => plate.getAttribute('data-limit');
// The children of the box of the limits, one word each: the plate of a limit, the history under it.
const seq = (parent) => parent.childNodes.map((n) => has(n, 'inspector') ? 'card'
  : has(n, 'tl-block') || has(n, 'tl-wrap') ? 'chart'
  : has(n, 'q-plate') ? 'row:' + limitKey(n) : has(n, 'acc-row') ? n.getAttribute('data-focus') : cls(n)[0]);
const plateOf = (env, key) => classes(env.root, 'q-plate').find((r) => limitKey(r) === key);
// Where the card of a limit stands: 'plate' (beside the bars of that limit's plate) or '' (nowhere).
const placeOf = (env, key) => { const r = plateOf(env, key); return r && classes(r, 'q-card').some((c) => !has(c, 'q-card-none')) ? 'plate' : ''; };
const rowsSeq = (env) => seq(classes(env.root, 'lrows')[0]);
// The history stands in the plate of the limit shown, its last part (the fold .tl-wrap).
const chartIn = (env, key) => { const p = plateOf(env, key); return !!p && has(p.childNodes[p.childNodes.length - 1], 'tl-wrap'); };
const tableSeq = (env) => seq(classes(env.root, 'acc-table')[0]);
const after = (list, item) => list[list.indexOf(item) + 1];
const barKey = (g, i = 0) => 'bar:' + g.key + ':' + g.bars[i].account;

(async () => {
  const fx = JSON.parse(process.env.PLACEMENT_FIXTURE);
  const mode = process.env.PLACEMENT_CASE;
  const groups = fx.view.reserve.summary.groups.filter((g) => (g.bars || []).some((b) => b.account));
  assert.ok(groups.length >= 2, 'the fixture has two limits with named bars');
  const env = await boot(fx.view);
  const charted = () => classes(env.root, 'q-ring').filter((r) => r.getAttribute('aria-pressed') === 'true').map((r) => r.getAttribute('data-limit'));
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
      // One plate: the shown limit's, the card beside its bars, the history in it under the bars.
      assert.deepEqual(rows, ['row:' + g.key], 'one plate: ' + rows.join(' '));
      assert.ok(chartIn(env, g.key), 'the history stands in the plate');
      assert.equal(placeOf(env, g.key), 'plate', 'the card stands beside the bars of ' + g.key);
      assert.deepEqual(charted(), [g.key]);
      assert.ok(keyboardOn(env, barKey(g)), 'the keyboard stays on the bar');
      click(env, 'inspector-clear');
      assert.equal(cards(env).length, 0);
      assert.equal(classes(env.root, 'q-card-none').length, 1, 'the card asks for a pick');
    }
  } else if (mode === 'chart_moves_card_stays') {
    showLimit(env, first);
    pick(env, barKey(first));
    showLimit(env, second);
    assert.deepEqual(charted(), [second.key]);
    // Another limit is shown: a pick stands in every limit (the reader's follows where its account has a bar, else the top bar is picked), so the card
    // stands beside the bars of the plate shown, with its history under it; the first limit is not drawn.
    assert.deepEqual(rowsSeq(env), ['row:' + second.key]);
    assert.ok(chartIn(env, second.key));
    assert.equal(placeOf(env, second.key), 'plate');
    oneCard();
    showLimit(env, first);
    assert.deepEqual(charted(), [first.key]);
    assert.deepEqual(rowsSeq(env), ['row:' + first.key]);
    assert.ok(chartIn(env, first.key));
    assert.equal(placeOf(env, first.key), 'plate');
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
    // A bar of the same account again lets it go (the existing toggle).
    click(env, barKey(second, i2));
    assert.equal(cards(env).length, 0);
  } else if (mode === 'list_row_card_under_its_row') {
    // The list opens in the place of the plate (folded away): the picked account's full card stands under its own row, one card; a row picks its
    // account and shuts the list, and the card stands beside the bars again, like the card of a pressed bar.
    click(env, 'accounts');
    oneCard();
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'true', 'the word is pressed while the list stands');
    const picked = classes(env.root, 'acc-row').find((r) => r.getAttribute('aria-pressed') === 'true').getAttribute('data-focus');
    assert.equal(after(tableSeq(env), picked), 'card', 'the card under its row: ' + tableSeq(env).join(' '));
    click(env, 'acct:codex:c2');
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'false', 'a row of the list shuts the list');
    oneCard();
    const shown = rowsSeq(env)[0].slice(4);
    const plateGroup = groups.find((g) => g.key === shown);
    assert.equal(placeOf(env, shown), 'plate', 'the card of the row stands beside the bars');
    const c2 = plateGroup.bars.findIndex((b) => b.account === 'codex:c2');
    assert.ok(c2 >= 0 && byFocus(env.root, barKey(plateGroup, c2)).getAttribute('aria-pressed') === 'true', 'the bar of the row account is picked');
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
    if (family.length > 1) family.filter((g) => g !== first).forEach(hide);
    assert.equal(classes(env.root, 'q-plate').length, 0, 'no limit is drawn');
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
    assert.equal(placeOf(env, first.key), 'plate', 'the card is back beside the bars of its limit once it is shown');
    oneCard();
  } else if (mode === 'the_reading_keeps_the_place') {
    // A timed read, a read that fails, and the read after it: one card, in the same place, the keyboard on
    // the same control — the 30-second redraw is not a reason for the card to move or the focus to be lost.
    showLimit(env, second);
    pick(env, barKey(second));
    byFocus(env.root, 'inspector-details').focus();
    click(env, 'inspector-details');
    const where = () => rowsSeq(env).join(' ') + ' | ' + placeOf(env, second.key);
    const placed = where();
    assert.equal(placeOf(env, second.key), 'plate');
    const state = (label) => {
      assert.equal(cards(env).length, 1, label + ': one card');
      assert.equal(where(), placed, label + ': same place');
      assert.ok(keyboardOn(env, 'inspector-details'), label + ': keyboard on the details, on a live node');
      assert.ok(classes(cards(env)[0], 'q-more-body').length, label + ': the details still open');
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
    assert.equal(placeOf(env, only.key), 'plate', 'the card still beside the bars of the limit it was opened from');
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

# The opened account's card stands in the plate of the limit its bar is in: beside the bars (to their left) where the plate is wide, over them on a phone.
WHERE = """() => {
  const cards = [...document.querySelectorAll('.q-card:not(.q-card-none)')];
  const c = cards[0] || null;
  const plate = c && c.closest('.q-plate');
  const strip = plate && plate.querySelector('.q-bars');
  const R = (n) => n.getBoundingClientRect();
  return { count: cards.length, plateLimit: plate ? plate.getAttribute('data-limit') : null,
           beside: c && strip ? R(c).right <= R(strip).left + 0.5 && R(c).top <= R(strip).bottom : null,
           over: c && strip ? R(c).bottom <= R(strip).top + 0.5 : null,
           inside: c && plate ? R(c).left >= R(plate).left - 0.5 && R(c).right <= R(plate).right + 0.5 : null,
           active: document.activeElement && document.activeElement.getAttribute ? document.activeElement.getAttribute('data-focus') : null,
           overflow: document.documentElement.scrollWidth > window.innerWidth + 1 };
}"""


def _pick(page, selector):
    """A press on a bar for the reader's own pick: the top one is picked already, and a press on a picked one lets it go."""
    if page.get_attribute(selector, "aria-pressed") == "true":
        page.click(selector)
    page.click(selector)
    page.wait_for_function("document.querySelectorAll('.q-card:not(.q-card-none)').length === 1", timeout=3000)


@pytest.mark.parametrize("engine, width", [("chromium", 1120), ("chromium", 390), ("webkit", 390), ("webkit", 598)])
def test_real_browser_the_card_stands_beside_the_bars_of_its_limit(widget_view, engine, width):
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
                page.click('[data-focus="ring:%s:%s"]' % (g["harness"], g["key"]))
                _pick(page, '[data-focus="%s"]' % key)
                where = page.evaluate(WHERE)
                assert where["count"] == 1 and where["plateLimit"] == g["key"] and where["inside"], (g["key"], where)
                if width > 520:
                    assert where["beside"], ("the card stands beside the bars", g["key"], where)
                elif len([b for b in g["bars"] if b.get("account")]) == 1:
                    assert where["beside"], ("on a phone a plate of one account stands its card and its bar side by side", g["key"], where)
                else:
                    assert where["over"], ("on a phone the card stands over the bars", g["key"], where)
                assert where["active"] == key and not where["overflow"], where
                page.keyboard.press("Escape")
                page.wait_for_function("document.querySelectorAll('.q-card:not(.q-card-none)').length === 0", timeout=3000)
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
