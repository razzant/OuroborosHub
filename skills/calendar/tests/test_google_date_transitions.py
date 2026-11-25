"""One invitation through provider and local topology changes, with no network."""
import json
from datetime import timedelta

import pytest

from test_held_google_instances import (
    _day, _moved_kept_date, _waiting_with_series_copies, add_local_calendar,
    cancelled, free, google_feed, instance, listed, pull, series, scripted_feed, update,
)
import ops
from scripts import worker
import tools


@pytest.mark.parametrize('attached', [False, True])
@pytest.mark.parametrize('survives', [False, True])
def test_parent_only_cancellation_uses_instance_id_not_local_shape(attached, survives):
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    record, when = _moved_kept_date(ctx, cid, server, day)
    shared = add_local_calendar(ctx.store, 'own date', publish_mode='busy')
    mirror = add_local_calendar(ctx.store, 'series mirror', publish_mode='busy')
    if not attached:
        assert update(ctx, id=record['id'], calendars=[cid, shared])['status'] == 'updated'
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert bool(ctx.store.get_event(record['id'])['master_id']) is attached
    assert update(ctx, id=master['id'], calendars=[cid, mirror], scope='all')['status'] == 'updated'
    del server.events['weekly']
    if not survives:
        del server.events[record['external_id']]
    server.script = [{'id': 'weekly', 'status': 'cancelled'}]
    for _ in range(2):
        assert pull(ctx, cid)['status'] == 'ok'
        for dest in (cid, mirror, *([shared] if not attached else [])):
            assert [t[1:] for t in listed(ctx, day, calendars=[dest])] == ([('13:00', '14:00')] if survives else [])
            assert listed(ctx, day + timedelta(days=7), calendars=[dest]) == []
        slots = free(ctx, day)
        assert (('09:00', '19:00') in slots) is not survives


def test_provider_series_move_keeps_received_hour_in_each_assignment():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    server.put(series('weekly', when - timedelta(days=7) + timedelta(hours=1), 'FREQ=WEEKLY;COUNT=3'))
    for _ in range(2):
        assert pull(ctx, cid)['status'] == 'ok'
        for dest in (cid, *cals.values()):
            assert [t[1:] for t in listed(ctx, day, calendars=[dest])] == [('13:00', '14:00')]
    assert ctx.store.get_event(record['id'])['recurrence_id'] == ops.iso_utc(when + timedelta(hours=1))


@pytest.mark.parametrize('source_handover', [False, True])
def test_assign_from_date_copy_withdraws_following_series_slot(source_handover):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, _, master, cals = _waiting_with_series_copies(ctx, cid, server, day, carriers=('mirror', 'new source'))
    group = ctx.store.get_event(master['id'])['link_group_id']
    if source_handover:
        source = next(m for m in ctx.store.group_masters(group) if m['calendar_id'] == cals['new source'])
        assert update(ctx, id=source['id'], link_action='source')['status'] == 'ok'
    copies = ctx.store.group_masters(ctx.store.get_event(record['id'])['link_group_id'])
    own_copy = next(m for m in copies if m['calendar_id'] == cals['shared'])
    assert update(ctx, id=own_copy['id'], calendars=[cid, cals['shared'], cals['mirror']])['status'] == 'updated'
    for dest in (cid, *cals.values()):
        assert [t[1:] for t in listed(ctx, day, calendars=[dest])] == [('13:00', '14:00')]


def test_assignment_rechecks_cancelled_address_inside_transaction(monkeypatch):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when = _moved_kept_date(ctx, cid, server, day)
    dest = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    assign = ops.reassign_event
    def after_address(*args, **kwargs):
        server.put(cancelled('weekly', when))
        assert pull(ctx, cid)['status'] == 'ok'
        return assign(*args, **kwargs)
    monkeypatch.setattr(ops, 'reassign_event', after_address)
    assert update(ctx, id=record['id'], calendars=[cid, dest])['status'] == 'failed'
    assert listed(ctx, day, calendars=[dest]) == []


def test_hidden_date_after_attach_and_direct_cancellation_agree():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when = _moved_kept_date(ctx, cid, server, day)
    assert update(ctx, id=record['id'], hidden=True)['status'] == 'updated'
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert not json.loads(tools.cal_events(ctx, start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(), include_hidden=False))['events']
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]
    server.put(cancelled('weekly', when))
    pull(ctx, cid)
    for addr in (record['id'], master['id'] + '@' + ops.iso_utc(when)):
        event = json.loads(tools.cal_events(ctx, id=addr))['event']
        assert event['status'] == 'cancelled' and event['start'][11:16] == '13:00'


def test_one_date_absent_by_id_remains_cancelled_after_a_full_listing():
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert ctx.store.get_event(record['id'])['master_id'] == master['id']
    del server.events[record['external_id']]
    ctx.store.set_sync_state(cid, cursor='')
    assert pull(ctx, cid)['status'] == 'ok'
    assert listed(ctx, day) == []
    assert listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]
    for addr in (record['id'], master['id'] + '@' + ops.iso_utc(when)):
        event = json.loads(tools.cal_events(ctx, id=addr))['event']
        assert event['status'] == 'cancelled' and event['start'][11:16] == '13:00'


def test_cancelled_series_takes_its_ordinary_dates_without_a_provider_lookup():
    """A held series' ordinary override is not a separately received date: no lookup, it retires with its series (2.1.0)."""
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    when = day + timedelta(hours=11)
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    server.put(instance('weekly', when))
    assert pull(ctx, cid)['status'] == 'ok'
    master = ctx.store.find_by_external(cid, external_id='weekly')
    (date,) = ctx.store.exceptions_for(master['id'])
    assert date['master_id'] == master['id'] and not date['kept_master']   # an ordinary override, not a received date
    server.script = [{'id': 'weekly', 'status': 'cancelled'}]              # the series alone: nothing about the date
    calls = len(server.calls)
    assert pull(ctx, cid)['status'] == 'ok'
    assert not [call for call in server.calls[calls:] if date['external_id'] in call[1]]   # no per-date lookup
    assert ctx.store.get_event(master['id'])['deleted_at']
    assert ctx.store.get_event(date['id'])['deleted_at']
    assert listed(ctx, day) == [] and listed(ctx, day + timedelta(days=7)) == []
    assert free(ctx, day) == [('09:00', '19:00')]


@pytest.mark.parametrize('failure', [None, 'auth'])
def test_missing_hidden_parent_does_not_leak_a_first_day_or_advance_an_unchecked_cursor(failure):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    when = day + timedelta(hours=11)
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], hidden=True, scope='all')['status'] == 'updated'
    del server.events['weekly']
    server.put(instance('weekly', when, when + timedelta(hours=2)))
    ctx.store.set_sync_state(cid, cursor='token-from-2.1.0', cursor_kind='google_sync_token')
    apply = server._apply
    def deny(method, path, params, body):
        if method == 'GET' and path.endswith('/events/weekly'):
            raise ops.ProviderError('auth', 'injected access loss', 401)
        return apply(method, path, params, body)
    if failure:
        server._apply = deny
        assert pull(ctx, cid)['status'] == 'auth_failed'
        assert not ctx.store.get_event(master['id'])['deleted_at']
        state = ctx.store.get_sync_state(cid)
        assert (state['cursor'], state['cursor_kind']) == ('token-from-2.1.0', 'google_sync_token')   # the failed read kept the previous token
        server._apply = apply
    assert pull(ctx, cid)['status'] == 'ok'
    assert 'syncToken' not in server.listings[-1]
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]
    assert not json.loads(tools.cal_events(ctx, start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(), include_hidden=False))['events']
    assert free(ctx, day) == [('09:00', '13:00'), ('14:00', '19:00')]


@pytest.mark.parametrize('phase', ['settled', 'stale_plan'])
def test_date_reminder_survives_a_change_of_shape_and_storage_zone(phase):
    import reminders as rem
    from test_held_google_instances import Channel
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    final = day + timedelta(days=1)
    ctx.store.set_setting('timezone', 'UTC')
    ctx.store.set_setting(rem.MODE_KEY, {cid: True})
    date = instance('weekly', day)
    date.update(start={'date': day.date().isoformat()}, end={'date': final.date().isoformat()},
                originalStartTime={'date': day.date().isoformat()})
    server.put(date)
    pull(ctx, cid)
    standalone = ctx.store.find_by_external(cid, external_id=date['id'])
    fire = day - timedelta(minutes=10)
    start_key, fire_key = ops.iso_utc(day), ops.iso_utc(fire)
    old_id = rem.notice_id_for(standalone['id'], start_key, 10)
    if phase == 'settled':
        ctx.store.schedule_reminder(standalone['id'], start_key, 10, fire_key, old_id)
        (notice,) = ctx.store.due_reminders(fire)
        ctx.store.mark_reminder(notice['id'], 'sent', 'test delivery')
    parent = series('weekly', day - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3')
    parent.update(start={'date': (day - timedelta(days=7)).date().isoformat(), 'timeZone': 'America/Los_Angeles'},
                  end={'date': (final - timedelta(days=7)).date().isoformat(), 'timeZone': 'America/Los_Angeles'})
    server.put(parent)
    pull(ctx, cid)
    attached = ctx.store.get_event(standalone['id'])
    master = ctx.store.get_event(attached['master_id'])
    assert attached['start_utc'] != start_key
    slot = attached['recurrence_id']
    if phase == 'stale_plan':
        # The planner's detached snapshot was read before attach dropped this queue.
        ctx.store.schedule_reminder(standalone['id'], start_key, 10, fire_key, old_id)
        (notice,) = ctx.store.due_reminders(fire)
        assert not ctx.store.reserve_reminder_send([notice['id']])
    ctx.store.schedule_reminder(master['id'], start_key, 10, fire_key,
                                rem.notice_id_for(master['id'], start_key, 10, slot), slot)
    ctx.store.update_event(standalone['id'], {'reminders_json': '[10]'})
    outcome = rem.deliver_due(ctx.store, Channel(), ctx.tz, fire)
    assert outcome['sent'] == (1 if phase == 'stale_plan' else 0)


@pytest.mark.parametrize('removal', ['sparse', 'not_found'])
def test_provider_reinvite_after_removal(removal):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    when = day + timedelta(hours=11)
    invited = server.put(instance('weekly', when))
    pull(ctx, cid)
    eid = ctx.store.find_by_external(cid, external_id=invited['id'])['id']
    shared = add_local_calendar(ctx.store, 'shared', publish_mode='full')
    mirror = add_local_calendar(ctx.store, 'mirror', publish_mode='busy')
    assert update(ctx, id=eid, calendars=[cid, shared])['status'] == 'updated'
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    assert update(ctx, id=master['id'], calendars=[cid, shared, mirror], scope='all')['status'] == 'updated'
    if removal == 'sparse':
        server.put({'id': invited['id'], 'status': 'cancelled'})
    else:
        del server.events[invited['id']]
        ctx.store.set_sync_state(cid, cursor='')
    pull(ctx, cid)
    for dest in (cid, shared, mirror):
        assert listed(ctx, day, calendars=[dest]) == []
    server.put(invited)
    pull(ctx, cid)
    for dest in (cid, shared, mirror):
        assert [item[1:] for item in listed(ctx, day, calendars=[dest])] == [('11:00', '12:00')]
    assert ctx.store.get_event(eid)['external_id'] == invited['id']
    assert ('11:00', '12:00') not in free(ctx, day)


@pytest.mark.parametrize('with_structured_predecessor', [False, True])
def test_sparse_cancellation_still_addresses_a_stored_ordinary_override(with_structured_predecessor):
    ctx, cid, server = scripted_feed()
    day = _day(5, 0)
    when = day + timedelta(hours=11)
    moved = instance('weekly', when, when + timedelta(hours=2))
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    server.put(moved)
    pull(ctx, cid)
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]
    server.script = ([cancelled('weekly', when)] if with_structured_predecessor else []) + [
        {'id': moved['id'], 'status': 'cancelled'},
    ]
    pull(ctx, cid)
    assert listed(ctx, day) == []
    assert listed(ctx, day + timedelta(days=7)) == [('Weekly sync', '11:00', '12:00')]


def test_attach_rolls_back_if_notice_transfer_cannot_commit(monkeypatch):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when = _moved_kept_date(ctx, cid, server, day)
    master = ctx.store.insert_provider_master({
        'calendar_id': cid, 'external_id': 'weekly', 'uid': 'weekly@google.com',
        'start_utc': ops.iso_utc(when), 'end_utc': ops.iso_utc(when + timedelta(hours=1)),
        'rrule': 'FREQ=WEEKLY;COUNT=3', 'title': 'Weekly sync',
    })
    before = ctx.store.get_event(record['id'])
    effective = before['start_utc']
    ctx.store.schedule_reminder(record['id'], effective, 10, effective, 'original-receipt')
    receipt, = ctx.store.due_reminders(when + timedelta(hours=3))
    ctx.store.mark_reminder(receipt['id'], 'sent', 'fake sent')
    def cannot_carry(*args, **kwargs):
        raise RuntimeError('injected custody write failure')
    with monkeypatch.context() as local:
        local.setattr(type(ctx.store), 'carry_settled_reminder', cannot_carry)
        with pytest.raises(RuntimeError, match='custody write failure'):
            worker._attach_held_instances(ctx.store, cid)
    assert ctx.store.get_event(record['id']) == before
    assert not ctx.store.exceptions_for(master['id'])
    worker._attach_held_instances(ctx.store, cid)
    assert ctx.store.get_event(record['id'])['master_id'] == master['id']
    assert len(ctx.store.settled_reminders_for_split(master['id'], ops.iso_utc(when))) == 1


@pytest.mark.parametrize('scope', ['all', 'following'])
def test_series_motion_preserves_received_hour_in_a_following_calendar(scope):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    first_or_cut = when - timedelta(days=7) if scope == 'all' else when
    result = update(ctx, id=master['id'] + '@' + ops.iso_utc(first_or_cut), scope=scope,
                    start=(first_or_cut + timedelta(hours=1)).isoformat())
    assert result['status'] in ('updated', 'pending')
    for dest in (cid, cals['shared'], cals['mirror']):
        assert [t[1:] for t in listed(ctx, day, calendars=[dest])] == [('13:00', '14:00')]


@pytest.mark.parametrize('action', ['move', 'delete'])
def test_local_series_source_addresses_the_waiting_date_in_the_google_follower(action):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when, master, cals = _waiting_with_series_copies(ctx, cid, server, day)
    local = next(m for m in ctx.store.group_masters(ctx.store.get_event(master['id'])['link_group_id'])
                 if m['calendar_id'] == cals['mirror'])
    assert update(ctx, id=local['id'], link_action='source')['status'] == 'ok'
    if action == 'move':
        response = update(ctx, id=local['id'] + '@' + ops.iso_utc(when), start=(day + timedelta(hours=15)).isoformat())
        expected = [('15:00', '16:00')]
    else:
        response = json.loads(tools.cal_delete(ctx, id=local['id'], scope='all', confirm=True))
        expected = []
    assert response['status'] in ('updated', 'deleted')
    for dest in (cid, cals['shared'], cals['mirror']):
        assert [t[1:] for t in listed(ctx, day, calendars=[dest])] == expected


def test_window_does_not_mix_an_attached_row_with_a_detached_slot(monkeypatch):
    ctx, cid, server = google_feed()
    day = _day(5, 0)
    record, when = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    assert ctx.store.get_event(record['id'])['master_id']
    read_window = type(ctx.store).window
    changed = False
    def during_read(view, *args, **kwargs):
        nonlocal changed
        rows = read_window(view, *args, **kwargs)
        if not changed:
            changed = True
            assert ctx.store.detach_instance(record['id'])
        return rows
    monkeypatch.setattr(type(ctx.store), 'window', during_read)
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]
    assert changed and not ctx.store.get_event(record['id'])['master_id']
    assert listed(ctx, day) == [('Weekly sync', '13:00', '14:00')]
    assert ('13:00', '14:00') not in free(ctx, day)


@pytest.mark.parametrize('had_exception', [False, True])
def test_keeping_a_date_with_retired_series_preserves_reminder_custody_and_existing_copy(had_exception):
    from test_held_google_instances import timed
    import reminders as rem
    ctx, cid, server = scripted_feed()
    mirror = add_local_calendar(ctx.store, 'existing', publish_mode='busy')
    unused = add_local_calendar(ctx.store, 'unrelated', publish_mode='busy')
    day = _day(5, 0)
    when = day + timedelta(hours=11)
    feed = server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    sparse = instance('weekly', when)
    if had_exception:
        server.put(sparse)
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id=feed['id'])
    ctx.store.set_setting(rem.MODE_KEY, {cid: True})
    if had_exception:
        stored, = ctx.store.exceptions_for(master['id'])
        ctx.store.update_event(stored['id'], {'visibility': 'hidden', 'reminders_json': '[45]'})
    assert update(ctx, id=master['id'], calendars=[cid, mirror], scope='all')['status'] == 'updated'
    target = ops.iso_utc(when)
    fire = ops.iso_utc(when - timedelta(minutes=45))
    ctx.store.schedule_reminder(master['id'], target, 45, fire, 'already-sent-google-date', target)
    receipt, = ctx.store.due_reminders(when)
    ctx.store.mark_reminder(receipt['id'], 'sent', 'confirmed by fake')
    server.script = [{'id': feed['id'], 'status': 'cancelled'},
                     {**sparse, 'etag': 'kept-v2', 'start': timed(when),
                      'reminders': {'useDefault': False, 'overrides': []}}]
    pull(ctx, cid)
    saved = ctx.store.find_by_external(cid, external_id=sparse['id'])
    assert saved and not saved['master_id'] and not saved['deleted_at']
    carried, = ctx.store.settled_reminders_for_split(saved['id'], '')
    assert carried['state'] == 'sent' and carried['offset_min'] == 45 and carried['occurrence_start_utc'] == target
    if had_exception:
        assert saved['visibility'] == 'hidden' and saved['reminders_json'] == '[45]'
    assert [t[1:] for t in listed(ctx, day, calendars=[mirror])] == [('11:00', '12:00')]
    assert listed(ctx, day + timedelta(days=7), calendars=[mirror]) == []
    assert listed(ctx, day, calendars=[unused]) == []


@pytest.mark.parametrize('outcome,status', [
    ('online', 'confirmed'), ('readback', 'confirmed'), ('etag_conflict', 'confirmed'),
    ('offline', 'confirmed'), ('etag_conflict', 'tentative'),
])
def test_reverse_handover_never_deletes_the_copy_before_restoring_the_slot(outcome, status):
    from test_held_google_instances import AccountAdapters, FakeCalDAV, YandexAdapter, _wire, add_external_calendar, run_open_intents
    ctx, cid, server = google_feed()
    dav = FakeCalDAV()
    work = add_external_calendar(ctx.store, 'work', publish_mode='full')
    ctx.providers = AccountAdapters({ctx.store.get_calendar(cid)['account_id']: ctx.providers.adapter,
                                     ctx.store.get_calendar(work)['account_id']: dav.attach(YandexAdapter('owner@example.test', 'fake'))})
    shared = add_local_calendar(ctx.store, 'shared', publish_mode='full')
    day = _day(5, 0)
    if status == 'tentative':
        when = day + timedelta(hours=11)
        sample = server.put({**instance('weekly', when, when + timedelta(hours=2)), 'status': status})
        assert pull(ctx, cid)['status'] == 'ok'
        record = ctx.store.find_by_external(cid, external_id=sample['id'])
    else:
        record, when = _moved_kept_date(ctx, cid, server, day)
    slot = ops.iso_utc(when)
    assert update(ctx, id=record['id'], calendars=[cid, shared])['status'] == 'updated'
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=3'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    update(ctx, id=master['id'], calendars=[cid, shared, work], scope='all')
    run_open_intents(ctx, 3)
    handover = update(ctx, id=record['id'], calendars=[cid, shared, work])
    if outcome == 'online':
        assert handover['status'] == 'updated'  # no later tick needed for the healthy handover
    else:
        run_open_intents(ctx, 3)  # make the reverse and readback controls independent of the forward ordering
    assert _wire(dav, 'weekly')[0] == [slot]
    resource, = [href for href in dav.resources if 'kept-' in href]
    if outcome == 'readback':
        # A later read first exposes the acknowledged cancellation as a master EXDATE.
        for href, (text, etag) in list(dav.resources.items()):
            if 'weekly' in href:
                dav.resources[href] = (text, etag + '-new')
        cal = ctx.store.get_calendar(work)
        worker.reconcile_calendar(ctx.store, ctx.providers, ctx.providers.adapter_for(cal['account_id']), cal, ctx.tz)
        linked = next(e for e in ctx.store.group_masters(ctx.store.get_event(master['id'])['link_group_id']) if e['calendar_id'] == work)
        assert linked['link_state'] == 'following'
    apply = dav._apply
    methods = []
    def transport(method, url, body, headers):
        methods.append(method)
        if method == 'PUT' and outcome in ('etag_conflict', 'offline'):
            raise ops.ProviderError('conflict' if outcome == 'etag_conflict' else 'network', 'injected restoration failure', unsent=True)
        return apply(method, url, body, headers)
    dav._apply = transport
    result = update(ctx, id=record['id'], calendars=[cid, shared])
    assert [t[1:] for t in listed(ctx, day, calendars=[work])] == [('13:00', '14:00')]
    if outcome in ('online', 'readback'):
        assert result['status'] == 'updated'
        assert methods.index('PUT') < methods.index('DELETE') and resource not in dav.resources
        assert not _wire(dav, 'weekly')[0]
    else:
        assert 'DELETE' not in methods and resource in dav.resources
        assert _wire(dav, 'weekly')[0] == [slot]
        assert result['status'] != 'updated'
    if outcome == 'etag_conflict':
        from conflicts import inspect
        kept_copy = next(e for e in ctx.store.group_masters(ctx.store.get_event(record['id'])['link_group_id']) if e['calendar_id'] == work)
        dav._apply = apply
        info = inspect(ctx, kept_copy['id'])   # follows the failed dependent to the conflicted exception
        assert info['provider']['status'] == 'cancelled' and info['resolution_token']
        assert update(ctx, id=info['event_id'], resolution='local', resolution_token=info['resolution_token'])['status'] == 'pending'
        run_open_intents(ctx, 3)
        assert resource not in dav.resources and not _wire(dav, 'weekly')[0]
        assert [t[1:] for t in listed(ctx, day, calendars=[work])] == [('13:00', '14:00')]
        wire, = [text for href, (text, _) in dav.resources.items() if 'weekly' in href]
        assert f'STATUS:{status.upper()}' in wire


def test_assignment_during_offline_split_never_copies_the_new_series():
    ctx, cid, server = google_feed()
    dest = add_local_calendar(ctx.store, 'unassigned', publish_mode='busy')
    day = _day(5, 0)
    record, when = _moved_kept_date(ctx, cid, server, day)
    server.put(series('weekly', when - timedelta(days=7), 'FREQ=WEEKLY;COUNT=4'))
    pull(ctx, cid)
    master = ctx.store.find_by_external(cid, external_id='weekly')
    server.down = True
    result = ops.update_event(ctx.store, ctx.providers, master['id'] + '@' + ops.iso_utc(when),
                              {'title': 'New segment'}, scope='following')
    date = ctx.store.get_event(record['id'])
    assert date['master_id'] == result['split_master_id'] and not date['external_id']
    answer = update(ctx, id=date['id'], scope='this', calendars=[cid, dest])
    assert answer['status'] == 'failed'
    assert listed(ctx, day, calendars=[dest]) == []
    assert listed(ctx, day + timedelta(days=7), calendars=[dest]) == []
