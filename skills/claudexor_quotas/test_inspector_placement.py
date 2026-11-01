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
from test_reserve import _node, _widget_fixture

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
// The stub keeps focus on a node a redraw removed; the keyboard is where it should be only on a live node.
const keyboardOn = (env, key) => walk(env.root).includes(active(env)) && active(env).getAttribute('data-focus') === key;
const cards = (env) => classes(env.root, 'inspector');
const limitKey = (row) => classes(row, 'l-chart')[0].getAttribute('data-focus').slice(6);
// The children of a list, one word each: a limit row, the card, a chart, an account row.
const seq = (parent) => parent.childNodes.map((n) => has(n, 'inspector') ? 'card' : has(n, 'tl-block') ? 'chart'
  : has(n, 'lrow') ? 'row:' + limitKey(n) : has(n, 'acc-row') ? n.getAttribute('data-focus') : cls(n)[0]);
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
  const rootKinds = () => env.root.childNodes.map((n) => cls(n)[0]);
  const oneCard = () => {
    assert.equal(cards(env).length, 1, 'exactly one card');
    assert.ok(!rootKinds().includes('inspector'), 'never at the far end of the screen');
  };

  if (mode === 'bar_card_under_its_limit') {
    // Every limit with a named bar: its bar's card is its next sibling; a chart that stood under that limit
    // follows the card, one that stands under another limit stays under that one.
    for (const g of groups) {
      click(env, barKey(g));
      oneCard();
      const rows = rowsSeq(env);
      assert.equal(after(rows, 'row:' + g.key), 'card', 'the card is under ' + g.key + ': ' + rows.join(' '));
      assert.equal(rows.filter((x) => x === 'chart').length, 1, 'one chart');
      const chartedKey = charted()[0];
      assert.equal(rows[rows.indexOf('chart') - 1], g.key === chartedKey ? 'card' : 'row:' + chartedKey,
        'the chart stays under its own limit; only the origin limit has the card before it');
      assert.ok(keyboardOn(env, barKey(g)), 'the keyboard stays on the bar');
      click(env, 'inspector-clear');
      assert.equal(cards(env).length, 0);
    }
  } else if (mode === 'chart_moves_card_stays') {
    click(env, barKey(first));
    click(env, 'limit:' + second.key);
    assert.deepEqual(charted(), [second.key]);
    let rows = rowsSeq(env);
    assert.equal(after(rows, 'row:' + first.key), 'card', rows.join(' '));
    assert.equal(after(rows, 'row:' + second.key), 'chart');
    // The chart moves INTO the origin row: the card is between the row and its chart.
    click(env, 'limit:' + first.key);
    rows = rowsSeq(env);
    assert.deepEqual(charted(), [first.key]);
    assert.deepEqual(rows.slice(rows.indexOf('row:' + first.key), rows.indexOf('row:' + first.key) + 3),
      ['row:' + first.key, 'card', 'chart']);
    // Folded: the chart leaves, the card stays.
    click(env, 'limit:' + first.key);
    rows = rowsSeq(env);
    assert.ok(!rows.includes('chart'));
    assert.equal(after(rows, 'row:' + first.key), 'card');
    oneCard();
  } else if (mode === 'another_selection_moves_the_card') {
    // Two bars of two different accounts in two limits (a bar of the selected account clears it instead).
    let i1 = -1, i2 = -1;
    first.bars.forEach((a, i) => second.bars.forEach((b, j) => {
      if (i1 < 0 && a.account && b.account && a.account !== b.account) { i1 = i; i2 = j; }
    }));
    assert.ok(i1 >= 0, 'the fixture has two accounts in two limits');
    click(env, barKey(first, i1));
    assert.equal(after(rowsSeq(env), 'row:' + first.key), 'card');
    click(env, barKey(second, i2));
    oneCard();
    const rows = rowsSeq(env);
    assert.equal(after(rows, 'row:' + second.key), 'card');
    assert.notEqual(after(rows, 'row:' + first.key), 'card');
    // A bar of the same account again clears it (the existing toggle).
    click(env, barKey(second, i2));
    assert.equal(cards(env).length, 0);
  } else if (mode === 'list_row_card_under_its_row') {
    click(env, 'accounts');
    click(env, 'acct:codex:c2');
    oneCard();
    assert.ok(!rowsSeq(env).includes('card'), 'not among the limits');
    assert.equal(after(tableSeq(env), 'acct:codex:c2'), 'card', tableSeq(env).join(' '));
    assert.ok(keyboardOn(env, 'acct:codex:c2'));
    // Folded: the card stays, under the list's header.
    click(env, 'accounts');
    oneCard();
    const box = cards(env)[0].parentNode;
    assert.ok(has(box, 'accounts'), 'in the accounts section');
    assert.ok(has(box.childNodes[box.childNodes.indexOf(cards(env)[0]) - 1], 'acc-head'), 'right under its header');
    // Open again: back under its row. A bar of another account takes the card out of the list.
    click(env, 'accounts');
    assert.equal(after(tableSeq(env), 'acct:codex:c2'), 'card');
    click(env, barKey(first));
    oneCard();
    assert.ok(!tableSeq(env).includes('card'));
    assert.equal(after(rowsSeq(env), 'row:' + first.key), 'card');
  } else if (mode === 'hidden_origin_limit') {
    // The limit that opened the card is hidden from the rows (display only): the card goes to the account's own
    // row of the list — or under the list's header — and returns under the limit when the limit does.
    const rowsMenu = () => classes(env.root, 'rows-menu')[0];
    const limitName = (g) => classes(env.root, 'lrow').map((r) => [limitKey(r), classes(r, 'l-name-text')[0].textContent])
      .find((p) => p[0] === g.key)[1];
    click(env, 'accounts');
    click(env, barKey(first));
    const account = first.bars[0].account;
    const box = walk(rowsMenu()).find((n) => n.tagName === 'INPUT' && n.getAttribute('aria-label') === 'Show ' + limitName(first));
    assert.ok(box, 'the Rows menu offers the limit');
    box.checked = false;
    box.listeners.change[0]();
    assert.ok(!rowsSeq(env).includes('row:' + first.key), 'the limit is hidden');
    oneCard();
    assert.equal(after(tableSeq(env), 'acct:' + account), 'card', tableSeq(env).join(' '));
    click(env, 'accounts');
    oneCard();
    assert.ok(has(cards(env)[0].parentNode, 'accounts'));
    const all = walk(rowsMenu()).find((n) => n.tagName === 'BUTTON' && /Show all rows/.test(n.textContent));
    assert.ok(all, 'Show all rows');
    all.listeners.click[0]({ stopPropagation() {} });
    assert.equal(after(rowsSeq(env), 'row:' + first.key), 'card', 'the card is back under its limit');
    oneCard();
  } else if (mode === 'the_reading_keeps_the_place') {
    // A timed read, a read that fails, and the read after it: one card, in the same place, the keyboard on
    // the same control — the 30-second redraw is not a reason for the card to move or the focus to be lost.
    click(env, barKey(second));
    byFocus(env.root, 'inspector-diag').focus();
    click(env, 'inspector-diag');
    const placed = rowsSeq(env).join(' ');
    assert.equal(after(rowsSeq(env), 'row:' + second.key), 'card');
    const state = (label) => {
      assert.equal(cards(env).length, 1, label + ': one card');
      assert.equal(rowsSeq(env).join(' '), placed, label + ': same place');
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
    click(env, 'bar:' + only.key + ':' + account);
    const gone = JSON.parse(JSON.stringify(fx.view));
    gone.reserve.summary.groups.forEach((g) => { if (g.key === only.key) g.bars = g.bars.filter((b) => b.account !== account); });
    env.window.fetch = (url, options = {}) => Promise.resolve(response(options.method === 'POST' ? { ok: true } : gone));
    env.interval()(); await settle();
    assert.equal(byFocus(env.root, 'bar:' + only.key + ':' + account), undefined, 'the opening bar is gone');
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

WHERE = """() => {
  const cards = [...document.querySelectorAll('.inspector')];
  const c = cards[0] || null;
  const prev = c && c.previousElementSibling;
  const next = c && c.nextElementSibling;
  const limit = prev && prev.querySelector('[data-focus^="limit:"]');
  const gap = c && prev ? c.getBoundingClientRect().top - prev.getBoundingClientRect().bottom : null;
  return { count: cards.length, parent: c ? c.parentElement.className : null,
           prevLimit: limit ? limit.getAttribute('data-focus').slice(6) : null,
           prevFocus: prev && prev.getAttribute ? prev.getAttribute('data-focus') : null,
           nextIsChart: !!(next && next.classList.contains('tl-block')), gap,
           active: document.activeElement && document.activeElement.getAttribute
             ? document.activeElement.getAttribute('data-focus') : null,
           overflow: document.documentElement.scrollWidth > window.innerWidth + 1 };
}"""


def _launch(playwright, engine):
    try:
        return getattr(playwright, engine).launch()
    except Exception as exc:  # no such browser build for this Playwright here
        pytest.skip(f"no {engine} build for Playwright on this machine ({type(exc).__name__})")


@pytest.mark.parametrize("engine, width", [("chromium", 1120), ("chromium", 390), ("webkit", 390)])
def test_real_browser_the_card_touches_the_row_it_came_from(widget_view, engine, width):
    sync_api = pytest.importorskip("playwright.sync_api")
    groups = [g for g in widget_view["reserve"]["summary"]["groups"] if any(b.get("account") for b in g.get("bars") or [])]
    chart_key = widget_view["reserve"]["chart"]["group_key"]
    assert len(groups) >= 2
    with sync_api.sync_playwright() as playwright:
        browser = _launch(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
            page.evaluate(REAL_STUB, widget_view)
            page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
            page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            page.wait_for_selector(".tl-block .chart-svg")
            for g in groups:
                key = "bar:%s:%s" % (g["key"], g["bars"][0]["account"])
                page.click('[data-focus="%s"]' % key)
                where = page.evaluate(WHERE)
                assert where["count"] == 1 and where["parent"] == "lrows" and where["prevLimit"] == g["key"], (g["key"], where)
                assert -1.5 <= where["gap"] <= 2, (g["key"], where)
                assert where["nextIsChart"] == (g["key"] == chart_key), "the card is before the chart of its own limit only"
                assert where["active"] == key and not where["overflow"], where
                page.keyboard.press("Escape")
                assert page.evaluate(WHERE)["count"] == 0
            # A row of the account list: the card is the row's next sibling, and a fold leaves it under the header.
            page.click('[data-focus="accounts"]')
            page.click('[data-focus="acct:codex:c2"]')
            where = page.evaluate(WHERE)
            assert where["count"] == 1 and where["prevFocus"] == "acct:codex:c2" and -1.5 <= where["gap"] <= 2, where
            assert "lrows" not in where["parent"]
            page.focus('[data-focus="accounts"]')
            page.keyboard.press("Enter")
            where = page.evaluate(WHERE)
            assert where["count"] == 1 and where["parent"] == "accounts" and where["active"] == "accounts", where
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_narrow_accounts_keep_the_whole_long_model_label_inside_the_row(tmp_path, engine):
    from test_ui_timeline import _views, LONG_MODEL
    sync_api = pytest.importorskip("playwright.sync_api")
    view = _views(tmp_path)["default"]
    with sync_api.sync_playwright() as playwright:
        browser = _launch(playwright, engine)
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
