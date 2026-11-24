"""Calendar against a fake Ouroboros sign-in service.

A loopback Host Service stands in for the core: ``/identity`` with or without ``sign_in_version``, the
accounts it holds for Calendar, short-lived tokens and the one-time handover.
A loopback Google Calendar API checks the bearer it gets. Google's token endpoint is recorded: on the host
path the skill must never refresh an account Ouroboros holds. Synthetic tokens only, no network.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
for path in (str(HERE.parent), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import Request, RouteAPI  # noqa: E402  (also stubs starlette when it is absent)
import accounts  # noqa: E402
import google_host  # noqa: E402
import host_sign_in  # noqa: E402
import ops  # noqa: E402
import providers_google as gp  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
import yaml  # noqa: E402
from model import get_tz, iso_utc, now_utc  # noqa: E402
from providers import Providers  # noqa: E402
from scripts import worker  # noqa: E402
from store import Store  # noqa: E402

SKILL_TOKEN = 'synthetic-skill-token'
VAULT_KEY = ''   # the generated local key file: no PBKDF2 on every vault read
CLIENT_SECRET = 'synthetic-client-secret'
SECRETS = {'CALENDAR_TOKEN_KEY': VAULT_KEY, 'GOOGLE_OAUTH_CLIENT_ID': 'personal-client', 'GOOGLE_OAUTH_CLIENT_SECRET': CLIENT_SECRET}
LEGACY_REFRESH, LEGACY_ACCESS = 'legacy-refresh-token-1', 'legacy-access-token-1'
OWNER = 'owner@example.test'
UTC = get_tz('UTC')
SCOPE = ' '.join(gp.SCOPES)
NOTE = 'If your Google Cloud app is in Testing, Google ends sign-ins after 7 days.'
HINT = ("SIGN_IN_REQUIRED: calendar needs the owner's sign-in to Google for owner@example.test (refresh_rejected). "
        "The owner can press Sign in again on its card in Extensions › Skills.")
SECRET_SHAPES = re.compile(r'legacy-refresh|legacy-access|host-access-|late-refresh|late-access|synthetic-client-secret|' + SKILL_TOKEN)


def host_key(subject):
    return 'a-' + hashlib.sha256(f'https://accounts.google.com\0{subject}'.encode()).hexdigest()[:20]


def legacy_entry(email, subject, refresh=LEGACY_REFRESH):
    return {'access_token': LEGACY_ACCESS, 'refresh_token': refresh, 'expires_at': iso_utc(now_utc() - timedelta(minutes=5)),
            'scope': SCOPE, 'email': email, 'subject': subject, 'provider': 'google', 'client_id': 'personal-client',
            'client_secret': CLIENT_SECRET, 'generation': 'g-' + subject}


class _Server:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.server = None


class FakeHost(_Server):
    """The core's Host Service sign-in routes for the Calendar skill's token."""

    def __init__(self, sign_in_version=1):
        self.sign_in_version, self.accounts, self.refuse, self.requests = sign_in_version, {}, {}, []
        self.issued, self.conn_seq, self.down, self.declared = 0, 0, False, True
        self.emit_connection_id, self.handover_exchanges = True, 0
        self.handover_attempts, self.handover_slots, self.handover_http_error, self.handover_raw = {}, {}, None, None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_HEAD(self):   # the old path's probe of the host's return page
                self.send_response(200 if self.path == google_host.RETURN_PAGE else 404)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                self.route('GET')

            def do_POST(self):
                self.route('POST')

            def route(self, method):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                fake.requests.append((method, self.path, body))
                if self.headers.get('X-Skill-Token') != SKILL_TOKEN:
                    return self.answer(403, {'ok': False, 'error': 'skill token refused'})
                if self.path == '/identity':
                    version = {'sign_in_version': fake.sign_in_version} if fake.sign_in_version else {}
                    return self.answer(200, {'ok': True, 'name': 'Ouroboros', 'notify_version': 1, **version})
                if not fake.sign_in_version:
                    return self.answer(404, {'ok': False, 'error': 'Not Found'})
                if (method, self.path) == ('GET', '/sign-in/accounts'):
                    rows = []
                    for key, a in fake.accounts.items():
                        conn = fake._connection_of(a)
                        rows.append({'key': key, 'label': a['label'], 'subject': a['subject'], 'verified': a.get('verified', True),
                                     'state': a['state'], 'scope': SCOPE, 'client_label': 'Personal',
                                     **({'via': a['via']} if a.get('via') is not None else {}),
                                     **({'legacy_generation': a['legacy_generation']} if a.get('legacy_generation') else {}),
                                     **({'connection_id': conn} if conn else {})})
                    return self.answer(200, {'ok': True, 'services': [{'id': 'google', 'label': 'Google', 'accounts': rows}] if fake.declared else []})
                if (method, self.path) == ('POST', '/sign-in/token'):
                    return self.answer(*fake.token(body))
                if (method, self.path) == ('POST', '/sign-in/handover'):
                    if fake.handover_raw is not None:
                        raw, fake.handover_raw = fake.handover_raw, None
                        return self.answer(200, raw)
                    if fake.handover_http_error is not None:
                        err, fake.handover_http_error = fake.handover_http_error, None
                        return self.answer(*err)
                    return self.answer(200, fake.handover(body))
                return self.answer(404, {'ok': False, 'error': 'Not Found'})

        super().__init__(Handler)

    def _connection_of(self, account):
        # Matches real core service_sign_in._connection: only verified accounts carry connection_id.
        if self.emit_connection_id and account.get('verified', True) is True:
            return account.get('connection_id') or ''
        return ''

    def add(self, label, subject, state='signed_in', refresh='card-refresh', *, verified=True, via='card',
            legacy_generation=None, key=None):
        key = key or host_key(subject)
        existing = self.accounts.get(key)
        if existing and existing.get('connection_id'):
            connection_id = existing['connection_id']
            if legacy_generation is None:
                legacy_generation = existing.get('legacy_generation')
        elif verified:
            self.conn_seq += 1
            connection_id = f'conn-{self.conn_seq}'
        else:
            connection_id = ''
        self.accounts[key] = {'label': label, 'subject': subject, 'verified': verified, 'state': state,
                              'refresh': refresh, 'via': via, 'legacy_generation': legacy_generation,
                              'connection_id': connection_id}
        return key

    def token(self, body):
        key = body.get('account')
        account = self.accounts.get(key)
        if body.get('service') != 'google' or account is None:
            return 404, {'ok': False, 'error': 'unknown account', 'reason': 'unknown_account'}
        expected_conn = body.get('connection_id') or ''
        current_conn = self._connection_of(account)
        if expected_conn and current_conn != expected_conn:
            return 409, {'ok': False, 'code': 'SIGN_IN_REQUIRED', 'reason': 'connection_changed', 'service': 'google',
                         'account': key, 'note': '',
                         'hint': 'SIGN_IN_REQUIRED: this Google sign-in was disconnected or replaced by a new connection.'}
        if self.down:
            return 503, {'ok': False, 'code': 'SIGN_IN_UNAVAILABLE', 'reason': 'provider_unreachable'}
        if account['state'] != 'signed_in':
            return 409, {'ok': False, 'code': 'SIGN_IN_REQUIRED', 'reason': 'refresh_rejected', 'service': 'google',
                         'account': key, 'note': NOTE, 'hint': HINT}
        if not account.get('access') or body.get('rejected') == account['access']:   # one refresh only for the token it holds
            self.issued += 1
            account['access'] = f'host-access-{self.issued}'
        return 200, {'ok': True, 'access_token': account['access'], 'token_type': 'Bearer', 'expires_at': time.time() + 3600,
                     'scope': SCOPE, 'account': {'key': key, 'label': account['label']},
                     **({'connection_id': current_conn} if current_conn else {})}

    def handover(self, body):
        recover = body.get('recover') is True
        items = []
        for grant in body.get('grants') or []:
            legacy_key = grant['legacy_key']
            generation = grant.get('legacy_generation')
            if legacy_key in self.refuse:
                spec = self.refuse[legacy_key]
                if isinstance(spec, dict):
                    if spec.get('record_attempt'):
                        self.handover_exchanges += 1
                        self.handover_attempts[legacy_key] = {'state': spec['record_attempt'], 'grant': grant}
                    items.append({'legacy_key': legacy_key, 'result': 'refused', 'reason': spec['reason'],
                                  **({'exchange': spec['exchange']} if 'exchange' in spec else {}),
                                  **({'legacy_generation': generation} if generation else {})})
                else:
                    items.append({'legacy_key': legacy_key, 'result': 'refused', 'reason': spec, 'exchange': 'none',
                                  **({'legacy_generation': generation} if generation else {})})
                continue
            subject = grant.get('subject') or ''
            verified = bool(subject)
            key = host_key(subject) if verified else host_key('legacy:' + legacy_key)
            held = self.accounts.get(key)
            if 'previous_legacy_generation' in grant and grant.get('connection_id') and held and self._connection_of(held) != grant['connection_id']:
                items.append({'legacy_key': legacy_key, 'result': 'refused', 'reason': 'connection_changed', 'exchange': 'none',
                              **({'legacy_generation': generation} if generation else {})})
                continue
            attempt, slot = self.handover_attempts.get(legacy_key), self.handover_slots.get(legacy_key)
            if recover:
                if slot and (generation is None or slot.get('legacy_generation') == generation):
                    filed = self.accounts.get(slot['key'])
                    if filed is None:
                        items.append({'legacy_key': legacy_key, 'result': 'refused', 'reason': 'account_disconnected',
                                      **({'legacy_generation': generation} if generation else {})})
                        continue
                    conn = self._connection_of(filed)
                    items.append({'legacy_key': legacy_key,
                                  'result': 'already_present' if filed.get('via') == 'handover' else 'kept_newer',
                                  'account': {'key': slot['key'], 'label': filed['label']}, 'state': filed['state'],
                                  **({'via': filed['via']} if filed.get('via') is not None else {}),
                                  **({'legacy_generation': generation} if generation else {}),
                                  **({'connection_id': conn} if conn else {})})
                    continue
                if attempt and attempt['state'] == 'sent':
                    items.append({'legacy_key': legacy_key, 'result': 'refused', 'reason': 'handover_unconfirmed',
                                  'exchange': 'unknown'})
                    continue
                if not (attempt and attempt['state'] == 'renewed'):
                    # Mere presence of an unrelated card account never fabricates a handover receipt or new grant;
                    # recover without a recorded receipt never claims exchange='none'.
                    items.append({'legacy_key': legacy_key, 'result': 'refused', 'reason': 'handover_unconfirmed',
                                  'exchange': 'unknown', **({'legacy_generation': generation} if generation else {})})
                    continue
            result = ('already_present' if held and held['refresh'] == grant['tokens'].get('refresh_token') else
                      'kept_newer' if held else 'imported')
            if not held:
                if not (attempt and attempt['state'] == 'renewed'):
                    self.handover_exchanges += 1
                self.add(grant['label'], subject, refresh=grant['tokens'].get('refresh_token'),
                         verified=verified, via='handover', legacy_generation=generation, key=key)
                held = self.accounts[key]
            self.handover_attempts.pop(legacy_key, None)
            self.handover_slots[legacy_key] = {'key': key, 'legacy_generation': generation}
            conn = self._connection_of(held)
            items.append({'legacy_key': legacy_key, 'result': result,
                          'account': {'key': key, 'label': held['label']}, 'state': held['state'],
                          **({'via': held['via']} if held.get('via') is not None else {}),
                          **({'legacy_generation': generation} if generation else {}),
                          **({'connection_id': conn} if conn else {})})
        clients = [{'client_id': client['client_id'], 'result': 'imported'} for client in body.get('clients') or []]
        return {'ok': True, 'clients': clients, 'grants': items}

    def sent(self, path):
        return [body for method, route, body in self.requests if route == path]


class FakeGoogleAPI(_Server):
    """Google Calendar's list and event feeds; a bearer in ``refuse_once`` gets one 401."""

    def __init__(self):
        self.bearers, self.refuse_once, self.writes = [], set(), []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                bearer = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
                fake.bearers.append(bearer)
                path = self.path.split('?', 1)[0]
                if bearer in fake.refuse_once:
                    fake.refuse_once.discard(bearer)
                    self.answer(401, {'error': {'errors': [{'reason': 'authError'}]}})
                elif path == '/users/me/calendarList':
                    self.answer(200, {'items': [{'id': OWNER, 'summary': 'Owner', 'accessRole': 'owner', 'primary': True, 'timeZone': 'UTC'}]})
                elif path.endswith('/events'):
                    self.answer(200, {'items': [], 'nextSyncToken': 'sync-1'})
                else:
                    self.answer(404, {'error': {'errors': [{'reason': 'notFound'}]}})

            def do_POST(self):   # a write reaching Google's event feed
                bearer = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
                fake.bearers.append(bearer)
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                if bearer in fake.refuse_once:   # Google refuses the write itself: nothing of it is applied
                    fake.refuse_once.discard(bearer)
                    return self.answer(401, {'error': {'errors': [{'reason': 'authError'}]}})
                fake.writes.append(body)
                self.answer(200, {'id': body.get('id') or 'created-1', 'status': 'confirmed', 'etag': 'etag-1',
                                  'start': {'dateTime': '2026-12-01T10:00:00Z'}, 'end': {'dateTime': '2026-12-01T11:00:00Z'}})

        super().__init__(Handler)


class Api:
    def __init__(self, state_dir, port):
        self.state_dir, self.port = state_dir, port

    def get_state_dir(self):
        return self.state_dir

    def get_settings(self, keys):
        return {key: SECRETS.get(key, '') for key in keys}

    def get_runtime_info(self):
        return {'server_port': self.port}


class HostSignInCase(unittest.TestCase):
    sign_in_version = 1

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_dir = temp.name
        self.store = Store(self.state_dir)
        self.host = FakeHost(self.sign_in_version)
        self.addCleanup(self.host.close)
        self.google = FakeGoogleAPI()
        self.addCleanup(self.google.close)
        self.local_refresh, self.logged = [], []
        for patcher in (patch.object(gp, 'API', self.google.url),
                        patch.object(gp, '_token_request', side_effect=self.google_token_endpoint),
                        patch.dict(os.environ, {'HOST_SERVICE_URL': self.host.url, 'HOST_SERVICE_TOKEN': SKILL_TOKEN})):
            patcher.start()
            self.addCleanup(patcher.stop)
        gp.save_tokens(self.state_dir, VAULT_KEY, {OWNER: legacy_entry(OWNER, 'sub-owner')})
        self.store.upsert_account({'id': 'google:' + OWNER, 'provider': 'google', 'alias': 'Owner', 'login': OWNER, 'status': 'ok'})

    def google_token_endpoint(self, body):
        """Google's own token endpoint, as the skill's old path reaches it."""
        self.local_refresh.append(body.get('grant_type'))
        if body.get('grant_type') == 'authorization_code':
            return {'access_token': 'late-access', 'refresh_token': 'late-refresh', 'expires_in': 3600, 'scope': SCOPE}
        return {'access_token': 'legacy-access-refreshed', 'expires_in': 3600}

    def log(self, message, **fields):
        self.logged.append({'msg': message, **fields})

    def worker_pass(self, handed_over=None):
        return google_host.worker_pass(self.store, SECRETS, self.state_dir, host_sign_in.HostSignIn(), handed_over, self.log)

    def context(self):
        return tools.Context(Api(self.state_dir, int(self.host.url.rsplit(':', 1)[1])))

    def adapter(self, key=OWNER):
        providers = Providers(SECRETS, self.state_dir)
        return providers.adapter_for('google:' + key), providers

    def vault(self):
        return Path(self.state_dir, gp.TOKENS_FILE).read_bytes()


class OlderCoreTests(HostSignInCase):
    sign_in_version = None

    def test_an_older_core_keeps_the_skills_own_sign_in(self):
        self.assertEqual(self.worker_pass(), (None, None))   # no handover, no following, no health facts
        self.assertEqual([path for _, path, _ in self.host.requests], ['/identity'])
        adapter, _ = self.adapter()
        self.assertNotIsInstance(adapter, google_host.HostGoogleAdapter)
        self.assertEqual(adapter.list_calendars()[0]['external_id'], OWNER)
        self.assertEqual(self.local_refresh, ['refresh_token'])   # refreshed here, with the skill's own refresh token
        self.assertEqual(self.google.bearers, ['legacy-access-refreshed'])
        ctx = self.context()
        status = json.loads(tools.cal_status(ctx))
        self.assertEqual(status['connection_options']['google_sign_in'], 'skill')
        self.assertNotIn('sign_in', status['accounts'][0])
        started = accounts.connect_google(ctx)
        self.assertEqual(started['status'], 'ok')   # the old path's own sign-in still starts here
        self.assertTrue(started['auth_url'].startswith(gp.AUTH_URL))

    def test_a_core_downgrade_returns_a_moved_account_to_its_untouched_copy(self):
        self.store.set_setting(google_host.MOVED, {OWNER: {'account': host_key('sub-owner'), 'label': OWNER, 'via': 'handover'}})
        adapter, _ = self.adapter()
        self.assertNotIsInstance(adapter, google_host.HostGoogleAdapter)
        adapter.list_calendars()
        self.assertEqual(self.local_refresh, ['refresh_token'])
        gone = json.loads(tools.cal_settings(self.context(), action='disconnect', calendar_id='google:' + OWNER, confirm=True))
        self.assertEqual(gone['status'], 'updated')     # an older core holds nothing: the disconnect is here
        self.assertEqual(self.store.get_setting(google_host.MOVED), {})   # and the record goes with the account

    def test_sign_in_withdrawn_marker_blocks_old_grant_after_downgrade_until_fresh_local_pkce(self):
        cal_id = f'google:{OWNER}:{OWNER}'
        self.store.upsert_calendar({'id': cal_id, 'account_id': 'google:' + OWNER, 'provider': 'google',
                                    'external_id': OWNER, 'name': 'Owner', 'writable': True})
        self.store.set_setting(google_host.MOVED, {
            OWNER: {'account': host_key('sub-owner'), 'label': OWNER, 'subject': 'sub-owner',
                    'via': 'handover', 'legacy_generation': 'g-sub-owner', 'connection_id': 'conn-1'}})
        intent = self.store.add_intent('create', 'google:' + OWNER, cal_id, 'evt-old', {})
        marker_path = Path(self.state_dir, google_host.WITHDRAWN_MARKER)
        # Unrelated or weak-field-only rows (subject/legacy_key without matching connection_id or generation) never block.
        marker_path.write_text(json.dumps({'v': 1, 'accounts': [
            {'service': 'google', 'account': host_key('sub-other')},
            {'service': 'google', 'subject': 'sub-owner', 'legacy_key': OWNER},
        ]}), encoding='utf-8')
        self.assertIsNotNone(self.adapter()[0])
        # Relevant disconnect row tied to MOVED.account without connection_id/legacy_generation is unknown (not withdrawn).
        marker_path.write_text(json.dumps({'v': 1, 'accounts': [{
            'service': 'google', 'account': host_key('sub-owner'), 'legacy_key': OWNER, 'at': iso_utc(now_utc())}]}), encoding='utf-8')
        adapter, providers = self.adapter()
        self.assertIsNone(adapter)
        self.assertIn(google_host.WITHDRAWN_MARKER, providers.errors['google:' + OWNER])
        self.assertNotIn('disconnected', providers.errors['google:' + OWNER])
        self.assertEqual(self.store.get_intent(intent['id'])['state'], 'pending')
        self.assertIn(OWNER, self.store.get_setting(google_host.MOVED))
        # Unreadable JSON is unknown (not withdrawn): holds honestly without erasing marks or cancelling queued work.
        marker_path.write_text('{corrupt', encoding='utf-8')
        adapter, providers = self.adapter()
        self.assertIsNone(adapter)
        self.assertIn(google_host.WITHDRAWN_MARKER, providers.errors['google:' + OWNER])
        self.assertNotIn('disconnected', providers.errors['google:' + OWNER])
        self.assertEqual(accounts.reload_google(self.context())['status'], 'error')
        self.assertEqual(self.store.get_intent(intent['id'])['state'], 'pending')
        self.assertIn(OWNER, self.store.get_setting(google_host.MOVED))
        # Core positively records Disconnect for OWNER before downgrade, with no final worker_pass.
        marker_path.write_text(json.dumps({'v': 1, 'accounts': [{
            'service': 'google', 'account': host_key('sub-owner'), 'subject': 'sub-owner',
            'legacy_key': OWNER, 'legacy_generation': 'g-sub-owner', 'connection_id': 'conn-1', 'at': iso_utc(now_utc())}]}), encoding='utf-8')
        self.store.set_setting(google_host.REFUSED, {OWNER: {'reason': google_host.UNCONFIRMED, 'legacy_generation': 'g-sub-owner'}})
        adapter, providers = self.adapter()
        self.assertIsNone(adapter)
        self.assertIn('disconnected', providers.errors['google:' + OWNER])
        self.assertEqual(self.local_refresh, [])
        # Explicit fresh local PKCE with a new generation opens a fresh path without reviving old queue or refresh.
        started = gp.start_auth(self.store, SECRETS, self.host.url + google_host.RETURN_PAGE)
        def userinfo(adapter):
            adapter._identity = {'sub': 'sub-owner'}
            return OWNER
        ctx = self.context()
        with patch.object(gp.GoogleAdapter, 'primary_email', userinfo), \
                patch.object(gp, '_token_request', return_value={'access_token': 'fresh-access', 'expires_in': 3600, 'scope': SCOPE}):
            done = accounts.complete_google(ctx, code='fresh-code', state=started['state'])
        self.assertTrue(done['ok'])
        self.assertFalse(done['offline_access'])  # old refresh token was NOT kept across Disconnect
        self.assertEqual(self.store.get_setting(google_host.REFUSED), {})
        self.assertEqual(self.store.get_setting(google_host.MOVED), {})
        self.assertEqual(self.store.get_intent(intent['id'])['state'], 'resolved')  # pre-disconnect queue withdrawn
        fresh_adapter = ctx.providers.adapter_for('google:' + OWNER)
        self.assertIsNotNone(fresh_adapter)
        self.assertNotIsInstance(fresh_adapter, google_host.HostGoogleAdapter)
        self.assertEqual(fresh_adapter.list_calendars()[0]['external_id'], OWNER)
        self.assertEqual(ops.retry_due_intents(self.store, ctx.providers, owner='test'), [])
        self.assertEqual(self.google.writes, [])
        # A later confirmed Disconnect of that fresh generation is still honored (no blanket allowed_generation bypass).
        fresh_gen = gp.load_tokens(self.state_dir, VAULT_KEY)[OWNER]['generation']
        marker_path.write_text(json.dumps({'v': 1, 'accounts': [{
            'service': 'google', 'legacy_key': OWNER, 'legacy_generation': fresh_gen, 'at': iso_utc(now_utc())}]}), encoding='utf-8')
        blocked, providers = self.adapter()
        self.assertIsNone(blocked)
        self.assertIn('disconnected', providers.errors['google:' + OWNER])
        # Unreadable JSON during explicit new local PKCE holds prior unsent intents for review (not sent, not resolved,
        # full payload preserved, distinct from unknown-delivery), keeps calendars/roles and new operations available,
        # and never sends or loses the held command after sign_in_withdrawn.json becomes readable again.
        self.store.update_calendar(cal_id, {'role_publish': 1, 'publish_mode': 'full'})
        with self.store.transaction() as local:
            staged = ops._stage_create_event(local, {
                'title': 'Held before review', 'start_utc': '2026-12-01T10:00:00+00:00',
                'end_utc': '2026-12-01T11:00:00+00:00', 'calendar_ids': [cal_id]})
        [held_id] = staged['assignments'][0]['_intent_ids']
        orig_payload = self.store.get_intent(held_id)['payload_json']
        lost = self.store.add_intent('update', 'google:' + OWNER, cal_id, 'evt-lost', {'changes': {'title': 'X'}})
        self.store.settle_intent(lost['id'], 'pending', {'delivery': 'unknown', 'delivery_error': 'timeout'})
        marker_path.write_text('{corrupt', encoding='utf-8')
        started_2 = gp.start_auth(self.store, SECRETS, self.host.url + google_host.RETURN_PAGE)
        ctx_2 = self.context()
        with patch.object(gp.GoogleAdapter, 'primary_email', userinfo), \
                patch.object(gp, '_token_request', return_value={'access_token': 'fresh-access-2', 'expires_in': 3600, 'scope': SCOPE}):
            done_2 = accounts.complete_google(ctx_2, code='fresh-code-2', state=started_2['state'])
        self.assertTrue(done_2['ok'])
        held_row = self.store.get_intent(held_id)
        self.assertEqual(held_row['state'], 'conflict')
        self.assertEqual(held_row['payload_json'], orig_payload)
        held_res = json.loads(held_row['result_json'])
        self.assertEqual((held_res.get('outcome'), held_res.get('unsent'), held_res.get('delivery')),
                         ('hold_for_review', True, 'none'))
        self.assertEqual(self.store.get_intent(lost['id'])['state'], 'uncertain')
        self.assertEqual(json.loads(self.store.get_intent(lost['id'])['result_json']).get('delivery'), 'unknown')
        cal = self.store.get_calendar(cal_id)
        self.assertEqual((cal['deleted_at'], cal['role_publish'], cal['publish_mode']), (None, 1, 'full'))
        created = ops.create_event(self.store, ctx_2.providers, {
            'title': 'Independent after PKCE', 'start_utc': '2026-12-01T14:00:00+00:00',
            'end_utc': '2026-12-01T15:00:00+00:00', 'calendar_ids': [cal_id]})
        self.assertEqual(created['assignments'][0]['status'], 'done')
        self.assertEqual(ops.retry_due_intents(self.store, ctx_2.providers, owner='test'), [])
        self.assertEqual(len(self.google.writes), 1)
        marker_path.write_text(json.dumps({'v': 1, 'accounts': [{
            'service': 'google', 'legacy_key': OWNER, 'legacy_generation': fresh_gen, 'at': iso_utc(now_utc())}]}), encoding='utf-8')
        self.assertEqual(ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test'), [])
        self.assertEqual(len(self.google.writes), 1)
        self.assertEqual(self.store.get_intent(held_id)['state'], 'conflict')
        self.assertEqual(self.store.get_intent(held_id)['payload_json'], orig_payload)
        created_2 = ops.create_event(self.store, Providers(SECRETS, self.state_dir), {
            'title': 'Independent after marker restored', 'start_utc': '2026-12-01T16:00:00+00:00',
            'end_utc': '2026-12-01T17:00:00+00:00', 'calendar_ids': [cal_id]})
        self.assertEqual(created_2['assignments'][0]['status'], 'done')
        self.assertEqual(len(self.google.writes), 2)


class HandoverTests(HostSignInCase):
    def test_the_handover_runs_once_and_the_moved_account_takes_host_tokens_only(self):
        before = self.vault()
        handed, health = self.worker_pass()
        self.assertEqual(handed, {'status': 'ok', 'results': {'imported': 1}})
        [body] = self.host.sent('/sign-in/handover')
        self.assertEqual(body['service'], 'google')
        self.assertEqual([c['client_id'] for c in body['clients']], ['personal-client'])
        self.assertEqual(body['clients'][0]['client_secret'], CLIENT_SECRET)   # with its secret, on loopback only
        self.assertEqual(body['clients'][0]['return_hint'], 'loopback')
        [grant] = body['grants']
        self.assertEqual({k: grant[k] for k in ('legacy_key', 'label', 'subject', 'client_id')},
                         {'legacy_key': OWNER, 'label': OWNER, 'subject': 'sub-owner', 'client_id': 'personal-client'})
        self.assertEqual(grant['tokens']['refresh_token'], LEGACY_REFRESH)
        self.assertIsInstance(grant['tokens']['expires_at'], float)
        mark = self.store.get_setting(google_host.MOVED)[OWNER]
        self.assertEqual((mark['account'], mark['via'], mark['result']), (host_key('sub-owner'), 'handover', 'imported'))
        self.assertEqual(health['follow'], {'status': 'ok', 'added': 0, 'withdrawn': 0})

        again, _ = self.worker_pass(handed)            # a later pass of the same worker
        self.assertIs(again, handed)
        restarted, _ = self.worker_pass()              # the next start: the client was offered, the account moved
        self.assertEqual(restarted, {'status': 'nothing'})
        self.assertEqual(len(self.host.sent('/sign-in/handover')), 1)

        report = worker.sync_account(self.store, Providers(SECRETS, self.state_dir), self.store.get_account('google:' + OWNER), UTC)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(self.google.bearers, ['host-access-1', 'host-access-1'])   # list and feed, the host's token
        self.assertEqual(self.local_refresh, [])        # never refreshed here
        self.assertEqual(self.vault(), before)          # the local copy stays untouched for a downgrade

    def test_a_repeated_handover_is_idempotent(self):
        self.worker_pass()
        self.store.set_setting(google_host.MOVED, {})   # as if the mark were lost between the answer and the record
        handed, _ = self.worker_pass()
        self.assertEqual(handed['results'], {'already_present': 1})
        self.assertEqual(self.store.get_setting(google_host.MOVED)[OWNER]['account'], host_key('sub-owner'))

    def test_a_refused_account_stays_on_its_own_path_says_why_and_is_offered_again(self):
        other = 'second@example.test'
        vault = {OWNER: legacy_entry(OWNER, 'sub-owner'), other: legacy_entry(other, 'sub-second', refresh='legacy-refresh-token-2')}
        gp.save_tokens(self.state_dir, VAULT_KEY, vault)
        self.store.upsert_account({'id': 'google:' + other, 'provider': 'google', 'alias': 'Second', 'login': other, 'status': 'ok'})
        self.host.refuse[OWNER] = 'provider_scope_unconfirmed'
        handed, _ = self.worker_pass()
        self.assertEqual(handed['results'], {'refused': 1, 'imported': 1})
        self.assertEqual(set(self.store.get_setting(google_host.MOVED)), {other})
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], 'provider_scope_unconfirmed')
        self.assertNotIsInstance(self.adapter()[0], google_host.HostGoogleAdapter)   # its own sign-in, as before
        self.assertIsInstance(self.adapter(other)[0], google_host.HostGoogleAdapter)
        status = json.loads(tools.cal_status(self.context()))
        rows = {a['id']: a for a in status['accounts']}
        self.assertEqual(rows['google:' + OWNER]['handover_refused'], 'provider_scope_unconfirmed')
        self.assertNotIn('sign_in', rows['google:' + OWNER])
        self.assertEqual(rows['google:' + other]['sign_in'], 'ouroboros')
        self.assertNotIn('handover_refused', rows['google:' + other])
        self.assertTrue(any('provider_scope_unconfirmed' in step for step in status['next_step']))
        del self.host.refuse[OWNER]
        handed, _ = self.worker_pass()                  # the next start offers only the refused one again
        self.assertEqual([g['legacy_key'] for g in self.host.sent('/sign-in/handover')[-1]['grants']], [OWNER])
        self.assertEqual(handed['results'], {'imported': 1})
        self.assertEqual(self.store.get_setting(google_host.REFUSED), {})


class HostTokenTests(HostSignInCase):
    def setUp(self):
        super().setUp()
        self.worker_pass()

    def test_google_refusing_the_token_asks_the_host_once_with_the_rejected_one(self):
        self.google.refuse_once.add('host-access-1')
        adapter, _ = self.adapter()
        self.assertEqual(adapter.list_calendars()[0]['external_id'], OWNER)
        self.assertEqual([body.get('rejected') for body in self.host.sent('/sign-in/token')], [None, 'host-access-1'])
        self.assertEqual(self.google.bearers, ['host-access-1', 'host-access-2'])
        self.assertEqual(self.local_refresh, [])
        self.assertEqual(adapter.calendar_capabilities()['sign_in'], 'ouroboros')
        self.assertTrue(adapter.calendar_capabilities()['sharing'])   # the granted scope the host reported

    def test_a_sign_in_the_host_needs_again_carries_its_words_and_other_accounts_keep_working(self):
        other = 'second@example.test'
        self.host.add(other, 'sub-second')
        google_host.follow(self.store, SECRETS, self.state_dir, host_sign_in.HostSignIn())
        self.host.accounts[host_key('sub-owner')]['state'] = 'sign_in_required'
        providers = Providers(SECRETS, self.state_dir)
        for account in self.store.list_accounts('google'):
            worker.sync_account(self.store, providers, account, UTC)
        owner, second = self.store.get_account('google:' + OWNER), self.store.get_account('google:' + other)
        self.assertEqual(owner['status'], 'auth_failed')
        self.assertIn(HINT, owner['last_error'])
        self.assertIn(NOTE, owner['last_error'])
        self.assertEqual((second['status'], second['last_error']), ('ok', ''))
        self.assertEqual(self.local_refresh, [])

    def test_an_unreachable_provider_keeps_the_sign_in_and_is_not_a_sign_in_failure(self):
        self.host.down = True
        report = worker.sync_account(self.store, Providers(SECRETS, self.state_dir), self.store.get_account('google:' + OWNER), UTC)
        self.assertEqual(report['status'], 'error')
        self.assertIn('provider_unreachable', self.store.get_account('google:' + OWNER)['last_error'])
        self.assertIn(OWNER, self.store.get_setting(google_host.MOVED))

    def test_a_moved_account_is_never_refreshed_here_while_the_host_does_not_answer(self):
        self.host.close()
        adapter, providers = self.adapter()
        self.assertIsNone(adapter)
        self.assertIn('nothing is refreshed here', providers.errors['google:' + OWNER])
        self.assertEqual(self.local_refresh, [])
        kept = json.loads(tools.cal_settings(self.context(), action='disconnect', calendar_id='google:' + OWNER, confirm=True))
        self.assertEqual(kept['status'], 'sign_in_on_card')   # a disconnect here would come back with the host
        self.assertIsNotNone(self.store.get_account('google:' + OWNER))
        other = 'legacy@example.test'   # an account Ouroboros does not hold keeps the skill's own path meanwhile
        gp.save_tokens(self.state_dir, VAULT_KEY, {**gp.load_tokens(self.state_dir, VAULT_KEY), other: legacy_entry(other, 'sub-legacy')})
        self.assertNotIsInstance(self.adapter(other)[0], google_host.HostGoogleAdapter)
        self.assertIsNotNone(self.adapter(other)[0])


class UnsentWriteTests(HostSignInCase):
    """A write the host's token refusal kept from being sent: unsent, not failed;
    original id kept; delivered after the ordinary re-login."""

    def setUp(self):
        super().setUp()
        self.worker_pass()
        self.store.upsert_calendar({'id': f'google:{OWNER}:{OWNER}', 'account_id': 'google:' + OWNER, 'provider': 'google',
                                    'external_id': OWNER, 'name': 'Owner', 'writable': True})
        self.host.accounts[host_key('sub-owner')]['state'] = 'sign_in_required'

    def create_on(self, *calendar_ids):
        with patch.object(ops, 'SIGN_IN_WAIT_SEC', 0):   # the wait is real time; the queue place is what is tested
            return ops.create_event(self.store, Providers(SECRETS, self.state_dir), {
                'title': 'Planning', 'start_utc': '2026-12-01T10:00:00+00:00', 'end_utc': '2026-12-01T11:00:00+00:00',
                'calendar_ids': list(calendar_ids)})

    def test_an_unsent_create_waits_keeps_its_id_and_is_delivered_after_the_ordinary_sign_in(self):
        result = self.create_on(f'google:{OWNER}:{OWNER}')
        event_id = result['event']['id']
        [assignment] = result['assignments']
        self.assertEqual(assignment['status'], 'pending')
        row = self.store.get_event(event_id)
        self.assertEqual(row['sync_state'], 'pending')   # unsent and queued, never failed
        account = self.store.get_account('google:' + OWNER)
        self.assertEqual(account['status'], 'auth_failed')
        self.assertIn(HINT, account['last_error'])
        self.assertIn(NOTE, account['last_error'])
        [intent] = self.store.intents_for_event(event_id)
        self.assertEqual((intent['kind'], intent['state'], intent['attempts']), ('create', 'pending', 0))   # waiting is not an attempt
        self.assertIn(HINT, json.loads(intent['result_json'])['note'])
        self.host.accounts[host_key('sub-owner')]['state'] = 'signed_in'   # the owner signed in again on the card
        report = ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test')
        self.assertEqual([item['status'] for item in report], ['done'])
        self.assertEqual(self.store.get_intent(intent['id'])['state'], 'done')
        delivered = self.store.get_event(event_id)
        self.assertEqual(delivered['id'], event_id)   # the same row, no replacement
        self.assertEqual(delivered['sync_state'], 'synced')
        self.assertTrue(delivered['external_id'] and delivered['etag'])
        self.assertEqual(len(self.store.intents_for_event(event_id)), 1)   # one operation, delivered once
        self.assertTrue(self.google.writes)   # the create reached Google under the host's fresh token

    def test_a_composite_command_reports_each_part_its_own_truth(self):
        self.store.upsert_calendar({'id': 'local:home', 'account_id': 'local:me', 'provider': 'local', 'name': 'Home'})
        result = self.create_on('local:home', f'google:{OWNER}:{OWNER}')
        self.assertEqual({a['calendar_id']: a['status'] for a in result['assignments']},
                         {'local:home': 'done', f'google:{OWNER}:{OWNER}': 'pending'})   # the sent part is not declared unsent
        self.assertEqual(self.store.get_event(result['event']['id'])['sync_state'], 'synced')

    def test_a_send_that_may_have_reached_google_keeps_its_recorded_outcome(self):
        result = self.create_on(f'google:{OWNER}:{OWNER}')
        [intent] = self.store.intents_for_event(result['event']['id'])
        self.store.settle_intent(intent['id'], 'uncertain', {'error': 'connection lost after the write', 'kind': 'network'})
        self.host.accounts[host_key('sub-owner')]['state'] = 'signed_in'
        self.assertEqual(ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test'), [])
        kept = self.store.get_intent(intent['id'])
        self.assertEqual((kept['state'], json.loads(kept['result_json'])['error']),
                         ('uncertain', 'connection lost after the write'))   # never re-sent or re-classified on its own

    def test_cal_status_carries_the_whole_refusal_and_the_saved_queue(self):
        self.create_on(f'google:{OWNER}:{OWNER}')
        status = json.loads(tools.cal_status(self.context()))
        [row] = [a for a in status['accounts'] if a['id'] == 'google:' + OWNER]
        self.assertEqual(row['status'], 'auth_failed')
        self.assertEqual(row['last_error'], self.store.get_account('google:' + OWNER)['last_error'])   # the host's sentence and Calendar's note whole
        self.assertEqual(row['sign_in'], 'ouroboros')
        self.assertTrue(any('после повторного входа на карточке' in step for step in status['next_step']))

    def test_offline_access_is_never_claimed_for_an_account_ouroboros_holds(self):
        self.assertIsNone(self.adapter()[0].calendar_capabilities()['offline_access'])   # no host answer proves an offline grant
        self.host.add('card@example.test', 'sub-card')
        google_host.follow(self.store, SECRETS, self.state_dir, host_sign_in.HostSignIn())
        self.assertIsNone(self.adapter('card@example.test')[0].calendar_capabilities()['offline_access'])

    def test_a_refresh_after_the_providers_401_refusal_is_still_unsent(self):
        adapter, _ = self.adapter()
        for request in (adapter.list_calendars,
                        lambda: adapter.share_calendar({'external_id': OWNER}, {'value': 'guest@example.test'})):
            adapter.access_token, adapter.expires_at = 'host-access-1', now_utc() + timedelta(hours=1)
            self.google.refuse_once.add('host-access-1')
            adapter.begin_operation()
            with self.assertRaises(ops.ProviderError) as raised:
                request()   # a read or a write Google refused with 401 applied nothing, so the refresh's refusal is unsent
            self.assertEqual((raised.exception.kind, raised.exception.unsent), ('auth', True))
        self.assertEqual(self.google.writes, [])

    def test_a_later_step_after_an_accepted_write_is_not_unsent(self):
        self.host.accounts[host_key('sub-owner')]['state'] = 'signed_in'
        adapter, _ = self.adapter()
        adapter.begin_operation()
        adapter.share_calendar({'external_id': OWNER}, {'value': 'guest@example.test'})   # an earlier write of this operation was accepted
        adapter.expires_at = now_utc() - timedelta(minutes=5)
        self.host.accounts[host_key('sub-owner')]['state'] = 'sign_in_required'
        with self.assertRaises(ops.ProviderError) as raised:
            adapter.list_calendars()   # the pre-flight refusal of a later step: part of this operation already took effect
        self.assertEqual((raised.exception.kind, raised.exception.unsent), ('auth', False))


class UnansweredHostTests(HostSignInCase):
    def test_an_unanswered_sign_in_service_is_a_state_of_its_own(self):
        self.worker_pass()
        self.host.close()
        ctx = self.context()
        status = json.loads(tools.cal_status(ctx))
        self.assertEqual(status['connection_options']['google_sign_in'], 'unreachable')   # an old core is a different state
        row = next(a for a in status['accounts'] if a['id'] == 'google:' + OWNER)
        self.assertEqual(row['sign_in'], 'ouroboros')   # the account stays Ouroboros's while the host does not answer
        self.assertIn('did not answer', row['adapter_error'])
        started = accounts.connect_google(ctx)
        self.assertEqual(started['status'], 'unreachable')
        self.assertNotIn('auth_url', started)


class FollowTests(HostSignInCase):
    def setUp(self):
        super().setUp()
        self.worker_pass()
        self.store.upsert_calendar({'id': f'google:{OWNER}:{OWNER}', 'account_id': 'google:' + OWNER, 'provider': 'google',
                                    'external_id': OWNER, 'name': 'Owner', 'writable': True})

    def follow(self):
        return google_host.follow(self.store, SECRETS, self.state_dir, host_sign_in.HostSignIn())

    def test_an_account_added_on_the_card_becomes_a_calendar_account(self):
        self.host.add('new@example.test', 'sub-new')
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 0})
        account = self.store.get_account('google:new@example.test')
        self.assertEqual((account['status'], account['login']), ('connected', 'new@example.test'))
        self.assertEqual(self.store.get_setting(google_host.MOVED)['new@example.test']['via'], 'card')
        self.assertTrue(self.store.get_setting('sync_requested_at'))
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})   # nothing new, nothing written twice

    def test_a_card_sign_in_of_a_known_account_keeps_its_calendar_key(self):
        known = 'known@example.test'
        gp.save_tokens(self.state_dir, VAULT_KEY, {**gp.load_tokens(self.state_dir, VAULT_KEY), known: legacy_entry(known, 'sub-known')})
        self.host.add(known, 'sub-known')
        self.follow()
        self.assertEqual(self.store.get_setting(google_host.MOVED)[known]['account'], host_key('sub-known'))
        self.assertIsInstance(self.adapter(known)[0], google_host.HostGoogleAdapter)   # the card's sign-in wins

    def test_a_disconnect_on_the_card_withdraws_the_account_here(self):
        del self.host.accounts[host_key('sub-owner')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 1})
        self.assertIsNone(self.store.get_account('google:' + OWNER))
        self.assertTrue(self.store.get_calendar(f'google:{OWNER}:{OWNER}')['deleted_at'])   # retired, history kept
        self.assertNotIn(OWNER, gp.load_tokens(self.state_dir, VAULT_KEY))
        self.assertEqual(self.store.get_setting(google_host.MOVED), {})

    def test_an_unreadable_or_undeclared_host_changes_nothing(self):
        self.host.declared = False
        self.assertEqual(self.follow(), {'status': 'undeclared'})
        self.host.close()
        self.assertEqual(self.follow(), {'status': 'unreachable'})
        self.assertIsNotNone(self.store.get_account('google:' + OWNER))
        self.assertIn(OWNER, self.store.get_setting(google_host.MOVED))


class CardPointerTests(HostSignInCase):
    def test_connect_and_disconnect_point_to_the_card_only_for_what_ouroboros_holds(self):
        legacy = 'legacy@example.test'
        self.host.refuse[legacy] = 'unknown_client'
        gp.save_tokens(self.state_dir, VAULT_KEY, {OWNER: legacy_entry(OWNER, 'sub-owner'), legacy: legacy_entry(legacy, 'sub-legacy')})
        self.store.upsert_account({'id': 'google:' + legacy, 'provider': 'google', 'alias': 'Legacy', 'login': legacy, 'status': 'ok'})
        self.worker_pass()
        ctx = self.context()
        self.assertEqual(accounts.connect_google(ctx)['status'], 'sign_in_on_card')
        self.assertEqual(json.loads(tools.cal_status(ctx))['connection_options']['google_sign_in'], 'ouroboros')
        held = json.loads(tools.cal_settings(ctx, action='disconnect', calendar_id='google:' + OWNER, confirm=True))
        self.assertEqual(held['status'], 'sign_in_on_card')
        self.assertIn('Extensions › Skills', held['message'])
        self.assertIsNotNone(self.store.get_account('google:' + OWNER))
        self.assertIn(OWNER, gp.load_tokens(self.state_dir, VAULT_KEY))
        own = json.loads(tools.cal_settings(ctx, action='disconnect', calendar_id='google:' + legacy, confirm=True))
        self.assertEqual(own['status'], 'updated')   # an account on the skill's own path disconnects here as before
        self.assertIsNone(self.store.get_account('google:' + legacy))

    def test_a_sign_in_the_old_path_started_still_completes_and_moves_at_the_next_start(self):
        started = gp.start_auth(self.store, SECRETS, self.host.url + google_host.RETURN_PAGE)   # begun before the update
        self.worker_pass()
        api = RouteAPI()
        routes.register_routes(api, self.context)
        def userinfo(adapter):
            adapter._identity = {'sub': 'sub-late'}
            return 'late@example.test'

        with patch.object(gp.GoogleAdapter, 'primary_email', userinfo):
            reply = asyncio.run(api.routes['oauth/complete'](Request({'code': 'one-time-code', 'state': started['state']})))
        done = json.loads(reply.body)
        self.assertEqual((reply.status_code, done['ok'], done['account_id']), (200, True, 'google:late@example.test'))
        self.assertNotIn('late@example.test', self.store.get_setting(google_host.MOVED))
        self.worker_pass()                              # the next start hands it over
        [grant] = self.host.sent('/sign-in/handover')[-1]['grants']
        self.assertEqual((grant['legacy_key'], grant['subject']), ('late@example.test', 'sub-late'))
        moved = self.store.get_setting(google_host.MOVED)['late@example.test']
        self.assertEqual((moved['account'], moved['subject']), (host_key('sub-late'), 'sub-late'))


class SecrecyTests(HostSignInCase):
    def test_no_token_or_secret_reaches_logs_health_status_or_answers(self):
        handed, health = self.worker_pass()
        self.google.refuse_once.add('host-access-1')
        worker.sync_account(self.store, Providers(SECRETS, self.state_dir), self.store.get_account('google:' + OWNER), UTC)
        self.host.accounts[host_key('sub-owner')]['state'] = 'sign_in_required'
        worker.sync_account(self.store, Providers(SECRETS, self.state_dir), self.store.get_account('google:' + OWNER), UTC)
        api = RouteAPI()
        routes.register_routes(api, self.context)
        shown = [json.dumps(item) for item in (self.logged, handed, health, self.store.list_accounts(),
                                                json.loads(tools.cal_status(self.context())),
                                                json.loads(asyncio.run(api.routes['status'](Request())).body),
                                                self.store.get_setting(google_host.MOVED))]
        self.assertTrue(self.logged and self.store.get_account('google:' + OWNER)['last_error'])   # there was something to show
        for text in shown:
            self.assertIsNone(SECRET_SHAPES.search(text), text)
        refusal = google_host._refusal(host_sign_in.HostError(409, {'hint': HINT, 'note': NOTE}))
        self.assertIsNone(SECRET_SHAPES.search(str(refusal)))


class LoopbackTests(unittest.TestCase):
    def test_the_skill_token_goes_only_to_plain_loopback(self):
        opened = []
        with patch.dict(os.environ, {'HOST_SERVICE_URL': '', 'HOST_SERVICE_TOKEN': ''}), \
                patch.object(host_sign_in.urllib.request, 'build_opener',
                             lambda *handlers: type('Opener', (), {'open': lambda self, request, timeout: opened.append(request)})()):
            for url in ('http://host.example.test:8767', 'https://127.0.0.1:8767', 'http://user:secret@127.0.0.1:8767',
                        'http://127.0.0.1.example.test:8767', 'http://[::ffff:127.0.0.1]:8767', ''):
                client = host_sign_in.HostSignIn(url, SKILL_TOKEN)
                self.assertEqual((client.base_url, client.state()), ('', host_sign_in.UNREACHABLE))
        self.assertEqual(opened, [])
        for url in ('http://127.0.0.1:8767', 'http://localhost:8767', 'http://[::1]:8767'):
            self.assertEqual(host_sign_in.HostSignIn(url, SKILL_TOKEN).base_url, url)


class ContinuityAndWriteAuthorityTests(HostSignInCase):
    def setUp(self):
        super().setUp()
        self.cal_id = f'google:{OWNER}:{OWNER}'
        self.store.upsert_calendar({'id': self.cal_id, 'account_id': 'google:' + OWNER, 'provider': 'google',
                                    'external_id': OWNER, 'name': 'Owner', 'writable': True})

    def queue_event(self, title='Deferred'):
        with self.store.transaction() as local:
            staged = ops._stage_create_event(local, {
                'title': title, 'start_utc': '2026-12-01T10:00:00+00:00',
                'end_utc': '2026-12-01T11:00:00+00:00', 'calendar_ids': [self.cal_id]})
        [intent_id] = staged['assignments'][0]['_intent_ids']
        return staged['event']['id'], intent_id

    def follow(self):
        return google_host.follow(self.store, SECRETS, self.state_dir, host_sign_in.HostSignIn())

    def test_confirmed_before_exchange_refusal_keeps_legacy_working_while_post_exchange_and_unreadable_hold(self):
        # Only explicit per-grant exchange=='none' (without disconnect) returns the old local right.
        self.host.refuse[OWNER] = {'reason': 'provider_approval_required', 'exchange': 'none'}
        handed, _ = self.worker_pass()
        self.assertEqual(handed['results'], {'refused': 1})
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], 'provider_approval_required')
        adapter, _ = self.adapter()
        self.assertIsNotNone(adapter)
        self.assertNotIsInstance(adapter, google_host.HostGoogleAdapter)
        self.assertEqual(adapter.list_calendars()[0]['external_id'], OWNER)
        self.assertEqual(self.local_refresh, ['refresh_token'])
        status = json.loads(tools.cal_status(self.context()))
        row = next(a for a in status['accounts'] if a['id'] == 'google:' + OWNER)
        self.assertEqual(row['handover_refused'], 'provider_approval_required')

        # Missing exchange (even for a benign reason name) or exchange=='saved'/'unknown' holds UNCONFIRMED.
        self.local_refresh.clear()
        self.host.refuse[OWNER] = {'reason': 'unknown_client'}
        self.worker_pass()
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], google_host.UNCONFIRMED)
        self.assertIsNone(self.adapter()[0])
        self.assertEqual(self.local_refresh, [])

        # HTTP 429 with explicit handover_started=False clears ONLY a fresh primary own reservation.
        self.store.set_setting(google_host.REFUSED, {})
        del self.host.refuse[OWNER]
        self.host.handover_http_error = (429, {'ok': False, 'error': 'rate limit', 'handover_started': False})
        handed, _ = self.worker_pass()
        self.assertEqual((handed['status'], handed['http']), ('refused', 429))
        self.assertEqual(self.store.get_setting(google_host.REFUSED), {})
        self.assertIsNotNone(self.adapter()[0])

        # HTTP 400 (no handover_started=False 429) or 429 on recover never clears an existing UNCONFIRMED reservation.
        self.host.handover_http_error = (400, {'ok': False, 'error': 'bad request'})
        self.worker_pass()
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], google_host.UNCONFIRMED)
        self.host.handover_http_error = (429, {'ok': False, 'error': 'rate limit', 'handover_started': False})
        self.worker_pass({'status': 'ok', 'results': {}})  # recover call: must NOT clear prior UNCONFIRMED
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], google_host.UNCONFIRMED)
        self.assertIsNone(self.adapter()[0])

        # Unreadable handover reply also keeps UNCONFIRMED hold.
        self.store.set_setting(google_host.REFUSED, {})
        self.host.handover_raw = b'{truncated'
        handed, _ = self.worker_pass()
        self.assertEqual(handed['status'], 'unreachable')
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], google_host.UNCONFIRMED)
        self.assertIsNone(self.adapter()[0])

    def test_recover_completes_saved_identity_check_without_second_exchange(self):
        # First pass: refresh succeeded on host ('saved'), then userinfo failed transiently.
        self.host.refuse[OWNER] = {'reason': 'provider_unreachable', 'exchange': 'saved', 'record_attempt': 'renewed'}
        handed, _ = self.worker_pass()
        self.assertEqual(self.host.handover_exchanges, 1)
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], google_host.UNCONFIRMED)
        self.assertIsNone(self.adapter()[0])

        # Next ordinary pass sends top-level recover=True and finishes identity check with zero extra exchanges.
        del self.host.refuse[OWNER]
        handed, _ = self.worker_pass(handed)
        self.assertEqual(handed['results'], {'imported': 1})
        self.assertEqual(self.host.handover_exchanges, 1)
        self.assertTrue(self.host.sent('/sign-in/handover')[-1].get('recover'))
        self.assertIn(OWNER, self.store.get_setting(google_host.MOVED))
        self.assertEqual(self.store.get_setting(google_host.REFUSED), {})

    def test_recover_without_receipt_keeps_unconfirmed_hold_and_never_reuses_legacy(self):
        self.store.set_setting(google_host.REFUSED, {
            OWNER: {'reason': google_host.UNCONFIRMED, 'legacy_generation': 'g-sub-owner', 'at': iso_utc(now_utc())}})
        self.worker_pass({'status': 'ok', 'results': {}})
        self.assertEqual(self.host.handover_exchanges, 0)
        self.assertTrue(self.host.sent('/sign-in/handover')[-1].get('recover'))
        refused = self.store.get_setting(google_host.REFUSED)[OWNER]
        self.assertEqual((refused['reason'], refused.get('exchange')), (google_host.UNCONFIRMED, 'unknown'))
        self.assertIsNone(self.adapter()[0])
        self.assertEqual(self.local_refresh, [])

    def test_renewal_grant_carries_previous_confirmed_connection_id(self):
        self.worker_pass()
        conn = self.store.get_setting(google_host.MOVED)[OWNER]['connection_id']
        vault = gp.load_tokens(self.state_dir, VAULT_KEY)
        vault[OWNER]['generation'] = 'g-sub-owner-2'
        gp.save_tokens(self.state_dir, VAULT_KEY, vault)
        self.host.refuse[OWNER] = {'reason': 'unknown_client', 'exchange': 'none'}
        handed, _ = self.worker_pass({'status': 'ok', 'results': {}})
        [grant] = self.host.sent('/sign-in/handover')[-1]['grants']
        self.assertEqual((grant['previous_legacy_generation'], grant['connection_id']), ('g-sub-owner', conn))
        # A confirmed pre-exchange renewal refusal keeps the still-valid host connection usable and does not loop.
        adapter, _ = self.adapter()
        self.assertIsInstance(adapter, google_host.HostGoogleAdapter)
        self.assertEqual(adapter.list_calendars()[0]['external_id'], OWNER)
        sent_before = len(self.host.sent('/sign-in/handover'))
        self.worker_pass(handed)
        self.assertEqual(len(self.host.sent('/sign-in/handover')), sent_before)
        # Metadata-only changes on the same connection_id refresh the binding instead of failing a write.
        marks = self.store.get_setting(google_host.MOVED)
        marks[OWNER] = {**marks[OWNER], 'label': 'renamed@example.test', 'via': 'card', 'at': iso_utc(now_utc())}
        self.store.set_setting(google_host.MOVED, marks)
        adapter.begin_operation()
        adapter.share_calendar({'external_id': OWNER}, {'value': 'guest@example.test'})
        self.assertEqual(len(self.google.writes), 1)

    def test_listing_via_card_never_masks_unconfirmed_handover_and_first_local_to_existing_card_cancels_queue(self):
        # While an unconfirmed handover exists, reading via=card + subject from /sign-in/accounts must NOT clear UNCONFIRMED or touch queue.
        _, intent_id = self.queue_event('Before card')
        self.host.refuse[OWNER] = {'reason': 'provider_unreachable', 'exchange': 'saved', 'record_attempt': 'renewed'}
        self.worker_pass()
        self.host.add(OWNER, 'sub-owner', via='card')
        self.follow()
        self.assertEqual(self.store.get_setting(google_host.MOVED) or {}, {})
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[OWNER]['reason'], google_host.UNCONFIRMED)
        self.assertEqual(self.store.get_intent(intent_id)['state'], 'pending')

        # Explicit recover resumes the saved attempt without a second exchange, files kept_newer against the card,
        # keeps identity/calendars/roles and withdraws the old local queue.
        del self.host.refuse[OWNER]
        handed, _ = self.worker_pass({'status': 'ok', 'results': {}})
        self.assertEqual(handed['results'], {'kept_newer': 1})
        self.assertEqual(self.host.handover_exchanges, 1)
        mark = self.store.get_setting(google_host.MOVED)[OWNER]
        self.assertEqual(mark['via'], 'card')
        self.assertEqual(self.store.get_intent(intent_id)['state'], 'resolved')
        self.assertIsNotNone(self.store.get_account('google:' + OWNER))
        self.assertFalse(self.store.get_calendar(self.cal_id)['deleted_at'])
        self.assertEqual(ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test'), [])
        self.assertEqual(self.google.writes, [])

        # With handover_pending on an existing moved mark, reading via=card never proves the new generation.
        self.store.set_setting(google_host.MOVED, {
            OWNER: {'account': host_key('sub-owner'), 'label': OWNER, 'subject': 'sub-owner',
                    'via': 'handover', 'legacy_generation': 'g-older', 'connection_id': mark['connection_id']}})
        self.follow()
        self.assertEqual(self.store.get_setting(google_host.MOVED)[OWNER]['legacy_generation'], 'g-older')

    def test_continuous_confirmed_reconsent_preserves_queue_while_disconnect_and_new_connection_withdraws_it(self):
        self.worker_pass()
        _, intent_1 = self.queue_event('Survives re-sign-in')
        hk = host_key('sub-owner')
        # Ordinary re-sign-in on the card keeps the same connection_id: queue survives.
        self.host.accounts[hk]['state'] = 'sign_in_required'
        self.follow()
        self.host.add(OWNER, 'sub-owner', state='signed_in', via='card')
        self.follow()
        report = ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test')
        self.assertEqual([item['status'] for item in report], ['done'])
        self.assertEqual(self.store.get_intent(intent_1)['state'], 'done')
        self.assertEqual(len(self.google.writes), 1)

        # Disconnect + new sign-in produces a new connection_id: old queue is withdrawn and never inherited.
        _, intent_2 = self.queue_event('Must not cross disconnect')
        del self.host.accounts[hk]
        self.host.add(OWNER, 'sub-owner', state='signed_in', via='card')
        followed = self.follow()
        self.assertEqual((followed['withdrawn'], followed['added']), (1, 1))
        self.assertEqual(self.store.get_intent(intent_2)['state'], 'resolved')
        self.assertEqual(ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test'), [])
        self.assertEqual(len(self.google.writes), 1)

    def test_cached_token_is_never_used_after_disconnect_and_expected_connection_id_blocks_stale_write(self):
        self.worker_pass()
        hk = host_key('sub-owner')
        old_conn = self.store.get_setting(google_host.MOVED)[OWNER]['connection_id']
        providers = Providers(SECRETS, self.state_dir)
        # Warm a read on this Providers instance so a token was issued for conn-1.
        self.assertEqual(worker.sync_account(self.store, providers, self.store.get_account('google:' + OWNER), UTC)['status'], 'ok')

        # Case 1: Disconnect on the card before follow() runs -> new write must check host and fail unsent (never cached, never parked).
        del self.host.accounts[hk]
        created = ops.create_event(self.store, providers, {
            'title': 'After disconnect', 'start_utc': '2026-12-01T12:00:00+00:00',
            'end_utc': '2026-12-01T13:00:00+00:00', 'calendar_ids': [self.cal_id]})
        self.assertEqual(created['assignments'][0]['status'], 'failed')
        self.assertEqual(self.google.writes, [])
        [intent] = self.store.intents_for_event(created['event']['id'])
        self.assertEqual(intent['state'], 'failed')
        self.assertTrue(json.loads(intent['result_json']).get('unsent'))
        last_tok = self.host.sent('/sign-in/token')[-1]
        self.assertEqual(last_tok.get('connection_id'), old_conn)
        self.assertNotIn('rejected', last_tok)  # pre-write check never sends rejected without an actual 401

        # Case 2: Re-sign-in with a NEW connection_id before follow() runs -> expected_connection_id mismatch blocks write.
        self.host.add(OWNER, 'sub-owner', state='signed_in', via='card')
        created_2 = ops.create_event(self.store, providers, {
            'title': 'Stale connection write', 'start_utc': '2026-12-01T14:00:00+00:00',
            'end_utc': '2026-12-01T15:00:00+00:00', 'calendar_ids': [self.cal_id]})
        self.assertEqual(created_2['assignments'][0]['status'], 'failed')
        self.assertEqual(self.google.writes, [])
        [intent_2] = self.store.intents_for_event(created_2['event']['id'])
        self.assertEqual(intent_2['state'], 'failed')
        res_2 = json.loads(intent_2['result_json'])
        self.assertEqual((res_2.get('unsent'), res_2.get('reason')), (True, 'connection_changed'))
        self.assertEqual(ops.retry_due_intents(self.store, providers, owner='test'), [])

    def test_provisional_import_has_no_connection_id_and_follow_decides_before_mutating(self):
        # Unverified import on real core carries no connection_id.
        prov_key = self.host.add(OWNER, '', state='sign_in_required', verified=False, via='handover',
                                 legacy_generation='g-older', key=host_key('legacy:' + OWNER))
        [row] = host_sign_in.HostSignIn().accounts()
        self.assertNotIn('connection_id', row)
        self.store.set_setting(google_host.MOVED, {
            OWNER: {'account': prov_key, 'label': OWNER, 'subject': None, 'via': 'handover', 'legacy_generation': 'g-older'}})
        _, intent_id = self.queue_event('Pending on provisional')
        # Card sign-in appears while handover_pending (vault has g-sub-owner != g-older): row is not applicable -> nothing mutated.
        card_key = self.host.add(OWNER, 'sub-owner', state='signed_in', verified=True, via='card')
        self.follow()
        self.assertEqual(self.store.get_setting(google_host.MOVED)[OWNER]['account'], prov_key)
        self.assertEqual(self.store.get_intent(intent_id)['state'], 'pending')

        # Once generations align (no handover_pending), rebind commits atomically and cancels unconfirmed queue.
        self.store.set_setting(google_host.MOVED, {
            OWNER: {'account': prov_key, 'label': OWNER, 'subject': None, 'via': 'handover', 'legacy_generation': 'g-sub-owner'}})
        self.follow()
        mark = self.store.get_setting(google_host.MOVED)[OWNER]
        self.assertEqual(mark['account'], card_key)
        self.assertTrue(mark.get('connection_id'))
        self.assertEqual(self.store.get_intent(intent_id)['state'], 'resolved')
        self.assertIsNotNone(self.store.get_account('google:' + OWNER))
        self.assertFalse(self.store.get_calendar(self.cal_id)['deleted_at'])

    def test_older_host_without_connection_id_keeps_ordinary_reconsent_on_unchanged_key(self):
        self.host.emit_connection_id = False
        self.worker_pass()
        mark = self.store.get_setting(google_host.MOVED)[OWNER]
        self.assertNotIn('connection_id', mark)
        self.assertEqual((mark['via'], mark['legacy_generation']), ('handover', 'g-sub-owner'))
        _, intent_id = self.queue_event('Same handed-over generation on older host')
        hk = host_key('sub-owner')
        self.host.accounts[hk]['state'] = 'sign_in_required'
        self.follow()
        self.host.accounts[hk]['state'] = 'signed_in'
        self.follow()
        report = ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test')
        self.assertEqual([item['status'] for item in report], ['done'])
        self.assertEqual(self.store.get_intent(intent_id)['state'], 'done')
        self.assertEqual(len(self.google.writes), 1)

        # When authorization changes on the same host key (via=card, confirmed connection_id appears without matching
        # generation), queued writes wait before follow(), ordinary reads stay available, follow() atomically withdraws
        # the old queue before exposing connection_id, and new writes after consent succeed.
        _, intent_2 = self.queue_event('Must not cross unconfirmed authorization change')
        self.host.accounts[hk]['via'] = 'card'
        self.host.accounts[hk]['legacy_generation'] = None
        self.host.emit_connection_id = True
        self.assertEqual(worker.sync_account(self.store, Providers(SECRETS, self.state_dir),
                                             self.store.get_account('google:' + OWNER), UTC)['status'], 'ok')
        ops.retry_due_intents(self.store, Providers(SECRETS, self.state_dir), owner='test')
        self.assertEqual(len(self.google.writes), 1)
        self.assertEqual(self.store.get_intent(intent_2)['state'], 'pending')
        self.follow()
        self.assertTrue(self.store.get_setting(google_host.MOVED)[OWNER].get('connection_id'))
        self.assertEqual(self.store.get_intent(intent_2)['state'], 'resolved')
        self.assertIsNotNone(self.store.get_account('google:' + OWNER))
        self.assertFalse(self.store.get_calendar(self.cal_id)['deleted_at'])
        created = ops.create_event(self.store, Providers(SECRETS, self.state_dir), {
            'title': 'New write after consent', 'start_utc': '2026-12-01T16:00:00+00:00',
            'end_utc': '2026-12-01T17:00:00+00:00', 'calendar_ids': [self.cal_id]})
        self.assertEqual(created['assignments'][0]['status'], 'done')
        self.assertEqual(len(self.google.writes), 2)


class DeclarationTests(unittest.TestCase):
    def test_the_manifest_declares_the_skills_own_google_scopes(self):
        text = (HERE.parent / 'SKILL.md').read_text(encoding='utf-8')
        front = yaml.safe_load(re.match(r'\A---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)[1])
        [service] = front['sign_in']
        self.assertEqual(service['id'], host_sign_in.SERVICE)
        self.assertEqual(service['scopes'], list(gp.SCOPES))   # one sign-in, the same permissions on both paths
        self.assertEqual(set(service['scope_labels']), set(gp.SCOPES))
        self.assertEqual(service['authorize_params'], {'access_type': 'offline', 'prompt': 'consent'})
        self.assertEqual((service['issuer'], service['accounts'], service['optional']), ('https://accounts.google.com', 'many', True))
        self.assertTrue(all(0 < len(note) <= 600 for note in service['notes'].values()))
        self.assertEqual(set(service['notes']), {'client', 'refused', 'refresh_rejected'})
        self.assertNotRegex(json.dumps(service), 'client_id|client_secret')   # per-install clients only


if __name__ == '__main__':
    unittest.main()
