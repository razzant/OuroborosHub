"""An old Calendar follows the shared Google sign-in of a core checkout, through a re-sign-in and a Disconnect.

Calendar starts as its own sign-in left it: an account, its calendars with the roles the owner chose and a saved
grant in its own vault. The checkout's Host Service and sign-in card then take over, with a made-up provider and
in-memory transports, while Calendar's own worker, tools and queue run against them.
Phase 1, an ordinary re-sign-in of the same verified subject with the IT client: the account, calendars, roles and
connection stay, and the write deferred earlier keeps its ID and goes out once, its token asked for under that
connection. Phase 2, an explicit Disconnect and a new consent: the account and roles come back on another connection,
and the write the Disconnect withdrew stays withdrawn. Synthetic data only; nothing leaves the process.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace
import urllib.error
from urllib.parse import urlsplit
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / 'skills' / 'calendar'
BASE, ISSUER, SKILL_TOKEN = 'http://127.0.0.1:8765', 'https://accounts.google.com', 'fixture-calendar-token'
SUB, OLD, NEW = 'fixture-subject', 'before@example.test', 'after@example.test'
PERSONAL, PERSONAL_SECRET = 'fixture-personal-client', 'fixture-personal-secret'
IT, IT_SECRET = 'fixture-it-client', 'fixture-it-secret'
HOST_ROUTES = {('GET', '/identity'), ('GET', '/sign-in/accounts'), ('POST', '/sign-in/token'), ('POST', '/sign-in/handover')}
ROLES = ('role_visible', 'role_busy', 'role_publish', 'publish_mode', 'is_default')


def test_old_calendar_follows_the_shared_sign_in_through_reauth_and_disconnect(tmp_path, monkeypatch):
    raw = os.environ.get('OUROBOROS_SIGN_IN_CORE_ROOT', '').strip()
    assert raw, 'set OUROBOROS_SIGN_IN_CORE_ROOT to a core checkout with service sign-in'
    core = Path(raw).resolve()
    helper = core / 'tests' / 'test_service_sign_in_loopback.py'
    assert helper.is_file(), f'missing {helper}'
    assert not any(name == 'ouroboros' or name.startswith('ouroboros.') for name in sys.modules)
    home, app, data, repo = (tmp_path / name for name in ('home', 'app', 'data', 'repo'))
    for path in (home, app, data, repo):
        path.mkdir()
    for name, value in (('HOME', home), ('OUROBOROS_APP_ROOT', app), ('OUROBOROS_DATA_DIR', data),
                        ('OUROBOROS_SETTINGS_PATH', data / 'settings.json'), ('OUROBOROS_REPO_DIR', repo),
                        ('HOST_SERVICE_URL', BASE), ('HOST_SERVICE_TOKEN', SKILL_TOKEN)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.syspath_prepend(str(SKILL))
    monkeypatch.syspath_prepend(str(core))
    import httpx
    import accounts
    import google_host
    import host_sign_in
    import ops
    import providers_google as gp
    import store as calendar_store
    import tools as cal
    from model import calendar_id, iso_utc, parse_stored
    from providers import Providers
    from ouroboros import config, oauth_engine, skill_loader
    assert (config.HOME.resolve(), config.DATA_DIR.resolve(), config.SETTINGS_PATH.resolve()) == (
        home.resolve(), data.resolve(), (data / 'settings.json').resolve())
    assert Path(oauth_engine.__file__).resolve().is_relative_to(core)
    assert all(Path(module.__file__).resolve().parent == SKILL.resolve()
               for module in (accounts, google_host, host_sign_in, ops, gp, calendar_store, cal))
    payload = data / 'skills' / 'external' / 'calendar'
    shutil.copytree(SKILL, payload, ignore=shutil.ignore_patterns('__pycache__'))
    skill = skill_loader.find_skill(data, 'calendar')
    assert skill is not None and skill.skill_dir.resolve() == payload.resolve()
    skill_loader.save_review_state(data, 'calendar', skill_loader.SkillReviewState(status='pass', content_hash=skill.content_hash))
    skill_loader.save_enabled(data, 'calendar', True)
    spec = spec_from_file_location('core_service_sign_in_helpers', helper)
    assert spec is not None and spec.loader is not None
    fx = module_from_spec(spec)
    spec.loader.exec_module(fx)
    monkeypatch.setattr(fx, 'ISSUER', ISSUER)
    state = str(data / 'state' / 'skills' / 'calendar')
    fx.atomic_write_json(Path(state) / fx.AUTH_TOKEN_FILENAME,
                         {'token': SKILL_TOKEN, 'content_hash': skill.content_hash, 'issued_at': 'fixture'})
    provider = fx.Provider()
    provider.person = {'sub': SUB, 'email': OLD}
    clock = [datetime.now(timezone.utc)]

    def oidc(request):
        assert str(request.url).startswith(ISSUER + '/'), str(request.url)
        return provider.handle(request)

    monkeypatch.setattr(oauth_engine, '_server_port', lambda: 8765)
    monkeypatch.setattr(oauth_engine, 'http_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(oidc)))
    monkeypatch.setattr(oauth_engine, 'time', SimpleNamespace(time=lambda: clock[0].timestamp(), monotonic=time.monotonic))
    monkeypatch.setattr(calendar_store, 'now_utc', lambda: clock[0])  # the core's clock and Calendar's queue clock move together

    # Calendar as its own sign-in left it, before the core takes anything over.
    account_id = 'google:' + OLD
    home_id, work_id = (calendar_id('google', OLD, name) for name in ('fixture-home', 'fixture-work'))
    db = calendar_store.Store(state)
    db.set_setting('timezone', 'UTC')
    db.upsert_account({'id': account_id, 'provider': 'google', 'alias': 'Before', 'login': OLD, 'status': 'ok'})
    for cid, name, chosen in ((home_id, 'fixture-home', dict(role_visible=True, role_busy=False, is_default=True)),
                              (work_id, 'fixture-work', dict(role_visible=False, role_publish=True, publish_mode='full'))):
        db.upsert_calendar(dict(id=cid, account_id=account_id, provider='google', external_id=name, name=name, **chosen))
    legacy = provider._issue(PERSONAL, dict(provider.person), ' '.join(gp.SCOPES))
    gp.save_tokens(state, '', {OLD: {
        'access_token': legacy['access_token'], 'refresh_token': legacy['refresh_token'], 'scope': legacy['scope'],
        'expires_at': iso_utc(clock[0] + timedelta(seconds=legacy['expires_in'])), 'email': OLD, 'subject': SUB,
        'provider': 'google', 'client_id': PERSONAL, 'client_secret': PERSONAL_SECRET, 'generation': 'fixture-generation'}})

    def roles():  # every calendar of the account, retired or not, with the roles the owner chose
        return {row['id']: tuple(row[column] for column in ROLES)
                for row in db.list_calendars(account_id=account_id, include_deleted=True)}

    def account_stands():  # the old account, calendar IDs and roles, as they were before the core was involved
        assert [row['id'] for row in db.list_accounts('google')] == [account_id] and roles() == baseline

    baseline = roles()
    assert set(baseline) == {home_id, work_id} and len(set(baseline.values())) == 2  # two calendars, two different sets of roles
    account_stands()

    with (fx.TestClient(fx.Starlette(routes=fx.sign_in_routes()), base_url=BASE) as owner,
          fx.TestClient(fx.create_host_service_app(data), base_url=BASE) as host):
        tokens, writes, stray = [], [], []  # Calendar's token exchanges with the host, the writes the provider took, anything unexpected

        def in_memory_open(_opener, req, data=None, timeout=None):
            # The only way out of this process. It records what Calendar and the provider exchange and answers as the real
            # Host Service and the made-up provider do; it never alters an answer.
            assert data is None
            method, url = req.get_method(), urlsplit(req.full_url)
            headers = dict(req.header_items())
            if req.full_url.startswith(BASE + '/') and (method, url.path) in HOST_ROUTES:
                out = host.request(method, url.path, headers=headers, content=req.data)
                if url.path == '/sign-in/token':
                    tokens.append((json.loads(req.data), out.status_code, out.json()))
            elif req.full_url.startswith(gp.API + '/'):
                bearer = provider.access[httpx.Headers(headers)['Authorization'].removeprefix('Bearer ')]
                assert bearer['person']['sub'] == SUB and set(bearer['scope'].split()) == set(gp.SCOPES)
                route = url.path.removeprefix(urlsplit(gp.API).path)
                if (method, route) == ('GET', '/users/me/calendarList'):
                    out = httpx.Response(200, json={'items': [dict(id=name, summary=name, accessRole='owner', timeZone='UTC')
                                                              for name in ('fixture-home', 'fixture-work')]})
                elif method == 'POST' and route.startswith('/calendars/') and route.endswith('/events'):
                    ack = {**json.loads(req.data), 'etag': 'fixture-ack', 'status': 'confirmed'}
                    writes.append((route, bearer['client'], bearer['person']['email'], ack))
                    out = httpx.Response(201, json=ack)
                else:
                    stray.append(req.full_url)
                    raise AssertionError(f'unexpected Google Calendar call: {method} {route}')
            else:
                stray.append(req.full_url)
                raise AssertionError(f'unexpected urllib destination: {req.full_url}')
            stream = io.BytesIO(out.content)
            stream.status, stream.headers = out.status_code, out.headers
            if out.status_code >= 400:
                raise urllib.error.HTTPError(req.full_url, out.status_code, 'fixture', stream.headers, stream)
            return stream

        monkeypatch.setattr(urllib.request.OpenerDirector, 'open', in_memory_open)
        bridge = host_sign_in.HostSignIn()
        assert bridge.available()
        api = SimpleNamespace(get_state_dir=lambda: state, get_settings=lambda keys: {})
        handed = None

        def worker_pass():  # one ordinary pass of Calendar's companion: hand over what the skill held, then follow the host
            nonlocal handed
            handed, _ = google_host.worker_pass(db, {}, state, bridge, handed, lambda *args, **kwargs: None)

        def card():
            return next(row for row in owner.get('/api/sign-in/status').json()['services']
                        if (row['skill'], row['service']) == ('calendar', 'google'))

        def consent(client, account=''):  # the owner signs in on Calendar's card with a saved client, via the core's own helper
            shown = card()
            words = sorted(set(shown['scopes']).union(*(shown['scope_spellings'].get(scope, ()) for scope in shown['scopes'])))
            done = fx._sign_in(provider, owner, 'calendar', client=client, scope_words=words,
                               **({'account': account} if account else {}))
            assert done['ok'] is True and done['missing_required'] == []
            return done['account']['key']

        def connection():  # the connection the Host Service lists for the one Google account, as Calendar's client reads it
            [listed] = bridge.accounts()
            assert listed['verified'] is True and listed.get('connection_id')
            return listed['connection_id']

        def defer_create(title, day):
            # The owner confirms one create in the home calendar while the host has no token to give: it waits, unsent,
            # under its own ID, and nothing is queued or written in any other calendar.
            provider.dead.update(provider.refresh)  # the provider no longer honours the refresh token the card holds
            clock[0] += timedelta(days=1)  # and the access token has run out
            written = len(writes)
            ctx = cal.Context(api)  # nothing of Calendar's own adapter or bearer survives from before
            args = dict(title=title, start=f'2031-12-{day}T10:00:00Z', end=f'2031-12-{day}T11:00:00Z',
                        calendars=[home_id], attendees=[], reminders=[], send_invites=False)
            assert json.loads(cal.cal_create(ctx, **args))['status'] == 'needs_confirm'
            result = json.loads(cal.cal_create(ctx, confirm=True, **args))
            [assignment] = result['assignments']
            event = db.get_event(result['event']['id'])
            [intent] = db.intents_for_event(event['id'])
            assert result['status'] == assignment['status'] == event['sync_state'] == 'pending'
            assert (intent['kind'], intent['state'], intent['attempts'], intent['calendar_id']) == ('create', 'pending', 0, home_id)
            assert [row['id'] for row in db.unsettled_account_intents(account_id)] == [intent['id']] and len(writes) == written
            return event, intent

        # Handover: the worker hands the skill's saved grant to the host. The same account, calendars and roles carry on.
        worker_pass()
        [held] = card()['accounts']
        key, [personal] = held['key'], [saved['id'] for saved in card()['clients']]
        assert (held['subject'], held['verified'], held['state'], held['via']) == (SUB, True, 'signed_in', 'handover')
        first_connection = connection()
        ctx = cal.Context(api)
        [found] = accounts.reload_google(ctx)['accounts']  # Calendar's own discovery, now with the token the host gives
        assert (found['account'], found['status']) == (account_id, 'ok') and {c['id'] for c in found['calendars']} == set(baseline)
        assert isinstance(ctx.providers.adapter_for(account_id), google_host.HostGoogleAdapter)
        account_stands()

        # Phase 1, an ordinary re-sign-in: the grant dies and a write waits, then the same subject signs in again with the IT client.
        event, intent = defer_create('Fixture queued event', '01')
        provider.person['email'] = NEW  # the same subject under another display address
        it_card = fx._save_client(owner, 'IT fixture', IT, IT_SECRET)
        assert consent(it_card, account=key) == key
        [signed] = card()['accounts']
        assert (signed['key'], signed['subject'], signed['label'], signed['client']['id']) == (key, SUB, NEW, it_card)
        assert connection() == first_connection  # an ordinary re-sign-in keeps the connection
        worker_pass()
        account_stands()
        assert db.get_event(event['id']) == event and db.get_intent(intent['id']) == intent  # still that one waiting write
        clock[0] = parse_stored(intent['next_attempt_at']) + timedelta(seconds=1)  # the queue's own due time
        mark = len(tokens)
        due = ops.retry_due_intents(db, Providers({}, state, host=bridge))
        assert [row['intent'] for row in due] == [intent['id']]
        asked = tokens[mark:]  # what Calendar itself asked the host for this one write, and what it was answered
        assert asked and all(
            body.get('account') == key and body.get('connection_id') == first_connection and status == 200
            and reply.get('connection_id') == first_connection and reply.get('token_type') == 'Bearer'
            and reply['account']['key'] == key
            for body, status, reply in asked), 'Calendar must ask the host for its token under the connection it holds and be answered under it'
        [(route, client, email, ack)] = writes
        assert (route, client, email, ack['summary']) == ('/calendars/fixture-home/events', IT, NEW, 'Fixture queued event')
        [receipt] = db.intents_for_event(event['id'])
        sent = db.get_event(event['id'])
        assert (receipt['id'], receipt['state'], receipt['attempts'], receipt['payload_json']) == (
            intent['id'], 'done', 1, intent['payload_json'])
        assert (sent['id'], sent['calendar_id'], sent['sync_state'], sent['external_id']) == (
            event['id'], home_id, 'synced', ack['id'])
        assert db.unsettled_account_intents(account_id) == []
        account_stands()

        # Phase 2, an explicit Disconnect and a new consent: a second write waits, the Disconnect withdraws it, and it stays withdrawn.
        _, revoked = defer_create('Fixture withdrawn event', '02')
        [current] = card()['accounts']
        disconnected = owner.post('/api/sign-in', json=dict(action='disconnect', skill='calendar', service='google',
                                                            account=key, consent=current['consent'])).json()
        assert disconnected['ok'] is True and card()['accounts'] == []
        worker_pass()  # Calendar follows the Disconnect; from here on the state is only read
        withdrawn = db.get_intent(revoked['id'])
        assert (withdrawn['state'], withdrawn['attempts']) == ('resolved', 0)
        assert db.get_account(account_id) is None and db.unsettled_account_intents(account_id) == []
        assert consent(personal) == key  # the same subject signs in again with the Personal client
        assert connection() != first_connection  # a consent after a Disconnect is a new connection
        worker_pass()
        [restored] = accounts.reload_google(cal.Context(api))['accounts']
        assert (restored['account'], restored['status']) == (account_id, 'ok')
        assert {row['id'] for row in db.list_calendars(account_id=account_id)} == set(baseline)
        assert db.default_calendar()['id'] == home_id
        account_stands()
        assert db.get_intent(revoked['id']) == withdrawn and db.unsettled_account_intents(account_id) == []
        assert len(writes) == 1 and not stray
