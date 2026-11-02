"""Withdrawal restores confirmed families without changing known or unknown provider effects."""
import json

import pytest

from test_calendar_provider_divergence import caldav_context, _day, run_open_intents
from test_calendar_lifecycle_custody import disconnect, reconnect, sync
from model import event_time_kind
import reminders
import tools


def series(ctx, cid, override=True):
    made = json.loads(tools.cal_create(ctx, title='Series', start=_day(2).isoformat(), duration_min=60,
                                      rrule='FREQ=DAILY;COUNT=5', calendars=[cid], confirm=True))
    eid = made['event']['id']
    if override:
        changed = json.loads(tools.cal_update(ctx, id=eid + '@' + _day(5).isoformat(), title='Confirmed override', confirm=True))
        assert changed['status'] == 'updated'
    return eid


def visible(ctx, cid):
    return json.loads(tools.cal_events(ctx, start=_day(2, 0).isoformat(), end=_day(8, 0).isoformat(), calendars=[cid]))['events']


def stable(row):
    return {key: row[key] for key in ('id', 'uid', 'origin', 'master_id', 'recurrence_id', 'title', 'start_utc',
                                    'end_utc', 'rrule', 'tz', 'time_kind', 'all_day', 'created_at', 'deleted_at')}


def test_withdrawn_single_delete_restores_state_provider_updates_and_reminder():
    ctx, cid, server = caldav_context()
    made = json.loads(tools.cal_create(ctx, title='Published', start=_day(2).isoformat(), reminders=[0], calendars=[cid], confirm=True))
    eid = made['event']['id']; original = ctx.store.get_event(eid)
    server.down = True
    assert json.loads(tools.cal_delete(ctx, id=eid, confirm=True))['status'] == 'pending'
    ctx.store.update_event(eid, {'sync_state': 'synced'})
    assert ctx.store.get_event(eid)['sync_state'] == 'pending_delete'  # Ordinary ACK guard remains intact.
    count, account, calendar = disconnect(ctx, cid)
    assert count == 1 and ctx.store.get_event(eid)['sync_state'] == 'synced'
    assert stable(ctx.store.get_event(eid)) == stable(original)
    server.down = False; reconnect(ctx, account, calendar)
    raw, _ = server.resources[original['href']]
    server.resources[original['href']] = (raw.replace('SUMMARY:Published', 'SUMMARY:Provider changed'), 'provider-v2')
    calls = len(server.calls)
    sync(ctx, cid); run_open_intents(ctx, 2)
    assert ctx.store.get_event(eid)['title'] == 'Provider changed'
    assert not [call for call in server.calls[calls:] if call[0] in ('PUT', 'DELETE')]
    reminders.set_mode(ctx.store, cid, True)
    reminders.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=_day(1))
    sent = []
    class Channel:
        def send(self, key, text): sent.append(key); return 'sent', 'synthetic accepted'
    reminders.deliver_due(ctx.store, Channel(), ctx.tz, now=_day(2))
    assert len(sent) == 1


@pytest.mark.parametrize('scope', ['this', 'following', 'all'])
def test_withdrawn_delete_restores_confirmed_exception_identity_before_and_after_sync(scope):
    ctx, cid, server = caldav_context(); eid = series(ctx, cid)
    master = ctx.store.get_event(eid); original, = ctx.store.exceptions_for(eid)
    before = stable(original); server.down = True
    address = eid if scope == 'all' else eid + '@' + _day(5 if scope == 'this' else 4).isoformat()
    assert json.loads(tools.cal_delete(ctx, id=address, scope=scope, confirm=True))['status'] == 'pending'
    count, account, calendar = disconnect(ctx, cid)
    assert count == 1
    assert stable(ctx.store.get_event(original['id'])) == before
    assert ctx.store.get_event(eid)['rrule'] == master['rrule']
    assert ctx.store.get_event(eid)['sync_state'] == 'synced'
    assert [row['id'] for row in ctx.store.exceptions_for(eid)] == [original['id']]
    server.down = False; reconnect(ctx, account, calendar); calls = len(server.calls)
    sync(ctx, cid); run_open_intents(ctx, 2)
    assert [row['id'] for row in ctx.store.exceptions_for(eid)] == [original['id']]
    refreshed = ctx.store.get_event(original['id'])
    assert event_time_kind(refreshed) == event_time_kind(original) == 'absolute'
    assert {**stable(refreshed), 'time_kind': 'absolute'} == {**before, 'time_kind': 'absolute'}
    assert len(visible(ctx, cid)) == 5
    assert not [call for call in server.calls[calls:] if call[0] in ('PUT', 'DELETE')]


def test_withdrawn_new_cancellation_retires_only_its_unsubmitted_row():
    ctx, cid, server = caldav_context(); eid = series(ctx, cid, override=False)
    wire = dict(server.resources); server.down = True
    assert json.loads(tools.cal_delete(ctx, id=eid + '@' + _day(4).isoformat(), scope='this', confirm=True))['status'] == 'pending'
    child, = ctx.store.exceptions_for(eid)
    receipt = ctx.store.intents_for_event(child['id'])[-1]
    count, account, calendar = disconnect(ctx, cid)
    assert count == 1 and ctx.store.get_event(child['id'])['deleted_at']
    assert ctx.store.get_event(child['id'])['uid'] == child['uid']
    kept = ctx.store.get_intent(receipt['id'])
    assert kept['state'] == 'resolved' and kept['payload_json'] == receipt['payload_json']
    server.down = False; reconnect(ctx, account, calendar); sync(ctx, cid); run_open_intents(ctx, 2)
    assert len(visible(ctx, cid)) == 5 and ctx.store.exceptions_for(eid) == []
    assert server.resources == wire


def test_withdrawn_following_edit_retires_unpublished_segment_and_keeps_history():
    ctx, cid, server = caldav_context(); eid = series(ctx, cid)
    original, = ctx.store.exceptions_for(eid); before = stable(original); wire = dict(server.resources)
    server.down = True
    assert json.loads(tools.cal_update(ctx, id=eid + '@' + _day(4).isoformat(), scope='following', title='Future split', confirm=True))['status'] == 'pending'
    queued = ctx.store.open_intents()
    created = {value for intent in queued for value in json.loads(intent['payload_json']).get('created_ids', [])}
    assert created
    for created_id in created:
        for state in ('scheduled', 'sent', 'unknown'):
            notice = created_id + ':' + state
            ctx.store.schedule_reminder(created_id, _day(4).isoformat(), 0, _day(4).isoformat(), notice)
            with ctx.store._conn() as connection:
                connection.execute('UPDATE reminders SET state=? WHERE notice_id=?', (state, notice))
    count, account, calendar = disconnect(ctx, cid)
    assert count == len(queued)
    assert stable(ctx.store.get_event(original['id'])) == before
    for created_id in created:
        assert ctx.store.get_event(created_id)['deleted_at']
        with ctx.store._conn() as connection:
            states = {row['state'] for row in connection.execute('SELECT state FROM reminders WHERE event_id=?', (created_id,))}
        assert states == {'sent', 'unknown'}
    for intent in queued:
        retained = ctx.store.get_intent(intent['id'])
        assert retained['state'] == 'resolved' and retained['payload_json'] == intent['payload_json']
    server.down = False; reconnect(ctx, account, calendar); sync(ctx, cid); run_open_intents(ctx, 2)
    assert len(visible(ctx, cid)) == 5
    assert not any(row['title'] == 'Future split' for row in visible(ctx, cid))
    assert server.resources == wire


def test_withdrawal_keeps_completed_prefix_and_its_unpublished_continuation_draft():
    ctx, cid, server = caldav_context(); eid = series(ctx, cid)
    original_href = ctx.store.get_event(eid)['href']; adapter = ctx.providers.adapter_for(ctx.store.get_calendar(cid)['account_id'])
    create = adapter.create
    def unavailable_new_segment(*args, **kwargs):
        import ops
        raise ops.ProviderError('network', 'new segment endpoint unavailable')
    adapter.create = unavailable_new_segment
    result = json.loads(tools.cal_update(ctx, id=eid + '@' + _day(4).isoformat(), scope='following', title='Future split', confirm=True))
    assert result['status'] == 'pending'
    completed = ctx.store.intents_for_event(eid)[-1]
    assert completed['state'] == 'done'
    committed_rule = ctx.store.get_event(eid)['rrule']; wire = dict(server.resources)
    new_master = next(row for row in ctx.store.window(_day(2), _day(8)) if not row['master_id'] and row['id'] != eid)
    uid = new_master['uid']; count, account, calendar = disconnect(ctx, cid)
    assert count and ctx.store.get_intent(completed['id'])['state'] == 'done'
    assert ctx.store.get_event(eid)['rrule'] == committed_rule
    draft = ctx.store.get_event(new_master['id'])
    assert not draft['deleted_at'] and draft['sync_state'] == 'failed' and draft['uid'] == uid
    adapter.create = create; reconnect(ctx, account, calendar); run_open_intents(ctx, 2)
    assert server.resources == wire and original_href in wire


@pytest.mark.parametrize('state', ['leased', 'uncertain'])
def test_withdrawal_protects_unknown_compound_snapshot_and_created_family(state):
    ctx, cid, server = caldav_context(); eid = series(ctx, cid)
    server.down = True
    assert json.loads(tools.cal_update(ctx, id=eid + '@' + _day(4).isoformat(), scope='following', title='Future split', confirm=True))['status'] == 'pending'
    intents = ctx.store.open_intents(); first = intents[0]
    payload = json.loads(first['payload_json'])
    protected = {first['event_id'], *payload.get('created_ids', []), *(row['id'] for row in payload.get('restore_rows', []))}
    snapshots = {value: stable(ctx.store.get_event(value)) for value in protected}
    leased = ctx.store.lease_intent(first['id'], 'synthetic-provider-worker')
    assert leased
    if state == 'uncertain': ctx.store.settle_intent(first['id'], 'uncertain', {'message': 'Existing unknown outcome'})
    count, account, calendar = disconnect(ctx, cid)
    assert count == len(intents) - 1 and ctx.store.get_intent(first['id'])['state'] == 'uncertain'
    assert {value: stable(ctx.store.get_event(value)) for value in protected} == snapshots
    if state == 'leased':
        assert ctx.store.current_leased_intent(leased) is None
        assert ctx.store.current_leased_intent(leased, allow_uncertain=True)
        assert ctx.store.acknowledge_write(leased, ctx.store.get_event(eid), {'etag': 'synthetic-late-ack'}, leased['expected_etag'], shared_resource=True)
        assert ctx.store.get_intent(first['id'])['state'] == 'done'
    else: assert json.loads(ctx.store.get_intent(first['id'])['result_json'])['message'] == 'Existing unknown outcome'


@pytest.mark.parametrize('shape', ['following', 'this', 'following_then_this'])
@pytest.mark.parametrize('reserved_identity', [False, True])
def test_unpublished_draft_retains_its_complete_split_and_override(shape, reserved_identity):
    ctx, cid, server = caldav_context()
    server.down = True
    made = json.loads(tools.cal_create(ctx, title='Draft', start=_day(2).isoformat(), duration_min=60,
                                      rrule='FREQ=DAILY;COUNT=5', calendars=[cid], confirm=True))
    eid = made['event']['id']; uid = ctx.store.get_event(eid)['uid']
    assert made['status'] == 'pending'
    if reserved_identity:
        # An address may be reserved before publication; it is not a provider ACK.
        ctx.store.update_event(eid, {'external_id': 'reserved-' + uid, 'href': 'https://calendar.example.test/reserved.ics'})
    if shape in ('following', 'following_then_this'):
        changed = json.loads(tools.cal_update(ctx, id=eid + '@' + _day(4).isoformat(), scope='following', title='Draft later', confirm=True))
        assert changed['status'] == 'pending'
        segment = next(row for row in ctx.store.window(_day(2), _day(8)) if row['id'] != eid and not row['master_id'])
        if shape == 'following_then_this':
            assert json.loads(tools.cal_update(ctx, id=segment['id'] + '@' + _day(5).isoformat(), title='Draft override', confirm=True))['status'] == 'pending'
    else:
        assert json.loads(tools.cal_update(ctx, id=eid + '@' + _day(4).isoformat(), title='Draft override', confirm=True))['status'] == 'pending'
    before = [(row['id'], row['title'], row['start'], row['end']) for row in visible(ctx, cid)]
    assert len(before) == 5 and server.resources == {}
    assert not any(intent['state'] == 'done' for intent in ctx.store.intents_for_event(eid))
    count, account, calendar = disconnect(ctx, cid)
    assert count
    server.down = False; reconnect(ctx, account, calendar); calls = len(server.calls)
    run_open_intents(ctx, 2)
    after = visible(ctx, cid)
    assert [(row['id'], row['title'], row['start'], row['end']) for row in after] == before
    assert all(row.get('sync_state') == 'failed' for row in after)
    assert ctx.store.get_event(eid)['uid'] == uid
    assert server.resources == {} and len(server.calls) == calls


def test_restored_provider_view_invalidates_existing_ctag_before_normal_sync():
    from scripts.worker import sync_account
    ctx, cid, server = caldav_context(); eid = series(ctx, cid, override=False)
    calendar = ctx.store.get_calendar(cid); account = ctx.store.get_account(calendar['account_id'])
    adapter = ctx.providers.adapter_for(account['id'])
    adapter.list_calendars = lambda: [{**calendar, 'ctag': 'same-provider-generation'}]
    server.down = True
    assert json.loads(tools.cal_delete(ctx, id=eid + '@' + _day(4).isoformat(), scope='this', confirm=True))['status'] == 'pending'
    server.down = False
    row = ctx.store.get_event(eid); raw, _ = server.resources[row['href']]
    server.resources[row['href']] = (raw.replace('SUMMARY:Series', 'SUMMARY:Provider newer'), '"external-v2"')
    sync_account(ctx.store, ctx.providers, account, ctx.tz)
    assert ctx.store.get_event(eid)['title'] == 'Provider newer'
    assert ctx.store.get_sync_state(cid)['cursor'] == 'same-provider-generation'
    count, account, calendar = disconnect(ctx, cid)
    assert count == 1 and ctx.store.get_event(eid)['title'] == 'Series'
    assert ctx.store.get_sync_state(cid)['cursor'] == ''
    reconnect(ctx, account, calendar); calls = len(server.calls)
    sync_account(ctx.store, ctx.providers, account, ctx.tz)
    sync_account(ctx.store, ctx.providers, account, ctx.tz)
    assert ctx.store.get_event(eid)['title'] == 'Provider newer'
    assert ctx.store.get_sync_state(cid)['cursor'] == 'same-provider-generation'
    assert not [call for call in server.calls[calls:] if call[0] in ('PUT', 'DELETE')]


def test_restored_google_view_requests_full_listing_instead_of_consumed_sync_token():
    from test_calendar_provider_divergence import google_context
    from scripts.worker import sync_account
    ctx, cid, server = google_context(); eid = series(ctx, cid, override=False)
    calendar = ctx.store.get_calendar(cid); account = ctx.store.get_account(calendar['account_id'])
    adapter = ctx.providers.adapter_for(account['id'])
    adapter.list_calendars = lambda: [calendar]
    fetch = adapter.fetch; cursors = []
    def observed_fetch(calendar, cursor='', tz=None):
        cursors.append(cursor)
        return fetch(calendar, cursor, tz)
    adapter.fetch = observed_fetch
    server.down = True
    assert json.loads(tools.cal_delete(ctx, id=eid + '@' + _day(4).isoformat(), scope='this', confirm=True))['status'] == 'pending'
    server.down = False
    external_id = ctx.store.get_event(eid)['external_id']
    server.events[external_id].update(summary='Provider newer', etag=server._etag())
    sync_account(ctx.store, ctx.providers, account, ctx.tz)
    assert ctx.store.get_event(eid)['title'] == 'Provider newer'
    assert ctx.store.get_sync_state(cid)['cursor']
    count, account, calendar = disconnect(ctx, cid)
    assert count == 1 and ctx.store.get_event(eid)['title'] == 'Series'
    assert ctx.store.get_sync_state(cid)['cursor'] == ''
    reconnect(ctx, account, calendar); fetched = len(cursors); calls = len(server.calls)
    sync_account(ctx.store, ctx.providers, account, ctx.tz)
    assert cursors[fetched] == ''
    listing = [call for call in server.calls[calls:] if call[0] == 'GET' and call[1].endswith('/events')]
    assert listing and 'syncToken' not in listing[0][2]
    assert 'timeMin' in listing[0][2] and listing[0][2]['showDeleted'] == 'false'
    assert ctx.store.get_event(eid)['title'] == 'Provider newer'
    assert not [call for call in server.calls[calls:] if call[0] in ('POST', 'PATCH', 'DELETE')]


def _chain_rows(ctx, cid):
    with ctx.store._conn() as connection:
        return {row['id']: dict(row) for row in connection.execute('SELECT * FROM events WHERE calendar_id=?', (cid,))}


def _chain_visible(ctx, cid):
    events = json.loads(tools.cal_events(ctx, start=_day(2, 0).isoformat(), end=_day(11, 0).isoformat(), calendars=[cid]))['events']
    return [(row['id'], row['title'], row['start'], row['end']) for row in events]


def _successive_splits(ctx, cid, root_id, count):
    roots = [root_id]
    for index, day in enumerate((4, 6, 8)[:count], 1):
        known = set(_chain_rows(ctx, cid))
        changed = json.loads(tools.cal_update(ctx, id=roots[-1] + '@' + _day(day).isoformat(),
                                             scope='following', title=f'Unsent segment {index}', confirm=True))
        assert changed['status'] == 'pending'
        new_roots = [row['id'] for row in _chain_rows(ctx, cid).values() if row['id'] not in known and not row['master_id']]
        assert len(new_roots) == 1
        roots.append(new_roots[0])
    # The same order consumed by withdrawal is the actual admission order, not timestamp/UUID ordering.
    with ctx.store._conn() as connection:
        admitted = [dict(row) for row in connection.execute('SELECT * FROM intents WHERE calendar_id=? ORDER BY rowid', (cid,))]
    for child in roots[1:-1]:
        made_at = next(i for i, row in enumerate(admitted) if child in json.loads(row['payload_json']).get('created_ids', []))
        used_at = next(i for i, row in enumerate(admitted) if json.loads(row['payload_json']).get('family_root_id') == child
                      and json.loads(row['payload_json']).get('created_ids'))
        assert made_at < used_at
    return roots


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('split_count', [2, 3])
@pytest.mark.parametrize('origin', ['confirmed', 'draft', 'completed_prefix'])
def test_successive_split_withdrawal_inherits_confirmed_origin_only(provider, split_count, origin):
    from test_calendar_provider_divergence import google_context
    import ops
    ctx, cid, server = (caldav_context if provider == 'caldav' else google_context)()
    server.down = origin == 'draft'
    made = json.loads(tools.cal_create(ctx, title='Series', start=_day(2).isoformat(), duration_min=60,
                                      rrule='FREQ=DAILY;COUNT=8', calendars=[cid], confirm=True))
    eid = made['event']['id']
    assert made['status'] == ('pending' if origin == 'draft' else 'created')
    if origin != 'draft' and provider == 'caldav':
        for day, title in ((3, 'Confirmed early'), (9, 'Confirmed late')):
            assert json.loads(tools.cal_update(ctx, id=eid + '@' + _day(day).isoformat(), title=title, confirm=True))['status'] == 'updated'
        sync(ctx, cid)
    original_rows = _chain_rows(ctx, cid)
    original_view = _chain_visible(ctx, cid)
    assert len(original_view) == 8
    adapter = ctx.providers.adapter_for(ctx.store.get_calendar(cid)['account_id'])
    create = adapter.create
    if origin == 'completed_prefix':
        def unavailable(*args, **kwargs):
            raise ops.ProviderError('network', 'new segment endpoint unavailable')
        adapter.create = unavailable
    else:
        server.down = True
    roots = _successive_splits(ctx, cid, eid, split_count)
    if origin == 'completed_prefix':
        assert ctx.store.intents_for_event(eid)[-1]['state'] == 'done'
    override_day = 7 if split_count == 2 else 8
    known = set(_chain_rows(ctx, cid))
    assert json.loads(tools.cal_update(ctx, id=roots[-1] + '@' + _day(override_day).isoformat(),
                                      title='Unsent descendant override', confirm=True))['status'] == 'pending'
    descendant = next(row['id'] for row in _chain_rows(ctx, cid).values() if row['id'] not in known)
    staged_view = _chain_visible(ctx, cid)
    committed_prefix = stable(ctx.store.get_event(eid))
    queued = ctx.store.open_intents()
    count, account, calendar = disconnect(ctx, cid)
    assert count == len(queued)
    adapter.create = create; server.down = False
    reconnect(ctx, account, calendar); calls = len(server.calls)
    run_open_intents(ctx, 2)
    if origin == 'confirmed':
        assert _chain_visible(ctx, cid) == original_view
        for row_id, row in original_rows.items():
            assert stable(ctx.store.get_event(row_id)) == stable(row)
        for row_id in {*roots[1:], descendant}:
            assert ctx.store.get_event(row_id)['deleted_at']
        sync(ctx, cid)
        assert _chain_visible(ctx, cid) == original_view
        assert set(row['id'] for row in _chain_rows(ctx, cid).values() if not row['deleted_at']) == set(original_rows)
    else:
        assert _chain_visible(ctx, cid) == staged_view
        assert all(not ctx.store.get_event(row_id)['deleted_at'] for row_id in {*roots, descendant})
        for row_id in roots[1:] + [descendant]:
            assert ctx.store.get_event(row_id)['sync_state'] == 'failed'
        if origin == 'draft': assert ctx.store.get_event(eid)['sync_state'] == 'failed'
        else:
            assert stable(ctx.store.get_event(eid)) == committed_prefix
            assert ctx.store.get_event(eid)['sync_state'] == 'synced'
    for row in queued:
        retained = ctx.store.get_intent(row['id'])
        assert retained['state'] == 'resolved' and retained['payload_json'] == row['payload_json']
    assert not [call for call in server.calls[calls:] if call[0] in ('POST', 'PUT', 'PATCH', 'DELETE')]


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('head', ['original', 'segment_birth'])
@pytest.mark.parametrize('state', ['leased', 'uncertain'])
def test_unknown_split_root_never_grants_retirement_authority_to_descendants(provider, head, state):
    from test_calendar_provider_divergence import google_context
    ctx, cid, server = (caldav_context if provider == 'caldav' else google_context)()
    eid = json.loads(tools.cal_create(ctx, title='Series', start=_day(2).isoformat(), duration_min=60,
                                     rrule='FREQ=DAILY;COUNT=8', calendars=[cid], confirm=True))['event']['id']
    server.down = True
    roots = _successive_splits(ctx, cid, eid, 3)
    selected = ctx.store.intents_for_event(eid)[-1] if head == 'original' else next(
        row for row in ctx.store.intents_for_event(roots[1]) if row['kind'] == 'create')
    leased = ctx.store.lease_intent(selected['id'], 'synthetic-provider-worker')
    assert leased
    if state == 'uncertain': ctx.store.settle_intent(selected['id'], 'uncertain', {'message': 'Existing unknown outcome'})
    snapshots = {row_id: stable(ctx.store.get_event(row_id)) for row_id in roots[1:]}
    disconnect(ctx, cid)
    assert {row_id: stable(ctx.store.get_event(row_id)) for row_id in roots[1:]} == snapshots
    assert ctx.store.get_intent(selected['id'])['state'] == 'uncertain'
    if state == 'leased':
        assert ctx.store.current_leased_intent(leased) is None
        assert ctx.store.current_leased_intent(leased, allow_uncertain=True)
        assert ctx.store.acknowledge_write(leased, ctx.store.get_event(selected['event_id']),
            {'etag': 'synthetic-late-ack'}, leased['expected_etag'], shared_resource=provider == 'caldav')
        assert ctx.store.get_intent(selected['id'])['state'] == 'done'
    else:
        assert json.loads(ctx.store.get_intent(selected['id'])['result_json'])['message'] == 'Existing unknown outcome'
    # Consistent post-ACK recovery of withdrawn dependents is the disclosed A1 boundary, not this repair.
