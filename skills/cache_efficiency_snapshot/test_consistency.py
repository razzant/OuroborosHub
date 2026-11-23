"""Public reading invariants for the bounded 0.8.1 consistency pass."""
import json
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_history import (new_index, plugin, NOW, event, boot, session_event, write_rows,
                          api_for, read_route, finish_index, legacy_index, v1_record, measurement)
from test_history_regressions import totals


@pytest.mark.parametrize('known', [False, True])
@pytest.mark.parametrize('missing', [False, True])
def test_archive_stat_failure_is_retryable_but_retention_is_not_error(tmp_path, monkeypatch, known, missing):
    archive = tmp_path / 'archive'
    source = archive / 'events_01.jsonl'
    write_rows(source, event())
    index = new_index(tmp_path / 'history')
    if known:
        index.update(archive)
        write_rows(source, event(prompt_tokens=200))
        index._last_verify = 0
    scan = plugin._history_module.os.scandir
    class Entries:
        def __enter__(self):
            self.inner = scan(archive)
            self.entries = self.inner.__enter__()
            return (SimpleNamespace(name=e.name, is_file=e.is_file, stat=self.stat) for e in self.entries)
        def stat(self):
            if missing:
                source.unlink(missing_ok=True)  # genuine retention between listing and stat
                raise FileNotFoundError('retired')
            raise PermissionError('transient archive stat')
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(plugin._history_module.os, 'scandir', lambda _: Entries())
            index.update(archive)
        meta = index.meta()
        if missing:
            assert meta['state'] == 'ready' and meta['read_error'] is None and meta['complete']
            assert totals(index)['request_count'] == int(known)
            assert meta['retained_sources_absent'] == int(known)
        else:
            assert meta['state'] == 'degraded' and meta['read_error'] == 'history_stat_error:PermissionError'
            # Retried at the next request, which is never a first backfill again (no reading turns back to loading).
            assert meta['complete'] and index._last_verify == 0 and index._retry
            index.request(archive)
            finish_index(index)
            assert index.meta()['complete'] and index.meta()['read_error'] is None
            assert totals(index)['prompt_tokens'] == (200 if known else 100)
    finally:
        index.close()


@pytest.mark.parametrize('phase', ['stat', 'read', 'partial'])
def test_pending_legacy_retries_on_cadence_without_reopening_good_index(tmp_path, monkeypatch, phase):
    archive, legacy = tmp_path / 'archive', tmp_path / 'old.json'
    legacy_index(legacy, archive, {'events_retired.jsonl': ([1, 2], [v1_record()], [], [])})
    write_rows(archive / 'events_live.jsonl', event(ledger_attempt_ids=['other']))
    index = new_index(tmp_path / 'history', legacy)
    clock = [100.0]
    monkeypatch.setattr(plugin._history_module.time, 'monotonic', lambda: clock[0])
    original_import, original_stat = index._import_legacy, Path.stat
    attempts = []
    def fail_import(*args):
        attempts.append(1)
        if phase == 'partial':
            original_import(*args)
        raise PermissionError('temporary legacy read')
    def fail_stat(path, *args, **kwargs):
        if path == legacy:
            attempts.append(1)
            raise PermissionError('temporary legacy stat')
        return original_stat(path, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'stat', fail_stat) if phase == 'stat' else patch.setattr(index, '_import_legacy', fail_import)
            index.request(archive)
            finish_index(index)
            assert index._loaded and index.meta()['legacy_import']['status'] == 'pending:PermissionError'
            assert index.meta()['recovery_incomplete']
            connection = index._db
            first_thread = index._thread
            for _ in range(5):
                index.request(archive)
            assert index._thread is first_thread and len(attempts) == 1
        def forbidden(*a, **kw):
            raise AssertionError('good index must not reopen/reload/rebuild for pending import')
        monkeypatch.setattr(index, '_open', forbidden)
        monkeypatch.setattr(index, '_load', forbidden)
        monkeypatch.setattr(index, '_rebuild_cells', forbidden)
        clock[0] += plugin._history_module.RECHECK_SECONDS + 1
        index.request(archive)
        finish_index(index)
        assert index._db is connection
        assert index.meta()['legacy_import']['status'] == 'read'
        assert index.meta()['legacy_import']['imported'] == 1
        assert not index.meta()['recovery_incomplete']
        assert totals(index)['request_count'] == 2
        index.update(archive)
        assert totals(index)['request_count'] == 2
    finally:
        index.close()


@pytest.mark.parametrize('with_request', [False, True])
def test_observed_span_includes_winning_sessions(tmp_path, with_request):
    early, late = NOW - timedelta(days=10), NOW - timedelta(minutes=1)
    entries = [session_event(ts=(early - timedelta(days=5)).isoformat(), run_id='late'),
               session_event(ts=early.isoformat(), run_id='early'),
               session_event(ts=late.isoformat(), run_id='late', input_token_usage=measurement(100, 50, 0))]
    if with_request:
        entries.append(event())
    write_rows(tmp_path / 'archive/events_01.jsonl', *entries)
    index = new_index(tmp_path / 'history')
    try:
        index.update(tmp_path / 'archive')
        assert index.meta()['indexed_oldest_ts'] == early.isoformat()
        assert index.meta()['indexed_newest_ts'] == late.isoformat()
        assert totals(index)['session_count'] == 2
    finally:
        index.close()


@pytest.mark.parametrize('zone', ['America', 'Europe', 'x' * 300, '../UTC', 'NoSuch/Zone'])
def test_invalid_timezone_returns_typed_error(tmp_path, monkeypatch, zone):
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        assert read_route(route, timezone=zone)['read_error'] == 'invalid_timezone'
        assert plugin._custom_bound('2026-01-01', zone=zone) is None
        assert plugin._custom_bound('2026-01-01T00:00:00Z', zone=zone) is None
    finally:
        unload[0]()


@pytest.mark.parametrize('daily', ['0', '1'])
@pytest.mark.parametrize('bounds', [
    ('0001-01-01T00:00:00Z', '2026-01-01', 'America/New_York'),
    ('2026-01-01', '9999-12-31T23:59:59Z', 'Asia/Kathmandu'),
    ('2026-01-01', '9999-12-31', 'UTC'),
])
def test_unrepresentable_custom_bound_returns_typed_error(tmp_path, monkeypatch, daily, bounds):
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        start, end, zone = bounds
        assert read_route(route, timeframe='CUSTOM', daily=daily, timezone=zone,
                          **{'from': start, 'to': end})['read_error'] == 'invalid_custom_range'
    finally:
        unload[0]()


@pytest.mark.parametrize('frame,hourly', [('CUSTOM', False), ('CUSTOM', True), ('ALL', False)])
def test_marker_omissions_include_hidden_bins_and_marker_cap(monkeypatch, frame, hourly):
    # Both bin truncation and marker truncation, with a predecessor before the window.
    start = NOW - timedelta(days=160 if frame == 'ALL' else 40)
    step = timedelta(hours=24 if frame == 'ALL' else 1 if hourly else 6)
    visible = NOW - step * plugin.MAX_BUCKETS_RETURNED
    times = [start - timedelta(minutes=1), start + timedelta(minutes=1), visible - timedelta(microseconds=1),
             visible, visible + step / 3, NOW - timedelta(seconds=1), NOW]
    boots = [{'ts': ts, 'sha': str(i) * 40, 'branch': 'test'} for i, ts in enumerate(times)]
    monkeypatch.setattr(plugin, 'MAX_MARKERS_RETURNED', 2)
    records = [plugin._normalize_usage_event(event(ts=start.isoformat()))]
    got = plugin.calculate_analytics(records, frame, now=NOW, start_override=start, end_override=NOW,
                                      hourly=hourly, boots=boots)
    assert got['quality']['buckets_omitted'] > 0
    assert got['quality']['markers_omitted'] == 3  # two hidden bins plus one capped visible marker
    assert [m['code'] for m in got['markers']] == ['4' * 9, '5' * 9]
    for marker in got['markers']:
        assert 0 <= marker['position'] < len(got['buckets'])
        assert marker['position'] == round(marker['position'], 4)
    assert got['markers'][0]['position'] == 0.3333


def test_promoted_duplicates_match_retained_rows_after_each_replacement_and_reopen(tmp_path):
    archive = tmp_path / 'archive'
    for name in ['03', '02', '01']:
        write_rows(archive / ('events_' + name + '.jsonl'), event())
    index = new_index(tmp_path / 'history')
    try:
        index.update(archive)
        assert index.meta()['duplicate_requests'] == 2
        for expected in [1, 0]:
            seg = index._db.execute('SELECT name FROM segments JOIN rows ON segments.id=rows.seg WHERE counted=1 LIMIT 1').fetchone()[0]
            write_rows(archive / seg)
            index._last_verify = 0
            index.update(archive)
            actual = index._db.execute('SELECT COUNT(*) FROM rows WHERE counted=0').fetchone()[0]
            assert index.meta()['duplicate_requests'] == actual == expected
            assert totals(index)['request_count'] == 1
        index.close()
        index = new_index(tmp_path / 'history')
        index.update(archive)
        assert index.meta()['duplicate_requests'] == 0
    finally:
        index.close()


@pytest.mark.parametrize('strong_in_history', [False, True])
def test_settled_count_is_global_winners_independent_of_window(tmp_path, monkeypatch, strong_in_history):
    weak = session_event(ts=(NOW - timedelta(days=2)).isoformat())
    strong = session_event(ts=(NOW - timedelta(days=20)).isoformat(), input_token_usage=measurement(300, 50, 10))
    write_rows(tmp_path / 'archive/events_01.jsonl', strong if strong_in_history else weak, event())
    write_rows(tmp_path / 'logs/events.jsonl', weak if strong_in_history else strong,
               session_event(run_id='only-live', ts=(NOW - timedelta(days=8)).isoformat()), event())
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe='ALL', daily='1')
        finish_index(plugin._HISTORY)
        for tf, sessions in [('1H', 0), ('7D', 0), ('MONTH', 2), ('ALL', 2)]:
            got = read_route(route, timeframe=tf, daily='1')
            assert got['quality']['settled_records_total'] == 3  # one request, two global session winners
            assert got['summary']['session_count'] == sessions
    finally:
        unload[0]()


def test_boot_migration_accesses_hold_shared_connection_lock(tmp_path, monkeypatch):
    index = new_index(None)
    index._open()
    real = index._db
    class Checked:
        def execute(self, sql, *args):
            assert index._db_lock._is_owned(), sql
            return real.execute(sql, *args)
    try:
        real.execute("UPDATE meta SET value=? WHERE key='boot_sources'", (json.dumps({'after': ''}),))
        index._db = Checked()
        index._restore_boot_sources(tmp_path / 'archive')  # SELECT and final UPDATE, also for the empty migration
        assert json.loads(real.execute("SELECT value FROM meta WHERE key='boot_sources'").fetchone()[0]) == {'done': True}
    finally:
        index._db = real
        index.close()


def test_close_does_not_close_connection_under_active_background_migration(tmp_path, monkeypatch):
    index = new_index(None)
    entered, release = threading.Event(), threading.Event()
    restore = index._restore_boot_sources
    def blocked(archive):
        entered.set()
        assert release.wait(10)
        assert index._db is not None  # close leaves the active worker responsible for disposal
        return restore(archive)
    monkeypatch.setattr(index, '_restore_boot_sources', blocked)
    try:
        index.request(tmp_path / 'archive')
        assert entered.wait(5)
        index.close()
        assert index._thread.is_alive() and index._db is not None
    finally:
        release.set()
        finish_index(index)
        index.close()
    assert index._db is None
