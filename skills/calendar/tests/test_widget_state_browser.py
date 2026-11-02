"""Calendar editing survives refresh, failures and the disposable widget frame."""
from __future__ import annotations

import re

from test_widget_browser import SkillCase, WIDGET, expect, event_row


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
        self.page.get_by_role('button', name='›', exact=True).click()
        self.page.get_by_role('button', name='›', exact=True).click()
        self.calls_reach('agenda', 3)
        self.page.evaluate("completeAgenda('2026-09-29')")
        expect(self.page.locator('.bar .title')).to_contain_text('2026-09-29')
        self.page.evaluate("completeAgenda('2026-09-28')")
        expect(self.page.locator('.bar .title')).to_contain_text('2026-09-29')

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
        self.page.get_by_role('button', name='Restore draft').wait_for()
        self.page.get_by_role('button', name='+ Событие').click()
        self.assertEqual(self.page.locator('.card').count(), 0)
        self.page.get_by_role('button', name='Restore draft').click()
        expect(self.page.get_by_placeholder('Название', exact=True)).to_have_value('Unfinished meeting')
        expect(self.page.get_by_placeholder('Описание', exact=True)).to_have_value('Keep the final sentence')
        self.shot('calendar-state-restored-draft')
        self.page.get_by_role('button', name='Отмена', exact=True).click()
        self.page.wait_for_function('backend.draft === null')
        self.assertEqual(self.page.get_by_role('button', name='Restore draft').count(), 0)
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
