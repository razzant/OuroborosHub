"""Accepted review scenarios through production calendar owners, offline only."""
import asyncio
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import links
import management
import ops
import providers
import providers_google
import reminders
import routes
import tools
import test_calendar_review_r5 as fixture
from test_calendar_atomic_replay import instance
from test_calendar_provider_divergence import _day
from test_calendar_round2 import RouteAPI, Request, add_local_calendar, make_context
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, now_utc, parse_stored
from scripts.worker import reconcile_calendar
from store import Store


@pytest.fixture
def linked_world():
    import shutil
    world = fixture.ReviewR5Tests().linked()
    yield world
    shutil.rmtree(world[0].state_dir)


@pytest.mark.parametrize('mode', ['full', 'busy'])
def test_source_occurrence_content_reaches_the_selected_projection(linked_world, mode):
    ctx, cid, caldav, google, adapter, source_id, dest = linked_world
    ctx.store.update_calendar(dest['calendar_id'], {'publish_mode': mode})
    remote_id, _ = instance(ctx, google, dest['id'], 3)
    def edit(calendar):
        master = next(c for c in calendar.walk() if c.name == 'VEVENT')
        exc = copy.deepcopy(master)
        exc.pop('RRULE', None)
        exc.add('recurrence-id', _day(3))
        exc['DTSTART'].dt, exc['DTEND'].dt = _day(3), _day(3) + timedelta(hours=1)
        exc['SUMMARY'], exc['DESCRIPTION'], exc['LOCATION'] = 'Renamed occurrence', 'New notes', 'New room'
        calendar.add_component(exc)
    caldav.edit(ctx.store.get_event(source_id)['href'], edit)
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
    source = ctx.store.exceptions_for(source_id)[0]
    assert (source['title'], source['description'], source['location']) == ('Renamed occurrence', 'New notes', 'New room')
    remote = google.events[remote_id]
    expected = ('Renamed occurrence', 'New notes', 'New room') if mode == 'full' else ('Занят', '', '')
    assert (remote['summary'], remote.get('description', ''), remote.get('location', '')) == expected
    assert ctx.store.get_event(dest['id'])['link_state'] == links.FOLLOWING


@pytest.mark.parametrize('status', [204, 207])
def test_collection_delete_requires_complete_provider_outcome(status, tmp_path):
    store = Store(str(tmp_path))
    account = 'caldav:fixture'
    calendar = {'id': 'caldav:fixture:work', 'account_id': account, 'provider': 'caldav', 'name': 'Work',
                'href': 'https://dav.example.test/calendars/work/'}
    store.upsert_account({'id': account, 'provider': 'caldav', 'login': 'fixture'})
    store.upsert_calendar(calendar)
    event = store.insert_event({'calendar_id': calendar['id'], 'start_utc': iso_utc(_day(2)),
                                'end_utc': iso_utc(_day(2)+timedelta(hours=1)), 'title': 'Retained resource'})
    pending = store.add_intent('update', account, calendar['id'], event['id'], {'changes': {'title': 'pending'}})
    adapter = providers.CalDAVAdapter('fixture', 'fixture', 'https://dav.example.test', account_id=account,
                                      home_url='https://dav.example.test/calendars/')
    body = '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/calendars/work/locked.ics</d:href><d:status>HTTP/1.1 423 Locked</d:status></d:response></d:multistatus>'
    calls = []
    adapter._request = lambda *args, **kwargs: (calls.append(args) or (status, {}, body if status == 207 else ''))
    ctx = SimpleNamespace(store=store, providers=SimpleNamespace(adapter_for=lambda _: adapter, errors={}))
    request = dict(action='delete', calendar_id=calendar['id'], request_id='delete', confirm=True)
    result = json.loads(management.cal_manage(ctx, **request))
    assert json.loads(management.cal_manage(ctx, **request))['status'] == result['status']
    assert len(calls) == 1
    if status == 204:
        assert result['status'] == 'done' and store.get_calendar(calendar['id'])['deleted_at']
        assert store.get_intent(pending['id'])['state'] == 'failed'
    else:
        assert result['status'] != 'done'
        assert not store.get_calendar(calendar['id'])['deleted_at']
        assert store.get_intent(pending['id'])['state'] == 'pending'


def test_provider_conflict_choice_keeps_copy_divergence_and_explicit_follow(linked_world):
    ctx, cid, caldav, google, left, source_id, projection = linked_world
    remote = google.events[projection['external_id']]
    remote.update(summary='Remote copy decision', etag=google._etag())
    result = json.loads(tools.cal_update(ctx, id=projection['id'], title='Local desired', scope='all', confirm=True))
    assert result['status'] == 'conflict'
    detail = json.loads(tools.cal_events(ctx, id=projection['id']))['event']['conflict']
    result = json.loads(tools.cal_update(ctx, id=projection['id'], resolution='provider', resolution_token=detail['resolution_token'], confirm=True))
    assert result['status'] == 'resolved'
    assert ctx.store.get_event(projection['id'])['link_state'] == links.DIVERGED
    tools.cal_update(ctx, id=source_id, title='Later source edit', scope='all', confirm=True)
    assert google.events[projection['external_id']]['summary'] == 'Remote copy decision'
    resumed = json.loads(tools.cal_update(ctx, id=projection['id'], link_action='follow', scope='all', confirm=True))
    assert resumed['status'] == 'ok'
    assert ctx.store.get_event(projection['id'])['link_state'] == links.FOLLOWING
    assert google.events[projection['external_id']]['summary'] == 'Later source edit'


@pytest.mark.parametrize('owner_zone', ['Asia/Dubai', 'Europe/Moscow'])
@pytest.mark.parametrize('mutation', ['renamed', 'moved', 'cancelled'])
def test_routine_exception_reminders_use_effective_time(tmp_path, owner_zone, mutation):
    ctx = make_context()
    ctx.state_dir = str(tmp_path)
    ctx.store = Store(str(tmp_path))
    ctx.tz = get_tz('Asia/Dubai')
    start = (now_utc()+timedelta(days=2)).astimezone(ctx.tz).replace(hour=6, minute=30, second=0, microsecond=0)
    made = json.loads(tools.cal_create(ctx, title='Routine', start=start.isoformat(), duration_min=30,
                                      availability='soft', rrule='FREQ=DAILY;COUNT=3', reminders=[15], confirm=True))
    eid = made['event']['id']
    ctx.tz = get_tz(owner_zone)
    ctx.store.set_setting('timezone', owner_zone)
    left = (start+timedelta(days=1)).astimezone(ctx.tz).replace(hour=0, minute=0)
    event = json.loads(tools.cal_events(ctx, start=left.isoformat(), end=(left+timedelta(days=1)).isoformat()))['events'][0]
    assert json.loads(tools.cal_update(ctx, id=event['id'], title='Changed routine', confirm=True))['status'] == 'updated'
    occurrence = next(o for o in ctx.occurrences(left, left+timedelta(days=1)) if o.get('series_id') == eid)
    fire = parse_stored(occurrence['start_utc']) - timedelta(minutes=15)
    ctx.store.drop_scheduled_reminders()
    reminders.plan(ctx.store, lambda s, e: [occurrence], now=fire)
    if mutation != 'renamed':
        # The queue was made before this external/local state change; delivery must still recheck it.
        exception = ctx.store.exceptions_for(eid)[0]
        fields = {'status': 'cancelled'} if mutation == 'cancelled' else {'start_utc': iso_utc(parse_stored(exception['start_utc'])+timedelta(hours=1))}
        ctx.store.update_event(exception['id'], fields)
    sent = []
    channel = SimpleNamespace(send=lambda notice_id, text: (sent.append(text) or ('sent', 'offline recorder')))
    result = reminders.deliver_due(ctx.store, channel, ctx.tz, now=fire)
    assert result['sent'] == (1 if mutation == 'renamed' else 0)
    assert len(sent) == (1 if mutation == 'renamed' else 0)


@pytest.mark.parametrize('shape', ['rule', 'dates', 'soft', 'all_day', 'all_day_dates'])
def test_provider_representation_does_not_mark_a_copy_changed(shape):
    start = datetime(2026, 11, 3, 0 if shape.startswith('all_day') else 15, tzinfo=get_tz('Europe/Moscow'))
    source = {'id': 'event', 'uid': 'uid', 'title': 'Meeting', 'description': '', 'location': '',
              'start_utc': iso_utc(start), 'end_utc': iso_utc(start+timedelta(days=1) if shape.startswith('all_day') else start+timedelta(hours=1)),
              'tz': 'Europe/Moscow', 'all_day': int(shape.startswith('all_day')), 'rrule': '', 'rdates': '', 'exdates': '',
              'status': 'confirmed', 'availability': 'soft' if shape == 'soft' else 'busy', 'attendees_json': '[]', 'reminders_json': '[]'}
    if shape == 'rule': source['rrule'] = 'FREQ=WEEKLY;BYDAY=TU,MO;COUNT=9'
    if shape in ('dates', 'all_day_dates'):
        source['rrule'] = 'FREQ=DAILY;COUNT=30'
        source['exdates'] = iso_utc(start+timedelta(days=4))+','+iso_utc(start+timedelta(days=2))
        source['rdates'] = iso_utc(start+timedelta(days=40))+','+iso_utc(start+timedelta(days=35))
    wire = providers_google.row_to_gevent(source)
    wire.update(id='event', iCalUID='uid')
    observed = providers_google.gevent_to_row(wire, 'calendar', get_tz('UTC'))
    if shape == 'rule': observed['rrule'] = 'COUNT=9;BYDAY=MO,TU;FREQ=WEEKLY'
    if shape in ('dates', 'all_day_dates'):
        for field in ('exdates', 'rdates'): observed[field] = ','.join(reversed(observed[field].split(',')))
    assert not links.changed(source, observed, provider_observation=True)
    assert links.changed(source, {**observed, 'title': 'Real edit'}, provider_observation=True)
    if shape == 'rule':
        actual_edit = {**observed, 'rrule': observed['rrule'].replace('COUNT=9', 'COUNT=10')}
    elif shape in ('dates', 'all_day_dates'):
        actual_edit = {**observed, 'exdates': observed['exdates'] + ',' + iso_utc(start+timedelta(days=9))}
    elif shape == 'soft':
        actual_edit = {**observed, 'availability': 'free'}
        assert links.changed(source, {**source, 'availability': 'busy'})
    else:
        actual_edit = {**observed, **{field: iso_utc(parse_stored(observed[field])+timedelta(days=1)) for field in ('start_utc', 'end_utc')}}
    assert links.changed(source, actual_edit, provider_observation=True)


@pytest.mark.parametrize('retired', ['original', 'selected', 'disconnected'])
def test_retired_default_is_explicit_and_named_selection_still_works(tmp_path, retired):
    ctx = make_context()
    ctx.store = Store(str(tmp_path))
    other = add_local_calendar(ctx.store, 'other', 'full')
    assert ctx.store.default_calendar()['id'] == DEFAULT_LOCAL_CALENDAR_ID
    if retired == 'disconnected':
        from test_calendar_provider_divergence import add_google_calendar
        chosen = add_google_calendar(ctx.store)
    else:
        chosen = DEFAULT_LOCAL_CALENDAR_ID if retired == 'original' else add_local_calendar(ctx.store, 'selected', 'full')
    assert json.loads(tools.cal_settings(ctx, action='set_calendar', calendar_id=chosen, default=True, confirm=True))['status'] == 'updated'
    assert ctx.store.default_calendar()['id'] == chosen
    if retired == 'disconnected':
        account_id = ctx.store.get_calendar(chosen)['account_id']
        assert json.loads(tools.cal_settings(ctx, action='disconnect', calendar_id=account_id, confirm=True))['status'] == 'updated'
    else:
        assert json.loads(management.cal_manage(ctx, action='delete', calendar_id=chosen, confirm=True))['status'] == 'done'
    ctx.store = Store(str(tmp_path))  # selection/retirement facts survive a process restart
    result = json.loads(tools.cal_create(ctx, title='Private', start=_day(2).isoformat(), confirm=True))
    assert result['status'] == 'error' and 'по умолчанию' in result['message']
    assert ctx.store.default_calendar() is None
    assert json.loads(tools.cal_create(ctx, title='Chosen', start=_day(2).isoformat(), calendars=other, confirm=True))['status'] == 'created'
    tools.cal_settings(ctx, action='set_calendar', calendar_id=other, default=True, confirm=True)
    assert json.loads(tools.cal_create(ctx, title='New default', start=_day(2).isoformat(), confirm=True))['event']['calendar_id'] == other


def test_ambiguous_reassign_route_reports_failure_and_partial_change(linked_world):
    ctx, cid, caldav, google, left, source_id, projection = linked_world
    ctx.store.update_event(projection['id'], {'is_primary': 1})
    other = add_local_calendar(ctx.store, 'other', 'full')
    api = RouteAPI()
    routes.register_routes(api, lambda: ctx)
    for title, expected in (('', 'failed'), ('Applied content', 'partial')):
        body = {'id': source_id, 'calendars': [cid, other], 'scope': 'all'}
        if title: body['title'] = title
        response = asyncio.run(api.routes['event/update'](Request(body)))
        result = json.loads(response.body)
        assert result['status'] == expected, result
        assert result['reassign']['failed']
        assert any(a['status'] == 'failed' for a in result['assignments'])
        if title: assert ctx.store.get_event(source_id)['title'] == title


def test_owner_deleted_local_copy_is_not_a_missing_provider_issue(tmp_path):
    ctx = make_context()
    ctx.store = Store(str(tmp_path))
    other = add_local_calendar(ctx.store, 'other', 'full')
    made = json.loads(tools.cal_create(ctx, title='Linked', start=_day(2).isoformat(), calendars=[DEFAULT_LOCAL_CALENDAR_ID, other], confirm=True))
    source = ctx.store.get_event(made['event']['id'])
    projection = next(e for e in ctx.store.group_masters(source['link_group_id']) if e['id'] != source['id'])
    assert json.loads(tools.cal_delete(ctx, id=projection['id'], confirm=True))['status'] == 'deleted'
    assert ctx.store.link_issues() == []
    assert ctx.store.get_event(source['id'])['deleted_at'] is None
    assert ctx.store.get_event(projection['id'])['deleted_at']


def test_normalizing_caldav_echo_and_explicit_follow_stay_following(linked_world):
    ctx, cid, server, google, adapter, caldav_id, google_row = linked_world
    source_id = google_row['id']
    assert ops.link_event(ctx.store, ctx.providers, source_id, 'source')['source_id'] == source_id
    def normalized(method, url, body=None, headers=None):
        result = server.request(method, url, body, headers)
        if method == 'PUT':
            server.edit(url, lambda calendar: calendar.add('X-PROVIDER-NORMALIZED', 'yes'))
            return result[0], {}, result[2]  # normalizing PUT does not supply a usable write ETag
        return result
    adapter._request = normalized
    changed = json.loads(tools.cal_update(ctx, id=source_id, rrule='FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;COUNT=9', scope='all', confirm=True))
    assert changed['status'] == 'updated', changed
    saved = ctx.store.get_event(caldav_id)
    assert saved['etag'] != server.resources[saved['href']][1]
    for title in ('Before', 'Source renamed'):
        if title != 'Before':
            assert json.loads(tools.cal_update(ctx, id=source_id, title=title, scope='all', confirm=True))['status'] == 'updated'
        server.edit(ctx.store.get_event(caldav_id)['href'], lambda calendar: calendar.add('X-METADATA-ONLY', title))
        reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
        assert ctx.store.get_event(caldav_id)['link_state'] == links.FOLLOWING
        assert ctx.store.get_event(caldav_id)['title'] == title
    def independent(calendar):
        next(item for item in calendar.walk() if item.name == 'VEVENT')['SUMMARY'] = 'Independent copy'
    server.edit(ctx.store.get_event(caldav_id)['href'], independent)
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
    assert ctx.store.get_event(caldav_id)['link_state'] == links.DIVERGED
    assert json.loads(tools.cal_update(ctx, id=caldav_id, link_action='follow', scope='all', confirm=True))['status'] == 'ok'
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
    assert ctx.store.get_event(caldav_id)['link_state'] == links.FOLLOWING
    assert ctx.store.get_event(caldav_id)['title'] == 'Source renamed'


@pytest.mark.parametrize('instance_after_master', ['inherited', 'independent'])
def test_source_batch_keeps_predecessor_and_observed_instance_baselines(linked_world, instance_after_master):
    ctx, cid, caldav, google, adapter, source_id, projection = linked_world
    remote_id, _ = instance(ctx, google, projection['id'], 3)
    google_adapter = ctx.providers.adapter_for('google:owner@example.test')
    request = google_adapter._request
    def apply(method, path, *args, **kwargs):
        result = request(method, path, *args, **kwargs)
        if method == 'PATCH' and path.endswith('/'+projection['external_id']):
            google.events[remote_id].update(summary='After' if instance_after_master == 'inherited' else 'Separate edit', etag=google._etag())
        return result
    google_adapter._request = apply
    def edit(calendar):
        master = next(c for c in calendar.walk() if c.name == 'VEVENT')
        master['SUMMARY'] = 'After'
        occurrence = copy.deepcopy(master)
        occurrence.pop('RRULE', None)
        occurrence.add('recurrence-id', _day(3))
        occurrence['DTSTART'].dt = _day(3)+timedelta(hours=2)
        occurrence['DTEND'].dt = _day(3)+timedelta(hours=3)
        calendar.add_component(occurrence)
    caldav.edit(ctx.store.get_event(source_id)['href'], edit)
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(cid), ctx.tz)
    override = ctx.store.exceptions_for(projection['id'])[0]
    if instance_after_master == 'inherited':
        assert override['sync_state'] == 'synced'
        assert parse_stored(google.events[remote_id]['start']['dateTime']) == _day(3)+timedelta(hours=2)
    else:
        assert override['sync_state'] == 'conflict'
        assert google.events[remote_id]['summary'] == 'Separate edit'
        assert parse_stored(google.events[remote_id]['start']['dateTime']) == _day(3)


@pytest.mark.parametrize('attempt', ['follow', 'direct'])
@pytest.mark.parametrize('choice', ['provider', 'local'])
def test_conflict_choice_restores_relation_before_the_abandoned_command(linked_world, attempt, choice):
    from test_calendar_provider_divergence import run_open_intents
    ctx, cid, caldav, google, left, source_id, projection = linked_world
    target = projection['id']
    remote = google.events[projection['external_id']]
    if attempt == 'follow':
        assert json.loads(tools.cal_update(ctx, id=target, title='Copy own', scope='all', confirm=True))['status'] == 'updated'
        assert ctx.store.get_event(target)['link_state'] == links.DIVERGED
    remote['etag'] = google._etag()  # metadata only, no independent content edit
    args = {'link_action': 'follow'} if attempt == 'follow' else {'title': 'Local desired'}
    tools.cal_update(ctx, id=target, scope='all', confirm=True, **args)
    assert ctx.store.get_event(target)['sync_state'] == 'conflict'
    detail = json.loads(tools.cal_events(ctx, id=target))['event']['conflict']
    result = json.loads(tools.cal_update(ctx, id=target, resolution=choice, resolution_token=detail['resolution_token'], confirm=True))
    assert result['status'] == ('resolved' if choice == 'provider' else 'pending')
    run_open_intents(ctx, 3)
    following = (attempt == 'direct') if choice == 'provider' else (attempt == 'follow')
    expected = links.FOLLOWING if following else links.DIVERGED
    assert ctx.store.get_event(target)['link_state'] == expected
    kept = remote['summary']
    assert json.loads(tools.cal_update(ctx, id=source_id, title='Later source edit', scope='all', confirm=True))['status'] == 'updated'
    assert remote['summary'] == ('Later source edit' if following else kept)


def test_external_occurrence_conflict_keeps_both_relation_observations(linked_world):
    ctx, cid, caldav, google, left, source_id, projection = linked_world
    remote_id, address = instance(ctx, google, projection['id'], 3)
    remote = google.events[remote_id]
    remote.update(summary='Remote occurrence', etag=google._etag())
    result = json.loads(tools.cal_update(ctx, id=address, title='Local occurrence', confirm=True))
    assert result['status'] == 'conflict'
    occurrence = ctx.store.exceptions_for(projection['id'])[0]
    detail = json.loads(tools.cal_events(ctx, id=occurrence['id']))['event']['conflict']
    assert json.loads(tools.cal_update(ctx, id=occurrence['id'], resolution='provider', resolution_token=detail['resolution_token'], confirm=True))['status'] == 'resolved'
    assert ctx.store.get_event(projection['id'])['link_state'] == links.DIVERGED
    assert ctx.store.get_event(occurrence['id'])['link_state'] == links.DIVERGED
    tools.cal_update(ctx, id=source_id, title='Later source edit', scope='all', confirm=True)
    assert remote['summary'] == 'Remote occurrence'


def test_reassign_waiting_for_deletion_keeps_existing_human_explanation(linked_world):
    ctx, cid, caldav, google, left, source_id, projection = linked_world
    real = ctx.providers
    ctx.providers = SimpleNamespace(adapter_for=lambda account: None if account.startswith('google:') else real.adapter_for(account), errors={})
    assert json.loads(tools.cal_delete(ctx, id=projection['id'], scope='all', confirm=True))['status'] == 'pending'
    api = RouteAPI()
    routes.register_routes(api, lambda: ctx)
    response = asyncio.run(api.routes['event/update'](Request({'id': source_id, 'scope': 'all', 'calendars': [cid, projection['calendar_id']]})))
    result = json.loads(response.body)
    assert result['status'] == 'conflict'
    messages = [row.get('message', '') for row in result['assignments']]
    assert 'pending_delete' not in messages
    assert any('удаление копии ещё не подтверждено' in message for message in messages)
    assert len(messages) == 1
