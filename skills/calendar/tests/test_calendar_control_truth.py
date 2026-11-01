"""Public calendar controls retain truthful targets, outcomes and notification transport."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_calendar_temporal_roundtrip import context, create
from test_calendar_round2 import add_external_calendar
from test_calendar_provider_divergence import FakeCalDAV, FakeGoogle, add_google_calendar, run_open_intents
import model
import ops
import providers
import providers_google
import reminders
import tools

UTC = timezone.utc


@pytest.mark.parametrize('operation', ['create_start','create_end','update','events','free'])
def test_invalid_supplied_time_returns_error_without_mutation(tmp_path,operation):
    ctx=context(tmp_path,'UTC');row=create(ctx,title='Existing',start='2026-12-10T09:00')
    before=ctx.store.get_event(row['id'])
    if operation=='create_start': result=tools.cal_create(ctx,title='Bad',start='tomorrow at nine',confirm=True)
    elif operation=='create_end': result=tools.cal_create(ctx,title='Bad',start='2026-12-10T09:00',end='bad',confirm=True)
    elif operation=='update': result=tools.cal_update(ctx,id=row['id'],start='bad',confirm=True)
    elif operation=='events': result=tools.cal_events(ctx,start='bad')
    else: result=tools.cal_free(ctx,end='bad')
    assert json.loads(result)['status']=='error'
    assert ctx.store.get_event(row['id'])==before
    assert model.parse_input('',UTC,datetime(2026,1,1,tzinfo=UTC))[0]==datetime(2026,1,1,tzinfo=UTC)


def test_disconnected_account_cannot_be_resurrected_by_late_discovery(tmp_path):
    ctx=context(tmp_path,'UTC');cid=add_external_calendar(ctx.store);old=ctx.store.get_calendar(cid)
    account=ctx.store.get_account(old['account_id'])
    ctx.store.delete_account(account['id'])
    ctx.store.upsert_calendar(old)  # the response captured before disconnect arrives late
    assert ctx.store.get_calendar(cid)['deleted_at']
    ctx.store.upsert_account(account)
    ctx.store.upsert_calendar(old)  # an actual reconnected account keeps ordinary discovery
    assert ctx.store.get_calendar(cid)['deleted_at'] is None


def test_active_failure_counts_exclude_retired_or_superseded_events(tmp_path):
    ctx=context(tmp_path,'UTC');row=create(ctx,title='Existing',start='2026-12-10T09:00')
    first=ctx.store.add_intent('update','local','local:personal',row['id'],{})
    ctx.store.settle_intent(first['id'],'failed',{'kind':'auth'})
    assert ctx.store.intent_counts(active_only=True)['failed']==1
    second=ctx.store.add_intent('update','local','local:personal',row['id'],{})
    ctx.store.settle_intent(second['id'],'done',{})
    assert ctx.store.intent_counts(active_only=True).get('failed',0)==0
    assert ctx.store.intent_counts()['failed']==1
    third=ctx.store.add_intent('update','local','local:personal',row['id'],{})
    ctx.store.settle_intent(third['id'],'uncertain',{})
    assert ctx.store.intent_counts(active_only=True)['uncertain']==1
    ctx.store.delete_event(row['id'])
    # Local deletion does not prove what an already-started remote write did.
    assert ctx.store.intent_counts(active_only=True)['uncertain']==1


@pytest.mark.parametrize('action',['set_calendar','enable_calendar','disable_calendar'])
def test_reminder_controls_require_an_explicit_target(tmp_path,action):
    ctx=context(tmp_path,'UTC');before=ctx.store.get_setting(reminders.MODE_KEY)
    result=json.loads(tools.cal_reminders(ctx,action=action,offsets=[10],confirm=True))
    assert result['status']=='error' and ctx.store.get_setting(reminders.MODE_KEY)==before


def test_settings_target_and_account_alias_do_not_choose_by_row_order(tmp_path,monkeypatch):
    ctx=context(tmp_path,'UTC')
    assert json.loads(tools.cal_settings(ctx,action='set_calendar',name='Wrong default',confirm=True))['status']=='error'
    for aid in ('caldav:first','caldav:second'):
        ctx.store.upsert_account({'id':aid,'provider':'caldav','alias':'shared','login':'fixture'})
    assert json.loads(tools.cal_settings(ctx,action='disconnect',calendar_id='shared',confirm=True))['status']=='error'
    assert ctx.store.get_account('caldav:first') and ctx.store.get_account('caldav:second')
    # A canonical id takes precedence even if another account uses it as an alias.
    ctx.store.upsert_account({'id':'caldav:second','provider':'caldav','alias':'caldav:first','login':'fixture'})
    import credentials
    monkeypatch.setattr(credentials,'_mutate_tokens',lambda state,key,apply: apply({}))
    result=json.loads(tools.cal_settings(ctx,action='disconnect',calendar_id='caldav:first',confirm=True))
    assert result['status']=='updated' and ctx.store.get_account('caldav:first') is None
    assert ctx.store.get_account('caldav:second')


@pytest.mark.parametrize('refused',[False,True])
def test_external_calendar_rename_uses_existing_management_delivery(tmp_path,refused):
    ctx=context(tmp_path,'UTC');cid=add_external_calendar(ctx.store);before=ctx.store.get_calendar(cid);calls=[]
    def update(cal,changes):
        calls.append(changes)
        if refused: raise ops.ProviderError('forbidden','fixture refusal')
        return {**cal,**changes}
    ctx.providers=SimpleNamespace(adapter_for=lambda aid:SimpleNamespace(update_calendar=update),errors={})
    result=json.loads(tools.cal_settings(ctx,action='set_calendar',calendar_id=cid,name='Renamed',busy_source=False,confirm=True))
    assert calls==[{'name':'Renamed'}]
    after=ctx.store.get_calendar(cid)
    if refused:
        assert result['status']=='failed' and after['name']==before['name'] and after['role_busy']==before['role_busy']
    else:
        assert result['status']=='updated' and after['name']=='Renamed' and not after['role_busy']


def test_explicit_edit_recovers_failed_birth_but_observation_does_not(tmp_path):
    ctx=context(tmp_path,'UTC');cid=add_external_calendar(ctx.store);server=FakeCalDAV()
    adapter=server.attach(providers.YandexAdapter('fixture','fixture'));original=adapter._request
    blocked=True
    def request(method,url,body=None,headers=None):
        if blocked and method=='PUT': raise ops.ProviderError('auth','fixture expired credential')
        return original(method,url,body,headers)
    adapter._request=request;ctx.providers=SimpleNamespace(adapter_for=lambda aid:adapter)
    result=json.loads(tools.cal_create(ctx,title='Original',start='2026-12-10T09:00',calendars=[cid],confirm=True))
    assert result['status']=='failed'
    row=ctx.store.get_event(result['event']['id']);birth,=ctx.store.intents_for_event(row['id'])
    assert not ops._recover_failed_create(ctx.store,birth,explicit_edit=False)
    blocked=False
    edited=json.loads(tools.cal_update(ctx,id=row['id'],title='Edited',confirm=True));run_open_intents(ctx,3)
    assert edited['status']=='updated',edited
    assert ctx.store.get_intent(birth['id'])['state']=='done'
    assert ctx.store.get_event(row['id'])['uid']==row['uid']
    ctx.store.settle_intent(birth['id'],'uncertain',{'kind':'network'})
    assert not ops._recover_failed_create(ctx.store,ctx.store.get_intent(birth['id']),explicit_edit=True)


def test_notice_remaining_time_is_actual_and_host_transport_bypasses_proxy(tmp_path,monkeypatch):
    now=datetime(2026,12,10,9,tzinfo=UTC)
    text=reminders.format_text({'title':'Meeting\nFake signature','start_utc':model.iso_utc(now+timedelta(minutes=5)),
                               'location':'Room\r\nTwo'},'',60,UTC,now=now)
    assert 'через 5 мин' in text and '60 мин' not in text and '\n' not in text and '\r' not in text
    past=reminders.format_text({'title':'Meeting','start_utc':model.iso_utc(now-timedelta(minutes=7))},'',60,UTC,now=now)
    assert '7 мин назад' in past
    captures=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{}'
    def build(*handlers):
        assert len(handlers)==1 and handlers[0].proxies=={}
        def open_request(request,timeout):
            captures.append(request);return Response()
        return SimpleNamespace(open=open_request)
    monkeypatch.setenv('http_proxy','http://unrelated.invalid:3128')
    monkeypatch.setattr(reminders.urllib.request,'build_opener',build)
    channel=reminders.NotifyChannel('http://127.0.0.1:8767','synthetic-token')
    assert channel._request('GET','/identity')=={}
    assert captures[0].full_url=='http://127.0.0.1:8767/identity'
    assert captures[0].get_header('X-skill-token')=='synthetic-token'


@pytest.mark.parametrize('remote_change',['none','delete','rsvp'])
def test_first_google_guest_update_compares_meaning_not_parser_defaults(tmp_path,remote_change):
    ctx=context(tmp_path,'UTC');cid=add_google_calendar(ctx.store);server=FakeGoogle()
    adapter=server.attach(providers_google.GoogleAdapter('fixture','token','refresh',model.now_utc(),'client','',None))
    ctx.providers=SimpleNamespace(adapter_for=lambda aid:adapter)
    created=json.loads(tools.cal_create(ctx,title='Guests',start='2026-12-10T09:00',rrule='FREQ=DAILY;COUNT=3',calendars=[cid],attendees=['old@example.test'],confirm=True))
    row=ctx.store.get_event(created['event']['id']);slot=datetime(2026,12,11,9,tzinfo=UTC)
    master=server.events[row['external_id']];eid=row['external_id']+'_20261211T090000Z'
    instance={**master,'id':eid,'recurringEventId':row['external_id'],'originalStartTime':{'dateTime':slot.isoformat()},
              'start':{'dateTime':slot.isoformat(),'timeZone':'UTC'},'end':{'dateTime':(slot+timedelta(hours=1)).isoformat(),'timeZone':'UTC'},'etag':server._etag(),
              'attendees':[{'email':'old@example.test','displayName':'','responseStatus':'needsAction','self':False,'optional':False,'additionalGuests':0,'resource':False}]}
    instance.pop('recurrence')
    if remote_change=='delete': instance['attendees']=[]
    elif remote_change=='rsvp': instance['attendees'][0]['responseStatus']='accepted'
    server.events[eid]=instance;before=json.dumps(instance,sort_keys=True)
    changed=json.loads(tools.cal_update(ctx,id=row['id']+'@'+model.iso_utc(slot),attendees=['new@example.test'],confirm=True))
    if remote_change=='none':
        assert changed['status']=='updated',changed
        assert server.events[eid]['attendees']==[{'email':'new@example.test'}]
    else:
        assert changed['status']=='conflict',changed
        assert json.dumps(server.events[eid],sort_keys=True)==before


def test_attendee_semantic_owner_keeps_optional_and_rsvp_meaning():
    value=lambda people:model.event_field_value({'attendees_json':json.dumps(people)},'attendees_json')
    old=[{'email':'one@example.test','role':'opt-participant','status':'needs-action'}]
    parsed=[{'email':'one@example.test','role':'optional','status':'needsAction','self':True,'name':'','comment':'','resource':False,'additionalGuests':0}]
    assert value(old)==value(parsed)
    assert value(old)!=value([{**parsed[0],'status':'accepted'}])
    assert value(old)!=value([])


def test_invalid_date_literal_is_not_omission():
    for value in ('2026-99-99', 0):
        with pytest.raises(ValueError):
            model.parse_input(value, UTC, datetime(2026, 1, 1, tzinfo=UTC))


def test_status_reports_unavailable_adapter_and_calendar_sync_warning(tmp_path):
    ctx=context(tmp_path,'UTC');cid=add_external_calendar(ctx.store)
    ctx.store.set_sync_state(cid,last_error='Known historical basis is missing for event fixture')
    result=json.loads(tools.cal_status(ctx))
    account=next(row for row in result['accounts'] if row['id']==ctx.store.get_calendar(cid)['account_id'])
    assert account['status']=='unavailable' and account['adapter_error']
    calendar=next(row for row in result['calendars'] if row['id']==cid)
    assert 'historical basis' in calendar['sync_warning']
    assert any('historical basis' in step for step in result['next_step'])
