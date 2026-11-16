"""Transaction, cold-start and migration regressions for the 0.8.1 repair."""
import json
import sqlite3
from datetime import timedelta

import pytest
from test_history import new_index, plugin, NOW, event, boot, session_event, write_rows, api_for, read_route, finish_index


class FailedWrite:
    def __init__(self, db, point):
        self.db, self.point, self.fired = db, point, False

    def execute(self, sql, *args):
        self.check(sql)
        return self.db.execute(sql, *args)

    def executemany(self, sql, *args):
        self.check(sql)
        return self.db.executemany(sql, *args)

    def check(self, sql):
        if (sql.startswith(self.point)):
            self.fired = True
            raise sqlite3.OperationalError('database or disk is full')

    def __getattr__(self, key):
        return getattr(self.db, key)


def totals(index):
    view = index.view()
    with index.bind(view) as bound:
        return plugin.calculate_analytics([], 'ALL', now=NOW, calendar_daily=True, history=bound)['summary']


@pytest.mark.parametrize('point', ['INSERT INTO rows', 'INSERT OR REPLACE INTO cells', 'COMMIT'])
def test_replacement_failure_preserves_whole_transaction_and_retries_at_low_uptime(tmp_path, monkeypatch, point):
    archive = tmp_path / 'archive'
    source = archive / 'events_01.jsonl'
    write_rows(source, event(prompt_tokens=100, cached_tokens=50))
    index = new_index(tmp_path / 'history')
    index.update(archive)
    before = totals(index)
    real = index._db
    saved = {t: real.execute('SELECT * FROM ' + t).fetchall() for t in ('segments', 'rows', 'sessions', 'boots', 'cells')}
    write_rows(source, event(prompt_tokens=200, cached_tokens=50, ledger_attempt_ids=['replacement']))
    # A zero last-check means verification is due, even before six hours of machine/process uptime.
    monkeypatch.setattr(plugin._history_module.time, 'monotonic', lambda: 10.0)
    index._last_verify = index._last_check = 0
    fault = FailedWrite(real, point)
    index._db = fault
    try:
        index.update(archive)
        assert fault.fired and index.meta()['state'] == 'degraded'
        assert totals(index) == before
        assert {t: real.execute('SELECT * FROM ' + t).fetchall() for t in saved} == saved
    finally:
        index._db = real
    index.request(archive)  # no six-hour delay after the failed write
    finish_index(index)
    assert (totals(index)['request_count'], totals(index)['prompt_tokens']) == (1, 200)
    index.close()
    again = new_index(tmp_path / 'history')
    again.update(archive)
    assert (totals(again)['request_count'], totals(again)['prompt_tokens']) == (1, 200)
    again.close()


def test_cold_view_deduplicates_before_fingerprints_finish_loading(tmp_path, monkeypatch):
    write_rows(tmp_path / 'archive/events_01.jsonl', event())
    write_rows(tmp_path / 'logs/events.jsonl', event())  # same observation at the rotation boundary
    seed = new_index(tmp_path / 'skill-state/history')
    seed.update(tmp_path / 'archive')
    seed.close()
    _, route, unload = api_for(tmp_path, monkeypatch)
    index = plugin._HISTORY
    index._open()
    index._loaded = True
    index._publish(force=True)
    assert index._fp_pending
    index._meta.update(complete=True, state='ready')
    index._ready.set()
    monkeypatch.setattr(index, 'request', lambda *a: None)  # deterministically hold the deferred fingerprint load
    try:
        got = read_route(route, timeframe='ALL', daily='1')
        assert got['status'] == 'ok'
        assert got['summary']['request_count'] == 1
        assert got['summary']['prompt_tokens'] == 100
    finally:
        unload[0]()


def test_degraded_rotation_does_not_erase_the_only_old_live_observation(tmp_path, monkeypatch):
    write_rows(tmp_path / 'logs/events.jsonl', event(ts=(NOW - timedelta(days=9)).isoformat()))
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        assert read_route(route, timeframe='ALL', daily='1')['summary']['request_count'] == 1
        finish_index(plugin._HISTORY)
        (tmp_path / 'logs/events.jsonl').rename(tmp_path / 'rotated-unindexed.jsonl')
        write_rows(tmp_path / 'logs/events.jsonl')
        plugin._HISTORY._meta.update(state='degraded', read_error='history_save_error:OperationalError')
        monkeypatch.setattr(plugin._HISTORY, 'request', lambda *a: None)
        got = read_route(route, timeframe='ALL', daily='1')
        assert got['status'] == 'degraded' and got['summary']['request_count'] == 1
        plugin._HISTORY._meta.update(state='ready', read_error=None)
        assert read_route(route, timeframe='ALL', daily='1')['summary']['request_count'] == 1
    finally:
        unload[0]()


def make_old_layout(index):
    db = index._db
    db.execute('BEGIN')
    db.execute('CREATE TABLE old_boots(identity TEXT PRIMARY KEY, ts INTEGER NOT NULL, sha TEXT NOT NULL, branch TEXT NOT NULL, seg INTEGER NOT NULL) WITHOUT ROWID')
    db.execute('INSERT OR IGNORE INTO old_boots SELECT * FROM boots ORDER BY seg')
    db.execute('DROP TABLE boots')
    db.execute('ALTER TABLE old_boots RENAME TO boots')
    db.execute("DELETE FROM meta WHERE key IN ('boot_sources', 'commits')")
    db.execute('COMMIT')


@pytest.mark.parametrize('interrupted', [False, True])
def test_existing_sqlite_keeps_retired_observations_and_recovers_boot_sources(tmp_path, monkeypatch, interrupted):
    directory, state = tmp_path / 'archive', tmp_path / 'history'
    shared = boot('b' * 40, 5)
    write_rows(directory / 'events_02.jsonl', shared, event(), session_event())
    write_rows(directory / 'events_01.jsonl', shared)
    write_rows(directory / 'events_retired.jsonl', boot('a' * 40, 30), event(ledger_attempt_ids=['retired']))
    old = new_index(state)
    old.update(directory)
    make_old_layout(old)
    old_rows = {t: old._db.execute('SELECT * FROM ' + t).fetchall() for t in ('rows', 'sessions', 'cells')}
    old.close()
    (directory / 'events_retired.jsonl').unlink()
    new = new_index(state)
    if interrupted:
        original = new._read_lines
        def fail(path, *args):
            if path.name == 'events_02.jsonl':
                raise OSError('interrupted migration')
            return original(path, *args)
        monkeypatch.setattr(new, '_read_lines', fail)
        new.update(directory)
        assert new.meta()['state'] == 'degraded'
        assert {t: new._db.execute('SELECT * FROM ' + t).fetchall() for t in old_rows} == old_rows
        new.close()
        new = new_index(state)
    new.update(directory)
    assert {t: new._db.execute('SELECT * FROM ' + t).fetchall() for t in old_rows} == old_rows
    assert len(new.view()._boots) == 2
    # Replace whichever source won the duplicate in the OLD layout. The surviving copy must still be present.
    with sqlite3.connect(new.path) as db:
        seg = db.execute("SELECT min(seg) FROM boots WHERE sha = ?", ('b' * 40,)).fetchone()[0]
        name = db.execute('SELECT name FROM segments WHERE id=?', (seg,)).fetchone()[0]
    write_rows(directory / name, event(ledger_attempt_ids=['changed']))
    new._last_verify = 0
    new.update(directory)
    assert len(new.view()._boots) == 2
    assert any(b['sha'] == 'a' * 40 for b in new.view()._boots)  # unique retired history survives
    new.close()
    again = new_index(state)
    again.update(directory)
    assert len(again.view()._boots) == 2
    assert json.loads(again._db.execute("SELECT value FROM meta WHERE key='boot_sources'").fetchone()[0]) == {'done': True}
    again.close()


@pytest.mark.parametrize('between_edges', [False, True])
def test_rotation_during_reading_keeps_cells_edges_and_live_overlay_together(tmp_path, monkeypatch, between_edges):
    directory = tmp_path / 'archive'
    base = NOW - timedelta(hours=3)
    write_rows(directory / 'events_01.jsonl', event(ts=(base + timedelta(minutes=1)).isoformat(), ledger_attempt_ids=['early']),
               event(ts=(base + timedelta(minutes=10)).isoformat(), ledger_attempt_ids=['later']),
               event(ts=(base + timedelta(minutes=11)).isoformat(), ledger_attempt_ids=['other'], model='second'),
               event(ts=(base + timedelta(minutes=2)).isoformat(), ledger_attempt_ids=['other-early'], model='second'))
    live = tmp_path / 'logs/events.jsonl'
    write_rows(live, event(ts=(base + timedelta(minutes=12)).isoformat(), ledger_attempt_ids=['live']))
    _, route, unload = api_for(tmp_path, monkeypatch)
    fired = []
    try:
        read_route(route, timeframe='ALL', daily='1')
        index = plugin._HISTORY
        finish_index(index)
        def rotate():
            if not fired:
                fired.append(True)
                live.rename(directory / 'events_02.jsonl')
                write_rows(live)
                index.update(directory)
        if between_edges:
            original = plugin._history_module.HistoryView.rows
            calls = []
            def rows(self, *args):
                calls.append(args)
                if len(calls) == 2:
                    rotate()
                return original(self, *args)
            monkeypatch.setattr(plugin._history_module.HistoryView, 'rows', rows)
        else:
            original = index.view
            def view():
                got = original()
                rotate()
                return got
            monkeypatch.setattr(index, 'view', view)
        got = read_route(route, timeframe='CUSTOM', **{'from': (base + timedelta(minutes=5)).isoformat(),
                                                      'to': (base + timedelta(minutes=15)).isoformat()})
        assert fired and got['status'] == 'ok'
        assert (got['summary']['request_count'], got['summary']['prompt_tokens']) == (3, 300)
    finally:
        unload[0]()


@pytest.mark.parametrize('ends', [(30, 50), (50, 30)])
def test_memo_end_includes_sessions_after_the_last_request(tmp_path, ends):
    base = NOW - timedelta(days=3, hours=2)
    directory = tmp_path / 'archive'
    write_rows(directory / 'events_01.jsonl', event(ts=(base + timedelta(minutes=5)).isoformat()),
               session_event(ts=(base + timedelta(minutes=40)).isoformat()))
    index = new_index(tmp_path / 'history')
    try:
        view = index.update(directory)
        for end in ends:
            with index.bind(view) as bound:
                got = plugin.calculate_analytics([], 'CUSTOM', now=NOW, start_override=base,
                                                  end_override=base + timedelta(minutes=end), hourly=True, history=bound)
            assert got['summary']['session_count'] == (1 if end == 50 else 0)
    finally:
        index.close()


def test_legacy_incomplete_warning_is_durable_and_preserves_valid_retired_records(tmp_path):
    from test_history import legacy_index, v1_record
    legacy = tmp_path / 'old.json'
    directory = tmp_path / 'archive'
    legacy_index(legacy, directory, {'events_good.jsonl': ([1, 2], [v1_record()], [], []),
                                    'events_bad.jsonl': ([3, 4], [dict(v1_record(), identity='request:invalid')], [], [])})
    original = legacy.read_bytes()
    for _ in range(2):
        index = new_index(tmp_path / 'history', legacy)
        try:
            assert index.update(directory).total_records() == 1
            meta = index.meta()
            assert meta['recovery_incomplete'] is True
            assert meta['legacy_import']['unreadable'] == meta['legacy_import']['imported'] == 1
            assert 'not included' in meta['recovery']
        finally:
            index.close()
    assert legacy.read_bytes() == original


def changed(index):
    index._last_check = index._last_verify = 0  # the periodic identity check (also run at every load)


def test_an_unmarked_invalid_line_never_replaces_the_only_indexed_copy(tmp_path):
    directory = tmp_path / 'archive'
    source = directory / 'events_01.jsonl'
    old = event(ledger_attempt_ids=['old'], ts=(NOW - timedelta(days=3)).isoformat())
    write_rows(source, old)
    index = new_index(tmp_path / 'history')
    try:
        assert index.update(directory).total_records() == 1
        # The usage prefilter never hands this line to the parser: the bytes are damaged, not a clean replacement.
        source.write_bytes(b'{BROKEN}\n')
        changed(index)
        view = index.update(directory)
        assert view.total_records() == 1 and index.meta()['changed_sources_kept'] == ['events_01.jsonl']
        assert totals(index)['request_count'] == 1
        # Valid usage lines beside one invalid unmarked line: still not a clean copy.
        new = [event(ledger_attempt_ids=[f'new-{i}'], ts=(NOW - timedelta(days=2, minutes=i)).isoformat()) for i in range(2)]
        source.write_bytes(b''.join(json.dumps(r).encode() + b'\n' for r in new) + b'{"unterminated": \n')
        changed(index)
        assert index.update(directory).total_records() == 1 and index.meta()['changed_sources_kept_count'] == 1
        # A later clean replacement (usage lines and a valid non-usage line) recovers and retracts the earlier copy.
        source.write_bytes(b''.join(json.dumps(r).encode() + b'\n' for r in new) + b'{"type": "other_event"}\n\n')
        changed(index)
        assert index.update(directory).total_records() == 2 and index.meta()['changed_sources_kept_count'] == 0
    finally:
        index.close()
    restarted = new_index(tmp_path / 'history')
    try:
        assert restarted.update(directory).total_records() == 2 and restarted.meta()['changed_sources_kept_count'] == 0
    finally:
        restarted.close()


@pytest.mark.parametrize('content', [b'', b'\n\n', b'{"type": "task_started"}\n'])
def test_a_clean_empty_or_non_usage_replacement_still_replaces(tmp_path, content):
    directory = tmp_path / 'archive'
    source = directory / 'events_01.jsonl'
    write_rows(source, event(ledger_attempt_ids=['old']))
    write_rows(directory / 'events_02.jsonl', event(ledger_attempt_ids=['other']))
    index = new_index(tmp_path / 'history')
    try:
        assert index.update(directory).total_records() == 2
        source.write_bytes(content)
        changed(index)
        assert index.update(directory).total_records() == 1 and index.meta()['changed_sources_kept_count'] == 0
    finally:
        index.close()


class _Entry:
    def __init__(self, entry, fail):
        self._entry, self._fail, self.name = entry, fail, entry.name

    def is_file(self):
        return self._entry.is_file()

    def stat(self):
        if self._fail(self.name):
            raise PermissionError(13, 'Permission denied')
        return self._entry.stat()


def failing_stats(monkeypatch, directory, fail):
    """The index's listing of ``directory`` sees entries whose stat() raises for the names ``fail`` picks."""
    real = plugin._history_module.os.scandir

    class Listing:
        def __init__(self, path):
            self._listing = real(path)

        def __enter__(self):
            return (_Entry(entry, fail) for entry in self._listing.__enter__())

        def __exit__(self, *exc):
            return self._listing.__exit__(*exc)

    monkeypatch.setattr(plugin._history_module.os, 'scandir',
                        lambda path: Listing(path) if str(path) == str(directory) else real(path))


def test_a_transient_stat_error_never_turns_a_later_rotation_into_loading(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'
    write_rows(archive / 'events_01.jsonl', event(ledger_attempt_ids=['one'], ts=(NOW - timedelta(hours=3)).isoformat()))
    _, route, unload = api_for(tmp_path, monkeypatch)
    try:
        read_route(route, timeframe='24H')
        finish_index(plugin._HISTORY)
        index = plugin._HISTORY
        failures = ['events_01.jsonl']
        failing_stats(monkeypatch, archive, lambda name: name in failures and not failures.remove(name))
        changed(index)
        index.update(archive)  # the verifying pass meets one transient stat error
        assert index.meta()['read_error'].startswith('history_stat_error') and index._last_verify == 0
        # The next rotation: while its archive is indexed a 24h reading is never 'loading' again.
        write_rows(archive / 'events_02.jsonl', event(ledger_attempt_ids=['two'], ts=(NOW - timedelta(hours=1)).isoformat()))
        seen = []
        original = index._parse_archive
        def parse(path, stat):
            if path.name == 'events_02.jsonl':
                seen.append((index.meta()['complete'], index.meta()['complete_after_us'], read_route(route, timeframe='24H')))
            return original(path, stat)
        monkeypatch.setattr(index, '_parse_archive', parse)
        monkeypatch.setattr(index, 'request', lambda *a: None)  # this synchronous pass is the only one
        changed(index)
        index.update(archive)
        (complete, after, reading), = seen
        assert complete and after is None
        assert reading['status'] != 'loading' and reading['quality']['history_pending'] is False
        assert reading['summary']['request_count'] == 1
        assert index.meta()['read_error'] is None and index.view().total_records() == 2
    finally:
        unload[0]()


@pytest.mark.parametrize('failure', ['stat', 'read'])
def test_a_persistent_error_keeps_the_published_view_and_its_memo(tmp_path, monkeypatch, failure):
    archive = tmp_path / 'archive'
    write_rows(archive / 'events_01.jsonl', event(ledger_attempt_ids=['one']))
    index = new_index(tmp_path / 'history')
    try:
        index.update(archive)
        if failure == 'stat':
            failing_stats(monkeypatch, archive, lambda name: name == 'events_01.jsonl')
            changed(index)
        else:
            write_rows(archive / 'events_02.jsonl', event(ledger_attempt_ids=['two']))
            original = index._read_lines
            def unreadable(path, identity, consume):
                if path.name == 'events_02.jsonl':
                    raise PermissionError(13, 'Permission denied')
                return original(path, identity, consume)
            monkeypatch.setattr(index, '_read_lines', unreadable)
        index._run(archive)
        view = index.view()
        view.remember('derived', {'kept': True})
        revision = index.meta()['revision']
        for _ in range(3):  # each request retries the error at once; nothing was committed, nothing is rebuilt
            assert index._retry
            index._run(archive)
        assert index.view() is view and view.memo.get('derived') == {'kept': True}
        assert index.meta()['revision'] == revision and index.meta()['state'] == 'degraded'
        assert view.total_records() == 1
    finally:
        index.close()


def test_a_cold_load_with_unexaminable_new_archives_releases_readers_degraded(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'
    write_rows(archive / 'events_01.jsonl', event(ledger_attempt_ids=['one'], ts=(NOW - timedelta(hours=3)).isoformat()))
    write_rows(tmp_path / 'logs/events.jsonl')
    _, route, unload = api_for(tmp_path, monkeypatch)
    read_route(route, timeframe='24H')
    finish_index(plugin._HISTORY)
    unload[0]()
    write_rows(archive / 'events_02.jsonl', event(ledger_attempt_ids=['two'], ts=(NOW - timedelta(hours=1)).isoformat()))
    _, route, unload = api_for(tmp_path, monkeypatch)
    index = plugin._HISTORY
    failing_stats(monkeypatch, archive, lambda name: name == 'events_02.jsonl')
    import threading
    entered, release = threading.Event(), threading.Event()
    original = index._index_list
    def held(*args):
        entered.set()  # the barrier: readers were released (or not) before this point
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(index, '_index_list', held)
    try:
        index.request(archive)
        assert entered.wait(10)
        assert index._ready.is_set()  # the preserved history is offered at once, without waiting two seconds
        reading = read_route(route, timeframe='24H')
        assert reading['status'] == 'degraded' and reading['read_error'].startswith('history_stat_error')
        assert reading['quality']['history_index']['state'] == 'degraded'
        assert reading['summary']['request_count'] == 1  # the indexed history, disclosed as incomplete
    finally:
        release.set()
        finish_index(index)
        unload[0]()


def loaded_state(index):
    """Everything a load puts in memory, in its order (dicts as item lists)."""
    with index._db_lock:
        index._load()
    return {'commits': index._commits, 'groups': list(index._groups), 'group_ids': dict(index._group_ids),
            'segments': list(index._segments.items()), 'facts': index._facts, 'kept': index._kept,
            'days': [(day, [(cell, list(inner.items())) for cell, inner in cells.items()]) for day, cells in index._days.items()],
            'span': (index._total, index._oldest, index._newest), 'sessions': list(index._sessions.items()),
            'session_order': index._session_order, 'boots': list(index._boots.items()), 'boot_list': index._boot_list,
            'boot_stamps': index._boot_stamps, 'fp_base': list(index._fp_base), 'fp_chunks': index._fp_chunks}


def test_one_statement_per_table_loads_exactly_what_a_cursor_loads(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'
    write_rows(archive / 'events_01.jsonl', event(), event(ledger_attempt_ids=['unknown'], prompt_tokens=None, cached_tokens=None),
               event(ledger_attempt_ids=['ü'], model='модель/🧪 "q" \\ \t'), session_event(), boot('abc', 30))
    write_rows(archive / 'events_02.jsonl', event(ledger_attempt_ids=['b'], ts=(NOW - timedelta(days=3)).isoformat()))
    index = new_index(tmp_path / 'history')
    index.update(archive)
    history, big, small = plugin._history_module, 2 ** 63 - 1, -2 ** 63
    stamp = plugin.to_us(NOW - timedelta(hours=5))
    with index._db_lock:  # edge values written directly: NULLs, int64 extremes, Unicode and control text, orphans
        db = index._db
        db.execute('INSERT INTO groups(provider, model, kind) VALUES (?, ?, ?)', ('провайдер ', 'm\x01"\\', 'attempt'))
        db.execute('INSERT INTO segments(name, kind, size, mtime_ns, low, high, stats, fps)'
                   ' VALUES (?, ?, NULL, NULL, NULL, NULL, ?, NULL)', ('events_ü🧪.jsonl', 'restored', json.dumps({'lines': big, 'note': 'ß'})))
        db.execute('INSERT INTO cells VALUES (?, ?, ?, ?, ?)', (stamp - stamp % history.CELL_US, 1,
                   history._pack([big, small] + [0] * (plugin.VECTOR_SIZE - 2)), small, big))
        for order, (p, c, w) in enumerate(((big, 0, None), (None, None, None), (small, small, small), (5, 3, 0))):
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (1 if order % 2 else 999, 100 + order, 'session:ид🧪' + str(order % 2), stamp + order,
                        'claude', 'ü/model\n', p, c, w, order * 9))
        db.execute('INSERT INTO boots VALUES (?, ?, ?, ?, ?)', ('boot:ü', stamp, 'дерево', 'ветка/🧪', 999))
    bulk = loaded_state(index)
    cursor = lambda connection, table, columns: connection.execute('SELECT ' + columns + ' FROM ' + table)  # noqa: E731
    monkeypatch.setattr(history, '_table', cursor)
    assert loaded_state(index) == bulk
    assert len(bulk['sessions']) >= 3 and bulk['segments'][-1][1]['facts']['lines'] == big and bulk['fp_base']
    index.close()


class NoJson:
    """A connection of an SQLite without JSON functions: its JSON aggregate fails as such an SQLite fails it."""

    def __init__(self, db, seen):
        self.db, self.seen = db, seen

    def execute(self, sql, *args):
        self.seen.append(sql)
        if 'json_group_array' in sql:
            raise sqlite3.OperationalError('no such function: json_group_array')
        return self.db.execute(sql, *args)

    def __getattr__(self, key):
        return getattr(self.db, key)


def test_an_sqlite_without_json_functions_degrades_and_never_reads_rows_another_way(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'
    write_rows(archive / 'events_01.jsonl', event(), session_event(), boot('abc', 30))
    seed = new_index(tmp_path / 'history')
    seed.update(archive)
    seed.close()
    tables = ('segments', 'rows', 'sessions', 'boots', 'cells')
    with sqlite3.connect(tmp_path / 'history' / 'history.sqlite3') as db:
        saved = {t: db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
    real, seen = sqlite3.connect, []
    monkeypatch.setattr(plugin._history_module.sqlite3, 'connect', lambda *a, **k: NoJson(real(*a, **k), seen))
    index = new_index(tmp_path / 'history')
    try:
        index._run(archive)
        meta = index.meta()
        assert meta['state'] == 'degraded' and meta['read_error'] == 'history_index_error:OperationalError'
        assert index.view() is None and index._ready.is_set() and not index._loaded
        assert any('json_group_array' in sql for sql in seen)
        assert not [sql for sql in seen if 'json_group_array' not in sql and ' FROM sessions' in sql]  # no other way
        assert not list((tmp_path / 'history').glob('*.unreadable-*'))  # a missing capability never sets the file aside
    finally:
        index.close()
    monkeypatch.undo()
    with sqlite3.connect(tmp_path / 'history' / 'history.sqlite3') as db:
        assert {t: db.execute('SELECT * FROM ' + t).fetchall() for t in tables} == saved


def test_a_reload_offers_stored_history_before_new_archives_and_counts_each_new_row_once(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'
    write_rows(archive / 'events_01.jsonl', event(ledger_attempt_ids=['one'], ts=(NOW - timedelta(hours=3)).isoformat()))
    write_rows(tmp_path / 'logs/events.jsonl')
    _, route, unload = api_for(tmp_path, monkeypatch)
    read_route(route, timeframe='24H')
    finish_index(plugin._HISTORY)
    unload[0]()
    two = event(ledger_attempt_ids=['two'], ts=(NOW - timedelta(hours=1)).isoformat())
    write_rows(archive / 'events_02.jsonl', two)
    write_rows(tmp_path / 'logs/events.jsonl', two)  # the same observation still live at the rotation boundary
    _, route, unload = api_for(tmp_path, monkeypatch)
    index = plugin._HISTORY
    import threading
    entered, release = threading.Event(), threading.Event()
    original = index._index_list
    def held(*args):
        entered.set()  # the barrier: the new archive is not indexed yet
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(index, '_index_list', held)
    try:
        index.request(archive)
        assert entered.wait(10)
        assert index._ready.is_set() and index.meta()['revision'] >= 1  # released with the first published view
        reading = read_route(route, timeframe='24H')
        assert reading['status'] == 'loading' and reading['summary']['request_count'] == 2  # stored one + live two
        release.set()
        finish_index(index)
        reading = read_route(route, timeframe='24H')
        assert reading['status'] == 'ok' and reading['summary']['request_count'] == 2  # two counted once
        assert index.view().total_records() == 2
    finally:
        release.set()
        unload[0]()
