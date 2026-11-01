"""R3 regressions: public synthetic status fixtures, real local SQLite.

No installed host, account or provider access. Disk gates below deliberately
hold worker operations so scheduling/stop assertions do not rely on disk speed.
The release-repair widget case serves the real registered handlers to the real
widget over an in-test loopback server; the host behind them is still fake.
"""
import asyncio
import concurrent.futures
import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace

import pytest

import plugin
import quota_history as qh
import quota_summary as qs
from test_reserve import (NOW, WEEK, _Host, at, constraint, group, host_at, observed,
                          payload, profile, rate_of, snap, summary_of, sweep)


@pytest.mark.parametrize('returning', [False, True])
def test_r2_f1_source_watch_bound_with_healthy_global_sweeps(tmp_path, returning):
    store = qh.HistoryStore(tmp_path)
    for offset in range(-4800, 1, 120):
        rows = []
        if returning and offset <= -3000:
            rows = [snap('codex', 'a', [constraint('primary', .1, reset=86400)], observed=-4800)]
        elif -600 <= offset < 0:
            rows = [snap('codex', 'a', [constraint('primary', .2, reset=86400)], observed=-1800)]
        elif offset == 0:
            rows = [snap('codex', 'a', [constraint('primary', .3, reset=86400)], observed=0)]
        data = payload(rows, [profile('codex', 'a')])
        plugin.persist_sweep(store, data, '', NOW + offset)
    result = plugin.reserve_view(store, data, NOW, NOW)
    watch = result['summary']['history']['unbroken_watch']
    # Watched for longer than the bounded look-back: a lower bound, said as one.
    assert watch['since'] <= qs.iso(NOW - 3600) and watch['exact'] is False
    pace = rate_of(result)
    assert pace['state'] != 'ok'
    assert pace['windows_per_hour'] is None
    assert not pace['exhaust_before_reset']
    assert result['chart']['recent_pace'] is None


def test_r2_f1_later_unchanged_observation_can_pin_watch_boundary(tmp_path):
    store = qh.HistoryStore(tmp_path)
    for offset in range(-4800, 1, 120):
        rows = []
        if offset >= -1800:
            obs = -2400 if offset < -1200 else (-1200 if offset < 0 else 0)
            rows = [snap('codex', 'a', [constraint('primary', .2 if offset < 0 else .3,
                                                 reset=86400)], observed=obs)]
        data = payload(rows, [profile('codex', 'a')])
        plugin.persist_sweep(store, data, '', NOW + offset)
    pace = rate_of(plugin.reserve_view(store, data, NOW, NOW))
    assert pace['state'] == 'ok'
    assert pace['span_min_seconds'] == 1800
    assert pace['windows_per_hour'] == pytest.approx(.2)


def two_sources(ratios, resets=(86400, 86400), obs=-2400, reverse=False):
    rows = [snap('codex', 'a', [constraint('primary', ratio, reset=reset)], source=source, observed=obs)
            for source, ratio, reset in zip(('app', 'rollout'), ratios, resets)]
    return payload(list(reversed(rows)) if reverse else rows, [profile('codex', 'a')])


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('kind', ['value', 'reset', 'rounding', 'newest'])
def test_r2_f2_history_uses_current_source_policy(tmp_path, reverse, kind):
    store = qh.HistoryStore(tmp_path)
    for offset in range(-2400, 1, 120):
        if offset == 0:
            data = two_sources((.2, .2), obs=0, reverse=reverse)
        else:
            ratios = (.1, .9) if kind in ('value', 'newest') else (.1, .11)
            resets = (86400, 90000) if kind == 'reset' else (86400, 86400)
            data = two_sources(ratios, resets, reverse=reverse)
            if kind == 'newest':
                for row in data['quota']:
                    if row['source'] == 'rollout':
                        row['observed_at'] = qs.iso(NOW - 2410)
        plugin.persist_sweep(store, data, '', NOW + offset)
    result = plugin.reserve_view(store, data, NOW, NOW)
    assert result['summary']['groups'][0]['measured']['windows'] == .8
    expected = {'value': None, 'reset': None, 'rounding': .89, 'newest': .9}[kind]
    for horizon in ('24h', '7d'):
        past = plugin.reserve_view(store, data, NOW, NOW, horizon=horizon)['chart']['past']
        actual = observed(past, -2000)
        assert actual is None if expected is None else actual == pytest.approx(expected)


def test_r2_f2_short_conflict_preserves_both_boundaries_between_samples(tmp_path):
    store = qh.HistoryStore(tmp_path)
    for offset in sorted(set(range(-2400, 1, 120)) | {-1810, -1750}):
        data = two_sources((.1, .9 if -1810 <= offset < -1750 else .1))
        plugin.persist_sweep(store, data, '', NOW + offset)
    for horizon in ('24h', '7d'):
        chart = plugin.reserve_view(store, data, NOW, NOW, horizon=horizon)['chart']
        assert observed(chart['past'], -1900) == pytest.approx(.9)
        assert observed(chart['past'], -1780) is None
        assert observed(chart['past'], -1700) == pytest.approx(.9)
        assert [int(NOW - 1810), None] in chart['past']
        assert [int(NOW - 1750), .9] in chart['past']


@pytest.mark.parametrize('change', ['ratio', 'resets_at', 'plan'])
def test_same_timestamp_correction_preserved_and_cuts_pace(tmp_path, change):
    store = qh.HistoryStore(tmp_path)
    original = host_at(NOW, {'a': [(-2400, .1, 86400)]}, plans={'a': 'pro'})
    for offset in range(-2400, -119, 120):
        plugin.persist_sweep(store, original, '', NOW + offset)
    norm = qs.normalize(original, NOW)
    old = qs.recordable(norm, NOW)[0]
    if change == 'plan':
        # What normalize makes of the same cached reading relabelled: its plan
        # evidence (the comparison key) changes with the label.
        corrected = qs.recordable(qs.normalize(host_at(NOW, {'a': [(-2400, .1, 86400)]},
                                                       plans={'a': 'max'}), NOW), NOW)[0]
        assert corrected.plan_key != old.plan_key
    else:
        corrected = replace(old, **{'ratio': .3} if change == 'ratio' else {'resets_at': NOW + 90000})
    state = qs.compute(original, store.read(lambda salt: qs.history_requests(
        qs.prepare(original, NOW)[1], salt, NOW), NOW), NOW)
    member = state.groups[0].members[0]
    before = qs._history_runs(state.view, member, member.reading.key)
    merged = qs._with_live(before, corrected, NOW, state.view)
    assert merged[-1].after_correction
    assert qs.same_content(merged[-1], corrected)
    store.record_sweep([corrected], NOW, True, '')
    view = store.read(lambda salt: {(qs.pseudo_id(salt, 'codex', 'a'), old.key): 0}, NOW)
    runs = next(iter(view.runs.values()))
    assert len(runs) == 2 and runs[-1].after_correction
    assert qs.same_content(runs[-1], corrected)
    assert qs.boundary(runs[0], runs[1]) == 'correction'
    # Re-reading the corrected content is a duplicate, not another correction.
    store.record_sweep([corrected], NOW + 120, True, '')
    assert len(next(iter(store.read(lambda salt: {(qs.pseudo_id(salt, 'codex', 'a'), old.key): 0},
                                   NOW + 120).runs.values()))) == 2


@pytest.mark.parametrize('damage', ['r1', 'missing_column', 'salt', 'index', 'trigger'])
def test_r2_f4_empty_requests_and_empty_sweeps_validate_all_schema(tmp_path, damage):
    store = qh.HistoryStore(tmp_path)
    store.record_sweep([], NOW - 120, True, '')
    conn = sqlite3.connect(store.path)
    if damage == 'r1':
        conn.execute('PRAGMA user_version=1')
        conn.execute('ALTER TABLE run DROP COLUMN after_gap')
    elif damage == 'missing_column':
        conn.execute('ALTER TABLE run DROP COLUMN after_correction')
    elif damage == 'salt':
        conn.execute("DELETE FROM meta WHERE key='salt'")
    elif damage == 'index':
        conn.execute('DROP INDEX run_lookup')
    else:
        conn.execute('CREATE TRIGGER unexpected AFTER INSERT ON sweep BEGIN DELETE FROM run; END')
    conn.commit()
    conn.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    for _ in range(2):
        assert store.read(lambda salt: {}, NOW).state == 'unavailable'
        with pytest.raises(qh.HistoryCorrupt):
            store.record_sweep([], NOW, False, 'quota_not_read')
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_unsupported_wal_database_preserved_by_empty_read_and_write(tmp_path):
    store = qh.HistoryStore(tmp_path)
    store.record_sweep([], NOW - 120, True, '')
    keeper = sqlite3.connect(store.path)
    keeper.execute('PRAGMA wal_autocheckpoint=0')
    keeper.execute('PRAGMA user_version=99')
    keeper.commit()
    evidence = [store.path, store.path.with_name(store.path.name + '-wal')]
    before = [p.read_bytes() for p in evidence]
    try:
        assert store.read(lambda salt: {}, NOW).state == 'unavailable'
        with pytest.raises(qh.HistoryCorrupt):
            store.record_sweep([], NOW, False, 'quota_not_read')
        assert [p.read_bytes() for p in evidence] == before
    finally:
        keeper.close()


def test_reader_connection_enforces_readonly_and_reads_committed_wal(tmp_path):
    store = qh.HistoryStore(tmp_path)
    store.record_sweep([], NOW - 120, True, '')
    keeper = sqlite3.connect(store.path)
    keeper.execute('INSERT INTO sweep VALUES (?, 1, "")', (NOW,))
    keeper.commit()
    try:
        assert store.read(lambda salt: {}, NOW).last_sweep_at == NOW
        conn = store._connect(create=False)
        try:
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                conn.execute('DELETE FROM sweep')
        finally:
            conn.close()
    finally:
        keeper.close()


async def until(event):
    deadline = time.monotonic() + 5
    while not event.is_set():
        assert time.monotonic() < deadline, 'worker did not reach the test gate'
        await asyncio.sleep(.002)


def test_r2_f3_registered_stop_during_persistence_rolls_back_off_loop(tmp_path, monkeypatch):
    store = qh.HistoryStore(tmp_path)
    store.record_sweep([], NOW - 120, True, '')
    entered, release = threading.Event(), threading.Event()
    original = qh.HistoryStore._upsert
    worker_threads = []

    def slow_upsert(self, *args):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(qh.HistoryStore, '_upsert', slow_upsert)
    make = plugin.make_collector
    data = host_at(NOW, {'a': [(0, .2, 86400)]})
    monkeypatch.setattr(plugin, 'make_collector', lambda api, latest, stop: make(
        api, latest, stop, read=lambda: (data, ''), clock=lambda: NOW, first_delay_sec=0))
    host = _Host(tmp_path)
    plugin.register(host)
    factory = host.tasks[0][1]

    async def scenario():
        task = asyncio.create_task(factory())
        try:
            await until(entered)
            tick = asyncio.Event()
            asyncio.get_running_loop().call_later(.01, tick.set)
            await asyncio.wait_for(tick.wait(), .5)
            start = time.monotonic()
            host.unload[0]()  # exact registered callback, while worker is in SQLite path
            assert time.monotonic() - start < .05
            assert not factory.control.settled.is_set()
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()  # repeat cancellation still must not detach the writer
            await asyncio.sleep(.01)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert factory.control.settled.is_set()
        assert worker_threads and all(t != threading.get_ident() for t in worker_threads)
    asyncio.run(scenario())
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT count(*) FROM sweep').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM run').fetchone()[0] == 0


def test_r2_f3_already_admitted_commit_settles_after_nonblocking_stop(tmp_path, monkeypatch):
    store = qh.HistoryStore(tmp_path)
    store.record_sweep([], NOW - 120, True, '')
    entered, release = threading.Event(), threading.Event()

    connect = qh.HistoryStore._connect

    def traced_connect(self, create, **kwargs):
        conn = connect(self, create, **kwargs)
        def trace(sql):
            if sql == 'COMMIT':
                # SQLite has received COMMIT, after the admission lock was released.
                entered.set()
                release.wait(5)
        if create:
            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(qh.HistoryStore, '_connect', traced_connect)
    control = qh.StopControl()
    factory = plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), control,
        read=lambda: (host_at(NOW, {'a': [(0, .2, 86400)]}), ''), clock=lambda: NOW,
        first_delay_sec=0)

    async def scenario():
        task = asyncio.create_task(factory())
        await until(entered)
        start = time.monotonic()
        control.set()
        assert time.monotonic() - start < .05
        assert not control.settled.is_set()
        release.set()
        await asyncio.wait_for(task, 2)
        assert control.settled.is_set()
    asyncio.run(scenario())
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT count(*) FROM sweep').fetchone()[0] == 2


def test_r2_f3_reload_generations_share_bounded_executor_and_cycle_lease(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return host_at(NOW, {'a': [(0, .2, 86400)]}), ''

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        first = plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), qh.StopControl(),
                                      read=read, first_delay_sec=0, interval_sec=.01)
        task = asyncio.create_task(first())
        await until(entered)
        first.control.set()
        task.cancel()
        try:
            for _ in range(12):
                factory = plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), qh.StopControl(),
                                                 read=read, first_delay_sec=0, interval_sec=.01)
                other = asyncio.create_task(factory())
                await asyncio.sleep(.01)
                other.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await other
                assert factory.control.settled.is_set()
            assert len(calls) == 1  # prior worker owns lease even after stop request
            assert not first.control.settled.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert first.control.settled.is_set()
    asyncio.run(scenario())
    assert not (tmp_path / qh.HISTORY_FILE).exists()


def test_r2_f3_queued_cycle_cancelled_before_any_io(tmp_path):
    occupied, release = threading.Event(), threading.Event()

    def occupy():
        occupied.set()
        release.wait(5)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        blocker = loop.run_in_executor(None, occupy)
        await until(occupied)
        factory = plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), qh.StopControl(),
                                         first_delay_sec=0)
        task = asyncio.create_task(factory())
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .5)
        assert factory.control.settled.is_set()
        release.set()
        await blocker
    asyncio.run(scenario())
    assert not list(tmp_path.iterdir())


def test_correction_cannot_retroactively_supply_a_pace_baseline(tmp_path):
    store = qh.HistoryStore(tmp_path)
    for offset in range(-3600, 1, 120):
        ratio = .1 if offset < -600 else (.3 if offset < 0 else .4)
        data = payload([snap('codex', 'a', [constraint('primary', ratio, reset=86400)],
                             observed=-3600 if offset < 0 else 0)], [profile('codex', 'a')])
        plugin.persist_sweep(store, data, '', NOW + offset)
    result = plugin.reserve_view(store, data, NOW, NOW)
    assert rate_of(result)['windows_per_hour'] is None
    assert observed(result['chart']['past'], -1000) == pytest.approx(.9)
    assert observed(result['chart']['past'], -300) == pytest.approx(.7)


def test_r2_f3_real_200000_row_expiry_keeps_event_loop_running(tmp_path, monkeypatch):
    store = qh.HistoryStore(tmp_path)
    store.record_sweep([], NOW - 120, True, '')
    old = NOW - qh.RETENTION_SEC - 1000
    with sqlite3.connect(store.path) as conn:
        conn.executemany('INSERT INTO run (subject, series, source, plan, ratio, resets_at, '
                         'first_obs, last_obs, n_obs, first_seen, last_seen, after_gap, after_correction) '
                         'VALUES (?, "codex|primary|604800|-", "app", "pro", .1, NULL, ?, ?, 1, ?, ?, 0, 0)',
                         ((f'synthetic-{i}', old, old, old, old) for i in range(200000)))
    period = []
    finished = threading.Event()
    prune = qh.HistoryStore._prune
    persist = plugin.persist_sweep

    def traced_prune(self, conn, now):
        period.append(time.monotonic())
        try:
            return prune(self, conn, now)
        finally:
            period.append(time.monotonic())

    def traced_persist(*args, **kwargs):
        result = persist(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(qh.HistoryStore, '_prune', traced_prune)
    monkeypatch.setattr(plugin, 'persist_sweep', traced_persist)
    factory = plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), qh.StopControl(),
                                     read=lambda: (payload([], []), ''), clock=lambda: NOW,
                                     first_delay_sec=0)
    ticks = []

    async def scenario():
        task = asyncio.create_task(factory())
        deadline = time.monotonic() + 10
        try:
            while not finished.is_set():
                ticks.append(time.monotonic())
                assert time.monotonic() < deadline
                await asyncio.sleep(.001)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert factory.control.settled.is_set()
    asyncio.run(scenario())
    assert len(period) == 2 and any(period[0] < tick < period[1] for tick in ticks)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT count(*) FROM run').fetchone()[0] == 0
    print(f'200000-row expiry: {period[1] - period[0]:.3f}s; '
          f'loop ticks during expiry: {sum(period[0] < tick < period[1] for tick in ticks)}')


def test_compressed_source_observation_cannot_be_backdated_to_before_its_sighting(tmp_path):
    store = qh.HistoryStore(tmp_path)
    for offset in range(-2400, 1, 120):
        rows = [snap('codex', 'a', [constraint('primary', .1, reset=86400)],
                     source='app', observed=-2400 if offset < -600 else -900)]
        if offset >= -1200:
            rows.append(snap('codex', 'a', [constraint('primary', .9, reset=86400)],
                             source='rollout', observed=-1200))
        data = payload(rows, [profile('codex', 'a')])
        plugin.persist_sweep(store, data, '', NOW + offset)
    result = plugin.reserve_view(store, data, NOW, NOW)
    assert result['summary']['groups'][0]['measured']['windows'] == .9
    # The newer app timestamp was received only at -600. A compressed run
    # cannot establish its exact arrival time; retain unknown past coverage.
    assert observed(result['chart']['past'], -800) is None


# ---------------------------------------------------------------------------
# Portable cycle lease. POSIX tests use the real flock on this machine; the
# Windows adapter is exercised against a documented model of msvcrt.locking
# (no native Windows runtime runs here).

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

posix_only = pytest.mark.skipif(fcntl is None, reason='POSIX flock not available')


def lease_fd(directory):
    return os.open(str(directory / plugin.LEASE_FILE), os.O_RDWR | os.O_CREAT, 0o600)


def sweeps_in(directory):
    path = directory / qh.HISTORY_FILE
    if not path.exists():
        return 0
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        return conn.execute('SELECT count(*) FROM sweep').fetchone()[0]
    except sqlite3.OperationalError as exc:
        # The collector has created the file but not yet committed its
        # schema: no sweep has been kept yet.
        if 'no such table' not in str(exc):
            raise
        return 0
    finally:
        conn.close()


@posix_only
def test_posix_lease_excludes_a_second_open_file_in_the_same_process(tmp_path):
    acquire, release = plugin.os_lease()
    first, second = lease_fd(tmp_path), lease_fd(tmp_path)
    try:
        assert acquire(first)
        assert not acquire(second)  # an old and a new registration exclude each other
        release(first)
        assert acquire(second)
        release(second)
    finally:
        os.close(first)
        os.close(second)


@posix_only
def test_posix_lease_held_by_another_process_skips_cycles_without_io(tmp_path, monkeypatch):
    holder = subprocess.Popen(
        [sys.executable, '-c', (
            'import fcntl, os, sys\n'
            'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n'
            'fcntl.flock(fd, fcntl.LOCK_EX)\n'
            'print("locked", flush=True)\n'
            'sys.stdin.read()\n'), str(tmp_path / plugin.LEASE_FILE)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'locked'
        reads, persisted = [], threading.Event()
        persist = plugin.persist_sweep

        def traced_persist(*args, **kwargs):
            try:
                return persist(*args, **kwargs)
            finally:
                persisted.set()

        monkeypatch.setattr(plugin, 'persist_sweep', traced_persist)

        def read():
            reads.append(time.monotonic())
            return host_at(NOW, {'a': [(0, .2, 86400)]}), ''

        host = _Host(tmp_path)
        factory = plugin.make_collector(host, plugin.LatestRead(), qh.StopControl(), read=read,
                                        first_delay_sec=0, interval_sec=.02, clock=lambda: NOW)

        async def scenario():
            task = asyncio.create_task(factory())
            try:
                await asyncio.sleep(.3)  # about a dozen cycles, each refused the lease
                assert reads == [] and not (tmp_path / qh.HISTORY_FILE).exists()
                holder.stdin.close()
                assert holder.wait(5) == 0  # process exit releases its lease
                deadline = time.monotonic() + 5
                while not persisted.is_set():
                    assert time.monotonic() < deadline, 'collector never took the released lease'
                    await asyncio.sleep(.01)
            finally:
                start = time.monotonic()
                factory.control.set()
                assert time.monotonic() - start < .05
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert factory.control.settled.is_set()
        asyncio.run(scenario())
        assert reads and sweeps_in(tmp_path) >= 1
        assert not [m for level, m in host.logs if level == 'warning']
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(5)


@posix_only
def test_posix_stop_request_keeps_lease_until_worker_settles_then_next_generation_writes(
        tmp_path, monkeypatch):
    events, entered, gate = [], threading.Event(), threading.Event()
    real_lease = plugin.os_lease

    def traced_lease():
        acquire, release = real_lease()

        def traced_release(fd):
            release(fd)
            events.append('released')
        return acquire, traced_release

    persist = plugin.persist_sweep

    def gated_persist(*args, **kwargs):
        entered.set()
        assert gate.wait(5)
        try:
            return persist(*args, **kwargs)
        finally:
            events.append('persist-returned')  # its connection is closed by now

    monkeypatch.setattr(plugin, 'os_lease', traced_lease)
    monkeypatch.setattr(plugin, 'persist_sweep', gated_persist)
    read = lambda: (host_at(NOW, {'a': [(0, .2, 86400)]}), '')  # noqa: E731
    first = plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), qh.StopControl(),
                                  read=read, first_delay_sec=0, clock=lambda: NOW)
    probe_acquire, probe_release = real_lease()

    def probe():
        fd = lease_fd(tmp_path)
        try:
            if probe_acquire(fd):
                probe_release(fd)
                return True
            return False
        finally:
            os.close(fd)

    async def scenario():
        task = asyncio.create_task(first())
        await until(entered)
        start = time.monotonic()
        first.control.set()  # the registered stop request: memory only
        assert time.monotonic() - start < .05
        assert not first.control.settled.is_set()
        assert not probe()  # stop requested is not settled: the lease is still held
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and not probe()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events.append('settled' if first.control.settled.is_set() else 'not-settled')
        assert probe()
    asyncio.run(scenario())
    assert events == ['persist-returned', 'released', 'settled']
    assert sweeps_in(tmp_path) == 0  # stop preceded commit admission: rolled back

    events.clear()
    second = plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), qh.StopControl(),
                                   read=read, first_delay_sec=0, clock=lambda: NOW)

    async def handoff():
        task = asyncio.create_task(second())
        await until(entered)
        deadline = time.monotonic() + 5
        while 'released' not in events:
            assert time.monotonic() < deadline
            await asyncio.sleep(.005)
        second.control.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert second.control.settled.is_set()
    asyncio.run(handoff())
    assert sweeps_in(tmp_path) == 1


class FakeMsvcrt:
    """A model of msvcrt.locking as documented for Windows, NOT a Windows run:
    the range starts at the descriptor's current position; a lock belongs to
    one handle, so another handle in the same process is refused; LK_NBLCK
    fails at once with EACCES; unlocking a range the handle does not hold
    fails with EACCES. Constants are the C runtime's _LK_* values."""

    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self, fail=None):
        self.fail = fail
        self.calls, self.held = [], {}
        self._lock = threading.Lock()

    def locking(self, fd, mode, nbytes):
        position = os.lseek(fd, 0, os.SEEK_CUR)
        info = os.fstat(fd)
        region = (info.st_dev, info.st_ino, position, nbytes)
        with self._lock:
            self.calls.append((fd, mode, nbytes, position))
            if self.fail is not None:
                raise OSError(self.fail, os.strerror(self.fail))
            owner = self.held.get(region)
            if mode == self.LK_NBLCK:
                if owner is not None:
                    raise OSError(errno.EACCES, 'Permission denied')
                self.held[region] = fd
            elif mode == self.LK_UNLCK:
                if owner != fd:
                    raise OSError(errno.EACCES, 'Permission denied')
                del self.held[region]
            else:
                raise AssertionError(f'unexpected locking mode {mode}')


def test_windows_lease_locks_first_byte_without_waiting_and_unlocks_the_same_byte(tmp_path):
    fake = FakeMsvcrt()
    acquire, release = plugin._windows_lease(fake)
    first, second = lease_fd(tmp_path), lease_fd(tmp_path)
    try:
        os.lseek(first, 7, os.SEEK_SET)  # wherever the descriptor was, the lease is byte 0
        assert acquire(first)
        assert not acquire(second)
        release(first)
        assert acquire(second)
        release(second)
    finally:
        os.close(first)
        os.close(second)
    assert fake.calls == [
        (first, fake.LK_NBLCK, 1, 0), (second, fake.LK_NBLCK, 1, 0), (first, fake.LK_UNLCK, 1, 0),
        (second, fake.LK_NBLCK, 1, 0), (second, fake.LK_UNLCK, 1, 0),
    ]
    assert fake.held == {}


@pytest.mark.parametrize('code, busy', [
    (errno.EACCES, True),
    (getattr(errno, 'EDEADLOCK', errno.EDEADLK), True),
    (errno.EBADF, False),
    (errno.ENOLCK, False),
])
def test_windows_lease_only_lock_contention_is_a_skip(tmp_path, code, busy):
    acquire, _release = plugin._windows_lease(FakeMsvcrt(fail=code))
    fd = lease_fd(tmp_path)
    try:
        if busy:
            assert acquire(fd) is False
        else:
            with pytest.raises(OSError) as raised:
                acquire(fd)
            assert raised.value.errno == code
    finally:
        os.close(fd)


def test_windows_adapter_serializes_reload_generations_in_the_real_collector(tmp_path, monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setattr(plugin, 'os_lease', lambda: plugin._windows_lease(fake))
    entered, gate, calls = threading.Event(), threading.Event(), []

    def read():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert gate.wait(5)
        return host_at(NOW, {'a': [(0, .2, 86400)]}), ''

    def generation():
        return plugin.make_collector(_Host(tmp_path), plugin.LatestRead(), qh.StopControl(),
                                     read=read, first_delay_sec=0, interval_sec=.01, clock=lambda: NOW)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        threads = threading.active_count()
        first = generation()
        task = asyncio.create_task(first())
        await until(entered)
        first.control.set()
        task.cancel()
        try:
            for _ in range(12):
                factory = generation()
                other = asyncio.create_task(factory())
                await asyncio.sleep(.01)
                other.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await other
                assert factory.control.settled.is_set()
            assert len(calls) == 1 and not first.control.settled.is_set()
            assert threading.active_count() <= threads + 2  # the shared executor, no thread per reload
        finally:
            gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert first.control.settled.is_set()
        assert fake.held == {}
        last = generation()
        task = asyncio.create_task(last())
        deadline = time.monotonic() + 5
        while sweeps_in(tmp_path) == 0:
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        last.control.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    locks = [c for c in fake.calls if c[1] == fake.LK_NBLCK]
    unlocks = [c for c in fake.calls if c[1] == fake.LK_UNLCK]
    assert all(c[2:] == (1, 0) for c in fake.calls)
    assert len(unlocks) >= 2 and fake.held == {}
    assert len(locks) - len(unlocks) >= 12  # refused attempts by the twelve waiting generations


def test_platform_without_an_os_file_lock_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'fcntl', None)
    monkeypatch.setitem(sys.modules, 'msvcrt', None)
    with pytest.raises(RuntimeError, match='no OS file lock'):
        plugin.os_lease()
    reads = []
    host = _Host(tmp_path)
    factory = plugin.make_collector(host, plugin.LatestRead(), qh.StopControl(),
                                    read=lambda: reads.append(1) or (payload([], []), ''),
                                    first_delay_sec=0, interval_sec=.02, clock=lambda: NOW)

    async def scenario():
        task = asyncio.create_task(factory())
        await asyncio.sleep(.1)
        factory.control.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert factory.control.settled.is_set()
    asyncio.run(scenario())
    assert reads == [] and not list(tmp_path.iterdir())
    assert ('warning', 'claudexor quota history sweep not kept: RuntimeError') in host.logs


# ---------------------------------------------------------------------------
# Release repair: exact-final review P2 findings 1-3 and bounded advisories.


def _plan_sweeps(store, plans, labels, obs=((-3600, .1), (0, .3)), start=-4080, step=120):
    """One account watched every ``step`` seconds; ``plans(t)`` is the account
    list's plan and ``labels(t)`` the quota reading's own label at offset t."""
    t = start
    data = None
    while t <= 0:
        seen = [o for o in obs if o[0] <= t]
        rows = [snap('codex', 'a', [constraint('primary', seen[-1][1], reset=2 * 86400)],
                     observed=seen[-1][0], plan=labels(t))] if seen else []
        data = payload(rows, [profile('codex', 'a', plan=plans(t))], harnesses=('codex',))
        plugin.persist_sweep(store, data, '', NOW + t)
        t += step
    return data


@pytest.mark.parametrize('label', [None, 'pro'], ids=['quota-label-absent', 'quota-label-retains-old'])
def test_account_plan_change_cuts_pace_whatever_the_quota_label_says(tmp_path, label):
    # The reviewer's case: the account list reports pro -> max at N-1800 while
    # the quota reading carries no label, or still "pro"; use rises 0.1 -> 0.3.
    store = qh.HistoryStore(tmp_path)
    data = _plan_sweeps(store, lambda t: 'pro' if t < -1800 else 'max', lambda t: label)
    result = plugin.reserve_view(store, data, NOW, NOW)
    g = group(result['summary'], '|primary|')
    assert g['plans']['breakdown'] == [{'plan': 'max', 'accounts': 1, 'windows': .7}]
    pace = g['recent_pace']
    assert pace['state'] != 'ok' and pace['windows_per_hour'] is None
    assert not pace['exhaust_before_reset'] and pace['earliest_exhaustion_at'] is None
    assert result['chart']['recent_pace'] is None
    # History carries the account list's plan beside the reading's own label.
    with sqlite3.connect(store.path) as conn:
        kept = {row[0] for row in conn.execute('SELECT DISTINCT plan FROM run')}
    assert kept == {qs.plan_evidence(label or '', 'pro'), qs.plan_evidence(label or '', 'max')}
    # The tool reads the same cut.
    assert qs.compact(result['summary'])['groups'][0]['recent_pace']['state'] == pace['state']


def test_unchanged_plans_keep_the_same_pace_the_cut_removes(tmp_path):
    # Control for the case above: without the plan change the same readings
    # do make a pace over the whole hour, so the cut is what removed it.
    store = qh.HistoryStore(tmp_path)
    data = _plan_sweeps(store, lambda t: 'max', lambda t: None)
    pace = group(plugin.reserve_view(store, data, NOW, NOW)['summary'], '|primary|')['recent_pace']
    assert pace['state'] == 'ok'
    assert pace['windows_per_hour'] == pytest.approx(.2) and pace['span_min_seconds'] == 3600


def test_a_new_plan_reported_with_a_new_reading_paces_only_within_that_plan(tmp_path):
    store = qh.HistoryStore(tmp_path)
    data = _plan_sweeps(store, lambda t: 'pro' if t < -1800 else 'max', lambda t: 'pro',
                        obs=((-3600, .1), (-1800, .2), (0, .3)))
    pace = group(plugin.reserve_view(store, data, NOW, NOW)['summary'], '|primary|')['recent_pace']
    assert pace['state'] == 'ok' and pace['span_min_seconds'] == 1800  # never back across the change
    assert pace['windows_per_hour'] == pytest.approx(.2)


def test_an_unread_account_list_is_unknown_plan_evidence_and_cuts(tmp_path):
    store = qh.HistoryStore(tmp_path)
    t = -4080
    while t <= 0:
        seen = [o for o in ((-3600, .1), (0, .3)) if o[0] <= t]
        rows = [snap('codex', 'a', [constraint('primary', seen[-1][1], reset=2 * 86400)],
                     observed=seen[-1][0])] if seen else []
        reads = {'catalog': 'ok', 'accounts': 'failed' if t == -1800 else 'ok', 'quota': 'ok'}
        data = payload(rows, [profile('codex', 'a', plan='max')], harnesses=('codex',), reads=reads)
        plugin.persist_sweep(store, data, '', NOW + t)
        t += 120
    pace = group(plugin.reserve_view(store, data, NOW, NOW)['summary'], '|primary|')['recent_pace']
    assert pace['state'] != 'ok' and pace['windows_per_hour'] is None


# P2 finding 2: Refresh, then an immediate chart/family switch (reuse=1).


def _refresh_host(monkeypatch, now):
    """Real registered handlers over a fake host: the status reports 30% used
    until a foreground Refresh, whose envelope (and every later status read)
    reports 91%. Every upstream call is recorded."""
    upstream = []
    state = {'used': .30, 'observed': now - 120}

    def status():
        limit = {'id': 'primary', 'label': 'primary', 'used_ratio': state['used'],
                 'window_seconds': WEEK, 'resets_at': qs.iso(now + 2 * 86400)}
        return payload([snap('codex', 'c1', [limit], observed_abs=state['observed'])],
                       [profile('codex', 'c1')], harnesses=('codex',))

    def fake_request(_port, path, method='GET', timeout_sec=0):
        upstream.append((method, path))
        if method == 'POST':
            state['used'], state['observed'] = .91, now - 5
            return {'snapshots': status()['quota'], 'absences': [], 'refreshed_at': qs.iso(now)}, '', 200
        return status(), '', 200

    monkeypatch.setattr(plugin, '_request_json', fake_request)
    return upstream


def _used(view):
    return [a['quota']['label'] for g in view['groups'] for a in g['accounts']]


def test_refresh_then_reuse_switch_reads_after_the_refresh_through_registered_handlers(tmp_path, monkeypatch):
    now = time.time()
    upstream = _refresh_host(monkeypatch, now)
    host = _Host(tmp_path)
    plugin.register(host)
    first = host.routes['quotas']({'query_params': {'harness': 'codex', 'horizon': '24h'}})
    assert _used(first) == ['30% used']
    refreshed = host.routes['refresh']({})
    assert refreshed['ok'] and refreshed['quota_updates'][0]['quota']['label'] == '91% used'
    # The widget's 7d switch right after Refresh: reuse=1 must not answer
    # from the read made before the Refresh.
    switched = host.routes['quotas']({'query_params': {'harness': 'codex', 'horizon': '7d', 'reuse': '1'}})
    assert _used(switched) == ['91% used']
    g = switched['reserve']['summary']['groups'][0]
    assert g['measured']['windows'] == pytest.approx(.09)
    assert switched['reserve']['chart']['horizon'] == '7d'
    assert upstream == [('GET', plugin.STATUS_PATH), ('POST', plugin.REFRESH_PATH),
                        ('GET', plugin.STATUS_PATH)]
    # That post-Refresh read is reused by the next switch and the tool: no
    # further status reads, and never a provider refresh.
    back = host.routes['quotas']({'query_params': {'harness': 'codex', 'horizon': '24h', 'reuse': '1'}})
    assert _used(back) == ['91% used']
    tool = json.loads(host.tools['quota_summary']['handler'](None, harness='codex'))
    assert tool['groups'][0]['remaining_windows'] == pytest.approx(.09)
    assert len(upstream) == 3


def test_tool_after_refresh_does_not_reuse_a_pre_refresh_read(tmp_path, monkeypatch):
    now = time.time()
    upstream = _refresh_host(monkeypatch, now)
    host = _Host(tmp_path)
    plugin.register(host)
    host.routes['quotas']({})
    host.routes['refresh']({})
    tool = json.loads(host.tools['quota_summary']['handler'](None, harness='codex'))
    assert tool['groups'][0]['remaining_windows'] == pytest.approx(.09)
    assert upstream[-1] == ('GET', plugin.STATUS_PATH) and len(upstream) == 3


def test_a_status_read_in_the_air_across_a_refresh_is_not_remembered(tmp_path, monkeypatch):
    now = time.time()
    upstream = _refresh_host(monkeypatch, now)
    inner = plugin._request_json
    entered, release = threading.Event(), threading.Event()

    def held_get(port, path, method='GET', timeout_sec=0):
        if method == 'GET' and not entered.is_set():
            answer = inner(port, path, method, timeout_sec)  # read before the Refresh
            entered.set()
            assert release.wait(5)
            return answer
        return inner(port, path, method, timeout_sec)

    monkeypatch.setattr(plugin, '_request_json', held_get)
    host = _Host(tmp_path)
    plugin.register(host)
    views = []
    reader = threading.Thread(target=lambda: views.append(host.routes['quotas']({})))
    reader.start()
    assert entered.wait(5)
    host.routes['refresh']({})  # returns while the older read is still in the air
    release.set()
    reader.join(5)
    assert _used(views[0]) == ['30% used']  # that answer itself stays what it read
    switched = host.routes['quotas']({'query_params': {'reuse': '1', 'horizon': '7d'}})
    assert _used(switched) == ['91% used']
    assert [m for m, _p in upstream] == ['GET', 'POST', 'GET']


def test_latest_read_epoch_guard():
    latest = plugin.LatestRead()
    before = latest.epoch()
    latest.put({'reads': {}}, '', 1.0, before)
    assert latest.get(60) is not None
    latest.invalidate()
    assert latest.get(60) is None
    latest.put({'reads': {}}, '', 2.0, before)  # begun before the Refresh returned
    assert latest.get(60) is None
    latest.put({'reads': {}}, '', 3.0, latest.epoch())
    assert latest.get(60)[1] == 3.0


NODE_REFRESH_BRIDGE = r"""
async function waitFor(cond, what) {
  for (let i = 0; i < 1000; i++) {
    if (cond()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error('timed out waiting for ' + what + ': ' + JSON.stringify(calls));
}

const calls = [];
(async () => {
  const BASE = process.env.BRIDGE_BASE;
  const made = makeDocument();
  const window = {
    // The real widget's requests go to the real registered handlers.
    fetch(url, options = {}) {
      const method = options.method || 'GET';
      calls.push({ url, method });
      return fetch(BASE + url, { method, body: options.body }).then(async (r) => {
        const body = await r.text();
        return { ok: r.ok, status: r.status, text: async () => body };
      });
    },
    setInterval() { return 17; },
    clearInterval() {},
    addEventListener() {},
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    // 0.7.0: the widget bounds every request with a backstop timer and
    // clears it when the request settles.
    clearTimeout: (id) => clearTimeout(id),
    __ouroWidgetOnDispose() {},
  };
  const context = vm.createContext({
    window, document: made.document, console, Date, Math, Object, Array, String,
    Number, RegExp, Promise, setImmediate,
  });
  vm.runInContext(instrumentedWidgetSource, context, { filename: 'widget.js' });
  const root = made.root;
  const text = () => root.textContent;
  const chartReady = () => !/Loading the chart for this limit/.test(text())
    && walk(root).some((n) => n.getAttribute('class') === 'chart-svg');

  await waitFor(() => /0\.70 of 1/.test(text()) && chartReady(), 'first reading with its timeline');
  assert.doesNotMatch(calls[0].url, /reuse=1|chart=0/);
  // 0.8.0: the timeline is open on every mount; its limit's own span (a week)
  // is asked for once, with reuse, from the same status read.
  assert.equal(calls.length, 2);
  assert.match(calls[1].url, /reuse=1/);
  assert.match(calls[1].url, /horizon=7d/);
  byFocus(root, 'accounts').listeners.click[0]({ stopPropagation() {} });
  byFocus(root, 'acct:codex:c1').listeners.click[0]({ stopPropagation() {} });
  byFocus(root, 'inspector-diag').listeners.click[0]({ stopPropagation() {} });
  assert.match(text(), /30% used/);

  // Refresh: one POST, then one read of the whole projection — the rows, the
  // timeline and the selected account all from a reading made after it.
  byFocus(root, 'refresh').listeners.click[0]();
  await waitFor(() => /0\.09 of 1/.test(text()) && /91% used/.test(text()) && chartReady(), 'refreshed projection');
  assert.equal(calls[2].method, 'POST');
  assert.equal(calls[3].method, 'GET');
  assert.doesNotMatch(calls[3].url, /reuse=1/);
  assert.match(calls[3].url, /horizon=7d/);
  assert.doesNotMatch(text(), /30% used/);
  assert.doesNotMatch(text(), /refreshed after this overview/);

  // Immediately switch the timeline to 24 hours: a reuse request answered
  // from that post-Refresh read.
  byFocus(root, 'horizon:24h').listeners.click[0]({ stopPropagation() {} });
  await waitFor(() => calls.length === 5 && chartReady(), '24h chart');
  assert.match(calls[4].url, /horizon=24h/);
  assert.match(calls[4].url, /reuse=1/);
  assert.match(text(), /91% used/);
  assert.match(text(), /0\.09 of 1/);

  // And back to 7 days.
  byFocus(root, 'horizon:7d').listeners.click[0]({ stopPropagation() {} });
  await waitFor(() => calls.length === 6 && chartReady(), '7d chart');
  assert.match(text(), /91% used/);
  console.log(JSON.stringify(calls));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_refresh_then_chart_switch_over_registered_handlers(tmp_path, monkeypatch):
    import http.server
    from pathlib import Path
    from urllib.parse import parse_qsl, urlsplit

    import test_quotas
    from test_reserve import _node

    node = _node()
    assert node is not None, 'a Node runtime is required for widget tests'
    now = time.time()
    upstream = _refresh_host(monkeypatch, now)
    host = _Host(tmp_path)
    plugin.register(host)
    widget_path = Path(__file__).with_name('widget.js').resolve()
    prefix = test_quotas._widget_route_prefix(widget_path)

    class Bridge(http.server.BaseHTTPRequestHandler):
        def _answer(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = urlsplit(self.path)
            assert parts.path == prefix + 'quotas', parts.path
            self._answer(host.routes['quotas']({'query_params': dict(parse_qsl(parts.query))}))

        def do_POST(self):
            assert urlsplit(self.path).path == prefix + 'refresh', self.path
            self._answer(host.routes['refresh']({}))

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
        result = subprocess.run(
            [str(node), '-e', harness + NODE_REFRESH_BRIDGE],
            cwd=widget_path.parent,
            env={**dict(os.environ), 'WIDGET_PATH': str(widget_path),
                 'BRIDGE_BASE': f'http://127.0.0.1:{server.server_address[1]}'},
            text=True, capture_output=True, timeout=60, check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0, result.stdout + result.stderr
    asked = json.loads(result.stdout.strip().splitlines()[-1])
    assert [c['method'] for c in asked] == ['GET', 'GET', 'POST', 'GET', 'GET', 'GET']
    assert 'reuse=1' not in asked[0]['url'] and 'reuse=1' in asked[1]['url']
    # The read after the Refresh is a new status read; the switches reuse it.
    assert 'reuse=1' not in asked[3]['url'] and all('reuse=1' in c['url'] for c in asked[4:])
    # Upstream: the first read, the one explicit Refresh, one read after it.
    assert upstream == [('GET', plugin.STATUS_PATH), ('POST', plugin.REFRESH_PATH),
                        ('GET', plugin.STATUS_PATH)]


# P2 finding 3: stop requested while the collector resolves the port.


def test_stop_during_port_lookup_admits_no_status_request(tmp_path, monkeypatch):
    events = []
    entered, release = threading.Event(), threading.Event()
    lookups = []

    class LookupHost(_Host):
        def get_runtime_info(self):
            lookups.append(1)
            if len(lookups) == 2:  # the second cycle: hold the runtime lookup
                events.append('lookup_entered')
                entered.set()
                assert release.wait(5)
                events.append('port_lookup_return')
            return {'server_port': 8765}

    data = host_at(time.time(), {'a': [(-10, .2, 86400)]})

    def fake_request(_port, path, method='GET', timeout_sec=0):
        events.append('fetch_started')
        return data, '', 200

    monkeypatch.setattr(plugin, '_request_json', fake_request)
    make = plugin.make_collector
    monkeypatch.setattr(plugin, 'make_collector', lambda api, latest, stop: make(
        api, latest, stop, first_delay_sec=0, interval_sec=.02))
    host = LookupHost(tmp_path)
    plugin.register(host)  # the real registration: port resolver and fetch
    factory = host.tasks[0][1]

    async def scenario():
        task = asyncio.create_task(factory())
        await until(entered)
        host.unload[0]()  # the registered stop callback returns at once
        events.append('stop_returned')
        release.set()
        await asyncio.wait_for(task, 5)  # stopped: the coroutine returns
        assert factory.control.settled.is_set()
    asyncio.run(scenario())
    # First cycle (control): lookup, then the request. Second: stop wins.
    assert events == ['fetch_started', 'lookup_entered', 'stop_returned', 'port_lookup_return']
    with sqlite3.connect(tmp_path / qh.HISTORY_FILE) as conn:
        assert conn.execute('SELECT count(*) FROM sweep').fetchone()[0] == 1


# Advisories: exact model-scope identity, the unrestricted figure on screen.


def _oversized_scopes():
    common = [f'm{i:02d}' for i in range(24)]
    rows = [snap('codex', sid, [constraint('scoped', .2, reset=86400, models=common + [extra], label='scoped')])
            for sid, extra in (('a', 'm99-x'), ('b', 'm99-y'))]
    return payload(rows, [profile('codex', 'a'), profile('codex', 'b')], harnesses=('codex',))


def test_oversized_model_scopes_keep_their_whole_identity():
    summary, _state = summary_of(_oversized_scopes())
    groups = summary['groups']
    assert len(groups) == 2 and groups[0]['key'] != groups[1]['key']
    assert [g['measured']['windows'] for g in groups] == [.8, .8]  # never one 1.6 group
    assert all(len(g['models']) == qs.MAX_MODELS and g['models_omitted'] == 1 for g in groups)
    compact = qs.compact(summary)['groups']
    assert [row['models_omitted'] for row in compact] == [1, 1]
    # Ordinary scopes keep the key they always had.
    assert qs.group_key('codex', 'p', WEEK, ('fable-a', 'fable-b')).endswith(
        '|' + hashlib.sha256(b'fable-a\nfable-b').hexdigest()[:10])
    assert 'models_omitted' not in qs.compact(summary_of(payload(
        [snap('codex', 'a', [constraint('scoped', .2, models=['x'])])], [profile('codex', 'a')]))[0])['groups'][0]


def test_a_cooldown_on_one_oversized_scope_does_not_restrict_its_twin():
    data = _oversized_scopes()
    common = [f'm{i:02d}' for i in range(24)]
    # Account a (scope ...m99-x) reports a live cooldown on the twin scope (...m99-y).
    cooldown = {'id': 'cooldown', 'used_ratio': None, 'window_seconds': None,
                'cooldown_until': at(600), 'applies_to_models': common + ['m99-y']}
    data['quota'][0]['constraints'].append(cooldown)
    summary, _state = summary_of(data)
    assert [g['restrictions'] for g in summary['groups']] == [{}, {}]
    # The same cooldown on a's own scope does restrict a, in that group only.
    cooldown['applies_to_models'] = common + ['m99-x']
    summary, _state = summary_of(data)
    assert sorted(g['restrictions'].get('cooling', {}).get('accounts', 0) for g in summary['groups']) == [0, 1]


NODE_ADVISORY_MATRIX = r"""
(async () => {
  const fixture = JSON.parse(process.env.ADVISORY_FIXTURE);
  const env = await boot(fixture);
  const text = env.root.textContent;
  // A 25-model scope: the row names the first model and how many more there
  // are; the scope words say how many are not listed. Two scopes whose
  // listed names are the same still get two names on screen.
  assert.match(text, /m00 \+24/);
  // 0.8.0: one .lrow per limit; its whole spoken summary is on the row's one
  // control (0.8.1: the chart toggle, .l-chart), which shows the limit in
  // the timeline.
  const rows = classes(env.root, 'lrow');
  const names = rows.map((r) => classes(r, 'l-name-text')[0].textContent);
  assert.equal(new Set(names).size, names.length, names.join(' | '));
  const heard = rows.map((r) => classes(r, 'l-chart')[0].getAttribute('aria-label')).join(' ');
  assert.match(heard, /models: m00, m01, .*m23 \+1 more/);
  // With a restriction present, the unrestricted account-windows stand beside it.
  assert.match(heard, /1 cooldown reported \(0\.70\)/);
  assert.match(heard, /0\.50 unrestricted/);
  // The limit in the timeline says the same in its details.
  const details = classes(env.root, 'chart-table')[0];
  assert.ok(details, 'the timeline carries the limit details');
  assert.match(details.textContent, /1 cooldown reported \(0\.70\)/);
  assert.match(details.textContent, /0\.50 unrestricted/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_names_oversized_scopes_and_shows_unrestricted(tmp_path):
    from pathlib import Path

    import test_quotas
    from test_reserve import _node, widget_reserve

    node = _node()
    assert node is not None, 'a Node runtime is required for widget tests'
    now = time.time()
    common = [f'm{i:02d}' for i in range(24)]

    def row(sid, primary, extra, cooling):
        constraints = [
            {'id': 'primary', 'label': 'primary', 'used_ratio': primary, 'window_seconds': WEEK,
             'resets_at': qs.iso(now + 86400)},
            {'id': 'scoped', 'label': 'scoped', 'used_ratio': .2, 'window_seconds': WEEK,
             'resets_at': qs.iso(now + 86400), 'applies_to_models': common + [extra]},
        ]
        if cooling:
            constraints.append({'id': 'cooldown', 'used_ratio': None, 'window_seconds': None,
                                'cooldown_until': qs.iso(now + 600)})
        return {'subject': {'harness': 'codex', 'subject_id': sid}, 'constraints': constraints,
                'availability': {'state': 'available'}, 'observed_at': qs.iso(now - 30),
                'freshness': 'fresh', 'source': 'app'}

    data = payload([row('a', .3, 'm99-x', True), row('b', .5, 'm99-y', False)],
                   [profile('codex', 'a'), profile('codex', 'b')], harnesses=('codex',))
    view = plugin.build_view(data, '')
    view['reserve'] = widget_reserve(qh.HistoryStore(tmp_path), data, now, now, harness='codex', name_accounts=True)
    widget_path = Path(__file__).with_name('widget.js').resolve()
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    result = subprocess.run(
        [str(node), '-e', harness + NODE_ADVISORY_MATRIX],
        cwd=widget_path.parent,
        env={**dict(os.environ), 'WIDGET_PATH': str(widget_path), 'ADVISORY_FIXTURE': json.dumps(view)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Skill-review closure: a run that fails is restarted by the host; unload and
# cancellation are not. The runner below is the host's supervised runner
# (ouroboros/extension_plugin_api.py, _start_supervised_task) without its
# backoff: a return ends it, a cancellation propagates, an Exception calls
# the factory again.

async def supervise(factory, max_restarts=3):
    restarts = 0
    while True:
        try:
            await factory()
            return restarts
        except asyncio.CancelledError:
            raise
        except Exception:
            restarts += 1
            if restarts > max_restarts:
                return restarts
            await asyncio.sleep(0)


class RefusesFirstJob(concurrent.futures.ThreadPoolExecutor):
    """An executor that refuses the first job it is given, as a shut-down or
    overloaded one would: run_in_executor raises inside the collector."""

    def __init__(self):
        super().__init__(max_workers=2)
        self.refused = 0

    def submit(self, fn, /, *args, **kwargs):
        if not self.refused:
            self.refused += 1
            raise RuntimeError('executor refused the job')
        return super().submit(fn, *args, **kwargs)


class LogFailsOnce(_Host):
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.failed = False

    def log(self, level, message, **fields):
        if not self.failed:
            self.failed = True
            raise RuntimeError('host log unavailable')
        super().log(level, message, **fields)


def _registered_collector(host, monkeypatch, read):
    make = plugin.make_collector
    monkeypatch.setattr(plugin, 'make_collector', lambda api, latest, stop: make(
        api, latest, stop, read=read, clock=lambda: NOW, first_delay_sec=0, interval_sec=.01))
    plugin.register(host)
    return host.tasks[0][1]


@pytest.mark.parametrize('failure', ['executor', 'log'])
def test_a_failed_run_is_restarted_by_the_host_and_sweeps_again(tmp_path, monkeypatch, failure):
    reads = []

    def read():
        reads.append(1)
        if failure == 'log' and len(reads) == 1:
            raise OSError('status read failed')  # the sweep fails; its log then fails too
        return host_at(NOW, {'a': [(0, .2, 86400)]}), ''

    host = LogFailsOnce(tmp_path) if failure == 'log' else _Host(tmp_path)
    factory = _registered_collector(host, monkeypatch, read)

    async def scenario():
        loop = asyncio.get_running_loop()
        if failure == 'executor':
            loop.set_default_executor(RefusesFirstJob())
        runner = asyncio.create_task(supervise(factory))
        deadline = time.monotonic() + 5
        while not sweeps_in(tmp_path):
            assert not runner.done(), 'the restarted run ended before any sweep'
            assert time.monotonic() < deadline, 'no sweep after the restart'
            await asyncio.sleep(.005)
        assert not factory.control.is_set(), 'the failure stopped the registration'
        host.unload[0]()  # exact registered callback
        restarts = await asyncio.wait_for(runner, 5)
        assert restarts == 1
        assert factory.control.settled.is_set()
    asyncio.run(scenario())
    assert sweeps_in(tmp_path) >= 1


@pytest.mark.parametrize('ending', ['cancel', 'unload'])
def test_unload_or_cancellation_cannot_restart_effects(tmp_path, monkeypatch, ending):
    reads = []

    def read():
        reads.append(1)
        return host_at(NOW, {'a': [(0, .2, 86400)]}), ''

    host = _Host(tmp_path)
    factory = _registered_collector(host, monkeypatch, read)

    async def scenario():
        runner = asyncio.create_task(supervise(factory))
        deadline = time.monotonic() + 5
        while not sweeps_in(tmp_path):
            assert time.monotonic() < deadline
            await asyncio.sleep(.005)
        if ending == 'cancel':
            runner.cancel()  # disable, unload or shutdown
            with pytest.raises(asyncio.CancelledError):
                await runner
        else:
            host.unload[0]()
            assert await asyncio.wait_for(runner, 5) == 0  # a return: no restart
        assert factory.control.is_set() and factory.control.settled.is_set()
        count, swept = len(reads), sweeps_in(tmp_path)
        # Whoever calls the factory again after that gets a run that ends
        # before any I/O.
        await asyncio.wait_for(supervise(factory), 1)
        await asyncio.wait_for(factory(), 1)
        await asyncio.sleep(.05)
        assert (len(reads), sweeps_in(tmp_path)) == (count, swept)
        assert factory.control.settled.is_set()
    asyncio.run(scenario())


class _Body:
    """A request whose body the route awaits, as an ASGI request's is."""

    def __init__(self, payload=None, broken=False):
        self.payload, self.broken = payload, broken

    async def json(self):
        if self.broken:
            raise ValueError('Expecting value: line 1 column 1 (char 0)')
        return self.payload


def _prefs_route(state_dir):
    host = _Host(state_dir)
    plugin.register(host)
    return host, host.routes['prefs']


def test_prefs_are_written_off_the_event_loop(tmp_path, monkeypatch):
    _host, route = _prefs_route(tmp_path)
    entered, release = threading.Event(), threading.Event()
    replace = os.replace
    threads = []

    def held_replace(src, dst):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5), 'the loop was blocked by the write'
        return replace(src, dst)

    monkeypatch.setattr(plugin.os, 'replace', held_replace)

    async def scenario():
        task = asyncio.create_task(route(_Body({'density': 'compact'})))
        await until(entered)  # the loop keeps turning while the file is written
        tick = asyncio.Event()
        asyncio.get_running_loop().call_soon(tick.set)
        await asyncio.wait_for(tick.wait(), .5)
        release.set()
        return await asyncio.wait_for(task, 5)

    answer = asyncio.run(scenario())
    assert answer['error'] == '' and answer['prefs']['density'] == 'compact'
    assert threads and all(t != threading.get_ident() for t in threads)
    assert json.loads((tmp_path / 'prefs.json').read_text())['density'] == 'compact'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['prefs.json']


def test_a_malformed_body_is_refused_off_the_loop_and_keeps_the_saved_choice(tmp_path, monkeypatch):
    host, route = _prefs_route(tmp_path)
    saved = asyncio.run(route(_Body({'density': 'detailed', 'models': {'claude': 'models'}})))
    assert saved['error'] == ''
    before = (tmp_path / 'prefs.json').read_bytes()
    read = plugin.read_prefs
    threads = []

    def traced_read(api):
        threads.append(threading.get_ident())
        return read(api)

    monkeypatch.setattr(plugin, 'read_prefs', traced_read)
    answer = asyncio.run(route(_Body(broken=True)))
    assert answer == {'prefs': saved['prefs'], 'error': 'request body was not JSON'}
    assert threads and all(t != threading.get_ident() for t in threads)
    assert (tmp_path / 'prefs.json').read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['prefs.json']
    assert any('not JSON' in message for _level, message in host.logs)


class RunsBackwards(concurrent.futures.ThreadPoolExecutor):
    """Holds every job, then runs them newest first on one thread: two saves
    reach the file in the opposite order to the one they arrived in."""

    def __init__(self):
        super().__init__(max_workers=1)
        self.jobs = []

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        self.jobs.append((future, fn, args))
        return future

    def run_backwards(self):
        for future, fn, args in reversed(self.jobs):
            try:
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)


def test_two_crossing_saves_keep_the_later_arrival(tmp_path):
    _host, route = _prefs_route(tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        executor = RunsBackwards()
        loop.set_default_executor(executor)
        first = asyncio.create_task(route(_Body({'density': 'compact'})))
        second = asyncio.create_task(route(_Body({'density': 'detailed'})))
        deadline = time.monotonic() + 5
        while len(executor.jobs) < 2:
            assert time.monotonic() < deadline
            await asyncio.sleep(.002)
        worker = threading.Thread(target=executor.run_backwards)
        worker.start()
        answers = await asyncio.wait_for(asyncio.gather(first, second), 5)
        worker.join(5)
        return answers

    first, second = asyncio.run(scenario())
    assert second == {'prefs': {**plugin.clean_prefs(None), 'density': 'detailed'}, 'error': ''}
    # The earlier save reached the file last: it answers with what is kept.
    assert first == second
    assert json.loads((tmp_path / 'prefs.json').read_text())['density'] == 'detailed'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['prefs.json']


def test_concurrent_saves_from_two_loads_never_share_a_temporary_file(tmp_path, monkeypatch):
    routes = [_prefs_route(tmp_path)[1], _prefs_route(tmp_path)[1]]  # e.g. across a reload
    replace = os.replace
    active, overlap = [0], [0]
    gate = threading.Lock()

    def slow_replace(src, dst):
        with gate:
            active[0] += 1
            overlap[0] = max(overlap[0], active[0])
        try:
            time.sleep(.004)
            return replace(src, dst)
        finally:
            with gate:
                active[0] -= 1

    monkeypatch.setattr(plugin.os, 'replace', slow_replace)
    choices = ['compact', 'normal', 'detailed']

    async def scenario():
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=8))
        return await asyncio.gather(*(
            routes[i % 2](_Body({'density': choices[i % 3]})) for i in range(24)))

    answers = asyncio.run(scenario())
    assert all(a['error'] == '' for a in answers), [a['error'] for a in answers if a['error']]
    assert overlap[0] >= 2, 'the two loads never wrote at the same time'
    stored = json.loads((tmp_path / 'prefs.json').read_text())
    assert stored == plugin.clean_prefs(stored) and stored['density'] in choices
    assert sorted(p.name for p in tmp_path.iterdir()) == ['prefs.json']
