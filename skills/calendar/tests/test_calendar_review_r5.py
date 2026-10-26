"""Independent public-consumer witnesses for the final review's R1/R2.

No real provider requests. The actual adapters run against existing conditional
in-memory transports. The Google fixture does not generate recurrence instances,
so the test explicitly installs the ordinary remote instance before replay.
"""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'skills/calendar'), str(ROOT/'skills/calendar/tests')]
from test_calendar_provider_divergence import (caldav_context, google_context, add_google_calendar,
    FakeGoogle, _day, run_open_intents)
from test_calendar_round2 import OneAdapterProviders
from test_calendar_atomic_replay import instance
from providers_google import GoogleAdapter
from model import DEFAULT_LOCAL_CALENDAR_ID, iso_utc, now_utc, parse_stored
from scripts.worker import reconcile_calendar
import ops
import tools


class ReviewR5Tests(unittest.TestCase):
    def test_copied_moved_exception_reaches_google_with_its_actual_override(self):
        ctx, cid, server = google_context()
        out = json.loads(tools.cal_create(ctx, title='Master', start=_day(2).isoformat(),
            rrule='FREQ=DAILY;COUNT=5', calendars=[DEFAULT_LOCAL_CALENDAR_ID], confirm=True))
        eid = out['event']['id']
        moved = json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(3))}',
            title='Moved', start=(_day(3)+timedelta(hours=2)).isoformat(), confirm=True))
        self.assertEqual(moved['status'], 'updated')
        ops.reassign_event(ctx.store, ctx.providers, ctx.store.get_event(eid), [DEFAULT_LOCAL_CALENDAR_ID, cid])
        copy = next(r for r in ctx.store.group_masters(ctx.store.get_event(eid)['link_group_id']) if r['calendar_id'] == cid)
        remote_id, _ = instance(ctx, server, copy['id'])
        run_open_intents(ctx, rounds=2)
        exc = ctx.store.exceptions_for(copy['id'])[0]
        self.assertEqual(exc['sync_state'], 'synced', ctx.store.intents_for_event(exc['id']))
        self.assertEqual(server.events[remote_id]['summary'], 'Moved')
        self.assertEqual(parse_stored(server.events[remote_id]['start']['dateTime']), _day(3)+timedelta(hours=2))

    def linked(self):
        ctx, left, server = caldav_context()
        right = add_google_calendar(ctx.store)
        google = FakeGoogle()
        adapter = google.attach(GoogleAdapter('owner@example.test', 'token', 'refresh',
            now_utc()+timedelta(days=1), 'client', '', None))
        caldav = ctx.providers.adapter_for('')
        class Routing:
            def adapter_for(self, account):
                return adapter if account.startswith('google:') else caldav
        ctx.providers = Routing()
        out = json.loads(tools.cal_create(ctx, title='Before', start=_day(2).isoformat(),
            rrule='FREQ=DAILY;COUNT=5', calendars=[left, right], confirm=True))
        self.assertEqual(out['status'], 'created', out)
        eid = out['event']['id']
        copy = next(r for r in ctx.store.group_masters(ctx.store.get_event(eid)['link_group_id']) if r['calendar_id'] == right)
        return ctx, left, server, google, caldav, eid, copy

    def change_source(self, ctx, server, eid, cancelled=False):
        def edit(calendar):
            event = next(c for c in calendar.walk() if c.name == 'VEVENT' and not c.get('RECURRENCE-ID'))
            event['SUMMARY'] = 'After'
            if not cancelled:
                event['DTSTART'].dt += timedelta(hours=1)
                event['DTEND'].dt += timedelta(hours=1)
            if cancelled:
                event.add('exdate', [_day(3), _day(4)])
        server.edit(ctx.store.get_event(eid)['href'], edit)

    def test_one_feed_time_and_title_change_survive_offline_copy(self):
        ctx, cid, server, google, adapter, eid, copy = self.linked()
        self.change_source(ctx, server, eid)
        google.down = True
        reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
        google.down = False
        run_open_intents(ctx, rounds=2)
        remote = google.events[copy['external_id']]
        self.assertEqual(remote['summary'], 'After')
        self.assertEqual(parse_stored(remote['start']['dateTime']), _day(2)+timedelta(hours=1))

    def test_one_feed_all_exdates_and_title_reach_an_offline_copy(self):
        ctx, cid, server, google, adapter, eid, copy = self.linked()
        self.change_source(ctx, server, eid, cancelled=True)
        # Seed provider instances the real Google listing normally supplies.
        ids = [instance(ctx, google, copy['id'], day)[0] for day in (3, 4)]
        google.down = True
        reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
        google.down = False
        run_open_intents(ctx, rounds=3)
        self.assertEqual(google.events[copy['external_id']]['summary'], 'After')
        from providers_google import gevent_to_row
        master = gevent_to_row(google.events[copy['external_id']], copy['calendar_id'], ctx.tz)
        master['id'] = copy['id']
        visible = ops.expand([master], _day(2), _day(6), lambda _: [], ctx.tz)
        self.assertNotIn(iso_utc(_day(3)), [r['start_utc'] for r in visible])
        self.assertNotIn(iso_utc(_day(4)), [r['start_utc'] for r in visible])

    def test_title_and_two_overrides_from_one_feed_stage_before_offline_send(self):
        import copy as copying
        ctx, cid, server, google, adapter, eid, dest = self.linked()
        ids = [instance(ctx, google, dest['id'], day)[0] for day in (3, 4)]
        def edit(calendar):
            master = next(c for c in calendar.walk() if c.name == 'VEVENT')
            master['SUMMARY'] = 'After'
            for day in (3, 4):
                exc = copying.deepcopy(master)
                exc.pop('RRULE', None)
                exc.add('recurrence-id', _day(day))
                exc['DTSTART'].dt = _day(day)+timedelta(hours=2)
                exc['DTEND'].dt = _day(day)+timedelta(hours=3)
                calendar.add_component(exc)
        server.edit(ctx.store.get_event(eid)['href'], edit)
        google.down = True
        reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
        google.down = False
        run_open_intents(ctx, rounds=3)
        self.assertEqual(google.events[dest['external_id']]['summary'], 'After')
        for day, rid in zip((3, 4), ids):
            self.assertEqual(parse_stored(google.events[rid]['start']['dateTime']), _day(day)+timedelta(hours=2))

    def test_latest_command_uses_insertion_order_when_wall_clock_moves_back(self):
        ctx, cid, server = google_context()
        out = json.loads(tools.cal_create(ctx, title='Clock', start=_day(2).isoformat(), calendars=[cid], confirm=True))
        eid = out['event']['id']
        old = ctx.store.add_intent('delete', 'google:owner@example.test', cid, eid, {})
        with ctx.store._conn() as conn:
            conn.execute('UPDATE intents SET created_at=? WHERE id=?', ('2999-01-01', old['id']))
        ctx.store.add_intent('update', 'google:owner@example.test', cid, eid, {})
        self.assertFalse(ctx.store.delete_requested(eid))
        ctx.store.update_event(eid, {'sync_state': 'synced'})
        self.assertEqual(ctx.store.get_event(eid)['sync_state'], 'synced')

if __name__ == '__main__':
    unittest.main()
