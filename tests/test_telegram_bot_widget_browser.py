"""Telegram Transport card through the core's real module bridge and the skill's own routes.

Opt in with OUROBOROS_THEME_CORE_ROOT (a core checkout with web/modules/widget_module.js). Synthetic private state
only: no companion, no runtime loop, no Telegram or Host call. Needs only skills/telegram-bot.
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

from widget_lease_gateway import LeaseGateway, controls

ROOT = Path(__file__).resolve().parents[1]
# The card's live publication as a host lists it (HOST mounts the same tab id and generation). Its route lease is
# answered as the core's controls answer it (tests/widget_lease_gateway.py), while the real module builds the frame,
# relays fetches and revokes the lease on disposal.
CARDS = {'telegram-bot': ('transport', 'fixture-generation')}
HOST = '''<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0;background:#0f0f11}.widgets-frame{width:100%;height:var(--widget-frame-height,320px);border:0;display:block}</style></head>
<body><section style="width:__WIDTH__px"><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('__THEME__');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'__SKILL__',ws_prefix:'ext:__SKILL__:',tab_id:'transport',generation:'fixture-generation'},
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
    name = f"telegram_bot_widget_browser_{skill.replace('-', '_')}"
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
    """One synthetic refused send, so the first line carries an attention item."""
    sys.path.insert(0, str(ROOT / 'skills' / 'telegram-bot'))
    from telegram_bot.custody import CustodyStore
    store = CustodyStore.shared(root / 'custody.sqlite3')
    store.enqueue_outbox('refused', {'kind': 'message', 'chat_id': '1000000002'})
    store.mark_outbox_failed(store.claim_outbox().delivery_id, reason='TelegramApiError: Telegram sendMessage failed (403): Forbidden')
    return {'TELEGRAM_PUBLIC_BOT_TOKEN': ''}


@pytest.fixture(params=['telegram-bot'])
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
    root = Path(tempfile.mkdtemp(prefix='telegram-bot-widget-'))
    api = API(root, seed(skill, root))
    load_plugin(skill, api)
    leases = LeaseGateway(CARDS)
    state = {'posts': [], 'errors': [], 'faults': [], 'leases': leases, 'skill': skill, 'root': root, 'api': api}

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
            if controls(url.path):
                # A refused one is recorded by the gateway and fails the teardown below.
                status, answer = leases.answer(self.command, url.path, self.headers, payload)
                return self.send(answer, status=status)
            if url.path == '/api/widgets/fault':
                state['faults'].append(payload.decode('utf-8', 'replace'))
                return self.send({'ok': True})
            if url.path.startswith('/static/'):
                web = core / 'web'
                source = (web / url.path[len('/static/'):]).resolve()
                if source.is_relative_to(web) and source.is_file():
                    return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or 'text/javascript')
            prefix = f'/api/extensions/{skill}/'
            if url.path == prefix + 'module/widget.js':
                return self.send((ROOT / 'skills' / skill / 'widget.js').read_bytes(), 'text/javascript')
            name = url.path.removeprefix(prefix)
            if name == 'status' and 'status_body' in state:
                return self.send(state['status_body'])  # a synthetic route answer (states a fixture cannot reach)
            if name == 'status' and state.get('status_fault'):
                return self.send({'ok': False, 'error': 'Telegram transport state is unavailable'},
                                 status=state['status_fault'])
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
        assert not leases.refused, leases.refused


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
            assert 'not delivered' in status.inner_text()
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
            # The gateway minted the route lease for the published tab, and the disposal revoked it.
            assert state['leases'].ended and not state['leases'].held
            assert not state['faults'], state['faults']
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['webkit', 'chromium'])
def test_telegram_group_form_validates_saves_and_says_so(served, engine):
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.goto(url + '/?theme=dark')
            page.wait_for_function('window.ready === true')
            frame = page.frame_locator('iframe')
            frame.locator('details.tc-setting > summary').click()
            field = frame.locator('#tc-mgid')
            field.fill('my group')
            frame.locator('.tc-setting .tc-btn').click()
            expect(frame.locator('.tc-result')).to_contain_text('Not saved')
            assert state['posts'] == []
            field.fill('-1000000000001')
            frame.locator('.tc-setting .tc-btn').click()
            expect(frame.locator('.tc-result')).to_have_text('Saved.', timeout=10000)
            assert state['posts'] == [('settings/save', {'management_group_id': '-1000000000001'})]
            saved = json.loads((state['root'] / 'settings.json').read_text(encoding='utf-8'))
            assert saved['management_group_id'] == '-1000000000001'
            expect(frame.locator('details.tc-setting > summary')).to_contain_text('-1000000000001', timeout=15000)
            # Clearing the field removes the group.
            field.fill('')
            frame.locator('.tc-setting .tc-btn').click()
            expect(frame.locator('.tc-result')).to_have_text('Saved: no management group.', timeout=10000)
            assert state['posts'][-1] == ('settings/save', {'management_group_id': ''})
            saved = json.loads((state['root'] / 'settings.json').read_text(encoding='utf-8'))
            assert not saved.get('management_group_id')
        finally:
            browser.close()


def refuse_another_send(root, chat_id):
    sys.path.insert(0, str(ROOT / 'skills' / 'telegram-bot'))
    from telegram_bot.custody import CustodyStore
    store = CustodyStore.shared(root / 'custody.sqlite3')
    store.enqueue_outbox(f'refused-{chat_id}', {'kind': 'message', 'chat_id': chat_id})
    store.mark_outbox_failed(store.claim_outbox().delivery_id, reason='TelegramApiError: Telegram sendMessage failed (403): Forbidden')


@pytest.mark.parametrize('engine', ['webkit', 'chromium'])
def test_status_repaints_while_the_group_field_is_being_edited(served, engine):
    """The first line and attention change under the reader's typing; their text, selection and fold stay."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1083, 'height': 1026})
            page.goto(url + '/?theme=dark&width=335')
            page.wait_for_function('window.ready === true')
            frame = page.frame_locator('iframe')
            status = frame.locator('.tc-status .tc-st-text')
            expect(status).to_contain_text('1 outgoing item not delivered', timeout=15000)
            frame.locator('details.tc-setting > summary').click()
            field = frame.locator('#tc-mgid')
            field.click()
            field.type('-1001234')
            field.evaluate('node => node.setSelectionRange(2, 5)')
            refuse_another_send(state['root'], '1000000003')
            # The next 10-second read lands while the field keeps focus.
            expect(status).to_contain_text('2 outgoing items not delivered', timeout=25000)
            expect(frame.locator('.tc-items')).to_contain_text('2 outgoing items not delivered')
            inner = next(f for f in page.frames if f is not page.main_frame)
            assert inner.evaluate('document.activeElement && document.activeElement.id') == 'tc-mgid'
            assert inner.evaluate('[document.activeElement.value, document.activeElement.selectionStart, '
                                  'document.activeElement.selectionEnd]') == ['-1001234', 2, 5]
            assert inner.evaluate("document.querySelector('details.tc-setting').open") is True
            assert state['posts'] == []
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['webkit', 'chromium'])
def test_an_unreadable_status_keeps_the_group_form_and_the_explanation(served, engine):
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    (state['root'] / 'settings.json').write_text(json.dumps({'management_group_id': '-1000000000009'}), encoding='utf-8')
    state['status_fault'] = 503
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.goto(url + '/?theme=light&width=366')
            page.wait_for_function('window.ready === true')
            frame = page.frame_locator('iframe')
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text('Can’t read the Telegram Transport status',
                                                                         timeout=15000)
            # The saved value is read once from settings/save, and the editor stays usable.
            summary = frame.locator('details.tc-setting > summary')
            expect(summary).to_contain_text('-1000000000009', timeout=10000)
            summary.click()
            expect(frame.locator('#tc-mgid')).to_have_value('-1000000000009')
            expect(frame.locator('.tc-setting .tc-note')).to_contain_text('Marks this room in event facts')
            frame.locator('details.tc-det > summary').click()
            expect(frame.locator('.tc-det')).to_contain_text('Telegram labels this account as a bot')
            frame.locator('#tc-mgid').fill('-1000000000010')
            frame.locator('.tc-setting .tc-btn').click()
            expect(frame.locator('.tc-result')).to_have_text('Saved.', timeout=10000)
            assert state['posts'] == [('settings/save', {'management_group_id': '-1000000000010'})]
            inner = next(f for f in page.frames if f is not page.main_frame)
            assert inner.evaluate('document.documentElement.scrollWidth <= innerWidth')
        finally:
            browser.close()


def open_card(pw, url, engine='chromium', theme='dark', width=335):
    browser = getattr(pw, engine).launch(headless=True)
    page = browser.new_page(viewport={'width': 1083 if width == 335 else 390, 'height': 1026})
    page.goto(url + f'/?theme={theme}&width={width}')
    page.wait_for_function('window.ready === true')
    return browser, page, page.frame_locator('iframe')


def store_of(state):
    sys.path.insert(0, str(ROOT / 'skills' / 'telegram-bot'))
    from telegram_bot.custody import CustodyStore
    return CustodyStore.shared(state['root'] / 'custody.sqlite3')


@pytest.mark.parametrize('kind', ['skill_access', 'place_not_connected'])
def test_the_hold_reason_that_needs_the_owner_is_shown_first(served, kind):
    """Six paused places outnumber the one owner-action hold (by the Host answer's kind); it leads, amber."""
    from playwright.sync_api import sync_playwright, expect
    from telegram_bot.host import SKILL_ACCESS_REFUSED
    state, url = served
    store = store_of(state)
    owner_note = SKILL_ACCESS_REFUSED if kind == 'skill_access' else 'Held: Connect this place in Consciousness > Places.'
    for index in range(7):
        for copy in range(1 if index == 6 else 9 - index):
            store.enqueue_outbox(f'held-{index}-{copy}', {'kind': 'message', 'chat_id': str(1000000100 + index * 10 + copy)})
            store.hold_outbox(store.claim_outbox().delivery_id,
                              owner_note if index == 6 else f'Held: Example place {index} is paused',
                              kind=kind if index == 6 else '')
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            held = frame.locator('.tc-item[data-kind=held]')
            expect(held).to_have_attribute('data-tone', 'warn', timeout=15000)
            first = held.locator('.tc-ib p').first
            expect(first).to_contain_text('Needs you:')
            expect(first).to_contain_text('Extensions' if kind == 'skill_access' else 'Connect this place')
            expect(held.locator('.tc-ib')).to_contain_text('…and 3 more reasons')
        finally:
            browser.close()


def test_sends_confirmed_in_part_point_to_the_receipt_never_a_blind_resend(served):
    """An incomplete album answer is a partial confirmation, not a lost answer; a refusal after a delivered first part
    is fixed and sent only where missing."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    store = store_of(state)
    store.enqueue_outbox('album', {'kind': 'album', 'chat_id': '1000000201'})
    lease = store.claim_outbox()
    store.checkpoint_outbox(lease.delivery_id, {**lease.payload, '_sent_media': [{'message_id': 501}, {'ok': True}]})
    store.mark_outbox_failed(lease.delivery_id, reason='AlbumIncomplete: Telegram answered 1 of 2 items', state='uncertain')
    store.enqueue_outbox('long', {'kind': 'message', 'chat_id': '1000000202'})
    lease = store.claim_outbox()
    store.checkpoint_outbox(lease.delivery_id, {**lease.payload, '_sent_messages': [{'message_id': 502}]})
    store.mark_outbox_failed(lease.delivery_id, reason='TelegramApiError: Telegram sendMessage failed (400): Bad Request')
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            unknown = frame.locator('.tc-item[data-kind=outcome_unknown]')
            expect(unknown).to_contain_text('Telegram confirmed part of it, but nobody knows whether the rest arrived',
                                            timeout=15000)
            expect(unknown).not_to_contain_text('was lost')
            expect(unknown).to_contain_text('confirmed_messages')
            failed = frame.locator('.tc-item[data-kind=not_delivered]')
            # The seeded refusal (the fixture's own) confirmed nothing; this one did: one of two in part.
            expect(failed).to_contain_text('Telegram confirmed part of 1 item before the failure')
            expect(failed).to_contain_text('send only what is missing')
            for text in (unknown.inner_text(), failed.inner_text()):
                assert 'confirmed_parts' not in text and 'send it again' not in text
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['webkit', 'chromium'])
def test_a_saved_group_survives_a_repaint_from_older_status(served, engine):
    """After a successful GET, a failed read and a save, the card shows the saved value, never the stale one."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url, engine=engine, width=366)
        try:
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated', timeout=15000)
            state['status_fault'] = 503  # the next reads fail; the card keeps the data read before the save
            frame.locator('.tc-fresh button').click()
            expect(frame.locator('.tc-fresh')).to_contain_text('Couldn’t refresh', timeout=15000)
            summary = frame.locator('details.tc-setting > summary')
            summary.click()
            field = frame.locator('#tc-mgid')
            field.fill('-1000000000077')
            frame.locator('.tc-setting .tc-btn').click()
            expect(frame.locator('.tc-result')).to_have_text('Saved.', timeout=10000)
            # The read after the save fails too; the older data repaints, and the saved value stays.
            expect(frame.locator('.tc-fresh')).to_contain_text('Couldn’t refresh', timeout=15000)
            page.wait_for_timeout(500)
            expect(field).to_have_value('-1000000000077')
            expect(summary).to_contain_text('-1000000000077')
            state['status_fault'] = 0  # a fresher successful read agrees with the file
            frame.locator('.tc-fresh button').click()
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated', timeout=15000)
            expect(field).to_have_value('-1000000000077')
            saved = json.loads((state['root'] / 'settings.json').read_text(encoding='utf-8'))
            assert saved['management_group_id'] == '-1000000000077'
            assert state['posts'] == [('settings/save', {'management_group_id': '-1000000000077'})]
        finally:
            browser.close()


def test_unknown_facts_read_as_unknown_never_as_none_or_unsupported(served):
    """Ouroboros not asked yet and custody counts not read: "not checked yet" and "—", never "none yet"."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    state['status_body'] = {
        'text': 'Starting…', 'tone': 'muted', 'attention': [], 'title': 'Telegram transport',
        'runtime_state': 'not_started', 'bot_label': 'not connected', 'host_checked': False,
        'presence_continuation_version': 2, 'reaction_context': 'available', 'reaction_history': 'available',
        'delivery_reporting': 'unknown', 'binding_state': 'missing', 'places_version': 0, 'management_group_id': '',
        'connect': None, 'connect_unavailable': 'Telegram bot not running', 'bot_unconfirmed': False,
        'last_event_at': '', 'last_delivery_at': '', 'last_error': '', 'recent_problems': None, 'held_reasons': [],
    }
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-facts').first).to_contain_text('unknown: the custody counts were not read',
                                                                     timeout=15000)
            frame.locator('details.tc-det > summary').click()

            def fact(label):
                return frame.locator('.tc-det dt', has_text=label).first.locator('xpath=following-sibling::dd[1]')
            expect(fact('Early answers')).to_have_text('not checked yet')
            expect(fact('Reactions in Host context')).to_have_text('not checked yet')
            expect(fact('Last update')).to_have_text('—')
            expect(fact('Last delivery')).to_have_text('—')
            assert 'none yet' not in frame.locator('.tc-det').inner_text()
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['webkit', 'chromium'])
def test_refresh_keeps_keyboard_focus(served, engine):
    """Enter on Refresh, and the next tick, replace only the freshness line: focus stays on Refresh."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url, engine=engine)
        try:
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated', timeout=15000)
            frame.locator('.tc-fresh button').focus()
            inner = next(f for f in page.frames if f is not page.main_frame)

            def focused():
                return inner.evaluate("document.activeElement && document.activeElement.getAttribute('data-focus')")
            page.keyboard.press('Enter')
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated', timeout=15000)
            page.wait_for_timeout(300)
            assert focused() == 'refresh'
            page.wait_for_timeout(10500)  # one 10-second tick repaints the freshness line
            assert focused() == 'refresh'
        finally:
            browser.close()


@pytest.mark.parametrize('failure,text,headline', [
    ('token', 'Check TELEGRAM_PUBLIC_BOT_TOKEN', 'Connection error · new messages are not being received'),
    ('local', 'Not a token or network problem', 'Error on this computer · new messages are not being processed'),
])
def test_the_link_item_dates_the_last_successful_answer_and_names_its_side(served, failure, text, headline):
    """The route's own summary of a failed attempt for the token set now, rendered as the card shows it."""
    from playwright.sync_api import sync_playwright, expect
    sys.path.insert(0, str(ROOT / 'skills' / 'telegram-bot'))
    from telegram_bot.status_summary import summarize
    state, url = served
    body = {'runtime_state': 'provider_error', 'poll_result': 'failed', 'poll_failure': failure,
            'poll_error': 'TelegramApiError: synthetic', 'poll_at': '2026-11-22T18:05:00Z',
            'provider_ok_at': '2026-11-22T18:00:00Z', 'host_checked': True, 'places_version': 1,
            'binding_state': 'missing', 'reaction_context': 'available', 'bot_label': '@example_bot',
            'connect': None, 'connect_unavailable': 'Observed chats could not be read', 'bot_unconfirmed': False,
            'recent_problems': None, 'held_reasons': [], 'management_group_id': ''}
    summary = summarize(body)
    state['status_body'] = {**body, **summary, 'title': 'Telegram transport'}
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(headline, timeout=15000)
            link = frame.locator('.tc-item[data-kind=link]')
            expect(link.locator('.tc-ih-at')).to_contain_text('last successful answer')
            expect(link).to_contain_text(text)
            if failure == 'local':
                expect(link).not_to_contain_text('TELEGRAM_PUBLIC_BOT_TOKEN')
        finally:
            browser.close()


@pytest.mark.parametrize('answer', [{}, None, []], ids=['empty-object', 'null', 'array'])
def test_a_200_that_is_not_a_status_is_a_failed_read(served, answer):
    """A 200 whose body is not a status keeps the last readable snapshot and the editor, and says the read failed."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            status = frame.locator('.tc-status .tc-st-text')
            expect(status).to_contain_text('not delivered', timeout=15000)
            before = status.inner_text()
            state['status_body'] = answer
            frame.locator('.tc-fresh button').click()
            expect(frame.locator('.tc-fresh')).to_contain_text('Couldn’t refresh', timeout=15000)
            expect(status).to_have_text(before)  # never "Stopped" or "token missing" from an empty answer
            expect(frame.locator('details.tc-setting > summary')).to_be_visible()
        finally:
            browser.close()


@pytest.mark.parametrize('answer', [{}, None], ids=['empty-object', 'null'])
def test_a_first_answer_that_is_not_a_status_reads_as_unreadable_with_the_editor(served, answer):
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    state['status_body'] = answer
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text('Can’t read the Telegram Transport status',
                                                                         timeout=15000)
            expect(frame.locator('.tc-fresh')).to_contain_text('Couldn’t read the status')
            expect(frame.locator('details.tc-setting > summary')).to_be_visible()
        finally:
            browser.close()
