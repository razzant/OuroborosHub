"""An admitted work reference survives unreadable observation, not canonical termination."""
import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
from lib.host_adapter import LoopbackPresenceHostAdapter,_ref
from lib.runtime import EmailRuntime
from lib.store import EmailStore


def reference(kind):
    if kind=='deferred':return _ref(kind,{'work_ref':'work-one','turn_ref':'turn-one','text':''})
    if kind=='retained':return _ref(kind,{'response':{'turn_ref':'work-one','disposition':'retained'},'work_refs':[]})
    return _ref(kind,{'continuation_ref':'work-one','turn_ref':'turn-one','continuation_version':2,'outcome':'deferred','text':'','work_refs':[]})


async def exercise(path,kind,blocked_code=None,terminal='completed',speech=True):
    store=EmailStore(path)
    store.ingest({'uid':1,'uidvalidity':1,'message_id':'<incoming>','sender':'reader@example.invalid','body':'Question'})
    item=store.claim_inbox();ref=reference(kind)
    store.set_host_reference(item.row_id,item.lease_token,ref)
    store.retry_inbox(item.row_id,item.lease_token,'',0)
    # Reopen the store so the test observes durable admitted custody.
    store=EmailStore(path);polls=[];submits=[]
    def handle(request):
        if request.url.path=='/identity':return httpx.Response(200,json={'places_version':1,'presence_delivery_version':1,'presence_continuation_versions':[2]})
        assert request.url.path=='/presence/work/work-one' and request.method=='GET'
        polls.append(request.url.path)
        if blocked_code and len(polls)==1:
            return httpx.Response(blocked_code,json={'ok':False,'code':'presence_auth_blocked' if blocked_code==403 else 'presence_work_not_found' if blocked_code==404 else 'presence_auth_unavailable',
                'disposition':'blocked' if blocked_code==403 else 'rejected' if blocked_code==404 else 'retry','error':'synthetic read refusal'})
        return httpx.Response(200,json={'status':terminal,'outcome':'message' if speech else 'silent',
            'text':'Owed canonical answer' if speech else '', 'output_ref':'selected-one','work_ref':'work-one',
            'continuation_ref':'work-one','continuation_version':2,'outputs':[]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        host=LoopbackPresenceHostAdapter('',host_service_url='http://127.0.0.1:8767',skill_token='fixture',http_client=http)
        host.capture_binding=lambda *args,**kwargs:{'supported':False}
        async def no_regeneration(*args,**kwargs):
            submits.append(True)
            raise AssertionError('Already admitted work must not be resubmitted')
        host.submit=no_regeneration
        runtime=EmailRuntime(store,SimpleNamespace(settings={'EMAIL_USER':'owner@example.invalid'}),host,account='mailbox')
        assert await runtime.process_inbox('observe')
        with store._connect() as db:
            first=dict(db.execute('SELECT state,host_reference,last_error FROM inbox').fetchone())
            db.execute('UPDATE inbox SET available_at=0')
        second=await runtime.process_inbox('observe') if blocked_code else False
        third=await runtime.process_inbox('observe')
        with store._connect() as db:
            final=dict(db.execute('SELECT state,host_reference,last_error FROM inbox').fetchone())
            outputs=[dict(row) for row in db.execute('SELECT request_id,body,in_reply_to FROM outbox')]
    return {'kind':kind,'http':blocked_code,'terminal':terminal,'speech':speech,'first':first,'second_claim':second,
        'third_claim':third,'final':final,'polls':len(polls),'submits':len(submits),'outputs':outputs,'reference':ref}


@pytest.mark.parametrize('kind',['deferred','retained','continuing'])
@pytest.mark.parametrize('code',[403,404,500])
def test_blocked_work_read_recovers_without_losing_or_regenerating_reply(tmp_path,kind,code):
    result=asyncio.run(exercise(tmp_path,kind,code))
    print('CUSTODY_READ',json.dumps(result))
    assert result['submits']==0 and result['first']['host_reference']==result['final']['host_reference']==result['reference']
    assert result['first']['state']=='retry'
    assert result['second_claim'] and not result['third_claim'] and result['polls']==2
    assert result['final']['state']=='completed'
    assert len(result['outputs'])==1 and result['outputs'][0]['body']=='Owed canonical answer'
    assert result['outputs'][0]['in_reply_to']=='<incoming>'


@pytest.mark.parametrize('terminal,speech',[('completed',True),('failed',True),('failed',False),('cancelled',False)])
def test_canonical_deferred_terminal_state_settles_without_an_extra_poll(tmp_path,terminal,speech):
    result=asyncio.run(exercise(tmp_path,'deferred',terminal=terminal,speech=speech))
    print('CUSTODY_TERMINAL',json.dumps(result))
    assert result['polls']==1 and result['submits']==0 and not result['third_claim']
    assert result['final']['state']==('completed' if terminal=='completed' else 'failed')
    assert len(result['outputs'])==int(speech)
    if speech:assert result['outputs'][0]['body']=='Owed canonical answer'
    assert result['final']['host_reference']==result['reference']
