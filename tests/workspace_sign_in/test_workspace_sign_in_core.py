"""A working Google Workspace sign-in in Settings moves to a core checkout's shared sign-in on one ordinary renewal.

The skill starts with the owner's manual connection: an OAuth client and refresh token in Settings. Its first ordinary
Drive read renews that token once; Google reports permissions that fit the skill, the skill hands the grant to the
checkout's Host Service, and the host confirms the account. The next read takes its token from the host, and Settings
is not renewed again. Google is made up, the Host Service is the core's real ASGI app, and everything stays in this
process: no network. Synthetic data only.

A local check; hosted CI does not run it yet. In a Python environment with that core's requirements installed:

    export OUROBOROS_SIGN_IN_CORE_ROOT=/path/to/ouroboros
    python -B -m pytest -p no:cacheprovider -v -rs tests/workspace_sign_in/test_workspace_sign_in_core.py
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import urllib.error
from urllib.parse import parse_qsl, urlsplit
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / 'skills' / 'google-workspace'
BASE = 'http://127.0.0.1:8765'  # a transport address only: nothing listens on it
ISSUER, OAUTH = 'https://accounts.google.com', 'https://oauth.example.test'
HOST_ROUTES = {('GET', '/identity'), ('GET', '/sign-in/accounts'), ('POST', '/sign-in/token'), ('POST', '/sign-in/handover')}
CLIENT_ID, CLIENT_SECRET = 'fixture-oauth-client-id.apps.googleusercontent.com', 'fixture-oauth-client-secret'
SETTINGS_REFRESH, RENEWED = 'fixture-settings-refresh-token', 'fixture-renewed-access-token'
SUBJECT, EMAIL = 'fixture-google-subject', 'fixture@example.test'
ACTOR = {'emailAddress': EMAIL, 'permissionId': 'fixture-permission-id'}
DRIVE = 'https://www.googleapis.com/auth/drive'
GRANTED = f'openid email {DRIVE} https://www.googleapis.com/auth/userinfo.email'
SETTINGS = {'GOOGLE_OAUTH_CLIENT_ID': CLIENT_ID, 'GOOGLE_OAUTH_CLIENT_SECRET': CLIENT_SECRET,
            'GOOGLE_OAUTH_REFRESH_TOKEN': SETTINGS_REFRESH}


def test_a_settings_sign_in_moves_to_the_host_on_one_ordinary_renewal(tmp_path, monkeypatch):
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
                        ('OUROBOROS_SETTINGS_PATH', data / 'settings.json'), ('OUROBOROS_REPO_DIR', repo),
                        ('HOST_SERVICE_URL', BASE)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.syspath_prepend(str(core))
    import httpx
    from starlette.testclient import TestClient
    from ouroboros import config, oauth_engine, skill_loader
    from ouroboros.extension_plugin_api import mint_skill_token
    from ouroboros.gateway.host_service import create_host_service_app
    assert (config.HOME.resolve(), config.DATA_DIR.resolve(), config.SETTINGS_PATH.resolve()) == (
        home.resolve(), data.resolve(), (data / 'settings.json').resolve())
    assert Path(oauth_engine.__file__).resolve().is_relative_to(core)
    payload = data / 'skills' / 'external' / 'google-workspace'
    shutil.copytree(SKILL, payload, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    skill = skill_loader.load_skill(payload, data)
    assert skill is not None and not skill.load_error
    review = skill_loader.SkillReviewState(status='pass', content_hash=skill.content_hash)
    skill_loader.save_review_state(data, 'google-workspace', review)
    skill_loader.save_enabled(data, 'google-workspace', True)
    state = data / 'state' / 'skills' / 'google-workspace'
    monkeypatch.setenv('HOST_SERVICE_TOKEN', mint_skill_token(state, 'google-workspace', payload))
    monkeypatch.syspath_prepend(str(payload))  # the installed skill's own modules
    import client as workspace_client
    import host_sign_in
    import plugin
    assert all(Path(module.__file__).resolve().parent == payload.resolve()
               for module in (workspace_client, host_sign_in, plugin))

    def no_network(*_args, **_kwargs):
        raise AssertionError('this check opens no network connection')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', no_network)
    renewals, reads, identities, host_renewals, asked = [], [], [], [], []

    def google(request):  # what the skill asks Google itself: the Settings renewal and the Drive read
        url = (request.url.host, request.url.path)
        if url == ('oauth2.googleapis.com', '/token'):
            assert request.method == 'POST'
            assert dict(parse_qsl(request.content.decode('ascii'))) == {
                'grant_type': 'refresh_token', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
                'refresh_token': SETTINGS_REFRESH}
            renewals.append(url)
            return httpx.Response(200, json={'access_token': RENEWED, 'token_type': 'Bearer', 'expires_in': 3600,
                                             'scope': GRANTED})
        assert url == ('www.googleapis.com', '/drive/v3/about')
        assert request.headers['authorization'] == f'Bearer {RENEWED}'
        reads.append(request.url.params['fields'])
        return httpx.Response(200, json={'user': ACTOR})

    def provider(request):  # what the host asks Google: discovery and whom the renewed token belongs to
        host, path = request.url.host, request.url.path
        if host == 'accounts.google.com' and path in ('/.well-known/oauth-authorization-server',
                                                      '/.well-known/openid-configuration'):
            assert request.method == 'GET'
            return httpx.Response(200, json={
                'issuer': ISSUER, 'authorization_endpoint': OAUTH + '/authorize', 'token_endpoint': OAUTH + '/token',
                'userinfo_endpoint': OAUTH + '/userinfo', 'response_types_supported': ['code'],
                'code_challenge_methods_supported': ['S256'], 'token_endpoint_auth_methods_supported': ['none']})
        if (host, path) == ('oauth.example.test', '/userinfo'):
            identities.append(request.headers['authorization'])
            return httpx.Response(200, json={'sub': SUBJECT, 'email': EMAIL, 'email_verified': True})
        assert (host, path) == ('oauth.example.test', '/token'), f'unexpected provider request: {request.url}'
        host_renewals.append(path)  # none is expected: the handed access token is fresh
        return httpx.Response(200, json={'access_token': 'fixture-host-renewed-token', 'token_type': 'Bearer',
                                         'expires_in': 3600, 'scope': GRANTED})

    monkeypatch.setattr(oauth_engine, 'http_client',
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False))
    with TestClient(create_host_service_app(data), base_url=BASE) as wire, \
            httpx.Client(transport=httpx.MockTransport(google), trust_env=False) as google_http:

        def to_host(_opener, req, data=None, timeout=None):  # the skill's urllib calls; no answer is altered
            method, route = req.get_method(), urlsplit(req.full_url).path
            assert data is None and req.full_url.startswith(BASE + '/') and (method, route) in HOST_ROUTES, route
            out = wire.request(method, route, headers=dict(req.header_items()), content=req.data)
            asked.append((route, json.loads(req.data) if req.data else None, out.json()))
            stream = io.BytesIO(out.content)
            stream.status, stream.headers = out.status_code, out.headers
            if out.status_code >= 400:
                raise urllib.error.HTTPError(req.full_url, out.status_code, 'fixture', stream.headers, stream)
            return stream

        monkeypatch.setattr(urllib.request.OpenerDirector, 'open', to_host)
        monkeypatch.setattr(plugin, 'GoogleWorkspaceClient',
                            lambda **kwargs: workspace_client.GoogleWorkspaceClient(http_client=google_http, **kwargs))
        bridge = host_sign_in.HostSignIn()
        assert bridge.available() and bridge.handover_version >= 2, 'the core must offer sign-in handover 2'
        api = SimpleNamespace(get_state_dir=lambda: str(state), log=lambda level, message: None,
                              get_settings=lambda keys: {key: SETTINGS[key] for key in keys if key in SETTINGS})
        request_tool = plugin._make_workspace_request(api)
        # Two ordinary reads: the first renews Settings and moves the sign-in, the second asks the host for its token.
        for _ in range(2):
            answer = request_tool('drive', 'GET', 'about', query={'fields': 'user'}, auth_mode='oauth')
            assert json.loads(answer) == {'status_code': 200, 'data': {'user': ACTOR}}
        report = json.loads(plugin._make_workspace_accounts(api)())

    # One handover carries the renewed grant, and the host confirms the account it names.
    [(sent, received)] = [(body, reply) for route, body, reply in asked if route == '/sign-in/handover']
    [registration], [grant], [receipt] = sent['clients'], sent['grants'], received['grants']
    assert (sent['service'], registration['client_id'], registration['client_secret']) == (
        'google', CLIENT_ID, CLIENT_SECRET)
    assert (grant['legacy_key'], grant['client_id'], grant['require_verified']) == ('settings', CLIENT_ID, True)
    assert grant['tokens'] == {'refresh_token': SETTINGS_REFRESH, 'access_token': RENEWED, 'scope': GRANTED,
                               'expires_at': grant['tokens']['expires_at']}
    assert (receipt['result'], receipt['via'], receipt['state'], receipt['legacy_generation']) == (
        'imported', 'handover', 'signed_in', grant['legacy_generation'])
    key = receipt['account']['key']
    # The skill now reports and uses that account of the host, without the owner having to choose it.
    assert (report['state'], report['source'], report['used'], report['selected']) == ('ready', 'ouroboros', key, '')
    [account] = report['accounts']
    assert (account['key'], account['subject'], account['label']) == (key, SUBJECT, EMAIL)
    assert (account['verified'], account['state'], account['scope']) == (True, 'signed_in', GRANTED)
    # Both reads asked for its token under the connection and consent of that one transfer.
    assert [tuple(body[name] for name in ('service', 'account', 'required_scope', 'connection_id', 'consent'))
            for route, body, _ in asked if route == '/sign-in/token'] == [
        ('google', key, DRIVE, receipt['connection_id'], receipt['consent'])] * 2
    # One ordinary Settings renewal, none by the host, one identity check and both Drive reads.
    assert (len(renewals), host_renewals, identities, reads) == (1, [], [f'Bearer {RENEWED}'], ['user', 'user'])
