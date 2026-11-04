"""The card of the picked account under the chart's plate: a second, narrower card tucked under the plate.

What is held here:
- wide (over 520px) the card is 16px narrower than the plate on each side and its top goes 16px under the plate (the plate is the higher layer); at 520px and under it is as wide as
  the plate, not tucked, one row of the plate below it;
- at rest only the strip shows (52px: the dot, the name, the plan, the lead window at the right); under the pointer (or the keyboard) one more row of the windows (about 96 in all);
  pressed, the whole card; the toggle is a button with aria-expanded, Enter and Space work, a second press folds the card back;
- the opening lives through the poll and the change of the bar and is dropped by a change of the window (or the limit) and of the family;
- with no chart over it (Chart folded) and in the account list the card is an ordinary one: whole, no toggle, Clear within reach; Clear works from the pressed card;
- at 520px and under nothing narrows, the name is not broken by letters, the page does not move sideways;
- the words of the card are in the English table and in the Russian one;
- the number and the notes of the plate stand in the card's summary (.kt-sum), the real nodes, once, and not in the plate; the strip wears copies of the number and of the note's head
  (aria-hidden); a bar let go gives them back in the order of the markup; another bar and the poll do not double them; the chart word pressed in the card stays on the plate and
  brings the chart back; the budget plate (Cursor) and a card out of the plate move nothing; at 360 the stamp and the chart word do not overlap and the page does not move sideways.
Real browsers (Chromium, and WebKit where Playwright has it) for the rectangles, Node (the fake DOM of the other widget tests) for the state itself.
"""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

import test_quotas
from conftest import RU_JSON, RU_JSON_SKIP
from test_reserve import _node, _widget_fixture, with_i18n

WIDGET = Path(os.environ.get("CARD_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
pytest.importorskip("playwright.sync_api")
STRINGS = json.loads(Path(__file__).with_name("strings_en.json").read_text(encoding="utf-8"))

GROUP = "codex|primary|604800|-"
C1, C2, C3 = "codex:c1", "codex:c2", "codex:c3"
TOGGLE = '[data-focus="inspector-toggle"]'
CLEAR = '[data-focus="inspector-clear"]'

# The words of the card in the Russian memory of the install (ru.json, given by QUOTAS_RU_JSON), the ones this file looks at.
RU = {"acc.clear": "Снять выбор", "acc.card_label": "Выбранный аккаунт", "acc.details": "Подробности аккаунта {name}",
      "acc.clear_label": "Снять выбор аккаунта", "acc.strip_of": "из {n}"}
# rule: the row of windows under the pointer (acc.peek_5h, acc.peek_week) is gone with the old "peek" of the strip - the widget no longer asks for those two words
CARD_WORDS = ["acc.clear", "acc.card_label", "acc.details", "acc.clear_label", "acc.strip_of"]


def bar(account, key=GROUP):
    return 'bar:%s:%s' % (key, account)


@pytest.fixture(scope="module")
def base_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("card"))["view"]


@pytest.fixture(scope="module")
def served_views(tmp_path_factory, base_view):
    """The base view (codex, one window, two limits), the claude view with a 5-hour and a weekly window, and the two families in one answer (every family asks, the answer is one)."""
    from test_window_tabs import _two_windows
    windows = _two_windows(tmp_path_factory.mktemp("card-windows"))
    both = copy.deepcopy(windows)
    both["reserve"]["summary"]["groups"] = both["reserve"]["summary"]["groups"] + copy.deepcopy(base_view["reserve"]["summary"]["groups"])
    both["groups"] = both["groups"] + copy.deepcopy(base_view["groups"])  # the head shows the families of these groups
    return {"base": base_view, "windows": windows, "both": both}


def _rename(view, old, new):
    view = copy.deepcopy(view)
    done = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("label") == old and o.get("subject_id") == old:
                o["label"] = new
                done.append(1)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(view)
    assert done, "the label of the account was not found in the fixture"
    return view


# ---------------------------------------------------------------------------
# Node: the state of the card, in the fake DOM.

NODE_CARD = r"""
const cls = (n) => String(n.className || '').split(/\s+/);
const has = (n, name) => cls(n).includes(name);
const click = (env, key) => {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  classes(env.root, 'inspector').forEach(withToggleLookup);  // the card that is on the screen answers the lookup before it is pressed
  node.listeners.click[0]({ stopPropagation() {} });
};
const settle2 = async () => { for (let i = 0; i < 6; i++) await settle(); };
const respond = (env, view) => {
  env.window.fetch = (url, options = {}) => Promise.resolve(response(options.method === 'POST' ? { ok: true } : view));
};
// The fake DOM has no querySelector, and the widget keeps the toggle's aria-expanded in step with the card through card.querySelector('.kt-toggle') (cardShow, which
// raises and lowers the card in place, with no re-draw): give the cards that one lookup, as the real DOM answers it.
const withToggleLookup = (node) => {
  if (node.querySelector) return;
  node.querySelector = (selector) => {
    if (selector !== '.kt-toggle') return null;
    const visit = (n) => { for (const child of n.childNodes) { if (cls(child).includes('kt-toggle')) return child; const found = visit(child); if (found) return found; } return null; };
    return visit(node);
  };
};
const card = (env) => { const found = classes(env.root, 'inspector'); found.forEach(withToggleLookup); return found; };
const toggle = (env) => byFocus(env.root, 'inspector-toggle');
const shown = (env) => { const c = card(env); assert.equal(c.length, 1, 'one card'); return { open: has(c[0], 'kt-open'), exp: toggle(env).getAttribute('aria-expanded') }; };
const press = (env, key) => { const node = byFocus(env.root, key); assert.ok(node, 'missing ' + key); if (node.getAttribute('aria-pressed') === 'true') click(env, key); click(env, key); };
const poll = async (env) => { env.interval()(); await settle2(); };
const limits = (v) => v.reserve.summary.groups.filter((g) => (g.bars || []).some((b) => b.account));
function showLimit(env, g) {
  const tab = byFocus(env.root, 'win:' + g.harness + ':' + g.window_seconds);
  assert.ok(tab, 'a tab for the window of ' + g.key);
  if (tab.getAttribute('aria-selected') !== 'true') tab.listeners.click[0]({ stopPropagation() {} });
  const scope = byFocus(env.root, 'scope:' + g.key);
  if (scope && scope.getAttribute('aria-pressed') !== 'true') scope.listeners.click[0]({ stopPropagation() {} });
  assert.ok(byFocus(env.root, 'limit:' + g.key), 'the plate of ' + g.key + ' is shown');
}

(async () => {
  const fx = JSON.parse(process.env.CARD_FIXTURE);
  const mode = process.env.CARD_CASE;
  const env = await boot(fx.views.base);
  const [first, second] = limits(fx.views.base);
  const K = (g, a) => 'bar:' + g.key + ':' + a;

  if (mode === 'a_button_with_aria_expanded_and_a_second_press_folds') {
    const t = toggle(env);
    assert.ok(t && t.tagName === 'BUTTON', 'the toggle is a button');
    assert.equal(t.getAttribute('type'), 'button');
    assert.equal(t.getAttribute('aria-label'), 'Details of c2');
    assert.deepEqual(shown(env), { open: false, exp: 'false' }, 'shut at the opening');
    click(env, 'inspector-toggle');
    assert.deepEqual(shown(env), { open: true, exp: 'true' }, 'pressed');
    click(env, 'inspector-toggle');
    assert.deepEqual(shown(env), { open: false, exp: 'false' }, 'a second press folds it');
    click(env, 'inspector-toggle');
    assert.deepEqual(shown(env), { open: true, exp: 'true' }, 'and a third opens it again');
  } else if (mode === 'the_opening_lives_through_the_poll_and_the_bar') {
    click(env, 'inspector-toggle');
    assert.deepEqual(shown(env), { open: true, exp: 'true' });
    await poll(env); await poll(env);
    assert.deepEqual(shown(env), { open: true, exp: 'true' }, 'the poll draws the card as it stood');
    press(env, K(first, 'codex:c1'));
    await settle2();
    assert.equal(toggle(env).getAttribute('aria-label'), 'Details of c1', 'another bar: another card');
    // rule: another bar lowers the card (the new bar's card stands in its strip, the chart under the new bar); it was: the change of the bar keeps it open
    assert.deepEqual(shown(env), { open: false, exp: 'false' }, 'the change of the bar lowers the card into its strip');
    await poll(env);
    assert.deepEqual(shown(env), { open: false, exp: 'false' }, 'and the poll keeps it down');
    click(env, 'inspector-toggle');
    assert.deepEqual(shown(env), { open: true, exp: 'true' }, 'the new bar\'s card can be pressed up again');
  } else if (mode === 'the_limit_and_the_window_and_the_family_drop_the_opening') {
    // another limit of the same window (the scope)
    click(env, 'inspector-toggle');
    assert.equal(shown(env).open, true);
    showLimit(env, second);
    await settle2();
    if (card(env).length) assert.equal(shown(env).open, false, 'another limit: shut');
    // back to the first: it is shut there, too (the opening was dropped by the move)
    showLimit(env, first);
    await settle2();
    assert.equal(shown(env).open, false, 'back: still shut');
    // the window: the claude view with a 5-hour and a weekly window
    env.disposeHooks[0]();
    const win = await boot(fx.views.windows);
    const [one, two] = limits(fx.views.windows);
    assert.ok(one && two && one.window_seconds !== two.window_seconds, 'two windows with named bars');
    showLimit(win, one);
    await settle2();
    assert.equal(card(win).length, 1);
    click(win, 'inspector-toggle');
    assert.equal(shown(win).open, true, 'pressed in the first window');
    await poll(win);
    assert.equal(shown(win).open, true, 'the poll keeps it');
    showLimit(win, two);
    await settle2();
    assert.equal(card(win).length, 1);
    assert.equal(shown(win).open, false, 'another window: shut');
    win.disposeHooks[0]();
    // the family
    const fam = await boot(fx.views.both);
    respond(fam, fx.views.both);
    click(fam, 'inspector-toggle');
    assert.equal(shown(fam).open, true, 'pressed in the first family');
    const heads = ['harness:claude', 'harness:codex'].map((k) => byFocus(fam.root, k));
    assert.ok(heads[0] && heads[1], 'the head shows two families');
    const current = heads.find((h) => h.getAttribute('aria-selected') === 'true' || h.getAttribute('aria-pressed') === 'true' || h.getAttribute('aria-current'));
    const other = heads.find((h) => h !== current) || heads[1];
    other.listeners.click[0]({ stopPropagation() {} });
    await settle2(); await settle2();
    assert.equal(card(fam).length, 1, 'the other family has its card');
    assert.equal(shown(fam).open, false, 'another family: shut');
    fam.disposeHooks[0]();
    return;
  } else if (mode === 'with_the_chart_folded_the_card_stands_in_its_own_place') {
    const key = 'limit:' + first.key;
    const holder = (c) => c.parentNode.parentNode.parentNode;
    assert.ok(has(holder(card(env)[0]), 'wt-plate'), 'in the plate under the chart');
    click(env, key);
    await settle2();
    assert.equal(card(env).length, 1, 'the card stays');
    assert.ok(!has(holder(card(env)[0]), 'wt-plate'), 'out of the plate: no chart over it');
    assert.equal(classes(env.root, 'tl-block').length, 0, 'no chart');
  } else if (mode === 'clear_lets_go_of_the_pick') {
    click(env, 'inspector-toggle');
    click(env, 'inspector-clear');
    await settle2();
    assert.equal(card(env).length, 0, 'no card after Clear');
    assert.equal(classes(env.root, 'bar').filter((b) => b.getAttribute('aria-pressed') === 'true').length, 0, 'no bar pressed');
  } else {
    throw new Error('unknown case ' + mode);
  }
  env.disposeHooks[0]();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""

NODE_CASES = ["a_button_with_aria_expanded_and_a_second_press_folds", "the_opening_lives_through_the_poll_and_the_bar",
              "the_limit_and_the_window_and_the_family_drop_the_opening", "with_the_chart_folded_the_card_stands_in_its_own_place",
              "clear_lets_go_of_the_pick"]


@pytest.mark.parametrize("case", NODE_CASES)
def test_node_the_state_of_the_card(served_views, case):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    harness = harness.replace("    root: made.root,", "    window,\n    root: made.root,")
    assert "    window,\n    root: made.root," in harness
    result = subprocess.run(
        [str(node), "-e", harness + NODE_CARD], cwd=WIDGET.parent,
        env={**os.environ, "WIDGET_PATH": str(WIDGET), "CARD_CASE": case, "CARD_FIXTURE": json.dumps({"views": served_views})},
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_words_of_the_card_are_in_the_english_table_and_the_widget_asks_for_them():
    widget = WIDGET.read_text(encoding="utf-8")
    for key in CARD_WORDS:
        assert key in STRINGS, key
        assert ("'%s'" % key) in widget, "the widget never asks for %s" % key
    assert STRINGS["acc.details"] == "Details of {name}" and "{name}" in RU["acc.details"]
    assert STRINGS["acc.clear"] == "Clear"
    # rule: the dead words are out of the table (the widget never asks for them any more; test_i18n keeps the table and the widget in step)
    assert not {"cover.outside", "cover.plain", "acc.peek_5h", "acc.peek_week"} & set(STRINGS)
    assert STRINGS["acc.strip_of"] == "of {n}" and RU["acc.strip_of"] == "из {n}", "the strip's copy of the number says its count by this word, never by parsing the caption"


def test_the_russian_words_here_are_the_ones_of_the_install_memory():
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    entries = json.loads(RU_JSON.read_text(encoding="utf-8")).get("entries", {})
    for key in CARD_WORDS:
        item = entries.get("code:quotas.ui." + key)
        assert item and item["text"] == RU[key] and item["source"] == STRINGS[key], (key, item)


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
}
""")

# rule: a card that is up (pressed, under the pointer, focused) no longer sends the line of the pick back into the bar: the outline and the stem stand through it, so a traced chart is waited for always
SETTLED = """() => !document.querySelector('.plate-trace, .tl-skel, .tl-wait, .tl-reveal, .fold-run, .fold-shut, .bar-arrow:not(.on)')
  && (!document.querySelector('.bars > .sel') || !!document.querySelector('.tl-block.tl-traced'))"""

# The card, its strip, the row of windows and the rest of it; the card and the chart open in folds (--dur-5), so these are read where the card has stood still.
STILL = """() => new Promise((done) => {
  const at = () => { const c = document.querySelector('.inspector'); const t = document.querySelector('.tl-top') || document.body;
    return c ? [c.getBoundingClientRect().top, c.getBoundingClientRect().height, t.getBoundingClientRect().bottom].join(':') : ''; };
  let last = at(), same = 0;
  const start = performance.now();
  const tick = () => { const now = at(); same = now === last && now ? same + 1 : 0; last = now; if (same >= 24 || performance.now() - start > 4000) done(same >= 24); else requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
})"""

MEASURE = """() => {
  const R = (n) => { const r = n.getBoundingClientRect(); return { t: r.top, b: r.bottom, l: r.left, r: r.right, h: r.height, w: r.width }; };
  const q = (s) => document.querySelector(s);
  const c = q('.inspector'), blk = q('.wt-plate .tl-block'), top = q('.kt-top'), tg = q('.kt-toggle'), more = q('.kt-more'), clip = q('.kt-clip'), pad = q('.kt-pad');
  const hit = c && blk ? document.elementFromPoint(R(c).l + R(c).w / 2, R(c).t + 4) : null;
  const wrap = q('.ins-wrap');
  const rule = q('.kt-rule');
  // The summary (.kt-sum) and the rule under it stand first in the pressed card; what is left of the card is the rest it had before the summary came.
  const sumBlock = rule && pad ? rule.getBoundingClientRect().bottom + (parseFloat(getComputedStyle(rule).marginBottom) || 0) - pad.getBoundingClientRect().top : 0;
  return { sumBlock, sum: q('.kt-sum') && R(q('.kt-sum')), card: c && R(c), blk: blk && R(blk), top: top && R(top), more: more && R(more), pad: pad && R(pad),
    peek: q('.kt-peek-wrap') && R(q('.kt-peek-wrap')), open: !!c && c.classList.contains('kt-open'), exp: tg && tg.getAttribute('aria-expanded'),
    label: tg && tg.getAttribute('aria-label'), toggleShown: !!tg && getComputedStyle(tg).display !== 'none',
    toggleLabel: tg && tg.getAttribute('aria-label'), lead: q('.kt-lead') && q('.kt-lead').textContent, leadTitle: q('.kt-lead') && q('.kt-lead').title,
    leadShown: !!q('.kt-lead') && getComputedStyle(q('.kt-lead')).display !== 'none',
    clip: clip && getComputedStyle(clip).visibility, clearVis: q('.insp-clear') && getComputedStyle(q('.insp-clear')).visibility,
    clearText: q('.insp-clear') && q('.insp-clear').textContent, clearLabel: q('.insp-clear') && q('.insp-clear').getAttribute('aria-label'),
    cardLabel: c && c.getAttribute('aria-label'), peekWords: [...document.querySelectorAll('.kt-pk')].map((n) => n.textContent),
    chartOver: !!(hit && blk && blk.contains(hit)), inPlate: !!(c && c.closest('.wt-plate')), blocks: document.querySelectorAll('.tl-block').length,
    wrapMargin: wrap ? getComputedStyle(wrap).marginTop : null, rowGap: c && c.closest('.wt-plate') ? (parseFloat(getComputedStyle(c.closest('.wt-plate')).rowGap) || 0)
      + (parseFloat(getComputedStyle(c.closest('.ins-wrap').previousElementSibling).marginBottom) || 0) : null,
    overflow: document.documentElement.scrollWidth > window.innerWidth + 1, scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth };
}"""

NAME = """() => {
  const n = document.querySelector('.insp-name'), r = n.getBoundingClientRect(), s = getComputedStyle(n);
  const range = document.createRange(); range.selectNodeContents(n);
  const pieces = [...document.querySelectorAll('.kt-pk')].map((p) => ({ h: p.getBoundingClientRect().height, lines: new Set([...p.getClientRects()].map((x) => Math.round(x.top))).size }));
  return { h: r.height, line: parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.4, w: r.width, scroll: n.scrollWidth, client: n.clientWidth,
           lines: new Set([...range.getClientRects()].map((x) => Math.round(x.top))).size, wrap: s.overflowWrap, space: s.whiteSpace, ellipsis: s.textOverflow, pieces,
           card: document.querySelector('.inspector').getBoundingClientRect().right, blk: document.querySelector('.wt-plate .tl-block').getBoundingClientRect().right };
}"""


# The card raised in the place of the chart: the chart folded into its strip (.tl-strip), the card under it, four corners, the height of the document the same.
RAISED = """() => {
  const R = (n) => { if (!n) return null; const r = n.getBoundingClientRect(); return { t: r.top, b: r.bottom, l: r.left, r: r.right, h: r.height, w: r.width }; };
  const q = (s) => document.querySelector(s);
  const c = q('.inspector'), wrap = q('.wt-plate > .tl-wrap'), strip = q('.wt-plate > .tl-wrap > .tl-strip'), plate = q('.wt-plate');
  const cs = c ? getComputedStyle(c) : null, ss = strip ? getComputedStyle(strip) : null;
  return { card: R(c), wrap: R(wrap), strip: R(strip), plate: R(plate), stripText: strip ? strip.textContent.trim() : null,
    stripShown: !!ss && ss.visibility !== 'hidden' && ss.opacity === '1', up: !!c && c.classList.contains('kt-open'),
    radii: cs ? [cs.borderTopLeftRadius, cs.borderTopRightRadius, cs.borderBottomRightRadius, cs.borderBottomLeftRadius] : null,
    docH: document.documentElement.scrollHeight };
}"""


def _open(browser, view, width=1120, height=1000, reduced=False, language=None, chart=True, strings=None):
    page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce" if reduced else "no-preference")
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(STUB, view)
    if language == "ru":
        # `strings`: the Russian of more keys than the card's own, for a test that reads another word of the widget
        page.evaluate("(words) => { const a = JSON.parse(window.__i18n); a.language = 'ru'; a.revision = 4; a.strings = words; window.__i18n = JSON.stringify(a); }", strings or RU)
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    if chart:
        page.wait_for_selector(".tl-block .chart-svg")
    _settle(page)
    page.mouse.move(2, 2)
    return page


def _settle(page):
    page.wait_for_function(SETTLED, timeout=6000)
    page.evaluate(STILL)


def _measure(page):
    page.evaluate(STILL)
    return page.evaluate(MEASURE)


def _poll(page):
    page.evaluate("() => window.__poll.forEach((fn) => fn())")
    page.wait_for_timeout(300)
    _settle(page)


def _press_bar(page, account):
    page.click('[data-focus="%s"]' % bar(account))
    page.mouse.move(2, 2)
    _settle(page)


def _near(value, want, tolerance, what):
    assert abs(value - want) <= tolerance, "%s: %s, wanted about %s" % (what, value, want)


def _raised(page):
    page.evaluate(STILL)
    return page.evaluate(RAISED)


def _assert_raised(up, rest, what):
    """The card stands in the place of the chart: the chart is a strip over it, the card has the chart's width, four corners, and the widget is as tall as at rest."""
    assert up["up"] and up["stripShown"], (what, "the card is up and the chart's strip shows", up)
    assert up["stripText"].startswith("Chart") and "\u00b7" in up["stripText"], (what, "the strip says 'Chart · <window>'", up["stripText"])
    _near(up["strip"]["h"], 52, 1.5, what + ": the chart's strip is as tall as the card's own")
    _near(up["wrap"]["h"], 52, 1.5, what + ": the chart folds into the strip, not to nothing")
    _near(up["card"]["l"], up["wrap"]["l"], 1.5, what + ": the card has the chart's place (left)")
    _near(up["card"]["r"], up["wrap"]["r"], 1.5, what + ": the card has the chart's place (right)")
    assert up["card"]["t"] >= up["wrap"]["b"] - 1.5, (what, "the card no longer goes under the plate", up["card"], up["wrap"])
    assert up["card"]["t"] < rest["card"]["t"] - 100, (what, "the card rose to the place of the chart", up["card"], rest["card"])
    assert len(set(up["radii"])) == 1 and up["radii"][0] != "0px", (what, "all four corners are its own", up["radii"])
    _near(up["docH"], rest["docH"], 2, what + ": the height of the widget is one")


# --- the card under the plate: the width, the 16 under, the layer ---------------------------------

@pytest.mark.parametrize("width", [1120, 800, 640])
def test_wide_the_card_is_16_narrower_on_each_side_and_goes_16_under_the_plate(browser, base_view, width):
    page = _open(browser, base_view, width=width)
    m = _measure(page)
    assert m["inPlate"] and m["blocks"] == 1, m
    _near(m["blk"]["b"] - m["card"]["t"], 16, 1.5, "16px of the card go under the chart's plate")
    _near(m["card"]["l"] - m["blk"]["l"], 16, 1.5, "16px narrower at the left")
    _near(m["blk"]["r"] - m["card"]["r"], 16, 1.5, "16px narrower at the right")
    assert m["chartOver"], "the chart's plate is over the card at its top edge"
    assert not m["overflow"], m
    page.close()


def test_the_plate_stays_over_the_card_whatever_the_card_does(browser, base_view):
    page = _open(browser, base_view)
    rest = _raised(page)
    for step in ("rest", "hover", "pressed", "shut"):
        if step == "hover":
            page.hover(TOGGLE)
        elif step == "pressed":
            page.mouse.move(2, 2)
            page.click(TOGGLE)
            page.mouse.move(2, 2)
        elif step == "shut":
            page.click(TOGGLE)
            page.mouse.move(2, 2)
        m = _measure(page)
        if step in ("rest", "shut"):
            _near(m["blk"]["b"] - m["card"]["t"], 16, 1.5, step + ": the 16 under the plate")
            assert m["chartOver"], step
        else:
            # rule: raised (under the pointer or pressed) the card no longer goes under the plate, it stands in the place of the chart
            _assert_raised(_raised(page), rest, step)
    page.close()


# --- rest 52, the pointer about 96, the press the whole card ----------------------------------

def test_rest_is_the_strip_52_the_pointer_raises_the_card_in_the_place_of_the_chart_and_a_press_pins_it(browser, base_view):
    page = _open(browser, base_view)
    rest = _measure(page)
    rest_up = _raised(page)
    _near(rest["top"]["h"], 52, 1, "at rest the strip stands 52 under the plate")
    _near(rest["card"]["b"] - rest["blk"]["b"], 52, 1.5, "and nothing more of the card is seen below the plate")
    assert (rest["peek"] or {"h": 0})["h"] < 1 and rest["more"]["h"] < 1, rest
    assert rest["clip"] == "hidden" and not page.locator(CLEAR).is_visible(), ("the rest of the card is hidden and Clear is out of reach", rest)
    assert rest["lead"] == "48%" and rest["leadShown"], "the lead window stands at the right of the strip"
    page.hover(TOGGLE)
    page.wait_for_timeout(120)
    hovered = _measure(page)
    # rule: the pointer on the strip raises the whole card in the place of the chart (it was: a row of windows, about 96, opened under the strip)
    assert hovered["open"] and hovered["exp"] == "true", hovered
    _near(hovered["top"]["h"], 52, 1, "no row of windows under the strip any more: the strip stays 52")
    assert (hovered["peek"] or {"h": 0})["h"] < 1 and hovered["peekWords"] == [], hovered
    _assert_raised(_raised(page), rest_up, "hover")
    page.mouse.move(2, 2)
    page.evaluate(STILL)
    _near(_measure(page)["top"]["h"], 52, 1, "the pointer away: the strip again")
    assert not _measure(page)["open"], "the pointer away puts the unpinned card down"
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    pressed = _measure(page)
    assert pressed["open"] and pressed["exp"] == "true", pressed
    _near(pressed["top"]["h"], 52, 1, "pressed: the strip stays 52")
    assert pressed["clip"] == "visible" and not page.locator(CLEAR).is_visible(), ("rule: Clear is not drawn in the plate's card", pressed)
    assert pressed["more"]["h"] > 100 and pressed["pad"]["h"] > 100, ("the whole card", pressed)
    assert pressed["pad"]["b"] <= pressed["card"]["b"] + 1 and pressed["pad"]["t"] >= pressed["card"]["t"], ("nothing of the card is cut", pressed)
    _near(pressed["card"]["b"], pressed["more"]["b"], 1.5, "the card ends where its rest ends")
    _assert_raised(_raised(page), rest_up, "pressed")
    page.close()


def test_the_keyboard_raises_the_card_like_the_pointer(browser, base_view):
    page = _open(browser, base_view)
    rest_up = _raised(page)
    page.focus(TOGGLE)
    page.wait_for_timeout(120)
    # rule: the focus on the strip raises the card in the place of the chart (it was: the row of windows, about 96)
    m = _measure(page)
    assert m["open"], ("the focus on the strip raises the card", m)
    _near(m["top"]["h"], 52, 1, "no row of windows under the strip")
    _assert_raised(_raised(page), rest_up, "focus")
    page.close()


FOCUS_NOW = """() => {
  const a = document.activeElement, s = a ? getComputedStyle(a) : null, r = a ? a.getBoundingClientRect() : null;
  return { tag: a && a.tagName, focus: a && a.getAttribute && a.getAttribute('data-focus'), cls: a && String(a.className),
           shown: !!a && a !== document.body && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0 };
}"""
STRIP_BUTTON = ".wt-plate > .tl-wrap > .tl-strip"


def _keyboard_on_the_strip(page):
    """The card pinned (the strip shows), then the keyboard comes on the strip: a key is pressed last, so the focus the script gives is the keyboard's (:focus-visible)."""
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    page.wait_for_selector(".inspector.kt-open", timeout=3000)
    page.evaluate(STILL)
    assert _raised(page)["stripShown"], "the strip of the folded chart shows"
    page.keyboard.press("Shift")
    # One focus() of the script (page.focus() of Playwright gives the node the focus twice, and the second one would stand for the reader's)
    page.evaluate("(sel) => document.querySelector(sel).focus()", STRIP_BUTTON)
    if not page.evaluate("(sel) => document.querySelector(sel).matches(':focus-visible')", STRIP_BUTTON):
        pytest.skip("this engine does not take the focus of a script for the keyboard's (:focus-visible), which is all the strip answers to")


def test_the_keyboard_that_lowers_the_card_from_the_strip_stays_on_the_picked_bar(browser, base_view):
    # The strip hides as the card goes down and the chart opens; the focus it held must not fall to the page.
    page = _open(browser, base_view)
    want = page.evaluate("() => document.querySelector('.bars .bar.sel').getAttribute('data-focus')")
    _keyboard_on_the_strip(page)
    page.wait_for_selector(".inspector:not(.kt-open)", timeout=3000)
    _settle(page)
    page.wait_for_timeout(400)
    now = page.evaluate(FOCUS_NOW)
    assert now["tag"] != "BODY" and now["shown"], ("the focus is not lost to the page", now)
    assert now["focus"] == want, ("the focus stands on the picked bar", now, want)
    assert not page.locator(STRIP_BUTTON).is_visible(), "the strip is gone, the chart is open"
    page.close()


def test_the_keyboard_that_lowers_the_card_from_the_strip_stays_on_the_plate_where_no_bar_is_picked(browser, base_view):
    page = _open(browser, base_view)
    page.evaluate("() => document.querySelector('.bars .bar.sel').classList.remove('sel')")
    _keyboard_on_the_strip(page)
    page.wait_for_selector(".inspector:not(.kt-open)", timeout=3000)
    _settle(page)
    page.wait_for_timeout(400)
    now = page.evaluate(FOCUS_NOW)
    assert now["shown"] and "wt-plate" in now["cls"], ("the focus stands on the plate", now)
    # The plate is focusable only for this moment: it is a plain box again once the focus goes on.
    page.keyboard.press("Tab")
    assert not page.evaluate("() => document.querySelector('.wt-plate').hasAttribute('tabindex')"), "the plate is not left in the tab order"
    page.close()


# --- aria-expanded, Enter, Space, a second press -----------------------------------------------

def test_aria_expanded_and_the_keys_enter_and_space_open_and_fold_the_card(browser, base_view):
    page = _open(browser, base_view)
    handle = page.locator(TOGGLE)
    blur = "() => document.activeElement.blur()"
    assert handle.evaluate("(n) => n.tagName") == "BUTTON" and handle.get_attribute("type") == "button"
    assert handle.get_attribute("aria-label") == "Details of c2" and handle.get_attribute("aria-expanded") == "false"
    for key in ("Enter", "Space"):
        page.focus(TOGGLE)
        page.keyboard.press(key)
        page.wait_for_selector(".inspector.kt-open")
        assert handle.get_attribute("aria-expanded") == "true", key
        assert page.evaluate("() => document.activeElement.getAttribute('data-focus')") == "inspector-toggle", "the keyboard stays on the strip"
        m = _measure(page)
        assert m["pad"]["h"] > 100 and m["clip"] == "visible", (key, m)
        # rule: the focus alone raises the card now, so the pin is told apart by the focus let go: a pinned card stays up
        page.evaluate(blur)
        page.wait_for_timeout(300)
        assert page.locator(".inspector.kt-open").count() == 1, (key, "the press pins the card: the focus let go does not put it down")
        page.focus(TOGGLE)
        page.keyboard.press(key)
        page.evaluate(blur)
        page.wait_for_selector(".inspector:not(.kt-open)")
        assert handle.get_attribute("aria-expanded") == "false", key
        m = _measure(page)
        assert m["more"]["h"] < 1 and m["clip"] == "hidden", (key, "the second press let the pin go, the card folds once the focus is gone", m)
    page.close()


def test_a_second_press_folds_the_card_back_to_the_strip(browser, base_view):
    page = _open(browser, base_view)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    pressed = _measure(page)
    assert pressed["sumBlock"] > 40, ("the summary (the number and the notes) stands first in the pressed card", pressed["sumBlock"])
    _near(pressed["more"]["h"] - pressed["sumBlock"], 128, 40, "pressed: the rest of the card, the summary taken off")
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    m = _measure(page)
    assert not m["open"] and m["exp"] == "false", m
    _near(m["card"]["b"] - m["blk"]["b"], 52, 1.5, "the strip again")
    # rule: Clear is not drawn in the plate's card (display: none), so it is asked whether it is seen, not what its visibility says (it was: visibility hidden at rest)
    assert not page.locator(CLEAR).is_visible(), m
    page.close()


# --- the outline and the stem of the pick stand through the card ---------------

# The outline is the ::after of the chart's wrap (the chart at rest, the strip it folds into under the card); the stem is the plate's .bar-arrow; the layer of the line (.plate-trace) is not drawn for a card.
OUTLINE = """() => {
  const q = (s) => document.querySelector(s);
  const wrap = q('.wt-plate > .tl-wrap'), blk = q('.wt-plate .tl-block'), strip = q('.wt-plate > .tl-wrap > .tl-strip');
  const w = wrap.getBoundingClientRect(), b = blk.getBoundingClientRect(), s = strip.getBoundingClientRect(), a = getComputedStyle(wrap, '::after');
  const arrow = q('.wt-plate > .bar-arrow'), picked = q('.bars > .sel');
  const ar = arrow && arrow.getBoundingClientRect(), pr = picked && picked.getBoundingClientRect();
  return { content: a.content, opacity: parseFloat(a.opacity), width: parseFloat(a.width), height: parseFloat(a.height), shadow: a.boxShadow, wrap: [w.width, w.height], block: [b.width, b.height], strip: [s.width, s.height],
    traced: blk.classList.contains('tl-traced'), arrowOn: !!arrow && arrow.classList.contains('on'), arrows: document.querySelectorAll('.bar-arrow').length, live: document.querySelectorAll('.plate-trace').length,
    stemIn: !!(ar && pr) && ar.left + ar.width / 2 >= pr.left - 1 && ar.left + ar.width / 2 <= pr.right + 1, up: !!q('.inspector.kt-open') };
}"""
# Every frame: the outline's opacity (0 where the pseudo-element is not there), the frames with no stem, the layers of the line drawn.
OUTLINE_WATCH = """() => {
  const line = window.__line = { minAfter: 1, noStem: 0, layers: 0, frames: 0, stop: false };
  new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes) if (n.nodeType === 1 && (n.matches('.plate-trace') || n.querySelector('.plate-trace'))) line.layers++; }).observe(document.body, { childList: true, subtree: true });
  const tick = () => {
    const w = document.querySelector('.wt-plate > .tl-wrap');
    if (w) {
      const a = getComputedStyle(w, '::after'), arrow = document.querySelector('.wt-plate > .bar-arrow');
      line.minAfter = Math.min(line.minAfter, a.content === 'none' ? 0 : parseFloat(a.opacity));
      if (!arrow || !arrow.classList.contains('on')) line.noStem++;
      line.frames++;
    }
    if (!line.stop) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}"""


def _outline(page):
    page.evaluate(STILL)
    return page.evaluate(OUTLINE)


def _assert_outline(o, size, what):
    """The outline stands (a ring of the accent, opacity 1) on the rect of the chart's wrap, which is `size` (the chart's block at rest, the strip under the card); the stem stands under the picked bar; no layer of the line."""
    assert o["content"] not in ("none", "normal") and o["opacity"] == 1 and "inset" in o["shadow"], (what, "the outline stands", o)
    assert o["traced"] and o["live"] == 0, (what, "the plate wears the line and no layer of it is drawn", o)
    _near(o["width"], size[0], 1.5, what + ": the outline is as wide as the chart's wrap")
    _near(o["height"], size[1], 1.5, what + ": the outline is as tall as the chart's wrap")
    _near(o["wrap"][1], size[1], 1.5, what + ": the wrap is the rect wanted")
    assert o["arrows"] == 1 and o["arrowOn"] and o["stemIn"], (what, "the stem stands under the picked bar", o)


def test_the_outline_and_the_stem_stand_through_the_card_and_fold_with_the_chart(browser, base_view):
    """rule: the line of the pick no longer goes back into the bar as the card goes up: the chart's outline folds with the chart into the strip (a ring on the strip,
    its rect the strip's), the stem stays under the bar, no layer of the line (.plate-trace) is drawn; the card down, the outline is the chart's again."""
    page = _open(browser, base_view)
    rest = _outline(page)
    _assert_outline(rest, rest["block"], "at rest")
    assert not rest["up"], rest
    page.evaluate(OUTLINE_WATCH)
    page.hover(TOGGLE)
    page.wait_for_timeout(120)
    hovered = _outline(page)
    assert hovered["up"], hovered
    _assert_outline(hovered, hovered["strip"], "under the pointer")
    page.mouse.move(2, 2)
    page.wait_for_timeout(120)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    pinned = _outline(page)
    assert pinned["up"], pinned
    _assert_outline(pinned, pinned["strip"], "pinned")
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    down = _outline(page)
    assert not down["up"], down
    _assert_outline(down, down["block"], "the card down again")
    line = page.evaluate("() => { window.__line.stop = true; return window.__line; }")
    assert line["frames"] > 10, ("the frames were watched", line)
    assert line["minAfter"] == 1, ("the outline never leaves while the chart folds and opens", line)
    assert line["noStem"] == 0 and line["layers"] == 0, ("the stem stands in every frame and no line is drawn for the card", line)
    page.close()


def test_the_outline_and_the_stem_come_to_another_bar_with_the_card_pinned(browser, base_view):
    """rule: the card pinned, another bar picked: the old outline and stem go (they stood through the card), the line is drawn at the new bar and the plate wears it again."""
    page = _open(browser, base_view)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    pinned = _outline(page)
    _assert_outline(pinned, pinned["strip"], "pinned")
    _press_bar(page, C1)
    o = _outline(page)
    assert not o["up"], o
    _assert_outline(o, o["block"], "the new bar")
    page.close()


# --- the opening lives through the poll and the bar, the window and the family drop it ------------

def test_the_opening_lives_through_the_poll_and_another_bar_puts_the_card_down(browser, base_view):
    page = _open(browser, base_view)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    _settle(page)
    _poll(page)
    m = _measure(page)
    assert m["open"] and m["exp"] == "true" and m["pad"]["h"] > 100, ("the poll keeps the card as it stood", m)
    _press_bar(page, C1)
    m = _measure(page)
    assert m["label"] == "Details of c1", "another bar, another card"
    # rule: another bar lets the pin go (it was: the opening lived through the change of the bar): the new card stands in its strip, the chart is under the new bar
    assert not m["open"] and m["exp"] == "false" and m["more"]["h"] < 1 and m["clip"] == "hidden", ("another bar: the card of the new bar is at rest", m)
    _near(m["card"]["b"] - m["blk"]["b"], 52, 1.5, "the strip of the new card under the chart")
    assert m["blocks"] == 1 and not page.locator(".wt-plate .tl-strip").is_visible(), "the chart stands open under the new bar"
    _poll(page)
    assert not _measure(page)["open"], "and the poll does not raise it"
    page.close()


def test_a_change_of_the_limit_drops_the_opening(browser, base_view):
    page = _open(browser, base_view)
    other = [g for g in base_view["reserve"]["summary"]["groups"] if g["key"] != GROUP and any(b.get("account") for b in g["bars"])][0]
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    _settle(page)
    assert _measure(page)["open"]
    page.click('[data-focus="scope:%s"]' % other["key"])
    page.mouse.move(2, 2)
    _settle(page)
    page.wait_for_selector(".inspector")
    m = _measure(page)
    assert not m["open"] and m["exp"] == "false", ("another limit: the card is at rest", m)
    page.close()


def test_a_change_of_the_window_and_of_the_family_drops_the_opening(browser, served_views):
    windows = served_views["windows"]
    first, second = [g for g in windows["reserve"]["summary"]["groups"] if any(b.get("account") for b in g["bars"])][:2]
    assert first["window_seconds"] != second["window_seconds"]
    page = _open(browser, windows)
    page.click('[data-focus="win:%s:%s"]' % (first["harness"], first["window_seconds"]))
    scope = page.locator('[data-focus="scope:%s"]' % first["key"])
    if scope.count():
        scope.click()
    page.mouse.move(2, 2)
    _settle(page)
    page.wait_for_selector(".inspector")
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    _settle(page)
    assert _measure(page)["open"], "pressed in the first window"
    _poll(page)
    assert _measure(page)["open"], "the poll keeps it"
    page.click('[data-focus="win:%s:%s"]' % (second["harness"], second["window_seconds"]))
    page.mouse.move(2, 2)
    _settle(page)
    page.wait_for_selector(".inspector")
    m = _measure(page)
    assert not m["open"] and m["exp"] == "false", ("another window: the card is at rest", m)
    page.close()
    # the family: the answer holds two families, the head shows both
    both = served_views["both"]
    page = _open(browser, both)
    page.wait_for_selector(".inspector")
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    _settle(page)
    assert _measure(page)["open"], "pressed in the first family"
    heads = page.evaluate("() => [...document.querySelectorAll('[data-focus^=\"harness:\"]')].map((n) => n.getAttribute('data-focus'))")
    assert len(heads) == 2, heads
    current = page.evaluate("() => { const n = document.querySelector('[data-focus^=\"harness:\"][aria-selected=\"true\"], [data-focus^=\"harness:\"][aria-pressed=\"true\"], [data-focus^=\"harness:\"][aria-current]'); return n && n.getAttribute('data-focus'); }")
    target = [h for h in heads if h != current][0]
    page.click('[data-focus="%s"]' % target)
    page.mouse.move(2, 2)
    page.wait_for_timeout(500)
    _settle(page)
    page.wait_for_selector(".inspector")
    m = _measure(page)
    assert not m["open"] and m["exp"] == "false", ("another family: the card is at rest", m)
    page.close()


# --- without a chart the card is an ordinary one ----------------------------------------------------

def test_without_the_chart_the_card_is_an_ordinary_whole_card(browser, base_view):
    page = _open(browser, base_view)
    # rule: the chart word of a card that is up lowers the card and brings the chart back (it no longer folds the chart), so the chart is folded from the plate's own word with the card at rest
    assert not page.locator('[data-focus="limit:%s"]' % GROUP).is_visible(), "folded, the card hides the chart word it carries"
    _fold_chart(page)
    m = _measure(page)
    assert m["blocks"] == 0 and not m["inPlate"], m
    assert not m["toggleShown"] and not m["leadShown"], ("no strip to press, no lead window", m)
    assert m["clip"] == "visible" and m["clearVis"] == "visible", ("the whole card and Clear within reach", m)
    assert m["pad"]["h"] > 100 and (m["peek"] or {"h": 0})["h"] < 1, m
    assert page.evaluate("() => document.querySelectorAll('.kt-sum, .kt-pair, .kt-mini, .kt-lim, .kt-rule').length") == 0, "out of the plate the card takes nothing from it"
    assert m["wrapMargin"] == "0px", ("not tucked under anything", m)
    assert not m["overflow"], m
    page.hover("body")
    page.hover('.inspector')
    page.wait_for_timeout(200)
    again = _measure(page)
    _near(again["card"]["h"], m["card"]["h"], 1, "the pointer adds no row to a whole card")
    page.close()


def test_a_row_of_the_account_list_picks_its_account_and_the_card_stands_in_the_plate_like_after_a_bar(browser, base_view):
    # rule: a row of the list is pressed like the bar of its account - the list shuts, the account is picked, its chart stands under the bar and its card is the plate's own card (the strip at rest, the number and the notes in its summary);
    # it was: the list took the plate's place and the card of a picked row stood out of the plate as an ordinary whole card
    page = _open(browser, base_view)
    page.click('[data-focus="accounts"]')
    page.wait_for_function("() => { const f = document.querySelector('.acc-listfold'); return !!f && f.getBoundingClientRect().height > 50 && !document.querySelector('.fold-run, .fold-shut'); }", timeout=6000)
    page.click('[data-focus="acct:codex:c1"]')
    page.mouse.move(2, 2)
    page.wait_for_function("() => { const f = document.querySelector('.acc-listfold'); return !f || f.getBoundingClientRect().height < 1; }", timeout=6000)
    _settle(page)
    assert page.locator('[data-focus="accounts"]').get_attribute("aria-pressed") == "false", "the list is shut"
    assert page.evaluate("() => document.querySelector('.bars > .sel').getAttribute('data-focus')") == bar("codex:c1"), "the account of the row is the picked one (its bar)"
    m = _measure(page)
    assert m["inPlate"] and m["toggleShown"] and m["leadShown"] and m["blocks"] == 1, ("the card is the plate's own, the chart stands under the bar", m)
    assert not m["open"] and m["exp"] == "false", ("the card is at rest: a strip under the chart", m)
    assert "c1" in page.locator(".inspector").inner_text(), "the card is the card of the account of the row"
    w = page.evaluate(WHERE, KINDS)
    assert w["plates"] == 1 and w["cardInPlate"] and w["cards"] == 1 and w["sums"] == 1 and w["rules"] == 1, ("the card takes the number and the notes of the plate once", w)
    for kind in ("fact", "stamp", "btn"):
        assert w[kind] == {"all": 1, "inSum": 1, "inPlate": 0}, (kind, "the real node stands in the summary, once, and not in the plate", w[kind])
    for kind in ("limit", "nx"):
        assert w[kind]["inSum"] == w[kind]["all"] and w[kind]["inPlate"] == 0, (kind, "the notes of the account the row picked stand in the summary", w[kind])
    assert not m["overflow"], m
    page.close()


# --- Clear -----------------------------------------------------------------------------------------

def test_clear_is_not_shown_in_the_plate_card_and_works_where_the_card_stands_out_of_the_plate(browser, base_view):
    page = _open(browser, base_view)
    # rule: Clear is not drawn in the card of the plate, at rest or pressed (the pick is always held); it was: out of reach at rest, within reach pressed
    assert not page.locator(CLEAR).is_visible(), "at rest the Clear button is not seen"
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    _settle(page)
    assert not page.locator(CLEAR).is_visible(), "pressed, it is not drawn either"
    assert page.evaluate("() => getComputedStyle(document.querySelector('.insp-clear')).display") == "none"
    # rule: the card is let go (a second press) and the chart word of the plate folds the chart (in a card that is up the word lowers the card instead): the card stands out of the plate, whole, and Clear works there as it did
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    page.wait_for_selector(".inspector:not(.kt-open)", timeout=3000)
    _settle(page)
    _fold_chart(page)
    assert page.locator(CLEAR).is_visible() and page.locator(CLEAR).text_content().strip() == "Clear"
    page.click(CLEAR)
    page.wait_for_function("() => !document.querySelector('.inspector') && !document.querySelector('.fold-run, .fold-shut')", timeout=6000)
    state = page.evaluate("() => ({ sel: document.querySelectorAll('.bars > .sel').length, pressed: [...document.querySelectorAll('.bars > .bar')].filter((b) => b.getAttribute('aria-pressed') === 'true').length })")
    assert state == {"sel": 0, "pressed": 0}, state
    page.close()


# --- 520 and under -----------------------------------------------------------------------------------

@pytest.mark.parametrize("width", [520, 414, 360])
def test_narrow_the_card_is_not_narrowed_and_the_page_does_not_move_sideways(browser, base_view, width):
    page = _open(browser, base_view, width=width)
    rest_up = _raised(page)
    for step in ("rest", "hover", "pressed"):
        if step == "hover":
            page.hover(TOGGLE)
            page.wait_for_timeout(120)
        elif step == "pressed":
            page.mouse.move(2, 2)
            page.click(TOGGLE)
            page.mouse.move(2, 2)
        m = _measure(page)
        _near(m["card"]["l"] - m["blk"]["l"], 0, 1.5, "%s: as wide as the plate (left)" % step)
        _near(m["blk"]["r"] - m["card"]["r"], 0, 1.5, "%s: as wide as the plate (right)" % step)
        assert not m["overflow"], (step, m["scrollW"], m["innerW"])
        if step != "pressed":
            _near(m["card"]["t"] - m["blk"]["b"], m["rowGap"], 1.5, "%s: under the plate, not tucked: the gap the plate keeps between its rows" % step)
        if step == "rest":
            _near(m["top"]["h"], 52, 1, "rest: the strip is 52")
        if step == "hover":
            # rule: at 520 and under there is no hover (a finger presses only): the pointer on the strip raises nothing
            assert not m["open"], (step, "no hover at 520 and under", m)
        if step == "pressed":
            assert m["pad"]["h"] > 100 and m["clip"] == "visible", m
            _assert_raised(_raised(page), rest_up, "%s: pressed" % width)
    page.close()


@pytest.mark.parametrize("width", [520, 360, 320])
def test_narrow_the_name_is_not_broken_by_letters_and_the_windows_are_not_torn(browser, base_view, width):
    for name, long in (("c2", False), ("Pro seat 2 of the studio", False), ("a-very-long-account-name-without-any-space-at-all-that-never-fits@example.com", True)):
        page = _open(browser, _rename(base_view, "c2", name), width=width)
        page.hover(TOGGLE)
        page.wait_for_timeout(150)
        page.evaluate(STILL)
        m = page.evaluate(NAME)
        assert m["wrap"] == "normal" and m["space"] == "nowrap", ("the name is one word of the strip", m)
        assert m["lines"] == 1 and m["h"] <= m["line"] * 1.6, ("one line, never broken by letters", name, m)
        if long:
            assert m["ellipsis"] == "ellipsis" and m["scroll"] > m["client"], ("only the name longer than its room is cut, with an ellipsis", m)
        assert m["card"] <= m["blk"] + 1.5, ("the card is no wider than the plate", m)
        assert all(p["lines"] == 1 for p in m["pieces"]), ("a window of the row is never torn", m["pieces"])
        assert not page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1"), name
        page.mouse.move(2, 2)
        page.click(TOGGLE)
        page.mouse.move(2, 2)
        page.evaluate(STILL)
        assert not page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1"), (name, "pressed")
        page.close()


@pytest.mark.parametrize("width", [520, 414, 375, 360, 320])
def test_narrow_the_name_keeps_the_width_of_its_own_text_whatever_stands_after_it(browser, base_view, width):
    # The title of the strip (the name, the plan, the next window) wraps on a narrow screen as it does outside the plate: what does not fit goes to a line of its own, and the name is never
    # squeezed by the words after it (it was cut to a letter or two at 375).
    for name in ("c2", "agy1"):
        page = _open(browser, _rename(base_view, "c2", name), width=width)
        m = page.evaluate(NAME)
        assert page.evaluate("() => getComputedStyle(document.querySelector('.kt-top .insp-title')).flexWrap") == "wrap", "the title wraps on a narrow screen"
        assert m["w"] > 10 and m["scroll"] <= m["client"] + 1, ("the name is whole, not cut", name, m)
        assert m["lines"] == 1, ("one line", name, m)
        page.close()


@pytest.mark.parametrize("width", [1120, 800, 521])
def test_wide_the_title_of_the_strip_keeps_its_one_line(browser, base_view, width):
    page = _open(browser, base_view, width=width)
    assert page.evaluate("() => getComputedStyle(document.querySelector('.kt-top .insp-title')).flexWrap") == "nowrap", "from 521 up the strip keeps one line"
    page.close()


# --- the number and the notes inside the card (cardTake / cardGive) ----------------------------

KINDS = {"fact": ".wt-fact", "stamp": ".wt-stamp", "btn": ".l-chart", "limit": ".wt-limit", "nx": ".nx"}
GONE = "() => document.querySelectorAll('.kt-sum, .kt-pair, .kt-mini, .kt-lim, .kt-rule').length"

# Where the five nodes stand: in the plate (all), in the summary of the card (inSum), a direct child of the plate (inPlate); and the pieces the card makes for them.
WHERE = r"""(kinds) => {
  const plate = document.querySelector('.wt-plate'), sum = document.querySelector('.kt-sum');
  const out = { plates: document.querySelectorAll('.wt-plate').length, sums: document.querySelectorAll('.kt-sum').length, pairs: document.querySelectorAll('.kt-pair').length,
    minis: document.querySelectorAll('.kt-mini').length, lims: document.querySelectorAll('.kt-lim').length, rules: document.querySelectorAll('.kt-rule').length,
    cards: document.querySelectorAll('.inspector').length, cardInPlate: !!document.querySelector('.wt-plate > .ins-wrap > .ins-fold > .inspector'),
    say: !!document.querySelector('.wt-plate > #quotas-win-say.sr-only') };
  for (const [kind, sel] of Object.entries(kinds)) {
    const all = plate ? [...plate.querySelectorAll(sel)] : [];
    out[kind] = { all: all.length, inSum: all.filter((n) => sum && sum.contains(n)).length, inPlate: all.filter((n) => n.parentNode === plate).length };
  }
  return out;
}"""

# The children of the plate in their order, each by the first of its classes that the markup of limitRow names (the bars, the folds and the line of the pick are not part of it).
SIGNATURE = r"""() => {
  const order = ['wt-fact', 'wt-scope', 'wt-stamp', 'l-chart', 'sr-only', 'wt-limit', 'lg', 'nx'];
  return [...document.querySelector('.wt-plate').children]
    .filter((n) => !/\b(bars|tl-top|tl-wrap|ins-wrap|plate-trace)\b/.test(String(n.getAttribute('class') || '')))
    .map((n) => order.find((c) => n.classList.contains(c)) || 'other');
}"""

STRIP = r"""() => {
  const R = (s) => { const n = document.querySelector(s); if (!n) return null; const r = n.getBoundingClientRect(); return { l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width, h: r.height }; };
  return { card: R('.inspector'), name: R('.kt-top .insp-name'), plan: R('.kt-top .plan-chip'), lead: R('.kt-top .kt-lead'), mini: R('.kt-top .kt-mini'),
    fact: R('.kt-sum .wt-fact'), stamp: R('.kt-sum .wt-stamp'), btn: R('.kt-sum .l-chart'), side: R('.kt-sum .kt-side'), sum: R('.kt-sum'),
    overflow: document.documentElement.scrollWidth > window.innerWidth + 1, scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth };
}"""


def _apart(a, b, what, gap=0.5):
    assert a and b, (what, "a part is missing", a, b)
    assert a["r"] <= b["l"] + gap or b["r"] <= a["l"] + gap or a["b"] <= b["t"] + gap or b["b"] <= a["t"] + gap, "%s overlap: %s and %s" % (what, a, b)


def _within(a, card, what, gap=1.0):
    assert a["l"] >= card["l"] - gap and a["r"] <= card["r"] + gap, "%s leaves the card: %s, card %s" % (what, a, card)


FOLD_CHART = "(sel) => document.querySelector(sel).click()"


def _fold_chart(page):
    """The chart folded from the plate's own word (the card at rest: the word stands in the folded summary, so it is clicked in the page, not by a pointer)."""
    page.evaluate(FOLD_CHART, '[data-focus="limit:%s"]' % GROUP)
    page.mouse.move(2, 2)
    page.wait_for_function("() => !document.querySelector('.tl-block') && !document.querySelector('.fold-run, .fold-shut')", timeout=6000)
    page.evaluate(STILL)


def _press_open(page):
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    _settle(page)


def test_the_number_and_the_notes_stand_in_the_card_once_and_the_plate_has_none(browser, base_view):
    page = _open(browser, base_view)
    w = page.evaluate(WHERE, KINDS)
    assert w["plates"] == 1 and w["cardInPlate"] and w["cards"] == 1 and w["sums"] == 1 and w["rules"] == 1, w
    for kind in ("fact", "stamp", "btn"):
        assert w[kind] == {"all": 1, "inSum": 1, "inPlate": 0}, (kind, "the real node stands in the summary, once, and not in the plate", w[kind])
    for kind in ("limit", "nx"):
        assert w[kind]["all"] <= 1 and w[kind]["inSum"] == w[kind]["all"] and w[kind]["inPlate"] == 0, (kind, w[kind])
    assert w["say"], "the spoken summary stays in the plate, so a reader hears the number with the card at rest"
    # (the .sr-only inside the moved stamp - the dot's spoken sign in .wt-sep - belongs to the real node; only the plate's own spoken summary, or a bare .sr-only in the summary's own rows, is foreign)
    kept = page.evaluate("""() => ({ foreign: !!document.querySelector('.kt-sum .wt-scope, .kt-sum .credits, .kt-sum #quotas-win-say, .kt-sum > .sr-only, .kt-sum .kt-row > .sr-only, .kt-sum .kt-side > .sr-only'),
      first: document.querySelector('.kt-pad').firstElementChild.className, second: document.querySelector('.kt-pad').children[1].className,
      row: [...document.querySelector('.kt-sum .kt-row').children].map((n) => n.className.split(' ')[0]),
      side: [...document.querySelector('.kt-sum .kt-side').children].map((n) => n.className.split(' ').find((c) => c === 'wt-stamp' || c === 'l-chart')),
      describedby: document.querySelector('.kt-sum .l-chart').getAttribute('aria-describedby') })""")
    # assumption: the widget carries the legend (.lg) with the notes into the summary (widget.js CARD_CLASS legend), so it is no longer foreign there; the scope, the credits row and the spoken summary stay
    assert not kept["foreign"], ("the scope, the credits and the spoken summary stay in the plate", kept)
    assert kept["first"] == "kt-sum" and kept["second"] == "kt-rule", ("the summary first in the card, a rule under it", kept)
    assert kept["row"] == ["wt-fact", "kt-side"] and kept["side"] == ["wt-stamp", "l-chart"], ("the fact on the left, the stamp and the chart word on the right", kept)
    assert kept["describedby"] == "quotas-win-say" and page.locator("#quotas-win-say").count() == 1, kept
    page.close()


@pytest.mark.parametrize("language", [None, "ru"])
def test_the_strip_wears_a_copy_of_the_number_and_the_row_of_windows_a_copy_of_the_note(browser, base_view, language):
    page = _open(browser, base_view, language=language)
    info = page.evaluate(r"""() => {
      const mini = document.querySelector('.kt-top .kt-pair .kt-mini'), fact = document.querySelector('.kt-sum .wt-fact');
      const lim = document.querySelector('.kt-top .kt-peek .kt-lim'), head = document.querySelector('.kt-sum .wt-limit .wt-limit-head');
      return { mini: !!mini, hidden: mini && mini.getAttribute('aria-hidden'), parts: mini ? [...mini.children].map((n) => n.textContent) : null,
        factNum: fact && fact.querySelector('.wt-num').textContent, of: fact && fact.getAttribute('data-of'), copiedFact: !!(mini && mini.querySelector('.wt-fact, .wt-stamp, button')),
        lead: !!document.querySelector('.kt-pair > .kt-lead'), pairs: document.querySelectorAll('.kt-pair').length, minis: document.querySelectorAll('.kt-mini').length,
        lim: !!lim, limHidden: lim && lim.getAttribute('aria-hidden'), limText: lim && lim.textContent.trim(), headText: head && head.textContent.trim(),
        lims: document.querySelectorAll('.kt-lim').length, limActions: lim ? lim.querySelectorAll('button, a, [data-focus]').length : 0 };
    }""")
    assert info["mini"] and info["hidden"] == "true", ("the copy of the number is aria-hidden: the reader hears the number once", info)
    count = int(info["of"])
    assert count > 0, info
    word = "из %d" % count if language == "ru" else "of %d" % count
    assert info["parts"] == [info["factNum"], word], ("the copy is the number and the word from acc.strip_of, not the caption parsed", info)
    assert not info["copiedFact"] and info["lead"] and info["pairs"] == 1 and info["minis"] == 1, ("the copy is the number alone, in the pair with the share", info)
    # rule: the row of windows under the pointer (.kt-peek, with the copy of the note's head, .kt-lim) is gone; the window's notes stand in the card's own .kt-sum
    assert not info["lim"] and info["lims"] == 0 and info["limActions"] == 0, ("no copy of the note's head in the strip any more", info)
    page.close()


def test_the_copies_show_at_rest_and_under_the_pointer_and_the_pair_melts_when_pressed(browser, base_view):
    page = _open(browser, base_view, reduced=True)
    state = lambda: page.evaluate(r"""() => { const o = (s) => { const n = document.querySelector(s); return n ? getComputedStyle(n) : null; };
      const clear = o('.kt-right .insp-clear'), card = document.querySelector('.inspector');
      return { mini: o('.kt-mini').opacity, miniTransition: o('.kt-mini').transitionDuration, up: card.classList.contains('kt-open'),
        clearShown: !!clear && clear.display !== 'none' && clear.visibility !== 'hidden' && clear.opacity !== '0', hasRow: !!document.querySelector('.kt-peek, .kt-lim') }; }""")
    rest = state()
    assert rest["mini"] == "1" and not rest["clearShown"] and not rest["up"], ("at rest the strip carries the number, Clear is out of reach", rest)
    assert rest["miniTransition"] in ("0s", "0s, 0s"), ("reduced motion: the copy has no transition", rest)
    assert not rest["hasRow"], ("rule: no row of windows (.kt-peek) in the strip", rest)
    page.hover(TOGGLE)
    page.wait_for_timeout(120)
    hover = state()
    # rule: the pointer on the strip raises the whole card in the place of the chart (it was: a row of windows opened under the strip); the copy of the number melts into the card's own
    assert hover["up"] and hover["mini"] == "0" and not hover["hasRow"], ("under the pointer the card is up and the copy of the number has melted into it", hover)
    page.mouse.move(2, 2)
    page.wait_for_timeout(400)
    # (not _press_open: its wait wants the chart traced, and the chart stands folded under the pressed card; the card's own stillness is what is waited for here)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    page.evaluate(STILL)
    pressed = state()
    assert pressed["up"] and pressed["mini"] == "0", ("pressed: the pair melts into the card", pressed)
    assert not pressed["clearShown"], ("rule: Clear is not shown in the plate's card, pressed or not", pressed)
    page.close()


def test_clear_gives_the_nodes_back_to_the_plate_in_the_order_of_the_markup(browser, base_view):
    page = _open(browser, base_view)
    held = page.evaluate(WHERE, KINDS)
    _press_open(page)
    # rule: Clear is not drawn in the plate's card; the pick is let go through the button's own handler (it stays in the markup, out of sight)
    page.evaluate("() => document.querySelector('.insp-clear').click()")
    page.wait_for_function("() => !document.querySelector('.inspector') && !document.querySelector('.fold-run, .fold-shut')", timeout=6000)
    given = page.evaluate(WHERE, KINDS)
    assert given["cards"] == 0 and given["sums"] == 0 and given["pairs"] == 0 and given["minis"] == 0 and given["lims"] == 0 and given["rules"] == 0, given
    for kind in KINDS:
        assert given[kind] == {"all": held[kind]["all"], "inSum": 0, "inPlate": held[kind]["all"]}, (kind, "back in the plate, none lost, none doubled", given[kind])
    order = page.evaluate(SIGNATURE)
    assert order[:1] == ["wt-fact"] and order.index("wt-stamp") < order.index("l-chart") < order.index("sr-only"), order
    assert [c for c in order if c in ("wt-limit", "nx")] == [c for c in ("wt-limit", "nx") if held[{"wt-limit": "limit", "nx": "nx"}[c]]["all"]], order
    assert order == sorted(order, key=["wt-fact", "wt-scope", "wt-stamp", "l-chart", "sr-only", "wt-limit", "other", "lg", "nx"].index), ("the order of the markup", order)
    _poll(page)  # a drawing from the markup itself (limitRow) puts them in just this order
    assert page.evaluate(SIGNATURE) == order, "what was given back stands where a fresh drawing puts it"
    assert page.evaluate(GONE) == 0
    page.close()


def test_another_bar_and_the_poll_with_the_card_open_do_not_double_the_nodes(browser, base_view):
    page = _open(browser, base_view)
    _press_open(page)
    held = page.evaluate(WHERE, KINDS)
    mini = page.evaluate("() => document.querySelector('.kt-mini').textContent")
    assert held["sums"] == 1 and held["minis"] == 1, held
    for step in ("the poll", "another bar", "the poll after it", "back to the first bar"):
        if step == "another bar":
            _press_bar(page, C1)
            assert _measure(page)["label"] == "Details of c1"
        elif step.startswith("the poll"):
            _poll(page)
        else:
            _press_bar(page, C2)
            assert _measure(page)["label"] == "Details of c2"
        now = page.evaluate(WHERE, KINDS)
        assert now == held, (step, "the nodes of the old card went to the new one, none doubled and none left", now, held)
        assert page.evaluate("(key) => document.querySelectorAll('[data-focus=\"' + key + '\"]').length", "limit:" + GROUP) == 1, step
        assert page.evaluate("() => document.querySelector('.kt-mini').textContent") == mini, step
        m = _measure(page)
        # rule: the poll keeps the card as it stood; another bar puts the card down (it was: the opening lived through the change of the bar)
        if step == "the poll":
            assert m["open"] and m["exp"] == "true" and m["pad"]["h"] > 100, (step, "the poll keeps the card open", m)
        else:
            assert not m["open"] and m["exp"] == "false", (step, "another bar put the card down", m)
    page.close()


def test_the_chart_word_pressed_in_the_card_lowers_the_card_and_brings_the_chart_back(browser, base_view):
    # rule: the word "Chart" in the card that is up lowers the card and gives the chart back (it was: the word folded the chart and the card stood out of the plate)
    page = _open(browser, base_view)
    word = '[data-focus="limit:%s"]' % GROUP
    assert not page.locator(word).is_visible(), "the card is folded: the word it carries is out of reach"
    held = page.evaluate(WHERE, KINDS)
    rest = _raised(page)
    _press_open(page)
    assert page.locator(word).is_visible() and page.locator(word).get_attribute("aria-pressed") == "true"
    assert _raised(page)["up"], "the card is up before the word is pressed"
    page.click(word)
    page.mouse.move(2, 2)
    page.wait_for_selector(".inspector:not(.kt-open)", timeout=3000)
    _settle(page)
    back = page.evaluate(WHERE, KINDS)
    assert back == held and back["cardInPlate"] and back["sums"] == 1, ("the card went down, the nodes stay in its summary, once", back, held)
    down = _raised(page)
    assert not down["up"] and not page.locator(".wt-plate .tl-strip").is_visible(), ("the strip is gone: the chart is open again", down)
    _near(down["docH"], rest["docH"], 2, "the height of the widget is one")
    assert page.locator(".tl-block .chart-svg").count() == 1 and page.locator(word).get_attribute("aria-pressed") == "true", "the chart is shown"
    m = _measure(page)
    assert not m["open"] and m["exp"] == "false" and m["blocks"] == 1, ("the card is at rest under the chart", m)
    page.close()


def test_the_chart_folded_from_the_plate_gives_the_nodes_back_and_the_chart_word_returns_them_with_the_chart(browser, base_view):
    page = _open(browser, base_view)
    word = '[data-focus="limit:%s"]' % GROUP
    held = page.evaluate(WHERE, KINDS)
    _fold_chart(page)
    out = page.evaluate(WHERE, KINDS)
    assert not out["cardInPlate"] and out["cards"] == 1 and out["sums"] == 0 and out["pairs"] == 0 and out["minis"] == 0 and out["lims"] == 0 and out["rules"] == 0, ("no chart: the card is out of the plate and takes nothing", out)
    for kind in KINDS:
        assert out[kind] == {"all": held[kind]["all"], "inSum": 0, "inPlate": held[kind]["all"]}, (kind, "the plate keeps them as it did before the card", out[kind])
    assert page.locator(word).is_visible() and page.locator(word).get_attribute("aria-pressed") == "false", "the chart word stays on the plate, in reach, not pressed"
    assert page.evaluate("(sel) => document.querySelector(sel).parentNode.classList.contains('wt-plate')", word)
    page.click(word)
    page.mouse.move(2, 2)
    page.wait_for_selector(".tl-block .chart-svg")
    _settle(page)
    back = page.evaluate(WHERE, KINDS)
    assert back == {**held, "cardInPlate": True} and back["sums"] == 1, ("the chart is back and the card takes the nodes again, once", back, held)
    assert page.locator(word).get_attribute("aria-pressed") == "true"
    page.close()


def test_the_budget_plate_and_a_card_out_of_the_plate_move_nothing(browser, tmp_path_factory):
    from test_window_tabs import _cursor_views
    views = _cursor_views(tmp_path_factory.mktemp("card-cursor"))
    for key in ("cursor_budget", "cursor_mixed"):
        page = _open(browser, views[key], chart=False)
        plate = page.evaluate("() => { const p = document.querySelector('.wt-plate-cycle'); return !!p && [...p.children].filter((n) => n.classList.contains('wt-fact')).length; }")
        assert plate == 1, (key, "the budget plate carries its own figure")
        before = page.evaluate("() => [...document.querySelector('.wt-plate-cycle').children].map((n) => n.className)")
        page.locator(".wt-plate-cycle .bars .bar").first.click()
        page.mouse.move(2, 2)
        page.wait_for_timeout(600)
        page.evaluate(STILL)
        assert page.evaluate(GONE) == 0, (key, "the plate of the cycle gives nothing to a card")
        after = page.evaluate("() => [...document.querySelector('.wt-plate-cycle').children].filter((n) => !n.classList.contains('ins-wrap')).map((n) => n.className)")
        assert [c for c in before if 'ins-wrap' not in c] == after, (key, "the plate stands as it did", before, after)
        page.close()


DETAILS = '[data-focus="inspector-details"]'
def _rest(page):
    # a plate with no chart has nothing the settle of the chart waits for: a beat for the redraw, then the motion is brought to its end
    page.wait_for_timeout(500)
    page.evaluate(STILL)


CARD_H = "() => Math.round(document.querySelector('.inspector').getBoundingClientRect().height)"


@pytest.mark.parametrize("width", [1000, 360])
def test_the_card_with_no_chart_is_a_strip_and_the_rest_waits_under_one_button(browser, tmp_path_factory, width):
    # rule: an account with no chart over it (Cursor: only its budget per billing cycle) has a strip - the head, its share left, its money and its next reset - and the resources, the readings and the credential
    # under "Account details", shut at the start; it was: 443px (1000) and 627px (360) of lines with no fold
    from test_window_tabs import _cursor_views
    views = _cursor_views(tmp_path_factory.mktemp("card-plain"))
    page = _open(browser, views["cursor_budget"], width=width, chart=False)
    page.locator(".wt-plate-cycle .bars .bar").first.click()
    page.mouse.move(2, 2)
    page.wait_for_selector(".inspector")
    _rest(page)
    assert page.evaluate(CARD_H) <= 140, ("the card at rest is a strip", page.evaluate(CARD_H))
    assert page.evaluate("() => document.querySelectorAll('.inspector .account-resources, .inspector .insp-diag-body').length") == 0, "nothing of the rest is drawn while it is shut"
    strip = page.locator(".inspector .insp-sum").inner_text()
    assert "$11.80" in strip and "$20.00" in strip and "resets" in strip, ("the money and the reset stand in the strip", strip)
    assert page.locator(DETAILS).get_attribute("aria-expanded") == "false"
    page.click(DETAILS)
    _rest(page)
    assert page.locator(DETAILS).get_attribute("aria-expanded") == "true"
    text = page.locator(".inspector").inner_text()
    assert "Balances" in text and "Spending" in text and "Reset offers" in text, ("the rest opens in place", text)
    assert page.evaluate(CARD_H) > 200
    assert not page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1")
    page.close()


def test_the_details_of_the_card_with_no_chart_live_through_the_poll_and_are_let_go_by_another_account_and_another_family(browser, tmp_path_factory, base_view):
    # rule: the opened details belong to the account they were opened on: another account, another family or a pick made for the reader starts them shut; the poll keeps them
    from test_window_tabs import _cursor_views
    cursor = _cursor_views(tmp_path_factory.mktemp("card-plain-reset"))["cursor_budget"]
    view = copy.deepcopy(base_view)
    view["groups"] = view["groups"] + copy.deepcopy(cursor["groups"])
    page = _open(browser, view, width=1000, chart=False)
    page.click('[data-focus^="harness:cursor"]')
    _rest(page)
    page.locator(".wt-plate-cycle .bars .bar").first.click()
    page.mouse.move(2, 2)
    _rest(page)
    page.click(DETAILS)
    _rest(page)
    shown = "() => !!document.querySelector('.insp-diag-body')"
    assert page.evaluate(shown)
    page.click('[data-focus="refresh"]')
    _rest(page)
    assert page.evaluate(shown), "a read of the same account keeps the details open"
    page.locator(".wt-plate-cycle .bars .bar").nth(1).click()
    page.mouse.move(2, 2)
    _rest(page)
    assert not page.evaluate(shown), "another account starts shut"
    page.click(DETAILS)
    _rest(page)
    page.click('[data-focus^="harness:codex"]')
    _rest(page)
    page.click('[data-focus^="harness:cursor"]')
    _rest(page)
    page.locator(".wt-plate-cycle .bars .bar").nth(1).click()
    page.mouse.move(2, 2)
    _rest(page)
    assert not page.evaluate(shown), "another family lets the details go"
    page.close()


def test_narrow_the_dot_and_the_long_name_stand_in_one_line_with_the_whole_name_in_its_title(browser, tmp_path_factory):
    # rule: the dot and the name are one pair (.insp-who): on a narrow line the plan and the next-up mark wrap after it, never the dot off the name; the name whole is its title
    from test_window_tabs import _cursor_views
    view = _rename(_cursor_views(tmp_path_factory.mktemp("card-long"))["cursor_budget"], "cursor1", "cursor1-a-very-long-account-name-without-any-space-that-never-fits@example.com")
    page = _open(browser, view, width=360, chart=False)
    page.locator(".wt-plate-cycle .bars .bar").first.click()
    page.mouse.move(2, 2)
    _rest(page)
    m = page.evaluate("""() => { const d = document.querySelector('.inspector .state-dot').getBoundingClientRect(), n = document.querySelector('.inspector .insp-name').getBoundingClientRect(),
        t = document.querySelector('.inspector .insp-name').title;
        return { dy: Math.abs((d.top + d.height / 2) - (n.top + n.height / 2)), title: t, fits: document.querySelector('.inspector').scrollWidth <= document.querySelector('.inspector').clientWidth + 1 }; }""")
    assert m["dy"] < 6, ("the dot is on the line of the name", m)
    assert m["fits"], m
    assert m["title"].startswith("cursor1-a-very-long") and m["title"].endswith("@example.com"), m
    page.close()


def _ru_words():
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    return {key[len("code:quotas.ui."):]: ({"forms": e["forms"]} if "forms" in e else e["text"]) for key, e in memory.items()}


def test_in_russian_the_about_panel_the_resources_and_the_spoken_count_are_russian_and_the_count_has_its_form(browser, tmp_path_factory):
    # rule: the system lines and the legend of "About", the names and the phrases of the resources block and the sentence a reader of a screen hears ("N current of M accounts") are words of the table, with the
    # plural forms where a number stands in them (the words of the Cursor card); the labels the engine names its rows with are taken from the table where they are known
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    from test_window_tabs import _cursor_views
    words = _ru_words()
    base = _widget_fixture(tmp_path_factory.mktemp("card-ru-count"))["view"]
    page = _open(browser, base, language="ru", strings=words)
    page.click('[data-focus="about"]')
    page.wait_for_selector(".about-panel")
    about = page.locator(".about-panel").inner_text()
    for english in ("What the daemon answered", "daemon not reported", "catalog not requested", "Windows left", "Never added", "share left"):
        assert english not in about, (english, about[:400])
    assert "Система" in about and "Не складывается" in about
    spoken = page.evaluate("() => document.querySelector('#quotas-win-say').textContent")
    assert "current of" not in spoken and "с актуальным чтением: " in spoken, spoken
    page.close()
    view = _cursor_views(tmp_path_factory.mktemp("card-ru-cursor"))["cursor_budget"]
    page = _open(browser, view, language="ru", strings=words, chart=False)
    page.locator(".wt-plate-cycle .bars .bar").first.click()
    page.mouse.move(2, 2)
    _rest(page)
    page.click(DETAILS)
    _rest(page)
    card = page.locator(".inspector").inner_text()
    for english in ("Balances", "Spending", "Reset offers", "Diagnostics", "observed", "not reported", "Personal included", "fee treatment not stated"):
        assert english not in card, (english, card)
    assert "Балансы" in card and "Траты" in card and "Личный остаток в тарифе" in card, card
    page.close()


@pytest.mark.parametrize("width", [520, 414, 360, 320])
def test_narrow_the_strip_and_the_summary_do_not_overlap_and_the_page_does_not_move_sideways(browser, base_view, width):
    for name in ("c2", "Pro seat 2 of the studio", "a-very-long-account-name-without-any-space-at-all-that-never-fits@example.com"):
        page = _open(browser, _rename(base_view, "c2", name), width=width)
        m = page.evaluate(STRIP)
        card = m["card"]
        strip = [("name", m["name"]), ("plan", m["plan"]), ("share", m["lead"]), ("number", m["mini"])]
        assert m["mini"] and m["lead"] and m["name"], (width, name, m)
        for i, (a_name, a) in enumerate(strip):
            if a:
                _within(a, card, "%s at %d, rest: %s" % (name[:12], width, a_name))
                for b_name, b in strip[i + 1:]:
                    if b:
                        _apart(a, b, "%s at %d, rest: %s and %s" % (name[:12], width, a_name, b_name))
        assert not m["overflow"], (name, width, "rest", m["scrollW"], m["innerW"])
        page.mouse.move(2, 2)
        page.click(TOGGLE)
        page.mouse.move(2, 2)
        _settle(page)
        m = page.evaluate(STRIP)
        card = m["card"]
        assert m["fact"] and m["stamp"] and m["btn"], (name, width, m)
        parts = [("number", m["fact"]), ("stamp", m["stamp"]), ("chart word", m["btn"])]
        for i, (a_name, a) in enumerate(parts):
            _within(a, card, "%s at %d, pressed: %s" % (name[:12], width, a_name))
            for b_name, b in parts[i + 1:]:
                _apart(a, b, "%s at %d, pressed: %s and %s" % (name[:12], width, a_name, b_name))
        assert m["side"]["r"] <= card["r"] + 1 and m["sum"]["r"] <= card["r"] + 1, (name, width, "the summary stays in the card", m)
        assert not m["overflow"], (name, width, "pressed", m["scrollW"], m["innerW"])
        page.close()


# --- the words, en and ru ---------------------------------------------------------------------------------

def test_the_words_of_the_card_in_english(browser, base_view):
    page = _open(browser, base_view)
    m = _measure(page)
    assert m["label"] == "Details of c2" and m["cardLabel"] == "Selected account", m
    # rule: no row of windows (the "peek" with 5h / Week) under the pointer any more; the strip keeps its share and its number
    assert m["peekWords"] == [] and m["leadTitle"].endswith("48%"), m
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    m = _measure(page)
    assert m["clearText"].strip() == "Clear" and m["clearLabel"] == "Clear the selected account", m
    page.close()


def test_the_words_of_the_card_in_russian(browser, base_view):
    page = _open(browser, base_view, language="ru")
    m = _measure(page)
    assert m["label"] == "Подробности аккаунта c2" and m["cardLabel"] == "Выбранный аккаунт", m
    assert m["peekWords"] == [], ("rule: no row of windows (Нед) under the pointer any more", m["peekWords"])
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    m = _measure(page)
    assert m["clearText"].strip() == "Снять выбор" and m["clearLabel"] == "Снять выбор аккаунта", m
    # rule: Clear is not shown in the plate's card (the word stays in the markup, out of sight); it was: visible once the card was pressed
    assert not page.locator(CLEAR).is_visible()
    assert not m["overflow"], m
    page.close()


@pytest.mark.parametrize("width", [360, 320])
def test_the_russian_card_does_not_move_the_page_sideways_at_narrow(browser, base_view, width):
    page = _open(browser, base_view, width=width, language="ru")
    for step in ("rest", "hover", "pressed"):
        if step == "hover":
            page.hover(TOGGLE)
            page.wait_for_timeout(150)
        elif step == "pressed":
            page.mouse.move(2, 2)
            page.click(TOGGLE)
            page.mouse.move(2, 2)
        m = _measure(page)
        assert not m["overflow"], (step, m["scrollW"], m["innerW"])
        _near(m["card"]["l"] - m["blk"]["l"], 0, 1.5, step)
    page.close()


# --- reduced motion: the end state at once -------------------------------------------------------------

def test_reduced_motion_the_press_is_in_its_end_state_at_once(browser, base_view):
    page = _open(browser, base_view, reduced=True)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    page.wait_for_timeout(60)
    m = page.evaluate(MEASURE)
    assert m["open"] and m["pad"]["h"] > 100 and m["clip"] == "visible", ("no motion to wait for", m)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    # the pointer's leave keeps its own beat (cardHold, --dur-2) before the card goes down; the end state must stand at once when it does, with no fold left to run
    page.wait_for_selector(".inspector:not(.kt-open)", timeout=2000)
    m = page.evaluate(MEASURE)
    assert not m["open"] and m["more"]["h"] < 1 and m["clip"] == "hidden", m
    assert page.evaluate("() => document.querySelectorAll('.fold-run, .fold-shut').length") == 0, "no motion to wait for"
    page.close()


# --- the keyboard and the ring on the card and its chart, the dots of its lines, the narrowest screens -------------------------------------------

CHART_WORD = '[data-focus^="limit:"]'
CHART_TABLE = '[data-focus="chart-table"]'


@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_the_keyboard_on_the_chart_word_of_the_pinned_card_is_not_left_on_the_page_when_the_card_lets_go(browser, base_view, key):
    # rule: the word hides as the card goes down and the chart comes: a keyboard that is on it goes to the picked bar (a drawing anew puts it back by its key); it was: the page itself
    page = _open(browser, base_view)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    page.wait_for_selector(".inspector.kt-open", timeout=3000)
    page.evaluate(STILL)
    page.focus(CHART_WORD)
    assert page.evaluate("() => document.activeElement.getAttribute('data-focus')").startswith("limit:")
    page.keyboard.press(key)
    page.wait_for_selector(".inspector:not(.kt-open)", timeout=3000)
    _settle(page)
    page.wait_for_timeout(400)
    now = page.evaluate(FOCUS_NOW)
    assert now["tag"] != "BODY" and now["shown"], ("the focus is not lost to the page", now)
    page.close()


def test_the_keyboard_that_goes_back_from_the_card_in_the_first_beat_is_not_lost_with_the_folding_chart(browser, base_view):
    # rule: the chart's own fold takes the focus for a beat (--dur-5) after the card rises and hides under it; the focus that comes on the chart then gives the chart back as the strip does; it was: the page
    page = _open(browser, base_view)
    page.focus(TOGGLE)
    page.keyboard.press("Shift")
    page.wait_for_selector(".inspector.kt-open", timeout=3000)
    # the beat in which the strip has not shown yet and the fold is still there to take the focus
    page.evaluate("""() => new Promise((done) => { setTimeout(() => { document.querySelector('[data-focus="chart-table"]').focus(); done(); }, 100); })""")
    page.wait_for_timeout(900)
    now = page.evaluate(FOCUS_NOW)
    assert now["tag"] != "BODY" and now["shown"], ("the focus stands on the chart, not on the page", now)
    assert page.evaluate("() => !document.querySelector('.inspector').classList.contains('kt-open')"), "the chart is given back"
    page.close()


def test_the_ring_of_the_summary_of_the_details_is_drawn_inside_its_box_for_the_box_of_the_details_clips_what_is_outside(browser, base_view):
    # rule: .chart-table clips on the sides (overflow-x), the summary is as wide as it: its ring is drawn inside it (the card's own toggle does the same); it was: outside, cut away
    page = _open(browser, base_view)
    page.keyboard.press("Shift")
    page.evaluate("(sel) => document.querySelector(sel).focus()", CHART_TABLE)
    m = page.evaluate("""(sel) => { const s = document.querySelector(sel), c = getComputedStyle(s), p = getComputedStyle(s.parentElement);
        return { visible: s.matches(':focus-visible'), style: c.outlineStyle, width: parseFloat(c.outlineWidth), offset: parseFloat(c.outlineOffset), clips: p.overflowX !== 'visible' }; }""", CHART_TABLE)
    assert m["visible"] and m["style"] != "none" and m["clips"], m
    assert m["offset"] + m["width"] <= 0, ("the ring lies inside the box", m)
    page.close()


@pytest.mark.parametrize("width", [320, 414, 1000])
def test_the_dot_between_the_items_of_the_summary_and_of_the_meta_line_ends_the_item_before_it_and_never_begins_a_line(browser, tmp_path_factory, width):
    # rule: the dot is drawn at the end of the item before it (not at the start of the one after), so a wrapped line never begins with it; the last item has none; it was: the first of the next line
    from test_window_tabs import _cursor_views
    page = _open(browser, _cursor_views(tmp_path_factory.mktemp("card-dots"))["cursor_budget"], width=width, chart=False)
    page.locator(".wt-plate-cycle .bars .bar").first.click()
    page.mouse.move(2, 2)
    _rest(page)
    page.click(DETAILS)
    _rest(page)
    lines = page.evaluate("""() => [...document.querySelectorAll('.insp-sum, .insp-meta')].map((line) => {
        const items = [...line.children].filter((n) => n.tagName === 'SPAN'), has = (n, w) => getComputedStyle(n, w).content !== 'none';
        return { cls: line.className, n: items.length, before: items.filter((n) => has(n, '::before')).length, after: items.map((n) => has(n, '::after')),
                 rows: new Set(items.map((n) => Math.round(n.getBoundingClientRect().top))).size }; })""")
    assert any(line["cls"] == "insp-sum" for line in lines) and any("insp-meta" in line["cls"] for line in lines), lines
    for line in lines:
        assert line["before"] == 0 and line["after"] == [True] * (line["n"] - 1) + [False] * min(line["n"], 1), line
    if width == 320:
        assert any(line["rows"] > 1 for line in lines), ("a line that wraps is in the test", lines)
    page.close()


@pytest.mark.parametrize("width", [240, 250, 260, 270])
def test_on_the_narrowest_screens_nothing_of_the_budget_plate_the_about_panel_or_the_card_goes_beyond_the_screen(browser, tmp_path_factory, width):
    # rule: the frame of 320 gives the widget 240: the line of the next reset (nowrap) wraps there, the unit of a three-digit sum stands under its figure, the grid of About is never wider than its panel; it was: 5 to 17px of scrolling
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    from test_window_tabs import _cursor_family, _cursor_resources
    now_res = lambda pid, now: _cursor_resources(pid, now, included=(16400, 40000, 23600), on_demand=(320, 5000, 4680))
    view = _cursor_family(tmp_path_factory.mktemp("card-narrow"), "cursor_big", [("cursor1", now_res), ("cursor2", now_res), ("cursor3", now_res)])
    page = _open(browser, view, width=width, chart=False, language="ru", strings=_ru_words())
    _rest(page)
    cuts = """() => ({ scroll: document.documentElement.scrollWidth, width: document.documentElement.clientWidth,
        out: [...document.querySelectorAll('#root *')].filter((n) => n.children.length === 0 && n.textContent.trim() && n.getBoundingClientRect().width > 0 && !n.closest('.bars')
            && n.getBoundingClientRect().right > document.documentElement.clientWidth + 0.5).map((n) => n.className + ':' + n.textContent.slice(0, 24)) })"""
    seen = page.evaluate(cuts)
    assert seen["scroll"] <= seen["width"] + 1 and not seen["out"], ("the plate", seen)
    page.click('[data-focus="about"]')
    page.wait_for_selector(".about-panel")
    _rest(page)
    seen = page.evaluate(cuts)
    assert seen["scroll"] <= seen["width"] + 1 and not seen["out"], ("About", seen)
    page.close()


def test_a_chart_is_drawn_anew_to_the_width_the_window_takes_and_the_pick_stays(browser, base_view):
    # rule: the plot is drawn to the card of the moment (its viewBox); a window that changes its width draws it to the new one without a poll (once in a beat while the width changes),
    # the picked bar and the open card stay; it was: the width of the first drawing until the poll (the picture only scaled)
    VB = "() => document.querySelector('.chart-svg').viewBox.baseVal.width"
    page = _open(browser, base_view, width=1120)
    wide = page.evaluate(VB)
    page.click(TOGGLE)
    page.mouse.move(2, 2)
    page.wait_for_selector(".inspector.kt-open", timeout=3000)
    want = page.evaluate("() => document.querySelector('.bars .bar.sel').getAttribute('data-focus')")
    page.set_viewport_size({"width": 560, "height": 1000})
    page.wait_for_function("(w) => document.querySelector('.chart-svg') && document.querySelector('.chart-svg').viewBox.baseVal.width < w - 100", arg=wide, timeout=3000)
    narrow = page.evaluate(VB)
    assert narrow <= 560, ("the plot is drawn to the narrow card", narrow, wide)
    assert page.evaluate("() => document.querySelector('.bars .bar.sel').getAttribute('data-focus')") == want, "the pick stands"
    assert page.evaluate("() => document.querySelector('.inspector').classList.contains('kt-open')"), "the card stays up"
    page.set_viewport_size({"width": 1120, "height": 1000})
    page.wait_for_function("(w) => document.querySelector('.chart-svg').viewBox.baseVal.width > w + 100", arg=narrow, timeout=3000)
    page.close()


def test_in_russian_the_readings_of_the_about_panel_are_russian(browser, tmp_path_factory):
    # rule: the paragraph "Readings" of About (when the provider was read, the record kept, the watch, the zone) is words of the table, not English left in a Russian panel
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    base = _widget_fixture(tmp_path_factory.mktemp("card-ru-readings"))["view"]
    page = _open(browser, base, language="ru", strings=_ru_words())
    page.click('[data-focus="about"]')
    page.wait_for_selector(".about-panel")
    items = page.evaluate("() => [...document.querySelectorAll('.about-item')].map((n) => n.innerText)")
    readings = [t for t in items if t.startswith("Чтения")]
    assert readings, items
    for english in ("provider readings", "status read", "times in", "Recorded since", "watched without", "Nothing recorded"):
        assert english not in readings[0], (english, readings[0])
    assert "Записано с" in readings[0] and "время" in readings[0], readings[0]
    page.close()


@pytest.mark.parametrize("answer", [(500, ""), (404, ""), (200, "{}"), (200, '{"language": "", "revision": 0, "en": {}, "strings": {}}')])
def test_the_numbers_are_drawn_whatever_the_text_route_answers_when_the_reading_is_whole(browser, base_view, answer):
    # rule: the numbers are never held back for the words: a text route that gave no table has the reading ask for the table (the plugin answers it with the view); it was: the one card "could not be loaded", no number
    status, body = answer
    page = browser.new_page(viewport={"width": 1120, "height": 1000})
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(STUB, base_view)
    page.evaluate("""([status, body]) => { const table = window.__i18n, orig = window.fetch;
        window.fetch = (url, options) => {
          if (/\\/i18n/.test(url)) return Promise.resolve({ ok: status < 400, status, text: async () => body });
          if (/[?&]text=1/.test(url)) return orig(url, options).then(async (r) => { const v = JSON.parse(await r.text()); v.text = JSON.parse(table);
            return { ok: true, status: 200, text: async () => JSON.stringify(v) }; });
          return orig(url, options); }; }""", [status, body])
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector(".wt-plate", timeout=6000)
    text = page.locator("#root").inner_text()
    assert "could not be loaded" not in text and "Retry" not in text, text[:200]
    assert page.locator(".bars .bar").count() > 0, text[:300]
    page.close()
