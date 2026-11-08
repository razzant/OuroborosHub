"""The list of accounts in the place of the plate, one height.

What is held here (real browsers: Chromium, and WebKit where Playwright has it; the widget and its stylesheet run unmodified, only the transport is stubbed):
- the word "Accounts · N" heads the bars of the plate, a text button with aria-pressed; there is no lower block "Accounts" under the plate: no .acc-toggle, no
  button.cover-link, no .rows-menu there;
- pressed, the plate folds shut and the list stands in its place (same top, same height); its head carries Rows (details.rows-menu) and the line of coverage
  (.acc-cover); the head of the columns stands and the body of the table alone scrolls; a row of the list picks its account and shuts the list; pressed again,
  the plate is back with the same pick;
- the height of the document is one at rest and with the list open, on 1280 and on 360 (1px); under reduced motion no transition runs;
- an unlimited account's cell says so in the reader's language.
"""
import copy

import pytest

import json
from pathlib import Path

from test_reserve import _widget_fixture, with_i18n

WIDGET = Path(__file__).with_name("widget.js")
STRINGS = json.loads(Path(__file__).with_name("strings_en.json").read_text(encoding="utf-8"))
GROUP = "codex|primary|604800|-"
C1, C2 = "codex:c1", "codex:c2"


def bar(account, key=GROUP):
    return "bar:%s:%s" % (key, account)


@pytest.fixture(scope="module")
def base_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("accounts"))["view"]


STUB = with_i18n(r"""
(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true } : view) });
  };
}
""")
# The plate and the list stand still: no fold of the list running.
SETTLED = "() => !document.querySelector('.acc-fold.acc-run')"
STILL = """() => new Promise((done) => { let n = 0; const tick = () => (++n >= 6 ? done(true) : requestAnimationFrame(tick)); requestAnimationFrame(tick); })"""


def _open(browser, view, width=1120, height=1000, reduced=False, language=None, strings=None):
    page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce" if reduced else "no-preference")
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(STUB, view)
    if language == "ru":
        page.evaluate("(words) => { const a = JSON.parse(window.__i18n); a.language = 'ru'; a.revision = 4; a.strings = words; window.__i18n = JSON.stringify(a); }", strings or {})
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    page.wait_for_selector(".tl-block .chart-svg")
    _settle(page)
    page.mouse.move(2, 2)
    return page


def _settle(page):
    page.wait_for_function(SETTLED, timeout=6000)
    page.evaluate(STILL)

ACC = '[data-focus="accounts"]'
HOVER_MEDIA = "(hover: hover) and (not (max-width: 520.98px))"
RECTS = """(sels) => Object.fromEntries(Object.entries(sels).map(([k, s]) => {
  const n = document.querySelector(s);
  if (!n) return [k, null];
  const r = n.getBoundingClientRect();
  return [k, { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height }];
}))"""
HEIGHTS = "() => ({ root: document.getElementById('root').getBoundingClientRect().height, body: document.body.scrollHeight })"


def _page(browser, view, width, reduced=False):
    return _open(browser, view, width=width, height=1400, reduced=reduced)


def _rects(page, **sels):
    return page.evaluate(RECTS, sels)


def _heights(page):
    return page.evaluate(HEIGHTS)


def _near(a, b, tol, what):
    assert abs(a - b) <= tol, "%s: %s against %s (tolerance %s)" % (what, a, b, tol)


def _can_hover(page):
    return page.evaluate("(q) => window.matchMedia(q).matches", HOVER_MEDIA)


def _list_calm(page):
    # The fold of the list is played in the next frame and ends by a timer (accRun, 450ms): wait for it to start, and to end.
    page.mouse.move(2, 2)
    page.wait_for_timeout(100)
    page.wait_for_function("() => !document.querySelector('.acc-fold.acc-run')", timeout=4000)
    page.wait_for_timeout(100)


def _press_word(page):
    page.click(ACC)
    _list_calm(page)


# --- the word over the bars, and the lower block that is gone ---------------------------------

@pytest.mark.parametrize("width", [1280, 360])
def test_the_word_accounts_stands_over_the_bars_on_the_right_and_nothing_stands_under_the_plate(browser, base_view, width):
    page = _page(browser, base_view, width)
    word = page.locator(ACC)
    assert word.count() == 1, "one word over the bars"
    assert word.evaluate("(n) => n.tagName") == "BUTTON"
    assert word.inner_text().strip() == STRINGS["acc.word"].replace("{n}", "3")
    assert word.get_attribute("aria-pressed") == "false"
    r = _rects(page, word=ACC, bars=".q-bars", plate=".q-plate")
    assert r["word"]["b"] <= r["bars"]["t"] + 0.5, "the word heads the bars: %r" % r
    assert r["plate"]["l"] <= r["word"]["l"] and r["word"]["r"] <= r["plate"]["r"], "the word stands in the plate: %r" % r
    # The lower block of the old capsule is gone: its toggle, the cover link, and the Rows menu (which moved into the head of the open list).
    for sel in (".acc-toggle", "button.cover-link", ".rows-menu", ".acc-cover"):
        assert page.locator(sel).count() == 0, "%s must not stand under the plate at rest" % sel
    page.close()


# --- the list in the place of the bars, the chart and the card ----------------------------------

@pytest.mark.parametrize("width", [1280, 360])
def test_pressed_the_word_folds_the_plate_and_the_list_stands_in_its_place(browser, base_view, width):
    page = _page(browser, base_view, width)
    before = _rects(page, main=".acc-main", plate=".q-plate")
    picked = page.evaluate("() => document.querySelector('.q-bars .sel').getAttribute('data-focus')")
    page.click(ACC)
    _list_calm(page)
    assert page.locator(ACC).get_attribute("aria-pressed") == "true"
    assert page.locator(ACC).inner_text().strip() == STRINGS["acc.word"].replace("{n}", "3"), "the word keeps its name while pressed"
    after = _rects(page, main=".acc-main", fold=".acc-listfold", head=".acc-list-head", plate=".q-plate")
    assert after["main"]["h"] < 1, "the plate and its history are folded shut: %r" % after
    assert page.evaluate("() => document.querySelector('.acc-main > .acc-in').inert === true"), "what is folded cannot be reached by the keyboard"
    # The list stands in the plate's place, under its word (the word heads the bars of the plate, and while the list is open it stands over the list).
    word = _rects(page, word=ACC)["word"]
    assert word["b"] <= after["fold"]["t"] + 0.5 and after["fold"]["t"] - before["main"]["t"] <= word["h"] + 12, (word, after, before)
    # The head of the list: Rows and the line of coverage; nothing of them stands under the plate.
    assert page.locator(".acc-list-head details.rows-menu summary").inner_text().strip() == STRINGS["rows.title"]
    assert page.locator(".acc-list-head .acc-cover").inner_text().strip() == STRINGS["cover.some"]
    assert page.locator(".rows-menu").count() == 1 and page.locator(".acc-cover").count() == 1
    assert page.locator(".acc-toggle").count() == 0 and page.locator(".cover-link").count() == 0
    below = page.evaluate("""(plateBottom) => [...document.querySelectorAll('.acc-toggle, .cover-link, .rows-menu, .acc-cover')]
        .filter((n) => n.getBoundingClientRect().top >= plateBottom - 0.5).length""", after["fold"]["b"])
    assert below == 0, "none of the lower block's parts stands under the list"
    assert page.locator(".acc-row").count() == 3
    # Pressed again: the plate is back, the pick is the same.
    page.click(ACC)
    _list_calm(page)
    _settle(page)
    assert page.locator(ACC).get_attribute("aria-pressed") == "false"
    back = _rects(page, main=".acc-main", fold=".acc-listfold")
    _near(back["main"]["h"], before["main"]["h"], 1.5, "the place of the bars is back")
    assert back["fold"]["h"] < 1
    assert page.evaluate("() => document.querySelector('.q-bars .sel').getAttribute('data-focus')") == picked
    page.close()


@pytest.mark.parametrize("width", [1280, 360])
def test_the_head_of_the_columns_stands_and_the_body_of_the_table_alone_scrolls(browser, base_view, width):
    page = _page(browser, base_view, width)
    page.click(ACC)
    _list_calm(page)
    heights = _heights(page)
    # The fixture has three accounts, which fit: rows are cloned to make the body longer than its place.
    page.evaluate("""() => { const t = document.querySelector('.acc-table'); const row = t.querySelector('.acc-row');
      for (let i = 0; i < 30; i++) t.appendChild(row.cloneNode(true)); }""")
    page.wait_for_timeout(100)
    table = page.evaluate("() => { const t = document.querySelector('.acc-table'); const s = getComputedStyle(t); return { sh: t.scrollHeight, ch: t.clientHeight, oy: s.overflowY }; }")
    assert table["oy"] in ("auto", "scroll") and table["sh"] > table["ch"] + 50, "the body of the table scrolls: %r" % table
    again = _heights(page)
    _near(again["root"], heights["root"], 1, "a longer table does not grow the widget")
    _near(again["body"], heights["body"], 1, "a longer table does not grow the document")
    before = _rects(page, head=".acc-list-head", cols=".acc-headrow", first=".acc-row")
    page.evaluate("() => { document.querySelector('.acc-table').scrollTop = 160; }")
    page.wait_for_timeout(100)
    after = _rects(page, head=".acc-list-head", cols=".acc-headrow", first=".acc-row")
    _near(after["head"]["t"], before["head"]["t"], 0.5, "the head of the list stands")
    _near(after["cols"]["t"], before["cols"]["t"], 0.5, "the head of the columns stands")
    assert after["first"]["t"] < before["first"]["t"] - 100, "the body moved: %r %r" % (before["first"], after["first"])
    assert not page.evaluate("() => document.querySelector('.acc-table').contains(document.querySelector('.acc-headrow'))"), "the head of the columns is not in the scrolling body"
    page.close()


@pytest.mark.parametrize("width", [1280, 360])
def test_a_row_of_the_list_picks_its_account_and_shuts_the_list(browser, base_view, width):
    page = _page(browser, base_view, width)
    assert page.evaluate("() => document.querySelector('.q-bars .sel').getAttribute('data-focus')") == bar(C2), "the fixture picks c2 first"
    page.click(ACC)
    _list_calm(page)
    page.locator(".acc-row", has_text="c1").click()
    _list_calm(page)
    _settle(page)
    assert page.locator(ACC).get_attribute("aria-pressed") == "false", "the list is shut"
    assert _rects(page, fold=".acc-listfold")["fold"]["h"] < 1
    assert page.evaluate("() => document.querySelector('.q-bars .sel').getAttribute('data-focus')") == bar(C1), "the account of the row is picked (its bar is the picked one)"
    assert _rects(page, wrap=".tl-wrap")["wrap"]["h"] > 150, "the history stands under the plate"
    assert "c1" in page.locator(".q-card").inner_text(), "its card stands beside the bars"
    page.close()


# --- one height ----------------------------------------------------------------------------------

@pytest.mark.parametrize("width", [1280, 360])
def test_the_document_has_one_height_at_rest_and_with_the_list_open(browser, base_view, width):
    page = _page(browser, base_view, width)
    rest = _heights(page)
    assert rest["body"] > 300, "the widget is drawn: %r" % rest
    page.click(ACC)
    _list_calm(page)
    listed = _heights(page)
    for name, got in (("with the list open", listed),):
        _near(got["root"], rest["root"], 1, "the root %s" % name)
        _near(got["body"], rest["body"], 1, "the document %s" % name)
    page.close()


# --- reduced motion --------------------------------------------------------------------------------

def test_under_reduced_motion_the_list_arrives_at_once_and_no_transition_runs(browser, base_view):
    page = _page(browser, base_view, 1280, reduced=True)
    running = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(document.getAnimations().filter((a) => a.constructor.name === 'CSSTransition').length))))"
    page.click(ACC)
    assert page.evaluate(running) == 0, "no transition runs when the list opens"
    assert _rects(page, main=".acc-main")["main"]["h"] < 1, "the list is already there two frames after the press"
    page.close()


# --- the cell of an account with no share ------------------------------------------------------------

def _unlimited(view):
    """The view with the second account of every limit read as unlimited (a pool with no share)."""
    view = copy.deepcopy(view)
    done = []
    for group in view["reserve"]["summary"]["groups"]:
        if len(group.get("bars") or []) > 1 and group["bars"][1].get("account"):
            group["bars"][1].update(state="unlimited", left=None, at_limit=False)
            done.append(group["bars"][1]["account"])
    assert done, "the fixture has no second account to read as unlimited"
    return view


@pytest.mark.parametrize("language", [None, "ru"])
def test_the_cell_of_an_unlimited_account_says_it_in_the_readers_language(browser, base_view, language):
    # The cell is the word of the table of texts (cycle.unlimited), not a literal; the line a screen reader gets stays as it was.
    words = {"cycle.unlimited": "без лимита"}
    page = _open(browser, _unlimited(base_view), width=1280, language=language, strings=words)
    page.click(ACC)
    page.mouse.move(2, 2)
    _list_calm(page)
    cells = page.evaluate("() => [...document.querySelectorAll('.acc-row .acc-cell')].filter((c) => /unlimited|без лимита/.test(c.textContent)).map((c) => ({ text: c.textContent, title: c.title }))")
    assert cells, "the list has a cell for the unlimited account"
    assert {c["text"] for c in cells} == {words["cycle.unlimited"] if language == "ru" else STRINGS["cycle.unlimited"]}, cells
    assert all("unlimited, no share" in c["title"] for c in cells), ("the spoken line is not this cell's word", cells)
    page.close()
