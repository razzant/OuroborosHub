"""Slack Bridge card through the core's real module bridge and the skill's own routes.

Opt in with OUROBOROS_THEME_CORE_ROOT (a core checkout with web/modules/widget_module.js). Synthetic private state
only: no companion, no runtime loop, no Slack or Host call. Needs only skills/slack-bridge.
"""
import asyncio
import importlib.util
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = '''<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0;background:#0f0f11}.widgets-frame{width:100%;height:var(--widget-frame-height,320px);border:0;display:block}</style></head>
<body><section style="width:__WIDTH__px"><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('__THEME__');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'__SKILL__',ws_prefix:'ext:__SKILL__:'},{entry:'widget.js',appearance:'host'},null,new Set());
window.ready=true;
</script></body></html>'''


class Request:
    def __init__(self, method, body, query):
        self.method, self._body, self.query_params = method, body, query

    async def json(self):
        return json.loads(self._body or b'null')

    async def body(self):
        return self._body


class API:
    """Registers routes; every other registration is accepted and ignored."""

    def __init__(self, root, settings):
        self.root, self.routes, self._settings, self.unloads = root, {}, settings, []

    def get_state_dir(self):
        return str(self.root)

    def get_settings(self, keys):
        return {key: self._settings.get(key, '') for key in keys}

    def get_skill_token(self):
        class Token:
            @staticmethod
            def use_in_request():
                return 'host-token'
        return Token()

    def register_route(self, name, handler, methods=('GET',)):
        self.routes[name] = handler

    def on_unload(self, callback):
        self.unloads.append(callback)

    def log(self, *_args, **_kwargs):
        pass

    def __getattr__(self, name):
        if name.startswith('register_'):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


def load_plugin(skill, api):
    name = f"slack_bridge_widget_browser_{skill.replace('-', '_')}"
    package = ModuleType(name)
    package.__path__ = [str(ROOT / 'skills' / skill)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + '.plugin', ROOT / 'skills' / skill / 'plugin.py',
                                                  submodule_search_locations=[str(ROOT / 'skills' / skill)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.register(api)
    return module


def seed(skill, root):
    """A connected bridge with a live companion PID (this test process)."""
    sys.path.insert(0, str(ROOT / 'skills' / 'slack-bridge'))
    from lib.store import BridgeStore
    store = BridgeStore(root)
    store.set_runtime(socket_state='connected', host_adapter_state='active', workspace_name='Northstar Labs',
                      companion_pid=os.getpid(), connected_at=1795381200.0)
    store.close()
    return {'SLACK_BOT_TOKEN': 'xoxb-synthetic', 'SLACK_APP_TOKEN': 'xapp-synthetic'}


@pytest.fixture(params=['slack-bridge'])
def served(request):
    # The hosted suite names its Presence core; a theme core checkout works the same for the module bridge.
    configured = os.environ.get('OUROBOROS_THEME_CORE_ROOT') or os.environ.get('OUROBOROS_PRESENCE_CORE_ROOT')
    if not configured:
        pytest.skip('set OUROBOROS_PRESENCE_CORE_ROOT or OUROBOROS_THEME_CORE_ROOT for the real host bridge')
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        if os.environ.get('TRANSPORT_WIDGET_REQUIRE_BROWSERS') == '1':
            raise
        pytest.skip('Playwright is not installed')
    core = Path(configured).resolve()
    assert (core / 'web/modules/widget_module.js').is_file()
    skill = request.param
    root = Path(tempfile.mkdtemp(prefix='slack-bridge-widget-'))
    api = API(root, seed(skill, root))
    load_plugin(skill, api)
    state = {'posts': [], 'errors': [], 'faults': [], 'leases': 0, 'skill': skill, 'root': root, 'api': api}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, mime='application/json', status=200):
            if not isinstance(body, bytes):
                body = (body if isinstance(body, str) else json.dumps(body)).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the frame aborted its own read (refresh or disposal)

        def dispatch(self):
            url = urlsplit(self.path)
            payload = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
            if url.path == '/':
                query = parse_qs(url.query)
                theme, width = query.get('theme', ['dark'])[-1], query.get('width', ['335'])[-1]
                page = HOST.replace('__SKILL__', skill).replace('__THEME__', theme).replace('__WIDTH__', str(int(width)))
                return self.send(page, 'text/html')
            if url.path == '/api/widgets/fault':
                state['faults'].append(payload.decode('utf-8', 'replace'))
                return self.send({'ok': True})
            # A classic module mount asks the host for a route lease before it builds the frame
            # (core web/modules/widget_assets.js); the fixture answers as the host would, nothing more.
            if url.path == '/api/widgets/asset-leases':
                body = json.loads(payload or b'{}')
                assert body.get('skill') == skill and body.get('scope') == 'routes', body
                state['leases'] += 1
                return self.send({'token': 'card-fixture', 'base_url': '/api/widget-resources/card-fixture/',
                                  'renew_after': 3600, 'expires_in': 7200})
            if url.path == '/api/widgets/asset-leases/revoke':
                return self.send({'ok': True})
            if url.path.startswith('/api/widget-resources/card-fixture/'):
                url = url._replace(path=f'/api/extensions/{skill}/' + url.path.removeprefix('/api/widget-resources/card-fixture/'))
            if url.path.startswith('/static/'):
                web = core / 'web'
                source = (web / url.path[len('/static/'):]).resolve()
                if source.is_relative_to(web) and source.is_file():
                    return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or 'text/javascript')
            prefix = f'/api/extensions/{skill}/'
            if url.path == prefix + 'module/widget.js':
                return self.send((ROOT / 'skills' / skill / 'widget.js').read_bytes(), 'text/javascript')
            name = url.path.removeprefix(prefix)
            if name in api.routes:
                if self.command == 'POST':
                    state['posts'].append((name, json.loads(payload or b'null')))
                try:
                    query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    result = asyncio.run(api.routes[name](Request(self.command, payload, query)))
                except Exception as exc:  # pragma: no cover - reported by the assertion below
                    state['errors'].append(repr(exc))
                    return self.send({'error': str(exc)}, status=500)
                return self.send(result.body, status=result.status_code)
            return self.send({'error': 'not found'}, status=404)
        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for unload in api.unloads:
            unload()
        shutil.rmtree(root, ignore_errors=True)
        assert not state['errors'], state['errors']


def route_status(state, url):
    """The route's own answer, read over HTTP like the frame reads it (Playwright's loop is running here)."""
    with urllib.request.urlopen(f"{url}/api/extensions/{state['skill']}/status", timeout=10) as response:
        return json.loads(response.read())


@pytest.mark.parametrize('width', [335, 366])  # the owner's two-column card, and a 390 px phone's card
@pytest.mark.parametrize('engine,theme', [('webkit', 'dark'), ('webkit', 'light'), ('chromium', 'dark'), ('chromium', 'light')])
def test_card_first_line_is_the_route_line_and_nothing_overflows(served, engine, theme, width):
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1083 if width == 335 else 390, 'height': 1026}, device_scale_factor=2)
            page.goto(url + f'/?theme={theme}&width={width}')
            page.wait_for_function('window.ready === true')
            frame = page.frame_locator('iframe')
            status = frame.locator('.tc-status .tc-st-text')
            expect(status).to_have_text(route_status(state, url)['text'], timeout=15000)
            assert 'Connected' in status.inner_text()
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated')
            inner = next(f for f in page.frames if f is not page.main_frame)
            assert inner.evaluate("document.documentElement.dataset.theme") == theme
            assert inner.evaluate('document.documentElement.scrollWidth <= innerWidth')
            frame.locator('details.tc-det > summary').click()
            expect(frame.locator('.tc-det .tc-group h4').first).to_be_visible()
            assert inner.evaluate('document.documentElement.scrollWidth <= innerWidth')
            frame.locator('.tc-fresh button').click()
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated')
            # Disposal goes through the host's handshake and leaves no fault behind.
            page.evaluate('async () => { await disposeWidget(); }')
            expect(page.locator('iframe')).to_have_count(0)
            assert state['leases'] >= 1
            assert not state['faults'], state['faults']
        finally:
            browser.close()
