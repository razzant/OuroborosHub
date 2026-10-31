"""Account onboarding and collection operations through the real skill boundaries."""
from __future__ import annotations
import asyncio
import base64
import json
import os
import shutil
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_calendar_round2 import FakeAPI, OneAdapterProviders, Request, RouteAPI
import accounts
import credentials
import management
import ops
import providers
import providers_google as google
import routes
import tools
from model import iso_utc, now_utc


class Response:
    status = 200
    headers = {}
    def __init__(self, data=None): self.data = data or {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.data).encode()


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI({'GOOGLE_OAUTH_CLIENT_ID': 'new-client', 'GOOGLE_OAUTH_CLIENT_SECRET': 'new-secret'})
        self.ctx = tools.Context(self.api)
        self.addCleanup(shutil.rmtree, self.ctx.state_dir)

    def finish(self, email='owner@example.test', subject='subject-one'):
        started = google.start_auth(self.ctx.store, self.ctx.secrets, 'http://127.0.0.1:8765/onboarding/static/provider-return.html')
        def identity(adapter):
            adapter._identity = {'email': email, 'sub': subject}
            return email
        with patch.object(google, '_token_request', return_value={'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
                                                                'expires_in': 3600, 'scope': ' '.join(google.SCOPES)}), \
             patch.object(google.GoogleAdapter, 'primary_email', identity):
            return started, google.finish_auth(self.ctx.store, self.ctx.secrets, self.ctx.state_dir, 'code', started['state'])

    def test_fresh_vault_generates_private_key_and_oauth_state_is_single_use(self):
        started, done = self.finish()
        state = json.loads(base64.urlsafe_b64decode(started['state'] + '=' * (-len(started['state']) % 4)))
        self.assertEqual((state['v'], state['skill']), (1, 'calendar'))
        self.assertTrue(state['nonce'])
        self.assertEqual(done['email'], 'owner@example.test')
        key = Path(self.ctx.state_dir, credentials.KEY_FILE)
        if os.name != 'nt':
            self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        self.assertNotIn(b'synthetic-access', Path(self.ctx.state_dir, credentials.TOKENS_FILE).read_bytes())
        with patch.object(google, '_token_request') as exchange:
            with self.assertRaises(ops.ProviderError):
                google.finish_auth(self.ctx.store, self.ctx.secrets, self.ctx.state_dir, 'code', started['state'])
            exchange.assert_not_called()

    def test_google_identity_comes_from_verified_userinfo_without_calendar_list(self):
        adapter = google.GoogleAdapter('pending', 'synthetic', '', now_utc() + timedelta(hours=1), 'client', '', None)
        with patch.object(adapter, '_request', side_effect=AssertionError('Calendar list is not identity')), \
             patch.object(google.urllib.request, 'urlopen', return_value=Response({'sub': '123', 'email': 'owner@example.test', 'email_verified': True})) as request:
            self.assertEqual(adapter.primary_email(), 'owner@example.test')
            self.assertEqual(adapter._identity['sub'], '123')
            self.assertEqual(request.call_args.args[0].full_url, google.USERINFO_URL)
        with patch.object(google.urllib.request, 'urlopen', return_value=Response({'sub': '123', 'email': 'other@example.test', 'email_verified': False})):
            with self.assertRaises(ops.ProviderError): adapter.primary_email()

    def test_subject_preserves_canonical_key_when_email_changes(self):
        _, first = self.finish()
        _, renamed = self.finish('renamed@example.test')
        self.assertEqual(first['account_id'], renamed['account_id'])
        vault = credentials.load_tokens(self.ctx.state_dir, '')
        self.assertEqual(len(vault), 1)
        self.assertEqual(vault['owner@example.test']['email'], 'renamed@example.test')
        adapter = google.GoogleAdapter.for_account(self.ctx.secrets, self.ctx.state_dir, 'owner@example.test')
        self.assertEqual((adapter.account_id, adapter.email), (first['account_id'], 'renamed@example.test'))
        _, reused_email = self.finish('owner@example.test', 'different-subject')
        self.assertNotEqual(first['account_id'], reused_email['account_id'])

    def test_legacy_client_and_vault_remain_bound_after_shared_default_is_added(self):
        legacy = {**self.ctx.secrets, 'GOOGLE_CALENDAR_CLIENT_ID': 'legacy-client', 'GOOGLE_CALENDAR_CLIENT_SECRET': 'legacy-secret',
                  'CALENDAR_TOKEN_KEY': 'legacy-passphrase'}
        credentials.save_tokens(self.ctx.state_dir, 'legacy-passphrase', {'old@example.test': {
            'access_token': 'old', 'refresh_token': 'old-refresh', 'expires_at': iso_utc(now_utc() + timedelta(hours=1))}})
        adapter = google.GoogleAdapter.for_account(legacy, self.ctx.state_dir, 'old@example.test')
        self.assertEqual((adapter.client_id, adapter.client_secret), ('legacy-client', 'legacy-secret'))
        with self.assertRaises(ops.ProviderError): credentials.load_tokens(self.ctx.state_dir, '')
        self.assertFalse(Path(self.ctx.state_dir, credentials.KEY_FILE).exists())
        begun = google.start_auth(self.ctx.store, legacy, 'https://calendar.example.test/return')
        self.assertIn('client_id=new-client', begun['auth_url'])

    def test_denied_consent_closes_flow_and_remote_prefix_is_preserved(self):
        api = RouteAPI()
        routes.register_routes(api, lambda: self.ctx)
        with patch.object(accounts.urllib.request, 'urlopen', return_value=Response()):
            reply = asyncio.run(api.routes['oauth/begin'](Request({'public_base_url': 'https://host.example.test/my-ouro'})))
        begun = json.loads(reply.body)
        self.assertEqual(begun['redirect_uri'], 'https://host.example.test/my-ouro/onboarding/static/provider-return.html')
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(begun['auth_url']).query)['state'][0]
        denied = asyncio.run(api.routes['oauth/complete'](Request({'state': state, 'error': 'access_denied'})))
        self.assertEqual(json.loads(denied.body)['code'], 'oauth_denied')
        self.assertIsNone(self.ctx.store.get_setting(google.PENDING_PREFIX + state))
        self.assertFalse(Path(self.ctx.state_dir, credentials.TOKENS_FILE).exists())

    def test_caldav_form_uses_same_vault_and_does_not_return_password(self):
        self.finish()
        def discover(adapter):
            return [{'id': adapter.account_id + ':work', 'account_id': adapter.account_id, 'provider': adapter.provider,
                     'external_id': 'work', 'href': adapter.home + 'work/', 'name': 'Work', 'writable': True}]
        with patch.object(providers.CalDAVAdapter, 'list_calendars', discover):
            result = accounts.connect_caldav(self.ctx, login='person', password='private-password', alias='Work', server_url='https://dav.example.test/dav/')
        self.assertEqual(result['status'], 'connected')
        self.assertNotIn('private-password', json.dumps(result))
        vault = credentials.load_tokens(self.ctx.state_dir, '')
        self.assertEqual(vault[result['account_id']]['password'], 'private-password')
        self.assertIn('owner@example.test', vault)
        adapter = providers.Providers(self.ctx.secrets, self.ctx.state_dir).adapter_for(result['account_id'])
        self.assertEqual(adapter.home, 'https://dav.example.test/dav/')
        self.assertNotIn(b'private-password', Path(self.ctx.state_dir, credentials.TOKENS_FILE).read_bytes())

    def test_local_identity_survives_collision_disconnect_and_calendar_mode_update(self):
        from model import DEFAULT_LOCAL_CALENDAR_ID
        original = self.ctx.store.get_calendar(DEFAULT_LOCAL_CALENDAR_ID)
        created = json.loads(tools.cal_settings(self.ctx, action='new_local_calendar', name='Personal', confirm=True))
        self.assertNotEqual(created['calendar']['id'], original['id'])
        self.assertEqual(self.ctx.store.get_calendar(original['id'])['name'], original['name'])
        blocked = json.loads(tools.cal_settings(self.ctx, action='disconnect', calendar_id='local', confirm=True))
        self.assertEqual(blocked['status'], 'error')
        self.assertEqual(len(self.ctx.store.list_calendars()), 2)
        cid = created['calendar']['id']
        changed = json.loads(tools.cal_settings(self.ctx, action='set_calendar', calendar_id=cid, publish_mode='busy', confirm=True))
        self.assertEqual(changed['calendar']['publish_mode'], 'busy')
        changed = json.loads(tools.cal_settings(self.ctx, action='set_calendar', calendar_id=cid, publish_mode='full', confirm=True))
        self.assertEqual(changed['calendar']['publish_mode'], 'full')

    def test_preferences_round_trip_and_invalid_settings_do_not_partially_apply(self):
        value = 'Standing instruction. ' * 1000 + 'KEEP THE LAST SENTENCE'
        saved = json.loads(tools.cal_settings(self.ctx, action='set', preferences=value, confirm=True))
        self.assertEqual(saved['preferences'], value)
        self.assertEqual(json.loads(tools.cal_settings(self.ctx))['preferences'], value)
        invalid = json.loads(tools.cal_settings(self.ctx, action='set', working_hours='08:00-17:00', timezone='No/SuchZone', confirm=True))
        self.assertEqual(invalid['status'], 'error')
        self.assertIsNone(self.ctx.store.get_setting('working_hours'))


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.ctx = tools.Context(FakeAPI())
        self.addCleanup(shutil.rmtree, self.ctx.state_dir)
        self.account = 'google:person@example.test'
        self.ctx.store.upsert_account({'id': self.account, 'provider': 'google', 'login': 'person@example.test'})
        class Adapter:
            calls = []
            failure = None
            def create_calendar(inner, changes):
                inner.calls.append(dict(changes))
                if inner.failure: raise inner.failure
                return {'id': 'google:new', 'account_id': self.account, 'provider': 'google', 'external_id': 'new', 'name': changes['name']}
        self.adapter = Adapter()
        self.ctx.providers = OneAdapterProviders(self.adapter)

    def test_confirm_and_request_id_preserve_once_only_creation(self):
        args = {'action': 'create', 'account_id': self.account, 'changes': {'name': 'New'}, 'request_id': 'request-one'}
        self.assertEqual(json.loads(management.cal_manage(self.ctx, **args))['status'], 'needs_confirm')
        self.assertEqual(self.adapter.calls, [])
        for _ in range(2):
            self.assertEqual(json.loads(management.cal_manage(self.ctx, **args, confirm=True))['status'], 'done')
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(json.loads(management.cal_calendars(self.ctx, request_id='request-one'))['status'], 'done')
        changed = json.loads(management.cal_manage(self.ctx, **{**args, 'changes': {'name': 'Different'}}, confirm=True))
        self.assertEqual(changed['status'], 'request_id_conflict')

    def test_lost_provider_response_stays_uncertain_across_worker_retry(self):
        self.adapter.failure = ops.ProviderError('network', 'response lost after creation')
        args = {'action': 'create', 'account_id': self.account, 'changes': {'name': 'Uncertain'}, 'request_id': 'uncertain-one', 'confirm': True}
        self.assertEqual(json.loads(management.cal_manage(self.ctx, **args))['status'], 'uncertain')
        ops.retry_due_intents(self.ctx.store, self.ctx.providers)
        self.assertEqual(json.loads(management.cal_manage(self.ctx, **args))['status'], 'uncertain')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_deleted_calendar_replays_receipt_and_keeps_history_out_of_active_agenda(self):
        from model import DEFAULT_LOCAL_CALENDAR_ID
        calendar = self.ctx.store.get_calendar(DEFAULT_LOCAL_CALENDAR_ID)
        created = json.loads(tools.cal_create(self.ctx, title='Kept history', start='2026-11-04T09:00:00+00:00', confirm=True))
        event_id = created['event']['id']
        args = {'action': 'delete', 'calendar_id': calendar['id'], 'request_id': 'delete-local', 'confirm': True}
        first = json.loads(management.cal_manage(self.ctx, **args))
        self.assertEqual(first['status'], 'done')
        self.assertTrue(self.ctx.store.get_calendar(calendar['id'])['deleted_at'])
        self.assertIsNotNone(self.ctx.store.get_event(event_id))
        repeat = json.loads(management.cal_manage(self.ctx, **args))
        self.assertEqual(repeat['status'], 'done', repeat)
        self.assertEqual(repeat['result'], first['result'])
        changed = json.loads(tools.cal_update(self.ctx, id=event_id, title='Wrong', confirm=True))
        self.assertEqual(changed['status'], 'failed', changed)
        self.assertEqual(self.ctx.store.get_event(event_id)['title'], 'Kept history')
        new = json.loads(management.cal_manage(self.ctx, action='create', account_id=calendar['account_id'],
                                             changes={'name': 'New local'}, confirm=True))
        self.assertEqual(new['status'], 'done', new)
        new_event = json.loads(tools.cal_create(self.ctx, title='New event', start='2026-11-04T11:00:00+00:00',
                                               calendars=new['result']['id'], confirm=True))
        self.assertEqual(new_event['status'], 'created', new_event)

    def test_google_sharing_notifications_are_explicit(self):
        adapter = google.GoogleAdapter('person@example.test', 'access', '', now_utc() + timedelta(hours=1), 'client', '', None)
        calls = []
        adapter._request = lambda *args, **kwargs: (calls.append((args, kwargs)) or (200, {}, {'id': 'rule'}))
        calendar = {'id': 'google:local', 'external_id': 'calendar@group.example.test'}
        adapter.share_calendar(calendar, {'value': 'guest@example.test', 'role': 'reader'})
        self.assertEqual(calls[-1][0][2]['sendNotifications'], 'false')
        adapter.share_calendar(calendar, {'value': 'guest@example.test', 'role': 'writer', 'send_notifications': True})
        self.assertEqual(calls[-1][0][2]['sendNotifications'], 'true')
        self.assertEqual(calls[-1][0][3]['scope'], {'type': 'user', 'value': 'guest@example.test'})


if __name__ == '__main__': unittest.main()
