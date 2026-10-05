"""Rendered calendar grid regression checks (optional local Playwright Chromium)."""

import pathlib
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

WIDGET = pathlib.Path(__file__).resolve().parents[1] / "widget.js"


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class CalendarWidgetBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(headless=True)
        except Exception as error:
            self.playwright.stop()
            self.skipTest(f"Chromium is not installed: {error}")
        self.page = self.browser.new_page(viewport={"width": 950, "height": 780})

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()

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
