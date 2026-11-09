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
from test_reserve import WEEK, _node, payload, profile, snap


NODE_BARS = r"""
// One plate (.q-plate) for the limit on screen; its bars are .q-bar buttons in .q-bars.
const row = (env, g) => classes(env.root, 'q-plate').find((p) => p.getAttribute('data-limit') === g.key);
const bars = (r) => classes(classes(r, 'q-bars')[0], 'q-bar');  // the strip's, not the legend's small bars
const fillOf = (c) => c.childNodes.find((n) => String(n.className).split(/\s+/).includes('q-fill')) || null;
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
const said = (env) => describedBy(env.root, classes(env.root, 'q-head')[0]);
// The note of the plate (.wt-foot, in the plate under the row of the figure): who has no fresh data, with the age of the readings that are last known.
const noteOf = (r) => classes(r, 'wt-foot').filter((n) => !has(n, 'wt-foot-hold'))[0];

(async () => {
  const fx = JSON.parse(require('fs').readFileSync(process.env.BARS_FIXTURE_FILE, 'utf8'));
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
  assert.equal(b[8].childNodes.filter((n) => has(n, 'q-fill')).length, 0, 'zero is a mark, never a fill');
  assert.equal(b.filter((c) => has(c, 'spent')).length, 1);
  // The restriction stays its own mark, on the bar it holds back.
  assert.deepEqual(b.map((c) => has(c, 'held')), [false, false, false, false, true, false, false, false, false]);
  // The exact share is in the bar's own words (its aria-label, and its title under the pointer), and a share not at the limit never reads 0. No figure is drawn in a bar:
  // the card of the opened account says the share of its own.
  const said_of = (c) => c.getAttribute('aria-label');
  assert.deepEqual(b.map((c) => said_of(c).match(/: (\S+)% left/)[1]), ['100', '80', '60', '50', '30', '25', '1', '0.04', '0']);
  assert.ok(b.every((c) => c.title && said_of(c).startsWith(c.title)), 'the title is the bar\'s words');
  assert.match(said_of(b[8]), /0% left — at the limit/);
  assert.ok(b.every((c) => !/\d/.test(c.textContent)), 'no figure in a bar');
  assert.match(said_of(b[4]), /30% left — restricted now/);
  assert.match(said_of(b[1]), /last known, read .* not current/);
  // The figure is the current readings alone (owner choice, 0.7.0): the
  // last-known values stand under it, dated and named, never inside it.
  assert.match(classes(r, 'l-fig')[0].textContent, /^2\.06 of 9 account-windows left$/);
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
  // The strip has no width of its own: the grid's columns are written on it (qBarsFit: --q-cols, --q-bw, --q-bh), never a width on a bar.
  assert.equal(classes(r, 'q-bars')[0].style.width || '', '');
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
  assert.match(classes(r, 'l-fig')[0].textContent, /^1\.00 of 3 account-windows left$/);
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
  assert.equal(classes(r, 'q-bars')[0].style.width || '', '');

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
  assert.match(classes(r, 'l-fig')[0].textContent, /^— of 41 account-windows left$/);
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
  assert.deepEqual(bars(r).map((c) => c.getAttribute('aria-label').match(/: (\S+)% left/)[1]), ['100', '99.97', '99.7', '99']);
  assert.deepEqual(bars(r).map((c) => Math.round(heightOf(c) * 100) / 100), [100, 99.97, 99.7, 99]);

  // 6. A measured zero is a real 0, not a "—": every current account is at
  // the limit; the stale one stays its own dated line and hatched bar.
  env = await boot(fx.zero);
  g = only(fx.zero);
  r = row(env, g);
  assert.deepEqual(bars(r).map((c) => c.getAttribute('data-state')), ['last_known', 'current', 'current']);
  assert.match(classes(r, 'l-fig')[0].textContent, /^0\.00 of 3 account-windows left$/);
  assert.doesNotMatch(classes(r, 'l-fig')[0].title, /No current reading/);
  assert.equal(classes(r, 'l-sub').length, 0, 'no caption under the bars');
  assert.equal(legendOf(r, 'last'), undefined, 'the note says the last-known account: no legend item repeats it');
  assert.match(noteOf(r).textContent, /No fresh data · 1 read just now · /);
  assert.match(said(env),
    /0\.00 of 3 account-windows left now — last known 0\.50 account-windows, read .*, not counted — 2 current of 3 accounts/);
  // With no last-known value there is nothing under a measured zero: the average is the window tab's alone, not a line of the plate.
  env = await boot(fx.zeroOnly);
  r = row(env, only(fx.zeroOnly));
  assert.match(classes(r, 'l-fig')[0].textContent, /^0\.00 of 2 account-windows left$/);
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
    fixture_file = tmp_path / 'bars.json'
    fixture_file.write_text(json.dumps(fixture), encoding='utf-8')
    result = subprocess.run(
        [str(node), '-e', harness + NODE_BARS],
        cwd=widget_path.parent,
        env={**dict(os.environ), 'WIDGET_PATH': str(widget_path), 'BARS_FIXTURE_FILE': str(fixture_file)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Actual browser geometry: a document can have no horizontal scroll while its
# flex children escape the strip and paint over the words under it (the legend) or the word Chart beside it.
ROW_GEOMETRY = """() => {
  const rect = (e) => { const r = e.getBoundingClientRect(); return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height}; };
  const plate = document.querySelector('.q-plate');
  return {width:innerWidth, scrollWidth:document.documentElement.scrollWidth, plate:rect(plate),
    strip:rect(plate.querySelector('.q-bars')), bars:[...plate.querySelectorAll('.q-bars .q-bar')].map(rect),
    card:plate.querySelector('.q-card') ? rect(plate.querySelector('.q-card')) : null,
    keys:plate.querySelector('.q-keys') ? rect(plate.querySelector('.q-keys')) : null,
    digits:[...plate.querySelectorAll('.q-bar')].some((b) => /\\d/.test(b.textContent)),
    foldedNames:[...document.querySelectorAll('.btn-word-short .btn-word-body')].map(e => ({name:rect(e), button:rect(e.closest('button'))}))};
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
    frame.wait_for_selector('.q-plate')
    frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return frame


def _assert_row_geometry(geometry):
    tolerance = 1
    assert geometry['scrollWidth'] <= geometry['width'] + tolerance, geometry
    plate, strip = geometry['plate'], geometry['strip']
    assert len(geometry['bars']) == 41
    assert not geometry['digits'], 'no figure in a bar'
    if geometry['width'] <= 520:
        # The accessible-only word is positioned inside its control. Clipping
        # alone can still contribute an offscreen box to WebKit's scroll width.
        for item in geometry['foldedNames']:
            assert item['button']['left'] <= item['name']['left'] <= item['name']['right'] <= item['button']['right'], item

    def separate(a, b):
        return (a['right'] <= b['left'] + tolerance or b['right'] <= a['left'] + tolerance
                or a['bottom'] <= b['top'] + tolerance or b['bottom'] <= a['top'] + tolerance)

    for bar in geometry['bars']:
        # every account keeps a bar of its own in the plate, no narrower than --bar-min (20px), wrapped onto more rows rather than scrolled aside
        assert plate['left'] - tolerance <= bar['left'] <= bar['right'] <= plate['right'] + tolerance, (bar, plate)
        assert 20 - tolerance <= bar['width'] <= 56 + tolerance, bar
        for other in ('card', 'keys'):
            if geometry[other]:
                assert separate(bar, geometry[other]), (other, bar, geometry[other])
    for other in ('card', 'keys'):
        if geometry[other]:
            assert plate['left'] - tolerance <= geometry[other]['left'] <= geometry[other]['right'] <= plate['right'] + tolerance, (other, geometry)


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
            for width in (320, 343, 359, 360, 361, 519, 520, 521, 597, 598, 639, 640, 641, 678):
                # innerWidth changes before the browser delivers resize. Observe
                # that event after the widget's existing synchronous fit handler;
                # an arbitrary sleep can sample the previous column widths.
                frame.evaluate("""() => {
                    window.quotaTestResizeWidth = null;
                    window.addEventListener('resize', () => {
                        window.quotaTestResizeWidth = innerWidth;
                    }, {once: true});
                }""")
                page.locator('iframe').evaluate('(e, width) => { e.style.width = width + "px"; }', width)
                frame.wait_for_function('(width) => innerWidth === width && window.quotaTestResizeWidth === width', arg=width)
                _assert_row_geometry(frame.evaluate(ROW_GEOMETRY))
            assert not errors, errors
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The opening of a bar (kit: bars): the words it opens to, for each state of a bar, and how it opens in a real browser.
