"""A new owner edit recovers an immutable failed birth, never a fresh ETag rebase."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
from test_calendar_provider_divergence import caldav_context, google_context, _day, exhaust_create_retries, run_open_intents, sync
from test_conflict_resolution import remote_edit
from store import Store
import tools


class BirthRecoveryTests(unittest.TestCase):
    def lost(self, factory, method):
        ctx, cid, server = factory()
        server.lose_response.add(method)
        out = json.loads(tools.cal_create(ctx, title='Before', start=_day(3).isoformat(), calendars=[cid], confirm=True))
        self.assertEqual(out['status'], 'pending')
        eid = out['event']['id']
        server.down = True
        birth = exhaust_create_retries(ctx, eid)
        server.down = False
        return ctx, cid, server, eid, birth

    def title(self, server):
        if hasattr(server, 'events'):
            return server.live()[0]['summary']
        from providers import _ical
        doc = _ical().Calendar.from_ical(next(iter(server.resources.values()))[0])
        return str(doc.walk('VEVENT')[0]['SUMMARY'])

    def test_recovery_keeps_distinct_desired_snapshots_attempts_and_one_resource(self):
        for factory, method in ((caldav_context, 'PUT'), (google_context, 'POST')):
            for adopted in (False, True):
                with self.subTest(provider=factory.__name__, sync_adopted=adopted):
                    ctx, cid, server, eid, birth = self.lost(factory, method)
                    if adopted: sync(ctx, cid)
                    answer = json.loads(tools.cal_update(ctx, id=eid, title='After', confirm=True))
                    self.assertEqual(answer['status'], 'updated')
                    writes = ctx.store.intents_for_event(eid)
                    recovered = ctx.store.get_intent(birth['id'])
                    self.assertEqual(recovered['state'], 'done')
                    self.assertGreater(recovered['attempts'], birth['attempts'])
                    self.assertEqual(json.loads(recovered['payload_json'])['desired']['title'], 'Before')
                    self.assertEqual(json.loads(writes[-1]['payload_json'])['desired']['title'], 'After')
                    self.assertEqual(self.title(server), 'After')
                    self.assertEqual(len(server.live() if hasattr(server, 'events') else server.resources), 1)

    def test_foreign_content_before_recovery_is_not_overwritten(self):
        for factory, method in ((caldav_context, 'PUT'), (google_context, 'POST')):
            with self.subTest(provider=factory.__name__):
                ctx, cid, server, eid, birth = self.lost(factory, method)
                sync(ctx, cid)
                remote_edit(ctx, server, eid, 'Foreign')
                answer = json.loads(tools.cal_update(ctx, id=eid, title='After', confirm=True))
                self.assertNotEqual(answer['status'], 'updated')
                self.assertEqual(self.title(server), 'Foreign')
                self.assertEqual(ctx.store.get_intent(birth['id'])['state'], 'conflict')

    def test_foreign_edit_after_recovery_ack_conflicts_without_rebase(self):
        for factory, method in ((caldav_context, 'PUT'), (google_context, 'POST')):
            with self.subTest(provider=factory.__name__):
                ctx, cid, server, eid, birth = self.lost(factory, method)
                original = ctx.store.acknowledge_write
                fired = []
                def race(intent, *args, **kwargs):
                    acknowledged = original(intent, *args, **kwargs)
                    if acknowledged and intent['id'] == birth['id']:
                        fired.append(True)
                        remote_edit(ctx, server, eid, 'Foreign after ACK')
                    return acknowledged
                with patch.object(ctx.store, 'acknowledge_write', race):
                    result = json.loads(tools.cal_update(ctx, id=eid, title='After', confirm=True))
                self.assertEqual(fired, [True])
                self.assertEqual(result['status'], 'conflict')
                self.assertEqual(self.title(server), 'Foreign after ACK')
                self.assertEqual(ctx.store.intents_for_event(eid)[-1]['state'], 'conflict')

    def test_network_during_recovery_keeps_both_pending_across_reopen(self):
        for factory, method in ((caldav_context, 'PUT'), (google_context, 'POST')):
            with self.subTest(provider=factory.__name__):
                ctx, cid, server, eid, birth = self.lost(factory, method)
                server.down = True
                result = json.loads(tools.cal_update(ctx, id=eid, title='After', confirm=True))
                self.assertEqual(result['status'], 'pending')
                writes = ctx.store.intents_for_event(eid)
                self.assertEqual([i['state'] for i in writes], ['pending', 'pending'])
                self.assertEqual(writes[-1]['attempts'], 0)
                ctx.store = Store(ctx.store.state_dir)
                server.down = False
                run_open_intents(ctx, rounds=2)
                self.assertEqual(self.title(server), 'After')
                self.assertEqual(ctx.store.get_event(eid)['sync_state'], 'synced')

    def test_birth_requeue_rolls_back_with_failed_new_intent_publication(self):
        ctx, cid, server, eid, birth = self.lost(caldav_context, 'PUT')
        before, calls = ctx.store.get_event(eid), len(server.calls)
        with patch.object(Store, 'add_intent', side_effect=RuntimeError('new intent unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'new intent unavailable'):
                tools.cal_update(ctx, id=eid, title='After', confirm=True)
        self.assertEqual(ctx.store.get_event(eid), before)
        self.assertEqual(ctx.store.get_intent(birth['id']), birth)
        self.assertEqual(len(server.calls), calls)

    def test_hard_rejection_and_missing_original_snapshot_are_not_reopened(self):
        for mode in ('auth', 'missing_snapshot'):
            with self.subTest(mode=mode):
                ctx, cid, server, eid, birth = self.lost(caldav_context, 'PUT')
                if mode == 'auth': ctx.store.settle_intent(birth['id'], 'failed', {'kind': 'auth'})
                else:
                    payload = json.loads(birth['payload_json']); payload.pop('desired')
                    with ctx.store._conn() as c:
                        c.execute('UPDATE intents SET payload_json=? WHERE id=?', (json.dumps(payload), birth['id']))
                calls = len(server.calls)
                result = json.loads(tools.cal_update(ctx, id=eid, title='After', confirm=True))
                self.assertNotEqual(result['status'], 'updated')
                self.assertEqual(ctx.store.get_intent(birth['id'])['state'], 'failed')
                self.assertEqual(len(server.calls), calls)

    def test_google_revival_uses_pinned_if_match_and_no_body_identity(self):
        ctx, cid, server = google_context()
        adapter = ctx.providers.adapter_for('')
        request, methods = adapter._request, []
        def tombstone(method, path, params=None, body=None, headers=None, **kw):
            if method == 'POST' and not methods:
                server.events[body['id']] = {'id': body['id'], 'status': 'cancelled', 'etag': server._etag()}
            methods.append((method, dict(headers or {}), body))
            return request(method, path, params, body, headers, **kw)
        with patch.object(adapter, '_request', tombstone):
            result = json.loads(tools.cal_create(ctx, title='Revive', start=_day(3).isoformat(), calendars=[cid], confirm=True))
        self.assertEqual(result['status'], 'created')
        put = next(item for item in methods if item[0] == 'PUT')
        self.assertTrue(put[1].get('If-Match')); self.assertNotIn('id', put[2])
        birth = ctx.store.intents_for_event(result['event']['id'])[0]
        self.assertEqual(birth['expected_etag'], put[1]['If-Match'])
