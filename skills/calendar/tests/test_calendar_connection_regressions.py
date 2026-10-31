"""Reproductions of calendar failures found while qualifying personal accounts.

Temporary stores and provider simulators only; no personal credentials or network.
"""
from __future__ import annotations

import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_calendar_round2 import OneAdapterProviders, make_context
from test_sync_atomicity import FeedAdapter, SyncCase
import ops
import providers
import providers_google as google
import reminders
import tools
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc
from scripts import worker


class RecurrenceConnectionTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.addCleanup(shutil.rmtree, self.ctx.state_dir)

    def create(self, **kwargs):
        result = json.loads(tools.cal_create(self.ctx, title='Routine', confirm=True, **kwargs))
        self.assertEqual(result['status'], 'created', result)
        return result['event']['id']

    def test_soft_routine_ids_survive_owner_timezone_change_and_edit(self):
        self.ctx.tz = get_tz('Asia/Dubai')
        eid = self.create(start='2026-10-20T06:30', duration_min=30, availability='soft', rrule='FREQ=DAILY;COUNT=3')
        self.ctx.tz = get_tz('Europe/Moscow')
        event = json.loads(tools.cal_events(self.ctx, start='2026-10-21', end='2026-10-22'))['events'][0]
        self.assertEqual(event['start'][11:16], '06:30')
        self.assertEqual(event['id'], eid + '@2026-10-21T02:30:00+00:00')
        direct = json.loads(tools.cal_events(self.ctx, id=event['id']))
        self.assertEqual(direct['status'], 'ok', direct)
        self.assertEqual(direct['event']['start'][11:16], '06:30')
        changed = json.loads(tools.cal_update(self.ctx, id=event['id'], title='Renamed', confirm=True))
        self.assertEqual(changed['status'], 'updated', changed)
        moved = json.loads(tools.cal_update(self.ctx, id=event['id'], start='2026-10-21T07:15', confirm=True))
        self.assertEqual(moved['status'], 'updated', moved)
        self.assertEqual(json.loads(tools.cal_events(self.ctx, id=event['id']))['event']['start'][11:16], '07:15')

    def test_all_day_exception_keeps_calendar_day_across_dst(self):
        self.ctx.tz = get_tz('Europe/Berlin')
        self.create(start='2026-10-24', all_day=True, rrule='FREQ=DAILY;COUNT=3')
        day = json.loads(tools.cal_events(self.ctx, start='2026-10-25', end='2026-10-26'))['events'][0]
        result = json.loads(tools.cal_update(self.ctx, id=day['id'], title='Renamed', confirm=True))
        self.assertEqual(result['status'], 'updated', result)
        event = json.loads(tools.cal_events(self.ctx, id=day['id']))['event']
        self.assertEqual(event['end'], '2026-10-26T00:00:00+01:00')

    def test_removing_recurrence_retires_overrides_and_waiting_notifications(self):
        self.ctx.tz = get_tz('UTC')
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
        eid = self.create(start=start.isoformat(), rrule='FREQ=DAILY;COUNT=3', reminders=[15])
        key = iso_utc(start + timedelta(days=1))
        changed = json.loads(tools.cal_update(self.ctx, id=eid + '@' + key, title='Override', confirm=True))
        override = next(e for e in self.ctx.store.exceptions_for(eid) if e['recurrence_id'] == key)
        self.ctx.store.add_intent('update', 'local', DEFAULT_LOCAL_CALENDAR_ID, override['id'], {'changes': {'title': 'Old queued edit'}})
        self.assertEqual(changed['status'], 'updated')
        result = json.loads(tools.cal_update(self.ctx, id=eid, rrule='', scope='all', confirm=True))
        self.assertEqual(result['status'], 'updated', result)
        self.assertTrue(self.ctx.store.get_event(override['id'])['deleted_at'])
        self.assertEqual(self.ctx.store.exceptions_for(eid), [])
        with patch('store.now_utc', return_value=datetime.now(timezone.utc) + timedelta(seconds=1)):
            ops.retry_due_intents(self.ctx.store, self.ctx.providers)
        self.assertFalse(any(i['state'] == 'pending' for i in self.ctx.store.intents_for_event(override['id'])))
        self.assertEqual(json.loads(tools.cal_events(self.ctx, id=override['id']))['status'], 'not_found')
        with self.ctx.store._conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders WHERE event_id=? AND state='scheduled'", (override['id'],)).fetchone()[0], 0)


class ReminderConnectionTests(unittest.TestCase):
    def test_reload_does_not_replace_pre_mute_defaults(self):
        ctx = make_context()
        self.addCleanup(shutil.rmtree, ctx.state_dir)
        account = 'google:owner@example.test'
        cal = {'id': account + ':primary', 'account_id': account, 'provider': 'google', 'external_id': 'owner@example.test',
               'name': 'Personal', 'writable': True, 'default_reminders': [{'method': 'popup', 'minutes': 10}]}
        ctx.store.upsert_account({'id': account, 'provider': 'google', 'login': 'owner@example.test'})
        ctx.store.upsert_calendar(cal)
        ctx.store.set_setting('google_default_reminders:' + cal['id'], cal['default_reminders'])
        class Adapter:
            defaults = list(cal['default_reminders'])
            def set_default_reminders(self, calendar, value): self.defaults = list(value)
            def list_calendars(self): return [{**cal, 'default_reminders': self.defaults}]
        adapter = Adapter()
        ctx.providers = OneAdapterProviders(adapter)
        self.assertTrue(tools.set_reminder_mode(ctx, cal['id'], True)['provider_muted'])
        with patch.object(google, 'load_tokens', return_value={'owner@example.test': {'scope': 'calendar'}}):
            self.assertEqual(tools.reload_google(ctx)['status'], 'ok')
        result = tools.set_reminder_mode(ctx, cal['id'], False)
        self.assertEqual(adapter.defaults, [{'method': 'popup', 'minutes': 10}], result)
        self.assertFalse((ctx.store.get_setting(reminders.MODE_KEY) or {}).get(cal['id']))


class ProviderConnectionTests(SyncCase):
    def test_exception_refresh_updates_busy_zone_and_all_day(self):
        master, slot, override = self.series()
        self.ctx.store.update_event(override['id'], {'availability': 'free', 'tz': 'UTC', 'all_day': 0})
        master = self.ctx.store.get_event(master['id'])
        event = self.provider_override(master, slot)
        event.update({'availability': 'busy', 'tz': 'Europe/Berlin', 'all_day': 1, 'etag': 'changed'})
        self.sync(FeedAdapter([self.feed_row(master), event], seen=[master['href']]))
        current = self.ctx.store.get_event(override['id'])
        self.assertEqual((current['availability'], current['tz'], current['all_day']), ('busy', 'Europe/Berlin', 1))

    def test_unchanged_ctag_only_skips_the_same_query_window(self):
        cal = self.ctx.store.get_calendar(self.external)
        account = self.ctx.store.list_accounts()[1]
        calls = []
        class Adapter(FeedAdapter):
            def list_calendars(self): return [{**cal, 'ctag': 'unchanged'}]
            def fetch(self, calendar, cursor='', tz=None):
                calls.append(calendar['id'])
                return super().fetch(calendar, cursor, tz)
        adapter = Adapter()
        resolver = OneAdapterProviders(adapter)
        now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        with patch.object(worker, 'now_utc', return_value=now), patch.object(providers, 'now_utc', return_value=now):
            worker.sync_account(self.ctx.store, resolver, account, self.ctx.tz)
            worker.sync_account(self.ctx.store, resolver, account, self.ctx.tz)
        self.assertEqual(len(calls), 1)
        with patch.object(worker, 'now_utc', return_value=now + timedelta(days=2)), patch.object(providers, 'now_utc', return_value=now + timedelta(days=2)):
            worker.sync_account(self.ctx.store, resolver, account, self.ctx.tz)
        self.assertEqual(len(calls), 2)


class FloatingCalDAVConnectionTests(unittest.TestCase):
    def test_edit_and_cancel_replace_the_existing_floating_override(self):
        text = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:series',
                            'DTSTART:20261101T090000', 'DTEND:20261101T100000', 'RRULE:FREQ=DAILY;COUNT=3',
                            'END:VEVENT', 'BEGIN:VEVENT', 'UID:series', 'RECURRENCE-ID:20261102T090000',
                            'DTSTART:20261102T110000', 'DTEND:20261102T120000', 'END:VEVENT', 'END:VCALENDAR', ''])
        for status in ('confirmed', 'cancelled'):
            with self.subTest(status=status):
                sent = []
                adapter = providers.YandexAdapter('synthetic', 'synthetic')
                adapter.get = lambda _: (text, 'e1')
                adapter._request = lambda method, url, body=None, headers=None: (sent.append(body) or (200, {'etag': 'e2'}, ''))
                adapter._write_exception({'uid': 'series', 'master_href': 'https://caldav.yandex.ru/calendars/test/series.ics',
                                          'master_etag': 'e1', 'tz': 'Europe/Moscow', 'recurrence_id': '2026-11-02T06:00:00+00:00',
                                          'start_utc': '2026-11-02T09:00:00+00:00', 'end_utc': '2026-11-02T10:00:00+00:00',
                                          'title': 'Changed', 'status': status}, {}, 'e1')
                calendar = providers._ical().Calendar.from_ical(sent[-1])
                overrides = [event for event in calendar.walk('VEVENT') if event.get('RECURRENCE-ID') is not None]
                self.assertEqual(len(overrides), 1 if status == 'confirmed' else 0)


if __name__ == '__main__':
    unittest.main()
