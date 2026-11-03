"""Telegram connector widget through the actual opaque module bridge and registered routes.

Opt in with OUROBOROS_THEME_CORE_ROOT. Synthetic private state only: no session, no
companion, no Telegram or Host call. Screenshots go to OUROBOROS_TELEGRAM_EVIDENCE_DIR.
"""
import asyncio
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'telegram-user-session'
ACCOUNT = 8667525238
HOST = '''<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0;background:#0f0f11}.widgets-frame{width:100%;height:var(--widget-frame-height,320px);border:0;display:block}</style></head>
<body><section><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('dark');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'telegram-user-session',ws_prefix:'ext:telegram-user-session:'},
 {entry:'widget.js',appearance:'host'},null,new Set());
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
    def __init__(self, root):
        self.root, self.routes = root, {}

    def get_state_dir(self):
        return str(self.root)

    def register_route(self, name, handler, methods):
        self.routes[name] = handler

    def register_tool(self, *a, **kw):
        pass

    def register_companion_process(self, name):
        pass

    def register_settings_section(self, *a, **kw):
        pass

    def register_ui_tab(self, *a, **kw):
        pass


def synthetic_state(root):
    sys.path.insert(0, str(SKILL))
    from userbot.config import Config, save_config, _write_json
    from userbot.participant_state import Journal, save_policy
    save_config(root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
    save_policy(root, {'mode': 'participant', 'outgoing': True, 'blocked_peers': [-1003]})
    now = datetime.now(timezone.utc).isoformat()
    _write_json(root, 'companion-health.json', {'category': 'running', 'at': now})
    _write_json(root, 'place-state.json', {'state': 'active', 'label': 'Telegram', 'at': now})
    _write_json(root, 'account-profile.json', {'id': str(ACCOUNT), 'username': 'ouro_demo', 'first_name': 'Ouroboros', 'last_name': 'AI'})
    _write_json(root, 'host-capabilities.json', {'at': now, 'places_version': 1, 'presence_delivery_version': 1})
    _write_json(root, 'chats-refresh-status.json', {'status': 'done', 'at': now, 'count': 4, 'complete': True})
    with Journal(root) as journal:
        journal.remember_chat(ACCOUNT, -1001, 'Board games on Fridays', 'supergroup', 'dialogs', members=24)
        journal.remember_chat(ACCOUNT, 4242, 'Mira', 'private', 'incoming')
        journal.remember_chat(ACCOUNT, -1002, 'Open workshop: interfaces and agents with a deliberately long title for wrapping', 'forum', 'dialogs', members=184)
        journal.remember_chat(ACCOUNT, -1003, 'Old announcements channel', 'channel', 'dialogs')
        journal.enqueue(str(ACCOUNT), 'held-reply', 'send_text', {'peer': '-1001', 'text': 'held'})
        row = dict(journal.db.execute("SELECT * FROM jobs WHERE id='held-reply'").fetchone())
        journal.hold(row, 'destination_inactive')


def load_plugin(api):
    name = 'telegram_widget_browser_test'
    package = ModuleType(name); package.__path__ = [str(SKILL)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + '.plugin', SKILL / 'plugin.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.register(api)


@pytest.fixture
def widget_server():
    configured = os.environ.get('OUROBOROS_THEME_CORE_ROOT')
    if not configured:
        pytest.skip('set OUROBOROS_THEME_CORE_ROOT for the real host bridge')
    core = Path(configured).resolve()
    assert (core / 'web/modules/widget_module.js').is_file()
    root = Path(tempfile.mkdtemp(prefix='tg-widget-'))
    synthetic_state(root)
    api = API(root)
    load_plugin(api)
    state = {'posts': [], 'errors': [], 'faults': []}

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
            payload = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
            if url.path == '/':
                return self.send(HOST, 'text/html')
            if url.path == '/api/widgets/fault':
                state['faults'].append(payload.decode('utf-8', 'replace'))
                return self.send({'ok': True})
            if url.path.startswith('/static/'):
                web = core / 'web'
                source = (web / url.path[len('/static/'):]).resolve()
                if source.is_relative_to(web) and source.is_file():
                    return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or 'text/javascript')
            prefix = '/api/extensions/telegram-user-session/'
            if url.path == prefix + 'module/widget.js':
                return self.send((SKILL / 'widget.js').read_bytes(), 'text/javascript')
            name = url.path.removeprefix(prefix)
            if name in api.routes:
                if self.command == 'POST':
                    state['posts'].append((name, json.loads(payload or b'null')))
                try:
                    query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    result = asyncio.run(api.routes[name](Request(self.command, payload, query)))
                    return self.send(result.body, status=result.status_code)
                except Exception as exc:
                    state['errors'].append(repr(exc))
                    return self.send({'error': str(exc)}, status=500)
            return self.send({'error': 'not found'}, status=404)
        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(root)
        assert not thread.is_alive()
        assert not state['errors'], state['errors']


def settle(page, frame):
    """The host bridge grows the frame after a paint; measure only once it has.

    The frame is an opaque origin, so its height is read inside it and the
    iframe box from the host page.
    """
    for _ in range(60):
        content = frame.evaluate('document.documentElement.scrollHeight')
        box = page.locator('iframe').bounding_box()
        if abs(box['height'] - content) <= 3:
            break
        page.wait_for_timeout(50)
    assert box['height'] + 3 >= content, (box, content)
    assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth')


def read_policy(root):
    from userbot.participant_state import policy
    return policy(root)


def recorded_after(root, mine, other):
    """A companion's start record comes after every earlier one, even where file times are coarse."""
    written = (root / mine).stat().st_mtime_ns
    if (root / other).exists() and (root / other).stat().st_mtime_ns >= written:
        os.utime(root / other, ns=(written - 10**9, written - 10**9))


def crop(page, frame, path, *selectors):
    """A 1:1 crop of the viewer's window around the controls that changed, after the frame has grown.

    A read the scene started can still redraw the whole card while it is measured; the crop is then
    measured again on the new card.
    """
    from playwright.sync_api import Error
    for attempt in range(3):
        try:
            settle(page, frame)
            nodes = [frame.locator(selector).first for selector in selectors]
            nodes[-1].scroll_into_view_if_needed()
            boxes = [node.bounding_box() for node in nodes]
        except Error:
            boxes = None
        if boxes and None not in boxes:
            break
        assert attempt < 2, 'the card kept redrawing while it was measured'
        page.wait_for_timeout(250)
    view = page.viewport_size
    left, top = max(0, min(b['x'] for b in boxes) - 16), max(0, min(b['y'] for b in boxes) - 16)
    right = min(view['width'], max(b['x'] + b['width'] for b in boxes) + 16)
    bottom = min(view['height'], max(b['y'] + b['height'] for b in boxes) + 16)
    page.screenshot(path=str(path), clip={'x': left, 'y': top, 'width': right - left, 'height': bottom - top})


@pytest.mark.parametrize('engine,width', [('chromium', 1100), ('webkit', 390), ('webkit', 1100)])
def test_telegram_widget_paths(widget_server, tmp_path, engine, width):
    from playwright.sync_api import sync_playwright, expect
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 900})
        page.on('console', lambda m: messages.append((m.type, m.text)) if m.type == 'error' else None)
        page.on('pageerror', lambda e: messages.append(('pageerror', str(e))))
        try:
            page.goto(url)
            page.wait_for_function('window.ready === true')
            frame_node = page.locator('iframe')
            assert 'allow-forms' not in frame_node.get_attribute('sandbox')
            frame = frame_node.element_handle().content_frame()
            # A verified account opens on what it hears; names, never bare IDs, are the rows.
            expect(frame.get_by_role('button', name='2 · What I hear')).to_have_attribute('aria-pressed', 'true')
            expect(frame.locator('.check', has_text='Board games on Fridays')).to_have_count(1)
            excluded = frame.locator('.check', has_text='Old announcements channel').locator('input')
            expect(excluded).not_to_be_checked()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 3 of 4 listed chats.')
            # This Host has not confirmed history import; the note says so instead of offering it.
            before = frame.locator('.tray', has_text='What about messages from before?')
            expect(before).to_contain_text('this one has not confirmed that')
            expect(before).not_to_contain_text('On the History tab you can import')
            frame.locator('.check', has_text='Mira').locator('input').uncheck()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 2 of 4 listed chats.')
            frame.get_by_role('button', name='Save what I hear').click()
            expect(frame.locator('[data-result="scope"]')).to_contain_text('Participant controls saved')
            assert sorted(read_policy(root)['blocked_peers']) == [-1003, 4242]
            # The narrower option: only chosen chats are received.
            frame.get_by_role('button', name='Selected chats').click()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 0 of 4 listed chats.')
            frame.locator('.check', has_text='Board games on Fridays').locator('input').check()
            frame.get_by_role('button', name='Save what I hear').click()
            expect(frame.locator('[data-result="scope"]')).to_contain_text('Participant controls saved')
            saved = read_policy(root)
            assert (saved['scope'], saved['selected_peers']) == ('selected', [-1001])
            frame.get_by_role('button', name='Refresh chat list from Telegram').click()
            expect(frame.locator('.meta', has_text='Refresh requested')).to_have_count(1)
            assert (root / 'chats-refresh.json').is_file()
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-scope-dark.png'), full_page=True)

            # Two clearly different ways in; the bot path never asks for a token here.
            frame.get_by_role('button', name='1 · Connect').click()
            expect(frame.get_by_role('button', name='Dedicated account')).to_have_attribute('aria-pressed', 'true')
            expect(frame.locator('.profile')).to_contain_text('@ouro_demo')
            expect(frame.locator('#heartbeat')).to_have_text('Running')
            # Nothing reads the bio, so the AI disclosure is stated as a setting to keep, not as observed.
            appear = frame.locator('.capline', has_text='How I appear')
            expect(appear).to_contain_text('should say I am an AI')
            expect(appear).to_contain_text('does not check')
            expect(frame.locator('.secondary', has_text='An account created for Ouroboros')).to_contain_text('should say openly')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-connect-account-dark.png'), full_page=True)
            frame.get_by_role('button', name='Telegram bot').click()
            expect(frame.locator('.capline', has_text='What reaches me')).to_contain_text('cannot read earlier history')
            expect(frame.locator('input[data-draft="api_hash"]')).to_have_count(0)
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-connect-bot-dark.png'), full_page=True)
            frame.get_by_role('button', name='Dedicated account').click()
            frame.get_by_role('button', name='Request login code').click()
            expect(frame.locator('[data-result="code"]')).to_contain_text('delivery is not confirmed')
            assert (root / 'login-request.json').is_file()
            frame.get_by_role('button', name='Request login code').click()
            expect(frame.locator('[data-result="code"]')).to_contain_text('already pending')
            frame.locator('input[data-draft="api_id"]').fill('123')
            frame.get_by_role('button', name='Save account setup').click()
            expect(frame.locator('[data-result="setup"]')).to_contain_text('No Telegram login was performed')

            # Participation: the local switch and held work, with Places as the behaviour editor.
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#place-chip')).to_contain_text('Active')
            expect(frame.locator('#queue-line')).to_contain_text('1 held')
            frame.get_by_role('button', name='Discard 1 held action').click()
            expect(frame.locator('[data-result="held"]')).to_contain_text('1 held operation(s) discarded')
            frame.locator('[data-control="outgoing"]').uncheck()
            expect(frame.locator('.tray', has_text='ones already queued wait as held')).to_have_count(0)
            frame.get_by_role('button', name='Save participation').click()
            expect(frame.locator('[data-result="controls"]')).to_contain_text('Participant controls saved')
            assert read_policy(root)['outgoing'] is False
            expect(frame.locator('.tray', has_text='ones already queued wait as held')).to_have_count(1)
            note = frame.locator('textarea[data-draft="note"]')
            expect(note).to_contain_text('ask me in Main and wait')
            expect(note).to_contain_text('Telegram admin rights alone do not make you a moderator')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-participation-dark.png'), full_page=True)

            # History: an honest unavailable state, a saved plan, and the Bot API limit stated.
            frame.get_by_role('button', name='4 · History').click()
            expect(frame.locator('#import-availability')).to_contain_text('Not available: this Ouroboros version cannot record')
            expect(frame.locator('.capline', has_text='Telegram bot')).to_contain_text('cannot read earlier chat history')
            frame.get_by_role('button', name='Chosen chats').click()
            frame.locator('.check', has_text='Board games on Fridays').locator('input').check()
            frame.get_by_role('button', name='Save import plan').click()
            expect(frame.locator('[data-result="import"]')).to_contain_text('Nothing is read from Telegram')
            expect(frame.locator('.plan', has_text='Saved · cannot start with this Ouroboros')).to_have_count(1)
            expect(frame.locator('.plan').get_by_role('button', name='Start')).to_have_count(0)
            plan_posts = [body for name, body in state['posts'] if name == 'participant/import']
            assert plan_posts[-1]['chats'] == ['-1001'] and plan_posts[-1]['exclude'] == []
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-history-dark.png'), full_page=True)
            frame.get_by_role('button', name='Cancel').click()
            expect(frame.locator('.row', has_text='Cancelled')).to_have_count(1)

            for theme in ('light', 'dark'):
                page.evaluate(f"ouroTheme.set('{theme}')")
                expect(frame.locator('html')).to_have_attribute('data-theme', theme)
            page.evaluate("ouroTheme.set('light')")
            for scene in ('1 · Connect', '2 · What I hear', '3 · Participation', '4 · History'):
                frame.get_by_role('button', name=scene).click()
                settle(page, frame)
                page.screenshot(path=str(evidence / f"telegram-{engine}-{width}-{scene[4:].replace(' ', '-').lower()}-light.png"), full_page=True)
            page.evaluate('async () => { await disposeWidget(); }')
            expect(page.locator('iframe')).to_have_count(0)
        finally:
            browser.close()
    # The second code request is refused with 409 by design; browsers log that response, not a script error.
    refused = [m for m in messages if m == ('error', 'Failed to load resource: the server responded with a status of 409 (Conflict)')]
    assert len(refused) <= 1 and [m for m in messages if m not in refused] == [], messages
    assert not state['faults'], state['faults']
    # The widget only ever called its own skill routes, and never a Telegram or Host action.
    assert {name for name, _ in state['posts']} <= {'participant/policy', 'participant/chats', 'participant/held',
                                                     'participant/import', 'setup/config', 'setup/request_code'}


def open_widget(browser, url, width, messages, clock=False):
    page = browser.new_page(viewport={'width': width, 'height': 900})
    page.on('console', lambda m: messages.append((m.type, m.text)) if m.type == 'error' else None)
    page.on('pageerror', lambda e: messages.append(('pageerror', str(e))))
    if clock:
        page.clock.install()  # One clock for the host page and the opaque widget frame.
    page.goto(url)
    page.wait_for_function('window.ready === true')
    frame = page.locator('iframe').element_handle().content_frame()
    # The widget picks its first scene once its first reads are in, redrawing the whole card; a click
    # landing during that redraw is lost, so the tests act only after it.
    from playwright.sync_api import expect
    expect(frame.locator('nav button[aria-pressed="true"]')).to_have_count(1)
    return page, frame


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_outgoing_draft_is_not_reported_as_saved(widget_server, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            frame.get_by_role('button', name='3 · Participation').click()
            tray = frame.locator('section', has_text='Outgoing actions').locator('.tray')
            save = frame.get_by_role('button', name='Save participation')
            expect(tray).to_contain_text('On. Every action')
            # Unticking is a draft: the companion keeps the saved switch until Save.
            frame.locator('[data-control="outgoing"]').uncheck()
            expect(tray).to_contain_text('On. Every action')
            expect(tray).to_contain_text('Not saved yet. Saving turns outgoing actions off.')
            expect(save).to_be_enabled()
            assert read_policy(root)['outgoing'] is True
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-participation-unsaved-dark.png'), full_page=True)
            save.click()
            expect(frame.locator('[data-result="controls"]')).to_contain_text('Participant controls saved')
            assert read_policy(root)['outgoing'] is False
            expect(tray).to_contain_text('Off. I cannot start Telegram actions')
            expect(tray).not_to_contain_text('Not saved')
            expect(save).to_be_disabled()
            frame.locator('[data-control="outgoing"]').check()
            expect(tray).to_contain_text('Off. I cannot start Telegram actions')
            expect(tray).to_contain_text('Saving turns outgoing actions on.')
            assert read_policy(root)['outgoing'] is False
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert {name for name, _ in state['posts']} == {'participant/policy'}


def discard_held(root):
    from userbot.participant_state import Journal
    with Journal(root) as journal:
        return journal.discard_held(str(ACCOUNT))


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_status_poll_keeps_open_details_focus_and_draft(widget_server, tmp_path, engine):
    """A changed held count makes the 20-second status poll re-render the scene."""
    from playwright.sync_api import sync_playwright, expect
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages, clock=True)
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#queue-line')).to_contain_text('1 held')
            details = frame.locator('details', has_text='Identity, pacing and transport mode')
            details.locator('summary').click()
            expect(details).to_have_js_property('open', True)
            account = frame.locator('[data-control="expected_account_id"]')
            account.fill('86675')
            account.press('ArrowLeft'); account.press('ArrowLeft')
            assert discard_held(root) == 1  # Behind the widget, as the companion or another tab would.
            page.clock.run_for(21000)
            expect(frame.locator('#queue-line')).to_contain_text('0 held')
            expect(details).to_have_js_property('open', True)
            assert frame.evaluate('document.activeElement && document.activeElement.dataset.control') == 'expected_account_id'
            assert frame.evaluate('[document.activeElement.selectionStart, document.activeElement.selectionEnd]') == [3, 3]
            expect(account).to_have_value('86675')
            page.keyboard.type('2')
            expect(account).to_have_value('866275')  # Typed at the kept caret, not at an end.
            # A field that nothing reads is not offered as owner recognition.
            expect(frame.locator('[data-control="owner_id"]')).to_have_count(0)
            expect(frame.locator('#controls-unsaved')).to_be_visible()
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-participation-details-after-poll-dark.png'), full_page=True)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_completed_chat_refresh_appears_without_losing_unsaved_scope(widget_server, tmp_path, engine):
    """The companion finishes a requested dialog read later; the open widget shows it by itself."""
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    from userbot.participant_state import Journal
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages, clock=True)
            refresh = frame.get_by_role('button', name='Refresh chat list from Telegram')
            refresh.click()
            expect(frame.locator('.meta', has_text='Refresh requested')).to_have_count(1)
            expect(refresh).to_be_disabled()
            # An unsaved choice and a half-typed ID with the caret inside it.
            frame.locator('.check', has_text='Mira').locator('input').uncheck()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 2 of 4 listed chats.')
            added = frame.locator('[data-draft="addPeer"]')
            added.fill('-100777'); added.press('ArrowLeft'); added.press('ArrowLeft')
            # Exactly what the companion writes when its one dialog read completes.
            with Journal(root) as journal:
                journal.remember_chat(ACCOUNT, 4243, 'Newly fetched chat', 'private', 'dialogs')
            (root / 'chats-refresh.json').unlink()
            _write_json(root, 'chats-refresh-status.json', {'status': 'done', 'count': 5, 'complete': True,
                                                            'at': datetime.now(timezone.utc).isoformat()})
            page.clock.run_for(6000)
            expect(frame.locator('.check', has_text='Newly fetched chat')).to_have_count(1)
            expect(frame.locator('.meta', has_text='Read from Telegram')).to_have_count(1)
            expect(refresh).to_be_enabled()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 3 of 5 listed chats.')
            expect(frame.locator('.check', has_text='Mira').locator('input')).not_to_be_checked()
            expect(frame.get_by_role('button', name='Save what I hear')).to_be_enabled()
            assert frame.evaluate('document.activeElement && document.activeElement.dataset.draft') == 'addPeer'
            assert frame.evaluate('[document.activeElement.selectionStart, document.activeElement.selectionEnd]') == [5, 5]
            expect(added).to_have_value('-100777')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-scope-after-refresh-dark.png'), full_page=True)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == [('participant/chats', {'refresh': True})]
    assert read_policy(root)['blocked_peers'] == [-1003]  # nothing was saved behind the owner's back


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_fresh_legacy_install_offers_participant_as_its_first_step(widget_server, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import Config, save_config
    from userbot.participant_state import save_policy
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    save_policy(root, {'mode': 'legacy'})  # a fresh install's default, with nothing enrolled for it
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            step = frame.locator('.first-step')
            expect(step).to_contain_text('First step: choose Free participant')
            expect(step).to_contain_text('none are enrolled')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-first-step-dark.png'), full_page=True)
            frame.get_by_role('button', name='Choose Free participant').click()
            expect(frame.locator('[data-result="first-step"]')).to_contain_text('Participant controls saved')
            expect(step).to_contain_text('Free participant chosen')
            expect(step).to_contain_text('turned off and on')
            assert read_policy(root)['mode'] == 'participant'
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('select[data-control="mode"]')).to_have_value('participant')
            # An enrolled legacy install keeps its mode and is not asked to switch.
            save_config(root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT,
                                     binding_id='b' * 32, allowed_peer_ids=(-1001,)))
            save_policy(root, {'mode': 'legacy'})
            _, frame = open_widget(browser, url, 1100, messages)
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#queue-line')).to_contain_text('held')  # status has been read
            expect(frame.locator('.first-step')).to_have_count(0)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == [('participant/policy', {'mode': 'participant'})]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_saving_legacy_again_in_the_same_widget_offers_the_first_step_again(widget_server, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    from userbot.participant_state import save_policy
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    save_policy(root, {'mode': 'legacy'})  # a fresh install's default, with nothing enrolled for it
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            step = frame.locator('.first-step')
            frame.get_by_role('button', name='Choose Free participant').click()
            expect(step).to_contain_text('Free participant chosen')
            # The owner goes back to Legacy in this same mount, from Participation.
            frame.get_by_role('button', name='3 · Participation').click()
            frame.locator('details', has_text='Identity, pacing and transport mode').locator('summary').click()
            frame.locator('select[data-control="mode"]').select_option('legacy')
            frame.get_by_role('button', name='Save participation').click()
            expect(frame.locator('[data-result="controls"]')).to_contain_text('turned off and on')
            assert read_policy(root)['mode'] == 'legacy'
            expect(step).to_contain_text('First step: choose Free participant')
            expect(step).not_to_contain_text('Free participant chosen')
            expect(frame.locator('[data-result="first-step"]')).to_have_text('')  # no receipt of the undone choice
            expect(frame.get_by_role('button', name='Choose Free participant')).to_be_enabled()
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-first-step-after-legacy-saved-dark.png'), full_page=True)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert [(name, body['mode']) for name, body in state['posts']] == [('participant/policy', 'participant'),
                                                                        ('participant/policy', 'legacy')]


def test_held_incoming_and_saved_plans_are_described_without_promises(widget_server):
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    from userbot.participant_state import Journal
    from userbot.presence import build_event
    root, state, url = widget_server
    with Journal(root) as journal:
        journal.capture(build_event(ACCOUNT, 4242, 77, 4242, 'hello'))
        journal.inbox_update(journal.heads()[0]['seq'], 'held', [])
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'places_version': 1,
                                                 'presence_delivery_version': 1, 'presence_history_import_version': 1,
                                                 'presence_source_order_version': 1})
    messages = []
    with sync_playwright() as pw:
        browser = pw.webkit.launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#queue-line')).to_contain_text('1 incoming held.')
            expect(frame.locator('#queue-line')).not_to_contain_text('by the host')
            frame.get_by_role('button', name='4 · History').click()
            availability = frame.locator('#import-availability')
            expect(availability).to_contain_text('never starts by itself')
            expect(availability).to_contain_text('your explicit Start')
            expect(frame.locator('.capline', has_text='Where to read it')).to_contain_text('Ouroboros versions with a Conversations view')
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == []


class ImportWorker:
    """The companion's history import worker over synthetic Telegram history and the contract-v1 Host fake.

    The worker is the real HistoryImporter with its real journal; only Telegram and the
    Host are stand-ins. It takes the steps the companion would take, when the test says so,
    on its own thread: Playwright's sync API owns this one's event loop.
    """

    def __init__(self, root):
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pool.submit(self._setup, root).result()

    def _setup(self, root):
        sys.path.insert(0, str(SKILL / 'tests'))
        from history_fakes import WireHost, History, message, user
        from telethon import types as T
        from userbot.history_import import HistoryImporter
        from userbot.participant import Participant, Host
        from userbot.participant_state import Journal
        from userbot.presence import PresenceToken, build_event
        from userbot.telegram import TelegramAdapter
        now = datetime.now(timezone.utc)
        self.days = lambda n: now - timedelta(days=n)
        photo = T.Photo(id=78, access_hash=1, file_reference=b'r', date=None, sizes=[], dc_id=1)
        board = T.Chat(id=1001, title='Board games on Fridays', photo=T.ChatPhotoEmpty(), participants_count=24, date=None, version=1)
        histories = {
            -1001: [message(9, -1001, 'Planning from last season', self.days(40), sender=811),
                    message(10, -1001, 'Has anyone tried this with six players?', self.days(12), sender=811),
                    message(11, -1001, '', self.days(11), sender=812, media=T.MessageMediaPhoto(photo=photo)),
                    message(12, -1001, 'Six works, but setup takes longer.', self.days(10), sender=811)],
            4242: [message(29, 4242, 'Are we still on for Friday?', self.days(9)),
                   message(30, 4242, 'Can you bring the rules?', self.days(8)),
                   message(31, 4242, 'Sure, I will bring them.', self.days(8) + timedelta(minutes=2), out=True),
                   message(32, 4242, 'Here is the rules summary.', self.days(7), out=True)]}
        self.wire = WireHost(ACCOUNT)
        # The Host already holds message 10 with other text: that record conflicts and stays held.
        self.wire.seed_live(build_event(ACCOUNT, -1001, 10, 811, 'Has anyone tried this with five players?',
                                        message={'sent_at': self.days(12).isoformat()}))
        self.telegram = History({-1001: board, 4242: user(4242, 'Mira', 'mira')}, histories)
        self.journal = Journal(root)
        # Live intake handed message 29 to the Host, which admitted it; this connector delivered 32 (receipt recorded)
        # and 31 (receipt not confirmed).
        live = build_event(ACCOUNT, 4242, 29, 4242, 'Are we still on for Friday?', message={'sent_at': self.days(9).isoformat()})
        self.wire.seed_live(live)
        self.journal.capture(live)
        seq = self.journal.db.execute('SELECT seq FROM inbox WHERE id=?', (live['source_event_id'],)).fetchone()[0]
        self.journal.observe_inbox(seq, 200, {'ok': True, 'status': 'completed'})
        self.journal.inbox_update(seq, 'done', [])
        for key, mid in (('auto:31', 31), ('auto:32', 32)):
            self.journal.report(key, {'schema_version': 1, 'delivery_id': key, 'part_id': '0', 'state': 'delivered', 'provider': 'telegram',
                                      'account_id': str(ACCOUNT), 'conversation_id': '4242', 'thread_id': '', 'text': '', 'format': 'text',
                                      'message': {'message_id': str(mid)}, 'origin': {'kind': 'automatic'}})
        self.journal.report_done('auto:32')
        adapter = TelegramAdapter(root / 'unused.session', 123, 'a' * 32, client=self.telegram)
        self.engine = Participant(root, adapter, Host('http://127.0.0.1:9', PresenceToken('skill-token'), transport=self.wire.transport),
                                  self.journal, ACCOUNT)
        self.clock = [time.time()]  # the widget reads waits against the same wall clock
        self.importer = HistoryImporter(self.engine, clock=lambda: self.clock[0])

    def run(self, change):
        """``change(journal)`` on the worker's own thread, where the companion keeps its journal."""
        return self.pool.submit(change, self.journal).result()

    def _restart(self, root):
        from userbot.history_import import HistoryImporter, interrupt_running
        from userbot.participant import Participant, record_host_facts
        record_host_facts(root, self.engine.host.identity())  # what a companion records as it starts in participant mode
        recorded_after(root, 'host-capabilities.json', 'presence-capability.json')
        interrupt_running(self.journal)
        self.engine = Participant(root, self.engine.adapter, self.engine.host, self.journal, ACCOUNT)
        self.importer = HistoryImporter(self.engine, clock=lambda: self.clock[0])

    def restart(self, root):
        """The skill is turned off and on in Free participant mode: a new companion with its own importer."""
        self.pool.submit(self._restart, root).result()

    def _steps(self, count):
        for _ in range(count):
            self.clock[0] += 700  # past the pacing gaps and any backoff
            asyncio.run(self.importer.once())

    def step(self, count=1):
        self.pool.submit(self._steps, count).result()

    def _finish(self, limit):
        for _ in range(limit):
            if not self.journal.db.execute("SELECT 1 FROM imports WHERE state='running'").fetchone():
                return
            self._steps(1)
        raise AssertionError('import did not finish')

    def finish(self, limit=60):
        self.pool.submit(self._finish, limit).result()

    def confirm_receipt(self, key):
        """The Host records a delivery receipt this connector reported earlier."""
        self.pool.submit(self.journal.report_done, key).result()

    def close(self):
        self.pool.submit(self.journal.close).result()
        self.pool.shutdown()


@pytest.mark.parametrize('engine,width', [('chromium', 1100), ('webkit', 390), ('webkit', 1100)])
def test_import_runs_through_provider_and_host_fakes(widget_server, tmp_path, engine, width):
    """Start, a partial Host confirmation, Pause, Resume, a held own message and Done, each from the real journal.

    Message 31 was sent by this connector and its delivery receipt is not confirmed: the plan stays
    Partial with Resume, a Resume before the receipt is recorded changes nothing, and once the Host
    records it a Resume skips the message without a history write. The conflicting message 10 stays held.
    """
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'places_version': 1,
                                                 'presence_delivery_version': 1, 'presence_history_import_version': 1,
                                                 'presence_source_order_version': 1})
    worker = ImportWorker(root)
    messages = []
    shot = lambda page, frame, name: (settle(page, frame), page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-{name}.png'), full_page=True))
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, width, messages, clock=True)
            frame.get_by_role('button', name='4 · History').click()
            expect(frame.locator('#import-availability')).to_contain_text('can record imported history')
            before = frame.get_by_role('button', name='2 · What I hear')
            frame.get_by_role('button', name='Chosen chats').click()
            frame.locator('.check', has_text='Board games on Fridays').locator('input').check()
            frame.locator('.check', has_text='Mira').locator('input').check()
            frame.locator('input[data-draft="importFrom"]').fill(worker.days(20).astimezone().strftime('%Y-%m-%d'))
            frame.locator('input[data-draft="importTo"]').fill(worker.days(1).astimezone().strftime('%Y-%m-%d'))
            frame.get_by_role('button', name='Save import plan').click()
            expect(frame.locator('[data-result="import"]')).to_contain_text('until you press Start')
            plan = frame.locator('.plan')
            expect(plan).to_contain_text('Saved · not started')
            worker.step(3)
            assert worker.wire.calls == [] and worker.telegram.calls == []  # a saved plan never starts by itself
            plan.get_by_role('button', name='Start').click()
            expect(plan).to_contain_text('Importing · chat 1 of 2')
            shot(page, frame, 'history-import-started-dark')
            # The companion reads one page and the Host confirms only part of it.
            worker.wire.faults.append(('unwritable_at', 1, False))
            worker.step(2)
            page.clock.run_for(4100)
            expect(plan).to_contain_text('Importing · waiting: Ouroboros did not confirm some records; they go again unchanged')
            expect(plan.locator('.plan-counts')).to_have_text('1 recorded')
            shot(page, frame, 'history-import-waiting-dark')
            plan.get_by_role('button', name='Pause').click()
            expect(plan).to_contain_text('Paused · by you')
            calls = len(worker.wire.calls)
            worker.step(3)
            assert len(worker.wire.calls) == calls  # paused: nothing read, nothing sent
            plan.get_by_role('button', name='Resume').click()
            expect(plan).to_contain_text('Importing')
            # The poll repaints the row as records are confirmed; a keyboard user keeps their place.
            plan.get_by_role('button', name='Pause').focus()
            worker.step(1)
            page.clock.run_for(4100)
            expect(plan.locator('.plan-counts')).to_have_text('2 recorded · 1 not imported')
            assert frame.evaluate('document.activeElement.dataset.value') == 'pause:' + plan.get_attribute('data-plan')
            worker.finish()
            page.clock.run_for(4100)
            # Message 31's delivery receipt is unconfirmed: the plan is partial, never a green Done.
            expect(plan.locator('.plan-state')).to_have_text('Partial · 1 message held back')
            expect(plan.locator('.plan-counts')).to_have_text('3 recorded · 2 already in the conversation · 1 held back · 1 not imported')
            expect(plan.locator('.plan-hint')).to_contain_text('Resume checks each held message again')
            expect(plan.get_by_role('button', name='Resume')).to_be_visible()
            plan.locator('summary').click()
            expect(plan.locator('.plan-lines li', has_text='Board games on Fridays:')).to_contain_text(
                '3 read · 2 recorded · 1 not imported (already recorded with other content) · every message in these dates read')
            expect(plan.locator('.plan-lines li', has_text='message 31')).to_have_text('Mira, message 31: own message held back: its delivery receipt is not confirmed yet; Resume checks again')
            expect(plan.locator('.plan-lines li', has_text='message 10')).to_contain_text('already recorded with other content')
            shot(page, frame, 'history-import-partial-dark')
            posts = len(worker.wire.history_bodies)
            # Resume before the receipt is recorded: rechecked, still held, nothing written.
            plan.get_by_role('button', name='Resume').click()
            expect(plan.locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            expect(plan.locator('.plan-state')).to_have_text('Partial · 1 message held back')
            assert len(worker.wire.history_bodies) == posts
            # Repair: the Host records the receipt. Resume proves message 31 is in the room and writes nothing.
            worker.confirm_receipt('auto:31')
            plan.get_by_role('button', name='Resume').click()
            expect(plan.locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            # Finished, but the conflicting message 10 is not imported: never a green Done.
            expect(plan.locator('.plan-state')).to_have_text('Done · 1 not imported')
            expect(plan.locator('.plan-state')).to_have_class(re.compile(r'\btone-warn\b'))
            expect(plan.locator('.plan-counts')).to_have_text('3 recorded · 3 already in the conversation · 1 not imported')
            expect(plan.locator('button')).to_have_count(0)
            expect(plan.locator('.plan-lines li', has_text='message 31')).to_have_count(0)
            expect(plan.locator('.plan-lines li', has_text='message 10')).to_contain_text('already recorded with other content')
            assert len(worker.wire.history_bodies) == posts
            shot(page, frame, 'history-import-done-dark')
            page.evaluate("ouroTheme.set('light')")
            expect(frame.locator('html')).to_have_attribute('data-theme', 'light')
            shot(page, frame, 'history-import-done-light')
            before.click()
            expect(frame.locator('.tray', has_text='What about messages from before?')).to_contain_text('On the History tab you can import')
            page.evaluate('async () => { await disposeWidget(); }')
            expect(page.locator('iframe')).to_have_count(0)
        finally:
            browser.close()
            worker.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert {name for name, _ in state['posts']} == {'participant/import'}
    assert [body['action'] for name, body in state['posts']] == ['plan', 'start', 'pause', 'resume', 'resume', 'resume']
    # The Host only ever saw identity, place and history requests; never a turn or an injected chat line.
    assert {path for _, path in worker.wire.paths()} == {'/identity', '/presence/place', '/presence/history'}
    first, second, third = (json.loads(body) for body in worker.wire.history_bodies)
    assert second['records'] == first['records'][1:]  # only the unconfirmed suffix, unchanged
    sent = [[r['event']['message']['message_id'] for r in body['records']] for body in (first, second, third)]
    # 10 was unconfirmed once, then conflicted and was never sent again; 31 was never sent.
    assert sent == [['12', '11', '10'], ['11', '10'], ['30']]
    # 29 is live intake's admitted copy, never an import write.
    assert sorted(worker.wire.rows) == sorted(f'telegram:{ACCOUNT}:{peer}:0:{mid}' for peer, mid in
                                              ((-1001, 10), (-1001, 11), (-1001, 12), (4242, 29), (4242, 30)))
    assert worker.wire.rows[f'telegram:{ACCOUNT}:4242:0:29']['custody'] == 'live'


class OutcomeWorker(ImportWorker):
    """Three saved plans over the real importer and journal, for the owner to start in turn.

    One imports everything new. One meets a service message, a message the Host already
    holds with other text, and a chat excluded after the plan was saved. One reads three
    own messages whose delivery receipts are still being reported.
    """

    def _setup(self, root):
        sys.path.insert(0, str(SKILL / 'tests'))
        from history_fakes import WireHost, History, message, service, user
        from telethon import types as T
        from userbot import history_import as H
        from userbot.participant import Participant, Host
        from userbot.participant_state import Journal, policy, save_policy
        from userbot.presence import PresenceToken, build_event
        from userbot.telegram import TelegramAdapter
        now = datetime.now(timezone.utc)
        days = self.days = lambda n: now - timedelta(days=n)
        chat = lambda gid, title: T.Chat(id=gid, title=title, photo=T.ChatPhotoEmpty(), participants_count=24, date=None, version=1)
        histories = {
            -1001: [message(21, -1001, 'Who brings snacks?', days(6), sender=811),
                    message(22, -1001, 'I will.', days(6) + timedelta(minutes=1), sender=812),
                    message(23, -1001, 'Great, see you Friday.', days(6) + timedelta(minutes=2), sender=811)],
            -1002: [service(40, -1002, days(5)),
                    message(41, -1002, 'Welcome!', days(5) + timedelta(minutes=1), sender=811),
                    message(42, -1002, 'Slides are in the pinned post.', days(5) + timedelta(minutes=2), sender=812)],
            4242: [message(30, 4242, 'Can you bring the rules?', days(4)),
                   message(31, 4242, 'Sure, I will bring them.', days(4) + timedelta(minutes=2), out=True),
                   message(32, 4242, 'Here is the rules summary.', days(3), out=True),
                   message(33, 4242, 'See you Friday.', days(3) + timedelta(minutes=1), out=True)]}
        self.wire = WireHost(ACCOUNT)
        # The Host already holds 42 and 32 under other text, so each conflicts when it is sent.
        self.wire.seed_live(build_event(ACCOUNT, -1002, 42, 812, 'Slides are in the pinned message.',
                                        message={'sent_at': (days(5) + timedelta(minutes=2)).isoformat()}))
        self.wire.seed_live(build_event(ACCOUNT, 4242, 32, ACCOUNT, 'Here is a summary of the rules.', message={'sent_at': days(3).isoformat()}))
        self.telegram = History({-1001: chat(1001, 'Board games on Fridays'), -1002: chat(1002, 'Open workshop'),
                                 4242: user(4242, 'Mira', 'mira'), 4243: user(4243, 'Tom', 'tom')}, histories)
        self.journal = Journal(root)
        self.journal.remember_chat(ACCOUNT, 4243, 'Tom', 'private', 'dialogs')
        # Live intake handed message 23 to the Host, which admitted it.
        live = build_event(ACCOUNT, -1001, 23, 811, 'Great, see you Friday.', message={'sent_at': (days(6) + timedelta(minutes=2)).isoformat()})
        self.wire.seed_live(live)
        self.journal.capture(live)
        seq = self.journal.db.execute('SELECT seq FROM inbox WHERE id=?', (live['source_event_id'],)).fetchone()[0]
        self.journal.observe_inbox(seq, 200, {'ok': True, 'status': 'completed'})
        self.journal.inbox_update(seq, 'done', [])
        # This connector sent 31, 32 and 33; their delivery receipts are still being reported.
        for mid in (31, 32, 33):
            self.journal.report(f'auto:{mid}', {'schema_version': 1, 'delivery_id': f'auto:{mid}', 'part_id': '0', 'state': 'delivered',
                                                'provider': 'telegram', 'account_id': str(ACCOUNT), 'conversation_id': '4242', 'thread_id': '',
                                                'text': '', 'format': 'text', 'message': {'message_id': str(mid)}, 'origin': {'kind': 'automatic'}})
        spec = lambda chats: H.validate_plan({'chats': [str(c) for c in chats], 'exclude': [], 'from': days(10).isoformat(),
                                              'to': days(1).isoformat(), 'max_per_chat': 500}, policy(root), ACCOUNT)
        self.plans = {name: H.save_plan(self.journal, ACCOUNT, spec(chats))
                      for name, chats in (('full', [-1001]), ('mixed', [-1002, 4243]), ('released', [4242]))}
        save_policy(root, {'blocked_peers': [-1003, 4243]})  # Tom is excluded after the plan was saved
        adapter = TelegramAdapter(root / 'unused.session', 123, 'a' * 32, client=self.telegram)
        engine = Participant(root, adapter, Host('http://127.0.0.1:9', PresenceToken('skill-token'), transport=self.wire.transport),
                             self.journal, ACCOUNT)
        self.clock = [time.time()]
        self.importer = H.HistoryImporter(engine, clock=lambda: self.clock[0])

    def refuse_receipts(self):
        """The Host refuses the three receipts for good, and confirms only part of the next history request."""
        def refuse():
            for mid in (31, 32, 33):
                self.journal.report_defer(f'auto:{mid}', 'host_409', permanent=True)
            self.wire.faults.append(('unwritable_at', 2, False))
        self.pool.submit(refuse).result()


@pytest.mark.parametrize('engine,width', [('chromium', 1100), ('webkit', 390), ('webkit', 1100)])
def test_finished_imports_say_what_was_imported(widget_server, tmp_path, engine, width):
    """Done is green only when nothing was left out, and a message a Resume sent shows what became of it."""
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'places_version': 1,
                                                 'presence_delivery_version': 1, 'presence_history_import_version': 1,
                                                 'presence_source_order_version': 1})
    worker = OutcomeWorker(root)
    messages = []
    shot = lambda page, frame, name: (settle(page, frame), page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-{name}.png'), full_page=True))
    why = '(its delivery receipt had been refused)'
    workshop = 'Open workshop: interfaces and agents with a deliberately long title for wrapping'
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, width, messages, clock=True)
            frame.get_by_role('button', name='4 · History').click()
            full, mixed, released = (frame.locator(f'.plan[data-plan="{worker.plans[name]}"]') for name in ('full', 'mixed', 'released'))
            def run(plan):
                plan.get_by_role('button', name='Start').click()
                expect(plan.locator('.plan-state')).to_contain_text('Importing')
                worker.finish()
                page.clock.run_for(4100)

            # Everything new was recorded and the rest was already there: a green Done.
            run(full)
            expect(full.locator('.plan-state')).to_have_text('Done')
            expect(full.locator('.plan-state')).to_have_class(re.compile(r'\btone-good\b'))
            expect(full.locator('.plan-counts')).to_have_text('2 recorded · 1 already in the conversation')
            full.locator('summary').click()
            expect(full.locator('.plan-lines li')).to_have_text(
                ['Board games on Fridays: 3 read · 2 recorded · 1 already in the conversation · every message in these dates read'])

            # Reading finished, but messages and a chat were left out: amber, and the chat line says so.
            run(mixed)
            expect(mixed.locator('.plan-state')).to_have_text('Done · 2 not imported · 1 chat not read')
            expect(mixed.locator('.plan-state')).to_have_class(re.compile(r'\btone-warn\b'))
            expect(mixed.locator('.plan-counts')).to_have_text('1 recorded · 2 not imported')
            mixed.locator('summary').click()
            expect(mixed.locator('.plan-lines li')).to_have_text([
                f'{workshop}: 3 read · 1 recorded · 2 not imported (1 already recorded with other content, 1 service) · every message in these dates read',
                'Tom: 0 read · excluded from receiving',
                f'{workshop}, message 42: already recorded with other content'])
            shot(page, frame, 'history-import-omissions-dark')

            # Own messages wait for their receipts; the Host then refuses the receipts and Resume sends the saved copies.
            run(released)
            expect(released.locator('.plan-state')).to_have_text('Partial · 3 messages held back')
            worker.refuse_receipts()
            released.locator('summary').click()
            released.get_by_role('button', name='Resume').click()
            expect(released.locator('.plan-state')).to_contain_text('Importing')
            worker.step(1)
            page.clock.run_for(4100)
            expect(released.locator('.plan-state')).to_have_text('Importing · waiting: Ouroboros did not confirm some records; they go again unchanged')
            lines = released.locator('.plan-lines li')
            expect(lines.filter(has_text='message 33')).to_have_text(f'Mira, message 33: imported on your Resume {why}')
            expect(lines.filter(has_text='message 32')).to_have_text([
                'Mira, message 32: already recorded with other content',
                f'Mira, message 32: sent on your Resume but not imported: already recorded with other content {why}'])
            expect(lines.filter(has_text='message 31')).to_have_text(f'Mira, message 31: sent on your Resume, waiting for Ouroboros to confirm {why}')
            shot(page, frame, 'history-import-released-waiting-dark')
            released.get_by_role('button', name='Pause').click()
            expect(released.locator('.plan-state')).to_have_text('Paused · by you')
            expect(released.locator('.plan-counts')).to_have_text('2 recorded · 1 not imported · 1 not confirmed by Ouroboros')
            expect(lines.filter(has_text='message 31')).to_have_text(f'Mira, message 31: sent on your Resume, not confirmed by Ouroboros {why}')
            released.get_by_role('button', name='Resume').click()
            expect(released.locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            expect(released.locator('.plan-state')).to_have_text('Done · 1 not imported')
            expect(released.locator('.plan-state')).to_have_class(re.compile(r'\btone-warn\b'))
            expect(released.locator('.plan-counts')).to_have_text('3 recorded · 1 not imported')
            expect(lines.filter(has_text='Mira:')).to_have_text(
                'Mira: 4 read · 3 recorded · 1 not imported (already recorded with other content) · every message in these dates read')
            expect(lines.filter(has_text='message 31')).to_have_text(f'Mira, message 31: imported on your Resume {why}')
            expect(lines.filter(has_text='message 32').nth(1)).to_have_text(
                f'Mira, message 32: sent on your Resume but not imported: already recorded with other content {why}')
            shot(page, frame, 'history-import-outcomes-dark')
            page.evaluate("ouroTheme.set('light')")
            expect(frame.locator('html')).to_have_attribute('data-theme', 'light')
            shot(page, frame, 'history-import-outcomes-light')
        finally:
            browser.close()
            worker.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert [body['action'] for name, body in state['posts']] == ['start', 'start', 'start', 'resume', 'pause', 'resume']
    assert {path for _, path in worker.wire.paths()} == {'/identity', '/presence/place', '/presence/history'}
    sent = [[r['event']['message']['message_id'] for r in json.loads(body)['records']] for body in worker.wire.history_bodies]
    # 32 conflicted once and was never sent again; 31 went again unchanged after the partial answer.
    assert sent == [['22', '21'], ['42', '41'], ['30'], ['33', '32', '31'], ['31']]
    assert 4243 not in {call[1] for call in worker.telegram.calls}  # the excluded chat was never read


class ArrivingWorker(ImportWorker):
    """Mira's chat with an older message and message 30, which live intake still has in hand when the chat is read."""

    def _setup(self, root):
        sys.path.insert(0, str(SKILL / 'tests'))
        from history_fakes import WireHost, History, message, user
        from userbot import history_import as H
        from userbot.participant import Participant, Host
        from userbot.participant_state import Journal, policy
        from userbot.presence import PresenceToken, build_event
        from userbot.telegram import TelegramAdapter
        now = datetime.now(timezone.utc)
        days = self.days = lambda n: now - timedelta(days=n)
        self.message = message
        histories = {4242: [message(28, 4242, 'Last week we played Azul.', days(5)), message(30, 4242, 'Can you bring the rules?', days(2))]}
        self.wire = WireHost(ACCOUNT)
        self.telegram = History({4242: user(4242, 'Mira', 'mira')}, histories)
        self.journal = Journal(root)
        # Live intake captured message 30 and has not handed it to the Host yet.
        self.journal.capture(build_event(ACCOUNT, 4242, 30, 4242, 'Can you bring the rules?', message={'sent_at': days(2).isoformat()}))
        self.plan = H.save_plan(self.journal, ACCOUNT, H.validate_plan({'chats': ['4242'], 'exclude': [], 'from': days(10).isoformat(),
                                                                       'to': days(1).isoformat(), 'max_per_chat': 500}, policy(root), ACCOUNT))
        adapter = TelegramAdapter(root / 'unused.session', 123, 'a' * 32, client=self.telegram)
        self.engine = Participant(root, adapter, Host('http://127.0.0.1:9', PresenceToken('skill-token'), transport=self.wire.transport),
                                  self.journal, ACCOUNT)
        self.clock = [time.time()]
        self.importer = H.HistoryImporter(self.engine, clock=lambda: self.clock[0])

    def hold_live(self):
        """Live intake reaches message 30 and the Host refuses that live turn: held for good, never admitted.

        Telegram meanwhile holds an edited revision of it, which nothing reads again.
        """
        def hold():
            self.wire.turn_refusal = (409, {'ok': False, 'code': 'place_not_connected', 'disposition': 'blocked', 'error': 'Connect this place'})
            asyncio.run(self.engine.incoming_once())
            self.wire.turn_refusal = None
            self.telegram.histories[4242][1] = self.message(30, 4242, 'Can you bring the rules and the timer?', self.days(2),
                                                           edit_date=self.days(1))
            return self.journal.db.execute('SELECT state FROM inbox').fetchone()[0]
        return self.pool.submit(hold).result()


@pytest.mark.parametrize('engine,width', [('chromium', 1100), ('webkit', 390), ('webkit', 1100)])
def test_a_message_live_intake_still_has_is_held_back_until_resume(widget_server, tmp_path, engine, width):
    """Pending live custody is never shown as already in the conversation or as a finished import.

    The import holds message 30 back with the copy it read, Partial with Resume. A Resume while live
    intake still has it changes nothing. Once live intake has let it go without the Host admitting it,
    Resume sends exactly that saved copy, and the plan is Done.
    """
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'places_version': 1,
                                                 'presence_delivery_version': 1, 'presence_history_import_version': 1,
                                                 'presence_source_order_version': 1})
    worker = ArrivingWorker(root)
    messages = []
    shot = lambda page, frame, name: (settle(page, frame), page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-{name}.png'), full_page=True))
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, width, messages, clock=True)
            frame.get_by_role('button', name='4 · History').click()
            plan = frame.locator(f'.plan[data-plan="{worker.plan}"]')
            plan.get_by_role('button', name='Start').click()
            expect(plan.locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            expect(plan.locator('.plan-state')).to_have_text('Partial · 1 message held back')
            expect(plan.locator('.plan-state')).to_have_class(re.compile(r'\btone-warn\b'))
            expect(plan.locator('.plan-counts')).to_have_text('1 recorded · 1 held back')
            expect(plan).not_to_contain_text('already in the conversation')
            plan.locator('summary').click()
            expect(plan.locator('.plan-lines li')).to_have_text([
                'Mira: 2 read · 1 recorded · 1 held back · every message in these dates read',
                'Mira, message 30: held back: it was still arriving as a new message when this chat was read; Resume checks again'])
            shot(page, frame, 'history-import-arriving-held-dark')
            posts = len(worker.wire.history_bodies)
            # Resume while live intake still has it: checked again, still held, nothing written.
            plan.get_by_role('button', name='Resume').click()
            expect(plan.locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            expect(plan.locator('.plan-state')).to_have_text('Partial · 1 message held back')
            assert len(worker.wire.history_bodies) == posts
            assert worker.hold_live() == 'held'
            plan.get_by_role('button', name='Resume').click()
            expect(plan.locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            expect(plan.locator('.plan-state')).to_have_text('Done')
            expect(plan.locator('.plan-state')).to_have_class(re.compile(r'\btone-good\b'))
            expect(plan.locator('.plan-counts')).to_have_text('2 recorded')
            expect(plan.locator('.plan-lines li')).to_have_text([
                'Mira: 2 read · 2 recorded · every message in these dates read',
                'Mira, message 30: imported on your Resume (it never reached the conversation as a new message)'])
            shot(page, frame, 'history-import-arriving-done-dark')
            page.evaluate("ouroTheme.set('light')")
            expect(frame.locator('html')).to_have_attribute('data-theme', 'light')
            shot(page, frame, 'history-import-arriving-done-light')
        finally:
            browser.close()
            worker.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert [body['action'] for name, body in state['posts']] == ['start', 'resume', 'resume']
    assert {path for _, path in worker.wire.paths()} == {'/identity', '/presence/place', '/presence/history', '/presence/turn'}
    first, second = (json.loads(body) for body in worker.wire.history_bodies)
    assert [r['event']['message']['message_id'] for r in first['records']] == ['28']
    # The copy saved at the read went, not Telegram's later edit, which was never read.
    assert [(r['event']['message']['message_id'], r['event']['text']) for r in second['records']] == [('30', 'Can you bring the rules?')]
    assert len(worker.telegram.reads()) == 1


@pytest.mark.parametrize('zone,start,end', [
    ('America/Los_Angeles', '2026-03-07T08:00:00+00:00', '2026-03-09T07:00:00+00:00'),  # the night clocks move forward
    ('Asia/Kolkata', '2026-03-06T18:30:00+00:00', '2026-03-08T18:30:00+00:00'),
    ('Pacific/Kiritimati', '2026-03-06T10:00:00+00:00', '2026-03-08T10:00:00+00:00')])
def test_inclusive_dates_are_whole_local_days(widget_server, zone, start, end):
    """From 7 March to 8 March inclusive covers both whole days in the owner's own time zone."""
    from playwright.sync_api import sync_playwright, expect
    root, state, url = widget_server
    messages = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(timezone_id=zone, viewport={'width': 1100, 'height': 900})
            page = context.new_page()
            page.on('pageerror', lambda e: messages.append(('pageerror', str(e))))
            page.goto(url)
            page.wait_for_function('window.ready === true')
            frame = page.locator('iframe').element_handle().content_frame()
            frame.get_by_role('button', name='4 · History').click()
            frame.locator('input[data-draft="importFrom"]').fill('2026-03-07')
            frame.locator('input[data-draft="importTo"]').fill('2026-03-08')
            frame.get_by_role('button', name='Save import plan').click()
            expect(frame.locator('.plan')).to_have_count(1)
            listed = frame.locator('.plan .row > div').first.inner_text()
            assert frame.evaluate("new Date(2026, 2, 7).toLocaleDateString()") in listed
            assert frame.evaluate("new Date(2026, 2, 8).toLocaleDateString()") in listed
        finally:
            browser.close()
    assert messages == [], messages
    sys.path.insert(0, str(SKILL))
    from userbot.participant_state import Journal
    with Journal(root) as journal:
        spec = json.loads(journal.db.execute('SELECT spec FROM imports').fetchone()[0])
    assert (spec['from'], spec['to']) == (start, end)


def test_held_messages_beyond_the_listed_ones_are_counted_in_the_widget(widget_server):
    """A plan holding more own messages than are listed says how many it did not list."""
    from playwright.sync_api import sync_playwright, expect
    from userbot import history_import as H
    from userbot.participant_state import Journal
    from userbot.presence import build_event
    from userbot.config import _write_json
    root, state, url = widget_server
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'presence_history_import_version': 1,
                                                 'presence_source_order_version': 1})
    sent = datetime.now(timezone.utc) - timedelta(days=3)
    with Journal(root) as journal:
        spec = {'chats': [4242], 'exclude': [], 'from': (sent - timedelta(days=1)).isoformat(), 'to': sent.isoformat(), 'max_per_chat': 500}
        plan = H.save_plan(journal, ACCOUNT, spec)
        with journal.db:  # what a finished run leaves when 55 own messages wait for their receipts
            journal.db.execute("UPDATE imports SET state='partial', progress=? WHERE id=?", (json.dumps(
                {'run': 1, 'rechecked': 1, 'reason': 'held_back', 'chats': [{'peer': 4242, 'state': 'done', 'read': 55}]}), plan))
            for mid in range(1, 56):
                record = {'event': build_event(ACCOUNT, 4242, mid, ACCOUNT, f'reply {mid}', message={'sent_at': sent.isoformat()}),
                          'authorship': 'platform_account', 'source_timestamp': sent.isoformat(), 'staged_files': []}
                journal.db.execute("INSERT INTO import_records(plan,seq,peer,thread,source_event_id,record,state,result) VALUES(?,?,?,?,?,?,?,?)",
                                   (plan, mid, 4242, '', record['event']['source_event_id'], json.dumps(record), 'residual',
                                    json.dumps({'reason': 'receipt_unconfirmed', 'receipt': 'retrying'})))
    messages = []
    with sync_playwright() as pw:
        browser = pw.webkit.launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            frame.get_by_role('button', name='4 · History').click()
            row = frame.locator('.plan')
            expect(row.locator('.plan-state')).to_have_text('Partial · 55 messages held back')
            row.locator('summary').click()
            expect(row.locator('.plan-lines li', has_text='own message held back')).to_have_count(50)
            expect(row.locator('.plan-lines li', has_text='more not listed')).to_have_text('and 5 more not listed')
        finally:
            browser.close()
    assert messages == [], messages
    assert state['posts'] == []


def save_plans(worker, root, *plans):
    """Plans saved through the importer's own functions on the companion's journal: (chats, from, to, start?)."""
    from userbot import history_import as H
    from userbot.participant_state import policy
    def change(journal):
        saved = []
        for chats, start, end, started in plans:
            spec = H.validate_plan({'chats': chats, 'exclude': [], 'from': start.isoformat(), 'to': end.isoformat()}, policy(root), ACCOUNT)
            saved.append(H.save_plan(journal, ACCOUNT, spec))
            if started:
                H.transition(journal, ACCOUNT, saved[-1], 'start', policy(root))
        return saved
    return worker.run(change)


@pytest.mark.parametrize('engine,width', [('chromium', 1100), ('webkit', 390)])
def test_an_import_older_than_twenty_newer_plans_keeps_its_controls(widget_server, tmp_path, engine, width):
    """A running import that 20 newer saved plans pushed past the newest 20 stays listed with Pause and Cancel.

    The real importer goes on with it, the refusal of a second Start points at a plan the owner can see, and
    once Cancel finishes it, it leaves the list and is counted below it instead.
    """
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'presence_history_import_version': 1,
                                                 'presence_source_order_version': 1})
    worker = ImportWorker(root)
    old, *newer = save_plans(worker, root, (['-1001'], worker.days(20), worker.days(1), True),
                             *[(['4242'], worker.days(60), worker.days(50), False)] * 20)
    messages = []
    name = lambda what, theme: evidence / f'telegram-{engine}-{width}-history-{what}-{theme}.png'
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, width, messages, clock=True)
            frame.get_by_role('button', name='4 · History').click()
            rows = frame.locator('.plan')
            row = frame.locator(f'.plan[data-plan="{old}"]')
            expect(rows).to_have_count(21)
            expect(rows.last).to_have_attribute('data-plan', old)  # the oldest, after the 20 newer ones
            expect(row.locator('.plan-state')).to_have_text('Importing · chat 1 of 1')
            expect(row.get_by_role('button', name='Pause')).to_be_visible()
            expect(row.get_by_role('button', name='Cancel')).to_be_visible()
            expect(frame.locator('#older-plans')).to_have_count(0)
            # The companion goes on with it: a page is read and sent, and the poll shows the result.
            worker.step(2)
            page.clock.run_for(4100)
            expect(row.locator('.plan-counts')).to_have_text('2 recorded · 1 not imported')
            crop(page, frame, name('older-running', 'dark'), f'.plan[data-plan="{newer[0]}"]', f'.plan[data-plan="{old}"]')
            newest = frame.locator(f'.plan[data-plan="{newer[-1]}"]')
            newest.get_by_role('button', name='Start').click()
            expect(frame.locator('[data-result="plans"]')).to_have_text('another import is running; pause or cancel it first')
            row.get_by_role('button', name='Pause').click()
            expect(row.locator('.plan-state')).to_have_text('Paused · by you')
            calls = len(worker.wire.calls) + len(worker.telegram.calls)
            worker.step(2)
            assert len(worker.wire.calls) + len(worker.telegram.calls) == calls  # paused: nothing read, nothing sent
            row.get_by_role('button', name='Resume').click()
            expect(row.locator('.plan-state')).to_contain_text('Importing')
            row.get_by_role('button', name='Cancel').click()
            # Finished and older than the newest 20: it leaves the list, and the list says so.
            expect(rows).to_have_count(20)
            expect(row).to_have_count(0)
            expect(frame.locator('#older-plans')).to_have_text('1 older saved, done or cancelled plan is not listed.')
            crop(page, frame, name('older-counted', 'dark'), f'.plan[data-plan="{newer[0]}"]', '#older-plans')
            # Nothing runs now, so the newest plan starts, and the companion finishes it.
            newest.get_by_role('button', name='Start').click()
            expect(newest.locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            expect(newest.locator('.plan-state')).to_have_text('Done')
            page.evaluate("ouroTheme.set('light')")
            expect(frame.locator('html')).to_have_attribute('data-theme', 'light')
            crop(page, frame, name('older-counted', 'light'), f'.plan[data-plan="{newer[0]}"]', '#older-plans')
        finally:
            browser.close()
            worker.close()
    # The second Start is refused with 400 by design; browsers log that response, not a script error.
    refused = ('error', 'Failed to load resource: the server responded with a status of 400 (Bad Request)')
    assert [m for m in messages if m != refused] == [] and messages.count(refused) <= 1, messages
    assert not state['faults'], state['faults']
    assert [(path, body['action'], body['id']) for path, body in state['posts']] == [
        ('participant/import', 'start', newer[-1]), ('participant/import', 'pause', old), ('participant/import', 'resume', old),
        ('participant/import', 'cancel', old), ('participant/import', 'start', newer[-1])]


@pytest.mark.parametrize('engine,width', [('webkit', 1100), ('chromium', 390)])
def test_start_and_resume_wait_for_a_companion_in_free_participant_mode(widget_server, tmp_path, engine, width):
    """Legacy mode has no importer: saving, Pause and Cancel work, Start and Resume are not offered.

    Host support recorded by an earlier participant-mode companion does not change that, and choosing Free
    participant again is not enough until the skill is turned off and on; then the real importer resumes.
    """
    from playwright.sync_api import sync_playwright, expect
    from userbot import history_import as H
    from userbot.config import Config, save_config, _write_json
    from userbot.participant_state import policy, save_policy
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'presence_history_import_version': 1,
                                                 'presence_source_order_version': 1})
    worker = ImportWorker(root)
    paused, running, saved = save_plans(worker, root, (['-1001'], worker.days(20), worker.days(1), True),
                                        (['4242'], worker.days(60), worker.days(50), False), (['-1001'], worker.days(30), worker.days(25), False))
    worker.step(1)  # the participant companion reads and freezes the first page
    worker.run(lambda journal: (H.transition(journal, ACCOUNT, paused, 'pause', policy(root)),
                                H.transition(journal, ACCOUNT, running, 'start', policy(root))))
    # The owner returns to an enrolled legacy transport: the participant companion stops at its next check.
    save_config(root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT, binding_id='b' * 32, allowed_peer_ids=(-1001,)))
    save_policy(root, {'mode': 'legacy'})
    calls = len(worker.wire.calls) + len(worker.telegram.calls)
    worker.step(2)
    assert len(worker.wire.calls) + len(worker.telegram.calls) == calls
    # Its restart dispatches to legacy, which records its own Host check and has no importer.
    _write_json(root, 'presence-capability.json', {'delivery_reporting_version': 1, 'status': 'available'})
    recorded_after(root, 'presence-capability.json', 'host-capabilities.json')
    messages = []
    name = lambda what, theme: evidence / f'telegram-{engine}-{width}-history-{what}-{theme}.png'
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, width, messages, clock=True)
            expect(frame.locator('.tray', has_text='What about messages from before?')).to_contain_text(
                'needs Free participant mode, which starts after the skill is turned off and on')
            frame.get_by_role('button', name='4 · History').click()
            availability = frame.locator('#import-availability')
            expect(availability).to_contain_text('Not available in Legacy scoped transport mode, which has no history import.')
            expect(availability).to_contain_text('Choose Free participant under Participation; the mode starts after the skill is turned off and on.')
            rows = {plan: frame.locator(f'.plan[data-plan="{plan}"]') for plan in (paused, running, saved)}
            expect(rows[running].locator('.plan-state')).to_have_text('Started · nothing is read in Legacy scoped transport mode')
            expect(rows[paused].locator('.plan-state')).to_have_text('Paused · by you')
            expect(rows[saved].locator('.plan-state')).to_have_text('Saved · needs Free participant mode')
            for label in ('Start', 'Resume'):
                expect(frame.get_by_role('button', name=label, exact=True)).to_have_count(0)
            expect(rows[running].get_by_role('button', name='Pause')).to_be_visible()
            crop(page, frame, name('legacy-tray', 'dark'), '#import-availability')
            crop(page, frame, name('legacy-plans', 'dark'), f'.plan[data-plan="{saved}"]', f'.plan[data-plan="{paused}"]')
            frame.get_by_role('button', name='Save import plan').click()
            expect(frame.locator('[data-result="import"]')).to_have_text(
                'Import plan saved. Nothing is read from Telegram for it until the companion runs in Free participant mode.')
            rows[running].get_by_role('button', name='Pause').click()
            expect(rows[running].locator('.plan-state')).to_have_text('Paused · by you')
            rows[saved].get_by_role('button', name='Cancel').click()
            expect(rows[saved].locator('.plan-state')).to_have_text('Cancelled')
            # Free participant chosen again: the companion that runs is still the legacy one.
            frame.get_by_role('button', name='3 · Participation').click()
            frame.locator('details', has_text='Identity, pacing and transport mode').locator('summary').click()
            frame.locator('select[data-control="mode"]').select_option('participant')
            frame.get_by_role('button', name='Save participation').click()
            expect(frame.locator('[data-result="controls"]')).to_contain_text('starts after the skill is turned off and on')
            frame.get_by_role('button', name='4 · History').click()
            expect(availability).to_contain_text('Not available yet: Free participant mode is saved but has not started.')
            for label in ('Start', 'Resume'):
                expect(frame.get_by_role('button', name=label, exact=True)).to_have_count(0)
            page.evaluate("ouroTheme.set('light')")
            expect(frame.locator('html')).to_have_attribute('data-theme', 'light')
            crop(page, frame, name('restart-required-tray', 'light'), '#import-availability')
            crop(page, frame, name('restart-required-plans', 'light'), '.plan', f'.plan[data-plan="{paused}"]')
            # The skill is turned off and on: a companion starts in Free participant mode, and Resume runs.
            worker.restart(root)
            frame.get_by_role('button', name='2 · What I hear').click()
            frame.get_by_role('button', name='4 · History').click()
            expect(availability).to_contain_text('This Ouroboros can record imported history.')
            rows[paused].get_by_role('button', name='Resume').click()
            expect(rows[paused].locator('.plan-state')).to_contain_text('Importing')
            worker.finish()
            page.clock.run_for(4100)
            expect(rows[paused].locator('.plan-state')).to_have_text('Done · 1 not imported')
            crop(page, frame, name('after-restart-plans', 'light'), '.plan', f'.plan[data-plan="{paused}"]')
        finally:
            browser.close()
            worker.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert [(path, body.get('action', body.get('mode'))) for path, body in state['posts']] == [
        ('participant/import', 'plan'), ('participant/import', 'pause'), ('participant/import', 'cancel'),
        ('participant/policy', 'participant'), ('participant/import', 'resume')]
    assert read_policy(root)['mode'] == 'participant'


def test_a_host_that_cannot_keep_same_second_order_offers_no_start(widget_server, tmp_path):
    """A Host that records history but predates source order: the reason is said, and nothing can start."""
    from playwright.sync_api import sync_playwright, expect
    from userbot import history_import as H
    from userbot.config import _write_json
    from userbot.participant_state import Journal, policy
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'presence_history_import_version': 1})
    now = datetime.now(timezone.utc)
    with Journal(root) as journal:
        spec = H.validate_plan({'chats': ['-1001'], 'exclude': [], 'from': (now - timedelta(days=20)).isoformat(), 'to': now.isoformat()},
                               policy(root), ACCOUNT)
        saved, stopped = H.save_plan(journal, ACCOUNT, spec), H.save_plan(journal, ACCOUNT, spec)
        with journal.db:  # what the companion's Host check leaves when /identity lacks the order
            journal.db.execute("UPDATE imports SET state='paused', progress=? WHERE id=?", (json.dumps(
                {'run': 1, 'reason': 'host_source_order_missing', 'detail': {},
                 'chats': [{'peer': -1001, 'state': 'pending', 'offset_id': 0, 'read': 0, 'skipped': {}}]}), stopped))
    messages = []
    with sync_playwright() as pw:
        browser = pw.webkit.launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            expect(frame.locator('.tray', has_text='What about messages from before?')).to_contain_text(
                'needs an Ouroboros version that keeps messages sent within the same second in their order')
            frame.get_by_role('button', name='4 · History').click()
            expect(frame.locator('#import-availability')).to_contain_text(
                'it records imported history but cannot keep messages sent within the same second in their order')
            expect(frame.locator(f'.plan[data-plan="{saved}"] .plan-state')).to_have_text('Saved · cannot start with this Ouroboros')
            expect(frame.locator(f'.plan[data-plan="{stopped}"] .plan-state')).to_have_text(
                'Paused · this Ouroboros cannot keep messages sent within the same second in their order; update it, then resume')
            for label in ('Start', 'Resume'):
                expect(frame.get_by_role('button', name=label, exact=True)).to_have_count(0)
            for theme in ('dark', 'light'):
                page.evaluate(f"ouroTheme.set('{theme}')")
                expect(frame.locator('html')).to_have_attribute('data-theme', theme)
                crop(page, frame, evidence / f'telegram-webkit-1100-history-no-source-order-tray-{theme}.png', '#import-availability')
                crop(page, frame, evidence / f'telegram-webkit-1100-history-no-source-order-plans-{theme}.png', '.plan', f'.plan[data-plan="{saved}"]')
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == []
