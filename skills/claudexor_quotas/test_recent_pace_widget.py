"""What the widget says of a Typical (48h pooled) future vs No new use, over real backend answers.

The answers come from the real collector and route path (test_recent_pace_binding's synthetic
accounts at the current time, fed over 16 hours so the 48-hour pooled estimator qualifies):
measured pooled burn draining beside another account's stale 30-day window, a measured zero,
all accounts held by an unknown sibling window, insufficient (<12h) history, and a Combined
view with and without a known zero. The fake DOM of the other widget tests reads the words
and controls; a real browser (Chromium, WebKit) reads the drawn line. Nothing here reads a
real account.
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
    """Each view as the widget's chart read gets it, at the current time (16h synthetic history)."""
    now = float(int(time.time() // 3600) * 3600 + 600)
    saved = {name: getattr(rp, name) for name in ("NOW", "START", "WEEK_RESET", "CUT", "STEP")}
    rp.NOW = now
    rp.START = now - 16 * rp.H
    rp.WEEK_RESET = now + 3 * 86400
    rp.CUT = now - 600
    rp.STEP = 300.0
    try:
        def view(name, accounts, **feed):
            store = rp.feed(folder / name, accounts, **feed)
            status = rp.status_at(now, accounts)
            out = plugin.build_view(status, "")
            out["reserve"] = plugin.reserve_view(store, status, now, now, harness="codex", group=rp.WEEK_KEY,
                                                 horizon="7d", name_accounts=True, analytics=True)
            return out
        drain_accounts = rp.codex_accounts(
            a=rp.weekly("a", rp.rising(0.15, rp.A_RATE)),
            b=rp.weekly("b", rp.rising(0.10, rp.B_RATE)),
        )
        views = {
            "drain": view("drain", drain_accounts),
            "zero": view("zero", rp.codex_accounts(a=rp.weekly("a", 0.53), b=rp.weekly("b", 0.36))),
            "held": view("held", rp.codex_accounts(a=rp.own_month("stale"), b=None, c=None, d=None)),
            "none": view("none", drain_accounts, start=now - 4 * rp.H),
            "nofigure": view("nofigure", rp.codex_accounts(
                a=rp.weekly("a", rp.rising(0.15, rp.A_RATE)),
                b=rp.weekly("b", rp.rising(0.10, rp.B_RATE)),
                d=None,
            )),
        }
    finally:
        for name, value in saved.items():
            setattr(rp, name, value)
    typ = {name: v["reserve"]["chart"]["analytics"]["window"]["scenarios"]["typical"]
           for name, v in views.items()}
    # The fixtures are what their names say (the backend tests hold the numbers).
    assert typ["drain"]["status"] == "ok" and typ["drain"]["rate_per_hour"] > 0.04
    assert typ["zero"]["status"] == "zero" and typ["zero"]["rate_per_hour"] == 0.0
    assert typ["held"]["status"] == "held" and typ["held"]["line"] == []
    assert typ["none"]["status"] in ("warming_up", "insufficient") and typ["none"]["valid_hours"] < 12 and typ["none"]["line"] == []
    return views


@pytest.fixture(scope="module")
def views(tmp_path_factory):
    return build_views(tmp_path_factory.mktemp("typical-widget-views"))


@pytest.fixture(scope="module")
def fixture_file(views, tmp_path_factory):
    path = tmp_path_factory.mktemp("typical-widget-fixture") / "views.json"
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
const typBtn = (env) => byFocus(env.root, 'scenario:typical');
const idle = (node) => String(node.className).split(/\s+/).includes('idle');
(async () => {
  const fx = JSON.parse(require('fs').readFileSync(process.env.UI_FIXTURE_FILE, 'utf8'));
  const en = I18N_ANSWER.en;
  async function open(name) {
    const env = await boot(() => fx[name]);
    await settle();
    return env;
  }
  const hasLine = (env) => walk(block(env)).some((n) => n.tagName === 'PATH' && String(n.getAttribute('class') || '').includes('line-scenario'));
  // Qualified 48h pooled typical burn drains beside another account's stale 30-day window.
  let env = await open('drain');
  assert.equal(typBtn(env).getAttribute('aria-pressed'), 'true', 'Typical is the default');
  assert.ok(!idle(typBtn(env)) && typBtn(env).getAttribute('data-idle') === null, typBtn(env).className);
  assert.equal(typBtn(env).title, en['chart.future_title_typical']);
  assert.ok(hasLine(env));
  assert.match(calm(env), /48h typical · 0\.\d{3} win\/h/);
  assert.doesNotMatch(calm(env), /not drawn/);
  assert.match(lead(env), /typical · 48h pool average \(0\.\d{3} win\/h, \d+(?:\.\d)?h valid\) across 4 current accounts; each refilled once/);
  // Switching to No new use shows refill-only line & legend.
  click(env, 'scenario:no_new_use');
  await settle();
  assert.ok(hasLine(env));
  assert.match(calm(env), /no new use · refill only/);
  env.disposeHooks[0]();

  // A measured zero over >= 12 valid hours IS a known Typical forecast (not idle, draws its flat line).
  env = await open('zero');
  assert.ok(!idle(typBtn(env)) && typBtn(env).getAttribute('data-idle') === null);
  assert.ok(hasLine(env), 'known measured zero draws its flat line');
  assert.match(calm(env), /48h typical · 0\.000 win\/h \(no burn measured\)/);
  assert.doesNotMatch(calm(env), /not enough 48h history|not drawn/);
  assert.match(lead(env), /typical · 48h pool average measured 0\.000 win\/h \(\d+(?:\.\d)?h valid — not proof of zero use\) across 4 current accounts/);
  env.disposeHooks[0]();

  // Held by the account's own window that is not known now -> draws NO future line or checkpoints.
  env = await open('held');
  assert.ok(idle(typBtn(env)));
  assert.equal(typBtn(env).title, en['chart.typical_held_title']);
  assert.ok(!hasLine(env), 'held Typical draws no future line');
  assert.equal(classes(block(env), 'checkpoints').length, 0);
  assert.match(calm(env), /typical: held — not drawn/);
  assert.match(lead(env), /Another limit of these accounts is unknown or spent with no reported reset: no forecast drawn/);
  env.disposeHooks[0]();

  // Insufficient (< 12 valid pool hours): draws NO future line and explains clearly why.
  env = await open('none');
  assert.ok(idle(typBtn(env)));
  assert.equal(typBtn(env).title, en['chart.typical_idle_title']);
  assert.ok(!hasLine(env), 'insufficient Typical draws no future line');
  assert.equal(classes(block(env), 'checkpoints').length, 0);
  assert.match(calm(env), /typical: not enough 48h history yet — not drawn/);
  assert.match(lead(env), /Needs at least 12h of valid pool history in the last 48 h/);
  env.disposeHooks[0]();

  // An answer without scenarios.typical (an older skill): Typical is idle and draws no line.
  const old = JSON.parse(JSON.stringify(fx.zero));
  delete old.reserve.chart.scenarios.typical;
  delete old.reserve.chart.analytics.window.scenarios.typical;
  env = await boot(() => old);
  await settle();
  assert.ok(idle(typBtn(env)));
  assert.ok(!hasLine(env));
  assert.match(calm(env), /typical: not enough 48h history yet — not drawn/);
  env.disposeHooks[0]();

  // Combined: a known zero reads 0.00 of the accounts it counts; none counted reads "—", never 0;
  // Combined marks Typical idle and draws NO future line under Typical; No new use works separately.
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
    if (name === 'drain') {
      assert.ok(idle(typBtn(env)), 'Combined marks Typical idle');
      assert.ok(!hasLine(env), 'Combined draws NO future line while Typical is selected');
      assert.match(calm(env), /typical: unavailable for Combined — not drawn/);
    }
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
  const button = document.querySelector('[data-focus="scenario:typical"]');
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
        page.wait_for_function("() => document.querySelector('[data-focus=\"scenario:typical\"]').getAttribute('aria-pressed') === 'true'")
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
    got = _drawn(browser, views["drain"], width, shot="typical-drain")
    assert got["pressed"] == "true" and got["idle"] is None, got
    assert re.search(r"48h typical · 0\.\d{3} win/h", got["legend"]), got["legend"]
    # SVG y grows downward: the pooled typical line falls between now and the first reported reset.
    ys = got["ys"]
    assert len(ys) >= 3 and max(ys[1:4]) > ys[0] + 1, ys[:6]
    assert got["width"] <= got["view"], "nothing goes sideways"


def test_a_real_browser_draws_measured_zero_typical_as_flat_live_line(browser, views):
    got = _drawn(browser, views["zero"], 1120, shot="typical-zero")
    assert got["idle"] is None and got["pressed"] == "true", got
    assert "48h typical · 0.000 win/h (no burn measured)" in got["legend"], got["legend"]
    ys = got["ys"]
    # flat until the first reported reset: the line keeps the figure at now
    first_reset = next((i for i in range(1, len(ys)) if abs(ys[i] - ys[0]) > 0.05), len(ys))
    assert first_reset >= 2 and all(abs(y - ys[0]) <= 0.05 for y in ys[:first_reset]), ys[:6]


@pytest.mark.parametrize("name,words", [("held", "held"), ("none", "not enough 48h history yet")])
def test_a_real_browser_draws_no_future_line_when_typical_is_unknown(browser, views, name, words):
    got = _drawn(browser, views[name], 1120, shot=f"typical-{name}")
    assert got["idle"] == "true" and got["pressed"] == "true", got
    assert f"typical: {words} — not drawn" in got["legend"], got["legend"]
    assert got["ys"] == [], f"unknown Typical must not draw any line-scenario path: {got}"
