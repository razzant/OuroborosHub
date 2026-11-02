"""Public calendar targets, draft custody and honest route projections."""
from __future__ import annotations

import asyncio
import json
import shutil
import types
import unittest
from unittest.mock import patch

from test_calendar_round2 import FakeAPI, OneAdapterProviders, RouteAPI
import management
import accounts
import ops
import routes
import tools


class Request:
    def __init__(self, body=None, method='GET', query=None):
        self.method, self.query_params, self.body = method, query or {}, body or {}
    async def json(self): return self.body


class CalendarStateContracts(unittest.TestCase):
    def setUp(self):
        self.ctx = tools.Context(FakeAPI())
        self.addCleanup(shutil.rmtree, self.ctx.state_dir)
        self.api = RouteAPI()
        routes.register_routes(self.api, lambda: self.ctx)

    def call(self, route, body=None, query=None):
        response = asyncio.run(self.api.routes[route](Request(body, 'POST' if body is not None else 'GET', query)))
        return response.status_code, json.loads(response.body)

    def test_draft_is_single_current_card_and_drops_account_fields(self):
        draft = {'event': {'id': 'one', 'title': 'Original', 'password': 'not-a-card-field'},
                 'inputs': {'title': 'Edited', 'description': 'Long text ' * 2000, 'password': 'not-a-card-field'},
                 'account': {'password': 'not-a-card-field'}}
        status, saved = self.call('draft', {'draft': draft})
        self.assertEqual(status, 200)
        self.assertNotIn('password', json.dumps(saved))
        self.assertEqual(saved['draft']['inputs']['description'], draft['inputs']['description'])
        self.assertEqual(self.call('draft')[1], saved)
        self.call('draft', {'draft': {'event': {'title': 'Second'}, 'inputs': {'title': 'Current'}}})
        self.assertEqual(self.call('draft')[1]['draft']['inputs']['title'], 'Current')
        self.call('draft', {'draft': None})
        self.assertIsNone(self.call('draft')[1]['draft'])

    def test_connection_reports_http_transport_on_success_and_failure(self):
        class Adapter:
            home = 'http://dav.example.test/calendar/'
            def __init__(self, *args, **kwargs): pass
            def list_calendars(self): return []
        with patch.object(accounts, 'CalDAVAdapter', Adapter):
            connected = accounts.connect_caldav(self.ctx, login='owner', password='private-password', server_url=Adapter.home)
            self.assertEqual(connected['status'], 'connected')
            self.assertIn('without encryption', connected['transport_warning'])
            with patch.object(Adapter, 'list_calendars', side_effect=ops.ProviderError('auth', 'Bad credentials')):
                failed = accounts.connect_caldav(self.ctx, login='owner', password='private-password', server_url=Adapter.home)
            self.assertEqual(failed['status'], 'error')
            self.assertIn('without encryption', failed['transport_warning'])
            self.assertNotIn('private-password', json.dumps(failed))

    def test_malformed_draft_does_not_replace_existing(self):
        self.ctx.store.set_setting('widget_draft', {'event': {'title': 'Kept'}, 'inputs': {}})
        self.assertEqual(self.call('draft', {'draft': {'event': 'bad', 'inputs': {}}})[0], 400)
        self.assertEqual(self.call('draft')[1]['draft']['event']['title'], 'Kept')

    def test_rdate_master_routes_require_an_occurrence_or_explicit_whole_series(self):
        for route in ('event/update', 'event/delete'):
            created = json.loads(tools.cal_create(self.ctx, title='Extra dates', start='2026-12-10T09:00Z',
                                                  end='2026-12-10T10:00Z', confirm=True))
            eid = created['event']['id']
            self.ctx.store.update_event(eid, {'rdates': '2026-12-11T09:00:00+00:00'})
            body = {'id': eid, 'title': 'Renamed'}
            self.assertEqual(self.call(route, body)[0], 400)
            self.assertFalse(self.ctx.store.get_event(eid)['deleted_at'])
            self.assertEqual(self.call(route, {**body, 'scope': 'all'})[0], 200)

    def test_public_sharing_targets_and_legacy_migration_are_truthful(self):
        aid, cid = 'google:owner@example.test', 'google:calendar'
        self.ctx.store.upsert_account({'id': aid, 'provider': 'google', 'login': 'owner@example.test'})
        self.ctx.store.upsert_calendar({'id': cid, 'account_id': aid, 'provider': 'google', 'name': 'Work'})
        calls = []
        adapter = types.SimpleNamespace(
            share_calendar=lambda cal, sharing: calls.append(('share', dict(sharing))) or {'id': 'rule'},
            unshare_calendar=lambda cal, sharing: calls.append(('unshare', dict(sharing))) or {'deleted': True})
        self.ctx.providers = OneAdapterProviders(adapter)
        args = {'calendar_id': cid, 'action': 'share', 'sharing': {'type': 'user', 'role': 'reader'}, 'confirm': True}
        legacy = json.loads(management.cal_manage(self.ctx, **{**args, 'sharing': {**args['sharing'], 'value': 'guest@example.test'}}))
        self.assertEqual((legacy['status'], legacy['reason_code'], legacy['argument'], calls), ('error', 'argument_required', 'grantee', []))
        conflict = json.loads(management.cal_manage(self.ctx, **{**args, 'sharing': {'value': 'other@example.test'}}, grantee='guest@example.test'))
        self.assertEqual((conflict['status'], calls), ('error', []))
        first = json.loads(management.cal_manage(self.ctx, **args, grantee='guest@example.test', request_id='share-one'))
        repeat = json.loads(management.cal_manage(self.ctx, **args, grantee='guest@example.test', request_id='share-one'))
        self.assertEqual((first['status'], repeat['status'], len(calls)), ('done', 'done', 1))
        self.assertEqual(calls[0][1]['value'], 'guest@example.test')
        legacy = json.loads(management.cal_manage(self.ctx, action='unshare', calendar_id=cid, sharing={'rule_id': 'rule'}, confirm=True))
        self.assertEqual(legacy['argument'], 'rule_id')
        final = json.loads(management.cal_manage(self.ctx, action='unshare', calendar_id=cid, rule_id='rule', confirm=True))
        self.assertEqual(final['status'], 'done')
        self.assertEqual(calls[-1], ('unshare', {'rule_id': 'rule'}))

    def test_agenda_reports_failed_series_without_hiding_good_data(self):
        observed = {}
        def occurrences(start, end, cals, **kwargs):
            observed.update(kwargs)
            kwargs['failed']['series-1'] = 'Cannot expand recurrence'
            return []
        with patch.object(self.ctx, 'occurrences', side_effect=occurrences):
            status, result = self.call('agenda', query={'date': '2026-11-01'})
        self.assertEqual(status, 200)
        self.assertTrue(observed['strict'])
        self.assertEqual(result['unexpanded_series'], {'series-1': 'Cannot expand recurrence'})
        self.assertFalse(result['events_complete'])
        self.assertEqual(result['events'], [])

    def test_routes_translate_explicit_invalid_time_errors(self):
        with patch.object(routes, 'parse_input', side_effect=ValueError('Invalid date: bad')):
            self.assertEqual(self.call('agenda', query={'date': 'bad'})[0], 400)
        with patch.object(tools, '_spec_from_args', side_effect=ValueError('Invalid start: bad')):
            status, result = self.call('event', {'title': 'Meeting', 'start': 'bad'})
            self.assertEqual(status, 400)
            self.assertIn('Invalid start', result['message'])


if __name__ == '__main__': unittest.main()
