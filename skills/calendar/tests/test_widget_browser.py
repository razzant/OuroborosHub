"""Rendered calendar widget regression checks (optional local Playwright Chromium).

The grid tests mount the widget against a one-route fake. The Accounts and link tests script the whole skill behind
the same bridge, so the forms, failures, waiting and link actions run exactly as the widget sends them; the real
routes behind the real host bridge are covered in the OuroborosHub repository, outside this payload:
https://github.com/razzant/OuroborosHub/blob/main/tests/test_calendar_host_browser.py.
"""

import os
import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None

WIDGET = pathlib.Path(__file__).resolve().parents[1] / "widget.js"
EVIDENCE = os.environ.get("OUROBOROS_CALENDAR_EVIDENCE_DIR")
NOW = "2026-09-27T18:30:00+04:00"
AUTH = "https://accounts.example.test/auth?state=abc"
SECRET = "S3cret-pass-123"
LONG = "Очень длинное название рабочего календаря для проверки переноса строк в узком окне"

# A scripted skill behind the widget bridge: every route is a function a test may replace in backend.routes.
BRIDGE = r"""cfg => {
    const backend = {accounts: cfg.accounts || [], calendars: cfg.calendars || [], events: cfg.events || [], linkIssues: cfg.linkIssues || [],
        eventGet: cfg.eventGet || {}, options: cfg.options, settings: cfg.settings, now: cfg.now, opened: [], hold: null, routes: {},
        openResult: {ok: true, native: false, host: 'browser'}};
    const calls = [], disposers = [];
    const reply = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
    const day = '2026-09-27';
    const defaults = {
        agenda: async () => {
            if (backend.hold) await backend.hold;
            return reply({status: 'ok', view: 'day', anchor: day, timezone: 'Asia/Dubai', now: backend.now, days: [{date: day, weekday: 'Вс'}],
                calendars: backend.calendars, events: backend.events, hidden_busy: [], usual: [], link_issues: backend.linkIssues,
                sync: {pending: 0, conflict: 0, failed: 0, companion: 'alive'},
                accounts: backend.accounts.map(a => ({id: a.id, alias: a.alias, provider: a.provider, status: a.status, last_sync: a.last_sync}))});
        },
        status: async () => reply({status: 'ok', accounts: backend.accounts, calendars: backend.calendars,
            connection_options: backend.options, settings: backend.settings}),
        'event/get': async query => backend.eventGet[query.id] ? reply({status: 'ok', event: backend.eventGet[query.id]})
            : reply({status: 'not_found', message: 'событие не найдено'}, 404),
    };
    window.backend = backend; window.calls = calls; window.disposers = disposers; window.reply = reply;
    window.__ouroWidgetOnDispose = fn => { disposers.push(fn); };
    window.OuroborosWidget = {
        onTheme(callback) { window.setTheme = callback; return () => { window.themeOff = true; }; },
        async openExternal(url) {
            backend.opened.push(url);
            if (backend.openResult instanceof Error) throw backend.openResult;
            return backend.openResult;
        },
        async fetch(path, init) {
            const url = new URL(path, 'http://skill.test'), route = url.pathname.replace('/api/extensions/calendar/', '');
            const query = Object.fromEntries(url.searchParams), body = init && init.body ? JSON.parse(init.body) : null;
            calls.push({method: (init && init.method) || 'GET', route, query, body});
            const handler = backend.routes[route] || defaults[route];
            return handler ? await handler(query, body) : reply({status: 'error', message: 'нет маршрута ' + route}, 404);
        },
    };
}"""

CONTRAST = r"""([fg, bg]) => {
    const rgb = c => c.match(/[\d.]+/g).slice(0, 3).map(Number);
    const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
    const [hi, lo] = [lum(rgb(fg)), lum(rgb(bg))].sort((a, b) => b - a);
    return (hi + 0.05) / (lo + 0.05);
}"""


def minutes_ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def account(provider, alias, status="ok", **extra):
    login = alias.lower() + "@example.test"
    return {"id": f"{provider}:{login}", "provider": provider, "alias": alias, "login": login, "status": status,
            "last_sync": minutes_ago(5), "last_error": "", "capabilities": {"create": True, "update": True, "delete": True, "sharing": False},
            **extra}


LOCAL = {"id": "local", "provider": "local", "alias": "local", "login": "", "status": "ok", "last_sync": None, "last_error": ""}


def calendar(id, name, account_id="local", provider="local", writable=True):
    return {"id": id, "name": name, "provider": provider, "account_id": account_id, "writable": writable, "visible": True, "default": id == "work"}


def event_row(id, calendar_id, calendar_name, **extra):
    return {"id": id, "title": "Planning", "start": "2026-09-27T18:00:00+04:00", "end": "2026-09-27T19:00:00+04:00", "all_day": False,
            "calendar_id": calendar_id, "calendar_name": calendar_name, "visibility": "shown", "availability": "busy", "status": "confirmed", **extra}


def linked(copy_state="following", ambiguous=False, source_missing=False, series=False):
    """One source in «Work» and one copy in «Personal», as the agenda rows and event/get describe them."""
    suffix = "@2026-09-27T14:00:00+00:00" if series else ""
    recurring = {"series_id": "cp", "rrule": "FREQ=WEEKLY"} if series else {}
    link = {"source_id": None if ambiguous else "src", "state": copy_state, "source_missing": source_missing, "source_ambiguous": ambiguous,
            "source": None if ambiguous else {"id": "src", "title": "Planning", "calendar_id": "work", "calendar_name": "Work"},
            "members": [{"id": "src", "calendar_id": "work", "calendar_name": "Work", "source": not ambiguous, "state": "following", "missing": False},
                        {"id": "cp", "calendar_id": "personal", "calendar_name": "Personal", "source": False, "state": copy_state, "missing": copy_state == "missing"}]}
    rows = [event_row("src" + suffix, "work", "Work", link_group_id="g", is_primary=not ambiguous, link_state="following"),
            event_row("cp" + suffix, "personal", "Personal", link_group_id="g", is_primary=False, link_state=copy_state, **recurring)]
    details = {row["id"]: {**row, "description": "", "location": "", "reminders": [], "attendees": [],
                           "assignments": [{"calendar_id": row["calendar_id"]}], "link": link} for row in rows}
    return rows, details


CALENDARS = [calendar("work", "Work"), calendar("personal", "Personal")]


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(headless=True)
        except Exception as error:
            self.playwright.stop()
            self.skipTest(f"Chromium is not installed: {error}")
        self.page = self.browser.new_page(viewport={"width": 950, "height": 780})
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        expect.set_options(timeout=20_000)   # shared machines are slow; a wrong state still fails, only later

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()
        self.assertEqual(self.page_errors, [])   # an uncaught error in the widget is a failure even when the assertions held


class CalendarWidgetBrowserTests(BrowserCase):
    def mount(self, now):
        self.page.set_content('<!doctype html><div id="root"></div>')
        self.page.evaluate("""now => {
            window.OuroborosWidget = {fetch: async path => {
                const week = path.includes('view=week');
                const day = /[?&]date=([^&]+)/.exec(path);
                const anchor = day ? decodeURIComponent(day[1]) : '2026-09-27';
                const days = Array.from({length: week ? 7 : 1}, (_, i) => {
                    const d = new Date(anchor + 'T00:00:00'); d.setDate(d.getDate() + i);
                    return {date: [d.getFullYear(), String(d.getMonth()+1).padStart(2,'0'), String(d.getDate()).padStart(2,'0')].join('-'), weekday:'Вс'};
                });
                return {ok: true, json: async () => ({anchor, days, now, timezone:'Asia/Dubai',
                    calendars:[], events:[{id:'e', title:'Meeting', start:'2026-09-27T18:00:00+04:00', end:'2026-09-27T19:00:00+04:00'}],
                    hidden_busy:[], usual:[], accounts:[], sync:{companion:'ready'}})};
            }};
        }""", now)
        self.page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        self.page.locator('.grid-scroll').wait_for()

    def position(self):
        return self.page.evaluate("""() => {
            const box = document.querySelector('.grid-scroll');
            const line = box.querySelector('.now');
            const a = box.getBoundingClientRect(), b = line && line.getBoundingClientRect();
            return {height: box.clientHeight, scroll: box.scrollTop, content: box.scrollHeight,
                lineTop: b && b.top - a.top, count: box.querySelectorAll('.col').length,
                day: line && line.parentElement.dataset.date};
        }""")

    def test_today_is_focused_in_day_week_and_every_zoom(self):
        self.mount('2026-09-27T18:30:00+04:00')
        first = self.position()
        self.assertEqual(first['height'], 400)
        self.assertGreater(first['content'] / first['height'], 2)
        self.assertEqual(first['day'], '2026-09-27')
        self.assertGreater(first['lineTop'], 30)
        self.assertLess(first['lineTop'], first['height'])
        for button in ('Увеличить масштаб сетки', 'Уменьшить масштаб сетки', 'Уменьшить масштаб сетки'):
            self.page.get_by_role('button', name=button).click()
            p = self.position()
            self.assertEqual(p['day'], '2026-09-27')
            self.assertGreater(p['lineTop'], 30)
            self.assertLess(p['lineTop'], p['height'])
            self.assertEqual(self.page.evaluate("document.activeElement.closest('.scale') !== null"), True)
        self.page.get_by_role('button', name='Неделя').click()
        p = self.position()
        self.assertEqual(p['count'], 7)
        self.assertEqual(p['day'], '2026-09-27')
        self.assertGreaterEqual(p['lineTop'], 30)
        self.assertLess(p['lineTop'], p['height'])

    def test_day_boundaries_and_no_false_line_on_other_date(self):
        for now in ('2026-09-27T00:05:00+04:00', '2026-09-27T23:55:00+04:00'):
            with self.subTest(now=now):
                self.mount(now)
                p = self.position()
                self.assertEqual(p['day'], '2026-09-27')
                self.assertGreaterEqual(p['lineTop'], 29)
                self.assertLess(p['lineTop'], p['height'])
                self.page.get_by_role('button', name='Увеличить масштаб сетки').click()
                p = self.position()
                self.assertGreaterEqual(p['lineTop'], 29)
                self.assertLess(p['lineTop'], p['height'])
                if '23:55' in now:
                    self.assertGreater(p['height'] - p['lineTop'], 25)
                self.page.get_by_role('button', name='›').click()
                self.assertEqual(self.page.locator('.now').count(), 0)

    def test_today_tracks_rollover_but_manual_navigation_pins_date(self):
        self.mount('2026-09-27T23:55:00+04:00')
        self.page.evaluate("""() => {
            const previous = OuroborosWidget.fetch;
            OuroborosWidget.fetch = async path => {
                const response = await previous(path), data = await response.json();
                data.now = '2026-09-28T00:10:00+04:00';
                if (/[?&]date=(&|$)/.test(path)) {
                    data.anchor = '2026-09-28';
                    data.days = [{date:'2026-09-28', weekday:'Пн'}];
                }
                return {ok:true, json:async () => data};
            };
        }""")
        self.page.get_by_role('button', name='Служебные').click()
        self.assertEqual(self.position()['day'], '2026-09-28')
        self.page.get_by_role('button', name='‹').click()
        self.assertEqual(self.page.locator('.now').count(), 0)
        self.assertIn('2026-09-27', self.page.locator('.bar .title').inner_text())

    def mount_recurring_card(self):
        self.mount('2026-09-27T18:30:00+04:00')
        self.page.evaluate("""() => {
            const old = OuroborosWidget.fetch;
            window.posted = [];
            OuroborosWidget.fetch = async (path, init) => {
                if (init && init.method === 'POST') {
                    posted.push({path, body: JSON.parse(init.body)});
                    return {ok:true, json:async () => ({status:'done', assignments:[]})};
                }
                if (path.includes('event/get')) return {ok:true, json:async () => ({event:{
                    description:'', reminders:[], attendees:[], assignments:[{calendar_id:'local:personal'}]}})};
                const response = await old(path), d = await response.json();
                d.calendars = [{id:'local:personal', name:'Personal', provider:'local', writable:true, default:true}];
                d.events[0] = {...d.events[0], id:'series@2026-09-27T14:00:00Z', series_id:'series',
                    calendar_id:'local:personal', availability:'busy', rrule:'FREQ=WEEKLY'};
                return {ok:true, json:async () => d};
            };
        }""")
        self.page.get_by_role('button', name='Служебные').click()
        self.page.locator('.ev').click()
        self.page.wait_for_function("!document.querySelector('.card .status').textContent.includes('Загружаю')")

    def test_all_scope_survives_delete_confirmation(self):
        self.mount_recurring_card()
        self.page.locator('.card select').last.select_option('all')
        self.page.get_by_role('button', name='Удалить', exact=True).click()
        self.assertEqual(self.page.locator('.card select').last.input_value(), 'all')
        self.page.get_by_role('button', name='Точно удалить (всё расписание)?', exact=True).click()
        body = self.page.evaluate('posted[0].body')
        self.assertEqual(body['scope'], 'all')
        self.assertIn('event/delete', self.page.evaluate('posted[0].path'))

    def test_title_only_series_save_does_not_send_prefilled_times(self):
        self.mount_recurring_card()
        self.page.locator('.card select').last.select_option('following')
        self.page.get_by_placeholder('Название').fill('Renamed')
        self.page.get_by_role('button', name='Сохранить', exact=True).click()
        body = self.page.evaluate('posted[0].body')
        self.assertEqual(body['scope'], 'following')
        self.assertEqual(body['title'], 'Renamed')
        self.assertNotIn('start', body)
        self.assertNotIn('end', body)


class SkillCase(BrowserCase):
    """The widget in front of a scripted skill."""

    def mount_skill(self, clock=False, **cfg):
        if clock:
            self.page.clock.install()
        self.page.set_content('<!doctype html><div id="root"></div>')
        self.page.evaluate(BRIDGE, {"now": NOW, "calendars": CALENDARS, **cfg})
        self.page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        self.page.locator('.grid-scroll').wait_for()

    def sent(self, route):
        return self.page.evaluate('route => calls.filter(c => c.route === route)', route)

    def calls_reach(self, route, count):
        self.page.wait_for_function('([route, count]) => calls.filter(c => c.route === route).length >= count', arg=[route, count])

    def open_accounts(self):
        self.page.get_by_role('button', name='Аккаунты', exact=True).click()
        expect(self.page.locator('section.acc')).to_be_visible()
        self.calls_reach('status', 1)

    def route(self, name, body, status=200):
        """Replace one route with a fixed answer (the body is data, not code)."""
        self.page.evaluate('([name, body, status]) => { backend.routes[name] = async () => reply(body, status); }', [name, body, status])

    def row(self, acc):
        return self.page.locator(f'section.acc .acc-row[data-id="{acc["id"]}"]')

    def shot(self, name):
        if EVIDENCE:
            os.makedirs(EVIDENCE, exist_ok=True)
            self.page.screenshot(path=os.path.join(EVIDENCE, name + '.png'), full_page=True)


class AccountsBrowserTests(SkillCase):
    def test_each_account_shows_its_own_readiness_and_only_real_problems(self):
        ready = account('google', 'Ready', last_error='stale failure of an old sync',
                        capabilities={'create': True, 'update': None, 'delete': False, 'sharing': None})
        denied = account('caldav', 'Denied', status='auth_failed', last_error='server said 401', server_url='https://dav.example.test/dav/')
        orphan = account('yandex', 'Orphan', adapter_error='saved credentials are missing')
        pending = account('caldav', 'Pending', status='connecting', last_sync=None)
        self.mount_skill(accounts=[ready, denied, orphan, pending, LOCAL])
        self.open_accounts()
        expect(self.page.locator('section.acc .acc-row')).to_have_count(4)   # the local calendars are not a connection
        tones = [self.row(a).locator('.dot').get_attribute('class') for a in (ready, denied, orphan, pending)]
        self.assertEqual(tones, ['dot ok', 'dot err', 'dot err', 'dot warn'])   # «ok» without credentials is still not ready
        self.assertNotIn('stale failure', self.row(ready).inner_text())
        self.assertIn('server said 401', self.row(denied).inner_text())
        self.assertIn('dav.example.test', self.row(denied).inner_text())
        self.assertIn('saved credentials are missing', self.row(orphan).inner_text())
        values = [part.rsplit(': ', 1)[1] for part in self.row(ready).locator('.caps').inner_text().split(' · ')]
        self.assertEqual(len(set(values)), 3)       # yes, no and not-checked are three different answers
        self.assertEqual(values[1], values[3])      # and an unknown one reads the same wherever it occurs
        marker = re.compile(r'\battn\b')
        expect(self.page.get_by_role('button', name='Аккаунты', exact=True)).to_have_class(marker)
        self.page.evaluate('backend.accounts.splice(1, 3)')
        self.page.get_by_role('button', name='Сегодня').click()
        expect(self.page.get_by_role('button', name='Аккаунты', exact=True)).not_to_have_class(marker)

    def test_caldav_form_posts_its_fields_clears_the_password_and_keeps_the_failure_visible(self):
        self.mount_skill(accounts=[])
        self.page.evaluate("""secret => {
            backend.routes['accounts/connect'] = async () => {
                backend.accounts.push({id: 'caldav:failed', provider: 'caldav', alias: 'Work', login: 'person', status: 'auth_failed',
                    last_sync: null, last_error: 'server rejected the login'});
                return reply({status: 'error', saved: true, account_id: 'caldav:failed', message: 'server rejected ' + secret}, 400);
            };
        }""", SECRET)
        self.open_accounts()
        self.page.get_by_label('Название (необязательно)').fill('Work')
        self.page.get_by_label('Адрес сервера CalDAV').fill('https://dav.example.test/dav/')
        self.page.get_by_label('Логин').fill('person')
        password = self.page.locator('input[type=password]')
        password.fill(SECRET)
        password.press('Enter')   # the frame forbids native form submission: Enter is the widget's own
        expect(password).to_have_value('')
        failure = self.page.locator('.msg.err')
        expect(failure).to_have_count(1)
        self.assertEqual(self.sent('accounts/connect')[0]['body'], {
            'preset': 'caldav', 'alias': 'Work', 'login': 'person', 'password': SECRET, 'server_url': 'https://dav.example.test/dav/'})
        expect(failure).to_contain_text('•••')
        self.assertNotIn(SECRET, self.page.evaluate('document.documentElement.outerHTML + document.body.innerText'))
        self.assertFalse(self.page.evaluate("secret => [...document.querySelectorAll('input')].some(i => i.value.includes(secret))", SECRET))
        expect(self.row({'id': 'caldav:failed'})).to_be_visible()   # saved with an error: the list says so
        shown = failure.text_content()
        self.page.get_by_role('button', name='Обновить', exact=True).click()   # a successful list read...
        self.calls_reach('status', 3)
        self.page.get_by_role('button', name='Сегодня').click()               # ...and an agenda refresh
        self.calls_reach('agenda', 3)
        expect(failure).to_have_text(shown)
        failure.get_by_role('button', name='Скрыть сообщение').click()
        expect(failure).to_have_count(0)

    def test_connected_form_starts_over_and_a_failed_one_keeps_what_was_typed_except_the_password(self):
        self.mount_skill(accounts=[])
        self.route('accounts/connect', {'status': 'error', 'message': 'нет такого сервера'}, 400)
        self.open_accounts()
        login, password = self.page.get_by_label('Логин'), self.page.locator('input[type=password]')
        self.page.get_by_label('Адрес сервера CalDAV').fill('https://dav.example.test/')
        login.fill('person')
        password.fill('first')
        self.page.get_by_role('button', name='Подключить', exact=True).click()
        expect(self.page.locator('.msg.err')).to_contain_text('нет такого сервера')
        expect(login).to_have_value('person')
        expect(password).to_have_value('')
        self.route('accounts/connect', {'status': 'connected', 'account_id': 'caldav:ok', 'label': 'person', 'calendars': [{'id': 'c1', 'name': 'Main'}]})
        password.fill('second')
        self.page.get_by_role('button', name='Подключить', exact=True).click()
        expect(self.page.locator('.msg.ok')).to_be_visible()
        expect(login).to_have_value('')
        expect(password).to_have_value('')

    def test_yandex_preset_needs_no_server_address_and_an_incomplete_form_sends_nothing(self):
        self.mount_skill(accounts=[])
        self.open_accounts()
        connect = self.page.get_by_role('button', name='Подключить', exact=True)
        server = self.page.get_by_label('Адрес сервера CalDAV')
        login, password = self.page.get_by_label('Логин'), self.page.locator('input[type=password]')
        for url in ('', 'dav.example.test', 'ftp://dav.example.test'):   # the generic form needs an http(s) address
            login.fill('person'); password.fill('pw'); server.fill(url)
            connect.click()
            expect(self.page.locator('.msg.err')).to_be_visible()
        self.assertEqual(self.sent('accounts/connect'), [])
        expect(password).to_have_value('pw')   # a refused form keeps its password: nothing left the widget
        self.page.get_by_role('radio', name='Яндекс').check()
        expect(server).to_be_hidden()
        password.fill('')
        connect.click()
        self.assertEqual(self.sent('accounts/connect'), [])   # still no password
        self.route('accounts/connect', {'status': 'connected', 'label': 'person', 'calendars': []})
        password.fill('app-pass')
        connect.click()
        expect(self.page.locator('.msg.ok')).to_be_visible()
        self.assertEqual(self.sent('accounts/connect')[0]['body'], {'preset': 'yandex', 'alias': '', 'login': 'person', 'password': 'app-pass'})

    def begin_route(self):
        self.route('oauth/begin', {'status': 'ok', 'auth_url': AUTH, 'message': 'hint from the skill',
                                   'redirect_uri': 'http://127.0.0.1:8765/onboarding/static/provider-return.html'})

    def test_google_sign_in_is_confirmed_only_by_the_account_list(self):
        existing = account('google', 'First')
        self.mount_skill(accounts=[existing])
        self.begin_route()
        self.open_accounts()
        self.page.locator('summary').click()
        self.page.get_by_label('Адрес установки', exact=True).fill('https://host.example.org/prefix')
        self.page.get_by_role('button', name='Подключить Google').click()
        waiting = self.page.locator('.acc-wait .wait')
        expect(waiting).to_contain_text('hint from the skill')
        self.assertEqual(self.sent('oauth/begin')[0]['body'], {'public_base_url': 'https://host.example.org/prefix'})
        self.assertEqual(self.page.evaluate('backend.opened'), [AUTH])
        check = waiting.get_by_role('button', name='Проверить')
        known = len(self.sent('status'))
        for change in (None, {'status': 'error', 'last_error': 'sync failed', 'last_sync': minutes_ago(0)}):   # an unrelated sync is no arrival
            if change:
                self.page.evaluate('change => Object.assign(backend.accounts[0], change)', change)
            check.click()
            self.calls_reach('status', known + 1)
            known += 1
            expect(waiting).to_be_visible()
            expect(self.page.locator('.msg.ok')).to_have_count(0)
            expect(self.page.locator('section.acc .acc-row')).to_have_count(1)
        self.page.evaluate('a => backend.accounts.push(a)', account('google', 'Second', status='connected'))
        check.click()
        expect(self.page.locator('.msg.ok')).to_contain_text('Second', timeout=10000)
        expect(waiting).to_have_count(0)
        expect(self.page.locator('section.acc .acc-row')).to_have_count(2)

    def test_signing_in_again_counts_when_an_account_becomes_ready(self):
        broken = account('google', 'Broken', status='auth_failed', last_error='token revoked')
        self.mount_skill(accounts=[broken])
        self.begin_route()
        self.open_accounts()
        self.page.get_by_role('button', name='Подключить Google').click()
        waiting = self.page.locator('.acc-wait .wait')
        expect(waiting).to_be_visible()
        self.page.evaluate("Object.assign(backend.accounts[0], {status: 'connected', last_error: ''})")
        waiting.get_by_role('button', name='Проверить').click()
        expect(self.page.locator('.msg.ok')).to_contain_text('Broken')

    def test_a_link_that_could_not_be_opened_stays_available_by_hand_and_proves_nothing(self):
        self.mount_skill(accounts=[])
        self.begin_route()
        self.page.evaluate("backend.openResult = new Error('Opening a link requires a user action')")
        self.open_accounts()
        self.page.get_by_role('button', name='Подключить Google').click()
        waiting = self.page.locator('.acc-wait .wait')
        expect(waiting).to_contain_text('Opening a link requires a user action')   # the host's own reason, not a claim
        link = waiting.get_by_role('link', name='Открыть вход в Google')
        expect(link).to_have_attribute('href', AUTH)
        self.page.evaluate("backend.openResult = {ok: false, native: true, degraded: 'copy-link'}")
        link.click()
        self.page.wait_for_function('backend.opened.length === 2')
        expect(waiting).to_contain_text('скопирована')   # copied, which is not opened
        expect(self.page.locator('.msg.ok')).to_have_count(0)
        waiting.get_by_role('button', name='Отмена').click()
        expect(waiting).to_have_count(0)
        expect(self.page.locator('.msg.ok')).to_have_count(0)
        self.page.evaluate('() => { delete OuroborosWidget.openExternal; }')   # a host that cannot open links at all
        self.page.get_by_role('button', name='Подключить Google').click()
        expect(waiting.get_by_role('link', name='Открыть вход в Google')).to_have_attribute('href', AUTH)
        expect(waiting).to_contain_text('не открывает ссылки')
        expect(self.page.locator('.msg.ok')).to_have_count(0)

    def test_failures_of_google_start_and_disconnect_survive_list_and_agenda_refreshes(self):
        google = account('google', 'Ready')
        self.mount_skill(accounts=[google], options={'google_client_configured': False})
        expect(self.page.get_by_role('button', name='Аккаунты', exact=True)).to_be_visible()
        self.open_accounts()
        expect(self.page.locator('section.acc')).to_contain_text('GOOGLE_OAUTH_CLIENT_ID')   # said up front, without blocking the click
        self.route('oauth/begin', {'status': 'not_connected', 'message': 'no client configured by the skill'}, 400)
        self.page.get_by_role('button', name='Подключить Google').click()
        google_failure = self.page.locator('.msg.err', has_text='no client configured by the skill')
        expect(google_failure).to_be_visible()
        self.route('accounts/disconnect', {'status': 'error', 'message': 'disconnect refused by the skill'})   # HTTP 200, status in the body
        self.row(google).get_by_role('button', name='Отключить Ready').click()
        self.row(google).locator('.acc-confirm').get_by_role('button', name='Отключить', exact=True).click()
        disconnect_failure = self.page.locator('.msg.err', has_text='disconnect refused by the skill')
        expect(disconnect_failure).to_be_visible()
        expect(self.row(google)).to_be_visible()   # nothing was removed
        self.page.get_by_role('button', name='Обновить', exact=True).click()
        self.page.get_by_role('button', name='Сегодня').click()
        self.calls_reach('agenda', 3)
        expect(google_failure).to_be_visible()
        expect(disconnect_failure).to_be_visible()

    def test_disconnect_asks_first_and_changes_the_list_only_when_the_skill_says_so(self):
        google, caldav = account('google', 'Ready'), account('caldav', 'Other')
        self.mount_skill(accounts=[google, caldav])
        self.open_accounts()
        confirm = self.row(google).locator('.acc-confirm')
        self.row(google).get_by_role('button', name='Отключить Ready').click()
        expect(confirm).to_contain_text('Ready')
        self.assertEqual(self.sent('accounts/disconnect'), [])
        expect(self.row(caldav).get_by_role('button', name='Отключить Other')).to_be_disabled()   # one question at a time
        confirm.get_by_role('button', name='Отмена').click()
        expect(confirm).to_have_count(0)
        self.assertEqual(self.sent('accounts/disconnect'), [])
        self.page.evaluate("""() => { backend.routes['accounts/disconnect'] = async (query, body) => {
            backend.accounts = backend.accounts.filter(a => a.id !== body.account_id);
            return reply({status: 'updated', message: 'аккаунт отключён по ответу навыка'}); }; }""")
        self.row(google).get_by_role('button', name='Отключить Ready').click()
        confirm.get_by_role('button', name='Отключить', exact=True).click()
        expect(self.page.locator('.msg.ok')).to_contain_text('по ответу навыка')
        expect(self.page.locator('section.acc .acc-row')).to_have_count(1)
        self.assertEqual([c['body'] for c in self.sent('accounts/disconnect')], [{'account_id': google['id']}])

    def test_typed_form_survives_every_render_and_the_poll_waits_for_focus(self):
        self.mount_skill(clock=True, accounts=[account('google', 'Ready')])
        self.open_accounts()
        login, password = self.page.get_by_label('Логин'), self.page.locator('input[type=password]')
        login.fill('person')
        password.fill('S3cret')
        agenda = len(self.sent('agenda'))
        self.page.clock.run_for(31000)   # the 30 s poll fires while the form has focus
        self.assertEqual(len(self.sent('agenda')), agenda)
        expect(password).to_be_focused()
        # an agenda answer that lands while the owner is typing re-renders the calendar around the form
        self.page.evaluate('() => { backend.hold = new Promise(resolve => { window.release = resolve; }); }')   # a returned promise would be awaited
        self.page.get_by_role('button', name='Сегодня').click()
        login.focus()
        login.evaluate('el => el.setSelectionRange(2, 4)')
        self.page.evaluate("document.querySelector('.grid-scroll').dataset.stale = '1'")   # the old calendar, until it is rebuilt
        self.page.evaluate('release()')
        self.page.wait_for_function("document.querySelector('.grid-scroll') && !document.querySelector('.grid-scroll').dataset.stale")
        expect(login).to_be_focused()
        self.assertEqual(login.evaluate('el => [el.selectionStart, el.selectionEnd]'), [2, 4])
        expect(login).to_have_value('person')
        expect(password).to_have_value('S3cret')
        self.page.locator('.bar .title').click()   # focus leaves the form: the poll resumes, the text stays
        self.page.clock.run_for(31000)
        self.calls_reach('agenda', agenda + 2)
        expect(login).to_have_value('person')
        expect(password).to_have_value('S3cret')

    def test_dispose_releases_the_theme_and_stops_every_timer(self):
        self.mount_skill(clock=True, accounts=[])
        self.begin_route()
        self.open_accounts()
        self.page.get_by_role('button', name='Подключить Google').click()
        expect(self.page.locator('.acc-wait .wait')).to_be_visible()
        self.page.evaluate('disposers.forEach(fn => fn())')
        self.assertTrue(self.page.evaluate('window.themeOff === true'))
        before = len(self.page.evaluate('calls'))
        self.page.clock.run_for(120000)
        self.assertEqual(len(self.page.evaluate('calls')), before)   # no poll, no sign-in check

    def test_layout_holds_in_light_dark_and_narrow_windows_with_long_labels(self):
        long_login = 'very.long.login.name.for.testing' * 2 + '@example.test'
        accounts = [account('google', 'Ready'), account('yandex', LONG, status='auth_failed', login=long_login, last_error='401: ' + LONG),
                    account('caldav', 'Pending', status='connecting', last_sync=None, server_url='https://caldav.example.org/' + 'long-path/' * 8)]
        cals = [*CALENDARS, calendar('g1', LONG, accounts[0]['id'], 'google')]
        issue = {'id': 'cp', 'calendar_id': 'personal', 'title': LONG, 'link_group_id': 'g', 'link_state': 'diverged', 'deleted_at': None}
        rows, details = linked('diverged')
        for row in details.values():
            row['title'] = LONG
        self.mount_skill(accounts=accounts, calendars=cals, linkIssues=[issue], events=rows, eventGet=details)
        self.page.set_viewport_size({'width': 390, 'height': 900})
        self.open_accounts()
        self.page.locator('summary').click()
        self.page.get_by_label('Адрес установки', exact=True).fill('https://host.example.org/' + 'prefix/' * 10)
        self.page.get_by_label('Логин').fill(long_login)
        self.page.get_by_role('button', name='Подключить', exact=True).click()   # an error is on screen for the contrast check
        expect(self.page.locator('.msg.err')).to_be_visible()
        self.page.get_by_role('button', name=re.compile('Копии связанных событий')).click()
        for theme in ('light', 'dark'):
            self.page.evaluate('theme => setTheme(theme)', theme)
            expect(self.page.locator('html')).to_have_attribute('data-theme', theme)
            self.shot(f'accounts-390-{theme}')
            self.assertEqual(self.page.evaluate("""() => [...document.querySelectorAll('section.acc *, .linkissues *')]
                .filter(e => e.getBoundingClientRect().right > innerWidth + 1).length"""), 0)
            for selector in ('button.primary', '.msg.err'):
                style = self.page.locator(selector).first.evaluate('e => [getComputedStyle(e).color, getComputedStyle(e).backgroundColor]')
                self.assertGreaterEqual(self.page.evaluate(CONTRAST, style), 4.5, (theme, selector, style))
        self.page.get_by_role('button', name='Закрыть', exact=True).click()
        self.page.locator('.linkissues').get_by_role('button', name='Открыть').click()
        expect(self.page.locator('.linkbox')).to_be_visible()
        self.page.get_by_role('button', name='Следовать источнику').click()
        for theme in ('light', 'dark'):
            self.page.evaluate('theme => setTheme(theme)', theme)
            self.shot(f'link-card-390-{theme}')
            self.assertTrue(self.page.evaluate("document.querySelector('.card form').scrollWidth <= document.querySelector('.card form').clientWidth"))


class LinksBrowserTests(SkillCase):
    def open_event(self, calendar_name):
        self.page.locator('.ev', has_text=calendar_name).click()
        expect(self.page.locator('.card .status')).not_to_have_text('Загружаю событие…')

    def test_actions_name_what_they_change_before_anything_is_sent(self):
        rows, details = linked('diverged')
        self.mount_skill(events=rows, eventGet=details, linkIssues=[{'id': 'cp', 'calendar_id': 'personal', 'title': 'Planning', 'link_group_id': 'g', 'link_state': 'diverged', 'deleted_at': None}])
        self.open_event('Personal')
        box = self.page.locator('.linkbox')
        title = self.page.get_by_placeholder('Название')
        title.fill('Unsaved edit')
        previews = {}
        for action in ('Следовать источнику', 'Сделать источником', 'Отвязать'):
            box.get_by_role('button', name=action, exact=True).click()
            expect(box.locator('.preview')).to_be_visible()
            previews[action] = box.locator('.preview').inner_text()
            self.assertEqual(self.sent('event/link'), [])   # arming sends nothing
            expect(title).to_have_value('Unsaved edit')     # and does not rebuild the form
            box.get_by_role('button', name='Отмена').click()
        # copy: names this copy and the source it will be rewritten from; group: names the source that stops being one;
        # link only: names the event that leaves, not the source
        self.assertTrue(all(label in previews['Следовать источнику'] for label in ('Personal', 'Work')))
        self.assertTrue(all(label in previews['Сделать источником'] for label in ('Personal', 'Work')))
        self.assertIn('Personal', previews['Отвязать'])
        self.assertNotIn('Work', previews['Отвязать'])
        # the owner applies one of them
        self.page.evaluate("""() => { backend.routes['event/link'] = async (query, body) => {
            backend.linkIssues = []; backend.events.forEach(e => { e.link_state = 'following'; });
            return reply({status: 'ok', assignments: [{calendar_id: 'personal', calendar_name: 'Personal', status: 'pending', message: 'queued'}], link: {}}); }; }""")
        box.get_by_role('button', name='Следовать источнику', exact=True).click()
        box.get_by_role('button', name='Применить').click()
        expect(self.page.locator('.card')).to_have_count(0)
        self.assertEqual([c['body'] for c in self.sent('event/link')], [{'id': 'cp', 'action': 'follow', 'scope': 'all'}])
        expect(self.page.locator('.linkissues')).to_have_count(0)   # the list was read again
        expect(self.page.locator('.status', has_text='Personal: queued')).to_be_visible()   # delivery is not claimed as done

    def test_a_refused_action_stays_in_its_box_and_keeps_the_card_open(self):
        rows, details = linked('diverged')
        self.mount_skill(events=rows, eventGet=details)
        self.route('event/link', {'status': 'error', 'message': 'Choose one existing source before following it.'}, 400)
        self.open_event('Personal')
        self.page.get_by_placeholder('Название').fill('Unsaved edit')
        box = self.page.locator('.linkbox')
        box.get_by_role('button', name='Следовать источнику', exact=True).click()
        box.get_by_role('button', name='Применить').click()
        expect(box.locator('.status.err')).to_contain_text('Choose one existing source')
        expect(self.page.get_by_placeholder('Название')).to_have_value('Unsaved edit')
        box.get_by_role('button', name='Отмена').click()
        expect(box.locator('.status.err')).to_have_count(0)

    def test_only_the_actions_that_can_work_are_offered(self):
        cases = {
            'a following copy': (linked('following'), 'Personal', {'Сделать источником', 'Отвязать'}),
            'a diverged copy': (linked('diverged'), 'Personal', {'Следовать источнику', 'Сделать источником', 'Отвязать'}),
            'the source itself': (linked('diverged'), 'Work', {'Отвязать'}),
            'a group without a source': (linked('diverged', ambiguous=True), 'Personal', {'Сделать источником', 'Отвязать'}),
            'a group whose source was deleted': (linked('diverged', source_missing=True), 'Personal', {'Сделать источником', 'Отвязать'}),
        }
        for name, ((rows, details), calendar_name, expected) in cases.items():
            with self.subTest(name):
                self.page.goto('about:blank')
                self.mount_skill(events=rows, eventGet=details)
                self.open_event(calendar_name)
                offered = {b.inner_text() for b in self.page.locator('.linkbox .acts button').all()}
                self.assertEqual(offered, expected)

    def test_a_series_follows_by_occurrence_and_changes_roles_on_its_master(self):
        rows, details = linked('diverged', series=True)
        self.mount_skill(events=rows, eventGet=details)
        self.route('event/link', {'status': 'ok', 'assignments': []})
        occurrence = 'cp@2026-09-27T14:00:00+00:00'
        for action, scope_label, expected in (
                ('Следовать источнику', 'только это вхождение', {'id': occurrence, 'action': 'follow', 'scope': 'this'}),
                ('Следовать источнику', 'всё расписание', {'id': occurrence, 'action': 'follow', 'scope': 'all'}),
                ('Сделать источником', None, {'id': 'cp', 'action': 'source', 'scope': 'all'}),
                ('Отвязать', None, {'id': 'cp', 'action': 'detach', 'scope': 'all'})):
            with self.subTest(action=action, scope=scope_label):
                self.open_event('Personal')
                box = self.page.locator('.linkbox')
                box.get_by_role('button', name=action, exact=True).click()
                if scope_label:
                    box.locator('select').select_option(label=scope_label)
                box.get_by_role('button', name='Применить').click()
                expect(self.page.locator('.card')).to_have_count(0)
                self.assertEqual(self.sent('event/link')[-1]['body'], expected)

    def test_copies_needing_attention_stay_listed_and_a_removed_copy_is_not_editable(self):
        rows, details = linked('missing')
        details['cp']['title'] = 'Removed copy'
        issues = [{'id': 'cp', 'calendar_id': 'personal', 'title': 'Removed copy', 'link_group_id': 'g', 'link_state': 'missing', 'deleted_at': minutes_ago(3)},
                  {'id': 'ghost', 'calendar_id': 'work', 'title': 'Purged copy', 'link_group_id': 'g2', 'link_state': 'diverged', 'deleted_at': None}]
        self.mount_skill(events=rows[:1], eventGet=details, linkIssues=issues)
        strip = self.page.locator('.linkissues')
        expect(strip).to_contain_text('2')   # counted, though one of them is not on the grid at all
        strip.get_by_role('button', name=re.compile('Копии связанных событий')).click()
        expect(strip.locator('.issue')).to_have_count(2)
        strip.get_by_role('button', name='Открыть: Purged copy').click()   # the event is gone: the failure stays in the list
        expect(strip.locator('.status.err')).to_contain_text('Purged copy')
        self.calls_reach('agenda', 1)
        self.page.get_by_role('button', name='Сегодня').click()
        self.calls_reach('agenda', 2)
        expect(strip.locator('.status.err')).to_be_visible()
        strip.get_by_role('button', name='Открыть: Removed copy').click()
        card = self.page.locator('.card')
        expect(card.get_by_role('button', name='Отвязать')).to_be_visible()
        expect(card.get_by_role('button', name='Следовать источнику')).to_have_count(0)
        expect(card.get_by_role('button', name='Сделать источником')).to_have_count(0)
        expect(card.get_by_role('button', name='Сохранить')).to_be_hidden()
        expect(card.get_by_role('button', name='Удалить')).to_be_hidden()
        self.assertTrue(self.page.get_by_placeholder('Название').is_disabled())
        self.route('event/link', {'status': 'ok', 'assignments': []})
        card.get_by_role('button', name='Отвязать').click()
        card.get_by_role('button', name='Применить').click()
        expect(card).to_have_count(0)
        self.assertEqual(self.sent('event/link')[-1]['body'], {'id': 'cp', 'action': 'detach', 'scope': 'all'})


if __name__ == '__main__':
    unittest.main()
