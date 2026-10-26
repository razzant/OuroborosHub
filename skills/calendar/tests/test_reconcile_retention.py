"""A provider resync preserves identities and unconfirmed owner edits."""
import json
import shutil
import unittest
from datetime import timedelta
from test_calendar_provider_divergence import google_context, sync, _day
import tools
from scripts.worker import wipe_external_cache


class ReconcileRetentionTests(unittest.TestCase):
    def setUp(self):
        self.ctx, self.cid, self.server = google_context()
        self.addCleanup(shutil.rmtree, self.ctx.state_dir)
        out = json.loads(tools.cal_create(self.ctx, title='Original', start=_day(2).isoformat(), calendars=[self.cid], confirm=True))
        self.eid = out['event']['id']
        self.ctx.store.update_event(self.eid, {'origin':'external'})

    def test_cursor_reset_keeps_pending_identity_and_intent(self):
        self.server.down = True
        tools.cal_update(self.ctx, id=self.eid, title='Keep mine', confirm=True)
        before = self.ctx.store.get_event(self.eid)
        intents = self.ctx.store.intents_for_event(self.eid)
        wipe_external_cache(self.ctx.store,self.cid)
        self.assertEqual(self.ctx.store.get_event(self.eid),before)
        self.assertEqual(self.ctx.store.intents_for_event(self.eid),intents)
        self.server.down = False
        sync(self.ctx,self.cid)
        self.assertEqual(self.ctx.store.get_event(self.eid)['title'],'Keep mine')

    def test_failed_edit_cannot_be_overwritten_by_sync_or_old_ack_display(self):
        self.ctx.store.update_event(self.eid, {'title':'Unsent', 'sync_state':'failed'})
        remote = self.server.live()[0]
        remote['etag'] = self.server._etag()
        sync(self.ctx,self.cid)
        self.assertEqual(self.ctx.store.get_event(self.eid)['title'],'Unsent')
        self.ctx.store.update_event(self.eid, {'sync_state':'synced'})
        self.ctx.store.add_intent('update','google:owner@example.test',self.cid,self.eid,{'changes':{'title':'Unsent'}})
        sync(self.ctx,self.cid)
        self.assertEqual(self.ctx.store.get_event(self.eid)['title'],'Unsent')

    def test_full_google_sync_confirms_missing_event_before_deletion(self):
        remote = self.server.live()[0]
        remote['status'] = 'cancelled'
        remote['etag'] = self.server._etag()
        wipe_external_cache(self.ctx.store,self.cid)
        sync(self.ctx,self.cid)
        row = self.ctx.store.get_event(self.eid)
        self.assertTrue(row is None or row.get('deleted_at'))
        self.assertTrue(any(c[0]=='GET' and c[1].endswith('/'+remote['id']) for c in self.server.calls))
