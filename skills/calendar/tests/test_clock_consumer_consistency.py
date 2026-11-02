"""The public edit, provider acceptance and delivery retain one effective event clock."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import icalendar
import pytest

from test_calendar_temporal_roundtrip import context, create
from test_calendar_provider_divergence import caldav_context
import model
import reminders
import routes
import tools


@pytest.mark.parametrize('master_availability,child_availability', [('busy', 'soft'), ('soft', 'busy')])
@pytest.mark.parametrize('owner_zone', ['Europe/Berlin', 'America/New_York'])
def test_exception_availability_clock_survives_actual_delivery(tmp_path, master_availability, child_availability, owner_zone):
    ctx = context(tmp_path, 'Europe/Berlin')
    master = create(ctx, title='Series', start='2026-12-10T09:00', end='2026-12-10T10:00',
                    rrule='FREQ=DAILY;COUNT=3', availability=master_availability, reminders=[0])
    slot = '2026-12-11T08:00:00+00:00'
    changed = json.loads(tools.cal_update(ctx, id=master['id']+'@'+slot, title='Override',
                                         availability=child_availability, confirm=True))
    assert changed['status'] == 'updated'
    ctx.tz = model.get_tz(owner_zone)
    rows = json.loads(tools.cal_events(ctx, start='2026-12-11', end='2026-12-12'))['events']
    shown, = [row for row in rows if row['title'] == 'Override']
    expected = datetime(2026, 12, 11, 9, tzinfo=ctx.tz) if child_availability == 'soft' else model.parse_stored(slot)
    assert model.parse_stored(shown['start']) == expected
    ctx.store.drop_scheduled_reminders()
    reminders.plan(ctx.store, ctx.occurrences, now=expected-timedelta(minutes=1))
    sent = []
    channel = SimpleNamespace(send=lambda key, text: (sent.append(text) or ('sent', 'synthetic')))
    result = reminders.deliver_due(ctx.store, channel, ctx.tz, now=expected)
    assert result['sent'] == 1 and any('Override' in text for text in sent)
    assert reminders.deliver_due(ctx.store, channel, ctx.tz, now=expected)['sent'] == 0


@pytest.mark.parametrize('owner_zone', ['America/New_York', 'Asia/Tokyo'])
@pytest.mark.parametrize('flag', [True, None, False])
def test_bare_date_edit_uses_requested_civil_day(tmp_path, owner_zone, flag):
    ctx = context(tmp_path, 'Europe/Berlin')
    row = create(ctx, title='Timed', start='2026-12-10T09:00', end='2026-12-10T10:00')
    ctx.tz = model.get_tz(owner_zone)
    result = json.loads(tools.cal_update(ctx, id=row['id'], start='2026-12-13', confirm=True,
                                        **({} if flag is None else {'all_day': flag})))
    assert result['status'] == 'updated'
    assert result['event']['all_day'] is (flag is not False)
    assert result['event']['start'].startswith('2026-12-13T00:00')


@pytest.mark.parametrize('explicit_zone', [False, True])
def test_provider_choice_retains_author_zone_unless_native_tzid_replaces_it(explicit_zone):
    ctx, cid, server = caldav_context()
    ctx.tz = model.get_tz('Europe/Berlin')
    made = json.loads(tools.cal_create(ctx, title='Original', start='2027-03-24T09:00', end='2027-03-24T10:00',
                                      calendars=[cid], confirm=True))
    eid = made['event']['id']; href = ctx.store.get_event(eid)['href']
    raw, _ = server.resources[href]
    raw = raw.replace('SUMMARY:Original', 'SUMMARY:Remote')
    expected_zone = 'America/New_York' if explicit_zone else 'Europe/Berlin'
    if explicit_zone:
        parsed = icalendar.Calendar.from_ical(raw)
        event = parsed.walk('VEVENT')[0]
        for field, hour in [('DTSTART', 9), ('DTEND', 10)]:
            event.pop(field, None)
            event.add(field, datetime(2027, 3, 24, hour, tzinfo=model.get_tz(expected_zone)))
        raw = parsed.to_ical().decode()
    server.resources[href] = (raw, '"remote-v2"')
    assert json.loads(tools.cal_update(ctx, id=eid, title='Mine', confirm=True))['status'] == 'conflict'
    comparison = json.loads(tools.cal_events(ctx, id=eid))['event']['conflict']
    assert json.loads(tools.cal_update(ctx, id=eid, resolution='provider',
                                      resolution_token=comparison['resolution_token'], confirm=True))['status'] == 'resolved'
    accepted = ctx.store.get_event(eid)
    assert accepted['tz'] == expected_zone and accepted['title'] == 'Remote'
    assert json.loads(tools.cal_update(ctx, id=eid, rrule='FREQ=WEEKLY;COUNT=3', scope='all', confirm=True))['status'] == 'updated'
    shown = json.loads(tools.cal_events(ctx, start='2027-03-20', end='2027-04-10'))['events']
    assert len(shown) == 3
    assert {model.parse_stored(row['start']).astimezone(model.get_tz(expected_zone)).hour for row in shown} == {9}


@pytest.mark.parametrize('assignments,expected', [
    (['uncertain'], 'uncertain'), (['done', 'uncertain'], 'uncertain'),
    (['pending', 'uncertain'], 'uncertain'), (['failed'], 'failed'),
    (['done'], 'ok'), (['pending'], 'pending'), (['conflict'], 'conflict')])
def test_widget_route_preserves_unconfirmed_outcome(assignments, expected):
    assert routes._aggregate([{'status': state} for state in assignments]) == expected


@pytest.mark.parametrize('master_kind,child_kind', [
    ('tzid', 'date'), ('tzid', 'floating'), ('tzid', 'absolute'), ('floating', 'date'),
    ('floating', 'absolute'), ('date', 'absolute'), ('date', 'floating')])
@pytest.mark.parametrize('owner_zone', ['Europe/Berlin', 'America/New_York'])
def test_provider_choice_preserves_original_slot_through_next_sync(tmp_path, master_kind, child_kind, owner_zone):
    from test_effective_event_clock import mixed_context
    from scripts.worker import reconcile_calendar
    ctx, master, child, server = mixed_context(tmp_path, master_kind, child_kind)
    ctx.tz = model.get_tz(owner_zone)
    def visible():
        return json.loads(tools.cal_events(ctx, start='2026-12-09', end='2026-12-14'))['events']
    before = visible()
    raw, _ = server.resources[master['href']]
    server.resources[master['href']] = (raw.replace('SUMMARY:Override', 'SUMMARY:Remote override'), '"provider-v2"')
    assert json.loads(tools.cal_update(ctx, id=child['id'], title='Local conflict', confirm=True))['status'] == 'conflict'
    comparison = json.loads(tools.cal_events(ctx, id=child['id']))['event']['conflict']
    assert json.loads(tools.cal_update(ctx, id=child['id'], resolution='provider',
                                      resolution_token=comparison['resolution_token'], confirm=True))['status'] == 'resolved'
    saved = ctx.store.get_event(child['id'])
    assert saved['recurrence_id'] == child['recurrence_id']
    assert len(visible()) == len(before)
    assert sum(row['title'] == 'Remote override' for row in visible()) == 1
    calendar = ctx.store.get_calendar(master['calendar_id'])
    reconcile_calendar(ctx.store, ctx.providers, ctx.providers.adapter_for(calendar['account_id']), calendar, ctx.tz)
    current, = ctx.store.exceptions_for(master['id'])
    assert (current['id'], current['recurrence_id']) == (child['id'], child['recurrence_id'])
    assert len(visible()) == len(before)


@pytest.mark.parametrize('master_kind', ['tzid', 'floating', 'date'])
def test_provider_choice_keeps_exception_author_zone_distinct_from_master(tmp_path, master_kind):
    from test_effective_event_clock import mixed_context
    ctx, master, child, server = mixed_context(tmp_path, master_kind, 'absolute')
    # A retained absolute exception may have been authored in a different zone;
    # its instant and master-owned recurrence address do not change with that metadata.
    ctx.store.update_event(child['id'], {'tz': 'America/Los_Angeles'})
    raw, _ = server.resources[master['href']]
    server.resources[master['href']] = (raw.replace('SUMMARY:Override', 'SUMMARY:Remote override'), '"provider-v2"')
    assert json.loads(tools.cal_update(ctx, id=child['id'], title='Local conflict', confirm=True))['status'] == 'conflict'
    comparison = json.loads(tools.cal_events(ctx, id=child['id']))['event']['conflict']
    assert json.loads(tools.cal_update(ctx, id=child['id'], resolution='provider',
                                      resolution_token=comparison['resolution_token'], confirm=True))['status'] == 'resolved'
    saved = ctx.store.get_event(child['id'])
    assert saved['tz'] == 'America/Los_Angeles'
    assert saved['start_utc'] == child['start_utc']
    assert saved['recurrence_id'] == child['recurrence_id']


@pytest.mark.parametrize('master_all_day', [False, True])
def test_google_mixed_provider_choice_preserves_original_instance_after_travel(master_all_day):
    from test_calendar_provider_divergence import google_context, sync
    ctx, cid, server = google_context()
    ctx.tz = model.get_tz('Europe/Berlin')
    def edge(day, all_day, hour=9):
        return {'date': day} if all_day else {'dateTime': day+f'T{hour:02d}:00:00+01:00', 'timeZone': 'Europe/Berlin'}
    master = {'id': 'mixed-master', 'iCalUID': 'mixed-series', 'etag': 'v1', 'summary': 'Master',
              'start': edge('2026-12-10', master_all_day),
              'end': edge('2026-12-11' if master_all_day else '2026-12-10', master_all_day, 10),
              'recurrence': ['RRULE:FREQ=DAILY;COUNT=3'], 'status': 'confirmed'}
    child = {'id': 'mixed-exception', 'iCalUID': 'mixed-series', 'etag': 'v1', 'summary': 'Override',
             'start': edge('2026-12-11', not master_all_day),
             'end': edge('2026-12-11' if master_all_day else '2026-12-12', not master_all_day, 10),
             'originalStartTime': edge('2026-12-11', master_all_day), 'recurringEventId': master['id'], 'status': 'confirmed'}
    server.events.update({master['id']: master, child['id']: child})
    sync(ctx, cid)
    source = ctx.store.find_by_external(cid, external_id=master['id'])
    original, = ctx.store.exceptions_for(source['id'])
    ctx.tz = model.get_tz('America/New_York')
    server.events[child['id']].update(summary='Remote override', etag='v2')
    assert json.loads(tools.cal_update(ctx, id=original['id'], title='Local conflict', confirm=True))['status'] == 'conflict'
    comparison = json.loads(tools.cal_events(ctx, id=original['id']))['event']['conflict']
    assert json.loads(tools.cal_update(ctx, id=original['id'], resolution='provider',
                                      resolution_token=comparison['resolution_token'], confirm=True))['status'] == 'resolved'
    for after_sync in (False, True):
        if after_sync:
            sync(ctx, cid)
        saved, = ctx.store.exceptions_for(source['id'])
        assert (saved['id'], saved['recurrence_id']) == (original['id'], original['recurrence_id'])
        rows = json.loads(tools.cal_events(ctx, start='2026-12-11', end='2026-12-12'))['events']
        assert len(rows) == 1 and rows[0]['title'] == 'Remote override'
