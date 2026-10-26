"""Absolute provider dates, local retirement, and independent cancellations.

Real commands and adapters against conditional fake providers; no account/network.
"""
import json
import sys
import unittest
from pathlib import Path
from datetime import timedelta
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'skills/calendar'), str(ROOT/'skills/calendar/tests')]
import test_calendar_review_r5 as r5
from test_calendar_provider_divergence import caldav_context, google_context, _day, run_open_intents
from test_calendar_atomic_replay import instance, reopen
from test_conflict_resolution import create
from providers import ics_to_rows
from providers_google import gevent_to_row
from model import iso_utc
from scripts.worker import reconcile_calendar
import ops
import tools


def caldav_row(ctx, cid, server, eid):
    row = ctx.store.get_event(eid)
    text, etag = server.resources[row['href']]
    return next(r for r in ics_to_rows(text,cid,row['href'],etag,ctx.tz) if not r.get('recurrence_id'))


class ReviewR7Tests(unittest.TestCase):
    def test_provider_shift_carries_unchanged_absolute_date_sets_both_directions(self):
        for direction in ('caldav-google', 'google-caldav'):
            with self.subTest(direction=direction):
                ctx,cid,caldav,google,left,eid,copy = r5.ReviewR5Tests().linked()
                for target in (eid, copy['id']):
                    result=ops.update_event(ctx.store,ctx.providers,target,
                        {'exdates':iso_utc(_day(3)), 'rdates':iso_utc(_day(9))},scope='all',propagate=False)
                    self.assertTrue(all(a['status']=='done' for a in result['assignments']),result)
                if direction == 'caldav-google':
                    def edit(cal):
                        event=next(c for c in cal.walk() if c.name=='VEVENT')
                        event['DTSTART'].dt += timedelta(hours=1); event['DTEND'].dt += timedelta(hours=1)
                    caldav.edit(ctx.store.get_event(eid)['href'],edit)
                    google.down=True
                    reconcile_calendar(ctx.store,ctx.providers,left,ctx.store.get_calendar(cid),ctx.tz)
                    google.down=False;reopen(ctx);run_open_intents(ctx,3)
                    remote=gevent_to_row(google.events[copy['external_id']],copy['calendar_id'],ctx.tz)
                else:
                    remote=google.events[copy['external_id']]
                    remote.update(start={'dateTime':(_day(2)+timedelta(hours=1)).isoformat()},
                                  end={'dateTime':(_day(2)+timedelta(hours=2)).isoformat()},etag=google._etag())
                    caldav.down=True
                    reconcile_calendar(ctx.store,ctx.providers,ctx.providers.adapter_for('google:owner@example.test'),
                                       ctx.store.get_calendar(copy['calendar_id']),ctx.tz)
                    caldav.down=False;reopen(ctx);run_open_intents(ctx,3)
                    remote=caldav_row(ctx,cid,caldav,eid)
                self.assertEqual(remote['start_utc'],iso_utc(_day(2)+timedelta(hours=1)))
                self.assertEqual(remote['exdates'],iso_utc(_day(3)))
                self.assertEqual(remote['rdates'],iso_utc(_day(9)))

    def test_unlinked_caldav_cancellation_is_local_retirement_even_readonly(self):
        for writable in (True, False):
            with self.subTest(writable=writable):
                ctx,cid,server=caldav_context();eid=create(ctx,cid)
                before=ctx.store.get_event(eid)
                cal=ctx.store.get_calendar(cid);cal['writable']=writable;ctx.store.upsert_calendar(cal)
                server.edit(before['href'],lambda c: next(x for x in c.walk() if x.name=='VEVENT').__setitem__('STATUS','CANCELLED'))
                server.calls.clear()
                reconcile_calendar(ctx.store,ctx.providers,ctx.providers.adapter_for(''),ctx.store.get_calendar(cid),ctx.tz)
                self.assertTrue(ctx.store.get_event(eid)['deleted_at'])
                self.assertNotIn('DELETE',[c[0] for c in server.calls])
                self.assertEqual(ctx.store.open_intents(),[])
                self.assertIn(before['href'],server.resources)

    def test_linked_caldav_cancellation_still_cascades_after_commit(self):
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        caldav.edit(ctx.store.get_event(eid)['href'],lambda c: next(x for x in c.walk() if x.name=='VEVENT').__setitem__('STATUS','CANCELLED'))
        reconcile_calendar(ctx.store,ctx.providers,left,ctx.store.get_calendar(cid),ctx.tz)
        self.assertEqual(google.events[copy['external_id']]['status'],'cancelled')

    def test_source_exdate_add_and_remove_preserve_a_separate_accepted_cancellation(self):
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        instance(ctx,google,copy['id'],3)
        deleted=json.loads(tools.cal_delete(ctx,id=f"{copy['id']}@{iso_utc(_day(3))}",scope='this',confirm=True))
        self.assertEqual(deleted['status'],'deleted',deleted)
        right=ctx.providers.adapter_for('google:owner@example.test')
        self.assertEqual(caldav_row(ctx,cid,caldav,eid)['exdates'],iso_utc(_day(3)))
        for add in (True,False):
            with self.subTest(add=add):
                remote=google.events[copy['external_id']]
                remote['recurrence']=['RRULE:FREQ=DAILY;COUNT=5']+(['EXDATE:'+_day(5).strftime('%Y%m%dT%H%M%SZ')] if add else [])
                remote['etag']=google._etag()
                reconcile_calendar(ctx.store,ctx.providers,right,ctx.store.get_calendar(copy['calendar_id']),ctx.tz)
                actual=set(filter(None,caldav_row(ctx,cid,caldav,eid)['exdates'].split(',')))
                self.assertEqual(actual,{iso_utc(_day(3)),iso_utc(_day(5))} if add else {iso_utc(_day(3))})

    def test_mixed_excluded_and_live_provider_overrides_keep_surviving_import(self):
        ctx,cid,google=google_context();eid=create(ctx,cid,rrule='FREQ=DAILY;COUNT=5')
        for day in (3,4):
            ext,_=instance(ctx,google,eid,day)
            google.events[ext].update(summary=f'Override {day}',etag=google._etag())
        master=ctx.store.get_event(eid)
        google.events[master['external_id']].update(recurrence=['RRULE:FREQ=DAILY;COUNT=5',
            'EXDATE:'+_day(3).strftime('%Y%m%dT%H%M%SZ')],etag=google._etag())
        reconcile_calendar(ctx.store,ctx.providers,ctx.providers.adapter_for(''),ctx.store.get_calendar(cid),ctx.tz)
        rows=ctx.store.exceptions_for(eid)
        self.assertEqual([(r['recurrence_id'],r['title']) for r in rows],[(iso_utc(_day(4)),'Override 4')])
        reconcile_calendar(ctx.store,ctx.providers,ctx.providers.adapter_for(''),ctx.store.get_calendar(cid),ctx.tz)
        self.assertEqual(len(ctx.store.exceptions_for(eid)),1)


if __name__=='__main__': unittest.main()
