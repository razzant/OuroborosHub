"""A rejected occurrence follow restores its relation; explicit defaults survive reconnect."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import links
import tools
from model import now_utc
from providers_google import GoogleAdapter
from scripts.worker import sync_account
from store import Store
from test_calendar_atomic_replay import instance
from test_calendar_provider_divergence import _day, add_google_calendar, FakeGoogle
from test_calendar_review_round1 import linked_world as linked_world
from test_calendar_round2 import add_local_calendar, make_context


def choose_provider(ctx, event_id):
    detail = json.loads(tools.cal_events(ctx, id=event_id))['event']['conflict']
    return json.loads(tools.cal_update(ctx, id=event_id, resolution='provider',
                                      resolution_token=detail['resolution_token'], confirm=True))


@pytest.mark.parametrize('follow_scope', ['this', 'all'])
@pytest.mark.parametrize('after_choice', ['preserve', 'resume'])
def test_abandoned_occurrence_follow_keeps_the_provider_choice(linked_world, follow_scope, after_choice):
    ctx, cid, caldav, google, left, source_id, projection = linked_world
    event_id = projection['id']
    remote_id, address = instance(ctx, google, event_id, 3)
    key = address.split('@', 1)[1]
    assert json.loads(tools.cal_update(ctx, id=address, title='Copy occurrence', confirm=True))['status'] == 'updated'
    google.events[remote_id]['etag'] = google._etag()
    result = json.loads(tools.cal_update(ctx, id=address if follow_scope == 'this' else event_id,
                                         link_action='follow', scope=follow_scope, confirm=True))
    assert any(item['status'] == 'conflict' for item in result['assignments'])
    if follow_scope == 'all':
        assert any(item['status'] == 'done' for item in result['assignments'])  # master applied, occurrence refused
    occurrence = ctx.store.exceptions_for(event_id)[0]
    assert occurrence['sync_state'] == 'conflict'
    assert choose_provider(ctx, occurrence['id'])['status'] == 'resolved'
    assert ctx.store.exceptions_for(event_id)[0]['link_state'] == links.DIVERGED
    assert ctx.store.get_event(event_id)['link_state'] == links.DIVERGED
    assert json.loads(tools.cal_update(ctx, id=f'{source_id}@{key}', title='Later source occurrence', confirm=True))['status'] == 'updated'
    assert google.events[remote_id]['summary'] == 'Copy occurrence'
    if after_choice == 'preserve':
        assert json.loads(tools.cal_delete(ctx, id=source_id, scope='all', confirm=True))['status'] == 'deleted'
        assert ctx.store.get_event(event_id)['deleted_at'] is None
        assert google.events[projection['external_id']].get('status') != 'cancelled'
        assert google.events[remote_id]['summary'] == 'Copy occurrence'
    else:
        assert json.loads(tools.cal_update(ctx, id=address, link_action='follow', scope='this', confirm=True))['status'] == 'ok'
        assert ctx.store.get_event(event_id)['link_state'] == links.FOLLOWING
        assert google.events[remote_id]['summary'] == 'Later source occurrence'
        tools.cal_update(ctx, id=f'{source_id}@{key}', title='New source occurrence', confirm=True)
        assert google.events[remote_id]['summary'] == 'New source occurrence'


def test_successful_occurrence_follow_still_receives_source_edits(linked_world):
    ctx, cid, caldav, google, left, source_id, projection = linked_world
    event_id = projection['id']
    remote_id, address = instance(ctx, google, event_id, 3)
    key = address.split('@', 1)[1]
    tools.cal_update(ctx, id=address, title='Copy occurrence', confirm=True)
    assert json.loads(tools.cal_update(ctx, id=address, link_action='follow', scope='this', confirm=True))['status'] == 'ok'
    assert ctx.store.get_event(event_id)['link_state'] == links.FOLLOWING
    tools.cal_update(ctx, id=f'{source_id}@{key}', title='Later source occurrence', confirm=True)
    assert google.events[remote_id]['summary'] == 'Later source occurrence'


@pytest.mark.parametrize('new_default', [True, False])
def test_reconnect_preserves_the_latest_explicit_default(tmp_path, new_default):
    ctx = make_context()
    ctx.store = Store(str(tmp_path))
    google_calendar = add_google_calendar(ctx.store)
    other = add_local_calendar(ctx.store, 'other', 'full')
    google = FakeGoogle()
    adapter = google.attach(GoogleAdapter('owner@example.test', 'token', 'refresh',
                                          now_utc()+timedelta(days=1), 'client', '', None))
    request = adapter._request
    def calendar_list(method, path, *args, **kwargs):
        if path == '/users/me/calendarList':
            return 200, {}, {'items': [{'id': 'primary', 'summary': 'Google', 'accessRole': 'owner'}]}
        return request(method, path, *args, **kwargs)
    adapter._request = calendar_list
    tools.cal_settings(ctx, action='set_calendar', calendar_id=google_calendar, default=True, confirm=True)
    tools.cal_settings(ctx, action='disconnect', calendar_id='google:owner@example.test', confirm=True)
    assert ctx.store.default_calendar() is None
    if new_default:
        tools.cal_settings(ctx, action='set_calendar', calendar_id=other, default=True, confirm=True)
    ctx.store.upsert_account({'id': 'google:owner@example.test', 'provider': 'google',
                              'login': 'owner@example.test', 'alias': 'g', 'status': 'connected'})
    ctx.providers = SimpleNamespace(adapter_for=lambda account: adapter, errors={})
    sync_account(ctx.store, ctx.providers, ctx.store.get_account('google:owner@example.test'), ctx.tz)
    expected = other if new_default else google_calendar
    assert ctx.store.default_calendar()['id'] == expected
    created = json.loads(tools.cal_create(ctx, title='Chosen destination', start=_day(4).isoformat(), confirm=True))
    assert created['status'] == 'created', created
    assert created['event']['calendar_id'] == expected
