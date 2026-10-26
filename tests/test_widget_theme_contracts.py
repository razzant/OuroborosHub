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


def test_dashboard_manifests_opt_into_host_appearance():
    for name in DASHBOARDS:
        skill = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert "appearance: host" in skill
        plugin = (ROOT / "skills" / name / "plugin.py").read_text(encoding="utf-8")
        assert '"appearance": "host"' in plugin


def test_dashboard_payloads_keep_a_legacy_fallback_and_dispose_theme_listeners():
    for name in DASHBOARDS:
        widget = (ROOT / "skills" / name / "widget.js").read_text(encoding="utf-8")
        assert "onTheme" in widget
        assert re.search(r"data-theme\s*=\s*[\"']?light", widget)
        assert "__ouroWidgetOnDispose" in widget
        # The bridge is optional, so the payload must not require it at parse/load.
        assert ("typeof window.OuroborosWidget.onTheme" in widget or "window.OuroborosWidget &&" in widget
                or "OuroborosWidget.onTheme?.(" in widget)
