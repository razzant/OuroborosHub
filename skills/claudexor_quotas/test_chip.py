"""Chip consumer uses the widget cache and never the provider-refresh action."""
import copy

import pytest

import plugin
from quota_chip import chip
from test_passive_quota_read import envelope, stub
from test_reserve import _Host, constraint, profile, snap


def test_registered_chip_reuses_widget_read_and_includes_tightest_families(tmp_path, monkeypatch):
    data = envelope(subjects=("fixture-a",))
    data["profiles"]["profiles"].append(profile("claude", "fixture-c"))
    data["quota"] = [snap("codex", "fixture-a", [constraint("primary", .62, reset=86400)]),
                     snap("claude", "fixture-c", [constraint("primary", .39, reset=86400)])]
    calls = stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    widget = host.routes["quotas"]({"query_params": {"chart": "0"}})
    result = host.routes["chip"]({})
    assert result == chip(widget)
    assert result["text"] == "Codex 38% · Claude 61%"
    assert result["tone"] == "ok"
    assert "next reset" in result["title"] and "2026-" in result["title"]
    assert len(calls) == 1 and calls[0][0:2] == ("GET", plugin.STATUS_PATH)


def test_cold_chip_uses_only_passive_status_and_transport_error_is_muted(tmp_path, monkeypatch):
    calls = stub(monkeypatch, [(None, "offline", 503)])
    host = _Host(tmp_path)
    plugin.register(host)
    assert host.routes["chip"]({}) == {"text": "—", "tone": "muted", "title": "offline"}
    assert len(calls) == 1 and calls[0][0:2] == ("GET", plugin.STATUS_PATH)


@pytest.mark.parametrize("remaining,tone", [(0, "bad"), (10, "bad"), (11, "warn"), (25, "warn"), (26, "ok")])
def test_display_thresholds_and_missing_readings(remaining, tone):
    group = {"harness": "codex", "family": "Codex", "label": "weekly", "tightest": True,
             "measured": {"average_remaining_pct": remaining}, "next_reset": None}
    view = {"ok": True, "reserve": {"summary": {"groups": [group]}}}
    assert chip(view)["tone"] == tone
    unknown = copy.deepcopy(view)
    unknown["reserve"]["summary"]["groups"][0]["measured"]["average_remaining_pct"] = None
    assert chip(unknown)["text"] == "—"
    assert chip(unknown)["tone"] == "muted"
    assert "unknown" in chip(unknown)["title"]


def test_chip_text_is_bounded_and_title_retains_all_families():
    groups = [{"harness": str(index), "family": label, "label": "weekly", "tightest": True,
               "measured": {"average_remaining_pct": index + 1}, "next_reset": {"at": "2026-10-18"}}
              for index, label in enumerate(["A very long engine family label", "Claude", "Codex"])]
    result = chip({"ok": True, "reserve": {"summary": {"groups": groups}}})
    assert len(result["text"]) <= 24
    assert all(group["family"] in result["title"] for group in groups)
