"""A chart kept after a failed reading draws nothing of the answer's moment as current, in either of its views.

The answer comes from the real collector and route path at the current time: synthetic accounts, eight hours of sweeps
(a: both windows rising, calibrated; b: no weekly window, left out of Combined; c: its weekly window spent). The selected
five-hour window has its coupled futures with three reported resets ahead; Combined has its figure now (0.50 of 2
counted), its conditional hours and its futures with their reset schedule. The widget draws that answer, then its own
poll reads nothing. On the kept screen, in the window's view and in Combined alike, the record stays and nothing of the
answer's moment is drawn as current: no future line, no reset ahead, no figure now, no count of whom it counts now, no
conditional hours. Nothing here reads a real account.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import plugin
import quota_history
import quota_summary as qs
import test_quotas
from test_reserve import FIVE_H, WEEK, _node, payload, profile, with_i18n

WIDGET = Path(os.environ.get("KEPT_CHART_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
H = 3600.0
STEP = 120.0
GROUP = "claude|five_hour|18000|-"


def answer_at(now, start):
    def window(cid, used, length, reset):
        return {"id": cid, "label": cid, "used_ratio": used, "window_seconds": length, "resets_at": qs.iso(reset)}

    def snap(sid, t, rows):
        return {"subject": {"harness": "claude", "subject_id": sid}, "constraints": rows,
                "availability": {"state": "available"}, "observed_at": qs.iso(t - 20), "freshness": "fresh",
                "source": "app"}

    def at(t):
        hours = (t - start) / H
        rows = [
            snap("a", t, [window("five_hour", 0.05 * hours, FIVE_H, now + 1 * H),
                          window("seven_day", 0.82 + 0.01 * hours, WEEK, now + 6 * 86400)]),
            snap("b", t, [window("five_hour", 0.3, FIVE_H, now + 2 * H)]),
            snap("c", t, [window("five_hour", 0.1, FIVE_H, now + 3 * H),
                          window("seven_day", 1.0, WEEK, now + 6 * 86400)]),
        ]
        return payload(rows, [profile("claude", s) for s in "abc"], harnesses=("claude",))
    return at


@pytest.fixture(scope="module")
def answer(tmp_path_factory):
    now = float(int(time.time()))
    start = now - 8 * H
    at = answer_at(now, start)
    store = quota_history.HistoryStore(tmp_path_factory.mktemp("kept-chart"))
    t = start
    while t <= now + 1e-6:
        plugin.persist_sweep(store, at(t), "", t)
        t += STEP
    status = at(now)
    view = plugin.build_view(status, "")
    view["reserve"] = plugin.reserve_view(store, status, now, now, harness="claude", group=GROUP, horizon="24h",
                                          name_accounts=True, analytics=True)
    analytics = view["reserve"]["chart"]["analytics"]
    # The answer is what the module note says (the backend tests hold its numbers).
    own, joint = analytics["window"], analytics["joint"]
    assert own["key"] == GROUP and own["scenarios"]["recent_pace"]["schedule"], own
    assert joint["available"] and joint["figure"] == pytest.approx(0.5, abs=1e-3)
    assert (joint["known"], joint["accounts"]) == (2, 3) and joint["hours"]["value"] > 0
    assert joint["scenarios"]["no_new_use"]["schedule"][0]["at"] == pytest.approx(now + H, abs=1)
    return {"now": now, "view": view}


@pytest.fixture(scope="module")
def fixture_file(answer, tmp_path_factory):
    path = tmp_path_factory.mktemp("kept-chart-fixture") / "view.json"
    path.write_text(json.dumps(answer["view"]), encoding="utf-8")
    return str(path)


NODE = r"""
const text = (n) => (n ? n.textContent : null);
const click = (env, key) => { const node = byFocus(env.root, key); assert.ok(node, 'missing control ' + key); node.listeners.click[0]({ stopPropagation() {} }); };
const block = (env) => classes(env.root, 'tl-block')[0];
const drawn = (env, cls) => walk(block(env)).filter((n) => n.getAttribute('class') === cls).length;
const first = (env, cls) => text(classes(block(env), cls)[0]);
const pick = async (env, joint) => {
  if ((byFocus(env.root, 'graph:joint').getAttribute('aria-pressed') === 'true') !== joint) click(env, 'graph:joint');
  await settle();
  assert.equal(byFocus(env.root, 'graph:joint').getAttribute('aria-pressed'), joint ? 'true' : 'false');
};
// Combined on the kept screen: its record stays, dated; nothing of the answer's moment is current.
function keptCombined(env) {
  assert.ok(first(env, 'tl-num').startsWith('—'), first(env, 'tl-num'));
  assert.match(first(env, 'tl-sub'), /^read \d\d:\d\d$/);
  assert.equal(first(env, 'tl-next-v'), '—');
  assert.equal(drawn(env, 'line-scenario'), 0, 'no future line');
  assert.equal(drawn(env, 'reset-dot'), 0, 'no reset ahead');
  assert.ok(drawn(env, 'line-observed') >= 1, 'the record stays');
  const note = classes(block(env), 'tl-joint')[0];
  assert.equal(text(walk(note).find((n) => n.tagName === 'B')), '—');
  assert.doesNotMatch(text(note), /account-hours|counted|Left out/);
  assert.match(text(note), /read \d\d:\d\d/);
  assert.equal(classes(block(env), 'schedule-card').length, 0, 'no reset schedule');
  assert.match(text(classes(block(env), 'calm-legend')[0]), /no future drawn/);
  assert.match(text(classes(block(env), 'chart-legend')[0]), /no future · nothing read since \d\d:\d\d/);
}
// The selected window's own view of the kept screen.
function keptWindow(env) {
  assert.equal(classes(block(env), 'tl-hero').length, 0);
  assert.equal(drawn(env, 'line-scenario'), 0, 'no future line');
  assert.equal(drawn(env, 'reset-dot'), 0, 'no reset ahead');
  assert.ok(drawn(env, 'line-observed') >= 1, 'the record stays');
  assert.equal(classes(block(env), 'schedule-card').length, 0, 'no reset schedule');
  assert.match(text(classes(block(env), 'calm-legend')[0]), /no future drawn/);
  assert.match(text(classes(block(env), 'chart-legend')[0]), /no future · nothing read since \d\d:\d\d/);
}
(async () => {
  const view = JSON.parse(require('fs').readFileSync(process.env.UI_FIXTURE_FILE, 'utf8'));
  const joint = process.env.KEPT_FIRST === 'joint';
  let fail = false;
  // Every read answers the view until the poll that fails: an answer that is not a view (the widget's "unreadable").
  const env = await boot(() => (fail ? null : view));
  await settle();
  // The owner picks the five-hour limit (the widget opens on the family's tightest one)
  // and switches to No new use so both window and Combined draw refill-only futures before failing.
  click(env, 'ring:claude:' + view.reserve.chart.group_key);
  await settle();
  click(env, 'scenario:no_new_use');
  await settle();

  // The answer as read: the window's futures with their resets, and Combined's figure, hours and next reset.
  assert.ok(drawn(env, 'line-scenario') >= 1 && drawn(env, 'reset-dot') >= 1, 'the window draws its future');
  await pick(env, true);
  assert.ok(first(env, 'tl-num').startsWith('0.50'), first(env, 'tl-num'));
  assert.match(first(env, 'tl-sub'), /2 of 3 accounts counted/);
  assert.notEqual(first(env, 'tl-next-v'), '—');
  assert.ok(drawn(env, 'line-scenario') >= 1 && drawn(env, 'reset-dot') >= 1, 'Combined draws its future');
  assert.match(text(classes(block(env), 'tl-joint')[0]), /account-hours/);
  assert.ok(classes(block(env), 'schedule-card').length, 'the reset schedule is in Details');
  await pick(env, joint);

  // The widget's own poll reads nothing: the screen is kept, in the view it stood in, then in the other one.
  fail = true;
  env.interval()();
  await settle();
  assert.match(env.root.textContent, /Reading could not be refreshed/);
  assert.match(env.root.textContent, /nothing in it is current/);
  assert.equal(byFocus(env.root, 'graph:joint').getAttribute('aria-pressed'), joint ? 'true' : 'false');
  (joint ? keptCombined : keptWindow)(env);
  await pick(env, !joint);
  (joint ? keptWindow : keptCombined)(env);
  env.disposeHooks[0]();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""


@pytest.mark.parametrize("first", ["window", "joint"])
def test_a_kept_chart_draws_nothing_current_in_either_view(fixture_file, first):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run([str(node), "-e", harness + NODE], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": str(WIDGET), "UI_FIXTURE_FILE": fixture_file,
                                 "KEPT_FIRST": first},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The same in real engines: the widget's own read fails (the page comes back into view and reads nothing).

pytest.importorskip("playwright.sync_api")

STUB = with_i18n(r"""
(view) => {
  window.__failRead = false;
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    if (!post && window.__failRead) return Promise.resolve({ ok: false, status: 503, text: async () => 'unavailable' });
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true, quota_updates: [] } : view) });
  };
}
""")

SEEN = """() => {
  const block = document.querySelector('.tl-block');
  const count = (sel) => block.querySelectorAll(sel).length;
  const one = (sel) => { const n = block.querySelector(sel); return n ? n.textContent : null; };
  const joint = document.querySelector('[data-focus="graph:joint"]');
  return { pressed: joint ? joint.getAttribute('aria-pressed') : null, num: one('.tl-num'), sub: one('.tl-now .tl-sub'),
           next: one('.tl-next-v'), future: count('path.line-scenario'), resets: count('circle.reset-dot'),
           record: count('path.line-observed'), schedule: count('.schedule-card'), note: one('.tl-joint'),
           calm: one('.calm-legend') };
}"""


def _shot(page, browser, name):
    evidence = os.environ.get("QUOTAS_EVIDENCE_DIR")
    if evidence:
        page.locator(".tl-block").first.screenshot(path=str(Path(evidence) / f"{browser.browser_type.name}-{name}.png"))


def _kept_combined(seen):
    assert seen["pressed"] == "true", seen
    assert seen["num"].startswith("—") and re.fullmatch(r"read \d\d:\d\d", seen["sub"]) and seen["next"] == "—", seen
    assert seen["future"] == 0 and seen["resets"] == 0 and seen["schedule"] == 0 and seen["record"] >= 1, seen
    assert "account-hours" not in seen["note"] and "counted" not in seen["note"], seen["note"]
    assert "no future drawn" in seen["calm"], seen["calm"]


def _kept_window(seen):
    assert seen["pressed"] == "false" and seen["num"] is None, seen
    assert seen["future"] == 0 and seen["resets"] == 0 and seen["schedule"] == 0 and seen["record"] >= 1, seen
    assert "no future drawn" in seen["calm"], seen["calm"]


def _pick(page, joint):
    if (page.get_attribute('[data-focus="graph:joint"]', "aria-pressed") == "true") != joint:
        page.click('[data-focus="graph:joint"]')
    want = "true" if joint else "false"
    page.wait_for_function("(want) => document.querySelector('[data-focus=\"graph:joint\"]').getAttribute('aria-pressed') === want", arg=want)


@pytest.mark.parametrize("first", ["window", "joint"])
def test_a_real_browser_keeps_both_views_of_the_chart_without_anything_current(browser, answer, first):
    joint = first == "joint"
    page = browser.new_page(viewport={"width": 1120, "height": 1500})
    try:
        page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
        page.evaluate(STUB, answer["view"])
        page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
        page.click(f'[data-focus="ring:claude:{GROUP}"]')
        page.click('[data-focus="scenario:no_new_use"]')
        page.wait_for_selector(".tl-block .chart-svg path.line-scenario")
        _pick(page, True)
        page.wait_for_selector(".tl-block .tl-hero")
        before = page.evaluate(SEEN)
        assert before["num"].startswith("0.50") and before["next"] != "—" and before["future"] >= 1, before
        if joint:
            _shot(page, browser, "kept-chart-combined-read")
        _pick(page, joint)
        page.evaluate("() => { window.__failRead = true; document.dispatchEvent(new Event('visibilitychange')); }")
        page.wait_for_function("() => /Reading could not be refreshed/.test(document.body.textContent)")
        (_kept_combined if joint else _kept_window)(page.evaluate(SEEN))
        _shot(page, browser, f"kept-chart-{first}-kept")
        _pick(page, not joint)
        (_kept_window if joint else _kept_combined)(page.evaluate(SEEN))
    finally:
        page.close()
