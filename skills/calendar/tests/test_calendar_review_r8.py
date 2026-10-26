"""Remaining review counterexamples against d5c9376; no product writes/network."""
import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'skills/calendar'),str(ROOT/'skills/calendar/tests'),str(Path(__file__).resolve().parent)]
import test_calendar_review_r5 as r5
from test_calendar_review_r7 import caldav_row
from test_calendar_provider_divergence import _day
from test_calendar_atomic_replay import instance
from model import iso_utc
from scripts.worker import reconcile_calendar
import ops,tools

class ReviewR8Tests(unittest.TestCase):
    def test_accepted_cancel_survives_rule_growth_and_overlapping_source_exdate(self):
        for mode in ('rule-growth','overlap-removal'):
            with self.subTest(mode=mode):
                ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
                ext,_=instance(ctx,google,copy['id'],3)
                out=json.loads(tools.cal_delete(ctx,id=f"{copy['id']}@{iso_utc(_day(3))}",scope='this',confirm=True))
                self.assertEqual(out['status'],'deleted',out)
                self.assertEqual(google.events[ext]['status'],'cancelled')
                self.assertEqual(caldav_row(ctx,cid,caldav,eid)['exdates'],iso_utc(_day(3)))
                sequences=[(7,[5])] if mode=='rule-growth' else [(5,[3,5]),(5,[5])]
                for count,days in sequences:
                    remote=google.events[copy['external_id']]
                    remote['recurrence']=[f'RRULE:FREQ=DAILY;COUNT={count}']+['EXDATE:'+_day(d).strftime('%Y%m%dT%H%M%SZ') for d in days]
                    remote['etag']=google._etag()
                    reconcile_calendar(ctx.store,ctx.providers,ctx.providers.adapter_for('google:owner@example.test'),
                        ctx.store.get_calendar(copy['calendar_id']),ctx.tz)
                self.assertIn(iso_utc(_day(3)),caldav_row(ctx,cid,caldav,eid)['exdates'].split(','))
                self.assertEqual(google.events[ext]['status'],'cancelled')

    def test_cancellation_survives_caldav_refresh_and_offline_restart_before_rule_change(self):
        from test_calendar_atomic_replay import reopen
        from test_calendar_provider_divergence import run_open_intents
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        instance(ctx,google,copy['id'],3)
        tools.cal_delete(ctx,id=f"{copy['id']}@{iso_utc(_day(3))}",confirm=True)
        caldav.edit(ctx.store.get_event(eid)['href'],lambda c: next(x for x in c.walk() if x.name=='VEVENT').__setitem__('LOCATION','Other room'))
        reconcile_calendar(ctx.store,ctx.providers,left,ctx.store.get_calendar(cid),ctx.tz)
        self.assertEqual([r['status'] for r in ctx.store.exceptions_for(eid)],['cancelled'])
        remote=google.events[copy['external_id']]
        remote.update(recurrence=['RRULE:FREQ=DAILY;COUNT=7'],etag=google._etag())
        caldav.down=True
        reconcile_calendar(ctx.store,ctx.providers,ctx.providers.adapter_for('google:owner@example.test'),ctx.store.get_calendar(copy['calendar_id']),ctx.tz)
        caldav.down=False;reopen(ctx);run_open_intents(ctx,3)
        self.assertIn(iso_utc(_day(3)),caldav_row(ctx,cid,caldav,eid)['exdates'].split(','))

    def test_source_only_exclusion_still_clears_without_a_cancelled_override(self):
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        for add in (True,False):
            remote=google.events[copy['external_id']]
            remote.update(recurrence=['RRULE:FREQ=DAILY;COUNT=5']+(['EXDATE:'+_day(3).strftime('%Y%m%dT%H%M%SZ')] if add else []),etag=google._etag())
            reconcile_calendar(ctx.store,ctx.providers,ctx.providers.adapter_for('google:owner@example.test'),ctx.store.get_calendar(copy['calendar_id']),ctx.tz)
            self.assertEqual(caldav_row(ctx,cid,caldav,eid)['exdates'],iso_utc(_day(3)) if add else '')

    def test_source_cancellation_does_not_erase_pending_copy_edit(self):
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        google.down=True
        ops.update_event(ctx.store,ctx.providers,copy['id'],{'title':'Unsent owner'},scope='all',propagate=False)
        caldav.edit(ctx.store.get_event(eid)['href'],lambda c: next(x for x in c.walk() if x.name=='VEVENT').__setitem__('STATUS','CANCELLED'))
        reconcile_calendar(ctx.store,ctx.providers,left,ctx.store.get_calendar(cid),ctx.tz)
        row=ctx.store.get_event(copy['id'])
        self.assertEqual((row['title'],row['sync_state'],row['deleted_at']),('Unsent owner','pending',None))

    def test_linked_source_cancellation_retires_without_an_orphan_conflict(self):
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        caldav.edit(ctx.store.get_event(eid)['href'],lambda c: next(x for x in c.walk() if x.name=='VEVENT').__setitem__('STATUS','CANCELLED'))
        for _ in range(2):reconcile_calendar(ctx.store,ctx.providers,left,ctx.store.get_calendar(cid),ctx.tz)
        self.assertEqual(google.events[copy['external_id']]['status'],'cancelled')
        self.assertEqual(ctx.store.open_intents(),[])

    def test_formerly_linked_source_retires_even_when_it_is_readonly_now(self):
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        ops.reassign_event(ctx.store,ctx.providers,ctx.store.get_event(eid),[cid])
        self.assertEqual(len(ctx.store.group_masters(ctx.store.get_event(eid)['link_group_id'])),1)
        cal=ctx.store.get_calendar(cid);cal['writable']=False;ctx.store.upsert_calendar(cal)
        caldav.edit(ctx.store.get_event(eid)['href'],lambda c: next(x for x in c.walk() if x.name=='VEVENT').__setitem__('STATUS','CANCELLED'))
        caldav.calls.clear()
        reconcile_calendar(ctx.store,ctx.providers,left,ctx.store.get_calendar(cid),ctx.tz)
        self.assertTrue(ctx.store.get_event(eid)['deleted_at'])
        self.assertNotIn('DELETE',[c[0] for c in caldav.calls])

if __name__=='__main__':unittest.main()
