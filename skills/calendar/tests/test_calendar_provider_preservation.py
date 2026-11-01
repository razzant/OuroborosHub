"""Provider copy defaults, complete page cursors and lossless unrelated alarm updates."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import urllib.request
from datetime import timedelta

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SKILL), str(SKILL / 'tests')]
import tools
import providers
import providers_google
from model import DEFAULT_LOCAL_CALENDAR_ID, BUSY_COPY_TITLE, TOOL_RESULT_LIMIT, get_tz, iso_utc, now_utc
from test_calendar_round2 import FakeAPI, OneAdapterProviders, add_external_calendar, make_context
from test_calendar_provider_divergence import FakeCalDAV, FakeGoogle, caldav_context, google_context, _day, sync

import accounts
import management


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('This reproduction permits no actual network request')
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(socket.socket, 'connect', refuse)


@pytest.fixture
def track():
    contexts = []
    def add(ctx):
        if os.environ.get('OUROBOROS_TEST_TEMP_ROOT'):
            assert Path(ctx.state_dir).resolve().is_relative_to(Path(os.environ['OUROBOROS_TEST_TEMP_ROOT']).resolve())
        contexts.append(ctx)
        ctx.tz = get_tz('UTC')
        return ctx
    yield add
    for ctx in contexts:
        shutil.rmtree(ctx.state_dir)


class CollectionDAV(FakeCalDAV):
    def _apply(self, method, url, body, headers):
        if method == 'MKCALENDAR':
            return 201, {}, ''
        return super()._apply(method, url, body, headers)


class CollectionGoogle(FakeGoogle):
    def _apply(self, method, path, params, body):
        if method == 'POST' and path == '/calendars':
            return 200, {}, {'id': 'created-calendar', **body}
        if method == 'GET' and path == '/users/me/calendarList':
            return 200, {}, {'items': [{'id': 'listed-calendar', 'summary': 'Work', 'accessRole': 'owner'}]}
        return super()._apply(method, path, params, body)


def external_copy(ctx, server, cid, title):
    result = json.loads(tools.cal_create(ctx, title=title, description='Private notes', location='Private room',
                                        start=_day(3).isoformat(), calendars='busy_set', confirm=True))
    assert result['status'] == 'created', result
    source = ctx.store.get_event(result['event']['id'])
    copy_row = next(row for row in ctx.store.group_masters(source['link_group_id']) if row['calendar_id'] == cid)
    if hasattr(server, 'resources'):
        body, _ = server.resources[copy_row['href']]
        event = next(event for event in providers._ical().Calendar.from_ical(body).walk('VEVENT') if not event.get('RECURRENCE-ID'))
        value = {'title': str(event.get('SUMMARY', '')), 'description': str(event.get('DESCRIPTION', '')), 'location': str(event.get('LOCATION', ''))}
    else:
        event = server.events[copy_row['external_id']]
        value = {'title': event.get('summary', ''), 'description': event.get('description', ''), 'location': event.get('location', '')}
    return value


def new_connection(ctx, path, monkeypatch):
    if path == 'connect_caldav':
        def discovery(adapter):
            return [{'id': adapter.account_id + ':work', 'account_id': adapter.account_id, 'provider': adapter.provider,
                     'external_id': 'work', 'href': adapter.home.rstrip('/') + '/work/', 'name': 'Work', 'writable': True}]
        monkeypatch.setattr(providers.CalDAVAdapter, 'list_calendars', discovery)
        result = accounts.connect_caldav(ctx, login='probe', password='synthetic-app-password',
                                         server_url='https://dav.example.test/calendars/', alias='Work')
        assert result['status'] == 'connected', result
        cid, account_id = result['calendars'][0]['id'], result['account_id']
        adapter = ctx.providers.adapter_for(account_id)
        server = FakeCalDAV()
        server.attach(adapter)
        return cid, server
    if path in ('create_caldav', 'refresh_caldav'):
        account_id = 'caldav:probe'
        ctx.store.upsert_account({'id': account_id, 'provider': 'caldav', 'login': 'probe'})
        adapter = providers.CalDAVAdapter('probe', 'synthetic-app-password', 'https://dav.example.test',
                                          account_id=account_id, home_url='https://dav.example.test/calendars/')
        server = CollectionDAV()
        server.attach(adapter)
    else:
        account_id = 'google:owner@example.test'
        ctx.store.upsert_account({'id': account_id, 'provider': 'google', 'login': 'owner@example.test'})
        server = CollectionGoogle()
        adapter = server.attach(providers_google.GoogleAdapter('owner@example.test', 'token', 'refresh', now_utc()+timedelta(days=1), 'client', '', None))
    ctx.providers = OneAdapterProviders(adapter)
    if path.startswith('refresh'):
        if path == 'refresh_caldav':
            adapter.list_calendars = lambda: [{'id': account_id+':listed', 'account_id': account_id, 'provider': 'caldav',
                                               'external_id': 'listed', 'href': adapter.home+'listed/', 'name': 'Work'}]
        result = json.loads(management.cal_calendars(ctx, account_id=account_id, refresh=True))
        assert result['status'] == 'ok', result
        return result['accounts'][0]['calendars'][0]['id'], server
    result = json.loads(management.cal_manage(ctx, action='create', account_id=account_id, changes={'name': 'Work'}, confirm=True))
    assert result['status'] == 'done', result
    return result['result']['id'], server


@pytest.mark.parametrize('path', ['connect_caldav', 'create_caldav', 'create_google', 'refresh_caldav', 'refresh_google'])
def test_new_external_default_matches_existing_busy_promise(track, monkeypatch, path):
    ctx = track(make_context())
    cid, server = new_connection(ctx, path, monkeypatch)
    initial = ctx.store.get_calendar(cid)
    assert not initial['role_publish']
    before = json.loads(tools.cal_create(ctx, title='Local only before selection', start=_day(2).isoformat(), calendars='busy_set', confirm=True))
    assert before['status'] == 'created'
    assert len(before['assignments']) == 1 and before['assignments'][0]['calendar_id'] == DEFAULT_LOCAL_CALENDAR_ID
    selected = json.loads(tools.cal_settings(ctx, action='set_calendar', calendar_id=cid, publish_busy=True, confirm=True))
    assert selected['status'] == 'updated'
    actual = external_copy(ctx, server, cid, 'Private appointment')
    assert actual == {'title': BUSY_COPY_TITLE, 'description': '', 'location': ''}


@pytest.mark.parametrize('mode', ['busy', 'full'])
def test_explicit_content_mode_is_independent_of_publication_membership(track, mode):
    ctx = track(make_context())
    cid = add_external_calendar(ctx.store, 'explicit', mode)
    server = FakeCalDAV()
    ctx.providers = OneAdapterProviders(server.attach(providers.YandexAdapter('round2@example.test', 'synthetic-app-password')))
    tools.cal_settings(ctx, action='set_calendar', calendar_id=cid, publish_busy=True, confirm=True)
    before = ctx.store.get_calendar(cid)
    refreshed = {key: value for key, value in before.items() if key not in ('publish_mode', 'role_publish', 'role_visible', 'role_busy', 'is_default')}
    ctx.store.upsert_calendar(refreshed)  # the common discovery write preserves explicit choices
    actual = external_copy(ctx, server, cid, 'Explicit appointment')
    assert ctx.store.get_calendar(cid)['publish_mode'] == mode
    assert actual == ({'title': BUSY_COPY_TITLE, 'description': '', 'location': ''} if mode == 'busy' else
                      {'title': 'Explicit appointment', 'description': 'Private notes', 'location': 'Private room'})


def test_legacy_yandex_discovery_keeps_existing_busy_default(track, monkeypatch):
    ctx = track(tools.Context(FakeAPI({'YANDEX_CALDAV_ACCOUNTS': json.dumps([{'login': 'probe@example.test', 'app_password': 'synthetic', 'alias': 'Probe'}])})))
    def discovery(adapter):
        return [{'id': 'yandex:probe@example.test:work', 'account_id': 'yandex:probe@example.test', 'provider': 'yandex',
                 'external_id': 'work', 'href': 'https://caldav.yandex.ru/calendars/probe/work/', 'name': 'Work', 'writable': True}]
    monkeypatch.setattr(providers.YandexAdapter, 'list_calendars', discovery)
    result = json.loads(tools.cal_settings(ctx, action='reload_yandex', confirm=True))
    assert result['status'] == 'ok', result
    row = ctx.store.get_calendar('yandex:probe@example.test:work')
    assert row['publish_mode'] == 'busy' and not row['role_publish']


@pytest.mark.parametrize('calendar_kind', ['local', 'google'])
@pytest.mark.parametrize('limit', [50, 10])
def test_advertised_paging_cursor_reaches_every_stored_event(track, limit, calendar_kind):
    ctx = track(make_context())
    start = _day(2).replace(hour=0)
    cid = DEFAULT_LOCAL_CALENDAR_ID
    if calendar_kind == 'google':
        adapter = providers_google.GoogleAdapter('owner@example.test', 'token', 'refresh', now_utc()+timedelta(days=1), 'client', '', None)
        adapter._request = lambda *args, **kwargs: (200, {}, {'items': [{'id': 'owner@example.test', 'summary': 'owner@example.test', 'accessRole': 'owner', 'primary': True}]})
        ctx.store.upsert_account({'id': adapter.account_id, 'provider': 'google', 'login': 'owner@example.test'})
        calendar = adapter.list_calendars()[0]
        ctx.store.upsert_calendar(calendar)
        cid = calendar['id']
    expected = set()
    for index in range(130):
        event = ctx.store.insert_event({'calendar_id': cid, 'title': f'Calendar event {index:03d}',
                                       'start_utc': iso_utc(start + timedelta(minutes=index*5)),
                                       'end_utc': iso_utc(start + timedelta(minutes=index*5+4)), 'tz': 'UTC'})
        expected.add(event['id'])
    offset, seen, pages = 0, [], []
    for _ in range(20):
        response = json.loads(tools.cal_events(ctx, start=start.isoformat(), end=(start+timedelta(days=1)).isoformat(), limit=limit, offset=offset))
        assert response['status'] == 'ok'
        rows = response['events']
        seen.extend(row['id'] for row in rows)
        pages.append({'offset': offset, 'returned': len(rows), 'next_offset': response['next_offset'], 'truncated': response.get('truncated', False), 'serialized_chars': len(json.dumps(response, ensure_ascii=False))})
        if response['next_offset'] is None:
            break
        assert response['next_offset'] > offset
        offset = response['next_offset']
    else:
        pytest.fail('cursor did not terminate')
    missing = expected - set(seen)
    assert not missing, pages
    assert len(seen) == len(expected)


@pytest.mark.parametrize('all_day', [False, True])
def test_overlapping_google_pages_keep_real_rows_and_matching_conflicts(track, all_day):
    ctx = track(make_context())
    start = _day(2).replace(hour=0)
    email = 'owner@example.test'
    adapter = providers_google.GoogleAdapter(email, 'token', 'refresh', now_utc()+timedelta(days=1), 'client', '', None)
    adapter._request = lambda *args, **kwargs: (200, {}, {'items': [{'id': email, 'summary': email, 'accessRole': 'owner', 'primary': True}]})
    ctx.store.upsert_account({'id': adapter.account_id, 'provider': 'google', 'login': email})
    calendar = adapter.list_calendars()[0]
    ctx.store.upsert_calendar(calendar)
    expected = set()
    for index in range(50):
        wire = {'id': f'googleevent{index:026d}', 'iCalUID': f'uid-{index}@google.com', 'etag': '"one"',
                'summary': f'Busy appointment {index:03d}', 'status': 'confirmed',
                'organizer': {'email': email}, 'reminders': {'useDefault': True},
                'start': {'dateTime': iso_utc(start)}, 'end': {'dateTime': iso_utc(start+timedelta(hours=1))}}
        if all_day:
            wire.update(start={'date': start.date().isoformat()}, end={'date': (start+timedelta(days=1)).date().isoformat()})
        row = providers_google.gevent_to_row(wire, calendar['id'], ctx.tz)
        expected.add(ctx.store.insert_event(row)['id'])
    args = {'start': start.isoformat(), 'end': (start+timedelta(days=1)).isoformat()}
    one = json.loads(tools.cal_events(ctx, limit=1, **args))
    assert one['status'] == 'ok' and len(one['events']) == 1 and isinstance(one['events'][0], dict)
    offset, seen = 0, []
    for _ in range(50):
        text = tools.cal_events(ctx, limit=50, offset=offset, **args)
        result = json.loads(text)
        assert result['status'] == 'ok' and result['total'] == 50
        assert len(text) <= TOOL_RESULT_LIMIT
        rows = result['events']
        assert rows and all(isinstance(row, dict) for row in rows), result
        ids = {row['id'] for row in rows}
        assert all(set(pair) <= ids for pair in result['conflicts']), result
        seen.extend(row['id'] for row in rows)
        if result['next_offset'] is None:
            break
        assert result['next_offset'] == offset + len(rows)
        offset = result['next_offset']
    else:
        pytest.fail('cursor did not terminate')
    assert set(seen) == expected and len(seen) == 50


def alarm_values(text):
    out = []
    for alarm in providers._ical().Calendar.from_ical(text).walk('VALARM'):
        trigger = alarm.decoded('TRIGGER')
        out.append({'action': str(alarm.get('ACTION')), 'trigger': trigger.total_seconds() if isinstance(trigger, timedelta) else trigger.isoformat()})
    return out


@pytest.mark.parametrize('action', ['unrelated_email', 'unrelated_popup', 'replace', 'replace_same', 'mute'])
def test_google_master_edit_preserves_or_explicitly_changes_alarm(track, action):
    ctx, cid, server = google_context()
    track(ctx)
    channel = 'popup' if action == 'unrelated_popup' else 'email'
    original = {'useDefault': False, 'overrides': [{'method': channel, 'minutes': 10}]}
    remote_id = 'imported-fixture'
    server.events[remote_id] = {'id': remote_id, 'iCalUID': 'imported-fixture@example.test', 'etag': server._etag(),
                                'summary': 'Imported appointment', 'status': 'confirmed',
                                'start': {'dateTime': _day(3).isoformat()}, 'end': {'dateTime': (_day(3)+timedelta(hours=1)).isoformat()},
                                'reminders': copy.deepcopy(original)}
    sync(ctx, cid)
    row = ctx.store.find_by_external(cid, external_id=remote_id)
    assert row['origin'] == 'external'
    assert not ctx.store.get_setting('reminder_mode')
    args = {'title': 'Renamed appointment'}
    if action in ('replace', 'replace_same'): args['reminders'] = [15 if action == 'replace' else 10]
    if action == 'mute': ctx.store.set_setting('reminder_mode', {cid: True})
    result = json.loads(tools.cal_update(ctx, id=row['id'], confirm=True, **args))
    assert result['status'] == 'updated', result
    actual = server.events[remote_id]['reminders']
    expected = ({'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 15 if action == 'replace' else 10}]} if action in ('replace', 'replace_same') else
                {'useDefault': False, 'overrides': []} if action == 'mute' else original)
    assert actual == expected


@pytest.mark.parametrize('action', ['unrelated_email_absolute', 'unrelated_display', 'replace', 'replace_same', 'mute'])
def test_caldav_master_edit_preserves_or_explicitly_changes_alarms(track, action):
    ctx, cid, server = caldav_context()
    track(ctx)
    start = _day(3)
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:imported-fixture',
             'DTSTART:'+start.strftime('%Y%m%dT%H%M%SZ'), 'DTEND:'+(start+timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ'),
             'SUMMARY:Imported appointment', 'BEGIN:VALARM', 'ACTION:'+('DISPLAY' if action == 'unrelated_display' else 'EMAIL'),
             'TRIGGER:-PT10M', 'DESCRIPTION:Reminder', 'SUMMARY:Reminder', 'ATTENDEE:mailto:owner@example.test', 'END:VALARM']
    if action != 'unrelated_display':
        lines += ['BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER;VALUE=DATE-TIME:'+(start-timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ'),
                  'DESCRIPTION:Absolute reminder', 'END:VALARM']
    lines += ['END:VEVENT', 'END:VCALENDAR', '']
    original = '\r\n'.join(lines)
    href = ctx.store.get_calendar(cid)['href'].rstrip('/') + '/imported-fixture.ics'
    server.resources[href] = (original, server._etag())
    sync(ctx, cid)
    row = ctx.store.find_by_external(cid, href=href)
    assert row['origin'] == 'external'
    assert not ctx.store.get_setting('reminder_mode')
    args = {'title': 'Renamed appointment'}
    if action in ('replace', 'replace_same'): args['reminders'] = [15 if action == 'replace' else 10]
    if action == 'mute': ctx.store.set_setting('reminder_mode', {cid: True})
    result = json.loads(tools.cal_update(ctx, id=row['id'], confirm=True, **args))
    assert result['status'] == 'updated', result
    actual = alarm_values(server.resources[href][0])
    before = alarm_values(original)
    expected = ([{'action': 'DISPLAY', 'trigger': -900.0 if action == 'replace' else -600.0}] if action in ('replace', 'replace_same') else [] if action == 'mute' else before)
    assert actual == expected


def test_trimmed_last_page_still_offers_the_unread_tail(track):
    ctx = track(make_context())
    start = _day(2).replace(hour=0)
    ids = []
    for index in range(3):
        row = ctx.store.insert_event({'calendar_id': DEFAULT_LOCAL_CALENDAR_ID, 'title': str(index) + 'x' * 6500,
                                     'start_utc': iso_utc(start+timedelta(hours=index)),
                                     'end_utc': iso_utc(start+timedelta(hours=index, minutes=30)), 'tz': 'UTC'})
        ids.append(row['id'])
    first = json.loads(tools.cal_events(ctx, start=start.isoformat(), end=(start+timedelta(days=1)).isoformat(), limit=50))
    assert first['status'] == 'ok'
    assert 0 < len(first['events']) < len(ids)
    assert first['next_offset'] == len(first['events'])
    tail = json.loads(tools.cal_events(ctx, start=start.isoformat(), end=(start+timedelta(days=1)).isoformat(),
                                      limit=50, offset=first['next_offset']))
    assert [row['id'] for row in first['events']+tail['events']] == ids
    assert tail['next_offset'] is None


def test_zero_fit_is_not_success_or_silent_skip_and_retains_event_address(track):
    ctx = track(make_context())
    start = _day(2)
    title = 'Long title ' * 1800
    row = ctx.store.insert_event({'calendar_id': DEFAULT_LOCAL_CALENDAR_ID, 'title': title,
                                 'start_utc': iso_utc(start), 'end_utc': iso_utc(start+timedelta(hours=1)), 'tz': 'UTC'})
    response = json.loads(tools.cal_events(ctx, start=start.isoformat(), end=(start+timedelta(days=1)).isoformat()))
    assert response['status'] == 'error'
    assert response['next_offset'] == 0 and response['event_id'] == row['id']
    detail = json.loads(tools.cal_events(ctx, id=response['event_id']))
    assert detail['status'] == 'ok' and detail['event']['title'] == title


def test_long_nonconflict_event_detail_is_read_whole(track):
    ctx = track(make_context())
    start = _day(2)
    description = 'Detailed meeting agenda. ' * 1000 + 'THE FINAL DECISION'
    row = ctx.store.insert_event({'calendar_id': DEFAULT_LOCAL_CALENDAR_ID, 'title': 'Meeting', 'description': description,
                                 'start_utc': iso_utc(start), 'end_utc': iso_utc(start+timedelta(hours=1)), 'tz': 'UTC'})
    result = json.loads(tools.cal_events(ctx, id=row['id']))
    assert result['event']['description'] == description



def _alarm_event(start, uid='alarms', *, title='Imported', minutes=10):
    ical = providers._ical()
    event = ical.Event()
    event.add('UID', uid)
    event.add('DTSTART', start)
    event.add('DTEND', start+timedelta(hours=1))
    event.add('SUMMARY', title)
    for action, trigger in [('EMAIL', timedelta(minutes=-minutes)), ('DISPLAY', start-timedelta(minutes=30))]:
        alarm = ical.Alarm()
        alarm.add('ACTION', action)
        alarm.add('TRIGGER', trigger)
        alarm.add('DESCRIPTION', 'Preserve this reminder text')
        if action == 'EMAIL':
            alarm.add('SUMMARY', 'Custom email reminder')
            alarm.add('ATTENDEE', 'mailto:owner@example.test')
        event.add_component(alarm)
    return event


def _alarm_bytes(component):
    return [alarm.to_ical() for alarm in component.walk('VALARM')]


@pytest.mark.parametrize('existing_override', [True, False])
@pytest.mark.parametrize('action', ['unrelated', 'replace_same', 'mute'])
def test_caldav_occurrence_alarm_source_survives_sparse_update(track, existing_override, action):
    ctx, cid, server = caldav_context()
    track(ctx)
    cal = providers._ical().Calendar()
    cal.add('VERSION', '2.0')
    master = _alarm_event(_day(3), minutes=20)
    master.add('RRULE', 'FREQ=DAILY;COUNT=3')
    cal.add_component(master)
    original = master
    if existing_override:
        original = _alarm_event(_day(4)+timedelta(hours=2), title='Override')
        original.add('RECURRENCE-ID', _day(4))
        cal.add_component(original)
    source_alarms = _alarm_bytes(original)
    master_alarms = _alarm_bytes(master)
    href = ctx.store.get_calendar(cid)['href'].rstrip('/')+'/alarms.ics'
    server.resources[href] = (cal.to_ical().decode('utf-8'), server._etag())
    sync(ctx, cid)
    row = ctx.store.find_by_external(cid, href=href)
    address = row['id']+'@'+iso_utc(_day(4))
    args = {'title': 'Only this title changed'}
    if action == 'replace_same': args['reminders'] = [10 if existing_override else 20]
    if action == 'mute': ctx.store.set_setting('reminder_mode', {cid: True})
    result = json.loads(tools.cal_update(ctx, id=address, confirm=True, **args))
    assert result['status'] == 'updated', result
    written = providers._ical().Calendar.from_ical(server.resources[href][0])
    target = next(event for event in written.walk('VEVENT') if event.get('RECURRENCE-ID'))
    assert str(target['SUMMARY']) == 'Only this title changed'
    expected_start = _day(4) + (timedelta(hours=2) if existing_override else timedelta())
    assert target.decoded('DTSTART') == expected_start
    assert target.decoded('DTEND') == expected_start + timedelta(hours=1)
    if action == 'unrelated':
        assert _alarm_bytes(target) == source_alarms
        assert _alarm_bytes(next(event for event in written.walk('VEVENT') if not event.get('RECURRENCE-ID'))) == master_alarms
    elif action == 'mute':
        assert _alarm_bytes(target) == []
    else:
        alarms = list(target.walk('VALARM'))
        assert len(alarms) == 1 and str(alarms[0]['ACTION']) == 'DISPLAY'
        assert alarms[0].decoded('TRIGGER') == timedelta(minutes=-args['reminders'][0])


@pytest.mark.parametrize('representation', ['floating', 'tzid', 'utc', 'date'])
def test_new_title_only_occurrence_keeps_its_time_and_inherited_alarms(tmp_path, representation):
    from datetime import date, datetime, timezone
    from test_calendar_temporal_roundtrip import floating_context

    ctx, row, writes = floating_context(tmp_path, representation)
    ical = providers._ical()
    source = ical.Calendar.from_ical(row['raw_payload'])
    master = source.walk('VEVENT')[0]
    for alarm in _alarm_event(datetime(2026, 10, 24, 7, tzinfo=timezone.utc)).walk('VALARM'):
        master.add_component(copy.deepcopy(alarm))
    alarms = _alarm_bytes(master)
    source_text = source.to_ical().decode('utf-8')
    ctx.store.update_event(row['id'], {'raw_payload': source_text})
    adapter = ctx.providers.adapter_for(row['account_id'])
    def request(method, url, body=None, headers=None):
        if method == 'GET':
            return 200, {'etag': '"v1"'}, source_text
        if method == 'PUT':
            writes.append(body)
            return 204, {'etag': '"v2"'}, ''
        raise AssertionError('Unexpected provider operation ' + method)
    adapter._request = request
    occurrence = json.loads(tools.cal_events(ctx, start='2026-10-25', end='2026-10-26',
                                            calendars=row['calendar_id']))['events'][0]
    assert occurrence['start'][:10] == '2026-10-25'
    result = json.loads(tools.cal_update(ctx, id=occurrence['id'], title='Occurrence title', confirm=True))
    assert result['status'] == 'updated' and len(writes) == 1
    written = ical.Calendar.from_ical(writes[0])
    target = next(event for event in written.walk('VEVENT') if event.get('RECURRENCE-ID'))
    expected = {'floating': datetime(2026, 10, 25, 9),
                'tzid': datetime(2026, 10, 25, 9, tzinfo=get_tz('Europe/Berlin')),
                'utc': datetime(2026, 10, 25, 7, tzinfo=timezone.utc),
                'date': date(2026, 10, 25)}[representation]
    assert target.decoded('DTSTART') == expected
    assert target.decoded('DTEND') == expected + (timedelta(days=1) if representation == 'date' else timedelta(hours=1))
    assert target.decoded('RECURRENCE-ID') == expected
    assert _alarm_bytes(target) == alarms
    assert _alarm_bytes(next(event for event in written.walk('VEVENT') if not event.get('RECURRENCE-ID'))) == alarms


def test_caldav_rename_after_own_reminder_ack_uses_current_resource(track):
    ctx, cid, server = caldav_context()
    track(ctx)
    cal = providers._ical().Calendar()
    cal.add('VERSION', '2.0')
    cal.add_component(_alarm_event(_day(3)))
    href = ctx.store.get_calendar(cid)['href'].rstrip('/')+'/alarm-ack.ics'
    server.resources[href] = (cal.to_ical().decode('utf-8'), server._etag())
    sync(ctx, cid)
    row = ctx.store.find_by_external(cid, href=href)
    assert json.loads(tools.cal_update(ctx, id=row['id'], reminders=[15], confirm=True))['status'] == 'updated'
    # ACK advances the row's ETag; its old imported raw text still has EMAIL/absolute alarms.
    assert 'ACTION:EMAIL' in ctx.store.get_event(row['id'])['raw_payload']
    before = alarm_values(server.resources[href][0])
    assert json.loads(tools.cal_update(ctx, id=row['id'], title='After reminder change', confirm=True))['status'] == 'updated'
    assert alarm_values(server.resources[href][0]) == before == [{'action': 'DISPLAY', 'trigger': -900.0}]


@pytest.mark.parametrize('action', ['unrelated', 'replace_same', 'mute'])
def test_google_occurrence_keeps_existing_sparse_alarm_contract(track, action):
    from test_calendar_atomic_replay import instance
    ctx, cid, server = google_context()
    track(ctx)
    created = json.loads(tools.cal_create(ctx, title='Series', start=_day(3).isoformat(), rrule='FREQ=DAILY;COUNT=3', calendars=cid, confirm=True))
    remote_id, address = instance(ctx, server, created['event']['id'], 4)
    original = {'useDefault': False, 'overrides': [{'method': 'email', 'minutes': 10}]}
    server.events[remote_id].update(reminders=copy.deepcopy(original), etag=server._etag())
    sync(ctx, cid)
    args = {'title': 'Renamed occurrence'}
    if action == 'replace_same': args['reminders'] = [10]
    if action == 'mute': ctx.store.set_setting('reminder_mode', {cid: True})
    result = json.loads(tools.cal_update(ctx, id=address, confirm=True, **args))
    assert result['status'] == 'updated', result
    expected = original if action == 'unrelated' else {'useDefault': False, 'overrides': [] if action == 'mute' else [{'method': 'popup', 'minutes': 10}]}
    assert server.events[remote_id]['reminders'] == expected


@pytest.mark.parametrize('operation', ['legacy', 'revive'])
def test_google_full_replacement_retains_alarms_when_no_sparse_intent(track, operation):
    ctx, cid, server = google_context()
    track(ctx)
    created = json.loads(tools.cal_create(ctx, title='Created', start=_day(3).isoformat(), calendars=cid, reminders=[15], confirm=True))
    row = ctx.store.get_event(created['event']['id'])
    remote = server.events[row['external_id']]
    remote.update(reminders={'useDefault': False, 'overrides': [{'method': 'email', 'minutes': 10}]}, etag=server._etag())
    payload = {}
    if operation == 'revive':
        remote['status'] = 'cancelled'
        payload = {'changes': {}, '_revive_cancelled': True}
    ctx.providers.adapter_for('').update(ctx.store.get_calendar(cid), row, remote['etag'], payload)
    assert remote['status'] == 'confirmed'
    assert remote['reminders'] == {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 15}]}


def test_ics_legacy_full_replacement_and_explicit_empty_mask_are_distinct():
    cal = providers._ical().Calendar()
    cal.add('VERSION', '2.0')
    event = _alarm_event(_day(3))
    cal.add_component(event)
    original = _alarm_bytes(event)
    row = {'uid': 'alarms', 'title': 'New title', 'start_utc': iso_utc(_day(3)), 'end_utc': iso_utc(_day(3)+timedelta(hours=1)),
           'tz': 'UTC', 'raw_payload': cal.to_ical().decode('utf-8'), 'reminders_json': '[15]'}
    full = providers.row_to_ics(row)
    assert alarm_values(full) == [{'action': 'DISPLAY', 'trigger': -900.0}]
    sparse = providers._ical().Calendar.from_ical(providers.row_to_ics(row, changed_fields=set()))
    assert _alarm_bytes(sparse.walk('VEVENT')[0]) == original
