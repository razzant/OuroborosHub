"""Calendar dates, stored recurrence identities and lossless temporal round trips."""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PAYLOAD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PAYLOAD))
import model
import ops
import providers
import providers_google
import routes
import tools
import reminders
from scripts.worker import sync_account
from store import Store


def context(tmp_path, zone):
    ctx = tools.Context(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda _: {}))
    ctx.tz = model.get_tz(zone)
    ctx.store.set_setting('timezone', zone)
    return ctx


def trace(case, **facts):
    print('WITNESS ' + json.dumps({'case': case, **facts}, ensure_ascii=False, default=str))


def create(ctx, **kwargs):
    result = json.loads(tools.cal_create(ctx, confirm=True, **kwargs))
    assert result['status'] == 'created', result
    return ctx.store.get_event(result['event']['id'])


def local_times(row):
    zone = model.get_tz(row['tz'])
    return tuple(model.parse_stored(row[key]).astimezone(zone) for key in ('start_utc', 'end_utc'))


@pytest.mark.parametrize('owner_zone', ['Europe/Moscow', 'America/New_York'])
def test_all_day_read_retains_calendar_date(tmp_path, owner_zone):
    ctx = context(tmp_path, 'Europe/Moscow')
    row = create(ctx, title='All day', start='2026-12-10', end='2026-12-11', all_day=True)
    before = (row['start_utc'], row['end_utc'])
    ctx.tz = model.get_tz(owner_zone)
    shown = json.loads(tools.cal_events(ctx, id=row['id']))['event']
    trace('3-display', owner_zone=owner_zone, stored=before, shown=shown)
    assert (ctx.store.get_event(row['id'])['start_utc'], ctx.store.get_event(row['id'])['end_utc']) == before
    assert (shown['start'][:10], shown['end'][:10]) == ('2026-12-10', '2026-12-11')


@pytest.mark.parametrize('owner_zone', ['Europe/Moscow', 'America/New_York'])
def test_widget_title_payload_does_not_move_all_day_date(tmp_path, owner_zone):
    ctx = context(tmp_path, 'Europe/Moscow')
    row = create(ctx, title='All day', start='2026-12-10', end='2026-12-11', all_day=True)
    ctx.tz = model.get_tz(owner_zone)
    shown = json.loads(tools.cal_events(ctx, id=row['id']))['event']
    # Exact saveCard transformations: datetime-local f.start/f.end versus DATE values.
    start, end = shown['start'][:16], shown['end'][:16]
    body = {'id': row['id'], 'title': 'Only title edited', 'start': start[:10], 'end': end[:10], 'scope': 'this'}
    if body['start'] == start and body['end'] == end:
        body.pop('start'); body.pop('end')
    handlers = {}
    api = SimpleNamespace(register_route=lambda name, handler, **_: handlers.update({name: handler}))
    routes.register_routes(api, lambda: ctx)
    class Request:
        async def json(self): return body
    response = asyncio.run(handlers['event/update'](Request()))
    saved = ctx.store.get_event(row['id'])
    times = local_times(saved)
    trace('3-widget-route', owner_zone=owner_zone, payload=body, result=json.loads(response.body), saved_local=times)
    assert response.status_code == 200
    assert tuple(t.date().isoformat() for t in times) == ('2026-12-10', '2026-12-11')


@pytest.mark.parametrize('owner_zone', ['Europe/Moscow', 'America/New_York'])
def test_title_only_tool_control_preserves_storage(tmp_path, owner_zone):
    ctx = context(tmp_path, 'Europe/Moscow')
    row = create(ctx, title='All day', start='2026-12-10', end='2026-12-11', all_day=True)
    ctx.tz = model.get_tz(owner_zone)
    result = json.loads(tools.cal_update(ctx, id=row['id'], title='Only title edited', confirm=True))
    saved = ctx.store.get_event(row['id'])
    trace('3-tool-positive', owner_zone=owner_zone, result=result, stored=(saved['start_utc'], saved['end_utc']))
    assert result['status'] == 'updated'
    assert (saved['start_utc'], saved['end_utc']) == (row['start_utc'], row['end_utc'])


@pytest.mark.parametrize('first_day', [24, 27])
def test_all_day_following_split_keeps_one_calendar_day(tmp_path, first_day):
    ctx = context(tmp_path, 'Europe/Berlin')
    start = datetime(2026, 10, first_day, tzinfo=ctx.tz)
    row = create(ctx, title='Before', start=start.date().isoformat(), end=(start+timedelta(days=1)).date().isoformat(),
                 all_day=True, rrule='FREQ=DAILY;COUNT=5')
    key = model.iso_utc(start + timedelta(days=1))
    result = json.loads(tools.cal_update(ctx, id=row['id']+'@'+key, title='After', scope='following', confirm=True))
    with ctx.store._conn() as conn:
        parts = [dict(r) for r in conn.execute("SELECT * FROM events WHERE master_id='' AND deleted_at IS NULL AND id!=?", (row['id'],))]
    assert len(parts) == 1, (result, parts)
    saved = parts[0]; times = local_times(saved)
    wire = providers_google.row_to_gevent(saved)
    trace('4-following', first_day=first_day, result=result, local=times, google_dates=(wire['start'], wire['end']))
    assert (times[1].date()-times[0].date()).days == 1 and times[1].hour == times[0].hour == 0


@pytest.mark.parametrize('first_day', [24, 27])
def test_unchanged_occurrence_dates_scope_all_keep_duration(tmp_path, first_day):
    ctx = context(tmp_path, 'Europe/Berlin')
    start = datetime(2026, 10, first_day, tzinfo=ctx.tz)
    row = create(ctx, title='Before', start=start.date().isoformat(), end=(start+timedelta(days=1)).date().isoformat(),
                 all_day=True, rrule='FREQ=DAILY;COUNT=5')
    occurrence = start+timedelta(days=1)
    result = json.loads(tools.cal_update(ctx, id=row['id']+'@'+model.iso_utc(occurrence),
        start=occurrence.date().isoformat(), end=(occurrence+timedelta(days=1)).date().isoformat(), all_day=True,
        scope='all', confirm=True))
    saved = ctx.store.get_event(row['id']); times = local_times(saved)
    trace('4-all', first_day=first_day, result=result, local=times)
    assert (times[0].date(), times[1].date()) == (start.date(), (start+timedelta(days=1)).date())


@pytest.mark.parametrize('new_rule', ['FREQ=DAILY;COUNT=6', 'FREQ=WEEKLY;COUNT=5'])
def test_visible_overrides_remain_addressable_after_rule_replacement(tmp_path, new_rule):
    ctx = context(tmp_path, 'UTC')
    row = create(ctx, title='Series', start='2026-12-10T10:00:00+00:00', end='2026-12-10T11:00:00+00:00', rrule='FREQ=DAILY;COUNT=5')
    key = '2026-12-11T10:00:00+00:00'
    assert json.loads(tools.cal_update(ctx, id=row['id']+'@'+key, title='Edited second', confirm=True))['status'] == 'updated'
    assert json.loads(tools.cal_update(ctx, id=row['id'], rrule=new_rule, scope='all', confirm=True))['status'] == 'updated'
    visible = json.loads(tools.cal_events(ctx, start='2026-12-10', end='2027-01-15'))['events']
    orphan = next((r for r in visible if r['title'] == 'Edited second'), None)
    if orphan is None:
        trace('5-rule', new_rule=new_rule, retained_visible=False)
        return
    read = json.loads(tools.cal_events(ctx, id=orphan['id']))
    delete = json.loads(tools.cal_delete(ctx, id=orphan['id'], confirm=True))
    trace('5-rule', new_rule=new_rule, visible=orphan, direct_read=read, delete=delete)
    assert read['status'] == 'ok' and delete['status'] == 'deleted'


def floating_context(tmp_path, representation):
    ctx = context(tmp_path, 'Europe/Berlin')
    cid = 'yandex:fixture:work'; aid = 'yandex:fixture'
    href = 'https://caldav.yandex.ru/calendars/fixture/work/event.ics'
    ctx.store.upsert_account({'id': aid, 'provider': 'yandex', 'login': 'fixture'})
    ctx.store.upsert_calendar({'id': cid, 'account_id': aid, 'provider': 'yandex', 'name': 'Work', 'href': href.rsplit('/',1)[0]+'/'})
    suffix = ';TZID=Europe/Berlin' if representation == 'tzid' else ''
    first, last = '20261024T090000', '20261024T100000'
    if representation == 'utc':
        first, last = '20261024T070000Z', '20261024T080000Z'
    elif representation == 'date':
        suffix, first, last = ';VALUE=DATE', '20261024', '20261025'
    source = '\r\n'.join(['BEGIN:VCALENDAR','VERSION:2.0','BEGIN:VEVENT','UID:floating-case',
        f'DTSTART{suffix}:{first}',f'DTEND{suffix}:{last}','RRULE:FREQ=DAILY;COUNT=4',
        'SUMMARY:Before','END:VEVENT','END:VCALENDAR',''])
    parsed = providers.ics_to_rows(source, cid, href, '"v1"', ctx.tz)[0]
    saved = ctx.store.insert_event(parsed)
    writes = []
    adapter = providers.YandexAdapter('fixture', 'fixture')
    def request(method, url, body=None, headers=None):
        if method == 'GET': return 200, {'etag': '"v1"'}, source
        if method == 'PUT':
            writes.append(body); return 204, {'etag': '"v2"'}, ''
        raise AssertionError('Unexpected provider operation '+method)
    adapter._request = request
    ctx.providers = SimpleNamespace(adapter_for=lambda _: adapter, errors={})
    return ctx, ctx.store.get_event(saved['id']), writes


@pytest.mark.parametrize('representation', ['floating', 'tzid'])
def test_floating_daily_wall_time_survives_dst(tmp_path, representation):
    ctx, row, _ = floating_context(tmp_path, representation)
    result = json.loads(tools.cal_events(ctx, start='2026-10-24', end='2026-10-28', calendars=row['calendar_id']))
    times = [e['start'] for e in result['events']]
    trace('7-expansion', representation=representation, stored_tz=row['tz'], times=times)
    assert len(times) == 4 and all(value[11:16] == '09:00' for value in times)


@pytest.mark.parametrize('representation', ['floating', 'tzid'])
def test_title_edit_preserves_dtstart_representation(tmp_path, representation):
    ctx, row, writes = floating_context(tmp_path, representation)
    result = json.loads(tools.cal_update(ctx, id=row['id'], title='After', scope='all', confirm=True))
    assert result['status'] == 'updated' and len(writes) == 1, (result, writes)
    import icalendar
    event = next(c for c in icalendar.Calendar.from_ical(writes[0]).walk() if c.name == 'VEVENT')
    value = event.decoded('DTSTART')
    trace('7-title-write', representation=representation, result=result, dtstart=str(value),
          tzid=str(event['DTSTART'].params.get('TZID','')), floating=value.tzinfo is None,
          dtstart_line=next(line for line in writes[0].splitlines() if line.startswith('DTSTART:') or line.startswith('DTSTART;')))
    assert value.hour == 9 and (value.tzinfo is None if representation == 'floating' else event['DTSTART'].params.get('TZID') == 'Europe/Berlin')


@pytest.mark.parametrize('zone', ['America/New_York', 'Pacific/Kiritimati'])
@pytest.mark.parametrize('recurring', [False, True])
def test_all_day_window_availability_and_notice_share_the_date(tmp_path, zone, recurring):
    ctx = context(tmp_path, 'Europe/Moscow')
    row = create(ctx, title='Calendar date', start='2026-12-10', end='2026-12-11', all_day=True,
                 rrule='FREQ=DAILY;COUNT=3' if recurring else '', reminders=[0])
    ctx.tz = model.get_tz(zone)
    begin = datetime(2026, 12, 10, tzinfo=ctx.tz); end = begin + timedelta(days=1)
    occurrences = ctx.occurrences(begin, end)
    assert len(occurrences) == 1
    hard, _ = ops.busy_intervals(occurrences)
    assert hard == [(begin, end)]
    listed = json.loads(tools.cal_events(ctx, start='2026-12-10', end='2026-12-11'))['events']
    assert [(event['start'][:10], event['end'][:10]) for event in listed] == [('2026-12-10', '2026-12-11')]
    assert '10.12' in reminders.format_text(row, '', 0, ctx.tz)
    ctx.store.drop_scheduled_reminders()
    reminders.plan(ctx.store, ctx.occurrences, now=begin)
    sent = []
    channel = SimpleNamespace(send=lambda key, text: (sent.append(text) or ('sent', 'offline recorder')))
    result = reminders.deliver_due(ctx.store, channel, ctx.tz, now=begin)
    assert result['sent'] == 1 and len(sent) == 1 and '10.12' in sent[0]


def test_stored_override_keeps_identity_and_pending_history_after_rule_change(tmp_path):
    ctx = context(tmp_path, 'UTC')
    master = create(ctx, title='Series', start='2026-12-10T10:00:00+00:00', rrule='FREQ=DAILY;COUNT=5')
    key = '2026-12-11T10:00:00+00:00'
    tools.cal_update(ctx, id=master['id']+'@'+key, title='Authored override', confirm=True)
    row = ctx.store.exceptions_for(master['id'])[0]
    pending = ctx.store.add_intent('update', 'local', row['calendar_id'], row['id'], {'changes': {'description': 'Retained pending'}})
    with ctx.store._conn() as conn:
        conn.execute("INSERT INTO reminders(id,event_id,occurrence_start_utc,recurrence_id,offset_min,fire_at_utc,notice_id,state,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ('history', master['id'], key, key, 0, key, 'history-notice', 'sent', key))
    tools.cal_update(ctx, id=master['id'], rrule='FREQ=WEEKLY;COUNT=5', scope='all', confirm=True)
    assert ctx.store.get_event(row['id'])['recurrence_id'] == key
    assert ctx.store.get_intent(pending['id'])['payload_json'] == pending['payload_json']
    assert json.loads(tools.cal_events(ctx, id=row['id']))['status'] == 'ok'
    assert json.loads(tools.cal_update(ctx, id=row['id'], description='New explicit edit', confirm=True))['status'] == 'updated'
    assert ctx.store.get_event(row['id'])['description'] == 'New explicit edit'
    with ctx.store._conn() as conn:
        assert conn.execute("SELECT state FROM reminders WHERE id='history'").fetchone()[0] == 'sent'
    assert json.loads(tools.cal_events(ctx, id=master['id']+'@2026-12-12T10:00:00+00:00'))['status'] == 'not_found'


@pytest.mark.parametrize('representation', ['floating', 'tzid'])
def test_explicit_temporal_edit_replaces_time_and_keeps_wire_kind(tmp_path, representation):
    ctx, row, writes = floating_context(tmp_path, representation)
    result = json.loads(tools.cal_update(ctx, id=row['id'], start='2026-10-24T11:00', scope='all', confirm=True))
    assert result['status'] == 'updated' and len(writes) == 1
    import icalendar
    event = next(c for c in icalendar.Calendar.from_ical(writes[0]).walk() if c.name == 'VEVENT')
    instant = event.decoded('DTSTART')
    assert instant.hour == 11
    assert (instant.tzinfo is None) == (representation == 'floating')


def test_floating_observation_uses_retained_basis_after_owner_zone_change(tmp_path):
    ctx, row, _ = floating_context(tmp_path, 'floating')
    key = '2026-10-25T08:00:00+00:00'
    # Keep a real accepted pending occurrence intent across the observation.
    exception_id = ops._ensure_exception(ctx.store, row, key, {'title': 'Pending authored occurrence'})
    pending = ctx.store.add_intent('update', row['account_id'], row['calendar_id'], exception_id, {'changes': {'title': 'Pending authored occurrence'}})
    before = ctx.store.get_event(exception_id)
    adapter = ctx.providers.adapter_for(row['account_id'])
    original = row['raw_payload']
    adapter.list_calendars = lambda: [ctx.store.get_calendar(row['calendar_id'])]
    adapter.fetch = lambda cal, cursor='', tz=None: (providers.ics_to_rows(original, cal['id'], row['href'], '"v2"', tz), '', 'window', [row['href']])
    ctx.tz = model.get_tz('America/New_York')
    sync_account(ctx.store, ctx.providers, ctx.store.get_account(row['account_id']), ctx.tz)
    after = ctx.store.get_event(row['id'])
    assert (after['start_utc'], after['tz'], after['time_kind']) == (row['start_utc'], 'Europe/Berlin', 'floating')
    assert ctx.store.get_event(exception_id)['recurrence_id'] == before['recurrence_id']
    assert ctx.store.get_intent(pending['id'])['payload_json'] == pending['payload_json']
    shown = json.loads(tools.cal_events(ctx, start='2026-10-24', end='2026-10-25', calendars=row['calendar_id']))['events']
    assert shown[0]['start'][11:16] == '09:00'


def test_legacy_migration_does_not_rebase_records_or_pending_work(tmp_path):
    ctx, row, _ = floating_context(tmp_path, 'floating')
    pending = ctx.store.add_intent('update', row['account_id'], row['calendar_id'], row['id'], {'changes': {'title': 'Pending'}})
    with ctx.store._conn() as conn:
        payload = json.loads(pending['payload_json']); payload['desired'].pop('time_kind', None)
        old_payload = json.dumps(payload)
        conn.execute('UPDATE intents SET payload_json=? WHERE id=?', (old_payload, pending['id']))
        conn.execute("UPDATE events SET tz='' WHERE id=?", (row['id'],))
        conn.execute('ALTER TABLE events DROP COLUMN time_kind')
    reopened = Store(str(tmp_path))
    current = reopened.get_event(row['id'])
    assert (current['start_utc'], current['end_utc'], current['tz'], current['time_kind']) == (row['start_utc'], row['end_utc'], '', '')
    assert reopened.get_intent(pending['id'])['payload_json'] == old_payload


@pytest.mark.parametrize('owner_zone', ['Europe/Berlin', 'America/New_York'])
def test_legacy_floating_refresh_adopts_only_a_compatible_basis(tmp_path, owner_zone):
    ctx, row, _ = floating_context(tmp_path, 'floating')
    ctx.store.update_event(row['id'], {'tz': '', 'time_kind': ''})
    adapter = ctx.providers.adapter_for(row['account_id'])
    adapter.list_calendars = lambda: [ctx.store.get_calendar(row['calendar_id'])]
    adapter.fetch = lambda cal, cursor='', tz=None: (providers.ics_to_rows(row['raw_payload'], cal['id'], row['href'], row['etag'], tz), '', 'window', [row['href']])
    ctx.tz = model.get_tz(owner_zone)
    sync_account(ctx.store, ctx.providers, ctx.store.get_account(row['account_id']), ctx.tz)
    current = ctx.store.get_event(row['id'])
    assert (current['start_utc'], current['end_utc']) == (row['start_utc'], row['end_utc'])
    if owner_zone == 'Europe/Berlin':
        assert (current['tz'], current['time_kind']) == ('Europe/Berlin', 'floating')
    else:
        assert (current['tz'], current['time_kind']) == ('', '')
        assert 'no recorded parsing zone' in ctx.store.get_sync_state(row['calendar_id'])['last_error']


@pytest.mark.parametrize('representation', ['utc', 'date'])
def test_unrelated_title_preserves_utc_and_date_wire_kinds(tmp_path, representation):
    import icalendar
    ctx, row, writes = floating_context(tmp_path, representation)
    before = next(c for c in icalendar.Calendar.from_ical(row['raw_payload']).walk() if c.name == 'VEVENT')
    assert json.loads(tools.cal_update(ctx, id=row['id'], title='After', scope='all', confirm=True))['status'] == 'updated'
    after = next(c for c in icalendar.Calendar.from_ical(writes[-1]).walk() if c.name == 'VEVENT')
    for field in ('DTSTART', 'DTEND'):
        assert (after[field].to_ical(), dict(after[field].params)) == (before[field].to_ical(), dict(before[field].params))


@pytest.mark.parametrize('surface', ['tool', 'route'])
def test_all_day_date_only_edit_uses_the_recorded_calendar_zone(tmp_path, surface):
    ctx = context(tmp_path, 'Europe/Moscow')
    row = create(ctx, title='Dates', start='2026-12-10', end='2026-12-11', all_day=True)
    ctx.tz = model.get_tz('America/New_York')
    body = {'id': row['id'], 'start': '2026-12-12', 'end': '2026-12-13'}
    if surface == 'tool':
        result = json.loads(tools.cal_update(ctx, confirm=True, **body))
        assert result['status'] == 'updated'
    else:
        handlers = {}
        routes.register_routes(SimpleNamespace(register_route=lambda name, handler, **_: handlers.update({name: handler})), lambda: ctx)
        class Request:
            async def json(self): return body
        assert asyncio.run(handlers['event/update'](Request())).status_code == 200
    assert tuple(t.date().isoformat() for t in local_times(ctx.store.get_event(row['id']))) == ('2026-12-12', '2026-12-13')


def test_floating_occurrence_edit_keeps_canonical_id_and_floating_wire_after_travel(tmp_path):
    import icalendar
    ctx, row, writes = floating_context(tmp_path, 'floating')
    ctx.tz = model.get_tz('America/New_York')
    event = json.loads(tools.cal_events(ctx, start='2026-10-25', end='2026-10-26', calendars=row['calendar_id']))['events'][0]
    assert event['start'][11:16] == '09:00' and event['id'].endswith('@2026-10-25T08:00:00+00:00')
    result = json.loads(tools.cal_update(ctx, id=event['id'], start='2026-10-25T11:00', confirm=True))
    assert result['status'] == 'updated', result
    exception = ctx.store.exceptions_for(row['id'])[0]
    assert exception['recurrence_id'] == '2026-10-25T08:00:00+00:00'
    component = next(c for c in icalendar.Calendar.from_ical(writes[-1]).walk() if c.name == 'VEVENT' and c.get('RECURRENCE-ID'))
    assert component.decoded('RECURRENCE-ID') == datetime(2026, 10, 25, 9)
    assert component.decoded('DTSTART') == datetime(2026, 10, 25, 11)
