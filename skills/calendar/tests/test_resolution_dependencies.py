"""Additional parent-run consumers for exact Sol source predictions; no live APIs."""
import sys, json
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_calendar_atomic_replay import reopen
from test_calendar_provider_divergence import caldav_context,google_context,_day,run_open_intents,sync
from test_conflict_resolution import create,remote_edit,inspection,resolve
from model import iso_utc
import tools, ops

@pytest.mark.parametrize('factory',[caldav_context,google_context])
@pytest.mark.parametrize('choice',['local','provider'])
@pytest.mark.parametrize('fail_tail',[False,True])
def test_later_segment_edit_has_explicit_resolution_fate(factory,choice,fail_tail):
    ctx,cid,server=factory();eid=create(ctx,cid,rrule='FREQ=DAILY;COUNT=10')
    server.down=True
    split=json.loads(tools.cal_update(ctx,id=f'{eid}@{iso_utc(_day(4))}',scope='following',title='Split',confirm=True))
    nid=next(a['event_id'] for a in split['assignments'] if a['event_id']!=eid)
    birth=ctx.store.intents_for_event(nid)[0]
    later=json.loads(tools.cal_update(ctx,id=nid,scope='all',title='Later accepted',confirm=True))
    assert later['status']=='pending'
    edit=ctx.store.intents_for_event(nid)[-1]
    server.down=False;remote_edit(ctx,server,eid)
    head=ctx.store.intents_for_event(eid)[-1]
    assert ops.execute_intent(ctx.store,ctx.providers,head,'test')['status']=='conflict'
    if fail_tail:
        for i in (birth,edit):
            assert ops.execute_intent(ctx.store,ctx.providers,ctx.store.get_intent(i['id']),'test')['status']=='failed'
    reopen(ctx);info=inspection(ctx,eid)
    assert edit['id'] in {i['id'] for i in info['operations']},info
    assert resolve(ctx,eid,info,choice)['status']==('pending' if choice=='local' else 'resolved')
    reopen(ctx);run_open_intents(ctx,rounds=6)
    assert not ctx.store.open_intents()
    row=ctx.store.get_event(nid)
    if choice=='local':
        remote=ctx.providers.adapter_for('').read_current(ctx.store.get_calendar(cid),row)
        assert remote['event']['title']=='Later accepted'
        assert row['sync_state']=='synced'
    else:
        assert row['deleted_at']
        assert len(server.resources if hasattr(server,'resources') else server.live())==1
    assert ctx.store.get_intent(edit['id'])['state']!='failed'

@pytest.mark.parametrize('choice',['local','provider'])
def test_partial_split_preserves_earlier_exception_command_without_old_topology(choice):
    ctx,cid,server=caldav_context();eid=create(ctx,cid,rrule='FREQ=DAILY;COUNT=10')
    for day in (5,6):
        assert json.loads(tools.cal_update(ctx,id=f'{eid}@{iso_utc(_day(day))}',title=f'Exception {day}',confirm=True))['status']=='updated'
    e1,e2=ctx.store.exceptions_for(eid)
    server.down=True
    assert json.loads(tools.cal_update(ctx,id=e2['id'],title='Earlier independent',confirm=True))['status']=='pending'
    independent=ctx.store.intents_for_event(e2['id'])[-1]['id']
    server.down=False
    adapter=ctx.providers.adapter_for('');update=adapter.update;fired=[]
    def fail_one(calendar,event,expected,payload):
        if event['id']==e1['id'] and event.get('master_id')!=eid:
            fired.append(True);raise ops.ProviderError('conflict','test independent writer')
        return update(calendar,event,expected,payload)
    with patch.object(adapter,'update',fail_one):
        result=json.loads(tools.cal_update(ctx,id=f'{eid}@{iso_utc(_day(4))}',scope='following',title='New segment',confirm=True))
    # A repair may deliberately wait for the old-resource independent intent
    # before splitting; that must be visible pending, not a fake completed head.
    if result['status']=='pending' and not fired:
        run_open_intents(ctx,rounds=4)
        assert ctx.store.get_intent(independent)['state']=='done'
        assert not ctx.store.open_intents()
        return
    assert fired
    reopen(ctx);info=inspection(ctx,e1['id'])
    assert resolve(ctx,e1['id'],info,choice)['status']==('pending' if choice=='local' else 'resolved')
    reopen(ctx);run_open_intents(ctx,rounds=4)
    kept=ctx.store.get_event(e2['id'])
    master,key=ops.resolve_occurrence(ctx.store,kept,'')
    assert master['id']!=eid
    assert ctx.store.get_intent(independent)['state']=='done'
    assert kept['title']=='Earlier independent'

def test_lease_clock_tick_does_not_expire_semantic_resolution():
    ctx,cid,server=caldav_context();eid=create(ctx,cid,rrule='FREQ=DAILY;COUNT=10')
    server.down=True
    out=json.loads(tools.cal_update(ctx,id=f'{eid}@{iso_utc(_day(3))}',title='Earlier',confirm=True))
    assert out['status']=='pending'
    independent=ctx.store.open_intents()[0]
    server.down=False;remote_edit(ctx,server,eid)
    out=json.loads(tools.cal_update(ctx,id=eid,scope='all',title='Master change',confirm=True))
    assert out['status']=='conflict'
    first=inspection(ctx,eid)
    leased=ctx.store.lease_intent(independent['id'],'clock-only');assert leased
    ctx.store.park_intent(independent['id'],60,'waiting')
    second=inspection(ctx,eid)
    assert first['resolution_token']==second['resolution_token']

def test_old_resource_conflict_discloses_its_waiting_structural_dependents():
    ctx,cid,server=caldav_context();eid=create(ctx,cid,rrule='FREQ=DAILY;COUNT=10')
    address=f'{eid}@{iso_utc(_day(3))}'
    assert json.loads(tools.cal_update(ctx,id=address,title='Published',confirm=True))['status']=='updated'
    exc=ctx.store.exceptions_for(eid)[0]
    server.down=True
    assert json.loads(tools.cal_update(ctx,id=exc['id'],title='Independent pending',confirm=True))['status']=='pending'
    independent=ctx.store.intents_for_event(exc['id'])[-1]
    server.down=False;remote_edit(ctx,server,eid)
    answer=json.loads(tools.cal_update(ctx,id=f'{eid}@{iso_utc(_day(4))}',scope='following',title='Split',confirm=True))
    assert answer['status']=='pending'
    assert ops.execute_intent(ctx.store,ctx.providers,independent,'test')['status']=='conflict'
    info=inspection(ctx,eid)
    assert independent['id'] in {i['id'] for i in info['operations']}
    assert len(info['operations'])>=3
    assert resolve(ctx,eid,info,'provider')['status']=='resolved'
    reopen(ctx);run_open_intents(ctx,rounds=3)
    assert not ctx.store.open_intents()
    assert len(server.resources)==1
