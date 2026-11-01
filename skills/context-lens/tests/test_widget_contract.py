"""Source-level contract checks for the widget and the manifest.

Two levels, both honest about their limits. The source checks assert that the
widget declares the behaviours SKILL.md promises — its own route prefix only,
host theming without a remount, an auto-height-safe geometry, paged keyboard
access, focus and disclosure preservation, cancellable frames, guarded answers —
and that it reaches for nothing outside them. Above them,
``tests/widget_smoke.cjs`` executes the widget against a minimal DOM stub, so
the render paths are exercised rather than merely grepped. Both Node levels
skip when no working Node runtime is found; neither is a browser, so nothing
here proves anything about real layout or painting.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
WIDGET = (_ROOT / "widget.js").read_text(encoding="utf-8")
MANIFEST = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
FRONT = MANIFEST.split("\n---\n", 1)[0]
# The stylesheet as the browser receives it: installStyle() joins its literals.
_STYLE = WIDGET[WIDGET.index("function installStyle()"):WIDGET.index("// -------------------------------------------------------------- startup")]
CSS = "".join(re.findall(r"'((?:[^'\\]|\\.)*)'", _STYLE[_STYLE.index("var css = ["):_STYLE.index("].join('');")]))


def _healthy_node():
    """Return a Node binary that actually executes, or None.

    A PATH entry is not proof: a broken or foreign-arch install answers every
    invocation with SIGKILL, which would surface here as a widget failure the
    widget did not cause. Each candidate is probed once; CONTEXT_LENS_NODE lets
    a host point at its own bundled runtime.
    """
    for candidate in (os.environ.get("CONTEXT_LENS_NODE"), shutil.which("node")):
        if not candidate:
            continue
        try:
            probe = subprocess.run([candidate, "-e", "process.exit(0)"],
                                   capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):      # pragma: no cover
            continue
        if probe.returncode == 0:
            return candidate
    return None


class TestWidgetSurface(unittest.TestCase):
    def test_the_widget_parses(self) -> None:
        node = _healthy_node()
        if not node:                                  # pragma: no cover
            self.skipTest("no working Node runtime available to parse the widget")
        result = subprocess.run([node, "--check", str(_ROOT / "widget.js")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_widget_runs_against_a_dom_stub(self) -> None:
        """Execute every render path: data, focus, stale refresh, empties, theme, dispose."""
        node = _healthy_node()
        if not node:                                  # pragma: no cover
            self.skipTest("no working Node runtime available to run the widget")
        result = subprocess.run([node, str(_ROOT / "tests" / "widget_smoke.cjs")],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("widget smoke: ok", result.stdout)

    def test_it_talks_only_to_its_own_route_prefix(self) -> None:
        self.assertIn("var ROOT = '/api/extensions/context-lens/';", WIDGET)
        for path in re.findall(r"['\"](/[A-Za-z0-9._/-]+)['\"]", WIDGET):
            self.assertTrue(path.startswith("/api/extensions/context-lens/"), path)
        self.assertNotIn("http://", WIDGET)
        self.assertNotIn("https://", WIDGET)

    def test_every_request_carries_a_signal_and_a_timeout(self) -> None:
        self.assertIn("{ signal: controller.signal, timeoutMs: REQUEST_TIMEOUT_MS }", WIDGET)
        self.assertIn("var REQUEST_TIMEOUT_MS = 20000;", WIDGET)
        self.assertIn("requestTimers.forEach(clearTimeout);", WIDGET)

    def test_it_reaches_for_no_forbidden_browser_capability(self) -> None:
        for forbidden in ("innerHTML", "localStorage", "sessionStorage", "indexedDB",
                          "WebSocket", "EventSource", "document.write", "eval(",
                          "new Function", "'POST'", '"POST"', "XMLHttpRequest",
                          "postMessage", "document.cookie", "onEvent("):
            self.assertNotIn(forbidden, WIDGET)


class TestThemeAndGeometry(unittest.TestCase):
    def test_the_manifest_opts_into_the_host_theme_and_auto_height(self) -> None:
        self.assertRegex(FRONT, r"\n    appearance: host\n")
        self.assertNotRegex(FRONT, r"\n\s+height:")
        self.assertNotIn("max_height", FRONT)

    def test_both_palettes_are_named_and_set_the_native_scheme(self) -> None:
        self.assertIn(":root{color-scheme:dark;", CSS)
        self.assertIn(":root[data-theme=light]{color-scheme:light;", CSS)
        dark = re.search(r":root\{color-scheme:dark;(.*?)\}", CSS).group(1)
        light = re.search(r":root\[data-theme=light\]\{color-scheme:light;(.*?)\}", CSS).group(1)
        names = lambda block: set(re.findall(r"(--lens-[a-z0-9-]+):", block))
        self.assertEqual(names(dark), names(light), "every token is defined for both themes")
        self.assertIn("html,body{margin:0;padding:0;background:transparent;}", WIDGET)

    def test_a_theme_change_repaints_without_a_remount(self) -> None:
        self.assertIn("window.OuroborosWidget.onTheme(function (theme) {", WIDGET)
        self.assertIn("document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';", WIDGET)
        theme = WIDGET[WIDGET.index("window.OuroborosWidget.onTheme("):]
        theme = theme[:theme.index("}) : null;")]
        self.assertIn("repaintCharts();", theme)
        self.assertNotIn("render()", theme)
        self.assertIn("if (typeof offTheme === 'function') offTheme();", WIDGET)
        self.assertIn("getPropertyValue('--lens-' + name)", WIDGET)

    def test_nothing_is_sized_from_the_viewport(self) -> None:
        # Under the host's auto-height bridge, a height derived from the frame's
        # own viewport would feed the reported height back into itself.
        for forbidden in ("100vh", "vh;", "height:100%;}", "min-height:100%", "overflow-y:auto",
                          "overflow-y:scroll"):
            self.assertNotIn(forbidden, WIDGET.replace(".canvas{width:100%;height:100%;", ""))
        self.assertIn(".chart{position:relative;height:260px;overflow:hidden;}", WIDGET)
        self.assertIn("@media (max-width:640px){.chart{height:220px;}}", WIDGET)
        self.assertIn("@media (max-width:400px){.chart{height:200px;}}", WIDGET)
        self.assertIn("#root{box-sizing:border-box;padding:12px 14px 14px;", WIDGET)

    def test_type_stays_on_the_host_scale(self) -> None:
        sizes = set(int(size) for size in re.findall(r"font-size:(\d+)px", WIDGET))
        self.assertLessEqual(sizes, {12, 13, 14, 16}, sizes)
        self.assertNotIn("24px", WIDGET)
        self.assertIn("'12px ' + FONT", WIDGET)


class TestChartFirstLayout(unittest.TestCase):
    def test_no_tiles_and_no_second_title(self) -> None:
        self.assertNotIn("statTile", WIDGET)
        self.assertNotIn("'tile'", WIDGET)
        self.assertNotIn("el('h1'", WIDGET)
        self.assertNotIn("'Context Lens'", WIDGET)

    def test_the_list_and_the_explanations_sit_behind_closed_disclosures(self) -> None:
        self.assertIn("open: { requests: false, about: false, technical: false }", WIDGET)
        for summary in ("'Requests · '", "'About this data'", "'Technical detail'"):
            self.assertIn(summary, WIDGET)
        self.assertIn("on(details, 'toggle', function () { state.open[key] = !!details.open; });", WIDGET)

    def test_the_list_opens_compact_and_pages_on_demand(self) -> None:
        self.assertIn("var ROWS_COLLAPSED = 6;", WIDGET)
        self.assertIn("var ROWS_STEP = 12;", WIDGET)
        self.assertIn("'Show more (' + (total - shown) + ' left)'", WIDGET)
        self.assertIn("'Show less'", WIDGET)

    def test_the_statistics_describe_exactly_the_drawn_points(self) -> None:
        self.assertIn("state.plotted = state.points.filter(measured);", WIDGET)
        self.assertIn("var values = state.plotted.map(function (p) { return p.prompt_tokens; })", WIDGET)
        self.assertIn("'median ' + compact(state.stats.median)", WIDGET)
        self.assertIn("'p95 ' + compact(state.stats.p95)", WIDGET)

    def test_task_focus_comes_from_the_held_answer_and_joins_nothing(self) -> None:
        self.assertNotIn("trajectory?", WIDGET)
        derive = WIDGET[WIDGET.index("function derive()"):WIDGET.index("// -------------------------------------------------------------- drawing")]
        self.assertIn("p.task === chosen.task", derive)
        draw = WIDGET[WIDGET.index("// One dot per measured request."):WIDGET.index("function liveChart(")]
        self.assertNotIn("lineTo", draw)

    def test_unknown_and_nano_modes_are_offered(self) -> None:
        self.assertIn("var modes = ['max', 'low', 'nano'].filter(", WIDGET)
        self.assertIn("modes.push('unknown');", WIDGET)


class TestAccessibilityAndLifecycle(unittest.TestCase):
    def test_keyboard_focus_survives_a_rerender(self) -> None:
        self.assertIn("function restoreFocus(", WIDGET)
        self.assertIn("active.getAttribute('data-focus')", WIDGET)
        for key in ("'filter-' + entry.key", "'row-' + point.id", "'refresh'", "'rows-more'",
                    "'rows-less'", "'chart'", "'clear-selection'", "'horizon-' + entry.key"):
            self.assertIn(key, WIDGET)

    def test_the_chart_is_keyboard_reachable_and_announced(self) -> None:
        self.assertIn("canvas.setAttribute('tabindex', '0');", WIDGET)
        for key in ("'ArrowRight'", "'ArrowLeft'", "'Home'", "'End'", "'Escape'"):
            self.assertIn(key, WIDGET)
        self.assertIn("live.setAttribute('aria-live', 'polite');", WIDGET)
        self.assertIn("every request in view is also listed under Requests", WIDGET)

    def test_a_status_change_does_not_rebuild_the_card(self) -> None:
        self.assertIn("function paintStatus()", WIDGET)
        self.assertIn("pendingRender = true;", WIDGET)
        # Refresh is never disabled: a focused control that becomes disabled
        # drops the keyboard.
        self.assertNotIn("refresh.disabled", WIDGET)

    def test_animation_frames_are_cancellable_and_cancelled(self) -> None:
        self.assertIn("function clearScheduled()", WIDGET)
        self.assertIn("cancelAnimationFrame", WIDGET)
        self.assertEqual(WIDGET.count("clearScheduled();"), 2)   # rebuild + dispose

    def test_the_chart_is_resize_observed_through_one_guarded_path(self) -> None:
        self.assertIn("function liveChart(", WIDGET)
        self.assertIn("liveChart(chart, canvas, function () { drawScatter(canvas); });", WIDGET)
        self.assertIn("if (width === lastW && height === lastH) return;", WIDGET)
        self.assertIn("if (pending) return;", WIDGET)

    def test_a_superseded_answer_is_dropped(self) -> None:
        self.assertIn("if (disposed || token !== dataSeq || requested !== state.horizon) return;", WIDGET)
        self.assertIn("if (applied && applied !== state.horizon) return;", WIDGET)

    def test_a_failed_refresh_keeps_the_last_answer_marked_stale(self) -> None:
        self.assertIn("'Showing the read from '", WIDGET)
        self.assertIn("'the latest refresh failed: '", WIDGET)
        self.assertIn("'Usage could not be read'", WIDGET)

    def test_the_poll_runs_only_while_visible_and_dispose_releases_everything(self) -> None:
        self.assertIn("if (disposed || document.visibilityState !== 'visible') return;", WIDGET)
        dispose = WIDGET[WIDGET.index("window.__ouroWidgetOnDispose(function () {"):]
        for release in ("timers.forEach(clearInterval);", "clearScheduled();", "observer.disconnect();",
                        "removeEventListener", "controllers.forEach(abort);", "offTheme();"):
            self.assertIn(release, dispose)


class TestHonestWording(unittest.TestCase):
    def test_the_time_axis_is_named_for_what_it_is(self) -> None:
        self.assertIn("'usage recorded →'", WIDGET)
        self.assertIn("it is not the send time and the gap between writes is not a latency", WIDGET)
        self.assertNotIn("latency'", WIDGET.replace("not a latency", ""))

    def test_cache_counts_are_never_added_or_turned_into_a_share(self) -> None:
        self.assertIn("never added to the input or turned into a share", WIDGET)
        self.assertNotIn("Already included in the input number", WIDGET)

    def test_no_cause_fill_cost_or_composition_is_claimed(self) -> None:
        self.assertIn("No cost, no window-fill percentage, no per-section contribution and no compaction cause", WIDGET)
        self.assertNotIn("% full", WIDGET)
        self.assertNotIn("$", WIDGET.replace("$.a", "").replace("$.physical_context", ""))

    def test_sent_and_registered_are_never_one_label(self) -> None:
        self.assertIn("' requests registered; '", WIDGET)
        self.assertIn("' of them were sent to a provider.'", WIDGET)
        self.assertIn("reserved: 'admitted, not sent yet'", WIDGET)
        self.assertIn("released: 'released without being sent'", WIDGET)

    def test_the_manifest_does_not_claim_the_skill_is_invisible_to_the_model(self) -> None:
        self.assertNotIn("never enters the model's context", MANIFEST)
        self.assertIn("manifest metadata", MANIFEST)
        self.assertIn("tool schema", MANIFEST)


if __name__ == "__main__":
    unittest.main()
