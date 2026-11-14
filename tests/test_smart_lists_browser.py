"""Opt-in: the real Smart Lists widget in the real module frame, over the registered routes.

Set OUROBOROS_THEME_CORE_ROOT to a core checkout; Playwright WebKit (the macOS app's
engine) is then required and a missing one fails. Locally, a machine without the Playwright
package or without its Chromium build skips those cases; CI sets SMART_LISTS_REQUIRE_BROWSERS=1
(scripts/ci_checks.py), and then both are required too. The widget source is mounted by the core's own
``mountModuleWidget`` in its sandboxed frame and talks to the skill's registered
tools and routes over a private temporary store. No installed skill, account,
owner data or model is used; every task here is invented.
OUROBOROS_LISTS_EVIDENCE_DIR optionally retains the screenshots and the download.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '/api/extensions/smart_lists/'
# The owner's Widgets page: a 1728x1084 window whose full-width card gives the frame 1211x940.
OWNER = {'viewport': (1728, 1084), 'width': 1211, 'height': 940, 'left': 492, 'top': 66}
PHONE = {'viewport': (390, 760), 'width': 390, 'height': 700, 'left': 0, 'top': 0}
STAGES = ('todo', 'in_progress', 'review', 'done')
# CI runs this suite in a job that installs Playwright and both engines; there a missing one is a failure.
REQUIRED = os.environ.get('SMART_LISTS_REQUIRE_BROWSERS') == '1'
HOST = """<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0}#card{margin:%(top)dpx 0 0 %(left)dpx;width:%(width)dpx}
iframe{display:block;width:100%%;height:var(--widget-frame-height);border:0}</style></head>
<body><section id="card"><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('%(theme)s');
window.opened=[];window.open=(url)=>{window.opened.push(String(url));return null;};
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'smart_lists',ws_prefix:'ext:smart_lists:'},
 {entry:'widget.js',height:%(height)d,appearance:'host'},null,new Set());
window.ready=true;
</script></body></html>"""

WORK = ('Сверить два отчёта', 'Обновить инструкцию', 'Собрать примеры ошибок', 'Проверить перенос встречи',
        'Выбрать данные для сравнения', 'Разобрать замечания к макету', 'Подготовить вопросы к созвону',
        'Записать итоги недели', 'Проверить резервную копию', 'Новый вид списка задач', 'Проверить стенд',
        'Выгрузить снимки для оценки', 'Описать порядок выпуска', 'Сверить тёмную тему', 'Прочитать изменения',
        'Согласовать описание опыта', 'Проверить замеры скорости', 'Подключить источник данных',
        'Обновить подсказки', 'Собрать обзор недели', 'Сохранить архив заметок', 'Разобрать замечания по API')
TAGS = (('бэкенд', 'ревью'), ('дизайн',), ('данные', 'бэкенд'), ('дом',), ('данные',), ('дизайн', 'ревью'),
        ('дом', 'планы'), ('планы',), ('инфра',), ('дизайн', 'бэкенд'), ('инфра', 'бэкенд'))


@pytest.fixture
def lists(tmp_path):
    configured = os.environ.get('OUROBOROS_THEME_CORE_ROOT')
    if not configured:
        pytest.skip('set OUROBOROS_THEME_CORE_ROOT to exercise the real module bridge')
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        if REQUIRED:
            pytest.fail('SMART_LISTS_REQUIRE_BROWSERS is set: the Playwright package is required')
        pytest.skip('install Playwright to exercise the real module bridge')
    from starlette.datastructures import QueryParams
    core = Path(configured).resolve()
    assert (core / 'web/modules/widget_module.js').is_file()
    spec = importlib.util.spec_from_file_location('lists_browser_support',
        ROOT / 'skills/smart_lists/tests/test_smart_lists.py')
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    api = support.FakePluginAPI(tmp_path / 'state')
    support.load_plugin().register(api)
    # calls: every skill request the frame made; faults: one-shot outages a test queues;
    # errors: a route handler that raised; disconnects: answers the browser closed before reading;
    # answered: every skill request of the frame whose answer was written back, in order.
    state = {'calls': [], 'faults': [], 'errors': [], 'widget_faults': [], 'disconnects': [], 'answered': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content_type='application/json', status=200):
            if not isinstance(body, bytes):
                body = (body if isinstance(body, str) else json.dumps(body)).encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
                path = urlsplit(self.path).path
                if path.startswith(PREFIX) and self.headers.get('X-Fixture-Read') != '1':
                    state['answered'].append(('GET' if self.command == 'HEAD' else self.command, path[len(PREFIX):]))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # The browser went away (a page closing with a request in flight): the answer
                # has no reader. That is transport, not a skill failure, and is only recorded.
                state['disconnects'].append(f'{self.command} {self.path}')
                self.close_connection = True

        def dispatch(self):
            url = urlsplit(self.path)
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0') or 0))
            if url.path == '/':
                shape = {**OWNER, 'theme': 'light', **{k: v[0] for k, v in parse_qs(url.query).items()}}
                return self.send(HOST % {**shape, **{k: int(shape[k]) for k in ('width', 'height', 'left', 'top')}},
                                 'text/html')
            if url.path.startswith('/api/widgets/asset-leases'):
                # Host-publication fixture; execute the real frame/bridge, not lease policy.
                return self.send({'token': 'fixture', 'base_url': '/api/widget-resources/fixture/',
                                  'renew_after': 60, 'expires_in': 600, 'scope': 'routes'})
            if url.path == '/api/widgets/fault':
                state['widget_faults'].append(raw.decode('utf-8', 'replace'))
                return self.send({'ok': True})
            if url.path.startswith('/static/'):
                web = core / 'web'
                source = (web / url.path[len('/static/'):]).resolve()
                if source.is_relative_to(web) and source.is_file():
                    return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or 'text/javascript')
            if url.path == PREFIX + 'module/widget.js':
                return self.send((ROOT / 'skills/smart_lists/widget.js').read_bytes(), 'text/javascript')
            name = url.path[len(PREFIX):] if url.path.startswith(PREFIX) else ''
            method = 'GET' if self.command == 'HEAD' else self.command  # the host probes a download first
            if name in api.routes and method in api.routes[name]['methods']:
                body = json.loads(raw or b'{}')
                # The test's own reads are neither the widget's calls nor meant to meet a queued fault.
                own = self.headers.get('X-Fixture-Read') == '1'
                if not own:
                    state['calls'].append((method, name, body, url.query))
                fault = None if own else next((f for f in state['faults'] if f[0] == method and f[1] == name), None)
                if fault:
                    state['faults'].remove(fault)
                    if isinstance(fault[2], dict):  # a typed answer the store gives in a state a test cannot make
                        return self.send(fault[2])
                    if fault[2] == 'slow':  # the store answers, late enough for the owner to keep typing
                        time.sleep(1.5)
                    if fault[2] == 'held':  # a late answer that still fits the host's one-second dispose wait
                        time.sleep(0.6)
                    if fault[2] == 'stalled':  # away long enough to leave the page meanwhile, and never applied
                        time.sleep(1.5)
                        return self.send({'error': 'synthetic outage'}, status=503)
                    if fault[2] == 'lost':  # never reached the store
                        return self.send({'error': 'synthetic outage'}, status=503)
                    if fault[2] == 'refused':
                        return self.send({'ok': False, 'error': 'Synthetic refusal; nothing changed'})
                    if fault[2] == 'late':  # no answer now; the store applies it after the widget's check read
                        def apply_later(body=body, query=url.query, route=api.routes[name]['handler']):
                            time.sleep(0.8)
                            try:
                                request = support.FakeRequest(body, method=method)
                                request.query_params = QueryParams(query)
                                result = route(request)
                                if asyncio.iscoroutine(result):
                                    asyncio.run(result)
                            except Exception as exc:
                                state['errors'].append(repr(exc))
                        threading.Thread(target=apply_later, daemon=True).start()
                        return self.send({'error': 'synthetic outage'}, status=503)
                # Only the route and its answer's serialization are the skill's; writing the
                # answer to the socket happens after this block, in send().
                try:
                    request = support.FakeRequest(body, method=method)
                    request.query_params = QueryParams(url.query)
                    result = api.routes[name]['handler'](request)
                    if asyncio.iscoroutine(result):
                        result = asyncio.run(result)
                    if hasattr(result, 'body'):
                        answer, status = bytes(result.body), result.status_code
                    else:
                        answer, status = json.dumps(result).encode('utf-8'), 200
                except Exception as exc:
                    state['errors'].append(repr(exc))
                    return self.send({'error': str(exc)}, status=500)
                if fault and fault[2] == 'unanswered':  # the store applied it, the answer was lost
                    return self.send({'error': 'synthetic outage'}, status=503)
                return self.send(answer, status=status)
            return self.send({'error': 'not found'}, status=404)

        do_GET = dispatch
        do_HEAD = dispatch
        do_POST = dispatch
        do_DELETE = dispatch

    def tool(name, /, **args):
        result = api.call_tool(name, **args)
        assert result['ok'], result
        return result

    tool('store', action='init')
    tool('group', action='create', name='Inbox')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Lists(tool, state, f'http://127.0.0.1:{server.server_port}', tmp_path, api)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not state['errors'], state['errors']
        assert not state['widget_faults'], state['widget_faults']


class Lists:
    """One private store behind the registered tools and routes, and browsers pointed at it."""

    def __init__(self, tool, state, url, tmp_path, api):
        self.tool, self.state, self.url, self.tmp, self.api = tool, state, url, tmp_path, api
        self.evidence = Path(os.environ.get('OUROBOROS_LISTS_EVIDENCE_DIR', str(tmp_path / 'evidence')))
        self.evidence.mkdir(parents=True, exist_ok=True)
        self._serial = 0

    def add(self, items):
        """Add invented tasks ({text, stage?, description?, tags?, turn?, references?, archived?}); return ids."""
        ids = []
        for start in range(0, len(items), 5):  # small batches: a tool result is capped and would drop rows
            self._serial += 1
            batch = items[start:start + 5]
            wire = []
            for item in batch:
                details = {key: item[key] for key in ('tags', 'turn', 'references', 'note', 'next_step') if key in item}
                wire.append({'text': item['text'], **{key: item[key] for key in ('stage', 'description') if key in item},
                             **({'details': details} if details else {})})
            added = self.tool('add', group='Inbox', items=wire, request_id=f'seed-{self._serial}')['added']
            assert len(added) == len(batch), 'the add result was shortened; seed fewer or smaller items per call'
            ids += [entry['id'] for entry in added]
            for item, entry in zip(batch, added):
                if item.get('archived'):
                    self._serial += 1
                    self.tool('archive', entry_id=entry['id'], archived=True, expected={'archived_at': None},
                              request_id=f'seed-archive-{self._serial}')
        return ids

    def entry(self, entry_id):
        """The stored entry, read through the registered route the widget also uses."""
        result = self.read('entry', f'entry_id={entry_id}')
        assert result['ok'], result
        return result['entry']

    def read(self, name, query=''):
        import urllib.request
        request = urllib.request.Request(f'{self.url}{PREFIX}{name}?{query}', headers={'X-Fixture-Read': '1'})
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def update(self, entry_id, **fields):
        self._serial += 1
        return self.tool('update', entry_id=entry_id, request_id=f'agent-{self._serial}', **fields)

    def calls(self, method, name):
        return [call for call in self.state['calls'] if call[0] == method and call[1] == name]

    def answered(self, method, name):
        """How many of the frame's requests to this route have had their answer written back."""
        return self.state['answered'].count((method, name))

    def fail(self, method, name, mode):
        self.state['faults'].append((method, name, mode))

    def open(self, pw, engine, shape=OWNER, theme='light'):
        try:
            browser = getattr(pw, engine).launch(headless=True)
        except Exception as exc:
            # WebKit is the engine the owner's app uses, so without it this suite proves nothing:
            # that is a failure. Only a Chromium build this machine never installed is skipped.
            if engine == 'chromium' and not REQUIRED and "Executable doesn't exist" in str(exc):
                pytest.skip('Playwright chromium is not installed')
            raise
        width, height = shape['viewport']
        context = browser.new_context(viewport={'width': width, 'height': height}, accept_downloads=True,
                                      timezone_id='UTC', locale='ru-RU')
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        # The browser logs each outage a test injects (503); any other console error is the widget's.
        page.on('console', lambda message: errors.append(message.text)
                if message.type == 'error' and '503' not in message.text else None)
        query = '&'.join(f'{key}={shape[key]}' for key in ('width', 'height', 'left', 'top')) + f'&theme={theme}'
        return Session(self, browser, context, page, errors, f'{self.url}/?{query}')


class Session:
    def __init__(self, lists, browser, context, page, errors, address):
        self.lists, self.browser, self.context, self.page = lists, browser, context, page
        self.errors, self.address = errors, address

    def mount(self):
        from playwright.sync_api import expect
        self.page.goto(self.address)
        self.page.wait_for_function('window.ready === true')
        frame = self.page.locator('iframe').element_handle().content_frame()
        frame.wait_for_selector('.column')
        expect(frame.locator('.column[data-stage="todo"] .count')).not_to_be_empty()
        return frame

    def refresh(self, frame):
        """What the 30-second poll does, now: the frame reads the store again."""
        frame.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

    def shot(self, name):
        self.page.wait_for_timeout(150)  # WebKit settles widths after the first paint
        self.page.screenshot(path=str(self.lists.evidence / name))

    def close(self):
        try:
            assert not self.errors, self.errors
        finally:
            self.context.close()
            self.browser.close()


def balanced():
    """Twenty-two invented tasks, 9/5/3/5 across the stages, and four archived ones."""
    items = []
    for index, text in enumerate(WORK):
        stage = STAGES[0 if index < 9 else 1 if index < 14 else 2 if index < 17 else 3]
        item = {'text': text, 'stage': stage, 'tags': list(TAGS[index % len(TAGS)]),
                'description': f'Цель: {text.lower()}.\n\nКонтекст и ожидаемый результат записаны здесь.'}
        if index % 3 == 0:
            item['references'] = [
                {'label': 'Обсуждение задачи', 'target': f'https://chat.example.org/archives/C{index}/p100'},
                {'label': 'Уточнение требований', 'target': f'https://t.me/c/100/{index}'},
                {'label': 'Изменения кода', 'target': f'https://code.example.org/team/app/pull/{index + 1}'}]
            item['turn'] = 'owner'
        items.append(item)
    for index, text in enumerate(('Прежний вид списка', 'Закрытый вопрос', 'Старый выпуск', 'Отменённая проба')):
        items.append({'text': text, 'stage': 'todo' if index == 3 else 'done', 'archived': True,
                      'tags': ['дизайн' if index % 2 else 'бэкенд', 'архив']})
    return items


def chips(card):
    """The tag tokens the card shows, in order."""
    return card.locator('.tagchip > span').all_inner_texts()


def add_tag(card, text):
    box = card.get_by_label('Теги')
    box.fill(text)
    box.press('Enter')


def column(frame, stage):
    return frame.locator(f'.column[data-stage="{stage}"]')


def shown(frame, stage=None):
    scope = column(frame, stage) if stage else frame
    return scope.locator('.task').evaluate_all('nodes => nodes.map(node => node.dataset.id)')


ENGINES = pytest.mark.parametrize('engine', ['webkit', 'chromium'])


@ENGINES
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_owner_frame_shows_the_whole_board_and_one_tag_filters_every_column(lists, engine, theme):
    from playwright.sync_api import sync_playwright, expect
    ids = lists.add(balanced())
    by_tag = lambda tag: [ids[i] for i in range(22) if tag in TAGS[i % len(TAGS)]]
    with sync_playwright() as pw:
        session = lists.open(pw, engine, OWNER, theme)
        try:
            frame = session.mount()
            assert frame.evaluate('[innerWidth, innerHeight]') == [1211, 940]
            expect(frame.locator('html')).to_have_attribute('data-theme', theme)
            expect(frame.locator('.column')).to_have_count(4)
            expect(frame.locator('.task')).to_have_count(22)
            assert [column(frame, stage).locator('.count').inner_text() for stage in STAGES] == ['9', '5', '3', '5']
            expect(frame.get_by_role('button', name='Архив 4', exact=True)).to_be_visible()
            # The whole board at a glance: no column scrolls and nothing is cut off.
            for stage in STAGES:
                cards = column(frame, stage).locator('.cards')
                assert cards.evaluate('node => node.scrollHeight <= node.clientHeight'), stage
                expect(column(frame, stage).locator('.colfoot')).to_have_text('')
            assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert frame.locator('.task .title').evaluate_all(
                'nodes => nodes.every(node => parseFloat(getComputedStyle(node).fontSize) >= 14)')
            session.shot(f'board-{engine}-{theme}.png')

            chips = frame.locator('#filters')
            chips.get_by_role('button', name='бэкенд', exact=True).click()
            expect(frame.locator('.task')).to_have_count(len(by_tag('бэкенд')))
            assert sorted(shown(frame)) == sorted(by_tag('бэкенд'))
            expect(frame.locator('.column')).to_have_count(4)
            expect(chips.locator('.hint')).to_contain_text(f'Только с тегом бэкенд · {len(by_tag("бэкенд"))} задач')
            for stage, total in zip(STAGES, (9, 5, 3, 5)):
                here = len(shown(frame, stage))
                expect(column(frame, stage).locator('.count')).to_have_text(f'{here} из {total}')
            assert frame.locator('.task').evaluate_all(
                "nodes => nodes.every(node => [...node.querySelectorAll('.tag.on')].some(tag => tag.textContent === 'бэкенд'))")
            session.shot(f'filter-{engine}-{theme}.png')

            chips.get_by_role('button', name='ревью', exact=True).click()
            both = sorted(set(by_tag('бэкенд')) & set(by_tag('ревью')))
            expect(frame.locator('.task')).to_have_count(len(both))
            assert sorted(shown(frame)) == both
            expect(chips.locator('.hint')).to_contain_text('со всеми выбранными тегами сразу (И)')
            empty = [stage for stage in STAGES if not shown(frame, stage)]
            assert empty, 'the two-tag filter should leave a stage empty in this fixture'
            expect(column(frame, empty[0]).locator('.empty')).to_have_text('Нет задач')
            chips.get_by_role('button', name='Все', exact=True).click()
            expect(frame.locator('.task')).to_have_count(22)

            frame.get_by_label('Найти задачу').fill('такого здесь нет')
            expect(frame.locator('.task')).to_have_count(0)
            expect(frame.locator('.column')).to_have_count(4)
            expect(chips.locator('.hint')).to_have_text('Найдено: 0 задач')
            frame.get_by_label('Найти задачу').fill('')
            expect(frame.locator('.task')).to_have_count(22)

            other = 'dark' if theme == 'light' else 'light'
            session.page.evaluate(f"ouroTheme.set('{other}')")
            expect(frame.locator('html')).to_have_attribute('data-theme', other)
        finally:
            session.close()


def open_card(frame, entry_id):
    from playwright.sync_api import expect
    frame.locator(f'.task[data-id="{entry_id}"]').click()
    card = frame.locator('dialog')
    expect(card).to_be_visible()
    expect(card.get_by_role('button', name='Готово')).to_be_enabled()
    # Visibility alone also passes for the 44px collapsed body in macOS WKWebView.
    frame.wait_for_timeout(150)
    geometry = card.evaluate('''dialog => {
        const body = dialog.querySelector('.dialogbody').getBoundingClientRect();
        const head = dialog.querySelector('.dialoghead').getBoundingClientRect();
        const foot = dialog.querySelector('.dialogfoot').getBoundingClientRect();
        return {visible: Math.min(body.bottom, foot.top, innerHeight)
                    - Math.max(body.top, head.bottom, 0),
                viewport: innerHeight, top: head.top, bottom: foot.bottom};
    }''')
    assert geometry['visible'] >= min(240, geometry['viewport'] * .3), geometry
    assert geometry['top'] >= 0 and geometry['bottom'] <= geometry['viewport'], geometry
    # Playwright's engines lay out even the old `flex:1` body correctly; only the app's native WKWebView
    # collapsed it to 44px. That needed a content-sized basis, so the rule itself is held here.
    assert card.evaluate("dialog => getComputedStyle(dialog.querySelector('.dialogbody')).flexBasis") == 'auto'
    return card


@ENGINES
def test_card_saves_only_what_changed_and_archive_keeps_the_task_whole(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    ids = lists.add(balanced())
    first = ids[0]
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, first)
            expect(card.get_by_label('Название задачи')).to_have_value('Сверить два отчёта')
            expect(card.get_by_label('Описание')).to_have_value(lists.entry(first)['description'])
            expect(card.locator('.ref')).to_have_count(3)
            assert card.locator('.ref b').all_inner_texts() == ['Ссылка', 'Telegram', 'PR']
            session.shot(f'card-{engine}-light.png')
            # A link leaves through the host bridge, never by navigating the frame.
            card.locator('.ref a').nth(2).click()
            session.page.wait_for_function('window.opened.length === 1')
            assert session.page.evaluate('window.opened') == ['https://code.example.org/team/app/pull/1']

            text = 'Сверить цифры за октябрь.\nИсточник: https://docs.example.org/report.'
            card.get_by_label('Описание').fill(text)
            expect(card.locator('.links a')).to_have_text('↗ docs.example.org/report')
            assert chips(card) == ['бэкенд', 'ревью']
            card.get_by_role('button', name='Убрать тег: ревью').click()
            add_tag(card, 'Отчёты')
            add_tag(card, 'отчёты')  # the same tag in another case is already there
            assert chips(card) == ['бэкенд', 'Отчёты']
            expect(card.get_by_label('Теги')).to_have_value('')
            card.get_by_label('Адрес ссылки').fill('просто слова')
            card.get_by_role('button', name='Добавить').click()
            expect(card.locator('.addrow + .note')).to_contain_text('https://')
            expect(card.locator('.ref')).to_have_count(3)
            card.get_by_label('Адрес ссылки').fill('https://code.example.org/team/app/merge_requests/7')
            card.get_by_label('Подпись ссылки, необязательно').fill('Правка расчёта')
            card.get_by_label('Адрес ссылки').press('Enter')
            expect(card.locator('.ref')).to_have_count(4)
            assert card.locator('.ref b').all_inner_texts()[-1] == 'MR'
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Есть несохранённые правки')
            assert lists.entry(first)['description'].startswith('Цель'), 'nothing is written before Готово'
            assert not lists.calls('POST', 'edit')

            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(first)
            assert stored['description'] == text
            assert stored['details']['tags'] == ['бэкенд', 'Отчёты']
            assert stored['details']['references'][3] == {
                'label': 'Правка расчёта', 'target': 'https://code.example.org/team/app/merge_requests/7'}
            assert stored['details']['turn'] == 'owner' and stored['text'] == 'Сверить два отчёта'
            (edit,) = lists.calls('POST', 'edit')
            assert set(edit[2]['changes']) == {'description', 'details'}
            assert set(edit[2]['changes']['details']) == {'tags', 'references'}
            assert edit[2]['expected']['description'].startswith('Цель')
            assert edit[2]['expected']['details']['tags'] == ['бэкенд', 'ревью']
            assert len(edit[2]['expected']['details']['references']) == 3
            tile = frame.locator(f'.task[data-id="{first}"]')
            expect(tile.locator('.linkcount')).to_have_text('↗ 4')
            expect(tile.locator('.tag').nth(1)).to_have_text('Отчёты')
            assert frame.evaluate('document.activeElement.dataset.id') == first, 'focus returns to the card'

            # The stage field moves the task at once and says so.
            card = open_card(frame, first)
            card.get_by_label('Стадия').select_option('review')
            expect(column(frame, 'review').locator(f'.task[data-id="{first}"]')).to_have_count(1)
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Сохранено')
            assert lists.entry(first)['stage'] == 'review'
            assert lists.calls('POST', 'edit')[-1][2]['changes'] == {'stage': 'review'}
            assert lists.calls('POST', 'edit')[-1][2]['expected'] == {'stage': 'todo'}

            card.get_by_role('button', name='В архив').click()
            expect(card).to_be_hidden()
            expect(frame.locator(f'.task[data-id="{first}"]')).to_have_count(0)
            expect(frame.locator('#toast')).to_contain_text('Перемещено в архив')
            archived = lists.entry(first)
            assert archived['archived_at'] and archived['stage'] == 'review' and archived['description'] == text
            assert len(archived['details']['references']) == 4
            frame.get_by_role('button', name='Архив 5', exact=True).click()
            expect(frame.locator('.archrow')).to_have_count(5)
            row = frame.locator(f'.archrow[data-id="{first}"]')
            expect(row.locator('small')).to_have_text('Review')
            session.shot(f'archive-{engine}-light.png')
            row.locator('.archtitle').click()
            expect(card.locator('.badge')).to_have_text('В архиве')
            expect(card.locator('.ref')).to_have_count(4)
            expect(card.get_by_role('button', name='Вернуть на доску')).to_be_visible()
            card.get_by_label('Закрыть').click()
            expect(card).to_be_hidden()
            row.get_by_role('button', name='Вернуть').click()
            expect(frame.locator('.archrow')).to_have_count(4)
            expect(frame.locator('#toast')).to_contain_text('Возвращено в Review')
            frame.get_by_role('button', name='Доска', exact=True).click()
            expect(column(frame, 'review').locator(f'.task[data-id="{first}"]')).to_have_count(1)
            restored = lists.entry(first)
            assert restored['archived_at'] is None and restored['stage'] == 'review' and restored['description'] == text

            # A task made under a tag filter is created once and keeps that tag, so it stays in view.
            frame.locator('#filters').get_by_role('button', name='дом', exact=True).click()
            before = sum('дом' in TAGS[index % len(TAGS)] for index in range(22))
            expect(frame.locator('.task')).to_have_count(before)
            frame.get_by_role('button', name='+ Задача').click()
            expect(card.locator('.dialoghead .hint')).to_have_text('Новая задача')
            assert chips(card) == ['дом']
            expect(card.get_by_label('Теги')).to_have_value('')
            card.get_by_role('button', name='Создать').click()
            expect(card.locator('.dialogfoot .note')).to_have_text('У задачи должно быть название.')
            assert not lists.calls('POST', 'add')
            card.get_by_label('Название задачи').fill('Купить лампу')
            card.get_by_label('Название задачи').press('Enter')
            expect(card).to_be_hidden()
            expect(frame.locator('.task')).to_have_count(before + 1)
            (add,) = lists.calls('POST', 'add')
            assert add[2]['text'] == 'Купить лампу'
            made = frame.locator('.task', has_text='Купить лампу').get_attribute('data-id')
            assert lists.entry(made)['details']['tags'] == ['дом'] and lists.entry(made)['stage'] == 'todo'
        finally:
            session.close()


@ENGINES
def test_draft_outlives_refresh_and_a_concurrent_change_is_shown_not_overwritten(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    ids = lists.add(balanced())
    target = ids[1]
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            draft = card.get_by_label('Описание')
            draft.fill('Черновик, который ещё не сохранён.')
            draft.evaluate('node => { node.focus(); node.setSelectionRange(8, 8); }')
            # Meanwhile the agent adds a source and takes the next step: fields this form did not touch.
            thread = {'label': 'Тред с уточнением', 'target': 'https://chat.example.org/archives/C1/p200'}
            lists.update(target, details={'references': [thread], 'turn': 'agent'})
            session.refresh(frame)
            expect(card.locator('.ref')).to_have_count(1)
            expect(card.get_by_label('Чей следующий ход')).to_have_value('agent')
            expect(draft).to_have_value('Черновик, который ещё не сохранён.')
            assert frame.evaluate('[document.activeElement.id, document.activeElement.selectionStart]') == [
                'sl-description', 8], 'focus and caret stay where the owner was typing'
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(target)
            assert stored['description'] == 'Черновик, который ещё не сохранён.'
            assert stored['details']['references'] == [thread] and stored['details']['turn'] == 'agent'
            assert lists.calls('POST', 'edit')[-1][2]['changes'] == {'description': 'Черновик, который ещё не сохранён.'}

            # The same field changed in both places: seen at the next refresh, both values on screen.
            card = open_card(frame, target)
            draft.fill('Моя версия описания')
            lists.update(target, description='Версия агента')
            session.refresh(frame)
            note = card.locator('.field', has=frame.locator('#sl-description')).locator('.note')
            expect(note).to_contain_text('изменили в другом месте')
            expect(note.locator('.theirs')).to_have_text('Версия агента')
            expect(draft).to_have_value('Моя версия описания')
            session.shot(f'conflict-{engine}-light.png')
            sent = len(lists.calls('POST', 'edit'))
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сначала выбери')
            assert len(lists.calls('POST', 'edit')) == sent and lists.entry(target)['description'] == 'Версия агента'
            note.get_by_role('button', name='Оставить моё').click()
            expect(note).to_be_hidden()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['description'] == 'Моя версия описания'
            assert lists.calls('POST', 'edit')[-1][2]['expected'] == {'description': 'Версия агента'}

            # No refresh in between: the store itself refuses the stale write and nothing is applied.
            card = open_card(frame, target)
            add_tag(card, 'моё')
            add_tag(card, 'срочно')
            card.get_by_label('Срок').fill('к пятнице')
            lists.update(target, details={'tags': ['агент']})
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Не сохранено: часть полей изменили в другом месте')
            tags_note = card.locator('.field', has=frame.locator('#sl-tags')).locator('.note')
            expect(tags_note.locator('.theirs')).to_have_text('«агент»')
            stored = lists.entry(target)
            assert stored['details']['tags'] == ['агент'] and stored['due'] is None, 'a refused write changes nothing'
            assert chips(card) == ['дизайн', 'моё', 'срочно']
            expect(card.get_by_label('Срок')).to_have_value('к пятнице')
            tags_note.get_by_role('button', name='Взять это значение').click()
            assert chips(card) == ['агент']
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(target)
            assert stored['details']['tags'] == ['агент'] and stored['due']['raw'] == 'к пятнице'
            assert lists.calls('POST', 'edit')[-1][2]['changes'] == {'due': 'к пятнице'}

            # A link added here while the agent rewrites the list: both survive, after a visible second step.
            card = open_card(frame, target)
            card.get_by_label('Адрес ссылки').fill('https://code.example.org/team/app/pull/42')
            card.get_by_role('button', name='Добавить').click()
            second = {'label': 'Протокол встречи', 'target': 'https://docs.example.org/minutes'}
            lists.update(target, details={'references': [thread, second]})
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.refs + .note')).to_contain_text('перенесены поверх нового списка')
            expect(card.locator('.ref')).to_have_count(3)
            assert lists.entry(target)['details']['references'] == [thread, second]
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            targets = [ref['target'] for ref in lists.entry(target)['details']['references']]
            assert targets == [thread['target'], second['target'], 'https://code.example.org/team/app/pull/42']
        finally:
            session.close()


@ENGINES
def test_unanswered_write_is_checked_first_and_never_repeated_under_a_new_id(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    ids = lists.add(balanced())
    target = ids[1]
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            edits = lambda: lists.calls('POST', 'edit')
            # The store applied the write but its answer was lost: the widget reads, finds it, and closes.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Первая правка')
            lists.fail('POST', 'edit', 'unanswered')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 1 and lists.entry(target)['description'] == 'Первая правка'

            # The write never arrived: the check read finds the old value, which does not prove it never will.
            # The note says only that the read did not confirm the write (the case with someone else's values
            # is below). The draft stays, nothing is sent again by itself, and «Готово» repeats the very request.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Вторая правка')
            lists.fail('POST', 'edit', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('проверочное чтение не подтвердило запись')
            expect(card.locator('.field .note:not([hidden])')).to_have_count(0)
            expect(card.get_by_label('Описание')).to_have_value('Вторая правка')
            session.page.wait_for_timeout(400)
            assert len(edits()) == 2 and lists.entry(target)['description'] == 'Первая правка'
            session.shot(f'unanswered-{engine}-light.png')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 3 and lists.entry(target)['description'] == 'Вторая правка'
            assert edits()[2][2] == edits()[1][2], 'the unconfirmed write is repeated as itself'

            # Neither the write nor the check got through: the next attempt is the very same request.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Третья правка')
            lists.fail('POST', 'edit', 'unanswered')
            lists.fail('GET', 'entry', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            expect(card).to_be_visible()
            assert len(edits()) == 4
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 5 and edits()[4][2] == edits()[3][2], 'same request id and body'
            assert lists.entry(target)['description'] == 'Третья правка'

            # Unconfirmed, then typed back to what the store held before it. The unknown request is still repeated
            # as itself (it may have applied); what the screen shows is then saved as a new request.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Отменённая правка')
            lists.fail('POST', 'edit', 'unanswered')
            lists.fail('GET', 'entry', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            card.get_by_label('Описание').fill('Третья правка')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 8 and edits()[6][2] == edits()[5][2], 'the unknown write is repeated as itself'
            assert edits()[7][2]['changes'] == {'description': 'Третья правка'}
            assert edits()[7][2]['expected'] == {'description': 'Отменённая правка'}
            assert edits()[7][2]['request_id'] != edits()[5][2]['request_id']
            assert lists.entry(target)['description'] == 'Третья правка'

            # Creating: an unanswered request is repeated as itself, so there is still one task.
            frame.get_by_role('button', name='+ Задача').click()
            card.get_by_label('Название задачи').fill('Заказать пропуск')
            lists.fail('POST', 'add', 'unanswered')
            card.get_by_role('button', name='Создать').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Создание не подтверждено')
            card.get_by_role('button', name='Создать').click()
            expect(card).to_be_hidden()
            adds = lists.calls('POST', 'add')
            assert len(adds) == 2 and adds[0][2] == adds[1][2]
            expect(frame.locator('.task', has_text='Заказать пропуск')).to_have_count(1)

            # A refusal is not an outage: it is shown as one and the form is kept.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Четвёртая правка')
            lists.fail('POST', 'edit', 'refused')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_have_text('Не сохранено: Synthetic refusal; nothing changed.')
            assert lists.entry(target)['description'] == 'Третья правка'
            card.get_by_label('Закрыть').click()
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Правки не сохранены.')
            card.get_by_role('button', name='Не сохранять').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['description'] == 'Третья правка'

            # The write never arrived and the agent changed every sent field meanwhile. The check read shows
            # neither what was sent nor what the form started from, so the note claims only that the read did
            # not confirm the write; the fields show the other values, and nothing is sent again by itself.
            from starlette.responses import JSONResponse
            real = lists.api.routes['edit']['handler']

            def changed_elsewhere_then_unanswered(request):
                lists.update(target, description='Версия агента', due='к понедельнику')
                return JSONResponse({'error': 'synthetic outage'}, status_code=503)

            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Пятая правка')
            card.get_by_label('Срок').fill('к среде')
            boards = lambda: len(lists.calls('GET', 'board'))
            lists.api.routes['edit']['handler'] = changed_elsewhere_then_unanswered
            try:
                card.get_by_role('button', name='Готово').click()
                expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            finally:
                lists.api.routes['edit']['handler'] = real
            expect(card.locator('.dialogfoot .note')).to_contain_text('проверочное чтение не подтвердило запись')
            expect(card.locator('.dialogfoot .note')).not_to_contain_text('прежние значения')
            expect(card.locator('.field .note:not([hidden]) .theirs')).to_have_text(['Версия агента', 'к понедельнику'])
            expect(card.get_by_label('Описание')).to_have_value('Пятая правка')
            expect(card.get_by_label('Срок')).to_have_value('к среде')
            session.page.wait_for_timeout(400)
            entry = lists.entry(target)
            assert (entry['description'], entry['due']['raw']) == ('Версия агента', 'к понедельнику')
            assert len(edits()) == 10, 'nothing is sent again by itself'
            for _ in range(2):
                card.get_by_role('button', name='Оставить моё').first.click()
            expect(card.get_by_role('button', name='Оставить моё')).to_have_count(0)
            # Keeping mine repeats the unknown request as itself; the store refuses it over the other values,
            # and only the next «Готово» saves the form as a new request against what the store now holds.
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Не сохранено: задачу изменили в другом месте')
            assert len(edits()) == 11 and edits()[10][2] == edits()[9][2], 'same request id and body'
            assert lists.entry(target)['description'] == 'Версия агента'
            before = boards()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 12 and edits()[11][2]['request_id'] != edits()[9][2]['request_id']
            assert edits()[11][2]['changes'] == {'description': 'Пятая правка', 'due': 'к среде'}
            assert edits()[11][2]['expected'] == {'description': 'Версия агента', 'due': 'к понедельнику'}
            entry = lists.entry(target)
            assert (entry['description'], entry['due']['raw']) == ('Пятая правка', 'к среде')

            # A failed board read keeps the last board on screen and says it is stale. The save above
            # refreshed the board itself: the outage is queued only once that read was answered, so no
            # read but the poll's own meets it. A poll while that refresh still reads is skipped, so it is
            # asked for again until a board read follows the outage.
            wait_until(lambda: boards() > before and lists.answered('GET', 'board') == boards(),
                       "the save's own board read was answered")
            lists.fail('GET', 'board', 'refused')

            def polled():
                if not lists.state['faults']:
                    return True  # a board read met the outage, and only a poll reads the board now
                session.refresh(frame)
                return False
            wait_until(polled, 'a poll read the board and met the outage')
            expect(frame.locator('.alert')).to_contain_text('Доска не обновлена: показаны прежние данные')
            expect(frame.locator('.task')).to_have_count(23)
            frame.locator('.alert').get_by_role('button', name='Повторить').click()
            expect(frame.locator('.alert')).to_be_hidden()
        finally:
            session.close()


def fully_visible(frame, stage):
    return column(frame, stage).locator('.cards').evaluate(
        """list => { const box = list.getBoundingClientRect();
        return [...list.querySelectorAll('.task')].filter(node => { const r = node.getBoundingClientRect();
          return r.top >= box.top - 1 && r.bottom <= box.bottom + 1; }).length; }""")


@ENGINES
def test_a_long_stage_keeps_its_real_count_says_what_is_hidden_and_loads_the_rest(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    # Skewed, as real backlogs are: 35 to do, 2 in progress, nothing in review, 3 done; 30 distinct tags.
    plan = ['todo'] * 35 + ['in_progress'] * 2 + ['done'] * 3
    lists.add([{'text': f'{WORK[i % len(WORK)]} — шаг {i + 1}', 'stage': stage,
                'tags': [f'тема-{i % 30 + 1}', 'общий']} for i, stage in enumerate(plan)])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            assert [column(frame, stage).locator('.count').inner_text() for stage in STAGES] == ['35', '2', '0', '3']
            expect(column(frame, 'todo').locator('.task')).to_have_count(35)
            expect(column(frame, 'review').locator('.empty')).to_have_text('Нет задач')
            seen = fully_visible(frame, 'todo')
            assert 0 < seen < 35
            expect(column(frame, 'todo').locator('.colfoot')).to_have_text(f'Видно {seen} из 35 · прокрутите колонку')
            expect(column(frame, 'in_progress').locator('.colfoot')).to_have_text('')
            assert frame.locator('.task .title').first.evaluate('node => getComputedStyle(node).fontSize') == '14px'
            # One row of tags; the rest are one press away and a chosen one never hides.
            chips = frame.locator('#filters')
            more = chips.get_by_role('button', name='Ещё теги', exact=False)
            hidden = int(more.inner_text().split('·')[1])
            assert hidden > 0 and chips.locator('[data-tag]:visible').count() == 31 - hidden
            assert chips.evaluate('node => node.getBoundingClientRect().height') < 40
            session.shot(f'skewed-{engine}-light.png')
            more.click()
            expect(chips.locator('[data-tag]:visible')).to_have_count(31)
            chips.get_by_role('button', name='тема-30', exact=True).click()
            expect(frame.locator('.task')).to_have_count(1)
            chips.get_by_role('button', name='Меньше тегов').click()
            expect(chips.get_by_role('button', name='тема-30', exact=True)).to_be_visible()
            chips.get_by_role('button', name='Все', exact=True).click()
            expect(column(frame, 'todo').locator('.task')).to_have_count(35)

            # Scroll position survives a refresh that changes nothing and one that adds work.
            cards = column(frame, 'todo').locator('.cards')
            cards.evaluate('node => { node.scrollTop = 420; }')
            session.refresh(frame)
            session.page.wait_for_timeout(300)
            assert cards.evaluate('node => node.scrollTop') == 420
            lists.add([{'text': f'Дополнительная задача {i + 1}', 'stage': 'todo'} for i in range(95)])
            session.refresh(frame)
            expect(column(frame, 'todo').locator('.count')).to_have_text('130')
            expect(column(frame, 'todo').locator('.task')).to_have_count(100)
            assert cards.evaluate('node => node.scrollTop') == 420
            rest = column(frame, 'todo').get_by_role('button', name='Загрузить ещё 30')
            expect(column(frame, 'todo').locator('.colfoot')).to_contain_text('из 130')
            rest.click()
            expect(column(frame, 'todo').locator('.task')).to_have_count(130)
            expect(rest).to_be_hidden()
            assert len(set(shown(frame, 'todo'))) == 130
            cards.evaluate('node => { node.scrollTop = node.scrollHeight; }')
            expect(column(frame, 'todo').locator('.colfoot')).to_contain_text('из 130 · прокрутите колонку')
            session.shot(f'large-{engine}-light.png')
        finally:
            session.close()


@ENGINES
def test_phone_width_scrolls_only_the_board_and_the_longest_task_stays_inside_its_card(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    title = ('Согласовать очень длинное название задачи, которое не помещается в две строки ' * 13)[:1000]
    description = ('Подробности: https://docs.example.org/spec/section. ' + 'длинноеслово' * 30 + ' текст. ') * 40
    description = description[:16000]
    long = {'text': title, 'stage': 'todo', 'description': description, 'turn': 'other',
            'tags': [f'{"оченьдлинныйтег" * 4}'[:57] + f'{i:02d}' for i in range(20)],
            'references': [{'label': f'Источник номер {i + 1} с длинной подписью для проверки переноса',
                            'target': f'https://docs.example.org/library/section-{i + 1}/page?revision={i * 7}'}
                           for i in range(100)],
            'note': 'Заметка агента о том, откуда взялась задача.', 'next_step': 'Дождаться ответа.'}
    ids = lists.add([long]) + lists.add(balanced())
    stored = lists.entry(ids[0])
    assert len(stored['text']) == 1000 and len(stored['description']) == 16000
    assert len(stored['details']['references']) == 100 and len(stored['details']['tags']) == 20
    with sync_playwright() as pw:
        session = lists.open(pw, engine, PHONE)
        try:
            frame = session.mount()
            assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'the page never scrolls sideways'
            board = frame.locator('.board')
            assert board.evaluate('node => node.scrollWidth > node.clientWidth'), 'the four columns scroll inside the board'
            assert frame.locator('.column').evaluate_all('nodes => nodes.map(node => node.offsetWidth)') == [260] * 4
            tile = frame.locator(f'.task[data-id="{ids[0]}"]')
            assert tile.evaluate('node => node.offsetHeight') <= 90, 'a long task stays a short card'
            assert tile.evaluate('node => node.scrollWidth <= node.clientWidth')
            expect(tile.locator('.tag')).to_have_count(4)
            expect(tile.locator('.linkcount')).to_have_text('↗ 100')
            session.shot(f'phone-{engine}-light.png')
            tile.click()
            card = frame.locator('dialog')
            expect(card.get_by_role('button', name='Готово')).to_be_enabled()
            assert card.evaluate("""node => { const r = node.getBoundingClientRect();
                return r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight; }""")
            body = card.locator('.dialogbody')
            assert body.evaluate('node => node.scrollWidth <= node.clientWidth'), 'nothing widens the card'
            assert body.evaluate('node => node.scrollHeight > node.clientHeight'), 'the card scrolls, not the page'
            expect(card.get_by_label('Название задачи')).to_have_value(title)
            expect(card.get_by_label('Описание')).to_have_value(description)
            expect(card.locator('.ref')).to_have_count(6)
            card.get_by_role('button', name='Показать все 100').click()
            expect(card.locator('.ref')).to_have_count(100)
            expect(card.locator('.context')).to_contain_text('Дальше: Дождаться ответа.')
            expect(card.locator('.context')).to_contain_text('Заметка агента')
            session.shot(f'phone-card-{engine}-light.png')
            # Opening and closing an untouched long task writes nothing and asks nothing.
            card.get_by_label('Закрыть').click()
            expect(card).to_be_hidden()
            assert not lists.calls('POST', 'edit') and lists.entry(ids[0]) == stored
        finally:
            session.close()
        session = lists.open(pw, engine, OWNER, 'dark')
        try:
            frame = session.mount()
            tile = frame.locator(f'.task[data-id="{ids[0]}"]')
            assert tile.evaluate('node => node.offsetHeight') <= 90
            tile.click()
            card = frame.locator('dialog')
            expect(card.get_by_role('button', name='Готово')).to_be_enabled()
            assert card.locator('.dialogbody').evaluate('node => node.scrollWidth <= node.clientWidth')
            session.shot(f'long-card-{engine}-dark.png')
        finally:
            session.close()


@ENGINES
def test_keyboard_reaches_every_card_and_dragging_changes_the_stage_once(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    ids = lists.add(balanced())
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            page = session.page
            focused = lambda: frame.evaluate('document.activeElement.dataset.id || document.activeElement.tagName')
            frame.locator(f'.task[data-id="{ids[0]}"]').focus()
            page.keyboard.press('ArrowDown')
            assert focused() == ids[1]
            page.keyboard.press('ArrowRight')
            assert focused() == ids[10], 'the same row of the next column'
            page.keyboard.press('ArrowLeft')
            page.keyboard.press('ArrowUp')
            assert focused() == ids[0]
            page.keyboard.press('Enter')
            card = frame.locator('dialog')
            expect(card.get_by_role('button', name='Готово')).to_be_enabled()
            page.keyboard.press('Escape')
            expect(card).to_be_hidden()
            assert focused() == ids[0], 'closing returns focus to the card it came from'

            # Escape never throws a draft away: it asks, and a second Escape goes back to editing.
            page.keyboard.press('Enter')
            expect(card.get_by_role('button', name='Готово')).to_be_enabled()
            card.get_by_label('Описание').fill('Незаконченная мысль')
            page.keyboard.press('Escape')
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Правки не сохранены.')
            expect(card).to_be_visible()
            page.keyboard.press('Escape')
            expect(card.get_by_role('button', name='Готово')).to_be_visible()
            expect(card.get_by_label('Описание')).to_have_value('Незаконченная мысль')
            card.get_by_label('Описание').press('Control+Enter')
            expect(card).to_be_hidden()
            assert lists.entry(ids[0])['description'] == 'Незаконченная мысль'

            def centre(locator):
                box = locator.bounding_box()
                return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2

            def drag(entry_id, stage, release=True):
                page.mouse.move(*centre(frame.locator(f'.task[data-id="{entry_id}"]')))
                page.mouse.down()
                x, y = centre(column(frame, stage))
                page.mouse.move(x, y, steps=12)
                if release:
                    page.mouse.up()

            before = len(lists.calls('POST', 'edit'))
            drag(ids[2], 'review', release=False)
            expect(column(frame, 'review')).to_have_class('column drop')
            expect(frame.locator('.task.ghost')).to_have_count(1)
            session.shot(f'drag-{engine}-light.png')
            page.mouse.up()
            expect(column(frame, 'review').locator(f'.task[data-id="{ids[2]}"]')).to_have_count(1)
            expect(frame.locator('.task.ghost')).to_have_count(0)
            expect(frame.locator('dialog')).to_be_hidden()
            assert lists.entry(ids[2])['stage'] == 'review'
            moves = lists.calls('POST', 'edit')[before:]
            assert len(moves) == 1 and moves[0][2]['changes'] == {'stage': 'review'}
            assert moves[0][2]['expected'] == {'stage': 'todo'}
            expect(frame.locator('#toast')).to_contain_text('Перенесено в Review')
            frame.locator('#toast').get_by_role('button', name='Отменить').click()
            expect(column(frame, 'todo').locator(f'.task[data-id="{ids[2]}"]')).to_have_count(1)
            assert lists.entry(ids[2])['stage'] == 'todo'

            # Escape during a drag, or a drop where it started, moves nothing.
            before = len(lists.calls('POST', 'edit'))
            drag(ids[2], 'done', release=False)
            page.keyboard.press('Escape')
            page.mouse.up()
            drag(ids[2], 'todo')
            page.wait_for_timeout(300)
            assert len(lists.calls('POST', 'edit')) == before and lists.entry(ids[2])['stage'] == 'todo'
            expect(frame.locator('dialog')).to_be_hidden()
            # A move the store did not answer is not repeated; the board is read again instead.
            lists.fail('POST', 'edit', 'lost')
            drag(ids[2], 'done')
            expect(frame.locator('#toast')).to_contain_text('результат неизвестен')
            expect(column(frame, 'todo').locator(f'.task[data-id="{ids[2]}"]')).to_have_count(1)
            assert len(lists.calls('POST', 'edit')) == before + 1

            frame.get_by_role('button', name='Ещё действия').click()
            with page.expect_download() as caught:
                frame.get_by_role('menuitem', name='Скачать копию').click()
            assert caught.value.suggested_filename == 'smart-lists-export.json'
            saved = lists.evidence / f'export-{engine}.json'
            caught.value.save_as(str(saved))
            exported = json.loads(saved.read_text())
            assert len(exported['sha256']) == 64
            assert page.evaluate('async () => { await disposeWidget(); return true; }')
            expect(page.locator('iframe')).to_have_count(0)
        finally:
            session.close()


# ---------------------------------------------------------------- 0.6.3 repairs
# These drive only what every version of the card has (the description by id,
# the link row, Готово and В архив), so the same cases show the defect before the repair.

@ENGINES
def test_untouched_tags_come_back_exactly_with_commas_and_newlines(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    tags = ['Research, Development', 'Q4, 2026', 'ops', 'строка\nвторая']
    (target,) = lists.add([{'text': 'Теги с запятыми', 'tags': tags, 'description': 'Исходное описание'}])
    lists.update(target, due=' к пятнице ')  # the store keeps a due date's text exactly as written
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert not lists.calls('POST', 'edit'), 'opening and closing a card writes nothing'
            assert lists.entry(target)['details']['tags'] == tags
            assert lists.entry(target)['due']['raw'] == ' к пятнице '

            card = open_card(frame, target)
            card.locator('#sl-description').fill('Новое описание')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['details']['tags'] == tags
            assert lists.entry(target)['due']['raw'] == ' к пятнице '
            assert lists.calls('POST', 'edit')[-1][2]['changes'] == {'description': 'Новое описание'}
        finally:
            session.close()


@ENGINES
def test_edits_typed_while_saving_or_archiving_stay_on_screen(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    first, second, third, fourth, fifth = lists.add(
        [{'text': f'Задача {n}', 'description': 'Было', 'tags': ['ops']} for n in (1, 2, 3, 4, 5)])
    status = lambda card: card.locator('.dialogfoot [role="status"]')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            # Готово: the first part is saved, the part typed while it was away stays a draft.
            card = open_card(frame, first)
            text = card.locator('#sl-description')
            text.fill('Первая часть')
            lists.fail('POST', 'edit', 'slow')
            card.get_by_role('button', name='Готово').click()
            expect(status(card)).to_have_text('Сохраняю…')
            text.fill('Первая часть и продолжение')
            expect(status(card)).not_to_have_text('Сохраняю…')
            frame.wait_for_timeout(300)
            expect(card).to_be_visible()
            expect(text).to_have_value('Первая часть и продолжение')
            assert lists.entry(first)['description'] == 'Первая часть'
            expect(card.locator('.field', has=frame.locator('#sl-description')).locator('.note')).to_be_hidden()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(first)['description'] == 'Первая часть и продолжение'

            # В архив with an unsaved edit: typing during that save keeps the task where it is.
            card = open_card(frame, second)
            text = card.locator('#sl-description')
            text.fill('Вторая')
            lists.fail('POST', 'edit', 'slow')
            card.get_by_role('button', name='В архив').click()
            expect(status(card)).to_have_text('Сохраняю…')
            text.fill('Вторая и ещё')
            expect(status(card)).not_to_have_text('Сохраняю…')
            frame.wait_for_timeout(300)
            expect(card).to_be_visible()
            expect(text).to_have_value('Вторая и ещё')
            stored = lists.entry(second)
            assert stored['description'] == 'Вторая' and stored['archived_at'] is None
            assert not lists.calls('POST', 'archive')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(second)['description'] == 'Вторая и ещё'

            # Typing while the archive move itself is away: the move lands, the draft stays.
            card = open_card(frame, third)
            text = card.locator('#sl-description')
            lists.fail('POST', 'archive', 'slow')
            card.get_by_role('button', name='В архив').click()
            expect(status(card)).to_have_text('Сохраняю…')
            text.fill('Во время переноса')
            expect(status(card)).not_to_have_text('Сохраняю…')
            frame.wait_for_timeout(300)
            expect(card).to_be_visible()
            expect(text).to_have_value('Во время переноса')
            stored = lists.entry(third)
            assert stored['archived_at'] and stored['description'] == 'Было'
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(third)
            assert stored['archived_at'] and stored['description'] == 'Во время переноса'

            # Every field typed back to what it was while its save is away: the store now holds what was sent,
            # and the screen keeps what the owner went back to, as an unsaved draft the next Готово writes.
            card = open_card(frame, fourth)
            fields = {'Название задачи': 'Задача 4', 'Описание': 'Было', 'Срок': ''}
            for name, value in fields.items():
                card.get_by_label(name, exact=True).fill(value + ' — ушло')
            card.get_by_label('Чей следующий ход').select_option('agent')
            add_tag(card, 'ушло')
            card.get_by_label('Адрес ссылки').fill('https://code.example.org/team/app/pull/4')
            card.get_by_label('Подпись ссылки, необязательно').fill('Ушедшая ссылка')
            card.get_by_role('button', name='Добавить').click()
            lists.fail('POST', 'edit', 'slow')
            card.get_by_role('button', name='Готово').click()
            expect(status(card)).to_have_text('Сохраняю…')
            for name, value in fields.items():
                card.get_by_label(name, exact=True).fill(value)
            card.get_by_label('Чей следующий ход').select_option('unassigned')
            card.get_by_role('button', name='Убрать тег: ушло').click()
            card.get_by_role('button', name='Убрать ссылку: Ушедшая ссылка').click()
            expect(status(card)).not_to_have_text('Сохраняю…')
            frame.wait_for_timeout(300)
            expect(card).to_be_visible()
            expect(card.locator('.dialogfoot .note')).to_contain_text('нажми «Готово» ещё раз')
            for name, value in fields.items():
                expect(card.get_by_label(name, exact=True)).to_have_value(value)
            expect(card.get_by_label('Чей следующий ход')).to_have_value('unassigned')
            assert chips(card) == ['ops']
            expect(card.locator('.ref')).to_have_count(0)
            expect(card.locator('.field .note:not([hidden])')).to_have_count(0)  # nothing reads as changed elsewhere
            stored = lists.entry(fourth)
            assert stored['text'] == 'Задача 4 — ушло' and stored['details']['tags'] == ['ops', 'ушло']
            assert stored['due']['raw'] == ' — ушло' and stored['details']['turn'] == 'agent'
            assert [ref['label'] for ref in stored['details']['references']] == ['Ушедшая ссылка']
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(fourth)
            assert (stored['text'], stored['description'], stored['due']) == ('Задача 4', 'Было', None)
            assert stored['details']['tags'] == ['ops'] and stored['details']['turn'] == 'unassigned'
            assert not stored['details'].get('references')
            last = lists.calls('POST', 'edit')[-1][2]
            assert last['expected']['description'] == 'Было — ушло' and last['changes']['description'] == 'Было'

            # The stage saves as soon as it is chosen; choosing the old one back while that save is away keeps it.
            card = open_card(frame, fifth)
            stage = card.get_by_label('Стадия')
            lists.fail('POST', 'edit', 'slow')
            stage.select_option('in_progress')
            expect(status(card)).to_have_text('Сохраняю…')
            stage.select_option('todo')
            expect(status(card)).not_to_have_text('Сохраняю…')
            frame.wait_for_timeout(300)
            expect(stage).to_have_value('todo')
            expect(status(card)).to_have_text('Есть несохранённые правки')
            assert lists.entry(fifth)['stage'] == 'in_progress'
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(fifth)['stage'] == 'todo'
        finally:
            session.close()


@ENGINES
def test_a_refused_retry_of_an_unconfirmed_edit_lets_keep_mine_save(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Спорное описание', 'description': 'Исходное'}])
    edits = lambda: lists.calls('POST', 'edit')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            card.locator('#sl-description').fill('Моя правка')
            lists.fail('POST', 'edit', 'lost')   # no answer, and it never reached the store
            lists.fail('GET', 'entry', 'lost')   # the check read fails too: the outcome stays unknown
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            assert len(edits()) == 1
            lists.update(target, description='Правка агента')
            card.get_by_role('button', name='Готово').click()
            note = card.locator('.field', has=frame.locator('#sl-description')).locator('.note')
            expect(note.locator('.theirs')).to_have_text('Правка агента')
            assert len(edits()) == 2 and edits()[1][2] == edits()[0][2], 'the unknown write is repeated as itself'
            note.get_by_role('button', name='Оставить моё').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['description'] == 'Моя правка'
            last = edits()[-1][2]
            assert last['request_id'] != edits()[0][2]['request_id']
            assert last['expected'] == {'description': 'Правка агента'}
            assert len(edits()) == 3
        finally:
            session.close()


@ENGINES
def test_an_uppercase_link_scheme_is_saved_in_canonical_form(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Ссылка заглавными'}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            card.get_by_label('Адрес ссылки').fill('HTTPS://Code.Example.org/Team/App/pull/9?Q=A')
            card.get_by_role('button', name='Добавить').click()
            expect(card.locator('.ref')).to_have_count(1)
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            (ref,) = lists.entry(target)['details']['references']
            assert ref['target'] == 'https://Code.Example.org/Team/App/pull/9?Q=A'
            # What the store would refuse is refused here, before anything is sent.
            card = open_card(frame, target)
            for refused in ('https:code.example.org/x', 'https://@code.example.org/x', 'HTTPS://user:pw@code.example.org/'):
                card.get_by_label('Адрес ссылки').fill(refused)
                card.get_by_role('button', name='Добавить').click()
                expect(card.locator('.addrow + .note')).to_contain_text('https://')
                expect(card.locator('.ref')).to_have_count(1)
        finally:
            session.close()


@ENGINES
def test_tag_tokens_selects_and_store_state_read_as_the_card_is_meant(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Токены', 'tags': ['Research, Development', 'ops']}])
    # The store counts characters, not UTF-16 units: forty emoji are a valid tag, six hundred a valid title.
    (wide,) = lists.add([{'text': '😀' * 600, 'tags': ['😀' * 40, 'ops']}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            assert chips(card) == ['Research, Development', 'ops']
            # Every control in the side column has the inputs' height.
            heights = card.locator('aside select, aside input').evaluate_all(
                'nodes => nodes.map(node => node.getBoundingClientRect().height)')
            assert max(heights) - min(heights) <= 2, heights
            expect(card.get_by_text('Описание', exact=True)).to_be_visible()
            add_tag(card, 'A, B')
            card.get_by_role('button', name='Убрать тег: ops').click()
            card.get_by_label('Теги').fill('черновик')  # typed, never confirmed with Enter
            assert chips(card) == ['Research, Development', 'A, B']
            session.shot(f'tags-{engine}-light.png')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['details']['tags'] == ['Research, Development', 'A, B', 'черновик']

            # A store the owner can only fix through the chat is described, not quoted from the agent's API.
            # The board read that follows the save has landed once the new tag is on the tile.
            expect(frame.locator(f'.task[data-id="{target}"] .tag', has_text='черновик')).to_have_count(1)
            lists.fail('GET', 'board', {'ok': False, 'code': 'store_not_initialized',
                                        'error': 'No Smart Lists store exists in this installation yet'})
            frame.get_by_role('button', name='Ещё действия').click()
            frame.get_by_role('menuitem', name='Обновить').click()
            expect(frame.locator('.alert')).to_contain_text('Списка ещё нет')
            assert 'No Smart Lists' not in frame.locator('.alert').inner_text()
            frame.locator('.alert').get_by_role('button', name='Повторить').click()
            expect(frame.locator('.alert')).to_be_hidden()

            # Characters beyond the Basic Multilingual Plane count once, as the store counts them.
            card = open_card(frame, wide)
            card.get_by_role('button', name='Убрать тег: ops').click()
            add_tag(card, '🏷' * 60)
            assert chips(card) == ['😀' * 40, '🏷' * 60]
            add_tag(card, 'я' * 61)
            expect(card.locator('.dialogfoot .note')).to_have_text('Тег длиннее 60 символов.')
            card.get_by_label('Теги').fill('')
            card.get_by_label('Название задачи').fill('😀' * 600 + '!')
            card.get_by_label('Срок').fill('📅' * 120)
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(wide)
            assert stored['details']['tags'] == ['😀' * 40, '🏷' * 60]
            assert stored['text'] == '😀' * 600 + '!' and stored['due']['raw'] == '📅' * 120
            card = open_card(frame, wide)
            card.get_by_label('Срок').fill('я' * 121)
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_have_text('Срок длиннее 120 символов.')
            assert lists.entry(wide)['due']['raw'] == '📅' * 120
        finally:
            session.close()


# ---------------------------------------------------------------- card draft slot

def leave(session):
    """What leaving the Widgets page does: the host disposes the frame (its hooks get up to one second)."""
    session.page.evaluate('window.disposeWidget()')


def leave_and_return(session):
    leave(session)
    return session.mount()


def wait_until(check, what='the expected state', seconds=10):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def kept(lists):
    """The draft slot's card form as stored, or {} when it holds none."""
    return lists.read('draft')['draft'] or {}


def resume(frame):
    """Continue the saved card. Like open_card, wait until it has read the task and takes input:
    while it reads, its body is inert, and a typed value would not reach the form."""
    from playwright.sync_api import expect
    frame.locator('.draftbar').get_by_role('button', name='Продолжить').click()
    card = frame.locator('dialog')
    expect(card).to_be_visible()
    expect(card.locator('.dialogfoot .primary')).to_be_enabled()
    expect(card.locator('.dialogbody')).not_to_have_attribute('inert', '')
    return card


def on_board(lists, text):
    board = lists.read('board', 'limit=100')
    return sum(entry['text'] == text for stage in board['stages'].values() for entry in stage['entries'])


@ENGINES
def test_a_card_left_open_comes_back_from_its_draft_and_leaving_saves_nothing(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    target, other = lists.add([{'text': 'Черновик описания', 'description': 'Было', 'tags': ['ops']},
                               {'text': 'Другая задача'}])
    store = lists.tmp / 'state' / 'store.json'
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            # An untouched card writes no draft.
            card = open_card(frame, other)
            card.get_by_label('Закрыть').click()
            expect(card).to_be_hidden()
            assert not lists.calls('POST', 'draft')

            # Typed, a tag not confirmed with Enter, a link typed but not added.
            card = open_card(frame, target)
            card.get_by_label('Теги').fill('недописанный')
            card.get_by_label('Адрес ссылки').fill('https://code.example.org/team/app/pull/7')
            card.get_by_label('Подпись ссылки, необязательно').fill('Ещё не добавлена')
            wait_until(lambda: kept(lists).get('typed', {}).get('label') == 'Ещё не добавлена', 'the typed label is kept')
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик сохранён')
            # The next draft write answers late; the second half is typed meanwhile and the page is left at once.
            # Leaving waits for that write and then writes the newest form.
            lists.fail('POST', 'draft', 'held')
            card.get_by_label('Описание').fill('Первая половина')
            card.get_by_label('Описание').fill('Первая половина и вторая')
            before = store.read_bytes()
            frame = leave_and_return(session)
            assert store.read_bytes() == before, 'leaving writes nothing to the store'
            assert not lists.calls('POST', 'edit') and not lists.calls('POST', 'add')
            bar = frame.locator('.draftbar')
            expect(bar).to_contain_text('Есть несохранённые правки задачи «Черновик описания»')
            # Another card waits until this one is continued or deleted.
            frame.locator(f'.task[data-id="{other}"]').click()
            expect(frame.locator('#toast')).to_contain_text('Сначала продолжи или удали')
            expect(frame.locator('dialog')).to_be_hidden()
            session.shot(f'draft-bar-{engine}-light.png')
            card = resume(frame)
            expect(card.get_by_label('Описание')).to_have_value('Первая половина и вторая')
            expect(card.get_by_label('Теги')).to_have_value('недописанный')
            expect(card.get_by_label('Адрес ссылки')).to_have_value('https://code.example.org/team/app/pull/7')
            expect(card.get_by_label('Подпись ссылки, необязательно')).to_have_value('Ещё не добавлена')
            expect(bar).to_be_hidden()
            assert chips(card) == ['ops']
            assert store.read_bytes() == before, 'continuing changes nothing in the store'
            session.shot(f'draft-resumed-{engine}-light.png')

            # Saving everything clears the slot.
            card.get_by_role('button', name='Добавить').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(target)
            assert stored['description'] == 'Первая половина и вторая'
            assert stored['details']['tags'] == ['ops', 'недописанный']
            assert [ref['label'] for ref in stored['details']['references']] == ['Ещё не добавлена']
            wait_until(lambda: lists.read('draft')['draft'] is None, 'a saved card clears the slot')

            # Discarding clears it too, and nothing is offered after a reload.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Передумал')
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик сохранён')
            card.get_by_label('Закрыть').click()
            card.get_by_role('button', name='Не сохранять').click()
            expect(card).to_be_hidden()
            wait_until(lambda: lists.read('draft')['draft'] is None, 'a discarded card clears the slot')
            frame = session.mount()
            frame.wait_for_timeout(300)
            expect(frame.locator('.draftbar')).to_be_hidden()
            assert lists.entry(target)['description'] == 'Первая половина и вторая'
        finally:
            session.close()


@ENGINES
def test_a_resumed_draft_meets_a_changed_conflicting_or_deleted_task(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    first, second, third = lists.add([{'text': 'Изменят в другом месте', 'description': 'Было'},
                                      {'text': 'Уже спорное', 'description': 'Было'},
                                      {'text': 'Удалят', 'description': 'Было'}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            # Changed elsewhere while away: an untouched field follows the store, the typed one stays.
            card = open_card(frame, first)
            card.get_by_label('Описание').fill('Моё описание')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Моё описание')
            leave(session)
            lists.update(first, text='Название от агента')
            frame = session.mount()
            card = resume(frame)
            expect(card.get_by_label('Название задачи')).to_have_value('Название от агента')
            expect(card.get_by_label('Описание')).to_have_value('Моё описание')
            expect(card.locator('.field .note:not([hidden])')).to_have_count(0)
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            stored = lists.entry(first)
            assert (stored['text'], stored['description']) == ('Название от агента', 'Моё описание')

            # A conflict already on screen comes back with both values.
            card = open_card(frame, second)
            card.get_by_label('Описание').fill('Моя версия')
            lists.update(second, description='Версия агента')
            session.refresh(frame)
            note = card.locator('.field', has=frame.locator('#sl-description')).locator('.note')
            expect(note.locator('.theirs')).to_have_text('Версия агента')
            wait_until(lambda: kept(lists).get('conflicts', {}).get('description') == 'Версия агента')
            frame = leave_and_return(session)
            card = resume(frame)
            note = card.locator('.field', has=frame.locator('#sl-description')).locator('.note')
            expect(note.locator('.theirs')).to_have_text('Версия агента')
            expect(card.get_by_label('Описание')).to_have_value('Моя версия')
            note.get_by_role('button', name='Оставить моё').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(second)['description'] == 'Моя версия'
            assert lists.calls('POST', 'edit')[-1][2]['expected'] == {'description': 'Версия агента'}

            # Deleted while away: the text stays readable, nothing is written back, the draft waits to be deleted.
            card = open_card(frame, third)
            card.get_by_label('Описание').fill('Текст, который жалко потерять')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Текст, который жалко потерять')
            leave(session)
            lists.tool('delete', entry_ids=[third], request_id='agent-delete-1')
            frame = session.mount()
            card = resume(frame)
            expect(card.locator('.dialogfoot .note')).to_contain_text('её удалили')
            expect(card.locator('.dialogfoot .note')).to_contain_text('остаётся в черновике')
            description = card.get_by_label('Описание')
            expect(description).to_have_value('Текст, который жалко потерять')
            assert description.evaluate('node => node.readOnly'), 'readable and copyable, not editable'
            session.shot(f'draft-deleted-task-{engine}-light.png')
            edits = len(lists.calls('POST', 'edit'))
            card.locator('.dialogfoot .primary').click()
            expect(card).to_be_hidden()
            assert len(lists.calls('POST', 'edit')) == edits
            bar = frame.locator('.draftbar')
            expect(bar).to_contain_text('Есть несохранённые правки задачи «Удалят»')
            assert kept(lists).get('form', {}).get('description') == 'Текст, который жалко потерять'
            bar.get_by_role('button', name='Удалить черновик').click()
            expect(bar).to_be_hidden()
            wait_until(lambda: lists.read('draft')['draft'] is None, 'the owner deleted the draft')
        finally:
            session.close()


@ENGINES
def test_leaving_while_a_write_is_away_keeps_its_request_and_reopening_repeats_nothing(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Правка в пути', 'description': 'Было'}])
    edits = lambda: lists.calls('POST', 'edit')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            # Applied, but the answer and the check read were lost; then more typing; then the page is left.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Ушло в запросе')
            lists.fail('POST', 'edit', 'unanswered')
            lists.fail('GET', 'entry', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            card.get_by_label('Название задачи').fill('Правка в пути и после')
            wait_until(lambda: kept(lists).get('form', {}).get('text') == 'Правка в пути и после')
            assert json.loads(kept(lists)['pending']['text']) == edits()[0][2], 'the unknown write is kept as sent'
            frame = leave_and_return(session)
            card = resume(frame)
            # Reopening sends nothing; its read finds the lost write applied, and the later typing stays a draft.
            expect(card.get_by_label('Название задачи')).to_have_value('Правка в пути и после')
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Есть несохранённые правки')
            expect(card.get_by_label('Описание')).to_have_value('Ушло в запросе')
            assert len(edits()) == 1
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 2 and edits()[1][2]['changes'] == {'text': 'Правка в пути и после'}
            assert edits()[1][2]['request_id'] != edits()[0][2]['request_id']
            assert lists.entry(target)['description'] == 'Ушло в запросе'

            # Unconfirmed and never applied: the reopened card repeats it as itself, and only on «Готово».
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Не дошло')
            lists.fail('POST', 'edit', 'lost')
            lists.fail('GET', 'entry', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            wait_until(lambda: json.loads((kept(lists).get('pending') or {'text': 'null'})['text']) == edits()[2][2],
                       'the unconfirmed write is kept as sent')
            frame = leave_and_return(session)
            card = resume(frame)
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            frame.wait_for_timeout(300)
            assert len(edits()) == 3, 'reopening repeats nothing by itself'
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 4 and edits()[3][2] == edits()[2][2], 'the same request id and body'
            assert lists.entry(target)['description'] == 'Не дошло'

            # Left while the save is still away (it never applies): the draft took the request before it was sent.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Ушло перед уходом')
            lists.fail('POST', 'edit', 'stalled')
            card.get_by_role('button', name='Готово').click()
            wait_until(lambda: len(edits()) == 5, 'the save was sent')
            frame = leave_and_return(session)
            assert json.loads(kept(lists)['pending']['text']) == edits()[4][2]
            card = resume(frame)
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 6 and edits()[5][2] == edits()[4][2], 'repeated as itself, not under a new id'
            assert lists.entry(target)['description'] == 'Ушло перед уходом'

            # Creating: the draft holds the request before it is sent, so a frame gone mid-request leaves one task.
            frame.get_by_role('button', name='+ Задача').click()
            card = frame.locator('dialog')
            card.get_by_label('Название задачи').fill('Создана в пути')
            lists.fail('POST', 'add', 'slow')
            card.get_by_role('button', name='Создать').click()
            wait_until(lambda: len(lists.calls('POST', 'add')) == 1, 'the add request was sent')
            (add,) = lists.calls('POST', 'add')
            assert json.loads(kept(lists)['creating']) == add[2], 'the draft held the request before it was sent'
            frame = leave_and_return(session)
            wait_until(lambda: on_board(lists, 'Создана в пути') == 1, 'the slow add landed after the frame was gone')
            bar = frame.locator('.draftbar')
            expect(bar).to_contain_text('Создание задачи «Создана в пути» не подтверждено')
            card = resume(frame)
            expect(card.locator('.dialogfoot .note')).to_contain_text('Создание не подтверждено')
            frame.wait_for_timeout(300)
            assert len(lists.calls('POST', 'add')) == 1, 'reopening repeats nothing by itself'
            card.get_by_role('button', name='Создать').click()
            expect(card).to_be_hidden()
            adds = lists.calls('POST', 'add')
            assert len(adds) == 2 and adds[1][2] == adds[0][2]
            assert on_board(lists, 'Создана в пути') == 1
            wait_until(lambda: lists.read('draft')['draft'] is None, 'the created card cleared the slot')
        finally:
            session.close()


@ENGINES
def test_draft_failures_and_a_second_window_never_overwrite_a_draft(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Окно A', 'description': 'Было'}])
    path = lists.tmp / 'state' / 'widget_draft.json'
    with sync_playwright() as pw:
        one, two = lists.open(pw, engine), None
        try:
            frame = one.mount()
            card = open_card(frame, target)
            text = card.get_by_label('Описание')
            text.fill('Первое окно пишет')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Первое окно пишет')
            held = lists.read('draft')['revision']
            # A refused draft write is said, and the slot keeps what it held.
            lists.fail('POST', 'draft', {'ok': False, 'code': 'draft_too_large', 'error': 'too large'})
            text.fill('Первое окно пишет ещё')
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик не сохранён: он больше 4 МБ.')
            assert lists.read('draft')['revision'] == held
            # Hiding the page writes the form again. A synthetic visibilitychange in the frame: it exercises the
            # widget's own handler, not a real switch of the app's window.
            frame.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                document.dispatchEvent(new Event('visibilitychange'));
                delete document.hidden;
            }""")
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Первое окно пишет ещё',
                       'hiding the page wrote the form again')
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик сохранён')
            text.fill('Первое окно пишет дальше')
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик сохранён')

            # A second window finds that draft and continues it explicitly; the first then stops writing over it.
            two = lists.open(pw, engine)
            other = two.mount()
            bar = other.locator('.draftbar')
            expect(bar).to_contain_text('«Окно A»')
            other_card = resume(other)
            expect(other_card.get_by_label('Описание')).to_have_value('Первое окно пишет дальше')
            other_card.get_by_label('Описание').fill('Второе окно продолжило')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Второе окно продолжило')
            text.fill('Первое окно не знает')
            expect(card.locator('.dialogfoot')).to_contain_text('хранит другое окно')
            one.shot(f'draft-elsewhere-{engine}-light.png')
            assert kept(lists)['form']['description'] == 'Второе окно продолжило', 'not overwritten'
            # The second window saves, which clears the slot; the first window's next change takes it.
            other_card.get_by_role('button', name='Готово').click()
            expect(other_card).to_be_hidden()
            wait_until(lambda: lists.read('draft')['draft'] is None)
            text.fill('Первое окно снова пишет')
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик сохранён')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Первое окно снова пишет')

            # A failed first read is said and deletes nothing; a retry then offers the draft.
            raw = path.read_bytes()
            leave(one)
            lists.fail('GET', 'draft', 'lost')
            frame = one.mount()
            bar = frame.locator('.draftbar')
            expect(bar).to_contain_text('Не удалось проверить')
            assert path.read_bytes() == raw
            bar.get_by_role('button', name='Повторить').click()
            expect(bar).to_contain_text('Есть несохранённые правки задачи «Окно A»')
            assert path.read_bytes() == raw
        finally:
            one.close()
            if two:
                two.close()


@ENGINES
def test_a_late_draft_write_never_brings_back_a_draft_cleared_elsewhere(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Окно A', 'description': 'Было'}])
    sent = lambda text: [call for call in lists.calls('POST', 'draft')
                         if ((call[2].get('draft') or {}).get('form') or {}).get('description') == text]
    with sync_playwright() as pw:
        one, two = lists.open(pw, engine), None
        try:
            frame = one.mount()
            card = open_card(frame, target)
            text = card.get_by_label('Описание')
            text.fill('Первая версия')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Первая версия')
            two = lists.open(pw, engine)
            bar = two.mount().locator('.draftbar')
            expect(bar).to_contain_text('«Окно A»')
            # This window's next draft write is held on its way; meanwhile the other window deletes the draft.
            lists.fail('POST', 'draft', 'slow')
            text.fill('Вторая версия')
            wait_until(lambda: len(sent('Вторая версия')) == 1, 'the late write reached the store')
            bar.get_by_role('button', name='Удалить черновик').click()
            expect(bar).to_be_hidden()
            # The late write arrives after the clear: refused, said here, and never sent again over the empty slot.
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик очистили в другом окне')
            frame.wait_for_timeout(800)
            assert lists.read('draft')['draft'] is None, 'the cleared draft stays cleared'
            assert len(sent('Вторая версия')) == 1, 'the refused write is not repeated'
            one.shot(f'draft-cleared-elsewhere-{engine}-light.png')
            # The owner's next change may take the empty slot, with what is on screen now.
            text.fill('Третья версия')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Третья версия')
            expect(card.locator('.dialogfoot')).to_contain_text('Черновик сохранён')
            assert len(sent('Вторая версия')) == 1
        finally:
            one.close()
            if two:
                two.close()


@ENGINES
def test_an_unadded_link_is_never_added_or_dropped_by_closing_saving_or_archiving(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    first, second, third = lists.add([{'text': f'Ссылка {n}', 'description': 'Было'} for n in (1, 2, 3)])
    url = 'https://code.example.org/team/app/pull/11'
    left = 'Ссылка набрана, но не добавлена: нажми «Добавить» или сотри её.'
    note = lambda card: card.locator('.dialogfoot .note')
    status = lambda card: card.locator('.dialogfoot [role="status"]')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            # ×: a typed link is asked about first; nothing is added, and only «Не сохранять» drops it.
            card = open_card(frame, first)
            card.get_by_label('Адрес ссылки').fill(url)
            card.get_by_label('Подпись ссылки, необязательно').fill('Набрана')
            wait_until(lambda: kept(lists).get('typed', {}).get('url') == url)
            card.get_by_label('Закрыть').click()
            expect(status(card)).to_have_text('Ссылка набрана, но не добавлена.')
            expect(card).to_be_visible()
            session.shot(f'link-typed-close-{engine}-light.png')
            # Its «Сохранить» saves what there is to save; the typed link stays typed, not added.
            card.get_by_role('button', name='Сохранить').click()
            expect(note(card)).to_have_text(left)
            expect(card.get_by_label('Адрес ссылки')).to_have_value(url)
            expect(card.get_by_label('Подпись ссылки, необязательно')).to_have_value('Набрана')
            assert not lists.calls('POST', 'edit') and not lists.entry(first)['details'].get('references')
            assert kept(lists)['typed'] == {'tag': '', 'url': url, 'label': 'Набрана'}
            card.get_by_label('Закрыть').click()
            card.get_by_role('button', name='Не сохранять').click()
            expect(card).to_be_hidden()
            wait_until(lambda: lists.read('draft')['draft'] is None, 'an explicit discard clears the draft')
            assert not lists.entry(first)['details'].get('references')

            # Готово with an edit, and a link typed while that save is away: the edit is saved, the link waits.
            card = open_card(frame, second)
            card.get_by_label('Описание').fill('Сохранится')
            lists.fail('POST', 'edit', 'slow')
            card.get_by_role('button', name='Готово').click()
            expect(status(card)).to_have_text('Сохраняю…')
            card.get_by_label('Адрес ссылки').fill(url)
            expect(note(card)).to_have_text(left)
            assert lists.entry(second)['description'] == 'Сохранится'
            expect(card.get_by_label('Адрес ссылки')).to_have_value(url)
            wait_until(lambda: kept(lists).get('typed', {}).get('url') == url and kept(lists)['form']['description'] == 'Сохранится')
            card.get_by_role('button', name='Добавить').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert [ref['target'] for ref in lists.entry(second)['details']['references']] == [url]
            wait_until(lambda: lists.read('draft')['draft'] is None)

            # В архив with only a typed link: the task moves, the card stays open with the link still typed.
            card = open_card(frame, third)
            card.get_by_label('Адрес ссылки').fill(url)
            card.get_by_role('button', name='В архив').click()
            expect(note(card)).to_have_text('Перемещено в архив. ' + left)
            assert lists.entry(third)['archived_at']
            expect(card.get_by_label('Адрес ссылки')).to_have_value(url)
            # Back to the board, with a label typed while that move is away.
            card.get_by_label('Адрес ссылки').fill('')
            lists.fail('POST', 'archive', 'slow')
            card.get_by_role('button', name='Вернуть на доску').click()
            expect(status(card)).to_have_text('Сохраняю…')
            card.get_by_label('Подпись ссылки, необязательно').fill('Поздняя подпись')
            expect(note(card)).to_have_text('Возвращено в To do. ' + left)
            assert lists.entry(third)['archived_at'] is None
            expect(card.get_by_label('Подпись ссылки, необязательно')).to_have_value('Поздняя подпись')
            wait_until(lambda: kept(lists).get('typed', {}).get('label') == 'Поздняя подпись')
            # Once the typed link is erased, closing is an ordinary close and clears the draft.
            card.get_by_label('Подпись ссылки, необязательно').fill('')
            card.get_by_label('Закрыть').click()
            expect(card).to_be_hidden()
            wait_until(lambda: lists.read('draft')['draft'] is None)
        finally:
            session.close()


@ENGINES
def test_a_new_card_keeps_what_was_chosen_before_it_has_a_title(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    lists.add([{'text': 'Уже на доске', 'tags': ['дом']}])
    status = lambda card: card.locator('.dialogfoot [role="status"]')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = frame.locator('dialog')
            # Opened under a tag filter and closed untouched: the filter's tag is not input, so nothing is kept.
            frame.locator('#filters').get_by_role('button', name='дом', exact=True).click()
            frame.get_by_role('button', name='+ Задача').click()
            expect(card).to_be_visible()
            assert chips(card) == ['дом']
            card.get_by_label('Закрыть').click()
            expect(card).to_be_hidden()
            frame.locator('#filters').get_by_role('button', name='дом', exact=True).click()
            frame.wait_for_timeout(300)
            assert not lists.calls('POST', 'draft')

            # Everything but a title: a confirmed tag, the turn, a due date and the stage are input to keep.
            frame.get_by_role('button', name='+ Задача').click()
            add_tag(card, 'срочно')
            wait_until(lambda: kept(lists).get('form', {}).get('tags') == ['срочно'], 'a tag alone is kept')
            card.get_by_label('Чей следующий ход').select_option('agent')
            card.get_by_label('Срок').fill('в пятницу')
            card.get_by_label('Стадия').select_option('review')
            wait_until(lambda: kept(lists).get('form', {}).get('stage') == 'review', 'the chosen fields are kept')
            assert {key: kept(lists)['form'][key] for key in ('tags', 'turn', 'due')} == \
                {'tags': ['срочно'], 'turn': 'agent', 'due': 'в пятницу'}
            # × asks instead of dropping them.
            card.get_by_label('Закрыть').click()
            expect(status(card)).to_have_text('Задача ещё не создана.')
            expect(card).to_be_visible()
            # Leaving keeps them and creates nothing; continuing brings every one back.
            frame = leave_and_return(session)
            assert not lists.calls('POST', 'add')
            expect(frame.locator('.draftbar')).to_contain_text('Есть несохранённая новая задача')
            card = resume(frame)
            assert chips(card) == ['срочно']
            expect(card.get_by_label('Чей следующий ход')).to_have_value('agent')
            expect(card.get_by_label('Срок')).to_have_value('в пятницу')
            expect(card.get_by_label('Стадия')).to_have_value('review')
            card.get_by_label('Название задачи').fill('Названа после возврата')
            card.get_by_role('button', name='Создать').click()
            expect(card).to_be_hidden()
            board = lists.read('board', 'limit=100')
            (made,) = [entry['id'] for stage in board['stages'].values() for entry in stage['entries']
                       if entry['text'] == 'Названа после возврата']
            stored = lists.entry(made)
            assert (stored['stage'], stored['due']['raw'], stored['details']['tags'], stored['details']['turn']) == \
                ('review', 'в пятницу', ['срочно'], 'agent')
            wait_until(lambda: lists.read('draft')['draft'] is None, 'the created card cleared the slot')
        finally:
            session.close()


@ENGINES
def test_a_late_edit_an_equal_refusal_and_a_failed_draft_write_leave_the_save_honest(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    target, second = lists.add([{'text': 'Поздняя правка', 'description': 'Было'},
                                {'text': 'Та же правка', 'description': 'Было'}])
    edits = lambda: lists.calls('POST', 'edit')
    note = lambda card: card.locator('.dialogfoot .note')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            # No answer, the check read still finds the old value, and the write lands after that read.
            # Not proof that nothing was saved: «Готово» repeats the very request, which the store answers as a replay.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Дошло позже')
            lists.fail('POST', 'edit', 'late')
            card.get_by_role('button', name='Готово').click()
            expect(note(card)).to_contain_text('Сохранение не подтверждено')
            expect(note(card)).not_to_contain_text('осталась прежней')
            wait_until(lambda: lists.entry(target)['description'] == 'Дошло позже', 'the late write landed')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 2 and edits()[1][2] == edits()[0][2], 'repeated as itself, not under a new id'

            # Refused because the store moved, but to exactly what the form holds: no field to choose, it is saved.
            card = open_card(frame, second)
            card.get_by_label('Описание').fill('Одинаково')
            lists.update(second, description='Одинаково')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert edits()[-1][2]['expected'] == {'description': 'Было'}, 'that save was refused as built'
            assert lists.entry(second)['description'] == 'Одинаково'

            # The draft refuses the write that holds the request: the save still goes, after that write settled.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Сохранится без черновика')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Сохранится без черновика')
            lists.fail('POST', 'draft', {'ok': False, 'code': 'draft_too_large', 'error': 'too large'})
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['description'] == 'Сохранится без черновика'
            calls = lists.state['calls']
            refused = [at for at, call in enumerate(calls) if call[:2] == ('POST', 'draft')
                       and ((call[2].get('draft') or {}).get('pending') or None)]
            last_edit = max(at for at, call in enumerate(calls) if call[:2] == ('POST', 'edit'))
            assert refused and refused[-1] < last_edit, 'the draft write holding the request went first'
            wait_until(lambda: lists.read('draft')['draft'] is None, 'the saved card cleared the slot')
        finally:
            session.close()


@ENGINES
def test_this_windows_own_late_draft_write_is_taken_as_its_own(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Свой поздний черновик', 'description': 'Было'}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            foot = card.locator('.dialogfoot')
            text = card.get_by_label('Описание')
            text.fill('Первая версия')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Первая версия')
            # The write gets no answer, the check read fails too, and the write lands afterwards.
            lists.fail('POST', 'draft', 'late')
            lists.fail('GET', 'draft', 'lost')
            text.fill('Вторая версия')
            expect(foot).to_contain_text('Черновик не сохранён')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Вторая версия', 'the late write landed')
            # That revision is this window's own: the next change writes over it, nothing says another window cleared it.
            text.fill('Третья версия')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Третья версия',
                       "the next write takes the slot this window wrote")
            expect(foot).to_contain_text('Черновик сохранён')
            expect(foot).not_to_contain_text('другом окне')
        finally:
            session.close()


def test_fixture_reports_route_failures_and_only_records_a_closed_connection(lists):
    """The fixture's own boundary: a skill route that raises is an error teardown reports;
    a browser that hangs up before reading an answer is not."""
    import socket
    import struct
    import urllib.error
    import urllib.request

    # A genuine failure inside a registered route is still caught, answered 500 and recorded.
    real = lists.api.routes['board']['handler']

    def broken(request):
        raise RuntimeError('route failed on purpose')

    lists.api.routes['board']['handler'] = broken
    try:
        with pytest.raises(urllib.error.HTTPError) as answer:
            urllib.request.urlopen(f'{lists.url}{PREFIX}board?limit=1')
        assert answer.value.code == 500
    finally:
        lists.api.routes['board']['handler'] = real
    assert lists.state['errors'] == ["RuntimeError('route failed on purpose')"]
    lists.state['errors'].clear()  # asserted here; teardown would otherwise (rightly) fail on it

    # A client that resets its connection while the route works: the write fails, nothing is an error.
    lists.fail('GET', 'board', 'slow')
    client = socket.create_connection(('127.0.0.1', urlsplit(lists.url).port))
    before = len(lists.calls('GET', 'board'))
    client.sendall(f'GET {PREFIX}board?limit=1 HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n'.encode())
    # Hang up only once the server holds the request (it records it, then the slow route sleeps 1.5 s).
    deadline = time.monotonic() + 10
    while len(lists.calls('GET', 'board')) == before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(lists.calls('GET', 'board')) == before + 1, 'the server received the request'
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))  # close with RST
    client.close()
    while not lists.state['disconnects'] and time.monotonic() < deadline:
        time.sleep(0.05)  # the route answers after its sleep, into the reset socket
    assert lists.state['disconnects'] == [f'GET {PREFIX}board?limit=1']
    assert lists.state['errors'] == []
    # The server keeps answering.
    assert lists.read('board', 'limit=1')['ok'] is True


@ENGINES
@pytest.mark.parametrize('outcome,newer', [('unanswered', 'Было'), ('late', 'Было'), ('late', 'Дописано')])
def test_poll_recognizes_an_unconfirmed_edit_without_losing_newer_typing(lists, engine, outcome, newer):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Неподтверждённая правка', 'description': 'Было'}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            text = card.get_by_label('Описание')
            text.fill('Отправлено')
            lists.fail('POST', 'edit', outcome)
            if outcome == 'unanswered':
                lists.fail('GET', 'entry', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            text.fill(newer)
            wait_until(lambda: lists.entry(target)['description'] == 'Отправлено', 'the unanswered edit landed')
            session.refresh(frame)
            # The error clears only after pull recognized the pending write, so these checks cannot
            # pass merely because the poll has not returned yet.
            expect(card.locator('.dialogfoot .note')).to_be_hidden()
            expect(text).to_have_value(newer)
            expect(card.locator('.field .note:not([hidden])')).to_have_count(0)
            assert len(lists.calls('POST', 'edit')) == 1, 'polling only reads'
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['description'] == newer
            edits = lists.calls('POST', 'edit')
            assert len(edits) == 2
            assert edits[-1][2]['expected'] == {'description': 'Отправлено'}
            assert edits[-1][2]['changes'] == {'description': newer}
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('reopen', [False, True], ids=['poll', 'resume'])
def test_pending_sent_fields_survive_a_partial_match_while_foreign_changes_conflict(lists, engine, reopen):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Два поля', 'description': 'Было'}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Отправлено')
            card.get_by_label('Срок').fill('Завтра')
            lists.fail('POST', 'edit', 'unanswered')
            lists.fail('GET', 'entry', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            card.get_by_label('Описание').fill('Было')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Было')
            pending = kept(lists)['pending']
            lists.update(target, due='Через неделю')  # only one of our sent fields still matches
            if reopen:
                frame = leave_and_return(session)
                card = resume(frame)
            else:
                session.refresh(frame)
            expect(card.locator('.field .note:not([hidden])')).to_have_count(1)
            expect(card.locator('.field .note:not([hidden]) .theirs')).to_have_text('Через неделю')
            expect(card.get_by_label('Описание')).to_have_value('Было')
            assert len(lists.calls('POST', 'edit')) == 1, 'neither polling nor resuming resends an unknown edit'
            assert kept(lists)['pending']['text'] == pending['text'], 'partial match does not replace the request id'
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('already_refused', [False, True], ids=['stale-bar', 'refused-card'])
def test_delete_draft_is_a_fresh_action_after_a_conflict(lists, engine, already_refused):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Общий черновик', 'description': 'Было'}])
    with sync_playwright() as pw:
        one, two = lists.open(pw, engine), None
        try:
            frame = one.mount()
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Первый черновик')
            wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Первый черновик')
            two = lists.open(pw, engine)
            other = two.mount()
            expect(other.locator('.draftbar')).to_be_visible()
            if already_refused:
                other_card = resume(other)
                other_card.get_by_label('Описание').fill('Другое окно')
                wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Другое окно')
                card.get_by_label('Описание').fill('Отказанная правка')
                expect(card.locator('.dialogfoot')).to_contain_text('хранит другое окно')
                card.get_by_label('Закрыть').click()
                card.get_by_role('button', name='Не сохранять').click()
                expect(card).to_be_hidden()
                deleting = frame
            else:
                card.get_by_label('Описание').fill('Более свежий черновик')
                wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Более свежий черновик')
                deleting = other
                # A stale clear must first learn the newer draft, not erase it on the stale revision.
                deleting.locator('.draftbar').get_by_role('button', name='Удалить черновик').click()
                expect(deleting.locator('.draftbar')).to_be_visible()
                assert kept(lists)['form']['description'] == 'Более свежий черновик'
            bar = deleting.locator('.draftbar')
            before = len(lists.calls('POST', 'draft'))
            bar.get_by_role('button', name='Удалить черновик').click()
            expect(bar).to_be_hidden()
            wait_until(lambda: lists.read('draft')['draft'] is None, 'the explicit delete cleared the slot')
            assert len(lists.calls('POST', 'draft')) == before + 1
            assert deleting.locator('#toast').inner_text() not in ('cleared', 'elsewhere')
            assert lists.entry(target)['description'] == 'Было', 'deleting a draft never edits the task'
        finally:
            one.close()
            if two:
                two.close()


def poll(lists, session, frame):
    """What the 30-second timer does, and wait until its read of the open entry has been applied."""
    before = len(lists.calls('GET', 'entry'))
    session.refresh(frame)
    wait_until(lambda: len(lists.calls('GET', 'entry')) > before, 'the poll read the open entry again')
    frame.wait_for_timeout(250)


def link_names(card):
    return card.locator('.ref .name').all_inner_texts()


@ENGINES
@pytest.mark.parametrize('steps,trigger', [('add-drop', 'save'), ('add-drop', 'poll'), ('add-drop', 'resume'),
                                           ('relabel', 'stage'), ('relabel', 'poll'), ('relabel', 'resume')])
def test_link_steps_keep_their_order_over_every_read_of_the_task(lists, engine, steps, trigger):
    """A link added and then removed is gone; × and adding the same address again (the card's only way
    to change a label) is a new label. Neither turns around when the card reads the task again."""
    from playwright.sync_api import sync_playwright, expect
    url = 'https://docs.example.org/spec'
    start = [{'label': 'Старая подпись', 'target': url}] if steps == 'relabel' else []
    (target,) = lists.add([{'text': 'Ссылки по порядку', 'description': 'Было', 'references': start}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            if steps == 'add-drop':
                card.get_by_label('Адрес ссылки').fill('https://wrong.example.org/not-this')
                card.get_by_label('Подпись ссылки, необязательно').fill('Не та ссылка')
                card.get_by_role('button', name='Добавить').click()
                card.get_by_role('button', name='Убрать ссылку: Не та ссылка').click()
                card.get_by_label('Описание').fill('Дописано')
                want, stored = [], []
            else:
                card.get_by_role('button', name='Убрать ссылку: Старая подпись').click()
                card.get_by_label('Адрес ссылки').fill(url)
                card.get_by_label('Подпись ссылки, необязательно').fill('Новая подпись')
                card.get_by_role('button', name='Добавить').click()
                want, stored = ['Новая подпись'], [{'label': 'Новая подпись', 'target': url}]
            assert link_names(card) == want
            if trigger == 'poll':
                poll(lists, session, frame)
            elif trigger == 'stage':
                card.get_by_label('Стадия').select_option('in_progress')
                wait_until(lambda: lists.entry(target)['stage'] == 'in_progress', 'the stage was saved at once')
                frame.wait_for_timeout(400)
            elif trigger == 'resume':
                wait_until(lambda: len(kept(lists).get('ops', [])) == 2, 'the draft holds both link steps')
                frame = leave_and_return(session)
                card = resume(frame)
                frame.wait_for_timeout(250)
            if trigger != 'save':
                assert link_names(card) == want, 'reading the task again changed the links'
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            entry = lists.entry(target)
            assert entry['details'].get('references', []) == stored
            if steps == 'add-drop':
                assert entry['description'] == 'Дописано'
            assert kept(lists) == {}, 'a card closed with everything saved leaves no draft'
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('trigger', ['poll', 'resume', 'retry'])
def test_keep_mine_is_saved_after_an_unconfirmed_edit_that_had_applied(lists, engine, trigger):
    """The twin of the refused-retry case above, for a write that did reach the store: repeating it is
    answered as a replay with the task as it is now, which already holds the change made elsewhere."""
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Спорное описание', 'description': 'Исходное'}])
    edits = lambda: lists.calls('POST', 'edit')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            card.locator('#sl-description').fill('Моя правка')
            lists.fail('POST', 'edit', 'unanswered')  # applied, the answer was lost
            lists.fail('GET', 'entry', 'lost')        # and the check read failed: the outcome is unknown here
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            lists.update(target, description='Правка агента')
            if trigger == 'poll':
                poll(lists, session, frame)
            elif trigger == 'resume':
                wait_until(lambda: bool((kept(lists).get('pending') or {}).get('text')), 'the draft keeps the request')
                frame = leave_and_return(session)
                card = resume(frame)
            else:
                card.get_by_role('button', name='Готово').click()  # repeated as itself, answered as a replay
                expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранено не всё')
                assert len(edits()) == 2 and edits()[1][2] == edits()[0][2]
            note = card.locator('.field', has=frame.locator('#sl-description')).locator('.note')
            expect(note.locator('.theirs')).to_have_text('Правка агента')
            expect(card.locator('#sl-description')).to_have_value('Моя правка')
            note.get_by_role('button', name='Оставить моё').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['description'] == 'Моя правка'
            last = edits()[-1][2]
            assert last['request_id'] != edits()[0][2]['request_id']
            assert (last['changes'], last['expected']) == ({'description': 'Моя правка'}, {'description': 'Правка агента'})
            # The unconfirmed request, its one repeat (answered as a replay) and the owner's choice.
            assert len(edits()) == 3 and edits()[1][2] == edits()[0][2]
            assert kept(lists) == {}
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('outcome', ['unanswered', 'lost'])
def test_repeating_an_unconfirmed_edit_never_writes_over_a_change_found_meanwhile(lists, engine, outcome):
    """Answering the earlier request reads the task again; a second field changed elsewhere since then is
    shown and waits for the owner's choice instead of being written over by the save that follows."""
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Две правки', 'description': 'Было'}])
    edits = lambda: lists.calls('POST', 'edit')
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Отправлено')
            lists.fail('POST', 'edit', outcome)
            lists.fail('GET', 'entry', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            card.get_by_label('Срок').fill('Завтра')
            lists.update(target, due='Через неделю')
            card.get_by_role('button', name='Готово').click()
            note = card.locator('.field', has=frame.locator('#sl-due')).locator('.note')
            expect(note.locator('.theirs')).to_have_text('Через неделю')
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранено не всё')
            entry = lists.entry(target)
            assert (entry['description'], entry['due']['raw']) == ('Отправлено', 'Через неделю')
            assert len(edits()) == 2 and edits()[1][2]['request_id'] == edits()[0][2]['request_id']
            note.get_by_role('button', name='Оставить моё').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['due']['raw'] == 'Завтра'
            assert (edits()[-1][2]['changes'], edits()[-1][2]['expected']) == ({'due': 'Завтра'}, {'due': 'Через неделю'})
            assert len(edits()) == 3 and kept(lists) == {}
        finally:
            session.close()


@ENGINES
def test_the_saves_own_check_read_keeps_a_field_typed_back_while_it_was_away(lists, engine):
    """As the partial-match case above, but the change elsewhere lands before the save's own check read."""
    from playwright.sync_api import sync_playwright, expect
    from starlette.responses import JSONResponse
    (target,) = lists.add([{'text': 'Два поля', 'description': 'Было'}])
    real = lists.api.routes['edit']['handler']

    def applied_then_changed_elsewhere_then_unanswered(request):
        result = real(request)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        assert result['ok'], result
        lists.update(target, due='Через неделю')
        time.sleep(1.5)  # the owner types meanwhile
        return JSONResponse({'error': 'synthetic outage'}, status_code=503)

    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            description = card.get_by_label('Описание')
            description.fill('Отправлено')
            card.get_by_label('Срок').fill('Завтра')
            lists.api.routes['edit']['handler'] = applied_then_changed_elsewhere_then_unanswered
            try:
                card.get_by_role('button', name='Готово').click()
                expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Сохраняю…')
                description.fill('Было')  # typed back to what it was before the save
                expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            finally:
                lists.api.routes['edit']['handler'] = real
            expect(card.locator('.dialogfoot .note')).to_contain_text('в задаче сейчас только часть отправленного')
            expect(card.locator('.field .note:not([hidden]) .theirs')).to_have_text(['Через неделю'])
            expect(description).to_have_value('Было')
            due = card.locator('.field', has=frame.locator('#sl-due')).locator('.note')
            due.get_by_role('button', name='Взять это значение').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            entry = lists.entry(target)
            assert (entry['description'], entry['due']['raw']) == ('Было', 'Через неделю')
            last = lists.calls('POST', 'edit')[-1][2]
            assert (last['changes'], last['expected']) == ({'description': 'Было'}, {'description': 'Отправлено'})
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('opened', ['open', 'resume'])
def test_closing_while_the_card_reads_keeps_only_a_real_draft(lists, engine, opened):
    from playwright.sync_api import sync_playwright, expect
    (target,) = lists.add([{'text': 'Медленно открывается', 'description': 'Было'}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = frame.locator('dialog')
            if opened == 'resume':
                open_card(frame, target).get_by_label('Описание').fill('Черновик')
                wait_until(lambda: kept(lists).get('form', {}).get('description') == 'Черновик')
                frame = leave_and_return(session)
                card = frame.locator('dialog')
            lists.fail('GET', 'entry', 'slow')  # the entry read answers 1.5 s late
            if opened == 'open':
                frame.locator(f'.task[data-id="{target}"]').click()
            else:
                frame.locator('.draftbar').get_by_role('button', name='Продолжить').click()
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Загружаю…')
            card.get_by_label('Закрыть').click()
            frame.wait_for_timeout(2200)
            if opened == 'open':  # nothing was typed: the card simply closes and keeps nothing
                expect(card).to_be_hidden()
                assert kept(lists) == {}
            else:  # the continued draft is real: closing asks first and the draft stays
                expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Правки не сохранены.')
                expect(card.get_by_role('button', name='Не сохранять')).to_be_visible()
                assert kept(lists)['form']['description'] == 'Черновик'
            assert lists.entry(target)['description'] == 'Было'
        finally:
            session.close()


def pump_until(page, check, what='the expected state', seconds=10):
    """wait_until for a page whose requests the test routes: Playwright serves them only while it is called."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, what
        page.wait_for_timeout(50)


@ENGINES
@pytest.mark.parametrize('opened', ['open', 'resume'])
@pytest.mark.parametrize('outcome', ['answer', 'failure'])
def test_a_late_read_of_a_closed_card_never_changes_the_task_opened_again(lists, engine, opened, outcome):
    """Closed while its first read was away, then the same task opened again and typed in: the late answer
    or failure of that first read belongs to the closed card. The open one keeps its form and its draft."""
    from playwright.sync_api import sync_playwright, expect
    from starlette.responses import JSONResponse
    (target,) = lists.add([{'text': 'Открыта второй раз', 'description': 'Было'}])
    typed = 'Новый ввод после повторного открытия'
    real = lists.api.routes['entry']['handler']
    held, release = threading.Event(), threading.Event()

    def first_read_held(request):
        if held.is_set():
            return real(request)
        held.set()
        assert release.wait(10), 'the test never released the first read'
        return JSONResponse({'error': 'synthetic outage'}, status_code=503) if outcome == 'failure' else real(request)

    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            # Every request passes through the test, so neither engine merges the second read of the
            # same address into the first: the first can answer last, as it may on any network.
            session.page.route('**/*', lambda route: route.continue_())
            frame = session.mount()
            if opened == 'resume':
                open_card(frame, target).get_by_label('Описание').fill('Черновик')
                pump_until(session.page, lambda: kept(lists).get('form', {}).get('description') == 'Черновик')
                frame = leave_and_return(session)
            card = frame.locator('dialog')
            finished = []
            session.page.on('requestfinished', lambda request: '/entry?' in request.url and finished.append(1))
            lists.api.routes['entry']['handler'] = first_read_held
            if opened == 'open':
                frame.locator(f'.task[data-id="{target}"]').click()
            else:
                frame.locator('.draftbar').get_by_role('button', name='Продолжить').click()
            expect(card.locator('.dialogfoot [role="status"]')).to_have_text('Загружаю…')
            pump_until(session.page, held.is_set, 'the first read reached the store')
            card.get_by_label('Закрыть').click()
            if opened == 'resume':  # the continued draft is real: it is discarded on purpose
                card.get_by_role('button', name='Не сохранять').click()
            expect(card).to_be_hidden()
            pump_until(session.page, lambda: kept(lists) == {}, 'nothing is kept from the closed card')

            card = open_card(frame, target)
            card.get_by_label('Описание').fill(typed)
            pump_until(session.page, lambda: kept(lists).get('form', {}).get('description') == typed,
                       'the new input is in the draft')
            release.set()
            pump_until(session.page, lambda: len(finished) == 2, 'the first read answered after the second')
            session.page.wait_for_timeout(400)  # its answer reaches the frame through the host
            expect(card.get_by_label('Описание')).to_have_value(typed)
            expect(card.locator('.dialogfoot .note')).to_be_hidden()
            expect(card.locator('.dialogbody')).not_to_have_attribute('inert', '')
            assert kept(lists).get('form', {}).get('description') == typed, 'the draft still holds the new input'
            assert not lists.calls('POST', 'edit')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['description'] == typed
            assert lists.calls('POST', 'edit')[-1][2]['expected'] == {'description': 'Было'}
        finally:
            release.set()
            lists.api.routes['entry']['handler'] = real
            session.close()


@ENGINES
@pytest.mark.parametrize('case,trigger', [('relabel', 'poll'), ('relabel', 'save'), ('add', 'poll'),
                                          ('add-drop', 'poll'), ('add-drop', 'save')])
def test_one_address_changed_here_and_elsewhere_is_asked_about_and_a_cancelled_link_drops_nothing(
        lists, engine, case, trigger):
    """The card's link steps are kept by address. A label given here while the same address got another
    one elsewhere stays on screen and is asked about like any field; a link added and removed again
    here leaves nothing to save, even when the same link was added elsewhere meanwhile."""
    from playwright.sync_api import sync_playwright, expect
    url = 'https://docs.example.org/spec'
    start = [{'label': 'Старая подпись', 'target': url}] if case == 'relabel' else []
    (target,) = lists.add([{'text': 'Одна ссылка в двух местах', 'description': 'Было', 'references': start}])
    theirs = [{'label': 'Подпись агента', 'target': url}]
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)

            def add(label):
                card.get_by_label('Адрес ссылки').fill(url)
                card.get_by_label('Подпись ссылки, необязательно').fill(label)
                card.get_by_role('button', name='Добавить').click()
            if case == 'relabel':
                card.get_by_role('button', name='Убрать ссылку: Старая подпись').click()
                add('Моя подпись')
            elif case == 'add':
                add('Моя подпись')
            else:
                add('Подпись агента')
                card.get_by_role('button', name='Убрать ссылку: Подпись агента').click()
                card.get_by_label('Описание').fill('Дописано')
            lists.update(target, details={'references': theirs})
            edits = len(lists.calls('POST', 'edit'))
            if trigger == 'poll':
                poll(lists, session, frame)
            else:
                card.get_by_role('button', name='Готово').click()
            asked = card.locator('.field', has=frame.locator('.refs')).locator('.note', has=frame.locator('.theirs'))
            if case == 'add-drop':
                if trigger == 'save':
                    expect(card).to_be_hidden()
                else:
                    assert link_names(card) == ['Подпись агента']
                    expect(asked).to_have_count(0)
                    card.get_by_role('button', name='Готово').click()
                    expect(card).to_be_hidden()
                entry = lists.entry(target)
                assert entry['details']['references'] == theirs, 'the link added elsewhere stays'
                assert entry['description'] == 'Дописано'
                assert kept(lists) == {}
                return
            expect(asked).to_contain_text('изменили в другом месте')
            expect(asked.locator('.theirs')).to_have_text(f'«Подпись агента» — {url}')
            assert link_names(card) == ['Моя подпись'], "the owner's label stays on screen"
            assert lists.entry(target)['details']['references'] == theirs
            if trigger == 'save':
                assert len(lists.calls('POST', 'edit')) == edits + 1, 'the store refused the stale list'
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сначала выбери')
            wait_until(lambda: kept(lists).get('conflicts', {}).get('references') == [{'target': url,
                       'label': 'Подпись агента'}], 'the question is kept with the draft')
            sent = len(lists.calls('POST', 'edit'))
            if case == 'relabel':
                asked.get_by_role('button', name='Оставить моё').click()
                expect(asked).to_have_count(0)
                card.get_by_role('button', name='Готово').click()
                expect(card).to_be_hidden()
                assert lists.entry(target)['details']['references'] == [{'label': 'Моя подпись', 'target': url}]
                last = lists.calls('POST', 'edit')[-1][2]
                assert last['expected'] == {'details': {'references': theirs}}
            else:
                asked.get_by_role('button', name='Взять это значение').click()
                expect(asked).to_have_count(0)
                assert link_names(card) == ['Подпись агента']
                card.get_by_role('button', name='Готово').click()
                expect(card).to_be_hidden()
                assert len(lists.calls('POST', 'edit')) == sent, 'nothing was left to save'
                assert lists.entry(target)['details']['references'] == theirs
            assert kept(lists) == {}
        finally:
            session.close()


SPEC = 'https://docs.example.org/spec'
SAME_ADDRESS = [{'label': label, 'target': SPEC} for label in ('Первая подпись', 'Вторая подпись', 'Третья подпись')]


def drop_link(card, ref):
    card.get_by_role('button', name=f"Убрать ссылку: {ref['label']}").click()


def add_link(card, ref):
    card.get_by_label('Адрес ссылки').fill(ref['target'])
    card.get_by_label('Подпись ссылки, необязательно').fill(ref['label'])
    card.get_by_role('button', name='Добавить').click()


@ENGINES
@pytest.mark.parametrize('removed,trigger', [('first', 'poll'), ('second', 'poll'), ('both', 'poll'),
                                             ('second', 'resume')])
def test_links_that_share_one_address_are_removed_one_by_one_over_every_read(lists, engine, removed, trigger):
    """The store keeps several links with one address (an agent may write them). The card's × removes
    exactly the one chosen, or each of them, and reading the task again brings none of them back."""
    from playwright.sync_api import sync_playwright, expect
    first, second = SAME_ADDRESS[:2]
    other = {'label': 'Другая ссылка', 'target': 'https://other.example.org/page'}
    (target,) = lists.add([{'text': 'Две ссылки одного адреса', 'references': [first, second, other]}])
    gone = [first, second] if removed == 'both' else [first if removed == 'first' else second]
    stays = [ref for ref in (first, second, other) if ref not in gone]
    names = [ref['label'] for ref in stays]
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            for ref in gone:
                drop_link(card, ref)
            assert link_names(card) == names
            if trigger == 'poll':
                poll(lists, session, frame)
            else:
                wait_until(lambda: len(kept(lists).get('ops', [])) == len(gone), 'the draft holds the removal')
                frame = leave_and_return(session)
                card = resume(frame)
                frame.wait_for_timeout(250)
            assert link_names(card) == names, 'reading the task again brought a removed link back'
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['details']['references'] == stays
            (edit,) = lists.calls('POST', 'edit')
            assert edit[2]['changes'] == {'details': {'references': stays}}
            assert kept(lists) == {}
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('choice', ['keep', 'take'])
def test_links_of_one_address_changed_here_and_elsewhere_are_asked_about_together(lists, engine, choice):
    """One of three links of an address removed here, another one elsewhere: the question shows every
    link the store now holds there, and either answer leaves exactly the version chosen."""
    from playwright.sync_api import sync_playwright, expect
    first, second, third = SAME_ADDRESS
    (target,) = lists.add([{'text': 'Три ссылки одного адреса', 'references': SAME_ADDRESS}])
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            drop_link(card, third)
            lists.update(target, details={'references': [second, third]})
            poll(lists, session, frame)
            asked = card.locator('.field', has=frame.locator('.refs')).locator('.note', has=frame.locator('.theirs'))
            expect(asked.locator('.theirs')).to_have_text(
                f"«{second['label']}» — {SPEC} «{third['label']}» — {SPEC}")
            assert link_names(card) == [second['label'], first['label']], "the owner's version stays on screen"
            if choice == 'keep':
                asked.get_by_role('button', name='Оставить моё').click()
                card.get_by_role('button', name='Готово').click()
                expect(card).to_be_hidden()
                (edit,) = lists.calls('POST', 'edit')
                assert edit[2]['expected'] == {'details': {'references': [second, third]}}
                assert lists.entry(target)['details']['references'] == [second, first]
            else:
                asked.get_by_role('button', name='Взять это значение').click()
                assert link_names(card) == [second['label'], third['label']]
                card.get_by_role('button', name='Готово').click()
                expect(card).to_be_hidden()
                assert not lists.calls('POST', 'edit'), 'nothing was left to save'
                assert lists.entry(target)['details']['references'] == [second, third]
            assert kept(lists) == {}
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('case,trigger', [('relabel-back', 'poll'), ('relabel-back', 'resume'),
                                          ('add-remove', 'poll'), ('relabel-back', 'elsewhere')])
def test_a_link_change_taken_back_after_its_unconfirmed_write_is_not_what_stays_saved(
        lists, engine, case, trigger):
    """A save that carried a link change got no answer and had not reached the store; the owner then puts
    the link back as it was. That address was sent, so it is not untouched: reading the task again keeps
    the owner's version, «Готово» repeats the request as itself, and what the form shows is saved over it.
    A different label given elsewhere meanwhile is asked about, as for any field sent and typed back."""
    from playwright.sync_api import sync_playwright, expect
    old, mine = {'label': 'Старая подпись', 'target': SPEC}, {'label': 'Моя подпись', 'target': SPEC}
    theirs = [{'label': 'Подпись агента', 'target': SPEC}]
    start = [old] if case == 'relabel-back' else []
    names = [ref['label'] for ref in start]
    (target,) = lists.add([{'text': 'Отменённая правка ссылки', 'references': start}])
    edits = lambda: [call[2] for call in lists.calls('POST', 'edit')]
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            for ref in start:
                drop_link(card, ref)
            add_link(card, mine)
            lists.fail('POST', 'edit', 'lost')  # never reached the store; the check read answers
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            assert lists.entry(target)['details'].get('references', []) == start
            drop_link(card, mine)
            for ref in start:
                add_link(card, ref)
            assert link_names(card) == names
            if trigger == 'resume':
                wait_until(lambda: len(kept(lists).get('ops', [])) == 4 and bool(kept(lists).get('pending')),
                           'the draft holds the request and every link step')
                frame = leave_and_return(session)
                card = resume(frame)
                frame.wait_for_timeout(250)
            else:
                if trigger == 'elsewhere':
                    lists.update(target, details={'references': theirs})
                poll(lists, session, frame)
            assert link_names(card) == names, "reading the task again dropped the owner's last version"
            if trigger == 'elsewhere':
                asked = card.locator('.field', has=frame.locator('.refs')).locator('.note', has=frame.locator('.theirs'))
                expect(asked.locator('.theirs')).to_have_text(f'«Подпись агента» — {SPEC}')
                asked.get_by_role('button', name='Оставить моё').click()
                card.get_by_role('button', name='Готово').click()  # the repeat is refused: the task moved
                expect(card.locator('.dialogfoot .note')).to_contain_text('нажми «Готово» ещё раз')
                assert link_names(card) == names
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['details'].get('references', []) == start
            sent = edits()
            assert len(sent) == 3 and sent[1] == sent[0], 'the unconfirmed request is repeated as itself, once'
            assert sent[2]['request_id'] != sent[0]['request_id']
            assert sent[2]['changes'] == {'details': {'references': start}}
            assert sent[2]['expected'] == {'details': {'references': theirs if trigger == 'elsewhere' else [mine]}}
            assert kept(lists) == {}
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('case,then', [('relabel', 'retry'), ('remove', 'retry'), ('remove', 'poll'),
                                       ('relabel', 'refused')])
def test_a_link_value_taken_while_an_earlier_write_is_unconfirmed_is_what_stays_saved(lists, engine, case, then):
    """A save that carried a link change got no answer and had not reached the store. The link then gets
    another label elsewhere, and the owner answers «Взять это значение». That address was sent with something
    else, so the value taken is the owner's version: if the store returns to what the old request expects,
    «Готово» repeats that request as itself and then saves the value taken over it; a poll that sees the
    return asks first. While the store still holds the value taken, the repeat is refused and nothing is left."""
    from playwright.sync_api import sync_playwright, expect
    old, mine = {'label': 'Старая подпись', 'target': SPEC}, {'label': 'Моя подпись', 'target': SPEC}
    theirs = {'label': 'Подпись агента', 'target': SPEC}
    carried = [mine] if case == 'relabel' else []
    (target,) = lists.add([{'text': 'Выбор при неподтверждённой записи', 'references': [old]}])
    edits = lambda: [call[2] for call in lists.calls('POST', 'edit')]
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            drop_link(card, old)
            for ref in carried:
                add_link(card, ref)
            lists.fail('POST', 'edit', 'lost')  # never reached the store; the check read answers
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            lists.update(target, details={'references': [theirs]})
            poll(lists, session, frame)
            asked = card.locator('.field', has=frame.locator('.refs')).locator('.note', has=frame.locator('.theirs'))
            expect(asked.locator('.theirs')).to_have_text(f'«Подпись агента» — {SPEC}')
            asked.get_by_role('button', name='Взять это значение').click()
            assert link_names(card) == ['Подпись агента']
            if then != 'refused':
                lists.update(target, details={'references': [old]})  # elsewhere: just what the old request expects
            if then == 'poll':
                poll(lists, session, frame)
                expect(asked.locator('.theirs')).to_have_text(f'«Старая подпись» — {SPEC}')
                assert link_names(card) == ['Подпись агента'], 'the value taken stays on screen until chosen again'
                asked.get_by_role('button', name='Оставить моё').click()
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['details']['references'] == [theirs], 'the card closed showing this link'
            sent = edits()
            assert sent[1] == sent[0], 'the unconfirmed request is repeated as itself'
            assert sent[0]['changes'] == {'details': {'references': carried}}
            if then == 'refused':
                assert len(sent) == 2, 'the store already holds the value taken'
            else:
                assert len(sent) == 3 and sent[2]['request_id'] != sent[0]['request_id']
                assert sent[2]['changes'] == {'details': {'references': [theirs]}}
                assert sent[2]['expected'] == {'details': {'references': carried}}
            assert kept(lists) == {}
        finally:
            session.close()


@ENGINES
@pytest.mark.parametrize('trigger', ['poll', 'resume'])
def test_a_link_stored_twice_and_put_back_after_its_unconfirmed_removal_is_not_lost(lists, engine, trigger):
    """The store accepts the same link twice (an agent may write it), and the card's × removes such a pair
    together. The removal is saved without an answer, the owner adds the link again, and the task is read
    again: the link put back is the owner's version. «Готово» repeats the removal as itself and then saves
    the link, and a pair stored twice under an address the card did not touch is written back as it was."""
    from playwright.sync_api import sync_playwright, expect
    twin = {'label': 'Спецификация', 'target': SPEC}
    other = {'label': 'Другая ссылка', 'target': 'https://other.example.org/page'}
    spare = {'label': 'Черновик', 'target': 'https://other.example.org/draft'}
    rest = [other, spare, dict(spare)]
    (target,) = lists.add([{'text': 'Ссылка, записанная дважды', 'references': [twin, dict(twin), *rest]}])
    stored = lambda: lists.entry(target)['details'].get('references', [])
    assert stored() == [twin, twin, *rest], 'the store keeps both links of a pair'
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            card = open_card(frame, target)
            card.get_by_role('button', name=f"Убрать ссылку: {twin['label']}").first.click()
            assert link_names(card) == [ref['label'] for ref in rest]
            lists.fail('POST', 'edit', 'lost')  # never reached the store; the check read answers
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('Сохранение не подтверждено')
            assert stored() == [twin, twin, *rest]
            add_link(card, twin)
            assert link_names(card) == [*(ref['label'] for ref in rest), twin['label']]
            if trigger == 'poll':
                poll(lists, session, frame)
            else:
                wait_until(lambda: bool(kept(lists).get('pending')) and any('ref' in op for op in kept(lists)['ops']),
                           'the draft holds the request and the link put back')
                frame = leave_and_return(session)
                card = resume(frame)
                frame.wait_for_timeout(250)
            assert twin['label'] in link_names(card)
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert stored() == [*rest, twin], 'the card closed showing this link'
            sent = [call[2] for call in lists.calls('POST', 'edit')]
            assert len(sent) == 3 and sent[1] == sent[0], 'the unconfirmed request is repeated as itself, once'
            assert sent[0]['changes'] == {'details': {'references': rest}}
            assert sent[2]['request_id'] != sent[0]['request_id']
            assert sent[2]['changes'] == {'details': {'references': [*rest, twin]}}
            assert sent[2]['expected'] == {'details': {'references': rest}}
            assert kept(lists) == {}
        finally:
            session.close()


@ENGINES
def test_a_new_task_keeps_its_title_exactly_as_typed(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    title = '  Купить молоко  '
    with sync_playwright() as pw:
        session = lists.open(pw, engine)
        try:
            frame = session.mount()
            frame.get_by_role('button', name='+ Задача').click()
            card = frame.locator('dialog')
            card.get_by_label('Название задачи').fill(title)
            card.get_by_role('button', name='Создать').click()
            expect(card).to_be_hidden()
            assert [call[2]['text'] for call in lists.calls('POST', 'add')] == [title]
            assert not lists.calls('POST', 'edit'), 'the created title is not edited afterwards'
            assert on_board(lists, title) == 1 and kept(lists) == {}
        finally:
            session.close()
