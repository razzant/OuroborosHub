"""Slack Bridge card through the core's real module bridge and the skill's own routes.

Opt in with OUROBOROS_THEME_CORE_ROOT (a core checkout with web/modules/widget_module.js). Synthetic private state
only: no companion, no runtime loop, no Slack or Host call. Needs skills/slack-bridge and tests/widget_lease_gateway.py,
which answers the card's route lease as the core's controls do.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widget_lease_gateway import RESOURCES, LeaseGateway, controls  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# The card's live publication as a host lists it (HOST mounts the same tab id and generation).
CARDS = {'slack-bridge': ('slack_presence', 'fixture-generation')}
HOST = '''<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0;background:#0f0f11}.widgets-frame{width:100%;height:var(--widget-frame-height,320px);border:0;display:block}</style></head>
<body><section style="width:__WIDTH__px"><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('__THEME__');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'__SKILL__',ws_prefix:'ext:__SKILL__:',tab_id:'slack_presence',generation:'fixture-generation'},
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


TOKENS = {'SLACK_BOT_TOKEN': 'xoxb-synthetic', 'SLACK_APP_TOKEN': 'xapp-synthetic'}


def dead_pid():
    """A provably exited OS PID: a short child, spawned and reaped."""
    import subprocess
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    assert child.wait(timeout=10.0) == 0
    return child.pid


HOST_FACTS = dict(host_adapter_state='active', workspace_name='Northstar Labs', reaction_context_state='available',
                  history_reporting_state='supported')


def retrying_event(store, parse, note, index=1):
    """An event Ouroboros has not confirmed receiving, saved for its next try."""
    raw = {'type': 'events_api', 'envelope_id': f'env-synth-{index}', 'payload': {
        'event_id': f'EvSYNTH{index}', 'team_id': 'T09SYNTH01', 'event': {
            'type': 'message', 'user': 'U09SYNTH01', 'channel': 'C09SYNTH01', 'channel_type': 'channel',
            'ts': f'170000000{index}.000100', 'event_ts': f'170000000{index}.000100', 'text': 'synthetic message'}}}
    store.ingest_envelope(raw, parse(raw, bot_user_id='U09OUROBOT1'))
    event = store.claim_inbox()
    store.retry_inbox(event.row_id, event.lease_token, note, delay_seconds=30.0)


# A token another version may have stored inside an error, as a header refusal prints it (synthetic).
LEAKED = "Illegal header value b'Bearer xoxb-4417-SYNTHETICTOKEN\\nTAILPIECE42'"


def seed(scenario, root):
    """Synthetic store states: healthy, recent problems with holds, refused tokens, and the degraded states."""
    sys.path.insert(0, str(ROOT / 'skills' / 'slack-bridge'))
    from lib.events import parse_socket_envelope
    from lib.store import BridgeStore
    if scenario == 'store_unreadable':
        (root / 'slack_bridge.sqlite3').write_bytes(b'not a database, synthetic' * 64)
        return TOKENS
    store = BridgeStore(root)
    try:
        if scenario == 'stopped':  # a clean stop after an active adapter: the daemon leaves "disconnected", no PID
            store.set_runtime(socket_state='disconnected', **{**HOST_FACTS, 'reaction_context_state': 'update_required'})
            retrying_event(store, parse_socket_envelope, 'Presence Host returned HTTP 503')
            return TOKENS
        if scenario == 'token_error':  # an older record whose error text holds a token: the route cleans it
            store.set_runtime(socket_state='error', last_socket_error='placeholder', **HOST_FACTS)
            store.close()
            import sqlite3
            with sqlite3.connect(root / 'slack_bridge.sqlite3') as conn:
                conn.execute("UPDATE runtime_state SET value_json=? WHERE key='last_socket_error'", (json.dumps(LEAKED),))
            return TOKENS
        if scenario == 'crashed':  # the companion died while its record still says connected
            store.set_runtime(socket_state='connected', companion_pid=dead_pid(), connected_at=1795381200.0, **HOST_FACTS)
            return TOKENS
        if scenario == 'unknown_liveness':  # the probe is made not to answer in the fixture below
            store.set_runtime(socket_state='error', last_socket_error='ConnectError: All connection attempts failed',
                              companion_pid=os.getpid(), **HOST_FACTS)
            retrying_event(store, parse_socket_envelope, 'Presence Host returned HTTP 503')
            return TOKENS
        if scenario == 'auth_refused':
            # What the daemon leaves after Slack refused the app token: the error, and no companion PID.
            store.set_runtime(socket_state='error', last_socket_error='Slack API error: invalid_auth',
                              workspace_name='Northstar Labs', host_adapter_state='active')
            return TOKENS
        store.set_runtime(socket_state='connected', host_adapter_state='active', workspace_name='Northstar Labs',
                          companion_pid=os.getpid(), connected_at=1795381200.0, reaction_context_state='available',
                          history_reporting_state='supported')
        if scenario == 'host_unanswered':  # the start check got a 503: what the runtime records for it
            store.set_runtime(host_adapter_state='missing_binding_id', history_reporting_state='unavailable',
                              reaction_context_state='update_required')
        if scenario == 'reactions_unbound':  # Ouroboros answered: no place confirmed, reactions need an update
            store.set_runtime(host_adapter_state='missing_binding_id', history_reporting_state='unsupported',
                              reaction_context_state='update_required')
        if scenario in ('lost_reply', 'inbound_failed'):
            raw = {'type': 'events_api', 'envelope_id': 'env-synth-1', 'payload': {
                'event_id': 'EvSYNTH1', 'team_id': 'T09SYNTH01', 'event': {
                    'type': 'message', 'user': 'U09SYNTH01', 'channel': 'C09SYNTH01', 'channel_type': 'channel',
                    'ts': '1700000001.000100', 'event_ts': '1700000001.000100', 'text': 'synthetic message'}}}
            store.ingest_envelope(raw, parse_socket_envelope(raw, bot_user_id='U09OUROBOT1'))
            event = store.claim_inbox()
            if scenario == 'lost_reply':  # the Host recorded the turn; its reply to the POST was lost
                store.retry_inbox(event.row_id, event.lease_token, 'ReadTimeout: reply lost after Host recorded the turn',
                                  delay_seconds=30.0)
            else:  # the reply was already delivered; a later poll was refused
                store.fail_inbox(event.row_id, event.lease_token, 'Presence binding was rejected by Host (HTTP 404)')
        if scenario == 'mixed':  # part 1 refused, part 2 held: one message with two outcomes
            store.enqueue_outbox(request_id='synthetic-mixed', target='C09SYNTH01', thread_ts='',
                                 chunks=('part one', 'part two'))
            first = store.claim_outbox()
            store.fail_outbox(first.row_id, first.lease_token, 'msg_blocked', state='failed')
            store.hold_outbox(store.claim_outbox(), 'Held: Ops room is paused')
        if scenario == 'mixed_1b':
            # One message: part 1 delivered, part 2 refused. Another: part 1 refused, part 2 still waiting.
            store.enqueue_outbox(request_id='synthetic-partly', target='C09SYNTH01', thread_ts='', chunks=('one', 'two'))
            first = store.claim_outbox()
            store.complete_outbox(first.row_id, first.lease_token, provider_message_ts='1700000000.000100')
            second = store.claim_outbox()
            store.fail_outbox(second.row_id, second.lease_token, 'msg_too_long', state='failed')
            store.enqueue_outbox(request_id='synthetic-queued', target='C09SYNTH02', thread_ts='', chunks=('one', 'two'))
            refused = store.claim_outbox()
            store.fail_outbox(refused.row_id, refused.lease_token, 'channel_not_found', state='failed')
        if scenario == 'held_old_problem':  # part 1 refused two days ago, part 2 held by a paused place
            import sqlite3
            store.enqueue_outbox(request_id='synthetic-old', target='C09SYNTH01', thread_ts='', chunks=('one', 'two'))
            first = store.claim_outbox()
            store.fail_outbox(first.row_id, first.lease_token, 'msg_blocked', state='failed')
            store.hold_outbox(store.claim_outbox(), 'Held: Ops room is paused')
            with sqlite3.connect(store.path) as conn:
                conn.execute('UPDATE outbox SET updated_at=updated_at-172800 WHERE id=?', (first.row_id,))
        if scenario == 'problems':
            store.enqueue_outbox(request_id='synthetic-refused', target='C09SYNTH01', thread_ts='', chunks=('hello',))
            refused = store.claim_outbox()
            store.fail_outbox(refused.row_id, refused.lease_token, 'not_in_channel', state='failed')
            store.enqueue_mutation(request_id='synthetic-edit', operation='update_message',
                                   payload={'channel': 'C09SYNTH01', 'ts': '1700000000.000100'})
            edit = store.claim_outbox()
            store.fail_outbox(edit.row_id, edit.lease_token, 'ReadTimeout', state='uncertain')
            for index, note in enumerate(("Held: Check this skill's access in Skills.", 'Held: Ops room is paused')):
                store.enqueue_outbox(request_id=f'synthetic-held-{index}', target=f'C09HOLD0{index}', thread_ts='',
                                     chunks=('later',))
                store.hold_outbox(store.claim_outbox(), note)
    finally:
        store.close()
    return TOKENS


@pytest.fixture
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
    skill = 'slack-bridge'
    root = Path(tempfile.mkdtemp(prefix='slack-bridge-widget-'))
    scenario = getattr(request, 'param', 'healthy')
    api = API(root, seed(scenario, root))
    plugin = load_plugin(skill, api)
    store_module = sys.modules[plugin.BridgeStore.__module__]
    real_probe = store_module._pid_is_alive
    if scenario == 'unknown_liveness':
        # The OS does not answer the process probe (lib/store.py _pid_is_alive returns None on such a Windows query).
        store_module._pid_is_alive = lambda _pid: None
    leases = LeaseGateway(CARDS)
    state = {'posts': [], 'errors': [], 'faults': [], 'skill': skill, 'root': root, 'api': api, 'leases': leases}

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
            if controls(url.path):
                # Lease issue, renewal and revoke as the core answers them; a refused one fails the teardown below.
                status, answer = leases.answer(self.command, url.path, self.headers, payload)
                return self.send(answer, status=status)
            if url.path.startswith(RESOURCES):
                token, _, rest = url.path[len(RESOURCES):].partition('/')
                if token not in leases.held:
                    return self.send({'error': 'widget publication or lease unavailable'}, status=409)
                url = url._replace(path=f'/api/extensions/{skill}/' + rest)
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
        store_module._pid_is_alive = real_probe
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
            assert state['leases'].held == {} and state['leases'].ended  # issued, then revoked on disposal
            assert not state['faults'], state['faults']
        finally:
            browser.close()


def open_card(pw, engine, url, theme, width, messages):
    browser = getattr(pw, engine).launch(headless=True)
    page = browser.new_page(viewport={'width': 1083 if width == 335 else 390, 'height': 1026}, device_scale_factor=2)
    page.on('console', lambda m: messages.append((m.type, m.text)) if m.type == 'error' else None)
    page.on('pageerror', lambda e: messages.append(('pageerror', str(e))))
    page.goto(url + f'/?theme={theme}&width={width}')
    page.wait_for_function('window.ready === true')
    return browser, page


@pytest.mark.parametrize('served', ['problems'], indirect=True)
@pytest.mark.parametrize('engine', ['webkit', 'chromium'])
def test_recent_problems_and_holds_render_as_their_items(served, engine):
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    route = route_status(state, url)
    assert [item['kind'] for item in route['attention']] == ['not_delivered', 'outcome_unknown', 'held']
    messages = []
    with sync_playwright() as pw:
        browser, page = open_card(pw, engine, url, 'dark', 335, messages)
        try:
            frame = page.frame_locator('iframe')
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(route['text'], timeout=15000)
            items = frame.locator('.tc-items .tc-item')
            expect(items).to_have_count(3)
            assert [items.nth(i).get_attribute('data-kind') for i in range(3)] == ['not_delivered', 'outcome_unknown', 'held']
            assert [items.nth(i).get_attribute('data-tone') for i in range(3)] == ['error', 'warn', 'warn']
            expect(items.nth(0)).to_contain_text('not retried')
            expect(items.nth(1)).to_contain_text('has no saved final result')
            expect(items.nth(1)).to_contain_text('Check in Slack whether it took effect')
            held = items.nth(2)
            expect(held).to_contain_text('Ouroboros refused this skill’s access')
            expect(held).to_contain_text('Ops room is paused')
            # The refusal's own error text, not a neighbour's, sits in its fold.
            items.nth(0).locator('summary').click()
            expect(items.nth(0).locator('.tc-raw')).to_have_text('not_in_channel')
            items.nth(1).locator('summary').click()
            expect(items.nth(1).locator('.tc-raw')).to_have_text('ReadTimeout')
            inner = next(f for f in page.frames if f is not page.main_frame)
            assert inner.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.evaluate('async () => { await disposeWidget(); }')
            expect(page.locator('iframe')).to_have_count(0)
        finally:
            browser.close()
    assert not messages, messages
    assert not state['faults'], state['faults']


@pytest.mark.parametrize('served', ['auth_refused'], indirect=True)
@pytest.mark.parametrize('engine,theme,width', [('webkit', 'dark', 335), ('chromium', 'light', 366)])
def test_refused_tokens_render_the_token_advice(served, engine, theme, width):
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    route = route_status(state, url)
    assert route['text'] == "Connection error · Slack refused the bridge's tokens"
    messages = []
    with sync_playwright() as pw:
        browser, page = open_card(pw, engine, url, theme, width, messages)
        try:
            frame = page.frame_locator('iframe')
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(route['text'], timeout=15000)
            item = frame.locator('.tc-items .tc-item').first
            assert item.get_attribute('data-kind') == 'link'
            expect(item).to_contain_text('Slack did not accept the bridge’s tokens')
            expect(item).to_contain_text('Check SLACK_BOT_TOKEN and SLACK_APP_TOKEN')
            item.locator('summary').click()
            expect(item.locator('.tc-raw')).to_have_text('Slack API error: invalid_auth')
            page.evaluate('async () => { await disposeWidget(); }')
        finally:
            browser.close()
    assert not messages, messages


# (scenario, first line, words the card must show, words it must not show) — Details and item folds open.
DEGRADED = [
    ('store_unreadable', "Status unknown · the bridge's queue store can't be read",
     ['unknown: the queue store could not be read'],
     ['Newest queue note', 'since install', 'Since install', 'the system did not answer', 'not checked yet',
      'Known after the bridge first signs in', 'Counts not listed are zero']),
    ('stopped', 'Disconnected · the bridge closed its Slack connection · 2 more need attention',
     ['yes, at the last check', 'skipped: this Ouroboros can’t take them, at the last check',
      'At the bridge’s last check this Ouroboros couldn’t take Slack reactions',
      'It is saved here and tried again when the bridge runs'],
     ['Messages work as usual', 'keeps trying by itself']),
    ('crashed', 'Disconnected · the bridge process is not running',
     ['yes, at the last check', 'passed to Ouroboros, at the last check'], ['Messages work as usual']),
    ('unknown_liveness', 'Connection error · the Slack connection failed · 2 more need attention',
     ['it is not known whether it is still trying', 'not known: the system did not answer',
      'ConnectError: All connection attempts failed', 'It is saved here; the next try uses the same event id'],
     ['at least every 30 seconds', 'tries again by itself', 'keeps trying by itself', 'tried again when the bridge runs']),
    ('lost_reply', 'Connected · 1 Slack event waits for Ouroboros to confirm it',
     ['waiting for Ouroboros to confirm it received it', 'reply lost after Host recorded the turn'],
     ['has not taken', 'to be passed to Ouroboros']),
    ('inbound_failed', 'Connected · 1 Slack event not handled',
     ['while handing it to Ouroboros or following the reply', 'Saved reason', 'inbound · event · not handled'],
     ['Ouroboros’s turn', 'event · not delivered']),
    ('mixed', 'Connected · 1 message not delivered · 1 more needs attention',
     ['Part of it was refused and is not retried', 'send only the refused parts',
      '1 of these messages also has a part refused', '1 of the queued ones with a part not delivered or unknown'],
     ['send again if needed', 'send only what is missing']),
    ('mixed_1b', 'Connected · 2 messages not delivered',
     ['1 of them had a part refused, which is not retried; its other parts are still queued',
      'For that one, fix the cause and wait until the queued parts are sent',
      'its other parts were delivered or have an unknown outcome', 'check slack_receipt and Slack for which parts arrived'],
     ['send again if needed', 'For the others', 'send only what is missing']),
    ('held_old_problem', 'Connected · 1 message held',
     ['an earlier part that was refused or ended with an unknown outcome more than a day ago',
      'Before sending that part again'],
     ['(listed above)']),
    ('host_unanswered', 'Connected · Slack events are not passed to Ouroboros · 1 more needs attention',
     ['The bridge checks again by itself with the next Slack event or send',
      'not yet: Ouroboros didn’t answer the check; asked again with the next event or send',
      'It is asked again with the bridge’s next Slack event or send'],
     ['Connect Slack in Circles', 'turn Slack Bridge off and on', 'when the bridge restarts']),
    ('reactions_unbound', 'Connected · Slack events are not passed to Ouroboros · 1 more needs attention',
     ['can’t take Slack reactions as context, so they are skipped', 'Connect Slack in Circles'],
     ['Messages work as usual']),
    ('token_error', 'Connection error · the bridge stopped after an error',
     ["Illegal header value b'Bearer [redacted]'"],
     ['SYNTHETICTOKEN', 'TAILPIECE42', 'xoxb-4417']),
]
# Details rows whose value must read exactly so (label → value).
ROWS = {'store_unreadable': {'Link': 'unknown: the queue store could not be read',
                             'Socket Mode': 'unknown: the queue store could not be read'}}


@pytest.mark.parametrize('served,line,shown,absent', DEGRADED, indirect=['served'],
                         ids=[case[0] for case in DEGRADED])
def test_each_state_says_only_what_the_store_recorded(served, line, shown, absent, request):
    from playwright.sync_api import sync_playwright, expect
    state, url = served
    assert route_status(state, url)['text'] == line
    messages = []
    with sync_playwright() as pw:
        browser, page = open_card(pw, 'webkit', url, 'dark', 335, messages)
        try:
            frame = page.frame_locator('iframe')
            expect(frame.locator('.tc-status .tc-st-text')).to_have_text(line, timeout=15000)
            frame.locator('details.tc-det > summary').click()
            folds = frame.locator('.tc-items .tc-item summary')
            for index in range(folds.count()):
                folds.nth(index).click()
            inner = next(f for f in page.frames if f is not page.main_frame)
            text = inner.evaluate('document.body.innerText')
            for words in shown:
                assert words in text, (words, text)
            for words in absent:
                assert words not in text, (words, text)
            for label, value in ROWS.get(request.node.callspec.id, {}).items():
                values = inner.evaluate('''(label) => [...document.querySelectorAll('.tc-det dt')]
                    .filter((dt) => dt.textContent === label).map((dt) => dt.nextElementSibling.textContent)''', label)
                assert values == [value], (label, values)
            assert inner.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.evaluate('async () => { await disposeWidget(); }')
        finally:
            browser.close()
    assert not messages, messages
    assert not state['faults'], state['faults']
