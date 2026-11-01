"""Long provider outages must not erase a queued owner edit."""
import json
import shutil
import unittest
from test_calendar_provider_divergence import google_context, caldav_context, sync, run_open_intents, _day
import tools


class OfflineRetentionTests(unittest.TestCase):
    def test_long_outage_keeps_edit_until_recovery_for_both_adapters(self):
        for factory in (google_context, caldav_context):
            with self.subTest(provider=factory.__name__):
                ctx, calendar_id, server = factory()
                self.addCleanup(shutil.rmtree, ctx.state_dir)
                created = json.loads(tools.cal_create(ctx, title='Before outage', start=_day(2).isoformat(), calendars=[calendar_id], confirm=True))
                event_id = created['event']['id']
                server.down = True
                out = json.loads(tools.cal_update(ctx, id=event_id, title='Owner edit', confirm=True))
                self.assertEqual(out['status'],'pending')
                run_open_intents(ctx, rounds=8)
                active = [i for i in ctx.store.intents_for_event(event_id) if i['kind']=='update'][-1]
                self.assertEqual(active['state'],'pending')
                self.assertGreater(active['attempts'],5)
                server.down = False
                sync(ctx, calendar_id)
                self.assertEqual(ctx.store.get_event(event_id)['title'],'Owner edit')
                run_open_intents(ctx)
                sync(ctx, calendar_id)
                self.assertEqual(ctx.store.get_event(event_id)['title'],'Owner edit')
                self.assertEqual(ctx.store.get_event(event_id)['sync_state'],'synced')
