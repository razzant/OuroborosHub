"""Linked copies retain temporal meaning through birth, source updates and explicit follow."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from test_calendar_temporal_roundtrip import floating_context
from test_calendar_provider_divergence import FakeCalDAV, FakeGoogle, add_google_calendar, run_open_intents
from test_calendar_round2 import OneAdapterProviders, add_external_calendar, add_local_calendar
from test_sync_atomicity import FeedAdapter
import links
import model
import ops
import providers
import providers_google
import tools
from scripts.worker import reconcile_calendar
from store import Store


def linked_context(tmp_path, representation, target, mode, *, override=False):
    ctx, source, _ = floating_context(tmp_path, representation)
    server = FakeCalDAV()
    server.resources[source['href']] = (source['raw_payload'], source['etag'])
    ctx.providers = OneAdapterProviders(server.attach(providers.YandexAdapter('fixture', 'fixture')))
    if override:
        key = model.iso_utc(datetime(2026, 10, 25, 0 if representation == 'date' else 9, tzinfo=ctx.tz))
        if representation == 'utc':
            key = '2026-10-25T07:00:00+00:00'
        changes = {'start': '2026-10-26', 'end': '2026-10-27'} if representation == 'date' else {
            'start': '2026-10-25T12:00:00+01:00', 'end': '2026-10-25T13:00:00+01:00'}
        edited = json.loads(tools.cal_update(ctx, id=source['id']+'@'+key, title='Override', confirm=True, **changes))
        assert edited['status'] == 'updated', edited
    if target == 'google':
        source_adapter = ctx.providers.adapter_for(source['account_id'])
        cid = add_google_calendar(ctx.store)
        ctx.store.update_calendar(cid, {'publish_mode': mode})
        server = FakeGoogle()
        adapter = server.attach(providers_google.GoogleAdapter('fixture', 'token', 'refresh', model.now_utc(), 'client', '', None))
        ctx.providers = SimpleNamespace(adapter_for=lambda aid: source_adapter if aid == source['account_id'] else adapter)
    else:
        cid = add_local_calendar(ctx.store, 'copy', mode) if target == 'local' else add_external_calendar(ctx.store, 'copy', mode)
    result = json.loads(tools.cal_update(ctx, id=source['id'], calendars=[source['calendar_id'], cid], scope='all', confirm=True))
    assert result['status'] == 'updated', result
    run_open_intents(ctx, 2)  # Overrides wait for the new master's provider identity.
    copied = ctx.store.get_event(result['reassign']['added'][0]['event_id'])
    return ctx, ctx.store.get_event(source['id']), copied, server


def assert_visible_times_match(ctx, source, copy):
    for zone in ('Europe/Berlin', 'America/New_York'):
        ctx.tz = model.get_tz(zone)
        result = json.loads(tools.cal_events(ctx, start='2026-10-24', end='2026-10-29'))
        times = {cid: sorted((row['start'], row['end']) for row in result['events'] if row['calendar_id'] == cid)
                 for cid in (source['calendar_id'], copy['calendar_id'])}
        assert times[source['calendar_id']] == times[copy['calendar_id']], (zone, times)


@pytest.mark.parametrize('representation', ['floating', 'tzid', 'date', 'utc'])
@pytest.mark.parametrize('target', ['local', 'caldav'])
@pytest.mark.parametrize('mode', ['full', 'busy'])
def test_copy_master_and_override_keep_time_basis(tmp_path, representation, target, mode):
    ctx, source, copy, server = linked_context(tmp_path, representation, target, mode, override=True)
    source_exc, = ctx.store.exceptions_for(source['id'])
    copy_exc, = ctx.store.exceptions_for(copy['id'])
    for original, duplicate in ((source, copy), (source_exc, copy_exc)):
        assert (duplicate['time_kind'], duplicate['tz']) == (original['time_kind'], original['tz'])
        assert (duplicate['start_utc'], duplicate['end_utc']) == (original['start_utc'], original['end_utc'])
        assert duplicate['uid'] == source['uid'] and duplicate['id'] != original['id']
        assert duplicate['attendees_json'] == duplicate['reminders_json'] == '[]'
        assert duplicate['title'] == (model.BUSY_COPY_TITLE if mode == 'busy' else original['title'])
    assert copy_exc['recurrence_id'] == source_exc['recurrence_id']
    if target == 'caldav':
        wire, etag = server.resources[copy['href']]
        observed = providers.ics_to_rows(wire, copy['calendar_id'], copy['href'], etag, ctx.tz)
        assert {row['time_kind'] for row in observed} == {source['time_kind']}
        assert {row['start_utc'] for row in observed} == {source['start_utc'], source_exc['start_utc']}
    ctx.store = Store(ctx.state_dir)
    assert_visible_times_match(ctx, source, copy)
    start = datetime(2026, 10, 25, tzinfo=ctx.tz)
    end = datetime(2026, 10, 26, tzinfo=ctx.tz)
    combined, _ = ops.busy_intervals(ctx.occurrences(start, end))
    source_only, _ = ops.busy_intervals(ctx.occurrences(start, end, [source['calendar_id']]))
    assert combined == source_only


@pytest.mark.parametrize('scope', ['this', 'all'])
@pytest.mark.parametrize('mode', ['full', 'busy'])
def test_explicit_follow_repairs_existing_copy_basis_without_readdressing(tmp_path, scope, mode):
    ctx, source, copy, _ = linked_context(tmp_path, 'floating', 'local', mode, override=True)
    source_exc, = ctx.store.exceptions_for(source['id'])
    copy_exc, = ctx.store.exceptions_for(copy['id'])
    # Existing copies can predate time_kind propagation; explicit follow must repair them.
    selected = [copy_exc] if scope == 'this' else [copy, copy_exc]
    for row in selected:
        ctx.store.update_event(row['id'], {'time_kind': '', 'link_state': links.DIVERGED})
    addressed = copy['id']+'@'+source_exc['recurrence_id'] if scope == 'this' else copy['id']
    result = json.loads(tools.cal_update(ctx, id=addressed, link_action='follow', scope=scope, confirm=True))
    assert result['status'] == 'ok', result
    for row in (copy, copy_exc):
        current = ctx.store.get_event(row['id'])
        assert current['time_kind'] == 'floating' and current['link_state'] == links.FOLLOWING
        for field in ('id', 'uid', 'master_id', 'recurrence_id', 'start_utc', 'end_utc'):
            assert current[field] == row[field]
    assert_visible_times_match(ctx, source, copy)


@pytest.mark.parametrize('target', ['local', 'caldav', 'google'])
def test_source_feed_kind_change_reaches_copy_and_retains_existing_intents(tmp_path, target):
    ctx, source, copy, _ = linked_context(tmp_path, 'tzid', target, 'full')
    before = ctx.store.intents_for_event(source['id'])
    incoming = {**source, 'time_kind': 'floating', 'etag': '"remote-floating"'}
    feed = FeedAdapter([incoming], seen=[source['href']])
    result = reconcile_calendar(ctx.store, ctx.providers, feed, ctx.store.get_calendar(source['calendar_id']), ctx.tz)
    assert result[2] == 1, result
    assert ctx.store.get_event(copy['id'])['time_kind'] == ('absolute' if target == 'google' else 'floating')
    assert ctx.store.intents_for_event(source['id']) == before
    if target != 'google':
        assert_visible_times_match(ctx, source, copy)


def test_copy_follow_keeps_offline_birth_identity_and_payload(tmp_path):
    ctx, source, _ = floating_context(tmp_path, 'floating')
    cid = add_external_calendar(ctx.store, 'offline', 'busy')
    ctx.providers = OneAdapterProviders(None)
    result = json.loads(tools.cal_update(ctx, id=source['id'], calendars=[source['calendar_id'], cid], scope='all', confirm=True))
    copy = ctx.store.get_event(result['reassign']['added'][0]['event_id'])
    birth, = ctx.store.intents_for_event(copy['id'])
    assert birth['state'] == 'pending'
    ctx.store.update_event(copy['id'], {'time_kind': '', 'link_state': links.DIVERGED})
    json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    current = ctx.store.get_event(copy['id'])
    assert current['time_kind'] == 'floating'
    assert (current['uid'], current['id']) == (copy['uid'], copy['id'])
    retained = next(intent for intent in ctx.store.intents_for_event(copy['id']) if intent['id'] == birth['id'])
    assert retained['payload_json'] == birth['payload_json'] and retained['state'] == birth['state']


@pytest.mark.parametrize('mode', ['full', 'busy'])
def test_google_copy_and_follow_keep_the_actual_zoned_wire(tmp_path, mode):
    ctx, source, copy, server = linked_context(tmp_path, 'floating', 'google', mode)
    ctx.tz = model.get_tz('America/New_York')
    calls = list(server.calls)
    visible = json.loads(tools.cal_events(ctx, start='2026-10-25', end='2026-10-26'))['events']
    times = {event['calendar_id']: event['start'] for event in visible}
    assert times[source['calendar_id']].endswith('09:00:00-04:00')
    assert times[copy['calendar_id']].endswith('04:00:00-04:00')
    assert server.calls == calls  # Owner-zone/display changes never move the provider event.
    assert copy['time_kind'] == 'absolute'
    ctx.store.update_event(copy['id'], {'link_state': links.DIVERGED, 'time_kind': ''})
    result = json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    assert result['status'] == 'ok', result
    adapter = ctx.providers.adapter_for(copy['account_id'])
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(copy['calendar_id']), ctx.tz)
    saved = ctx.store.get_event(copy['id'])
    assert (saved['time_kind'], saved['link_state']) == ('absolute', links.FOLLOWING)
    wire = server.events[saved['external_id']]
    parsed = providers_google.gevent_to_row(wire, copy['calendar_id'], ctx.tz)
    assert parsed['start_utc'] == saved['start_utc'] and parsed['tz'] == saved['tz']


@pytest.mark.parametrize('source_kind', ['floating', 'absolute'])
def test_caldav_follow_changes_master_and_override_wire_kind(tmp_path, source_kind):
    ctx, source, copy, server = linked_context(tmp_path, 'tzid' if source_kind == 'floating' else 'floating', 'caldav', 'full', override=True)
    for row in [source] + ctx.store.exceptions_for(source['id']):
        ctx.store.update_event(row['id'], {'time_kind': source_kind})
    ctx.store.update_event(copy['id'], {'link_state': links.DIVERGED})
    result = json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    assert result['status'] == 'ok', result
    run_open_intents(ctx, 2)
    wire, etag = server.resources[copy['href']]
    parsed = providers.ics_to_rows(wire, copy['calendar_id'], copy['href'], etag, ctx.tz)
    assert len(parsed) == 2 and {row['time_kind'] for row in parsed} == {source_kind}
    for row in [copy] + ctx.store.exceptions_for(copy['id']):
        saved = ctx.store.get_event(row['id'])
        assert saved['time_kind'] == source_kind and saved['link_state'] == links.FOLLOWING
    assert_visible_times_match(ctx, source, copy)


@pytest.mark.parametrize('edited', [False, True])
def test_legacy_type_enrichment_alone_does_not_diverge_copy(tmp_path, edited):
    ctx, source, copy, server = linked_context(tmp_path, 'tzid', 'caldav', 'full')
    ctx.store.update_event(copy['id'], {'time_kind': ''})
    if edited:
        server.edit(copy['href'], lambda cal: cal.walk('VEVENT')[0].__setitem__('SUMMARY', 'Remote change'))
    adapter = ctx.providers.adapter_for(copy['account_id'])
    reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(copy['calendar_id']), ctx.tz)
    saved = ctx.store.get_event(copy['id'])
    assert saved['time_kind'] == 'absolute'
    assert saved['link_state'] == (links.DIVERGED if edited else links.FOLLOWING)
    assert ctx.store.get_event(source['id'])['title'] == source['title']
