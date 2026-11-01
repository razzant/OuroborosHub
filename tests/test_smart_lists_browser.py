"""Opt-in real module bridge + registered Smart Lists tools/routes + private store.

Set OUROBOROS_THEME_CORE_ROOT to a core checkout; Playwright Chromium/WebKit
must be installed. No installed skill, account, owner data or model is used.
OUROBOROS_LISTS_EVIDENCE_DIR optionally retains inspected screenshots/downloads.
"""
from __future__ import annotations

import importlib.util
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = """<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0}iframe{width:100%;height:560px;border:0}</style></head>
<body><section><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('light');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'smart_lists',ws_prefix:'ext:smart_lists:'},
 {entry:'widget.js',height:560,appearance:'host'},null,new Set());
window.ready=true;
</script></body></html>"""


@pytest.fixture
def lists_server(tmp_path):
    configured = os.environ.get('OUROBOROS_THEME_CORE_ROOT')
    if not configured:
        pytest.skip('set OUROBOROS_THEME_CORE_ROOT to exercise the real module bridge')
    core = Path(configured).resolve()
    assert (core / 'web/modules/widget_module.js').is_file()
    spec = importlib.util.spec_from_file_location('lists_browser_support',
        ROOT / 'skills/smart_lists/tests/test_smart_lists.py')
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    api = support.FakePluginAPI(tmp_path / 'state')
    plugin = support.load_plugin()
    plugin.register(api)

    def tool(name, /, **args):
        result = api.call_tool(name, **args)
        assert result['ok'], result
        return result

    tool('store', action='init')
    tool('group', action='create', name='Home')
    tool('group', action='create', name='Groceries', parent='Home')
    tool('group', action='create', name='Work')
    item = tool('add', group='Home / Groceries', items=[{'text':'Oat milk', 'due':'tomorrow'}],
                request_id='browser-seed')['added'][0]['id']
    state = {'reject':False, 'checks':[], 'errors':[]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content_type='application/json', status=200):
            if not isinstance(body, bytes):
                body = (body if isinstance(body, str) else json.dumps(body)).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
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
            prefix = '/api/extensions/smart_lists/'
            if url.path == prefix + 'module/widget.js':
                return self.send((ROOT / 'skills/smart_lists/widget.js').read_bytes(), 'text/javascript')
            if url.path.startswith(prefix):
                name = url.path[len(prefix):]
                if name in api.routes:
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
                    if name == 'check':
                        state['checks'].append(body)
                        if state['reject']:
                            return self.send({'ok':False,'error':'Synthetic refusal; nothing changed'})
                    try:
                        return self.send(api.call_route(name, method='GET' if self.command == 'HEAD' else self.command, body=body,
                            query={k:v[-1] for k,v in parse_qs(url.query).items()}))
                    except Exception as exc:
                        state['errors'].append(repr(exc))
                        return self.send({'error':str(exc)}, status=500)
            return self.send({'error':'not found'}, status=404)

        do_GET = dispatch
        do_HEAD = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tool, state, item, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not state['errors'], state['errors']


@pytest.mark.parametrize('engine', ['chromium','webkit'])
@pytest.mark.parametrize('width', [980,390])
def test_lists_registered_tools_and_real_widget(lists_server, tmp_path, engine, width):
    from playwright.sync_api import sync_playwright, expect
    tool, state, item, url = lists_server
    evidence = Path(os.environ.get('OUROBOROS_LISTS_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True, downloads_path=str(tmp_path / 'downloads'))
        context = browser.new_context(viewport={'width':width,'height':720}, accept_downloads=True, timezone_id='UTC')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def mount():
            page.goto(url)
            page.wait_for_function('window.ready === true')
            frame = page.locator('iframe').element_handle().content_frame()
            frame.wait_for_selector('.panel')
            return frame

        try:
            frame = mount()
            box = frame.locator(f'input[data-entry-id="{item}"]')
            expect(box).not_to_be_checked()
            expect(frame.locator('.list')).to_contain_text('tomorrow')
            box.check()
            expect(frame.locator(f'input[data-entry-id="{item}"]')).to_have_attribute('aria-label', 'Вернуть в дела: Oat milk')
            assert tool('read', status='done')['entries'][0]['id'] == item
            frame = mount()  # Persistence through a new opaque frame.
            expect(frame.locator(f'input[data-entry-id="{item}"]')).to_be_checked()
            frame.locator(f'input[data-entry-id="{item}"]').uncheck()
            expect(frame.locator(f'input[data-entry-id="{item}"]')).to_have_attribute('aria-label', 'Отметить выполненным: Oat milk')
            assert tool('read')['entries'][0]['status'] == 'open'
            state['reject'] = True
            frame.locator(f'input[data-entry-id="{item}"]').check()
            expect(frame.locator('.error')).to_contain_text('Synthetic refusal')
            expect(frame.locator(f'input[data-entry-id="{item}"]')).not_to_be_checked()
            assert tool('read')['entries'][0]['status'] == 'open'
            state['reject'] = False
            tool('update', entry_id=item, text='Oat milk, unsweetened', request_id='edit')
            tool('move', entry_ids=[item], to_group='Work', request_id='move')
            frame = mount()
            frame.get_by_role('button', name='Home', exact=True).click()
            expect(frame.locator('.list input')).to_have_count(0)
            frame.get_by_role('button', name='Work', exact=True).click()
            expect(frame.locator('.list')).to_contain_text('Oat milk, unsweetened')
            tool('delete', entry_ids=[item], request_id='trash')
            frame = mount()
            expect(frame.locator('.list input')).to_have_count(0)
            tool('delete', entry_ids=[item], action='undo', request_id='undo')
            for batch, count in enumerate((100,5)):
                tool('add', group='Home / Groceries', request_id=f'page-{batch}',
                     items=[{'text':f'Item {batch}-{i}'} for i in range(count)])
            frame = mount()
            expect(frame.locator('.list input')).to_have_count(100)
            frame.get_by_role('button', name='Показать ещё', exact=False).click()
            expect(frame.locator('.list input')).to_have_count(106)
            frame.get_by_role('button', name='Увеличить текст списка').click()
            expect(frame.locator('output')).to_have_text('17 px')
            page.evaluate("ouroTheme.set('dark')")
            expect(frame.locator('html')).to_have_attribute('data-theme','dark')
            page.screenshot(path=str(evidence / f'lists-{engine}-{width}-dark.png'))
            page.evaluate("ouroTheme.set('light')")
            page.screenshot(path=str(evidence / f'lists-{engine}-{width}-light.png'))
            assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth')
            with page.expect_download() as caught:
                frame.get_by_role('button', name='Скачать резервную копию').click()
            downloaded = evidence / f'lists-{engine}-{width}-export.json'
            assert caught.value.suggested_filename == 'smart-lists-export.json'
            caught.value.save_as(str(downloaded))
            exported = json.loads(downloaded.read_text())
            assert len(exported['sha256']) == 64
            tool('delete', entry_ids=[item], request_id='post-export-delete')
            tool('store', action='restore', file=str(downloaded), replace=True)
            assert any(e['id'] == item for e in tool('read', group='Work', status='all')['entries'])
            awaitable = page.evaluate('async () => { await disposeWidget(); return true; }')
            assert awaitable
            expect(page.locator('iframe')).to_have_count(0)
            assert not errors, errors
            assert len(state['checks']) == 3
        finally:
            context.close()
            browser.close()
