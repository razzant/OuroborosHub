"""Conflicts through real tools/adapters and isolated fake provider stores."""
import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import patch
from test_calendar_provider_divergence import (caldav_context, google_context, _day, run_open_intents, sync, RouteAPI, Request)
import conflicts
import ops
import routes
import tools
from model import iso_utc, now_utc


def create(ctx, cid, **extra):
    return json.loads(tools.cal_create(ctx, title='Original', start=_day(2).isoformat(), calendars=[cid], confirm=True, **extra))['event']['id']


def remote_edit(ctx, server, eid, title='Remote'):
    row = ctx.store.get_event(eid)
    if hasattr(server, 'events'):
        server.events[row['external_id']].update(summary=title, etag=server._etag())
    else:
        def mutate(cal):
            next(c for c in cal.walk('VEVENT') if not c.get('RECURRENCE-ID'))['SUMMARY'] = title
        server.edit(row.get('href') or ctx.store.get_event(row['master_id'])['href'], mutate)


def inspection(ctx, eid):
    return json.loads(tools.cal_events(ctx, id=eid))['event']['conflict']


def resolve(ctx, eid, info, choice, **kw):
    return json.loads(tools.cal_update(ctx, id=eid, resolution=choice, resolution_token=info['resolution_token'], confirm=True, **kw))


class ConflictTests(unittest.TestCase):
    def test_inspect_then_keep_local_and_keep_provider_both_adapters(self):
        for factory in (google_context, caldav_context):
            for choice in ('local','provider'):
                with self.subTest(factory=factory.__name__,choice=choice):
                    ctx,cid,server=factory(); eid=create(ctx,cid)
                    remote_edit(ctx,server,eid)
                    self.assertEqual(json.loads(tools.cal_update(ctx,id=eid,title='Mine',confirm=True))['status'],'conflict')
                    before=ctx.store.get_event(eid)
                    for _ in range(3): sync(ctx,cid)
                    info=inspection(ctx,eid)
                    self.assertEqual(info['local']['title'],'Mine'); self.assertEqual(info['provider']['title'],'Remote')
                    self.assertEqual(ctx.store.get_event(eid),before)
                    self.assertEqual(info,inspection(ctx,eid))
                    self.assertFalse(json.loads(tools.cal_free(ctx,start=_day(2).isoformat(),end=_day(3).isoformat(),sources=cid))['availability_complete'])
                    calls=len(server.calls)
                    out=resolve(ctx,eid,info,choice)
                    self.assertEqual(out['status'], 'pending' if choice=='local' else 'resolved')
                    self.assertTrue(all(c[0] in ('GET',) for c in server.calls[calls:]))
                    self.assertTrue(any(i['state']=='resolved' and 'previous' in json.loads(i['result_json']) for i in ctx.store.intents_for_event(eid)))
                    if choice=='local': run_open_intents(ctx)
                    sync(ctx,cid)
                    self.assertEqual(ctx.store.get_event(eid)['title'], 'Mine' if choice=='local' else 'Remote')
                    self.assertEqual(ctx.store.get_event(eid)['sync_state'],'synced')
                    self.assertTrue(json.loads(tools.cal_free(ctx,start=_day(2).isoformat(),end=_day(3).isoformat(),sources=cid))['availability_complete'])

    def test_changed_remote_or_local_snapshot_never_applies_old_choice(self):
        for factory in (google_context,caldav_context):
            with self.subTest(factory=factory.__name__):
                ctx,cid,server=factory(); eid=create(ctx,cid); remote_edit(ctx,server,eid)
                tools.cal_update(ctx,id=eid,title='Mine',confirm=True)
                info=inspection(ctx,eid); before=ctx.store.get_event(eid)
                remote_edit(ctx,server,eid,'New remote')
                self.assertTrue(resolve(ctx,eid,info,'provider')['snapshot_changed'])
                self.assertEqual(ctx.store.get_event(eid),before)
                info=inspection(ctx,eid)
                adapter=ctx.providers.adapter_for(''); original=adapter.read_current
                def race(*a,**kw):
                    current=original(*a,**kw)
                    ctx.store.update_event(eid,{'location':'Concurrent local'})
                    return current
                with patch.object(adapter,'read_current',race):
                    self.assertTrue(resolve(ctx,eid,info,'provider')['snapshot_changed'])
                self.assertEqual(ctx.store.get_event(eid)['location'],'Concurrent local')
                self.assertEqual(ctx.store.get_event(eid)['title'],'Mine')

    def test_second_412_and_offline_inspection_keep_desired_state(self):
        for factory in (google_context,caldav_context):
            with self.subTest(factory=factory.__name__):
                ctx,cid,server=factory(); eid=create(ctx,cid); remote_edit(ctx,server,eid)
                tools.cal_update(ctx,id=eid,title='Mine',confirm=True)
                server.down=True
                self.assertNotIn('resolution_token',inspection(ctx,eid))
                server.down=False
                info=inspection(ctx,eid); self.assertEqual(resolve(ctx,eid,info,'local')['status'],'pending')
                remote_edit(ctx,server,eid,'Raced again'); run_open_intents(ctx)
                self.assertEqual(ctx.store.get_event(eid)['sync_state'],'conflict')
                self.assertEqual(ctx.store.get_event(eid)['title'],'Mine')

    def test_delete_conflict_provider_choice_retires_delete_intent(self):
        for factory in (google_context,caldav_context):
            with self.subTest(factory=factory.__name__):
                ctx,cid,server=factory(); eid=create(ctx,cid); remote_edit(ctx,server,eid)
                self.assertEqual(json.loads(tools.cal_delete(ctx,id=eid,confirm=True))['status'],'conflict')
                info=inspection(ctx,eid)
                self.assertEqual(resolve(ctx,eid,info,'provider')['status'],'resolved')
                self.assertFalse(ctx.store.delete_requested(eid)); sync(ctx,cid)
                self.assertIsNone(ctx.store.get_event(eid)['deleted_at'])
                self.assertEqual(ctx.store.get_event(eid)['sync_state'],'synced')

    def test_ordinary_tools_and_widget_refuse_before_altering_conflict(self):
        ctx,cid,server=google_context(); eid=create(ctx,cid); remote_edit(ctx,server,eid)
        tools.cal_update(ctx,id=eid,title='Mine',confirm=True)
        before=ctx.store.get_event(eid); intents=ctx.store.intents_for_event(eid)
        self.assertEqual(json.loads(tools.cal_update(ctx,id=eid,title='Second',confirm=True))['status'],'conflict')
        self.assertEqual(json.loads(tools.cal_delete(ctx,id=eid,confirm=True))['status'],'conflict')
        api=RouteAPI(); routes.register_routes(api,lambda:ctx)
        out=asyncio.run(api.routes['event/update'](Request({'id':eid,'title':'Third'})))
        self.assertEqual(json.loads(out.body)['status'],'conflict')
        self.assertEqual(ctx.store.get_event(eid),before)
        self.assertEqual(ctx.store.intents_for_event(eid),intents)
        info=inspection(ctx,eid)
        self.assertEqual(resolve(ctx,eid,info,'provider',title='Must not disappear')['status'],'error')

    def test_active_lease_and_large_comparison_withhold_resolution(self):
        ctx,cid,server=google_context(); eid=create(ctx,cid); remote_edit(ctx,server,eid)
        tools.cal_update(ctx,id=eid,title='Mine',confirm=True)
        with ctx.store._conn() as c:
            c.execute("UPDATE intents SET lease_until=? WHERE event_id=? AND state='conflict'",(iso_utc(now_utc()+timedelta(minutes=5)),eid))
        info=inspection(ctx,eid)
        self.assertEqual(resolve(ctx,eid,info,'provider')['status'],'busy')
        ctx.store.update_event(eid,{'description':'x'*16000})
        info=inspection(ctx,eid)
        self.assertTrue(info['comparison_incomplete']); self.assertNotIn('resolution_token',info)

    def test_provider_absence_preserves_local_without_recreate(self):
        ctx,cid,server=google_context(); eid=create(ctx,cid); remote_edit(ctx,server,eid)
        tools.cal_update(ctx,id=eid,title='Mine',confirm=True)
        server.events.clear(); before=ctx.store.get_event(eid)
        self.assertNotIn('resolution_token',inspection(ctx,eid))
        self.assertEqual(ctx.store.get_event(eid),before)

    def test_caldav_exception_resolves_with_resource_etag_without_losing_siblings(self):
        ctx,cid,server=caldav_context(); eid=create(ctx,cid,rrule='FREQ=DAILY')
        slot=_day(3); other=_day(4)
        for at,title in ((slot,'First exception'),(other,'Other exception')):
            self.assertEqual(json.loads(tools.cal_update(ctx,id=f'{eid}@{iso_utc(at)}',title=title,confirm=True))['status'],'updated')
        remote_edit(ctx,server,eid)
        self.assertEqual(json.loads(tools.cal_update(ctx,id=f'{eid}@{iso_utc(slot)}',title='Mine exception',confirm=True))['status'],'conflict')
        exc=next(e for e in ctx.store.exceptions_for(eid) if e['recurrence_id']==iso_utc(slot))
        sibling=ctx.store.get_event(next(e for e in ctx.store.exceptions_for(eid) if e['recurrence_id']==iso_utc(other))['id'])
        info=inspection(ctx,exc['id'])
        self.assertEqual(info['provider']['title'],'First exception')
        self.assertEqual(resolve(ctx,exc['id'],info,'local')['status'],'pending')
        run_open_intents(ctx)
        text,etag=server.resources[ctx.store.get_event(eid)['href']]
        self.assertIn('SUMMARY:Mine exception',text)
        self.assertIn('SUMMARY:Other exception',text)
        self.assertIn('SUMMARY:Remote',text)
        self.assertEqual(ctx.store.get_event(sibling['id']),sibling)

    def test_resolution_does_not_touch_linked_copy(self):
        ctx,cid,server=google_context()
        out=json.loads(tools.cal_create(ctx,title='Original',start=_day(2).isoformat(),calendars=[cid,'local:personal'],confirm=True))
        eid=out['event']['id']; copies=ctx.store.group_masters(ctx.store.get_event(eid)['link_group_id'])
        sibling=next(e for e in copies if e['id']!=eid); before=ctx.store.get_event(sibling['id'])
        remote_edit(ctx,server,eid)
        tools.cal_update(ctx,id=eid,title='Mine',confirm=True)
        before=ctx.store.get_event(sibling['id'])  # ordinary linked edit was explicit; resolving one copy is not another group edit
        info=inspection(ctx,eid); self.assertEqual(resolve(ctx,eid,info,'provider')['status'],'resolved')
        self.assertEqual(ctx.store.get_event(sibling['id']),before)

    def test_update_and_rsvp_resolution_tail_uses_new_etag_and_keeps_guest_metadata(self):
        ctx,cid,server=google_context(); eid=create(ctx,cid,attendees=['owner@example.test','guest@example.test'])
        ext=ctx.store.get_event(eid)['external_id']
        server.events[ext]['attendees']=[{'email':'owner@example.test','self':True,'responseStatus':'needsAction'},
            {'email':'guest@example.test','optional':True,'comment':'Keep me','additionalGuests':2,'displayName':'Guest'}]
        server.events[ext]['etag']=server._etag(); sync(ctx,cid)
        remote_edit(ctx,server,eid)
        out=json.loads(tools.cal_update(ctx,id=eid,title='Mine',response='accepted',send_updates=True,confirm=True))
        self.assertEqual(out['status'],'conflict')
        info=inspection(ctx,eid); self.assertEqual(resolve(ctx,eid,info,'local')['status'],'pending')
        run_open_intents(ctx,rounds=2)
        self.assertEqual(ctx.store.get_event(eid)['sync_state'],'synced')
        guest=next(a for a in server.events[ext]['attendees'] if a['email']=='guest@example.test')
        self.assertEqual((guest['optional'],guest['comment'],guest['additionalGuests'],guest['displayName']), (True,'Keep me',2,'Guest'))
        me=next(a for a in server.events[ext]['attendees'] if a['email']=='owner@example.test')
        self.assertEqual(me['responseStatus'],'accepted')
