"""A following cut partitions the recurrence set without becoming its generator anchor."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SKILL), str(SKILL / 'tests')]
import model
import ops
import providers
import providers_google
import reminders
import tools
from test_calendar_provider_divergence import FakeCalDAV, FakeGoogle, add_google_calendar, run_open_intents
from test_recurrence_until_semantics import native_context, strict_native_occurrences


def context(tmp_path, zone='UTC'):
    ctx = tools.Context(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda _: {}))
    ctx.tz = model.get_tz(zone)
    ctx.store.set_setting('timezone', zone)
    return ctx


def slots(ctx, start='2026-12-01', end='2027-01-20'):
    result = json.loads(tools.cal_events(ctx, start=start, end=end, limit=100))
    assert result['events_complete'], result
    return sorted(model.iso_utc(model.parse_stored(row['start'])) for row in result['events'])


def extra_series(ctx, rule):
    return ctx.store.insert_event({
        'calendar_id': 'local:personal', 'title': 'Extra dates', 'tz': 'UTC',
        'start_utc': '2026-12-10T09:00:00+00:00', 'end_utc': '2026-12-10T10:00:00+00:00',
        'rrule': rule, 'rdates': ','.join(f'{day}T09:00:00+00:00' for day in
                                       ['2026-12-08', '2026-12-14', '2027-01-12'])})


@pytest.mark.parametrize('rule', ['', 'FREQ=DAILY;COUNT=3', 'FREQ=WEEKLY;INTERVAL=2;BYDAY=TH;COUNT=3'])
@pytest.mark.parametrize('cut', ['2026-12-08', '2026-12-14', '2027-01-12'])
@pytest.mark.parametrize('hours', [0, 1])
def test_extra_date_following_preserves_exact_membership(tmp_path, rule, cut, hours):
    ctx = context(tmp_path)
    row = extra_series(ctx, rule)
    before = slots(ctx)
    key = cut + 'T09:00:00+00:00'
    delta = timedelta(hours=hours)
    expected = sorted(model.iso_utc(model.parse_stored(stamp) + (delta if stamp >= key else timedelta()))
                      for stamp in before)
    changed = {'start': model.iso_utc(model.parse_stored(key) + delta)} if hours else {}
    result = json.loads(tools.cal_update(ctx, id=row['id'] + '@' + key, scope='following',
                                        title='Suffix', confirm=True, **changed))
    assert result['status'] == 'updated', result
    assert slots(ctx) == expected


def test_off_clock_extra_date_does_not_shift_generator_weekday(tmp_path):
    ctx = context(tmp_path)
    row = extra_series(ctx, 'FREQ=WEEKLY;BYDAY=TH;COUNT=3')
    key = '2026-12-14T23:00:00+00:00'
    ctx.store.update_event(row['id'], {'rdates': key})
    before = slots(ctx)
    result = json.loads(tools.cal_update(ctx, id=row['id'] + '@' + key, scope='following',
                                        start='2026-12-15T01:00:00+00:00', confirm=True))
    assert result['status'] == 'updated', result
    assert slots(ctx) == sorted(model.iso_utc(model.parse_stored(stamp) + (
        timedelta(hours=2) if stamp >= key else timedelta())) for stamp in before)


@pytest.mark.parametrize('replacement', ['', 'FREQ=DAILY;COUNT=2'])
def test_extra_date_explicit_rule_owns_new_anchor(tmp_path, replacement):
    ctx = context(tmp_path)
    row = extra_series(ctx, 'FREQ=WEEKLY;INTERVAL=2;COUNT=3')
    ctx.store.update_event(row['id'], {'rdates': '2026-12-08T09:00:00+00:00'})
    result = json.loads(tools.cal_update(ctx, id=row['id'] + '@2026-12-08T09:00:00+00:00',
                                        scope='following', rrule=replacement, title='Replacement', confirm=True))
    assert result['status'] == 'updated', result
    assert slots(ctx) == (['2026-12-08T09:00:00+00:00', '2026-12-09T09:00:00+00:00'] if replacement else
                         ['2026-12-08T09:00:00+00:00'])


@pytest.mark.parametrize('rule', ['', 'FREQ=DAILY;COUNT=3', 'FREQ=DAILY;UNTIL=20261212T090000Z'])
def test_following_delete_after_finite_generator_preserves_prefix(tmp_path, rule):
    ctx = context(tmp_path)
    row = extra_series(ctx, rule)
    before = slots(ctx)
    cut = '2026-12-14T09:00:00+00:00'
    result = json.loads(tools.cal_delete(ctx, id=row['id'] + '@' + cut, scope='following', confirm=True))
    assert result['status'] == 'deleted', result
    assert slots(ctx) == [stamp for stamp in before if stamp < cut]


def test_extra_cut_keeps_exceptions_and_settled_notice_identity(tmp_path):
    ctx = context(tmp_path)
    row = extra_series(ctx, 'FREQ=DAILY;COUNT=3')
    cut = '2026-12-08T09:00:00+00:00'
    effective_cut = '2026-12-08T11:00:00+00:00'
    for key, effective in [(cut, effective_cut), ('2026-12-11T09:00:00+00:00', '2026-12-11T13:00:00+00:00')]:
        result = json.loads(tools.cal_update(ctx, id=row['id'] + '@' + key, start=effective, confirm=True))
        assert result['status'] == 'updated', result
    before = slots(ctx)
    old_exceptions = ctx.store.exceptions_for(row['id'])
    for key, effective, state in [(cut, effective_cut, 'sent'), ('2026-12-10T09:00:00+00:00', '2026-12-10T09:00:00+00:00', 'unknown')]:
        notice = reminders.notice_id_for(row['id'], effective, 15, key)
        ctx.store.schedule_reminder(row['id'], effective, 15,
            model.iso_utc(model.parse_stored(effective) - timedelta(minutes=15)), notice, key)
        saved = next(item for item in ctx.store.upcoming_reminders(model.parse_stored('2026-12-01T00:00:00Z'), 100)
                     if item['notice_id'] == notice)
        ctx.store.mark_reminder(saved['id'], state)
    result = json.loads(tools.cal_update(ctx, id=row['id'] + '@' + cut, title='Retitled', scope='following', confirm=True))
    assert result['status'] == 'updated', result
    assert slots(ctx) == before
    new_master = ctx.store.get_event(ctx.store.get_event(old_exceptions[0]['id'])['master_id'])
    assert new_master['start_utc'] == row['start_utc']
    assert {(item['id'], item['recurrence_id'], item['start_utc']) for item in ctx.store.exceptions_for(new_master['id'])} == {
        (item['id'], item['recurrence_id'], item['start_utc']) for item in old_exceptions}
    with ctx.store._conn() as connection:
        notices = [dict(item) for item in connection.execute('SELECT * FROM reminders WHERE event_id=?', (new_master['id'],))]
    assert {(item['recurrence_id'], item['occurrence_start_utc'], item['state']) for item in notices} == {
        (cut, effective_cut, 'sent'), ('2026-12-10T09:00:00+00:00', '2026-12-10T09:00:00+00:00', 'unknown')}


@pytest.mark.parametrize('kind', ['floating', 'tzid', 'utc', 'date'])
@pytest.mark.parametrize('scope', ['all', 'following'])
def test_inherited_until_moves_with_native_recurrence_clock(tmp_path, kind, scope):
    ctx, row, server = native_context(tmp_path, kind, until=True)
    before = slots(ctx, '2026-10-20', '2026-11-01')
    cut = model.parse_stored(before[0 if scope == 'all' else 2])
    zone = model.get_tz(row['tz'])
    delta = timedelta(days=2 if kind == 'date' else 0, hours=0 if kind == 'date' else 1) if scope == 'all' else timedelta(days=-2)
    moved = model.shift_recurrence_time(row, cut, delta)
    args = {'start': moved.date().isoformat(), 'all_day': True} if kind == 'date' else {'start': moved.isoformat()}
    result = json.loads(tools.cal_update(ctx, id=row['id'] + ('@' + model.iso_utc(cut) if scope == 'following' else ''),
                                        scope=scope, confirm=True, **args))
    assert result['status'] == 'updated', result
    run_open_intents(ctx, 3)
    expected = sorted(model.iso_utc(model.shift_recurrence_time(row, model.parse_stored(stamp), delta))
                      if model.parse_stored(stamp) >= cut else stamp for stamp in before)
    assert slots(ctx, '2026-10-20', '2026-11-01') == expected
    native = []
    for wire, _ in server.resources.values():
        for value in strict_native_occurrences(wire):
            native.append(model.iso_utc(value.replace(tzinfo=zone) if value.tzinfo is None else value))
    assert sorted(native) == expected


@pytest.mark.parametrize('scope', ['all', 'following'])
@pytest.mark.parametrize('bound', ['UNTIL=20261118T080000Z', 'COUNT=5'])
def test_zoned_week_shift_preserves_last_slot_across_dst(tmp_path, scope, bound):
    ctx = context(tmp_path, 'Europe/Berlin')
    row = json.loads(tools.cal_create(ctx, title='Weekly', start='2026-10-21T09:00:00+02:00',
        end='2026-10-21T10:00:00+02:00', rrule='FREQ=WEEKLY;' + bound, confirm=True))['event']
    before = slots(ctx, '2026-10-01', '2026-11-25')
    cut = model.parse_stored(before[0 if scope == 'all' else 1])
    target = (cut.astimezone(ctx.tz).replace(tzinfo=None) - timedelta(days=7)).replace(tzinfo=ctx.tz)
    result = json.loads(tools.cal_update(ctx, id=row['id'] + ('@' + model.iso_utc(cut) if scope == 'following' else ''),
                                        scope=scope, start=target.isoformat(), confirm=True))
    assert result['status'] == 'updated', result
    expected = sorted(model.iso_utc((model.parse_stored(stamp).astimezone(ctx.tz).replace(tzinfo=None)
                                    - timedelta(days=7)).replace(tzinfo=ctx.tz)) if model.parse_stored(stamp) >= cut else stamp
                      for stamp in before)
    assert slots(ctx, '2026-10-01', '2026-11-25') == expected


@pytest.mark.parametrize('scope', ['all', 'following'])
def test_timed_date_until_keeps_inclusive_bound_across_midnight(tmp_path, scope):
    ctx = context(tmp_path)
    made = json.loads(tools.cal_create(ctx, title='Late routine', start='2026-12-14T23:00:00Z',
        end='2026-12-15T00:00:00Z', rrule='FREQ=DAILY;UNTIL=20261217', confirm=True))
    row = ctx.store.get_event(made['event']['id'])
    before = slots(ctx)
    cut = model.parse_stored(before[0 if scope == 'all' else 2])
    result = json.loads(tools.cal_update(ctx, id=row['id'] + ('@' + model.iso_utc(cut) if scope == 'following' else ''),
                                        scope=scope, start=model.iso_utc(cut + timedelta(hours=2)), confirm=True))
    assert result['status'] == 'updated', result
    assert slots(ctx) == sorted(model.iso_utc(model.parse_stored(stamp) + timedelta(hours=2))
                                if model.parse_stored(stamp) >= cut else stamp for stamp in before)


@pytest.mark.parametrize('source_provider', ['caldav', 'google'])
def test_extra_date_split_and_linked_follower_keep_native_generator(tmp_path, source_provider):
    ctx = context(tmp_path)
    cid = add_google_calendar(ctx.store)
    google = FakeGoogle()
    google_adapter = google.attach(providers_google.GoogleAdapter('fixture', 'token', 'refresh', model.now_utc(), 'client', '', None))
    aid, dav_id, href = 'caldav:fixture', 'caldav:fixture:work', 'https://dav.example.test/work/'
    ctx.store.upsert_account({'id': aid, 'provider': 'caldav', 'login': 'fixture'})
    ctx.store.upsert_calendar({'id': dav_id, 'account_id': aid, 'provider': 'caldav', 'name': 'Work', 'href': href})
    dav = FakeCalDAV()
    dav_adapter = dav.attach(providers.CalDAVAdapter('fixture', 'synthetic', 'https://dav.example.test'))
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda account: dav_adapter if account == aid else google_adapter)
    source, follower = (dav_id, cid) if source_provider == 'caldav' else (cid, dav_id)
    created = json.loads(tools.cal_create(ctx, title='Native series', start='2026-12-10T09:00:00+00:00',
        end='2026-12-10T10:00:00+00:00', rrule='FREQ=DAILY;COUNT=3',
        calendars=[source, follower], confirm=True))
    assert created['status'] == 'created', created
    row = ctx.store.get_event(created['event']['id'])
    result = ops.update_event(ctx.store, ctx.providers, row['id'],
        {'rdates': '2026-12-08T09:00:00+00:00,2026-12-14T09:00:00+00:00'}, scope='all')
    assert all(item['status'] == 'done' for item in result['assignments']), result
    before = slots(ctx)
    result = json.loads(tools.cal_update(ctx, id=row['id'] + '@2026-12-08T09:00:00+00:00',
                                        scope='following', title='Suffix', confirm=True))
    assert result['status'] == 'updated', result
    run_open_intents(ctx, 3)
    assert slots(ctx) == before
    native_rows = []
    for resource_href, (wire, etag) in dav.resources.items():
        native_rows.extend(providers.ics_to_rows(wire, dav_id, resource_href, etag, ctx.tz))
    native_rows.extend(providers_google.gevent_to_row(event, cid, ctx.tz) for event in google.events.values())
    native_rows = [{**item, 'id': 'native-' + str(index)} for index, item in enumerate(native_rows)]
    expanded = ops.expand(native_rows, datetime(2026, 12, 1, tzinfo=timezone.utc),
                          datetime(2027, 1, 20, tzinfo=timezone.utc), lambda _: [], strict=True)
    assert sorted(row['start_utc'] for row in expanded) == before
