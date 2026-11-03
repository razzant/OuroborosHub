"""Withdrawn observations finish ordering, without rewriting independent commands or real prerequisites."""
import json
from datetime import timedelta

import pytest

from test_calendar_disconnect_effects import linked, edit_remote
from test_calendar_lifecycle_custody import disconnect
from test_calendar_provider_divergence import run_open_intents, caldav_context, _day
import management
import model
import ops
import tools
from scripts import worker


def queued_series(provider, *, lost_response=False):
    ctx, source, target, edges = linked(provider)
    result = json.loads(tools.cal_update(ctx, id=source['id'], rrule='FREQ=DAILY;COUNT=5', scope='all', confirm=True))
    assert result['status'] == 'updated', result
    source, target = ctx.store.get_event(source['id']), ctx.store.get_event(target['id'])
    adapter, server, target_adapter, target_server = edges
    target_server.down = not lost_response
    if lost_response:
        target_server.lose_response.add('PUT' if target['provider'] != 'google' else 'PATCH')
    edit_remote(source, server, 'Observed title')
    if source['provider'] == 'google':
        server.events[source['external_id']].update(location='Captured location', etag=server._etag())
    else:
        server.edit(source['href'], lambda wire: wire.walk('VEVENT')[0].__setitem__('LOCATION', 'Captured location'))
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    prior = ctx.store.intents_for_event(target['id'])[-1]
    assert prior['state'] == 'pending'
    return ctx, source, target, target_adapter, target_server, prior


def owner_command(ctx, target, action):
    kwargs = {'id': target['id'], 'title': 'Independent owner edit', 'scope': 'all', 'confirm': True}
    if action == 'move_all':
        kwargs['start'] = model.iso_utc(model.parse_stored(target['start_utc']) + timedelta(hours=1))
    elif action == 'following':
        kwargs.update(id=target['id'] + '@' + model.iso_utc(model.parse_stored(target['start_utc']) + timedelta(days=1)), scope='following')
    answer = json.loads(tools.cal_update(ctx, **kwargs))
    assert answer['status'] == 'pending', answer
    with ctx.store._conn() as conn:
        own = [dict(row) for row in conn.execute("SELECT * FROM intents WHERE calendar_id=? AND state='pending' ORDER BY rowid", (target['calendar_id'],))
               if 'source_connection' not in json.loads(row['payload_json'])]
    assert own
    return own


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('action', ['title', 'move_all', 'following'])
@pytest.mark.parametrize('withdraw', [False, True])
def test_independent_structural_command_keeps_captured_snapshot(provider, action, withdraw):
    ctx, source, target, adapter, server, prior = queued_series(provider)
    own = owner_command(ctx, target, action)
    if withdraw:
        disconnect(ctx, source['calendar_id'])
        for intent in own:
            assert ctx.store.get_intent(intent['id'])['payload_json'] == intent['payload_json']
    server.down = False
    run_open_intents(ctx, 4)
    final = [ctx.store.get_intent(intent['id']) for intent in own]
    assert all(intent['state'] == 'done' for intent in final), [(i['state'], i['result_json']) for i in final]
    for before, receipt in zip(own, final):
        desired = json.loads(before['payload_json'])['desired']
        assert json.loads(receipt['payload_json'])['desired'] == desired
        actual = ctx.store.get_event(desired['id'])
        native = adapter.read_current(ctx.store.get_calendar(target['calendar_id']), actual)['event']
        # The independent command owns the snapshot it accepted, including observed content.
        for key in ('title', 'location', 'start_utc', 'end_utc', 'rrule'):
            assert actual[key] == desired[key], (key, actual[key], desired[key])
            assert native[key] == desired[key], (key, native[key], desired[key])
    previous = ctx.store.get_intent(prior['id'])
    assert previous['state'] == ('resolved' if withdraw else 'done')
    if withdraw:
        assert json.loads(previous['result_json'])['outcome'] == 'withdrawn'
        assert previous['payload_json'] == prior['payload_json']


@pytest.mark.parametrize('provider', ['caldav', 'google'])
def test_withdrawn_observation_does_not_rebase_changed_provider_version(provider):
    ctx, source, target, adapter, server, prior = queued_series(provider, lost_response=True)
    own = owner_command(ctx, target, 'move_all')
    disconnect(ctx, source['calendar_id'])
    run_open_intents(ctx, 2)
    assert ctx.store.get_intent(prior['id'])['state'] == 'resolved'
    assert ctx.store.get_intent(own[0]['id'])['state'] == 'conflict'
    assert ctx.store.get_intent(own[0]['id'])['expected_etag'] == own[0]['expected_etag']
    native = adapter.read_current(ctx.store.get_calendar(target['calendar_id']), target)['event']
    assert native['title'] == 'Observed title' and native['location'] == 'Captured location'
    assert native['start_utc'] == target['start_utc']


@pytest.mark.parametrize('edge,kind,state,outcome,source,independent,allowed', [
    ('prior', 'update', 'resolved', 'withdrawn', True, True, True),
    ('prior', 'delete', 'resolved', 'withdrawn', True, True, True),
    ('prior', 'create', 'resolved', 'withdrawn', True, True, False),
    ('after', 'update', 'resolved', 'withdrawn', True, True, False),
    ('prior', 'update', 'uncertain', 'withdrawn', True, True, False),
    ('prior', 'update', 'pending', 'withdrawn', True, True, False),
    ('prior', 'update', 'failed', 'withdrawn', True, True, False),
    ('prior', 'update', 'conflict', 'withdrawn', True, True, False),
    ('prior', 'update', 'resolved', 'provider', True, True, False),
    ('prior', 'update', 'resolved', 'withdrawn', False, True, False),
    ('prior', 'update', 'resolved', 'withdrawn', True, False, False),
    ('after', 'create', 'done', '', False, True, True),
])
def test_ordering_completion_does_not_invent_required_provider_ack(edge, kind, state, outcome, source, independent, allowed):
    ctx, cid, _ = caldav_context()
    row = ctx.store.insert_event({'calendar_id': cid, 'title': 'Saved', 'start_utc': _day(2).isoformat(),
                                 'end_utc': _day(2, 10).isoformat(), 'href': 'https://fixture/existing.ics', 'etag': 'original'})
    provenance = {'account_id': 'source', 'calendar_id': 'source:cal', 'created_at': 'stored'}
    predecessor = ctx.store.add_intent(kind, 'account', cid, row['id'], {'source_connection': provenance} if source else {})
    ctx.store.settle_intent(predecessor['id'], state, {'outcome': outcome})
    payload = {'prior_intent_ids': [predecessor['id']]} if edge == 'prior' else {'after_intent_id': predecessor['id']}
    if not independent:
        payload['source_connection'] = provenance
    successor = ctx.store.add_intent('update', 'account', cid, row['id'], payload)
    assert (ops._blocked_by(ctx.store, successor) is None) == allowed
    with ctx.store._conn() as conn:
        conn.execute('DELETE FROM intents WHERE id=?', (predecessor['id'],))
    assert ops._blocked_by(ctx.store, successor)['id'] == predecessor['id']  # Missing evidence never becomes a completed order.


def retired_source():
    ctx, source, target, (adapter, server, _, target_server) = linked()
    target_server.down = True
    edit_remote(source, server)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    derived = ctx.store.intents_for_event(target['id'])[-1]
    assert derived['state'] == 'pending'
    adapter.delete_calendar = lambda calendar: {'deleted': True, 'calendar_id': calendar['id']}
    deleted = json.loads(management.cal_manage(ctx, action='delete', calendar_id=source['calendar_id'], confirm=True))
    assert deleted['status'] == 'done', deleted
    target_server.down = False
    return ctx, source, target, target_server, derived


def due_rounds(ctx, rounds=3):
    for _ in range(rounds):
        with ctx.store._conn() as conn:
            conn.execute("UPDATE intents SET lease_until=NULL, next_attempt_at=NULL WHERE state='pending'")
        ops.retry_due_intents(ctx.store, ctx.providers)


def test_retired_source_calendar_does_not_strand_independent_follower_edit():
    ctx, source, target, server, prior = retired_source()
    due_rounds(ctx)
    result = json.loads(tools.cal_update(ctx, id=target['id'], title='Owner edit after retirement', confirm=True))
    due_rounds(ctx)
    assert result['status'] == 'updated'
    assert server.events[target['external_id']]['summary'] == 'Owner edit after retirement'
    assert ctx.store.get_intent(prior['id'])['state'] == 'done'


def test_retired_source_calendar_does_not_strand_follower_delete():
    ctx, source, target, server, prior = retired_source()
    json.loads(tools.cal_delete(ctx, id=target['id'], scope='all', confirm=True))
    due_rounds(ctx)
    assert server.events[target['external_id']]['status'] == 'cancelled'
    assert ctx.store.get_intent(prior['id'])['state'] == 'done'


def test_retired_source_calendar_follower_sync_and_queue_still_finish():
    ctx, source, target, server, prior = retired_source()
    calendar = ctx.store.get_calendar(target['calendar_id'])
    adapter = ctx.providers.adapter_for(calendar['account_id'])
    for _ in range(3):
        worker.reconcile_calendar(ctx.store, ctx.providers, adapter, calendar, ctx.tz)
        due_rounds(ctx, 1)
    assert ctx.store.get_intent(prior['id'])['state'] == 'done'
    assert ctx.store.get_event(target['id'])['sync_state'] == 'synced'
    assert server.events[target['external_id']]['summary'] == 'Observed edit'


def test_account_disconnect_after_retirement_keeps_followers_usable():
    ctx, source, target, server, prior = retired_source()
    due_rounds(ctx)
    disconnect(ctx, source['calendar_id'])
    result = json.loads(tools.cal_update(ctx, id=target['id'], title='Owner edit after disconnect', confirm=True))
    due_rounds(ctx)
    assert result['status'] == 'updated'
    assert server.events[target['external_id']]['summary'] == 'Owner edit after disconnect'
