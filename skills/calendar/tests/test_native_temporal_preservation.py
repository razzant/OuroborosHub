"""Calendar meaning survives native serialization, sync and explicit conflict choice."""
import json
import io
import urllib.request
import urllib.response
from email.message import Message
from datetime import datetime
from types import SimpleNamespace

import icalendar
import pytest
from dateutil.rrule import rrulestr

import model
import providers
import providers_google
import reminders
import tools
from scripts.worker import reconcile_calendar
from test_calendar_provider_divergence import FakeGoogle, add_google_calendar
from test_recurrence_until_semantics import native_context, master_component


def sync(ctx, row):
    calendar = ctx.store.get_calendar(row['calendar_id'])
    return reconcile_calendar(ctx.store, ctx.providers, ctx.providers.adapter_for(calendar['account_id']), calendar, ctx.tz)


def listed(ctx, start, end, cid):
    return json.loads(tools.cal_events(ctx, start=start, end=end, calendars=cid))['events']


@pytest.mark.parametrize('sync_before', [False, True])
def test_one_off_to_weekly_keeps_author_zone_and_native_wall_hour(tmp_path, sync_before):
    ctx, source, server = native_context(tmp_path, 'tzid')
    result = json.loads(tools.cal_create(ctx, title='Weekly later', start='2026-10-14T09:00',
                                       end='2026-10-14T10:00', calendars=source['calendar_id'], confirm=True))
    assert result['status'] == 'created', result
    row = ctx.store.get_event(result['event']['id'])
    if sync_before:
        sync(ctx, source)
        assert ctx.store.get_event(row['id'])['tz'] == row['tz'] == 'Europe/Berlin'
        # A subsequent equal-version observation is a no-op, not another enrichment.
        assert sync(ctx, source)[0] == 0
    edited = json.loads(tools.cal_update(ctx, id=row['id'], rrule='FREQ=WEEKLY;COUNT=4', scope='all', confirm=True))
    assert edited['status'] == 'updated', edited
    wire = master_component(server.resources[row['href']][0])
    native = list(rrulestr(wire['RRULE'].to_ical().decode(), dtstart=wire.decoded('DTSTART')))
    shown = [e for e in listed(ctx, '2026-10-14', '2026-11-10', source['calendar_id']) if e['title'] == 'Weekly later']
    assert [dt.astimezone(ctx.tz).hour for dt in native] == [int(e['start'][11:13]) for e in shown] == [9] * 4


def test_accept_provider_conflict_replaces_temporal_kind(tmp_path):
    ctx, source, server = native_context(tmp_path, 'floating')
    original = server.resources[source['href']][0].replace('RRULE:FREQ=DAILY;COUNT=4\r\n', '')
    row = providers.ics_to_rows(original, source['calendar_id'], source['href'], 'v1', ctx.tz)[0]
    ctx.store.update_event(source['id'], row)
    server.resources[source['href']] = (original.replace('DTSTART:', 'DTSTART;TZID=Europe/Berlin:')
                                       .replace('DTEND:', 'DTEND;TZID=Europe/Berlin:'), 'v2')
    ctx.tz = model.get_tz('America/New_York')
    result = json.loads(tools.cal_update(ctx, id=source['id'], title='Local edit', confirm=True))
    assert result['status'] == 'conflict', result
    comparison = json.loads(tools.cal_events(ctx, id=source['id']))['event']['conflict']
    result = json.loads(tools.cal_update(ctx, id=source['id'], resolution='provider',
                                       resolution_token=comparison['resolution_token'], confirm=True))
    assert result['status'] == 'resolved', result
    accepted = json.loads(tools.cal_events(ctx, id=source['id']))['event']
    assert accepted['start'] == comparison['provider']['start']
    assert ctx.store.get_event(source['id'])['time_kind'] == 'absolute'


def native_with_people():
    return '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:series',
        'DTSTART;TZID=Europe/Berlin:20261210T090000', 'DTEND;TZID=Europe/Berlin:20261210T100000',
        'RRULE:FREQ=DAILY;COUNT=2', 'SUMMARY:Native meeting',
        'ORGANIZER;SENT-BY="mailto:assistant@example.test":mailto:organizer@example.test',
        'ATTENDEE;PARTSTAT=DELEGATED;DELEGATED-TO="mailto:delegate@example.test";RSVP=TRUE;ROLE=REQ-PARTICIPANT;SCHEDULE-AGENT=SERVER:mailto:fixture',
        'END:VEVENT', 'BEGIN:VEVENT', 'UID:series', 'RECURRENCE-ID;TZID=Europe/Berlin:20261213T090000',
        'DTSTART;TZID=Europe/Berlin:20261213T110000', 'DTEND;TZID=Europe/Berlin:20261213T120000',
        'SUMMARY:Retained exception', 'END:VEVENT', 'END:VCALENDAR', ''])


def test_sparse_edit_preserves_people_and_retained_exception_until_explicit_cut():
    row = providers.ics_to_rows(native_with_people(), 'cal', 'href', 'v1', model.get_tz('Europe/Berlin'))[0]
    original = master_component(row['raw_payload'])
    row.update(title='Renamed', rrule='FREQ=DAILY;UNTIL=20261211T080000Z')
    ordinary = providers.row_to_ics(row, changed_fields={'title', 'rrule'})
    cal = icalendar.Calendar.from_ical(ordinary)
    assert len(cal.walk('VEVENT')) == 2
    current = master_component(ordinary)
    for field in ('ATTENDEE', 'ORGANIZER'):
        assert current[field].to_ical() == original[field].to_ical()
        assert dict(current[field].params) == dict(original[field].params)
    cut = providers.row_to_ics(row, changed_fields={'rrule'}, truncate_before='2026-12-12T08:00:00+00:00')
    assert len(icalendar.Calendar.from_ical(cut).walk('VEVENT')) == 1
    replacement = providers.row_to_ics({**row, 'attendees_json': json.dumps([{'email': 'new@example.test'}])}, changed_fields={'attendees'})
    assert str(master_component(replacement)['ATTENDEE']) == 'mailto:new@example.test'


def test_rsvp_changes_only_own_partstat_and_preserves_native_guest_fields(tmp_path):
    ctx, source, server = native_context(tmp_path, 'tzid')
    raw = native_with_people()
    row = providers.ics_to_rows(raw, source['calendar_id'], source['href'], 'v1', ctx.tz)[0]
    server.resources[source['href']] = (raw, 'v1')
    adapter = ctx.providers.adapter_for('fixture')
    adapter.respond(ctx.store.get_calendar(source['calendar_id']), row,
                    {'response': 'accepted', '_expected_etag': 'v1'})
    after = master_component(server.resources[source['href']][0])
    before = master_component(raw)
    assert after['ATTENDEE'].params['PARTSTAT'] == 'ACCEPTED'
    assert {k: v for k, v in after['ATTENDEE'].params.items() if k != 'PARTSTAT'} == {
        k: v for k, v in before['ATTENDEE'].params.items() if k != 'PARTSTAT'}
    assert dict(after['ORGANIZER'].params) == dict(before['ORGANIZER'].params)


@pytest.mark.parametrize('timed_override', [False, True])
def test_caldav_date_exception_address_survives_travel_and_new_version(tmp_path, timed_override):
    ctx, source, server = native_context(tmp_path, 'date')
    ctx.tz = model.get_tz('Europe/Moscow')
    start = 'DTSTART;TZID=Europe/Berlin:20261211T100000' if timed_override else 'DTSTART;VALUE=DATE:20261211'
    end = 'DTEND;TZID=Europe/Berlin:20261211T110000' if timed_override else 'DTEND;VALUE=DATE:20261212'
    raw = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:travel-series',
        'DTSTART;VALUE=DATE:20261210', 'DTEND;VALUE=DATE:20261211', 'RRULE:FREQ=DAILY;COUNT=3',
        'SUMMARY:Daily date', 'END:VEVENT', 'BEGIN:VEVENT', 'UID:travel-series',
        'RECURRENCE-ID;VALUE=DATE:20261211', start, end, 'SUMMARY:Date exception', 'END:VEVENT', 'END:VCALENDAR', ''])
    ctx.store.delete_event(source['id'], hard=True)
    server.resources[source['href']] = (raw, 'v2')
    sync(ctx, source)
    master = ctx.store.find_by_external(source['calendar_id'], uid='travel-series')
    before, = ctx.store.exceptions_for(master['id'])
    ctx.tz = model.get_tz('America/New_York')
    for etag in ('v2', 'v3'):
        server.resources[source['href']] = (raw, etag)
        sync(ctx, source)
        after, = ctx.store.exceptions_for(master['id'])
        assert (after['id'], after['recurrence_id']) == (before['id'], before['recurrence_id'])
        if timed_override:
            assert after['start_utc'] == before['start_utc']
        assert len(listed(ctx, '2026-12-11', '2026-12-12', source['calendar_id'])) == 1


def test_google_date_instance_address_uses_original_date_basis_after_travel(tmp_path):
    ctx, _, _ = native_context(tmp_path, 'date')
    cid = add_google_calendar(ctx.store)
    server = FakeGoogle()
    adapter = server.attach(providers_google.GoogleAdapter('fixture', 'token', 'refresh', model.now_utc(), 'client', '', None))
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda _: adapter)
    ctx.tz = model.get_tz('Europe/Moscow')
    master = {'id': 'date-master', 'iCalUID': 'date-series', 'etag': 'v1', 'summary': 'Daily date',
              'start': {'date': '2026-12-10'}, 'end': {'date': '2026-12-11'},
              'recurrence': ['RRULE:FREQ=DAILY;COUNT=3'], 'status': 'confirmed'}
    server.events[master['id']] = master
    sync(ctx, {'calendar_id': cid})
    source = ctx.store.find_by_external(cid, external_id=master['id'])
    ctx.tz = model.get_tz('America/New_York')
    instance = {**master, 'id': 'date-master_20261211', 'etag': 'v2', 'summary': 'Edited date',
                'start': {'date': '2026-12-11'}, 'end': {'date': '2026-12-12'},
                'originalStartTime': {'date': '2026-12-11'}, 'recurringEventId': master['id']}
    instance.pop('recurrence')
    server.events[instance['id']] = instance
    sync(ctx, {'calendar_id': cid})
    exception, = ctx.store.exceptions_for(source['id'])
    assert exception['recurrence_id'] == model.iso_utc(datetime(2026, 12, 11, tzinfo=model.get_tz(source['tz'])))
    assert [e['title'] for e in listed(ctx, '2026-12-11', '2026-12-12', cid)] == ['Edited date']


@pytest.mark.parametrize('method', ['PROPFIND', 'REPORT', 'PUT'])
@pytest.mark.parametrize('code', [301, 302, 307, 308])
def test_dav_redirect_retains_method_body_and_conditional_header(method, code):
    adapter = providers.CalDAVAdapter('fixture', 'fixture', 'https://calendar.example.test')
    request = urllib.request.Request('https://calendar.example.test/old', data=b'body', method=method,
                                     headers={'If-Match': 'v1', 'Authorization': 'synthetic'})
    result = adapter._redirect().redirect_request(request, None, code, 'redirect', {}, 'https://calendar.example.test/new')
    assert (result.get_method(), result.data) == (method, b'body')
    assert result.get_header('If-match') == 'v1'
    with pytest.raises(providers.ProviderError):
        adapter._redirect().redirect_request(request, None, code, 'redirect', {}, 'https://other.example.test/new')


@pytest.mark.parametrize('send', [False, True])
def test_fresh_caldav_creation_preserves_explicit_scheduling_choice(tmp_path, send):
    ctx, source, server = native_context(tmp_path, 'tzid')
    result = json.loads(tools.cal_create(ctx, title='Invite choice', start='2026-12-10T09:00',
        end='2026-12-10T10:00', calendars=source['calendar_id'], attendees=['guest@example.test'],
        send_invites=send, confirm=True))
    assert result['status'] == 'created', result
    row = ctx.store.get_event(result['event']['id'])
    guest = master_component(server.resources[row['href']][0])['ATTENDEE']
    assert guest.params.get('SCHEDULE-AGENT', 'SERVER') == ('SERVER' if send else 'NONE')


def test_missing_and_legacy_caldav_credentials_report_real_readiness(tmp_path):
    resolver = providers.Providers({}, str(tmp_path))
    assert resolver.adapter_for('yandex:missing') is None
    assert 'yandex:missing' in resolver.errors
    resolver.vault_error = 'Cannot read credential vault'
    assert resolver.adapter_for('caldav:locked') is None
    assert resolver.errors['caldav:locked'] == resolver.vault_error
    legacy = providers.Providers({'YANDEX_CALDAV_ACCOUNTS': json.dumps([{'login': 'fixture', 'app_password': 'synthetic'}])}, str(tmp_path))
    assert isinstance(legacy.adapter_for('yandex:fixture'), providers.YandexAdapter)
    assert 'yandex:fixture' not in legacy.errors


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('pending', [False, True])
def test_old_date_instance_addresses_reconcile_without_rearming_settled_notices(tmp_path, provider, pending):
    ctx, initial, server = native_context(tmp_path, 'date')
    ctx.tz = model.get_tz('Europe/Moscow')
    correct_id = None
    if provider == 'caldav':
        cid = initial['calendar_id']
        raw = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:legacy-date',
            'DTSTART;VALUE=DATE:20261210', 'DTEND;VALUE=DATE:20261211', 'RRULE:FREQ=DAILY;COUNT=3',
            'SUMMARY:Daily', 'END:VEVENT', 'BEGIN:VEVENT', 'UID:legacy-date',
            'RECURRENCE-ID;VALUE=DATE:20261211', 'DTSTART;VALUE=DATE:20261211', 'DTEND;VALUE=DATE:20261212',
            'SUMMARY:Exception', 'END:VEVENT', 'END:VCALENDAR', ''])
        ctx.store.delete_event(initial['id'], hard=True)
        server.resources[initial['href']] = (raw, 'legacy-v1')
        sync(ctx, initial)
        master = ctx.store.find_by_external(cid, uid='legacy-date')
        correct_id = ctx.store.exceptions_for(master['id'])[0]['id']
        ctx.tz = model.get_tz('America/New_York')
        wrong = providers.ics_to_rows(raw, cid, initial['href'], 'legacy-v1', ctx.tz)[1]
    else:
        cid = add_google_calendar(ctx.store)
        server = FakeGoogle()
        adapter = server.attach(providers_google.GoogleAdapter('fixture', 'token', 'refresh', model.now_utc(), 'client', '', None))
        ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda _: adapter)
        base = {'id': 'legacy-master', 'iCalUID': 'legacy-date', 'etag': 'legacy-v1', 'summary': 'Daily',
                'start': {'date': '2026-12-10'}, 'end': {'date': '2026-12-11'}, 'recurrence': ['RRULE:FREQ=DAILY;COUNT=3']}
        server.events[base['id']] = base
        sync(ctx, {'calendar_id': cid})
        master = ctx.store.find_by_external(cid, external_id=base['id'])
        ctx.tz = model.get_tz('America/New_York')
        instance = {**base, 'id': 'legacy-master_20261211', 'summary': 'Exception',
                    'start': {'date': '2026-12-11'}, 'end': {'date': '2026-12-12'},
                    'originalStartTime': {'date': '2026-12-11'}, 'recurringEventId': base['id']}
        instance.pop('recurrence')
        server.events[instance['id']] = instance
        wrong = providers_google.gevent_to_row(instance, cid, ctx.tz)
    # This is a real older-writer state: the row carries the later reader's basis.
    wrong = ctx.store.insert_event({**wrong, 'master_id': master['id'], 'sync_state': 'pending' if pending else 'synced'})
    canonical = model.iso_utc(datetime(2026, 12, 11, tzinfo=model.get_tz(master['tz'])))
    assert wrong['recurrence_id'] != canonical
    effective = '2026-12-11T05:00:00+00:00'
    notice = reminders.notice_id_for(master['id'], effective, 0, wrong['recurrence_id'])
    ctx.store.schedule_reminder(master['id'], effective, 0, effective, notice, wrong['recurrence_id'])
    with ctx.store._conn() as conn:
        conn.execute("UPDATE reminders SET state='unknown' WHERE notice_id=?", (notice,))
    sync(ctx, {'calendar_id': cid})
    current = ctx.store.get_event(wrong['id'])
    if pending:
        assert not current['deleted_at'] and current['recurrence_id'] == wrong['recurrence_id']
        assert ctx.providers.adapter_for('fixture').parse_errors
    else:
        only, = ctx.store.exceptions_for(master['id'])
        assert only['recurrence_id'] == canonical
        assert only['id'] == (correct_id or wrong['id'])
        carried = reminders.notice_id_for(master['id'], effective, 0, canonical)
        with ctx.store._conn() as conn:
            record = conn.execute('SELECT state FROM reminders WHERE notice_id=?', (carried,)).fetchone()
        assert record and record['state'] == 'unknown'


@pytest.mark.parametrize('kind', ['date', 'floating', 'tzid', 'utc'])
@pytest.mark.parametrize('travel', [False, True])
def test_acknowledged_native_cancellation_survives_equal_version_sync_and_later_rule_edit(tmp_path, kind, travel):
    ctx, source, server = native_context(tmp_path, kind)
    initial = listed(ctx, '2026-10-24', '2026-10-29', source['calendar_id'])
    selected = initial[1]
    key = selected['id'].split('@', 1)[1]
    result = json.loads(tools.cal_delete(ctx, id=selected['id'], scope='this', confirm=True))
    assert result['status'] == 'deleted', result
    if travel:
        ctx.tz = model.get_tz('America/New_York')
    sync(ctx, source)
    assert selected['id'] not in {event['id'] for event in listed(ctx, '2026-10-23', '2026-10-30', source['calendar_id'])}
    cancelled = [row for row in ctx.store.exceptions_for(source['id']) if row['recurrence_id'] == key]
    assert len(cancelled) == 1 and cancelled[0]['status'] == 'cancelled'
    changed = json.loads(tools.cal_update(ctx, id=source['id'], rrule='FREQ=DAILY;COUNT=5', scope='all', confirm=True))
    assert changed['status'] == 'updated', changed
    wire, etag = server.resources[source['href']]
    parsed = providers.ics_to_rows(wire, source['calendar_id'], source['href'], etag, ctx.tz)[0]
    canonical = model.civil_in_basis(parsed, ctx.store.get_event(source['id']))
    assert key in canonical['exdates'].split(',')


@pytest.mark.parametrize('change', ['external_title', 'missing_ack_etag'])
def test_utc_transport_of_one_off_does_not_replace_authoring_zone(tmp_path, change, monkeypatch):
    ctx, source, server = native_context(tmp_path, 'tzid')
    adapter = ctx.providers.adapter_for('fixture')
    if change == 'missing_ack_etag':
        monkeypatch.setattr(adapter, '_write_version', lambda *args: '')
    result = json.loads(tools.cal_create(ctx, title='Author zone', start='2026-10-14T09:00', end='2026-10-14T10:00',
                                       calendars=source['calendar_id'], confirm=True))
    assert result['status'] == 'created', result
    row = ctx.store.get_event(result['event']['id'])
    if change == 'external_title':
        def rename(cal):
            event = cal.walk('VEVENT')[0]
            event['SUMMARY'] = 'External title'
        server.edit(row['href'], rename)
    sync(ctx, source)
    assert ctx.store.get_event(row['id'])['tz'] == 'Europe/Berlin'
    edited = json.loads(tools.cal_update(ctx, id=row['id'], rrule='FREQ=WEEKLY;COUNT=4', scope='all', confirm=True))
    assert edited['status'] == 'updated', edited
    native = master_component(server.resources[row['href']][0])
    occurrences = list(rrulestr(native['RRULE'].to_ical().decode(), dtstart=native.decoded('DTSTART')))
    assert [value.astimezone(model.get_tz('Europe/Berlin')).hour for value in occurrences] == [9] * 4


def test_native_utc_tzid_is_an_explicit_zone_change(tmp_path):
    ctx, source, server = native_context(tmp_path, 'tzid')
    result = json.loads(tools.cal_create(ctx, title='Explicit UTC', start='2026-12-10T09:00', calendars=source['calendar_id'], confirm=True))
    row = ctx.store.get_event(result['event']['id'])
    raw, _ = server.resources[row['href']]
    raw = raw.replace('DTSTART:20261210T080000Z', 'DTSTART;TZID=UTC:20261210T080000')
    raw = raw.replace('DTEND:20261210T090000Z', 'DTEND;TZID=UTC:20261210T090000')
    server.resources[row['href']] = (raw, 'explicit-utc')
    sync(ctx, source)
    assert ctx.store.get_event(row['id'])['tz'] == 'UTC'


def test_dav_308_dispatches_through_the_real_urllib_opener(monkeypatch):
    seen = []
    def reply(handler, request):
        seen.append((request.full_url, request.get_method(), request.data, request.get_header('If-match')))
        headers = Message()
        code = 308 if request.full_url.endswith('/old') else 200
        if code == 308:
            headers['Location'] = 'http://calendar.example.test/new'
        result = urllib.response.addinfourl(io.BytesIO(b'result'), headers, request.full_url, code=code)
        result.msg = 'Redirect' if code == 308 else 'OK'
        return result
    monkeypatch.setattr(urllib.request.HTTPHandler, 'http_open', reply)
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {})
    adapter = providers.CalDAVAdapter('fixture', 'fixture', 'http://calendar.example.test')
    status, _, body = adapter._request('REPORT', 'http://calendar.example.test/old', '<query/>', {'If-Match': 'v1'})
    assert (status, body) == (200, 'result')
    assert seen == [('http://calendar.example.test/old', 'REPORT', b'<query/>', 'v1'),
                    ('http://calendar.example.test/new', 'REPORT', b'<query/>', 'v1')]


@pytest.mark.parametrize('end', ['2026-12-10T08:00Z', '2026-12-10T09:00Z'])
def test_explicit_reversed_or_empty_ranges_are_not_silently_replaced(tmp_path, end):
    ctx, _, _ = native_context(tmp_path, 'utc')
    for operation in (tools.cal_events, tools.cal_free):
        result = json.loads(operation(ctx, start='2026-12-10T09:00Z', end=end))
        assert result['status'] == 'error'
    omitted = json.loads(tools.cal_events(ctx, start='2026-12-10T09:00Z'))
    assert omitted['status'] == 'ok' and omitted['range']['end'] > omitted['range']['start']
