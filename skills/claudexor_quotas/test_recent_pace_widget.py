"""What the widget says of a Recent pace future, over real backend answers.

The answers come from the real collector and route path (test_recent_pace_binding's synthetic
accounts at the current time): measured paces draining beside another account's stale 30-day
window, a measured zero, a measured pace held by the account's own unknown window, no measured
pace at all, and a Combined view with and without a known zero. The fake DOM of the other
widget tests reads the words and controls; a real browser (Chromium, WebKit) reads the drawn
pace line. Nothing here reads a real account.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import plugin
import test_quotas
import test_recent_pace_binding as rp
from test_reserve import _node, with_i18n

WIDGET = Path(os.environ.get("RECENT_PACE_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()


def build_views(folder):
    """Each view as the widget's chart read gets it, at the current time (the fixtures' clock moved)."""
    now = float(int(time.time()))
    saved = {name: getattr(rp, name) for name in ("NOW", "START", "WEEK_RESET", "CUT")}
    rp.NOW, rp.START, rp.WEEK_RESET, rp.CUT = now, now - 1.5 * rp.H, now + 3 * 86400, now - 600
    try:
        def view(name, accounts, **feed):
            store = rp.feed(folder / name, accounts, **feed)
            status = rp.status_at(now, accounts)
            out = plugin.build_view(status, "")
            out["reserve"] = plugin.reserve_view(store, status, now, now, harness="codex", group=rp.WEEK_KEY,
                                                 horizon="7d", name_accounts=True, analytics=True)
            return out
        views = {
            "drain": view("drain", rp.codex_accounts()),
            "zero": view("zero", rp.codex_accounts(a=rp.weekly("a", 0.53), b=rp.weekly("b", 0.36))),
            "held": view("held", rp.codex_accounts(a=rp.own_month("stale"), b=None, d=None)),
            "none": view("none", rp.codex_accounts(), start=now - 600),
            "nofigure": view("nofigure", rp.codex_accounts(d=None)),
        }
    finally:
        for name, value in saved.items():
            setattr(rp, name, value)
    split = {name: rp.split(v["reserve"]["chart"]["analytics"]["window"]["scenarios"]["recent_pace"])
             for name, v in views.items()}
    # The fixtures are what their names say (the backend tests hold the numbers).
    assert split["drain"]["at_pace"] == 2 and split["drain"]["zero_growth"] == 2
    assert split["zero"]["at_pace"] == 0 and split["zero"]["zero_growth"] == 4 and split["zero"]["pace_held"] == 0
    assert split["held"]["at_pace"] == 0 and split["held"]["pace_held"] == 1 and split["held"]["zero_growth"] == 1
    assert split["none"]["at_pace"] == 0 and split["none"]["pace_unknown"] == 4
    return views


@pytest.fixture(scope="module")
def views(tmp_path_factory):
    return build_views(tmp_path_factory.mktemp("recent-pace-views"))


@pytest.fixture(scope="module")
def fixture_file(views, tmp_path_factory):
    path = tmp_path_factory.mktemp("recent-pace-fixture") / "views.json"
    path.write_text(json.dumps(views), encoding="utf-8")
    return str(path)


NODE = r"""
const text = (n) => n.textContent;
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  node.listeners.click[0]({stopPropagation() {}});
}
const block = (env) => classes(env.root, 'tl-block')[0];
const calm = (env) => text(classes(block(env), 'calm-legend')[0]);
const lead = (env) => classes(block(env), 'chart-legend').map(text).join(' | ');
const pace = (env) => byFocus(env.root, 'scenario:recent_pace');
const idle = (node) => String(node.className).split(/\s+/).includes('idle');
(async () => {
  const fx = JSON.parse(require('fs').readFileSync(process.env.UI_FIXTURE_FILE, 'utf8'));
  const en = I18N_ANSWER.en;
  async function open(name) {
    const env = await boot(() => fx[name]);
    await settle();
    click(env, 'scenario:recent_pace');
    await settle();
    return env;
  }
  // Measured paces drain beside another account's stale 30-day window: a line of its own.
  let env = await open('drain');
  assert.ok(!idle(pace(env)) && pace(env).getAttribute('data-idle') === null, pace(env).className);
  assert.equal(pace(env).title, en['chart.future_title_recent_pace']);
  assert.match(calm(env), /recent pace: a condition, not a forecast/);
  assert.doesNotMatch(calm(env), /same as no new use/);
  assert.match(lead(env), /recent pace · 2 of 4 current at their pace of the last [^,;]+, 2 with no net change; each refilled once/);
  assert.doesNotMatch(lead(env), /held/);
  env.disposeHooks[0]();

  // A measured zero: the same line as No new use, said as a measured zero, not as a missing pace.
  env = await open('zero');
  assert.ok(idle(pace(env)));
  assert.equal(pace(env).title, en['chart.pace_zero_title']);
  assert.equal(pace(env).getAttribute('aria-label'), 'Recent pace — ' + en['chart.pace_zero_title']);
  assert.match(calm(env), /recent pace: no growth measured — same as no new use/);
  assert.doesNotMatch(calm(env), /no measured pace yet/);
  assert.match(lead(env), /4 current accounts held at their shares; .* Their measured pace shows no net change in that span \(not proof of zero use\)\./);
  env.disposeHooks[0]();

  // A measured pace held by the account's own window that is not known now.
  env = await open('held');
  assert.ok(idle(pace(env)));
  assert.equal(pace(env).title, en['chart.pace_held_title']);
  assert.match(calm(env), /recent pace: pace measured but held — same as no new use/);
  assert.match(lead(env), /2 current accounts held at their shares; .* A measured pace is held: another limit of those accounts is not known now/);
  env.disposeHooks[0]();

  // No measured pace at all: the old words, with why.
  env = await open('none');
  assert.ok(idle(pace(env)));
  assert.equal(pace(env).title, en['chart.pace_idle_title']);
  assert.match(calm(env), /recent pace: no measured pace yet — same as no new use/);
  assert.match(lead(env), /No account has a measured pace yet \(warming up/);
  env.disposeHooks[0]();

  // An answer without the split (an older skill): its idle line keeps the old words.
  const old = JSON.parse(JSON.stringify(fx.zero));
  for (const sc of Object.values(old.reserve.chart.analytics.window.scenarios)) {
    delete sc.pace_held; delete sc.pace_unknown; sc.zero_growth = 0;
  }
  delete old.reserve.chart.analytics.window.holds;
  env = await boot(() => old);
  await settle();
  click(env, 'scenario:recent_pace');
  await settle();
  assert.match(calm(env), /recent pace: no measured pace yet — same as no new use/);
  env.disposeHooks[0]();

  // Combined: a known zero reads 0.00 of the accounts it counts; none counted reads "—", never 0.
  for (const [name, figure] of [['drain', '0.00'], ['nofigure', '—']]) {
    env = await boot(() => fx[name]);
    await settle();
    click(env, 'graph:joint');
    await settle();
    const hero = text(classes(block(env), 'tl-num')[0]);
    assert.ok(hero.startsWith(figure), name + ': ' + hero);
    assert.match(text(classes(block(env), 'tl-sub')[0]), name === 'drain' ? /1 of 5 accounts counted · 4 left out/ : /0 of 4 accounts counted · 4 left out/);
    const note = classes(block(env), 'tl-joint')[0];
    assert.equal(text(walk(note).find((n) => n.tagName === 'B')), figure, name + ': the figure in Details says the same');
    assert.match(text(note), /Left out, never counted as positive: 4 window missing/);
    env.disposeHooks[0]();
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""


def test_the_widget_says_why_a_pace_line_is_flat_or_draws_it(fixture_file):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run([str(node), "-e", harness + NODE], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": str(WIDGET), "UI_FIXTURE_FILE": fixture_file},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# A real browser draws the line

pytest.importorskip("playwright.sync_api")

STUB = with_i18n(r"""
(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true } : view) });
  };
}
""")

DRAWN = """() => {
  const path = document.querySelector('.tl-block .chart-svg path.line-scenario');
  const legend = document.querySelector('.tl-block .calm-legend');
  const button = document.querySelector('[data-focus="scenario:recent_pace"]');
  const ys = path ? (path.getAttribute('d').match(/-?[\\d.]+/g) || []).map(Number).filter((_, i) => i % 2 === 1) : [];
  const xs = path ? (path.getAttribute('d').match(/-?[\\d.]+/g) || []).map(Number).filter((_, i) => i % 2 === 0) : [];
  return { legend: legend ? legend.textContent : null, idle: button ? button.getAttribute('data-idle') : null,
           pressed: button ? button.getAttribute('aria-pressed') : null, ys, xs,
           width: document.documentElement.scrollWidth, view: innerWidth };
}"""


def _drawn(browser, view, width, shot=None):
    page = browser.new_page(viewport={"width": width, "height": 1300})
    try:
        page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
        page.evaluate(STUB, view)
        page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
        page.wait_for_selector(".tl-block .chart-svg")
        page.click('[data-focus="scenario:recent_pace"]')
        page.wait_for_function("() => document.querySelector('[data-focus=\"scenario:recent_pace\"]').getAttribute('aria-pressed') === 'true'")
        page.wait_for_timeout(150)
        got = page.evaluate(DRAWN)
        evidence = os.environ.get("QUOTAS_EVIDENCE_DIR")
        if evidence and shot:
            page.locator(".tl-block").first.screenshot(path=str(Path(evidence) / f"{browser.browser_type.name}-{shot}-{width}.png"))
        return got
    finally:
        page.close()


@pytest.mark.parametrize("width", [1120, 390])
def test_a_real_browser_draws_the_measured_paces_falling(browser, views, width):
    got = _drawn(browser, views["drain"], width, shot="recent-pace-drain")
    assert got["pressed"] == "true" and got["idle"] is None, got
    assert re.search(r"recent pace: a condition, not a forecast", got["legend"]), got["legend"]
    # SVG y grows downward: the pace line falls between now and the first reported reset (b runs out at +16 h)
    ys = got["ys"]
    assert len(ys) >= 3 and max(ys[1:4]) > ys[0] + 1, ys[:6]
    assert got["width"] <= got["view"], "nothing goes sideways"


@pytest.mark.parametrize("name,words", [("zero", "no growth measured"), ("held", "pace measured but held"),
                                        ("none", "no measured pace yet")])
def test_a_real_browser_says_why_an_idle_pace_line_is_flat(browser, views, name, words):
    got = _drawn(browser, views[name], 1120, shot=f"recent-pace-{name}")
    assert got["idle"] == "true" and got["pressed"] == "true", got
    assert f"recent pace: {words} — same as no new use" in got["legend"], got["legend"]
    ys = got["ys"]
    # flat until the first reported reset: the line keeps the figure at now
    first_reset = next((i for i in range(1, len(ys)) if abs(ys[i] - ys[0]) > 0.05), len(ys))
    assert first_reset >= 2 and all(abs(y - ys[0]) <= 0.05 for y in ys[:first_reset]), ys[:6]
