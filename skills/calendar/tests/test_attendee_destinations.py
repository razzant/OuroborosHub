"""Registered tools expose the exact guest addresses before any provider effect."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_calendar_provider_divergence import google_context
import plugin


def registered(monkeypatch):
    ctx, calendar, server = google_context()
    handlers = {}
    api = SimpleNamespace(
        register_tool=lambda **kw: handlers.update({kw['name']: kw['handler']}),
        register_route=lambda *a, **kw: None, register_ui_tab=lambda *a, **kw: None,
        register_companion_process=lambda *a, **kw: None)
    monkeypatch.setattr(plugin.tools, 'Context', lambda api: ctx)
    plugin.register(api)
    return ctx, calendar, server, handlers


def create_args(calendar, attendees):
    return dict(title='Guests', start='2026-12-10T09:00Z', calendars=calendar,
                attendees=attendees, send_invites=True, confirm=True)


@pytest.mark.parametrize('encoded', [False, True])
def test_rich_attendees_prepare_exact_retry_without_effect(monkeypatch, encoded):
    ctx, calendar, server, handlers = registered(monkeypatch)
    people = [{'email': 'guest@example.test', 'name': 'Guest', 'role': 'optional'}]
    original = create_args(calendar, json.dumps(people) if encoded else people)
    result = json.loads(handlers['cal_create'](**original))
    assert result['status'] == 'needs_arguments'
    assert server.calls == []
    with ctx.store._conn() as connection:
        assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
    retry = result['retry_arguments']
    assert retry == {**original, 'attendee_addresses': ['guest@example.test']}
    created = json.loads(handlers['cal_create'](**retry))
    assert created['status'] == 'created'
    assert server.live()[0]['attendees'] == [{'email': 'guest@example.test', 'displayName': 'Guest', 'optional': True}]
    assert next(c for c in server.calls if c[0] == 'POST')[2]['sendUpdates'] == 'all'


def test_scalar_guest_list_stays_one_call_and_sparse_edit_preserves_metadata(monkeypatch):
    _, calendar, server, handlers = registered(monkeypatch)
    created = json.loads(handlers['cal_create'](**create_args(calendar, ['guest@example.test'])))
    assert created['status'] == 'created'
    native = server.live()[0]
    native['attendees'][0].update(displayName='Provider name', responseStatus='accepted')
    result = json.loads(handlers['cal_update'](id=created['event']['id'], title='Renamed', confirm=True))
    assert result['status'] == 'updated'
    assert native['attendees'] == [{'email': 'guest@example.test', 'displayName': 'Provider name',
                                    'responseStatus': 'accepted'}]


def test_update_mismatched_destinations_do_not_write_and_retry_keeps_metadata(monkeypatch):
    _, calendar, server, handlers = registered(monkeypatch)
    created = json.loads(handlers['cal_create'](**create_args(calendar, [])))
    people = [{'email': 'new@example.test', 'role': 'optional'}]
    args = dict(id=created['event']['id'], attendees=people, attendee_addresses=['wrong@example.test'],
                send_updates=True, confirm=True)
    before = list(server.calls)
    result = json.loads(handlers['cal_update'](**args))
    assert result['status'] == 'needs_arguments'
    assert server.calls == before
    retry = result['retry_arguments']
    assert retry == {**args, 'attendee_addresses': ['new@example.test']}
    updated = json.loads(handlers['cal_update'](**retry))
    assert updated['status'] == 'updated'
    assert server.live()[0]['attendees'] == [{'email': 'new@example.test', 'optional': True}]
    assert next(c for c in reversed(server.calls) if c[0] == 'PATCH')[2]['sendUpdates'] == 'all'


def test_manifest_projection_sees_prepared_real_recipients(monkeypatch):
    core = os.environ.get('OUROBOROS_SOURCE_DIR') or os.environ.get('OUROBOROS_THEME_CORE_ROOT')
    if not core:
        pytest.skip('set OUROBOROS_SOURCE_DIR for the real Host target projection')
    spec = importlib.util.spec_from_file_location('calendar_host_effects', Path(core) / 'ouroboros/tool_effects.py')
    effects = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, effects)
    spec.loader.exec_module(effects)
    import yaml
    manifest = yaml.safe_load((Path(plugin.__file__).parent / 'SKILL.md').read_text(encoding='utf-8').split('---', 2)[1])
    declarations = {row['name']: row for row in manifest['tools']}
    _, calendar, _, handlers = registered(monkeypatch)
    args = create_args(calendar, [{'email': 'guest@example.test', 'role': 'optional'}])
    prepared = json.loads(handlers['cal_create'](**args))['retry_arguments']
    for name in ('cal_create', 'cal_update'):
        effect = effects.ToolEffect('write', targets=tuple(declarations[name]['target']), declared=True)
        assert effects.target_values(effect, prepared)['attendee_addresses'] == ['guest@example.test']
        import jsonschema
        jsonschema.validate(prepared, plugin.SCHEMAS['cal_create'])
