"""A divergent linked recurrence cannot stop the companion's other work."""
import json
from unittest.mock import patch
from test_calendar_provider_divergence import caldav_context, _day, run_open_intents
from test_conflict_resolution import create
from model import iso_utc
from scripts import worker
import ops


def test_divergent_copy_rolls_back_only_its_feed_commands():
    ctx, cid, server = caldav_context()
    eid = create(ctx, cid, rrule='FREQ=DAILY;COUNT=5')
    before = ctx.store.get_event(eid)
    basis = ctx.store.sync_basis(cid)
    with ctx.store.transaction() as view:
        result = worker._stage_sync_copy(view, basis, eid, [
            ('update', eid, {'title': 'Must not half commit'}, 'all'),
            ('update', f'{eid}@{iso_utc(_day(20))}', {'title': 'Outside'}, 'this')])
    assert result['status'] == 'protected', result
    assert ctx.store.get_event(eid)['title'] == before['title']
    assert not ctx.store.open_intents()
    assert 'outside' in ctx.store.get_sync_state(cid)['last_error']


def test_one_bad_account_does_not_starve_replay_or_reminder_planning(tmp_path):
    ctx, cid, server = caldav_context()
    server.down = True
    result = ops.create_event(ctx.store, ctx.providers, {'title':'Queued', 'start_utc':iso_utc(_day(2)),
        'end_utc':iso_utc(_day(2,10)), 'calendar_ids':[cid]})
    assert result['assignments'][0]['status'] == 'pending'
    server.down = False
    # Real pending command, made due for the next tick (no provider identity fabrication).
    for i in ctx.store.open_intents():
        ctx.store.settle_intent(i['id'], 'pending', {}, retry_in_sec=None)
    health=[]
    def stop_after_tick(path, value):
        health.append(value);worker._stop=True
    class Channel:
        def state(self,refresh=False):return 'ready'
    worker._stop=False
    with patch.dict(worker.os.environ, {'OUROBOROS_SKILL_STATE_DIR':str(tmp_path)}), \
         patch.object(worker, 'Store', return_value=ctx.store), \
         patch.object(worker, 'Providers', return_value=ctx.providers), \
         patch.object(worker.rem, 'NotifyChannel', return_value=Channel()), \
         patch.object(worker.rem, 'deliver_due', return_value={'sent':0}), \
         patch.object(worker, 'sync_account', side_effect=ValueError('divergent recurrence')), \
         patch.object(worker, 'write_health', side_effect=stop_after_tick), \
         patch.object(worker.signal, 'signal'):
        worker.main()
    assert health[0]['intents'] is not None, health
    assert health[0]['reminders'] is not None, health
    assert not ctx.store.open_intents()
    assert health[0]['sync'][0]['status']=='error'
    worker._stop=False
