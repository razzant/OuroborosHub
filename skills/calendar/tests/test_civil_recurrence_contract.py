"""Civil dates, floating moves and recurrence sets keep one meaning across consumers."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_calendar_temporal_roundtrip import context, create
from test_recurrence_until_semantics import native_context
import model
import ops
import providers
import reminders
import tools

UTC = timezone.utc


def listed(ctx, start, end):
    return json.loads(tools.cal_events(ctx, start=start, end=end))


@pytest.mark.parametrize('start,end', [
    ('2026-12-13', '2026-12-14'),
    ('2026-12-13T00:00:00+03:00', '2026-12-14T00:00:00+03:00'),
    ('2026-12-13T00:00', '2026-12-14T00:00'),
])
def test_all_day_edit_uses_owner_date_and_retains_recorded_basis(tmp_path, start, end):
    ctx = context(tmp_path, 'America/New_York')
    row = create(ctx, title='Date', start='2026-12-10', end='2026-12-11', all_day=True)
    ctx.tz = model.get_tz('Europe/Moscow')
    result = json.loads(tools.cal_update(ctx, id=row['id'], start=start, end=end, confirm=True))
    assert result['status'] == 'updated', result
    saved = ctx.store.get_event(row['id'])
    assert saved['tz'] == 'America/New_York'
    assert model.parse_stored(saved['start_utc']).astimezone(model.get_tz(saved['tz'])).date().isoformat() == '2026-12-13'
    assert result['event']['start'].startswith('2026-12-13T00:00')
    assert 'DTSTART;VALUE=DATE:20261213' in providers.row_to_ics(saved)


def test_date_override_uses_its_own_time_kind_for_reads_availability_and_reminders(tmp_path):
    ctx = context(tmp_path, 'Europe/Berlin')
    row = create(ctx, title='Whole days', start='2026-12-10', end='2026-12-11', all_day=True,
                 rrule='FREQ=DAILY;COUNT=3', reminders=[0])
    selected = listed(ctx, '2026-12-11', '2026-12-12')['events'][0]
    result = json.loads(tools.cal_update(ctx, id=selected['id'], title='Timed exception', start='2026-12-11T10:00', end='2026-12-11T11:00', all_day=False, confirm=True))
    assert result['status'] == 'updated', result
    exc, = ctx.store.exceptions_for(row['id'])
    result = listed(ctx, '2026-12-11', '2026-12-12')
    assert [(r['start'][11:16], r['end'][11:16]) for r in result['events']] == [('10:00', '11:00')]
    assert json.loads(tools.cal_events(ctx, id=exc['id']))['event']['start'][11:16] == '10:00'
    # An end-only follow-up must not reinterpret the timed override as its DATE master.
    changed = json.loads(tools.cal_update(ctx, id=exc['id'], end='2026-12-11T12:00', confirm=True))
    assert changed['status'] == 'updated', changed
    assert ctx.store.get_event(exc['id'])['start_utc'] == exc['start_utc']
    begin = datetime(2026, 12, 11, 9, tzinfo=UTC)
    hard, _ = ops.busy_intervals(ctx.occurrences(begin, begin + timedelta(hours=3)))
    assert hard == [(begin, begin + timedelta(hours=2))]
    ctx.store.drop_scheduled_reminders()
    reminders.plan(ctx.store, ctx.occurrences, now=begin - timedelta(minutes=1))
    sent = []
    channel = SimpleNamespace(send=lambda key, text: (sent.append(text) or ('sent', 'fixture')))
    delivered = reminders.deliver_due(ctx.store, channel, ctx.tz, now=begin)
    assert delivered['sent'] == 1 and 'Timed exception' in sent[0]


def test_floating_scope_all_moves_wall_coordinates_and_date_sets_through_dst(tmp_path):
    ctx, source, server = native_context(tmp_path, 'floating')
    key = '2026-10-27T08:00:00+00:00'
    assert json.loads(tools.cal_update(ctx, id=source['id']+'@'+key, title='Override', start='2026-10-27T12:00', end='2026-10-27T13:00', confirm=True))['status'] == 'updated'
    ctx.store.update_event(source['id'], {'exdates':'2026-10-26T08:00:00+00:00', 'rdates':'2026-10-29T08:00:00+00:00'})
    selected = listed(ctx, '2026-10-25', '2026-10-26')['events'][0]
    result = json.loads(tools.cal_update(ctx, id=selected['id'], start='2026-10-26T09:00', end='2026-10-26T10:00', scope='all', confirm=True))
    assert result['status'] == 'updated', result
    saved = ctx.store.get_event(source['id'])
    assert 'DTSTART:20261025T090000' in server.resources[source['href']][0]
    assert saved['exdates'] == '2026-10-27T08:00:00+00:00'
    assert saved['rdates'] == '2026-10-30T08:00:00+00:00'
    exc, = ctx.store.exceptions_for(source['id'])
    assert exc['recurrence_id'] == '2026-10-28T08:00:00+00:00'
    assert model.parse_stored(exc['start_utc']).astimezone(ctx.tz).hour == 12
    assert [r['start'][8:16] for r in listed(ctx,'2026-10-25','2026-10-31')['events']] == ['25T09:00','26T09:00','28T12:00','30T09:00']


def rdate_series(ctx):
    return ctx.store.insert_event({'calendar_id':'local:personal','uid':'dates','title':'Explicit dates',
        'start_utc':'2026-12-10T09:00:00+00:00','end_utc':'2026-12-10T10:00:00+00:00','tz':'UTC',
        'rdates':'2026-12-08T09:00:00+00:00,2026-12-12T09:00:00+00:00,2026-12-14T09:00:00+00:00',
        'exdates':'2026-12-12T09:00:00+00:00','reminders_json':'[0]'})


def test_rdate_set_is_visible_outside_dtstart_window_and_uses_exclusions(tmp_path):
    ctx = context(tmp_path,'UTC'); source = rdate_series(ctx)
    assert [r['start'][8:10] for r in listed(ctx,'2026-12-08','2026-12-15')['events']] == ['08','10','14']
    event, = listed(ctx,'2026-12-14','2026-12-15')['events']
    assert event['id'] == source['id']+'@2026-12-14T09:00:00+00:00'
    assert json.loads(tools.cal_events(ctx,id=event['id']))['status'] == 'ok'
    begin = datetime(2026,12,14,9,tzinfo=UTC)
    assert ops.busy_intervals(ctx.occurrences(begin,begin+timedelta(hours=1)))[0] == [(begin,begin+timedelta(hours=1))]
    reminders.plan(ctx.store,ctx.occurrences,now=begin-timedelta(minutes=1))
    assert ctx.store.due_reminders(begin)


@pytest.mark.parametrize('scope',['this','following','all'])
def test_rdate_set_keeps_edit_and_delete_capabilities(tmp_path,scope):
    ctx = context(tmp_path,'UTC'); source = rdate_series(ctx)
    event=source['id']+'@2026-12-14T09:00:00+00:00'
    changed=json.loads(tools.cal_update(ctx,id=event,title='Changed',scope=scope,confirm=True))
    assert changed['status']=='updated',changed
    visible=listed(ctx,'2026-12-14','2026-12-15')['events']
    assert len(visible)==1 and visible[0]['title']=='Changed'
    deleted=json.loads(tools.cal_delete(ctx,id=visible[0]['id'],scope='following',confirm=True))
    assert deleted['status']=='deleted',deleted
    assert listed(ctx,'2026-12-14','2026-12-15')['events']==[]
    assert [r['start'][8:10] for r in listed(ctx,'2026-12-08','2026-12-13')['events']] == ['08','10']


@pytest.mark.parametrize('rule',['FREQ=NOT_A_FREQUENCY','FREQ=MINUTELY;COUNT=1100'])
def test_list_exposes_existing_expansion_failure_without_fabricating_master(tmp_path,rule):
    ctx=context(tmp_path,'UTC')
    failed=ctx.store.insert_event({'calendar_id':'local:personal','title':'Unexpanded','start_utc':'2026-12-10T09:00:00+00:00',
        'end_utc':'2026-12-10T09:01:00+00:00','tz':'UTC','rrule':rule})
    create(ctx,title='Ordinary',start='2026-12-10T11:00')
    result=listed(ctx,'2026-12-10','2026-12-12')
    assert result['events_complete'] is False and failed['id'] in result['unexpanded_series']
    assert [event['title'] for event in result['events']]==['Ordinary']
