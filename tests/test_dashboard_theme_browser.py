"""Real opaque-frame appearance and legacy fallback acceptance.

Opt in with OUROBOROS_THEME_CORE_ROOT pointing to a core checkout containing the
merged theme bridge and the historical ref below, then run this file explicitly.
The host uses the real mount, document CSP, fetch relay and disposal handshake.
Only skill API responses are synthetic. No installed runtime is read or changed.
OUROBOROS_THEME_EVIDENCE_DIR optionally retains screenshots.
This harness mounts classic widget.js entries. memory-atlas is an ES module entry
that only PluginAPI 2.1 hosts load; tests/memory_atlas checks its theme in a real host.
"""
from __future__ import annotations

import importlib.util
import difflib
import json
import mimetypes
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGACY_REF = "3e71431e4fa790d7bea6169a83ad572bf7eb6f20"
READY = {
    "cache_efficiency_snapshot": ".kpi:nth-child(2) b",
    "claudexor_quotas": ".q-plate",
    "context-lens": ".row",
    "token-usage": ".to-total",
}
FOCUS = {
    "cache_efficiency_snapshot": '[data-chart="volume"] [data-pick]',
    'claudexor_quotas': '[data-focus="accounts"]',
    "context-lens": "select",
    "token-usage": "select",
}
HOST = """<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0}iframe{width:100%;height:850px;border:0}section{width:100%}</style>
</head><body><section data-widget-key="qa"><div class="widgets-card-status"></div>
<div id="mount"></div></section><script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
const p=new URLSearchParams(location.search); ouroTheme.set(p.get('theme'));
window.handlers=new Set();
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
  {skill:p.get('skill'),ws_prefix:'ext:'+p.get('skill')+':'},
  {entry:'widget.js',height:850,appearance:'host'},null,window.handlers);
window.ready=true;
</script></body></html>"""
LISTENERS = """(()=>{
 window.qaThemeListeners=new Set();
 const add=EventTarget.prototype.addEventListener,remove=EventTarget.prototype.removeEventListener;
 window.addEventListener=function(type,fn,options){
   if(type==='ouro:theme-changed')qaThemeListeners.add(fn);
   return add.call(this,type,fn,options);
 };
 window.removeEventListener=function(type,fn,options){
   if(type==='ouro:theme-changed')qaThemeListeners.delete(fn);
   return remove.call(this,type,fn,options);
 };
})();"""


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def theme_server():
    configured = os.environ.get("OUROBOROS_THEME_CORE_ROOT")
    if not configured:
        pytest.skip("set OUROBOROS_THEME_CORE_ROOT to the core theme-bridge checkout")
    core = Path(configured).resolve()
    assert (core / "web/modules/widget_module.js").is_file()
    responses = json.loads((ROOT / "tests/fixtures/dashboard_theme_responses.json").read_text(encoding="utf-8"))
    # The current quotas widget consumes a reserve projection as well as accounts.
    accounts = responses["quota"]["groups"][0]["accounts"]
    observed = datetime.now(timezone.utc).isoformat()
    ends = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    bars = [{"account": a["key"], "state": "current", "left": .7,
             "at_limit": False, "restricted": False, "flags": [],
             "observed_at": observed, "resets_at": ends} for a in accounts]
    group = {"key": "claude|weekly|604800|-", "harness": "claude", "family": "Claude",
             "label": "Weekly", "meaning": "weekly", "window_seconds": 604800,
             "models": [], "model_scope": "none", "bars": bars, "slots": len(bars),
             "measured": {"accounts": len(bars), "windows": .7 * len(bars), "average_remaining_pct": 70},
             "observed": {"oldest_at": observed, "newest_at": observed},
             "last_known": {"accounts": 0, "windows": 0}, "unknown": {"accounts": 0},
             "coverage": {"measured": len(bars)}, "restrictions": {}}
    responses["quota"]["reserve"] = {"summary": {"schema": 1, "groups": [group],
        "reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"}, "roster": "current",
        "status_read_at": observed, "generated_at": observed}, "chart": None}
    cache = _load("_theme_cache_plugin", ROOT / "skills/cache_efficiency_snapshot/plugin.py")
    accounting = _load("_theme_token_accounting", ROOT / "skills/token-usage/accounting.py")
    snapshot = json.loads((ROOT / "skills/token-usage/fixtures/numeric_snapshot.json").read_text(encoding="utf-8"))
    now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
    cache_data = cache.calculate_analytics([
        cache._normalize_record({
            "state": "settled", "kind": "attempt", "provider": "test", "model": f"model-{i % 2}",
            "ts": (now - timedelta(minutes=120-i*10)).isoformat(),
            "prompt_tokens": 1000+i*100, "cached_tokens": 500+i*60, "cache_write_tokens": 0,
        }) for i in range(12)
    ], now=now)
    unmatched = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content_type="application/json", status=200):
            if not isinstance(body, bytes):
                body = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            query = {key: value[-1] for key, value in parse_qs(url.query).items()}
            if path == "/":
                return self.send(HOST, "text/html")
            if path.startswith("/static/"):
                source = core / "web" / path[len("/static/"):]
                if source.is_file():
                    return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or "text/javascript")
            if path.startswith("/api/extensions/"):
                parts = path.split("/")
                skill, tail = parts[3], "/" + "/".join(parts[4:])
                if tail == "/module/widget.js":
                    return self.send((ROOT / "skills" / skill / "widget.js").read_bytes(), "text/javascript")
                if skill == "cache_efficiency_snapshot" and tail == "/data":
                    return self.send(cache_data)
                elif skill == "claudexor_quotas" and tail == "/quotas":
                    return self.send(responses["quota"])
                elif skill == "claudexor_quotas" and tail == "/i18n":
                    return self.send({"language": "", "revision": 0, "strings": {}, "en": json.loads(
                        (ROOT / "skills/claudexor_quotas/strings_en.json").read_text())})
                elif skill == "claudexor_quotas" and tail == "/preferences":
                    return self.send({"ok": True, "prefs": {}})
                elif skill == "context-lens" and tail in ("/data", "/trajectory"):
                    return self.send(responses["lens"][tail[1:]])
                elif skill == "token-usage" and tail == "/preferences":
                    return self.send({"preferences": {}})
                elif skill == "token-usage" and tail == "/data":
                    return self.send(accounting.query_snapshot(
                        snapshot, query, now=datetime(2026, 9, 10, 10, tzinfo=timezone.utc)))
            unmatched.append((path, query))
            return self.send({"error": "unmatched fixture " + path}, status=404)

        do_PUT = do_GET

        def do_POST(self):
            # Current classic mounts request a route lease before constructing
            # the opaque frame. This fixture supplies the host response only;
            # the real module still owns fetch relay, CSP and lease disposal.
            path = urlsplit(self.path).path
            if path.startswith("/api/widgets/asset-leases"):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                if path == "/api/widgets/asset-leases":
                    assert payload.get("skill") in READY and payload.get("scope") == "routes"
                    return self.send({"token": "theme-fixture", "base_url": "/api/widget-resources/theme-fixture/",
                                      "renew_after": 3600, "expires_in": 7200})
                if path == "/api/widgets/asset-leases/revoke":
                    assert payload.get("token") == "theme-fixture"
                    return self.send({"ok": True})
            return self.do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}", "core": core, "unmatched": unmatched}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request, theme_server):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as api:
        instance = getattr(api, request.param).launch(headless=True)
        yield instance, request.param
        instance.close()


def _exercise_selection(frame, skill):
    if skill == "cache_efficiency_snapshot":
        frame.wait_for_function('() => document.querySelector(".kpi:nth-child(2) b")?.textContent.includes("%")')
        frame.locator('[data-chart="volume"] [data-pick]').first.press("ArrowRight")
    elif skill == "claudexor_quotas":
        frame.locator('[data-focus="accounts"]').click()
        # Accounts intentionally folds the plate. Capture the theme baseline
        # only once that transition has ended, not during its two-frame start.
        frame.wait_for_function("""() => {
          const fold = document.querySelector('.acc-main');
          return fold && fold.classList.contains('acc-shut') &&
            !document.querySelector('.acc-run') && fold.getBoundingClientRect().height <= 1;
        }""")
    elif skill == "context-lens":
        frame.locator('[data-focus="filter-model"]').select_option("vendor/model-a")
        frame.locator(".row").first.click()
        frame.wait_for_function('() => document.querySelectorAll("canvas").length === 2')
    elif skill == "token-usage":
        frame.locator('select[data-pref="model"]').select_option("native-test")
        frame.wait_for_function('() => !document.querySelector(".to-content").matches("[aria-busy=true]")')
        frame.locator("details").first.evaluate("node => node.open=true")


@pytest.mark.parametrize("skill", READY)
@pytest.mark.parametrize("legacy", [False, True], ids=["current", "pre-onTheme"])
@pytest.mark.parametrize("initial", ["light", "dark"])
def test_dashboard_theme_keeps_live_state_and_legacy_fallback(theme_server, browser, skill, legacy, initial, tmp_path):
    instance, engine = browser
    context = instance.new_context(viewport={"width": 1100, "height": 850})
    page = context.new_page()
    page.emulate_media(color_scheme="dark")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(LISTENERS)
    if legacy:
        for name in ("widget_module.js", "widget_frame.js"):
            source = subprocess.check_output(
                ["git", "show", f"{LEGACY_REF}:web/modules/{name}"], cwd=theme_server["core"])
            page.route("**/static/modules/" + name,
                       lambda route, request, source=source: route.fulfill(body=source, content_type="text/javascript"))
    evidence = Path(os.environ.get("OUROBOROS_THEME_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        page.goto(theme_server["url"] + f"/?skill={skill}&theme={initial}")
        page.wait_for_function("window.ready === true")
        node = page.locator("iframe").element_handle()
        frame = node.content_frame()
        frame.wait_for_selector(READY[skill])
        assert frame.evaluate("typeof OuroborosWidget.onTheme") == ("undefined" if legacy else "function")
        assert frame.evaluate("getComputedStyle(document.documentElement).colorScheme") == ("dark" if legacy else initial)
        _exercise_selection(frame, skill)
        # Two animation frames let the canvases finish the render scheduled by selection.
        frame.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        frame.locator(FOCUS[skill]).first.focus()
        frame.evaluate("""() => {
          window.qaRoot=document.getElementById('root');
          window.qaCanvases=Array.from(document.querySelectorAll('canvas'));
          window.qaSvg=Array.from(document.querySelectorAll('svg'));
          window.qaText=document.body.innerText;
          window.qaFocus=document.activeElement; window.qaValue=qaFocus.value;
          window.qaOpen=Array.from(document.querySelectorAll('details')).map(e=>e.open);
        }""")
        before = frame.evaluate("qaCanvases.map(c=>c.toDataURL())")
        svg_colors = "qaSvg.flatMap(svg=>Array.from(svg.querySelectorAll('*')).map(node=>{const s=getComputedStyle(node);return [s.fill,s.stroke,s.color]}))"
        svg_before = frame.evaluate(svg_colors)
        page.screenshot(path=str(evidence / f"{engine}-{skill}-{legacy}-{initial}.png"))
        other = "dark" if initial == "light" else "light"
        page.evaluate("theme => ouroTheme.set(theme)", other)
        if not legacy:
            frame.wait_for_function("theme => document.documentElement.dataset.theme === theme", arg=other)
        frame.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        assert page.evaluate("node => document.querySelector('iframe') === node", node)
        assert frame.evaluate("document.getElementById('root')===qaRoot && qaCanvases.every(c=>c.isConnected) && qaSvg.every(s=>s.isConnected)")
        assert frame.evaluate("document.activeElement===qaFocus && document.activeElement.value===qaValue")
        texts = frame.evaluate("({before:qaText,after:document.body.innerText})")
        (evidence / f"{engine}-{skill}-{legacy}-{initial}-text.json").write_text(json.dumps(texts, ensure_ascii=False))
        assert texts['before'] == texts['after'], '\n'.join(difflib.unified_diff(texts['before'].splitlines(), texts['after'].splitlines(), fromfile='before', tofile='after'))
        assert frame.evaluate("JSON.stringify(Array.from(document.querySelectorAll('details')).map(e=>e.open))===JSON.stringify(qaOpen)")
        assert frame.evaluate("getComputedStyle(document.documentElement).colorScheme") == ("dark" if legacy else other)
        after = frame.evaluate("qaCanvases.map(c=>c.toDataURL())")
        if svg_before:
            assert (svg_before == frame.evaluate(svg_colors)) == legacy, "SVG paints must follow the host theme without node replacement"
        if before:
            assert (before == after) == legacy, "canvas must repaint only when receiving a host theme"
        assert page.evaluate("qaThemeListeners.size") == (0 if legacy else 1)
        page.screenshot(path=str(evidence / f"{engine}-{skill}-{legacy}-{other}-toggle.png"))
        page.evaluate("async () => await disposeWidget()")
        page.wait_for_function("document.querySelector('iframe') === null")
        assert page.evaluate("qaThemeListeners.size") == 0
        assert errors == []
        assert theme_server["unmatched"] == []
    finally:
        context.close()


@pytest.mark.parametrize("mode", ["trend", "volume"])
@pytest.mark.parametrize("scale", [1, 2])
def test_retained_cache_svg_follows_theme_while_hidden(theme_server, browser, mode, scale):
    """Returning to a retained graph must match an ordinary visible repaint."""
    instance, _ = browser
    context = instance.new_context(viewport={"width": 1100, "height": 850}, device_scale_factor=scale)
    page = context.new_page()
    try:
        page.goto(theme_server["url"] + "/?skill=cache_efficiency_snapshot&theme=dark")
        page.wait_for_function("() => window.ready === true")
        node = page.locator("iframe").element_handle()
        frame = node.content_frame()
        frame.wait_for_function('() => document.querySelector(".kpi:nth-child(2) b")?.textContent.includes("%")')
        selector = '[data-chart="' + ('rate' if mode == 'trend' else 'volume') + '"] svg'
        page.mouse.move(0, 0)
        frame.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")

        def theme(value):
            page.evaluate("t => ouroTheme.set(t)", value)
            frame.wait_for_function("t => document.documentElement.dataset.theme === t", arg=value, polling=25)

        pixels = "JSON.stringify([...document.querySelector('" + selector + "').querySelectorAll('*')].map(n=>[n.outerHTML,getComputedStyle(n).fill,getComputedStyle(n).stroke]))"
        theme("light")
        reference = frame.evaluate(pixels)
        theme("dark")
        page.locator("section[data-widget-key]").evaluate('n => n.style.display = "none"')
        theme("light")
        page.locator("section[data-widget-key]").evaluate('n => n.style.display = ""')
        frame.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        assert page.evaluate('n => document.querySelector("iframe") === n', node)
        same_paint = frame.evaluate(pixels) == reference
        assert same_paint, "hidden theme change must match visible SVG geometry and colors"
        page.evaluate("async () => await disposeWidget()")
    finally:
        context.close()


@pytest.mark.parametrize("mode", ["trend", "volume"])
@pytest.mark.parametrize("initial_scale,new_scale", [(1, 2), (2, 1)])
def test_cache_svg_preserves_logical_geometry_across_device_scale(theme_server, browser, mode, initial_scale, new_scale):
    """SVG no longer has a backing canvas; theme and logical geometry survive DPR changes."""
    instance, engine = browser
    if engine != "chromium":
        pytest.skip("runtime DPR emulation uses Chromium CDP")
    context = instance.new_context(viewport={"width": 1100, "height": 850}, device_scale_factor=initial_scale)
    page = context.new_page()
    try:
        page.goto(theme_server["url"] + "/?skill=cache_efficiency_snapshot&theme=light")
        page.wait_for_function("() => window.ready === true")
        frame = page.locator("iframe").element_handle().content_frame()
        frame.wait_for_function('() => document.querySelector(".kpi:nth-child(2) b")?.textContent.includes("%")')
        selector = '[data-chart="' + ('rate' if mode == 'trend' else 'volume') + '"] svg'
        page.mouse.move(0, 0)
        frame.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        pixels = "JSON.stringify([...document.querySelector('" + selector + "').querySelectorAll('*')].map(n=>[n.outerHTML,getComputedStyle(n).fill,getComputedStyle(n).stroke]))"
        reference = frame.evaluate(pixels)
        frame.evaluate("() => {window.qaResizeCount=0; window.addEventListener('resize', () => qaResizeCount++);}")
        cdp = context.new_cdp_session(page)
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": 1100, "height": 850, "deviceScaleFactor": new_scale, "mobile": False,
        })
        frame.wait_for_function("value => devicePixelRatio === value", arg=new_scale)
        assert frame.evaluate("qaResizeCount") == 0
        # A repaint at the same theme isolates backing-scale consistency from colour changes.
        page.evaluate("() => ouroTheme.set('dark')")
        frame.wait_for_function("() => document.documentElement.dataset.theme === 'dark'")
        page.evaluate("() => ouroTheme.set('light')")
        frame.wait_for_function("() => document.documentElement.dataset.theme === 'light'")
        same_paint = frame.evaluate(pixels) == reference
        assert same_paint, "a DPR-only change must preserve the logical paint geometry"
        page.evaluate("async () => await disposeWidget()")
    finally:
        context.close()
