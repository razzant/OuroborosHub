"""Identity, effective time and original slot of independently received Google dates."""
import json
from datetime import timedelta

import pytest

import conflicts
import ops
import tools
from test_held_google_instances import (
    _day, _moved_kept_date, _waiting_with_series_copies, add_local_calendar,
    add_second_google_calendar, free, google_feed, instance, listed, pull,
    run_open_intents, scripted_feed, series, update,
)


def _start(ctx, cid, server, day):
    record, slot = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=4'))
    assert pull(ctx, cid)['status'] == 'ok'
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert ctx.store.get_event(record['id'])['master_id'] == master['id']
    return record, slot, master


def _generate_google_slot_on_create(server, slot):
    """The fake stores series but does not otherwise materialize their ordinary instances."""
    apply = server._apply
    def with_generated_date(method, path, params, body):
        result = apply(method, path, params, body)
        if method == 'POST' and body.get('recurrence'):
            server.put(instance(body['id'], slot))
        return result
    server._apply = with_generated_date


@pytest.mark.parametrize('scope', ['all', 'following', 'provider'])
def test_joined_date_stays_at_its_hour_during_series_motion(scope):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, slot, master = _start(ctx, cid, server, day)
    mirror = add_local_calendar(ctx.store, 'busy copy', publish_mode='busy')
    assert update(ctx, id=master['id'], calendars=[cid, mirror], scope='all')['status'] == 'updated'
    assert '13:00' == listed(ctx, day, [mirror])[0][1]
    start = len(server.calls)
    if scope == 'provider':
        server.put(series('weekly', slot - timedelta(days=7) + timedelta(hours=1), 'FREQ=WEEKLY;COUNT=4'))
        assert pull(ctx, cid)['status'] == 'ok'
    else:
        if scope == 'following':
            # The test transport does not generate a new series' concrete instances on POST.
            # Model Google's generated suffix slot; do not seed the moved date's effective hour.
            _generate_google_slot_on_create(server, slot + timedelta(hours=1))
        answer = update(ctx, id=master['id'] + '@' + ops.iso_utc(slot), scope=scope,
                        start=(slot + timedelta(hours=1)).isoformat())
        assert answer['status'] == 'updated', answer
    for dest in (cid, mirror):
        assert [r[1:] for r in listed(ctx, day, [dest])] == [('13:00', '14:00')]
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    fresh = ctx.store.get_event(record['id'])
    assert fresh['recurrence_id'] == ops.iso_utc(slot + timedelta(hours=1))
    date_writes = [body for method, path, _, body in server.calls[start:]
                   if method == 'PATCH' and '_' in path.rsplit('/', 1)[-1] and body and 'start' in body]
    if scope != 'provider':
        assert date_writes
    assert all(ops.parse_stored(b['start']['dateTime']).hour == 13 for b in date_writes)


def test_provider_series_shift_never_writes_a_shifted_effective_hour_to_a_google_follower():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    _, slot, master = _start(ctx, cid, server, day)
    destination = add_second_google_calendar(ctx.store)
    _generate_google_slot_on_create(server, slot)
    assert update(ctx, id=master['id'], calendars=[cid, destination], scope='all')['status'] == 'updated'
    run_open_intents(ctx, 4)
    mirror = next(m for m in ctx.store.group_masters(ctx.store.get_event(master['id'])['link_group_id'])
                  if m['calendar_id'] == destination)
    copy, = ctx.store.exceptions_for(mirror['id'])
    assert copy['external_id'] and '13:00' == listed(ctx, day, [destination])[0][1]
    calls = len(server.calls)
    server.put(series('weekly', slot - timedelta(days=7) + timedelta(hours=1), 'FREQ=WEEKLY;COUNT=4'))
    assert pull(ctx, cid)['status'] == 'ok'
    date_writes = [body for method, path, _, body in server.calls[calls:]
                   if method == 'PATCH' and path.endswith('/' + copy['external_id']) and 'start' in body]
    assert date_writes and all(ops.parse_stored(b['start']['dateTime']).hour == 13 for b in date_writes)
    assert ctx.store.get_event(copy['id'])['recurrence_id'] == ops.iso_utc(slot + timedelta(hours=1))
    assert listed(ctx, day, [destination]) == [(ops.BUSY_COPY_TITLE, '13:00', '14:00')]


def test_adjacent_received_dates_move_as_a_set_without_losing_notice_identity():
    import reminders as rem
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    first = day + timedelta(hours=11)
    second = first + timedelta(days=1)
    keys = []
    for slot, title in ((first, 'First date'), (second, 'Second date')):
        eid = server.put(instance('daily', slot, title=title))['id']
        assert pull(ctx, cid)['status'] == 'ok'
        keys.append(ctx.store.find_by_external(cid, external_id=eid)['id'])
    server.put(series('daily', first, 'FREQ=DAILY;COUNT=2'))
    assert pull(ctx, cid)['status'] == 'ok'
    master = ctx.store.find_by_external(cid, external_id='daily')
    for slot in (first, second):
        key = ops.iso_utc(slot)
        notice_id = rem.notice_id_for(master['id'], key, 30, key)
        ctx.store.schedule_reminder(master['id'], key, 30, ops.iso_utc(slot - timedelta(minutes=30)), notice_id, key)
    for reminder in ctx.store.due_reminders(second):
        ctx.store.mark_reminder(reminder['id'], 'sent', 'confirmed fake delivery')
    server.put(series('daily', second, 'FREQ=DAILY;COUNT=2'))
    assert pull(ctx, cid)['status'] == 'ok'
    for eid, slot, title in zip(keys, (first, second), ('First date', 'Second date')):
        new = ops.iso_utc(slot + timedelta(days=1))
        row = ctx.store.get_event(eid)
        assert (row['recurrence_id'], row['start_utc']) == (new, ops.iso_utc(slot))
        assert listed(ctx, slot.replace(hour=0), [cid]) == [(title, '11:00', '12:00')]
        assert any(r['recurrence_id'] == new and r['occurrence_start_utc'] == ops.iso_utc(slot) and r['state'] == 'sent'
                   for r in ctx.store.settled_reminders_for_split(master['id'], new))
    assert not listed(ctx, day + timedelta(days=2), [cid])


def test_parent_departure_and_date_move_propagates_to_own_following_copies():
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    record, slot = _moved_kept_date(ctx, cid, server, day)
    busy = add_local_calendar(ctx.store, 'busy destination', publish_mode='busy')
    independent = add_local_calendar(ctx.store, 'independent', publish_mode='full')
    assert update(ctx, id=record['id'], calendars=[cid, busy, independent])['status'] == 'updated'
    own_group = ctx.store.get_event(record['id'])['link_group_id']
    other = next(m for m in ctx.store.group_masters(own_group) if m['calendar_id'] == independent)
    assert update(ctx, id=other['id'], title='Own appointment')['status'] == 'updated'
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=4'))
    assert pull(ctx, cid)['status'] == 'ok'
    del server.events['weekly']
    changed = server.put(instance('weekly', slot, slot + timedelta(hours=5), title='Private update'))
    server.script = [{'id': 'weekly', 'status': 'cancelled'}, changed]
    assert pull(ctx, cid)['status'] == 'ok'
    assert listed(ctx, day, [cid]) == [('Private update', '16:00', '17:00')]
    assert listed(ctx, day, [busy]) == [(ops.BUSY_COPY_TITLE, '16:00', '17:00')]
    assert listed(ctx, day, [independent]) == [('Own appointment', '13:00', '14:00')]


def test_selecting_a_date_copy_as_source_keeps_slot_handover_bound_to_the_received_identity():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, _, _, cals = _waiting_with_series_copies(ctx, cid, server, day)
    source = next(m for m in ctx.store.group_masters(ctx.store.get_event(record['id'])['link_group_id'])
                  if m['calendar_id'] == cals['shared'])
    assert update(ctx, id=source['id'], link_action='source')['status'] == 'ok'
    assert update(ctx, id=source['id'], title='Owner chose this title')['status'] == 'updated'
    for destinations in ([cid, cals['shared'], cals['mirror']], [cid, cals['shared']]):
        assert update(ctx, id=source['id'], calendars=destinations)['status'] == 'updated'
        assert [r[1:] for r in listed(ctx, day, [cals['mirror']])] == [('13:00', '14:00')]
        assert ctx.store.get_event(source['id'])['is_primary']
        assert listed(ctx, day, [cals['mirror']])[0][0] == 'Owner chose this title'


def test_owner_deleted_joined_date_does_not_revive_on_a_new_google_version():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, slot, _ = _start(ctx, cid, server, day)
    assert json.loads(tools.cal_delete(ctx, id=record['id'], scope='this', confirm=True))['status'] == 'deleted'
    assert not listed(ctx, day)
    server.put(instance('weekly', slot, slot + timedelta(hours=2), title='New invitation version'))
    assert pull(ctx, cid)['status'] == 'ok'
    assert not listed(ctx, day)
    assert free(ctx, day) == [('09:00', '19:00')]


@pytest.mark.parametrize('kind, code, status', [('auth', 401, 'auth_failed'), ('forbidden', 403, 'error'), ('network', 0, 'ok')])
def test_offline_identity_read_for_missing_received_date_never_installs_new_cursor(kind, code, status):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, _ = _moved_kept_date(ctx, cid, server, day)
    del server.events[record['external_id']]
    server.changed.clear()
    ctx.store.set_sync_state(cid, cursor='old-token', cursor_kind='google_sync_token')
    apply = server._apply
    def fail(method, path, params, body):
        if method == 'GET' and path.endswith('/' + record['external_id']):
            raise ops.ProviderError(kind, 'synthetic lookup failure', code)
        return apply(method, path, params, body)
    server._apply = fail
    assert pull(ctx, cid)['status'] == status
    assert ctx.store.get_sync_state(cid)['cursor'] == ('old-token' if code else '')
    assert listed(ctx, day)
    server._apply = apply
    assert pull(ctx, cid)['status'] == 'ok'
    assert not listed(ctx, day)


@pytest.mark.parametrize('scope', ['all', 'following'])
def test_accepting_provider_rolls_back_waiting_date_binding_but_not_independent_content(scope):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, slot, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    # Only the cached master ETag is now stale, making the command's first PATCH conflict.
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    answer = update(ctx, id=master['id'] + '@' + ops.iso_utc(slot), scope=scope,
                    start=(slot + timedelta(hours=1)).isoformat())
    assert answer['status'] == 'conflict'
    moved = ctx.store.get_event(record['id'])
    assert moved['recurrence_id'] == ops.iso_utc(slot + timedelta(hours=1))
    # A separate accepted local field change after staging must survive accepting Google's version of the series.
    ctx.store.update_event(record['id'], {'visibility': 'hidden', 'reminders_json': '[45]'})
    view = conflicts.inspect(ctx, master['id'])
    assert view['resolution_token']
    assert update(ctx, id=view['event_id'], resolution='provider', resolution_token=view['resolution_token'])['status'] == 'resolved'
    restored = ctx.store.get_event(record['id'])
    assert restored['kept_master'] == master['id'] and restored['recurrence_id'] == ops.iso_utc(slot)
    assert (restored['visibility'], restored['reminders_json']) == ('hidden', '[45]')
    assert [r[1:] for r in listed(ctx, day, [cid])] == [('13:00', '14:00')]


@pytest.mark.parametrize('status', ['confirmed', 'tentative'])
def test_google_reverse_handover_retries_the_same_cancelled_occurrence_after_412(status):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    date = server.put({**instance('weekly', slot, slot + timedelta(hours=2)), 'status': status})
    assert pull(ctx, cid)['status'] == 'ok'
    record = ctx.store.find_by_external(cid, external_id=date['id'])
    shared = add_local_calendar(ctx.store, 'own copy', publish_mode='busy')
    mirror = add_second_google_calendar(ctx.store)
    assert update(ctx, id=record['id'], calendars=[cid, shared])['status'] == 'updated'
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=4'))
    assert pull(ctx, cid)['status'] == 'ok'
    master = ctx.store.find_by_external(cid, external_id='weekly')
    _generate_google_slot_on_create(server, slot)
    assert update(ctx, id=master['id'], calendars=[cid, mirror], scope='all')['status'] == 'updated'
    run_open_intents(ctx, 4)
    assert update(ctx, id=record['id'], calendars=[cid, shared, mirror])['status'] in ('updated', 'pending')
    run_open_intents(ctx, 4)
    group = ctx.store.get_event(record['id'])['link_group_id']
    copy = next(m for m in ctx.store.group_masters(group) if m['calendar_id'] == mirror)
    mirror_root = next(m for m in ctx.store.group_masters(ctx.store.get_event(master['id'])['link_group_id']) if m['calendar_id'] == mirror)
    exception, = ctx.store.exceptions_for(mirror_root['id'])
    remote_id = exception['external_id']
    assert server.events[remote_id]['status'] == 'cancelled'
    server.put(server.events[remote_id])  # ETag changes after the last observed cancellation
    answer = update(ctx, id=record['id'], calendars=[cid, shared])
    assert answer['status'] != 'updated'
    assert not ctx.store.get_event(copy['id'])['deleted_at']
    view = conflicts.inspect(ctx, copy['id'])
    assert view['event_id'] == exception['id'] and view['provider']['status'] == 'cancelled'
    answer = update(ctx, id=view['event_id'], resolution='local', resolution_token=view['resolution_token'])
    assert answer['status'] == 'pending'
    run_open_intents(ctx, 4)
    assert server.events[copy['external_id']]['status'] == 'cancelled'
    assert any(i['kind'] == 'delete' and i['state'] == 'done' for i in ctx.store.intents_for_event(copy['id']))
    assert ctx.store.get_event(exception['id'])['external_id'] == remote_id
    assert server.events[remote_id]['status'] == status
    assert listed(ctx, day, [mirror]) == [(ops.BUSY_COPY_TITLE, '13:00', '14:00')]


def test_late_google_date_does_not_rewrite_another_series_source():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    slot = day + timedelta(hours=11)
    server.put(series('weekly', slot - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    assert pull(ctx, cid)['status'] == 'ok'
    master = ctx.store.find_by_external(cid, external_id='weekly')
    dest = add_local_calendar(ctx.store, 'chosen source', publish_mode='full')
    assert update(ctx, id=master['id'], calendars=[cid, dest], scope='all')['status'] == 'updated'
    local = next(m for m in ctx.store.group_masters(ctx.store.get_event(master['id'])['link_group_id']) if m['calendar_id'] == dest)
    assert update(ctx, id=local['id'], link_action='source')['status'] == 'ok'
    server.put(instance('weekly', slot, slot + timedelta(hours=2), title='Google occurrence'))
    assert pull(ctx, cid)['status'] == 'ok'
    assert listed(ctx, day, [cid]) == [('Google occurrence', '13:00', '14:00')]
    assert listed(ctx, day, [dest]) == [('Weekly sync', '11:00', '12:00')]
    assert ctx.store.get_event(local['id'])['is_primary']
    assert free(ctx, day) == [('09:00', '11:00'), ('12:00', '13:00'), ('14:00', '19:00')]


def test_rsvp_of_a_native_cancelled_caldav_invitation_still_sets_own_partstat():
    from providers import YandexAdapter, ics_to_rows
    source = (
        'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n'
        'UID:invite-1\r\nSTATUS:CANCELLED\r\nSEQUENCE:7\r\nSUMMARY:Invite\r\n'
        'DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n'
        'ORGANIZER:mailto:organizer@example.test\r\n'
        'ATTENDEE;CN=Owner;PARTSTAT=NEEDS-ACTION:mailto:owner@example.test\r\n'
        'END:VEVENT\r\nEND:VCALENDAR\r\n'
    )
    event, = ics_to_rows(source, 'synthetic', 'https://calendar.example.test/invite.ics', '"e1"', ops.get_tz('UTC'))
    adapter = YandexAdapter('owner@example.test', 'fake')
    adapter.get = lambda href: (source, '"e1"')
    puts = []
    def write(method, url, body=None, headers=None):
        puts.append(body)
        return 204, {"etag": '"e2"'}, ''
    adapter._request = write
    adapter.respond({'id': event['calendar_id']}, event, {'response': 'accepted'})
    written, = puts
    assert 'PARTSTAT=ACCEPTED' in written and 'STATUS:CANCELLED' in written
