"""Offline preflight identity and diagnostics; never opens a Telegram session."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telethon import types as T, functions as F
from userbot.config import Config, save_config
from userbot.participant_state import Journal, save_policy
from userbot.participant import Participant
from userbot.telegram import TelegramAdapter
import test_participant as fixtures

ACCOUNT = 123456789
PEER = '-100123'


class PreflightTargets(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
        save_policy(self.root, {'mode': 'participant', 'outgoing': True})
        self.client = fixtures.Client()
        self.adapter = TelegramAdapter(self.root / 'never-opened.session', 123, 'a' * 32,
                                       client=self.client)
        self.journal = Journal(self.root)
        self.host = fixtures.HostFake()
        self.engine = Participant(self.root, self.adapter, self.host, self.journal, ACCOUNT)
        async def me(): return ACCOUNT
        self.adapter.get_me = me

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    async def test_reply_requires_exact_message_and_resolved_peer(self):
        for bad in (None, NS(id=88, peer_id=T.PeerChannel(123)),
                    NS(id=42, peer_id=T.PeerChannel(999)), NS(id=42)):
            async def read(*a, **kw): return bad
            self.client.get_messages = read
            with self.subTest(target=repr(bad)):
                with self.assertRaisesRegex(ValueError, 'reply target unavailable or wrong peer'):
                    await self.engine.ops.prepare('send_text',
                        {'peer': PEER, 'reply_id': 42, 'text': 'synthetic'})
        self.assertEqual(self.client.calls, [])

    async def test_mutations_require_exact_message_not_only_peer(self):
        async def wrong_id(*a, **kw): return NS(id=88, peer_id=T.PeerChannel(123))
        self.client.get_messages = wrong_id
        for op, extra in [('edit', {'text': 'synthetic'}), ('delete', {}),
                          ('react', {'emoji': 'x'}), ('pin', {})]:
            with self.subTest(op=op):
                with self.assertRaisesRegex(ValueError, 'message target belongs to another conversation or is unavailable'):
                    await self.engine.ops.prepare(op, {'peer': PEER, 'message_id': 42, **extra})
        self.assertEqual(self.client.calls, [])

    async def test_saved_messages_targets_use_resolved_account(self):
        async def resolve(*a): return T.InputPeerSelf()
        async def read(*a, **kw): return NS(id=42, peer_id=T.PeerUser(ACCOUNT))
        self.client.get_input_entity = resolve
        self.client.get_messages = read
        a = await self.engine.ops.prepare('send_text', {'peer': 'me', 'reply_id': 42, 'text': 'synthetic'})
        self.assertEqual(a['resolved_peer'], str(ACCOUNT))
        self.assertIsNone(a['topic_id'])
        for op, extra in [('edit', {'text': 'synthetic'}), ('delete', {}), ('react', {'emoji': 'x'}), ('pin', {})]:
            a = await self.engine.ops.prepare(op, {'peer': 'me', 'message_id': 42, **extra})
            self.assertEqual(a['resolved_peer'], str(ACCOUNT))
        async def wrong(*a, **kw): return NS(id=42, peer_id=T.PeerUser(999))
        self.client.get_messages = wrong
        with self.assertRaisesRegex(ValueError, 'reply target unavailable or wrong peer'):
            await self.engine.ops.prepare('send_text', {'peer': 'me', 'reply_id': 42, 'text': 'synthetic'})

    async def test_ordinary_reply_keeps_chat_conversation(self):
        async def read(*a, **kw):
            return NS(id=42, peer_id=T.PeerChannel(123),
                      reply_to=T.MessageReplyHeader(reply_to_msg_id=41, reply_to_top_id=40))
        self.client.get_messages = read
        a = await self.engine.ops.prepare('send_text', {'peer': PEER, 'reply_id': 42, 'text': 'synthetic'})
        self.assertIsNone(a['topic_id'])
        await self.engine.ops.execute('send_text', a, 1)
        request = self.client.calls[-1]
        self.assertIsInstance(request, F.messages.SendMessageRequest)
        self.assertEqual(request.reply_to.reply_to_msg_id, 42)
        self.assertIsNone(request.reply_to.top_msg_id)

    async def dispatch(self, name, op='send_text', extra=None):
        args = {'peer': PEER, 'text': 'synthetic', 'reply_id': 42} if op == 'send_text' else {}
        args.update(extra or {})
        self.journal.enqueue(ACCOUNT, name, op, args)
        await self.engine.dispatch_one()
        row = self.journal.get(ACCOUNT, name)
        return row, json.loads(row['result'])

    async def test_refusal_records_safe_reason_and_phase(self):
        async def missing(*a, **kw): return None
        self.client.get_messages = missing
        row, result = await self.dispatch('missing')
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(result['code'], 'preflight_refused')
        self.assertEqual(result.get('phase'), 'operation_preparation')
        self.assertEqual(result.get('reason'), 'reply_target_unavailable')
        async def arbitrary(*a): raise ValueError('private-synthetic-secret')
        self.engine.ops.prepare = arbitrary
        row, result = await self.dispatch('arbitrary', 'dialogs')
        self.assertEqual(result.get('phase'), 'operation_preparation')
        self.assertIsNone(result['reason'])
        self.assertNotIn('private-synthetic-secret', json.dumps(result))
        self.assertEqual(self.client.calls, [])

    async def test_transient_keeps_queue_and_phase_without_exception_values(self):
        async def unavailable(*a): raise OSError('private-synthetic-secret')
        self.engine.ops.prepare = unavailable
        row, result = await self.dispatch('transient', 'dialogs')
        self.assertEqual(row['state'], 'queued')
        self.assertEqual(result['code'], 'preflight_unavailable')
        self.assertEqual(result.get('phase'), 'operation_preparation')
        self.assertEqual(result.get('type'), 'OSError')
        self.assertNotIn('private-synthetic-secret', json.dumps(result))

    async def test_account_and_final_authorization_phases(self):
        async def wrong(): return 999
        self.adapter.get_me = wrong
        row, result = await self.dispatch('account', 'dialogs')
        self.assertEqual(result.get('phase'), 'account_verification')
        self.assertEqual(result['reason'], 'account_changed')
        async def me(): return ACCOUNT
        self.adapter.get_me = me
        async def prepared(*a): return {'resolved_peer': PEER}
        self.engine.ops.prepare = prepared
        async def guard(peer='', topic='', write=False, **kw):
            if peer == PEER: raise ValueError('peer blocked by current configuration')
        self.engine.authorize = guard
        row, result = await self.dispatch('final', 'dialogs')
        self.assertEqual(result.get('phase'), 'final_authorization')
        self.assertEqual(result['reason'], 'peer_excluded')
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__':
    unittest.main()
