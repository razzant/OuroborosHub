"""Producer-to-route regressions for historical ranges and exact intervals."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cache_efficiency import (
    plugin, event, session_event, measurement, boot, write_rows, NOW,
)


def api_for(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(plugin, "datetime", Clock)
    monkeypatch.setattr(plugin, "_EVENTS", plugin.UsageEventsCache())
    monkeypatch.setattr(plugin, "_CACHE", plugin.LedgerCache())
    monkeypatch.setattr(plugin, "_HISTORY", None)
    routes, unload = {}, []
    api = SimpleNamespace(get_runtime_info=lambda: {"data_dir": str(tmp_path)},
                          register_route=lambda name, fn, **kw: routes.update({name: fn}),
                          register_ui_tab=lambda *a, **kw: None, on_unload=unload.append)
    plugin.register(api)
    return api, routes["data"], unload


def finish_index(index):
    thread = index._thread
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()


def read_route(route, **query):
    return route({"query_params": query})


def test_history_reads_are_off_request_path_and_coalesced(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    index = plugin.HistoryIndex(tmp_path / "history.json")
    original = index._load
    def held_load():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(index, "_load", held_load)
    try:
        first = index.request(tmp_path / "archive", None, NOW)
        assert entered.wait(2)
        thread = index._thread
        for _ in range(10):
            assert index.request(tmp_path / "archive", None, NOW)[2]["state"] == "loading"
            assert index._thread is thread
        assert first[0] == []
    finally:
        release.set()
        finish_index(index)
        index.close()


def test_unchanged_history_neither_decodes_nor_rebuilds(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    index = plugin.HistoryIndex(tmp_path / "history.json")
    assert len(index.update(directory, None, NOW)[0]) == 1
    def forbidden(*args):
        raise AssertionError("whole-history decoding/rebuild on unchanged read")
    monkeypatch.setattr(index, "_decode", forbidden)
    monkeypatch.setattr(index, "_build_snapshot", forbidden)
    for _ in range(3):
        rows, _, meta = index.update(directory, NOW - timedelta(days=1), NOW)
        assert len(rows) == 1 and meta["state"] == "ready"
        assert meta["archives_read_this_call"] == 0
    index.close()


@pytest.mark.parametrize("strong_in_history", [True, False])
def test_global_session_winner_before_slice_both_directions(tmp_path, strong_in_history):
    directory = tmp_path / "archive"
    strong = session_event(ts=(NOW - timedelta(days=20)).isoformat(),
                           input_token_usage=measurement(None, None))
    weak = session_event(ts=(NOW - timedelta(days=10)).isoformat())
    write_rows(directory / "events_01.jsonl", strong if strong_in_history else weak)
    index = plugin.HistoryIndex(tmp_path / "history.json")
    index.update(directory, None, NOW)
    overlay = [plugin._normalize_usage_event(weak if strong_in_history else strong)]
    rows, _, _ = index.request(directory, NOW - timedelta(days=11), NOW - timedelta(days=9), current=overlay)
    assert rows == []  # A stronger unknown outside the slice must not become a known in-slice count.
    rows, _, _ = index.request(directory, NOW - timedelta(days=21), NOW - timedelta(days=19), current=overlay)
    assert len(rows) == 1 and rows[0]["prompt_tokens"] is None
    index.close()


def test_equal_rank_winner_stays_first_across_restart(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", session_event(prompt_tokens=100, cached_tokens=25))
    write_rows(directory / "events_02.jsonl", session_event(prompt_tokens=200, cached_tokens=100))
    path = tmp_path / "history.json"
    first = plugin.HistoryIndex(path)
    a = first.update(directory, None, NOW)[0]
    first.close()
    second = plugin.HistoryIndex(path)
    b = second.update(directory, None, NOW)[0]
    assert a == b and b[0]["prompt_tokens"] == 100
    second.close()


def test_preceding_boot_survives_history_filter(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", boot("a" * 40, 90), boot("b" * 40, 45), boot("b" * 40, 30))
    index = plugin.HistoryIndex(tmp_path / "history.json")
    rows, boots, _ = index.update(directory, NOW - timedelta(hours=1), NOW)
    data = plugin.calculate_analytics(rows, "1H", now=NOW, boots=boots)
    assert [m["code"] for m in data["markers"]] == ["b" * 9]
    assert 0 <= data["markers"][0]["position"] < 12
    index.close()


def test_removed_archive_keeps_persisted_measurements_with_disclosure(tmp_path):
    directory = tmp_path / "archive"
    source = directory / "events_01.jsonl"
    write_rows(source, event())
    path = tmp_path / "history.json"
    first = plugin.HistoryIndex(path)
    first.update(directory, None, NOW)
    first.close()
    source.unlink()
    second = plugin.HistoryIndex(path)
    rows, _, meta = second.update(directory, None, NOW)
    assert len(rows) == 1 and meta["retained_sources_absent"] == 1
    second.close()


def test_corrupt_index_is_preserved_and_rebuilt(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    path = tmp_path / "history.json"
    path.write_text("{bad index", encoding="utf-8")
    index = plugin.HistoryIndex(path)
    rows, _, meta = index.update(directory, None, NOW)
    backups = list(tmp_path.glob("history.json.unreadable-*"))
    assert len(backups) == 1 and backups[0].read_text() == "{bad index"
    assert len(rows) == 1 and meta["recovery"]
    assert json.loads(path.read_text())["version"] == 1
    index.close()


def test_atomic_save_failure_keeps_previous_index_and_retry_publishes(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    index = plugin.HistoryIndex(tmp_path / "history.json")
    index.update(directory, None, NOW)
    before = index.path.read_bytes()
    write_rows(directory / "events_02.jsonl", event(ledger_attempt_ids=["a2"], model="two"))
    original = index._save
    def fail_save():
        raise OSError("full disk")
    monkeypatch.setattr(index, "_save", fail_save)
    rows, _, meta = index.update(directory, None, NOW)
    assert len(rows) == 1 and meta["state"] == "degraded"
    assert index.path.read_bytes() == before
    monkeypatch.setattr(index, "_save", original)
    rows, _, meta = index.update(directory, None, NOW)
    assert len(rows) == 2 and meta["state"] == "ready"
    index.close()


def test_unreadable_archive_degrades_and_can_retry(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    source = directory / "events_01.jsonl"
    write_rows(source, event())
    original = Path.open
    def refuse(path, *args, **kwargs):
        if path == source:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)
    index = plugin.HistoryIndex(tmp_path / "history.json")
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", refuse)
        _, _, meta = index.update(directory, None, NOW)
    assert meta["state"] == "degraded" and "PermissionError" in meta["read_error"]
    assert index.update(directory, None, NOW)[2]["state"] == "ready"
    index.close()


def test_close_during_backfill_prevents_late_publish(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    index = plugin.HistoryIndex(tmp_path / "history.json")
    original = index._load
    def held_load():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(index, "_load", held_load)
    index.request(tmp_path / "archive", None, NOW)
    assert entered.wait(2)
    index._stop.set()
    release.set()
    finish_index(index)
    assert index._meta["revision"] == 0
    assert not index.path.exists()
    index.close()


@pytest.mark.parametrize("preset", ["MONTH", "ALL", "CUSTOM"])
def test_full_route_backfills_old_data_without_live_duplicate(tmp_path, monkeypatch, preset):
    archive = tmp_path / "archive"
    old = event(ts=(NOW - timedelta(days=20)).isoformat(), ledger_attempt_ids=["old"], model="old")
    current = event(model="current")
    write_rows(archive / "events_old.jsonl", old)
    write_rows(archive / "events_new.jsonl", current)
    write_rows(tmp_path / "logs/events.jsonl", current)
    _, route, unload = api_for(tmp_path, monkeypatch)
    query = dict(timeframe=preset, daily="1", timezone="Europe/Moscow")
    if preset == "CUSTOM":
        query.update({"from": (NOW - timedelta(days=21)).date().isoformat(), "to": NOW.date().isoformat()})
    try:
        read_route(route, **query)
        finish_index(plugin._HISTORY)
        result = read_route(route, **query)
        assert result["status"] == "ok"
        assert result["summary"]["request_count"] == 2
        assert result["summary"]["prompt_tokens"] == 200
        assert sum(b["prompt_tokens"] or 0 for b in result["buckets"]) == 200
        assert len(result["buckets"]) >= 20
    finally:
        unload[0]()


def test_register_after_unload_gets_new_usable_index(tmp_path, monkeypatch):
    write_rows(tmp_path / "logs/events.jsonl", event())
    api, route, unload = api_for(tmp_path, monkeypatch)
    first = plugin._HISTORY
    unload[0]()
    assert plugin._HISTORY is None
    plugin.register(api)
    assert plugin._HISTORY is not first and not plugin._HISTORY._stop.is_set()
    unload[-1]()


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_local_day_edges_and_hourly_detail_across_dst(day, hours):
    zone = "America/New_York"
    start = plugin._custom_bound(day, zone=zone)
    end = plugin._custom_bound(day, end=True, zone=zone)
    assert (end - start).total_seconds() == hours * 3600
    now = end + timedelta(days=1)
    records = [plugin._normalize_usage_event(event(ts=t.isoformat(), ledger_attempt_ids=[str(i)]))
               for i, t in enumerate((start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end))]
    data = plugin.calculate_analytics(records, "CUSTOM", start_override=start, end_override=end,
                                      calendar_daily=True, now=now, zone=zone)
    assert len(data["buckets"]) == 1 and data["summary"]["request_count"] == 2
    assert data["buckets"][0]["day"] == day and not data["buckets"][0]["partial"]
    hourly = plugin.calculate_analytics(records, "CUSTOM", start_override=start, end_override=end,
                                        now=now, zone=zone, hourly=True)
    assert len(hourly["buckets"]) == hours
    assert sum(b["request_count"] for b in hourly["buckets"]) == 2


def test_unknown_and_measured_zero_share_one_arithmetic_contract():
    source = [event(prompt_tokens=100, cached_tokens=80, ledger_attempt_ids=["a"]),
              event(prompt_tokens=900, cached_tokens=0, ledger_attempt_ids=["b"]),
              event(prompt_tokens=500, cached_tokens=None, ledger_attempt_ids=["c"]),
              event(prompt_tokens=None, cached_tokens=None, ledger_attempt_ids=["d"]),
              event(prompt_tokens=0, cached_tokens=0, ledger_attempt_ids=["e"])]
    data = plugin.calculate_analytics([plugin._normalize_usage_event(r) for r in source], "1H", now=NOW)
    s = data["summary"]
    assert (s["prompt_tokens"], s["eligible_prompt_tokens"], s["eligible_cached_tokens"], s["uncached_tokens"], s["unknown_prompt_tokens"]) == (1500, 1000, 80, 920, 500)
    assert s["cache_read_rate"] == 8 and s["unknown_read_records"] == 2 and s["unknown_total_records"] == 1
    assert s["zero_volume_records"] == 1
    assert sum((b["eligible_cached_tokens"] + b["uncached_tokens"] + b["unknown_prompt_tokens"]) for b in data["buckets"]) == 1500


def test_history_errors_are_top_level_degraded():
    data = plugin.calculate_analytics([], meta={"history_index": {"state": "degraded", "read_error": "fixture_error"}}, now=NOW)
    assert data["status"] == "degraded" and data["read_error"] == "fixture_error"


def test_all_preserves_more_than_120_daily_buckets():
    old = plugin._normalize_usage_event(event(ts=(NOW - timedelta(days=150)).isoformat()))
    data = plugin.calculate_analytics([old], "ALL", calendar_daily=True, now=NOW)
    assert len(data["buckets"]) == 151 and data["quality"]["buckets_omitted"] == 0


def test_index_holds_only_hashed_request_identity_not_task_id(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event(task_id="private-example-task"))
    index = plugin.HistoryIndex(tmp_path / "history.json")
    rows, _, _ = index.update(directory, None, NOW)
    assert rows[0]["identity"].startswith("request:")
    assert "private-example-task" not in index.path.read_text()
    index.close()


def test_unload_with_an_open_save_cannot_corrupt_replacement_index(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    old, new = plugin.HistoryIndex(path), plugin.HistoryIndex(path)
    old._entries = {"old": "x" * 10000}
    new._entries = {"new": "saved"}
    entered, release = threading.Event(), threading.Event()
    original = json.dump
    filenames = []
    def held_dump(data, stream, **kwargs):
        filenames.append(stream.name)
        if threading.current_thread() is old._thread:
            stream.write(" ")
            stream.flush()
            entered.set()
            assert release.wait(5)
        return original(data, stream, **kwargs)
    monkeypatch.setattr(json, "dump", held_dump)
    old._thread = threading.Thread(target=old._save)
    old._thread.start()
    try:
        assert entered.wait(2)
        old.close()  # Bounded join returns with the old file still open.
        assert old._thread.is_alive()
        new._save()
        saved = path.read_bytes()
    finally:
        release.set()
        old._thread.join(2)
        new.close()
    assert not old._thread.is_alive()
    assert path.read_bytes() == saved
    assert json.loads(saved)["archives"] == {"new": "saved"}
    assert len(set(filenames)) == 2 and not list(tmp_path.glob("*.tmp"))


def test_recent_detail_uses_available_global_winner_before_filter(tmp_path, monkeypatch):
    horizon = NOW - timedelta(days=7)
    strong = session_event(ts=(horizon-timedelta(minutes=1)).isoformat(),
                           input_token_usage=measurement(None, None))
    weak = session_event(ts=(horizon+timedelta(minutes=1)).isoformat())
    other = session_event(ts=(horizon+timedelta(minutes=1)).isoformat(), run_id="different",
                          prompt_tokens=100, cached_tokens=80)
    old_source = tmp_path / "archive/events_01.jsonl"
    write_rows(old_source, strong)
    os.utime(old_source, ((horizon-timedelta(minutes=1)).timestamp(),) * 2)
    write_rows(tmp_path / "archive/events_02.jsonl", weak, other)
    write_rows(tmp_path / "logs/events.jsonl", event())
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="MONTH", daily="1")
        finish_index(plugin._HISTORY)
        query = {"timeframe": "CUSTOM", "from": horizon.isoformat(),
                 "to": (horizon+timedelta(hours=1)).isoformat(), "granularity": "hour"}
        detail = read_route(route, **query)
        assert detail["status"] == "ok"
        assert detail["summary"]["session_count"] == 1
        assert detail["summary"]["prompt_tokens"] == 100
        assert sum(b["session_count"] for b in detail["buckets"]) == 1
    finally:
        unload[0]()


def test_old_unrotated_live_records_are_in_all_and_custom_incrementally(tmp_path, monkeypatch):
    live = tmp_path / "logs/events.jsonl"
    old = event(ts=(NOW-timedelta(days=20)).isoformat(), ledger_attempt_ids=["old-live"])
    write_rows(live, old, event())
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="ALL", daily="1")
        finish_index(plugin._HISTORY)
        result = read_route(route, timeframe="ALL", daily="1")
        assert result["status"] == "ok" and result["summary"]["request_count"] == 2
        lines = plugin._EVENTS.raw_stats["total_lines_read"]
        query = {"timeframe":"CUSTOM", "from":(NOW-timedelta(days=21)).isoformat(),
                 "to":(NOW-timedelta(days=19)).isoformat()}
        assert read_route(route, **query)["summary"]["prompt_tokens"] == 100
        assert plugin._EVENTS.raw_stats["total_lines_read"] == lines
        with live.open("a") as stream:
            stream.write(json.dumps({**old, "ledger_attempt_ids":["old-live-2"]})+"\n")
        assert read_route(route, **query)["summary"]["prompt_tokens"] == 200
        assert plugin._EVENTS.raw_stats["total_lines_read"] == lines+1
    finally:
        unload[0]()
