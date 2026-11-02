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
