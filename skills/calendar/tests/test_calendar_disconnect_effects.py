"""Disconnect withdraws observations and dependent effects, retaining actual delivery custody."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_calendar_provider_divergence import caldav_context, _day, FakeGoogle, add_google_calendar, run_open_intents
from test_calendar_lifecycle_custody import disconnect, reconnect
from providers_google import GoogleAdapter
import model
import ops
import reminders
import tools
from scripts import worker


def linked(source_provider='caldav'):
    ctx, caldav, dav = caldav_context()
    google = add_google_calendar(ctx.store)
    gs = FakeGoogle()
    ga = gs.attach(GoogleAdapter('owner@example.test', 'fixture', 'fixture',
                                model.now_utc() + timedelta(days=1), 'fixture', '', None))
    da = ctx.providers.adapter
    dav_account = ctx.store.get_calendar(caldav)['account_id']
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda aid: da if aid == dav_account else ga)
    source_id, target_id = (caldav, google) if source_provider == 'caldav' else (google, caldav)
    made = json.loads(tools.cal_create(ctx, title='Original', start=_day(2).isoformat(),
                                      calendars=[source_id, target_id], confirm=True))
    assert made['status'] == 'created', made
    source = ctx.store.get_event(made['event']['id'])
    target = next(row for row in ctx.store.group_members(source['link_group_id']) if row['calendar_id'] == target_id)
    return ctx, source, target, (da, dav, ga, gs) if source_provider == 'caldav' else (ga, gs, da, dav)


def edit_remote(source, server, title='Observed edit'):
    if source['provider'] == 'google':
        server.events[source['external_id']].update(summary=title, etag=server._etag())
    else:
        server.edit(source['href'], lambda wire: wire.walk('VEVENT')[0].__setitem__('SUMMARY', title))


def effects(server, after=0):
    return [call for call in server.calls[after:] if call[0] in ('POST', 'PUT', 'PATCH', 'DELETE')]


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('withdraw', [False, 'disconnect', 'reconnect'])
def test_delayed_feed_keeps_source_connection_and_connected_control(provider, withdraw):
    ctx, source, target, (adapter, server, _, target_server) = linked(provider)
    edit_remote(source, server)
    fetch = adapter.fetch
    def delayed(*args, **kwargs):
        result = fetch(*args, **kwargs)
        if withdraw:
            _, account, calendar = disconnect(ctx, source['calendar_id'])
            if withdraw == 'reconnect':
                # Existing created_at is a stored timestamp, not a new connection epoch.
                with patch('store.now_utc', return_value=model.now_utc() + timedelta(seconds=2)):
                    reconnect(ctx, account, calendar)
        return result
    adapter.fetch = delayed
    prior = len(target_server.calls)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    assert ctx.store.get_event(source['id'])['title'] == ('Original' if withdraw else 'Observed edit')
    assert ctx.store.get_event(target['id'])['title'] == ('Original' if withdraw else 'Observed edit')
    assert bool(effects(target_server, prior)) is (not withdraw)


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('withdraw', [False, True])
def test_late_provider_absence_cannot_start_new_follower_delete(provider, withdraw):
    ctx, source, target, (adapter, server, _, target_server) = linked(provider)
    if provider == 'caldav':
        server.resources.clear()
        original = adapter.exists
        def exists(*args, **kwargs):
            result = original(*args, **kwargs)
            if withdraw: disconnect(ctx, source['calendar_id'])
            return result
        adapter.exists = exists
    else:
        server.events[source['external_id']].update(status='cancelled', etag=server._etag())
        original = adapter.fetch
        def fetch(*args, **kwargs):
            result = original(*args, **kwargs)
            if withdraw: disconnect(ctx, source['calendar_id'])
            return result
        adapter.fetch = fetch
    prior = len(target_server.calls)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    assert bool(effects(target_server, prior)) is (not withdraw)
    assert bool(ctx.store.get_event(target['id'])) is bool(withdraw)


@pytest.mark.parametrize('independent_edit', [False, True])
def test_queued_propagation_withdraws_but_independent_follower_edit_survives(independent_edit):
    ctx, source, target, (adapter, server, _, target_server) = linked()
    target_server.down = True
    edit_remote(source, server)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    propagated = ctx.store.intents_for_event(target['id'])[-1]
    assert propagated['state'] == 'pending'
    owned = None
    if independent_edit:
        result = json.loads(tools.cal_update(ctx, id=target['id'], title='Independent edit', confirm=True))
        assert result['status'] == 'pending'
        owned = ctx.store.intents_for_event(target['id'])[-1]
    count, account, calendar = disconnect(ctx, source['calendar_id'])
    assert count == 1
    withdrawn = ctx.store.get_intent(propagated['id'])
    assert withdrawn['state'] == 'resolved'
    assert json.loads(withdrawn['result_json'])['outcome'] == 'withdrawn'
    assert ctx.store.get_event(target['id'])['title'] == ('Independent edit' if owned else 'Original')
    if owned:
        assert ctx.store.get_intent(owned['id']) == owned
    target_server.down = False
    prior = len(target_server.calls)
    reconnect(ctx, account, calendar)
    run_open_intents(ctx)
    assert len(effects(target_server, prior)) == int(bool(owned))
    assert target_server.events[target['external_id']]['summary'] == ('Independent edit' if owned else 'Original')
    result = json.loads(tools.cal_update(ctx, id=target['id'], title='New owner edit', confirm=True))
    assert result['status'] == 'updated'
    assert target_server.events[target['external_id']]['summary'] == 'New owner edit'


@pytest.mark.parametrize('outcome', ['ack', 'lost_response'])
def test_inflight_propagation_keeps_late_receipt_and_never_reconnect_resends(outcome):
    ctx, source, target, (adapter, server, target_adapter, target_server) = linked()
    edit_remote(source, server)
    update, saved = target_adapter.update, {}
    def delayed(*args, **kwargs):
        _, account, calendar = disconnect(ctx, source['calendar_id'])
        saved.update(account=account, calendar=calendar)
        result = update(*args, **kwargs)
        if outcome == 'lost_response': raise ops.ProviderError('network', 'lost after apply')
        return result
    target_adapter.update = delayed
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    intent = ctx.store.intents_for_event(target['id'])[-1]
    assert intent['state'] == ('done' if outcome == 'ack' else 'uncertain')
    assert target_server.events[target['external_id']]['summary'] == 'Observed edit'
    target_adapter.update = update
    reconnect(ctx, saved['account'], saved['calendar'])
    prior = len(target_server.calls)
    run_open_intents(ctx)
    assert not effects(target_server, prior)


def reminder_case():
    ctx, cid, _ = caldav_context()
    now = model.now_utc().replace(microsecond=0)
    made = json.loads(tools.cal_create(ctx, title='Reminder', start=(now + timedelta(minutes=10)).isoformat(),
                                      calendars=[cid], reminders=[15, 5], confirm=True))
    eid = made['event']['id']
    reminders.set_mode(ctx.store, cid, True)
    reminders.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=now)
    return ctx, cid, eid, now


def rows(store, event_id):
    with store._conn() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM reminders WHERE event_id=? ORDER BY fire_at_utc', (event_id,))]


@pytest.mark.parametrize('waiting', ['scheduled', 'no_channel'])
def test_disconnect_withdraws_due_and_future_notices_preserving_terminal_history(waiting):
    ctx, cid, eid, now = reminder_case()
    before = rows(ctx.store, eid)
    assert len(before) == 2
    for row in before: ctx.store.mark_reminder(row['id'], waiting)
    event = ctx.store.get_event(eid)
    for i, terminal in enumerate(('sent', 'unknown')):
        ctx.store.schedule_reminder(eid, event['start_utc'], 20+i, model.iso_utc(now), 'retained-'+terminal)
        item = next(row for row in rows(ctx.store, eid) if row['notice_id'] == 'retained-'+terminal)
        ctx.store.mark_reminder(item['id'], terminal, 'delivery custody')
    history = [row for row in rows(ctx.store, eid) if row['state'] in ('sent', 'unknown')]
    disconnect(ctx, cid)
    assert rows(ctx.store, eid) == history
    sent = []
    result = reminders.deliver_due(ctx.store, SimpleNamespace(send=lambda key, text: (sent.append(key) or ('sent', 'accepted'))), ctx.tz, now=now)
    assert sent == [] and result['sent'] == 0
    assert rows(ctx.store, eid) == history


@pytest.mark.parametrize('race', ['due_snapshot', 'reservation', 'after_reservation'])
def test_disconnect_notice_races_keep_actual_send_custody(race):
    ctx, cid, eid, now = reminder_case()
    original_due, original_reserve = ctx.store.due_reminders, ctx.store.reserve_reminder_send
    if race == 'due_snapshot':
        def due(*args, **kwargs):
            result = original_due(*args, **kwargs); disconnect(ctx, cid); return result
        ctx.store.due_reminders = due
    else:
        def reserve(ids):
            if race == 'reservation':
                disconnect(ctx, cid); return original_reserve(ids)
            reserved = original_reserve(ids); disconnect(ctx, cid); return reserved
        ctx.store.reserve_reminder_send = reserve
    sent = []
    result = reminders.deliver_due(ctx.store, SimpleNamespace(send=lambda key, text: (sent.append(key) or ('sent', 'accepted'))), ctx.tz, now=now)
    assert len(sent) == result['sent'] == int(race == 'after_reservation')
    assert all(row['state'] == 'sent' for row in rows(ctx.store, eid))


def test_connected_and_local_reminders_still_send():
    ctx, cid, eid, now = reminder_case()
    local = json.loads(tools.cal_create(ctx, title='Local', start=(now + timedelta(minutes=10)).isoformat(), reminders=[15], confirm=True))
    sent = []
    result = reminders.deliver_due(ctx.store, SimpleNamespace(send=lambda key, text: (sent.append(key) or ('sent', 'accepted'))), ctx.tz, now=now)
    assert result['sent'] == len(sent) == 2
    assert any(row['state'] == 'sent' for row in rows(ctx.store, eid))
    assert any(row['state'] == 'sent' for row in rows(ctx.store, local['event']['id']))


@pytest.mark.parametrize('terminal', ['sent', 'unknown'])
def test_old_due_snapshot_cannot_overwrite_another_attempts_terminal_custody(terminal):
    ctx, cid, eid, now = reminder_case()
    original = ctx.store.due_reminders
    kept = []
    def stale(*args, **kwargs):
        due = original(*args, **kwargs)
        ids = ctx.store.reserve_reminder_send([row['id'] for row in due])
        assert ids
        for rid in ids: ctx.store.mark_reminder(rid, terminal, 'other attempt')
        kept.extend(row for row in rows(ctx.store, eid) if row['id'] in ids)
        disconnect(ctx, cid)
        return due
    ctx.store.due_reminders = stale
    sent = []
    reminders.deliver_due(ctx.store, SimpleNamespace(send=lambda key, text: (sent.append(key) or ('sent', 'accepted'))), ctx.tz, now=now)
    assert not sent and rows(ctx.store, eid) == kept


def test_disconnect_after_lease_before_provider_call_preserves_owed_custody():
    ctx, source, target, (adapter, server, target_adapter, target_server) = linked()
    edit_remote(source, server)
    original = ctx.providers.adapter_for
    fired = []
    def adapter_for(account_id):
        result = original(account_id)
        if account_id == target['account_id'] and not fired:
            fired.append(True)
            disconnect(ctx, source['calendar_id'])
        return result
    ctx.providers.adapter_for = adapter_for
    prior = len(target_server.calls)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    assert fired and not effects(target_server, prior)
    assert ctx.store.intents_for_event(target['id'])[-1]['state'] == 'uncertain'


def test_disconnect_reports_unknown_dependent_attempt_on_other_account():
    ctx, source, target, (adapter, server, _, target_server) = linked()
    target_server.down = True
    edit_remote(source, server)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    intent = ctx.store.intents_for_event(target['id'])[-1]
    assert ctx.store.lease_intent(intent['id'], 'another-attempt')
    with patch('credentials._mutate_tokens', side_effect=lambda state, key, apply: apply({})):
        result = json.loads(tools.cal_settings(ctx, action='disconnect', calendar_id=source['account_id'], confirm=True))
    assert result['cancelled_intents'] == 0 and result['uncertain_intents'] == 1
    assert ctx.store.get_intent(intent['id'])['state'] == 'uncertain'


def test_old_unattributed_intent_is_not_relabelled_from_current_link_source():
    ctx, source, target, (adapter, server, _, target_server) = linked()
    target_server.down = True
    edit_remote(source, server)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    intent = ctx.store.intents_for_event(target['id'])[-1]
    payload = json.loads(intent['payload_json'])
    payload.pop('source_connection')  # Exact missing fact in the previously published producer.
    with ctx.store._conn() as conn:
        conn.execute('UPDATE intents SET payload_json=? WHERE id=?', (json.dumps(payload), intent['id']))
    old = ctx.store.get_intent(intent['id'])
    disconnect(ctx, source['calendar_id'])
    assert ctx.store.get_intent(intent['id']) == old
    assert old['state'] == 'pending'


@pytest.mark.parametrize('action', ['source', 'detach'])
def test_withdrawing_observation_preserves_new_owner_relationship_choice(action):
    ctx, source, target, (adapter, server, _, target_server) = linked()
    target_server.down = True
    edit_remote(source, server)
    worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    intent = ctx.store.intents_for_event(target['id'])[-1]
    assert json.loads(intent['payload_json'])['source_connection']['account_id'] == source['account_id']
    chosen = json.loads(tools.cal_update(ctx, id=target['id'], link_action=action, confirm=True))
    assert chosen['status'] == 'ok'
    relationship = {key: ctx.store.get_event(target['id'])[key] for key in ('is_primary', 'link_group_id', 'link_state')}
    count, _, _ = disconnect(ctx, source['calendar_id'])
    assert count == 1 and ctx.store.get_intent(intent['id'])['state'] == 'resolved'
    restored = ctx.store.get_event(target['id'])
    assert restored['title'] == 'Original'
    assert {key: restored[key] for key in relationship} == relationship
