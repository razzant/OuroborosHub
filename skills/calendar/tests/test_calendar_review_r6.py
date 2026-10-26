"""Provider-representation and whole-feed durability regressions, no live I/O."""
import json
import sys
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'skills/calendar'), str(ROOT/'skills/calendar/tests')]
import test_calendar_review_r5 as r5
from test_calendar_provider_divergence import google_context, _day, run_open_intents, rows_for_calendar
from test_calendar_atomic_replay import instance, reopen
from providers import ics_to_rows
from providers_google import gevent_to_row
from model import DEFAULT_LOCAL_CALENDAR_ID, iso_utc
from scripts.worker import reconcile_calendar
import ops
import tools

class ReviewR6Tests(unittest.TestCase):
    def test_shift_with_replaced_and_cleared_absolute_dates(self):
        for clearing in (False, True):
            with self.subTest(clearing=clearing):
                ctx, cid, source, dest, adapter, eid, copy = r5.ReviewR5Tests().linked()
                # Establish old date sets at both ends through real conditional writes.
                for target in (eid, copy['id']):
                    result = ops.update_event(ctx.store, ctx.providers, target,
                        {'exdates':iso_utc(_day(3)), 'rdates':iso_utc(_day(9))}, scope='all', propagate=False)
                    self.assertTrue(all(a['status']=='done' for a in result['assignments']),result)
                wanted_ex = [] if clearing else [_day(4)+timedelta(hours=1),_day(5)+timedelta(hours=1)]
                wanted_rd = [] if clearing else [_day(10)+timedelta(hours=1)]
                def edit(calendar):
                    event=next(c for c in calendar.walk() if c.name=='VEVENT')
                    event['DTSTART'].dt += timedelta(hours=1); event['DTEND'].dt += timedelta(hours=1)
                    event.pop('EXDATE',None); event.pop('RDATE',None)
                    if wanted_ex: event.add('EXDATE',wanted_ex)
                    if wanted_rd: event.add('RDATE',wanted_rd)
                source.edit(ctx.store.get_event(eid)['href'],edit)
                dest.down=True
                reconcile_calendar(ctx.store,ctx.providers,adapter,ctx.store.get_calendar(cid),ctx.tz)
                dest.down=False; reopen(ctx); run_open_intents(ctx,3)
                remote=gevent_to_row(dest.events[copy['external_id']],copy['calendar_id'],ctx.tz)
                self.assertEqual(set(filter(None,remote['exdates'].split(','))),set(map(iso_utc,wanted_ex)))
                self.assertEqual(set(filter(None,remote['rdates'].split(','))),set(map(iso_utc,wanted_rd)))

    def test_soft_master_free_override_has_provider_equivalent_baseline(self):
        for competing in (False, True):
            with self.subTest(competing=competing):
                ctx,cid,server=google_context()
                created=json.loads(tools.cal_create(ctx,title='Routine',availability='soft',start=_day(2).isoformat(),
                    calendars=[DEFAULT_LOCAL_CALENDAR_ID],rrule='FREQ=DAILY;COUNT=5',confirm=True))
                eid=created['event']['id']
                tools.cal_update(ctx,id=f'{eid}@{iso_utc(_day(3))}',availability='free',confirm=True)
                ops.reassign_event(ctx.store,ctx.providers,ctx.store.get_event(eid),[DEFAULT_LOCAL_CALENDAR_ID,cid])
                copy=next(r for r in ctx.store.group_masters(ctx.store.get_event(eid)['link_group_id']) if r['calendar_id']==cid)
                rid,_=instance(ctx,server,copy['id'])
                if competing: server.events[rid].update(transparency='transparent',etag=server._etag())
                run_open_intents(ctx,2)
                exc=ctx.store.exceptions_for(copy['id'])[0]
                self.assertEqual(exc['sync_state'],'conflict' if competing else 'synced')
                if not competing: self.assertEqual(server.events[rid]['transparency'],'transparent')

    def test_google_exdate_removal_replaces_caldav_copy_exclusions(self):
        ctx,cid,caldav,google,left,eid,copy=r5.ReviewR5Tests().linked()
        right=ctx.providers.adapter_for('google:owner@example.test')
        remote=google.events[copy['external_id']]
        for dates in ([iso_utc(_day(3))], []):
            remote['recurrence']=['RRULE:FREQ=DAILY;COUNT=5']+['EXDATE:'+_day(3).strftime('%Y%m%dT%H%M%SZ')] if dates else ['RRULE:FREQ=DAILY;COUNT=5']
            remote['etag']=google._etag()
            reconcile_calendar(ctx.store,ctx.providers,right,ctx.store.get_calendar(copy['calendar_id']),ctx.tz)
            row=ctx.store.get_event(eid); text,etag=caldav.resources[row['href']]
            actual=next(r for r in ics_to_rows(text,cid,row['href'],etag,ctx.tz) if not r.get('recurrence_id'))
            self.assertEqual(set(filter(None,actual['exdates'].split(','))),set(dates))

    def test_late_absence_scan_failure_cannot_lose_committed_copy_change(self):
        ctx,cid,server,google,adapter,eid,copy=r5.ReviewR5Tests().linked()
        r5.ReviewR5Tests().change_source(ctx,server,eid)
        real=ctx.store.window; fired=[]
        def fail(*a,**kw):
            fired.append(True); raise RuntimeError('late absence scan')
        with patch.object(ctx.store,'window',fail):
            with self.assertRaises(RuntimeError):
                reconcile_calendar(ctx.store,ctx.providers,adapter,ctx.store.get_calendar(cid),ctx.tz)
        self.assertTrue(fired)
        reopen(ctx); run_open_intents(ctx,3)
        reconcile_calendar(ctx.store,ctx.providers,adapter,ctx.store.get_calendar(cid),ctx.tz)
        self.assertEqual(google.events[copy['external_id']]['summary'],'After')

    def test_failure_before_copy_intents_rolls_back_the_source_observation(self):
        import scripts.worker as worker
        ctx,cid,server,google,adapter,eid,copy=r5.ReviewR5Tests().linked()
        before=ctx.store.get_event(eid)
        r5.ReviewR5Tests().change_source(ctx,server,eid)
        original=worker._stage_sync_copy; fired=[]
        def fail(*args,**kwargs):
            result=original(*args,**kwargs)
            fired.append(True)
            raise RuntimeError('interrupted before feed commit')
        with patch.object(worker,'_stage_sync_copy',fail):
            with self.assertRaises(RuntimeError):
                reconcile_calendar(ctx.store,ctx.providers,adapter,ctx.store.get_calendar(cid),ctx.tz)
        self.assertEqual(fired,[True])
        self.assertEqual(ctx.store.get_event(eid)['etag'],before['etag'])
        self.assertEqual(ctx.store.get_event(copy['id'])['title'],'Before')
        reopen(ctx)
        reconcile_calendar(ctx.store,ctx.providers,adapter,ctx.store.get_calendar(cid),ctx.tz)
        self.assertEqual(google.events[copy['external_id']]['summary'],'After')

    def test_whole_feed_excludes_concurrent_owner_writer_and_commits_before_network(self):
        import sqlite3, threading
        from store import Store
        from test_calendar_round2 import OneAdapterProviders
        ctx,cid,server,google,adapter,eid,copy=r5.ReviewR5Tests().linked()
        r5.ReviewR5Tests().change_source(ctx,server,eid)
        original=Store.apply_provider_snapshot
        observer=Store(ctx.store.state_dir)
        fired,started,done=threading.Event(),threading.Event(),threading.Event()
        failures=[];threads=[];network=[]
        def owner():
            started.set()
            try: ops.update_event(Store(ctx.store.state_dir),OneAdapterProviders(None),eid,{'title':'Owner later'},scope='all',propagate=False)
            except BaseException as e: failures.append(repr(e))
            finally: done.set()
        def guard(view,event_id,fields,observed=None):
            result=original(view,event_id,fields,observed)
            if event_id==eid and not fired.is_set():
                fired.set(); t=threading.Thread(target=owner);threads.append(t);t.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(done.wait(.1))
                self.assertEqual(observer.get_event(eid)['title'],'Before')
            return result
        request=google.request
        def send(*a,**kw):
            self.assertTrue(done.wait(3))
            with sqlite3.connect(ctx.store.path,timeout=2) as conn: conn.execute('BEGIN IMMEDIATE')
            network.append(a[0]);return request(*a,**kw)
        ctx.providers.adapter_for('google:owner@example.test')._request=send
        try:
            with patch.object(Store,'apply_provider_snapshot',guard):
                reconcile_calendar(ctx.store,ctx.providers,adapter,ctx.store.get_calendar(cid),ctx.tz)
        finally:
            for t in threads:t.join(5)
        self.assertTrue(fired.is_set());self.assertTrue(done.is_set());self.assertEqual(failures,[])
        self.assertTrue(network)
        self.assertEqual(ctx.store.get_event(eid)['title'],'Owner later')

if __name__=='__main__': unittest.main()
