"""Recurrence edits preserve requested meaning and native event status."""
import sys,json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone
from itertools import islice
import pytest
SKILL=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(SKILL),str(SKILL/'tests')]
import tools,ops,model,providers,providers_google
from test_calendar_provider_divergence import FakeCalDAV,FakeGoogle,add_google_calendar

def context(tmp_path):
    ctx=tools.Context(SimpleNamespace(get_state_dir=lambda:str(tmp_path),get_settings=lambda keys:{}))
    ctx.tz=model.get_tz('UTC')
    return ctx

def create(ctx,rule):
    result=json.loads(tools.cal_create(ctx,title='Series',start='2026-12-14T09:00:00+00:00',end='2026-12-14T10:00:00+00:00',rrule=rule,confirm=True))
    assert result['status']=='created',result
    return result['event']['id']

def listed(ctx,start='2026-12-14',end='2027-01-20'):
    return json.loads(tools.cal_events(ctx,start=start,end=end))['events']

@pytest.mark.parametrize('interval',[0,1])
def test_interval_validation_and_bounded_progress(tmp_path,monkeypatch,interval):
    from dateutil.rrule import rrule,rrulestr
    text=f'FREQ=DAILY;INTERVAL={interval}'
    start='2026-12-14T09:00:00+00:00'
    try: validation=ops.validate_rrule(text,start,'UTC')
    except ValueError: validation='REJECTED'
    # A finite prefix starting at DTSTART witnesses zero progress without asking xafter to search forever.
    emitted=[d.isoformat() for d in islice(rrulestr(text,dtstart=datetime.fromisoformat(start)),6)]
    original_iter=rrule._iter;stops=[]
    class ReviewSafetyStop(RuntimeError):pass
    def bounded_iter(self):
        for index,value in enumerate(original_iter(self)):
            if index>=8:
                stops.append({'interval':self._interval,'value':value.isoformat()})
                raise ReviewSafetyStop('review instrumentation stopped underlying iterator after eight emissions')
            yield value
    monkeypatch.setattr(rrule,'_iter',bounded_iter)
    ctx=context(tmp_path)
    result=json.loads(tools.cal_create(ctx,title='Interval probe',start=start,rrule=text,confirm=True))
    failed={}
    later=ctx.occurrences(datetime(2026,12,15,tzinfo=timezone.utc),datetime(2026,12,16,tzinfo=timezone.utc),strict=True,failed=failed)
    print('INTERVAL',json.dumps({'value':interval,'validation':validation,'prefix':emitted,'public_status':result['status'],'stored_rule':ctx.store.get_event(result['event']['id'])['rrule'] if result.get('event') else None,'later_count':len(later),'failures':failed,'safety_stops':stops}))
    if interval==0:
        assert validation=='REJECTED','INTERVAL=0 was accepted despite an unchanging recurrence iterator'
        assert result['status']=='error' and ctx.store.counts()['events']==0
    else:
        assert len(set(emitted))==6 and len(later)==1 and not failed and not stops

@pytest.mark.parametrize('shape',['following_clear','following_omitted','all_clear'])
def test_explicit_empty_rule_is_distinct_from_omission(tmp_path,shape):
    ctx=context(tmp_path);eid=create(ctx,'FREQ=DAILY;COUNT=5')
    args={'id':eid+'@2026-12-16T09:00:00+00:00','scope':'following','title':'Adjusted'}
    if shape=='all_clear':args.update(id=eid,scope='all')
    if shape!='following_omitted':args['rrule']=''
    result=json.loads(tools.cal_update(ctx,**args,confirm=True));rows=listed(ctx)
    print('CLEAR',json.dumps({'shape':shape,'status':result['status'],'rows':[(r['start'],r.get('rrule')) for r in rows]}))
    assert result['status']=='updated'
    assert len(rows)==({'following_clear':3,'following_omitted':5,'all_clear':1}[shape])
    if shape=='following_clear':assert not rows[-1].get('rrule')

@pytest.mark.parametrize('selector',['hour','day'])
@pytest.mark.parametrize('rule_shape',['explicit_selector','implicit_selector','explicit_replacement'])
def test_scope_all_start_move_preserves_requested_schedule(tmp_path,selector,rule_shape):
    ctx=context(tmp_path)
    base='FREQ=DAILY;COUNT=4' if selector=='hour' else 'FREQ=WEEKLY;COUNT=4'
    suffix=';BYHOUR=9' if selector=='hour' else ';BYDAY=MO'
    eid=create(ctx,base+(suffix if rule_shape!='implicit_selector' else ''))
    args={'id':eid,'scope':'all','start':'2026-12-14T10:00:00+00:00' if selector=='hour' else '2026-12-15T09:00:00+00:00'}
    if rule_shape=='explicit_replacement':args['rrule']=base+(';BYHOUR=10' if selector=='hour' else ';BYDAY=TU')
    result=json.loads(tools.cal_update(ctx,**args,confirm=True));rows=listed(ctx)
    print('SELECTOR',json.dumps({'selector':selector,'shape':rule_shape,'status':result['status'],'master':ctx.store.get_event(eid)['start_utc'],'rule':ctx.store.get_event(eid)['rrule'],'listed':[r['start'] for r in rows]}))
    assert result['status']=='updated' and len(rows)==4
    expected=datetime.fromisoformat(args['start'])
    assert model.parse_stored(rows[0]['start'])==expected,'The requested new master start is not the first actual occurrence'
    assert all(model.parse_stored(r['start']).hour==10 for r in rows) if selector=='hour' else all(model.parse_stored(r['start']).weekday()==1 for r in rows)

@pytest.mark.parametrize('provider',['caldav','google'])
@pytest.mark.parametrize('status,operation',[('tentative','title'),('tentative','legacy_title'),('confirmed','title'),('cancelled','title'),('tentative','rsvp'),('confirmed','rsvp')])
def test_unrelated_native_edit_preserves_event_status(tmp_path,provider,status,operation):
    ctx=context(tmp_path)
    if provider=='caldav':
        aid,cid='caldav:fixture','caldav:fixture:work';href='https://dav.example.test/work/native.ics'
        ctx.store.upsert_account({'id':aid,'provider':'caldav','login':'fixture'})
        ctx.store.upsert_calendar({'id':cid,'account_id':aid,'provider':'caldav','name':'Work','href':href.rsplit('/',1)[0]+'/'})
        wire='\r\n'.join(['BEGIN:VCALENDAR','VERSION:2.0','BEGIN:VEVENT','UID:native-status','DTSTART:20261214T090000Z','DTEND:20261214T100000Z','STATUS:'+status.upper(),'ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:fixture@example.test','SUMMARY:Before','END:VEVENT','END:VCALENDAR',''])
        server=FakeCalDAV();server.resources[href]=(wire,'v1')
        adapter=server.attach(providers.CalDAVAdapter('fixture@example.test','synthetic','https://dav.example.test'))
        incoming=providers.ics_to_rows(wire,cid,href,'v1',ctx.tz)[0]
    else:
        cid=add_google_calendar(ctx.store)
        wire={'id':'native-status','iCalUID':'native-status','etag':'v1','summary':'Before','start':{'dateTime':'2026-12-14T09:00:00Z'},'end':{'dateTime':'2026-12-14T10:00:00Z'},'status':status,'attendees':[{'email':'fixture@example.test','self':True,'responseStatus':'needsAction'}]}
        server=FakeGoogle();server.events[wire['id']]=dict(wire)
        adapter=server.attach(providers_google.GoogleAdapter('fixture@example.test','synthetic','',model.now_utc(),'client','',None))
        incoming=providers_google.gevent_to_row(wire,cid,ctx.tz)
    ctx.providers=SimpleNamespace(errors={},adapter_for=lambda _:adapter)
    if operation=='legacy_title':incoming['status']='confirmed'
    event=ctx.store.insert_event(incoming)
    result=json.loads(tools.cal_update(ctx,id=event['id'],confirm=True,**({'title':'After'} if operation.endswith('title') else {'response':'accepted'})))
    if provider=='caldav':
        native=providers._ical().Calendar.from_ical(server.resources[href][0]).walk('VEVENT')[0];after=str(native['STATUS']).lower();title=str(native['SUMMARY'])
    else:after=server.events[wire['id']]['status'];title=server.events[wire['id']]['summary']
    print('NATIVE_STATUS',json.dumps({'provider':provider,'before':status,'parsed':incoming['status'],'result':result['status'],'after':after,'title':title}))
    assert result['status']=='updated' and title==('After' if operation.endswith('title') else 'Before')
    assert after==status,'An unrelated edit changes the provider event status'
    if operation=='legacy_title':
        from scripts.worker import reconcile_calendar
        calendar=ctx.store.get_calendar(cid)
        reconcile_calendar(ctx.store,ctx.providers,adapter,calendar,ctx.tz)
        assert ctx.store.get_event(event['id'])['status']==status
    if operation=='rsvp':
        response=str(native['ATTENDEE'].params['PARTSTAT']).lower() if provider=='caldav' else server.events[wire['id']]['attendees'][0]['responseStatus']
        assert response=='accepted'


@pytest.mark.parametrize('interval',[0,-1])
def test_nonpositive_interval_rejected_before_authoring_and_imported_iteration(tmp_path,monkeypatch,interval):
    from dateutil.rrule import rrule
    import reminders
    ctx=context(tmp_path)
    good=create(ctx,'')
    original=ctx.store.get_event(good)
    invalid=f'FREQ=DAILY;INTERVAL={interval}'
    made=json.loads(tools.cal_create(ctx,title='Invalid',start='2026-12-14T09:00Z',rrule=invalid,confirm=True))
    assert made['status']=='error' and ctx.store.counts()['events']==1
    changed=json.loads(tools.cal_update(ctx,id=good,rrule=invalid,confirm=True))
    assert changed['status']=='error' and ctx.store.get_event(good)==original
    bad=ctx.store.insert_event({'calendar_id':'local:personal','title':'Imported invalid','start_utc':'2026-12-14T09:00:00+00:00',
        'end_utc':'2026-12-14T10:00:00+00:00','tz':'UTC','rrule':invalid})
    iterator_calls=[];original_iter=rrule._iter
    def bounded(self):
        if self._interval<=0:
            iterator_calls.append(self._interval)
            raise AssertionError('An invalid imported interval reached the iterator')
        yield from original_iter(self)
    monkeypatch.setattr(rrule,'_iter',bounded)
    result=json.loads(tools.cal_events(ctx,start='2026-12-14',end='2026-12-15'))
    assert not result['events_complete'] and bad['id'] in result['unexpanded_series']
    assert [row['id'] for row in result['events']]==[good]
    held={};reminders.set_rules(ctx.store,default=[0])
    ctx.store.schedule_reminder(bad['id'],'2026-12-14T09:00:00+00:00',0,'2026-12-14T09:00:00+00:00','retained-invalid')
    reminders.plan(ctx.store,lambda start,end:ctx.occurrences(start,end,strict=True,failed=held),
        now=model.parse_stored('2026-12-13T12:00:00+00:00'),held=held)
    assert bad['id'] in held and iterator_calls==[]
    with ctx.store._conn() as conn:
        assert conn.execute('SELECT state FROM reminders WHERE notice_id=?',('retained-invalid',)).fetchone()[0]=='scheduled'


@pytest.mark.parametrize('rule,start,new_start,expected_hours,expected_days',[
    ('FREQ=DAILY;BYHOUR=9,17;COUNT=4','2026-12-14T09:00Z','2026-12-14T10:00Z',{10,18},None),
    ('FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4','2026-12-14T09:00Z','2026-12-15T09:00Z',{9},{1,3}),
    ('FREQ=WEEKLY;BYDAY=MO;BYHOUR=23;COUNT=3','2026-12-14T23:00Z','2026-12-15T01:00Z',{1},{1}),
    ('FREQ=MONTHLY;BYDAY=1MO;BYHOUR=9;COUNT=2','2026-12-07T09:00Z','2026-12-07T10:00Z',{10},{0}),
])
def test_selector_lists_keep_factored_wall_clock_meaning(tmp_path,rule,start,new_start,expected_hours,expected_days):
    ctx=context(tmp_path)
    eid=json.loads(tools.cal_create(ctx,title='Pattern',start=start,rrule=rule,confirm=True))['event']['id']
    moved=json.loads(tools.cal_update(ctx,id=eid,start=new_start,scope='all',confirm=True))
    assert moved['status']=='updated'
    rows=listed(ctx,start='2026-12-01',end='2027-01-20')
    assert model.parse_stored(rows[0]['start'])==model.parse_stored(new_start)
    assert {model.parse_stored(row['start']).hour for row in rows}==expected_hours
    if expected_days is not None:assert {model.parse_stored(row['start']).weekday() for row in rows}==expected_days


def route_update(ctx,body):
    import asyncio,routes
    handlers={}
    routes.register_routes(SimpleNamespace(register_route=lambda name,handler,**kwargs:handlers.update({name:handler})),lambda:ctx)
    class Request:
        async def json(self):return body
    response=asyncio.run(handlers['event/update'](Request()))
    return response.status_code,json.loads(response.body)


@pytest.mark.parametrize('surface',['tool','route'])
@pytest.mark.parametrize('shape',['complex_feedback','explicit_replacement','faithful_move'])
def test_series_argument_feedback_precedes_all_combined_effects(tmp_path,monkeypatch,surface,shape):
    ctx=context(tmp_path)
    complex_rule=shape!='faithful_move'
    start='2026-12-07T09:00Z' if complex_rule else '2026-12-14T09:00Z'
    rule='FREQ=MONTHLY;BYDAY=1MO;COUNT=3' if complex_rule else 'FREQ=WEEKLY;BYDAY=MO;COUNT=3'
    eid=json.loads(tools.cal_create(ctx,title='Series',start=start,rrule=rule,confirm=True))['event']['id']
    ctx.store.upsert_calendar({'id':'local:copy','account_id':'local','provider':'local','name':'Copy'})
    before=ctx.store.get_event(eid);counts=ctx.store.counts();planned=[]
    monkeypatch.setattr(tools,'_plan_reminders',lambda ctx:planned.append(True) or '')
    body={'id':eid,'scope':'all','start':'2026-12-08T09:00Z' if complex_rule else '2026-12-15T09:00Z',
          'calendars':['local:personal','local:copy']}
    if shape=='explicit_replacement':body['rrule']='FREQ=MONTHLY;BYDAY=2TU;COUNT=3'
    if surface=='tool':status,result=200,json.loads(tools.cal_update(ctx,**body,confirm=True))
    else:status,result=route_update(ctx,body)
    if shape=='complex_feedback':
        assert result['status']=='argument_required' and result['argument']=='rrule'
        assert 'explicit replacement' in result['message']
        assert status==(400 if surface=='route' else 200)
        assert ctx.store.get_event(eid)==before and ctx.store.counts()==counts and planned==[]
        assert ctx.store.open_intents()==[]
    else:
        assert result['status'] in ('ok','updated') and planned==[True]
        group=ctx.store.get_event(eid)['link_group_id']
        assert {row['calendar_id'] for row in ctx.store.group_masters(group)}=={'local:personal','local:copy'}
        assert all(model.parse_stored(row['start']).weekday()==1 for row in listed(ctx,start='2026-12-01',end='2027-01-20'))


@pytest.mark.parametrize('rule,start,new_start',[
    ('FREQ=DAILY;BYHOUR=9,10;BYMINUTE=0,30;COUNT=4','2026-12-14T09:00Z','2026-12-14T09:45Z'),
    ('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR;COUNT=4','2026-12-14T09:00Z','2026-12-17T09:00Z'),
])
def test_correlated_or_alternating_selectors_need_an_explicit_replacement(tmp_path,rule,start,new_start):
    ctx=context(tmp_path)
    eid=json.loads(tools.cal_create(ctx,title='Pattern',start=start,rrule=rule,confirm=True))['event']['id']
    before=ctx.store.get_event(eid)
    result=json.loads(tools.cal_update(ctx,id=eid,start=new_start,scope='all',confirm=True))
    assert result['status']=='argument_required' and result['argument']=='rrule'
    assert ctx.store.get_event(eid)==before


@pytest.mark.parametrize('provider',['caldav','google'])
@pytest.mark.parametrize('legacy_master',[False,True])
def test_tentative_series_first_override_and_copy_keep_event_status(tmp_path,provider,legacy_master):
    from test_effective_event_clock import mixed_context
    from test_calendar_provider_divergence import google_context,sync
    if provider=='caldav':
        ctx,master,_,server=mixed_context(tmp_path,'tzid','absolute')
        wire,etag=server.resources[master['href']]
        wire=wire.replace('SUMMARY:Master','STATUS:TENTATIVE\r\nSUMMARY:Master')
        server.resources[master['href']]=(wire,'tentative-v2')
        sync(ctx,master['calendar_id'])
        row=ctx.store.get_event(master['id']);slot='2026-12-12T08:00:00+00:00'
    else:
        ctx,cid,server=google_context()
        native={'id':'tentative-master','iCalUID':'series','etag':'v1','summary':'Master','status':'tentative',
            'start':{'dateTime':'2026-12-10T09:00:00Z','timeZone':'UTC'},
            'end':{'dateTime':'2026-12-10T10:00:00Z','timeZone':'UTC'},'recurrence':['RRULE:FREQ=DAILY;COUNT=3']}
        instance={**native,'id':'tentative-master_20261212T090000Z','recurringEventId':native['id'],'originalStartTime':{'dateTime':'2026-12-12T09:00:00Z'},
            'start':{'dateTime':'2026-12-12T09:00:00Z','timeZone':'UTC'},'end':{'dateTime':'2026-12-12T10:00:00Z','timeZone':'UTC'}}
        instance.pop('recurrence')
        server.events.update({native['id']:native,instance['id']:instance})
        sync(ctx,cid)
        row=ctx.store.find_by_external(cid,external_id=native['id']);slot='2026-12-12T09:00:00+00:00'
        # Exercise the first local override path against an addressable real instance.
        for child in ctx.store.exceptions_for(row['id']):ctx.store.delete_event(child['id'],hard=True)
    assert row['status']=='tentative'
    if legacy_master:ctx.store.update_event(row['id'],{'status':'confirmed'})
    result=json.loads(tools.cal_update(ctx,id=row['id']+'@'+slot,title='Only this title',confirm=True))
    assert result['status']=='updated',result
    child=next(e for e in ctx.store.exceptions_for(row['id']) if e['recurrence_id']==slot)
    if not legacy_master:assert child['status']=='tentative'
    if provider=='caldav':
        native_child=next(e for e in providers._ical().Calendar.from_ical(server.resources[row['href']][0]).walk('VEVENT') if str(e.get('SUMMARY'))=='Only this title')
        assert str(native_child['STATUS'])=='TENTATIVE'
    else:assert server.events['tentative-master_20261212T090000Z']['status']=='tentative'
    sync(ctx,row['calendar_id'])
    assert ctx.store.get_event(row['id'])['status']=='tentative'
    assert ctx.store.get_event(child['id'])['status']=='tentative'
    ctx.store.upsert_calendar({'id':'local:copy','account_id':'local','provider':'local','name':'Copy','publish_mode':'full'})
    copied=json.loads(tools.cal_update(ctx,id=row['id'],scope='all',calendars=[row['calendar_id'],'local:copy'],confirm=True))
    assert copied['status']=='updated'
    copy=ctx.store.get_event(copied['reassign']['added'][0]['event_id'])
    assert copy['status']=='tentative'
    selected=json.loads(tools.cal_update(ctx,id=copy['id'],link_action='source',scope='all',confirm=True))
    assert selected['status']=='ok'
    ctx.store.update_event(row['id'],{'status':'confirmed'})
    ctx.store.update_event(child['id'],{'status':'confirmed'})
    sync(ctx,row['calendar_id'])
    assert ctx.store.get_event(row['id'])['status']=='tentative'
    assert ctx.store.get_event(child['id'])['status']=='tentative'
    assert ctx.store.get_event(row['id'])['link_state']=='following'
    assert ctx.store.get_event(child['id'])['link_state']=='following'
    ctx.store.update_event(child['id'],{'status':'confirmed','sync_state':'pending'})
    pending=ctx.store.get_event(child['id'])
    sync(ctx,row['calendar_id'])
    assert ctx.store.get_event(child['id'])==pending


def test_positive_frequent_rule_is_valid_without_a_new_work_quota():
    rule='FREQ=SECONDLY;INTERVAL=1;COUNT=3'
    assert ops.validate_rrule(rule,'2026-12-14T09:00:00+00:00','UTC')==rule



def test_observed_source_move_does_not_infer_an_unrequested_rule_rewrite(tmp_path):
    ctx=context(tmp_path)
    eid=create(ctx,'FREQ=WEEKLY;BYDAY=MO;COUNT=3')
    with ctx.store.transaction() as view:
        result=ops._stage_update_event(view,eid,{'start_utc':'2026-12-15T09:00:00+00:00',
            'end_utc':'2026-12-15T10:00:00+00:00'},scope='all',propagate=False,from_source=True)
    assert result['status']=='ok'
    assert ctx.store.get_event(eid)['rrule']=='FREQ=WEEKLY;BYDAY=MO;COUNT=3'
    # The source owns an observed rule; it is not the owner's request to move a pattern.


@pytest.mark.parametrize('selector,cut,new_start,expected',[
    ('hour','2026-12-16T09:00:00+00:00','2026-12-16T10:00:00+00:00',
     ['2026-12-14T09:00:00+00:00','2026-12-15T09:00:00+00:00','2026-12-16T10:00:00+00:00','2026-12-17T10:00:00+00:00']),
    ('day','2026-12-28T09:00:00+00:00','2026-12-29T09:00:00+00:00',
     ['2026-12-14T09:00:00+00:00','2026-12-21T09:00:00+00:00','2026-12-29T09:00:00+00:00','2027-01-05T09:00:00+00:00']),
])
def test_following_move_translates_selectors_from_cut_with_remaining_count(tmp_path,selector,cut,new_start,expected):
    ctx=context(tmp_path)
    rule='FREQ=DAILY;BYHOUR=9;COUNT=4' if selector=='hour' else 'FREQ=WEEKLY;BYDAY=MO;COUNT=4'
    eid=create(ctx,rule)
    result=json.loads(tools.cal_update(ctx,id=eid+'@'+cut,scope='following',start=new_start,confirm=True))
    assert result['status']=='updated',result
    rows=listed(ctx)
    assert [model.iso_utc(model.parse_stored(row['start'])) for row in rows]==expected
    suffix=ctx.store.get_event(rows[-1]['id'].split('@')[0])
    assert 'COUNT=2' in suffix['rrule']


@pytest.mark.parametrize('bound',['COUNT=4','UNTIL=20261217T090000Z'])
def test_following_move_uses_original_cut_and_preserves_finite_bound(tmp_path,bound):
    ctx=context(tmp_path)
    eid=create(ctx,'FREQ=DAILY;BYHOUR=9;'+bound)
    cut='2026-12-16T09:00:00+00:00'
    changed=json.loads(tools.cal_update(ctx,id=eid+'@'+cut,start='2026-12-16T11:00Z',confirm=True))
    assert changed['status']=='updated'
    moved=json.loads(tools.cal_update(ctx,id=eid+'@'+cut,scope='following',start='2026-12-16T12:00Z',confirm=True))
    assert moved['status']=='updated',moved
    rows=listed(ctx)
    assert [model.iso_utc(model.parse_stored(row['start'])) for row in rows]==[
        '2026-12-14T09:00:00+00:00','2026-12-15T09:00:00+00:00',
        '2026-12-16T12:00:00+00:00','2026-12-17T10:00:00+00:00']
    suffix=ctx.store.get_event(rows[-1]['id'].split('@')[0])
    assert suffix['start_utc']=='2026-12-16T10:00:00+00:00'
    assert 'BYHOUR=10' in suffix['rrule']
    assert ('COUNT=2' if bound.startswith('COUNT') else 'UNTIL=20261217T100000Z') in suffix['rrule']


@pytest.mark.parametrize('surface',['tool','route'])
@pytest.mark.parametrize('shape',['complex_feedback','explicit_replacement','faithful_move'])
def test_following_argument_feedback_precedes_split_reassignment_and_planning(tmp_path,monkeypatch,surface,shape):
    ctx=context(tmp_path)
    complex_rule=shape!='faithful_move'
    rule='FREQ=MONTHLY;BYDAY=1MO;COUNT=3' if complex_rule else 'FREQ=WEEKLY;BYDAY=MO;COUNT=3'
    start='2026-12-07T09:00Z' if complex_rule else '2026-12-14T09:00Z'
    cut='2027-01-04T09:00:00+00:00' if complex_rule else '2026-12-21T09:00:00+00:00'
    wanted='2027-01-05T09:00:00+00:00' if complex_rule else '2026-12-22T09:00:00+00:00'
    eid=json.loads(tools.cal_create(ctx,title='Series',start=start,rrule=rule,confirm=True))['event']['id']
    ctx.store.upsert_calendar({'id':'local:copy','account_id':'local','provider':'local','name':'Copy'})
    before=ctx.store.get_event(eid);counts=ctx.store.counts();planned=[]
    monkeypatch.setattr(tools,'_plan_reminders',lambda ctx:planned.append(True) or '')
    body={'id':eid+'@'+cut,'scope':'following','start':wanted,'calendars':['local:personal','local:copy']}
    if shape=='explicit_replacement':body['rrule']='FREQ=MONTHLY;BYDAY=1TU;COUNT=2'
    if surface=='tool':status,result=200,json.loads(tools.cal_update(ctx,**body,confirm=True))
    else:status,result=route_update(ctx,body)
    if shape=='complex_feedback':
        assert result['status']=='argument_required' and result['argument']=='rrule'
        assert status==(400 if surface=='route' else 200)
        assert ctx.store.get_event(eid)==before and ctx.store.counts()==counts and planned==[]
        assert ctx.store.open_intents()==[]
    else:
        assert result['status'] in ('ok','updated') and planned==[True]
        rows=listed(ctx,start='2026-12-01',end='2027-01-20')
        assert len([row for row in rows if model.iso_utc(model.parse_stored(row['start']))==wanted])==2
        assert len([row for row in rows if model.parse_stored(row['start'])==model.parse_stored(start)])==1


@pytest.mark.parametrize('scope',['all','following'])
def test_imported_invalid_interval_move_requests_rule_before_mutation(tmp_path,monkeypatch,scope):
    ctx=context(tmp_path)
    row=ctx.store.insert_event({'calendar_id':'local:personal','title':'Imported','start_utc':'2026-12-14T09:00:00+00:00',
        'end_utc':'2026-12-14T10:00:00+00:00','tz':'UTC','rrule':'FREQ=DAILY;BYHOUR=9;INTERVAL=0'})
    if scope=='following':
        # A real retained override resolves even when its imported parent no longer expands.
        ctx.store.insert_event({**row,'id':model.new_id('evt'),'master_id':row['id'],'rrule':'',
            'recurrence_id':'2026-12-16T09:00:00+00:00','start_utc':'2026-12-16T09:00:00+00:00',
            'end_utc':'2026-12-16T10:00:00+00:00'})
    row=ctx.store.get_event(row['id'])
    from dateutil.rrule import rrule
    def forbidden(self):raise AssertionError('Invalid imported interval reached the iterator')
    monkeypatch.setattr(rrule,'_iter',forbidden)
    eid=row['id']+('@2026-12-16T09:00:00+00:00' if scope=='following' else '')
    result=json.loads(tools.cal_update(ctx,id=eid,scope=scope,start='2026-12-16T10:00Z',confirm=True))
    assert result['status']=='argument_required' and result['argument']=='rrule'
    assert ctx.store.get_event(row['id'])==row
    title=json.loads(tools.cal_update(ctx,id=row['id'],scope='all',title='Kept imported rule',confirm=True))
    assert title['status']=='updated'
    assert ctx.store.get_event(row['id'])['rrule']==row['rrule']
    assert not json.loads(tools.cal_events(ctx,start='2026-12-14',end='2026-12-18'))['events_complete']


def test_following_split_retains_tentative_status(tmp_path):
    ctx=context(tmp_path)
    eid=create(ctx,'FREQ=DAILY;COUNT=4')
    ctx.store.update_event(eid,{'status':'tentative'})
    result=json.loads(tools.cal_update(ctx,id=eid+'@2026-12-16T09:00:00+00:00',scope='following',title='Suffix',confirm=True))
    assert result['status']=='updated'
    rows=listed(ctx)
    assert len(rows)==4 and all(row['status']=='tentative' for row in rows)
