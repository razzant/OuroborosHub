import pytest

from lib.client import MailClient


def test_xoauth2_formats_imap_and_smtp_and_finishes_error_challenge():
    client = MailClient({"EMAIL_USER": "test@example.org", "EMAIL_AUTH_MODE": "oauth2", "EMAIL_OAUTH_ACCESS_TOKEN": "test-token"})
    class IMAP:
        def authenticate(self, mechanism, callback):
            assert mechanism == "XOAUTH2"
            assert callback(b"") == b"user=test@example.org\x01auth=Bearer test-token\x01\x01"
            assert callback(b'{"status":"401"}') == b""
    class SMTP:
        def auth(self, mechanism, callback):
            assert mechanism == "XOAUTH2"
            assert callback() == "user=test@example.org\x01auth=Bearer test-token\x01\x01"
            assert callback(b'{"status":"401"}') == ""
    client._auth(IMAP())
    client._auth(SMTP(), smtp=True)


def test_refresh_uses_existing_credentials_and_caches_access_token(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {"access_token": "refreshed", "expires_in": 3600}
    def post(url, data, timeout):
        assert url == "https://oauth2.googleapis.com/token"
        assert data == {"grant_type": "refresh_token", "refresh_token": "refresh", "client_id": "client", "client_secret": "secret"}
        calls.append(data)
        return Response()
    monkeypatch.setattr("lib.client.httpx.post", post)
    client = MailClient({"EMAIL_OAUTH_REFRESH_TOKEN": "refresh", "EMAIL_OAUTH_CLIENT_ID": "client", "EMAIL_OAUTH_CLIENT_SECRET": "secret"})
    assert client._access_token() == "refreshed"
    assert client._access_token() == "refreshed"
    assert len(calls) == 1


def test_refresh_error_never_echoes_provider_response_or_secret(monkeypatch):
    class Response:
        status_code = 401
        text = "credential secret"
    monkeypatch.setattr("lib.client.httpx.post", lambda *a, **kw: Response())
    with pytest.raises(RuntimeError, match=r"OAuth token refresh failed \(HTTP 401\)"):
        MailClient({"EMAIL_OAUTH_REFRESH_TOKEN": "refresh"})._access_token()


def test_two_gmail_grants_follow_the_host_without_crossing_history_or_refresh_owners(tmp_path, monkeypatch):
    import base64
    import json
    import time
    from email.message import EmailMessage

    import httpx

    from lib.accounts import AccountError, AccountStore
    from lib.gmail import GmailClient
    from lib.gmail_host import GmailSignIn, read_view, sync_gmail
    from lib.google_auth import FULL_MAIL_SCOPE, MAIL_SCOPES, SETTINGS_SCOPE
    from lib.store import EmailStore

    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    keys = {'one': 'a-' + '1' * 20, 'two': 'a-' + '2' * 20}
    consents = {'one': 'a1' * 16, 'two': 'b2' * 16}  # each sign-in's own opaque consent, as version 2 names it
    prior = {'one': [*MAIL_SCOPES, SETTINGS_SCOPE, FULL_MAIL_SCOPE], 'two': [*MAIL_SCOPES, SETTINGS_SCOPE]}
    granted = {'one': prior['one'], 'two': list(MAIL_SCOPES)}
    held, handovers, provider_reads, letters, passes = {}, [], [], [], []
    incomplete = old_core = False

    def host_request(request):
        nonlocal incomplete
        assert request.headers['x-skill-token'] == 'fixture-mail-host'
        if request.url.path == '/identity':
            # Version 2 takes the clients these saved sign-ins never recorded a return address for.
            return httpx.Response(200, json={'ok': True, **({} if old_core else {'sign_in_version': 1, 'sign_in_handover_version': 2})})
        assert not old_core
        if request.url.path == '/sign-in/accounts':
            if incomplete:
                incomplete = False
                return httpx.Response(200, json={'ok': True, 'services': [{'id': 'google'}]})
            return httpx.Response(200, json={'ok': True, 'services': [{'id': 'google', 'accounts': list(held.values())}]})
        body = json.loads(request.content)
        assert body['service'] == 'google'
        if request.url.path == '/sign-in/handover' and 'receipt' in body:  # the host's own record, read by address
            asked = body['receipt']
            row = next(row for row in held.values() if 'google:' + row['subject'] == asked['legacy_key'])
            return httpx.Response(200, json={'ok': True, 'receipt': {
                **asked, 'result': 'imported', 'account': {'key': row['key']}, 'connection_id': row['connection_id'],
                'consent': row['consent']}})
        if request.url.path == '/sign-in/handover':
            handovers.append([item['legacy_key'] for item in body['grants']])
            clients = {item['client_id']: item for item in body['clients']}
            assert set(clients) == {'work-client', 'home-client'}
            receipts = []
            for item in body['grants']:
                subject = item['subject']
                client = ('work' if subject == 'one' else 'home') + '-client'
                assert item['client_id'] == client
                assert clients[client]['client_secret'] == 'fixture-' + client
                assert clients[client]['return_hint'] == 'unknown'  # never a guessed address
                assert item['tokens']['refresh_token'] == 'fixture-old-' + subject
                assert item['tokens']['scope'] == ' '.join(prior[subject])
                key = keys[subject]
                held[key] = {'key': key, 'label': subject + '@example.test', 'subject': subject,
                             'verified': True, 'state': 'signed_in', 'scope': ' '.join(granted[subject]),
                             'client_label': client, 'via': 'handover', 'legacy_generation': item['legacy_generation'],
                             'connection_id': 'cn-' + subject, 'consent': consents[subject]}
                receipts.append({'legacy_key': item['legacy_key'], 'result': 'imported', 'account': {'key': key},
                                 'legacy_generation': item['legacy_generation'], 'consent': consents[subject]})
            # One omitted receipt and a partial listing are not proof of withdrawal or permission to resubmit.
            incomplete = True
            return httpx.Response(200, json={'ok': True, 'grants': receipts[:1]})
        assert request.url.path == '/sign-in/token'
        row = held.get(body['account'])
        if row is None:
            return httpx.Response(404, json={'ok': False})
        passes.append((row['subject'], body.get('connection_id')))
        echo = {'consent': row['consent']} if body.get('consent') == row['consent'] else {}  # echoed only on a match
        return httpx.Response(200, json={'ok': True, 'account': {'key': row['key']}, 'connection_id': row['connection_id'],
            'token_type': 'Bearer', 'access_token': 'fixture-host-' + row['subject'],
            'scope': row['scope'], 'expires_at': time.time() + 3600, **echo})

    def mail_request(request):
        if old_core and request.url.path == '/token':
            from urllib.parse import parse_qs
            form = parse_qs(request.content.decode())
            assert form['refresh_token'] == ['fixture-old-one'] and form['client_id'] == ['work-client']
            assert form['scope'] == [' '.join(prior['one'])]
            return httpx.Response(200, json={'access_token': 'fixture-rollback', 'scope': form['scope'][0]})
        assert request.url.host == 'gmail.example.test' and request.url.path in ('/mail/profile', '/mail/messages/abc123')
        if old_core:
            assert request.url.path == '/mail/profile' and request.headers['Authorization'] == 'Bearer fixture-rollback'
            return httpx.Response(200, json={'emailAddress': 'one@example.test'})
        assert request.method == 'GET' and 'x-skill-token' not in request.headers
        subject = request.headers['Authorization'].removeprefix('Bearer fixture-host-')
        assert subject in keys
        if request.url.path.endswith('/abc123'):  # the standard raw read of one fictional letter; nothing is sent
            assert request.url.params['format'] == 'raw'
            letters.append(subject)
            letter = EmailMessage()
            letter['From'], letter['To'], letter['Subject'] = 'Sender <sender@example.test>', subject + '@example.test', 'Letter ' + subject
            letter['Message-ID'] = '<letter-' + subject + '@example.test>'
            letter.set_content('Hello ' + subject + '.')
            return httpx.Response(200, json={'id': 'abc123', 'labelIds': ['INBOX'],
                                             'raw': base64.urlsafe_b64encode(letter.as_bytes()).decode().rstrip('=')})
        provider_reads.append(subject)
        return httpx.Response(200, json={'emailAddress': subject + '@example.test'})

    with httpx.Client(transport=httpx.MockTransport(host_request)) as wire, httpx.Client(transport=httpx.MockTransport(mail_request)) as mail:
        host = GmailSignIn('http://127.0.0.1:1', 'fixture-mail-host', http=wire)
        store = AccountStore(tmp_path, sign_in=host)
        paths = {}
        for subject, client in [('one', 'work-client'), ('two', 'home-client')]:
            address, account_id = subject + '@example.test', 'google:' + subject
            account = store.save({'id': account_id, 'provider': 'gmail', 'address': address, 'label': subject,
                'senders': [address], 'sender': address, 'folder': 'INBOX', 'sending': 'off',
                'oauth': {'client_id': client, 'client_secret': 'fixture-' + client}, 'scopes': prior[subject],
                'tokens': {'refresh_token': 'fixture-old-' + subject}})
            paths[subject] = store.mail_dir(account)
            history = EmailStore(paths[subject])
            history.put('gmail_cursor', {'history_id': subject})
            history.close()
        sync_gmail(store)
        assert not store.get('google:two')['connected']
        assert store.get('google:two')['gmail_transfer']
        sync_gmail(store)
        mode, visible = read_view(store)
        assert mode == 'host' and len(handovers) == 1 and len(visible) == 2
        for row in visible:
            subject = row['id'].split(':')[1]
            assert row['connected'] and row['scopes'] == granted[subject] and row['sending'] == 'off'
            assert 'tokens' not in row and 'oauth' not in row
            assert 'fixture-' not in json.dumps(store.public(row))
            assert store.mail_dir(row) == paths[subject]
            history = EmailStore(paths[subject])
            assert history.get('gmail_cursor') == {'history_id': subject}
            history.close()
            client = GmailClient(store, row['id'], http=mail)
            assert client.test_connection()['address'] == subject + '@example.test'
            assert client.account['scopes'] == granted[subject]
            letter = client.read(message_ref='gmail:abc123')  # a moved mailbox reads its mail with its own host pass
            assert letter['subject'] == 'Letter ' + subject and ('Hello ' + subject + '.') in letter['body']
        assert sorted(provider_reads) == sorted(letters) == ['one', 'two']
        denial = GmailClient(store, 'google:two', http=mail).settings_write(resource='language', values={'displayLanguage': 'en'})
        assert denial['code'] == 'settings_permission_missing'
        # Only a change names the connection the mailbox was confirmed on; reading mail never does.
        assert passes[-1] == ('two', 'cn-two') and all(link is None for _, link in passes[:-1])
        parked = store.get('google:one')
        assert parked['connected'] and parked['tokens']['refresh_token'] == 'fixture-old-one'
        assert parked['oauth']['client_id'] == 'work-client' and parked['scopes'] == prior['one']
        assert not store.public(parked)['refresh_token_configured']
        assert 'tokens' not in store.get('google:two')  # Its current grant is narrower; do not re-enable settings.
        old_core = True
        rollback = AccountStore(tmp_path, sign_in=GmailSignIn('http://127.0.0.1:1', 'fixture-mail-host', http=wire))
        assert GmailClient(rollback, 'google:one', http=mail).test_connection()['address'] == 'one@example.test'
        assert store.get('google:one')['tokens']['access_token'] == 'fixture-rollback'
        with pytest.raises(AccountError, match='Gmail sign-in has not been confirmed'):
            GmailClient(rollback, 'google:two', http=mail).test_connection()
        old_core = False
        del held[keys['one']]
        with pytest.raises(AccountError, match='Google sign-in'):
            GmailClient(store, 'google:one', http=mail).test_connection()
        sync_gmail(store)
        gone = store.get('google:one')
        assert not gone['connected'] and not {'oauth', 'tokens', 'gmail_legacy_copy'} & gone.keys()
        assert store.mail_dir(gone) == paths['one']
        old_core = True
        with pytest.raises(AccountError, match='Check this Google account'):
            GmailClient(rollback, 'google:one', http=mail).test_connection()
        assert store.get('google:two')['connected'] and len(handovers) == 1


@pytest.mark.parametrize(('reason', 'exchange', 'local', 'retained'), [
    ('unknown_client', 'none', True, True), ('provider_unreachable', 'unknown', False, True),
    ('account_disconnected', None, False, False),
])
def test_gmail_handover_outcome_keeps_only_the_proven_local_owner(tmp_path, monkeypatch, reason, exchange, local, retained):
    import json
    import time

    import httpx

    from lib.accounts import AccountError, AccountStore
    from lib.gmail import GmailClient
    from lib.gmail_host import GmailSignIn, read_view, sync_gmail
    from lib.google_auth import MAIL_SCOPES

    calls, handovers, lookups = [], [], []
    def wire(request):
        if request.url.path == '/identity':
            return httpx.Response(200, json={'ok': True, 'sign_in_version': 1, 'sign_in_handover_version': 2})
        if request.url.path == '/sign-in/accounts':
            return httpx.Response(200, json={'ok': True, 'services': [{'id': 'google', 'accounts': []}]})
        assert request.url.path == '/sign-in/handover'
        body = json.loads(request.content)
        if 'receipt' in body:  # the host keeps no filed record of this transfer
            return httpx.Response(200, json={'ok': True, 'receipt': {**body['receipt'], 'result': 'refused',
                                                                     'reason': reason, 'exchange': 'unknown'}})
        item, = body['grants']
        (lookups if body.get('recover') else handovers).append(item['legacy_generation'])
        return httpx.Response(200, json={'ok': True, 'grants': [
            {'legacy_key': item['legacy_key'], 'result': 'refused', 'reason': reason,
             **({'exchange': exchange} if exchange else {})}]})

    def provider(request):
        assert request.url.path == '/mail/profile' and request.headers['Authorization'] == 'Bearer fixture-old-access'
        calls.append(request.method)
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    with httpx.Client(transport=httpx.MockTransport(wire)) as server, httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn('http://127.0.0.1:1', 'fixture-mail-host', http=server))
        account_id = 'google:one'
        store.save({'id': account_id, 'provider': 'gmail', 'address': 'one@example.test', 'scopes': list(MAIL_SCOPES),
                    'oauth': {'client_id': 'fixture-client'}, 'tokens': {'refresh_token': 'fixture-old-refresh',
                    'access_token': 'fixture-old-access', 'expires_at': time.time() + 3600}})
        sync_gmail(store)
        sync_gmail(store)
        assert len(handovers) == 1 and set(lookups) <= set(handovers)  # a lookup names the reserved generation, no new one
        assert len(lookups) == int(exchange == 'unknown')  # only an untold outcome is asked about again
        assert ('tokens' in store.get(account_id)) is retained
        _, (row,) = read_view(store)
        assert row['connected'] is local and not {'oauth', 'tokens'} & row.keys()
        if local:
            assert GmailClient(store, account_id, http=mail).test_connection()['ok'] and calls == ['GET']
        else:
            with pytest.raises(AccountError):
                GmailClient(store, account_id, http=mail).test_connection()
            assert calls == []


def test_only_an_old_host_response_keeps_the_legacy_gmail_path(tmp_path, monkeypatch):
    import time

    import httpx

    from lib.accounts import AccountError, AccountStore
    from lib.gmail import GmailClient
    from lib.gmail_host import GmailSignIn
    from lib.google_auth import MAIL_SCOPES

    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    old = True
    reads = []

    def identity(request):
        assert request.url.path == '/identity'
        return httpx.Response(200, json={'ok': True}) if old else httpx.Response(503, json={'ok': False})

    def provider(request):
        assert request.url.path == '/mail/profile'
        assert request.headers['Authorization'] == 'Bearer fixture-legacy-access'
        reads.append(True)
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    with httpx.Client(transport=httpx.MockTransport(identity)) as wire, httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        host = GmailSignIn('http://127.0.0.1:1', 'fixture-mail-host', http=wire)
        store = AccountStore(tmp_path, sign_in=host)
        # A saved local grant as a sign-in stores it: its client and refresh token beside the bearer.
        store.save({'id': 'google:one', 'provider': 'gmail', 'address': 'one@example.test',
                    'senders': ['one@example.test'], 'scopes': list(MAIL_SCOPES),
                    'oauth': {'client_id': 'fixture-client'},
                    'tokens': {'access_token': 'fixture-legacy-access', 'refresh_token': 'fixture-refresh',
                               'expires_at': time.time() + 3600}})
        assert GmailClient(store, 'google:one', http=mail).test_connection()['ok']
        old = False
        with pytest.raises(AccountError, match='has not been confirmed'):
            GmailClient(store, 'google:one', http=mail).test_connection()
        assert reads == [True]


def test_an_already_open_legacy_return_keeps_its_new_consent_for_host_reconciliation(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    import httpx

    from lib.accounts import AccountStore
    from lib.gmail_host import GmailSignIn
    from lib.google_auth import MAIL_SCOPES, begin, finish

    def identity(request):
        assert request.url.path == '/identity'
        return httpx.Response(200, json={'ok': True, 'sign_in_version': 1})

    def provider(request):
        path = request.url.path
        if path == '/token':
            form = parse_qs(request.content.decode())
            assert form['grant_type'] == ['authorization_code'] and form['client_id'] == ['fixture-begun-client']
            return httpx.Response(200, json={'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh',
                                             'scope': ' '.join(MAIL_SCOPES), 'expires_in': 3600})
        if path.endswith('/userinfo'):
            return httpx.Response(200, json={'sub': 'one', 'email': 'one@example.test', 'email_verified': True})
        if path.endswith('/profile'):
            return httpx.Response(200, json={'emailAddress': 'one@example.test'})
        assert path.endswith('/settings/sendAs')
        return httpx.Response(200, json={'sendAs': []})

    old = AccountStore(tmp_path)
    old.save({'id': 'google:one', 'provider': 'gmail', 'address': 'one@example.test', 'label': 'One',
              'senders': ['one@example.test'], 'sender': 'one@example.test', 'sending': 'off',
              'scopes': list(MAIL_SCOPES), 'gmail_host': {'key': 'a-' + '1' * 20, 'verified': True, 'state': 'signed_in'}})
    prepared = begin(old, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-begun-client'},
                     origin='http://127.0.0.1:1', verify_return=False)
    state = parse_qs(urlsplit(prepared['authorization_url']).query)['state'][0]
    with httpx.Client(transport=httpx.MockTransport(identity)) as wire, httpx.Client(transport=httpx.MockTransport(provider)) as http:
        store = AccountStore(tmp_path, sign_in=GmailSignIn('http://127.0.0.1:1', 'fixture-mail-host', http=wire))
        reply = finish(store, state=state, code='fixture-code', http=http)
        row = store.get('google:one')
        assert reply['code'] == 'consent_saved' and not reply['ok']
        assert row['gmail_host']['key'] == 'a-' + '1' * 20 and row['sending'] == 'off'
        assert 'tokens' not in row and row['gmail_late_consent']['oauth']['client_id'] == 'fixture-begun-client'
