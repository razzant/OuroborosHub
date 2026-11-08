"""The head's family tabs (kit: mark tab), as the owner's approved composition draws them (8 Nov 2026): one capsule per family, the vendor's
own mark, the name and the count of accounts, in one fixed order (the order the families were first seen in: a pick moves no tab), the picked one
on the crimson ground of the plate (--q-sel, the colour of the picked ring and the opened account's card), the others quiet; on the narrow shell
(520px and under) a tab not picked folds its name and keeps its mark and its count, so the four families and the words at the right stand on one
line where the demo had them. No slide, no growth, no start state: the one motion is the fade of ground and ink, none under reduced motion.

The look is the kit's (kit: mark tab) with a usage contract; markTab and markRow are the only builders of a tab and a row; the head adds its wiring
(label, title, focus key, click) and its place (.harness-seg). The rules are read from the STYLE array; the markup runs in the widget's own fake-DOM
run; what a browser really does (the ground, the folded names, one line) is checked in Chromium and WebKit."""
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_quotas
from test_order_focus import REAL_PAGE_STUB
from test_reserve import _node, with_i18n
from test_style_canons import _declarations, _rules, _sections, _selectors, _style, _tokens
from test_text_actions import _css_rgb, _themes

WIDGET = Path(__file__).with_name("widget.js")
KIT = "kit: mark tab"
HEAD = "capsule: head"
CLASSES = ["tab-mark", "tab-mark-count", "tab-mark-icon", "tab-mark-name", "tab-mark-row"]


def _kit_rules():
    """[(media, selector, {prop: value})] of the kit's rules, one entry per selector."""
    out = []
    for media, listing, body in _rules(_sections(_style())[KIT]):
        decls = {p.strip(): v.strip() for p, v in _declarations(body)}
        out.extend((media, selector, decls) for selector in _selectors(listing))
    return out


def _one(selector, media=""):
    found = [d for m, s, d in _kit_rules() if s == selector and m == media]
    assert len(found) == 1, "%s under %r: %d rules" % (selector, media, len(found))
    return found[0]


def _contract():
    lines = _style().splitlines()
    first = next(i for i, line in enumerate(lines) if line.strip() == "/* %s */" % KIT) + 1
    out = []
    for line in lines[first:]:
        if not line.strip().startswith("//"):
            break
        out.append(line.strip()[2:].strip())
    return " ".join(out)


def _function(source, name):
    found = re.search(r"\n    function %s\(.*?\n    }\n" % name, source, re.S)
    assert found, "function %s is not in widget.js" % name
    return found.group(0)


# ---------------------------------------------------------------------------
# The sheet: a capsule tab, the crimson pick, the folded name, no motion but the fade.

def test_the_section_opens_with_its_contract_and_names_every_token_its_rules_read():
    contract = _contract()
    parts = ["What it is:", "When to use it:", "How to use it:", "States:", "Motion:", "Tokens it reads:", "Accessibility:", "Not to do:"]
    at = [contract.find(part) for part in parts]
    assert all(i >= 0 for i in at) and at == sorted(at), dict(zip(parts, at))
    named = set(re.findall(r"--[a-z0-9-]+", contract.split("Tokens it reads:")[1].split("Accessibility:")[0]))
    read = {token for _, _, decls in _kit_rules() for value in decls.values() for token in re.findall(r"var\((--[a-z0-9-]+)", value)}
    assert read <= named, sorted(read - named)
    assert named <= set(_tokens(_sections(_style())))


def test_a_tab_is_a_capsule_quiet_at_rest_crimson_when_picked_and_its_name_folds_on_the_narrow_shell():
    base = _one(".tab-mark")
    assert base["border"] == "0" and base["background"] == "none" and base["border-radius"] == "var(--radius-pill)"
    assert base["color"] == "var(--text-secondary)" and base["font-size"] == "var(--type-meta)" and base["font-weight"] == "var(--weight-regular)"
    assert base["white-space"] == "nowrap" and base["min-height"] == "var(--row-h)"
    picked = _one(".tab-mark[aria-pressed=true]")
    assert picked == {"background": "var(--q-sel)", "color": "var(--q-sel-ink)", "font-weight": "var(--weight-semibold)"}, picked
    hover = _one(".tab-mark:is(:hover,:focus-visible):not([aria-pressed=true]):not(:disabled)")
    assert hover == {"background": "var(--q-capsule-hover)", "color": "var(--text-primary)"}, hover
    count = _one(".tab-mark-count")
    assert count["color"] == "var(--text-meta)" and "tabular-nums" in count["font-variant-numeric"]
    assert _one(".tab-mark[aria-pressed=true] .tab-mark-count")["color"] == "var(--q-sel-meta)"
    narrow = {s: d for m, s, d in _kit_rules() if m == "@media(max-width:520.98px)"}
    assert narrow[".tab-mark:not([aria-pressed=true]) .tab-mark-name"] == {"display": "none"}, narrow
    assert ".tab-mark" in narrow and ".tab-mark[aria-pressed=true]" in narrow, "the folded tab is tighter, the picked one keeps its air"
    # No rule of the kit draws a shadow, a border, an outline or a transform; the one transition is the tab's, with its reduced pair.
    for media, selector, decls in _kit_rules():
        assert not [p for p in decls if re.match(r"^(box-shadow|outline.*|transform|animation.*)$", p) or (p.startswith("border") and p != "border-radius" and decls[p] != "0")], (selector, decls)
    assert _one(".tab-mark", "@media(prefers-reduced-motion:reduce)") == {"transition": "none"}
    assert "transition" in _one(".tab-mark") and "background-color" in _one(".tab-mark")["transition"]
    # The head keeps only the row's place.
    head = {s: d for m, s, d in ((m, s, {p.strip(): v.strip() for p, v in _declarations(b)}) for m, s0, b in _rules(_sections(_style())[HEAD]) for s in _selectors(s0)) if "harness" in s}
    assert head[".harness-seg"] == {"flex": "0 1 auto", "min-width": "0"}, head


def test_markTab_and_markRow_are_the_only_builders_and_the_order_is_the_widgets_memory():
    source, css = WIDGET.read_text(encoding="utf-8"), _style()
    code = "\n".join(line for line in source.replace(css, "").splitlines() if not line.strip().startswith("//"))
    assert sorted(set(re.findall(r"tab-mark[\w-]*", code))) == CLASSES
    for name in ("markTab", "markRow"):
        assert source.count("function %s(" % name) == 1, name
    rest = code.replace(_function(code, "markTab"), "").replace(_function(code, "markRow"), "")
    assert rest.count("tab-mark") == 0 and rest.count("data-mark-key") == 0
    assert code.count("el('button', 'tab-mark')") == 1 and code.count("el('div', 'tab-mark-row')") == 1
    assert "tab-mark-dot" not in source, "no dot between the name and the count"
    tab = _function(code, "familyTab")
    for wiring in ("setAttribute('aria-label', say)", "setAttribute('data-focus', 'harness:' + group.harness_id)", "btn.title = say", "addEventListener('click'"):
        assert wiring in tab, wiring
    assert "markTab({ key: group.harness_id, mark: familyMark(group)" in tab and "pressed: isOn })" in tab
    # The order: first seen, first; one variable, written by orderedFamilies alone, kept nowhere; a pick moves no tab.
    order = _function(code, "orderedFamilies")
    assert not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie|fetch\(|postMessage|unshift|splice", order), order
    assert code.count("var familyOrder = [];") == 1 and code.replace(order, "").count("familyOrder") == 1
    assert "markRow(orderedFamilies(groups).map(" in _function(code, "renderHarnessSeg")
    assert "tab-mark" not in _function(code, "limitRail"), "the rings are no tabs of the kit"


# ---------------------------------------------------------------------------
# The markup, in the widget's own fake-DOM run.

NODE_HEAD = r"""
const has = (n, c) => String(n.className || '').split(/\s+/).includes(c);
const click = (env, key) => byFocus(env.root, key).listeners.click[0]({ stopPropagation() {} });
const tabs = (env) => classes(env.root, 'tab-mark');
const ids = (env) => tabs(env).map((t) => t.getAttribute('data-mark-key'));
const settle2 = async () => { for (let i = 0; i < 6; i++) await settle(); };
(async () => {
  const seen = JSON.parse(process.env.TABS_VIEW);
  const env = await boot(seen);
  // One tab per family, in the order of the answer: the mark (the kit's class on the head's drawing), the name, the count; no dot.
  assert.deepEqual(ids(env), ['claude', 'codex', 'openrouter']);
  for (const tab of tabs(env)) {
    assert.equal(tab.tagName, 'BUTTON');
    assert.equal(tab.getAttribute('type'), 'button');
    // the mark (the vendor's drawing, or the initial where the widget has none) wears the kit's class; then the name; then the count, where there is one
    assert.deepEqual(tab.childNodes.map((n) => has(n, 'tab-mark-icon') ? 'mark' : String(n.className || '').split(/\s+/)[0]), has(tab, 'empty') ? ['mark', 'tab-mark-name'] : ['mark', 'tab-mark-name', 'tab-mark-count']);
    assert.ok(tab.childNodes[0].tagName === 'SVG' || has(tab.childNodes[0], 'harness-initial'), 'the mark is a drawing or the initial');
    assert.ok(tab.getAttribute('aria-label'), 'the label says what the count counts');
    assert.equal(classes(tab, 'tab-mark-dot').length, 0);
  }
  assert.deepEqual(tabs(env).map((t) => t.getAttribute('aria-pressed')), ['true', 'false', 'false']);
  assert.deepEqual(classes(tabs(env)[1], 'tab-mark-count').map((n) => n.textContent), ['2'], 'the count is the accounts switched on');
  const row = classes(env.root, 'harness-seg')[0];
  assert.ok(has(row, 'tab-mark-row') && row.getAttribute('role') === 'group' && row.getAttribute('aria-label'));
  // A pick: the pressed tab changes, the order does not.
  click(env, 'harness:codex');
  await settle2();
  assert.deepEqual(ids(env), ['claude', 'codex', 'openrouter'], 'a pick moves no tab');
  assert.deepEqual(tabs(env).map((t) => t.getAttribute('aria-pressed')), ['false', 'true', 'false']);
  click(env, 'harness:openrouter');
  await settle2();
  assert.deepEqual(ids(env), ['claude', 'codex', 'openrouter']);
  assert.deepEqual(tabs(env).map((t) => t.getAttribute('aria-pressed')), ['false', 'false', 'true']);
  env.disposeHooks[0]();
  // Before the first answer: three ghosts, tabs of the kit, disabled and no choice.
  const waiting = await boot({ ok: true, transport_error: '', facets: {}, groups: [] });
  const ghosts = tabs(waiting);
  assert.equal(ghosts.length, 3);
  assert.ok(ghosts.every((g) => g.disabled === true && g.getAttribute('aria-pressed') === null));
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _account(family, subject, enabled=True):
    now = datetime.now(timezone.utc)
    return {"key": "%s:%s" % (family, subject), "kind": "profile", "subject_id": subject, "label": "Same label", "caption": "oauth",
            "email": "named@example.com", "plan": "Pro", "enabled": enabled, "signed_in": True, "next_up": False,
            "last_verified_at": _iso(now - timedelta(hours=1)), "verification_state": "passed", "verification_source": "vendor",
            "verified_live": True, "verification": {"tone": "ok", "label": "Verified live"}, "detail": "",
            "quota": {"state": "ok", "label": "30% used", "resets_at": "", "note": "",
                      "constraints": [{"id": "weekly", "label": "Weekly", "used_pct": 30, "resets_at": "", "cooldown_until": "",
                                       "scoped_models": [], "window_seconds": 604800}],
                      "stale": [], "availability": "available", "observed_at": _iso(now - timedelta(minutes=2)), "absence": None}}


def _family(harness, label, accounts):
    return {"harness_id": harness, "family_label": label, "harness_status": "ok", "harness_enabled": True, "provider_family": "",
            "catalog_known": True, "accounts": accounts, "accounts_signed_in": len(accounts), "accounts_unavailable": False}


@pytest.fixture(scope="module")
def tabs_view():
    return {"ok": True, "transport_error": "", "facets": {"catalog": "ok", "accounts": "ok", "quota": "ok"}, "facet_note": "",
            "daemon": {"state": "running", "engine_version": "3.9.4"},
            "groups": [_family("claude", "Claude", [_account("claude", "p1"), _account("claude", "p2")]),
                       _family("codex", "Codex", [_account("codex", "c1"), _account("codex", "c2"), _account("codex", "c3", False)]),
                       _family("openrouter", "OpenRouter", [])]}


def test_the_head_draws_one_tab_per_family_in_a_fixed_order_and_a_pick_moves_none(tabs_view):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run([str(node), "-e", harness + NODE_HEAD], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": str(WIDGET.resolve()), "TABS_VIEW": json.dumps(tabs_view)},
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok"), result.stdout


# ---------------------------------------------------------------------------
# Real browsers: the ground of the pick, the folded names, one line.

def _open(playwright, engine, view, width, **context):
    try:
        browser = getattr(playwright, engine).launch()
    except Exception as exc:  # no browser build for this Playwright here
        pytest.skip("no %s for Playwright on this machine (%s)" % (engine, type(exc).__name__))
    page = browser.new_context(viewport={"width": width, "height": 700}, **context).new_page()
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(with_i18n(REAL_PAGE_STUB), view)
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return browser, page


SNAP = """() => {
  const head = document.querySelector('.control-bar');
  const tabs = [...document.querySelectorAll('.tab-mark')];
  const rows = new Set([...head.querySelectorAll('button')].map((b) => Math.round(b.getBoundingClientRect().top + b.getBoundingClientRect().height / 2)));
  return { order: tabs.map((t) => t.getAttribute('data-mark-key')), rows: rows.size, headHeight: head.getBoundingClientRect().height,
    scrollX: document.documentElement.scrollWidth - innerWidth,
    tabs: Object.fromEntries(tabs.map((t) => { const s = getComputedStyle(t); const name = t.querySelector('.tab-mark-name'); const icon = t.querySelector('.tab-mark-icon');
      return [t.getAttribute('data-mark-key'), { pressed: t.getAttribute('aria-pressed'), background: s.backgroundColor, color: s.color, weight: s.fontWeight,
        shadow: s.boxShadow, border: s.borderTopWidth, radius: s.borderTopLeftRadius, nameShown: getComputedStyle(name).display !== 'none',
        iconTransform: getComputedStyle(icon).transform, height: t.getBoundingClientRect().height }]; })) };
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [600, 390])
def test_in_a_real_browser_the_picked_tab_is_crimson_the_others_quiet_and_the_row_is_one_line(tabs_view, engine, width):
    sync_api = pytest.importorskip("playwright.sync_api")
    dark, _ = _themes()
    crimson = _css_rgb(dark["--q-sel"])
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, tabs_view, width)
        try:
            got = page.evaluate(SNAP)
            assert got["order"] == ["claude", "codex", "openrouter"] and got["scrollX"] <= 0, got
            assert got["rows"] == 1 and got["headHeight"] < 40, ("the families, About and Refresh stand on one line", got)
            picked, quiet = got["tabs"]["claude"], got["tabs"]["codex"]
            assert picked["pressed"] == "true" and picked["background"] == crimson and picked["weight"] in ("600", "bold"), picked
            assert quiet["pressed"] == "false" and quiet["background"] in ("rgba(0, 0, 0, 0)", "transparent"), quiet
            for tab in got["tabs"].values():
                assert tab["shadow"] == "none" and tab["border"] == "0px" and tab["iconTransform"] == "none", tab
                assert tab["height"] >= 31, tab
            # The names: every one at the card's width; on the narrow shell the picked one alone.
            shown = [k for k, t in got["tabs"].items() if t["nameShown"]]
            assert shown == (["claude", "codex", "openrouter"] if width == 600 else ["claude"]), shown
            # A pick: the ground moves to the tab picked, the order stays, the folded names follow the pick.
            page.click('[data-focus="harness:codex"]')
            page.wait_for_function("document.querySelector('[data-focus=\"harness:codex\"]').getAttribute('aria-pressed') === 'true'")
            page.wait_for_timeout(250)
            after = page.evaluate(SNAP)
            assert after["order"] == got["order"] and after["rows"] == 1, after
            assert after["tabs"]["codex"]["background"] == crimson and after["tabs"]["claude"]["background"] in ("rgba(0, 0, 0, 0)", "transparent"), after
            assert [k for k, t in after["tabs"].items() if t["nameShown"]] == (["claude", "codex", "openrouter"] if width == 600 else ["codex"]), after
        finally:
            browser.close()
