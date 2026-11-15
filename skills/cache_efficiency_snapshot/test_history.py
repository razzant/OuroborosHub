"""History index v2: proportional persistence, exact historical ranges and safe migration."""
import json
import os
import random
import sqlite3
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
                          get_state_dir=lambda: str(tmp_path / "skill-state"),
                          register_route=lambda name, fn, **kw: routes.update({name: fn}),
                          register_ui_tab=lambda *a, **kw: None, on_unload=unload.append)
    plugin.register(api)
    return api, routes["data"], unload


def finish_index(index):
    thread = index._thread
    assert thread is not None
    thread.join(10)
    assert not thread.is_alive()


def read_route(route, **query):
    return route({"query_params": query})


def new_index(path, legacy=None):
    return plugin.make_history_index(path, legacy)


def requests_in(view, start=None, end=NOW):
    start_us = plugin.to_us(start) if start else 0
    return sum(vector[0] for _, _, vector, _, _ in view.cells(start_us, plugin.to_us(end)))


def test_history_reads_are_off_request_path_and_coalesced(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    index = new_index(tmp_path / "history")
    original = index._open
    def held_open():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(index, "_open", held_open)
    try:
        index.request(tmp_path / "archive")
        assert entered.wait(2)
        thread = index._thread
        for _ in range(10):
            index.request(tmp_path / "archive")
            assert index._thread is thread and index.view() is None
            assert index.meta()["state"] == "loading"
    finally:
        release.set()
        finish_index(index)
        index.close()


def test_unchanged_history_neither_reads_nor_rebuilds(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    index = new_index(tmp_path / "history")
    view = index.update(directory)
    assert view.total_records() == 1
    def forbidden(*args, **kwargs):
        raise AssertionError("an unchanged archive was read or the cells rebuilt")
    monkeypatch.setattr(index, "_read_lines", forbidden)
    monkeypatch.setattr(index, "_rebuild_cells", forbidden)
    for _ in range(3):
        index._last_check = 0
        again = index.update(directory)
        assert again.total_records() == 1 and index.meta()["state"] == "ready"
        assert index.meta()["archives_read_last_pass"] == 0
    index.close()


def test_a_new_archive_costs_only_its_own_rows(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    for day in range(20):
        write_rows(directory / f"events_{day:02d}.jsonl",
                   *(event(ts=(NOW - timedelta(days=30 - day, minutes=m)).isoformat(), ledger_attempt_ids=[f"{day}-{m}"])
                     for m in range(50)))
    index = new_index(tmp_path / "history")
    assert index.update(directory).total_records() == 1000
    read = []
    original = index._read_lines
    monkeypatch.setattr(index, "_read_lines", lambda path, identity, consume: read.append(path.name) or original(path, identity, consume))
    monkeypatch.setattr(index, "_rebuild_cells", lambda *a: (_ for _ in ()).throw(AssertionError("whole rebuild")))
    write_rows(directory / "events_99.jsonl", event(ledger_attempt_ids=["new"]))
    statted = []
    real_scandir = plugin._history_module.os.scandir
    class Counted:
        def __init__(self, entry): self._entry, self.name = entry, entry.name
        def is_file(self): return self._entry.is_file()
        def stat(self): statted.append(self.name); return self._entry.stat()
    class Listing:
        def __init__(self, path): self._it = real_scandir(path)
        def __enter__(self): return (Counted(e) for e in self._it.__enter__())
        def __exit__(self, *exc): return self._it.__exit__(*exc)
    monkeypatch.setattr(plugin._history_module.os, "scandir", Listing)
    index._last_check = 0
    assert index.update(directory).total_records() == 1001
    assert read == ["events_99.jsonl"] and statted == ["events_99.jsonl"]  # metadata of the new name only
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT count(*) FROM segments").fetchone() == (21,)
    index.close()


@pytest.mark.parametrize("strong_in_history", [True, False])
def test_global_session_winner_before_slice_both_directions(tmp_path, monkeypatch, strong_in_history):
    strong = session_event(ts=(NOW - timedelta(days=20)).isoformat(), input_token_usage=measurement(None, None))
    weak = session_event(ts=(NOW - timedelta(days=10)).isoformat())
    archive = tmp_path / "archive" / "events_01.jsonl"
    write_rows(archive, strong if strong_in_history else weak)
    os.utime(archive, ((NOW - timedelta(days=9)).timestamp(),) * 2)
    write_rows(tmp_path / "logs/events.jsonl", weak if strong_in_history else strong)
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="ALL", daily="1")
        finish_index(plugin._HISTORY)
        day = lambda offset: (NOW - timedelta(days=offset)).date().isoformat()
        # A stronger unknown outside the slice must not leave a weaker known count inside it.
        assert read_route(route, timeframe="CUSTOM", **{"from": day(11), "to": day(9)})["summary"]["session_count"] == 0
        held = read_route(route, timeframe="CUSTOM", **{"from": day(21), "to": day(19)})["summary"]
        assert held["session_count"] == 1 and held["prompt_tokens"] is None
    finally:
        unload[0]()


def test_equal_rank_winner_is_the_first_archive_across_restart_and_ingest_order(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", session_event(prompt_tokens=100, cached_tokens=25))
    write_rows(directory / "events_02.jsonl", session_event(prompt_tokens=200, cached_tokens=100))
    path = tmp_path / "history"
    first = new_index(path)
    a = first.update(directory).sessions  # archives are indexed newest first
    first.close()
    second = new_index(path)
    b = second.update(directory).sessions
    assert list(a.values())[0]["prompt_tokens"] == list(b.values())[0]["prompt_tokens"] == 100
    second.close()


def test_preceding_boot_survives_history_filter(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", boot("a" * 40, 90), boot("b" * 40, 45), boot("b" * 40, 30))
    index = new_index(tmp_path / "history")
    view = index.update(directory)
    boots = view.boots_between(NOW - timedelta(hours=1), NOW)
    data = plugin.calculate_analytics([], "1H", now=NOW, boots=boots)
    assert [m["code"] for m in data["markers"]] == ["b" * 9]
    assert 0 <= data["markers"][0]["position"] < 12
    index.close()


def test_removed_archive_keeps_persisted_measurements_with_disclosure(tmp_path):
    directory = tmp_path / "archive"
    source = directory / "events_01.jsonl"
    write_rows(source, event(), session_event(), boot("a" * 40, 5))
    path = tmp_path / "history"
    first = new_index(path)
    first.update(directory)
    first.close()
    source.unlink()
    second = new_index(path)
    view = second.update(directory)
    assert view.total_records() == 1 and len(view.sessions) == 1 and len(view.boots_between(None, NOW)) == 1
    assert second.meta()["retained_sources_absent"] == 1
    second.close()


def test_unreadable_index_is_preserved_and_rebuilt(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    path = tmp_path / "history"
    path.mkdir()
    (path / "history.sqlite3").write_bytes(b"not a database at all" * 100)
    index = new_index(path)
    view = index.update(directory)
    backups = list(path.glob("history.sqlite3.unreadable-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"not a database at all" * 100
    assert view.total_records() == 1 and "unreadable" in index.meta()["recovery"]
    assert index.meta()["rebuild_incomplete"] is True and "not included" in index.meta()["recovery"]
    index.close()
    again = new_index(path)  # the incomplete rebuild stays disclosed after a restart
    again.update(directory)
    assert again.meta()["rebuild_incomplete"] is True and "not included" in again.meta()["recovery"]
    again.close()


def test_failed_write_keeps_the_published_reading_and_a_later_pass_saves_it(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    index = new_index(tmp_path / "history")
    index.update(directory)
    write_rows(directory / "events_02.jsonl", event(ledger_attempt_ids=["a2"], model="two"))
    original = index._stage
    def full_disk(*args, **kwargs):
        original(*args, **kwargs)  # rows written inside the transaction, then the disk fills
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(index, "_stage", full_disk)
    index._last_check = 0
    view = index.update(directory)
    assert view.total_records() == 1 and index.meta()["state"] == "degraded"
    assert index.meta()["read_error"] == "history_save_error:OperationalError"
    with sqlite3.connect(index.path) as db:  # the rolled-back batch left nothing behind
        assert db.execute("SELECT count(*) FROM segments").fetchone() == (1,)
    monkeypatch.setattr(index, "_stage", original)
    view = index.update(directory)  # a failed pass retries without waiting for the cadence
    assert view.total_records() == 2 and index.meta()["state"] == "ready"
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
    index = new_index(tmp_path / "history")
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", refuse)
        index.update(directory)
    assert index.meta()["state"] == "degraded" and "PermissionError" in index.meta()["read_error"]
    assert index.update(directory).total_records() == 1 and index.meta()["state"] == "ready"
    index.close()


def test_close_during_backfill_prevents_late_publish(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    index = new_index(tmp_path / "history")
    original = index._open
    def held_open():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(index, "_open", held_open)
    index.request(tmp_path / "archive")
    assert entered.wait(2)
    index._stop.set()
    release.set()
    finish_index(index)
    assert index.view() is None and index.meta()["revision"] == 0
    index.close()


def test_torn_and_malformed_archive_content_is_disclosed_once_not_retried(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    good = json.dumps(event(ledger_attempt_ids=["good"]))
    tail = json.dumps(event(ledger_attempt_ids=["tail"]))
    (directory).mkdir()
    (directory / "events_01.jsonl").write_text(good + "\n" + '{"type": "llm_usage", broken\n' + tail)  # no final newline
    (directory / "events_02.jsonl").write_text(good.replace('"good"', '"cut"') + "\n" + tail[: len(tail) // 2])
    write_rows(directory / "events_03.jsonl", boot("", 5), boot("a" * 40, 4))
    index = new_index(tmp_path / "history")
    view = index.update(directory)
    meta = index.meta()
    assert view.total_records() == 3  # both complete lines and the whole record left without its newline
    assert (meta["torn_tails"], meta["malformed_lines"], meta["boots_without_code"]) == (2, 2, 1)
    assert meta["state"] == "ready" and meta["read_error"] is None
    read = []
    monkeypatch.setattr(index, "_read_lines", lambda *a: read.append(a) or (_ for _ in ()).throw(AssertionError))
    index._last_check = 0
    index.update(directory)
    assert read == [] and index.meta()["state"] == "ready"
    index.close()


def test_duplicate_requests_count_once_and_a_changed_archive_hands_over(tmp_path):
    directory = tmp_path / "archive"
    shared = event(ledger_attempt_ids=["shared"], ts=(NOW - timedelta(days=9)).isoformat())
    write_rows(directory / "events_01.jsonl", shared, event(ledger_attempt_ids=["one"], ts=(NOW - timedelta(days=9)).isoformat()))
    write_rows(directory / "events_02.jsonl", shared)
    index = new_index(tmp_path / "history")
    view = index.update(directory)
    assert view.total_records() == 2 and index.meta()["duplicate_requests"] == 1
    # The archive whose copy counted is rewritten without that request: the other copy takes over.
    counted_in_02 = index._segments["events_02.jsonl"]["facts"]["duplicates"] == 0
    rewritten = directory / ("events_02.jsonl" if counted_in_02 else "events_01.jsonl")
    write_rows(rewritten, event(ledger_attempt_ids=["replacement"], ts=(NOW - timedelta(days=9)).isoformat()))
    # Archives are immutable by the core's contract, so a pass is triggered by the next rotation, which
    # also re-checks every archive's identity.
    write_rows(directory / "events_03.jsonl", event(ledger_attempt_ids=["rotation"]))
    index._last_check = index._last_verify = 0  # the periodic identity check (also run at every load)
    view = index.update(directory)
    expected = 4 if counted_in_02 else 3
    assert view.total_records() == expected
    record = plugin._normalize_usage_event(shared)
    assert view.contains(plugin.identity_fingerprint(record["identity"]))
    index.close()


def test_new_arithmetic_rebuilds_cells_from_rows(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event(), event(ledger_attempt_ids=["b"], cached_tokens=None))
    path = tmp_path / "history"
    first = new_index(path)
    before = list(first.update(directory).cells(0, plugin.to_us(NOW)))
    first.close()
    with sqlite3.connect(path / "history.sqlite3") as db:
        db.execute("UPDATE meta SET value = '0' WHERE key = 'vector'")
        db.execute("UPDATE cells SET vector = '[0]'")
    second = new_index(path)
    after = list(second.update(directory).cells(0, plugin.to_us(NOW)))
    assert [(c, g, tuple(v)) for c, g, v, _, _ in after] == [(c, g, tuple(v)) for c, g, v, _, _ in before]
    second.close()


def test_index_holds_only_hashed_request_identity_not_task_or_prompt(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event(task_id="private-example-task", prompt_text="secret words"))
    index = new_index(tmp_path / "history")
    index.update(directory)
    index.close()
    stored = b"".join(p.read_bytes() for p in (tmp_path / "history").iterdir())
    assert b"private-example-task" not in stored and b"secret words" not in stored


def test_two_instances_on_one_index_never_corrupt_it(tmp_path):
    directory = tmp_path / "archive"
    for day in range(6):
        write_rows(directory / f"events_{day}.jsonl", event(ledger_attempt_ids=[str(day)], model=f"m{day}"))
    path = tmp_path / "history"
    old, new = new_index(path), new_index(path)
    old.update(directory)
    view = new.update(directory)  # loaded before or after: either way each archive counts once
    assert view.total_records() == 6
    old.close()
    new.close()
    third = new_index(path)
    assert third.update(directory).total_records() == 6
    third.close()


def test_memory_index_without_a_state_directory(tmp_path):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    index = new_index(None)
    assert index.update(directory).total_records() == 1 and index.meta()["storage"] == "memory"
    index.close()
    assert not list(tmp_path.glob("**/*.sqlite3"))


# ---------------------------------------------------------------------------
# The earlier JSON index: read once, never written; retired sources are restored
# ---------------------------------------------------------------------------

def legacy_index(path, archive_dir, entries):
    """A 0.8.x history-index.json with normalized records as that version stored them."""
    def encode(record):
        result = dict(record)
        result["ts"] = record["ts"].isoformat()
        return result
    archives = {}
    for name, (identity, records, sessions, boots) in entries.items():
        archives[str(archive_dir / name)] = {"identity": identity, "records": [encode(r) for r in records],
                                             "sessions": [encode(r) for r in sessions],
                                             "boots": [encode(b) for b in boots], "malformed": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "archives": archives}, separators=(",", ":")))
    return path.read_bytes()


def v1_record(**values):
    """How 0.8.1 normalized a request: zeros were kept as measured zeros."""
    record = plugin._normalize_usage_event(event(**values))
    record.pop("legacy_zeros")
    return record


def test_legacy_index_restores_retired_sources_once_and_is_never_written(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    old_ts = NOW - timedelta(days=40)
    write_rows(archive / "events_extant.jsonl",
               {k: v for k, v in event(ts=(NOW - timedelta(days=30)).isoformat(), ledger_attempt_ids=["extant"],
                                       cached_tokens=0).items() if k != "record_contract_version"})
    retired_records = [v1_record(ts=old_ts.isoformat(), ledger_attempt_ids=["r1"], cached_tokens=0, cache_write_tokens=0),
                       v1_record(ts=old_ts.isoformat(), ledger_attempt_ids=["r2"], prompt_tokens=200, cached_tokens=150, cache_write_tokens=10)]
    normalized_session = plugin._normalize_usage_event(session_event(ts=old_ts.isoformat(), run_id="kept",
                                                                     input_token_usage=measurement(300, 0, 0)))
    normalized_session.pop("legacy_zeros")
    legacy_session = plugin._normalize_usage_event(session_event(ts=old_ts.isoformat(), run_id="codex", cached_tokens=0))
    legacy_session.update(prompt_tokens=2043012, cached_tokens=0, measured=True, eligible=True)
    legacy_session.pop("legacy_zeros")
    old_boot = plugin._boot_from_event({**boot("c" * 40, 0), "ts": old_ts.isoformat()})
    extant_v1 = v1_record(ts=(NOW - timedelta(days=30)).isoformat(), ledger_attempt_ids=["extant"], cached_tokens=0)
    legacy_path = tmp_path / "state" / "cache_efficiency_snapshot" / "history-index.json"
    original = legacy_index(legacy_path, archive, {
        "events_extant.jsonl": ([1, 2], [extant_v1], [], []),
        "events_retired.jsonl": ([3, 4], retired_records, [normalized_session, legacy_session], [old_boot]),
    })
    stat_before = legacy_path.stat()
    write_rows(tmp_path / "logs/events.jsonl")  # an idle live log
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="ALL", daily="1")
        finish_index(plugin._HISTORY)
        meta = plugin._HISTORY.meta()
        assert meta["restored_sources"] == 1 and meta["retained_sources_absent"] == 1
        result = read_route(route, timeframe="ALL", daily="1")
        s = result["summary"]
        assert result["status"] == "ok"
        assert (s["request_count"], s["session_count"]) == (3, 2)
        # Retired measurements survive; their zeros have no contract stamp, so they are unknown.
        assert s["legacy_zero_read_records"] == 2 + 1  # r1, the re-parsed unstamped extant row, the legacy session
        assert s["legacy_zero_write_records"] == 2  # r1 and the re-parsed unstamped extant row
        assert s["prompt_tokens"] == 100 + 200 + 100 + 300 + 2043012
        assert s["eligible_prompt_tokens"] == 200 + 300 and s["eligible_cached_tokens"] == 150 + 0
        assert [m["code"] for m in result["markers"]] == []  # one code start: no predecessor, no mark
        assert "Restored 1" in meta["recovery"]
    finally:
        unload[0]()
    assert legacy_path.read_bytes() == original and legacy_path.stat().st_mtime_ns == stat_before.st_mtime_ns
    # A restart reads the migrated index; the earlier one is not imported twice.
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="ALL", daily="1")
        finish_index(plugin._HISTORY)
        assert read_route(route, timeframe="ALL", daily="1")["summary"]["request_count"] == 3
    finally:
        unload[0]()


def test_an_unreadable_legacy_index_is_left_alone_and_disclosed(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_text("{bad index")
    index = new_index(tmp_path / "history", legacy)
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    assert index.update(directory).total_records() == 1
    with sqlite3.connect(index.path) as db:
        report = json.loads(db.execute("SELECT value FROM meta WHERE key = 'legacy'").fetchone()[0])
    assert report["status"].startswith("unreadable") and legacy.read_text() == "{bad index"
    index.close()


# ---------------------------------------------------------------------------
# Routes: exact historical ranges, published readings and recovery
# ---------------------------------------------------------------------------

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
        # A cold reading waits briefly for the first pass, so it already holds the archived history.
        result = read_route(route, **query)
        assert result["status"] == "ok"
        assert result["summary"]["request_count"] == 2
        assert result["summary"]["prompt_tokens"] == 200
        assert sum(b["prompt_tokens"] or 0 for b in result["buckets"]) == 200
        assert len(result["buckets"]) >= 20
    finally:
        unload[0]()


def test_a_published_index_stays_ready_while_it_checks_for_new_archives(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    write_rows(archive / "events_old.jsonl", event(ts=(NOW - timedelta(days=20)).isoformat(), ledger_attempt_ids=["old"]))
    write_rows(tmp_path / "logs/events.jsonl", event())
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="MONTH", daily="1")
        finish_index(plugin._HISTORY)
        entered, release = threading.Event(), threading.Event()
        original = plugin._HISTORY._pass
        def slow_pass(directory):
            entered.set()
            assert release.wait(5)
            return original(directory)
        monkeypatch.setattr(plugin._HISTORY, "_pass", slow_pass)
        plugin._HISTORY._last_check = 0
        detail_day = (NOW - timedelta(days=20)).date().isoformat()
        for query in ({"timeframe": "MONTH", "daily": "1"}, {"timeframe": "ALL", "daily": "1"}, {"timeframe": "24H"},
                      {"timeframe": "CUSTOM", "from": detail_day, "to": detail_day, "granularity": "hour"}):
            result = read_route(route, **query)
            assert entered.wait(2)
            history = result["quality"]["history_index"]
            assert result["status"] == "ok" and result["summary"]["total_records"] >= 1, query
            assert history["state"] == "ready" and history["refreshing"] is True
        release.set()
        finish_index(plugin._HISTORY)
        assert plugin._HISTORY.meta()["refreshing"] is False
    finally:
        release.set()
        unload[0]()


def test_historical_ranges_equal_the_exact_reading_at_any_boundaries(tmp_path, monkeypatch):
    """Cells, refined edges and the recent reader together give the same figures as every record read exactly."""
    generator = random.Random(11)
    archive = tmp_path / "archive"
    rows, start = [], NOW - timedelta(days=45)
    for index in range(1500):
        stamp = start + timedelta(seconds=generator.randint(0, 45 * 86400 - 1))
        values = dict(ts=stamp.isoformat(), ledger_attempt_ids=[str(index)],
                      provider=generator.choice(["openai", "anthropic"]), model=generator.choice(["a", "b", "c"]),
                      prompt_tokens=generator.choice([None, 0, 50, 400, 1200]),
                      cached_tokens=generator.choice([None, 0, 10, 300]),
                      cache_write_tokens=generator.choice([None, 0, 5, 100]))
        source = event(**values)
        if generator.random() < 0.4:
            del source["record_contract_version"]
        rows.append(source)
    rows.sort(key=lambda r: r["ts"])
    for chunk in range(0, len(rows), 100):
        part = rows[chunk:chunk + 100]
        path = archive / f"events_{chunk:05d}.jsonl"
        write_rows(path, *part)
        os.utime(path, ((datetime.fromisoformat(part[-1]["ts"]) + timedelta(seconds=1)).timestamp(),) * 2)
    exact = [plugin._normalize_usage_event(r) for r in rows]
    write_rows(tmp_path / "logs/events.jsonl")  # an idle live log
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="ALL", daily="1")
        finish_index(plugin._HISTORY)
        cases = [dict(timeframe="ALL", daily="1", timezone="Asia/Kathmandu"), dict(timeframe="MONTH", daily="1", timezone="America/St_Johns"),
                 dict(timeframe="ALL"), dict(timeframe="CUSTOM", daily="1", timezone="Europe/Moscow",
                                             **{"from": (NOW - timedelta(days=40)).date().isoformat(), "to": NOW.date().isoformat()})]
        # +05:30, and a 23-hour local day (America/Santiago moved to summer time on 2026-09-06).
        dst_from, dst_to = plugin._custom_bound("2026-09-06", zone="America/Santiago"), plugin._custom_bound("2026-09-06", end=True, zone="America/Santiago")
        assert (dst_to - dst_from) == timedelta(hours=23)
        cases += [dict(timeframe="ALL", daily="1", timezone="Asia/Kolkata"),
                  dict(timeframe="MONTH", daily="1", timezone="America/Santiago"),
                  dict(timeframe="CUSTOM", daily="1", timezone="Asia/Kolkata", **{"from": "2026-08-01", "to": "2026-08-20"}),
                  dict(timeframe="CUSTOM", granularity="hour", timezone="America/Santiago",
                       **{"from": dst_from.isoformat(), "to": dst_to.isoformat()}),
                  dict(timeframe="CUSTOM", granularity="hour", timezone="Asia/Kolkata",
                       **{"from": plugin._custom_bound("2026-08-15", zone="Asia/Kolkata").isoformat(),
                          "to": plugin._custom_bound("2026-08-15", end=True, zone="Asia/Kolkata").isoformat()})]
        for _ in range(6):
            a = start + timedelta(seconds=generator.randint(0, 40 * 86400))
            b = a + timedelta(seconds=generator.randint(60, 5 * 86400))
            cases.append(dict(timeframe="CUSTOM", **{"from": a.isoformat(), "to": b.isoformat()}))
            cases.append(dict(timeframe="CUSTOM", granularity="hour", **{"from": a.isoformat(), "to": (a + timedelta(hours=7)).isoformat()}))
        for query in cases:
            got = read_route(route, **query)
            zone = query.get("timezone", "UTC")
            want = plugin.calculate_analytics(
                exact, query["timeframe"], now=NOW, zone=zone, calendar_daily=query.get("daily") == "1",
                start_override=plugin._custom_bound(query.get("from"), zone=zone),
                end_override=plugin._custom_bound(query.get("to"), end=True, zone=zone),
                hourly=query.get("granularity") == "hour")
            assert got["status"] == "ok", (query, got.get("read_error"))
            assert got["summary"] == want["summary"], query
            assert [{k: v for k, v in b.items()} for b in got["buckets"]] == want["buckets"], query
            assert sorted(map(json.dumps, got["models"])) == sorted(map(json.dumps, want["models"])), query
            assert (got["start"], got["end"], got["quality"]["oldest_ts"], got["quality"]["newest_ts"]) == \
                   (want["start"], want["end"], want["quality"]["oldest_ts"], want["quality"]["newest_ts"]), query
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


@pytest.mark.parametrize("timeframe", ["1H", "6H", "24H", "7D", "CUSTOM"])
def test_short_range_recovers_published_history_after_transient_error(tmp_path, monkeypatch, timeframe):
    directory = tmp_path / "archive"
    write_rows(directory / "events_01.jsonl", event())
    write_rows(tmp_path / "logs/events.jsonl", event())
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe="MONTH", daily="1")
        finish_index(plugin._HISTORY)
        bad = directory / "events_02.jsonl"
        write_rows(bad, event(ts=(NOW-timedelta(days=20)).isoformat(), ledger_attempt_ids=["recovered"]))
        os.utime(bad, ((NOW-timedelta(days=20)).timestamp(),) * 2)
        original = Path.open
        def denied(path, *args, **kwargs):
            if path == bad:
                raise PermissionError("temporary fixture failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", denied)
        plugin._HISTORY._last_check = 0
        read_route(route, timeframe="MONTH", daily="1")
        finish_index(plugin._HISTORY)
        query = {"timeframe": timeframe, "daily": "1"}
        if timeframe == "CUSTOM":
            query.update({"from": (NOW-timedelta(hours=1)).isoformat(), "to": NOW.isoformat()})
        assert read_route(route, **query)["status"] == "degraded"
        monkeypatch.setattr(Path, "open", original)
        read_route(route, **query)  # a failed pass is retried at once, not after the cadence
        finish_index(plugin._HISTORY)
        result = read_route(route, **query)
        assert result["status"] == "ok"
        assert result["quality"]["history_index"]["archives_indexed"] == 2
        assert result["summary"]["request_count"] == 1
    finally:
        unload[0]()


def test_an_interrupted_legacy_import_is_retried_and_never_doubled(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    write_rows(archive / "events_extant.jsonl", event(ledger_attempt_ids=["extant"]))
    old_ts = NOW - timedelta(days=40)
    entries = {f"events_retired_{i}.jsonl": ([i, i], [v1_record(ts=(old_ts + timedelta(minutes=i)).isoformat(),
                                                                ledger_attempt_ids=[f"r{i}"])], [], [])
               for i in range(3)}
    legacy = tmp_path / "old.json"
    legacy_index(legacy, archive, entries)
    index = new_index(tmp_path / "history", legacy)
    original = index._import_entry
    calls = []
    def stop_after_first(name, entry):
        original(name, entry)
        calls.append(name)
        if len(calls) == 1:
            index._stop.set()  # the skill unloads in the middle of the import
    monkeypatch.setattr(index, "_import_entry", stop_after_first)
    index.update(archive)
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT value FROM meta WHERE key = 'legacy'").fetchone() is None
        assert db.execute("SELECT count(*) FROM segments WHERE kind = 'restored'").fetchone() == (1,)
    index._close_db()
    monkeypatch.undo()
    again = new_index(tmp_path / "history", legacy)
    view = again.update(archive)
    assert again.meta()["restored_sources"] == 3 and view.total_records() == 4  # three retired + one extant, none twice
    with sqlite3.connect(again.path) as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key = 'legacy'").fetchone()[0])["status"] == "read"
    again.close()


def test_a_fingerprint_collision_is_never_a_duplicate(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    first, second = event(ledger_attempt_ids=["one"]), event(ledger_attempt_ids=["two"])
    write_rows(directory / "events_01.jsonl", first, second, first)  # the last line is a true replay
    index = new_index(tmp_path / "history")
    monkeypatch.setattr(index, "_fingerprint", lambda identity: 42)  # every identity collides
    view = index.update(directory)
    assert view.total_records() == 2 and index.meta()["fingerprint_collisions"] == 1
    assert index.meta()["duplicate_requests"] == 1
    third = plugin._normalize_usage_event(event(ledger_attempt_ids=["three"]))
    assert not view.holds(third, 42)  # a recent request that only shares the fingerprint still counts
    assert view.holds(plugin._normalize_usage_event(first), 42)
    index.close()


def test_a_changed_archive_replaces_its_copy_only_when_it_reads_cleanly(tmp_path):
    directory = tmp_path / "archive"
    source = directory / "events_01.jsonl"
    rows = [event(ledger_attempt_ids=[str(i)], ts=(NOW - timedelta(days=3, minutes=i)).isoformat()) for i in range(4)]
    write_rows(source, *rows)
    index = new_index(tmp_path / "history")
    assert index.update(directory).total_records() == 4
    # Cut short (a torn last line): the only indexed copy of these rows stays counted, the change is disclosed.
    data = source.read_bytes()
    source.write_bytes(data[: len(data) - 20])
    write_rows(directory / "events_02.jsonl", event(ledger_attempt_ids=["rotation"]))
    index._last_check = index._last_verify = 0  # the periodic identity check (also run at every load)
    view = index.update(directory)
    assert view.total_records() == 5 and index.meta()["changed_sources_kept"] == ["events_01.jsonl"]
    write_rows(directory / "events_03.jsonl", event(ledger_attempt_ids=["rotation-2"]))
    index._last_check = index._last_verify = 0
    reads = []
    original = index._read_lines
    index._read_lines = lambda path, identity, consume: reads.append(path.name) or original(path, identity, consume)
    index.update(directory)
    assert reads == ["events_03.jsonl"]  # judged once; examined again only if the bytes change again
    # A clean replacement (the archive rewritten whole with two of its rows) retracts the earlier copy.
    write_rows(source, *rows[:2])
    write_rows(directory / "events_04.jsonl", event(ledger_attempt_ids=["rotation-3"]))
    index._last_check = index._last_verify = 0
    view = index.update(directory)
    assert view.total_records() == 2 + 3 and index.meta()["changed_sources_kept_count"] == 0
    index.close()
    restarted = new_index(tmp_path / "history")
    assert restarted.update(directory).total_records() == 5
    restarted.close()


def test_a_cold_reading_after_reload_is_complete_without_reading_archives(tmp_path, monkeypatch):
    for minute in range(30):
        write_rows(tmp_path / "archive" / f"events_{minute:03d}.jsonl",
                   event(ledger_attempt_ids=[str(minute)], ts=(NOW - timedelta(hours=2, minutes=minute)).isoformat()))
    write_rows(tmp_path / "logs/events.jsonl", event(ledger_attempt_ids=["live"]))
    _, route, unload = api_for(tmp_path, monkeypatch)
    read_route(route, timeframe="24H")
    finish_index(plugin._HISTORY)
    unload[0]()
    # Reload: a new instance on the preserved index; the first 24h reading is whole and parses no archive.
    _, route, unload = api_for(tmp_path, monkeypatch)
    parsed = []
    original = plugin._HISTORY._read_lines
    monkeypatch.setattr(plugin._HISTORY, "_read_lines", lambda path, identity, consume: parsed.append(path.name) or original(path, identity, consume))
    try:
        result = read_route(route, timeframe="24H")
        assert result["status"] == "ok" and result["summary"]["request_count"] == 31
        assert parsed == [] and plugin._EVENTS.raw_stats["total_lines_read"] == 1
    finally:
        unload[0]()


def test_during_the_first_migration_a_covered_range_is_complete_before_all(tmp_path, monkeypatch):
    for day in range(12):
        path = tmp_path / "archive" / f"events_{day:03d}.jsonl"
        write_rows(path, event(ledger_attempt_ids=[str(day)], ts=(NOW - timedelta(days=11 - day, hours=1)).isoformat()))
        os.utime(path, ((NOW - timedelta(days=11 - day)).timestamp(),) * 2)
    write_rows(tmp_path / "logs/events.jsonl")
    gate, entered = threading.Event(), threading.Event()
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        original = plugin._HISTORY._parse_archive
        def slow(path, stat):
            if path.name == "events_008.jsonl":  # newest first: the last three days are in, older ones wait
                entered.set()
                assert gate.wait(10)
            return original(path, stat)
        monkeypatch.setattr(plugin._HISTORY, "_parse_archive", slow)
        monkeypatch.setattr(plugin, "FIRST_PASS_WAIT_SECONDS", 0.05)
        monkeypatch.setattr(plugin._history_module, "BATCH_ARCHIVES", 1)  # each archive commits and publishes:
        monkeypatch.setattr(plugin._history_module, "PUBLISH_SECONDS", 0)  # the bound follows the published view
        read_route(route, timeframe="24H")
        assert entered.wait(5)
        day = read_route(route, timeframe="24H")
        month = read_route(route, timeframe="MONTH", daily="1")
        assert day["status"] == "ok" and day["summary"]["request_count"] == 1
        assert month["status"] == "loading" and month["summary"]["request_count"] == 3
        assert month["quality"]["history_index"]["archives_done"] < month["quality"]["history_index"]["archives_total"]
        gate.set()
        finish_index(plugin._HISTORY)
        month = read_route(route, timeframe="MONTH", daily="1")
        assert month["status"] == "ok" and month["summary"]["request_count"] == 12
    finally:
        gate.set()
        unload[0]()
