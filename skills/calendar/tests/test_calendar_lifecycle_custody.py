"""Calendar command cancellation and settled-notice identity retain real provider custody."""
import io
import json
import urllib.request
from email.message import Message
from datetime import timedelta
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

from test_calendar_temporal_roundtrip import context
from test_recurrence_until_semantics import native_context
from test_calendar_provider_divergence import caldav_context, _day, run_open_intents
from test_calendar_round2 import OneAdapterProviders, add_external_calendar
import accounts
import management
import model
import ops
import providers
import reminders
import routes
import tools
from scripts.worker import reconcile_calendar

def sync(ctx, cid):
    cal = ctx.store.get_calendar(cid)
    return reconcile_calendar(ctx.store, ctx.providers, ctx.providers.adapter_for(cal['account_id']), cal, ctx.tz)

@pytest.mark.parametrize('already_waiting', [False, 'scheduled', 'no_channel'])
@pytest.mark.parametrize('settled_state', ['sent', 'unknown'])
def test_date_readdress_preserves_settled_notice_over_existing_waiting_row(tmp_path, already_waiting, settled_state):
    ctx, initial, server = native_context(tmp_path, 'date')
    ctx.tz = model.get_tz('Europe/Moscow')
    raw = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'UID:legacy-date',
        'DTSTART;VALUE=DATE:20261210', 'DTEND;VALUE=DATE:20261211', 'RRULE:FREQ=DAILY;COUNT=3',
        'SUMMARY:Daily', 'END:VEVENT', 'BEGIN:VEVENT', 'UID:legacy-date',
        'RECURRENCE-ID;VALUE=DATE:20261211', 'DTSTART;VALUE=DATE:20261211', 'DTEND;VALUE=DATE:20261212',
        'SUMMARY:Exception', 'END:VEVENT', 'END:VCALENDAR', ''])
    ctx.store.delete_event(initial['id'], hard=True)
    server.resources[initial['href']] = (raw, 'legacy-v1')
    sync(ctx, initial['calendar_id'])
    master = ctx.store.find_by_external(initial['calendar_id'], uid='legacy-date')
    canonical, = ctx.store.exceptions_for(master['id'])
    ctx.tz = model.get_tz('America/New_York')
    wrong = providers.ics_to_rows(raw, initial['calendar_id'], initial['href'], 'legacy-v1', ctx.tz)[1]
    wrong = ctx.store.insert_event({**wrong, 'master_id': master['id']})
    effective = '2026-12-11T05:00:00+00:00'
    old_notice = reminders.notice_id_for(master['id'], effective, 0, wrong['recurrence_id'])
    new_notice = reminders.notice_id_for(master['id'], effective, 0, canonical['recurrence_id'])
    ctx.store.schedule_reminder(master['id'], effective, 0, effective, old_notice, wrong['recurrence_id'])
    with ctx.store._conn() as conn:
        conn.execute('UPDATE reminders SET state=? WHERE notice_id=?', (settled_state, old_notice))
    if already_waiting:
        # The previous version could list both logical copies and plan this notice before adoption.
        ctx.store.schedule_reminder(master['id'], effective, 0, effective, new_notice, canonical['recurrence_id'])
        with ctx.store._conn() as conn:
            conn.execute('UPDATE reminders SET state=?, attempts=4 WHERE notice_id=?', (already_waiting, new_notice))
    sync(ctx, initial['calendar_id'])
    with ctx.store._conn() as conn:
        record = dict(conn.execute('SELECT * FROM reminders WHERE notice_id=?', (new_notice,)).fetchone())
    reminders.set_rules(ctx.store, default=[0])
    reminders.set_mode(ctx.store, initial['calendar_id'], True)
    deliveries = []
    class Channel:
        def send(self, key, text):
            deliveries.append({'key': key, 'text': text})
            return 'sent', 'synthetic host accepted'
    # deliver_due re-reads each due reminder at max(now, wall clock): pin the wall clock to this fixture's instant
    with patch.object(reminders, 'now_utc', return_value=model.parse_stored(effective)):
        delivered = reminders.deliver_due(ctx.store, Channel(), ctx.tz, now=model.parse_stored(effective))
    print('READDRESS_NOTICE', json.dumps({'already_waiting': already_waiting, 'prior_state': settled_state,
          'canonical_state': record['state'], 'remaining_exceptions': len(ctx.store.exceptions_for(master['id'])),
          'delivery_stats': delivered, 'deliveries': deliveries}, ensure_ascii=False))
    assert deliveries == []
    assert record['state'] == settled_state
    if already_waiting: assert record['attempts'] == 4


@pytest.mark.parametrize('terminal', ['sent', 'unknown'])
def test_carry_preserves_terminal_destination_and_does_not_suppress_moved_meeting(tmp_path, terminal):
    ctx = context(tmp_path, 'UTC')
    start = '2026-12-11T09:00:00+00:00'; later = '2026-12-11T10:00:00+00:00'
    made = json.loads(tools.cal_create(ctx, title='Moved meeting', start=later, end='2026-12-11T11:00:00+00:00', reminders=[0], confirm=True))
    eid = made['event']['id']
    previous = {'state': 'unknown', 'occurrence_start_utc': start, 'offset_min': 0, 'sent_at': None, 'detail': 'old custody', 'attempts': 2}
    ctx.store.schedule_reminder(eid, start, 0, start, 'canonical')
    with ctx.store._conn() as conn:
        conn.execute('UPDATE reminders SET state=?, detail=?, attempts=7 WHERE notice_id=?', (terminal, 'canonical custody', 'canonical'))
    ctx.store.carry_settled_reminder(previous, eid, start, '', start, 'canonical')
    with ctx.store._conn() as conn: row=dict(conn.execute('SELECT * FROM reminders WHERE notice_id=?', ('canonical',)).fetchone())
    assert (row['state'], row['detail'], row['attempts']) == (terminal, 'canonical custody', 7)
    current = reminders.notice_id_for(eid, later, 0)
    reminders.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=model.parse_stored(later) - timedelta(days=1))
    ctx.store.carry_settled_reminder(previous, eid, later, '', later, current)
    calls=[]
    class Channel:
        def send(self, key, text): calls.append(key); return 'sent', 'accepted'
    # deliver_due re-reads each due reminder at max(now, wall clock): pin the wall clock to this fixture's instant
    with patch.object(reminders, 'now_utc', return_value=model.parse_stored(later)):
        reminders.deliver_due(ctx.store, Channel(), ctx.tz, now=model.parse_stored(later))
    assert calls == [current]


def disconnect(ctx, cid):
    calendar=ctx.store.get_calendar(cid); account=ctx.store.get_account(calendar['account_id'])
    preserved = ctx.providers
    with patch('credentials._mutate_tokens', side_effect=lambda state, key, apply: apply({})):
        result = json.loads(tools.cal_settings(ctx, action='disconnect', calendar_id=account['id'], confirm=True))
    ctx.providers = preserved  # Keep only the synthetic provider edge; the public disconnect path really ran.
    assert result['status'] == 'updated'
    return result['cancelled_intents'],account,calendar


def reconnect(ctx, account, calendar):
    # The reconnection's discovery observes the calendar again, as accounts.connect_caldav/reload_* do.
    ctx.store.upsert_account(account); ctx.store.upsert_calendar(calendar, restore=True)


def test_withdrawn_create_requires_explicit_edit_and_keeps_uid():
    ctx,cid,server=caldav_context(); server.down=True
    made=json.loads(tools.cal_create(ctx,title='Offline',start=_day(2).isoformat(),calendars=[cid],confirm=True))
    eid=made['event']['id']; uid=ctx.store.get_event(eid)['uid']
    count,account,calendar=disconnect(ctx,cid)
    assert count==1 and ctx.store.get_event(eid)['sync_state']=='failed'
    original=ctx.store.intents_for_event(eid)[0]
    assert original['state']=='resolved'
    assert json.loads(original['result_json'])['previous_result']['kind']=='network'
    assert json.loads(original['payload_json'])['desired']['uid']==uid
    server.down=False; reconnect(ctx,account,calendar);run_open_intents(ctx,2)
    assert server.resources=={}
    edited=json.loads(tools.cal_update(ctx,id=eid,title='Explicit retry',confirm=True))
    assert edited['status']=='updated' and len(server.resources)==1
    assert ctx.store.get_event(eid)['uid']==uid


def test_withdrawn_update_restores_confirmed_preimage_even_at_same_etag():
    ctx,cid,server=caldav_context()
    made=json.loads(tools.cal_create(ctx,title='Published',start=_day(2).isoformat(),calendars=[cid],confirm=True))
    eid=made['event']['id']; before=ctx.store.get_event(eid)
    server.down=True
    json.loads(tools.cal_update(ctx,id=eid,title='Unsent local',confirm=True))
    count,account,calendar=disconnect(ctx,cid)
    restored=ctx.store.get_event(eid)
    assert count==1 and (restored['title'],restored['etag'],restored['sync_state'])==(before['title'],before['etag'],'synced')
    withdrawn=ctx.store.intents_for_event(eid)[-1]
    assert json.loads(withdrawn['payload_json'])['desired']['title']=='Unsent local'
    server.down=False; reconnect(ctx,account,calendar); calls=len(server.calls)
    sync(ctx,cid);run_open_intents(ctx,2)
    assert ctx.store.get_event(eid)['title']=='Published'
    assert not [call for call in server.calls[calls:] if call[0] in ('PUT','DELETE')]
    assert ctx.store.intent_counts(active_only=True).get('failed',0)==0


def test_missing_preimage_stays_honest_and_existing_unknown_is_not_resolved():
    ctx,cid,server=caldav_context()
    made=json.loads(tools.cal_create(ctx,title='Published',start=_day(2).isoformat(),calendars=[cid],confirm=True))
    eid=made['event']['id']; account=ctx.store.get_calendar(cid)['account_id']
    ctx.store.update_event(eid, {'title':'Dirty','sync_state':'pending'})
    intent=ctx.store.add_intent('update',account,cid,eid,{})
    unknown=ctx.store.add_intent('calendar',account,cid,'',{})
    ctx.store.settle_intent(unknown['id'],'uncertain',{'message':'Earlier effect unknown'})
    assert ctx.store.cancel_intents_for_account(account)==1
    assert ctx.store.get_intent(intent['id'])['state']=='resolved'
    assert ctx.store.get_event(eid)['sync_state']=='failed'
    assert ctx.store.get_event(eid)['title']=='Dirty'
    assert ctx.store.get_intent(unknown['id'])['state']=='uncertain'


@pytest.mark.parametrize('outcome',['ack','lost_response'])
def test_active_create_disconnect_retains_late_outcome_and_never_reconnect_resends(outcome):
    ctx,cid,server=caldav_context(); adapter=ctx.providers.adapter_for(ctx.store.get_calendar(cid)['account_id'])
    create=adapter.create; retained={}
    def during(calendar,event,payload):
        count,account,cal=disconnect(ctx,cid);retained.update(count=count,account=account,calendar=cal,eid=event['id'])
        intent=ctx.store.intents_for_event(event['id'])[-1]
        assert intent['state']=='uncertain' and intent['lease_owner'] and count==0
        result=create(calendar,event,payload)
        if outcome=='lost_response': raise ops.ProviderError('network','Response lost after apply')
        return result
    adapter.create=during
    made=json.loads(tools.cal_create(ctx,title='In flight',start=_day(2).isoformat(),calendars=[cid],confirm=True))
    eid=retained['eid'];latest=ctx.store.intents_for_event(eid)[-1]
    assert latest['state']==('done' if outcome=='ack' else 'uncertain')
    assert len(server.resources)==1
    adapter.create=create; reconnect(ctx,retained['account'],retained['calendar']);calls=len(server.calls)
    run_open_intents(ctx,2)
    assert len(server.calls)==calls
    if outcome=='lost_response':
        changed=json.loads(tools.cal_update(ctx,id=eid,title='Must wait for comparison',confirm=True))
        assert not [call for call in server.calls[calls:] if call[0]=='PUT']
        assert ctx.store.get_intent(latest['id'])['state']=='uncertain'
        assert changed['status']!='updated'
    else:
        assert made['status']=='created'


def test_late_receipt_checks_attempt_tuple_not_expiry_and_never_overwrites_new_owner():
    ctx,cid,server=caldav_context(); account=ctx.store.get_calendar(cid)['account_id']
    event=ctx.store.insert_event({'calendar_id':cid,'title':'Draft','start_utc':_day(2).isoformat(),'end_utc':_day(2,10).isoformat()})
    intent=ctx.store.add_intent('create',account,cid,event['id'],{})
    leased=ctx.store.lease_intent(intent['id'],'old-owner')
    with patch('store.now_utc', return_value=model.parse_stored(leased['lease_until']) + timedelta(hours=1)):
        assert ctx.store.current_leased_intent(leased) is None
        assert ctx.store.current_leased_intent(leased, allow_uncertain=True)['state'] == 'pending'
    assert ctx.store.cancel_intents_for_account(account)==0
    assert ctx.store.current_leased_intent(leased) is None
    assert ctx.store.current_leased_intent(leased,allow_uncertain=True)['state']=='uncertain'
    assert ctx.store.acknowledge_write(leased,event,{'href':'https://fixture/event','etag':'v1'},'',False)
    assert ctx.store.get_intent(intent['id'])['state']=='done'
    other=ctx.store.add_intent('update',account,cid,event['id'],{})
    old=ctx.store.lease_intent(other['id'],'old-owner')
    with ctx.store._conn() as conn:
        conn.execute("UPDATE intents SET lease_owner='new-owner',attempts=attempts+1 WHERE id=?",(other['id'],))
    assert not ctx.store.acknowledge_write(old,event,{'etag':'stale'},'')
    assert not ctx.store.acknowledge_write(old,event,{},'',delete=True)
    assert ctx.store.get_event(event['id']) is not None
    assert ops._defer(ctx.store,old,'stale error')['status']=='uncertain'
    assert ctx.store.get_intent(other['id'])['lease_owner']=='new-owner'


def test_agenda_readiness_and_persistent_warning_match_status(tmp_path):
    ctx=context(tmp_path,'UTC'); cid=add_external_calendar(ctx.store)
    ctx.store.set_sync_state(cid,last_error='Linked copy copy-1 remains diverged')
    for _ in range(2):
        data=routes.agenda_payload(ctx,'day','',[],False)
        account=next(a for a in data['accounts'] if a['id']==ctx.store.get_calendar(cid)['account_id'])
        assert account['status']=='unavailable' and account['adapter_error']
        assert next(c for c in data['calendars'] if c['id']==cid)['sync_warning']=='Linked copy copy-1 remains diverged'
        report = next(a for a in json.loads(management.cal_calendars(ctx))['accounts'] if a['account_id'] == account['id'])
        assert report['status'] == 'unavailable' and report['error']
        assert next(c for c in report['calendars'] if c['id'] == cid)['sync_warning'] == 'Linked copy copy-1 remains diverged'


@pytest.mark.parametrize('public_base',['','https://public.example.test/ouro'])
def test_local_oauth_probe_is_direct_without_restricting_public_redirect(tmp_path,monkeypatch,public_base):
    ctx=context(tmp_path,'UTC');ctx.secrets={'GOOGLE_OAUTH_CLIENT_ID':'synthetic-client'}
    ctx.api=SimpleNamespace(get_runtime_info=lambda:{'server_port':9876});seen=[]
    def transport(self,request):
        seen.append((request.host,request.full_url,request.get_method()))
        response=urllib.response.addinfourl(io.BytesIO(b''),Message(),request.full_url,code=200);response.msg='OK';return response
    monkeypatch.setattr(urllib.request,'_opener',None)
    monkeypatch.setattr(urllib.request,'getproxies',lambda:{'http':'http://synthetic-proxy.example:3128'})
    monkeypatch.setattr(urllib.request,'proxy_bypass',lambda host:False)
    monkeypatch.setattr(urllib.request.HTTPHandler,'http_open',transport)
    with patch('host_sign_in.HostSignIn._call', return_value={'ok': True}):
        result=accounts.connect_google(ctx,public_base_url=public_base)
    assert result['status']=='ok'
    assert seen==[('127.0.0.1:9876','http://127.0.0.1:9876/onboarding/static/provider-return.html','HEAD')]
    assert result['redirect_uri'].startswith(public_base or 'http://127.0.0.1:9876')


@pytest.mark.parametrize('outcome', ['ack', 'lost_response'])
def test_active_update_keeps_late_result_and_explicit_provider_comparison(outcome):
    ctx,cid,server=caldav_context()
    made=json.loads(tools.cal_create(ctx,title='Published',start=_day(2).isoformat(),calendars=[cid],confirm=True))
    eid=made['event']['id'];adapter=ctx.providers.adapter_for(ctx.store.get_calendar(cid)['account_id'])
    update=adapter.update;retained={}
    def during(calendar,event,expected,payload):
        count,account,cal=disconnect(ctx,cid);retained.update(account=account,calendar=cal)
        assert count==0 and ctx.store.intents_for_event(eid)[-1]['state']=='uncertain'
        result=update(calendar,event,expected,payload)
        if outcome=='lost_response':raise ops.ProviderError('network','Reply lost after accepted update')
        return result
    adapter.update=during
    changed=json.loads(tools.cal_update(ctx,id=eid,title='Accepted remotely',confirm=True))
    latest=ctx.store.intents_for_event(eid)[-1]
    assert latest['state']==('done' if outcome=='ack' else 'uncertain')
    adapter.update=update;reconnect(ctx,retained['account'],retained['calendar']);calls=len(server.calls)
    run_open_intents(ctx,2)
    assert len(server.calls)==calls
    if outcome=='lost_response':
        inspection=json.loads(tools.cal_events(ctx,id=eid))['event']['conflict']
        assert inspection['resolution_token']
        resolved=json.loads(tools.cal_update(ctx,id=eid,resolution='provider',resolution_token=inspection['resolution_token'],confirm=True))
        assert resolved['status']=='resolved'
        assert ctx.store.get_intent(latest['id'])['state']=='resolved'
        assert ctx.store.get_event(eid)['title']=='Accepted remotely'
        assert not [call for call in server.calls[calls:] if call[0] in ('PUT','DELETE')]
    else:assert changed['status']=='updated'


def test_active_delete_ack_is_atomic_after_disconnect():
    ctx,cid,server=caldav_context()
    made=json.loads(tools.cal_create(ctx,title='Delete in flight',start=_day(2).isoformat(),calendars=[cid],confirm=True))
    eid=made['event']['id'];adapter=ctx.providers.adapter_for(ctx.store.get_calendar(cid)['account_id']);delete=adapter.delete
    def during(calendar,event,expected,payload=None):
        count,_,_=disconnect(ctx,cid)
        assert count==0 and ctx.store.intents_for_event(eid)[-1]['state']=='uncertain'
        return delete(calendar,event,expected,payload)
    adapter.delete=during
    removed=json.loads(tools.cal_delete(ctx,id=eid,confirm=True))
    assert removed['status']=='deleted'
    assert ctx.store.get_event(eid) is None and server.resources=={}
    assert ctx.store.intents_for_event(eid)[-1]['state']=='done'


def test_disconnect_between_lease_and_park_keeps_its_recorded_outcome():
    ctx, cid, server = caldav_context(); server.down = True
    eid = json.loads(tools.cal_create(ctx, title='Queued', start=_day(2).isoformat(), calendars=[cid], confirm=True))['event']['id']
    leased = ctx.store.lease_intent(ctx.store.intents_for_event(eid)[-1]['id'], 'companion')
    def gone(account_id):   # the owner disconnects after this lease and before the attempt parks
        disconnect(ctx, cid)
        return None
    result = ops.run_leased_intent(ctx.store, SimpleNamespace(adapter_for=gone, errors={}), leased)
    assert (result['status'], ctx.store.get_intent(leased['id'])['state']) == ('uncertain', 'uncertain')


def test_disconnect_treats_an_older_unproven_network_failure_as_unknown():
    ctx, cid, server = caldav_context(); server.down = True
    made = [json.loads(tools.cal_create(ctx, title=title, start=_day(2).isoformat(), calendars=[cid], confirm=True))['event']['id']
            for title in ('Recorded earlier', 'Offline now')]
    legacy, offline = (ctx.store.intents_for_event(eid)[-1] for eid in made)
    ctx.store.settle_intent(legacy['id'], 'pending', {'error': 'connection lost', 'kind': 'network'})   # the shape earlier versions wrote
    count, _, _ = disconnect(ctx, cid)
    assert (count, [ctx.store.get_intent(i['id'])['state'] for i in (legacy, offline)]) == (1, ['uncertain', 'resolved'])


@pytest.mark.parametrize('failure', [ConnectionResetError('the reply broke off after the PUT left'),
                                     urllib.error.URLError(ConnectionRefusedError('offline'))])
def test_a_caldav_create_is_withdrawn_on_disconnect_only_when_it_never_left(failure):
    ctx, cid, _ = caldav_context()
    ctx.providers = OneAdapterProviders(providers.YandexAdapter('round2@example.test', 'app-password'))   # the real transport
    lost = isinstance(failure, ConnectionResetError)
    with patch('urllib.request.build_opener') as build:
        build.return_value.open.side_effect = failure
        made = json.loads(tools.cal_create(ctx, title='Sent once', start=_day(2).isoformat(), calendars=[cid], confirm=True))
    intent = ctx.store.intents_for_event(made['event']['id'])[-1]
    assert (made['status'], json.loads(intent['result_json'])['delivery']) == ('pending', 'unknown' if lost else 'none')
    free = json.loads(tools.cal_free(ctx, start=_day(2, 0).isoformat(), end=_day(3, 0).isoformat()))
    assert (made['event']['id'] in free['conflict_event_ids']) is lost   # a create that may exist is not a settled slot
    count, _, _ = disconnect(ctx, cid)
    assert (count, ctx.store.get_intent(intent['id'])['state']) == ((0, 'uncertain') if lost else (1, 'resolved'))


def test_lost_caldav_update_is_not_resent_and_keeps_its_slot_disputed():
    ctx, cid, server = caldav_context()
    eid = json.loads(tools.cal_create(ctx, title='Planned', start=_day(2).isoformat(), calendars=[cid], confirm=True))['event']['id']
    puts = []
    def transport(request, timeout=None):
        text, etag = server.resources[request.full_url]
        if request.get_method() == 'PUT':   # the server applies the write, then the reply breaks off
            puts.append(request.full_url)
            server.resources[request.full_url] = (request.data.decode(), server._etag())
            raise ConnectionResetError('the reply broke off after the PUT was applied')
        reply = MagicMock()
        reply.__enter__.return_value, reply.status, reply.headers = reply, 200, {'ETag': etag}
        reply.read.return_value = text.encode()
        return reply
    ctx.providers = OneAdapterProviders(providers.YandexAdapter('round2@example.test', 'app-password'))   # the real transport
    with patch('urllib.request.build_opener') as build:
        build.return_value.open.side_effect = transport
        changed = json.loads(tools.cal_update(ctx, id=eid, title='Moved', confirm=True))
        run_open_intents(ctx, 2)
    intent = ctx.store.intents_for_event(eid)[-1]
    assert (changed['status'], intent['state'], len(puts)) == ('uncertain', 'uncertain', 1)   # never sent again
    free = json.loads(tools.cal_free(ctx, start=_day(2, 0).isoformat(), end=_day(3, 0).isoformat()))
    assert free['availability_complete'] is False and eid in free['conflict_event_ids']
    count, _, _ = disconnect(ctx, cid)
    assert (count, ctx.store.get_intent(intent['id'])['state'], ctx.store.get_event(eid)['title']) == (0, 'uncertain', 'Moved')


@pytest.mark.parametrize('applied', [False, True])
@pytest.mark.parametrize('choice', ['provider', 'local'])
def test_unknown_create_resolves_at_its_original_provider_identity(applied, choice):
    ctx, cid, server = caldav_context()
    adapter = ctx.providers.adapter_for(ctx.store.get_calendar(cid)['account_id'])
    create = adapter.create
    retained = {}
    def interrupted(calendar, event, payload):
        _, account, cal = disconnect(ctx, cid)
        retained.update(account=account, calendar=cal, eid=event['id'], uid=event['uid'])
        if applied:
            create(calendar, event, payload)
        raise ops.ProviderError('network', 'The original request ended without a delivery receipt')
    adapter.create = interrupted
    initial = json.loads(tools.cal_create(ctx, title='Original request', start=_day(2).isoformat(), calendars=[cid], confirm=True))
    assert initial['status'] == 'uncertain'
    adapter.create = create
    reconnect(ctx, retained['account'], retained['calendar'])
    calls = len(server.calls)
    run_open_intents(ctx, 2)
    assert len(server.calls) == calls
    inspection = json.loads(tools.cal_events(ctx, id=retained['eid']))['event']['conflict']
    assert inspection['delivery_outcome_unknown'] and inspection['resolution_token']
    assert (inspection['provider'] is not None) == applied
    resolved = json.loads(tools.cal_update(ctx, id=retained['eid'], resolution=choice,
                                         resolution_token=inspection['resolution_token'], confirm=True))
    assert resolved['status'] in ('resolved', 'pending'), resolved
    run_open_intents(ctx, 3)
    if choice == 'provider':
        assert len(server.resources) == int(applied)
        assert not [call for call in server.calls[calls:] if call[0] in ('PUT', 'DELETE')]
    else:
        assert len(server.resources) == 1
        current = ctx.store.get_event(retained['eid'])
        assert current['uid'] == retained['uid'] and current['sync_state'] == 'synced'


@pytest.mark.parametrize('kind', ['update', 'delete', 'rsvp'])
@pytest.mark.parametrize('legacy', [True, False])
def test_unknown_queued_mutation_is_compared_without_resend(kind, legacy):
    ctx, cid, server = caldav_context()
    eid = json.loads(tools.cal_create(ctx, title='Published', start=_day(2).isoformat(), calendars=[cid], confirm=True))['event']['id']
    row = ctx.store.get_event(eid)
    payload = {'response': 'accepted'} if kind == 'rsvp' else {'changes': {'title': 'Edited'}} if kind == 'update' else {}
    intent = ctx.store.add_intent(kind, row['account_id'], cid, eid, payload, expected_etag=row['etag'])
    previous = {'error': 'reply lost', 'kind': 'network', **({} if legacy else {'delivery': 'unknown'})}
    ctx.store.settle_intent(intent['id'], 'pending', previous)
    writes = lambda: [c for c in server.calls if c[0] in ('PUT', 'DELETE', 'POST')]
    before = writes()
    info = json.loads(tools.cal_events(ctx, id=eid))['event']['conflict']
    assert info['delivery_outcome_unknown'] and info['resolution_token']
    assert json.loads(tools.cal_update(ctx, id=eid, title='Another edit', confirm=True))['status'] == 'conflict'
    [reply] = ops.retry_due_intents(ctx.store, ctx.providers)
    assert (reply['status'], writes()) == ('uncertain', before)
    saved = ctx.store.get_intent(intent['id'])
    assert (saved['state'], ctx.store.get_event(eid)['sync_state']) == ('uncertain', 'conflict')
    assert json.loads(saved['result_json'])['previous_result'] == previous


def test_legacy_unknown_create_still_recovers_at_its_original_address():
    ctx, cid, server = caldav_context()
    server.lose_response.add('PUT')
    made = json.loads(tools.cal_create(ctx, title='Stable birth', start=_day(2).isoformat(), calendars=[cid], confirm=True))
    eid = made['event']['id']
    birth = ctx.store.intents_for_event(eid)[-1]
    ctx.store.settle_intent(birth['id'], 'pending', {'error': 'reply lost', 'kind': 'network'})
    [reply] = ops.retry_due_intents(ctx.store, ctx.providers)
    assert reply['status'] == 'done' and len(server.resources) == 1
    assert ctx.store.get_event(eid)['sync_state'] == 'synced'


@pytest.mark.parametrize('provider', ['caldav', 'google'])
def test_confirmed_absence_settles_unknown_delete_without_losing_receipt(provider):
    from test_calendar_provider_divergence import google_context
    ctx, cid, server = (caldav_context if provider == 'caldav' else google_context)()
    eid = json.loads(tools.cal_create(ctx, title='Remove', start=_day(2).isoformat(), calendars=[cid], confirm=True))['event']['id']
    server.lose_response.add('DELETE')
    assert json.loads(tools.cal_delete(ctx, id=eid, scope='all', confirm=True))['status'] == 'uncertain'
    previous = ctx.store.intents_for_event(eid)[-1]
    sync(ctx, cid)
    closed = ctx.store.get_intent(previous['id'])
    assert ctx.store.get_event(eid) is None and closed['state'] == 'done'
    assert json.loads(closed['result_json'])['previous_result'] == json.loads(previous['result_json'])
    assert ctx.store.intent_counts(active_only=True) == {}


def test_absence_does_not_release_unsent_delete_held_for_review():
    ctx, cid, server = caldav_context()
    eid = json.loads(tools.cal_create(ctx, title='Held', start=_day(2).isoformat(), calendars=[cid], confirm=True))['event']['id']
    server.down = True
    assert json.loads(tools.cal_delete(ctx, id=eid, scope='all', confirm=True))['status'] == 'pending'
    aid = ctx.store.get_calendar(cid)['account_id']
    assert ctx.store.hold_intents_for_account(aid, 'sign-in status unconfirmed') == 1
    held = ctx.store.intents_for_event(eid)[-1]
    assert json.loads(held['result_json'])['outcome'] == 'hold_for_review'
    server.down = False
    server.resources.clear()
    sync(ctx, cid)
    assert ctx.store.get_event(eid)['sync_state'] == 'conflict'
    assert ctx.store.get_intent(held['id']) == held
