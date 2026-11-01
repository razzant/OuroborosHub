"""UNTIL retains the DTSTART value kind at native and fixed-zone provider boundaries."""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import icalendar
import pytest
from dateutil.rrule import rrulestr

from test_calendar_temporal_roundtrip import floating_context
from test_calendar_provider_divergence import FakeCalDAV, FakeGoogle, add_google_calendar, run_open_intents
import links
import model
import ops
import providers
import providers_google
import tools
from scripts.worker import reconcile_calendar


def native_context(tmp_path, kind, until=False):
    ctx, row, _ = floating_context(tmp_path, kind)
    wire = row['raw_payload']
    if until:
        bound = '20261027' if kind == 'date' else '20261027T070000Z' if kind == 'utc' else (
            '20261027T080000Z' if kind == 'tzid' else '20261027T090000')
        wire = wire.replace('COUNT=4', 'UNTIL=' + bound)
        parsed = providers.ics_to_rows(wire, row['calendar_id'], row['href'], row['etag'], ctx.tz)[0]
        ctx.store.update_event(row['id'], parsed)
        row = ctx.store.get_event(row['id'])
    server = FakeCalDAV()
    server.resources[row['href']] = (wire, row['etag'])
    adapter = server.attach(providers.YandexAdapter('fixture', 'fixture'))
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda _: adapter)
    return ctx, row, server


def master_component(wire):
    return next(event for event in icalendar.Calendar.from_ical(wire).walk('VEVENT')
                if event.get('RECURRENCE-ID') is None)


def strict_native_occurrences(wire):
    event = master_component(wire)
    start = event.decoded('DTSTART')
    if isinstance(start, date) and not isinstance(start, datetime):
        start = datetime.combine(start, datetime.min.time())
    return list(rrulestr(event['RRULE'].to_ical().decode('utf-8'), dtstart=start))


@pytest.mark.parametrize('kind', ['floating', 'tzid', 'utc', 'date'])
@pytest.mark.parametrize('action', ['split', 'delete'])
def test_following_native_until_matches_start_and_readback(tmp_path, kind, action):
    ctx, row, server = native_context(tmp_path, kind)
    shown = json.loads(tools.cal_events(ctx, start='2026-10-26', end='2026-10-27'))['events'][0]
    if action == 'split':
        result = json.loads(tools.cal_update(ctx, id=shown['id'], title='Later segment', scope='following', confirm=True))
        assert result['status'] == 'updated', result
    else:
        result = json.loads(tools.cal_delete(ctx, id=shown['id'], scope='following', confirm=True))
        assert result['status'] == 'deleted', result
    run_open_intents(ctx, 3)
    wire, etag = server.resources[row['href']]
    native = master_component(wire)
    original = master_component(row['raw_payload'])
    assert (native['DTSTART'].to_ical(), dict(native['DTSTART'].params)) == (
        original['DTSTART'].to_ical(), dict(original['DTSTART'].params))
    assert [value.day for value in strict_native_occurrences(wire)] == [24, 25]
    until = native.decoded('RRULE')['UNTIL'][0]
    if kind == 'date':
        assert type(until) is date
    else:
        assert (until.tzinfo is None) == (kind == 'floating')
    parsed = providers.ics_to_rows(wire, row['calendar_id'], row['href'], etag, ctx.tz)[0]
    parsed['id'] = row['id']
    projected = ops._expand_master(parsed, datetime(2026, 10, 24, tzinfo=ctx.tz),
                                   datetime(2026, 10, 30, tzinfo=ctx.tz), [], strict=True)
    assert len(projected) == 2


@pytest.mark.parametrize('zone', ['Europe/Berlin', 'America/New_York'])
@pytest.mark.parametrize('kind', ['floating', 'tzid'])
def test_local_until_prunes_only_overrides_after_wall_cutoff(zone, kind):
    tz = model.get_tz(zone)
    suffix = '' if kind == 'floating' else ';TZID=' + zone
    until = '20261126T085959' if kind == 'floating' else (
        datetime(2026, 11, 26, 8, 59, 59, tzinfo=tz).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:series',
             f'DTSTART{suffix}:20261124T080000', f'DTEND{suffix}:20261124T090000',
             'RRULE:FREQ=HOURLY;COUNT=100', 'SUMMARY:Series', 'END:VEVENT']
    for stamp, title in [('20261126T080000', 'Keep'), ('20261126T090000', 'Remove')]:
        lines.extend(['BEGIN:VEVENT', 'UID:series', f'RECURRENCE-ID{suffix}:{stamp}',
                      f'DTSTART{suffix}:{stamp}', 'DURATION:PT30M', 'SUMMARY:' + title, 'END:VEVENT'])
    lines.extend(['END:VCALENDAR', ''])
    raw = '\r\n'.join(lines)
    row = providers.ics_to_rows(raw, 'cal', 'href', 'v1', tz)[0]
    row['rrule'] = 'FREQ=HOURLY;UNTIL=' + until
    wire = providers.row_to_ics(row, changed_fields={'rrule'})
    exceptions = [event for event in icalendar.Calendar.from_ical(wire).walk('VEVENT') if event.get('RECURRENCE-ID')]
    assert [str(event['SUMMARY']) for event in exceptions] == ['Keep']
    assert strict_native_occurrences(wire)[-1].hour == 8


@pytest.mark.parametrize('move', ['none', 'hour', 'across_dst'])
def test_floating_split_retains_and_shifts_local_until(tmp_path, move):
    ctx, row, server = native_context(tmp_path, 'floating', until=True)
    selected = json.loads(tools.cal_events(ctx, start='2026-10-26', end='2026-10-27'))['events'][0]
    changes = {'start': '2026-10-26T10:00', 'end': '2026-10-26T11:00'} if move == 'hour' else (
        {'start': '2026-10-24T09:00', 'end': '2026-10-24T10:00'} if move == 'across_dst' else {})
    result = json.loads(tools.cal_update(ctx, id=selected['id'], title='New segment', scope='following', confirm=True, **changes))
    assert result['status'] == 'updated', result
    run_open_intents(ctx, 3)
    old = strict_native_occurrences(server.resources[row['href']][0])
    new_wire = next(wire for href, (wire, _) in server.resources.items() if href != row['href'])
    new = strict_native_occurrences(new_wire)
    assert [(value.day, value.hour) for value in old] == [(24, 9), (25, 9)]
    assert [(value.day, value.hour) for value in new] == ([(24, 9), (25, 9)] if move == 'across_dst' else [
        (26, 10 if move == 'hour' else 9), (27, 10 if move == 'hour' else 9)])


@pytest.mark.parametrize('action', ['birth', 'split', 'delete'])
def test_floating_local_until_google_copy_has_zoned_wire_and_no_false_divergence(tmp_path, action):
    ctx, source, caldav = native_context(tmp_path, 'floating', until=True)
    source_adapter = ctx.providers.adapter_for(source['account_id'])
    cid = add_google_calendar(ctx.store)
    google = FakeGoogle()
    adapter = google.attach(providers_google.GoogleAdapter('fixture', 'token', 'refresh', model.now_utc(), 'client', '', None))
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda aid: source_adapter if aid == source['account_id'] else adapter)
    result = json.loads(tools.cal_update(ctx, id=source['id'], calendars=[source['calendar_id'], cid], scope='all', confirm=True))
    assert result['status'] == 'updated', result
    copy = ctx.store.get_event(result['reassign']['added'][0]['event_id'])
    wire = google.events[copy['external_id']]
    start = datetime.fromisoformat(wire['start']['dateTime']).astimezone(model.get_tz(wire['start']['timeZone']))
    native = list(rrulestr(next(rule[6:] for rule in wire['recurrence'] if rule.startswith('RRULE:')), dtstart=start))
    assert [(value.day, value.hour) for value in native] == [(24, 9), (25, 9), (26, 9), (27, 9)]
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
    saved = ctx.store.get_event(copy['id'])
    assert saved['link_state'] == links.FOLLOWING
    assert not links.changed(saved, links.projection_changes(ctx.store, source, saved))
    # Travel changes only the display: Google keeps the recorded fixed-zone projection.
    ctx.tz = model.get_tz('America/New_York')
    calls = list(google.calls)
    shown = json.loads(tools.cal_events(ctx, start='2026-10-27', end='2026-10-28'))['events']
    assert {event['calendar_id']: event['start'][11:16] for event in shown} == {source['calendar_id']: '09:00', cid: '04:00'}
    assert google.calls == calls

    if action != 'birth':
        occurrence = source['id'] + '@2026-10-26T08:00:00+00:00'
        if action == 'split':
            changed = json.loads(tools.cal_update(ctx, id=occurrence, title='Following', scope='following', confirm=True))
            assert changed['status'] == 'updated', changed
        else:
            changed = json.loads(tools.cal_delete(ctx, id=occurrence, scope='following', confirm=True))
            assert changed['status'] == 'deleted', changed
        run_open_intents(ctx, 3)
        google_slots = []
        for event in google.events.values():
            first = datetime.fromisoformat(event['start']['dateTime']).astimezone(model.get_tz(event['start']['timeZone']))
            rule = next(rule[6:] for rule in event['recurrence'] if rule.startswith('RRULE:'))
            google_slots.extend((slot.day, slot.hour) for slot in rrulestr(rule, dtstart=first))
        expected = [(day, 9) for day in ([24, 25, 26, 27] if action == 'split' else [24, 25])]
        assert sorted(google_slots) == expected
        assert sorted((slot.day, slot.hour) for wire, _ in caldav.resources.values()
                      for slot in strict_native_occurrences(wire)) == expected
        reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
        masters = ctx.store.window(datetime(2026, 10, 24, tzinfo=ctx.tz), datetime(2026, 10, 30, tzinfo=ctx.tz))
        assert all(master['link_state'] == links.FOLLOWING for master in masters)
