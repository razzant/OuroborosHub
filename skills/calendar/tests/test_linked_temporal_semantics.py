"""Linked copies retain temporal meaning through birth, source updates and explicit follow."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from test_calendar_temporal_roundtrip import context, floating_context
from test_calendar_provider_divergence import FakeCalDAV, FakeGoogle, add_google_calendar, run_open_intents
from test_calendar_atomic_replay import instance
from test_calendar_provider_divergence import _day
from test_calendar_round2 import OneAdapterProviders, add_external_calendar, add_local_calendar
from test_sync_atomicity import FeedAdapter
import links
import model
import ops
import providers
import providers_google
import routes
import tools
from scripts.worker import reconcile_calendar
from store import Store


def linked_context(tmp_path, representation, target, mode, *, override=False):
    ctx, source, _ = floating_context(tmp_path, representation)
    server = FakeCalDAV()
    server.resources[source['href']] = (source['raw_payload'], source['etag'])
    ctx.providers = OneAdapterProviders(server.attach(providers.YandexAdapter('fixture', 'fixture')))
    if override:
        key = model.iso_utc(datetime(2026, 10, 25, 0 if representation == 'date' else 9, tzinfo=ctx.tz))
        if representation == 'utc':
            key = '2026-10-25T07:00:00+00:00'
        changes = {'start': '2026-10-26', 'end': '2026-10-27'} if representation == 'date' else {
            'start': '2026-10-25T12:00:00+01:00', 'end': '2026-10-25T13:00:00+01:00'}
        edited = json.loads(tools.cal_update(ctx, id=source['id']+'@'+key, title='Override', confirm=True, **changes))
        assert edited['status'] == 'updated', edited
    if target == 'google':
        source_adapter = ctx.providers.adapter_for(source['account_id'])
        cid = add_google_calendar(ctx.store)
        ctx.store.update_calendar(cid, {'publish_mode': mode})
        server = FakeGoogle()
        adapter = server.attach(providers_google.GoogleAdapter('fixture', 'token', 'refresh', model.now_utc(), 'client', '', None))
        ctx.providers = SimpleNamespace(adapter_for=lambda aid: source_adapter if aid == source['account_id'] else adapter)
    else:
        cid = add_local_calendar(ctx.store, 'copy', mode) if target == 'local' else add_external_calendar(ctx.store, 'copy', mode)
    result = json.loads(tools.cal_update(ctx, id=source['id'], calendars=[source['calendar_id'], cid], scope='all', confirm=True))
    assert result['status'] == 'updated', result
    run_open_intents(ctx, 2)  # Overrides wait for the new master's provider identity.
    copied = ctx.store.get_event(result['reassign']['added'][0]['event_id'])
    return ctx, ctx.store.get_event(source['id']), copied, server


def assert_visible_times_match(ctx, source, copy):
    for zone in ('Europe/Berlin', 'America/New_York'):
        ctx.tz = model.get_tz(zone)
        result = json.loads(tools.cal_events(ctx, start='2026-10-24', end='2026-10-29'))
        times = {cid: sorted((row['start'], row['end']) for row in result['events'] if row['calendar_id'] == cid)
                 for cid in (source['calendar_id'], copy['calendar_id'])}
        assert times[source['calendar_id']] == times[copy['calendar_id']], (zone, times)


@pytest.mark.parametrize('representation', ['floating', 'tzid', 'date', 'utc'])
@pytest.mark.parametrize('target', ['local', 'caldav'])
@pytest.mark.parametrize('mode', ['full', 'busy'])
def test_copy_master_and_override_keep_time_basis(tmp_path, representation, target, mode):
    ctx, source, copy, server = linked_context(tmp_path, representation, target, mode, override=True)
    source_exc, = ctx.store.exceptions_for(source['id'])
    copy_exc, = ctx.store.exceptions_for(copy['id'])
    for original, duplicate in ((source, copy), (source_exc, copy_exc)):
        assert (duplicate['time_kind'], duplicate['tz']) == (original['time_kind'], original['tz'])
        assert (duplicate['start_utc'], duplicate['end_utc']) == (original['start_utc'], original['end_utc'])
        assert duplicate['uid'] == source['uid'] and duplicate['id'] != original['id']
        assert duplicate['attendees_json'] == duplicate['reminders_json'] == '[]'
        assert duplicate['title'] == (model.BUSY_COPY_TITLE if mode == 'busy' else original['title'])
    assert copy_exc['recurrence_id'] == source_exc['recurrence_id']
    if target == 'caldav':
        wire, etag = server.resources[copy['href']]
        observed = providers.ics_to_rows(wire, copy['calendar_id'], copy['href'], etag, ctx.tz)
        assert {row['time_kind'] for row in observed} == {source['time_kind']}
        assert {row['start_utc'] for row in observed} == {source['start_utc'], source_exc['start_utc']}
    ctx.store = Store(ctx.state_dir)
    assert_visible_times_match(ctx, source, copy)
    start = datetime(2026, 10, 25, tzinfo=ctx.tz)
    end = datetime(2026, 10, 26, tzinfo=ctx.tz)
    combined, _ = ops.busy_intervals(ctx.occurrences(start, end))
    source_only, _ = ops.busy_intervals(ctx.occurrences(start, end, [source['calendar_id']]))
    assert combined == source_only


@pytest.mark.parametrize('scope', ['this', 'all'])
@pytest.mark.parametrize('mode', ['full', 'busy'])
def test_explicit_follow_repairs_existing_copy_basis_without_readdressing(tmp_path, scope, mode):
    ctx, source, copy, _ = linked_context(tmp_path, 'floating', 'local', mode, override=True)
    source_exc, = ctx.store.exceptions_for(source['id'])
    copy_exc, = ctx.store.exceptions_for(copy['id'])
    # Existing copies can predate time_kind propagation; explicit follow must repair them.
    selected = [copy_exc] if scope == 'this' else [copy, copy_exc]
    for row in selected:
        ctx.store.update_event(row['id'], {'time_kind': '', 'link_state': links.DIVERGED})
    addressed = copy['id']+'@'+source_exc['recurrence_id'] if scope == 'this' else copy['id']
    result = json.loads(tools.cal_update(ctx, id=addressed, link_action='follow', scope=scope, confirm=True))
    assert result['status'] == 'ok', result
    for row in (copy, copy_exc):
        current = ctx.store.get_event(row['id'])
        assert current['time_kind'] == 'floating' and current['link_state'] == links.FOLLOWING
        for field in ('id', 'uid', 'master_id', 'recurrence_id', 'start_utc', 'end_utc'):
            assert current[field] == row[field]
    assert_visible_times_match(ctx, source, copy)


@pytest.mark.parametrize('target', ['local', 'caldav', 'google'])
def test_source_feed_kind_change_reaches_copy_and_retains_existing_intents(tmp_path, target):
    ctx, source, copy, _ = linked_context(tmp_path, 'tzid', target, 'full')
    before = ctx.store.intents_for_event(source['id'])
    incoming = {**source, 'time_kind': 'floating', 'etag': '"remote-floating"'}
    feed = FeedAdapter([incoming], seen=[source['href']])
    result = reconcile_calendar(ctx.store, ctx.providers, feed, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    assert result[2] == 1, result
    assert ctx.store.get_event(copy['id'])['time_kind'] == ('absolute' if target == 'google' else 'floating')
    assert ctx.store.intents_for_event(source['id']) == before
    if target != 'google':
        assert_visible_times_match(ctx, source, copy)


def test_copy_follow_keeps_offline_birth_identity_and_payload(tmp_path):
    ctx, source, _ = floating_context(tmp_path, 'floating')
    cid = add_external_calendar(ctx.store, 'offline', 'busy')
    ctx.providers = OneAdapterProviders(None)
    result = json.loads(tools.cal_update(ctx, id=source['id'], calendars=[source['calendar_id'], cid], scope='all', confirm=True))
    copy = ctx.store.get_event(result['reassign']['added'][0]['event_id'])
    birth, = ctx.store.intents_for_event(copy['id'])
    assert birth['state'] == 'pending'
    ctx.store.update_event(copy['id'], {'time_kind': '', 'link_state': links.DIVERGED})
    json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    current = ctx.store.get_event(copy['id'])
    assert current['time_kind'] == 'floating'
    assert (current['uid'], current['id']) == (copy['uid'], copy['id'])
    retained = next(intent for intent in ctx.store.intents_for_event(copy['id']) if intent['id'] == birth['id'])
    assert retained['payload_json'] == birth['payload_json'] and retained['state'] == birth['state']


@pytest.mark.parametrize('mode', ['full', 'busy'])
def test_google_copy_and_follow_keep_the_actual_zoned_wire(tmp_path, mode):
    ctx, source, copy, server = linked_context(tmp_path, 'floating', 'google', mode)
    ctx.tz = model.get_tz('America/New_York')
    calls = list(server.calls)
    visible = json.loads(tools.cal_events(ctx, start='2026-10-25', end='2026-10-26'))['events']
    times = {event['calendar_id']: event['start'] for event in visible}
    assert times[source['calendar_id']].endswith('09:00:00-04:00')
    assert times[copy['calendar_id']].endswith('04:00:00-04:00')
    assert server.calls == calls  # Owner-zone/display changes never move the provider event.
    availability = json.loads(tools.cal_free(ctx, start='2026-10-25T00:00', end='2026-10-26T00:00',
        duration_min=30, working_hours='00:00-23:59', max_slots=10))
    assert availability['availability_complete']
    for event in visible:
        start, end = model.parse_stored(event['start']), model.parse_stored(event['end'])
        assert not any(model.parse_stored(slot['start']) < end and start < model.parse_stored(slot['end'])
                       for slot in availability['slots'])
    assert server.calls == calls
    assert copy['time_kind'] == 'absolute'
    ctx.store.update_event(copy['id'], {'link_state': links.DIVERGED, 'time_kind': ''})
    result = json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    assert result['status'] == 'ok', result
    adapter = ctx.providers.adapter_for(copy['account_id'])
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(copy['calendar_id']), ctx.tz)
    saved = ctx.store.get_event(copy['id'])
    assert (saved['time_kind'], saved['link_state']) == ('absolute', links.FOLLOWING)
    wire = server.events[saved['external_id']]
    parsed = providers_google.gevent_to_row(wire, copy['calendar_id'], ctx.tz)
    assert parsed['start_utc'] == saved['start_utc'] and parsed['tz'] == saved['tz']


@pytest.mark.parametrize('source_kind', ['floating', 'absolute'])
def test_caldav_follow_changes_master_and_override_wire_kind(tmp_path, source_kind):
    ctx, source, copy, server = linked_context(tmp_path, 'tzid' if source_kind == 'floating' else 'floating', 'caldav', 'full', override=True)
    for row in [source] + ctx.store.exceptions_for(source['id']):
        ctx.store.update_event(row['id'], {'time_kind': source_kind})
    ctx.store.update_event(copy['id'], {'link_state': links.DIVERGED})
    result = json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    assert result['status'] == 'ok', result
    run_open_intents(ctx, 2)
    wire, etag = server.resources[copy['href']]
    parsed = providers.ics_to_rows(wire, copy['calendar_id'], copy['href'], etag, ctx.tz)
    assert len(parsed) == 2 and {row['time_kind'] for row in parsed} == {source_kind}
    for row in [copy] + ctx.store.exceptions_for(copy['id']):
        saved = ctx.store.get_event(row['id'])
        assert saved['time_kind'] == source_kind and saved['link_state'] == links.FOLLOWING
    assert_visible_times_match(ctx, source, copy)


@pytest.mark.parametrize('edited', [False, True])
def test_legacy_type_enrichment_alone_does_not_diverge_copy(tmp_path, edited):
    ctx, source, copy, server = linked_context(tmp_path, 'tzid', 'caldav', 'full')
    ctx.store.update_event(copy['id'], {'time_kind': ''})
    if edited:
        server.edit(copy['href'], lambda cal: cal.walk('VEVENT')[0].__setitem__('SUMMARY', 'Remote change'))
    adapter = ctx.providers.adapter_for(copy['account_id'])
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(copy['calendar_id']), ctx.tz)
    saved = ctx.store.get_event(copy['id'])
    assert saved['time_kind'] == 'absolute'
    assert saved['link_state'] == (links.DIVERGED if edited else links.FOLLOWING)
    assert ctx.store.get_event(source['id'])['title'] == source['title']


@pytest.mark.parametrize('representation,path,concurrent', [
    ('tzid', 'feed', ''), ('floating', 'feed', ''), ('tzid', 'follow', ''), ('tzid', 'create', ''),
    ('tzid', 'feed', 'before_read'), ('tzid', 'feed', 'after_read'),
])
def test_google_linked_occurrence_pins_wire_fields_and_etag(tmp_path, representation, path, concurrent):
    import icalendar
    ctx, source, copy, server = linked_context(tmp_path, representation, 'google', 'full')
    adapter = ctx.providers.adapter_for(source['account_id'])
    source_server = adapter._request.__self__
    calendar = ctx.store.get_calendar(source['calendar_id'])
    if path == 'create':
        created = json.loads(tools.cal_create(ctx, title='Created series', start='2026-10-24T09:00', duration_min=60,
            rrule='FREQ=DAILY;COUNT=5', calendars=[source['calendar_id'], copy['calendar_id']], confirm=True))
        assert created['status'] == 'created', created
        source = ctx.store.get_event(created['event']['id'])
        copy = next(row for row in ctx.store.group_masters(source['link_group_id']) if row['id'] != source['id'])
        reconcile_calendar(ctx.store, ctx.providers, adapter, calendar, ctx.tz)
        run_open_intents(ctx, 2)
        copy = ctx.store.get_event(copy['id'])
    slot = datetime(2026, 10, 25, 9, tzinfo=ctx.tz)
    key = model.iso_utc(slot)
    ext = copy['external_id'] + '_' + model.parse_stored(key).strftime('%Y%m%dT%H%M%SZ')
    server.events[ext] = {**server.events[copy['external_id']], 'id': ext, 'etag': server._etag(),
        'start': {'dateTime': slot.isoformat(), 'timeZone': 'Europe/Berlin'},
        'end': {'dateTime': (slot + timedelta(hours=1)).isoformat(), 'timeZone': 'Europe/Berlin'},
        'originalStartTime': {'dateTime': slot.isoformat()}, 'recurringEventId': copy['external_id']}
    server.events[ext].pop('recurrence', None)
    assert 'time_kind' not in providers_google.gevent_to_row(server.events[ext], copy['calendar_id'], ctx.tz)
    assert copy['time_kind'] == 'absolute'
    before_intents = ctx.store.intents_for_event(copy['id'])
    if concurrent == 'before_read':
        server.events[ext].update(summary='Another client', etag=server._etag())
    pinned_etag = server.events[ext]['etag']
    google = ctx.providers.adapter_for(copy['account_id'])
    request = google._request
    races = []
    def race(method, *args, **kwargs):
        if concurrent == 'after_read' and method == 'PATCH':
            races.append(True)
            server.events[ext].update(summary='Another client', etag=server._etag())
        return request(method, *args, **kwargs)
    google._request = race
    def moved_occurrence(cal):
        event = icalendar.Event()
        event.add('UID', source['uid'])
        event.add('SUMMARY', 'Moved at source')
        native_slot = slot.replace(tzinfo=None) if representation == 'floating' else slot
        event.add('RECURRENCE-ID', native_slot)
        event.add('DTSTART', native_slot + timedelta(hours=3))
        event.add('DTEND', native_slot + timedelta(hours=4))
        cal.add_component(event)
    source_server.edit(source['href'], moved_occurrence)
    if path == 'follow':
        ctx.store.update_event(copy['id'], {'link_state': links.DIVERGED})
    call_count = len(server.calls)
    reconcile_calendar(ctx.store, ctx.providers, adapter, calendar, ctx.tz)
    if path == 'follow':
        assert not ctx.store.exceptions_for(copy['id'])
        json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    run_open_intents(ctx, 2)
    child, = ctx.store.exceptions_for(copy['id'])
    intent, = ctx.store.intents_for_event(child['id'])
    assert child['recurrence_id'] == key and child['master_id'] == copy['id']
    assert child['link_group_id'] == source['link_group_id']
    assert all(ctx.store.get_intent(item['id']) == item for item in before_intents)
    assert (intent['state'], child['sync_state']) == (('conflict', 'conflict') if concurrent else ('done', 'synced'))
    assert server.events[ext]['summary'] == ('Another client' if concurrent else 'Moved at source')
    if concurrent == 'before_read':
        assert not any(call[0] == 'PATCH' for call in server.calls[call_count:])
    else:
        assert intent['expected_etag'] == pinned_etag
        assert bool(races) == (concurrent == 'after_read')
        if not concurrent:
            assert model.parse_stored(server.events[ext]['start']['dateTime']) == slot + timedelta(hours=3)


@pytest.mark.parametrize('availability', [model.AVAIL_BUSY, model.AVAIL_SOFT, model.AVAIL_FREE])
@pytest.mark.parametrize('shift_hours', [0, 6])
def test_linked_busy_dedup_keeps_distinct_intervals_and_availability(availability, shift_hours):
    start = datetime(2026, 11, 26, 9, tzinfo=model.get_tz('UTC'))
    end = start + timedelta(hours=1)
    source = {'link_group_id': 'group', 'occurrence_start_utc': model.iso_utc(start),
        'start_utc': model.iso_utc(start), 'end_utc': model.iso_utc(end), 'availability': availability, 'title': 'Meeting'}
    delta = timedelta(hours=shift_hours)
    copy = {**source, 'start_utc': model.iso_utc(start+delta), 'end_utc': model.iso_utc(end+delta)}
    cancelled = {**source, 'status': 'cancelled', 'start_utc': model.iso_utc(start-delta)}
    expected = [(start, end)] + ([(start+delta, end+delta)] if shift_hours else [])
    for rows in ([cancelled, source, copy], [cancelled, copy, source]):
        hard, soft = ops.busy_intervals(rows)
        assert hard == (expected if availability == model.AVAIL_BUSY else [])
        assert sorted(soft) == ([(s, e, 'Meeting') for s, e in expected] if availability == model.AVAIL_SOFT else [])


def google_first_write_context(tmp_path, *, hidden=False):
    ctx = context(tmp_path, 'UTC')
    cid = add_google_calendar(ctx.store)
    server = FakeGoogle()
    adapter = server.attach(providers_google.GoogleAdapter('owner@example.test', 'token', 'refresh',
        model.now_utc() + timedelta(days=1), 'client', '', None))
    ctx.providers = OneAdapterProviders(adapter)
    created = json.loads(tools.cal_create(ctx, title='Series', start=_day(2).isoformat(), duration_min=60,
        rrule='FREQ=DAILY;COUNT=5', calendars=[cid], description='Original details', location='Original room',
        attendees=['old@example.test'], reminders=[15], hidden=hidden, confirm=True))
    assert created['status'] == 'created', created
    # An accepted newer provider snapshot supplies canonical guest metadata.
    created_row = ctx.store.get_event(created['event']['id'])
    server.events[created_row['external_id']]['etag'] = server._etag()
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
    row = ctx.store.get_event(created['event']['id'])
    ext, addressed = instance(ctx, server, row['id'])
    return ctx, row, server, ext, addressed


@pytest.mark.parametrize('field,new_value,canonical,empty', [
    ('description', 'New details', 'description', ''), ('location', 'New room', 'location', ''),
    ('attendees', ['new@example.test'], 'attendees_json', '[]'), ('reminders', [30], 'reminders_json', '[]'),
])
@pytest.mark.parametrize('deleted_at_provider', [False, True])
def test_google_first_pin_retains_deleted_wire_field_conflicts(tmp_path, field, new_value, canonical, empty, deleted_at_provider):
    ctx, row, server, ext, addressed = google_first_write_context(tmp_path)
    assert row[canonical] != empty
    server.down = True
    result = json.loads(tools.cal_update(ctx, id=addressed, confirm=True, **{field: new_value}))
    assert result['status'] == 'pending', result
    server.down = False
    if deleted_at_provider:
        server.events[ext].pop(field)
        server.events[ext]['etag'] = server._etag()
    observed = providers_google.gevent_to_row(server.events[ext], row['calendar_id'], ctx.tz)
    assert canonical in observed  # Missing raw fields are empty values, not missing canonical fields.
    assert observed[canonical] == (empty if deleted_at_provider else row[canonical])
    expected_etag = server.events[ext]['etag']
    adapter = ctx.providers.adapter_for('')
    request, sent = adapter._request, []
    def record(method, path, params=None, body=None, headers=None, **kwargs):
        if method == 'PATCH':
            sent.append((dict(body), dict(headers)))
        return request(method, path, params, body, headers, **kwargs)
    adapter._request = record
    run_open_intents(ctx, 2)
    child, = ctx.store.exceptions_for(row['id'])
    intent, = ctx.store.intents_for_event(child['id'])
    assert (intent['state'], child['sync_state']) == (('conflict', 'conflict') if deleted_at_provider else ('done', 'synced'))
    if deleted_at_provider:
        assert not sent and field not in server.events[ext]
        assert not intent['expected_etag']
    else:
        assert len(sent) == 1 and sent[0][1] == {'If-Match': expected_etag}
        assert intent['expected_etag'] == expected_etag
        expected = ([{'email': 'new@example.test'}] if field == 'attendees' else
                    {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 30}]} if field == 'reminders' else new_value)
        assert sent[0][0][field] == server.events[ext][field] == expected


@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('remote_status', ['confirmed', 'omitted', 'cancelled'])
def test_google_first_pin_local_visibility_keeps_status_and_widget_filtering(tmp_path, hidden, remote_status):
    ctx, row, server, ext, addressed = google_first_write_context(tmp_path, hidden=not hidden)
    if remote_status == 'omitted':
        server.events[ext].pop('status')
    else:
        server.events[ext]['status'] = remote_status
    server.events[ext]['visibility'] = 'private'  # Google privacy is unrelated to local display hiding.
    expected_etag = server.events[ext]['etag']
    before = len(server.calls)
    result = json.loads(tools.cal_update(ctx, id=addressed, hidden=hidden, confirm=True))
    run_open_intents(ctx, 2)
    child, = ctx.store.exceptions_for(row['id'])
    intent, = ctx.store.intents_for_event(child['id'])
    assert child['visibility'] == ('hidden' if hidden else 'shown')
    assert ctx.store.get_event(row['id'])['visibility'] == row['visibility']
    assert server.events[ext]['visibility'] == 'private'
    if remote_status == 'cancelled':
        assert result['status'] == child['sync_state'] == intent['state'] == 'conflict'
        assert not any(call[0] == 'PATCH' for call in server.calls[before:])
    else:
        assert result['status'] == 'updated' and (child['sync_state'], intent['state']) == ('synced', 'done')
        assert intent['expected_etag'] == expected_etag
        # Exercise the widget's actual agenda projection without starting a browser.
        day = _day(3).date().isoformat()
        shown = routes.agenda_payload(ctx, 'day', day, [row['calendar_id']], True)
        normal = routes.agenda_payload(ctx, 'day', day, [row['calendar_id']], False)
        assert [event['id'] for event in shown['events']] == [child['id']]
        assert shown['events'][0]['visibility'] == child['visibility']
        assert [event['id'] for event in normal['events']] == ([] if hidden else [child['id']])
        assert bool(normal['hidden_busy']) == hidden
