"""Two Gmail mailboxes move to a core checkout's shared Google sign-in and each keeps reading its own mail.

Mail starts as its own sign-in left it: two accounts, each with its own client, permissions, folder and sync cursor.
One ordinary sync hands both saved grants to the checkout's Host Service. Each mailbox then reads its own message,
twice, with a token the host gives for its own account, and reading or syncing again changes no mailbox record.
Google is made up, the Host Service is the core's real ASGI app, and everything stays in this process: no network,
no mail sent. Synthetic data only.

A local check; hosted CI does not run it yet. In a Python environment with that core's requirements installed:

    export OUROBOROS_SIGN_IN_CORE_ROOT=/path/to/ouroboros
    python -B -m pytest -p no:cacheprovider -v -rs tests/mail_sign_in/test_mail_sign_in_core.py
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / 'skills' / 'email-presence'
BASE = 'http://127.0.0.1:1'  # a transport address only: nothing listens on it
ISSUER, OAUTH = 'https://accounts.google.com', 'https://oauth.example.test'


def test_two_gmail_mailboxes_move_to_the_host_and_each_reads_its_own_mail(tmp_path, monkeypatch):
    raw = os.environ.get('OUROBOROS_SIGN_IN_CORE_ROOT', '').strip()
    assert raw, 'set OUROBOROS_SIGN_IN_CORE_ROOT to a core checkout with service sign-in'
    core = Path(raw).resolve()
    assert (core / 'ouroboros' / 'service_sign_in.py').is_file(), f'no service sign-in in {core}'
    assert not any(name == 'ouroboros' or name.startswith('ouroboros.') for name in sys.modules)
    home, app, data, repo = (tmp_path / name for name in ('home', 'app', 'data', 'repo'))
    for path in (home, app, data, repo):
        path.mkdir()
    for name in [name for name in os.environ if name.startswith(('OUROBOROS_', 'HOST_SERVICE_'))]:
        monkeypatch.delenv(name)  # nothing of a running installation reaches this check
    for name, value in (('HOME', home), ('OUROBOROS_APP_ROOT', app), ('OUROBOROS_DATA_DIR', data),
                        ('OUROBOROS_SETTINGS_PATH', data / 'settings.json'), ('OUROBOROS_REPO_DIR', repo)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.syspath_prepend(str(core))
    monkeypatch.syspath_prepend(str(SKILL))
    import httpx
    from starlette.testclient import TestClient
    from lib import gmail_host
    from lib.accounts import AccountStore, public_message_ref
    from lib.gmail import GmailClient
    from lib.google_auth import MAIL_SCOPES, SETTINGS_SCOPE
    from lib.service import MailService
    from lib.store import EmailStore
    from ouroboros import config, oauth_engine, skill_loader
    from ouroboros.extension_plugin_api import mint_skill_token
    from ouroboros.gateway.host_service import create_host_service_app
    assert (config.HOME.resolve(), config.DATA_DIR.resolve(), config.SETTINGS_PATH.resolve()) == (
        home.resolve(), data.resolve(), (data / 'settings.json').resolve())
    assert Path(oauth_engine.__file__).resolve().is_relative_to(core)
    assert Path(gmail_host.__file__).resolve().parent == SKILL / 'lib'
    payload = data / 'skills' / 'external' / 'email-presence'
    shutil.copytree(SKILL, payload, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    skill = skill_loader.load_skill(payload, data)
    assert skill is not None and not skill.load_error
    review = skill_loader.SkillReviewState(status='pass', content_hash=skill.content_hash)
    skill_loader.save_review_state(data, 'email-presence', review)
    skill_loader.save_enabled(data, 'email-presence', True)
    state = data / 'state' / 'skills' / 'email-presence'
    token = mint_skill_token(state, 'email-presence', payload)  # of this disposable installation only

    def no_network(*_args, **_kwargs):
        raise AssertionError('this check opens no network connection')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', no_network)
    # Two made-up mailboxes as Mail saved them: client, permissions and folder.
    mailboxes = {'home': ('desktop.test', MAIL_SCOPES, 'INBOX'),
                 'work': ('approved.test', (*MAIL_SCOPES, SETTINGS_SCOPE), 'WorkArchive')}
    refresh_owner = {'fixture-refresh-' + sub: sub for sub in mailboxes}
    issued, refreshes, identities, reads = {}, [], [], []

    def google(request):
        host, path = request.url.host, request.url.path
        if host == 'accounts.google.com' and path.startswith('/.well-known/'):
            assert request.method == 'GET'
            if path != '/.well-known/openid-configuration':
                return httpx.Response(404)
            return httpx.Response(200, json={
                'issuer': ISSUER, 'authorization_endpoint': OAUTH + '/authorize', 'token_endpoint': OAUTH + '/token',
                'userinfo_endpoint': OAUTH + '/userinfo', 'response_types_supported': ['code'],
                'code_challenge_methods_supported': ['S256'], 'token_endpoint_auth_methods_supported': ['none']})
        if (host, path) == ('oauth.example.test', '/token'):
            assert request.method == 'POST'
            form = dict(parse_qsl(request.content.decode('ascii')))
            sub = refresh_owner[form['refresh_token']]
            client, scopes, _ = mailboxes[sub]
            assert form == {'grant_type': 'refresh_token', 'refresh_token': 'fixture-refresh-' + sub,
                            'client_id': client, 'scope': ' '.join(scopes)}
            refreshes.append(sub)
            access = 'fixture-issued-' + sub
            issued['Bearer ' + access] = sub
            return httpx.Response(200, json={'access_token': access, 'token_type': 'Bearer',
                                             'scope': form['scope'], 'expires_in': 3600})
        assert request.method == 'GET'
        sub = issued[request.headers['authorization']]  # whom this bearer was issued for
        if (host, path) == ('oauth.example.test', '/userinfo'):
            identities.append(sub)
            return httpx.Response(200, json={'sub': sub, 'email': sub + '@example.test', 'email_verified': True})
        assert (host, path) == ('gmail.googleapis.com', '/gmail/v1/users/me/messages/a1')
        assert request.url.params['format'] == 'raw'
        reads.append(sub)
        letter = (f'From: fixture@example.test\r\nTo: {sub}@example.test\r\n'
                  f'Message-ID: <{sub}@example.test>\r\nSubject: Fixture {sub}\r\n\r\nHello {sub}.\r\n')
        return httpx.Response(200, json={'id': 'a1', 'threadId': 'b2', 'labelIds': ['UNREAD'],
                                         'raw': base64.urlsafe_b64encode(letter.encode('ascii')).decode('ascii')})

    handovers, token_keys = [], []

    def observe(response):  # what Mail asks the Host Service: every answer succeeds and none is altered
        path = response.request.url.path
        assert response.status_code == 200, (path, response.status_code)
        if path == '/sign-in/handover':
            response.read()
            handovers.append(response.json()['grants'])
        elif path == '/sign-in/token':
            token_keys.append(json.loads(response.request.content)['account'])

    transport = httpx.MockTransport(google)
    monkeypatch.setattr(oauth_engine, 'http_client', lambda: httpx.AsyncClient(transport=transport, trust_env=False))
    with TestClient(create_host_service_app(data), base_url=BASE) as wire, \
            httpx.Client(transport=transport, trust_env=False) as gmail_http:
        wire.event_hooks['response'].append(observe)
        sign_in = gmail_host.GmailSignIn(BASE, token, http=wire)
        assert sign_in.mode() == 'host' and sign_in.handover_version >= 2, 'the core must offer sign-in handover 2'
        store = AccountStore(state, sign_in=sign_in)
        generations, mail_dirs = {}, {}
        for sub, (client, scopes, folder) in mailboxes.items():
            address = sub + '@example.test'
            row = store.save({'id': 'google:' + sub, 'provider': 'gmail', 'address': address, 'label': 'Local ' + sub,
                              'sender': address, 'senders': [address], 'folder': folder, 'sending': 'off',
                              'oauth': {'client_id': client}, 'scopes': list(scopes),
                              'tokens': {'refresh_token': 'fixture-refresh-' + sub}})
            generations[sub], mail_dirs[sub] = row['generation'], store.mail_dir(row)
            with closing(EmailStore(mail_dirs[sub])) as history:
                history.put('gmail_cursor', {'history_id': sub})

        gmail_host.sync_gmail(store)  # one ordinary sync hands both saved grants to the host
        held = {row['subject']: row for row in sign_in.accounts().values()}
        keys = {sub: row['key'] for sub, row in held.items()}
        assert set(keys) == set(mailboxes) and len(set(keys.values())) == 2
        [receipts] = handovers
        assert {r['legacy_key']: (r['result'], r['account']['key'], r['legacy_generation']) for r in receipts} == {
            'google:' + sub: ('imported', keys[sub], generations[sub]) for sub in mailboxes}

        moved = store.list()
        for again in (False, True):
            if again:
                gmail_host.sync_gmail(store)
            mode, rows = gmail_host.read_view(store)
            assert mode == 'host' and {row['id'] for row in rows} == {'google:' + sub for sub in mailboxes}
            for row in rows:
                sub = row['id'].removeprefix('google:')
                _, scopes, folder = mailboxes[sub]
                assert held[sub]['verified'] and held[sub]['scope'].split() == list(scopes)
                assert row['connected'] and row['auth_owner'] == 'host'
                assert row['gmail_host']['key'] == keys[sub] and row['scopes'] == list(scopes)
                assert (row['label'], row['folder'], row['sending']) == ('Local ' + sub, folder, 'off')
                assert store.mail_dir(row) == mail_dirs[sub]
                with closing(EmailStore(mail_dirs[sub])) as history:
                    assert history.get('gmail_cursor') == {'history_id': sub}
                ref = public_message_ref(row['id'], 'gmail:a1')
                mailbox = GmailClient(store, row['id'], http=gmail_http)
                message = mailbox.read(**MailService.unwrap_ref(row, ref))
                assert message['subject'] == 'Fixture ' + sub and message['body'].strip() == f'Hello {sub}.'
                assert MailService.wrap_ref(row, message) == ref and message['labels'] == ['UNREAD']
                assert mailbox.account['scopes'] == list(scopes)
            assert store.list() == moved

    # One handover; each grant renewed and identified once; each read asked for its own account's token.
    assert len(handovers) == 1 and Counter(refreshes) == Counter(identities) == Counter(mailboxes.keys())
    assert Counter(token_keys) == Counter({key: 2 for key in keys.values()})
    assert Counter(reads) == Counter({sub: 2 for sub in mailboxes})


def test_mail_queued_before_the_move_goes_out_under_the_hosts_sign_in_and_notices_wait_for_their_grant(
        tmp_path, monkeypatch):
    """Rule A against the core's real Host Service: a mail Mail queued before its sign-in moved (no record of its access)
    goes out once, under the connection and consent the host lists for that same Google account; one whose outcome is
    unknown is kept as a draft in that mailbox; the owner's notice about it waits while the host refuses it (no
    `notify_owner` grant, then no chat writer in this process) and is shown meanwhile."""
    raw = os.environ.get('OUROBOROS_SIGN_IN_CORE_ROOT', '').strip()
    assert raw, 'set OUROBOROS_SIGN_IN_CORE_ROOT to a core checkout with service sign-in'
    core = Path(raw).resolve()
    assert (core / 'ouroboros' / 'service_sign_in.py').is_file(), f'no service sign-in in {core}'
    for name in [name for name in sys.modules if name.split('.')[0] in ('ouroboros', 'supervisor')]:
        monkeypatch.delitem(sys.modules, name)  # the core is imported anew under this check's own disposable roots
    home, app, data, repo = (tmp_path / name for name in ('home', 'app', 'data', 'repo'))
    for path in (home, app, data, repo):
        path.mkdir()
    for name in [name for name in os.environ if name.startswith(('OUROBOROS_', 'HOST_SERVICE_'))]:
        monkeypatch.delenv(name)  # nothing of a running installation reaches this check
    for name, value in (('HOME', home), ('OUROBOROS_APP_ROOT', app), ('OUROBOROS_DATA_DIR', data),
                        ('OUROBOROS_SETTINGS_PATH', data / 'settings.json'), ('OUROBOROS_REPO_DIR', repo)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.syspath_prepend(str(core))
    monkeypatch.syspath_prepend(str(SKILL))
    from types import SimpleNamespace
    import httpx
    from starlette.testclient import TestClient
    from lib import gmail_host
    from lib.accounts import AccountStore
    from lib.gmail import GmailClient
    from lib.google_auth import MAIL_SCOPES
    from lib.owner_notice import OwnerNotifier, tell_owner
    from lib.runtime import EmailRuntime
    from lib.store import EmailStore
    from ouroboros import oauth_engine, skill_loader
    from ouroboros.extension_plugin_api import mint_skill_token
    from ouroboros.gateway.host_service import create_host_service_app
    from ouroboros.skill_grant_follow import save_skill_grants
    assert Path(oauth_engine.__file__).resolve().is_relative_to(core)
    payload = data / 'skills' / 'external' / 'email-presence'
    shutil.copytree(SKILL, payload, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    skill = skill_loader.load_skill(payload, data)
    assert skill is not None and not skill.load_error
    skill_loader.save_review_state(data, 'email-presence',
                                   skill_loader.SkillReviewState(status='pass', content_hash=skill.content_hash))
    skill_loader.save_enabled(data, 'email-presence', True)
    state = data / 'state' / 'skills' / 'email-presence'
    token = mint_skill_token(state, 'email-presence', payload)  # of this disposable installation only

    def no_network(*_args, **_kwargs):
        raise AssertionError('this check opens no network connection')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', no_network)
    issued, sent, drafted = set(), [], []

    def google(request):  # a made-up Google: discovery, one renewable grant, its identity and a recording Gmail
        host, path = request.url.host, request.url.path
        if host == 'accounts.google.com':
            if path != '/.well-known/openid-configuration':
                return httpx.Response(404)
            return httpx.Response(200, json={
                'issuer': ISSUER, 'authorization_endpoint': OAUTH + '/authorize', 'token_endpoint': OAUTH + '/token',
                'userinfo_endpoint': OAUTH + '/userinfo', 'response_types_supported': ['code'],
                'code_challenge_methods_supported': ['S256'], 'token_endpoint_auth_methods_supported': ['none']})
        if (host, path) == ('oauth.example.test', '/token'):
            assert dict(parse_qsl(request.content.decode('ascii')))['refresh_token'] == 'fixture-refresh-home'
            access = f'fixture-issued-{len(issued)}'
            issued.add('Bearer ' + access)
            return httpx.Response(200, json={'access_token': access, 'token_type': 'Bearer',
                                             'scope': ' '.join(MAIL_SCOPES), 'expires_in': 3600})
        assert request.headers['authorization'] in issued
        if (host, path) == ('oauth.example.test', '/userinfo'):
            return httpx.Response(200, json={'sub': 'home', 'email': 'home@example.test', 'email_verified': True})
        assert host == 'gmail.googleapis.com'
        path = path.removeprefix('/gmail/v1/users/me')
        if (request.method, path) == ('POST', '/messages/send'):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={'id': f'a{len(sent)}', 'threadId': 'fixture-thread'})
        if (request.method, path) == ('POST', '/drafts'):
            drafted.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'fixture-draft', 'message': {'id': 'b1'}})
        assert request.method == 'GET' and path in ('/messages', '/drafts')  # Gmail holds no copy of the lost one
        return httpx.Response(200, json={})

    tokens = []

    def observe(response):
        if response.request.url.path == '/sign-in/token':
            tokens.append(json.loads(response.request.content))

    transport = httpx.MockTransport(google)
    monkeypatch.setattr(oauth_engine, 'http_client', lambda: httpx.AsyncClient(transport=transport, trust_env=False))
    with TestClient(create_host_service_app(data), base_url=BASE) as wire, \
            httpx.Client(transport=transport, trust_env=False) as gmail_http:
        wire.event_hooks['response'].append(observe)
        sign_in = gmail_host.GmailSignIn(BASE, token, http=wire)
        assert sign_in.mode() == 'host' and sign_in.handover_version >= 2
        accounts = AccountStore(state, sign_in=sign_in)
        mailbox = accounts.save({'id': 'google:home', 'provider': 'gmail', 'address': 'home@example.test',
                                 'label': 'Home', 'sender': 'home@example.test', 'senders': ['home@example.test'],
                                 'folder': 'INBOX', 'sending': 'on', 'oauth': {'client_id': 'desktop.test'},
                                 'scopes': list(MAIL_SCOPES), 'tokens': {'refresh_token': 'fixture-refresh-home'}})
        with closing(EmailStore(accounts.mail_dir(mailbox))) as queue:  # what Mail 0.10.11 left in its queue
            for request_id in ('earlier', 'lost'):
                queue.enqueue_outbox(request_id=request_id, to=['reader@example.test'], subject='Fixture ' + request_id,
                                     body='Queued before the move.', reporting={'account_id': 'google:home',
                                                                                'sender': 'home@example.test'})
            with queue._connect() as db:
                db.execute("UPDATE outbox SET state='uncertain',available_at=0 WHERE request_id='lost'")
        gmail_host.sync_gmail(accounts)  # the saved grant moves to the host
        row, = sign_in.accounts().values()
        assert row['verified'] and row['subject'] == 'home' and row['connection_id'] and row['consent']
        with closing(EmailStore(accounts.mail_dir(mailbox))) as queue:
            runtime = EmailRuntime(queue, GmailClient(accounts, 'google:home', http=gmail_http), SimpleNamespace(),
                                   account='google:home')
            while runtime.process_outbox() or runtime.process_uncertain():
                pass
            earlier, lost = queue.receipt('earlier'), queue.receipt('lost')
        assert earlier['state'] == 'completed' and len(sent) == 1  # once, under the host's sign-in of the same account
        assert earlier['delivery_reporting']['gmail_connection'] == {'host': row['connection_id'], 'consent': row['consent']}
        assert [(asked.get('connection_id'), asked.get('consent')) for asked in tokens
                if asked.get('connection_id')] == [(row['connection_id'], row['consent'])] * 2  # the send and the draft
        assert lost['unknown_outcome'] == 'drafted' and lost['draft_id'] == 'fixture-draft' and len(drafted) == 1

        notifier = OwnerNotifier(BASE, token, http=wire)
        assert notifier.supported() is True  # the core announces notify_version 1
        assert tell_owner(accounts, notifier) == 0  # no notify_owner grant for these files: 403, kept
        save_skill_grants(data, 'email-presence', [], content_hash=skill.content_hash, requested_keys=[],
                          granted_permissions=['presence', 'notify_owner'],
                          requested_permissions=['presence', 'notify_owner'])
        with closing(EmailStore(accounts.mail_dir(mailbox))) as queue:
            with queue._connect() as db:
                db.execute("UPDATE runtime_state SET value=json_set(value,'$.available_at',0) WHERE key LIKE 'owner_notice:%'")
        assert tell_owner(accounts, notifier) == 0  # granted, but no chat writer runs here: 503, kept
        with closing(EmailStore(accounts.mail_dir(mailbox))) as queue:
            told, = queue.status()['owner_notices']
    assert told['kind'] == 'drafted' and told['attempts'] == 2 and 'Fixture lost' in told['text']
