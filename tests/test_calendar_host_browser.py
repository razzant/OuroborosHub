"""Calendar through the actual opaque module bridge and the skill's registered routes.

Opt in with OUROBOROS_THEME_CORE_ROOT. No installed skill or personal account is used. The page is a small host: it
serves the core's own web modules, answers the widget lease controls the core asks for before it builds a frame and
passes the skill's real routes to the browser. Only the edges to Google and to a CalDAV server are replaced.
"""
import asyncio
import json
import mimetypes
import os
import shutil
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
    real = urllib.request.urlopen

    def guarded(request, *args, **kwargs):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        assert urlsplit(url).hostname in ('127.0.0.1', 'localhost'), f'outside network: {url}'
        return real(request, *args, **kwargs)
    monkeypatch.setattr(urllib.request, 'urlopen', guarded)


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
        def __init__(self, body, query, headers):
            super().__init__(body, query)
            self.headers = headers   # the real request's: the begin route reads the Origin the bridge sent
    return HostRequest


@contextmanager
def serve(core, routes_api):
    """The page, the lease controls and one skill's routes on a loopback port."""
    state = {'posts': [], 'reads': [], 'responses': [], 'errors': []}
    request = request_class()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, mime='application/json', status=200):
            if not isinstance(body, bytes):
                body = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

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
                try:
                    result = asyncio.run(routes_api.routes[name](request(body, {k: v[-1] for k, v in parse_qs(url.query).items()}, self.headers)))
                    state['responses'].append((name, result.status_code, result.body))
                    return self.send(result.body, status=result.status_code)
                except Exception as exc:
                    state['errors'].append(repr(exc))
                    return self.send({'error': str(exc)}, status=500)
            return self.send({'error': 'not found'}, status=404)
        do_GET = do_POST = do_HEAD = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
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
    api = RouteAPI()
    routes.register_routes(api, lambda: ctx)
    event = json.loads(tools.cal_create(ctx, title='Notify meeting', start=_day(0).isoformat(),
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


def widget_frame(browser, url, engine, height=720):
    """The calendar inside its real module frame; the widths follow the existing cases (WebKit is the phone)."""
    from playwright.sync_api import expect
    expect.set_options(timeout=20_000)
    context = browser.new_context(viewport={'width': 390 if engine == 'webkit' else 980, 'height': 900})
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
    frame.wait_for_selector('.grid')
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
                frame.locator('.ev', has_text='Host-created').click()
                expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
                frame.get_by_placeholder('Название', exact=True).fill('Host-edited')
                frame.get_by_placeholder('Название', exact=True).press('Enter')
                expect(frame.locator('.card')).to_have_count(0)
                expect(frame.locator('.ev', has_text='Host-edited')).to_have_count(1)
                assert [x for x in state['posts'] if x[0] == 'event/update'][-1][1]['title'] == 'Host-edited'
            else:
                frame.locator('.ev', has_text='Notify meeting').click()
                expect(frame.locator('.card .status')).not_to_have_text('Загружаю событие…')
                notify = frame.locator('label.inline', has_text='уведомить участников').locator('input')
                notify.check()
                frame.get_by_placeholder('Место или ссылка').fill('Unsaved location retained')
                frame.get_by_role('button', name='Удалить', exact=True).click()
                expect(notify).to_be_checked()
                expect(frame.get_by_placeholder('Место или ссылка')).to_have_value('Unsaved location retained')
                frame.get_by_role('button', name='Точно удалить?', exact=True).click()
                expect(frame.locator('.card')).to_have_count(0)
                assert state['posts'][-1][0] == 'event/delete'
                assert state['posts'][-1][1]['send_updates'] is True
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
            frame.get_by_role('button', name='Проверить').click()
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
                frame.locator('.ev').filter(has_text=title).filter(has_text='personal').click()
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
            frame.locator('.ev').filter(has_text='Planning').filter(has_text='work').click()
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
                expect(frame.locator('#root > .status', has_text='источник')).to_be_visible()
                assert ctx.store.get_event(group['source'])['title'] == 'Content applied'
                frame.get_by_role('button', name='Сегодня', exact=True).click()
                expect(frame.locator('#root > .status', has_text='источник')).to_be_visible()
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
