"""The list of accounts in the place of the chart, one height, and the account card that rises into the chart's place.

What is held here (real browsers: Chromium, and WebKit where Playwright has it; the widget and its stylesheet run unmodified, only the transport is stubbed):
- the word "Accounts · N" stands over the bars on the right, a text button with aria-pressed; there is no lower block "Accounts" under the plate: no .acc-toggle, no button.cover-link, no .rows-menu there;
- pressed, the bars, the chart and the card fold shut and the list stands in their place (same top, same height); its head carries Rows (details.rows-menu) and the line of coverage (.acc-cover);
  the head of the columns stands and the body of the table alone scrolls; a row of the list picks its account and shuts the list; pressed again, the bars and the chart are back;
- the card of the picked account: at rest a strip under the chart. The pointer on the strip (or the focus, or a press) raises it into the place of the chart (the width of the plate, rounded on
  all four sides) and the chart folds into the strip .tl-strip ("Chart · <window>", --kt-strip high); the pointer on that strip (or a press) gives the chart back; a press on the card's strip pins it
  (cardOpen: the pointer leaving does not lower it); another bar lets it go: the card is a strip again and the chart stands under the new bar. The rows of windows that used to slide down
  under the strip ("peek") are gone, "Clear" (.insp-clear) is not shown in the card of the plate;
- the height of the document is one at rest, with the list open and with the card raised, on 1280 and on 360 (1px);
- under reduced motion no transition runs.
Old rules that the rules above have replaced (and so are not held here any more): the peek of the windows sliding down (about 96px), the opening of the card kept through
a change of bar, the lower button "Accounts" with the cover link, "Clear" in the card of the plate.
"""
import copy

import pytest

from test_card import STILL, SETTLED, STRINGS, bar, base_view, C1, C2, _open, _settle  # noqa: F401 (fixtures are used by name)

ACC = '[data-focus="accounts"]'
TOGGLE = '[data-focus="inspector-toggle"]'
STRIP = ".tl-strip"
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


def _up(page):
    return page.evaluate("() => !!document.querySelector('.inspector.kt-open')")


def _calm_up(page):
    page.wait_for_function("() => !!document.querySelector('.inspector.kt-open')", timeout=4000)
    page.evaluate(STILL)


def _calm_down(page):
    page.wait_for_function("() => !document.querySelector('.inspector.kt-open')", timeout=4000)
    page.evaluate(STILL)
    page.wait_for_function(SETTLED, timeout=6000)


def _raise(page, pin=False):
    """Raise the card the way a reader of this screen does: the pointer on its strip (a wide screen with a mouse), or a press (a narrow one); `pin` presses it too."""
    if _can_hover(page):
        page.hover(TOGGLE)
        _calm_up(page)
        if pin:
            page.click(TOGGLE)
            page.mouse.move(2, 2)
            page.wait_for_timeout(400)
            _calm_up(page)
    else:
        page.click(TOGGLE)
        page.mouse.move(2, 2)
        _calm_up(page)


def _hover_only(browser, view, width=1280):
    page = _page(browser, view, width)
    if not _can_hover(page):
        pytest.skip("this engine has no hovering pointer in the headless mode (%s)" % HOVER_MEDIA)
    return page


# --- the word over the bars, and the lower block that is gone ---------------------------------

@pytest.mark.parametrize("width", [1280, 360])
def test_the_word_accounts_stands_over_the_bars_on_the_right_and_nothing_stands_under_the_plate(browser, base_view, width):
    page = _page(browser, base_view, width)
    word = page.locator(ACC)
    assert word.count() == 1, "one word over the bars"
    assert word.evaluate("(n) => n.tagName") == "BUTTON"
    assert word.inner_text().strip() == STRINGS["acc.word"].replace("{n}", "3")
    assert word.get_attribute("aria-pressed") == "false"
    r = _rects(page, word=ACC, bars=".bars", plate=".wt-plate")
    assert r["word"]["b"] <= r["bars"]["t"] + 0.5, "the word is over the bars: %r" % r
    assert r["word"]["r"] >= r["plate"]["r"] - 24 and r["word"]["l"] > r["plate"]["l"] + r["plate"]["w"] / 3, "the word stands on the right: %r" % r
    # The lower block of the old capsule is gone: its toggle, the cover link, and the Rows menu (which moved into the head of the open list).
    for sel in (".acc-toggle", "button.cover-link", ".rows-menu", ".acc-cover"):
        assert page.locator(sel).count() == 0, "%s must not stand under the plate at rest" % sel
    page.close()


# --- the list in the place of the bars, the chart and the card ----------------------------------

@pytest.mark.parametrize("width", [1280, 360])
def test_pressed_the_word_folds_the_bars_the_chart_and_the_card_and_the_list_stands_in_their_place(browser, base_view, width):
    page = _page(browser, base_view, width)
    before = _rects(page, main=".acc-main", plate=".wt-plate")
    picked = page.evaluate("() => document.querySelector('.bars .sel').getAttribute('data-focus')")
    page.click(ACC)
    _list_calm(page)
    assert page.locator(ACC).get_attribute("aria-pressed") == "true"
    assert page.locator(ACC).inner_text().strip() == STRINGS["acc.word"].replace("{n}", "3"), "the word keeps its name while pressed"
    after = _rects(page, main=".acc-main", fold=".acc-listfold", head=".acc-list-head", plate=".wt-plate")
    assert after["main"]["h"] < 1, "the bars, the chart and the card are folded shut: %r" % after
    assert page.evaluate("() => document.querySelector('.acc-main > .acc-in').inert === true"), "what is folded cannot be reached by the keyboard"
    _near(after["fold"]["t"], before["main"]["t"], 1, "the list stands where the bars stood")
    _near(after["fold"]["h"], before["main"]["h"], 1.5, "the list takes the height of the place it replaces")
    # The head of the list: Rows and the line of coverage; nothing of them stands under the plate.
    assert page.locator(".acc-list-head details.rows-menu summary").inner_text().strip() == STRINGS["rows.title"]
    assert page.locator(".acc-list-head .acc-cover").inner_text().strip() == STRINGS["cover.some"]
    assert page.locator(".rows-menu").count() == 1 and page.locator(".acc-cover").count() == 1
    assert page.locator(".acc-toggle").count() == 0 and page.locator(".cover-link").count() == 0
    below = page.evaluate("""(plateBottom) => [...document.querySelectorAll('.acc-toggle, .cover-link, .rows-menu, .acc-cover')]
        .filter((n) => n.getBoundingClientRect().top >= plateBottom - 0.5).length""", after["fold"]["b"])
    assert below == 0, "none of the lower block's parts stands under the list"
    assert page.locator(".acc-row").count() == 3
    # Pressed again: the bars and the chart are back, the pick is the same.
    page.click(ACC)
    _list_calm(page)
    _settle(page)
    assert page.locator(ACC).get_attribute("aria-pressed") == "false"
    back = _rects(page, main=".acc-main", fold=".acc-listfold")
    _near(back["main"]["h"], before["main"]["h"], 1.5, "the place of the bars is back")
    assert back["fold"]["h"] < 1
    assert page.evaluate("() => document.querySelector('.bars .sel').getAttribute('data-focus')") == picked
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
    assert page.evaluate("() => document.querySelector('.bars .sel').getAttribute('data-focus')") == bar(C2), "the fixture picks c2 first"
    page.click(ACC)
    _list_calm(page)
    page.locator(".acc-row", has_text="c1").click()
    _list_calm(page)
    _settle(page)
    assert page.locator(ACC).get_attribute("aria-pressed") == "false", "the list is shut"
    assert _rects(page, fold=".acc-listfold")["fold"]["h"] < 1
    assert page.evaluate("() => document.querySelector('.bars .sel').getAttribute('data-focus')") == bar(C1), "the account of the row is picked (its bar is the picked one)"
    assert not _up(page), "the card stands as a strip under the chart"
    assert _rects(page, wrap=".tl-wrap")["wrap"]["h"] > 150, "the chart stands under the new bar"
    assert "c1" in page.locator(".inspector").inner_text()
    page.close()


# --- the card that rises into the place of the chart -----------------------------------------------

def test_at_rest_the_card_is_a_strip_without_the_rows_that_slid_down_and_the_chart_strip_is_hidden(browser, base_view):
    page = _page(browser, base_view, 1280)
    assert page.locator(".kt-peek, .kt-peek-wrap").count() == 0, "the rows of the windows that slid down under the strip are gone"
    assert not _up(page)
    assert page.locator(TOGGLE).get_attribute("aria-expanded") == "false"
    hidden = page.evaluate("() => getComputedStyle(document.querySelector('.tl-strip')).visibility")
    assert hidden == "hidden", "the chart's strip shows only while the card is up"
    page.close()


def test_the_pointer_on_the_card_strip_raises_it_into_the_place_of_the_chart_and_the_chart_folds_into_its_strip(browser, base_view):
    page = _hover_only(browser, base_view)
    rest = _rects(page, card=".inspector", wrap=".tl-wrap", plate=".wt-plate")
    page.hover(TOGGLE)
    _calm_up(page)
    up = _rects(page, card=".inspector", strip=STRIP, wrap=".tl-wrap", plate=".wt-plate", toggle=TOGGLE)
    token = page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('.inspector')).getPropertyValue('--kt-strip'))")
    assert page.locator(TOGGLE).get_attribute("aria-expanded") == "true"
    # The card is as wide as the plate and no longer goes under it, it is inside it, and it stands in the place the chart had.
    _near(up["card"]["l"], up["plate"]["l"], 1, "the card's left edge is the plate's")
    _near(up["card"]["r"], up["plate"]["r"], 1, "the card's right edge is the plate's")
    assert up["card"]["t"] >= up["strip"]["b"] - 1 and up["card"]["b"] <= up["plate"]["b"] + 1, "the card stands under the chart's strip, inside the plate: %r" % up
    assert up["card"]["t"] < rest["card"]["t"] - 100, "the card rose: %r against %r" % (up["card"], rest["card"])
    _near(up["card"]["b"], rest["card"]["b"], 1, "the card rose up, not down: its lower edge stays where the strip's was")
    # The chart folded into a strip as high as the card's own at rest, with the word and the window.
    _near(up["strip"]["h"], token, 1, "the chart's strip is --kt-strip high")
    _near(up["strip"]["t"], rest["wrap"]["t"], 1, "the chart's strip stands where the chart began")
    _near(up["wrap"]["h"], token, 1.5, "the chart folded to its strip")
    shown = page.evaluate("() => { const s = getComputedStyle(document.querySelector('.tl-strip')); return [s.visibility, s.opacity]; }")
    assert shown == ["visible", "1"], shown
    word = page.locator(STRIP).inner_text().strip()
    assert word.startswith(STRINGS["chart.strip"].split("{")[0].strip()), "the strip says Chart and the window: %r" % word
    # Rounded on all four sides.
    radii = page.evaluate("""() => { const s = getComputedStyle(document.querySelector('.inspector'));
      return ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius'].map((k) => parseFloat(s[k])); }""")
    assert all(x > 0 for x in radii) and max(radii) - min(radii) < 0.5, "the card is rounded on all four sides alike: %r" % radii
    assert page.locator(".kt-peek, .kt-peek-wrap").count() == 0
    page.close()


def test_the_pointer_on_the_chart_strip_gives_the_chart_back(browser, base_view):
    page = _hover_only(browser, base_view)
    rest = _rects(page, wrap=".tl-wrap", card=".inspector")
    page.hover(TOGGLE)
    _calm_up(page)
    page.hover(STRIP)
    _calm_down(page)
    back = _rects(page, wrap=".tl-wrap", card=".inspector")
    _near(back["wrap"]["h"], rest["wrap"]["h"], 1.5, "the chart is back at its height")
    _near(back["card"]["t"], rest["card"]["t"], 1.5, "the card is back in its strip")
    assert page.locator(TOGGLE).get_attribute("aria-expanded") == "false"
    assert page.evaluate("() => getComputedStyle(document.querySelector('.tl-strip')).visibility") == "hidden"
    page.close()


def test_the_pointer_leaving_a_card_that_was_only_hovered_lowers_it(browser, base_view):
    # The counter-case of the pin below: without a press the card goes down as the pointer goes.
    page = _hover_only(browser, base_view)
    page.hover(TOGGLE)
    _calm_up(page)
    page.mouse.move(2, 2)
    _calm_down(page)
    assert not _up(page)
    page.close()


@pytest.mark.parametrize("width", [1280, 360])
def test_a_press_on_the_card_strip_pins_it_and_the_pointer_leaving_does_not_lower_it(browser, base_view, width):
    page = _page(browser, base_view, width)
    _raise(page, pin=True)
    assert _up(page)
    page.mouse.move(2, 2)
    page.wait_for_timeout(700)
    page.evaluate(STILL)
    assert _up(page), "the pinned card stays up after the pointer left"
    assert page.locator(TOGGLE).get_attribute("aria-expanded") == "true"
    # A press on the chart's strip gives the chart back and lets the pin go for good.
    page.click(STRIP)
    page.mouse.move(2, 2)
    _calm_down(page)
    page.wait_for_timeout(300)
    assert not _up(page), "the chart came back and the pin is gone"
    page.close()


def test_at_360_the_pointer_alone_raises_nothing_only_a_press_does(browser, base_view):
    page = _page(browser, base_view, 360)
    page.hover(TOGGLE)
    page.wait_for_timeout(500)
    assert not _up(page), "a narrow screen is pressed, not hovered"
    page.click(TOGGLE)
    _calm_up(page)
    assert _up(page)
    page.close()


@pytest.mark.parametrize("width", [1280, 360])
def test_another_bar_with_the_card_up_lowers_it_into_the_strip_and_the_chart_stands_under_the_new_bar(browser, base_view, width):
    page = _page(browser, base_view, width)
    _raise(page, pin=True)
    page.click('[data-focus="%s"]' % bar(C1))
    page.mouse.move(2, 2)
    _calm_down(page)
    assert page.evaluate("() => document.querySelector('.bars .sel').getAttribute('data-focus')") == bar(C1)
    assert not _up(page), "the card of the new bar is a strip"
    assert page.locator(TOGGLE).get_attribute("aria-expanded") == "false"
    assert _rects(page, wrap=".tl-wrap")["wrap"]["h"] > 150, "the chart stands under the new bar"
    assert page.evaluate("() => getComputedStyle(document.querySelector('.tl-strip')).visibility") == "hidden"
    # The old rule kept the opening through a change of bar; it is gone: going back to the first bar does not find the card open.
    page.click('[data-focus="%s"]' % bar(C2))
    page.mouse.move(2, 2)
    _calm_down(page)
    assert not _up(page), "no opening is kept through a change of bar"
    page.close()


@pytest.mark.parametrize("width", [1280, 360])
def test_a_row_of_the_list_with_the_card_up_lowers_the_card_and_shows_the_chart_under_the_new_bar(browser, base_view, width):
    page = _page(browser, base_view, width)
    _raise(page, pin=True)
    page.click(ACC)
    _list_calm(page)
    page.locator(".acc-row", has_text="c1").click()
    _list_calm(page)
    _calm_down(page)
    assert page.evaluate("() => document.querySelector('.bars .sel').getAttribute('data-focus')") == bar(C1)
    assert not _up(page)
    assert _rects(page, wrap=".tl-wrap")["wrap"]["h"] > 150
    page.close()


@pytest.mark.parametrize("width", [1280, 360])
def test_clear_is_not_shown_in_the_card_of_the_plate_up_or_down(browser, base_view, width):
    page = _page(browser, base_view, width)
    probe = "() => { const b = document.querySelector('.insp-clear'); return !b || getComputedStyle(b).display === 'none'; }"
    assert page.evaluate(probe), "Clear is not drawn in the strip's card"
    _raise(page, pin=True)
    assert page.evaluate(probe), "Clear is not drawn in the raised card"
    page.close()


# --- one height ----------------------------------------------------------------------------------

@pytest.mark.parametrize("width", [1280, 360])
def test_the_document_has_one_height_at_rest_with_the_list_open_and_with_the_card_raised(browser, base_view, width):
    page = _page(browser, base_view, width)
    rest = _heights(page)
    assert rest["body"] > 300, "the widget is drawn: %r" % rest
    page.click(ACC)
    _list_calm(page)
    listed = _heights(page)
    page.click(ACC)
    _list_calm(page)
    _settle(page)
    _raise(page, pin=True)
    raised = _heights(page)
    for name, got in (("with the list open", listed), ("with the card raised", raised)):
        _near(got["root"], rest["root"], 1, "the root %s" % name)
        _near(got["body"], rest["body"], 1, "the document %s" % name)
    page.close()


# --- reduced motion --------------------------------------------------------------------------------

def test_under_reduced_motion_the_list_and_the_raised_card_arrive_at_once_and_no_transition_runs(browser, base_view):
    page = _page(browser, base_view, 1280, reduced=True)
    running = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(document.getAnimations().filter((a) => a.constructor.name === 'CSSTransition').length))))"
    page.click(ACC)
    assert page.evaluate(running) == 0, "no transition runs when the list opens"
    assert _rects(page, main=".acc-main")["main"]["h"] < 1, "the list is already there two frames after the press"
    page.click(ACC)
    page.mouse.move(2, 2)
    _list_calm(page)
    _settle(page)
    if _can_hover(page):
        page.hover(TOGGLE)
    else:
        page.click(TOGGLE)
    assert page.evaluate(running) == 0, "no transition runs when the card rises"
    assert _up(page)
    got = _rects(page, card=".inspector")["card"]
    page.evaluate(STILL)
    _near(_rects(page, card=".inspector")["card"]["t"], got["t"], 1, "the card was at its place already two frames after the pointer came")
    page.close()


def test_with_motion_allowed_the_card_does_move_by_a_transition(browser, base_view):
    # The counter-case of the test above: the check "no transition runs" would be empty if no transition ever ran.
    page = _hover_only(browser, base_view)
    page.hover(TOGGLE)
    page.wait_for_function("() => document.querySelector('.inspector.kt-open')")
    count = page.evaluate("() => document.getAnimations().filter((a) => a.constructor.name === 'CSSTransition').length")
    assert count > 0, "a transition runs while the card rises when motion is allowed"
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
