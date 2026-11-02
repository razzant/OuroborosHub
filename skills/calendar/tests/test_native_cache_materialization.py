"""Native observations survive ACKs and derived occurrences without historical payload copies."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

from test_recurrence_edit_contracts import legacy_status_source, tools, ops, model, providers, providers_google
from test_calendar_provider_divergence import run_open_intents, sync


def context(tmp_path, provider):
    ctx, eid, cid, adapter = legacy_status_source(tmp_path, provider)
    server = adapter._request.__self__
    if provider == 'google':
        # Google exposes generated instances of a new series through events.get.
        # Keep the real adapter; only this in-memory native transport supplies them.
        apply = server._apply
        def instances(method, path, params, body):
            key = unquote(path.split('/')[-1])
            if method == 'GET' and key not in server.events and '_' in key:
                parent, stamp = key.rsplit('_', 1)
                master = server.events.get(parent)
                if master and master.get('recurrence'):
                    slot = datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
                    item = {**master, 'id': key, 'recurringEventId': parent,
                            'originalStartTime': {'dateTime': model.iso_utc(slot)},
                            'start': {'dateTime': model.iso_utc(slot)},
                            'end': {'dateTime': model.iso_utc(slot + timedelta(hours=1))}}
                    item.pop('recurrence')
                    server.events[key] = item
            return apply(method, path, params, body)
        server._apply = instances
    return ctx, eid, cid, adapter, server


def raw_status(raw, provider, slot=''):
    if provider == 'google':
        return json.loads(raw)['status']
    rows = providers.ics_to_rows(raw, 'fixture', 'https://fixture', '', model.get_tz('UTC'))
    return next(row['status'] for row in rows if row['recurrence_id'] == slot)


def native_status(ctx, row, provider, server):
    if provider == 'google':
        return server.events[row['external_id']]['status']
    parent = ctx.store.get_event(row['master_id']) if row.get('master_id') else row
    return raw_status(server.resources[parent['href']][0], provider, row.get('recurrence_id') or '')


def materialize(ctx, eid, cid, operation):
    if operation in ('copy', 'title_copy'):
        destination = {**ctx.store.get_calendar(cid), 'id': cid + ':copy', 'name': 'Copy',
                       'external_id': 'copy', 'href': 'https://dav.example.test/copy/'}
        ctx.store.upsert_calendar(destination)
        extra = {'title': 'Rename and copy'} if operation == 'title_copy' else {}
        result = json.loads(tools.cal_update(ctx, id=eid, scope='all', calendars=[cid, destination['id']], confirm=True, **extra))
        created = ctx.store.get_event(result['reassign']['added'][0]['event_id'])
    elif operation == 'following':
        result = json.loads(tools.cal_update(ctx, id=eid+'@2026-12-15T09:00:00+00:00', scope='following', title='Suffix', confirm=True))
        with ctx.store._conn() as connection:
            created = dict(connection.execute("SELECT * FROM events WHERE calendar_id=? AND master_id='' AND id<>?", (cid, eid)).fetchone())
    else:
        result = json.loads(tools.cal_update(ctx, id=eid+'@2026-12-15T09:00:00+00:00', title='Occurrence', confirm=True))
        created = ctx.store.exceptions_for(eid)[0]
    assert '_native_payload' not in json.dumps(result)
    return result, created


def old_title_write(ctx, eid, adapter, monkeypatch):
    """True old writer/ACK shape, without importing private exports or old commits.

    Both old adapters serialized the flattened row's status for a title edit;
    their acknowledged result retained identity/ETag only. Exercise actual native
    serialization/transport and the actual store ACK with precisely those facts.
    """
    write = adapter.update
    def old(calendar, event, expected, payload):
        result = write(calendar, event, expected, {key: value for key, value in payload.items() if key != 'changes'})
        return {key: value for key, value in result.items() if key != '_native_payload'}
    with monkeypatch.context() as patch:
        patch.setattr(adapter, 'update', old)
        result = json.loads(tools.cal_update(ctx, id=eid, scope='all', title='Old edit', confirm=True))
    assert result['status'] == 'updated'
    receipt = ctx.store.intents_for_event(eid)[-1]
    assert json.loads(receipt['result_json']) == {'ok': True, 'etag': ctx.store.get_event(eid)['etag']}
    return receipt


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('history', ['no_write', 'old_title', 'current_title'])
@pytest.mark.parametrize('operation', ['copy', 'following', 'override', 'title_copy'])
def test_native_fact_distinguishes_old_and_current_title_ack(tmp_path, monkeypatch, provider, history, operation):
    ctx, eid, cid, adapter, server = context(tmp_path, provider)
    old = None
    if history == 'old_title':
        old = old_title_write(ctx, eid, adapter, monkeypatch)
    elif history == 'current_title':
        result = json.loads(tools.cal_update(ctx, id=eid, scope='all', title='Current edit', confirm=True))
        assert result['status'] == 'updated'
        assert json.loads(ctx.store.intents_for_event(eid)[-1]['result_json'])['native_cache'] is True
    expected = 'confirmed' if history == 'old_title' else 'tentative'
    source = ctx.store.get_event(eid)
    assert native_status(ctx, source, provider, server) == expected
    if old:
        assert raw_status(source['raw_payload'], provider) == 'tentative'
        assert ctx.store.native_cache_source(source) is None
    result, created = materialize(ctx, eid, cid, operation)
    assert result['status'] == 'updated', result
    assert created['status'] == expected
    assert all(json.loads(item['payload_json'])['desired']['status'] == expected for item in ctx.store.intents_for_event(created['id']))
    assert native_status(ctx, created, provider, server) == expected
    assert native_status(ctx, ctx.store.get_event(eid), provider, server) == expected
    if old:
        assert ctx.store.get_intent(old['id']) == old
    with ctx.store._conn() as connection:
        receipts = [item[0] for item in connection.execute('SELECT result_json FROM intents')]
    assert all('_native_payload' not in receipt and 'BEGIN:VCALENDAR' not in receipt and 'raw_payload' not in receipt for receipt in receipts)


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('pending', [False, True])
def test_equal_etag_feed_refreshes_cache_under_existing_owner_guard(tmp_path, monkeypatch, provider, pending):
    ctx, eid, cid, adapter, server = context(tmp_path, provider)
    old = old_title_write(ctx, eid, adapter, monkeypatch)
    if pending:
        with ctx.store.transaction() as view:
            ops._stage_update_event(view, eid, {'title': 'Pending owner title'}, scope='all')
    before = ctx.store.get_event(eid)
    assert raw_status(before['raw_payload'], provider) == 'tentative'
    sync(ctx, cid)
    after = ctx.store.get_event(eid)
    if pending:
        assert after == before
    else:
        assert raw_status(after['raw_payload'], provider) == 'confirmed'
        result, created = materialize(ctx, eid, cid, 'copy')
        assert result['status'] == 'updated' and created['status'] == 'confirmed'
    assert ctx.store.get_intent(old['id']) == old


def add_native_exception(ctx, eid, cid, adapter, server, provider, master_status, child_status, legacy):
    source = ctx.store.get_event(eid)
    slot = '2026-12-16T09:00:00+00:00'
    if provider == 'caldav':
        native = providers._ical().Calendar.from_ical(source['raw_payload'])
        native.walk('VEVENT')[0]['STATUS'] = master_status.upper()
        child = providers._ical().Event()
        child.add('UID', source['uid'])
        child.add('RECURRENCE-ID', model.parse_stored(slot))
        child.add('DTSTART', model.parse_stored(slot))
        child.add('DTEND', model.parse_stored(slot) + timedelta(hours=1))
        child.add('SUMMARY', 'Own override')
        child.add('STATUS', child_status.upper())
        native.add_component(child)
        raw = native.to_ical().decode('utf-8')
        server.resources[source['href']] = (raw, source['etag'])
        child_row = next(row for row in providers.ics_to_rows(raw, cid, source['href'], source['etag'], ctx.tz) if row['recurrence_id'])
    else:
        native = server.events[source['external_id']]
        native['status'] = master_status
        raw = json.dumps(native)
        child_native = {**native, 'id':source['external_id']+'_20261216T090000Z', 'status':child_status,
                        'recurringEventId':source['external_id'], 'originalStartTime':{'dateTime':slot},
                        'start':{'dateTime':slot}, 'end':{'dateTime':'2026-12-16T10:00:00+00:00'}}
        child_native.pop('recurrence')
        server.events[child_native['id']] = child_native
        child_row = providers_google.gevent_to_row(child_native, cid, ctx.tz)
        if legacy:child_row['raw_payload'] = ''  # Old Google exception facts were actually discarded.
    ctx.store.update_event(eid, {'raw_payload':raw, 'status':master_status})
    return ctx.store.insert_event({**child_row, 'master_id':eid, 'status':'confirmed' if legacy else child_status})


@pytest.mark.parametrize('provider', ['caldav', 'google'])
@pytest.mark.parametrize('operation', ['copy', 'following'])
@pytest.mark.parametrize('master_status,child_status', [('confirmed','tentative'), ('tentative','confirmed')])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('offline', [False, True])
def test_saved_exception_materialization_keeps_subject_status_and_target_delta(tmp_path, provider, operation, master_status, child_status, legacy, offline):
    ctx, eid, cid, adapter, server = context(tmp_path, provider)
    original = add_native_exception(ctx, eid, cid, adapter, server, provider, master_status, child_status, legacy)
    expected = 'confirmed' if legacy and provider == 'google' else child_status
    before = {item['id']:item for item in ctx.store.intents_for_event(original['id'])}
    server.down = offline
    result, parent = materialize(ctx, eid, cid, operation)
    assert result['status'] == ('pending' if offline else 'updated'), result
    moved = ctx.store.exceptions_for(parent['id'])[0]
    assert parent['status'] == master_status and moved['status'] == expected
    new = [item for item in ctx.store.intents_for_event(moved['id']) if item['id'] not in before]
    assert new and all(json.loads(item['payload_json'])['desired']['status'] == expected for item in new)
    if expected != master_status:
        assert all(json.loads(item['payload_json'])['changes']['status'] == expected for item in new)
    server.down = False
    run_open_intents(ctx, rounds=3)
    moved = ctx.store.get_event(moved['id'])
    assert all(ctx.store.get_intent(item['id'])['state'] == 'done' for item in new)
    assert native_status(ctx, moved, provider, server) == expected
    assert {key:ctx.store.get_intent(key) for key in before} == before


@pytest.mark.parametrize('operation', ['copy', 'following'])
@pytest.mark.parametrize('prior', ['title_pending', 'status_pending', 'status_acknowledged', 'cancelled'])
def test_caldav_saved_child_owns_its_status_and_old_command_custody(tmp_path, operation, prior):
    ctx, eid, cid, adapter, server = context(tmp_path, 'caldav')
    child = add_native_exception(ctx, eid, cid, adapter, server, 'caldav', 'confirmed', 'tentative', True)
    changes = {'title':'Pending child title'} if prior == 'title_pending' else {'status':'cancelled' if prior == 'cancelled' else 'confirmed'}
    if prior == 'status_acknowledged':
        result = ops.update_event(ctx.store, ctx.providers, child['id'], changes)
        assert all(row['status'] == 'done' for row in result['assignments'])
    else:
        with ctx.store.transaction() as view:
            ops._stage_update_event(view, child['id'], changes)
    before = {item['id']:item for item in ctx.store.intents_for_event(child['id'])}
    server.down = True
    result, parent = materialize(ctx, eid, cid, operation)
    assert result['status'] == 'pending'
    moved = ctx.store.exceptions_for(parent['id'])[0]
    expected = 'tentative' if prior == 'title_pending' else 'cancelled' if prior == 'cancelled' else 'confirmed'
    assert moved['status'] == expected
    new = [item for item in ctx.store.intents_for_event(moved['id']) if item['id'] not in before]
    assert new and all(json.loads(item['payload_json'])['desired']['status'] == expected for item in new)
    assert {key:ctx.store.get_intent(key) for key in before} == before


@pytest.mark.parametrize('has_body,has_version', [(False,True),(True,False),(True,True)])
def test_ack_native_cache_requires_an_accepted_body_and_version(tmp_path, has_body, has_version):
    ctx, eid, cid, adapter, server = context(tmp_path, 'caldav')
    with ctx.store.transaction() as view:
        ops._stage_update_event(view, eid, {'title':'Queued'}, scope='all')
    intent = ctx.store.intents_for_event(eid)[-1]
    leased = ctx.store.lease_intent(intent['id'], 'writer')
    source = ctx.store.get_event(eid)
    body = source['raw_payload'].replace('STATUS:TENTATIVE','STATUS:CONFIRMED')
    result = {'etag':'native-v2' if has_version else '', **({'_native_payload':body} if has_body else {})}
    assert ctx.store.acknowledge_write(leased, source, result, source['etag'], shared_resource=True)
    saved = ctx.store.get_event(eid)
    accepted = has_body and has_version
    assert saved['raw_payload'] == (body if accepted else source['raw_payload'])
    receipt = json.loads(ctx.store.get_intent(intent['id'])['result_json'])
    assert bool(receipt.get('native_cache')) == accepted and '_native_payload' not in receipt
    assert (ctx.store.native_cache_source(saved) is not None) == accepted
    other = ctx.store.add_intent('update', ctx.store.get_calendar(cid)['account_id'], cid, eid, {})
    stale = ctx.store.lease_intent(other['id'], 'stale-writer')
    with ctx.store._conn() as connection:
        connection.execute("UPDATE intents SET lease_owner='new-writer',attempts=attempts+1 WHERE id=?", (other['id'],))
    before = ctx.store.get_event(eid)
    assert not ctx.store.acknowledge_write(stale, before, {'etag':'wrong','_native_payload':'must not land'}, '', shared_resource=True)
    assert ctx.store.get_event(eid) == before


def test_late_child_ack_keeps_the_staged_old_resource_cache_binding(tmp_path):
    ctx, eid, cid, adapter, server = context(tmp_path, 'caldav')
    child = add_native_exception(ctx, eid, cid, adapter, server, 'caldav', 'confirmed', 'tentative', False)
    with ctx.store.transaction() as view:
        ops._stage_update_event(view, child['id'], {'title':'Earlier child write'})
    earlier = ctx.store.intents_for_event(child['id'])[-1]
    leased = ctx.store.lease_intent(earlier['id'], 'older-resource-writer')
    old_event = {**ctx.store.get_event(child['id']), **json.loads(earlier['payload_json'])['desired']}
    old_raw = ctx.store.get_event(eid)['raw_payload']
    with ctx.store.transaction() as view:
        result = ops._stage_update_event(view, eid+'@2026-12-15T09:00:00+00:00', {'title':'Suffix'}, scope='following')
    new_parent = ctx.store.get_event(result['split_master_id'])
    assert ctx.store.get_event(child['id'])['master_id'] == new_parent['id']
    assert ctx.store.acknowledge_write(leased, old_event, {'etag':'late-old-ack','_native_payload':old_raw}, earlier['expected_etag'], shared_resource=True)
    assert ctx.store.get_event(eid)['raw_payload'] == old_raw
    assert ctx.store.get_event(new_parent['id'])['raw_payload'] == new_parent['raw_payload'] == ''
    assert ctx.store.get_intent(earlier['id'])['payload_json'] == earlier['payload_json']
    assert ctx.store.get_event(child['id'])['status'] == 'tentative'


def test_conditional_revival_caches_the_final_native_write_not_cancelled_lookup(tmp_path):
    ctx, eid, cid, adapter, server = context(tmp_path, 'google')
    with ctx.store.transaction() as view:
        staged = ops._stage_create_event(view, {'title':'Explicit new create', 'start_utc':'2026-12-20T09:00:00+00:00',
            'end_utc':'2026-12-20T10:00:00+00:00', 'tz':'UTC', 'calendar_ids':[cid]})
    created = ctx.store.get_event(staged['event']['id'])
    identity = adapter.created_identity(ctx.store.get_calendar(cid), created)
    server.events[identity['external_id']] = {**providers_google.row_to_gevent(created), 'id':identity['external_id'],
        'iCalUID':created['uid'], 'etag':'cancelled-version', 'status':'cancelled'}
    intent = ctx.store.intents_for_event(created['id'])[0]
    assert ops.execute_intent(ctx.store, ctx.providers, intent, 'revival-test')['status'] == 'done'
    saved = ctx.store.get_event(created['id'])
    assert json.loads(saved['raw_payload'])['status'] == 'confirmed'
    assert json.loads(saved['raw_payload'])['etag'] == saved['etag'] != 'cancelled-version'
    receipt = json.loads(ctx.store.get_intent(intent['id'])['result_json'])
    assert receipt['native_cache'] is True and '_native_payload' not in receipt
    assert server.events[saved['external_id']]['status'] == 'confirmed'


def test_missing_google_native_response_is_not_synthesized_or_fetched(tmp_path, monkeypatch):
    ctx, eid, cid, adapter, server = context(tmp_path, 'google')
    request = adapter._request
    def partial(method, path, *args, **kwargs):
        status, headers, body = request(method, path, *args, **kwargs)
        return (status, headers, {'id':body['id'], 'etag':body['etag']}) if method == 'PATCH' else (status, headers, body)
    monkeypatch.setattr(adapter, '_request', partial)
    before = ctx.store.get_event(eid)['raw_payload']
    assert json.loads(tools.cal_update(ctx,id=eid,scope='all',title='No fabricated native body',confirm=True))['status'] == 'updated'
    assert [call[0] for call in server.calls] == ['PATCH']
    assert ctx.store.get_event(eid)['raw_payload'] == before
    assert ctx.store.native_cache_source(ctx.store.get_event(eid)) is None
    assert 'native_cache' not in json.loads(ctx.store.intents_for_event(eid)[-1]['result_json'])


def test_accepted_cache_refresh_does_not_overwrite_newer_owner_status(tmp_path):
    ctx, eid, cid, adapter, server = context(tmp_path, 'caldav')
    with ctx.store.transaction() as view:
        ops._stage_update_event(view, eid, {'title':'Earlier title'}, scope='all')
    older = ctx.store.intents_for_event(eid)[-1]
    leased = ctx.store.lease_intent(older['id'], 'older-writer')
    source = ctx.store.get_event(eid)
    with ctx.store.transaction() as view:
        ops._stage_update_event(view, eid, {'status':'cancelled'}, scope='all')
    newer = ctx.store.intents_for_event(eid)[-1]
    assert ctx.store.acknowledge_write(leased, source, {'etag':'accepted-title','_native_payload':source['raw_payload']}, source['etag'], shared_resource=True)
    saved = ctx.store.get_event(eid)
    assert saved['status'] == 'cancelled' and saved['sync_state'] == 'pending'
    current = ctx.store.get_intent(newer['id'])
    assert current['payload_json'] == newer['payload_json'] and current['attempts'] == newer['attempts'] == 0
    assert ctx.store.get_intent(older['id'])['payload_json'] == older['payload_json']


@pytest.mark.parametrize('native_status_value', ['tentative', 'confirmed'])
def test_google_authored_event_uses_calendar_scoped_native_id(tmp_path, monkeypatch, native_status_value):
    from types import SimpleNamespace
    from scripts.worker import reconcile_calendar
    from test_calendar_provider_divergence import FakeGoogle, add_google_calendar
    ctx = tools.Context(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda keys: {}))
    ctx.tz = model.get_tz('UTC')
    source_calendar = add_google_calendar(ctx.store)
    destination_account = 'google:destination@example.test'
    destination_calendar = destination_account + ':primary'
    ctx.store.upsert_account({'id':destination_account, 'provider':'google', 'login':'destination@example.test'})
    ctx.store.upsert_calendar({'id':destination_calendar, 'account_id':destination_account, 'provider':'google',
                              'external_id':'primary', 'name':'Destination', 'writable':True, 'publish_mode':'full'})
    source_server, destination_server = FakeGoogle(), FakeGoogle()
    source_adapter = source_server.attach(providers_google.GoogleAdapter('owner@example.test','synthetic','',model.now_utc(),'client','',None))
    destination_adapter = destination_server.attach(providers_google.GoogleAdapter('destination@example.test','synthetic','',model.now_utc(),'client','',None))
    adapters = {'google:owner@example.test':source_adapter, destination_account:destination_adapter}
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda account: adapters[account])
    created = json.loads(tools.cal_create(ctx,title='Authored Google event',start='2026-12-14T09:00Z',
        end='2026-12-14T10:00Z',rrule='FREQ=DAILY;COUNT=3',calendars=[source_calendar],confirm=True))
    assert created['status'] == 'created'
    source = ctx.store.get_event(created['event']['id'])
    authored_uid = source['uid']
    assert authored_uid != source_server.events[source['external_id']]['iCalUID']
    source_adapter._request('PATCH','/calendars/primary/events/'+source['external_id'],body={'status':native_status_value})
    fetch = source_adapter.fetch
    def legacy_fetch(*args, **kwargs):
        rows, cursor, kind, extra = fetch(*args, **kwargs)
        return [{**row, 'status':'confirmed'} for row in rows], cursor, kind, extra
    with monkeypatch.context() as patch:
        patch.setattr(source_adapter,'fetch',legacy_fetch)
        reconcile_calendar(ctx.store,ctx.providers,source_adapter,ctx.store.get_calendar(source_calendar),ctx.tz)
    source = ctx.store.get_event(source['id'])
    raw = json.loads(source['raw_payload'])
    assert source['uid'] == authored_uid != raw['iCalUID']
    assert source['external_id'] == raw['id'] == source_server.events[source['external_id']]['id']
    assert source['etag'] == raw['etag'] == source_server.events[source['external_id']]['etag']
    assert providers.materialized_event_status(source,ctx.store) == native_status_value
    copied = json.loads(tools.cal_update(ctx,id=source['id'],scope='all',calendars=[source_calendar,destination_calendar],confirm=True))
    assert copied['status'] == 'updated'
    copy = ctx.store.get_event(copied['reassign']['added'][0]['event_id'])
    assert copy['uid'] == authored_uid and ctx.store.get_event(source['id'])['uid'] == authored_uid
    assert copy['external_id'] == source['external_id']  # Distinct calendar/account namespaces permit this.
    assert copy['status'] == destination_server.events[copy['external_id']]['status'] == native_status_value
    assert all(json.loads(item['payload_json'])['desired']['status'] == native_status_value for item in ctx.store.intents_for_event(copy['id']))
