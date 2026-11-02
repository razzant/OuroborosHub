"""Original recurrence coordinates and the addressed event's effective clock stay independent."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import icalendar
import pytest

from test_calendar_temporal_roundtrip import context, create
from test_calendar_provider_divergence import FakeCalDAV
import model
import ops
import providers
import reminders
import routes
import tools

UTC = timezone.utc
BERLIN = model.get_tz('Europe/Berlin')


def mixed_context(tmp_path, master_kind, child_kind):
    ctx = context(tmp_path, 'Europe/Berlin')
    aid, cid = 'yandex:fixture', 'yandex:fixture:work'
    href = 'https://caldav.yandex.ru/calendars/fixture/work/mixed.ics'
    ctx.store.upsert_account({'id': aid, 'provider': 'yandex', 'login': 'fixture'})
    ctx.store.upsert_calendar({'id': cid, 'account_id': aid, 'provider': 'yandex', 'name': 'Mixed', 'href': href.rsplit('/', 1)[0] + '/'})
    def temporal(field, kind, day, hour):
        if kind == 'date':
            return f'{field};VALUE=DATE:202612{day:02d}'
        suffix = ';TZID=Europe/Berlin' if kind == 'tzid' else ''
        return f'{field}{suffix}:202612{day:02d}T{hour:02d}0000' + ('Z' if kind == 'absolute' else '')
    wire = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:mixed',
        temporal('DTSTART', master_kind, 10, 9), temporal('DTEND', master_kind, 11 if master_kind == 'date' else 10, 10),
        'RRULE:FREQ=DAILY;COUNT=3', 'SUMMARY:Master', 'END:VEVENT', 'BEGIN:VEVENT', 'UID:mixed',
        temporal('RECURRENCE-ID', master_kind, 11, 9), temporal('DTSTART', child_kind, 11, 10),
        temporal('DTEND', child_kind, 12 if child_kind == 'date' else 11, 11), 'SUMMARY:Override',
        'END:VEVENT', 'END:VCALENDAR', ''])
    master_row, child_row = providers.ics_to_rows(wire, cid, href, '"fixture"', ctx.tz)
    master = ctx.store.insert_event(master_row)
    child = ctx.store.insert_event({**child_row, 'master_id': master['id'], 'reminders_json': '[0]'})
    ctx.store.set_setting(reminders.MODE_KEY, {cid: True})
    server = FakeCalDAV(); server.resources[href] = (wire, '"fixture"')
    adapter = server.attach(providers.YandexAdapter('fixture', 'fixture'))
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda _: adapter)
    return ctx, master, child, server


@pytest.mark.parametrize('owner_zone', ['Europe/Berlin', 'America/New_York'])
@pytest.mark.parametrize('master_kind,child_kind', [
    ('tzid', 'date'), ('tzid', 'floating'), ('floating', 'absolute'),
    ('floating', 'date'), ('date', 'absolute'), ('date', 'floating'),
])
def test_effective_override_clock_agrees_in_list_detail_busy_and_delivery(tmp_path, master_kind, child_kind, owner_zone):
    ctx, master, child, server = mixed_context(tmp_path, master_kind, child_kind)
    ctx.tz = model.get_tz(owner_zone)
    begin = datetime(2026, 12, 11, 0 if child_kind == 'date' else 10, tzinfo=UTC if child_kind == 'absolute' else ctx.tz)
    end = begin + (timedelta(days=1) if child_kind == 'date' else timedelta(hours=1))
    rows = json.loads(tools.cal_events(ctx, start='2026-12-10', end='2026-12-13'))['events']
    shown, = [row for row in rows if row['title'] == 'Override']
    direct = json.loads(tools.cal_events(ctx, id=child['id']))['event']
    for event in (shown, direct):
        assert model.parse_stored(event['start']) == begin
        assert model.parse_stored(event['end']) == end
    occurrences = [row for row in ctx.occurrences(begin, end) if row['id'] == child['id']]
    assert ops.busy_intervals(occurrences)[0] == [(begin, end)]
    ctx.store.drop_scheduled_reminders()
    reminders.plan(ctx.store, ctx.occurrences, now=begin-timedelta(minutes=1))
    sent = []
    channel = SimpleNamespace(send=lambda key, text: (sent.append((key, text)) or ('sent', 'fixture')))
    delivered = reminders.deliver_due(ctx.store, channel, ctx.tz, now=begin)
    assert delivered['sent'] == 1 and 'Override' in sent[0][1]
    assert reminders.deliver_due(ctx.store, channel, ctx.tz, now=begin+timedelta(seconds=5))['sent'] == 0
    saved = ctx.store.get_event(child['id'])
    assert {key: saved[key] for key in ('id', 'uid', 'master_id', 'recurrence_id', 'start_utc', 'end_utc')} == {
        key: child[key] for key in ('id', 'uid', 'master_id', 'recurrence_id', 'start_utc', 'end_utc')}
    assert server.calls == []  # Reading/travel/planning never writes a remote projection.


def update_route(ctx, body):
    handlers = {}
    routes.register_routes(SimpleNamespace(register_route=lambda name, handler, **_: handlers.update({name: handler})), lambda: ctx)
    class Request:
        async def json(self): return body
    response = asyncio.run(handlers['event/update'](Request()))
    assert response.status_code == 200
    return json.loads(response.body)


@pytest.mark.parametrize('surface', ['tool', 'route'])
@pytest.mark.parametrize('owner_zone', ['Europe/Berlin', 'America/New_York'])
def test_absolute_child_end_only_edit_keeps_its_start_and_original_slot(tmp_path, surface, owner_zone):
    ctx, master, child, server = mixed_context(tmp_path, 'floating', 'absolute')
    ctx.tz = model.get_tz(owner_zone)
    requested = datetime(2026, 12, 11, 12, tzinfo=UTC)
    body = {'id': child['id'], 'end': requested.astimezone(ctx.tz).isoformat()}
    if surface == 'tool':
        preview = json.loads(tools.cal_update(ctx, **body))
        assert preview['status'] == 'needs_confirm'
        assert model.parse_stored(preview['changes']['end_utc']) == requested
        result = json.loads(tools.cal_update(ctx, **body, confirm=True))
        assert result['status'] == 'updated', result
    else:
        result = update_route(ctx, body)
        assert result['status'] == 'ok', result
    saved = ctx.store.get_event(child['id'])
    assert saved['start_utc'] == child['start_utc']
    assert saved['end_utc'] == model.iso_utc(requested)
    assert (saved['id'], saved['uid'], saved['recurrence_id']) == (child['id'], child['uid'], child['recurrence_id'])
    native = next(event for event in icalendar.Calendar.from_ical(server.resources[master['href']][0]).walk('VEVENT') if event.get('RECURRENCE-ID'))
    assert native.decoded('DTSTART') == model.parse_stored(child['start_utc'])
    assert native.decoded('DTEND') == requested
    assert native.decoded('RECURRENCE-ID') == datetime(2026, 12, 11, 9)


@pytest.mark.parametrize('surface', ['tool', 'route'])
def test_end_only_preserves_second_dst_fold_instant(tmp_path, surface):
    ctx = context(tmp_path, 'Europe/Berlin')
    row = create(ctx, title='Second 02:30', start='2026-10-25T02:30:00+01:00', end='2026-10-25T03:30:00+01:00')
    body = {'id': row['id'], 'end': '2026-10-25T04:30:00+01:00'}
    if surface == 'tool':
        assert json.loads(tools.cal_update(ctx, **body, confirm=True))['status'] == 'updated'
    else:
        assert update_route(ctx, body)['status'] == 'ok'
    assert ctx.store.get_event(row['id'])['start_utc'] == '2026-10-25T01:30:00+00:00'


@pytest.mark.parametrize('source', ['floating', 'soft_date'])
@pytest.mark.parametrize('surface', ['tool', 'route'])
def test_civil_conversion_is_anchored_once_after_travel(tmp_path, source, surface):
    if source == 'floating':
        ctx, row, _, server = mixed_context(tmp_path, 'floating', 'absolute')
        body = {'id': row['id'], 'scope': 'all', 'start': '2026-12-13', 'end': '2026-12-14', 'all_day': True}
    else:
        ctx = context(tmp_path, 'Europe/Moscow')
        row = create(ctx, title='Soft dates', start='2026-12-10', end='2026-12-11', all_day=True, availability='soft')
        body = {'id': row['id'], 'start': '2026-12-13T00:00:00-05:00', 'end': '2026-12-14T00:00:00-05:00'}
    ctx.tz = model.get_tz('America/New_York')
    if surface == 'tool':
        result = json.loads(tools.cal_update(ctx, **body, confirm=True)); assert result['status'] == 'updated', result
    else:
        result = update_route(ctx, body); assert result['status'] == 'ok', result
    saved = ctx.store.get_event(row['id'])
    public = model.event_public(saved, ctx.tz)
    assert public['start'] == '2026-12-13T00:00:00-05:00'
    assert public['end'] == '2026-12-14T00:00:00-05:00'
    assert model.parse_stored(saved['start_utc']).astimezone(model.get_tz(saved['tz'])).date() == date(2026, 12, 13)


def test_soft_single_event_uses_same_wall_clock_for_listing_and_end_edit(tmp_path):
    ctx = context(tmp_path, 'Pacific/Kiritimati')
    row = create(ctx, title='Local routine', start='2026-12-10T00:30', end='2026-12-10T01:30', availability='soft')
    ctx.tz = model.get_tz('America/New_York')
    shown, = json.loads(tools.cal_events(ctx, start='2026-12-10', end='2026-12-11'))['events']
    assert shown['start'] == '2026-12-10T00:30:00-05:00'
    result = json.loads(tools.cal_update(ctx, id=row['id'], end='2026-12-10T02:30', confirm=True))
    assert result['status'] == 'updated', result
    assert ctx.store.get_event(row['id'])['start_utc'] == row['start_utc']
    assert result['event']['end'] == '2026-12-10T02:30:00-05:00'


@pytest.mark.parametrize('zone', ['UTC', 'Europe/Berlin'])
@pytest.mark.parametrize('scope', ['all', 'following'])
@pytest.mark.parametrize('terminal', ['sent', 'unknown'])
def test_calendar_move_readdresses_slots_and_preserves_notice_custody(tmp_path, zone, scope, terminal):
    ctx = context(tmp_path, zone)
    local = lambda month, day, hour=9: datetime(2026, month, day, hour, tzinfo=ctx.tz)
    row = create(ctx, title='Weekly', start=local(10, 21).isoformat(), end=local(10, 21, 10).isoformat(),
                 rrule='FREQ=WEEKLY;COUNT=5', reminders=[0])
    cancelled = model.iso_utc(local(11, 4)); edited = model.iso_utc(local(11, 11))
    assert json.loads(tools.cal_delete(ctx, id=row['id']+'@'+cancelled, confirm=True))['status'] == 'deleted'
    assert json.loads(tools.cal_update(ctx, id=row['id']+'@'+edited, title='Edited',
        start=local(11, 11, 12).isoformat(), end=local(11, 11, 13).isoformat(), confirm=True))['status'] == 'updated'
    override = next(child for child in ctx.store.exceptions_for(row['id']) if child['title'] == 'Edited')
    ctx.store.update_event(row['id'], {'exdates': model.iso_utc(local(11, 18)), 'rdates': model.iso_utc(local(11, 25))})
    ctx.store.drop_scheduled_reminders()
    old_time = model.iso_utc(local(11, 11, 12))
    notice = reminders.notice_id_for(row['id'], old_time, 0, edited)
    ctx.store.schedule_reminder(row['id'], old_time, 0, old_time, notice, edited)
    with ctx.store._conn() as connection:
        connection.execute('UPDATE reminders SET state=? WHERE notice_id=?', (terminal, notice))
    address = row['id'] if scope == 'all' else row['id']+'@'+row['start_utc']
    result = json.loads(tools.cal_update(ctx, id=address, start=local(10, 28).isoformat(),
        end=local(10, 28, 10).isoformat(), scope=scope, confirm=True))
    assert result['status'] == 'updated', result
    changed = ctx.store.get_event(override['id'])
    master = ctx.store.get_event(changed['master_id'])
    assert master['start_utc'] == model.iso_utc(local(10, 28))
    assert changed['recurrence_id'] == model.iso_utc(local(11, 18))
    expected = local(11, 18 if scope == 'all' else 11, 12)
    assert changed['start_utc'] == model.iso_utc(expected)
    assert master['exdates'] == model.iso_utc(local(11, 25))
    assert master['rdates'] == model.iso_utc(local(12, 2))
    shown = json.loads(tools.cal_events(ctx, start='2026-10-21', end='2026-12-10'))['events']
    assert len(shown) == 4 and len([item for item in shown if item['title'] == 'Edited']) == 1
    occurrences = ctx.occurrences(local(10, 21), local(12, 10))
    assert model.iso_utc(local(11, 11)) not in [event['occurrence_start_utc'] for event in occurrences]
    direct = json.loads(tools.cal_events(ctx, id=master['id']+'@'+changed['recurrence_id']))
    assert direct['event']['title'] == 'Edited' and model.parse_stored(direct['event']['start']) == expected
    ctx.store.drop_scheduled_reminders()
    reminders.plan(ctx.store, ctx.occurrences, now=expected-timedelta(minutes=1))
    sent = []
    channel = SimpleNamespace(send=lambda key, text: (sent.append((key, text)) or ('sent', 'fixture')))
    delivery = reminders.deliver_due(ctx.store, channel, ctx.tz, now=expected)
    assert delivery['sent'] == (1 if scope == 'all' else 0), (delivery, sent)
    with ctx.store._conn() as connection:
        assert connection.execute('SELECT state FROM reminders WHERE notice_id=?', (notice,)).fetchone()[0] == terminal


@pytest.mark.parametrize('master_kind,child_kind', [('date', 'absolute'), ('floating', 'absolute'), ('tzid', 'date')])
def test_following_end_only_retains_the_effective_cut_start_after_travel(tmp_path, master_kind, child_kind):
    ctx, master, child, server = mixed_context(tmp_path, master_kind, child_kind)
    ctx.tz = model.get_tz('America/New_York')
    end = '2026-12-13' if child_kind == 'date' else '2026-12-11T12:00:00+00:00'
    result = json.loads(tools.cal_update(ctx, id=child['id'], end=end, scope='following', confirm=True))
    assert result['status'] == 'updated', result
    saved = ctx.store.get_event(child['id'])
    assert saved['start_utc'] == child['start_utc']
    new_master = ctx.store.get_event(saved['master_id'])
    native = next(event for event in icalendar.Calendar.from_ical(server.resources[new_master['href']][0]).walk('VEVENT') if event.get('RECURRENCE-ID'))
    if child_kind == 'date':
        assert model.event_public(saved, ctx.tz)['end'].startswith('2026-12-13T00:00')
        assert (native.decoded('DTSTART'), native.decoded('DTEND')) == (date(2026, 12, 11), date(2026, 12, 13))
    else:
        assert saved['end_utc'] == '2026-12-11T12:00:00+00:00'
        assert native.decoded('DTSTART') == model.parse_stored(child['start_utc'])
        assert native.decoded('DTEND') == datetime(2026, 12, 11, 12, tzinfo=UTC)
