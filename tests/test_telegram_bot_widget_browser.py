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
import time
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
                    if state.get('post_delay'):
                        time.sleep(state['post_delay'])  # a slow save: the reader keeps typing meanwhile
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


def synthetic_status(**facts):
    """A route answer made by the route's own summary from synthetic facts (states a fixture store cannot reach)."""
    sys.path.insert(0, str(ROOT / 'skills' / 'telegram-bot'))
    from telegram_bot.status_summary import link_state, summarize
    body = {'runtime_state': 'polling', 'poll_result': 'ok', 'host_checked': True, 'places_version': 1,
            'binding_state': 'missing', 'reaction_context': 'available', 'bot_label': '@example_bot',
            'connect': [], 'bot_unconfirmed': False, 'held_reasons': [], 'management_group_id': '',
            'recent_problems': {'window_seconds': 86400,
                                'outbound': {'failed': 0, 'uncertain': 0, 'failed_partly_confirmed': 0,
                                             'uncertain_partly_confirmed': 0, 'last_failed_at': '',
                                             'last_uncertain_at': '', 'last_error': None},
                                'inbound': {'failed': 0, 'last_failed_at': '', 'last_error': None}}}
    outbound = facts.pop('outbound', {})
    body['recent_problems']['outbound'].update(outbound)
    body.update(facts)
    return {**body, **summarize(body), 'link': link_state(body), 'title': 'Telegram transport'}


def test_sends_confirmed_in_part_point_to_the_receipt_never_a_blind_resend(served):
    """An album whose answer named no message was not lost: its delivery could not be established. One confirmed in
    part says so apart; a refusal after a delivered first part is fixed and sent only where missing."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    store = store_of(state)
    store.enqueue_outbox('album', {'kind': 'album', 'chat_id': '1000000201'})
    lease = store.claim_outbox()
    store.checkpoint_outbox(lease.delivery_id, {**lease.payload, '_sent_media': [{'message_id': 501}, {'ok': True}]})
    store.mark_outbox_failed(lease.delivery_id, reason='AlbumIncomplete: Telegram answered 1 of 2 items', state='uncertain')
    store.enqueue_outbox('album-no-ids', {'kind': 'album', 'chat_id': '1000000203'})
    lease = store.claim_outbox()
    store.checkpoint_outbox(lease.delivery_id, {**lease.payload, '_sent_media': [{'ok': True}, {'ok': True}]})
    store.mark_outbox_failed(lease.delivery_id, reason='AlbumIncomplete: Telegram answered ok without message ids',
                             state='uncertain')
    store.enqueue_outbox('long', {'kind': 'message', 'chat_id': '1000000202'})
    lease = store.claim_outbox()
    store.checkpoint_outbox(lease.delivery_id, {**lease.payload, '_sent_messages': [{'message_id': 502}]})
    store.mark_outbox_failed(lease.delivery_id, reason='TelegramApiError: Telegram sendMessage failed (400): Bad Request')
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            unknown = frame.locator('.tc-item[data-kind=outcome_unknown]')
            expect(unknown).to_contain_text('Telegram confirmed part of 1 item; for the other, delivery could not be '
                                            'established', timeout=15000)
            expect(unknown).to_contain_text('confirmed_messages')
            failed = frame.locator('.tc-item[data-kind=not_delivered]')
            # The seeded refusal (the fixture's own) confirmed nothing; this one did: one of two in part.
            expect(failed).to_contain_text('Telegram confirmed part of 1 item before the failure')
            expect(failed).to_contain_text('send only what is missing')
            texts = [unknown.inner_text(), failed.inner_text()]
            # One send, nothing confirmed (an ok naming no message, or no answer at all): not established, never "lost".
            state['status_body'] = synthetic_status(outbound={'uncertain': 1, 'last_uncertain_at': '2026-11-22T18:00:00Z'})
            frame.locator('.tc-fresh button').click()
            expect(unknown).to_contain_text('Delivery could not be established, so nobody knows whether it arrived',
                                            timeout=15000)
            texts.append(unknown.inner_text())
            # One send confirmed in part.
            state['status_body'] = synthetic_status(outbound={'uncertain': 1, 'uncertain_partly_confirmed': 1,
                                                              'last_uncertain_at': '2026-11-22T18:00:00Z'})
            frame.locator('.tc-fresh button').click()
            expect(unknown).to_contain_text('Telegram confirmed part of it, but nobody knows whether the rest arrived',
                                            timeout=15000)
            expect(unknown).to_contain_text('confirmed_messages')
            texts.append(unknown.inner_text())
            for text in texts:
                assert 'was lost' not in text and 'confirmed_parts' not in text and 'send it again' not in text
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


_ZERO_COUNTS = {key: 0 for key in (
    'inbox_waiting', 'inbox_leased', 'inbox_submitted', 'inbox_retained', 'inbox_failed', 'reactions_withheld',
    'outbox_waiting', 'outbox_held', 'outbox_leased', 'outbox_delivered', 'outbox_failed', 'outbox_uncertain',
    'work_waiting', 'work_leased', 'work_terminal', 'delivery_reports_pending', 'delivery_reports_held',
    'telegram_offset', 'group_backfill_unreadable')}
_CHECKED = {'host_checked': True, 'host_checked_at': '2026-11-22T18:00:00Z', 'delivery_reporting': 'supported',
            'reaction_context': 'available', 'reaction_history': 'available', 'presence_continuation_version': 2,
            'places_version': 1}


def test_degraded_states_claim_no_more_than_the_status_records(served):
    """Counts unknown, the bot stopped, Ouroboros not checked or not answering, a fresh install, an inbound refusal:
    each reads as what the status records, never as zero, a total, a current delivery or an outbound failure."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    previous = {'sends_as': {'id': '77', 'label': '@example_bot', 'kind': 'bot'}}
    with sync_playwright() as pw:
        state['status_body'] = synthetic_status(
            runtime_state='stopped', bot_label='@example_bot', identity=previous, connect=None,
            connect_unavailable='Telegram bot not running', recent_problems=None, **_CHECKED)
        browser, page, frame = open_card(pw, url)
        try:
            card = frame.locator('.tc')

            def fact(label):
                return frame.locator('.tc-det dt', has_text=label).first.locator('xpath=following-sibling::dd[1]')

            def show(body, first_line):
                state['status_body'] = body
                frame.locator('.tc-fresh button').click()
                expect(frame.locator('.tc-status .tc-st-text')).to_have_text(first_line, timeout=15000)

            # Counts unknown (the store was not read) while the bot is stopped; Ouroboros was checked earlier.
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text('Stopped · the bot is not running', timeout=15000)
            frame.locator('details.tc-det > summary').click()
            expect(frame.locator('.tc-facts').first).to_contain_text('unknown: the custody counts were not read')
            expect(frame.locator('.tc-det')).to_contain_text('The custody counts were not read, so the queues are unknown.')
            expect(fact('Last update')).to_have_text('—')
            expect(fact('Sends as')).to_contain_text('@example_bot (77) · last confirmed bot; the bot is not running')
            expect(fact('Delivery history')).to_contain_text('supported by Ouroboros (checked ')
            expect(fact('Early answers')).to_contain_text('supported by Ouroboros (checked ')
            for claim in ('Totals since install', 'Counts not listed are zero', 'none yet', 'recorded in Ouroboros',
                          'passed to Ouroboros'):
                assert claim not in card.inner_text(), claim
            # Ouroboros not asked yet.
            show(synthetic_status(host_checked=False, delivery_reporting='unknown', recent_problems=None),
                 'Connected · state partly known')
            for label in ('Delivery history', 'Reactions in Host context', 'Reaction history', 'Early answers'):
                expect(fact(label)).to_have_text('not checked yet')
            expect(fact('Place binding')).to_have_text('none saved; whether this Ouroboros needs one is not checked yet')
            # Ouroboros did not answer the check.
            show(synthetic_status(host_checked=False, delivery_reporting='unavailable',
                                  host_checked_at='2026-11-22T18:00:00Z', recent_problems=None),
                 'Connected · state partly known')
            expect(fact('Delivery history')).to_contain_text('not known: Ouroboros didn’t answer the check (checked ')
            # Every row that depends on that check says it went unanswered, with when; none says "not checked yet".
            for label in ('Reactions in Host context', 'Reaction history', 'Early answers', 'Place binding'):
                expect(fact(label)).to_contain_text('not known: Ouroboros didn’t answer the check (checked ')
            assert 'not checked yet' not in frame.locator('.tc-det').inner_text()
            # A fresh install: counts known and zero, no chats yet; the check is a past check, never a delivery.
            show(synthetic_status(**_ZERO_COUNTS, **_CHECKED), 'Connected · nothing needs attention')
            expect(frame.locator('.tc-note').first).to_have_text('No messages received or sent yet.')
            expect(fact('Chats to connect')).to_have_text('no chats available')
            expect(fact('Delivery history')).to_contain_text('supported by Ouroboros (checked ')
            assert 'recorded in Ouroboros' not in card.inner_text()
            # An update Ouroboros refused: not passed to Ouroboros, never "not delivered".
            show(synthetic_status(**{**_ZERO_COUNTS, 'inbox_failed': 1}, **_CHECKED,
                                  recent_problems={'window_seconds': 86400,
                                                   'outbound': {'failed': 0, 'uncertain': 0},
                                                   'inbound': {'failed': 1, 'last_failed_at': '2026-11-22T18:00:00Z',
                                                               'last_error': {'at': '2026-11-22T18:00:00Z',
                                                                              'state': 'failed', 'text': 'rejected'}}}),
                 'Connected · 1 Telegram update not passed to Ouroboros')
            handed = frame.locator('.tc-item[data-kind=not_handed]')
            expect(handed).to_contain_text('Ouroboros refused it for good')
            expect(frame.locator('.tc-facts').first).to_contain_text('1 refused')
            expect(fact('Incoming updates')).to_contain_text('1 refused by Ouroboros')
            assert 'not delivered' not in handed.inner_text()
            assert 'not delivered' not in frame.locator('.tc-facts').first.inner_text()
        finally:
            browser.close()


# Counts every Refresh button the card takes out of the page from now on, and marks the one focused now.
_WATCH_REFRESH = """() => {
    window.__refreshRemoved = 0;
    new MutationObserver(records => {
        for (const record of records) for (const node of record.removedNodes) {
            if (node.nodeType === 1 && (node.matches('button[data-focus=refresh]')
                    || node.querySelector('button[data-focus=refresh]'))) window.__refreshRemoved++;
        }
    }).observe(document.getElementById('root') || document.body, {childList: true, subtree: true});
    const button = document.querySelector('.tc-fresh button');
    button.__probe = 'the-focused-one';
    button.focus();
}"""


@pytest.mark.parametrize('engine', ['webkit', 'chromium'])
def test_refresh_keeps_the_same_focused_button_and_repaints_a_bounded_number_of_times(served, engine):
    """Enter on Refresh, then a 10-second tick: the freshness line changes in place. Replacing the focused button
    blurred it, the blur scheduled another paint, and that paint replaced it again: 112 replacements in 500 ms."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url, engine=engine)
        try:
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated', timeout=15000)
            inner = next(f for f in page.frames if f is not page.main_frame)
            inner.evaluate(_WATCH_REFRESH)
            page.keyboard.press('Enter')
            page.wait_for_timeout(500)
            assert inner.evaluate('window.__refreshRemoved') <= 1
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated', timeout=15000)
            page.wait_for_timeout(10500)  # one 10-second tick repaints the freshness line
            assert inner.evaluate("document.activeElement && document.activeElement.__probe") == 'the-focused-one'
            assert inner.evaluate('window.__refreshRemoved') == 0
        finally:
            browser.close()


_TOKEN_ADVICE, _NETWORK_ADVICE = 'Check TELEGRAM_PUBLIC_BOT_TOKEN', 'network connection'


@pytest.mark.parametrize('failure,stage,says,never,headline', [
    ('token', 'identity', _TOKEN_ADVICE, [_NETWORK_ADVICE], 'Connection error · new messages are not being received'),
    # A token with a space or tab never left this computer: correct the token, never check the network.
    ('token_format', 'identity', 'Correct TELEGRAM_PUBLIC_BOT_TOKEN', [_NETWORK_ADVICE, 'Telegram refused'],
     'Connection error · new messages are not being received'),
    ('network', 'updates', 'check this computer’s network connection', [_TOKEN_ADVICE],
     'Connection error · new messages are not being received'),
    ('telegram', 'updates', 'Telegram’s own explanation', [_TOKEN_ADVICE, _NETWORK_ADVICE],
     'Connection error · new messages are not being received'),
    ('local', 'record', 'recording the new messages on this computer', [_TOKEN_ADVICE, _NETWORK_ADVICE],
     'Error on this computer · new messages are not being processed'),
    ('local', 'identity', 'Telegram confirmed the bot, but recording it on this computer failed',
     [_TOKEN_ADVICE, _NETWORK_ADVICE], 'Error on this computer · new messages are not being processed'),
])
def test_the_link_item_dates_the_last_successful_answer_and_names_its_side(served, failure, stage, says, never,
                                                                          headline):
    """A failed attempt for the token set now, by the side and stage it stopped at: only that side's advice."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    state['status_body'] = synthetic_status(
        runtime_state='provider_error', poll_result='failed', poll_failure=failure, poll_stage=stage,
        poll_error='TelegramApiError: synthetic', poll_at='2026-11-22T18:05:00Z', provider_ok_at='2026-11-22T18:00:00Z')
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(headline, timeout=15000)
            link = frame.locator('.tc-item[data-kind=link]')
            expect(link.locator('.tc-ih-at')).to_contain_text('last successful answer')
            expect(link).to_contain_text(says)
            if failure == 'local':
                expect(link).to_contain_text('Not a token or network problem')
            for words in never:
                expect(link).not_to_contain_text(words)
        finally:
            browser.close()


def test_a_check_that_made_no_request_never_says_ouroboros_did_not_answer(served):
    """Review wave 5, 7d: without this skill's Ouroboros access token no /identity request is made. The attention line
    and every row that depends on the check say Ouroboros was not asked, never that it did not answer."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    state['status_body'] = synthetic_status(host_checked=False, delivery_reporting='unavailable',
                                            host_unasked='skill_token_unavailable',
                                            host_checked_at='2026-11-22T18:00:00Z')
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text('Connected · state partly known', timeout=15000)
            expect(frame.locator('.tc-item[data-kind=partial]')).to_contain_text(
                'this skill’s Ouroboros access token is unavailable, so no request was made')
            frame.locator('details.tc-det > summary').click()
            for label in ('Delivery history', 'Reactions in Host context', 'Reaction history', 'Early answers',
                          'Place binding'):
                value = frame.locator('.tc-det dt', has_text=label).first.locator('xpath=following-sibling::dd[1]')
                expect(value).to_contain_text('Ouroboros was not asked, this skill’s access token is unavailable (tried ')
            text = frame.locator('.tc').inner_text()
            assert 'answer the check' not in text and 'did not answer' not in text, text
        finally:
            browser.close()


def test_names_from_telegram_and_ouroboros_render_as_text_never_html(served):
    """Review wave 5, 7f: the bot's name, the identity Ouroboros sends as, a held reason and an error text are shown as
    the characters they are; markup in them never becomes an element."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    markup = '<img src=x onerror="window.__injected=1">'
    state['status_body'] = synthetic_status(
        bot_label='@example' + markup, identity={'sends_as': {'id': '77', 'label': 'Example' + markup, 'kind': 'bot'}},
        outbox_held=1, held_reasons=[{'reason': 'Paused ' + markup, 'note': 'Held: Paused ' + markup, 'count': 1}],
        last_error='TelegramApiError: ' + markup, last_error_at='2026-11-22T18:00:00Z')
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-item[data-kind=held]')).to_contain_text('Paused ' + markup, timeout=15000)
            frame.locator('details.tc-det > summary').click()
            expect(frame.locator('.tc-det')).to_contain_text('@example' + markup)
            expect(frame.locator('.tc-det')).to_contain_text('Example' + markup)
            assert frame.locator('.tc img').count() == 0
            assert all(f.evaluate('window.__injected === undefined') for f in page.frames)
        finally:
            browser.close()


# Status-shaped answers wrong in exactly one field: each fails only by its own check (text, tone), so dropping either
# check from the kit fails a test; null and a list stand for answers that are not an object at all.
_TONE_MISSING = {'text': 'Connected · nothing needs attention', 'attention': [], 'runtime_state': 'polling'}
_TEXT_NOT_A_STRING = {'text': 7, 'tone': 'ok', 'attention': [], 'runtime_state': 'polling'}
_TONE_NOT_A_STRING = {'text': 'Connected · nothing needs attention', 'tone': 5, 'attention': [], 'runtime_state': 'polling'}


@pytest.mark.parametrize('answer', [_TONE_MISSING, _TEXT_NOT_A_STRING, None], ids=['tone-missing', 'text-number', 'null'])
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


@pytest.mark.parametrize('answer', [_TONE_NOT_A_STRING, []], ids=['tone-number', 'array'])
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


def test_a_save_confirms_the_value_it_sent_while_a_newer_draft_waits(served):
    """While Save sent -1000000000002 the reader typed -1000000000003: the answer names what was saved and keeps the
    draft, never "Saved." beside a value the server does not have."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-fresh')).to_contain_text('Updated', timeout=15000)
            frame.locator('details.tc-setting > summary').click()
            field = frame.locator('#tc-mgid')
            field.fill('-1000000000002')
            state['post_delay'] = 1.5
            frame.locator('.tc-setting .tc-btn').click()
            expect(frame.locator('.tc-result')).to_have_text('Saving…')
            field.fill('-1000000000003')
            result = frame.locator('.tc-result')
            expect(result).to_contain_text('-1000000000002', timeout=10000)
            expect(result).to_contain_text('not saved yet')
            assert result.inner_text() != 'Saved.'
            expect(field).to_have_value('-1000000000003')
            saved = json.loads((state['root'] / 'settings.json').read_text(encoding='utf-8'))
            assert saved['management_group_id'] == '-1000000000002'
            # The summary names what is saved; the draft stays until the reader saves it.
            expect(frame.locator('details.tc-setting > summary')).to_contain_text('-1000000000002', timeout=15000)
            page.wait_for_timeout(500)
            expect(field).to_have_value('-1000000000003')
        finally:
            state['post_delay'] = 0
            browser.close()


def test_details_word_the_link_as_the_first_line_does_and_mark_the_last_confirmed_bot(served):
    """Details › State reads the route's own link judgement: during a token rotation it says connecting (never
    "requests failing"), and the bot it still names is the last confirmed one; a local failure reads as local."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    old_bot = {'sends_as': {'id': '77', 'label': '@previous_bot', 'kind': 'bot'}}
    state['status_body'] = synthetic_status(
        runtime_state='provider_error', poll_result='', bot_label='@previous_bot', identity=old_bot,
        connect=None, connect_unavailable='Bot identity is unconfirmed; check the transport status',
        bot_unconfirmed=True, last_error='TelegramApiError: Telegram getMe failed (401): Unauthorized',
        last_error_source='polling')
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text('Connecting…', timeout=15000)
            frame.locator('details.tc-det > summary').click()

            def fact(label):
                return frame.locator('.tc-det dt', has_text=label).first.locator('xpath=following-sibling::dd[1]')
            expect(fact('State')).to_have_text('connecting: no completed answer from Telegram for this token yet')
            expect(fact('Sends as')).to_contain_text('@previous_bot (77) · last confirmed bot; the current token is not '
                                                     'confirmed yet')
            expect(frame.locator('.tc-facts').first).to_contain_text('@previous_bot (last confirmed bot')
            state['status_body'] = synthetic_status(
                runtime_state='provider_error', poll_result='failed', poll_failure='local', poll_stage='record',
                poll_error='sqlite3.OperationalError: database is locked', provider_ok_at='2026-11-22T18:00:00Z',
                identity={'sends_as': {'id': '9', 'label': '@example_bot', 'kind': 'bot'}})
            frame.locator('.tc-fresh button').click()
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(
                'Error on this computer · new messages are not being processed', timeout=15000)
            expect(fact('State')).to_have_text('failing on this computer, retrying')
            expect(fact('Sends as')).to_have_text('@example_bot (9)')
        finally:
            browser.close()


def test_held_and_capability_items_promise_no_send_the_link_cannot_make(served):
    """A held send and the reactions note stay visible in every link state; what they say happens next follows the
    route's link: nothing is sent while the bot is stopped or has no token, and no note says messages work as usual."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    held = {'outbox_held': 2, 'held_reasons': [{'reason': 'Example place is paused', 'note': 'Held: Example place is paused',
                                                 'count': 2}],
            'reaction_context': 'update_required', 'reactions_withheld': 3}
    state['status_body'] = synthetic_status(runtime_state='stopped', **held)
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            hold = frame.locator('.tc-item[data-kind=held]')
            reactions = frame.locator('.tc-item[data-kind=reactions]')
            expect(frame.locator('.tc-status .tc-st-text')).to_contain_text('Stopped · the bot is not running',
                                                                            timeout=15000)
            expect(hold).to_contain_text('Example place is paused')
            expect(hold).to_contain_text('Nothing is retried while the bot is stopped')
            expect(hold).not_to_contain_text('sent by themselves')
            expect(reactions).to_contain_text('Update Ouroboros')
            expect(reactions).not_to_contain_text('work as usual')
            for link_facts, says in (({'runtime_state': 'waiting_for_token', 'poll_result': '', 'connect': None,
                                       'connect_unavailable': 'Bot token missing'},
                                      'Without a bot token every retry uses up one of'),
                                     ({}, 'checking the hold uses no attempt')):
                state['status_body'] = synthetic_status(**link_facts, **held)
                frame.locator('.tc-fresh button').click()
                expect(hold).to_contain_text(says, timeout=15000)
                expect(hold).to_contain_text('Example place is paused')
        finally:
            browser.close()


# What the outbox worker does with a held row in each link state (runtime.process_one_outbox): with a confirmed bot the
# place is checked again about every 5 s and a hold gives its attempt back; without a token, or when the bot cannot
# reach Telegram, each claim uses up one of _MAX_OUTBOX_ATTEMPTS (5) and the row fails for good after the last;
# a stopped bot runs no worker.
_HELD_STATES = [
    ('stopped', {'runtime_state': 'stopped'}, 'Nothing is retried while the bot is stopped'),
    ('not_started', {'runtime_state': 'not_started'}, 'retried once it runs'),
    ('waiting_for_token', {'runtime_state': 'waiting_for_token', 'poll_result': '', 'connect': None,
                           'connect_unavailable': 'Bot token missing'}, 'Without a bot token every retry uses up one of'),
    ('connecting', {'poll_result': ''}, 'a retry that cannot reach Telegram uses up one of'),
    ('provider_error', {'runtime_state': 'provider_error', 'poll_result': 'failed', 'poll_failure': 'network'},
     'a retry that cannot reach Telegram uses up one of'),
    ('local_error', {'runtime_state': 'provider_error', 'poll_result': 'failed', 'poll_failure': 'local',
                     'poll_stage': 'record'}, 'does not show whether sending works'),
    ('connected', {}, 'checking the hold uses no attempt'),
]


def test_a_held_send_is_worded_from_what_the_worker_does_in_each_link_state(served):
    """No state promises that a held send will go: each says how it is retried (and where attempts are used up) and
    points to its receipt. Without a token a held row really uses up its attempts and fails (runtime.py), so the old
    "then they go once the reason clears" was false."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    held = {'outbox_held': 2, 'held_reasons': [{'reason': 'Example place is paused',
                                                 'note': 'Held: Example place is paused', 'count': 2}]}
    state['status_body'] = synthetic_status(**_HELD_STATES[0][1], **held)
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            hold = frame.locator('.tc-item[data-kind=held]')
            for index, (link, facts, says) in enumerate(_HELD_STATES):
                if index:
                    state['status_body'] = synthetic_status(**facts, **held)
                    frame.locator('.tc-fresh button').click()
                assert state['status_body']['link'] == link
                expect(hold).to_contain_text(says, timeout=15000)
                expect(hold).to_contain_text('telegram_receipt')
                expect(hold).to_contain_text('Example place is paused')
                text = hold.inner_text()
                for promise in ('they go', 'it goes', 'will go', 'sent by themselves', 'sent by itself'):
                    assert promise not in text, (link, promise)
        finally:
            browser.close()


def test_waiting_updates_without_a_recorded_stage_claim_neither_failure_nor_unsent(served):
    """An update whose last attempt left no recorded stage (an earlier version's row, or Ouroboros answered and
    recording that here failed): the card says it waits for another attempt and that its handling is not confirmed
    here; its note is shown as an earlier version's note, never as the last error. A recorded handoff failure keeps
    its own words and its "Last error"."""
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    unknown = {'count': 1, 'submit': 0, 'download': 0, 'other': 1, 'since': '2026-11-22T18:00:00Z',
               'next_attempt_at': '2026-11-22T18:00:30Z', 'last_stage': '', 'last_error_at': '',
               'last_error': 'PresenceHostHTTPError: presence Host returned HTTP 429: presence_capacity_full',
               'last_error_recorded': False, 'last_error_newest': False}
    state['status_body'] = synthetic_status(handoff_retrying=unknown, inbox_waiting=1)
    with sync_playwright() as pw:
        browser, page, frame = open_card(pw, url)
        try:
            item = frame.locator('.tc-item[data-kind=handoff_waiting]')
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(
                'Connected · 1 Telegram update waits for another attempt', timeout=15000)
            expect(item).to_contain_text('Not confirmed here whether it was handled')
            expect(item).to_contain_text('Note left by an earlier version (not known to be a failure)')
            for claim in ('Last error', 'to be passed to Ouroboros', 'not passed on', 'failed;'):
                expect(item).not_to_contain_text(claim)
            recorded = {**unknown, 'submit': 1, 'other': 0, 'last_stage': 'submit', 'last_error': 'PresenceHostError: 500',
                        'last_error_at': '2026-11-22T18:00:00Z', 'last_error_recorded': True, 'last_error_newest': True}
            state['status_body'] = synthetic_status(handoff_retrying=recorded, inbox_waiting=1)
            frame.locator('.tc-fresh button').click()
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(
                'Connected · 1 Telegram update waits to be passed to Ouroboros', timeout=15000)
            expect(item).to_contain_text('Passing to Ouroboros failed')
            expect(item).to_contain_text('Last error')
        finally:
            browser.close()
