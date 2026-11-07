"""Calendar editing survives refresh, failures and the disposable widget frame."""
from __future__ import annotations

import re

from test_widget_browser import SkillCase, WIDGET, expect, event_row

TITLE = '.bar .title'


class CalendarWidgetStateTests(SkillCase):
    def mount_state(self, **kwargs):
        self.mount_skill(**kwargs)
        self.page.evaluate("""() => {
            backend.draft = null;
            backend.routes.draft = async (query, body) => {
                if (body) backend.draft = body.draft;
                return reply({status:'ok', draft:backend.draft});
            };
        }""")

    def open_plain(self, **kwargs):
        row = event_row('event-1', 'work', 'Work', **kwargs)
        self.mount_state(events=[row], eventGet={row['id']: {**row, 'description':'', 'location':'', 'reminders':[], 'attendees':[]}})
        self.page.locator('.ev').click()
        self.page.locator('.card').wait_for()
        self.calls_reach('event/get', 1)
        expect(self.page.locator('.card .status')).not_to_have_text('Загружаю событие…')

    def test_unsent_drafts_are_visible_in_grid_all_day_and_list(self):
        self.mount_state(events=[
            event_row('draft', 'work', 'Work', title='Unsent timed', sync_state='failed'),
            event_row('day', 'work', 'Work', title='Unsent date', sync_state='failed', all_day=True,
                      start='2026-09-27T00:00:00+04:00', end='2026-09-28T00:00:00+04:00'),
            event_row('confirmed', 'work', 'Work', title='Confirmed', sync_state='synced')])
        expect(self.page.locator('.ev').filter(has_text='Unsent timed')).to_contain_text('не отправлено')
        expect(self.page.locator('.allday .chip')).to_contain_text('не отправлено')
        expect(self.page.locator('.list .row').filter(has_text='Unsent timed')).to_contain_text('не отправлено')
        expect(self.page.locator('.ev').filter(has_text='Confirmed')).not_to_contain_text('не отправлено')
        self.shot('calendar-unsent-drafts')

    def test_end_only_edit_does_not_resubmit_the_start(self):
        self.open_plain()
        self.route('event/update', {'status':'ok', 'assignments':[{'status':'done'}]})
        self.page.locator('.card input[type=datetime-local]').nth(1).fill('2026-09-27T19:15')
        self.page.get_by_role('button', name='Сохранить', exact=True).click()
        self.calls_reach('event/update', 1)
        sent = self.sent('event/update')[-1]['body']
        self.assertEqual(sent['end'], '2026-09-27T19:15')
        self.assertNotIn('start', sent)

    def test_failed_delete_keeps_card_and_outcome_survives_later_refresh(self):
        self.open_plain()
        self.route('event/delete', {'status':'failed', 'assignments':[{'status':'failed', 'calendar_id':'work', 'message':'Provider refused deletion'}]})
        self.page.get_by_role('button', name='Удалить', exact=True).click()
        self.page.get_by_role('button', name=re.compile('Точно удалить')).click()
        expect(self.page.locator('.card')).to_be_visible()
        expect(self.page.locator('.card .status')).to_contain_text('Provider refused deletion')
        self.shot('calendar-state-failed-delete')
        self.page.get_by_role('button', name='Отмена', exact=True).click()
        self.page.get_by_role('button', name='Сегодня', exact=True).click()
        expect(self.page.get_by_role('status')).to_contain_text('Provider refused deletion')

    def test_drag_failure_survives_successful_agenda_refresh(self):
        self.open_plain()
        self.page.get_by_role('button', name='Отмена', exact=True).click()
        self.route('event/update', {'status':'failed', 'assignments':[{'status':'failed', 'calendar_id':'work', 'message':'Move refused'}]})
        node = self.page.locator('.ev'); node.scroll_into_view_if_needed()
        box = node.bounding_box()
        self.page.mouse.move(box['x'] + 12, box['y'] + 8)
        self.page.mouse.down(); self.page.mouse.move(box['x'] + 12, box['y'] + 39, steps=4); self.page.mouse.up()
        self.calls_reach('event/update', 1)
        expect(self.page.get_by_role('status')).to_contain_text('Move refused')
        self.assertGreaterEqual(len(self.sent('agenda')), 2)

    def test_reminder_panel_preserves_typing_and_saved_rule(self):
        self.mount_state(clock=True)
        self.route('reminders', {'status':'ok', 'rules':{'default':[15]}, 'channel':{'state':'ready'}})
        self.route('reminders/save', {'status':'updated', 'rules':{'default':[45]}})
        self.page.get_by_title('Напоминания и синхронизация').click()
        field = self.page.get_by_placeholder(re.compile('напоминать за N'))
        field.fill('45')
        self.page.clock.fast_forward(31_000)
        expect(field).to_have_value('45')
        expect(field).to_be_focused()
        self.page.get_by_role('button', name='Сохранить правило').click()
        self.calls_reach('reminders/save', 1)
        self.page.clock.fast_forward(31_000)
        expect(field).to_have_value('45')
        self.page.get_by_role('button', name='Сохранить правило').click()
        self.assertEqual(self.sent('reminders/save')[-1]['body']['offsets'], [45])

    def test_old_agenda_reply_cannot_overwrite_new_navigation(self):
        self.mount_state()
        self.page.evaluate("""() => {
            window.pendingAgenda = {};
            backend.routes.agenda = query => new Promise(resolve => { pendingAgenda[query.date] = resolve; });
            window.completeAgenda = day => pendingAgenda[day](reply({status:'ok', view:'day', anchor:day,
                timezone:'Asia/Dubai', now:backend.now, days:[{date:day,weekday:'Day'}], calendars:backend.calendars,
                events:[],hidden_busy:[],usual:[],accounts:[],sync:{companion:'alive'}}));
        }""")
        self.page.get_by_role('button', name='Следующий день', exact=True).click()
        self.page.get_by_role('button', name='Следующий день', exact=True).click()
        self.calls_reach('agenda', 3)
        self.page.evaluate("completeAgenda('2026-09-29')")
        expect(self.page.locator('.bar .title')).to_contain_text('29 сентября')
        self.page.evaluate("completeAgenda('2026-09-28')")
        expect(self.page.locator('.bar .title')).to_contain_text('29 сентября')

    def test_http_warning_is_visible_before_password_submission(self):
        self.mount_state(); self.open_accounts()
        self.page.locator('#acc-url').fill('http://dav.example.test/cal/')
        expect(self.page.get_by_text('HTTP sends your login and password without encryption.', exact=True)).to_be_visible()
        self.assertEqual(self.sent('accounts/connect'), [])
        self.page.locator('#acc-url').fill('https://dav.example.test/cal/')
        expect(self.page.get_by_text('HTTP sends your login and password without encryption.', exact=True)).to_be_hidden()

    def test_draft_survives_dispose_and_requires_restore_before_new_card(self):
        self.mount_state()
        self.page.get_by_role('button', name='+ Событие').click()
        self.page.get_by_placeholder('Название', exact=True).fill('Unfinished meeting')
        self.page.get_by_placeholder('Описание', exact=True).fill('Keep the final sentence')
        self.page.evaluate('async () => { for (const dispose of disposers) await dispose(); }')
        saved = self.page.evaluate('backend.draft')
        self.assertEqual(saved['inputs']['title'], 'Unfinished meeting')
        self.assertEqual(saved['inputs']['description'], 'Keep the final sentence')
        self.assertNotIn('password', str(saved))
        self.page.evaluate("document.querySelector('#root').innerHTML = ''; window.disposers.length = 0")
        self.page.add_script_tag(content=WIDGET.read_text(encoding='utf-8'))
        self.page.get_by_role('button', name='Восстановить', exact=True).wait_for()
        self.page.get_by_role('button', name='+ Событие').click()
        self.assertEqual(self.page.locator('.card').count(), 0)
        self.page.get_by_role('button', name='Восстановить', exact=True).click()
        expect(self.page.get_by_placeholder('Название', exact=True)).to_have_value('Unfinished meeting')
        expect(self.page.get_by_placeholder('Описание', exact=True)).to_have_value('Keep the final sentence')
        self.shot('calendar-state-restored-draft')
        self.page.get_by_role('button', name='Отмена', exact=True).click()
        self.page.wait_for_function('backend.draft === null')
        self.assertEqual(self.page.get_by_role('button', name='Восстановить', exact=True).count(), 0)
        self.shot('calendar-state-draft-cleared')


    def test_draft_clear_error_never_replaces_uncertain_operation_outcome(self):
        self.open_plain()
        self.page.get_by_placeholder('Название', exact=True).fill('Edited title')
        self.page.wait_for_function('backend.draft && backend.draft.inputs.title === "Edited title"')
        self.route('event/update', {'status':'partial','assignments':[
            {'calendar_id':'work','status':'done'},
            {'calendar_id':'copy','status':'uncertain','message':'Remote copy outcome UNKNOWN'}]})
        self.page.evaluate("""() => { backend.routes.draft = async (q, body) => {
            if (body && body.draft === null) return reply({status:'error',message:'Draft storage unavailable'},500);
            if (body) backend.draft=body.draft;
            return reply({status:'ok',draft:backend.draft});
        }; }""")
        self.page.get_by_role('button', name='Сохранить', exact=True).click()
        expect(self.page.locator('.card')).to_have_count(0)
        expect(self.page.get_by_role('status')).to_contain_text('Remote copy outcome UNKNOWN')
        expect(self.page.locator('.draft-error')).to_contain_text('Draft storage unavailable')
        self.page.get_by_role('button',name='Сегодня',exact=True).click()
        expect(self.page.get_by_role('status')).to_contain_text('Remote copy outcome UNKNOWN')
        expect(self.page.locator('.draft-error')).to_be_visible()
        self.shot('calendar-independent-operation-and-draft-errors')


    def test_persistent_sync_warning_and_unavailable_account_reach_footer(self):
        self.mount_state(clock=True, accounts=[{'id':'yandex:fixture','provider':'yandex','alias':'Test account','status':'unavailable'}],
            calendars=[{'id':'work','name':'Work','provider':'yandex','writable':True,'visible':True,'default':True,
                        'sync_warning':'Linked copy copy-1 remains diverged'}])
        expect(self.page.get_by_text(re.compile('Test account: unavailable'))).to_be_visible()
        expect(self.page.get_by_text(re.compile('Linked copy copy-1 remains diverged'))).to_be_visible()
        expect(self.page.get_by_role('button',name='Аккаунты',exact=True)).to_have_class(re.compile('attn'))
        self.page.evaluate("""async () => {
            const data = await (await OuroborosWidget.fetch('/api/extensions/calendar/agenda')).json();
            data.sync.uncertain = 1; backend.routes.agenda = async () => reply(data);
        }""")
        self.page.clock.fast_forward(31_000)
        expect(self.page.get_by_text(re.compile('Linked copy copy-1 remains diverged'))).to_be_visible()
        expect(self.page.get_by_text(re.compile('outcomes unconfirmed: 1'))).to_be_visible()
        self.shot('calendar-persistent-sync-warning')

    def test_background_worker_is_quiet_while_alive_and_named_when_stale_or_unknown(self):
        self.mount_state(clock=True)
        expect(self.page.locator('.calendar-body')).not_to_contain_text('фон:')
        for companion, shown in (('stale', 'фон: давно не отвечал'), ('unknown', 'фон: неизвестно'), (None, 'фон: неизвестно'), ('alive', None)):
            with self.subTest(companion=companion):
                self.page.evaluate("""companion => {
                    backend.routes.agenda = async () => reply({status:'ok', view:'day', anchor:'2026-09-27', timezone:'Asia/Dubai', now:backend.now, days:[{date:'2026-09-27', weekday:'Вс'}],
                            calendars:backend.calendars, events:[], hidden_busy:[], usual:[], link_issues:[], accounts:[],
                            sync: companion === null ? {pending:0, conflict:0, failed:0} : {pending:0, conflict:0, failed:0, companion}}); }""", companion)
                self.page.clock.fast_forward(31_000)   # the ordinary poll, no extra read
                if shown:
                    expect(self.page.get_by_role('status').filter(has_text=shown)).to_be_visible()
                else:
                    expect(self.page.locator('.calendar-body')).not_to_contain_text('фон:')
        self.assertEqual({c['route'] for c in self.page.evaluate('calls')}, {'agenda', 'draft'})


class CalendarNavigationStateTests(SkillCase):
    """The request (view, date or «follow today»), the body actually shown, and the failed request kept for Retry."""

    def test_failed_day_keeps_following_today_and_retry_loads_the_failed_day(self):
        self.mount_week_agenda(clock=True)
        self.page.evaluate("() => { window.agendaFail = q => q.date === '2026-09-28'; }")
        self.page.get_by_role('button', name='Следующий день', exact=True).click()
        failure = self.page.locator('.msg.err')
        expect(failure).to_contain_text('Не загружено: понедельник, 28 сентября; на экране воскресенье, 27 сентября. Ошибка: calendar unavailable')
        expect(self.page.locator(TITLE)).to_have_text('воскресенье, 27 сентября')
        self.page.clock.fast_forward(31_000)   # the poll refreshes what is shown, still as «today», and keeps the failure
        self.page.wait_for_function("calls.filter(c => c.route === 'agenda').length >= 3")
        self.assertEqual(self.agenda_queries()[-1], {'view': 'day', 'date': ''})
        expect(failure).to_contain_text('понедельник, 28 сентября')
        self.page.evaluate('agendaFail = null')
        self.page.get_by_role('button', name='Повторить', exact=True).click()
        expect(self.page.locator(TITLE)).to_have_text('понедельник, 28 сентября')
        expect(failure).to_have_count(0)
        self.assertEqual(self.agenda_queries()[-1], {'view': 'day', 'date': '2026-09-28'})
        self.assertEqual(self.page.locator('.col').get_attribute('data-date'), '2026-09-28')

    def test_failed_switch_to_week_keeps_the_day_truthful_and_retry_loads_the_week(self):
        for width in (950, 390):
            with self.subTest(width=width):
                self.page.goto('about:blank')
                self.mount_week_agenda(events=[event_row('plan', 'work', 'Work')], width=width)
                self.page.evaluate("() => { window.agendaFail = q => q.view === 'week'; }")
                self.page.get_by_role('button', name='Неделя', exact=True).click()
                failure = self.page.locator('.msg.err')
                expect(failure).to_contain_text('Не загружено: неделя 21–27 сентября; на экране воскресенье, 27 сентября')
                expect(self.page.locator(TITLE)).to_have_text('воскресенье, 27 сентября')
                expect(self.page.get_by_role('button', name='День', exact=True)).to_have_class('on')
                expect(self.page.get_by_role('button', name='Предыдущий день', exact=True)).to_be_visible()
                if width > 560:
                    self.assertEqual(self.page.locator('.col').count(), 1)
                    expect(self.page.locator('.list')).to_be_visible()
                else:
                    expect(self.page.locator('.mobile-agenda h3')).to_have_text('Повестка дня')
                self.page.evaluate('agendaFail = null')
                self.page.get_by_role('button', name='Повторить', exact=True).click()
                expect(self.page.locator(TITLE)).to_have_text('Неделя · 21–27 сентября')
                self.assertEqual(self.agenda_queries()[-1], {'view': 'week', 'date': ''})   # still «this week», not pinned
                if width > 560:
                    expect(self.page.locator('.col')).to_have_count(7)
                else:
                    expect(self.page.locator('.mobile-day')).to_have_count(7)
                self.shot(f'calendar-week-retry-{width}')

    def test_pending_week_names_its_target_and_a_failure_restores_the_shown_week(self):
        self.mount_week_agenda()
        self.page.get_by_role('button', name='Неделя', exact=True).click()
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 21–27 сентября')
        self.page.evaluate("""() => { window.agendaHold = new Promise(resolve => { window.releaseAgenda = resolve; }); }""")
        self.page.get_by_role('button', name='Следующая неделя', exact=True).click()
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 28 сентября – 4 октября')   # before any answer
        expect(self.page.locator('.status.pending')).to_have_text('На экране неделя 21–27 сентября · загружается неделя 28 сентября – 4 октября…')
        self.assertEqual(self.page.locator('.col').first.get_attribute('data-date'), '2026-09-21')   # the old body is still there, labelled
        self.shot('calendar-week-pending')
        self.page.evaluate("() => { window.agendaFail = q => q.date === '2026-10-04'; window.agendaHold = null; releaseAgenda(); }")
        expect(self.page.locator('.msg.err')).to_contain_text('Не загружено: неделя 28 сентября – 4 октября; на экране неделя 21–27 сентября')
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 21–27 сентября')
        expect(self.page.locator('.status.pending')).to_have_count(0)

    def test_week_titles_name_months_and_years_where_the_ends_differ(self):
        self.mount_week_agenda(now='2026-12-31T12:00:00+04:00')
        self.page.evaluate("backend.today = '2026-12-31'")
        self.page.get_by_role('button', name='Неделя', exact=True).click()
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 28 декабря 2026 – 3 января 2027')
        self.page.get_by_role('button', name='Следующая неделя', exact=True).click()
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 4–10 января')

    def test_switching_views_before_midnight_keeps_following_today_and_arrows_still_pin(self):
        self.mount_week_agenda(clock=True, now='2026-09-27T23:55:00+04:00')   # Sunday, the last day of its week
        for view, title in (('Неделя', 'Неделя · 21–27 сентября'), ('День', 'воскресенье, 27 сентября'), ('Неделя', 'Неделя · 21–27 сентября')):
            self.page.get_by_role('button', name=view, exact=True).click()
            expect(self.page.locator(TITLE)).to_have_text(title)
        self.assertEqual({q['date'] for q in self.agenda_queries()}, {''})
        self.page.evaluate("backend.today = '2026-09-28'; backend.now = '2026-09-28T00:10:00+04:00'")
        self.page.clock.fast_forward(31_000)   # the ordinary poll after midnight
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 28 сентября – 4 октября')
        self.page.get_by_role('button', name='День', exact=True).click()
        expect(self.page.locator(TITLE)).to_have_text('понедельник, 28 сентября')
        self.assertEqual({q['date'] for q in self.agenda_queries()}, {''})
        self.page.get_by_role('button', name='Предыдущий день', exact=True).click()   # an arrow pins its date across view changes and polls
        self.page.get_by_role('button', name='Неделя', exact=True).click()
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 21–27 сентября')
        self.page.clock.fast_forward(31_000)
        self.page.wait_for_function("calls.filter(c => c.route === 'agenda').length >= 9")
        self.assertEqual(self.agenda_queries()[-3:], [{'view': 'day', 'date': '2026-09-27'}, {'view': 'week', 'date': '2026-09-27'},
                                                       {'view': 'week', 'date': '2026-09-27'}])
        expect(self.page.locator(TITLE)).to_have_text('Неделя · 21–27 сентября')
