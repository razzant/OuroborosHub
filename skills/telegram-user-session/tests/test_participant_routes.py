"""Widget routes on isolated synthetic state: scope, chat list, held work, import plans.

No companion runs here, so nothing reaches Telegram or a Host.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace as NS
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from userbot.config import Config, save_config, _write_json
from userbot.participant_state import Journal, save_policy, policy

ACCOUNT = 8667525238


def load_plugin(name):
    package = ModuleType(name); package.__path__ = [str(ROOT)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + '.plugin', ROOT / 'plugin.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Request:
    def __init__(self, method='GET', body=None):
        self.method, self._body, self.query_params = method, body, {}
    async def json(self):
        return self._body


class API:
    def __init__(self, root):
        self.root, self.routes, self.tools, self.tabs = root, {}, {}, []
    def get_state_dir(self): return str(self.root)
    def register_tool(self, name, handler, **kw): self.tools[name] = kw.get('description', '')
    def register_route(self, name, handler, methods): self.routes[name] = handler
    def register_companion_process(self, name): pass
    def register_settings_section(self, *a, **kw): pass
    def register_ui_tab(self, tab_id, title, render): self.tabs.append(render)


class Routes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
        save_policy(self.root, {'mode': 'participant', 'outgoing': True})
        self.api = API(self.root)
        load_plugin('participant_routes_test').register(self.api)

    def tearDown(self):
        self.tmp.cleanup()

    async def call(self, name, method='GET', body=None):
        result = await self.api.routes[name](Request(method, body))
        return result.status_code, json.loads(result.body)

    async def test_registered_tab_is_wide_module_with_host_appearance(self):
        self.assertEqual(self.api.tabs, [{'kind': 'module', 'entry': 'widget.js', 'start': 'auto', 'appearance': 'host', 'span': 2}])
        for name in ('participant/chats', 'participant/held', 'participant/import', 'participant/policy', 'participant/status'):
            self.assertIn(name, self.api.routes)

    async def test_tool_descriptions_point_to_place_notes_without_rules(self):
        self.assertIn('ask in Main and wait', self.api.tools['tg_join'])
        self.assertIn('one short note to Main', self.api.tools['tg_join'])
        for name in ('tg_ban', 'tg_mute', 'tg_admin'):
            self.assertIn('do not make you a moderator', self.api.tools[name])
        self.assertIn('Your own messages are yours to delete', self.api.tools['tg_delete'])
        self.assertIn('held was never attempted', self.api.tools['tg_status'])
        self.assertNotIn('place note', self.api.tools['tg_history'])

    async def test_descriptions_name_every_receipt_state_and_lasting_effect(self):
        from userbot.participant_ops import SPECS
        for state in ('done', 'sent', 'failed', 'unknown', 'held', 'cancelled'):
            self.assertIn(state, self.api.tools['tg_status'])
        self.assertIn('cancelled', self.api.tools['tg_send_text'])
        # Any message-effect operation can be a first contact, not only text.
        for op, (effect, _, _) in SPECS.items():
            self.assertEqual('ask in Main and wait' in self.api.tools['tg_' + op], effect == 'message' or op == 'join', op)
        self.assertIn('request_submitted is not a join', self.api.tools['tg_join'])
        self.assertIn('for everyone in the chat', self.api.tools['tg_delete'])

    async def test_unused_owner_id_is_not_asked_for_and_stored_value_is_kept(self):
        from userbot.participant_plugin import SETTINGS
        fields = [field['name'] for part in SETTINGS['components'] for field in part.get('fields', [])]
        self.assertNotIn('owner_id', fields)
        self.assertNotIn('Recognize the configured owner by numeric ID', json.dumps(SETTINGS))
        _write_json(self.root, 'participant.json', {**policy(self.root), 'owner_id': '42'})
        code, body = await self.call('participant/policy', 'POST', {'interval': '5'})
        self.assertEqual(code, 200, body)
        self.assertEqual((policy(self.root)['owner_id'], policy(self.root)['interval']), ('42', 5))

    async def test_policy_round_trips_scope_and_selection(self):
        code, body = await self.call('participant/policy', 'POST', {'scope': 'selected', 'selected_peers': '-1001, 809, 809', 'blocked_peers': ''})
        self.assertEqual(code, 200, body)
        code, body = await self.call('participant/policy')
        self.assertEqual((body['scope'], body['selected_peers'], body['blocked_peers']), ('selected', '-1001,809', ''))
        code, body = await self.call('participant/policy', 'POST', {'scope': 'some'})
        self.assertEqual(code, 409)
        self.assertEqual(policy(self.root)['scope'], 'selected')

    async def test_chat_list_marks_scope_and_refresh_only_stages_one_request(self):
        with Journal(self.root) as journal:
            journal.remember_chat(ACCOUNT, -1001, 'Board games', 'supergroup', 'dialogs', members=24)
            journal.remember_chat(ACCOUNT, 809, 'Mira', 'private', 'incoming')
            journal.remember_chat(ACCOUNT, -1002, 'Old announcements', 'channel', 'dialogs')
        save_policy(self.root, {'blocked_peers': [-1002]})
        code, body = await self.call('participant/chats')
        self.assertEqual(code, 200)
        rows = {c['id']: (c['title'], c['receiving'], c['excluded']) for c in body['chats']}
        self.assertEqual(rows, {'-1001': ('Board games', True, False), '809': ('Mira', True, False),
                                '-1002': ('Old announcements', False, True)})
        self.assertFalse(body['refresh']['pending'])
        for _ in range(2):
            code, reply = await self.call('participant/chats', 'POST', {'refresh': True})
            self.assertEqual(code, 200)
        self.assertTrue((self.root / 'chats-refresh.json').is_file())
        self.assertTrue((await self.call('participant/chats'))[1]['refresh']['pending'])
        self.assertEqual((await self.call('participant/chats', 'POST', {'refresh': 'yes'}))[0], 409)

    async def test_stale_inflight_refresh_is_not_pending(self):
        import os, time
        marker = self.root / 'chats-refresh.inflight'
        marker.write_text('')
        self.assertTrue((await self.call('participant/chats'))[1]['refresh']['pending'])
        old = time.time() - 600
        os.utime(marker, (old, old))
        self.assertFalse((await self.call('participant/chats'))[1]['refresh']['pending'])

    async def test_chat_refresh_needs_a_verified_account(self):
        save_config(self.root, Config(123, 'a' * 32, '+1234567890'))
        code, body = await self.call('participant/chats', 'POST', {'refresh': True})
        self.assertEqual((code, body['code']), (409, 'account_not_verified'))
        self.assertFalse((self.root / 'chats-refresh.json').exists())

    async def test_discard_held_reports_count_and_never_touches_attempted(self):
        with Journal(self.root) as journal:
            journal.enqueue(str(ACCOUNT), 'held', 'send_text', {'peer': '1', 'text': 'x'})
            journal.enqueue(str(ACCOUNT), 'attempted', 'send_text', {'peer': '1', 'text': 'y'})
            rows = {r['id']: dict(r) for r in journal.db.execute('SELECT * FROM jobs')}
            journal.hold(rows['held'], 'destination_inactive')
            journal.set(rows['attempted']['seq'], 'attempted')
        code, body = await self.call('participant/held', 'POST', {'action': 'discard'})
        self.assertEqual((code, body['discarded']), (200, 1))
        with Journal(self.root) as journal:
            self.assertEqual(journal.get(str(ACCOUNT), 'attempted')['state'], 'attempted')
        self.assertEqual((await self.call('participant/held', 'POST', {'action': 'send'}))[0], 409)

    async def test_import_plan_is_saved_but_cannot_start_without_host_support(self):
        code, body = await self.call('participant/import')
        self.assertEqual((body['available'], body['host_supports_import'], body['reason']), (False, False, 'host_support_pending'))
        plan = {'action': 'plan', 'chats': ['-1001'], 'exclude': [], 'from': '2026-09-26T00:00:00+03:00',
                'to': '2026-10-26T00:00:00+03:00', 'max_per_chat': 300}
        code, body = await self.call('participant/import', 'POST', plan)
        self.assertEqual(code, 200, body)
        self.assertIn('Nothing is read from Telegram', body['message'])
        listed = (await self.call('participant/import'))[1]['plans']
        self.assertEqual([(p['state'], p['spec']['chats'], p['spec']['from']) for p in listed],
                         [('saved', [-1001], '2026-09-25T21:00:00+00:00')])
        code, body = await self.call('participant/import', 'POST', {'action': 'start', 'id': listed[0]['id']})
        self.assertEqual((code, body['code']), (409, 'import_unavailable'))
        self.assertEqual((await self.call('participant/import'))[1]['plans'][0]['state'], 'saved')
        self.assertEqual((await self.call('participant/import', 'POST', {'action': 'cancel', 'id': listed[0]['id']}))[0], 200)
        self.assertEqual((await self.call('participant/import'))[1]['plans'][0]['state'], 'cancelled')

    async def test_import_plan_validation_refuses_out_of_scope_and_bad_windows(self):
        save_policy(self.root, {'blocked_peers': [-1002]})
        base = {'action': 'plan', 'chats': 'receiving', 'from': '2026-09-26T00:00:00+00:00', 'to': '2026-10-26T00:00:00+00:00'}
        for change, words in (({'chats': ['-1002']}, 'outside the receiving scope'), ({'from': '2026-11-01T00:00:00+00:00'}, 'before'),
                              ({'to': '2026-10-26'}, 'time zone'), ({'max_per_chat': 0}, 'max_per_chat'), ({'chats': []}, 'at least one')):
            code, body = await self.call('participant/import', 'POST', {**base, **change})
            self.assertEqual(code, 400, change)
            self.assertIn(words, body['message'])
        code, body = await self.call('participant/import', 'POST', base)
        self.assertEqual(code, 200)

    async def test_supported_host_runs_only_on_explicit_owner_actions(self):
        _write_json(self.root, 'host-capabilities.json', {'at': '2026-10-26T00:00:00+00:00', 'presence_history_import_version': 1,
                                                          'presence_source_order_version': 1})
        with Journal(self.root) as journal:
            journal.remember_chat(ACCOUNT, -1001, 'Board games', 'supergroup', 'dialogs')
        body = (await self.call('participant/import'))[1]
        self.assertEqual((body['available'], body['host_supports_import'], body['reason']), (True, True, ''))
        code, body = await self.call('participant/import', 'POST', {'action': 'plan', 'chats': 'receiving', 'exclude': [],
                                                                    'from': '2026-09-26T00:00:00+00:00', 'to': '2026-10-26T00:00:00+00:00'})
        self.assertIn('until you press Start', body['message'])
        plan = body['id']
        self.assertEqual((await self.call('participant/import'))[1]['plans'][0]['state'], 'saved')  # never by itself
        for action, state in (('start', 'running'), ('pause', 'paused'), ('resume', 'running'), ('cancel', 'cancelled')):
            code, body = await self.call('participant/import', 'POST', {'action': action, 'id': plan})
            self.assertEqual(code, 200, body)
            view = (await self.call('participant/import'))[1]['plans'][0]
            self.assertEqual(view['state'], state)
        self.assertEqual([c['id'] for c in view['chats']], ['-1001'])
        self.assertEqual(view['reason'], 'owner')
        for change in ({'action': 'resume', 'id': plan}, {'action': 'start'}, {'action': 'start', 'id': plan, 'chats': []},
                       {'action': 'cancel', 'id': 'imp_0000000000000000'}, {'action': 'run', 'id': plan}):
            code, body = await self.call('participant/import', 'POST', change)
            self.assertEqual(code, 400, change)

    async def test_start_and_resume_need_a_participant_companion_and_a_host_that_keeps_order(self):
        """Saving, Pause and Cancel always work; Start and Resume only where an import can run, and keep messages in order."""
        import os
        with Journal(self.root) as journal:
            journal.remember_chat(ACCOUNT, -1001, 'Board games', 'supergroup', 'dialogs')
        capabilities, started = self.root / 'host-capabilities.json', self.root / 'presence-capability.json'
        def host(**facts):  # what a companion started in participant mode records from /identity
            _write_json(self.root, 'host-capabilities.json', {'at': '2026-10-26T00:00:00+00:00', 'presence_history_import_version': 1, **facts})
        def legacy_started(later):  # what a legacy companion's start writes, before or after the participant record
            _write_json(self.root, 'presence-capability.json', {'delivery_reporting_version': 1, 'status': 'available'})
            when = capabilities.stat().st_mtime + (5 if later else -5)
            os.utime(started, (when, when))
        host(presence_source_order_version=1)
        legacy_started(later=False)
        window = {'chats': 'receiving', 'exclude': [], 'from': '2026-09-26T00:00:00+00:00', 'to': '2026-10-26T00:00:00+00:00'}
        running, paused, saved = [(await self.call('participant/import', 'POST', {'action': 'plan', **window}))[1]['id'] for _ in range(3)]
        for action, plan in (('start', paused), ('pause', paused), ('start', running)):
            self.assertEqual((await self.call('participant/import', 'POST', {'action': action, 'id': plan}))[0], 200, action)
        async def refused(reason, words):
            body = (await self.call('participant/import'))[1]
            self.assertEqual((body['available'], body['reason']), (False, reason))
            for action, plan in (('start', saved), ('resume', paused)):
                code, answer = await self.call('participant/import', 'POST', {'action': action, 'id': plan})
                self.assertEqual((code, answer['code']), (409, reason), action)
                self.assertIn(words, answer['message'])
            code, answer = await self.call('participant/import', 'POST', {'action': 'plan', **window})
            self.assertEqual(code, 200, answer)
            return answer['message']
        # A Host that records history but not the order of messages within one second: nothing starts, honestly said.
        host()
        self.assertIn('cannot keep messages sent within the same second in their order', await refused('host_source_order_missing', 'same second'))
        host(presence_source_order_version=1)
        # Free participant saved, but the companion last started in legacy mode: the earlier participant record is old.
        legacy_started(later=True)
        self.assertIn('until the companion runs in Free participant mode', await refused('restart_required', 'turned off and on'))
        legacy_started(later=False)
        # Legacy mode itself: the cached Host support stays a fact and does not make an import runnable.
        save_policy(self.root, {'mode': 'legacy'})
        await refused('participant_mode_required', 'Free participant')
        self.assertIs((await self.call('participant/import'))[1]['host_supports_import'], True)
        for action, plan in (('pause', running), ('cancel', saved)):
            self.assertEqual((await self.call('participant/import', 'POST', {'action': action, 'id': plan}))[0], 200, action)
        states = {p['id']: p['state'] for p in (await self.call('participant/import'))[1]['plans']}
        self.assertEqual((states[running], states[paused], states[saved]), ('paused', 'paused', 'cancelled'))
        save_policy(self.root, {'mode': 'participant'})
        self.assertEqual((await self.call('participant/import', 'POST', {'action': 'resume', 'id': paused}))[0], 200)

    async def test_plans_list_keeps_unfinished_imports_beyond_the_newest_twenty_and_counts_the_rest(self):
        _write_json(self.root, 'host-capabilities.json', {'at': '2026-10-26T00:00:00+00:00', 'presence_history_import_version': 1,
                                                          'presence_source_order_version': 1})
        with Journal(self.root) as journal:
            journal.remember_chat(ACCOUNT, -1001, 'Board games', 'supergroup', 'dialogs')
        window = {'action': 'plan', 'chats': 'receiving', 'exclude': [], 'from': '2026-09-26T00:00:00+00:00', 'to': '2026-10-26T00:00:00+00:00'}
        async def plan():
            return (await self.call('participant/import', 'POST', window))[1]['id']
        async def owner(action, plan_id):
            return (await self.call('participant/import', 'POST', {'action': action, 'id': plan_id}))[0]
        cancelled, partial, paused, running = [await plan() for _ in range(4)]
        with Journal(self.root) as journal:  # what an earlier run left: messages held back for the owner's Resume
            journal.db.execute("UPDATE imports SET state='partial', progress='{\"run\": 1, \"chats\": []}' WHERE id=?", (partial,))
            journal.db.commit()
        for action, plan_id in (('cancel', cancelled), ('start', paused), ('pause', paused), ('start', running)):
            self.assertEqual(await owner(action, plan_id), 200, action)
        newer = [await plan() for _ in range(20)]
        body = (await self.call('participant/import'))[1]
        self.assertEqual([p['id'] for p in body['plans']], newer[::-1] + [running, paused, partial])
        self.assertEqual(body['older_plans'], 1)
        self.assertEqual((await self.call('participant/import', 'POST', {'action': 'start', 'id': newer[-1]}))[1]['message'],
                         'another import is running; pause or cancel it first')
        self.assertEqual(await owner('cancel', running), 200)
        body = (await self.call('participant/import'))[1]
        self.assertEqual(([p['id'] for p in body['plans']][20:], body['older_plans']), ([paused, partial], 2))
        self.assertEqual(await owner('start', newer[-1]), 200)

    async def test_import_poll_counts_again_only_plans_whose_records_changed(self):
        """The widget asks every four seconds while an import runs; records of unchanged plans are not read again."""
        from userbot import history_import as H
        from userbot.presence import build_event
        sent = '2026-10-20T09:00:00+00:00'
        with Journal(self.root) as journal:
            def plan(state, records):  # what earlier runs left in the journal
                plan_id = H.save_plan(journal, ACCOUNT, {'chats': [4242], 'exclude': [], 'from': '2026-10-01T00:00:00+00:00',
                                                         'to': '2026-10-25T00:00:00+00:00', 'max_per_chat': 500})
                with journal.db:
                    journal.db.execute("UPDATE imports SET state=?, progress=? WHERE id=?", (state, json.dumps(
                        {'run': 1, 'chats': [{'peer': 4242, 'state': 'done', 'read': len(records)}]}), plan_id))
                    for seq, (mid, record_state, result) in enumerate(records, 1):
                        event = build_event(ACCOUNT, 4242, mid, 4242, f'm{mid}', message={'sent_at': sent})
                        journal.db.execute("INSERT INTO import_records(plan,seq,peer,thread,source_event_id,record,state,result) "
                                           "VALUES(?,?,?,?,?,?,?,?)", (plan_id, seq, 4242, '', event['source_event_id'],
                                                                      json.dumps({'event': event}), record_state, json.dumps(result)))
                return plan_id
            done = plan('done', [(1, 'recorded', {}), (2, 'duplicate', {}), (3, 'conflict', {'code': 'presence_event_identity_conflict'})])
            released = plan('done', [(4, 'recorded', {'released': 'receipt_refused', 'held_as': 'receipt_unconfirmed'}), (5, 'recorded', {})])
            partial = plan('partial', [(6, 'residual', {'reason': 'receipt_unconfirmed', 'receipt': 'retrying'}), (7, 'recorded', {})])
            cancelled = plan('cancelled', [(8, 'recorded', {}), (9, 'pending', {})])
        statements = []
        routes = sys.modules['participant_routes_test.userbot.participant_plugin']
        class Traced(routes.Journal):  # the route's own connection, as it opens one for each request
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                self.db.set_trace_callback(statements.append)
        async def poll():
            statements.clear()
            plans = (await self.call('participant/import'))[1]['plans']
            return {p['id']: p for p in plans}, [s for s in statements if 'import_records' in s]
        with mock.patch.object(routes, 'Journal', Traced):
            first, read = await poll()
            # One pass over each plan's records, and a listing only where something is held or released.
            self.assertEqual(len(read), 7, read)
            second, again = await poll()
            self.assertEqual((second, again), (first, []))
            # An answer that was in flight when the owner cancelled lands: only that plan is counted again.
            with Journal(self.root) as journal:
                H.HistoryImporter(NS(), journal, ACCOUNT)._settle_one({'plan': cancelled, 'seq': 2}, 'recorded', {'custody': 'history'})
            third, changed = await poll()
            self.assertEqual(len(changed), 1, changed)
        self.assertEqual(third[cancelled]['counts'], {'recorded': 2})
        self.assertEqual({k: v for k, v in third.items() if k != cancelled}, {k: v for k, v in first.items() if k != cancelled})
        # Every count and listed ID stays as the plan view gave it.
        self.assertEqual((first[done]['counts'], first[done]['held_total'], first[done]['held_omitted'],
                          [h['message_id'] for h in first[done]['held']]),
                         ({'recorded': 1, 'duplicate': 1, 'conflict': 1}, 1, 0, ['3']))
        self.assertEqual((first[released]['released_total'], first[released]['released_omitted'], first[released]['released']),
                         (1, 0, [{'chat': '4242', 'state': 'recorded', 'released': 'receipt_refused', 'message_id': '4'}]))
        self.assertEqual((first[partial]['held_total'], [h['reason'] for h in first[partial]['held']], first[partial]['released_total']),
                         (1, ['receipt_unconfirmed'], 0))
        self.assertEqual(first[cancelled]['counts'], {'recorded': 1, 'pending': 1})

    async def test_status_exposes_scope_place_and_profile_without_private_fields(self):
        _write_json(self.root, 'place-state.json', {'state': 'silent', 'label': 'Telegram', 'at': 'now'})
        _write_json(self.root, 'account-profile.json', {'id': str(ACCOUNT), 'username': 'ouro_demo', 'first_name': 'Ouroboros', 'last_name': 'AI'})
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [1, 2], 'blocked_peers': [3]})
        code, body = await self.call('participant/status')
        self.assertEqual(body['scope'], {'mode': 'selected', 'excluded': 1, 'selected': 2})
        self.assertEqual(body['place']['state'], 'silent')
        self.assertEqual(body['profile']['username'], 'ouro_demo')
        self.assertNotIn('phone', json.dumps(body))
        _write_json(self.root, 'account-profile.json', {'id': '1', 'username': 'other'})
        self.assertEqual((await self.call('participant/status'))[1]['profile'], {})

    async def test_status_says_whether_legacy_mode_has_an_enrollment_to_serve(self):
        # The legacy companion waits for a binding and allowed chats; without them it serves nothing.
        self.assertIs((await self.call('participant/status'))[1]['legacy_enrolled'], False)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT, allowed_peer_ids=(-1001,)))
        self.assertIs((await self.call('participant/status'))[1]['legacy_enrolled'], False)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT,
                                      binding_id='b' * 32, allowed_peer_ids=(-1001,)))
        self.assertIs((await self.call('participant/status'))[1]['legacy_enrolled'], True)


if __name__ == '__main__':
    unittest.main()
