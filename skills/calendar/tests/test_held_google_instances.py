"""Google instances whose series the calendar does not hold (Hub #355), through the companion's sync and public tools.

An invitation to one date of someone else's series arrives as an instance with ``recurringEventId`` and no
master. These tests use an in-memory Google change feed with synthetic events; no account, token or network.
"""
import json
import urllib.parse
from datetime import timedelta

import pytest

from test_calendar_provider_divergence import FakeGoogle, _day, add_google_calendar, run_open_intents
from test_calendar_round2 import Channel, OneAdapterProviders, add_local_calendar, make_context
from model import get_tz, now_utc
from providers_google import GoogleAdapter
from scripts import worker
import ops
import reminders as rem
import tools

HOST = {'email': 'host@example.test', 'organizer': True, 'responseStatus': 'accepted'}
ME = {'email': 'owner@example.test', 'self': True, 'responseStatus': 'accepted'}


class FeedGoogle(FakeGoogle):
    """A sync token returns only items changed since the previous listing; a full listing returns current items,
    including cancelled instances of recurring events, as Google does with singleEvents=false."""

    def __init__(self):
        super().__init__()
        self.changed, self.listings, self.fail_listing = [], [], False

    def put(self, item):
        self.events[item['id']] = {**item, 'etag': self._etag()}
        self.changed.append(item['id'])
        return self.events[item['id']]

    def _apply(self, method, path, params, body):
        parts = path.split('/')
        if method == 'GET' and len(parts) == 4 and parts[3] == 'events':
            self.listings.append(dict(params))
            if self.fail_listing:
                raise ops.ProviderError('network', 'listing unavailable', unsent=True)
            if params.get('syncToken'):
                items = [dict(self.events[i]) for i in dict.fromkeys(self.changed)]
            else:
                items = [dict(i) for i in self.events.values() if i.get('status') != 'cancelled' or i.get('recurringEventId')]
            self.changed = []
            return 200, {}, {'items': items, 'nextSyncToken': f'token-{len(self.listings)}'}
        result = super()._apply(method, path, params, body)
        if method != 'GET':
            self.changed.append(body['id'] if method == 'POST' else urllib.parse.unquote(parts[4]))
        return result


def google_feed():
    ctx = make_context()
    ctx.tz = get_tz('UTC')
    cid = add_google_calendar(ctx.store)
    server = FeedGoogle()
    adapter = server.attach(GoogleAdapter('owner@example.test', 'token', 'refresh', now_utc() + timedelta(days=1), 'client', '', None))
    calendar = ctx.store.get_calendar(cid)
    adapter.list_calendars = lambda: [calendar]
    ctx.providers = OneAdapterProviders(adapter)
    return ctx, cid, server


def pull(ctx, cid):
    """One companion sync pass of the account."""
    account = ctx.store.get_account(ctx.store.get_calendar(cid)['account_id'])
    return worker.sync_account(ctx.store, ctx.providers, account, ctx.tz)


def timed(value):
    return {'dateTime': value.isoformat()}


def instance(series, original, start=None, *, title='Weekly sync'):
    start = start or original
    return {'id': series + '_' + original.strftime('%Y%m%dT%H%M%SZ'), 'iCalUID': series + '@google.com', 'status': 'confirmed',
            'summary': title, 'start': timed(start), 'end': timed(start + timedelta(hours=1)),
            'recurringEventId': series, 'originalStartTime': timed(original), 'organizer': {'email': HOST['email']}, 'attendees': [HOST, ME]}


def cancelled(series, original):
    """Google's form of a cancelled instance: identity and original slot only."""
    return {'id': series + '_' + original.strftime('%Y%m%dT%H%M%SZ'), 'status': 'cancelled',
            'recurringEventId': series, 'originalStartTime': timed(original)}


def series(series_id, first, rule):
    return {'id': series_id, 'iCalUID': series_id + '@google.com', 'status': 'confirmed', 'summary': 'Weekly sync',
            'start': timed(first), 'end': timed(first + timedelta(hours=1)), 'recurrence': ['RRULE:' + rule],
            'organizer': {'email': HOST['email']}, 'attendees': [HOST, ME]}


def meeting(event_id, start, title='Ordinary meeting'):
    return {'id': event_id, 'iCalUID': event_id + '@google.com', 'status': 'confirmed', 'summary': title,
            'start': timed(start), 'end': timed(start + timedelta(hours=1)), 'organizer': {'email': HOST['email']}, 'attendees': [HOST, ME]}


def listed(ctx, day, calendars=None):
    out = json.loads(tools.cal_events(ctx, start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(), calendars=calendars))
    assert out['events_complete']
    return [(e['title'], e['start'][11:16], e['end'][11:16]) for e in out['events']]


def free(ctx, day):
    out = json.loads(tools.cal_free(ctx, start=(day + timedelta(hours=9)).isoformat(), end=(day + timedelta(hours=19)).isoformat(), duration_min=60))
    assert out['availability_complete']
    return [(slot['start'][11:16], slot['end'][11:16]) for slot in out['slots']]


def test_held_instance_is_listed_once_and_its_busy_hour_is_not_offered():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    server.put(instance('weekly', day + timedelta(hours=11)))
    server.put(meeting('plain', day + timedelta(hours=15)))
    assert pull(ctx, cid)['status'] == 'ok'
    expected = [('Weekly sync', '11:00', '12:00'), ('Ordinary meeting', '15:00', '16:00')]
    assert listed(ctx, day) == expected
    assert free(ctx, day) == [('09:00', '11:00'), ('12:00', '15:00'), ('16:00', '19:00')]
    state = ctx.store.get_sync_state(cid)
    assert state['cursor'] and state['cursor_kind'] == worker.GOOGLE_CURSOR_KIND
    # Replays: unchanged items in an incremental delta, then a full listing after a lost token.
    server.changed = list(server.events)
    pull(ctx, cid)
    ctx.store.set_sync_state(cid, cursor='')
    pull(ctx, cid)
    assert server.listings[-2].get('syncToken') and 'syncToken' not in server.listings[-1]
    assert listed(ctx, day) == expected
    assert len(ctx.store.held_instances(cid)) == 1
    assert all(call[0] == 'GET' for call in server.calls)   # a read never writes to the provider


def test_series_arriving_alone_adopts_its_moved_and_cancelled_dates_by_exact_series_id():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    first, second = day + timedelta(hours=11), day + timedelta(days=7, hours=11)
    moved = server.put(instance('weekly', first))
    server.put(instance('weekly', second))
    pull(ctx, cid)
    records = ctx.store.held_instances(cid)
    assert len(records) == 2 and len({r['uid'] for r in records}) == 1   # one record per instance ID, shared iCalUID
    server.put({**moved, 'start': timed(first + timedelta(hours=2)), 'end': timed(first + timedelta(hours=3))})
    server.put(cancelled('weekly', second))
    pull(ctx, cid)
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7)) == [] and free(ctx, day + timedelta(days=7)) == [('09:00', '19:00')]
    # A stored series that the instances do not name is not theirs.
    server.put(series('standup', first - timedelta(days=7), 'FREQ=WEEKLY;COUNT=1'))
    pull(ctx, cid)
    assert {r['id'] for r in ctx.store.held_instances(cid)} == {r['id'] for r in records}
    # The real series arrives alone in the delta.
    server.put(series('weekly', first - timedelta(days=7), 'FREQ=WEEKLY;COUNT=5'))
    pull(ctx, cid)
    assert ctx.store.held_instances(cid) == []
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert {r['id'] for r in ctx.store.exceptions_for(master['id'])} == {r['id'] for r in records}
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]          # the moved hour does not return at 11:00
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    assert listed(ctx, day + timedelta(days=7)) == []                       # the cancelled hour stays cancelled
    assert listed(ctx, day + timedelta(days=14)) == [('Weekly sync', '11:00', '12:00')]
    assert all(call[0] == 'GET' for call in server.calls)


def test_cancellation_without_series_stays_hidden_and_is_remembered_when_the_series_arrives():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    item = server.put(cancelled('weekly', slot))
    pull(ctx, cid)
    assert listed(ctx, day) == [] and free(ctx, day) == [('09:00', '19:00')]
    # A later full listing without it, whose lookup answers 404, does not erase the remembered cancellation.
    del server.events[item['id']]
    ctx.store.set_sync_state(cid, cursor='')
    pull(ctx, cid)
    assert len(ctx.store.held_instances(cid)) == 1
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    assert listed(ctx, day) == [] and free(ctx, day) == [('09:00', '19:00')]
    assert listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]


def test_an_older_google_token_is_replaced_once_by_a_full_listing_and_a_failed_read_keeps_everything():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    server.put(meeting('plain', day + timedelta(hours=15)))
    pull(ctx, cid)
    plain = ctx.store.find_by_external(cid, external_id='plain')
    # The state Calendar 2.1.0 left: its token under the older kind; the instance it skipped is unchanged since.
    server.put(instance('weekly', day + timedelta(hours=11)))
    server.changed = []
    ctx.store.set_sync_state(cid, cursor='token-from-2.1.0', cursor_kind='google_sync_token')
    server.down = True
    assert json.loads(tools.cal_update(ctx, id=plain['id'], title='Renamed offline', confirm=True))['status'] == 'pending'
    server.down, server.fail_listing = False, True
    assert pull(ctx, cid)['status'] == 'error'
    state = ctx.store.get_sync_state(cid)
    assert (state['cursor'], state['cursor_kind']) == ('token-from-2.1.0', 'google_sync_token')
    assert [i['state'] for i in ctx.store.intents_for_event(plain['id'])] == ['pending']
    assert listed(ctx, day) == [('Renamed offline', '15:00', '16:00')]
    server.fail_listing = False
    run_open_intents(ctx)
    before = len(server.listings)
    pull(ctx, cid)
    full = server.listings[before]
    assert 'syncToken' not in full and full['timeMin'] and full['showDeleted'] == 'false'
    assert listed(ctx, day) == [('Weekly sync', '11:00', '12:00'), ('Renamed offline', '15:00', '16:00')]
    token = ctx.store.get_sync_state(cid)['cursor']
    pull(ctx, cid)
    assert server.listings[-1].get('syncToken') == token and len(server.listings) == before + 2


def test_held_instance_writes_address_only_that_instance_and_never_carry_recurrence():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    item = server.put(instance('weekly', day + timedelta(hours=11)))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    refused = json.loads(tools.cal_update(ctx, id=record['id'], rrule='FREQ=DAILY;COUNT=3', confirm=True))
    assert refused['status'] == 'argument_required' and not ctx.store.intents_for_event(record['id'])
    calls = len(server.calls)
    assert json.loads(tools.cal_update(ctx, id=record['id'], reminders=[10], confirm=True))['status'] == 'updated'
    writes = [(method, path.rsplit('/', 1)[-1], body) for method, path, _, body in server.calls[calls:] if method != 'GET']
    assert writes == [('PATCH', item['id'], {'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 10}]}})]
    calls = len(server.calls)
    assert json.loads(tools.cal_delete(ctx, id=record['id'], confirm=True))['status'] == 'deleted'
    assert [(m, p.rsplit('/', 1)[-1]) for m, p, _, _ in server.calls[calls:] if m != 'GET'] == [('DELETE', item['id'])]
    pull(ctx, cid)   # Google now lists the instance as cancelled
    assert listed(ctx, day) == []


def test_moved_date_waiting_for_owner_work_is_listed_once_and_joins_keeping_its_sent_notice():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    original, moved_to = day + timedelta(hours=11), day + timedelta(hours=13)
    at = moved_to - timedelta(minutes=10)   # the clock when its 10-minute notice is due
    ctx.store.set_setting(rem.MODE_KEY, {cid: True})   # «напоминает Уроборос» for this Google calendar
    server.put(instance('weekly', original, moved_to))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    assert json.loads(tools.cal_update(ctx, id=record['id'], reminders=[10], confirm=True))['status'] == 'updated'
    channel = Channel()
    rem.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=at)
    assert rem.deliver_due(ctx.store, channel, ctx.tz, now=at)['sent'] == 1
    server.down = True
    assert json.loads(tools.cal_update(ctx, id=record['id'], title='Offline title', confirm=True))['status'] == 'pending'
    server.down = False
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    # The owner's write is unsettled: the record and its queued write stay as they are, and the series does not
    # list or occupy the date the record stands for.
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert [r['id'] for r in ctx.store.held_instances(cid)] == [record['id']] and not ctx.store.exceptions_for(master['id'])
    assert [i['state'] for i in ctx.store.intents_for_event(record['id'])][-1] == 'pending'
    expected = [('Offline title', '13:00', '14:00')]
    assert listed(ctx, day) == expected and free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    assert listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]
    run_open_intents(ctx)
    pull(ctx, cid)
    adopted = ctx.store.get_event(record['id'])
    assert ctx.store.held_instances(cid) == [] and adopted['master_id'] == master['id']
    assert listed(ctx, day) == expected and free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    assert json.loads(adopted['reminders_json']) == [10]
    rem.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=at)
    assert rem.deliver_due(ctx.store, channel, ctx.tz, now=at)['sent'] == 0 and len(channel.sent) == 1


def test_held_date_with_linked_copies_stays_separate_without_repeating_its_series_date():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror')
    day = _day(5, 0)
    original, moved_to = day + timedelta(hours=11), day + timedelta(hours=13)
    server.put(instance('weekly', original, moved_to))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    assert json.loads(tools.cal_update(ctx, id=record['id'], calendars=[cid, mirror], confirm=True))['status'] == 'updated'
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    assert [r['id'] for r in ctx.store.held_instances(cid)] == [record['id']]   # its linked copy keeps it separate
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[cid]) == [('Weekly sync', '11:00', '12:00')]


@pytest.mark.parametrize('removal', ['deleted_in_feed', 'absent_from_listing', 'deleted_by_owner'])
def test_removed_held_date_stays_removed_when_its_series_arrives_alone(removal):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    item = server.put(instance('weekly', slot))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    if removal == 'deleted_in_feed':          # Google's deleted form: the ID and status only
        server.put({'id': item['id'], 'status': 'cancelled'})
        pull(ctx, cid)
    elif removal == 'absent_from_listing':    # a full listing without it; its lookup answers 404
        del server.events[item['id']]
        ctx.store.set_sync_state(cid, cursor='')
        pull(ctx, cid)
    else:                                     # the owner's delete, before Google's echo of it
        assert json.loads(tools.cal_delete(ctx, id=record['id'], confirm=True))['status'] == 'deleted'
        server.changed.clear()
    assert listed(ctx, day) == []
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    assert listed(ctx, day) == [] and free(ctx, day) == [('09:00', '19:00')]
    assert listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]
    assert json.loads(tools.cal_events(ctx, id=record['id']))['status'] == 'not_found'
    assert json.loads(tools.cal_update(ctx, id=record['id'], title='Again', confirm=True))['status'] == 'not_found'
    assert all(intent['state'] == 'done' for intent in ctx.store.intents_for_event(record['id']))


# ── Review round 1: one received date is one meeting for copies, addresses, zones, cancellations and feeds ──

from providers import CalDAVAdapter   # noqa: E402


def add_second_google_calendar(store):
    account = 'google:owner@example.test'
    calendar_id = account + ':work'
    store.upsert_calendar({'id': calendar_id, 'account_id': account, 'provider': 'google', 'external_id': 'work',
                           'href': '', 'name': 'Work', 'writable': True, 'publish_mode': 'busy'})
    return calendar_id


def update(ctx, **kwargs):
    return json.loads(tools.cal_update(ctx, confirm=True, **kwargs))


def creates(server, since=0):
    return [body['id'] for method, path, _, body in server.calls[since:] if method == 'POST']


def test_copies_of_two_kept_dates_of_one_series_have_their_own_stable_addresses():
    ctx, cid, server = google_feed()
    work = add_second_google_calendar(ctx.store)
    day = _day(5, 0)
    server.put(instance('weekly', day + timedelta(hours=11)))
    server.put(instance('weekly', day + timedelta(days=7, hours=11)))
    pull(ctx, cid)
    first, second = sorted(ctx.store.held_instances(cid), key=lambda r: r['start_utc'])
    before = len(server.calls)
    assert update(ctx, id=first['id'], calendars=[cid, work])['status'] == 'updated'
    assert update(ctx, id=second['id'], calendars=[cid, work])['status'] == 'updated'
    posted = creates(server, before)
    assert len(posted) == 2 and len(set(posted)) == 2                       # no 409: two meetings, two addresses
    copies = [m for r in (first, second) for m in ctx.store.group_masters(ctx.store.get_event(r['id'])['link_group_id'])
              if m['calendar_id'] == work]
    assert [c['uid'] for c in copies] == [ops.kept_copy_uid(first), ops.kept_copy_uid(second)]
    assert first['uid'] == second['uid'] and first['uid'] not in {c['uid'] for c in copies}
    dav = {'href': 'https://dav.example.test/calendars/work/'}
    hrefs = {CalDAVAdapter.created_identity(None, dav, c)['href'] for c in copies}
    assert len(hrefs) == 2                                                 # and two CalDAV resources
    assert all(i['state'] == 'done' for c in copies for i in ctx.store.intents_for_event(c['id']))
    assert ops.kept_copy_uid(ctx.store.get_event(first['id'])) == copies[0]['uid']   # the same date always derives it


def _moved_kept_date(ctx, cid, server, day):
    original, moved_to = day + timedelta(hours=11), day + timedelta(hours=13)
    server.put(instance('weekly', original, moved_to))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    return record, original


def test_own_copy_first_then_series_copy_lists_the_date_once_at_its_hour_in_that_calendar():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, mirror])['status'] == 'updated'
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], calendars=[cid, mirror], scope='all')['status'] == 'updated'
    assert [t[1:] for t in listed(ctx, day, calendars=[mirror])] == [('13:00', '14:00')]
    assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[mirror])] == [('11:00', '12:00')]
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]


def test_series_copy_while_a_date_waits_carries_its_hour_and_its_own_copy_replaces_it_there():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='full')
    shared = add_local_calendar(ctx.store, 'shared', publish_mode='full')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, shared])['status'] == 'updated'   # linked: it stays separate
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], calendars=[cid, shared, mirror], scope='all')['status'] == 'updated'
    assert listed(ctx, day, calendars=[mirror]) == [('Weekly sync', '13:00', '14:00')]   # the waiting date's own hour
    assert listed(ctx, day, calendars=[shared]) == [('Weekly sync', '13:00', '14:00')]   # its own copy, once
    # The organizer moves the date again: the date's own copy and the series copy that carries it follow.
    item = server.events[record['external_id']]
    server.put({**item, 'start': timed(day + timedelta(hours=15)), 'end': timed(day + timedelta(hours=16))})
    pull(ctx, cid)
    run_open_intents(ctx)
    for calendar in (cid, mirror, shared):
        assert listed(ctx, day, calendars=[calendar]) == [('Weekly sync', '15:00', '16:00')]
    # The date is then cancelled: no calendar keeps it, and its own copy leaves no empty meeting.
    server.put(cancelled('weekly', original))
    pull(ctx, cid)
    run_open_intents(ctx)
    for calendar in (cid, mirror, shared):
        assert listed(ctx, day, calendars=[calendar]) == []
    assert ctx.store.get_event(record['id'])['title'] == 'Weekly sync'   # the sparse cancellation kept its content
    assert listed(ctx, day + timedelta(days=7), calendars=[mirror]) == [('Weekly sync', '11:00', '12:00')]


def test_old_id_with_scope_this_assigns_only_that_date_after_its_series_arrived_and_scope_all_the_series():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    wide = add_local_calendar(ctx.store, 'wide', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert ctx.store.get_event(record['id'])['master_id'] == master['id']   # joined its series
    # Explicit scope=all through the date's ID still assigns the whole series.
    assert update(ctx, id=record['id'], calendars=[cid, wide], scope='all')['status'] == 'updated'
    assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[wide])] == [('11:00', '12:00')]
    assert [t[1:] for t in listed(ctx, day, calendars=[wide])] == [('13:00', '14:00')]
    # Saving the date's card unchanged (its series' calendars) changes nothing.
    assert update(ctx, id=record['id'], calendars=[cid, wide])['status'] == 'updated'
    assert ctx.store.get_event(record['id'])['master_id'] == master['id']
    # The same old ID with the default scope=this gives only that date another calendar.
    assert update(ctx, id=record['id'], calendars=[cid, wide, mirror])['status'] == 'updated'
    assert [t[1:] for t in listed(ctx, day, calendars=[mirror])] == [('13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[mirror]) == []
    assert listed(ctx, day + timedelta(days=14), calendars=[mirror]) == []
    for calendar in (cid, wide):
        assert [t[1:] for t in listed(ctx, day, calendars=[calendar])] == [('13:00', '14:00')]
        assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[calendar])] == [('11:00', '12:00')]
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    run_open_intents(ctx)
    pull(ctx, cid)   # with its own copy the date stays its own record; nothing is listed twice
    assert ctx.store.get_event(record['id'])['master_id'] == ''
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]


def test_series_date_address_with_scope_this_reaches_the_waiting_record_not_the_series():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    other = add_local_calendar(ctx.store, 'other', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, mirror])['status'] == 'updated'
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    address = f"{master['id']}@{ops.iso_utc(original)}"
    read = json.loads(tools.cal_events(ctx, id=address))
    assert read['status'] == 'ok' and read['event']['id'] == record['id'] and read['event']['start'][11:16] == '13:00'
    assert update(ctx, id=address, calendars=[cid, mirror, other])['status'] == 'updated'
    assert [t[1:] for t in listed(ctx, day, calendars=[other])] == [('13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[other]) == []        # not the whole series
    assert update(ctx, id=address, title='Renamed date')['status'] in ('updated', 'pending')
    assert ctx.store.exceptions_for(master['id']) == [] and ctx.store.get_event(record['id'])['title'] == 'Renamed date'
    assert listed(ctx, day, calendars=[cid]) == [('Renamed date', '13:00', '14:00')]


@pytest.mark.parametrize('zone, local_hour', [('America/Los_Angeles', '22:00'), ('Europe/Berlin', '13:00')])
def test_all_day_series_date_moved_to_an_hour_in_another_zone_keeps_its_own_day(zone, local_hour):
    def scenario(together):
        ctx, cid, server = google_feed()
        ctx.tz = get_tz('Europe/Moscow')
        from datetime import date as _date
        first = _date(now_utc().year + 1, 1, 12)   # winter: the expected Moscow hours assume standard time there
        target = first + timedelta(days=2)
        from datetime import datetime as _dt
        start = _dt(target.year, target.month, target.day, 11, tzinfo=get_tz(zone))
        date_item = {'id': 'daily_' + target.strftime('%Y%m%d'), 'iCalUID': 'daily@google.com', 'status': 'confirmed',
                     'summary': 'Moved day', 'start': {'dateTime': start.isoformat(), 'timeZone': zone},
                     'end': {'dateTime': (start + timedelta(hours=1)).isoformat(), 'timeZone': zone},
                     'recurringEventId': 'daily', 'originalStartTime': {'date': target.isoformat()}}
        series_item = {'id': 'daily', 'iCalUID': 'daily@google.com', 'status': 'confirmed', 'summary': 'All day',
                       'start': {'date': first.isoformat()}, 'end': {'date': (first + timedelta(days=1)).isoformat()},
                       'recurrence': ['RRULE:FREQ=DAILY;COUNT=5']}
        if together:
            server.put(series_item)
        server.put(date_item)
        pull(ctx, cid)
        if not together:
            server.down = True    # an unsettled owner edit keeps the date waiting when its series arrives
            assert update(ctx, id=ctx.store.held_instances(cid)[0]['id'], reminders=[5])['status'] == 'pending'
            server.down = False
            server.put(series_item)
            pull(ctx, cid)
        return ctx, cid, target

    def days(ctx, target):
        out = []
        for offset in (-1, 0, 1):
            d = get_tz('Europe/Moscow')
            from datetime import datetime as _dt
            start = _dt(target.year, target.month, target.day, tzinfo=d) + timedelta(days=offset)
            events = json.loads(tools.cal_events(ctx, start=start.isoformat(), end=(start + timedelta(days=1)).isoformat()))['events']
            out.append(sorted((e['title'], e['all_day'], e['start'][11:16]) for e in events))
        return out

    expected = [[('All day', True, '00:00')], [('Moved day', False, local_hour)], [('All day', True, '00:00')]]
    control, control_cid, target = scenario(True)
    assert days(control, target) == expected
    ctx, cid, _ = scenario(False)
    assert len(ctx.store.held_instances(cid)) == 1 and days(ctx, target) == expected      # waiting
    run_open_intents(ctx)
    pull(ctx, cid)
    assert ctx.store.held_instances(cid) == [] and days(ctx, target) == expected          # joined
    master = ctx.store.find_by_external(cid, external_id='daily')
    joined = [e['recurrence_id'] for e in ctx.store.exceptions_for(master['id'])]
    reference = control.store.find_by_external(control_cid, external_id='daily')
    assert joined == [e['recurrence_id'] for e in control.store.exceptions_for(reference['id'])]


def test_a_lost_delete_answer_is_settled_by_googles_cancellation_of_that_date():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    server.put(instance('weekly', slot))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    server.lose_response.add('DELETE')
    assert json.loads(tools.cal_delete(ctx, id=record['id'], confirm=True))['status'] != 'deleted'
    pull(ctx, cid)
    assert [i['state'] for i in ctx.store.intents_for_event(record['id'])] == ['done']
    assert listed(ctx, day) == [] and free(ctx, day) == [('09:00', '19:00')]
    assert ctx.store.get_sync_state(cid)['cursor']
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    assert listed(ctx, day) == [] and listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]


def test_an_offline_delete_is_settled_when_the_organizer_cancels_the_date_first():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    server.put(instance('weekly', slot))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    server.down = True
    assert json.loads(tools.cal_delete(ctx, id=record['id'], confirm=True))['status'] == 'pending'
    server.down = False
    reconnected = len(server.calls)
    server.put(cancelled('weekly', slot))   # a new ETag: our If-Match DELETE would be answered 412
    pull(ctx, cid)
    run_open_intents(ctx)
    assert [i['state'] for i in ctx.store.intents_for_event(record['id'])] == ['done']
    assert not [call for call in server.calls[reconnected:] if call[0] == 'DELETE']
    assert listed(ctx, day) == []


class ScriptedFeed(FeedGoogle):
    """The next incremental read returns exactly ``script`` (one item may appear twice, as across pages)."""

    def __init__(self):
        super().__init__()
        self.script = None

    def _apply(self, method, path, params, body):
        parts = path.split('/')
        if self.script is not None and method == 'GET' and len(parts) == 4 and params.get('syncToken'):
            items, self.script, self.changed = self.script, None, []
            self.listings.append(dict(params))
            return 200, {}, {'items': items, 'nextSyncToken': f'token-{len(self.listings)}'}
        return super()._apply(method, path, params, body)


def scripted_feed():
    ctx, cid, _ = google_feed()
    server = ScriptedFeed()
    adapter = server.attach(GoogleAdapter('owner@example.test', 'token', 'refresh', now_utc() + timedelta(days=1), 'client', '', None))
    calendar = ctx.store.get_calendar(cid)
    adapter.list_calendars = lambda: [calendar]
    ctx.providers = OneAdapterProviders(adapter)
    return ctx, cid, server


def test_a_later_move_or_cancellation_of_the_same_date_in_one_read_is_not_lost():
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    item = server.put(instance('weekly', slot))
    pull(ctx, cid)
    moved = {**item, 'start': timed(slot + timedelta(hours=3)), 'end': timed(slot + timedelta(hours=4)), 'etag': '"m1"'}
    server.script = [moved, {**cancelled('weekly', slot), 'etag': '"m2"'}]
    pull(ctx, cid)
    assert listed(ctx, day) == [] and ctx.store.get_sync_state(cid)['cursor']
    server.script = [{**moved, 'etag': '"m3"'}, {**moved, 'start': timed(slot + timedelta(hours=5)),
                                                 'end': timed(slot + timedelta(hours=6)), 'etag': '"m4"', 'status': 'confirmed'}]
    pull(ctx, cid)
    assert listed(ctx, day) == [('Weekly sync', '16:00', '17:00')]   # the organizer restored it: the later hour wins


def test_a_series_cancelled_in_the_same_read_keeps_the_date_still_received():
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    joined = server.put(instance('weekly', slot, slot + timedelta(hours=2)))
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    record = ctx.store.find_by_external(cid, external_id='weekly')
    assert len(ctx.store.exceptions_for(record['id'])) == 1
    later = instance('weekly', slot + timedelta(days=7))
    server.script = [{'id': 'weekly', 'status': 'cancelled'}, {**joined, 'etag': '"j2"'}, {**later, 'etag': '"l1"'}]
    pull(ctx, cid)
    assert ctx.store.find_by_external(cid, external_id='weekly').get('deleted_at') or \
        ctx.store.get_event(record['id']) is None
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    assert listed(ctx, day + timedelta(days=14)) == []


def test_a_stored_date_in_the_slot_of_a_waiting_record_keeps_both_and_reports_it():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    server.put(instance('weekly', slot))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    server.down = True
    assert update(ctx, id=record['id'], reminders=[5])['status'] == 'pending'
    server.down = False
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    # A stored override in that slot (no tool creates one: the slot's address reaches the record) is not merged away.
    ctx.store.insert_event({'calendar_id': cid, 'uid': master['uid'], 'master_id': master['id'], 'recurrence_id': ops.iso_utc(slot),
                            'title': 'Stored', 'start_utc': ops.iso_utc(slot), 'end_utc': ops.iso_utc(slot + timedelta(hours=1)),
                            'tz': 'UTC', 'origin': 'external'})
    run_open_intents(ctx)
    report = pull(ctx, cid)
    assert any('name the same slot' in note for note in report.get('parse_errors', []))
    assert [r['id'] for r in ctx.store.held_instances(cid)] == [record['id']]
    assert sorted(t[0] for t in listed(ctx, day)) == ['Stored', 'Weekly sync']


def test_listing_reads_each_kept_record_once_and_no_series_again():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    for n in range(6):
        server.put(series(f'series{n}', day - timedelta(days=7) + timedelta(hours=8 + n), 'FREQ=DAILY;COUNT=30'))
    server.put(instance('weekly', day + timedelta(hours=16)))
    server.put(instance('weekly', day + timedelta(days=1, hours=16)))
    pull(ctx, cid)
    counts = {'native': 0, 'get_event': 0}
    store_type = type(ctx.store)  # also observe reads through the transaction's Store view
    original_native, original_get = ops.native_instance, store_type.get_event
    def native(row):
        counts['native'] += 1
        return original_native(row)
    def get_event(store, event_id):
        counts['get_event'] += 1
        return original_get(store, event_id)
    ops.native_instance, store_type.get_event = native, get_event
    try:
        assert len(listed(ctx, day)) == 7
    finally:
        ops.native_instance, store_type.get_event = original_native, original_get
    assert counts == {'native': 2, 'get_event': 0}


def test_series_address_of_a_joined_date_with_scope_this_assigns_only_that_date():
    ctx, cid, server = google_feed()
    busy = add_local_calendar(ctx.store, 'busy', publish_mode='busy')
    wide = add_local_calendar(ctx.store, 'wide', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert ctx.store.get_event(record['id'])['master_id'] == master['id']   # joined: a physical exception exists
    address = master['id'] + '@' + ops.iso_utc(original)
    assert update(ctx, id=address, scope='this', calendars=[cid, busy])['status'] == 'updated'
    assert [t[1:] for t in listed(ctx, day, calendars=[busy])] == [('13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[busy]) == []      # not the neighbouring week at 11:00
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[cid]) == [('Weekly sync', '11:00', '12:00')]
    # Explicit scope=all still assigns the series.
    assert update(ctx, id=master['id'], scope='all', calendars=[cid, wide])['status'] == 'updated'
    assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[wide])] == [('11:00', '12:00')]
    assert [t[1:] for t in listed(ctx, day, calendars=[wide])] == [('13:00', '14:00')]


def test_owner_move_of_a_kept_date_reaches_its_own_copy_and_the_series_copy_that_carries_it():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='full')
    shared = add_local_calendar(ctx.store, 'shared', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, shared])['status'] == 'updated'
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], calendars=[cid, shared, mirror], scope='all')['status'] == 'updated'
    for calendar in (cid, mirror, shared):
        assert [t[1:] for t in listed(ctx, day, calendars=[calendar])] == [('13:00', '14:00')]
    moved = update(ctx, id=record['id'], start=(day + timedelta(hours=15)).isoformat(), end=(day + timedelta(hours=16)).isoformat())
    assert moved['status'] == 'updated'
    for _ in range(2):
        for calendar in (cid, mirror, shared):
            assert [t[1:] for t in listed(ctx, day, calendars=[calendar])] == [('15:00', '16:00')]
        assert listed(ctx, day, calendars=[mirror])[0][0] == 'Weekly sync'          # full copy keeps content
        assert listed(ctx, day + timedelta(days=7), calendars=[mirror]) == [('Weekly sync', '11:00', '12:00')]
        pull(ctx, cid)
        run_open_intents(ctx)


# ── Review round 2: the received date's own reading of its slot, and only that date's ─────────────────────────────

def writes_since(server, since):
    return [(method, path.rsplit('/', 1)[-1]) for method, path, _, _ in server.calls[since:] if method != 'GET']


def _waiting_with_series_copies(ctx, cid, server, day, own='shared', carriers=('mirror',)):
    """A received date moved 11:00 → 13:00 with its own copy in ``own``; its series then arrives and is copied to
    ``own`` and ``carriers`` (the carriers show the date through the series copy)."""
    cals = {name: add_local_calendar(ctx.store, name, publish_mode='full') for name in (own, *carriers)}
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, cals[own]])['status'] == 'updated'
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], calendars=[cid, *cals.values()], scope='all')['status'] == 'updated'
    for calendar in (cid, *cals.values()):
        assert [t[1:] for t in listed(ctx, day, calendars=[calendar])] == [('13:00', '14:00')]
    return record, original, master, cals


@pytest.mark.parametrize('arrival', ['with_its_series', 'alone_then_series'])
def test_unchecking_a_series_calendar_keeps_2_1_0_for_ordinary_overrides_and_is_refused_only_for_received_dates(arrival):
    ctx, cid, server = google_feed()
    work = add_local_calendar(ctx.store, 'work', publish_mode='busy')
    extra = add_local_calendar(ctx.store, 'extra', publish_mode='busy')
    day = _day(5, 0)
    original = day + timedelta(hours=11)
    moved = instance('weekly', original, original + timedelta(hours=2))
    if arrival == 'with_its_series':      # an ordinary moved date of a series this calendar holds
        server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=4'))
        server.put(moved)
        pull(ctx, cid)
    else:                                 # a date received without its series
        server.put(moved)
        pull(ctx, cid)
        server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=4'))
        pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    (date,) = ctx.store.exceptions_for(master['id'])
    assert update(ctx, id=master['id'], calendars=[cid, work], scope='all')['status'] == 'updated'
    answer = update(ctx, id=date['id'], calendars=[cid])   # uncheck Work on the moved date's card (scope=this)
    if arrival == 'with_its_series':
        assert answer['status'] == 'updated'                # 2.1.0: an occurrence card's calendars are its series'
        assert listed(ctx, day, calendars=[work]) == [] and listed(ctx, day + timedelta(days=7), calendars=[work]) == []
        assert update(ctx, id=date['id'], calendars=[cid, extra])['status'] == 'updated'
        assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[extra])] == [('11:00', '12:00')]
        assert ctx.store.get_event(date['id'])['master_id'] == master['id']    # never detached
    else:
        assert answer['status'] == 'failed' and 'scope=all' in answer['reassign']['failed']   # not a silent «updated»
        for when, hour in ((day, '13:00'), (day + timedelta(days=7), '11:00')):
            assert [t[1] for t in listed(ctx, when, calendars=[work])] == [hour]
        # Only that action is refused: the date's own calendar, then the series' calendars, still change.
        assert update(ctx, id=date['id'], calendars=[cid, work, extra])['status'] == 'updated'
        assert [t[1:] for t in listed(ctx, day, calendars=[extra])] == [('13:00', '14:00')]
        assert listed(ctx, day + timedelta(days=7), calendars=[extra]) == []
        assert update(ctx, id=master['id'], calendars=[cid], scope='all')['status'] == 'updated'
        assert listed(ctx, day + timedelta(days=7), calendars=[work]) == []
    assert json.loads(tools.cal_delete(ctx, id=master['id'], scope='all', confirm=True))['status'] == 'deleted'
    for calendar in (cid, work, extra):
        assert listed(ctx, day, calendars=[calendar]) == [] and listed(ctx, day + timedelta(days=7), calendars=[calendar]) == []


def test_series_address_of_a_waiting_date_takes_its_times_and_preview_from_that_date():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, original, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    address = master['id'] + '@' + ops.iso_utc(original)
    preview = json.loads(tools.cal_delete(ctx, id=address))
    # The date, its own copy in Shared, and the following series copy in Mirror that carries its slot.
    assert {a['calendar_id'] for a in preview['affected']} == {cid, cals['shared'], cals['mirror']}
    assert record['id'] in {a['event_id'] for a in preview['affected']} and master['id'] not in {a['event_id'] for a in preview['affected']}
    assert update(ctx, id=address, duration_min=30)['status'] == 'updated'
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '13:30')]   # not back at the hour it left
    assert update(ctx, id=address, start=(day + timedelta(hours=15)).isoformat())['status'] == 'updated'
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '15:00', '15:30')]
    run_open_intents(ctx)
    for calendar in cals.values():
        assert [t[1:] for t in listed(ctx, day, calendars=[calendar])] == [('15:00', '15:30')]
    assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[cid])] == [('11:00', '12:00')]


@pytest.mark.parametrize('how', ['owner_deletes_the_date', 'google_deletes_the_date'])
def test_a_removed_waiting_date_leaves_no_series_copy_busy(how):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, original, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    if how == 'owner_deletes_the_date':
        assert json.loads(tools.cal_delete(ctx, id=record['id'], confirm=True))['status'] == 'deleted'
    else:                                  # Google's deleted form: the ID and status only
        server.put({'id': record['external_id'], 'status': 'cancelled'})
        pull(ctx, cid)
    for _ in range(2):
        run_open_intents(ctx)
        pull(ctx, cid)
        for calendar in (cid, *cals.values()):
            assert listed(ctx, day, calendars=[calendar]) == []
        assert free(ctx, day) == [('09:00', '19:00')]
    assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[cals['mirror']])] == [('11:00', '12:00')]
    assert json.loads(tools.cal_events(ctx, id=record['id']))['status'] == 'not_found'


@pytest.mark.parametrize('scope', ['all', 'following'])
def test_deleting_the_series_deletes_its_waiting_date_too(scope):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, original, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    address = master['id'] if scope == 'all' else master['id'] + '@' + ops.iso_utc(original - timedelta(days=7))
    assert json.loads(tools.cal_delete(ctx, id=address, scope=scope, confirm=True))['status'] == 'deleted'
    run_open_intents(ctx)
    pull(ctx, cid)
    for calendar in (cid, *cals.values()):
        for when in (day, day + timedelta(days=7)):
            assert listed(ctx, when, calendars=[calendar]) == []
    assert free(ctx, day) == [('09:00', '19:00')]


def test_moving_and_splitting_the_series_carry_the_slot_of_its_waiting_date():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, mirror])['status'] == 'updated'
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    first = original - timedelta(days=7)
    moved = update(ctx, id=master['id'] + '@' + ops.iso_utc(first), scope='all',
                   start=(first + timedelta(hours=1)).isoformat(), end=(first + timedelta(hours=2)).isoformat())
    assert moved['status'] == 'updated'
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]      # no series date at 12:00 beside it
    assert listed(ctx, day + timedelta(days=7), calendars=[cid]) == [('Weekly sync', '12:00', '13:00')]
    split = update(ctx, id=master['id'] + '@' + ops.iso_utc(first + timedelta(hours=1)), scope='following', title='Renamed')
    assert split['status'] in ('updated', 'pending')
    run_open_intents(ctx)
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]      # still once, at its own hour
    assert listed(ctx, day + timedelta(days=7), calendars=[cid]) == [('Renamed', '12:00', '13:00')]


def test_restoring_or_withdrawing_a_series_slot_reaches_only_following_copies():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, original, master, cals = _waiting_with_series_copies(ctx, cid, server, day, carriers=('mirror', 'other'))
    slot = ops.iso_utc(original)
    # The owner makes the series copy in Other independent: that date of it now meets at 17:00.
    group = ctx.store.get_event(master['id'])['link_group_id']
    other_copy = next(m for m in ctx.store.group_masters(group) if m['calendar_id'] == cals['other'])
    assert update(ctx, id=other_copy['id'] + '@' + slot, start=(day + timedelta(hours=17)).isoformat(),
                  end=(day + timedelta(hours=18)).isoformat())['status'] == 'updated'
    assert ctx.store.get_event(other_copy['id'])['link_state'] == 'diverged'
    # The date gets its own copy in Mirror and Other: Mirror's series copy hands the date over, Other keeps its 17:00.
    assert update(ctx, id=record['id'], calendars=[cid, cals['shared'], cals['mirror'], cals['other']])['status'] == 'updated'
    run_open_intents(ctx)
    assert [t[1:] for t in listed(ctx, day, calendars=[cals['mirror']])] == [('13:00', '14:00')]
    assert ('17:00', '18:00') in [t[1:] for t in listed(ctx, day, calendars=[cals['other']])]
    # Its own copy leaves Mirror again: Mirror's series copy carries the date at its hour once more.
    assert update(ctx, id=record['id'], calendars=[cid, cals['shared'], cals['other']])['status'] == 'updated'
    run_open_intents(ctx)
    assert [t[1:] for t in listed(ctx, day, calendars=[cals['mirror']])] == [('13:00', '14:00')]
    assert ('17:00', '18:00') in [t[1:] for t in listed(ctx, day, calendars=[cals['other']])]


def test_a_kept_date_joins_its_series_once_its_last_own_copy_is_gone():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, mirror])['status'] == 'updated'
    assert update(ctx, id=record['id'], calendars=[cid])['status'] == 'updated'
    assert ctx.store.get_event(record['id'])['link_group_id']           # the group label outlives its last copy
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert ctx.store.held_instances(cid) == [] and ctx.store.get_event(record['id'])['master_id'] == master['id']
    assert listed(ctx, day, calendars=[mirror]) == [] and listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]


def test_choosing_another_copy_as_the_series_source_does_not_widen_one_date():
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='full')
    third = add_local_calendar(ctx.store, 'third', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], calendars=[cid, mirror], scope='all')['status'] == 'updated'
    copy = next(m for m in ctx.store.group_masters(ctx.store.get_event(master['id'])['link_group_id']) if m['calendar_id'] == mirror)
    assert json.loads(tools.cal_update(ctx, id=copy['id'], link_action='source', confirm=True))['status'] == 'ok'
    assert update(ctx, id=record['id'], calendars=[cid, mirror, third])['status'] == 'updated'
    assert [t[1:] for t in listed(ctx, day, calendars=[third])] == [('13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[third]) == []
    assert listed(ctx, day + timedelta(days=7), calendars=[cid]) == [('Weekly sync', '11:00', '12:00')]


def test_a_date_its_organizer_cancelled_takes_no_edit_and_no_new_calendar():
    ctx, cid, server = google_feed()
    other = add_local_calendar(ctx.store, 'other', publish_mode='busy')
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    server.put(instance('weekly', slot))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    server.put(cancelled('weekly', slot))
    pull(ctx, cid)
    calls = len(server.calls)
    answers = [json.loads(tools.cal_update(ctx, id=record['id'], confirm=True, **kwargs))['status']
               for kwargs in ({'calendars': [cid, other]}, {'title': 'Stale card'})]
    assert listed(ctx, day, calendars=[other]) == [] and listed(ctx, day) == []   # no meeting appears anywhere
    assert answers == ['error', 'error'] and writes_since(server, calls) == []


def test_the_series_and_a_sparse_cancellation_of_its_received_date_in_one_read():
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    item = server.put(instance('weekly', slot, slot + timedelta(hours=2)))
    pull(ctx, cid)
    (record,) = ctx.store.held_instances(cid)
    server.script = [{**series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'), 'etag': '"s1"'},
                     {'id': item['id'], 'status': 'cancelled', 'etag': '"c1"'}]
    pull(ctx, cid)
    assert listed(ctx, day) == [] and free(ctx, day) == [('09:00', '19:00')]
    assert listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]
    assert json.loads(tools.cal_events(ctx, id=record['id']))['status'] == 'not_found'
    pull(ctx, cid)
    assert listed(ctx, day) == []


def test_a_move_then_a_sparse_cancellation_of_one_date_in_one_read_cancels_it():
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    item = server.put(instance('weekly', slot))
    pull(ctx, cid)
    moved = {**item, 'start': timed(slot + timedelta(hours=3)), 'end': timed(slot + timedelta(hours=4)), 'etag': '"m1"'}
    server.script = [moved, {'id': item['id'], 'status': 'cancelled', 'etag': '"m2"'}]
    pull(ctx, cid)
    assert listed(ctx, day) == [] and free(ctx, day) == [('09:00', '19:00')] and ctx.store.get_sync_state(cid)['cursor']
    pull(ctx, cid)
    assert listed(ctx, day) == []


def test_a_series_gone_from_a_full_listing_after_a_failed_lookup_keeps_its_received_date():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    server.put(instance('weekly', slot, slot + timedelta(hours=2)))
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    del server.events['weekly']   # the series left Google; the received date is still listed
    lookups, apply = [], server._apply
    def flaky(method, path, params, body):
        if method == 'GET' and path.endswith('/events/weekly'):
            lookups.append(path)
            if len(lookups) == 1:
                raise ops.ProviderError('network', 'lookup timed out', unsent=True)
        return apply(method, path, params, body)
    server._apply = flaky
    for _ in range(2):
        ctx.store.set_sync_state(cid, cursor='')
        pull(ctx, cid)
        assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]
        assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    assert len(lookups) == 2 and ctx.store.find_by_external(cid, external_id='weekly')['deleted_at']
    assert listed(ctx, day + timedelta(days=7)) == []


def test_an_owner_move_accepted_during_a_read_that_ends_the_series_is_not_undone():
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    item = server.put(instance('weekly', slot))
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    (date,) = ctx.store.exceptions_for(master['id'])
    server.script = [{'id': 'weekly', 'status': 'cancelled', 'etag': '"x1"'}, {**item, 'etag': '"stale"'}]
    adapter = ctx.providers.adapter
    fetch = adapter.fetch
    def slow_fetch(*args, **kwargs):
        out = fetch(*args, **kwargs)   # Google answered before the owner's move below was accepted
        moved = update(ctx, id=date['id'], start=(slot + timedelta(hours=3)).isoformat(), end=(slot + timedelta(hours=4)).isoformat())
        assert moved['status'] == 'updated'
        return out
    adapter.fetch = slow_fetch
    pull(ctx, cid)
    adapter.fetch = fetch
    assert listed(ctx, day) == [('Weekly sync', '14:00', '15:00')]
    assert ctx.store.get_sync_state(cid)['cursor'] == ''   # the next read is a full listing that confirms it


# ── Review round 2, continued: a CalDAV destination of the series' copy, against the in-memory CalDAV server ──

from test_calendar_provider_divergence import FakeCalDAV   # noqa: E402
from test_calendar_round2 import add_external_calendar     # noqa: E402
from providers import YandexAdapter                        # noqa: E402


class AccountAdapters:
    def __init__(self, by_account):
        self.by_account = by_account

    def adapter_for(self, account_id):
        return self.by_account[account_id]


def _wire(dav, href_part):
    """(EXDATE values, [(RECURRENCE-ID, DTSTART)]) of the CalDAV resource whose href contains ``href_part``."""
    from providers import _ical
    out = []
    for href, (text, _) in dav.resources.items():
        if href_part in href:
            cal = _ical().Calendar.from_ical(text)
            for comp in cal.walk('VEVENT'):
                out.append(comp)
    exdates = sorted(ops.iso_utc(d.dt) for comp in out if comp.get('EXDATE') is not None
                     for prop in (comp['EXDATE'] if isinstance(comp['EXDATE'], list) else [comp['EXDATE']]) for d in prop.dts)
    overrides = sorted((ops.iso_utc(comp.decoded('RECURRENCE-ID')), ops.iso_utc(comp.decoded('DTSTART')))
                       for comp in out if comp.get('RECURRENCE-ID') is not None)
    return exdates, overrides


def test_a_caldav_copy_of_the_series_hands_the_date_to_its_own_copy_and_back_on_the_wire():
    ctx, cid, server = google_feed()
    dav = FakeCalDAV()
    work = add_external_calendar(ctx.store, 'work', publish_mode='full')
    ctx.providers = AccountAdapters({ctx.store.get_calendar(cid)['account_id']: ctx.providers.adapter,
                                     ctx.store.get_calendar(work)['account_id']: dav.attach(YandexAdapter('round2@example.test', 'app-password'))})
    shared = add_local_calendar(ctx.store, 'shared', publish_mode='full')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    slot, cancelled_week = ops.iso_utc(original), ops.iso_utc(original + timedelta(days=7))
    assert update(ctx, id=record['id'], calendars=[cid, shared])['status'] == 'updated'   # it waits: its own copy
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=4'))
    server.put(cancelled('weekly', original + timedelta(days=7)))                      # another date, cancelled
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], calendars=[cid, shared, work], scope='all')['status'] in ('updated', 'pending')

    def reread():
        run_open_intents(ctx, 3)
        cal = ctx.store.get_calendar(work)
        worker.reconcile_calendar(ctx.store, ctx.providers, ctx.providers.adapter_for(cal['account_id']), cal, ctx.tz, '')

    def work_days():
        return [[t[1:] for t in listed(ctx, day + timedelta(days=d), calendars=[work])] for d in (0, 7, 14)]

    reread()
    assert work_days() == [[('13:00', '14:00')], [], [('11:00', '12:00')]]
    assert _wire(dav, 'weekly') == ([cancelled_week], [(slot, ops.iso_utc(day + timedelta(hours=13)))])
    # The date gets its own copy in Work. Its creation fails first: until the provider has the replacement, the
    # series copy keeps the date busy there.
    put = dav._apply
    def refuse_creates(method, url, body, headers):
        if method == 'PUT' and headers.get('If-None-Match') == '*':
            raise ops.ProviderError('network', 'create lost', unsent=True)
        return put(method, url, body, headers)
    dav._apply = refuse_creates
    assert update(ctx, id=record['id'], calendars=[cid, shared, work])['status'] in ('pending', 'updated_partially')
    run_open_intents(ctx, 3)
    assert not [h for h in dav.resources if 'kept-' in h]
    assert _wire(dav, 'weekly')[0] == [cancelled_week]                     # the series copy's date still stands
    dav._apply = put
    reread()
    assert work_days() == [[('13:00', '14:00')], [], [('11:00', '12:00')]]
    assert _wire(dav, 'weekly')[0] == sorted([slot, cancelled_week]) and len([h for h in dav.resources if 'kept-' in h]) == 1
    # Its own copy leaves Work: the series copy carries the date at its hour again, on the wire as well.
    assert update(ctx, id=record['id'], calendars=[cid, shared])['status'] in ('updated', 'pending')
    reread()
    reread()
    assert work_days() == [[('13:00', '14:00')], [], [('11:00', '12:00')]]
    assert not [h for h in dav.resources if 'kept-' in h]
    assert _wire(dav, 'weekly') == ([cancelled_week], [(slot, ops.iso_utc(day + timedelta(hours=13)))])


def test_a_joined_date_its_organizer_cancelled_takes_no_edit_on_either_address_while_the_series_still_does():
    ctx, cid, server = google_feed()
    other = add_local_calendar(ctx.store, 'other', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert ctx.store.get_event(record['id'])['master_id'] == master['id']          # joined
    server.put(cancelled('weekly', original))
    pull(ctx, cid)
    assert ctx.store.get_event(record['id'])['status'] == 'cancelled' and listed(ctx, day) == []
    calls = len(server.calls)
    answers = [json.loads(tools.cal_update(ctx, id=address, confirm=True, **kwargs))['status']
               for address in (record['id'], master['id'] + '@' + ops.iso_utc(original))
               for kwargs in ({'calendars': [cid, other]}, {'title': 'Stale card'}, {'reminders': [5]})]
    assert listed(ctx, day, calendars=[other]) == [] and listed(ctx, day) == []
    assert answers == ['error'] * 6 and writes_since(server, calls) == []
    # The series itself still takes its calendars (scope=all), without bringing that date back.
    assert update(ctx, id=master['id'], calendars=[cid, other], scope='all')['status'] == 'updated'
    assert listed(ctx, day, calendars=[other]) == []
    assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[other])] == [('11:00', '12:00')]


@pytest.mark.parametrize('scope', ['this', 'all', 'following'])
def test_the_delete_preview_names_exactly_the_calendars_the_delete_changes(scope):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, original, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    address = {'this': master['id'] + '@' + ops.iso_utc(original), 'all': master['id'],
               'following': master['id'] + '@' + ops.iso_utc(original - timedelta(days=7))}[scope]
    calendars = (cid, *cals.values())
    def snapshot():
        return {c: [listed(ctx, day + timedelta(days=d), calendars=[c]) for d in (0, 7)] for c in calendars}
    before = snapshot()
    preview = json.loads(tools.cal_delete(ctx, id=address, scope=scope))
    assert preview['status'] == 'needs_confirm' and before == snapshot()           # a preview changes nothing
    assert json.loads(tools.cal_delete(ctx, id=address, scope=scope, confirm=True))['status'] == 'deleted'
    after = snapshot()
    assert {a['calendar_id'] for a in preview['affected']} == {c for c in calendars if before[c] != after[c]}
    assert record['id'] in {a['event_id'] for a in preview['affected']}         # the waiting date itself is named


def test_a_calendar_edit_through_the_series_address_answers_with_the_date_it_addressed():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, original, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    extra = add_local_calendar(ctx.store, 'extra', publish_mode='busy')
    group = ctx.store.get_event(record['id'])['link_group_id']
    own = sorted(m['calendar_id'] for m in ctx.store.group_masters(group))
    out = update(ctx, id=master['id'] + '@' + ops.iso_utc(original), calendars=own + [extra])
    assert out['status'] == 'updated' and out['event']['id'] == record['id'] and out['event']['start'][11:16] == '13:00'
    assert [t[1:] for t in listed(ctx, day, calendars=[extra])] == [('13:00', '14:00')]


def test_after_an_older_token_a_failed_lookup_of_the_missing_series_is_repeated_by_the_next_ordinary_sync():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    server.put(instance('weekly', slot, slot + timedelta(hours=2)))
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    # The state an older version left: its token kind; the series has meanwhile left Google, the date has not.
    ctx.store.set_sync_state(cid, cursor='token-from-2.1.0', cursor_kind='google_sync_token')
    del server.events['weekly']
    lookups, apply = [], server._apply
    def flaky(method, path, params, body):
        if method == 'GET' and path.endswith('/events/weekly'):
            lookups.append(path)
            if len(lookups) == 1:
                raise ops.ProviderError('network', 'lookup timed out', unsent=True)
        return apply(method, path, params, body)
    server._apply = flaky
    listings = len(server.listings)
    pull(ctx, cid)                                   # the full listing that replaces the older token; lookup fails
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')] and ctx.store.get_sync_state(cid)['cursor'] == ''
    pull(ctx, cid)                                   # the next ordinary sync, cursor untouched: full again, lookup 404
    assert [('syncToken' in p) for p in server.listings[listings:]] == [False, False] and len(lookups) == 2
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')] and free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    assert listed(ctx, day + timedelta(days=7)) == [] and ctx.store.get_sync_state(cid)['cursor']
    pull(ctx, cid)                                   # decided: incremental from here
    assert server.listings[-1].get('syncToken') and len(lookups) == 2
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]


def test_a_bound_date_after_a_following_split_and_googles_end_of_its_old_instance():   # own copy only
    ctx, cid, server = google_feed()
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    day = _day(5, 0)
    record, original = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], calendars=[cid, mirror])['status'] == 'updated'      # it waits, bound
    server.put(series('weekly', original - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    first = original - timedelta(days=7)
    split = update(ctx, id=master['id'] + '@' + ops.iso_utc(first), scope='following', title='Renamed')
    assert split['status'] in ('updated', 'pending')
    run_open_intents(ctx, 3)
    pull(ctx, cid)
    bound = ctx.store.get_event(record['id'])
    assert bound['kept_master'] and bound['kept_master'] != master['id']                  # the new series holds its slot
    assert listed(ctx, day, calendars=[cid]) == [('Weekly sync', '13:00', '14:00')]
    assert [t[1:] for t in listed(ctx, day, calendars=[mirror])] == [('13:00', '14:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[cid]) == [('Renamed', '11:00', '12:00')]
    # The next ordinary read: Google ends the old series' instance past its new end.
    server.put(cancelled('weekly', original))
    pull(ctx, cid)
    run_open_intents(ctx, 2)
    assert listed(ctx, day, calendars=[cid]) == [('Renamed', '11:00', '12:00')]          # the new series' own date, once
    assert listed(ctx, day, calendars=[mirror]) == []
    assert ctx.store.get_event(record['id'])['kept_master'] in ('', master['id'])        # no longer the new series'
    assert free(ctx, day) == [('09:00', '11:00'), ('12:00', '19:00')]
    assert json.loads(tools.cal_update(ctx, id=record['id'], title='Again', confirm=True))['status'] == 'error'


@pytest.mark.parametrize('cancellation', ['structured', 'sparse', 'not_found'])
def test_a_bound_date_after_a_following_split_and_googles_end_of_its_old_instance_in_the_series_copies(cancellation):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, original, master, cals = _waiting_with_series_copies(ctx, cid, server, day, carriers=('mirror', 'other'))
    slot = ops.iso_utc(original)
    group = ctx.store.get_event(master['id'])['link_group_id']
    other_copy = next(m for m in ctx.store.group_masters(group) if m['calendar_id'] == cals['other'])
    assert update(ctx, id=other_copy['id'] + '@' + slot, start=(day + timedelta(hours=17)).isoformat(),
                  end=(day + timedelta(hours=18)).isoformat())['status'] == 'updated'   # Other is independent there
    assert update(ctx, id=master['id'] + '@' + ops.iso_utc(original - timedelta(days=7)), scope='following',
                  title='Renamed')['status'] in ('updated', 'pending')
    run_open_intents(ctx, 3)
    pull(ctx, cid)
    for calendar in (cid, cals['shared'], cals['mirror']):
        assert [t[1:] for t in listed(ctx, day, calendars=[calendar])] == [('13:00', '14:00')]
    old = cancelled('weekly', original)
    if cancellation == 'not_found':
        del server.events[old['id']]
        ctx.store.set_sync_state(cid, cursor='')
    elif cancellation == 'sparse':
        server.put({'id': old['id'], 'status': 'cancelled'})
    else:
        server.put(old)
    for _ in range(2):
        pull(ctx, cid)
        run_open_intents(ctx, 3)
        for calendar in (cid, cals['shared'], cals['mirror']):   # the new series' own date, once, in each following copy
            assert [t[1:] for t in listed(ctx, day, calendars=[calendar])] == [('11:00', '12:00')]
            assert [t[1:] for t in listed(ctx, day + timedelta(days=7), calendars=[calendar])] == [('11:00', '12:00')]
        assert ('17:00', '18:00') in [t[1:] for t in listed(ctx, day, calendars=[cals['other']])]
        assert free(ctx, day) == [('09:00', '11:00'), ('12:00', '17:00'), ('18:00', '19:00')]
