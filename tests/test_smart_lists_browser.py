"""Opt-in: the real Smart Lists widget in the real module frame, over the registered routes.

Set OUROBOROS_THEME_CORE_ROOT to a core checkout; Playwright WebKit (the macOS app's
engine) is then required and a missing one fails. Chromium runs when installed and is
skipped only when Playwright has no Chromium build on this machine. The widget source is mounted by the core's own
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
    pytest.importorskip('playwright.sync_api')
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
    # errors: a route handler that raised; disconnects: answers the browser closed before reading.
    state = {'calls': [], 'faults': [], 'errors': [], 'widget_faults': [], 'disconnects': []}

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
                state['calls'].append((method, name, body, url.query))
                fault = next((f for f in state['faults'] if f[0] == method and f[1] == name), None)
                if fault:
                    state['faults'].remove(fault)
                    if isinstance(fault[2], dict):  # a typed answer the store gives in a state a test cannot make
                        return self.send(fault[2])
                    if fault[2] == 'slow':  # the store answers, late enough for the owner to keep typing
                        time.sleep(1.5)
                    if fault[2] == 'lost':  # never reached the store
                        return self.send({'error': 'synthetic outage'}, status=503)
                    if fault[2] == 'refused':
                        return self.send({'ok': False, 'error': 'Synthetic refusal; nothing changed'})
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
        with urllib.request.urlopen(f'{self.url}{PREFIX}{name}?{query}') as response:
            result = json.loads(response.read())
        self.state['calls'].pop()  # the test's own read is not the widget's
        return result

    def update(self, entry_id, **fields):
        self._serial += 1
        return self.tool('update', entry_id=entry_id, request_id=f'agent-{self._serial}', **fields)

    def calls(self, method, name):
        return [call for call in self.state['calls'] if call[0] == method and call[1] == name]

    def fail(self, method, name, mode):
        self.state['faults'].append((method, name, mode))

    def open(self, pw, engine, shape=OWNER, theme='light'):
        try:
            browser = getattr(pw, engine).launch(headless=True)
        except Exception as exc:
            # WebKit is the engine the owner's app uses, so without it this suite proves nothing:
            # that is a failure. Only a Chromium build this machine never installed is skipped.
            if engine == 'chromium' and "Executable doesn't exist" in str(exc):
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

            # The write never arrived: said plainly, the draft stays, and nothing is sent again by itself.
            card = open_card(frame, target)
            card.get_by_label('Описание').fill('Вторая правка')
            lists.fail('POST', 'edit', 'lost')
            card.get_by_role('button', name='Готово').click()
            expect(card.locator('.dialogfoot .note')).to_contain_text('запись осталась прежней')
            expect(card.get_by_label('Описание')).to_have_value('Вторая правка')
            session.page.wait_for_timeout(400)
            assert len(edits()) == 2 and lists.entry(target)['description'] == 'Первая правка'
            session.shot(f'unanswered-{engine}-light.png')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert len(edits()) == 3 and lists.entry(target)['description'] == 'Вторая правка'

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

            # A failed board read keeps the last board on screen and says it is stale.
            lists.fail('GET', 'board', 'refused')
            session.refresh(frame)
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

            card = open_card(frame, target)
            card.locator('#sl-description').fill('Новое описание')
            card.get_by_role('button', name='Готово').click()
            expect(card).to_be_hidden()
            assert lists.entry(target)['details']['tags'] == tags
            assert lists.calls('POST', 'edit')[-1][2]['changes'] == {'description': 'Новое описание'}
        finally:
            session.close()


@ENGINES
def test_edits_typed_while_saving_or_archiving_stay_on_screen(lists, engine):
    from playwright.sync_api import sync_playwright, expect
    first, second, third = lists.add([{'text': f'Задача {n}', 'description': 'Было'} for n in (1, 2, 3)])
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
