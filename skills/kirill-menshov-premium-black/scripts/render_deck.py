"""
Premium Black: render every .slide of a built deck HTML to PNG (1920x1080 * scale).

Python port of the original render_deck.js: the same page code measures text geometry, the
same two screenshots per slide feed the PPTX. The browser is Playwright's pinned Chromium
headless shell from this skill's own state folder (scripts/setup_renderer.py installs it).
It always runs with the Chromium sandbox on, a throwaway profile, a mock keychain and no
network: the deck HTML is self-contained, the context is offline and every request other
than the deck file itself is aborted and reported. When the pinned browser is missing the
build reports exit code 4 and the HTML stays usable; no other browser is ever picked up.

  pptx mode additionally writes bg_NN.png (slide without text) and geometry.json (every text
  block with position, font, size, weight, colour; SVG text labels such as chart and ring captions;
  living-line media boxes) for build_pptx.py. Slide file names are zero-padded to one width per
  deck and ordered by number, so decks of 100+ slides keep slide and background pairs together.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import premium_common as pc


class RendererUnavailable(Exception):
    """The pinned browser is missing or cannot start; exit code 4."""


# Slide files are s_<number>.png; consumers order them by that number, never lexicographically:
# with 100+ slides "s_100.png" would otherwise sort between "s_10.png" and "s_11.png".
SLIDE_PNG = re.compile(r"^s_(\d+)\.png$")


def slide_pngs(png_dir) -> list[pathlib.Path]:
    """Slide PNGs of a job folder in deck order (slide 1 first, then 2, ... 100, ...)."""
    files = [p for p in pathlib.Path(png_dir).iterdir() if SLIDE_PNG.match(p.name)]
    return sorted(files, key=lambda p: int(SLIDE_PNG.match(p.name).group(1)))


NOTEXT_CSS = """
.slide.notext *:not(svg):not(svg *){-webkit-text-fill-color:transparent !important;text-shadow:none !important}
.slide.notext .bronze{background:none !important}
.slide.notext img.anim[data-anim]{visibility:hidden !important}
.slide.notext svg text{fill:transparent !important;stroke:none !important}
"""

# Animated media (living line) of one slide: build_pptx.py places the GIF between background and text.
COLLECT_MEDIA_JS = """(slide) => {
  const sr = slide.getBoundingClientRect();
  return Array.from(slide.querySelectorAll('img.anim[data-anim]')).map(img => {
    const r = img.getBoundingClientRect();
    return { x: r.left - sr.left, y: r.top - sr.top, w: r.width, h: r.height, anim: img.dataset.anim };
  });
}"""

# Collects editable text blocks of one slide (unchanged from the original renderer).
COLLECT_GEOMETRY_JS = """(slide) => {
  const sr = slide.getBoundingClientRect();
  const hasDirectText = (el) => Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
  const effOpacity = (el) => { let o = 1; for (let e = el; e && e !== slide; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity || 1); return o; };
  const styleOf = (el) => {
    const cs = getComputedStyle(el);
    let color = cs.color;
    const bronze = el.classList.contains('bronze') || (el.closest('.bronze') && el.closest('.bronze') !== slide) || cs.webkitBackgroundClip === 'text';
    if (bronze) color = slide.classList.contains('day') ? 'rgb(166,124,63)' : 'rgb(220, 200, 168)';
    return {
      font: cs.fontFamily.split(',')[0].replace(/["']/g, '').trim(),
      size: parseFloat(cs.fontSize),
      weight: parseInt(cs.fontWeight, 10) || 400,
      color,
      spacing: cs.letterSpacing === 'normal' ? 0 : parseFloat(cs.letterSpacing),
      transform: cs.textTransform,
      opacity: effOpacity(el),
    };
  };
  const rotationOf = (el) => {
    for (let e = el; e && e !== slide; e = e.parentElement) {
      const t = getComputedStyle(e).transform;
      if (t && t !== 'none') { const m = t.match(/matrix\\(([^)]+)\\)/); if (m) { const v = m[1].split(',').map(Number); return Math.atan2(v[1], v[0]) * 180 / Math.PI; } }
    }
    return 0;
  };
  const blocks = [];
  const all = slide.querySelectorAll('*');
  for (const el of all) {
    if (el.closest('svg')) continue;
    if (!hasDirectText(el)) continue;
    if (el.parentElement && el.parentElement !== slide && hasDirectText(el.parentElement)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    const base = styleOf(el);
    const runs = [];
    const walk = (node, st) => {
      for (const n of node.childNodes) {
        if (n.nodeType === 3) { if (n.textContent) runs.push({ text: n.textContent.replace(/\\s+/g, ' '), ...st }); }
        else if (n.nodeType === 1) {
          if (n.tagName === 'BR') runs.push({ text: '\\n', ...st });
          else if (n.tagName === 'SVG' || n.tagName === 'svg' || n.tagName === 'IMG' || n.tagName === 'I' || n.tagName === 'B' && !n.textContent.trim()) continue;
          else walk(n, styleOf(n));
        }
      }
    };
    walk(el, base);
    if (!runs.some(r => r.text.trim())) continue;
    while (runs.length && !runs[0].text.trim() && runs[0].text !== '\\n') runs.shift();
    while (runs.length && !runs[runs.length - 1].text.trim() && runs[runs.length - 1].text !== '\\n') runs.pop();
    if (runs.length) { runs[0].text = runs[0].text.replace(/^\\s+/, ''); runs[runs.length - 1].text = runs[runs.length - 1].text.replace(/\\s+$/, ''); }
    const pl = parseFloat(cs.paddingLeft) + parseFloat(cs.borderLeftWidth), pr = parseFloat(cs.paddingRight) + parseFloat(cs.borderRightWidth);
    const pt = parseFloat(cs.paddingTop) + parseFloat(cs.borderTopWidth), pb = parseFloat(cs.paddingBottom) + parseFloat(cs.borderBottomWidth);
    const rot = Math.round(rotationOf(el));
    let bx = r.left - sr.left + pl, by = r.top - sr.top + pt, bw = r.width - pl - pr, bh = r.height - pt - pb;
    if (rot) {
      bw = el.offsetWidth - pl - pr; bh = el.offsetHeight - pt - pb;
      bx = r.left - sr.left + r.width / 2 - bw / 2; by = r.top - sr.top + r.height / 2 - bh / 2;
    }
    blocks.push({
      x: bx, y: by, w: bw, h: bh, rot,
      vcenter: cs.display.includes('flex') && cs.alignItems === 'center',
      nowrap: cs.whiteSpace === 'nowrap' || cs.whiteSpace === 'pre',
      align: cs.textAlign === 'start' ? 'left' : cs.textAlign,
      lineHeight: cs.lineHeight === 'normal' ? base.size * 1.2 : parseFloat(cs.lineHeight),
      runs,
    });
  }
  for (const el of slide.querySelectorAll('svg text')) {
    if (!el.textContent.trim()) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    const anchor = el.getAttribute('text-anchor') || 'start';
    blocks.push({
      x: r.left - sr.left, y: r.top - sr.top, w: r.width, h: r.height, rot: 0,
      vcenter: false, nowrap: true,
      align: anchor === 'middle' ? 'center' : (anchor === 'end' ? 'right' : 'left'),
      lineHeight: cs.lineHeight === 'normal' ? parseFloat(cs.fontSize) * 1.2 : parseFloat(cs.lineHeight),
      runs: [{ text: el.textContent.replace(/\\s+/g, ' ').trim(),
               font: (cs.fontFamily || 'Inter').split(',')[0].replace(/["']/g, '').trim(),
               size: parseFloat(cs.fontSize), weight: parseInt(cs.fontWeight, 10) || 400,
               color: cs.fill, spacing: cs.letterSpacing === 'normal' ? 0 : parseFloat(cs.letterSpacing),
               opacity: effOpacity(el) }]
    });
  }
  return blocks;
}"""

# Playwright already starts Chromium with a throwaway profile; these keep it away from the OS
# keychain and from any background traffic. The sandbox stays on (chromium_sandbox=True).
LAUNCH_ARGS = ["--use-mock-keychain", "--password-store=basic", "--disable-background-networking",
               "--disable-component-update", "--disable-domain-reliability", "--no-first-run",
               "--disable-sync", "--metrics-recording-only"]


class _Session:
    """Pinned headless shell from <state>/ms-playwright, offline context, requests audited."""

    def __init__(self, state: pathlib.Path, *, scale: float = 1):
        root = pc.browsers_dir(state)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)   # read by the Playwright driver when it starts
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError as exc:
            raise RendererUnavailable(f"нет Python-пакета playwright=={pc.PLAYWRIGHT_VERSION}: включи скилл заново, "
                                      f"Enable ставит зависимости ({exc})") from None
        self.error_type = PlaywrightError
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(headless=True, chromium_sandbox=True, args=LAUNCH_ARGS,
                                                   timeout=60000)
        except PlaywrightError as exc:
            self.pw.stop()
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
            raise RendererUnavailable(
                "браузер рендера не запустился: " + first[:400] +
                ". Если он не установлен: setup_renderer.py install (около 100 МБ в папку состояния скилла)") from None
        self.context = self.browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=scale,
                                                offline=True, service_workers="block", accept_downloads=False)
        self.requests: list[str] = []
        self.blocked: list[str] = []

    def open(self, url: str):
        from urllib.parse import unquote
        target = unquote(url)

        def route(r):
            if unquote(r.request.url) == target:
                r.continue_()
            else:
                self.blocked.append(r.request.url)
                r.abort()
        self.context.route("**/*", route)
        page = self.context.new_page()
        page.on("request", lambda req: None if req.url.startswith("data:") else self.requests.append(req.url))
        return page

    def close(self):
        try:
            self.context.close()
            self.browser.close()
        finally:
            self.pw.stop()

    def outside_requests(self, allowed: str) -> list[str]:
        from urllib.parse import unquote
        target = unquote(allowed)
        found = {u for u in self.requests if unquote(u) != target} | set(self.blocked)
        return sorted(u[:300] for u in found)


def render(html: pathlib.Path, png_dir: pathlib.Path, state: pathlib.Path, *, scale: float = 1,
           work_dir: pathlib.Path | None = None) -> dict:
    """Screenshots into png_dir (which must be new and empty); pptx data into work_dir when given."""
    url = html.resolve().as_uri()
    session = _Session(state, scale=scale)
    try:
        png_dir.mkdir(parents=False, exist_ok=False)     # created only once the browser is up
        page = session.open(url)
        page.add_init_script("window.btRender = true;")    # keep still PNGs instead of GIFs while rendering
        page.goto(url, wait_until="load", timeout=60000)
        page.evaluate("() => document.fonts.ready.then(() => document.fonts.size)")
        page.evaluate("() => { if (window.btFit) window.btFit(); }")   # second safety net for pills and numbers
        if work_dir is not None:
            page.add_style_tag(content=NOTEXT_CSS)
        page.wait_for_timeout(600)
        slides = page.locator(".slide")
        n = slides.count()
        pad = max(2, len(str(n)))          # equal-width names: s_001..s_100 never sort between s_10 and s_11
        geometry = []
        for i in range(n):
            s = slides.nth(i)
            s.scroll_into_view_if_needed()
            nn = f"{i + 1:0{pad}d}"
            s.screenshot(path=str(png_dir / f"s_{nn}.png"))
            if work_dir is not None:
                blocks = s.evaluate(COLLECT_GEOMETRY_JS)
                media = s.evaluate(COLLECT_MEDIA_JS)
                geometry.append({"n": i + 1, "blocks": blocks, "media": media})
                s.evaluate("el => el.classList.add('notext')")
                s.screenshot(path=str(work_dir / f"bg_{nn}.png"))
                s.evaluate("el => el.classList.remove('notext')")
        if work_dir is not None:
            (work_dir / "geometry.json").write_text(
                json.dumps({"width": 1920, "height": 1080, "slides": geometry}, ensure_ascii=False), encoding="utf-8")
        chromium = session.browser.version
        outside = session.outside_requests(url)
    except session.error_type as exc:
        raise RuntimeError(f"рендер не удался: {str(exc).strip()[:600]}") from None
    finally:
        session.close()
    return {"slides": n, "chromium": chromium, "outside_requests": outside}


def smoke(state: pathlib.Path) -> dict:
    """Start the browser once on a built-in page: proves the installation, touches no file."""
    session = _Session(state)
    try:
        url = "data:text/html,<div style='width:64px;height:64px;background:%23C6A274'></div>"
        page = session.open(url)
        page.goto(url, wait_until="load", timeout=30000)
        shot = page.screenshot()
        return {"chromium": session.browser.version, "screenshot_bytes": len(shot)}
    finally:
        session.close()
