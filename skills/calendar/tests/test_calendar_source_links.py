"""A chosen source drives following copies; external changes remain observable."""
from __future__ import annotations
import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from test_calendar_round2 import OneAdapterProviders, RecordingAdapter, add_external_calendar, make_context
from test_sync_atomicity import FeedAdapter
import links
import tools
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz
from scripts.worker import reconcile_calendar
from store import Store


class SourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.addCleanup(shutil.rmtree, self.ctx.state_dir)
        self.ctx.tz = get_tz('UTC')
        self.external = add_external_calendar(self.ctx.store, 'projection', 'full')
        self.adapter = RecordingAdapter()
        self.ctx.providers = OneAdapterProviders(self.adapter)
        self.start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
        created = json.loads(tools.cal_create(self.ctx, title='Source', start=self.start.isoformat(),
                                             calendars=[DEFAULT_LOCAL_CALENDAR_ID, self.external], confirm=True))
        self.source = self.ctx.store.get_event(created['event']['id'])
        self.copy = next(member for member in self.ctx.store.group_masters(self.source['link_group_id']) if member['id'] != self.source['id'])

    def edit(self, eid, **changes):
        return json.loads(tools.cal_update(self.ctx, id=eid, confirm=True, **changes))

    def feed(self, **changes):
        current = self.ctx.store.get_event(self.copy['id'])
        incoming = {key: value for key, value in current.items() if key not in ('id', 'provider', 'account_id', 'calendar_name')}
        incoming.update(changes)
        adapter = FeedAdapter([incoming], seen=[current['href']])
        return reconcile_calendar(self.ctx.store, self.ctx.providers, adapter, self.ctx.store.get_calendar(self.external), self.ctx.tz)

    def test_following_projection_updates_and_explicit_copy_edit_does_not_change_source(self):
        self.assertEqual(self.edit(self.source['id'], title='Source updated')['status'], 'updated')
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['title'], 'Source updated')
        self.assertEqual(self.edit(self.copy['id'], title='Only the copy')['status'], 'updated')
        self.assertEqual(self.ctx.store.get_event(self.source['id'])['title'], 'Source updated')
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['link_state'], links.DIVERGED)
        self.edit(self.source['id'], title='Source again')
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['title'], 'Only the copy')

    def test_external_divergence_survives_own_ack_restart_and_next_sync(self):
        self.feed(title='Provider edit', etag='remote-edit')
        self.assertEqual(self.ctx.store.get_event(self.source['id'])['title'], 'Source')
        self.edit(self.copy['id'], title='Explicit copy edit')
        row = self.ctx.store.get_event(self.copy['id'])
        self.assertEqual((row['sync_state'], row['link_state']), ('synced', links.DIVERGED))
        self.ctx.store = Store(self.ctx.state_dir)
        self.feed()
        self.edit(self.source['id'], title='New source title')
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['title'], 'Explicit copy edit')
        detail = json.loads(tools.cal_events(self.ctx, id=self.copy['id']))['event']['link']
        self.assertEqual((detail['source_id'], detail['state']), (self.source['id'], links.DIVERGED))

    def test_provider_deletion_is_retained_as_missing_without_cascade_or_resurrection(self):
        gone = FeedAdapter()
        reconcile_calendar(self.ctx.store, self.ctx.providers, gone, self.ctx.store.get_calendar(self.external), self.ctx.tz)
        self.assertIsNone(self.ctx.store.get_event(self.source['id'])['deleted_at'])
        row = self.ctx.store.get_event(self.copy['id'])
        self.assertTrue(row['deleted_at'])
        self.assertEqual(row['link_state'], links.MISSING)
        initial_creates = len([call for call in self.adapter.calls if call[0] == 'create'])
        self.edit(self.source['id'], title='Changed source')
        self.assertEqual(len([call for call in self.adapter.calls if call[0] == 'create']), initial_creates)
        result = json.loads(tools.cal_events(self.ctx, id=self.copy['id']))
        self.assertTrue(result['event']['missing'])
        self.assertEqual(result['event']['link']['state'], links.MISSING)
        self.assertEqual(self.ctx.store.link_issues()[0]['id'], self.copy['id'])

    def test_explicit_follow_restores_copy_and_future_source_updates(self):
        self.feed(title='Diverged', etag='external')
        preview = json.loads(tools.cal_update(self.ctx, id=self.copy['id'], link_action='follow', scope='all'))
        self.assertEqual(preview['status'], 'needs_confirm')
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['title'], 'Diverged')
        result = self.edit(self.copy['id'], link_action='follow', scope='all')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['link_state'], links.FOLLOWING)
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['title'], 'Source')
        self.edit(self.source['id'], title='Following works')
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['title'], 'Following works')

    def test_ambiguous_legacy_group_needs_explicit_source_and_preserves_pending_work(self):
        self.ctx.store.update_event(self.copy['id'], {'is_primary': 1})
        self.ctx.providers = OneAdapterProviders(None)
        self.edit(self.copy['id'], title='Queued copy change')
        pending = self.ctx.store.intents_for_event(self.copy['id'])
        self.assertTrue(any(intent['state'] == 'pending' for intent in pending))
        self.ctx.store = Store(self.ctx.state_dir)
        self.assertIsNone(links.source(self.ctx.store, self.source))
        result = self.edit(self.copy['id'], link_action='source', scope='all')
        self.assertEqual(result['source_id'], self.copy['id'])
        self.assertEqual(links.source(self.ctx.store, self.source)['id'], self.copy['id'])
        self.assertEqual(self.ctx.store.intents_for_event(self.copy['id']), pending)

    def test_detach_is_explicit_and_keeps_event_and_its_record(self):
        result = self.edit(self.copy['id'], link_action='detach', scope='all')
        self.assertEqual(result['status'], 'ok')
        row = self.ctx.store.get_event(self.copy['id'])
        self.assertEqual((row['link_group_id'], row['is_primary']), ('', 1))
        self.assertIsNone(row['deleted_at'])
        self.edit(self.source['id'], title='Independent')
        self.assertEqual(self.ctx.store.get_event(self.copy['id'])['title'], 'Source')


class SourceRecurrenceTests(unittest.TestCase):
    def test_copy_occurrence_cancellation_survives_later_source_rule_change(self):
        from test_calendar_review_r5 import ReviewR5Tests
        from test_calendar_atomic_replay import instance
        from test_calendar_provider_divergence import _day, run_open_intents
        from model import iso_utc
        ctx, cid, caldav, google, left, source_id, copy = ReviewR5Tests().linked()
        self.addCleanup(shutil.rmtree, ctx.state_dir)
        remote_id, _ = instance(ctx, google, copy['id'], 3)
        cancelled = json.loads(tools.cal_delete(ctx, id=f"{copy['id']}@{iso_utc(_day(3))}", confirm=True))
        self.assertEqual(cancelled['status'], 'deleted', cancelled)
        self.assertEqual(google.events[remote_id]['status'], 'cancelled')
        self.assertEqual(ctx.store.get_event(copy['id'])['link_state'], links.DIVERGED)
        self.assertEqual(ctx.store.exceptions_for(source_id), [])
        changed = json.loads(tools.cal_update(ctx, id=source_id, rrule='FREQ=DAILY;COUNT=7', scope='all', confirm=True))
        self.assertEqual(changed['status'], 'updated', changed)
        run_open_intents(ctx, 3)
        self.assertEqual(ctx.store.get_event(copy['id'])['rrule'], 'FREQ=DAILY;COUNT=5')
        self.assertEqual(google.events[remote_id]['status'], 'cancelled')
        self.assertEqual(ctx.store.exceptions_for(copy['id'])[0]['link_state'], links.DIVERGED)
        resumed = json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
        self.assertEqual(resumed['status'], 'ok', resumed)
        self.assertEqual(google.events[remote_id]['status'], 'confirmed')
        self.assertEqual(ctx.store.get_event(copy['id'])['link_state'], links.FOLLOWING)

    def test_diverged_recurring_copy_keeps_its_own_busy_interval(self):
        from test_calendar_review_r5 import ReviewR5Tests
        from test_calendar_atomic_replay import instance
        from test_calendar_provider_divergence import _day
        from model import iso_utc
        import ops
        ctx, cid, caldav, google, left, source_id, copy = ReviewR5Tests().linked()
        self.addCleanup(shutil.rmtree, ctx.state_dir)
        instance(ctx, google, copy['id'], 3)
        start, end = _day(3).replace(hour=0), _day(4).replace(hour=0)
        before, _ = ops.busy_intervals(ctx.occurrences(start, end))
        self.assertEqual(len(before), 1)  # an unchanged projection does not double count
        result = json.loads(tools.cal_update(ctx, id=f"{copy['id']}@{iso_utc(_day(3))}",
                                            start=(_day(3) + timedelta(hours=3)).isoformat(), confirm=True))
        self.assertEqual(result['status'], 'updated', result)
        after, _ = ops.busy_intervals(ctx.occurrences(start, end))
        self.assertEqual(after, [(_day(3), _day(3) + timedelta(hours=1)),
                                 (_day(3) + timedelta(hours=3), _day(3) + timedelta(hours=4))])

    def test_splitting_projection_keeps_the_original_source_observable(self):
        from test_calendar_review_r5 import ReviewR5Tests
        from test_calendar_provider_divergence import _day
        from model import iso_utc
        ctx, cid, caldav, google, left, source_id, copy = ReviewR5Tests().linked()
        self.addCleanup(shutil.rmtree, ctx.state_dir)
        before_source = ctx.store.get_event(source_id)
        result = json.loads(tools.cal_update(ctx, id=f"{copy['id']}@{iso_utc(_day(4))}", title='Later copy', scope='following', confirm=True))
        self.assertEqual(result['status'], 'updated', result)
        later = next(row for row in ctx.store.group_masters(before_source['link_group_id']) if row['title'] == 'Later copy')
        self.assertEqual(links.source(ctx.store, later)['id'], source_id)
        self.assertEqual(later['link_state'], links.DIVERGED)
        self.assertEqual(ctx.store.get_event(source_id), before_source)

    def test_external_source_content_updates_following_full_copy(self):
        from test_calendar_review_r5 import ReviewR5Tests
        ctx, cid, caldav, google, left, source_id, copy = ReviewR5Tests().linked()
        self.addCleanup(shutil.rmtree, ctx.state_dir)
        def edit(calendar):
            event = next(c for c in calendar.walk() if c.name == 'VEVENT')
            event['DESCRIPTION'] = 'New description'
            event['LOCATION'] = 'New room'
        caldav.edit(ctx.store.get_event(source_id)['href'], edit)
        reconcile_calendar(ctx.store, ctx.providers, left, ctx.store.get_calendar(cid), ctx.tz)
        observed = ctx.store.get_event(source_id)
        self.assertEqual((observed['description'], observed['location']), ('New description', 'New room'))
        remote = google.events[copy['external_id']]
        self.assertEqual((remote['description'], remote['location']), ('New description', 'New room'))


if __name__ == '__main__': unittest.main()
