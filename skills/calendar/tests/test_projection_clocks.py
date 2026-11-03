"""Derived copies retain their projection clock independently of source-role changes."""
import json
from types import SimpleNamespace

import pytest

import model
import providers
import providers_google
import tools
from test_calendar_temporal_roundtrip import context
from test_calendar_provider_divergence import FakeCalDAV, FakeGoogle, add_google_calendar, run_open_intents
from test_recurrence_until_semantics import native_context


def copy_context(tmp_path, target, mode, creation='together'):
    ctx = context(tmp_path, 'Europe/Berlin')
    if target == 'google':
        cid = add_google_calendar(ctx.store)
        server = FakeGoogle()
        adapter = server.attach(providers_google.GoogleAdapter('fixture', 'token', 'refresh', model.now_utc(), 'client', '', None))
    elif target == 'caldav':
        cid = 'caldav:copy:calendar'
        ctx.store.upsert_account({'id': 'caldav:copy', 'provider': 'caldav'})
        ctx.store.upsert_calendar({'id': cid, 'account_id': 'caldav:copy', 'provider': 'caldav',
                                  'href': 'https://calendar.example.test/copy/', 'name': 'Copy'})
        server = FakeCalDAV()
        adapter = server.attach(providers.CalDAVAdapter('fixture', 'fixture', 'https://calendar.example.test'))
    else:
        cid = 'local:copy'
        ctx.store.upsert_calendar({'id': cid, 'account_id': 'local', 'provider': 'local', 'name': 'Copy'})
        server, adapter = SimpleNamespace(calls=[]), None
    ctx.store.update_calendar(cid, {'publish_mode': mode})
    ctx.providers = SimpleNamespace(errors={}, adapter_for=lambda _: adapter)
    selected = ['local:personal', cid] if creation == 'together' else ['local:personal']
    created = json.loads(tools.cal_create(ctx, title='Routine', start='2026-10-24T09:00', end='2026-10-24T10:00',
                                        availability='soft', rrule='FREQ=DAILY;COUNT=4', calendars=selected, confirm=True))
    assert created['status'] == 'created', created
    source = ctx.store.get_event(created['event']['id'])
    if creation == 'added':
        added = json.loads(tools.cal_update(ctx, id=source['id'], calendars=['local:personal', cid], scope='all', confirm=True))
        assert added['status'] == 'updated', added
    copy = next(row for row in ctx.store.group_masters(ctx.store.get_event(source['id'])['link_group_id']) if row['calendar_id'] == cid)
    return ctx, source, copy, server


def shown(ctx):
    return {row['calendar_id']: row['start'] for row in json.loads(tools.cal_events(ctx, start='2026-10-25', end='2026-10-26'))['events']}


@pytest.mark.parametrize('target', ['google', 'caldav', 'local'])
@pytest.mark.parametrize('mode', ['busy', 'full'])
@pytest.mark.parametrize('creation', ['together', 'added'])
def test_soft_copy_uses_its_own_native_clock_after_travel(tmp_path, target, mode, creation):
    ctx, source, copy, server = copy_context(tmp_path, target, mode, creation)
    assert source['origin'] == 'local' and copy['origin'] == 'projection'
    # Native-kind enrichment of an authored local routine does not turn it into a copy.
    ctx.store.update_event(source['id'], {'time_kind': 'absolute'})
    ctx.tz = model.get_tz('America/New_York')
    calls = list(server.calls)
    times = shown(ctx)
    assert times[source['calendar_id']].endswith('09:00:00-04:00')
    assert times[copy['calendar_id']].endswith('04:00:00-04:00' if target == 'google' else '09:00:00-04:00')
    assert server.calls == calls


def test_source_selection_and_detach_do_not_reinterpret_a_google_projection(tmp_path):
    ctx, source, copy, server = copy_context(tmp_path, 'google', 'full')
    ctx.tz = model.get_tz('America/New_York')
    before = shown(ctx)[copy['calendar_id']]
    calls = list(server.calls)
    for action in ('source', 'detach'):
        result = json.loads(tools.cal_update(ctx, id=copy['id'], link_action=action, scope='all', confirm=True))
        assert result['status'] == 'ok', result
        assert shown(ctx)[copy['calendar_id']] == before
        assert ctx.store.get_event(copy['id'])['origin'] == 'projection'
    assert server.calls == calls


def test_explicit_follow_repairs_legacy_copy_provenance_without_changing_source_clock(tmp_path):
    ctx, source, copy, _ = copy_context(tmp_path, 'google', 'full')
    ctx.store.update_event(copy['id'], {'origin': 'local'})  # older releases did not record derived provenance
    ctx.tz = model.get_tz('America/New_York')
    assert shown(ctx)[copy['calendar_id']].endswith('09:00:00-04:00')
    repaired = json.loads(tools.cal_update(ctx, id=copy['id'], link_action='follow', scope='all', confirm=True))
    assert repaired['status'] == 'ok', repaired
    run_open_intents(ctx, 2)
    assert shown(ctx)[copy['calendar_id']].endswith('04:00:00-04:00')
    assert shown(ctx)[source['calendar_id']].endswith('09:00:00-04:00')
    assert ctx.store.get_event(source['id'])['origin'] == 'local'


def test_source_softness_changes_update_local_projection_clock(tmp_path):
    ctx, source, copy, _ = copy_context(tmp_path, 'local', 'full')
    ctx.tz = model.get_tz('America/New_York')
    changed = json.loads(tools.cal_update(ctx, id=source['id'], availability='busy', scope='all', confirm=True))
    assert changed['status'] == 'updated', changed
    assert shown(ctx)[source['calendar_id']] == shown(ctx)[copy['calendar_id']]
    assert shown(ctx)[copy['calendar_id']].endswith('04:00:00-04:00')


def test_external_soft_absolute_source_stays_fixed_through_follow_and_propagation(tmp_path):
    ctx, source, _ = native_context(tmp_path, 'tzid')
    ctx.store.upsert_calendar({'id': 'local:copy', 'account_id': 'local', 'provider': 'local', 'name': 'Copy', 'publish_mode': 'full'})
    changed = json.loads(tools.cal_update(ctx, id=source['id'], availability='soft', calendars=[source['calendar_id'], 'local:copy'], scope='all', confirm=True))
    assert changed['status'] == 'updated', changed
    copy = ctx.store.get_event(changed['reassign']['added'][0]['event_id'])
    ctx.tz = model.get_tz('America/New_York')
    for change in ({'title': 'Renamed'}, {'link_action': 'follow'}):
        eid = copy['id'] if 'link_action' in change else source['id']
        result = json.loads(tools.cal_update(ctx, id=eid, scope='all', confirm=True, **change))
        assert result['status'] in ('ok', 'updated'), result
        assert shown(ctx)[source['calendar_id']].endswith('04:00:00-04:00')
        assert shown(ctx)[copy['calendar_id']] == shown(ctx)[source['calendar_id']]
