"""The chart's plate and its Details, combed: in a real browser, the rectangles and the styles the reader sees.

What is held here:
- "what is left at a few moments ahead" is a caption over one white tile for each moment: as many tiles as there are moments, the number
  whole with the crimson point, the moment and the total under it; a value that is not known reads "unknown" in its tile, never 0; the
  group has a name for a screen reader;
- on a narrow screen (520 px and under) the tiles stand in one column, the number keeps one line, nothing of a tile is cut, and the
  page does not go sideways; the wide screen keeps its row;
- the reset table stands in a white card, its last row has no line under it, and "N more resets" opens the rest and folds it back;
- the limit's details (dl) stand in a white card, two columns (label 180 px, value) that fold to one at 520 px and under;
- the plate of the chart has four round corners and no line under it;
- the plot area has its rounded corners as a clip of the background layers (the tint of the future, the lines); the labels of the axes
  stand outside it, so nothing of them is cut.
The fake DOM of the other widget tests has no layout: these run in Chromium (and WebKit where Playwright has it).
"""
import json
import os
from pathlib import Path

import pytest

from test_reserve import _widget_fixture, with_i18n

WIDGET = Path(os.environ.get("CHART_POLISH_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
pytest.importorskip("playwright.sync_api")

STUB = with_i18n(r"""
(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true } : view) });
  };
}
""")


@pytest.fixture(scope="module")
def base_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("polish"))["view"]


def _variant(base, moments=None, unknown=None, resets=5):
    """The fixture view with `resets` events in the schedule, `moments` checkpoints (the first ones, or more of the last) and the checkpoint
    at index `unknown` not known."""
    view = json.loads(json.dumps(base))
    sc = view["reserve"]["chart"]["scenarios"]["no_new_use"]
    first = sc["schedule"][0]
    sc["schedule"] = [dict(first, at=first["at"] + 3600 * i, last=first["at"] + 3600 * i, total_after=min(3.0, first["total_after"] + .1 * i))
                      for i in range(resets)]
    if moments is not None:
        cps = sc["checkpoints"]
        while len(cps) < moments:
            cps.append(dict(cps[-1], after_seconds=cps[-1]["after_seconds"] + 3600, at=cps[-1]["at"] + 3600))
        sc["checkpoints"] = cps[:moments]
    if unknown is not None:
        sc["checkpoints"][unknown]["value"] = None
    return view


def _open(browser, view, width, height=1000, details=True):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(STUB, view)
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    page.wait_for_selector(".tl-block .chart-svg")
    if details:
        page.click('[data-focus="chart-table"]')
        page.wait_for_selector(".checkpoints .cp-tile")
    return page


TILES = """() => [...document.querySelectorAll('.checkpoints .cp-tile')].map((tile) => {
  const num = tile.querySelector('.cp-num'), when = tile.querySelector('.cp-when'), box = tile.getBoundingClientRect();
  const numBox = num.getBoundingClientRect(), whenBox = when.getBoundingClientRect(), style = getComputedStyle(tile);
  const lines = (node) => { const r = document.createRange(); r.selectNodeContents(node); return new Set([...r.getClientRects()].map((b) => Math.round(b.top))).size; };
  return { num: num.textContent, none: num.classList.contains('none'), dots: num.querySelectorAll('.cp-dot').length, when: when.textContent,
           left: box.left, right: box.right, top: box.top, bottom: box.bottom,
           numRight: numBox.right, numBottom: numBox.bottom, whenRight: whenBox.right, whenBottom: whenBox.bottom,
           numScroll: num.scrollWidth, numClient: num.clientWidth, whenScroll: when.scrollWidth, whenClient: when.clientWidth,
           numLines: lines(num), whenLines: lines(when),
           bg: style.backgroundColor, radius: style.borderTopLeftRadius + '|' + style.borderBottomRightRadius, shadow: style.boxShadow };
})"""

PAGE_FITS = "() => ({ doc: document.documentElement.scrollWidth, view: window.innerWidth })"


def test_a_tile_for_each_moment_the_unknown_reads_in_its_tile_and_the_group_has_a_name(browser, base_view):
    for moments in (1, 2, 3, 4):
        page = _open(browser, _variant(base_view, moments=moments, unknown=moments - 1 if moments > 1 else None), 1120)
        box = page.locator(".checkpoints")
        assert box.count() == 1
        # a group with a name: the scenario, said once for a reader that has no eyes
        assert box.get_attribute("role") == "group"
        assert box.get_attribute("aria-label") == "Left if nothing more is used"
        assert page.locator(".checkpoints .cp-cap").inner_text() == "Left if nothing more is used"
        tiles = page.evaluate(TILES)
        assert len(tiles) == moments, tiles
        # one row: each tile at its own left, all at one top
        assert len({round(t["left"]) for t in tiles}) == moments and len({round(t["top"]) for t in tiles}) == 1, tiles
        for tile in tiles:
            assert tile["when"].endswith(" · of 3") and tile["when"].startswith("in "), tile
            assert tile["radius"] == "14px|14px" and tile["shadow"] != "none", tile
        assert len({t["bg"] for t in tiles}) == 1
        known = [t for t in tiles if not t["none"]]
        assert all(t["dots"] == 1 and "." in t["num"] for t in known if t["num"].replace(".", "").isdigit()), known
        if moments > 1:
            # a value that is not known is said in its tile, not a zero and not an empty place
            unknown = tiles[-1]
            assert unknown["none"] and unknown["num"] == "unknown" and unknown["dots"] == 0, unknown
            assert not any(t["num"] in ("0", "0.00") for t in tiles), "an unknown value is not drawn as zero"
        page.close()


def test_the_known_figure_is_whole_with_one_crimson_point(browser, base_view):
    page = _open(browser, _variant(base_view, moments=3), 1120)
    tiles = page.evaluate(TILES)
    assert [t["num"] for t in tiles] == ["1.34", "2.00", "2.00"]
    assert all(t["dots"] == 1 and not t["none"] for t in tiles)
    page.close()


@pytest.mark.parametrize("width", [360, 320, 520])
def test_on_a_narrow_screen_the_tiles_stand_in_one_column_and_nothing_of_a_tile_is_cut(browser, base_view, width):
    page = _open(browser, _variant(base_view, moments=3, unknown=2), width, details=True)
    tiles = page.evaluate(TILES)
    assert len(tiles) == 3
    # one column: one left, three tops, one under another
    assert len({round(t["left"]) for t in tiles}) == 1 and len({round(t["top"]) for t in tiles}) == 3, tiles
    assert [t["top"] for t in tiles] == sorted(t["top"] for t in tiles)
    for tile in tiles:
        # the number on one line, and inside its tile on every side
        assert tile["numLines"] == 1 and tile["whenLines"] == 1, tile
        assert tile["numScroll"] <= tile["numClient"] + 1 and tile["whenScroll"] <= tile["whenClient"] + 1, tile
        assert tile["numRight"] <= tile["right"] + .5 and tile["whenRight"] <= tile["right"] + .5, tile
        assert tile["numBottom"] <= tile["bottom"] + .5 and tile["whenBottom"] <= tile["bottom"] + .5, tile
    fits = page.evaluate(PAGE_FITS)
    assert fits["doc"] <= fits["view"], ("the page goes sideways", fits)
    page.close()


@pytest.mark.parametrize("width", [521, 640, 860, 1280])
def test_from_521_up_the_tiles_keep_their_row_and_their_text(browser, base_view, width):
    page = _open(browser, _variant(base_view, moments=3, unknown=2), width)
    tiles = page.evaluate(TILES)
    assert len({round(t["top"]) for t in tiles}) == 1 and len({round(t["left"]) for t in tiles}) == 3, tiles
    for tile in tiles:
        assert tile["numLines"] == 1 and tile["numScroll"] <= tile["numClient"] + 1 and tile["numRight"] <= tile["right"] + .5, tile
        assert tile["whenRight"] <= tile["right"] + .5, tile
    fits = page.evaluate(PAGE_FITS)
    assert fits["doc"] <= fits["view"], fits
    page.close()


CARD = """(sel) => { const node = document.querySelector(sel), style = getComputedStyle(node);
  return { bg: style.backgroundColor, radius: style.borderTopLeftRadius + '|' + style.borderTopRightRadius + '|' + style.borderBottomRightRadius + '|' + style.borderBottomLeftRadius,
           shadow: style.boxShadow, parent: node.parentElement.className }; }"""


def test_the_reset_table_stands_in_a_white_card_and_the_button_opens_and_folds_the_rest(browser, base_view):
    page = _open(browser, _variant(base_view, resets=5), 1120)
    assert page.locator(".schedule-card > table.schedule").count() == 1
    assert page.locator(".schedule-card").evaluate("n => n.children.length") == 1, "the table is the card's only child"
    card, tile = page.evaluate(CARD, ".schedule-card"), page.evaluate(CARD, ".cp-tile")
    assert card["radius"] == "14px|14px|14px|14px" and card["shadow"] != "none"
    assert card["bg"] == tile["bg"], "the card is white as the tiles are"
    # the table's caption and head are in the card, the table fits in it
    inside = page.evaluate("""() => { const c = document.querySelector('.schedule-card').getBoundingClientRect(), t = document.querySelector('.schedule').getBoundingClientRect();
      return t.left >= c.left - .5 && t.right <= c.right + .5 && t.top >= c.top - .5 && t.bottom <= c.bottom + .5; }""")
    assert inside
    rows = lambda: page.locator(".schedule tbody tr").count()
    button = page.locator(".schedule .schedule-more")
    assert button.count() == 1 and rows() == 2 + 1, "two events and the row of the button"
    assert button.get_attribute("aria-expanded") == "false" and "3 more resets" in button.inner_text()
    # a capsule with a soft shadow in place of a frame
    style = button.evaluate("n => { const s = getComputedStyle(n); return { shadow: s.boxShadow, radius: s.borderTopLeftRadius, w: s.borderTopWidth, c: s.borderTopColor }; }")
    assert style["shadow"] != "none" and float(style["radius"].rstrip("px")) >= 14 and style["c"] == "rgba(0, 0, 0, 0)", style
    button.click()
    button = page.locator(".schedule .schedule-more")
    assert rows() == 5 + 1 and button.get_attribute("aria-expanded") == "true" and "Show the first 2" in button.inner_text()
    button.click()
    button = page.locator(".schedule .schedule-more")
    assert rows() == 2 + 1 and button.get_attribute("aria-expanded") == "false"
    # the last row has no line under it (the lines are the cells' tops)
    below = page.evaluate("() => [...document.querySelectorAll('.schedule tbody tr:last-child td')].map((c) => getComputedStyle(c).borderBottomWidth)")
    assert set(below) == {"0px"}, below
    page.close()


@pytest.mark.parametrize("width", [360, 320])
def test_on_a_narrow_screen_the_table_reads_as_before_and_the_page_does_not_go_sideways(browser, base_view, width):
    page = _open(browser, _variant(base_view, resets=5), width)
    fits = page.evaluate(PAGE_FITS)
    assert fits["doc"] <= fits["view"], ("the page goes sideways", fits)
    # the card carries the table: it scrolls by itself where the table is wider than the screen, the table's head is still there
    assert page.locator(".schedule-card").evaluate("n => getComputedStyle(n).overflowX") == "auto"
    assert page.locator(".schedule thead th").count() == 4
    cards = page.evaluate("() => { const b = document.querySelector('.schedule-card').getBoundingClientRect(); return [b.left, b.right]; }")
    assert cards[0] >= 0 and cards[1] <= width + .5
    page.close()


DL = """() => { const dl = document.querySelector('dl.reserve-detail'), style = getComputedStyle(dl);
  const dts = [...dl.children].filter((n) => n.tagName === 'DT'), dds = [...dl.children].filter((n) => n.tagName === 'DD');
  const edge = (n) => getComputedStyle(n).borderBottomWidth;
  return { cols: style.gridTemplateColumns, bg: style.backgroundColor, radius: style.borderTopLeftRadius, shadow: style.boxShadow,
           inDetails: !!dl.closest('details'), count: dts.length, same: dts.length === dds.length,
           dtColor: getComputedStyle(dts[0]).color, ddColor: getComputedStyle(dds[0]).color,
           dtWidth: dts[0].getBoundingClientRect().width, ddLeft: dds[0].getBoundingClientRect().left, dtLeft: dts[0].getBoundingClientRect().left,
           dtBottom: dts[0].getBoundingClientRect().bottom, ddTop: dds[0].getBoundingClientRect().top,
           dtEdges: dts.map(edge), ddEdges: dds.map(edge), page: document.documentElement.scrollWidth <= window.innerWidth,
           right: dl.getBoundingClientRect().right, box: dl.parentElement.getBoundingClientRect().right }; }"""


def test_the_details_stand_in_a_white_card_two_columns_a_line_between_the_rows_and_none_under_the_last(browser, base_view):
    page = _open(browser, _variant(base_view), 1120)
    dl = page.evaluate(DL)
    tile = page.evaluate(CARD, ".cp-tile")
    assert dl["inDetails"] and dl["count"] >= 3 and dl["same"], dl
    assert dl["bg"] == tile["bg"] and dl["radius"] == "14px" and dl["shadow"] != "none", dl
    first = dl["cols"].split()
    assert len(first) == 2 and first[0] == "180px", dl["cols"]
    assert dl["ddLeft"] - dl["dtLeft"] >= 180 and dl["dtWidth"] >= 180, dl
    assert dl["dtColor"] != dl["ddColor"], "the label is the quiet ink, the value the full one"
    # a line under each row but the last
    assert dl["dtEdges"][-1] == "0px" and dl["ddEdges"][-1] == "0px", dl
    assert all(w != "0px" for w in dl["dtEdges"][:-1]) and all(w != "0px" for w in dl["ddEdges"][:-1]), dl
    page.close()


@pytest.mark.parametrize("width", [520, 360, 320])
def test_on_a_narrow_screen_the_label_stands_over_its_value_in_one_column(browser, base_view, width):
    page = _open(browser, _variant(base_view), width)
    dl = page.evaluate(DL)
    assert len(dl["cols"].split()) == 1, dl["cols"]
    assert abs(dl["dtLeft"] - dl["ddLeft"]) < 1 and dl["dtBottom"] <= dl["ddTop"] + 1, dl
    # the line is under the value, none under the label, none under the last value
    assert dl["dtEdges"][0] == "0px" and dl["ddEdges"][0] != "0px" and dl["ddEdges"][-1] == "0px", dl
    assert dl["page"] and dl["right"] <= dl["box"] + .5, dl
    page.close()


PLATE = """() => { const block = document.querySelector('.tl-block'), s = getComputedStyle(block), b = block.getBoundingClientRect();
  const svg = block.querySelector('.chart-svg').getBoundingClientRect();
  return { corners: [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomRightRadius, s.borderBottomLeftRadius],
           line: [s.borderTopWidth, s.borderBottomWidth], bg: s.backgroundColor,
           within: svg.left >= b.left - .5 && svg.right <= b.right + .5 && svg.top >= b.top - .5 && svg.bottom <= b.bottom + .5,
           kids: [...block.children].every((c) => c.getBoundingClientRect().right <= b.right + .5 && c.getBoundingClientRect().left >= b.left - .5),
           page: document.documentElement.scrollWidth <= window.innerWidth }; }"""


@pytest.mark.parametrize("width", [1280, 860, 360])
def test_the_history_stands_in_the_plate_under_a_hairline_with_no_plate_of_its_own_and_holds_the_chart(browser, base_view, width):
    page = _open(browser, _variant(base_view), width)
    plate = page.evaluate(PLATE)
    # The history is the plate's last part now (the approved composition, 8 Nov 2026): no plate of its own, a hairline over it, the plate's ground.
    assert plate["corners"] == ["0px"] * 4, plate
    assert plate["line"] == ["1px", "0px"], plate
    assert plate["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), plate
    assert plate["within"] and plate["kids"] and plate["page"], plate
    page.close()


FIELD = """() => {
  const svg = document.querySelector('.tl-block .chart-svg'), clip = svg.querySelector('clipPath#qa-field');
  const rect = clip && clip.querySelector('rect');
  const clipped = [...svg.querySelectorAll('[clip-path]')];
  const area = rect.getBBox();
  const labels = [...svg.querySelectorAll('.axis-text')];
  const inClip = (n) => !!n.closest('[clip-path]');
  return { has: !!clip, rx: rect.getAttribute('rx'), rxCss: getComputedStyle(rect).rx,
           clipped: clipped.map((n) => n.getAttribute('class') + '|' + n.getAttribute('clip-path')),
           classes: [...new Set(clipped.map((n) => n.getAttribute('class')))],
           labelsClipped: labels.filter(inClip).length, labels: labels.length,
           yLabels: labels.filter((n) => n.getAttribute('text-anchor') === 'end').map((n) => n.getBBox().x + n.getBBox().width <= area.x + .5),
           xLabels: labels.filter((n) => n.getAttribute('text-anchor') !== 'end').map((n) => n.getBBox().y >= area.y + area.height - 1),
           lines: [...svg.querySelectorAll('.line-observed,.line-scenario,.reset-dot,.hit')].filter(inClip).length,
           area: [area.x, area.y, area.width, area.height] };
}"""


def test_the_plot_area_has_rounded_corners_as_a_clip_of_its_background_and_the_axis_labels_stand_outside_it(browser, base_view):
    page = _open(browser, _variant(base_view), 1120, details=False)
    field = page.evaluate(FIELD)
    assert field["has"] and field["rx"] == "10" and field["rxCss"] == "10px", field
    # the tint of the future, the lines of the axes and of the days, the marks of the resets are cut by the corner
    assert {"future-bg", "grid base", "grid", "grid cap"} <= set(field["classes"]), field["classes"]
    assert all(c.endswith("|url(#qa-field)") for c in field["clipped"]), field["clipped"]
    # the labels are never cut: not one of them stands in the clip, the y labels are left of the area and the x labels under it
    assert field["labels"] > 4 and field["labelsClipped"] == 0, field
    assert field["yLabels"] and all(field["yLabels"]) and field["xLabels"] and all(field["xLabels"]), field
    # the record and the scenario, the dots and the hit area are not clipped
    assert field["lines"] == 0, field
    page.close()


def test_the_clip_follows_the_plot_when_the_screen_is_narrow(browser, base_view):
    page = _open(browser, _variant(base_view), 360, details=False)
    field = page.evaluate(FIELD)
    assert field["has"] and field["labelsClipped"] == 0 and all(field["yLabels"]) and all(field["xLabels"]), field
    svg = page.evaluate("() => { const s = document.querySelector('.tl-block .chart-svg').getBoundingClientRect(); const b = document.querySelector('.tl-block').getBoundingClientRect(); return s.right <= b.right + .5 && s.left >= b.left - .5; }")
    assert svg
    page.close()


IDLE_INK = """() => {
  const parse = (c) => { const v = c.match(/[\\d.]+/g).map(Number); return { r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1 }; };
  const lin = (x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const idle = document.querySelector('.tl-controls .seg-opt.idle:not(.active)');
  if (!idle) return null;
  const quiet = document.querySelector('.tl-controls .seg-opt:not(.idle):not(.active)');
  let ground = { r: 255, g: 255, b: 255 };
  const chain = []; for (let n = idle; n; n = n.parentElement) chain.push(n);
  for (const n of chain.reverse()) { const c = parse(getComputedStyle(n).backgroundColor); if (c.a > 0) ground = { r: c.r * c.a + ground.r * (1 - c.a), g: c.g * c.a + ground.g * (1 - c.a), b: c.b * c.a + ground.b * (1 - c.a) }; }
  const ink = parse(getComputedStyle(idle).color), hi = Math.max(lum(ink), lum(ground)), lo = Math.min(lum(ink), lum(ground));
  const cs = getComputedStyle(idle);
  return { ratio: (hi + 0.05) / (lo + 0.05), ink: cs.color, quietInk: quiet ? getComputedStyle(quiet).color : null, border: cs.borderTopStyle };
}"""


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_idle_option_keeps_the_ink_of_a_live_button_and_is_told_by_its_outline(browser, base_view, theme):
    # A pace nobody has measured draws what No new use draws, but the button works: its words read as the other options' do (4.5:1 at least, both themes), and it is told by a dashed outline.
    view = json.loads(json.dumps(base_view))
    view["reserve"]["chart"]["scenarios"]["recent_pace"]["at_pace"] = 0  # no account has a measured pace: the option draws what No new use draws
    page = _open(browser, view, 1120, details=False)
    if theme == "light":
        page.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
    page.wait_for_timeout(200)
    got = page.evaluate(IDLE_INK)
    assert got, "the future segment has an idle option on this fixture"
    assert got["ratio"] >= 4.5, ("the idle option reads", theme, got)
    assert got["ink"] == got["quietInk"], ("the ink of the idle option is the ink of the other options", got)
    assert got["border"] == "dashed", ("the idle option is told by its form", got)
    page.close()


def test_tooltip_at_now_keeps_both_series_inside_narrow_page(browser, base_view):
    page = _open(browser, base_view, 320, details=False)
    try:
        page.locator('[data-focus="chart-plot"]').focus()
        tip = page.locator('.chart-tip')
        assert tip.is_visible()
        assert tip.locator('.tip-row').count() == 2
        box = tip.bounding_box()
        assert box and box['x'] >= 0 and box['x'] + box['width'] <= 320
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        evidence = os.environ.get('QUOTAS_EVIDENCE_DIR')
        if evidence:
            page.screenshot(path=str(Path(evidence) / (browser.browser_type.name + '-tooltip-now.png')), full_page=True)
    finally:
        page.close()
