"""Hub contract checks for dashboard widget theme opt-in.

The bridge is deliberately optional: a legacy host must still execute each
payload, while current hosts receive an explicit host appearance subscription.
These checks complement each skill's functional widget tests without pretending
that source inspection proves browser contrast or canvas paint.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = (
    "cache_efficiency_snapshot",
    "claudexor_quotas",
    "context-lens",
    "memory-atlas",
    "token-usage",
    "smart_lists",
    "calendar",
)
# memory-atlas is an ES module entry (render.entry_type: module) that only PluginAPI 2.1 hosts load, and every one of
# them has the theme bridge: it keeps no legacy palette and is checked by the module test below, and in a real host
# by tests/memory_atlas/test_widget_browser.py.
CLASSIC = tuple(name for name in DASHBOARDS if name != "memory-atlas")


def test_dashboard_manifests_opt_into_host_appearance():
    for name in DASHBOARDS:
        skill = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert "appearance: host" in skill
        plugin = (ROOT / "skills" / name / "plugin.py").read_text(encoding="utf-8")
        assert '"appearance": "host"' in plugin


def test_dashboard_payloads_keep_a_legacy_fallback_and_dispose_theme_listeners():
    for name in CLASSIC:
        widget = (ROOT / "skills" / name / "widget.js").read_text(encoding="utf-8")
        assert "onTheme" in widget
        assert re.search(r"data-theme\s*=\s*[\"']?light", widget)
        assert "__ouroWidgetOnDispose" in widget
        # The bridge is optional, so the payload must not require it at parse/load.
        assert ("typeof window.OuroborosWidget.onTheme" in widget or "window.OuroborosWidget &&" in widget
                or "OuroborosWidget.onTheme?.(" in widget)


def test_module_entry_dashboard_follows_the_host_theme_only_through_the_bridge():
    """memory-atlas declares PluginAPI 2.1 and an ES module entry. The entry still feature-detects the bridge, applies
    light or dark to its own document, and unsubscribes when the host disposes the frame."""
    root = ROOT / "skills" / "memory-atlas"
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    assert 'plugin_api: "2.1"' in skill and "entry_type: module" in skill and "entry: app/main.js" in skill
    entry = (root / "app" / "main.js").read_text(encoding="utf-8")
    assert "typeof bridge.onTheme === 'function'" in entry and "themeOff = bridge.onTheme(applyTheme)" in entry
    assert re.search(r"dataset\.theme\s*=\s*theme === 'light' \? 'light' : 'dark'", entry)
    assert "__ouroWidgetOnDispose(() => { themeOff();" in entry
    assert re.search(r"data-theme\s*=\s*[\"']?light", (root / "app" / "atlas.css").read_text(encoding="utf-8"))
