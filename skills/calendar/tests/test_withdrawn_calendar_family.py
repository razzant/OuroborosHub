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
