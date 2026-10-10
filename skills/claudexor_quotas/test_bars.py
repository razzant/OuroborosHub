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
// 0.8.0: one .lrow per limit; its name button carries the data-focus
// "limit:<key>" and the row's whole spoken summary; the bars are .bar
// buttons in .bars.
const row = (env, g) => byFocus(env.root, 'limit:' + g.key).parentNode;
const bars = (r) => classes(r, 'bar');
const fillOf = (c) => c.childNodes.find((n) => String(n.className).split(/\s+/).includes('fill')) || null;
const heightOf = (c) => (fillOf(c) ? parseFloat(fillOf(c).style.height) : null);
const has = (c, name) => String(c.className).split(/\s+/).includes(name);

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
  // Exact on hover, and a share not at the limit never reads 0.
  assert.deepEqual(b.map((c) => c.title.match(/: (\S+)% left/)[1]), ['100', '80', '60', '50', '30', '25', '1', '0.04', '0']);
  assert.match(b[8].title, /0% left — at the limit/);
  assert.match(b[4].title, /30% left — restricted now/);
  assert.match(b[1].title, /last known, read .* not current/);
  // The figure is the current readings alone (owner choice, 0.7.0): the
  // last-known values stand under it, dated and named, never inside it.
  assert.match(classes(r, 'l-fig')[0].textContent, /^2\.06 of 9 accounts$/);
  assert.match(classes(r, 'l-sub')[0].textContent, /^Last known 1\.40 · /);
  assert.match(classes(r, 'l-sub')[0].title, /not in the figure: 1\.40 account-windows of 2 accounts/);
  assert.match(classes(r, 'l-sub')[0].title, /The figure: 2\.06 of 7 current accounts/);
  assert.doesNotMatch(r.textContent, /incl\./);
  assert.match(byFocus(env.root, 'limit:' + g.key).getAttribute('aria-label'),
    /2\.06 of 9 account-windows left now — last known 1\.40 account-windows, read .*, not counted — 7 current of 9 accounts/);
  // One strip width for the slot count; the bars share it equally.
  assert.equal(classes(r, 'bars')[0].style.width, (9 * 28 + 8 * 3) + 'px');
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
  assert.match(unknown[0].title, /unknown — its window reset after the last reading/);
  assert.match(unknown[0].title, /unknown, not counted, never a zero/);
  assert.doesNotMatch(unknown[0].title, /: \S+% left/);
  assert.match(classes(r, 'l-fig')[0].textContent, /^1\.00 of 3 accounts$/);
  assert.match(classes(r, 'l-tail')[0].textContent, /1 unknown — not counted/);

  // 3. A crowded row: every account still has a bar of its own (narrower,
  // one width for all), the scale does not change, and nothing is gathered.
  env = await boot(fx.dense);
  r = row(env, only(fx.dense));
  assert.equal(bars(r).length, 26);
  assert.deepEqual([...new Set(bars(r).filter((c) => !has(c, 'last')).map(heightOf))], [50]);
  assert.deepEqual([...new Set(bars(r).filter((c) => has(c, 'last')).map(heightOf))], [70]);
  assert.equal(classes(r, 'bars')[0].style.width, (26 * 21 + 25 * 2) + 'px');

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
  assert.match(classes(r, 'l-fig')[0].textContent, /^— of 41 accounts$/);
  assert.match(classes(r, 'l-fig')[0].title, /^No current reading — not zero\./);
  assert.match(classes(r, 'l-sub')[0].textContent, /^Last known 20\.50 · /);
  assert.match(classes(r, 'l-sub')[0].title, /The figure: no current reading\./);
  assert.doesNotMatch(r.textContent, /incl\.|20\.50 of 41|0\.00 of 41/);
  assert.match(byFocus(env.root, 'limit:' + g.key).getAttribute('aria-label'),
    /no current reading — last known 20\.50 account-windows, read .*, not counted — 0 current of 41 accounts/);

  // 6. A measured zero is a real 0, not a "—": every current account is at
  // the limit; the stale one stays its own dated line and hatched bar.
  env = await boot(fx.zero);
  g = only(fx.zero);
  r = row(env, g);
  assert.deepEqual(bars(r).map((c) => c.getAttribute('data-state')), ['last_known', 'current', 'current']);
  assert.match(classes(r, 'l-fig')[0].textContent, /^0\.00 of 3 accounts$/);
  assert.doesNotMatch(classes(r, 'l-fig')[0].title, /No current reading/);
  assert.match(classes(r, 'l-sub')[0].textContent, /^Last known 0\.50 · /);
  assert.match(byFocus(env.root, 'limit:' + g.key).getAttribute('aria-label'),
    /0\.00 of 3 account-windows left now — last known 0\.50 account-windows, read .*, not counted — 2 current of 3 accounts/);
  // With no last-known value the line under a measured zero is its average.
  env = await boot(fx.zeroOnly);
  r = row(env, only(fx.zeroOnly));
  assert.match(classes(r, 'l-fig')[0].textContent, /^0\.00 of 2 accounts$/);
  assert.match(classes(r, 'l-sub')[0].textContent, /^0% avg left$/);
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
# flex children escape the strip and paint over the caption beside it.
ROW_GEOMETRY = """() => {
  const rect = (e) => {
    const r = e.getBoundingClientRect();
    return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height};
  };
  return {width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
    rows:[...document.querySelectorAll('.lrow')].map((row) => ({
      row:rect(row), strip:rect(row.querySelector('.bars')),
      bars:[...row.querySelectorAll('.bar')].map(rect),
      name:rect(row.querySelector('.l-name')), figure:rect(row.querySelector('.l-fig')),
      caption:rect(row.querySelector('.l-sub')), control:rect(row.querySelector('.l-chart'))
    }))};
}"""


def _mount_layout_frame(page, view, width, source=None, theme='light'):
    # Only the data transport is stubbed. Run the unmodified widget and its
    # stylesheet inside an opaque-origin iframe at the actual widget width.
    source = source if source is not None else Path(__file__).with_name('widget.js').read_text()
    stub = 'window.fetch = async () => ({ok:true,status:200,text:async () => ' + json.dumps(json.dumps(view)) + '});'
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
        for bar in row['bars']:
            assert bar['left'] >= strip['left'] - tolerance and bar['right'] <= strip['right'] + tolerance, row
            assert bar['width'] >= 4 - tolerance, bar
            assert separate(bar, row['caption']) and separate(bar, row['control']), row
        for item in ('name', 'figure', 'caption', 'control', 'strip'):
            box = row[item]
            assert row['row']['left'] - tolerance <= box['left'] <= box['right'] <= row['row']['right'] + tolerance, row
        assert separate(row['name'], row['figure']) and separate(row['figure'], row['control']), row
        heights.append(strip['height'])
        control_edges.append(row['control']['right'])
    assert max(heights) - min(heights) <= tolerance, heights
    assert max(control_edges) - min(control_edges) <= tolerance, control_edges


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('reading', ['current', 'mixed', 'last_known'])
def test_dense_rows_fit_real_iframe_around_responsive_breakpoints(tmp_path, engine, reading):
    from test_order_focus import _launch_engine

    sync_api = pytest.importorskip('playwright.sync_api')
    readings = [(i / 40, reading == 'current' or (reading == 'mixed' and i % 2 == 0), False)
                for i in range(41)]
    view = _view(tmp_path, reading, readings, windows=(WEEK, 18000))
    with sync_api.sync_playwright() as playwright:
        browser = _launch_engine(playwright, engine)
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
                frame.locator('.l-chart[aria-pressed="true"]').click()
                _assert_row_geometry(frame.evaluate(ROW_GEOMETRY))
                frame.locator('.l-chart').first.click()
            assert not errors, errors
        finally:
            browser.close()
