"""Source-level contract checks for the widget and the manifest.

Two levels, both honest about their limits. The source checks assert that the
widget declares the behaviours the manifest promises — paged keyboard access,
focus restoration, cancellable animation frames, guarded trajectory answers —
and that it reaches for nothing outside its own route prefix. Above them,
``tests/widget_smoke.cjs`` actually executes the widget against a minimal DOM
stub, so the render paths are exercised rather than merely grepped. Both Node
levels skip when no Node runtime is installed; neither is a browser, so nothing
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
        """Execute every render path: first paint, data, paging, selection, dispose.

        ``tests/widget_smoke.cjs`` drives the real widget source in a Node VM
        context against a minimal DOM and a recording canvas, asserting what the
        card does rather than what it contains. It is not a browser — no layout,
        no painting — but it does catch a render path that throws or draws the
        wrong population.
        """
        node = _healthy_node()
        if not node:                                  # pragma: no cover
            self.skipTest("no working Node runtime available to run the widget")
        result = subprocess.run([node, str(_ROOT / "tests" / "widget_smoke.cjs")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("widget smoke: ok", result.stdout)

    def test_it_talks_only_to_its_own_route_prefix(self) -> None:
        self.assertIn("var ROOT = '/api/extensions/context-lens/';", WIDGET)
        for path in re.findall(r"['\"](/[A-Za-z0-9._/-]+)['\"]", WIDGET):
            self.assertTrue(path.startswith("/api/extensions/context-lens/"), path)
        self.assertNotIn("http://", WIDGET)
        self.assertNotIn("https://", WIDGET)

    def test_it_reaches_for_no_forbidden_browser_capability(self) -> None:
        for forbidden in ("innerHTML", "localStorage", "sessionStorage", "indexedDB",
                          "WebSocket", "EventSource", "document.write", "eval(",
                          "new Function", "'POST'", '"POST"', "XMLHttpRequest"):
            self.assertNotIn(forbidden, WIDGET)

    def test_the_frame_height_is_fixed_by_the_manifest(self) -> None:
        """The manifest decides the frame height, so the host mounts no observer.

        With render.height set, web/modules/widget_module.js skips its
        auto-height bridge; the document inside the frame is then free to be the
        one scrolling surface. The percentage heights below are legal exactly
        because nothing measures this document and feeds the result back.
        """
        self.assertRegex(MANIFEST, r"\n\s+height:\s*\d+\s*\n")
        # A viewport unit would still be wrong: it is the browser window's
        # height, not the height the manifest asked the host to give this card.
        self.assertNotIn("100vh", WIDGET)
        self.assertNotIn("vh;", WIDGET)
        # The document scrolls itself; no inner pane competes with it.
        self.assertIn("body{min-height:100%;overflow-y:auto;", WIDGET)
        self.assertRegex(WIDGET, r"\.chart\{position:relative;height:\d+px;\}")
        self.assertIn("#root{box-sizing:border-box;padding:16px", WIDGET)

    def test_buttons_use_the_host_button_type_size(self) -> None:
        self.assertIn(".button{font:inherit;font-size:13px", WIDGET)


class TestCompactByDefault(unittest.TestCase):
    def test_the_list_opens_compact_and_pages_on_demand(self) -> None:
        self.assertIn("var ROWS_COLLAPSED = 6;", WIDGET)
        self.assertIn("var ROWS_STEP = 12;", WIDGET)
        self.assertIn("'Show more (' + (total - shown) + ' left)'", WIDGET)
        self.assertIn("'Show less'", WIDGET)
        # The old behaviour — a fixed wall of 40 rows with no way to reach the
        # rest — must not come back.
        self.assertNotIn("slice(-40)", WIDGET)
        # And the rest is reached by paging, not by a second scrollbar nested
        # inside a card that already scrolls.
        self.assertNotIn("max-height:268px", WIDGET)
        self.assertEqual(WIDGET.count("overflow-y:auto"), 1)

    def test_explanatory_prose_sits_behind_disclosures(self) -> None:
        self.assertIn("'How to read this'", WIDGET)
        self.assertIn("'Coverage details'", WIDGET)
        self.assertIn("'Technical detail'", WIDGET)
        self.assertGreaterEqual(WIDGET.count("el('details'"), 4)

    def test_the_trajectory_is_drawn_above_the_technical_fields(self) -> None:
        trajectory_at = WIDGET.index("renderTrajectory(card, point);")
        technical_at = WIDGET.index("technical.appendChild(el('summary', null, 'Technical detail'));")
        self.assertLess(trajectory_at, technical_at)

    def test_the_tiles_are_labelled_for_a_reader_not_for_a_statistician(self) -> None:
        for label in ("'Typical input'", "'95% below'", "'Peak'", "'Data coverage'"):
            self.assertIn(label, WIDGET)
        self.assertIn("linear-interpolated 95th percentile", WIDGET)

    def test_coverage_is_computed_from_the_same_points_the_chart_draws(self) -> None:
        # The tile describes the current filter; the whole-window figure is kept
        # separate and explicitly labelled inside Coverage details.
        self.assertIn("view.total ? Math.round((view.measured / view.total) * 100)", WIDGET)
        self.assertIn("'Across the selected horizon, before any filter'", WIDGET)
        self.assertIn("These counts cover every filter at once", WIDGET)


class TestAccessibilityAndLifecycle(unittest.TestCase):
    def test_the_chart_does_not_claim_every_request_is_listed(self) -> None:
        self.assertNotIn("The same requests are listed below", WIDGET)
        self.assertIn("can be reached as text in the Recent requests list below", WIDGET)

    def test_keyboard_focus_survives_a_rerender(self) -> None:
        self.assertIn("function restoreFocus(", WIDGET)
        self.assertIn("active.getAttribute('data-focus')", WIDGET)
        for key in ("'filter-' + entry.key", "'row-' + point.id", "'refresh'",
                    "'rows-more'", "'rows-less'", "'trajectory-reload'"):
            self.assertIn(key, WIDGET)

    def test_animation_frames_are_cancellable_and_cancelled(self) -> None:
        self.assertIn("function clearScheduled()", WIDGET)
        self.assertIn("cancelAnimationFrame", WIDGET)
        # Cancelled both when the tree is rebuilt and when the card is disposed.
        self.assertEqual(WIDGET.count("clearScheduled();"), 2)   # rebuild + dispose

    def test_both_charts_are_resize_observed_through_one_debounced_path(self) -> None:
        self.assertIn("function liveChart(", WIDGET)
        self.assertIn("liveChart(chart, canvas, function () { drawScatter(canvas); });", WIDGET)
        self.assertIn("liveChart(chart, canvas, function () { drawTrajectory(canvas, payload); });", WIDGET)
        self.assertIn("if (pending) return;", WIDGET)

    def test_a_superseded_trajectory_answer_is_dropped(self) -> None:
        self.assertIn("var trajectorySeq = 0;", WIDGET)
        self.assertIn("if (disposed || token !== trajectorySeq) return;", WIDGET)
        self.assertIn("function forgetTrajectory()", WIDGET)

    def test_unknown_mode_is_always_offered(self) -> None:
        self.assertIn("modes.push('unknown');", WIDGET)


class TestHonestWording(unittest.TestCase):
    def test_absent_evidence_is_described_as_absent_from_this_ledger(self) -> None:
        # Observability may hold the discriminator; this widget simply cannot
        # join to it. Saying "no field in this install" would overclaim.
        self.assertNotIn("no field in this install", WIDGET)
        self.assertNotIn("records no field", WIDGET)
        self.assertIn("is not available from this ledger", WIDGET)
        self.assertIn("outside this widget’s scope", WIDGET)

    def test_the_manifest_does_not_claim_the_skill_is_invisible_to_the_model(self) -> None:
        self.assertNotIn("never enters the model's context", MANIFEST)
        self.assertIn("manifest metadata", MANIFEST)
        self.assertIn("tool schema", MANIFEST)

    def test_no_join_between_the_ledger_and_observability_is_claimed(self) -> None:
        self.assertNotIn("join key", WIDGET)
        self.assertIn("never claims one happened", WIDGET)


if __name__ == "__main__":
    unittest.main()
