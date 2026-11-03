"""Retained legacy intake against the current Host's Presence shapes.

The actual bridge, PresenceClient and durable store on synthetic private state; the Host
is a scripted transport and Telegram a fake client. No session, network or live effect.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_participant import Adapter, Client, HostFake
from userbot import runtime
from userbot.config import Config, save_config
from userbot.participant import CurrentAuthorization
from userbot.participant_state import save_policy
from userbot.presence import PresenceClient, PresenceToken
from userbot.runtime import UserSessionBridge
from userbot.store import DurableStore

ACCOUNT, PEER, ACTOR, BINDING = 8667525238, -1000000000123, 809, 'b' * 32


def incoming(mid=10, text='synthetic text'):
    return NS(chat_id=PEER, sender_id=ACTOR, raw_text=text,
              message=NS(id=mid, out=False, mentioned=False, reply_to=None, action=None))


def turn(outcome='silent', text='', child=''):
    """A version-0 /presence/turn answer: work_ref names the admitted child, if any."""
    return 200, {'ok': True, 'status': 'completed', 'outcome': outcome, 'text': text,
                 'turn_ref': 'turn', 'work_ref': child, 'delivery_reporting_version': 1}


def pending(ref):
    return 202, {'ok': True, 'status': 'pending', 'work_ref': ref, 'task_id': ref,
                 'delivery_reporting_version': 1}


def ended(ref, outcome='silent', text='', output='', child='', status='completed', **facts):
    """A /presence/work view: work_ref names the polled reference itself."""
    return 200, {'ok': True, 'status': status, 'outcome': outcome, 'text': text, 'work_ref': ref,
                 'task_id': ref, 'output_ref': output, 'child_work_ref': child,
                 'delivery_reporting_version': 1, **facts}


RETAINED = (409, {'ok': False, 'disposition': 'retained', 'code': 'presence_attempt_outcome_unknown',
                  'error': 'presence_attempt_outcome_unknown: source_event_id', 'field': 'source_event_id',
                  'turn_ref': 'turn', 'delivery_reporting_version': 1})


class LegacyIntake(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.stack = ExitStack()
        self.cfg = Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT, binding_id=BINDING,
                          allowed_peer_ids=(PEER,))
        save_config(self.root, self.cfg)
        save_policy(self.root, {'mode': 'legacy', 'outgoing': True})
        self.client = Client()
        self.adapter = Adapter(self.root / 'unused.session', 123, 'a' * 32, client=self.client)
        self.catalog_reads = 0
        async def no_forum(_peer):
            self.catalog_reads += 1
            raise ValueError('not a forum')
        self.adapter.topic_ids = no_forum
        self.store = self.stack.enter_context(DurableStore(self.root / 'transport.sqlite3'))
        self.calls, self.script = [], {}
        presence = PresenceClient('http://127.0.0.1:1', PresenceToken('synthetic'), BINDING,
                                  transport=self.transport)
        self.bridge = UserSessionBridge(self.root, self.cfg, self.adapter, self.store, presence,
                                        authorization=CurrentAuthorization(self.root, HostFake(), ACCOUNT, 'legacy'))

    def tearDown(self):
        self.stack.close(); self.tmp.cleanup()

    def transport(self, method, path, payload, _headers):
        # Each scripted answer is used once; the last one repeats.
        key = path if method == 'POST' else path.split('?')[0].rsplit('/', 1)[-1]
        self.calls.append(key)
        answers = self.script[key]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    async def rounds(self, count):
        for _ in range(count):
            await self.bridge.process_incoming(limit=1)

    def outbox(self):
        rows = self.store.db.execute('SELECT intent_id, payload_ref FROM outgoing ORDER BY rowid').fetchall()
        return [(row['intent_id'], json.loads((self.root / 'outgoing' / (row['payload_ref'] + '.json')).read_text()))
                for row in rows]

    def texts(self):
        return [payload['text'] for _, payload in self.outbox()]

    def acked(self):
        return [row['status'] for row in self.store.db.execute('SELECT status FROM inbox_delivery')] == ['acked']

    async def test_exclusion_stops_capture_and_a_first_submission(self):
        save_policy(self.root, {'blocked_peers': [PEER]})
        self.assertIsNone(self.bridge.spool_update(incoming(10)))
        save_policy(self.root, {'blocked_peers': []})
        path = self.bridge.spool_update(incoming(11))
        save_policy(self.root, {'blocked_peers': [PEER]})  # excluded after capture, before admission
        await self.rounds(2)
        self.assertEqual((self.calls, self.catalog_reads), ([], 0))
        self.assertTrue(path.with_suffix('.held').exists())
        self.assertEqual(self.store.pending_raw_count(str(ACCOUNT)), 0)
        save_policy(self.root, {'blocked_peers': []})  # re-inclusion revives nothing
        await self.rounds(1)
        self.assertEqual(self.calls, [])

    async def test_exclusion_saved_while_the_event_is_prepared_stops_its_first_submission(self):
        # The owner excludes the chat while topic/forum preparation awaits Telegram.
        self.script = {'/presence/turn': [turn()]}
        path = self.bridge.spool_update(incoming())
        prepare = self.bridge._eligible
        async def excluded_meanwhile(raw):
            result = await prepare(raw)
            save_policy(self.root, {'blocked_peers': [PEER]})
            return result
        self.bridge._eligible = excluded_meanwhile
        await self.rounds(1)
        self.assertEqual(self.calls, [])
        self.assertTrue(path.with_suffix('.held').exists())
        self.assertEqual(self.store.pending_raw_count(str(ACCOUNT)), 0)

    async def test_exclusion_keeps_work_the_host_already_admitted(self):
        self.script = {'/presence/turn': [turn('deferred', child='child')],
                       'child': [ended('child', 'message', 'Child answer', output='out-child')]}
        self.bridge.spool_update(incoming())
        await self.rounds(1)
        save_policy(self.root, {'blocked_peers': [PEER]})
        await self.rounds(1)
        self.assertEqual((self.calls, self.texts()), (['/presence/turn', 'child'], ['Child answer']))
        self.assertTrue(self.acked())
        await self.bridge.send_one()  # a reply to the now excluded chat is refused, not sent
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.get(str(ACCOUNT), self.outbox()[0][0]).status, 'failed')

    async def test_retained_turn_is_observed_by_its_original_reference(self):
        self.script = {'/presence/turn': [RETAINED],
                       'turn': [pending('turn'), ended('turn', status='failed', child='child',
                                                      code='presence_attempt_outcome_unknown')],
                       'child': [ended('child', 'message', 'From the child', output='out-child')]}
        path = self.bridge.spool_update(incoming())
        await self.rounds(1)
        self.assertEqual(json.loads(path.read_text())['work_ref'], 'turn')
        await self.rounds(4)
        self.assertEqual(self.calls, ['/presence/turn', 'turn', 'turn', 'child'])
        self.assertEqual(self.texts(), ['From the child'])
        self.assertTrue(self.acked()); self.assertFalse(path.exists())

    async def test_each_deferral_is_followed_to_its_child_and_every_output_queued_once(self):
        self.script = {'/presence/turn': [turn('deferred', 'Looking.', child='child')],
                       'child': [ended('child', 'deferred', 'Still checking.', output='out-1', child='grandchild')],
                       'grandchild': [pending('grandchild'), ended('grandchild', 'message', 'Found it.', output='out-2')]}
        self.bridge.spool_update(incoming())
        await self.rounds(6)
        self.assertEqual(self.calls, ['/presence/turn', 'child', 'grandchild', 'grandchild'])
        self.assertEqual(self.texts(), ['Looking.', 'Still checking.', 'Found it.'])
        self.assertEqual(len({intent for intent, _ in self.outbox()}), 3)
        self.assertTrue(self.acked())

    async def test_an_answer_with_a_child_also_waits_for_the_child(self):
        self.script = {'/presence/turn': [turn('message', 'First part.', child='child')],
                       'child': [ended('child', 'message', 'Second part.', output='out-child')]}
        self.bridge.spool_update(incoming())
        await self.rounds(3)
        self.assertEqual((self.calls, self.texts()), (['/presence/turn', 'child'], ['First part.', 'Second part.']))
        self.assertTrue(self.acked())

    async def test_failed_work_keeps_selected_output_and_its_child(self):
        self.script = {'/presence/turn': [pending('turn')],
                       'turn': [ended('turn', 'message', 'A useful partial answer.', output='selected',
                                      child='child', status='failed')],
                       'child': [ended('child', 'message', 'The rest.', output='out-child')]}
        self.bridge.spool_update(incoming())
        await self.rounds(4)
        self.assertEqual(self.calls, ['/presence/turn', 'turn', 'child'])
        self.assertEqual(self.texts(), ['A useful partial answer.', 'The rest.'])
        self.assertTrue(self.acked())

    async def test_failed_text_without_an_output_identity_is_not_speech(self):
        self.script = {'/presence/turn': [pending('turn')],
                       'turn': [ended('turn', 'message', 'Task failed: provider error', status='failed')]}
        path = self.bridge.spool_update(incoming())
        await self.rounds(3)
        self.assertEqual((self.calls, self.texts()), (['/presence/turn', 'turn'], []))
        self.assertTrue(path.with_suffix('.held').exists())

    async def test_a_stop_around_storing_the_child_neither_loses_nor_repeats_a_reply(self):
        self.script = {'/presence/turn': [pending('turn')],
                       'turn': [ended('turn', 'deferred', 'Looking.', output='out-1', child='child')],
                       'child': [ended('child', 'message', 'Found it.', output='out-2')]}
        path = self.bridge.spool_update(incoming())
        await self.rounds(1)
        real = runtime._atomic_json
        def lost_write(target, value):
            if value.get('work_ref') == 'child':
                raise OSError('synthetic stop before the child is stored')
            return real(target, value)
        with patch.object(runtime, '_atomic_json', lost_write):
            await self.rounds(1)
        self.assertEqual((json.loads(path.read_text())['work_ref'], self.texts()), ('turn', []))
        real_queue = self.bridge._queue_reply
        def stop_once(*args, **kw):
            self.bridge._queue_reply = real_queue
            raise OSError('synthetic stop after the child is stored')
        self.bridge._queue_reply = stop_once
        self.bridge._blocked_until.clear()
        await self.rounds(1)
        self.assertEqual((json.loads(path.read_text())['work_ref'], self.texts()), ('child', []))
        self.bridge._blocked_until.clear()
        await self.rounds(2)
        self.assertEqual(self.calls, ['/presence/turn', 'turn', 'turn', 'child'])
        self.assertEqual(self.texts(), ['Looking.', 'Found it.'])
        self.assertTrue(self.acked())

    async def test_replies_queued_before_output_identities_are_not_sent_twice(self):
        # An event stuck under the earlier adapter: its interim was queued in the interim
        # slot while the parent was polled, and a second interim waited without an identity.
        self.script = {'/presence/turn': [pending('turn')],
                       'turn': [ended('turn', 'deferred', 'Looking.', output='out-1', child='child')],
                       'child': [ended('child', 'deferred', 'Still checking.', output='out-2', child='grandchild')],
                       'grandchild': [ended('grandchild', 'message', 'Found it.', output='out-3')]}
        path = self.bridge.spool_update(incoming())
        await self.rounds(1)
        raw = json.loads(path.read_text())
        source = 'telegram:%s:%s:0:10' % (ACCOUNT, PEER)
        self.bridge._queue_reply(raw, None, BINDING, 'Looking.', 1, source, 'turn', phase='interim')
        await self.rounds(1)
        raw = json.loads(path.read_text())
        raw['pending_interim'] = {'text': 'Still checking.', 'version': 1, 'turn_ref': 'child'}
        runtime._atomic_json(path, raw)
        await self.rounds(3)
        self.assertEqual(self.texts(), ['Looking.', 'Still checking.', 'Found it.'])
        prefix = 'auto_' + hashlib.sha256(source.encode()).hexdigest()
        self.assertEqual(self.outbox()[0][0], prefix + '_interim')
        self.assertTrue(self.acked())

    def same_text_selections(self, child):
        """The repair review probe: the earlier adapter queued one interim without an output
        identity while a parent was polled; that parent also released another output saying
        the same."""
        self.script = {'/presence/turn': [pending('turn')],
                       'turn': [ended('turn', 'deferred', 'same', output='terminal-selection', child='child', outputs=[
                           {'output_ref': 'released-selection', 'outcome': 'deferred', 'text': 'same'}])],
                       'child': [child]}
        path = self.bridge.spool_update(incoming())
        source = 'telegram:%s:%s:0:10' % (ACCOUNT, PEER)
        return path, source, 'auto_' + hashlib.sha256(source.encode()).hexdigest()

    async def test_a_reply_queued_before_output_identities_is_only_one_selected_output(self):
        path, source, prefix = self.same_text_selections(ended('child'))
        await self.rounds(1)
        self.bridge._queue_reply(json.loads(path.read_text()), None, BINDING, 'same', 1, source, 'turn', phase='interim')
        await self.rounds(3)
        self.assertEqual(self.calls, ['/presence/turn', 'turn', 'child'])
        self.assertEqual([(intent, payload.get('output_ref', ''), payload['text']) for intent, payload in self.outbox()],
                         [(prefix + '_interim', '', 'same'),
                          (prefix + '_' + hashlib.sha256(b'terminal-selection').hexdigest()[:32], 'terminal-selection', 'same')])
        self.assertTrue(self.acked())

    async def test_which_output_an_earlier_reply_belongs_to_survives_a_stop(self):
        # The child then selects a third output saying the same.
        path, source, prefix = self.same_text_selections(ended('child', 'message', 'same', output='child-selection'))
        await self.rounds(1)
        self.bridge._queue_reply(json.loads(path.read_text()), None, BINDING, 'same', 1, source, 'turn', phase='interim')
        enqueue = self.store.enqueue
        def stop_before_the_second(account, intent, **kw):
            if intent.endswith(hashlib.sha256(b'terminal-selection').hexdigest()[:32]):
                self.store.enqueue = enqueue
                raise OSError('synthetic stop after the earlier reply was matched')
            return enqueue(account, intent, **kw)
        self.store.enqueue = stop_before_the_second
        await self.rounds(1)
        raw = json.loads(path.read_text())
        self.assertEqual((raw['work_ref'], raw['legacy_reply_outputs'], len(self.outbox())),
                         ('child', {prefix + '_interim': 'released-selection'}, 1))
        # A new process: only the source event file and the stores carry the match.
        self.bridge = UserSessionBridge(self.root, self.cfg, self.adapter, self.store, self.bridge.presence,
                                        authorization=self.bridge.authorization)
        await self.rounds(3)
        self.assertEqual(self.calls, ['/presence/turn', 'turn', 'child'])
        self.assertEqual([payload.get('output_ref', '') for _, payload in self.outbox()],
                         ['', 'terminal-selection', 'child-selection'])
        self.assertEqual(self.texts(), ['same'] * 3)
        self.assertTrue(self.acked())

    async def test_each_reply_reports_its_own_delivery(self):
        self.script = {'/presence/turn': [turn('deferred', 'Looking.', child='child')],
                       'child': [ended('child', 'message', 'Found it.', output='out-child')],
                       '/presence/delivery': [(200, {'ok': True, 'recorded': True})]}
        self.bridge.spool_update(incoming())
        await self.rounds(2)
        self.assertTrue(await self.bridge.send_one()); self.assertTrue(await self.bridge.send_one())
        bodies = []
        real = self.transport
        def record(method, path, payload, headers):
            if path == '/presence/delivery':
                bodies.append(payload)
            return real(method, path, payload, headers)
        self.bridge.presence._transport = record
        self.assertEqual(await self.bridge.report_sent(limit=5), 2)
        source = 'telegram:%s:%s:0:10' % (ACCOUNT, PEER)
        prefix = 'auto_' + hashlib.sha256(source.encode()).hexdigest()
        self.assertEqual([(b['delivery_id'], b['part_id'], b['origin']['task_id']) for b in bodies],
                         [(prefix, '0', 'turn'),
                          (prefix + '_' + hashlib.sha256(b'out-child').hexdigest()[:32], '0', 'child')])
        self.assertEqual(await self.bridge.report_sent(limit=5), 0)  # reporting never re-sends


if __name__ == '__main__':
    unittest.main()
