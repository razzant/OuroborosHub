"""Chip consumer uses the widget cache and never the provider-refresh action."""
import copy

import pytest

import plugin
from quota_chip import chip
from test_passive_quota_read import envelope, stub
from test_reserve import _Host, FIVE_H, constraint, profile, snap


def codex_average_example(third="absent"):
    subjects = ("fixture-a", "fixture-b")
    data = envelope(subjects=subjects if third == "absent" else (*subjects, "fixture-c"))
    data["quota"] = [
        snap("codex", sid, [constraint("weekly", used, reset=86400, label="Weekly text")], plan=plan)
        for sid, used, plan in zip(subjects, (.9, .4), ("Plan A", "Plan B"))
    ]
    if third == "stale":
        data["quota"].append(snap("codex", "fixture-c", [constraint("weekly", .25, reset=86400,
                                                                   label="Weekly text")], fresh=False))
    return data


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
    assert result["text"] == "Codex avg 38%"
    assert result["tone"] == "ok"
    assert "Claude: 61% average remaining" in result["title"]
    assert "next reset" in result["title"] and "2026-" in result["title"]
    assert len(calls) == 1 and calls[0][0:2] == ("GET", plugin.STATUS_PATH)


@pytest.mark.parametrize(("third", "coverage"), [
    ("absent", "2 of 2 accounts known for this limit"),
    ("missing", "2 of 2 accounts known for this limit; no reading for 1 other account (limit may not apply)"),
    ("stale", "2 of 3 accounts known for this limit"),
])
def test_registered_chip_says_average_and_its_fresh_coverage(tmp_path, monkeypatch, third, coverage):
    calls = stub(monkeypatch, [(codex_average_example(third), "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    result = host.routes["chip"]({})
    widget = host.routes["quotas"]({"query_params": {"reuse": "1", "chart": "0"}})
    [group] = widget["reserve"]["summary"]["groups"]
    assert group["measured"] == {"accounts": 2, "windows": .7, "average_remaining_pct": 35.0, "at_limit": 0}
    assert group["last_known"]["accounts"] == (1 if third == "stale" else 0)
    assert group["applicability_unknown"] == (1 if third == "missing" else 0)
    assert set(result) == {"text", "tone", "title"}
    assert result["text"] == "Codex avg 35%" and result["tone"] == "ok"
    assert f"35% average remaining (Weekly text); fresh readings: {coverage}; next reset" in result["title"]
    assert "fixture-" not in result["title"]
    assert calls == [("GET", plugin.STATUS_PATH, plugin.STATUS_TIMEOUT_SEC)]


@pytest.mark.parametrize(("used", "fresh", "text", "tone", "title_fragment"), [
    (1.0, True, "Codex avg 0%", "bad", "0% average remaining"),
    (.9999, True, "Codex avg <1%", "bad", "<0.1% average remaining"),
    (.9, False, "—", "muted", "remaining unknown"),
])
def test_registered_chip_distinguishes_exhaustion_from_no_current_reading(
        tmp_path, monkeypatch, used, fresh, text, tone, title_fragment):
    data = codex_average_example()
    for row in data["quota"]:
        row["freshness"] = "fresh" if fresh else "stale"
        row["constraints"][0]["used_ratio"] = used
    stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    result = host.routes["chip"]({})
    assert result["text"] == text and result["tone"] == tone
    assert f"{title_fragment} (Weekly text); fresh readings: {2 if fresh else 0} of 2" in result["title"]


@pytest.mark.parametrize("cached", [False, True])
def test_registered_chip_marks_unread_roster(tmp_path, monkeypatch, cached):
    healthy = codex_average_example("missing")
    incomplete = envelope(accounts="failed")
    incomplete["quota"] = healthy["quota"]
    calls = stub(monkeypatch, ([(healthy, "", 200)] if cached else []) + [(incomplete, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    for _ in range(1 + cached):
        host.routes["quotas"]({"query_params": {"chart": "0"}})  # direct read forces the new envelope
    result = host.routes["chip"]({})
    assert result["text"] == "Codex avg 35%"
    if cached:
        assert "2 of 2 accounts known for this limit; no reading for 1 other account" in result["title"]
        assert result["title"].endswith("Account list last known")
    else:
        assert "fresh readings for this limit: 2 accounts" in result["title"]
        assert "2 of" not in result["title"]
        assert result["title"].endswith("Account list unavailable")
    assert len(calls) == 1 + cached


def test_registered_chip_keeps_duration_and_request_scope_apart(tmp_path, monkeypatch):
    data = codex_average_example("missing")
    for row in data["quota"]:
        row["constraints"][0]["applies_to_request_kinds"] = ["text"]
        row["constraints"].append(constraint("weekly", .05, FIVE_H, label="5-hour text", reset=3600))
    # A third account reports the same duration and constraint id, but for image only.
    image = constraint("weekly", .2, label="Weekly image", reset=43200)
    image["applies_to_request_kinds"] = ["image"]
    data["quota"].append(snap("codex", "fixture-c", [image]))
    stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    result = host.routes["chip"]({})
    widget = host.routes["quotas"]({"query_params": {"reuse": "1", "chart": "0"}})
    groups = widget["reserve"]["summary"]["groups"]
    assert {g["label"]: g["measured"]["average_remaining_pct"] for g in groups} == {
        "Weekly text": 35, "Weekly image": 80, "5-hour text": 95,
    }
    [tightest] = [g for g in groups if g["tightest"]]
    assert tightest["scope"]["request_kinds"] == ["text"]
    assert result["text"] == "Codex avg 35%"
    assert "(Weekly text); fresh readings: 2 of 2" in result["title"]
    assert "Weekly image" not in result["title"] and "5-hour text" not in result["title"]


@pytest.mark.parametrize("selected", ["text-week", "image-week", "text-five-hour"])
def test_registered_chip_distinguishes_same_label_by_duration_and_scope(tmp_path, monkeypatch, selected):
    data = codex_average_example()
    for row in data["quota"]:
        used = row["constraints"][0]["used_ratio"]
        row["constraints"] = [
            dict(constraint("primary", used if name == selected else .05, window, label="weekly"),
                 applies_to_request_kinds=[kind])
            for name, kind, window in [("text-week", "text", 604800), ("image-week", "image", 604800),
                                       ("text-five-hour", "text", FIVE_H)]
        ]
    calls = stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    result = host.routes["chip"]({})
    widget = host.routes["quotas"]({"query_params": {"reuse": "1", "chart": "0"}})
    [tightest] = [g for g in widget["reserve"]["summary"]["groups"] if g["tightest"]]
    kind = selected.split("-")[0]
    assert tightest["scope"]["request_kinds"] == [kind]
    assert tightest["window_seconds"] == (FIVE_H if selected.endswith("five-hour") else 604800)
    assert result["text"] == "Codex avg 35%" and len(result["text"]) <= 24
    assert f"35% average remaining (weekly ({tightest['duration']}) {kind} requests only)" in result["title"]
    assert "fresh readings: 2 of 2" in result["title"]
    assert len(calls) == 1


def test_cold_chip_uses_only_passive_status_and_transport_error_is_muted(tmp_path, monkeypatch):
    calls = stub(monkeypatch, [(None, "offline", 503)])
    host = _Host(tmp_path)
    plugin.register(host)
    assert host.routes["chip"]({}) == {"text": "—", "tone": "muted", "title": "offline"}
    assert len(calls) == 1 and calls[0][0:2] == ("GET", plugin.STATUS_PATH)


@pytest.mark.parametrize("remaining,tone", [(0, "bad"), (10, "bad"), (11, "warn"), (25, "warn"), (26, "ok")])
def test_display_thresholds_and_missing_readings(remaining, tone):
    group = {"harness": "codex", "family": "Codex", "label": "weekly", "tightest": True,
             "measured": {"average_remaining_pct": remaining, "accounts": 1, "at_limit": int(remaining == 0)},
             "slots": 1, "applicability_unknown": 0, "next_reset": None}
    view = {"ok": True, "reserve": {"summary": {"groups": [group], "roster": "current"}}}
    assert chip(view)["tone"] == tone
    unknown = copy.deepcopy(view)
    unknown["reserve"]["summary"]["groups"][0]["measured"] = {
        "average_remaining_pct": None, "accounts": 0, "at_limit": 0}
    assert chip(unknown)["text"] == "—"
    assert chip(unknown)["tone"] == "muted"
    assert "unknown" in chip(unknown)["title"]


def test_chip_text_is_bounded_and_title_retains_all_families():
    groups = [{"harness": str(index), "family": label, "label": "weekly", "tightest": True,
               "measured": {"average_remaining_pct": index + 1, "accounts": 1, "at_limit": 0},
               "slots": 1, "applicability_unknown": 0, "next_reset": {"at": "2026-10-18"}}
              for index, label in enumerate(["A very long engine family label", "Claude", "Codex"])]
    result = chip({"ok": True, "reserve": {"summary": {"groups": groups}}})
    assert len(result["text"]) == 24 and result["text"].endswith("… avg 1%")
    assert all(group["family"] in result["title"] for group in groups)


def test_the_reset_in_the_chip_title_is_in_the_zone_of_this_machine(monkeypatch):
    import time
    from quota_chip import local_reset
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        assert local_reset("2026-10-30T14:00:00.123456Z") == "2026-10-30 23:00 JST"
        assert local_reset("2026-10-30T14:00:00+00:00") == "2026-10-30 23:00 JST"
        assert local_reset("2026-10-18") == "2026-10-18"
        assert local_reset("not a time") == "not a time"
        assert local_reset(None) == "unknown"
        # A moment the zone cannot hold (a "forever" marker in the year 9999 east of UTC) stays as it came: the chip is drawn, not dropped.
        assert local_reset("9999-12-31T23:59:59Z") == "9999-12-31T23:59:59Z"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
