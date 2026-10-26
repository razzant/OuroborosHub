"""Public-command, independent-connection and restart regression witnesses.

Provider boundaries are the existing in-memory transports, not live accounts.
Interleaving tests assert that the selected injection really ran.
"""
import json
import sqlite3
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from test_calendar_provider_divergence import caldav_context, google_context, _day, run_open_intents, sync
from test_conflict_resolution import create, remote_edit, inspection, resolve
from store import Store
from model import iso_utc
import ops
import tools


def instance(ctx, server, eid, day=3):
    master = ctx.store.get_event(eid)
    slot = _day(day)
    ext = master['external_id'] + '_' + slot.strftime('%Y%m%dT%H%M%SZ')
    server.events[ext] = {**server.events[master['external_id']], 'id': ext,
        'start': {'dateTime': slot.isoformat()}, 'end': {'dateTime': (slot+timedelta(hours=1)).isoformat()},
        'originalStartTime': {'dateTime': slot.isoformat()}, 'recurringEventId': master['external_id'],
        'etag': server._etag()}
    server.events[ext].pop('recurrence', None)
    return ext, f'{eid}@{iso_utc(slot)}'


def reopen(ctx):
    ctx.store = Store(ctx.store.state_dir)


class AtomicReplayTests(unittest.TestCase):
    def test_owner_stage_is_atomic_to_a_separate_connection(self):
        ctx, cid, server = google_context(); eid = create(ctx, cid)
        remote_edit(ctx, server, eid)
        observer = Store(ctx.store.state_dir)
        fired, started, done = threading.Event(), threading.Event(), threading.Event()
        answers, failures, threads = [], [], []
        original = Store.update_event
        def sync_write():
            started.set()
            try:
                answers.append(observer.apply_provider_snapshot(eid, {'title': 'Remote'}))
            except BaseException as exc:
                failures.append(exc)
            finally:
                done.set()
        def interleaved(store, event_id, fields):
            original(store, event_id, fields)
            if event_id == eid and fields.get('title') == 'Owner choice' and not fired.is_set():
                fired.set()
                thread = threading.Thread(target=sync_write); threads.append(thread); thread.start()
                self.assertTrue(started.wait(2))
                # A reader sees the old committed row, never the half-staged choice.
                self.assertEqual(observer.get_event(eid)['title'], 'Original')
                self.assertFalse(done.wait(.05))
        try:
            with patch.object(Store, 'update_event', interleaved):
                result = json.loads(tools.cal_update(ctx, id=eid, title='Owner choice', confirm=True))
        finally:
            for thread in threads: thread.join(6)
        self.assertTrue(fired.is_set()); self.assertTrue(done.is_set()); self.assertFalse(failures)
        self.assertEqual(answers, [False]); self.assertEqual(result['status'], 'conflict')
        self.assertEqual(ctx.store.get_event(eid)['title'], 'Owner choice')

    def test_stage_rollback_has_no_network_or_partial_owner_state(self):
        ctx, cid, server = google_context(); eid = create(ctx, cid)
        before, intents, count = ctx.store.get_event(eid), ctx.store.intents_for_event(eid), len(server.calls)
        fired = []
        def fail(*args, **kwargs):
            fired.append(True); raise RuntimeError('intent publication rejected')
        with patch.object(Store, 'add_intent', fail):
            with self.assertRaisesRegex(RuntimeError, 'intent publication'):
                ops.update_event(ctx.store, ctx.providers, eid, {'title': 'Must roll back'})
        self.assertEqual(fired, [True]); self.assertEqual(len(server.calls), count)
        self.assertEqual(ctx.store.get_event(eid), before); self.assertEqual(ctx.store.intents_for_event(eid), intents)

    def test_network_starts_after_staging_commit(self):
        ctx, cid, server = google_context(); eid = create(ctx, cid)
        request = server.request; observed = []
        def unlocked(method, *args, **kwargs):
            if method == 'PATCH':
                conn = sqlite3.connect(ctx.store.path, timeout=.1, isolation_level=None)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    observed.append(conn.execute("SELECT count(*) FROM intents WHERE event_id=? AND state='pending'", (eid,)).fetchone()[0])
                    conn.rollback()
                finally: conn.close()
            return request(method, *args, **kwargs)
        ctx.providers.adapter_for('')._request = unlocked
        result = json.loads(tools.cal_update(ctx, id=eid, title='Committed', confirm=True))
        self.assertEqual(result['status'], 'updated'); self.assertEqual(observed, [1])

    def test_split_both_choices_survive_restart_on_both_adapters(self):
        for factory in (caldav_context, google_context):
            for choice in ('local', 'provider'):
                with self.subTest(provider=factory.__name__, choice=choice):
                    ctx, cid, server = factory(); eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10')
                    remote_edit(ctx, server, eid)
                    result = json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(4))}', scope='following', title='New segment', confirm=True))
                    self.assertEqual(result['status'], 'conflict')
                    reopen(ctx)
                    info = inspection(ctx, eid)
                    self.assertGreaterEqual(len(info['operations']), 2)
                    answer = resolve(ctx, eid, info, choice)
                    self.assertEqual(answer['status'], 'pending' if choice == 'local' else 'resolved')
                    reopen(ctx); run_open_intents(ctx, rounds=3); sync(ctx, cid)
                    remote = server.resources if hasattr(server, 'resources') else server.live()
                    self.assertEqual(len(remote), 2 if choice == 'local' else 1)
                    rows = ctx.store.window(_day(2), _day(13), [cid])
                    masters = [r for r in rows if not r['master_id']]
                    self.assertEqual(len(masters), 2 if choice == 'local' else 1)
                    self.assertFalse(ctx.store.open_intents())
                    self.assertTrue(all(r['sync_state'] == 'synced' for r in masters))
                    if choice == 'local':
                        self.assertIn('UNTIL=', ctx.store.get_event(eid)['rrule'])
                    else:
                        self.assertEqual(ctx.store.get_event(eid)['title'], 'Remote')

    def test_first_offline_google_instance_does_not_adopt_concurrent_changed_field(self):
        for field, value in (('title', 'Owner title'), ('response', 'declined')):
            with self.subTest(field=field):
                ctx, cid, server = google_context()
                eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10', attendees=['owner@example.test'])
                ext, addressed = instance(ctx, server, eid)
                server.down = True
                result = json.loads(tools.cal_update(ctx, id=addressed, confirm=True, **{field: value}))
                self.assertEqual(result['status'], 'pending')
                server.down = False
                if field == 'title': server.events[ext]['summary'] = 'Other client'
                else: server.events[ext]['attendees'][0]['responseStatus'] = 'tentative'
                server.events[ext]['etag'] = server._etag()
                count = len(server.calls)
                reopen(ctx); run_open_intents(ctx)
                self.assertFalse(any(c[0] == 'PATCH' for c in server.calls[count:]))
                self.assertTrue(any(i['state'] == 'conflict' for i in ctx.store.open_intents()))

    def test_lost_first_instance_patch_never_rebases_on_retry(self):
        ctx, cid, server = google_context(); eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10')
        ext, addressed = instance(ctx, server, eid)
        server.lose_response.add('PATCH')
        result = json.loads(tools.cal_update(ctx, id=addressed, location='Owner room', confirm=True))
        self.assertEqual(result['status'], 'pending')
        old = ctx.store.open_intents()[0]['expected_etag']; self.assertTrue(old)
        server.events[ext].update(location='Other room', etag=server._etag())
        reopen(ctx); run_open_intents(ctx)
        self.assertEqual(server.events[ext]['location'], 'Other room')
        self.assertEqual(ctx.store.open_intents()[0]['expected_etag'], old)
        self.assertEqual(ctx.store.open_intents()[0]['state'], 'conflict')

    def test_preleased_google_batch_rereads_own_ack_version(self):
        ctx, cid, server = google_context(); eid = create(ctx, cid)
        server.down = True
        tools.cal_update(ctx, id=eid, title='Owner title', confirm=True)
        tools.cal_update(ctx, id=eid, location='Owner room', confirm=True)
        server.down = False
        with ctx.store._conn() as conn:
            conn.execute("UPDATE intents SET next_attempt_at=NULL WHERE state='pending'")
        leased = ctx.store.lease_due_intents('batch'); self.assertEqual(len(leased), 2)
        self.assertEqual(leased[0]['expected_etag'], leased[1]['expected_etag'])
        self.assertEqual(ops.run_leased_intent(ctx.store, ctx.providers, leased[0])['status'], 'done')
        self.assertNotEqual(ctx.store.get_intent(leased[1]['id'])['expected_etag'], leased[1]['expected_etag'])
        self.assertEqual(ops.run_leased_intent(ctx.store, ctx.providers, leased[1])['status'], 'done')
        self.assertEqual(server.live()[0]['summary'], 'Owner title'); self.assertEqual(server.live()[0]['location'], 'Owner room')

    def test_stale_feed_cannot_undo_an_acknowledged_owner_edit_during_fetch(self):
        ctx, cid, server = google_context(); eid = create(ctx, cid)
        remote_edit(ctx, server, eid, 'Remote before fetch')
        adapter = ctx.providers.adapter_for(''); fetch = adapter.fetch; fired = []
        # Bring the expected version up to date before creating the actual race.
        sync(ctx, cid)
        def racing(*args, **kwargs):
            result = fetch(*args, **kwargs)
            fired.append(True)
            answer = json.loads(tools.cal_update(ctx, id=eid, title='Accepted during fetch', confirm=True))
            self.assertEqual(answer['status'], 'updated')
            return result
        with patch.object(adapter, 'fetch', racing): sync(ctx, cid)
        self.assertEqual(fired, [True]); self.assertEqual(server.live()[0]['summary'], 'Accepted during fetch')
        self.assertEqual(ctx.store.get_event(eid)['title'], 'Accepted during fetch')

    def test_provider_choice_keeps_an_earlier_independent_exception_edit(self):
        ctx, cid, server = caldav_context(); eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10')
        address = f'{eid}@{iso_utc(_day(3))}'
        self.assertEqual(json.loads(tools.cal_update(ctx, id=address, title='Published exception', confirm=True))['status'], 'updated')
        exc = ctx.store.exceptions_for(eid)[0]
        server.down = True
        self.assertEqual(json.loads(tools.cal_update(ctx, id=exc['id'], title='Independent edit', confirm=True))['status'], 'pending')
        independent = ctx.store.intents_for_event(exc['id'])[-1]['id']
        server.down = False; remote_edit(ctx, server, eid)
        # This master-only edit has no topology dependency on the older child
        # edit. Choosing its provider version must not consume that child edit.
        answer = json.loads(tools.cal_update(ctx, id=eid, scope='all', title='Master choice', confirm=True))
        self.assertEqual(answer['status'], 'conflict')
        info = inspection(ctx, eid)
        self.assertIn(independent, info['preserved_operations'])
        self.assertEqual(resolve(ctx, eid, info, 'provider')['status'], 'resolved')
        reopen(ctx)
        kept = ctx.store.get_event(exc['id'])
        self.assertEqual(kept['title'], 'Independent edit'); self.assertEqual(kept['recurrence_id'], exc['recurrence_id'])
        self.assertEqual(ctx.store.get_intent(independent)['state'], 'pending')
        self.assertEqual(json.loads(tools.cal_events(ctx, id=exc['id']))['event']['title'], 'Independent edit')

    def test_readback_of_another_caldav_writer_does_not_advance_queued_versions(self):
        ctx, cid, server = caldav_context(); eid = create(ctx, cid)
        server.down = True
        tools.cal_update(ctx, id=eid, title='Owner first', confirm=True)
        tools.cal_update(ctx, id=eid, location='Owner second', confirm=True)
        old = ctx.store.open_intents()[-1]['expected_etag']; server.down = False
        adapter = ctx.providers.adapter_for(''); request = adapter._request; fired = []
        def without_etag(method, url, body=None, headers=None):
            result = request(method, url, body, headers)
            if method == 'PUT' and not fired:
                fired.append(True); remote_edit(ctx, server, eid, 'Other writer')
                return result[0], {}, result[2]
            return result
        with patch.object(adapter, '_request', without_etag): run_open_intents(ctx, rounds=2)
        self.assertEqual(fired, [True]); self.assertEqual(ctx.store.open_intents()[-1]['expected_etag'], old)
        self.assertEqual(ctx.store.open_intents()[-1]['state'], 'conflict')
        self.assertIn('SUMMARY:Other writer', next(iter(server.resources.values()))[0])

    def test_first_google_occurrence_cancel_is_conditional(self):
        ctx, cid, server = google_context(); eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10')
        ext, addressed = instance(ctx, server, eid)
        result = json.loads(tools.cal_delete(ctx, id=addressed, confirm=True))
        self.assertEqual(result['status'], 'deleted'); self.assertEqual(server.events[ext]['status'], 'cancelled')
        exc = ctx.store.exceptions_for(eid)[0]
        self.assertTrue(ctx.store.intents_for_event(exc['id'])[-1]['expected_etag'])

    def test_lost_create_does_not_overwrite_an_external_edit_on_retry(self):
        for factory, method in ((google_context, 'POST'), (caldav_context, 'PUT')):
            with self.subTest(factory=factory.__name__):
                ctx, cid, server = factory(); server.lose_response.add(method)
                out = json.loads(tools.cal_create(ctx, title='First choice', start=_day(3).isoformat(), calendars=[cid], confirm=True))
                self.assertEqual(out['status'], 'pending'); eid = out['event']['id']
                adapter = ctx.providers.adapter_for('')
                identity = adapter.created_identity(ctx.store.get_calendar(cid), ctx.store.get_event(eid))
                ctx.store.record_created_event(eid, {**identity, 'etag': ''})
                remote_edit(ctx, server, eid, 'Other writer')
                reopen(ctx); run_open_intents(ctx)
                self.assertEqual(ctx.store.get_event(eid)['sync_state'], 'conflict')
                if hasattr(server, 'events'):
                    self.assertEqual(server.live()[0]['summary'], 'Other writer')
                else:
                    self.assertIn('SUMMARY:Other writer', next(iter(server.resources.values()))[0])
