"""Conflict choices preserve the resource topology of the entire command chain."""
import json
from test_calendar_atomic_replay import reopen
from test_calendar_provider_divergence import caldav_context, _day, run_open_intents
from test_conflict_resolution import create, remote_edit, inspection, resolve
from model import iso_utc
import tools
import ops


def test_local_choice_rebinds_relocated_override_to_new_resource():
    ctx, cid, server = caldav_context()
    eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10')
    assert json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(5))}',
        title='Keep override', confirm=True))['status'] == 'updated'
    exception = ctx.store.exceptions_for(eid)[0]
    remote_edit(ctx, server, eid)
    result = json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(4))}',
        scope='following', title='Split', confirm=True))
    assert result['status'] == 'conflict'
    assert resolve(ctx, eid, inspection(ctx, eid), 'local')['status'] == 'pending'
    reopen(ctx)
    run_open_intents(ctx, 6)
    assert not ctx.store.open_intents(), ctx.store.open_intents()
    moved = ctx.store.get_event(exception['id'])
    master = ctx.store.get_event(moved['master_id'])
    assert master['id'] != eid
    remote = ctx.providers.adapter_for('').read_current(ctx.store.get_calendar(cid),
        {**moved, 'master_href': master['href'], 'master_etag': master['etag']})
    assert remote['event']['title'] == 'Keep override'


def test_provider_choice_restores_earliest_preimage_across_dependent_commands():
    ctx, cid, server = caldav_context()
    eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10')
    for day in (3, 5):
        assert json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(day))}',
            title=f'Published {day}', confirm=True))['status'] == 'updated'
    first, later = ctx.store.exceptions_for(eid)
    server.down = True
    assert json.loads(tools.cal_update(ctx, id=first['id'], title='Offline', confirm=True))['status'] == 'pending'
    earlier = ctx.store.intents_for_event(first['id'])[-1]
    assert json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(4))}',
        scope='following', title='Split', confirm=True))['status'] == 'pending'
    server.down = False
    remote_edit(ctx, server, eid)
    assert ops.execute_intent(ctx.store, ctx.providers, earlier, 'test')['status'] == 'conflict'
    assert resolve(ctx, eid, inspection(ctx, eid), 'provider')['status'] == 'resolved'
    reopen(ctx)
    run_open_intents(ctx, 6)
    kept = ctx.store.get_event(later['id'])
    assert not kept['deleted_at'], kept
    assert kept['master_id'] == eid
    assert kept['title'] == 'Published 5'
    assert not ctx.store.open_intents()
    assert len(server.resources) == 1


def test_new_split_resource_keeps_pending_later_cancellation():
    from test_cancellation_replay import provider_slots, local_slots
    ctx, cid, server = caldav_context()
    eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=10')
    server.down = True
    assert json.loads(tools.cal_delete(ctx, id=f'{eid}@{iso_utc(_day(6))}', confirm=True))['status'] == 'pending'
    result = json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(4))}', scope='following', title='Split', confirm=True))
    nid = next(a['event_id'] for a in result['assignments'] if a['event_id'] != eid)
    server.down = False
    run_open_intents(ctx, 8)
    assert not ctx.store.open_intents(), ctx.store.open_intents()
    master = ctx.store.get_event(nid)
    assert provider_slots(ctx, cid, server, master['href']) == local_slots(ctx, nid)
    assert iso_utc(_day(6)) not in provider_slots(ctx, cid, server, master['href'])


def test_protected_google_deletion_retains_rescan_until_copy_settles():
    from test_cancellation_replay import linked_google_primary
    from scripts.worker import reconcile_calendar
    ctx, cid, left, server, google, adapter, caldav, eid, copy = linked_google_primary()
    server.down = True
    result = ops.update_event(ctx.store, ctx.providers, copy['id'], {'title': 'Accepted copy edit'}, propagate=False)
    assert result['assignments'][0]['status'] == 'pending'
    ext = ctx.store.get_event(eid)['external_id']
    google.events[ext].update(status='cancelled', etag=google._etag())
    observation = reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz, 'old-token')
    assert observation[3] == '', observation
    assert ctx.store.get_event(copy['id'])['title'] == 'Accepted copy edit'
    assert not ctx.store.get_event(eid)['deleted_at']
    server.down = False
    run_open_intents(ctx, 4)
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz, '')
    assert ctx.store.get_event(eid)['deleted_at']
    assert not ctx.store.open_intents()
