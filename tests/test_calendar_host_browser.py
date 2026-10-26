"""Calendar editing through the actual opaque module bridge and registered routes.

Opt in with OUROBOROS_THEME_CORE_ROOT. No installed skill or personal account is used.
"""
import asyncio
import json
import mimetypes
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = '''<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0}iframe{width:100%;height:720px;border:0}</style></head>
<body><section><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('light');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'calendar',ws_prefix:'ext:calendar:'},
 {entry:'widget.js',height:720,appearance:'host'},null,new Set());
window.ready=true;
</script></body></html>'''


@pytest.fixture
def calendar_server():
    configured = os.environ.get('OUROBOROS_THEME_CORE_ROOT')
    if not configured:
        pytest.skip('set OUROBOROS_THEME_CORE_ROOT for the real host bridge')
    core = Path(configured).resolve()
    assert (core / 'web/modules/widget_module.js').is_file()
    sys.path.insert(0, str(ROOT / 'skills/calendar/tests'))
    from test_calendar_provider_divergence import google_context, _day
    from test_calendar_round2 import RouteAPI, Request
    import routes
    import tools
    ctx, calendar_id, provider = google_context()
    api = RouteAPI()
    routes.register_routes(api, lambda: ctx)
    event = json.loads(tools.cal_create(ctx, title='Notify meeting', start=_day(0).isoformat(),
        calendars=[calendar_id], attendees=['guest@example.test'], confirm=True))['event']
    state = {'posts': [], 'errors': []}

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
            self.wfile.write(body)

        def dispatch(self):
            url = urlsplit(self.path)
            if url.path == '/':
                return self.send(HOST, 'text/html')
            if url.path.startswith('/static/'):
                web = core / 'web'
                source = (web / url.path[len('/static/'):]).resolve()
                if source.is_relative_to(web) and source.is_file():
                    return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or 'text/javascript')
            prefix = '/api/extensions/calendar/'
            if url.path == prefix + 'module/widget.js':
                return self.send((ROOT / 'skills/calendar/widget.js').read_bytes(), 'text/javascript')
            name = url.path.removeprefix(prefix)
            if name in api.routes:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                if self.command == 'POST':
                    state['posts'].append((name, body))
                try:
                    result = asyncio.run(api.routes[name](Request(body, {k:v[-1] for k,v in parse_qs(url.query).items()})))
                    return self.send(result.body, status=result.status_code)
                except Exception as exc:
                    state['errors'].append(repr(exc))
                    return self.send({'error':str(exc)}, status=500)
            return self.send({'error':'not found'}, status=404)
        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ctx, provider, event, state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(ctx.state_dir)
        sys.path.remove(str(ROOT / 'skills/calendar/tests'))
        assert not thread.is_alive()
        assert not state['errors'], state['errors']


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('scenario', ['save', 'delete'])
def test_calendar_host_card(calendar_server, tmp_path, engine, scenario):
    from playwright.sync_api import sync_playwright, expect
    ctx, provider, event, state, url = calendar_server
    evidence = Path(os.environ.get('OUROBOROS_CALENDAR_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        page = browser.new_page(viewport={'width':390 if engine == 'webkit' else 980, 'height':900})
        try:
            page.goto(url)
            page.wait_for_function('window.ready === true')
            assert 'allow-forms' not in page.locator('iframe').get_attribute('sandbox')
            frame = page.locator('iframe').element_handle().content_frame()
            frame.wait_for_selector('.grid')
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
        finally:
            browser.close()
