"""Gmail's move to host-owned sign-in: fictional mailboxes, an inert scripted host and a recording fake provider.

Nothing here reaches Google, a real host or a real mailbox; a "send" is only recorded by the fake provider.
"""
import base64
import json
import sys
import time
from email.message import EmailMessage
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from lib.accounts import AccountError, AccountStore, bearer_mark
from lib.gmail import GmailClient
from lib.gmail_host import GmailSignIn, bearer_scopes, credential_owner, read_view, sync_gmail
from lib.google_auth import FULL_MAIL_SCOPE, MAIL_SCOPES, SETTINGS_SCOPE, begin, finish
from lib.runtime import EmailRuntime
from lib.store import EmailStore

KEY = 'a-' + '1' * 20
RETURN = 'exact:https://mail.example.test/onboarding/static/provider-return.html'
HOST = ('http://127.0.0.1:1', 'fixture-mail-host')
MOVED = {'key': KEY, 'verified': True, 'state': 'signed_in', 'connection_id': 'cn-1'}
IMPORTED = 'c' * 32  # one opaque grant reference
OPAQUE = 'fixture-consent:7/A'  # another valid spelling


def mailbox(store, *, hint=RETURN, **extra):
    """A saved Gmail mailbox of the fictional Google subject `one`; `extra` replaces any field."""
    oauth = {'client_id': 'fixture-client', 'client_secret': 'fixture-secret', **({'return_hint': hint} if hint else {})}
    return store.save({'id': 'google:one', 'provider': 'gmail', 'address': 'one@example.test', 'label': 'One',
                       'senders': ['one@example.test'], 'sender': 'one@example.test', 'folder': 'INBOX',
                       'sending': 'on', 'words': [], 'poll_interval_sec': 30, 'config': {},
                       'scopes': list(MAIL_SCOPES), 'oauth': oauth,
                       'tokens': {'refresh_token': 'fixture-old-refresh', 'access_token': 'fixture-old-access',
                                  'expires_at': time.time() + 3600}, **extra})


def listed(connection, *, via='handover', generation=None):
    return {'key': KEY, 'label': 'one@example.test', 'subject': 'one', 'verified': True, 'state': 'signed_in',
            'scope': ' '.join(MAIL_SCOPES), 'client_label': 'Imported from Mail', 'via': via,
            'connection_id': connection, **({'legacy_generation': generation} if generation else {})}


def reader(reads):
    """A fake Gmail that answers one profile read made with the saved local bearer, and records it."""
    def provider(request):
        assert request.url.path == '/mail/profile' and request.headers['Authorization'] == 'Bearer fixture-old-access'
        reads.append(request.method)
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})
    return provider


class Host:
    """An inert scripted host: it lists `held`, answers each handover from `answers` and issues fixture bearers."""

    def __init__(self, *, older=False, handover=None):
        self.held, self.answers, self.handovers, self.tokens = {}, [], [], []
        self.reads, self.filed, self.unread = [], {}, 0  # receipts read by address, what was filed, failed reads to come
        self.access = 'fixture-host-access'
        self.identity = ({'ok': True} if older else
                         {'ok': True, 'sign_in_version': 1, **({'sign_in_handover_version': handover} if handover else {})})

    def __call__(self, request):
        assert request.headers['x-skill-token'] == HOST[1]
        if request.url.path == '/identity':
            return httpx.Response(200, json=self.identity)
        if request.url.path == '/sign-in/accounts':
            return httpx.Response(200, json={'ok': True, 'services': [{'id': 'google', 'accounts': list(self.held.values())}]})
        body = json.loads(request.content)
        if request.url.path == '/sign-in/handover' and 'receipt' in body:  # the host's own record, read by address
            self.reads.append(body)
            if self.unread:
                self.unread -= 1
                return httpx.Response(503, json={'ok': False})
            asked = body['receipt']
            kept = self.filed.get((asked['legacy_key'], asked['legacy_generation']))
            return httpx.Response(200, json={'ok': True, 'receipt': {
                **asked, **(kept or {'result': 'refused', 'exchange': 'unknown'})}})
        if request.url.path == '/sign-in/handover':
            self.handovers.append(body)
            return self.answers.pop(0)(body)
        assert request.url.path == '/sign-in/token'
        self.tokens.append(body)
        row = self.held.get(body['account'])
        if row is None:
            return httpx.Response(404, json={'ok': False})
        if body.get('connection_id') and body['connection_id'] != row['connection_id']:
            return httpx.Response(409, json={'ok': False, 'code': 'SIGN_IN_REQUIRED', 'reason': 'connection_changed'})
        if body.get('consent') and body['consent'] != row.get('consent'):
            return httpx.Response(409, json={'ok': False, 'code': 'SIGN_IN_REQUIRED', 'reason': 'consent_changed'})
        return httpx.Response(200, json={'ok': True, 'account': {'key': KEY}, 'connection_id': row['connection_id'],
                                         **({'consent': row['consent']} if row.get('consent') else {}),
                                         'token_type': 'Bearer', 'scope': row['scope'], 'expires_at': time.time() + 3600,
                                         'access_token': 'fixture-fresh' if body.get('rejected') else self.access})

    def imported(self, body):
        item, = body['grants']
        # The imported grant and its receipt name its own consent, as a host naming consents answers.
        self.held[KEY] = {**listed('cn-1', generation=item['legacy_generation']), 'consent': IMPORTED}
        self.filed[(item['legacy_key'], item['legacy_generation'])] = {
            'result': 'imported', 'account': {'key': KEY}, 'consent': IMPORTED, 'connection_id': 'cn-1'}
        return httpx.Response(200, json={'ok': True, 'grants': [{
            'legacy_key': item['legacy_key'], 'result': 'imported', 'account': {'key': KEY}, 'consent': IMPORTED,
            'legacy_generation': item['legacy_generation'], 'connection_id': 'cn-1'}]})

    @staticmethod
    def started_nothing(body):
        item, = body['grants']
        return httpx.Response(200, json={'ok': True, 'grants': [{
            'legacy_key': item['legacy_key'], 'result': 'refused', 'reason': 'unknown_client', 'exchange': 'none'}]})

    @staticmethod
    def busy(_body):
        return httpx.Response(429, json={'ok': False, 'error': 'rate limit exceeded', 'handover_started': False})


def test_a_known_return_moves_on_version_1_and_a_busy_host_keeps_the_saved_sign_in_serving(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads = []
    host = Host()

    def busy_with_preferences(body):
        store.update_preferences('google:one', {'label': 'Renamed meanwhile', 'folder': 'INBOX'})
        return host.busy(body)

    host.answers = [busy_with_preferences, host.imported]
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        sign_in = GmailSignIn(*HOST, http=wire)
        store = AccountStore(tmp_path, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-default-client'}, sign_in=sign_in)
        before = mailbox(store)
        sync_gmail(store)
        after = store.get('google:one')
        assert len(host.handovers) == 1 and after['connected'] and after['gmail_transfer']['refusal'] == 'not_started'
        assert after['label'] == 'Renamed meanwhile'
        assert [after[key] for key in ('oauth', 'tokens', 'scopes')] == [before[key] for key in ('oauth', 'tokens', 'scopes')]
        assert GmailClient(store, 'google:one', http=mail).test_connection()['ok'] and reads == ['GET']
        sync_gmail(store)
        assert len(host.handovers) == 1  # an offer that started nothing waits before it is made again
        sign_in.offers['google:one'] = (0, 30)  # that pause has passed
        sync_gmail(store)
    offer = host.handovers[-1]
    assert not offer.get('recover') and 'require_verified' not in offer['grants'][0]  # version 1 is not asked for it
    assert offer['clients'] == [{'client_id': 'fixture-client', 'client_secret': 'fixture-secret',
                                 'label_hint': 'Mail', 'return_hint': RETURN}]  # no default client is registered
    assert offer['grants'][0]['tokens']['refresh_token'] == 'fixture-old-refresh'
    moved = store.get('google:one')
    assert moved['gmail_host']['key'] == KEY and moved['connected'] and 'gmail_transfer' not in moved


def test_a_transfer_that_started_nothing_keeps_the_old_sign_in_and_is_offered_again_later(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads = []
    host = Host()
    host.answers = [host.started_nothing, host.imported]
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        sign_in = GmailSignIn(*HOST, http=wire)
        store = AccountStore(tmp_path, sign_in=sign_in)
        mailbox(store)
        sync_gmail(store)
        assert store.get('google:one')['gmail_transfer']['refusal'] == 'not_started'
        _, (row,) = read_view(store)
        assert row['connected'] and row['auth_owner'] == 'legacy'
        assert GmailClient(store, 'google:one', http=mail).test_connection()['ok'] and reads == ['GET']
        sync_gmail(store)
        assert len(host.handovers) == 1
        sign_in.offers['google:one'] = (0, 30)  # a later pass
        sync_gmail(store)
    first, second = host.handovers
    assert second['grants'][0]['legacy_generation'] != first['grants'][0]['legacy_generation']
    assert store.get('google:one')['gmail_host']['key'] == KEY


def test_an_unrecorded_return_waits_unreserved_until_the_host_announces_version_2(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads = []
    host = Host()
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        before = mailbox(store, hint='')
        sync_gmail(store)
        after = store.get('google:one')
        assert host.handovers == [] and 'gmail_transfer' not in after and after['generation'] == before['generation']
        _, (row,) = read_view(store)
        assert row['connected'] and row['auth_owner'] == 'legacy' and row['gmail_auth_state'] == 'signed_in'
        assert GmailClient(store, 'google:one', http=mail).test_connection()['ok'] and reads == ['GET']
    updated = Host(handover=2)
    updated.answers = [updated.imported]
    with httpx.Client(transport=httpx.MockTransport(updated)) as wire:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        sync_gmail(store)
    offer, = updated.handovers
    assert offer['clients'][0]['return_hint'] == 'unknown'
    assert store.get('google:one')['gmail_host']['key'] == KEY


def test_mail_queued_before_a_disconnect_is_never_sent_through_the_next_sign_in(tmp_path, monkeypatch):
    from lib import service as mail_service

    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        assert request.headers['Authorization'] == 'Bearer fixture-host-access'
        sent.append(base64.urlsafe_b64decode(json.loads(request.content)['raw'] + '==='))
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    host = Host()
    host.held[KEY] = listed('cn-1', via='card')
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        sign_in = GmailSignIn(*HOST, http=wire)
        monkeypatch.setattr(mail_service.GmailSignIn, 'environment', classmethod(lambda cls: sign_in))
        monkeypatch.setattr(mail_service, 'account_client',
                            lambda accounts, row, **_: GmailClient(accounts, row['id'], http=mail))
        accounts = AccountStore(tmp_path, sign_in=sign_in)
        account = mailbox(accounts, gmail_host=dict(MOVED))
        service = mail_service.MailService(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda keys: {}))
        store = EmailStore(accounts.mail_dir(account))
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')

        def queue(request_id):
            result = service.send(account_id='google:one', to='reader@example.test', subject='Fixture', body=request_id,
                                  request_id=request_id)
            assert result['ok'] and result['state'] == 'pending'

        def deliver():
            with store._connect() as db:
                db.execute('UPDATE outbox SET available_at=0')
            while runtime.process_outbox():
                pass

        queue('first')
        deliver()
        queue('second')
        host.held[KEY] = {**host.held[KEY], 'client_label': 'Work client'}  # an ordinary new sign-in: same connection
        sync_gmail(accounts)
        deliver()
        queue('third')
        del host.held[KEY]  # Disconnect on the card
        sync_gmail(accounts)
        assert not accounts.get('google:one')['connected']
        host.held[KEY] = listed('cn-2', via='card')  # the same Google account signs in again
        sync_gmail(accounts)
        queue('fourth')
        deliver()
    third = store.receipt('third')
    assert third['state'] == 'held' and 'never sent' in third['last_error']
    assert [store.receipt(rid)['state'] for rid in ('first', 'second', 'fourth')] == ['completed'] * 3
    assert len(sent) == 3 and [body.get('connection_id') for body in host.tokens] == ['cn-1', 'cn-1', 'cn-2']


@pytest.mark.parametrize('with_scope', [True, False])
def test_an_older_host_serves_a_moved_mailbox_after_the_owner_signs_in_there(tmp_path, monkeypatch, with_scope):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')

    def provider(request):
        path = request.url.path
        if path == '/token':
            form = parse_qs(request.content.decode())
            assert form['grant_type'] == ['authorization_code'] and form['client_id'] == ['fixture-new-client']
            return httpx.Response(200, json={'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh',
                                             'expires_in': 3600,
                                             **({'scope': ' '.join(MAIL_SCOPES)} if with_scope else {})})
        if path.endswith('/userinfo'):
            return httpx.Response(200, json={'sub': 'one', 'email': 'one@example.test', 'email_verified': True})
        if path.endswith('/settings/sendAs'):
            return httpx.Response(200, json={'sendAs': []})
        assert path.endswith('/profile') and request.headers['Authorization'] == 'Bearer fixture-new-access'
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    host = Host(older=True)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as http:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        store.save({'id': 'google:one', 'provider': 'gmail', 'address': 'one@example.test', 'label': 'One',
                    'senders': ['one@example.test'], 'sender': 'one@example.test', 'folder': 'INBOX', 'sending': 'off',
                    'words': [], 'poll_interval_sec': 30, 'config': {}, 'scopes': list(MAIL_SCOPES),
                    'gmail_host': {**MOVED, 'legacy_generation': 'fixture-generation'}})
        with pytest.raises(AccountError, match='Sign in to Google again in Email accounts') as refused:
            GmailClient(store, 'google:one', http=http).test_connection()
        assert 'Extensions' not in str(refused.value)
        _, (row,) = read_view(store)
        assert not row['connected'] and row['auth_owner'] == 'legacy' and row['gmail_auth_state'] == 'needs_sign_in'
        opened = begin(store, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-new-client'}, origin='http://127.0.0.1:8765',
                       verify_return=False)
        state = parse_qs(urlsplit(opened['authorization_url']).query)['state'][0]
        reply = finish(store, state=state, code='fixture-code', http=http)
        assert reply['ok'] and 'Extensions' not in json.dumps(reply)
        assert GmailClient(store, 'google:one', http=http).test_connection()['ok']
        saved = store.get('google:one')
        assert saved['sending'] == 'off' and saved['gmail_host']['key'] == KEY  # its ID, preferences and transfer stay
        late = saved['gmail_late_consent']
        assert late['oauth']['return_hint'] == 'exact:http://127.0.0.1:8765/onboarding/static/provider-return.html'
        _, (row,) = read_view(store)
        assert row['connected'] and row['auth_owner'] == 'legacy' and row['gmail_auth_state'] == 'signed_in'
        assert row['scopes'] == (list(MAIL_SCOPES) if with_scope else None)

    newer = Host(handover=2)
    newer.held[KEY] = {**listed('cn-2', via='card'), 'consent': IMPORTED}
    with httpx.Client(transport=httpx.MockTransport(newer)) as wire:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        sync_gmail(store)
        after = store.get('google:one')
        assert after['gmail_late_consent'] == late and after['gmail_connection'] == saved['gmail_connection']
        assert read_view(store)[1][0]['auth_owner'] == 'host' and newer.handovers == []
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as http:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        assert GmailClient(store, 'google:one', http=http).test_connection()['ok']


def test_a_refused_host_bearer_on_a_single_read_is_replaced_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    letter = EmailMessage()
    letter['From'], letter['To'], letter['Subject'] = 'sender@example.test', 'one@example.test', 'Fixture letter'
    letter['Message-ID'] = '<fixture-letter@example.test>'
    letter.set_content('Hello.')
    raw = base64.urlsafe_b64encode(letter.as_bytes()).decode().rstrip('=')
    presented = []

    def provider(request):
        assert request.method == 'GET' and request.url.path == '/mail/messages/abc123'
        presented.append(request.headers['Authorization'])
        if request.headers['Authorization'] == 'Bearer fixture-stale':
            return httpx.Response(401, json={'error': {'errors': [{'reason': 'authError'}]}})
        return httpx.Response(200, json={'id': 'abc123', 'labelIds': ['INBOX'], 'raw': raw})

    host = Host()
    host.access = 'fixture-stale'
    host.held[KEY] = listed('cn-1', via='card')
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store, gmail_host=dict(MOVED))
        message = GmailClient(store, 'google:one', http=mail).read(message_ref='gmail:abc123')
    assert message['subject'] == 'Fixture letter'
    assert presented == ['Bearer fixture-stale', 'Bearer fixture-fresh']
    assert [body.get('rejected') for body in host.tokens] == [None, 'fixture-stale']
    assert all('min_valid' not in body for body in host.tokens)


def test_a_refresh_token_google_replaces_is_kept_through_an_unrelated_edit(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    accounts = AccountStore(tmp_path)
    mailbox(accounts, tokens={'refresh_token': 'fixture-old-refresh', 'expires_at': 0})

    def provider(request):
        if request.url.path == '/token':
            accounts.update_preferences('google:one', {'label': 'Renamed meanwhile'})
            return httpx.Response(200, json={'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh',
                                             'expires_in': 3600})
        assert request.url.path == '/mail/profile' and request.headers['Authorization'] == 'Bearer fixture-new-access'
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    with httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        assert GmailClient(accounts, 'google:one', http=mail).test_connection()['ok']
    saved = accounts.get('google:one')
    assert saved['label'] == 'Renamed meanwhile' and saved['tokens']['refresh_token'] == 'fixture-new-refresh'


def test_a_narrower_renewal_keeps_ordinary_mail_and_the_refresh_token_google_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    host, reads = Host(older=True), []

    def provider(request):
        if request.url.path == '/token':
            # Asked for the recorded permissions, Google answers without the optional settings permission.
            assert parse_qs(request.content.decode())['scope'] == [' '.join([*MAIL_SCOPES, SETTINGS_SCOPE])]
            return httpx.Response(200, json={'access_token': 'fixture-narrow', 'refresh_token': 'fixture-replaced',
                                             'scope': ' '.join(MAIL_SCOPES), 'expires_in': 3600})
        assert request.url.path == '/mail/profile' and request.headers['Authorization'] == 'Bearer fixture-narrow'
        reads.append(request.method)
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store, scopes=[*MAIL_SCOPES, SETTINGS_SCOPE],
                tokens={'refresh_token': 'fixture-old-refresh', 'expires_at': 0},
                gmail_host={**MOVED, 'legacy_generation': 'fixture-generation'})
        client = GmailClient(store, 'google:one', http=mail)
        assert client.test_connection()['scopes'] == list(MAIL_SCOPES)  # what this bearer was issued with
        denied = client.settings_write(resource='language', values={'displayLanguage': 'en'})
        _, (row,) = read_view(store)
    assert denied['code'] == 'settings_permission_missing' and reads == ['GET'] and row['scopes'] == list(MAIL_SCOPES)
    saved = store.get('google:one')
    assert saved['scopes'] == [*MAIL_SCOPES, SETTINGS_SCOPE]  # the recorded limit stays for a later renewal or transfer
    assert saved['tokens']['refresh_token'] == 'fixture-replaced' and saved['tokens']['scope'] == ' '.join(MAIL_SCOPES)
    assert 'refreshing' not in saved['tokens']


@pytest.mark.parametrize('granted', [['openid', 'email', SETTINGS_SCOPE], [*MAIL_SCOPES, FULL_MAIL_SCOPE]])
def test_a_renewal_without_gmail_access_or_beyond_the_recorded_permissions_is_refused(tmp_path, granted):
    host = Host(older=True)

    def provider(request):
        assert request.url.path == '/token'
        return httpx.Response(200, json={'access_token': 'fixture-refused', 'refresh_token': 'fixture-replaced',
                                         'scope': ' '.join(granted), 'expires_in': 3600})

    with httpx.Client(transport=httpx.MockTransport(host)) as wire, httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store, scopes=[*MAIL_SCOPES, SETTINGS_SCOPE], tokens={'refresh_token': 'fixture-old-refresh', 'expires_at': 0},
                gmail_host={**MOVED, 'legacy_generation': 'fixture-generation'})
        with pytest.raises(AccountError, match='did not confirm the saved permissions'):
            GmailClient(store, 'google:one', http=mail).test_connection()
    tokens = store.get('google:one')['tokens']
    assert tokens['refresh_token'] == 'fixture-replaced' and tokens.get('access_token') != 'fixture-refused'
    assert 'refreshing' not in tokens


def test_an_older_host_disconnects_a_moved_mailbox_in_email_accounts(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from test_plugin import _settings_app

    app = _settings_app(tmp_path)
    module = sys.modules['email_settings_fixture.lib.gmail_host']
    host = Host(older=True)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, TestClient(app) as http:
        monkeypatch.setattr(module.GmailSignIn, 'environment', classmethod(lambda cls: cls(*HOST, http=wire)))
        mailbox(AccountStore(tmp_path), gmail_host={**MOVED, 'legacy_generation': 'fixture-generation'})
        listed_row, = http.get('/accounts').json()['accounts']
        assert listed_row['connected'] and listed_row['auth_owner'] == 'legacy'
        answer = http.post('/accounts/disconnect', json={'account_id': 'google:one'})
        assert answer.status_code == 200 and not answer.json()['account']['connected']
        assert 'Extensions' not in answer.text
        status = http.get('/status', params={'account_id': 'google:one'}).json()
        assert 'card' not in status['readiness']['text']
    saved = AccountStore(tmp_path).get('google:one')
    assert saved['gmail_connection'] == {'ended': True} and 'tokens' not in saved


@pytest.mark.parametrize('via', ['handover', 'card'])
def test_queued_mail_reaches_the_host_only_through_continuous_access(tmp_path, monkeypatch, via):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    older, newer = Host(), Host(handover=2)
    newer.answers = [newer.imported]
    with httpx.Client(transport=httpx.MockTransport(older)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = mailbox(accounts, hint='')  # deferred on version 1: its saved sign-in serves it here
        store = EmailStore(accounts.mail_dir(account))
        store.enqueue_outbox(request_id='unrecorded', to=['reader@example.test'], body='no record of its access')
        link = GmailClient(accounts, 'google:one', http=mail).connection_link()
        store.enqueue_outbox(request_id='local', to=['reader@example.test'], body='queued on the saved sign-in',
                             reporting={'gmail_connection': link})
    if via == 'card':
        newer.held[KEY] = listed('cn-1', via='card')  # the same Google account, signed in anew on the card
    with httpx.Client(transport=httpx.MockTransport(newer)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        sync_gmail(accounts)
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')
        while runtime.process_outbox():
            pass
    assert store.receipt('unrecorded')['state'] == 'held'
    assert store.receipt('local')['state'] == ('completed' if via == 'handover' else 'held')
    assert sent == (['Bearer fixture-host-access'] if via == 'handover' else [])


LIMIT = ' '.join([*MAIL_SCOPES, SETTINGS_SCOPE])


@pytest.mark.parametrize(('own_scope', 'offered', 'carried'), [
    (None, LIMIT, False),  # no record of the bearer's permissions: the saved limit, renewed by the host
    (LIMIT, LIMIT, True),
    (' '.join(MAIL_SCOPES), ' '.join(MAIL_SCOPES), True),  # narrower: it stays narrower, settings do not return
    ('openid email', LIMIT, False),  # a bearer without Gmail access is not carried
    (LIMIT + ' ' + FULL_MAIL_SCOPE, LIMIT, False),  # wider: never passed off as its intersection with the limit
])
def test_a_transfer_carries_a_saved_bearer_only_with_its_own_permissions(tmp_path, own_scope, offered, carried):
    host = Host(handover=2)
    host.answers = [lambda _body: httpx.Response(502, json={'ok': False}), host.imported]  # the first answer is lost
    tokens = {'refresh_token': 'fixture-old-refresh', 'access_token': 'fixture-old-access',
              'expires_at': time.time() + 3600, **({'scope': own_scope, 'scope_of': bearer_mark('fixture-old-access')} if own_scope else {})}
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store, scopes=[*MAIL_SCOPES, SETTINGS_SCOPE], tokens=tokens)
        sync_gmail(store)
        sync_gmail(store)  # asks with recover for what the host kept: the same offer, nothing new
    first, second = host.handovers
    assert second.get('recover') is True and second['grants'] == first['grants']
    grant, = first['grants']
    assert grant['require_verified'] is True and grant['tokens']['scope'] == offered
    assert ('access_token' in grant['tokens']) is carried  # otherwise the host renews it once within the limit
    assert store.get('google:one')['gmail_host']['key'] == KEY


def test_a_local_refresh_and_a_transfer_of_one_sign_in_never_drop_each_others_replacement(tmp_path):
    host = Host(handover=2)
    host.answers = [host.imported]
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        # Kept on its saved sign-in: the host proved that an earlier offer of it started nothing.
        mailbox(store, tokens={'refresh_token': 'fixture-old-refresh', 'expires_at': 0},
                gmail_transfer={'generation': 'fixture-earlier', 'refusal': 'not_started'})
        offers_while_refreshing = []

        def provider(request):
            assert request.url.path == '/token'
            sync_gmail(store)  # the companion's pass while this refresh is in flight
            offers_while_refreshing.append(len(host.handovers))
            return httpx.Response(200, json={'access_token': 'fixture-new-access', 'refresh_token': 'fixture-rotated',
                                             'scope': ' '.join(MAIL_SCOPES), 'expires_in': 3600})

        with httpx.Client(transport=httpx.MockTransport(provider)) as mail:
            assert GmailClient(store, 'google:one', http=mail)._access_token() == 'fixture-new-access'
        assert offers_while_refreshing == [0]
        assert store.get('google:one')['tokens']['refresh_token'] == 'fixture-rotated'
        sync_gmail(store)  # the refresh has ended: the replacement is what moves
    offer, = host.handovers
    assert offer['grants'][0]['tokens']['refresh_token'] == 'fixture-rotated'
    assert store.get('google:one')['gmail_host']['key'] == KEY
    with store.account_lock('google:one') as first:
        with store.account_lock('google:one') as second, store.account_lock('google:two') as other:
            assert first and not second and other  # one holder per account; another mailbox never waits for it
    old = store.get('google:one')
    assert store.refresh_tokens('google:one', old['generation'], {'access_token': 'fixture-later'},
                                legacy_owner=True, source='fixture-rotated')
    # A copy read before that renewal is never written back over it.
    assert store.save({**old, 'label': 'Stale'}, expected_generation=old['generation'], expected=old) is None
    assert store.get('google:one')['tokens']['access_token'] == 'fixture-later'


def test_on_a_host_naming_consent_mail_queued_before_a_new_sign_in_stays_held(tmp_path, monkeypatch):
    from lib import service as mail_service

    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    host = Host(handover=2)
    host.held[KEY] = {**listed('cn-1', via='card'), 'consent': 'c' * 32}
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        sign_in = GmailSignIn(*HOST, http=wire)
        monkeypatch.setattr(mail_service.GmailSignIn, 'environment', classmethod(lambda cls: sign_in))
        monkeypatch.setattr(mail_service, 'account_client',
                            lambda accounts, row, **_: GmailClient(accounts, row['id'], http=mail))
        accounts = AccountStore(tmp_path, sign_in=sign_in)
        account = mailbox(accounts, gmail_host=dict(MOVED))
        sync_gmail(accounts)
        service = mail_service.MailService(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda keys: {}))
        store = EmailStore(accounts.mail_dir(account))
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')

        def queue(request_id):
            assert service.send(account_id='google:one', to='reader@example.test', subject='Fixture', body=request_id,
                                request_id=request_id)['ok']

        def deliver():
            with store._connect() as db:
                db.execute('UPDATE outbox SET available_at=0')
            while runtime.process_outbox():
                pass

        queue('before')
        host.held[KEY] = {**host.held[KEY], 'consent': 'd' * 32}  # a new sign-in on the card keeps the connection
        queue('after')  # read from the host's current list, ahead of Mail's next pass
        deliver()
        sync_gmail(accounts)
        deliver()
    before = store.receipt('before')
    assert before['state'] == 'held' and 'never sent' in before['last_error']
    assert store.receipt('after')['state'] == 'completed' and sent == ['Bearer fixture-host-access']
    assert [body.get('consent') for body in host.tokens] == ['c' * 32, 'd' * 32]


def test_mail_bound_to_a_consent_is_not_sent_where_the_host_names_none(tmp_path):
    host = Host()  # version 1: its list names no consent
    host.held[KEY] = listed('cn-1', via='card')
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = mailbox(accounts, gmail_host={**MOVED, 'consent': 'c' * 32})
        sync_gmail(accounts)
        store = EmailStore(accounts.mail_dir(account))
        store.enqueue_outbox(request_id='bound', to=['reader@example.test'], body='queued under a named consent',
                             reporting={'gmail_connection': {'host': 'cn-1', 'consent': 'c' * 32}})
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')
        while runtime.process_outbox():
            pass
    assert 'does not confirm' in store.receipt('bound')['last_error'] and host.tokens == []


def test_a_new_sign_in_on_the_card_ends_the_continuation_of_mail_queued_on_the_saved_sign_in(tmp_path):
    host = Host(handover=2)

    def imported(body):  # the moved grant, as a host naming each sign-in's consent lists it
        answer = host.imported(body)
        host.held[KEY] = {**host.held[KEY], 'consent': 'c' * 32}
        return answer

    host.answers = [imported]
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = mailbox(accounts, gmail_connection={'id': 'fixture-local'})
        store = EmailStore(accounts.mail_dir(account))
        store.enqueue_outbox(request_id='local', to=['reader@example.test'], body='queued on the saved sign-in',
                             reporting={'gmail_connection': {'id': 'fixture-local'}})
        sync_gmail(accounts)
        assert accounts.get('google:one')['gmail_host']['continues'] == 'fixture-local'
        host.held[KEY] = {**listed('cn-1', via='card'), 'consent': 'd' * 32}  # a new sign-in on the same connection
        sync_gmail(accounts)
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')
        while runtime.process_outbox():
            pass
    assert 'never sent' in store.receipt('local')['last_error'] and host.tokens == []


def test_a_mail_is_not_queued_while_its_google_access_cannot_be_named(tmp_path, monkeypatch):
    from lib import service as mail_service

    host = Host()
    host.held[KEY] = listed('cn-1', via='card')
    down = [True]

    def answer(request):  # the host's /identity fails for a moment, as during its restart
        if down[0] and request.url.path == '/identity':
            return httpx.Response(503, json={'ok': False})
        return host(request)

    with httpx.Client(transport=httpx.MockTransport(answer)) as wire:
        monkeypatch.setattr(mail_service.GmailSignIn, 'environment', classmethod(lambda cls: cls(*HOST, http=wire)))
        monkeypatch.setattr(mail_service, 'account_client',
                            lambda accounts, row, **_: GmailClient(accounts, row['id'], http=wire))
        account = mailbox(AccountStore(tmp_path), gmail_host=dict(MOVED))
        service = mail_service.MailService(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda keys: {}))
        store = EmailStore(AccountStore(tmp_path).mail_dir(account))
        with pytest.raises(AccountError, match='Nothing was queued'):
            service.send(account_id='google:one', to='reader@example.test', subject='Fixture', body='later',
                         request_id='outage')
        assert store.receipt('outage') is None
        down[0] = False
        assert service.send(account_id='google:one', to='reader@example.test', subject='Fixture', body='later',
                            request_id='outage')['ok']
    assert store.claim_outbox().reporting['gmail_connection'] == {'host': 'cn-1'}


def test_mail_disconnects_a_mailbox_its_saved_sign_in_serves_and_the_card_keeps_the_rest(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from test_plugin import _settings_app

    app = _settings_app(tmp_path)
    module = sys.modules['email_settings_fixture.lib.gmail_host']
    host = Host()  # version 1: a saved sign-in without a recorded return address keeps serving here
    host.held[KEY] = listed('cn-1', via='card')
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, TestClient(app) as http:
        monkeypatch.setattr(module.GmailSignIn, 'environment', classmethod(lambda cls: cls(*HOST, http=wire)))
        mailbox(AccountStore(tmp_path), gmail_host=dict(MOVED))
        refused = http.post('/accounts/disconnect', json={'account_id': 'google:one'})
        assert refused.status_code == 400 and 'Extensions › Skills' in refused.json()['error']
        del host.held[KEY]
        mailbox(AccountStore(tmp_path), hint='')  # this mailbox's own saved sign-in serves it
        listed_row, = http.get('/accounts').json()['accounts']
        assert listed_row['connected'] and listed_row['auth_owner'] == 'legacy'
        answer = http.post('/accounts/disconnect', json={'account_id': 'google:one'})
        assert answer.status_code == 200 and not answer.json()['account']['connected']
    saved = AccountStore(tmp_path).get('google:one')
    assert saved['gmail_connection'] == {'ended': True} and 'tokens' not in saved


@pytest.mark.parametrize(('receipt_consent', 'listed_consent', 'continues'), [
    (IMPORTED, IMPORTED, True), (OPAQUE, OPAQUE, True), (None, IMPORTED, False), (IMPORTED, 'e' * 32, False)])
def test_on_version_2_local_mail_continues_only_under_the_consent_its_transfer_receipt_names(
        tmp_path, receipt_consent, listed_consent, continues):
    host = Host(handover=2)

    def imported(body):  # what the host files and lists, and what its receipt names
        item, = body['grants']
        host.held[KEY] = {**listed('cn-1', generation=item['legacy_generation']), 'consent': listed_consent}
        return httpx.Response(200, json={'ok': True, 'grants': [{
            'legacy_key': item['legacy_key'], 'result': 'imported', 'account': {'key': KEY},
            **({'consent': receipt_consent} if receipt_consent else {}),
            'legacy_generation': item['legacy_generation'], 'connection_id': 'cn-1'}]})

    host.answers = [imported]
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(accounts, gmail_connection={'id': 'fixture-local'})
        sync_gmail(accounts)
    binding = accounts.get('google:one')['gmail_host']
    assert binding['key'] == KEY and (binding.get('continues') == 'fixture-local') is continues


def test_a_bearer_without_a_record_of_its_permissions_shows_them_as_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads = []
    host = Host()
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store, hint='')  # its own saved sign-in serves it here; an earlier version saved its bearer
        _, (row,) = read_view(store)
        result = GmailClient(store, 'google:one', http=mail).test_connection()
    assert row['auth_owner'] == 'legacy' and AccountStore.public(row)['scope_status'] == 'unknown'
    assert result['ok'] and result['scope_status'] == 'unknown' and reads == ['GET']  # ordinary reading still works


def test_a_new_local_sign_in_holds_mail_queued_under_the_one_it_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert Google and Gmail: nothing is delivered anywhere
        path = request.url.path
        if path == '/token':
            form = parse_qs(request.content.decode())
            assert form['grant_type'] == ['authorization_code'] and form['client_id'] == ['fixture-other-client']
            return httpx.Response(200, json={'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh',
                                             'scope': ' '.join(MAIL_SCOPES), 'expires_in': 3600})
        if path.endswith('/userinfo'):
            return httpx.Response(200, json={'sub': 'one', 'email': 'one@example.test', 'email_verified': True})
        if path.endswith('/settings/sendAs'):
            return httpx.Response(200, json={'sendAs': []})
        if path.endswith('/profile'):
            return httpx.Response(200, json={'emailAddress': 'one@example.test'})
        assert request.method == 'POST' and path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        store = AccountStore(tmp_path)  # an Ouroboros without the sign-in service: Mail's own sign-in serves
        account = mailbox(store)
        outbox = EmailStore(store.mail_dir(account))
        outbox.enqueue_outbox(request_id='before', to=['reader@example.test'], body='queued under the first sign-in',
                              reporting={'gmail_connection': GmailClient(store, 'google:one', http=http).connection_link()})
        opened = begin(store, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-other-client'}, origin='http://127.0.0.1:8765',
                       verify_return=False)
        state = parse_qs(urlsplit(opened['authorization_url']).query)['state'][0]
        assert finish(store, state=state, code='fixture-code', http=http)['ok']  # signed in again, with another client
        outbox.enqueue_outbox(request_id='after', to=['reader@example.test'], body='queued under the new sign-in',
                              reporting={'gmail_connection': GmailClient(store, 'google:one', http=http).connection_link()})
        runtime = EmailRuntime(outbox, GmailClient(store, 'google:one', http=http), SimpleNamespace(), account='google:one')
        while runtime.process_outbox():
            pass
    assert 'never sent' in outbox.receipt('before')['last_error']
    assert outbox.receipt('after')['state'] == 'completed' and sent == ['Bearer fixture-new-access']


def test_a_filed_transfer_whose_answer_was_lost_is_settled_by_its_receipt_without_a_new_offer(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    host = Host(handover=2)

    def filed_but_lost(body):  # the host files the import; its answer never arrives
        host.imported(body)
        return httpx.Response(504, json={'ok': False})

    host.answers = [filed_but_lost, lambda _body: httpx.Response(503, json={'ok': False})]  # then a busy recover
    host.unread = 1  # and the next run's first read of the receipt fails as well
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = mailbox(accounts, gmail_connection={'id': 'fixture-local'})
        store = EmailStore(accounts.mail_dir(account))
        store.enqueue_outbox(request_id='local', to=['reader@example.test'], body='queued on the saved sign-in',
                             reporting={'gmail_connection': {'id': 'fixture-local'}})
        sync_gmail(accounts)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        sign_in = GmailSignIn(*HOST, http=wire)  # the companion's next run: the list already shows the import
        accounts = AccountStore(tmp_path, sign_in=sign_in)
        sync_gmail(accounts)
        assert 'gmail_transfer' in accounts.get('google:one') and 'gmail_host' not in accounts.get('google:one')
        sign_in.pauses.clear()  # the next pause has passed
        sync_gmail(accounts)
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')
        while runtime.process_outbox():
            pass
    assert [bool(body.get('recover')) for body in host.handovers] == [False, True] and len(host.reads) == 2
    binding = accounts.get('google:one')['gmail_host']
    assert binding['continues'] == 'fixture-local' and binding['continues_consent'] == IMPORTED
    assert store.receipt('local')['state'] == 'completed' and sent == ['Bearer fixture-host-access']


def test_the_addressed_final_refresh_refusal_stops_checking_the_same_offer(tmp_path):
    host = Host(handover=2)
    host.answers = [lambda _body: httpx.Response(503, json={'ok': False})]
    reads = []

    def wire(request):
        if request.url.path == '/sign-in/handover':
            body = json.loads(request.content)
            if 'receipt' in body:
                reads.append(body['receipt'])
                return httpx.Response(200, json={'ok': True, 'receipt': {
                    'legacy_key': 'google:one', 'result': 'refused', 'reason': 'refresh_rejected',
                    'exchange': 'unknown'}})
        return host(request)

    with httpx.Client(transport=httpx.MockTransport(wire)) as http:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=http))
        mailbox(store)
        sync_gmail(store)
        waiting = store.get('google:one')
        assert not waiting['connected'] and 'refusal' not in waiting['gmail_transfer']
        generation = waiting['gmail_transfer']['generation']
        sync_gmail(store)
        settled = store.get('google:one')
        assert not settled['connected'] and settled['gmail_transfer']['refusal'] == 'refresh_rejected'
        reopened = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=http))
        sync_gmail(reopened)
        assert reopened.get('google:one')['gmail_transfer'] == settled['gmail_transfer']
    assert reads == [{'legacy_key': 'google:one', 'legacy_generation': generation}]
    assert len(host.handovers) == 1 and not host.handovers[0].get('recover')


def test_permissions_count_only_beside_the_mark_of_their_own_bearer(tmp_path):
    store = AccountStore(tmp_path)  # an Ouroboros without the sign-in service: Mail's own sign-in serves this mailbox
    # An earlier version renewed this bearer after a rollback: a new access token beside the last one's scope and mark.
    mailbox(store, scopes=[*MAIL_SCOPES, SETTINGS_SCOPE],
            tokens={'refresh_token': 'fixture-old-refresh', 'access_token': 'fixture-rolled-back',
                    'expires_at': time.time() + 3600, 'scope': LIMIT, 'scope_of': bearer_mark('fixture-old-access')})
    _, (row,) = read_view(store)
    assert AccountStore.public(row)['scope_status'] == 'unknown'
    tokens = store.get('google:one')['tokens']
    assert bearer_scopes(tokens, [*MAIL_SCOPES, SETTINGS_SCOPE]) is None
    assert bearer_scopes({**tokens, 'scope_of': bearer_mark('fixture-rolled-back')}, list(MAIL_SCOPES)) == list(MAIL_SCOPES)


def test_an_os_failure_of_the_account_lock_is_raised_never_reported_as_busy():
    from lib.accounts import _try_lock

    class Unopened:  # a descriptor no file is open on: no other holder
        def seek(self, _offset):
            return 0

        def fileno(self):
            return 2 ** 30

    with pytest.raises(OSError):
        _try_lock(Unopened())


def test_a_new_consent_without_a_new_refresh_token_holds_earlier_mail_and_keeps_a_later_rotation(tmp_path):
    store = AccountStore(tmp_path)  # no sign-in host: Mail's own sign-in serves this mailbox
    account = mailbox(store)
    earlier = store.gmail_connection('google:one')
    mail = EmailStore(store.mail_dir(account))
    mail.enqueue_outbox(request_id='earlier', to=['reader@example.test'], body='queued before the new consent',
                        reporting={'gmail_connection': {'id': earlier}})

    def google(request):  # an inert Google: the same client's new consent, answered without a refresh token
        path = request.url.path
        if path == '/token':
            return httpx.Response(200, json={'access_token': 'fixture-new-access', 'scope': ' '.join(MAIL_SCOPES),
                                             'expires_in': 3600})
        if path.endswith('/userinfo'):
            return httpx.Response(200, json={'sub': 'one', 'email': 'one@example.test', 'email_verified': True})
        if path.endswith('/settings/sendAs'):  # meanwhile an ordinary renewal replaced the saved refresh token
            current = store.get('google:one')
            store.save({**current, 'tokens': {**current['tokens'], 'refresh_token': 'fixture-rotated'}})
            return httpx.Response(200, json={'sendAs': []})
        assert path.endswith('/profile')  # nothing is sent
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    opened = begin(store, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-client'}, origin='http://127.0.0.1:8765',
                   verify_return=False)
    state = parse_qs(urlsplit(opened['authorization_url']).query)['state'][0]
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        assert finish(store, state=state, code='fixture-code', http=http)['ok']
        runtime = EmailRuntime(mail, GmailClient(store, 'google:one', http=http), SimpleNamespace(), account='google:one')
        while runtime.process_outbox():
            pass
    saved = store.get('google:one')
    assert saved['tokens']['refresh_token'] == 'fixture-rotated'  # never the value read before that renewal
    assert saved['gmail_connection']['id'] != earlier and mail.receipt('earlier')['state'] == 'held'


def test_a_transfer_filed_before_version_2_serves_new_mail_and_holds_the_earlier(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    host = Host(handover=2)  # updated after an earlier Mail parked this mailbox on its version 1 receipt
    host.held[KEY] = {**listed('cn-1', generation='fixture-generation'), 'consent': IMPORTED}
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = mailbox(accounts, gmail_connection={'id': 'fixture-local'},
                          gmail_transfer={'generation': 'fixture-generation', 'renewal': False},
                          gmail_host={'key': KEY, 'verified': False, 'state': 'sign_in_required'})
        store = EmailStore(accounts.mail_dir(account))
        store.enqueue_outbox(request_id='earlier', to=['reader@example.test'], body='queued before the update',
                             reporting={'gmail_connection': {'id': 'fixture-local'}})
        sync_gmail(accounts)
        client = GmailClient(accounts, 'google:one', http=mail)
        store.enqueue_outbox(request_id='new', to=['reader@example.test'], body='a new command',
                             reporting={'gmail_connection': client.connection_link()})
        runtime = EmailRuntime(store, client, SimpleNamespace(), account='google:one')
        while runtime.process_outbox():
            pass
    binding = accounts.get('google:one')['gmail_host']
    assert binding['verified'] and not binding.get('continues_consent') and host.reads == host.handovers == []
    assert store.receipt('earlier')['state'] == 'held' and store.receipt('new')['state'] == 'completed'
    assert sent == ['Bearer fixture-host-access']


def test_a_first_offer_that_never_reached_the_host_keeps_the_saved_sign_in_and_is_offered_again(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads, refused = [], [True]
    host = Host(handover=2)
    host.answers = [host.imported]

    def wire(request):
        if refused[0] and request.url.path == '/sign-in/handover':
            refused[0] = False
            raise httpx.ConnectError('fixture: connection refused', request=request)
        return host(request)

    with httpx.Client(transport=httpx.MockTransport(wire)) as http, httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        sign_in = GmailSignIn(*HOST, http=http)
        store = AccountStore(tmp_path, sign_in=sign_in)
        original = mailbox(store)
        sync_gmail(store)
        kept = store.get('google:one')
        assert not refused[0] and kept['connected'] and kept['gmail_transfer']['refusal'] == 'not_started'
        assert [kept[key] for key in ('oauth', 'tokens', 'scopes')] == [original[key] for key in ('oauth', 'tokens', 'scopes')]
        assert GmailClient(store, 'google:one', http=mail).test_connection()['ok'] and reads == ['GET']
        sync_gmail(store)
        assert host.handovers == host.reads == []
        sign_in.offers['google:one'] = (0, 30)
        sync_gmail(store)
    offer, = host.handovers
    assert not offer.get('recover') and store.get('google:one')['gmail_host']['key'] == KEY


def test_a_mailbox_no_transfer_reserved_keeps_its_changes_within_its_recorded_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    drafts = []

    def provider(request):
        assert request.method == 'POST' and request.url.path == '/mail/drafts'
        drafts.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': 'fixture-draft', 'message': {'id': 'abc123'}})

    host = Host(handover=2)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store)
        # The saved client registrations differ. Neither is chosen for automatic transfer.
        store.save({**store.get('google:one'), 'id': 'google:two', 'address': 'two@example.test', 'label': 'Two',
                    'senders': ['two@example.test'], 'sender': 'two@example.test',
                    'oauth': {'client_id': 'fixture-client', 'client_secret': 'fixture-other-secret', 'return_hint': RETURN},
                    'tokens': {'refresh_token': 'fixture-two-refresh'}})
        sync_gmail(store)
        assert host.handovers == [] and 'gmail_transfer' not in store.get('google:one')
        client = GmailClient(store, 'google:one', http=mail)
        assert client.draft(to='reader@example.test', subject='Fixture', body='A draft')['ok']
        assert client.connection_link() == {'id': store.get('google:one')['gmail_connection']['id']}
    assert drafts == ['Bearer fixture-old-access']


def test_one_read_of_the_hosts_list_serves_an_operations_reads_and_never_a_change(tmp_path):
    lists, host = [], Host(handover=2)

    def wire(request):
        if request.url.path == '/sign-in/accounts':
            lists.append(request.method)
        return host(request)

    with httpx.Client(transport=httpx.MockTransport(wire)) as http:
        sign_in = GmailSignIn(*HOST, http=http)
        store = AccountStore(tmp_path, sign_in=sign_in)
        account = mailbox(store)
        operation = []

        def read_accounts():
            if not operation:
                operation.append(sign_in.accounts())
            return operation[0]

        owners = [credential_owner(store, account, reading=True, read_accounts=read_accounts) for _ in range(3)]
        assert owners == ['legacy'] * 3 and len(lists) == 1
        assert credential_owner(store, account, read_accounts=read_accounts) == 'legacy'
        assert credential_owner(store, account, reading=True) == 'legacy'
    assert len(lists) == 3


def test_a_change_asks_for_a_bearer_that_outlasts_its_request_under_the_consent_the_host_names(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    posted, short = [], [False]

    def provider(request):
        if request.method == 'POST':
            posted.append(request.url.path)
            return httpx.Response(200, json={'id': 'fixture-draft', 'message': {'id': 'abc123'}})
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    host = Host(handover=2)
    host.held[KEY] = {**listed('cn-1', via='card'), 'consent': OPAQUE}

    def wire(request):
        if short[0] and request.url.path == '/sign-in/token' and 'min_valid' in json.loads(request.content):
            return httpx.Response(409, json={'ok': False, 'code': 'SIGN_IN_REQUIRED', 'reason': 'min_valid_unmet'})
        return host(request)

    with httpx.Client(transport=httpx.MockTransport(wire)) as http, httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=http))
        mailbox(store, gmail_host=dict(MOVED))
        client = GmailClient(store, 'google:one', http=mail)
        assert client.test_connection()['ok']
        assert client.draft(to='reader@example.test', subject='Fixture', body='A draft')['ok']
        short[0] = True
        with pytest.raises(AccountError, match='stays valid for this change'):
            client.draft(to='reader@example.test', subject='Fixture', body='Another draft')
    assert posted == ['/mail/drafts']
    assert [(item.get('consent'), item.get('min_valid')) for item in host.tokens] == [(None, None), (OPAQUE, 30)]


@pytest.mark.parametrize('late_receipt', [False, True], ids=['card-list', 'transfer-receipt'])
def test_a_later_sign_in_on_an_older_host_outlives_a_card_disconnect_recorded_before_it(tmp_path, monkeypatch, late_receipt):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')

    def google(request):
        path = request.url.path
        if path == '/token':
            return httpx.Response(200, json={'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh',
                                             'scope': ' '.join(MAIL_SCOPES), 'expires_in': 3600})
        if path.endswith('/userinfo'):
            return httpx.Response(200, json={'sub': 'one', 'email': 'one@example.test', 'email_verified': True})
        if path.endswith('/settings/sendAs'):
            return httpx.Response(200, json={'sendAs': []})
        assert path.endswith('/profile')
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    def disconnected(body):
        item, = body['grants']
        assert body.get('recover') is True
        return httpx.Response(200, json={'ok': True, 'grants': [
            {'legacy_key': item['legacy_key'], 'result': 'refused', 'reason': 'account_disconnected'}]})

    newer, older = Host(), Host(older=True)
    if late_receipt:
        newer.answers = [lambda _body: httpx.Response(502, json={'ok': False}), disconnected]
    else:
        newer.held[KEY] = listed('cn-1', via='card')
    with httpx.Client(transport=httpx.MockTransport(newer)) as wire:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = (mailbox(accounts, gmail_connection={'id': 'fixture-local'}) if late_receipt
                   else mailbox(accounts, gmail_host=dict(MOVED)))
        earlier = {'id': 'fixture-local'} if late_receipt else {'host': 'cn-1'}
        outbox = EmailStore(accounts.mail_dir(account))
        outbox.enqueue_outbox(request_id='before', to=['reader@example.test'], body='Queued before disconnect',
                              reporting={'gmail_connection': earlier})
        sync_gmail(accounts)
        newer.held.clear()
        (tmp_path / 'sign_in_withdrawn.json').write_text(json.dumps({'v': 1, 'accounts': [
            {'service': 'google', 'account': KEY, 'subject': 'one', 'connection_id': 'cn-1', 'via': 'card'}]}),
            encoding='utf-8')
        if not late_receipt:
            sync_gmail(accounts)
        assert not accounts.get('google:one')['connected']
    with httpx.Client(transport=httpx.MockTransport(older)) as wire, httpx.Client(transport=httpx.MockTransport(google)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        opened = begin(accounts, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-new-client'}, origin='http://127.0.0.1:8765',
                       verify_return=False)
        state = parse_qs(urlsplit(opened['authorization_url']).query)['state'][0]
        assert finish(accounts, state=state, code='fixture-code', http=mail)['ok']
        signed = accounts.get('google:one')
        new_link = GmailClient(accounts, 'google:one', http=mail).connection_link()
        assert new_link == signed['gmail_connection'] and new_link != earlier
    with httpx.Client(transport=httpx.MockTransport(newer)) as wire:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        sync_gmail(accounts)
        kept = accounts.get('google:one')
        assert [kept[name] for name in ('gmail_late_consent', 'gmail_connection', 'connected')] == [
            signed[name] for name in ('gmail_late_consent', 'gmail_connection', 'connected')]
        assert not {'oauth', 'tokens', 'scopes'} & kept.keys()
        if late_receipt:
            assert kept['gmail_transfer']['withdrawn']
            assert [bool(body.get('recover')) for body in newer.handovers] == [False, True]
        else:
            assert kept['gmail_host']['state'] == 'disconnected'
        _, (row,) = read_view(accounts)
        assert not row['connected'] and row['gmail_auth_state'] == 'disconnected'
    with httpx.Client(transport=httpx.MockTransport(older)) as wire, httpx.Client(transport=httpx.MockTransport(google)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        client = GmailClient(accounts, 'google:one', http=mail)
        assert client.test_connection()['ok'] and client.connection_link() == new_link
    assert outbox.receipt('before')['delivery_reporting']['gmail_connection'] == earlier
