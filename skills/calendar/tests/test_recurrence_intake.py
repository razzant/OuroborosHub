"""Invalid user rules cannot poison unrelated reminders; imported rules stay visible."""
import asyncio
import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from test_calendar_round2 import make_context, RouteAPI, Request
import ops
import routes
import tools
import reminders as rem
from model import iso_utc, get_tz, DEFAULT_LOCAL_CALENDAR_ID

BAD = 'FREQ=WEEKLY;BYDAY=MON,WED'


class RecurrenceIntakeTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.addCleanup(shutil.rmtree, self.ctx.state_dir)
        self.ctx.tz = get_tz('UTC')
        self.start = self.ctx.now().replace(microsecond=0) + timedelta(hours=2)
        self.api = RouteAPI()
        routes.register_routes(self.api, lambda: self.ctx)

    def create(self, **kwargs):
        return json.loads(tools.cal_create(self.ctx, title='Series', start=self.start.isoformat(), confirm=True, **kwargs))

    def test_user_rules_refused_on_both_write_surfaces_before_effect(self):
        self.assertEqual(self.create(rrule=BAD)['status'], 'error')
        self.assertEqual(self.ctx.store.counts()['events'], 0)
        r = asyncio.run(self.api.routes['event'](Request({'title':'Invalid', 'start':self.start.isoformat(), 'rrule':BAD})))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.ctx.store.counts()['events'], 0)
        good = self.create(rrule='RRULE:FREQ=DAILY;COUNT=3')['event']['id']
        previous = self.ctx.store.get_event(good)
        out = json.loads(tools.cal_update(self.ctx, id=good, rrule=BAD, scope='all', confirm=True))
        self.assertEqual(out['status'], 'error')
        r = asyncio.run(self.api.routes['event/update'](Request({'id':good, 'rrule':BAD, 'scope':'all'})))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.ctx.store.get_event(good)['rrule'], previous['rrule'])

    def test_floating_until_uses_start_zone_across_dst(self):
        start = datetime(2026, 10, 19, 7, tzinfo=timezone.utc)
        row = {'id':'series', 'start_utc':iso_utc(start), 'end_utc':iso_utc(start + timedelta(hours=1)),
               'tz':'Europe/Berlin', 'origin':'external'}
        for until, expected in (('20261102T090000',3), ('20261102T080000',2), ('20261102',3)):
            with self.subTest(until=until):
                row['rrule'] = 'FREQ=WEEKLY;UNTIL=' + until
                self.assertEqual(len(ops.expand([row], start, start+timedelta(days=22), lambda _:[], strict=True)), expected)

    def test_timed_to_all_day_from_tool_and_route_has_exclusive_next_day_end(self):
        self.ctx.tz = get_tz('Europe/Berlin')
        for surface in ('tool', 'route', 'flag-only'):
            with self.subTest(surface=surface):
                created = json.loads(tools.cal_create(self.ctx, title='Whole day', start='2026-10-20T09:00', confirm=True))
                eid = created['event']['id']
                if surface == 'tool':
                    out = json.loads(tools.cal_update(self.ctx, id=eid, start='2026-10-20', all_day=True, confirm=True))
                    self.assertEqual(out['status'],'updated')
                elif surface == 'flag-only':
                    out = json.loads(tools.cal_update(self.ctx, id=eid, all_day=True, confirm=True))
                    self.assertEqual(out['status'],'updated')
                else:
                    out = asyncio.run(self.api.routes['event/update'](Request({'id':eid, 'all_day':True, 'start':'2026-10-20', 'end':'2026-10-20'})))
                    self.assertEqual(out.status_code,200)
                from model import parse_stored
                row = self.ctx.store.get_event(eid)
                self.assertTrue(row['all_day'])
                self.assertEqual(parse_stored(row['start_utc']).astimezone(self.ctx.tz).isoformat(),'2026-10-20T00:00:00+02:00')
                self.assertEqual(parse_stored(row['end_utc']).astimezone(self.ctx.tz).isoformat(),'2026-10-21T00:00:00+02:00')

    def test_declined_event_does_not_get_or_send_reminder(self):
        from test_calendar_round2 import Channel
        good = self.create(reminders=[15])['event']['id']
        self.ctx.store.update_event(good, {'my_response':'declined'})
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, self.ctx.tz, now=self.start-timedelta(minutes=14))
        self.assertFalse(channel.sent)
        tools._plan_reminders(self.ctx)
        with self.ctx.store._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM reminders WHERE event_id=? AND state='scheduled'",(good,)).fetchone()[0],0)

    def test_series_to_single_preserves_sent_notice(self):
        from test_calendar_round2 import Channel
        eid = self.create(rrule='FREQ=DAILY', reminders=[15])['event']['id']
        channel = Channel()
        rem.deliver_due(self.ctx.store, channel, self.ctx.tz, now=self.start-timedelta(minutes=15))
        self.assertEqual(len(channel.sent),1)
        out = json.loads(tools.cal_update(self.ctx,id=eid,rrule='',scope='all',confirm=True))
        self.assertEqual(out['status'],'updated')
        rem.deliver_due(self.ctx.store, channel, self.ctx.tz, now=self.start-timedelta(minutes=10))
        self.assertEqual(len(channel.sent),1)

    def test_malformed_import_keeps_its_queue_but_does_not_block_good_events(self):
        bad = self.create()['event']['id']
        self.ctx.store.update_event(bad, {'rrule':BAD})  # externally imported invalid data, not a user write
        key = iso_utc(self.start)
        notice = rem.notice_id_for(bad, key, 15, key)
        self.ctx.store.schedule_reminder(bad, key, 15, iso_utc(self.start-timedelta(minutes=15)), notice, key)
        good = self.create(reminders=[15])['event']['id']
        warning = tools._plan_reminders(self.ctx)
        self.assertIn(bad, warning)
        with self.ctx.store._conn() as c:
            waiting = [dict(r) for r in c.execute("SELECT * FROM reminders WHERE state='scheduled'")]
        self.assertEqual({r['event_id'] for r in waiting}, {bad, good})
        self.assertEqual(sum(r['notice_id']==notice for r in waiting),1)
