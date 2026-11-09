"""Calendar through the actual opaque module bridge and the skill's registered routes.

Opt in with OUROBOROS_THEME_CORE_ROOT. No installed skill or personal account is used. The page is a small host: it
serves the core's own web modules, answers the widget lease controls the core asks for before it builds a frame and
passes the skill's real routes to the browser. Only the edges to Google and to a CalDAV server are replaced.
"""
import asyncio
import errno
import http.client
import json
import mimetypes
import os
import shutil
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = str(ROOT / 'skills/calendar/tests')
SECRET = 'S3cret-pass-123'
LEASE = {'token': 'lease', 'base_url': '/api/widget-resources/lease/', 'expires_in': 120, 'renew_after': 40}
HOST = '''<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0}iframe{width:100%;height:__HEIGHT__px;border:0}</style></head>
<body><section><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('light');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'calendar',ws_prefix:'ext:calendar:'},
 {entry:'widget.js',height:__HEIGHT__,appearance:'host'},null,new Set());
window.ready=true;
</script></body></html>'''


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    """The skill's own HTTP calls (the begin route probes this host's return page) may only reach loopback."""
    import urllib.request
    real = urllib.request.OpenerDirector.open

    def guarded(opener, request, *args, **kwargs):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        assert urlsplit(url).hostname in ('127.0.0.1', 'localhost'), f'outside network: {url}'
        return real(opener, request, *args, **kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', guarded)


def core_root():
    configured = os.environ.get('OUROBOROS_THEME_CORE_ROOT')
    if not configured:
        pytest.skip('set OUROBOROS_THEME_CORE_ROOT for the real host bridge')
    core = Path(configured).resolve()
    assert (core / 'web/modules/widget_module.js').is_file()
    return core


def request_class():
    from test_calendar_round2 import Request

    class HostRequest(Request):
        def __init__(self, body, query, headers, method):
            super().__init__(body, query)
            self.method = method
            self.headers = headers   # the real request's: the begin route reads the Origin the bridge sent
    return HostRequest


# Socket write failures that mean the client closed its end. Nothing broader: other OSError/ConnectionError stays an error.
DISCONNECTED = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def encode(body):
    return body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()


@contextmanager
def serve(core, routes_api):
    """The page, the lease controls and one skill's routes on a loopback port."""
    state = {'posts': [], 'reads': [], 'responses': [], 'errors': [], 'disconnects': []}
    request = request_class()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, mime='application/json', status=200):
            body = encode(body)
            try:
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
            except DISCONNECTED as exc:
                # The browser went away while its answer was being written (a closed page or context, an abandoned
                # fetch): the answer existed, only its delivery failed. Kept apart from errors, with what was sent.
                state['disconnects'].append((self.command, self.path, status, type(exc).__name__))
                self.close_connection = True
            except Exception as exc:   # any other failure to write stays an error
                state['errors'].append(repr(exc))
                raise

        def dispatch(self):
            url = urlsplit(self.path)
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if url.path.startswith('/api/widgets/asset-leases'):
                return self.send({'ok': True} if url.path.endswith('/revoke') else LEASE)
            if url.path == '/':
                return self.send(HOST.replace('__HEIGHT__', parse_qs(url.query).get('height', ['720'])[-1]), 'text/html')
            for mount in ('/static/', '/onboarding/static/'):   # the core serves one builtin tree under both
                if url.path.startswith(mount):
                    web = core / 'web'
                    source = (web / url.path[len(mount):]).resolve()
                    if source.is_relative_to(web) and source.is_file():
                        return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or 'text/javascript')
            prefix = '/api/extensions/calendar/'
            if url.path == prefix + 'module/widget.js':
                return self.send((ROOT / 'skills/calendar/widget.js').read_bytes(), 'text/javascript')
            name = url.path.removeprefix(prefix)
            if url.path.startswith(prefix) and name in routes_api.routes:
                body = json.loads(raw or b'{}')
                (state['posts'] if self.command == 'POST' else state['reads']).append((name, body) if self.command == 'POST' else name)
                try:   # the route and its answer; any exception here, a ConnectionError raised by the route included, is an error
                    result = asyncio.run(routes_api.routes[name](request(body, {k: v[-1] for k, v in parse_qs(url.query).items()}, self.headers, self.command)))
                    state['responses'].append((name, result.status_code, result.body))
                    answer = encode(result.body)
                except Exception as exc:
                    state['errors'].append(repr(exc))
                    return self.send({'error': str(exc)}, status=500)
                return self.send(answer, status=result.status_code)
            return self.send({'error': 'not found'}, status=404)
        do_GET = do_POST = do_HEAD = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    # Handler threads are not daemons, so server_close() joins every request still running: a late route finishes, and
    # records its outcome, before the fixture inspects the state or deletes the store the route writes to.
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{server.server_port}', server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def calendar_server():
    core = core_root()
    sys.path.insert(0, TESTS)
    from test_calendar_provider_divergence import google_context, _day
    from test_calendar_round2 import RouteAPI
    import routes
    import tools
    ctx, calendar_id, provider = google_context()
    fixed_now = _day(0, 12)
    ctx.now = lambda: fixed_now.astimezone(ctx.tz)
    api = RouteAPI()
    routes.register_routes(api, lambda: ctx)
    event = json.loads(tools.cal_create(ctx, title='Notify meeting', start=fixed_now.replace(hour=9).isoformat(),
        calendars=[calendar_id], attendees=['guest@example.test'], confirm=True))['event']
    try:
        with serve(core, api) as (state, url, _):
            yield ctx, provider, event, state, url
    finally:
        shutil.rmtree(ctx.state_dir)
        sys.path.remove(TESTS)
    assert not state['errors'], state['errors']


@pytest.fixture
def skill_host():
    """The real routes over a fresh store whose Google client is configured; nothing is connected yet."""
    core = core_root()
    sys.path.insert(0, TESTS)
    from test_calendar_round2 import FakeAPI, RouteAPI
    import routes
    import tools
    fake = FakeAPI({'GOOGLE_OAUTH_CLIENT_ID': 'test-client', 'GOOGLE_OAUTH_CLIENT_SECRET': 'test-secret'})
    ctx = tools.Context(fake)
    ctx.store.set_setting('timezone', 'UTC')
    api = RouteAPI()
    routes.register_routes(api, lambda: tools.Context(fake))   # the runtime builds a fresh context for every route call
    try:
        with serve(core, api) as (state, url, port):
            fake.get_runtime_info = lambda: {'server_port': port}   # the begin route probes this host's own return page
            yield ctx, state, url
    finally:
        shutil.rmtree(ctx.state_dir)
        sys.path.remove(TESTS)
    assert not state['errors'], state['errors']


@pytest.fixture
def link_host():
    """Two linked groups between two local calendars: a source in «work» and its copy in «personal»."""
    core = core_root()
    sys.path.insert(0, TESTS)
    from test_calendar_provider_divergence import _day
    from test_calendar_round2 import FakeAPI, RouteAPI, add_local_calendar
    import routes
    import tools
    from model import get_tz
    fake = FakeAPI()
    ctx = tools.Context(fake)
    ctx.store.set_setting('timezone', 'UTC')
    ctx.tz = get_tz('UTC')
    work, personal = add_local_calendar(ctx.store, 'work', 'full'), add_local_calendar(ctx.store, 'personal', 'full')
    groups = {}
    for title, hour in (('Planning', 9), ('Review', 11)):
        made = json.loads(tools.cal_create(ctx, title=title, start=_day(0, hour).isoformat(), duration_min=60, calendars=[work, personal], confirm=True))
        source = made['event']['id']
        copy = next(m['id'] for m in ctx.store.group_masters(ctx.store.get_event(source)['link_group_id']) if m['id'] != source)
        groups[title] = {'source': source, 'copy': copy}
    api = RouteAPI()
    routes.register_routes(api, lambda: tools.Context(fake))
    try:
        with serve(core, api) as (state, url, _):
            yield ctx, groups, state, url
    finally:
        shutil.rmtree(ctx.state_dir)
        sys.path.remove(TESTS)
    assert not state['errors'], state['errors']


def evidence_dir(tmp_path):
    evidence = Path(os.environ.get('OUROBOROS_CALENDAR_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    return evidence


def wait_until(page, condition, seconds=20):
    for _ in range(seconds * 20):
        if condition():
            return
        page.wait_for_timeout(50)
    raise AssertionError('the browser did not reach the expected state')


def widget_frame(browser, url, engine, height=720, timezone_id=None, width=None):
    """The real module frame: retain phone coverage for WebKit; grid-only gestures name a wide viewport."""
    from playwright.sync_api import expect
    expect.set_options(timeout=20_000)
    width = width or (390 if engine == 'webkit' else 980)
    context = browser.new_context(viewport={'width': width, 'height': 900},
                                  **({'timezone_id': timezone_id} if timezone_id else {}))
    context.outside, context.consent = [], []

    def guard(route):
        request = route.request
        host = urlsplit(request.url).hostname
        if host in ('127.0.0.1', 'localhost'):
            return route.continue_()
        if host == 'accounts.google.com':   # Google's consent page: answered by a local stub that records what the host opened
            context.consent.append(request.url)
            return route.fulfill(status=200, content_type='text/html', body='<!doctype html><title>Google consent (stub)</title>')
        context.outside.append(request.url)
        route.abort()
    context.route('**/*', guard)
    page = context.new_page()
    page.goto(f'{url}/?height={height}')
    page.wait_for_function('window.ready === true')
    assert 'allow-forms' not in page.locator('iframe').get_attribute('sandbox')
    frame = page.locator('iframe').element_handle().content_frame()
    frame.wait_for_selector('.mobile-agenda' if width <= 560 else '.grid')
    expect(frame.locator('html')).to_have_attribute('data-theme', 'light')
    return context, page, frame


def shoot(page, frame, evidence, name):
    from playwright.sync_api import expect
    for theme in ('light', 'dark'):
        page.evaluate(f"ouroTheme.set('{theme}')")
        expect(frame.locator('html')).to_have_attribute('data-theme', theme)
        page.screenshot(path=str(evidence / f'{name}-{theme}.png'))
    page.evaluate("ouroTheme.set('light')")


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('scenario', ['save', 'delete'])
def test_calendar_host_card(calendar_server, tmp_path, engine, scenario):
    from playwright.sync_api import sync_playwright, expect
    ctx, provider, event, state, url = calendar_server
    evidence = evidence_dir(tmp_path)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine)
            if scenario == 'save':
                frame.get_by_role('button', name='+ Событие', exact=True).click()
                frame.get_by_placeholder('Название', exact=True).fill('Host-created')
                frame.get_by_role('button', name='Создать', exact=True).click()
                expect(frame.locator('.card')).to_have_count(0)
                assert [x for x in state['posts'] if x[0] == 'event']
                frame.locator('.ev, button.mobile-event', has_text='Host-created').click()
                expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
                frame.get_by_placeholder('Название', exact=True).fill('Host-edited')
                frame.get_by_placeholder('Название', exact=True).press('Enter')
                expect(frame.locator('.card')).to_have_count(0)
                expect(frame.locator('.ev, button.mobile-event', has_text='Host-edited')).to_have_count(1)
                assert [x for x in state['posts'] if x[0] == 'event/update'][-1][1]['title'] == 'Host-edited'
            else:
                frame.locator('.ev, button.mobile-event', has_text='Notify meeting').click()
                expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
                notify = frame.locator('label.inline', has_text='уведомить участников').locator('input')
                notify.check()
                frame.get_by_placeholder('Место или ссылка').fill('Unsaved location retained')
                frame.get_by_role('button', name='Удалить', exact=True).click()
                expect(notify).to_be_checked()
                expect(frame.get_by_placeholder('Место или ссылка')).to_have_value('Unsaved location retained')
                frame.get_by_role('button', name='Точно удалить?', exact=True).click()
                expect(frame.locator('.card')).to_have_count(0)
                deletion = [item for item in state['posts'] if item[0] == 'event/delete']
                assert len(deletion) == 1
                assert deletion[0][1]['send_updates'] is True
                assert [c for c in provider.calls if c[0] == 'DELETE'][-1][2]['sendUpdates'] == 'all'
            for theme in ('light', 'dark'):
                page.evaluate(f"ouroTheme.set('{theme}')")
                expect(frame.locator('html')).to_have_attribute('data-theme', theme)
                page.screenshot(path=str(evidence / f'calendar-{engine}-{scenario}-{theme}.png'))
            assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.evaluate('async () => { await disposeWidget(); }')
            expect(page.locator('iframe')).to_have_count(0)
            assert context.outside == []
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_calendar_host_phone_agenda(calendar_server, tmp_path, engine):
    """Both engines show Day/Week through the real bridge at phone width, with the real event card."""
    from playwright.sync_api import sync_playwright, expect
    ctx, provider, event, state, url = calendar_server
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine, width=390)
            expect(frame.locator('.mobile-event', has_text='Notify meeting')).to_be_visible()
            assert frame.locator('.grid-scroll').count() == 0
            frame.get_by_role('button', name='Неделя', exact=True).click()
            expect(frame.locator('.mobile-day')).to_have_count(7)
            frame.locator('.mobile-day.today').get_by_role('button', name='Notify meeting').click()
            expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
            expect(frame.get_by_placeholder('Название', exact=True)).to_have_value('Notify meeting')
            frame.get_by_role('button', name='Отмена', exact=True).click()
            shoot(page, frame, evidence_dir(tmp_path), f'calendar-{engine}-phone-week')
            assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.evaluate('async () => { await disposeWidget(); }')
            expect(page.locator('iframe')).to_have_count(0)
            assert context.outside == []
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_google_sign_in_returns_through_the_host_page_and_only_then_counts(skill_host, tmp_path, engine):
    """Widget -> bridge -> real begin route -> link opened by the host -> (Google) -> the core's own return page ->
    real complete route -> the widget's account list. Google answers only by redirecting back; its token and identity
    calls are the two replaced edges."""
    from playwright.sync_api import sync_playwright, expect
    import credentials
    import providers_google as google
    ctx, state, url = skill_host
    evidence = evidence_dir(tmp_path)
    exchanged = {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh', 'expires_in': 3600, 'scope': ' '.join(google.SCOPES)}

    def identity(adapter):
        adapter._identity = {'email': 'owner@example.test', 'sub': 'subject-one'}
        return 'owner@example.test'
    with patch.object(google, '_token_request', return_value=exchanged), patch.object(google.GoogleAdapter, 'primary_email', identity), \
            sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine, height=1500)
            frame.get_by_role('button', name='Аккаунты', exact=True).click()
            expect(frame.locator('section.acc')).to_be_visible()
            expect(frame.locator('section.acc .acc-row')).to_have_count(0)
            with context.expect_page() as opened:
                frame.get_by_role('button', name='Подключить Google').click()
            popup = opened.value   # the stub that stands for Google's consent page
            assert len(context.consent) == 1 and context.consent[0].startswith('https://accounts.google.com/o/oauth2/v2/auth?')
            query = parse_qs(urlsplit(context.consent[0]).query)
            assert query['client_id'] == ['test-client'] and query['code_challenge_method'] == ['S256']
            assert query['redirect_uri'] == [f'{url}/onboarding/static/provider-return.html']   # the Origin the bridge sent, not the frame's null
            # the link is open and Google has not answered: nothing may claim a connection
            waiting = frame.locator('.acc-wait .wait')
            expect(waiting).to_be_visible()
            reads = state['reads'].count('status')
            frame.get_by_role('button', name='Проверить').click()
            wait_until(page, lambda: state['reads'].count('status') > reads)
            expect(waiting).to_be_visible()
            expect(frame.locator('.msg.ok')).to_have_count(0)
            expect(frame.locator('section.acc .acc-row')).to_have_count(0)
            shoot(page, frame, evidence, f'calendar-{engine}-google-waiting')
            # Google answers by sending the browser back to the redirect address with the code and the issued state
            popup.goto(query['redirect_uri'][0] + '?' + urlencode({'code': 'test-code', 'state': query['state'][0]}))
            expect(popup.locator('[data-return-status]')).to_have_attribute('data-tone', 'ok')
            expect(popup.locator('[data-return-account]')).to_have_text('owner@example.test')
            # Polling may already have removed the pending-only button; the connection is the outcome to await.
            expect(frame.locator('.msg.ok')).to_contain_text('owner@example.test')
            expect(waiting).to_have_count(0)
            row = frame.locator('section.acc .acc-row')
            expect(row).to_have_count(1)
            assert row.locator('.dot').get_attribute('class') == 'dot ok'
            assert not any(b'synthetic-' in body for _, _, body in state['responses'])   # no token ever leaves the skill
            shoot(page, frame, evidence, f'calendar-{engine}-google-connected')
            row.get_by_role('button', name='Отключить owner@example.test').click()
            row.locator('.acc-confirm').get_by_role('button', name='Отключить', exact=True).click()
            expect(frame.locator('.msg.ok', has_text='отключён')).to_be_visible()
            expect(row).to_have_count(0)
            assert credentials.load_tokens(ctx.state_dir, '') == {}   # local disconnect dropped the stored tokens
            assert context.outside == []   # nothing but loopback and the consent stub was requested
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_caldav_form_connects_and_disconnects_through_the_real_routes(skill_host, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    import credentials
    import ops
    import providers
    ctx, state, url = skill_host
    evidence = evidence_dir(tmp_path)
    outcome = {'error': ops.ProviderError('auth', 'Сервер отклонил логин или пароль')}

    def discover(adapter):
        if outcome['error']:
            raise outcome['error']
        return [{'id': adapter.account_id + ':work', 'account_id': adapter.account_id, 'provider': adapter.provider,
                 'external_id': 'work', 'href': adapter.home + 'work/', 'name': 'Work', 'writable': True}]
    with patch.object(providers.CalDAVAdapter, 'list_calendars', discover), sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine, height=1500)
            frame.get_by_role('button', name='Аккаунты', exact=True).click()
            password = frame.locator('input[type=password]')

            def submit():
                frame.get_by_label('Название (необязательно)').fill('Work')
                frame.get_by_label('Адрес сервера CalDAV').fill('https://dav.example.test/dav/')
                frame.get_by_label('Логин').fill('person')
                password.fill(SECRET)
                frame.get_by_role('button', name='Подключить', exact=True).click()
                expect(password).to_have_value('')   # emptied as the request leaves, whatever the answer
            submit()
            failure = frame.locator('.msg.err')
            expect(failure).to_contain_text('Сервер отклонил логин или пароль')
            expect(failure).to_contain_text('сохранён')   # the server answered «saved»: the list shows the account with its error
            row = frame.locator('section.acc .acc-row')
            expect(row).to_have_count(1)
            assert row.locator('.dot').get_attribute('class') == 'dot err'
            account_id = row.get_attribute('data-id')
            assert credentials.load_tokens(ctx.state_dir, '')[account_id]['password'] == SECRET   # kept in the vault...
            assert not any(SECRET.encode() in body for _, _, body in state['responses'])           # ...and never answered
            assert SECRET not in frame.evaluate('document.documentElement.outerHTML + document.body.innerText')
            frame.get_by_role('button', name='Обновить', exact=True).click()
            frame.get_by_role('button', name='Сегодня').click()
            expect(failure).to_be_visible()   # a successful list read and agenda refresh leave the failure in place
            shoot(page, frame, evidence, f'calendar-{engine}-caldav-failed')
            outcome['error'] = None
            submit()
            expect(frame.locator('.msg.ok')).to_be_visible()
            expect(row).to_have_count(1)
            assert row.get_attribute('data-id') == account_id
            expect(row.locator('.dot')).to_have_class('dot ok')
            values = [part.rsplit(': ', 1)[1] for part in row.locator('.caps').inner_text().split(' · ')]
            assert values[0] == values[1] == values[2] and len(set(values)) == 2   # unknown stays unknown, sharing is a plain «no»
            assert not any(SECRET.encode() in body for _, _, body in state['responses'])
            shoot(page, frame, evidence, f'calendar-{engine}-caldav-connected')
            row.get_by_role('button', name='Отключить Work').click()
            row.locator('.acc-confirm').get_by_role('button', name='Отключить', exact=True).click()
            expect(frame.locator('.msg.ok', has_text='отключён')).to_be_visible()
            expect(row).to_have_count(0)
            assert account_id not in credentials.load_tokens(ctx.state_dir, '')
            assert context.outside == []
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_link_actions_change_the_real_store_only_after_the_owner_applies_them(link_host, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    import links
    import tools
    ctx, groups, state, url = link_host
    evidence = evidence_dir(tmp_path)
    planning, review = groups['Planning'], groups['Review']
    store = ctx.store
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine, height=1200)
            strip = frame.locator('.linkissues')
            expect(strip).to_have_count(0)   # nothing differs yet

            def open_copy(title):
                frame.locator('.ev, button.mobile-event').filter(has_text=title).filter(has_text='personal').click()
                expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
                return frame.locator('.linkbox')

            def apply(box, action):
                box.get_by_role('button', name=action, exact=True).click()
                expect(box.locator('.preview')).to_be_visible()
                posts = len(state['posts'])
                box.get_by_role('button', name='Применить').click()
                expect(frame.locator('.card')).to_have_count(0)
                assert len(state['posts']) == posts + 1 and state['posts'][-1][0] == 'event/link'
            # the copy is edited on its own: the difference stays visible and nothing repairs it
            assert json.loads(tools.cal_update(ctx, id=planning['copy'], title='Edited in personal', confirm=True))['status'] == 'updated'
            frame.get_by_role('button', name='Сегодня').click()
            expect(strip).to_have_count(1)
            strip.get_by_role('button', name='Копии связанных событий').click()
            expect(strip.locator('.issue')).to_have_count(1)
            assert store.get_event(planning['copy'])['link_state'] == links.DIVERGED
            strip.get_by_role('button', name='Открыть').click()
            box = frame.locator('.linkbox')
            expect(box.get_by_role('button', name='Следовать источнику', exact=True)).to_be_visible()
            box.get_by_role('button', name='Следовать источнику', exact=True).click()
            expect(box.locator('.preview')).to_contain_text('personal')
            assert [p for p in state['posts'] if p[0] == 'event/link'] == []   # explained, not sent
            shoot(page, frame, evidence, f'calendar-{engine}-link-follow')
            box.get_by_role('button', name='Применить').click()
            expect(frame.locator('.card')).to_have_count(0)
            assert state['posts'][-1] == ('event/link', {'id': planning['copy'], 'action': 'follow', 'scope': 'all'})
            expect(strip).to_have_count(0)
            copy = store.get_event(planning['copy'])
            assert (copy['title'], copy['link_state']) == ('Planning', links.FOLLOWING)
            assert store.get_event(planning['source'])['is_primary'] == 1   # following never touched the original
            # the owner makes the copy the source of its group
            apply(open_copy('Review'), 'Сделать источником')
            assert links.source(store, store.get_event(review['copy']))['id'] == review['copy']
            assert store.get_event(review['source'])['is_primary'] == 0
            # the provider removed the other member: it stays listed as removed and offers only to be forgotten
            store.delete_event(review['source'])
            store.update_event(review['source'], {'link_state': links.MISSING})
            frame.get_by_role('button', name='Сегодня').click()
            expect(strip).to_have_count(1)
            strip.get_by_role('button', name='Открыть').click()
            box = frame.locator('.linkbox')
            expect(box.get_by_role('button', name='Отвязать')).to_be_visible()
            assert box.locator('.acts button').count() == 1
            assert frame.get_by_placeholder('Название').is_disabled()
            shoot(page, frame, evidence, f'calendar-{engine}-link-removed-copy')
            apply(box, 'Отвязать')
            assert store.get_event(review['source'])['link_group_id'] == ''
            expect(strip).to_have_count(0)
            # a copy leaves its group on request; the original keeps its own data
            apply(open_copy('Planning'), 'Отвязать')
            assert store.get_event(planning['copy'])['link_group_id'] == ''
            assert store.get_event(planning['source'])['title'] == 'Planning'
            assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert context.outside == []
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('partial', [False, True])
def test_calendar_reassign_failure_stays_visible_and_success_still_closes(link_host, tmp_path, engine, partial):
    from playwright.sync_api import sync_playwright, expect
    ctx, groups, state, url = link_host
    group = groups['Planning']
    ctx.store.update_event(group['copy'], {'is_primary': 1})
    evidence = evidence_dir(tmp_path)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine, height=720)
            frame.locator('.ev, button.mobile-event').filter(has_text='Planning').filter(has_text='work').click()
            expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
            frame.locator('.card .cals label', has_text='personal').locator('input').uncheck()
            if partial:
                frame.get_by_placeholder('Название', exact=True).fill('Content applied')
            frame.get_by_role('button', name='Сохранить', exact=True).click()
            wait_until(page, lambda: any(name == 'event/update' for name, _, _ in state['responses']))
            response = next(json.loads(raw) for name, _, raw in reversed(state['responses']) if name == 'event/update')
            assert response['status'] == ('partial' if partial else 'failed'), response
            if partial:
                expect(frame.locator('.card')).to_have_count(0)
                expect(frame.locator('.calendar-body > .status', has_text='источник')).to_be_visible()
                assert ctx.store.get_event(group['source'])['title'] == 'Content applied'
                frame.get_by_role('button', name='Сегодня', exact=True).click()
                expect(frame.locator('.calendar-body > .status', has_text='источник')).to_be_visible()
            else:
                expect(frame.locator('.card .status.err')).to_contain_text('источник')
                expect(frame.get_by_role('button', name='Сохранить', exact=True)).to_be_enabled()
            shoot(page, frame, evidence, f'calendar-{engine}-reassign-{ "partial" if partial else "failed" }')
            assert ctx.store.get_event(group['copy'])['deleted_at'] is None
            if not partial:
                ctx.store.update_event(group['copy'], {'is_primary': 0})
                frame.get_by_role('button', name='Сохранить', exact=True).click()
                expect(frame.locator('.card')).to_have_count(0)
                assert ctx.store.get_event(group['copy'])['deleted_at']
            assert context.outside == []
        finally:
            browser.close()


@pytest.fixture
def temporal_host():
    """Real calendar routes with a fixed owner day and independent browser timezone."""
    from datetime import datetime
    core = core_root()
    sys.path.insert(0, TESTS)
    from test_calendar_round2 import FakeAPI, RouteAPI
    from model import get_tz
    import routes
    import tools
    ctx = tools.Context(FakeAPI())
    ctx.tz = get_tz('Europe/Moscow')
    ctx.now = lambda: datetime(2026, 3, 8, 1, tzinfo=ctx.tz)
    timed = json.loads(tools.cal_create(ctx, title='Owner clock', start='2026-03-08T02:30:00+03:00',
                                      end='2026-03-08T03:30:00+03:00', confirm=True))['event']['id']
    dated = json.loads(tools.cal_create(ctx, title='Calendar date', start='2026-03-08', end='2026-03-09',
                                      all_day=True, confirm=True))['event']['id']
    api = RouteAPI()
    routes.register_routes(api, lambda: ctx)
    try:
        with serve(core, api) as (state, url, _):
            yield ctx, timed, dated, state, url
    finally:
        shutil.rmtree(ctx.state_dir)
        sys.path.remove(TESTS)
    assert not state['errors'], state['errors']


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('browser_zone', ['America/New_York', 'UTC'])
def test_owner_wall_clock_resize_ignores_browser_dst(temporal_host, tmp_path, engine, browser_zone):
    from playwright.sync_api import sync_playwright, expect
    from model import parse_stored
    ctx, event_id, _, state, url = temporal_host
    original = ctx.store.get_event(event_id)['start_utc']
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine, timezone_id=browser_zone, width=980)  # resize handles belong to the grid
            event = frame.locator('.ev, button.mobile-event', has_text='Owner clock')
            expect(event.locator('.m')).to_contain_text('02:30–03:30')
            hour = frame.locator('.col .hline').first.bounding_box()['height']
            assert abs(event.bounding_box()['y'] - frame.locator('.col').first.bounding_box()['y'] - 2.5 * hour) < 1
            handle = event.locator('.rs').bounding_box()
            page.mouse.move(handle['x'] + handle['width'] / 2, handle['y'] + handle['height'] / 2)
            page.mouse.down()
            page.mouse.move(handle['x'] + handle['width'] / 2, handle['y'] + handle['height'] / 2 + hour, steps=6)
            page.mouse.up()
            wait_until(page, lambda: any(name == 'event/update' for name, _ in state['posts']))
            body = next(body for name, body in reversed(state['posts']) if name == 'event/update')
            assert 'start' not in body and body['end'] == '2026-03-08T04:30'
            expect(event.locator('.m')).to_contain_text('02:30–04:30')
            saved = ctx.store.get_event(event_id)
            assert saved['start_utc'] == original
            assert parse_stored(saved['end_utc']).astimezone(ctx.tz).strftime('%H:%M') == '04:30'
            shoot(page, frame, evidence_dir(tmp_path), f'calendar-{engine}-wall-resize-{browser_zone.replace("/", "-")}')
            assert context.outside == []
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('owner_zone', ['Europe/Moscow', 'America/New_York'])
def test_all_day_title_card_keeps_dates_after_owner_zone_change(temporal_host, tmp_path, engine, owner_zone):
    from playwright.sync_api import sync_playwright, expect
    from model import get_tz
    ctx, _, event_id, state, url = temporal_host
    before = ctx.store.get_event(event_id)
    ctx.tz = get_tz(owner_zone)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            context, page, frame = widget_frame(browser, url, engine, timezone_id='America/New_York')
            event = frame.locator('.allday .chip, button.mobile-event', has_text='Calendar date')
            expect(event).to_be_visible()
            event.click()
            expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
            inputs = frame.locator('.card input[type=datetime-local]')
            expect(inputs.nth(0)).to_have_value('2026-03-08T00:00')
            expect(inputs.nth(1)).to_have_value('2026-03-09T00:00')
            frame.get_by_placeholder('Название', exact=True).fill('Date title changed')
            frame.get_by_role('button', name='Сохранить', exact=True).click()
            expect(frame.locator('.card')).to_have_count(0)
            body = next(body for name, body in reversed(state['posts']) if name == 'event/update')
            assert 'start' not in body and 'end' not in body
            saved = ctx.store.get_event(event_id)
            assert (saved['start_utc'], saved['end_utc'], saved['tz']) == (before['start_utc'], before['end_utc'], before['tz'])
            expect(frame.locator('.allday .chip, button.mobile-event', has_text='Date title changed')).to_be_visible()
            shoot(page, frame, evidence_dir(tmp_path), f'calendar-{engine}-all-day-{owner_zone.replace("/", "-")}')
            assert context.outside == []
        finally:
            browser.close()


# The host server's own account of failures: a client that leaves while its answer is written is a disconnect; a route
# that fails, whatever it raises, and any other write failure are errors, which every fixture above asserts away.
LARGE = 64 << 20   # bytes, far beyond either kernel's loopback buffers: this write cannot complete once the reader is gone


class Answer:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code


async def small(request):
    return Answer(b'{"ok": true}')


async def large(request):
    return Answer(bytes(LARGE))


@pytest.fixture
def bare_server(tmp_path):
    """serve() alone over a stand-in route table: no core, skill store or browser."""
    sys.path.insert(0, TESTS)
    table = SimpleNamespace(routes={'small': small, 'large': large})
    try:
        with serve(tmp_path, table) as (state, _, port):
            yield table.routes, state, port
    finally:
        sys.path.remove(TESTS)


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, 'the host server did not reach the expected state'
        time.sleep(0.01)


def get(port, name):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('GET', '/api/extensions/calendar/' + name)
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        connection.close()


def held_route(routes, name):
    """A route that answers LARGE only once the test releases it, so the client can leave first."""
    entered, release = threading.Event(), threading.Event()

    async def held(request):
        entered.set()
        assert release.wait(10), 'the test never released the route'
        return Answer(bytes(LARGE))
    routes[name] = held
    return entered, release


def assert_left_cleanly(state, port, name, departures=('BrokenPipeError', 'ConnectionResetError')):
    """One recorded departure for the answered route, no error, and the next request is served as usual."""
    wait_for(lambda: state['disconnects'])
    [(method, path, status, kind)] = state['disconnects']
    assert (method, path, status) == ('GET', '/api/extensions/calendar/' + name, 200) and kind in departures
    assert [(n, code) for n, code, _ in state['responses']] == [(name, 200)]
    assert get(port, 'small') == (200, b'{"ok": true}')
    assert state['errors'] == [] and len(state['disconnects']) == 1


def test_host_server_records_a_client_reset_mid_answer_apart_from_errors(bare_server):
    routes, state, port = bare_server
    client = socket.create_connection(('127.0.0.1', port), timeout=10)
    try:
        client.sendall(b'GET /api/extensions/calendar/large HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        received = b''
        while not received.partition(b'\r\n\r\n')[2]:   # the status line, headers and the first body bytes have arrived
            chunk = client.recv(65536)
            assert chunk, 'the server closed before writing its answer'
            received += chunk
        assert received.split(b'\r\n', 1)[0].endswith(b' 200 OK')
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))   # leave with a reset, as a killed tab does
    finally:
        client.close()
    assert_left_cleanly(state, port, 'large')


def test_host_server_records_a_client_gone_before_its_answer_apart_from_errors(bare_server):
    routes, state, port = bare_server
    entered, release = held_route(routes, 'held')
    client = socket.create_connection(('127.0.0.1', port), timeout=10)
    client.sendall(b'GET /api/extensions/calendar/held HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
    assert entered.wait(10)
    client.close()   # an ordinary close while the route is still working
    release.set()
    assert_left_cleanly(state, port, 'held')


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_host_server_records_a_closed_browser_apart_from_errors(bare_server, tmp_path, engine):
    """The real engines: a page's fetch is still being answered when its browser closes."""
    core_root()   # opt in like the other browser checks
    from playwright.sync_api import sync_playwright
    routes, state, port = bare_server
    entered, release = held_route(routes, 'held')
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        try:
            page = browser.new_page()
            page.goto(f'http://127.0.0.1:{port}/api/extensions/calendar/small')
            page.evaluate("() => { fetch('/api/extensions/calendar/held'); }")
            assert entered.wait(10)
        finally:
            browser.close()
    release.set()
    state['responses'].pop(0)   # the page itself
    assert_left_cleanly(state, port, 'held')


@pytest.mark.parametrize('failure, departure', [
    (BrokenPipeError(errno.EPIPE, 'Broken pipe'), True),
    (ConnectionResetError(errno.ECONNRESET, 'Connection reset by peer'), True),
    (ConnectionAbortedError(errno.ECONNABORTED, 'Software caused connection abort'), True),
    (ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused'), False),   # a ConnectionError, not a departure
    (TimeoutError('timed out'), False),
    (OSError(errno.EIO, 'Input/output error'), False),
])
def test_host_server_counts_only_client_departures_as_disconnects(bare_server, failure, departure):
    from http.server import BaseHTTPRequestHandler as base
    routes, state, port = bare_server
    with patch.object(base, 'flush_headers', side_effect=failure):   # the headers' write is where the socket answers
        with pytest.raises((http.client.HTTPException, OSError)):
            get(port, 'small')
        wait_for(lambda: state['disconnects'] or state['errors'])
    assert get(port, 'small') == (200, b'{"ok": true}')   # the sibling request is served as usual
    expected = [('GET', '/api/extensions/calendar/small', 200, type(failure).__name__)]
    assert (state['disconnects'], state['errors']) == ((expected, []) if departure else ([], [repr(failure)]))


@pytest.mark.parametrize('failure', [
    RuntimeError('the route broke'),
    ConnectionError('the provider connection failed'),   # raised by the route's own provider call, not by the client leaving
    BrokenPipeError(errno.EPIPE, 'Broken pipe'),
    ConnectionResetError(errno.ECONNRESET, 'Connection reset by peer'),
], ids=lambda failure: type(failure).__name__)
def test_host_server_keeps_route_failures_as_errors(bare_server, failure):
    routes, state, port = bare_server

    async def broken(request):
        raise failure
    routes['broken'] = broken
    assert get(port, 'broken') == (500, json.dumps({'error': str(failure)}).encode())
    assert state['errors'] == [repr(failure)] and state['disconnects'] == [] and state['responses'] == []


def test_host_server_keeps_an_unwritable_answer_an_error(bare_server):
    routes, state, port = bare_server

    async def unserializable(request):
        return Answer({'when': object()})
    routes['unserializable'] = unserializable
    status, body = get(port, 'unserializable')
    assert status == 500 and 'not JSON serializable' in json.loads(body)['error']
    assert len(state['errors']) == 1 and state['errors'][0].startswith('TypeError(') and state['disconnects'] == []


@contextmanager
def held_until_joined():
    """A route held until serve()'s exit joins the very handler thread running it.

    The route is still running when the exit starts, and resumes only when that exit waits for its thread, so an exit
    that does not wait finds it unfinished every time. On leaving, the route is released and its thread joined in any
    case, before the caller removes what the route writes to."""
    entered, release, held = threading.Event(), threading.Event(), []
    join = threading.Thread.join

    def joining(thread, *args, **kwargs):
        if any(thread is handler for handler in held):
            release.set()   # serve()'s exit is waiting for this handler: let its route finish
        return join(thread, *args, **kwargs)

    def hold():
        held.append(threading.current_thread())   # the route runs on its handler's thread
        entered.set()
        assert release.wait(10), "serve() never joined the handler running this route"
    try:
        with patch.object(threading.Thread, 'join', joining):
            yield hold, entered
    finally:
        release.set()
        for handler in held:
            join(handler, 10)


def answer_of(client):
    received = b''
    while chunk := client.recv(65536):
        received += chunk
    return received


def test_serve_exit_waits_for_a_late_route_before_its_store_is_removed():
    """The real draft route still writing the store when serve() exits: it completes first, then the store goes."""
    sys.path.insert(0, TESTS)
    from test_calendar_round2 import FakeAPI, RouteAPI
    import routes
    import tools
    fake = FakeAPI()
    ctx = tools.Context(fake)
    try:
        api = RouteAPI()
        routes.register_routes(api, lambda: tools.Context(fake))
        draft = api.routes['draft']
        value = {'event': {'title': 'Late'}, 'inputs': {'title': 'Late'}}
        payload = json.dumps({'draft': value}).encode()
        with held_until_joined() as (hold, entered):
            async def late_draft(request):
                hold()
                return await draft(request)
            api.routes['draft'] = late_draft
            with serve(Path(ctx.state_dir), api) as (state, _, port):
                client = socket.create_connection(('127.0.0.1', port), timeout=10)
                client.sendall(b'POST /api/extensions/calendar/draft HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                               b'Content-Length: %d\r\n\r\n%s' % (len(payload), payload))
                assert entered.wait(10)
            try:   # serve() has returned: the route has answered and recorded its outcome
                assert [(name, code) for name, code, _ in state['responses']] == [('draft', 200)] and state['errors'] == []
                assert answer_of(client).split(b'\r\n', 1)[0].endswith(b' 200 OK')
            finally:
                client.close()
            assert tools.Context(fake).store.get_setting('widget_draft') == value
    finally:
        shutil.rmtree(ctx.state_dir)   # as every fixture does after serve(): nothing writes into it any more
        sys.path.remove(TESTS)


def test_serve_exit_keeps_a_late_route_failure_visible(tmp_path):
    """A route that fails while serve() closes is in the errors before any fixture asserts them away."""
    sys.path.insert(0, TESTS)
    try:
        with held_until_joined() as (hold, entered):
            async def late_failure(request):
                hold()
                raise RuntimeError('failed while the server closed')
            with serve(tmp_path, SimpleNamespace(routes={'late': late_failure})) as (state, _, port):
                client = socket.create_connection(('127.0.0.1', port), timeout=10)
                client.sendall(b'GET /api/extensions/calendar/late HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
                assert entered.wait(10)
            try:
                assert state['errors'] == ["RuntimeError('failed while the server closed')"] and state['disconnects'] == []
                assert answer_of(client).split(b'\r\n', 1)[0].endswith(b' 500 Internal Server Error')
            finally:
                client.close()
    finally:
        sys.path.remove(TESTS)
