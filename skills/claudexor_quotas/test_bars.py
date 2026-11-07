"""0.6.2, rewritten for 0.7.0's approved rows: the bars are drawn to the
value, on one scale, one per account the limit applies to.

The real aggregation builds synthetic readings; the real widget draws them in
the bundled-Node fake DOM of test_quotas. What is checked is what the widget
sets on each bar — its height, its marks, its words — not the style sheet.
"""
import json
import html
import os
import subprocess
import time
from pathlib import Path

import pytest
import plugin
import quota_history as qh
import quota_summary as qs
import test_quotas
from conftest import RU_JSON, RU_JSON_SKIP
from test_reserve import WEEK, _node, payload, profile, snap


NODE_BARS = r"""
// 0.8.0: one .lrow per limit; its name button carries the data-focus
// "limit:<key>" and the row's whole spoken summary; the bars are .bar
// buttons in .bars.
const row = (env, g) => byFocus(env.root, 'limit:' + g.key).parentNode;
const bars = (r) => classes(classes(r, 'bars')[0], 'bar');  // the strip's, not the legend's small bars of the same classes
const fillOf = (c) => c.childNodes.find((n) => String(n.className).split(/\s+/).includes('fill')) || null;
const heightOf = (c) => (fillOf(c) ? parseFloat(fillOf(c).style.height) : null);
const has = (c, name) => String(c.className).split(/\s+/).includes(name);
// The words under the bars: one legend item for each kind of account that is not in the line (data-k), or none.
const legendOf = (r, kind) => classes(r, 'legend-item').find((i) => i.getAttribute('data-k') === kind);
const legendText = (r, kind) => {
  const item = legendOf(r, kind);
  assert.ok(item, 'no legend item ' + kind + ' in: ' + classes(r, 'legend-item').map((n) => n.textContent).join(' | '));
  return item.textContent;
};
// What the plate's control is described by: the whole spoken summary of the limit, a note the word Chart points at.
const said = (env) => describedBy(env.root, classes(env.root, 'l-chart')[0]);
// The note of the plate (.wt-foot, in the plate under the row of the figure): who has no fresh data, with the age of the readings that are last known.
const noteOf = (r) => classes(r, 'wt-foot').filter((n) => !has(n, 'wt-foot-hold'))[0];

(async () => {
  const fx = JSON.parse(process.env.BARS_FIXTURE);
  const only = (view) => view.reserve.summary.groups[0];

  // 1. One bar per account the limit applies to, as tall as its share left:
  // the seven current ones exact (0.04 stays 0.04), the two stale ones as
  // dated last-known values interleaved by their share, and the account at
  // the limit has no fill at all, only its red base mark.
  let env = await boot(fx.main);
  let g = only(fx.main);
  let r = row(env, g);
  let b = bars(r);
  assert.equal(b.length, 9);
  assert.deepEqual(b.map(heightOf), [100, 80, 60, 50, 30, 25, 1, 0.04, null]);
  assert.equal(fillOf(b[7]).style.height, '0.04%');
  assert.deepEqual(b.map((c) => c.getAttribute('data-state')),
    ['current', 'last_known', 'last_known', 'current', 'current', 'current', 'current', 'current', 'current']);
  assert.deepEqual(b.map((c) => has(c, 'last')), [false, true, true, false, false, false, false, false, false]);
  assert.ok(has(b[8], 'spent'));
  assert.equal(b[8].childNodes.filter((n) => has(n, 'fill')).length, 0, 'zero is a mark, never a fill');
  assert.equal(b.filter((c) => has(c, 'spent')).length, 1);
  // The restriction stays its own mark, on the bar it holds back.
  assert.deepEqual(b.map((c) => has(c, 'held')), [false, false, false, false, true, false, false, false, false]);
  // The exact share is in the bar's own words (its aria-label: a bar of an account carries no native title since the bars open to the side, the
  // title would cover the opening in English; the opening, .bar-i, is aria-hidden and says it in the reader's language), and a share not at the limit never reads 0.
  const said_of = (c) => c.getAttribute('aria-label');
  assert.ok(b.every((c) => !c.title), 'a bar of an account has no native title');
  assert.ok(b.every((c) => classes(c, 'bar-i').length === 1 && classes(c, 'bar-i')[0].getAttribute('aria-hidden') === 'true'), 'every bar of an account has its aria-hidden opening');
  assert.deepEqual(b.map((c) => said_of(c).match(/: (\S+)% left/)[1]), ['100', '80', '60', '50', '30', '25', '1', '0.04', '0']);
  assert.match(said_of(b[8]), /0% left — at the limit/);
  // The number over a bar is its share in whole percent: "<1" where it is something and rounds to nothing (0.04), "0" at the limit, and the full bar alone says 100.
  assert.deepEqual(b.map((c) => classes(c, 'bar-v')[0].textContent), ['100', '80', '60', '50', '30', '25', '1', '<1', '0']);
  assert.match(said_of(b[4]), /30% left — restricted now/);
  assert.match(said_of(b[1]), /last known, read .* not current/);
  // The figure is the current readings alone (owner choice, 0.7.0): the
  // last-known values stand under it, dated and named, never inside it.
  assert.match(classes(r, 'l-fig')[0].textContent, /^2\.06 left of 9 accounts$/);
  // Each fact once, in words, under the bars: no line of its own under them (the old "Last known 1.40 · …" caption is gone); the two
  // dated values are the note's ("No fresh data · 2 read 2 h ago · …", the note names them and their age), so the legend has no item
  // for them (they would say the same twice); the plate's spoken summary below keeps their sum, not counted.
  assert.equal(classes(r, 'l-sub').length, 0, 'no caption under the bars');
  assert.equal(legendOf(r, 'last'), undefined, 'the note says the last-known accounts: no legend item repeats it');
  // (read half a minute ago: within two minutes it is "read just now", never "just now ago")
  assert.match(noteOf(r).textContent, /No fresh data · 2 read just now · /);
  assert.doesNotMatch(noteOf(r).textContent, /just now ago/);
  assert.doesNotMatch(r.textContent, /incl\./);
  assert.match(said(env),
    /2\.06 of 9 account-windows left now — last known 1\.40 account-windows, read .*, not counted — 7 current of 9 accounts/);
  // The strip has no width of its own, nor a gap the script sets: it is the plate's width, and a bar takes a share of it (the sheet's --bar-min .. --bar-max).
  assert.equal(classes(r, 'bars')[0].style.width || '', '');
  assert.equal(classes(r, 'bars')[0].style.gap || '', '');
  assert.ok(b.every((c) => !c.style.width), 'no bar is given a width by the script');
  // About says the same with its own samples.
  byFocus(env.root, 'about').listeners.click[0]({ stopPropagation() {} });
  const about = classes(env.root, 'about-panel')[0];
  assert.match(about.textContent, /full height is 100% left, the base 0%/);
  assert.match(about.textContent, /hatched bar is a last-known value/);
  const legend = classes(about, 'strip-legend')[0];
  assert.equal(classes(legend, 'spent')[0].childNodes.length, 0);
  assert.ok(classes(legend, 'last').length === 1);

  // 2. An account whose last reading is from a cycle that has ended is a
  // "?" of its own: no fill, no height, not a zero, and not counted.
  env = await boot(fx.ended);
  g = only(fx.ended);
  r = row(env, g);
  b = bars(r);
  assert.equal(b.length, 3);
  const unknown = b.filter((c) => has(c, 'unknown'));
  assert.equal(unknown.length, 1);
  assert.equal(b[2], unknown[0], 'unknown stands after the rest');
  assert.equal(fillOf(unknown[0]), null);
  assert.match(said_of(unknown[0]), /unknown — its window reset after the last reading/);
  assert.match(said_of(unknown[0]), /unknown, not counted, never a zero/);
  assert.doesNotMatch(said_of(unknown[0]), /: \S+% left/);
  assert.match(classes(r, 'l-fig')[0].textContent, /^1\.00 left of 3 accounts$/);
  // An account with no usable reading is a "?" and has no line under the bars (the sheet's rule: its bar says it, and the note under the
  // window row names who is not read); the one at the limit now is the legend's only item.
  assert.equal(legendOf(r, 'unknown'), undefined, 'no legend line for an unknown account');
  assert.doesNotMatch(classes(r, 'lg').map((n) => n.textContent).join(' '), /unknown|not counted/);

  // 3. A crowded row: every account still has a bar of its own (narrower,
  // one width for all), the scale does not change, and nothing is gathered.
  env = await boot(fx.dense);
  r = row(env, only(fx.dense));
  assert.equal(bars(r).length, 26);
  assert.deepEqual([...new Set(bars(r).filter((c) => !has(c, 'last')).map(heightOf))], [50]);
  assert.deepEqual([...new Set(bars(r).filter((c) => has(c, 'last')).map(heightOf))], [70]);
  assert.equal(classes(r, 'bars')[0].style.width || '', '');

  // 4. Past forty accounts there is still one bar per account, never an
  // average standing in for one.
  env = await boot(fx.many);
  r = row(env, only(fx.many));
  assert.equal(bars(r).length, 41);
  assert.equal(classes(r, 'strip-avg').length, 0);

  // 5. With no current reading but every value last known, the bars stay
  // (hatched, at their last value), the figure is "—" — never 0 — and the
  // last-known windows are the dated line under it.
  env = await boot(fx.none);
  g = only(fx.none);
  r = row(env, g);
  assert.equal(bars(r).length, 41);
  assert.ok(bars(r).every((c) => has(c, 'last') && heightOf(c) === 50));
  assert.match(classes(r, 'l-fig')[0].textContent, /^— left of 41 accounts$/);
  assert.match(classes(r, 'l-fig')[0].title, /^No current reading — not zero\./);
  assert.equal(classes(r, 'l-sub').length, 0, 'no caption under the bars');
  assert.equal(legendOf(r, 'last'), undefined, 'the note says the last-known accounts: no legend item repeats it');
  assert.match(noteOf(r).textContent, /No fresh data · 41 read just now · /);
  assert.doesNotMatch(r.textContent, /incl\.|20\.50 of 41|0\.00 of 41/);
  assert.match(said(env),
    /no current reading — last known 20\.50 account-windows, read .*, not counted — 0 current of 41 accounts/);

  // 5b. A share a little short of all that rounds to 100 says 99 (the paired case of "<1": a number never says what the bar is not): 99.97, 99.7 and 99.5
  // and over, the full bar 100, and 99.0 as it is.
  env = await boot(fx.nearFull);
  r = row(env, only(fx.nearFull));
  assert.deepEqual(bars(r).map((c) => classes(c, 'bar-v')[0].textContent), ['100', '99', '99', '99']);
  assert.deepEqual(bars(r).map((c) => Math.round(heightOf(c) * 100) / 100), [100, 99.97, 99.7, 99]);

  // 6. A measured zero is a real 0, not a "—": every current account is at
  // the limit; the stale one stays its own dated line and hatched bar.
  env = await boot(fx.zero);
  g = only(fx.zero);
  r = row(env, g);
  assert.deepEqual(bars(r).map((c) => c.getAttribute('data-state')), ['last_known', 'current', 'current']);
  assert.match(classes(r, 'l-fig')[0].textContent, /^0\.00 left of 3 accounts$/);
  assert.doesNotMatch(classes(r, 'l-fig')[0].title, /No current reading/);
  assert.equal(classes(r, 'l-sub').length, 0, 'no caption under the bars');
  assert.equal(legendOf(r, 'last'), undefined, 'the note says the last-known account: no legend item repeats it');
  assert.match(noteOf(r).textContent, /No fresh data · 1 read just now · /);
  assert.match(said(env),
    /0\.00 of 3 account-windows left now — last known 0\.50 account-windows, read .*, not counted — 2 current of 3 accounts/);
  // With no last-known value there is nothing under a measured zero: the average is the window tab's alone, not a line of the plate.
  env = await boot(fx.zeroOnly);
  r = row(env, only(fx.zeroOnly));
  assert.match(classes(r, 'l-fig')[0].textContent, /^0\.00 left of 2 accounts$/);
  assert.equal(classes(r, 'l-sub').length, 0, 'no "avg left" line on the plate');
  assert.equal(legendOf(r, 'last'), undefined, 'and no dated value to tell');
  assert.doesNotMatch(r.textContent, /avg left/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _view(tmp_path, name, readings, ended=(), windows=(WEEK,)):
    now = time.time()
    rows, profiles = [], []
    for i, (used, fresh, cooling) in enumerate(readings):
        sid = f'{name}{i:02d}'
        # An index in ``ended`` reports a reset that has already passed.
        reset = qs.iso(now - 600) if i in ended else None
        constraints = [{'id': 'primary' if window == WEEK else 'secondary', 'label': 'primary',
                        'used_ratio': used, 'window_seconds': window, 'resets_at': reset}
                       for window in windows]
        if cooling:
            constraints.append({'id': 'cooldown', 'used_ratio': None, 'window_seconds': None,
                                'cooldown_until': qs.iso(now + 600)})
        rows.append(snap('codex', sid, constraints, fresh=fresh, observed_abs=now - 30))
        profiles.append(profile('codex', sid))
    data = payload(rows, profiles, harnesses=('codex',))
    view = plugin.build_view(data, '')
    store = qh.HistoryStore(tmp_path / name)
    view['reserve'] = plugin.reserve_view(store, data, now, now, harness='codex', name_accounts=True)
    assert len(view['reserve']['summary']['groups']) == len(windows)
    return view


def test_real_widget_draws_each_share_to_scale(tmp_path):
    node = _node()
    assert node is not None, 'a Node runtime is required for widget tests'
    fixture = {
        'main': _view(tmp_path, 'main', [(0, True, False), (.5, True, False), (.7, True, True),
                                         (.75, True, False), (.99, True, False), (.9996, True, False),
                                         (1, True, False), (.2, False, False), (.4, False, False)]),
        'ended': _view(tmp_path, 'ended', [(.5, True, False), (.5, True, False), (.2, False, False)],
                       ended=(2,)),
        'dense': _view(tmp_path, 'dense', [(.5, True, False)] * 20 + [(.3, False, False)] * 6),
        'many': _view(tmp_path, 'many', [(i / 40, True, False) for i in range(41)]),
        'none': _view(tmp_path, 'none', [(.5, False, False)] * 41),
        'forty': _view(tmp_path, 'forty', [(i / 40, True, False) for i in range(38)] + [(.5, False, False)] * 2),
        'fortyNone': _view(tmp_path, 'fortyNone', [(.5, False, False)] * 40),
        'nearFull': _view(tmp_path, 'nearFull', [(0, True, False), (.0003, True, False), (.003, True, False), (.01, True, False)]),
        'zero': _view(tmp_path, 'zero', [(1, True, False), (1, True, False), (.5, False, False)]),
        'zeroOnly': _view(tmp_path, 'zeroOnly', [(1, True, False), (1, True, False)]),
    }
    shares = fixture['main']['reserve']['summary']['groups'][0]['shares']
    assert [s['left'] for s in shares] == [1, .5, .3, .25, .01, .0004, 0]
    widget_path = Path(__file__).with_name('widget.js').resolve()
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    result = subprocess.run(
        [str(node), '-e', harness + NODE_BARS],
        cwd=widget_path.parent,
        env={**dict(os.environ), 'WIDGET_PATH': str(widget_path), 'BARS_FIXTURE': json.dumps(fixture)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Actual browser geometry: a document can have no horizontal scroll while its
# flex children escape the strip and paint over the words under it (the legend) or the word Chart beside it.
ROW_GEOMETRY = """() => {
  const rect = (e) => {
    const r = e.getBoundingClientRect();
    return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height};
  };
  return {width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
    rows:[...document.querySelectorAll('.lrow')].map((row) => ({
      row:rect(row), strip:rect(row.querySelector('.bars')),
      scroll:(() => { const s = row.querySelector('.bars'); return {scrollWidth:s.scrollWidth, clientWidth:s.clientWidth, overflowX:getComputedStyle(s).overflowX}; })(),
      bars:[...row.querySelectorAll('.bars .bar')].map(rect),
      name:row.querySelector('.l-name') ? rect(row.querySelector('.l-name')) : null, figure:rect(row.querySelector('.wt-fact') || row.querySelector('.l-fig')),  // the fact's grid holds the figure (.l-fig is display:contents: no box of its own)

      legend:row.querySelector('.lg') ? rect(row.querySelector('.lg')) : null, control:rect(row.querySelector('.l-chart'))
    }))};
}"""


def _mount_layout_frame(page, view, width, source=None, theme='light', answer=None):
    # Only the data transport is stubbed. Run the unmodified widget and its
    # stylesheet inside an opaque-origin iframe at the actual widget width.
    source = source if source is not None else Path(__file__).with_name('widget.js').read_text()
    texts = json.loads(Path(__file__).with_name('strings_en.json').read_text(encoding='utf-8'))
    # (``answer``: the words of another language, as the skill's answer gives them; English is always beside it)
    answer = dict(answer or {'language': '', 'revision': 0, 'plural_select': None, 'strings': {}}, en=texts)
    stub = ('window.fetch = async (u) => ({ok:true,status:200,text:async () => String(u).indexOf("/i18n") >= 0 ? '
            + json.dumps(json.dumps(answer)) + ' : ' + json.dumps(json.dumps(view)) + '});')
    doc = ('<!doctype html><html data-theme="' + theme + '"><head></head><body><div id="root"></div>'
           + '<script>' + stub + '</script><script>' + source.replace('</script', '<\\/script')
           + '</script></body></html>')
    page.set_content('<iframe sandbox="allow-scripts" style="border:0;width:' + str(width)
                     + 'px;height:1400px" srcdoc="' + html.escape(doc, quote=True) + '"></iframe>')
    frame = page.frames[1]
    frame.wait_for_selector('.lrow')
    frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return frame


def _assert_row_geometry(geometry):
    tolerance = 1
    assert geometry['scrollWidth'] <= geometry['width'] + tolerance, geometry
    heights, control_edges = [], []

    def separate(a, b):
        return (a['right'] <= b['left'] + tolerance or b['right'] <= a['left'] + tolerance
                or a['bottom'] <= b['top'] + tolerance or b['bottom'] <= a['top'] + tolerance)

    for row in geometry['rows']:
        strip = row['strip']
        assert len(row['bars']) == 41
        # The strip is a row that scrolls (kit: bars): a bar is a share of the strip's width, at least --bar-min (16px) wide, and what does not
        # fit at that least goes aside with the scroll (the box clips it), so a bar stands in the strip's content, and only the part in view must keep
        # clear of the legend and of the word Chart.
        scroll = row['scroll']
        assert scroll['scrollWidth'] <= scroll['clientWidth'] or scroll['overflowX'] in ('auto', 'scroll'), row
        for bar in row['bars']:
            assert bar['left'] >= strip['left'] - tolerance and bar['right'] <= strip['left'] + scroll['scrollWidth'] + tolerance, row
            assert bar['width'] >= 16 - tolerance, bar
            seen = {'left': max(bar['left'], strip['left']), 'right': min(bar['right'], strip['right']), 'top': bar['top'], 'bottom': bar['bottom']}
            if seen['left'] >= seen['right']:
                continue
            assert (row['legend'] is None or separate(seen, row['legend'])) and separate(seen, row['control']), row
        for item in ('name', 'figure', 'legend', 'control', 'strip'):
            box = row[item]
            if box is None:  # the plate stands under its window's tab and draws no name
                continue
            assert row['row']['left'] - tolerance <= box['left'] <= box['right'] <= row['row']['right'] + tolerance, row
        assert row['name'] is None or separate(row['name'], row['figure']), row
        assert separate(row['figure'], row['control']), row
        heights.append(strip['height'])
        control_edges.append(row['control']['right'])
    assert max(heights) - min(heights) <= tolerance, heights
    assert max(control_edges) - min(control_edges) <= tolerance, control_edges


def _open_the_card_of_the_chart_word(frame):
    # With the top account picked the chart word stands in the summary of its card (cardTake), and a folded card hides it: the card is pressed open first, as a reader does.
    # (It stays open through the chart folding and coming back, so it is pressed only where it is shut.)
    toggle = frame.locator('[data-focus="inspector-toggle"]')
    if toggle.count() and toggle.get_attribute('aria-expanded') == 'false':
        toggle.click()
        frame.wait_for_function("() => { const c = document.querySelector('.inspector'); return !c || c.classList.contains('kt-open'); }")
        frame.wait_for_function("() => !document.querySelector('.fold-run, .fold-shut')")


def _let_go_the_pick(frame):
    # With the top account picked its card stands in the chart plate and carries the facts, the stamp, the legend and the chart word (cardTake); a test about the plate itself lets the pick go first (the clear button, by script), so everything stands on the plate as before.
    # The pick is let go for this window only: a test that changes the window or the environment calls it again after the change.
    frame.evaluate("() => { const b = document.querySelector('.insp-clear'); if (b) b.click(); }")
    frame.wait_for_function("() => !document.querySelector('.inspector') && !document.querySelector('.fold-run, .fold-shut')")


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('reading', ['current', 'mixed', 'last_known'])
def test_dense_rows_fit_real_iframe_around_responsive_breakpoints(tmp_path, engine, reading):
    from conftest import launch_engine

    sync_api = pytest.importorskip('playwright.sync_api')
    readings = [(i / 40, reading == 'current' or (reading == 'mixed' and i % 2 == 0), False)
                for i in range(41)]
    view = _view(tmp_path, reading, readings, windows=(WEEK, 18000))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={'width': 1100, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            frame = _mount_layout_frame(page, view, 678)
            # Both sides of every row breakpoint, the reported 521 px case,
            # the owner's normal card and narrow card widths.
            for width in (320, 343, 359, 360, 361, 519, 520, 521, 639, 640, 641, 678):
                page.locator('iframe').evaluate('(e, width) => { e.style.width = width + "px"; }', width)
                frame.wait_for_function('(width) => innerWidth === width', arg=width)
                _assert_row_geometry(frame.evaluate(ROW_GEOMETRY))
                # Folding/unfolding the selected timeline must not resize its
                # tracks or displace the aligned control column.
                # Rule: the chart word stands in the summary of the raised card and gives the chart back at once while it lets the card go,
                # so there is no second press on a chart word that a lowered card has hidden. The card raised is the chart folded into its strip (measured), the chart word is the chart
                # back (measured again, the card down).
                _open_the_card_of_the_chart_word(frame)
                _assert_row_geometry(frame.evaluate(ROW_GEOMETRY))
                frame.locator('.l-chart[aria-pressed="true"]').click()
                frame.page.mouse.move(2, 2)
                frame.wait_for_function("() => !document.querySelector('.inspector.kt-open') && !document.querySelector('.fold-run, .fold-shut')")
                _assert_row_geometry(frame.evaluate(ROW_GEOMETRY))
            assert not errors, errors
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The opening of a bar (kit: bars): the words it opens to, for each state of a bar, and how it opens in a real browser.
HOUR = 3600
# What each state of a bar says when it opens, from the top: the account's name, then these lines (as the table of the states has them). The second tuple is the line that wears .bar-a (the accent).
STATES = [
    # (name, patch of the bar, English lines, Russian lines, the line in the accent)
    ('current', dict(state='current', left=.5, at_limit=False, restricted=False, flags=[], resets_at=3.5 * HOUR, observed_at=-3 * HOUR),
     ['reset in 3h', 'read 3 h ago'], ['сброс через 3ч', 'прочитано 3 ч назад'], None),
    ('spent', dict(state='current', left=0.0, at_limit=True, restricted=False, flags=[], resets_at=53.5 * HOUR, observed_at=-3 * HOUR),
     ['at the limit', 'back in 2d 5h'], ['на лимите', 'вернётся через 2д 5ч'], 'at the limit'),
    ('held', dict(state='current', left=.3, at_limit=False, restricted=True, flags=['cooling'], resets_at=40 * 60 + 45, observed_at=-3 * HOUR),
     ['restricted now', 'reset in 40m'], ['ограничен сейчас', 'сброс через 40м'], 'restricted now'),
    ('was', dict(state='last_known', left=0.0, at_limit=True, restricted=False, flags=[], resets_at=3.5 * HOUR, observed_at=-3 * HOUR),
     ['was at the limit', 'read 3 h ago'], ['был на лимите', 'читали 3 ч назад'], None),
    ('last', dict(state='last_known', left=.4, at_limit=False, restricted=False, flags=[], resets_at=3.5 * HOUR, observed_at=-3 * HOUR),
     ['read 3 h ago', 'reset in 3h'], ['читали 3 ч назад', 'сброс через 3ч'], None),
    # Held back now and dated too (.held.last): it says it is held back now, in the accent, and when it resets - not the date alone.
    ('held, dated too', dict(state='last_known', left=.3, at_limit=False, restricted=True, flags=['cooling'], resets_at=40 * 60 + 45, observed_at=-3 * HOUR),
     ['restricted now', 'reset in 40m'], ['ограничен сейчас', 'сброс через 40м'], 'restricted now'),
    ('unknown', dict(state='unknown', left=None, at_limit=False, restricted=False, flags=[], why='not_read', resets_at=None, observed_at=-3 * HOUR),
     ['no data'], ['нет данных'], None),
    # A reset that has passed is no line: the line is not drawn (never an empty one, never "reset in <1m" for a thing that is over).
    ('current, its reset has passed', dict(state='current', left=.5, at_limit=False, restricted=False, flags=[], resets_at=-600, observed_at=-3 * HOUR),
     ['read 3 h ago'], ['прочитано 3 ч назад'], None),
    ('spent, its reset has passed', dict(state='current', left=0.0, at_limit=True, restricted=False, flags=[], resets_at=-600, observed_at=-3 * HOUR),
     ['at the limit'], ['на лимите'], 'at the limit'),
    # The longest Russian line there is: it must stand whole in the room of the words.
    ('current, read 45 min ago', dict(state='current', left=.5, at_limit=False, restricted=False, flags=[], resets_at=3.5 * HOUR, observed_at=-45 * 60),
     ['reset in 3h', 'read 45 min ago'], ['сброс через 3ч', 'прочитано 45 мин назад'], None),
    ('current, no time of reading', dict(state='current', left=.5, at_limit=False, restricted=False, flags=[], resets_at=3.5 * HOUR, observed_at=None),
     ['reset in 3h'], ['сброс через 3ч'], None),
    # Under two minutes it is "just now", never "just now ago" (as the note says it).
    ('last, read a moment ago', dict(state='last_known', left=.4, at_limit=False, restricted=False, flags=[], resets_at=3.5 * HOUR, observed_at=-30),
     ['read just now', 'reset in 3h'], ['читали только что', 'сброс через 3ч'], None),
    ('current, read a moment ago', dict(state='current', left=.5, at_limit=False, restricted=False, flags=[], resets_at=3.5 * HOUR, observed_at=-30),
     ['reset in 3h', 'read just now'], ['сброс через 3ч', 'прочитано только что'], None),
]


def _states_view(tmp_path, nameless=False):
    """One window of one family, one bar of an account for each state above (the real aggregation's bars, then given the state they stand for);
    with ``nameless`` a last bar of no account (a disabled bar, which has nothing to open)."""
    view = _view(tmp_path, 'states', [(.5, True, False)] * (len(STATES) + (1 if nameless else 0)))
    now = time.time()
    group = view['reserve']['summary']['groups'][0]
    for bar, (_name, patch, _en, _ru, _accent) in zip(group['bars'], STATES):
        for stale in ('why', 'last_reading', 'pace', 'origin', 'age_seconds'):
            bar.pop(stale, None)
        for key, value in patch.items():
            # times are given as a distance from now, in seconds
            bar[key] = qs.iso(now + value) if key in ('resets_at', 'observed_at') and value is not None else value
        if patch['state'] == 'last_known':
            bar['origin'] = 'payload'
    if nameless:
        del group['bars'][-1]['account']
    return view


NODE_STATES = r"""
(async () => {
  const env = await boot(JSON.parse(process.env.BARS_FIXTURE));
  const strip = classes(env.root, 'bars')[0];
  const out = classes(strip, 'bar').map((c) => {
    const boxes = classes(c, 'bar-i');
    return { cls: String(c.className), title: c.title || '', aria: c.getAttribute('aria-label'), disabled: !!c.disabled, boxes: boxes.length,
      hidden: boxes[0] ? boxes[0].getAttribute('aria-hidden') : null, boxClass: boxes[0] ? String(boxes[0].className) : null,
      lines: boxes[0] ? boxes[0].childNodes.map((n) => ({ text: n.textContent, cls: String(n.className) })) : [] };
  });
  console.log(JSON.stringify(out));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _draw_states(view, answer):
    node = _node()
    assert node is not None, 'a Node runtime is required for widget tests'
    widget_path = Path(__file__).with_name('widget.js').resolve()
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    harness = harness.replace('const isI18n =', 'Object.assign(I18N_ANSWER, JSON.parse(process.env.I18N_OVERRIDE));\nconst isI18n =', 1)
    result = subprocess.run(
        [str(node), '-e', harness + NODE_STATES], cwd=widget_path.parent,
        env={**dict(os.environ), 'WIDGET_PATH': str(widget_path), 'WIDGET_ROUTE_PREFIX': test_quotas._widget_route_prefix(widget_path),
             'BARS_FIXTURE': json.dumps(view), 'I18N_OVERRIDE': json.dumps(answer)},
        text=True, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _russian_answer():
    import test_i18n
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    entries = json.loads(RU_JSON.read_text(encoding='utf-8'))['entries']
    strings = {key[len(test_i18n.MEMORY_PREFIX):]: (entry['text'] if 'text' in entry else {'forms': entry['forms']})
               for key, entry in entries.items()}
    return {'language': 'ru', 'revision': 7, 'plural_select': test_i18n.ru_select(), 'strings': strings}


@pytest.mark.parametrize('language', ['en', 'ru'])
def test_each_state_of_a_bar_opens_to_its_own_lines(tmp_path, language):
    answer = _russian_answer() if language == 'ru' else {'language': '', 'revision': 0, 'plural_select': None, 'strings': {}}
    drawn = _draw_states(_states_view(tmp_path), answer)
    assert len(drawn) == len(STATES)
    for bar, (name, _patch, english, russian, accent) in zip(drawn, STATES):
        wanted = english if language == 'en' else russian
        # no native title (it would stand over the opening, in English), the opening is hidden from a reader (who has the bar's aria-label), and it is not a .bar
        assert bar['title'] == '' and bar['boxes'] == 1 and bar['hidden'] == 'true' and bar['boxClass'] == 'bar-i', (name, bar)
        texts = [line['text'] for line in bar['lines']]
        # the account's name always first (and it is the name the bar's own words begin with), then the lines of the state, in order, none of them empty
        assert bar['lines'][0]['cls'] == 'bar-n' and bar['aria'].startswith(texts[0]) and texts[0].strip(), (name, bar)
        assert texts[1:] == wanted, (name, language, texts)
        assert all(text.strip() for text in texts), (name, texts)
        # the line in the accent is the first of the two, and only where the bar is at the limit now or held back now
        assert [line['text'] for line in bar['lines'][1:] if line['cls'] == 'bar-a'] == ([wanted[0]] if accent else []), (name, bar['lines'])
        assert all(line['cls'] in ('', 'bar-n', 'bar-a') for line in bar['lines']), (name, bar['lines'])
        assert 'undefined' not in ' '.join(texts) and '{' not in ' '.join(texts), (name, texts)
        if name == 'held, dated too':
            assert {'held', 'last'} <= set(bar['cls'].split()), bar['cls']
        if language == 'en':
            assert 'ago ago' not in ' '.join(texts) and 'just now ago' not in ' '.join(texts), (name, texts)


def test_a_bar_of_no_account_does_not_open_and_keeps_its_title(tmp_path):
    drawn = _draw_states(_states_view(tmp_path, nameless=True), {'language': '', 'revision': 0, 'plural_select': None, 'strings': {}})
    bare = drawn[-1]
    assert bare['disabled'] and bare['boxes'] == 0 and bare['title'] == bare['aria'] and bare['title'], bare
    assert all(not bar['disabled'] for bar in drawn[:-1])


def _still_source():
    """The widget as it is, but for its poll of 30 s (it draws the tree anew): a test that walks over the bars one by one takes longer than that, and a bar the poll has taken out is no bar to measure."""
    source = Path(__file__).with_name('widget.js').read_text(encoding='utf-8')
    assert 'var REFRESH_MS = 30000;' in source
    return source.replace('var REFRESH_MS = 30000;', 'var REFRESH_MS = 3600000;')


OPEN_STATE = """(i) => {
  const strip = document.querySelector('.bars');
  const bars = [...strip.querySelectorAll(':scope > .bar')];
  const bar = bars[i];
  const width = (e) => (e ? e.getBoundingClientRect().width : null);
  const fill = bar.querySelector(':scope > .fill');
  const number = bar.querySelector(':scope > .bar-v');
  const words = bar.querySelector(':scope > .bar-i');
  return {
    widths: bars.map(width), bar: width(bar), fill: width(fill), number: width(number), base: parseFloat(getComputedStyle(bar, '::after').width),
    rest: parseFloat(strip.style.getPropertyValue('--bar-rest')), cls: bar.className, disabled: bar.disabled,
    words: words ? { opacity: parseFloat(getComputedStyle(words).opacity), scroll: words.scrollWidth, client: words.clientWidth,
      lines: [...words.children].map((c) => c.scrollWidth - c.clientWidth), text: words.textContent } : null,
    page: document.documentElement.scrollWidth, view: innerWidth, strip: { scroll: strip.scrollWidth, client: strip.clientWidth, left: strip.scrollLeft },
  };
}"""

# Samples the width of the liquid and of the number of a bar on every frame, until stopped: the liquid must not splash as the bar opens or shuts.
WATCH_LIQUID = """(i) => {
  const bar = document.querySelectorAll('.bars > .bar')[i];
  const parts = [bar.querySelector(':scope > .fill'), bar.querySelector(':scope > .bar-v')].filter(Boolean);
  const seen = { run: true, lo: 1e9, hi: 0, frames: 0 };
  window.__liquid = seen;
  const tick = () => {
    for (const p of parts) { const w = p.getBoundingClientRect().width; seen.lo = Math.min(seen.lo, w); seen.hi = Math.max(seen.hi, w); }
    seen.frames += 1;
    if (seen.run) requestAnimationFrame(tick);
  };
  tick();
}"""


def _let_go_the_top_pick(page, frame):
    """The widget picks the top account itself: from the first drawing its bar is open at the open width and red (.sel), the rest rest. That is the standing state, and it is
    checked here; then the pick is let go as the reader does it (a press on the picked bar, the pointer off it), so the hover of a bar is measured from rest, as these tests do."""
    first = frame.evaluate(OPEN_STATE, 0)
    rest = first['rest']
    assert 'sel' in first['cls'].split() and abs(first['bar'] - (rest + 172)) <= 1.5, ('the top bar is picked and open at the start', first)
    assert all(abs(w - rest) <= 1.5 for w in first['widths'][1:]), ('the other bars rest', first)
    page.frame_locator('iframe').locator('.bars > .bar').nth(0).click()
    page.mouse.move(2, 2)
    frame.wait_for_function('() => ![...document.querySelectorAll(".bars > .bar")].some((b) => b.classList.contains("sel"))', timeout=5000)
    page.wait_for_timeout(900)
    return frame.evaluate(OPEN_STATE, 0)


def test_a_bar_opens_to_the_side_under_the_pointer_and_shuts_after_it_in_a_real_browser(tmp_path):
    from conftest import launch_engine

    sync_api = pytest.importorskip('playwright.sync_api')
    view = _states_view(tmp_path, nameless=True)
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, 'chromium')
        try:
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            frame = _mount_layout_frame(page, view, 1280, source=_still_source())
            frame.evaluate("() => document.fonts.ready")
            count = frame.locator('.bars > .bar').count()
            assert count == len(STATES) + 1
            tolerance = 1.5
            shut = _let_go_the_top_pick(page, frame)
            rest = shut['rest']
            assert 20 <= rest <= 56, shut
            # at rest every bar is as wide as the token the script wrote says, and the liquid fills it
            assert all(abs(w - rest) <= tolerance for w in shut['widths']), shut
            assert shut['page'] <= shut['view'] + tolerance and shut['strip']['scroll'] <= shut['strip']['client'] + tolerance, shut
            assert shut['words']['opacity'] == 0, 'the words are in the bar, hidden by their transparency, until it opens'

            for i, (name, _patch, _en, _ru, accent) in enumerate(STATES):
                bar = frame.locator('.bars > .bar').nth(i)
                frame.evaluate(WATCH_LIQUID, i)
                bar.hover()
                frame.wait_for_function('([i, wide]) => document.querySelectorAll(".bars > .bar")[i].getBoundingClientRect().width >= wide - 1', arg=[i, rest + 172], timeout=5000)
                page.wait_for_timeout(500)
                opened = frame.evaluate(OPEN_STATE, i)
                # it opens to the width it rests at and 172 px more (--bar-open, the room of the words: the basis and the most of the open bar), not to a width that depends on its words
                assert abs(opened['bar'] - (rest + 172)) <= tolerance, (name, opened)
                assert opened['page'] <= opened['view'] + tolerance, (name, 'the page goes sideways', opened)
                assert opened['strip']['scroll'] <= opened['strip']['client'] + tolerance and opened['strip']['left'] == 0, (name, opened)
                # the liquid and the number keep the width the bar had at rest; the base too, where the bar has one
                for part in ('fill', 'number'):
                    assert opened[part] is None or abs(opened[part] - rest) <= tolerance, (name, part, opened)
                if any(word in opened['cls'].split() for word in ('spent', 'was', 'held')):
                    assert abs(opened['base'] - rest) <= tolerance, (name, 'base', opened)
                # the words are shown and not cut: nothing of a line goes past the box that holds them
                assert opened['words']['opacity'] == 1, (name, opened)
                assert opened['words']['scroll'] <= opened['words']['client'] + 1 and all(extra <= 1 for extra in opened['words']['lines']), (name, opened['words'])
                page.mouse.move(2, 2)
                page.wait_for_timeout(600)
                closed = frame.evaluate(OPEN_STATE, i)
                seen = frame.evaluate('() => { window.__liquid.run = false; return window.__liquid; }')
                assert seen['frames'] > 10, seen
                assert abs(closed['bar'] - rest) <= tolerance and all(abs(w - rest) <= tolerance for w in closed['widths']), (name, closed)
                assert closed['words']['opacity'] == 0, (name, closed['words'])
                # from the pointer's coming to the bar's being shut again, the liquid never had another width than the bar's at rest (no splash, no stretch)
                if closed['fill'] is not None or closed['number'] is not None:
                    assert rest - tolerance <= seen['lo'] and seen['hi'] <= rest + tolerance, (name, seen, rest)
                assert not errors, errors

            # a bar of no account is a disabled button: the pointer over it opens nothing
            last = frame.locator('.bars > .bar').nth(count - 1)
            assert last.is_disabled()
            last.hover(force=True)
            page.wait_for_timeout(600)
            bare = frame.evaluate(OPEN_STATE, count - 1)
            assert bare['disabled'] and bare['words'] is None, bare
            assert all(abs(w - rest) <= tolerance for w in bare['widths']), bare
            assert bare['page'] <= bare['view'] + tolerance, bare
            assert not errors, errors
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The move of the opening, the room of the words and the row that cannot hold an opening (kit: bars).
# Samples the width of a bar on every frame, until stopped.
WATCH_WIDTH = """(i) => {
  const bar = document.querySelectorAll('.bars > .bar')[i];
  const seen = { run: true, widths: [] };
  window.__trail = seen;
  const tick = () => { seen.widths.push(bar.getBoundingClientRect().width); if (seen.run) requestAnimationFrame(tick); };
  tick();
}"""

# What the words of a bar need and what they have (a line cut by an ellipsis needs more than it has), and where the bar stands in its row.
WORDS_FIT = """(i) => {
  const strip = document.querySelector('.bars');
  const bar = strip.querySelectorAll(':scope > .bar')[i];
  const box = bar.querySelector(':scope > .bar-i');
  const room = strip.getBoundingClientRect();
  const at = bar.getBoundingClientRect();
  return { width: at.width, left: at.left - room.left, right: room.right - at.right, rest: parseFloat(strip.style.getPropertyValue('--bar-rest')),
    scroll: strip.scrollWidth, client: strip.clientWidth, opacity: parseFloat(getComputedStyle(box).opacity), box: box.clientWidth,
    lines: [...box.children].map((c) => ({ text: c.textContent, need: c.scrollWidth, room: c.clientWidth })) };
}"""


def _hover_open(page, frame, i):
    """The pointer on bar i (after it left the others: a bar that shuts slides the row), and the wait until that bar has its open width and its words."""
    page.mouse.move(2, 2)
    page.wait_for_timeout(350)
    page.frame_locator('iframe').locator('.bars > .bar').nth(i).hover(force=True)
    frame.wait_for_function('''(i) => { const b = document.querySelectorAll(".bars > .bar")[i]; const strip = b.parentNode;
      return b.getBoundingClientRect().width >= parseFloat(strip.style.getPropertyValue("--bar-rest")) + 171 && getComputedStyle(b.querySelector(":scope > .bar-i")).opacity === "1"; }''', arg=i, timeout=5000)
    page.wait_for_timeout(150)


def _trail(frame):
    return frame.evaluate('() => { window.__trail.run = false; return window.__trail.widths; }')


def test_a_bar_opens_and_shuts_by_a_move_not_by_a_jump_in_a_real_browser(tmp_path):
    from conftest import launch_engine

    sync_api = pytest.importorskip('playwright.sync_api')
    view = _states_view(tmp_path)
    tolerance = 1.5
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, 'chromium')
        try:
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            frame = _mount_layout_frame(page, view, 1280, source=_still_source())
            frame.evaluate("() => document.fonts.ready")
            rest = _let_go_the_top_pick(page, frame)['rest']
            wide = rest + 172
            bar = page.frame_locator('iframe').locator('.bars > .bar').nth(0)

            # The pointer comes: on the next frames the bar is between its rest and its open width (it goes there smoothly), and never goes back on the way.
            frame.evaluate(WATCH_WIDTH, 0)
            bar.hover()
            page.wait_for_timeout(700)
            opening = _trail(frame)
            moving = [w for w in opening if w > rest + tolerance]
            between = [w for w in opening if rest + 3 < w < wide - 3]
            assert moving and moving[0] < wide - 3, ('the first frame of the move is already the open width', opening)
            assert len(between) >= 2, ('the bar jumped to its open width', opening)
            assert all(b >= a - 0.5 for a, b in zip(opening, opening[1:])), ('the bar went back as it opened', opening)
            assert abs(opening[-1] - wide) <= tolerance, opening

            # The pointer goes: the same on the way back.
            frame.evaluate(WATCH_WIDTH, 0)
            page.mouse.move(2, 2)
            page.wait_for_timeout(700)
            closing = _trail(frame)
            between = [w for w in closing if rest + 3 < w < wide - 3]
            assert len(between) >= 2, ('the bar jumped to its width at rest', closing)
            assert all(b <= a + 0.5 for a, b in zip(closing, closing[1:])), ('the bar grew as it shut', closing)
            assert abs(closing[-1] - rest) <= tolerance, closing

            # Under reduced motion it is the pair of the move: the width is the one or the other, never between.
            page.emulate_media(reduced_motion='reduce')
            frame.evaluate(WATCH_WIDTH, 0)
            bar.hover()
            page.wait_for_timeout(500)
            still = _trail(frame)
            assert abs(still[-1] - wide) <= tolerance and not [w for w in still if rest + 3 < w < wide - 3], still
            assert not errors, errors
        finally:
            browser.close()


def test_the_open_bar_does_not_shrink_and_the_words_stand_whole_in_a_dense_row_in_both_languages(tmp_path):
    from conftest import launch_engine

    sync_api = pytest.importorskip('playwright.sync_api')
    dense = _view(tmp_path, 'dense41', [(i / 40, True, False) for i in range(41)])
    states = _states_view(tmp_path)
    tolerance = 1.5
    russian = _russian_answer()
    names = [state[0] for state in STATES]
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, 'chromium')
        try:
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            # 41 bars in 340 px: each rests at its least, the open one is that and 172 px more, and its words are whole; the row goes sideways to hold it.
            frame = _mount_layout_frame(page, dense, 340, source=_still_source())
            frame.evaluate("() => document.fonts.ready")
            assert frame.evaluate(WORDS_FIT, 20)['rest'] == 20
            for i in (0, 20, 40):
                _hover_open(page, frame, i)
                fit = frame.evaluate(WORDS_FIT, i)
                assert abs(fit['width'] - (fit['rest'] + 172)) <= tolerance, (i, fit)
                assert fit['scroll'] > fit['client'] + 100, ('the row holds all the bars and the open one without going sideways', fit)
                assert fit['opacity'] == 1 and all(line['need'] <= line['room'] + 1 for line in fit['lines']), (i, fit)
            page.mouse.move(2, 2)

            # The Russian words, the longest of them, in a row of room and in a row of none, and a name longer than the room is cut by an ellipsis, not widened to.
            for language, answer, width in (('en', None, 1280), ('ru', russian, 1280), ('ru', russian, 340)):
                frame = _mount_layout_frame(page, states, width, source=_still_source(), answer=answer)
                frame.evaluate("() => document.fonts.ready")
                # (the bars with the longest lines of the states; the room is the same for every bar, and the pointer leaves between two, or the row slides under it)
                for i in [names.index(name) for name in ('current', 'spent', 'held, dated too', 'was', 'current, read 45 min ago')]:
                    _hover_open(page, frame, i)
                    fit = frame.evaluate(WORDS_FIT, i)
                    assert abs(fit['width'] - (fit['rest'] + 172)) <= tolerance, (language, width, i, fit)
                    for line in fit['lines']:
                        assert line['text'] and line['need'] <= line['room'] + 1, (language, width, i, 'a line is cut', line, fit['box'])
                page.mouse.move(2, 2)
                page.wait_for_timeout(350)
                texts = frame.evaluate("() => [...document.querySelectorAll('.bar-i > *')].map((n) => n.textContent)")
                if language == 'ru':
                    assert 'прочитано 45 мин назад' in texts, texts
                # an account's name that is longer than the room: cut with an ellipsis, the bar keeps its width
                frame.evaluate("""() => { const n = document.querySelector('.bars > .bar .bar-n'); n.textContent = 'a-very-long-address-of-a-mailbox@some-long-company-name.example.com'; }""")
                _hover_open(page, frame, 0)
                fit = frame.evaluate(WORDS_FIT, 0)
                name = fit['lines'][0]
                assert name['need'] > name['room'] and frame.evaluate("() => getComputedStyle(document.querySelector('.bars > .bar .bar-n')).textOverflow") == 'ellipsis', fit
                assert abs(fit['width'] - (fit['rest'] + 172)) <= tolerance, fit
                page.mouse.move(2, 2)
            assert not errors, errors
        finally:
            browser.close()


def test_the_bar_the_keyboard_opens_is_brought_into_the_room_of_a_row_that_cannot_hold_it_in_a_real_browser(tmp_path):
    from conftest import launch_engine

    sync_api = pytest.importorskip('playwright.sync_api')
    dense = _view(tmp_path, 'dense41k', [(i / 40, True, False) for i in range(41)])
    tolerance = 1.5
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, 'chromium')
        try:
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            frame = _mount_layout_frame(page, dense, 340, source=_still_source())
            frame.evaluate("() => document.fonts.ready")
            bars = page.frame_locator('iframe').locator('.bars > .bar')
            # The keyboard goes along the row to a bar that is far off to the right; the last bar is the farthest.
            bars.nth(36).focus()
            for _ in range(4):
                page.keyboard.press('Tab')
            focused = frame.evaluate("() => [...document.querySelectorAll('.bars > .bar')].indexOf(document.activeElement)")
            assert focused == 40, focused
            before = page.evaluate('() => scrollY')
            page.wait_for_timeout(700)
            fit = frame.evaluate(WORDS_FIT, 40)
            assert abs(fit['width'] - (fit['rest'] + 172)) <= tolerance and fit['opacity'] == 1, fit
            # the whole of it is in the room of the row: its words are seen, none is under the edge
            assert fit['left'] >= -tolerance and fit['right'] >= -tolerance, ('the open bar sticks out of its row', fit)
            assert all(line['need'] <= line['room'] + 1 for line in fit['lines']), fit
            assert page.evaluate('() => scrollY') == before, 'the page jumped'
            # and the one before it, to the left: the same, from the other side
            for _ in range(6):
                page.keyboard.press('Shift+Tab')
            page.wait_for_timeout(700)
            index = frame.evaluate("() => [...document.querySelectorAll('.bars > .bar')].indexOf(document.activeElement)")
            fit = frame.evaluate(WORDS_FIT, index)
            assert fit['left'] >= -tolerance and fit['right'] >= -tolerance, ('the open bar sticks out of its row', index, fit)
            assert not errors, errors
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The white capsule has more room inside it on the wide shell (from 860px); the narrow shell keeps the padding it had.
CARD_ROOM = """() => {
  const card = document.querySelector('.shell-card'), cs = getComputedStyle(card), box = card.getBoundingClientRect();
  const kids = [...card.children].map((n) => n.getBoundingClientRect()).filter((r) => r.height > 0 && r.width > 0);
  const strip = document.querySelector('.bars'), chart = document.querySelector('.l-chart'), plate = document.querySelector('.wt-plate');
  const inset = (n) => { const r = n.getBoundingClientRect(); return { left: r.left - box.left, right: box.right - r.right, top: r.top - box.top, bottom: box.bottom - r.bottom }; };
  return { pad: [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft],
    kids: { left: Math.min(...kids.map((r) => r.left)) - box.left, right: box.right - Math.max(...kids.map((r) => r.right)),
            top: Math.min(...kids.map((r) => r.top)) - box.top, bottom: box.bottom - Math.max(...kids.map((r) => r.bottom)) },
    chart: inset(chart), plate: inset(plate), strip: inset(strip), bars: strip.querySelectorAll(':scope > .bar').length,
    rest: parseFloat(strip.style.getPropertyValue('--bar-rest')), scroll: strip.scrollWidth, client: strip.clientWidth,
    page: document.documentElement.scrollWidth, view: innerWidth, rail: !!document.querySelector('.shell.has-rail') };
}"""


def test_the_capsule_has_24px_over_and_under_and_16px_at_the_sides_from_860px_and_its_parts_stand_inside_them_and_the_narrow_shell_is_as_it_was(tmp_path):
    from conftest import launch_engine

    sync_api = pytest.importorskip('playwright.sync_api')
    view = _view(tmp_path, 'room20', [(i / 20, True, False) for i in range(20)])
    tolerance = 0.75
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, 'chromium')
        try:
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            seen = {}
            for width in (1280, 860, 859, 600, 521, 520, 375):
                frame = _mount_layout_frame(page, view, width, source=_still_source())
                frame.evaluate("() => document.fonts.ready")
                seen[width] = frame.evaluate(CARD_ROOM)
                assert seen[width]['page'] <= seen[width]['view'] + 1, (width, 'the page goes sideways', seen[width])
            # wide: 24px over the first part and under the last, 16px at the sides; every part (the row of the bars, the word Chart at its right, the plate) stands inside the fields
            for width in (1280, 860):
                room = seen[width]
                assert room['pad'] == ['24px', '16px', '24px', '16px'], (width, room['pad'])
                assert room['kids']['top'] >= 24 - tolerance and room['kids']['bottom'] >= 24 - tolerance, (width, 'over and under', room['kids'])
                assert room['kids']['left'] >= 12 - tolerance and room['kids']['right'] >= 12 - tolerance, (width, 'at the sides', room['kids'])
                for part in ('chart', 'plate', 'strip'):
                    assert room[part]['left'] >= 12 - tolerance and room[part]['right'] >= 12 - tolerance, (width, part, room[part])
                assert room['rail'], (width, 'the rail stands at the side of the card')
            # twenty bars stand in the row at 1280 with no scroll, each wider than the least a bar is (--bar-min: 20px)
            wide = seen[1280]
            assert wide['bars'] == 20 and wide['scroll'] <= wide['client'] + 1 and wide['rest'] >= 20, ('twenty bars fit the row at 1280 with no scroll', wide)
            # narrow: the padding it had (8px; 8px over and under, 4px at the sides under 521px)
            for width in (859, 600, 521):
                assert seen[width]['pad'] == ['8px', '8px', '8px', '8px'], (width, seen[width]['pad'])
            for width in (520, 375):
                assert seen[width]['pad'] == ['8px', '4px', '8px', '4px'], (width, seen[width]['pad'])
            assert not errors, errors
        finally:
            browser.close()
