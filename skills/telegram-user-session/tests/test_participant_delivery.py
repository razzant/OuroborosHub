"""Held work, fair hydration, receiving scope and stable identity. Offline fakes only."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_participant import ARGS, Adapter, Client, HostFake
from userbot.config import Config, save_config, _write_json
from userbot.participant import Participant
from userbot.participant_state import Journal, digest, policy, save_policy, receiving_refusal
from userbot.normalize import canonical_text, content_revision, media_descriptor
from telethon import functions as F, types as T

ACCOUNT = 8667525238
GROUP = -1000000000123


def text_message(mid, peer, text='hi', **kw):
    return T.Message(id=mid, peer_id=T.PeerUser(peer) if peer > 0 else T.PeerChannel(-peer - 1000000000000),
                     date=kw.pop('date', datetime.now(timezone.utc)), message=text,
                     from_id=T.PeerUser(kw.pop('sender', peer if peer > 0 else 809)), **kw)


def incoming(message, peer, sender=None, private=None):
    sender = sender or (peer if peer > 0 else 809)
    return NS(message=message, chat_id=peer, sender_id=sender, raw_text=message.message,
              is_private=peer > 0 if private is None else private)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
        save_policy(self.root, {'mode': 'participant', 'outgoing': True, 'expected_account_id': str(ACCOUNT)})
        self.client = Client(); self.adapter = Adapter(self.root / 'unused.session', 123, 'a' * 32, client=self.client)
        self.j = Journal(self.root); self.host = HostFake()
        self.e = Participant(self.root, self.adapter, self.host, self.j, ACCOUNT)

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def sends(self):
        return [r for r in self.client.calls if isinstance(r, F.messages.SendMessageRequest)]


class HeldWork(Base):
    async def test_silent_place_holds_then_active_sends_exactly_once(self):
        self.host.state = 'silent'
        self.j.enqueue(self.e.account, 'reply', 'send_text', ARGS['send_text'])
        await self.e.dispatch_one()
        row = self.j.get(self.e.account, 'reply')
        self.assertEqual(row['state'], 'held')
        result = json.loads(row['result'])
        self.assertEqual((result['code'], result['reason'], result['checks']), ('held', 'destination_inactive', 1))
        self.assertEqual(self.sends(), [])
        # Not due yet: nothing is claimed early.
        self.assertFalse(await self.e.dispatch_one())
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        await self.e.dispatch_one()
        again = json.loads(self.j.get(self.e.account, 'reply')['result'])
        self.assertEqual(again['checks'], 2)
        self.assertEqual(again['held_since_unix'], result['held_since_unix'])
        self.host.state = 'active'
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        await self.e.dispatch_one()
        self.assertEqual(self.j.get(self.e.account, 'reply')['state'], 'sent')
        self.assertEqual(len(self.sends()), 1)
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        self.assertFalse(await self.e.dispatch_one())
        self.assertEqual(len(self.sends()), 1)

    async def test_paused_and_local_switch_hold_without_any_provider_call(self):
        self.host.state = 'paused'
        self.j.enqueue(self.e.account, 'paused', 'react', ARGS['react'])
        await self.e.dispatch_one()
        self.assertEqual(json.loads(self.j.get(self.e.account, 'paused')['result'])['reason'], 'destination_inactive')
        save_policy(self.root, {'outgoing': False})
        # Another chat: a later write to the held one's chat waits behind it instead.
        self.j.enqueue(self.e.account, 'local', 'send_text', {**ARGS['send_text'], 'peer': '-100456'})
        await self.e.dispatch_one()
        row = self.j.get(self.e.account, 'local')
        self.assertEqual((row['state'], json.loads(row['result'])['reason']), ('held', 'outgoing_paused'))
        self.assertEqual(self.client.calls, [])

    async def test_reads_are_not_held_by_silent_or_paused(self):
        self.host.state = 'paused'
        self.j.enqueue(self.e.account, 'read', 'history', ARGS['history'])
        await self.e.dispatch_one()
        self.assertEqual(self.j.get(self.e.account, 'read')['state'], 'done')

    async def test_new_work_for_active_place_goes_before_held_rechecks(self):
        self.j.enqueue(self.e.account, 'held', 'send_text', ARGS['send_text'])
        row = self.j.claim(self.e.account)
        self.j.hold(row, 'destination_inactive')
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        self.j.enqueue(self.e.account, 'fresh', 'send_text', {'peer': '-100456', 'text': 'fresh'})
        self.assertEqual(self.j.claim(self.e.account)['id'], 'fresh')

    async def test_follow_up_to_the_same_chat_never_overtakes_held_work(self):
        # The original review probe: held across rechecks, then the place reopens.
        self.host.state = 'paused'
        self.j.enqueue(self.e.account, 'first', 'send_text', {'peer': '-100123', 'text': 'first'})
        for _ in range(5):
            self.j.db.execute("UPDATE jobs SET due=0 WHERE id='first'"); self.j.db.commit()
            await self.e.dispatch_one()
        self.assertEqual(json.loads(self.j.get(self.e.account, 'first')['result'])['checks'], 5)
        self.host.state = 'active'
        self.j.enqueue(self.e.account, 'second', 'send_text', {'peer': '-100123', 'text': 'second'})
        self.j.enqueue(self.e.account, 'other-chat', 'send_text', {'peer': '-100456', 'text': 'other chat'})
        self.j.enqueue(self.e.account, 'other-topic', 'send_text', {'peer': '-100123', 'text': 'other topic', 'topic_id': 40})
        self.j.enqueue(self.e.account, 'read', 'history', {'peer': '-100123'})
        # Before the held row's backoff ends, everything except its own chat proceeds.
        for _ in range(4):
            await self.e.dispatch_one()
        self.assertEqual([r.message for r in self.sends()], ['other chat', 'other topic'])
        self.assertEqual(self.j.get(self.e.account, 'read')['state'], 'done')
        self.assertEqual(self.j.get(self.e.account, 'second')['state'], 'queued')
        self.assertFalse(await self.e.dispatch_one())
        self.j.db.execute("UPDATE jobs SET due=0 WHERE id='first'"); self.j.db.commit()
        await self.e.dispatch_one(); await self.e.dispatch_one()
        self.assertEqual([r.message for r in self.sends()], ['other chat', 'other topic', 'first', 'second'])

    async def test_a_reply_whose_topic_is_read_in_preparation_is_not_overtaken_in_that_topic(self):
        # The repair review probe: one numeric chat, the held write names topic 40 only
        # through the message it replies to, the follow-up names it explicitly.
        async def forum(peer):
            return T.Channel(id=123, title='Forum', photo=T.ChatPhotoEmpty(), date=None, megagroup=True,
                             access_hash=456, forum=True)
        async def in_topic(peer, ids=None, **kw):
            m = T.Message(id=ids or 50, peer_id=T.PeerChannel(123), date=None, message='source',
                          reply_to=T.MessageReplyHeader(reply_to_msg_id=40, reply_to_top_id=40, forum_topic=True))
            return m if ids else [m]
        self.client.get_entity, self.client.get_messages = forum, in_topic
        self.host.state = 'paused'
        self.j.enqueue(self.e.account, 'first', 'send_text', {'peer': str(GROUP), 'text': 'first', 'reply_id': 42})
        await self.e.dispatch_one()
        self.host.state = 'active'
        self.j.enqueue(self.e.account, 'second', 'send_text', {'peer': str(GROUP), 'text': 'second', 'topic_id': 40})
        self.j.enqueue(self.e.account, 'other-chat', 'send_text', {'peer': '-100456', 'text': 'other chat'})
        self.j.enqueue(self.e.account, 'read', 'history', {'peer': str(GROUP), 'topic_id': 40})
        for _ in range(3):
            await self.e.dispatch_one()
        self.assertEqual([r.message for r in self.sends()], ['other chat'])
        self.assertEqual(self.j.get(self.e.account, 'read')['state'], 'done')
        self.assertEqual([self.j.get(self.e.account, k)['state'] for k in ('first', 'second')], ['held', 'queued'])
        self.assertFalse(await self.e.dispatch_one())
        self.j.db.execute("UPDATE jobs SET due=0 WHERE id='first'"); self.j.db.commit()
        await self.e.dispatch_one(); await self.e.dispatch_one()
        self.assertEqual([r.message for r in self.sends()], ['other chat', 'first', 'second'])
        self.assertEqual(self.sends()[1].reply_to.top_msg_id, 40)

    async def test_held_order_follows_the_topic_preparation_will_use(self):
        first, later = {'text': 'first'}, {'text': 'later'}
        reaction, edit = {'message_id': 10, 'emoji': '👍'}, {'message_id': 10, 'text': 'new'}
        cases = [  # earlier held write, later operation, whether the later one may go first
            (('send_text', {**first, 'reply_id': 42}), ('send_text', {**later, 'topic_id': 40}), False),
            (('send_text', {**first, 'topic_id': 40}), ('send_text', {**later, 'reply_id': 42}), False),
            (('send_text', {**first, 'reply_id': 42}), ('send_text', later), False),
            (('send_text', first), ('send_text', {**later, 'reply_id': 42}), False),
            # A reaction or edit without topic_id takes the topic of the message it acts on.
            (('react', reaction), ('send_text', {**later, 'topic_id': 40}), False),
            (('send_text', {**first, 'topic_id': 40}), ('edit', edit), False),
            (('send_text', {**first, 'topic_id': 1}), ('send_text', later), False),  # General is topic 1
            (('send_text', {**first, 'topic_id': 40}), ('send_text', {**later, 'topic_id': 41}), True),
            (('send_text', {**first, 'topic_id': 40}), ('send_text', later), True),
            (('send_text', {**first, 'topic_id': 40, 'reply_id': 42}),
             ('send_text', {**later, 'topic_id': 41, 'reply_id': 43}), True),
            (('react', {**reaction, 'topic_id': 40}), ('send_text', {**later, 'topic_id': 41}), True),
            (('send_text', {**first, 'reply_id': 42}), ('send_text', {**later, 'peer': '-100456'}), True),
            (('send_text', {**first, 'reply_id': 42}), ('history', {}), True),  # reads never wait
        ]
        for n, ((op, earlier), (later_op, args), overtakes) in enumerate(cases):
            self.j.db.execute("UPDATE jobs SET state='cancelled'"); self.j.db.commit()  # each case alone
            with self.subTest(earlier=(op, earlier), later=(later_op, args)):
                self.j.enqueue(self.e.account, f'earlier-{n}', op, {'peer': '-100123', **earlier})
                self.j.hold(self.j.claim(self.e.account), 'destination_inactive')
                self.j.enqueue(self.e.account, f'later-{n}', later_op, {'peer': '-100123', **args})
                claimed = self.j.claim(self.e.account)
                self.assertEqual(claimed and claimed['id'], f'later-{n}' if overtakes else None)

    async def test_backed_off_queued_write_also_keeps_its_place(self):
        self.j.enqueue(self.e.account, 'first', 'send_text', {'peer': '-100123', 'text': 'first'})
        row = self.j.claim(self.e.account)
        self.j.set(row['seq'], 'queued', {'code': 'preflight_unavailable'}, 30)
        self.j.enqueue(self.e.account, 'second', 'send_text', {'peer': '-100123', 'text': 'second'})
        self.assertIsNone(self.j.claim(self.e.account))
        # A discarded or definitely failed head no longer holds its chat.
        self.j.set(row['seq'], 'failed', {'code': 'preflight_refused'})
        self.assertEqual(self.j.claim(self.e.account)['id'], 'second')

    async def test_flood_wait_also_gates_held_rows(self):
        self.j.enqueue(self.e.account, 'held', 'send_text', ARGS['send_text'])
        self.j.hold(self.j.claim(self.e.account), 'outgoing_paused')
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        self.j.flood(self.e.account, 300)
        self.assertIsNone(self.j.claim(self.e.account))

    async def test_owner_discard_cancels_only_never_attempted_held_rows(self):
        for key in ('held', 'attempted', 'unknown', 'queued'):
            self.j.enqueue(self.e.account, key, 'send_text', {**ARGS['send_text'], 'text': key})
        rows = {r['id']: r for r in map(dict, self.j.db.execute('SELECT * FROM jobs'))}
        self.j.hold(rows['held'], 'destination_inactive')
        self.j.set(rows['attempted']['seq'], 'attempted')
        self.j.set(rows['unknown']['seq'], 'unknown', {'code': 'outcome_unknown'})
        self.assertEqual(self.j.discard_held(self.e.account), 1)
        states = {r['id']: r['state'] for r in self.j.db.execute('SELECT id,state FROM jobs')}
        self.assertEqual(states, {'held': 'cancelled', 'attempted': 'attempted', 'unknown': 'unknown', 'queued': 'queued'})

    async def test_unknown_outcome_is_never_turned_into_a_hold(self):
        self.j.enqueue(self.e.account, 'one', 'send_text', ARGS['send_text'])
        self.client.raise_error = TimeoutError()
        await self.e.dispatch_one()
        self.assertEqual(self.j.get(self.e.account, 'one')['state'], 'unknown')
        self.client.raise_error = None
        self.host.state = 'silent'
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        self.assertFalse(await self.e.dispatch_one())
        self.assertEqual(self.j.get(self.e.account, 'one')['state'], 'unknown')
        self.assertEqual(len(self.client.calls), 1)

    async def test_held_status_receipt_is_visible_to_the_mind(self):
        from userbot.participant_plugin import status
        self.host.state = 'silent'
        self.j.enqueue(self.e.account, 'visible', 'send_text', ARGS['send_text'])
        await self.e.dispatch_one()
        receipt = json.loads(status(NS(get_state_dir=lambda: self.root), 'visible'))
        self.assertEqual(receipt['status'], 'held')
        self.assertEqual(receipt['result']['reason'], 'destination_inactive')


class FairHydration(Base):
    def media_message(self, mid, peer):
        doc = T.Document(id=mid, access_hash=9, file_reference=b'f', date=datetime.now(timezone.utc),
                         mime_type='video/mp4', size=10, dc_id=1, attributes=[])
        return text_message(mid, peer, 'big video', media=T.MessageMediaDocument(document=doc))

    async def test_one_chat_slow_download_does_not_hold_other_chats(self):
        release = asyncio.Event(); started = asyncio.Event()
        async def slow_download(message, file):
            started.set()
            await release.wait()
            Path(file).write_bytes(b'video')
        self.client.download_media = slow_download
        self.e.spool(incoming(self.media_message(1, 111), 111))
        self.e.spool(incoming(text_message(2, 111, 'second from A'), 111))
        self.e.spool(incoming(text_message(3, 222, 'from B'), 222))
        self.e.hydrate_pump()
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(20):
            await asyncio.sleep(0)
            self.e.hydrate_pump()
        captured = [json.loads(r['event'])['text'] for r in self.j.db.execute('SELECT event FROM inbox ORDER BY seq')]
        self.assertEqual(captured, ['from B'])  # A's later text still waits behind A's media.
        release.set()
        for _ in range(40):
            await asyncio.sleep(0)
            self.e.hydrate_pump()
        captured = [json.loads(r['event'])['text'] for r in self.j.db.execute('SELECT event FROM inbox ORDER BY seq')]
        self.assertEqual(captured, ['from B', 'big video', 'second from A'])
        await self.e.stop_hydration()

    async def test_pump_bounds_concurrency_and_shutdown_keeps_sources(self):
        gate = asyncio.Event(); active = []
        async def held(message, file):
            active.append(message.id)
            await gate.wait()
        self.client.download_media = held
        for peer in (11, 12, 13, 14, 15):
            self.e.spool(incoming(self.media_message(peer, peer), peer))
        self.e.hydrate_pump()
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(len(active), self.e.hydration_limit)
        await self.e.stop_hydration()
        pending = self.j.db.execute("SELECT count(*) FROM raw_updates WHERE state='pending'").fetchone()[0]
        self.assertEqual(pending, 5)
        self.assertEqual(list((self.root / 'incoming-media').glob('.*.download')), [])
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith('telegram-media-hydration:')])

    async def test_unexpected_worker_failure_surfaces_from_the_pump(self):
        self.e.spool(incoming(text_message(5, 333, 'boom'), 333))
        def fail(*a, **kw): raise ValueError('journal identity conflict')
        self.j.capture = fail
        self.e.hydrate_pump()
        for _ in range(10):
            await asyncio.sleep(0)
        with self.assertRaises(ValueError):
            self.e.hydrate_pump()
        self.assertEqual(len(self.j.raw_heads()), 1)

    async def test_flood_wait_stops_new_hydration_starts(self):
        self.e.spool(incoming(text_message(6, 444), 444))
        self.j.flood(self.e.account, 60)
        self.e.hydrate_pump()
        self.assertEqual(self.e._hydrating, {})

    async def test_supervisor_cancellation_joins_peer_workers(self):
        gate = asyncio.Event()
        async def held(message, file): await gate.wait()
        self.client.download_media = held
        self.e.spool(incoming(self.media_message(7, 555), 555))
        supervisor = asyncio.create_task(self.e.hydrate_forever(tick=0), name='telegram-media-hydration')
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(len(self.e._hydrating), 1)
        supervisor.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await supervisor
        self.assertEqual(self.e._hydrating, {})
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith('telegram-media-hydration')])


class ReceivingScope(Base):
    async def test_policy_written_before_scope_reads_as_all_chats(self):
        _write_json(self.root, 'participant.json', {'mode': 'participant', 'outgoing': True, 'owner_id': '',
                                                    'expected_account_id': '', 'blocked_peers': [5], 'interval': 3})
        row = policy(self.root)
        self.assertEqual((row['scope'], row['selected_peers'], row['blocked_peers']), ('all', [], [5]))
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [7]})
        self.assertEqual(policy(self.root)['selected_peers'], [7])
        with self.assertRaises(ValueError):
            save_policy(self.root, {'scope': 'everything'})
        with self.assertRaises(ValueError):
            save_policy(self.root, {'selected_peers': [0]})
        self.assertEqual(policy(self.root)['scope'], 'selected')

    async def test_selected_scope_receives_only_selected_chats(self):
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [222]})
        self.e.spool(incoming(text_message(1, 111, 'not selected'), 111))
        self.e.spool(incoming(text_message(2, 222, 'selected'), 222))
        self.assertEqual(len(self.j.raw_heads()), 1)
        await self.e.hydrate_once()
        self.assertEqual([json.loads(r['event'])['text'] for r in self.j.heads()], ['selected'])

    async def test_scope_narrowed_after_capture_drops_unhydrated_source(self):
        self.e.spool(incoming(text_message(1, 111, 'was in scope'), 111))
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [222]})
        await self.e.hydrate_once()
        self.assertEqual(self.j.heads(), [])
        self.assertEqual(self.j.raw_heads(), [])

    async def test_outgoing_outside_selection_fails_with_fixed_reason(self):
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [222]})
        self.j.enqueue(self.e.account, 'outside', 'send_text', ARGS['send_text'])
        await self.e.dispatch_one()
        row = self.j.get(self.e.account, 'outside')
        self.assertEqual((row['state'], json.loads(row['result'])['reason']), ('failed', 'peer_not_selected'))
        save_policy(self.root, {'scope': 'all', 'blocked_peers': [GROUP]})
        self.j.enqueue(self.e.account, 'excluded', 'send_text', ARGS['send_text'])
        await self.e.dispatch_one()
        self.assertEqual(json.loads(self.j.get(self.e.account, 'excluded')['result'])['reason'], 'peer_excluded')
        self.assertEqual(self.client.calls, [])

    async def test_saved_messages_stay_reachable_under_selected_scope(self):
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [222]})
        self.assertIsNone(receiving_refusal(policy(self.root), ACCOUNT, ACCOUNT))
        old = self.client.get_input_entity
        async def entity(peer):
            return T.InputPeerSelf() if peer == 'me' else await old(peer)
        self.client.get_input_entity = entity
        save_policy(self.root, {'selected_peers': [GROUP]})
        self.j.enqueue(self.e.account, 'pack', 'pack_create', {**ARGS['pack_create'], 'source_peer': 'me'})
        await self.e.dispatch_one()
        self.assertEqual(self.j.get(self.e.account, 'pack')['state'], 'sent')

    async def test_listings_omit_chats_outside_the_scope(self):
        async def dialogs(**kw):
            return [NS(entity=T.Channel(id=123, title='Excluded group', photo=T.ChatPhotoEmpty(), date=None, megagroup=True, access_hash=1)),
                    NS(entity=T.User(id=222, first_name='Kept', bot=False)),
                    NS(entity=T.User(id=ACCOUNT, first_name='Saved', bot=False, is_self=True))]
        self.client.get_dialogs = dialogs
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [222], 'blocked_peers': []})
        result = await self.e.ops.execute('dialogs', await self.e.ops.prepare('dialogs', {}), 1)
        self.assertEqual([item['id'] for item in result['items']], ['222', str(ACCOUNT)])
        self.assertEqual(result['omitted_outside_scope'], 1)
        save_policy(self.root, {'scope': 'all', 'blocked_peers': [-1000000000123]})
        result = await self.e.ops.execute('dialogs', await self.e.ops.prepare('dialogs', {}), 1)
        self.assertEqual(result['omitted_outside_scope'], 1)

    async def test_unresolvable_invite_refused_under_selected_scope(self):
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [222]})
        old = self.client.__class__.__call__
        async def inspect(client, request):
            if isinstance(request, F.messages.CheckChatInviteRequest):
                client.calls.append(request); return NS(chat=None)
            return await old(client, request)
        with patch.object(Client, '__call__', inspect):
            self.j.enqueue(self.e.account, 'link', 'join', {'peer': 'https://t.me/+invite'})
            await self.e.dispatch_one()
        self.assertEqual(self.j.get(self.e.account, 'link')['state'], 'failed')
        self.assertFalse(any(isinstance(r, F.messages.ImportChatInviteRequest) for r in self.client.calls))


class Capturing(Base):
    async def capture(self, message, peer=809, kind='new'):
        ev = incoming(message, peer)
        ev.get_sender = lambda: asyncio.sleep(0, result=self.sender)
        ev.get_chat = lambda: self.client.get_entity(peer)
        await self.e.capture(ev, kind)

    def setUp(self):
        super().setUp()
        self.sender = T.User(id=809, first_name='Masha', last_name='K', username='masha', bot=False)


class StableIdentity(Capturing):

    async def test_reaction_update_without_edit_is_not_a_revision(self):
        m = text_message(10, 809, 'hello', reactions=T.MessageReactions(results=[]))
        self.e.spool(incoming(m, 809), 'edit')
        self.assertEqual(self.j.raw_heads(), [])

    async def test_counter_changes_keep_one_edit_revision_and_first_capture(self):
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        first = text_message(10, 809, 'fixed text', edit_date=edited, views=1, forwards=0)
        later = text_message(10, 809, 'fixed text', edit_date=edited, views=40, forwards=2,
                             reactions=T.MessageReactions(results=[T.ReactionCount(T.ReactionEmoji('👍'), 3)]))
        self.assertNotEqual(bytes(first), bytes(later))
        self.assertEqual(content_revision(first), content_revision(later))
        await self.capture(first, kind='edit')
        self.sender = T.User(id=809, first_name='Renamed', username='masha2', bot=False)
        await self.capture(later, kind='edit')
        rows = [json.loads(r['event']) for r in self.j.db.execute('SELECT event FROM inbox')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['actor']['display_name'], 'Masha K')
        genuine = text_message(10, 809, 'fixed again', edit_date=edited + timedelta(minutes=5))
        await self.capture(genuine, kind='edit')
        ids = [json.loads(r['event'])['source_event_id'] for r in self.j.db.execute('SELECT event FROM inbox ORDER BY seq')]
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(':edit:' in i for i in ids))

    async def test_entity_values_are_content_and_other_entities_keep_their_revision(self):
        # The original review case: two custom emojis behind one fallback in one edit second.
        edited = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        def edit(*entities, **kw):
            return text_message(10, 809, '🙂 code', edit_date=edited, entities=list(entities), **kw)
        first, second = edit(T.MessageEntityCustomEmoji(0, 2, 101)), edit(T.MessageEntityCustomEmoji(0, 2, 202))
        self.assertNotEqual(bytes(first), bytes(second))
        self.assertNotEqual(content_revision(first), content_revision(second))
        await self.capture(first, kind='edit'); await self.capture(second, kind='edit')
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 2)
        # Each other value a Telethon 1.45 entity carries is content as well.
        for a, b in ((T.MessageEntityPre(3, 4, 'python'), T.MessageEntityPre(3, 4, 'rust')),
                     (T.MessageEntityBlockquote(0, 7), T.MessageEntityBlockquote(0, 7, collapsed=True)),
                     (T.MessageEntityDiffReplace(3, 4, 'old'), T.MessageEntityDiffReplace(3, 4, 'older')),
                     (T.MessageEntityFormattedDate(3, 4, edited), T.MessageEntityFormattedDate(3, 4, edited + timedelta(days=1))),
                     (T.MessageEntityFormattedDate(3, 4, edited), T.MessageEntityFormattedDate(3, 4, edited, relative=True))):
            self.assertNotEqual(content_revision(edit(a)), content_revision(edit(b)), type(a).__name__)
        # Counters and reactions still make no revision; a decoded message agrees with a built one.
        counted = edit(T.MessageEntityCustomEmoji(0, 2, 101), views=9,
                       reactions=T.MessageReactions(results=[T.ReactionCount(T.ReactionEmoji('👍'), 1)]))
        self.assertEqual(content_revision(counted), content_revision(first))
        from telethon.extensions import BinaryReader
        quoted = edit(T.MessageEntityBlockquote(0, 7), T.MessageEntityPre(3, 4, ''))
        with BinaryReader(bytes(quoted)) as reader:
            self.assertEqual(content_revision(reader.tgread_object()), content_revision(quoted))
        # An entity without such a value keeps exactly the revision it had before.
        plain = edit(T.MessageEntityBold(0, 2), T.MessageEntityTextUrl(3, 4, 'https://example.org'),
                     T.MessageEntityMentionName(0, 2, 809), T.MessageEntityBlockquote(0, 7), T.MessageEntityPre(3, 4, ''))
        rows = [['MessageEntityBold', 0, 2, None, None], ['MessageEntityTextUrl', 3, 4, 'https://example.org', None],
                ['MessageEntityMentionName', 0, 2, None, 809], ['MessageEntityBlockquote', 0, 7, None, None],
                ['MessageEntityPre', 3, 4, None, None]]
        self.assertEqual(content_revision(plain), edited.isoformat() + ':' + digest(['🙂 code', rows, None, None])[:32])

    async def test_same_source_with_other_text_is_still_a_conflict(self):
        await self.capture(text_message(11, 809, 'original'))
        with self.assertRaises(ValueError):
            await self.capture(text_message(11, 809, 'different text under the same id'))

    async def test_event_carries_author_date_and_media_without_local_notes_in_text(self):
        sent = datetime(2026, 10, 25, 18, 20, tzinfo=timezone.utc)
        photo = T.Photo(id=77, access_hash=1, file_reference=b'r', date=sent, sizes=[], dc_id=1)
        m = text_message(12, 809, '', date=sent, media=T.MessageMediaPhoto(photo=photo))
        async def unavailable(message, file):
            from telethon.errors import MethodInvalidError
            raise MethodInvalidError(request=None)
        self.client.download_media = unavailable
        await self.capture(m)
        event = json.loads(self.j.heads()[0]['event'])
        self.assertEqual(event['text'], '[Telegram photo]')
        self.assertEqual(event['actor'], {'platform_actor_id': '809', 'display_name': 'Masha K',
                                          'username': 'masha', 'is_bot': False})
        self.assertEqual(event['message']['sent_at'], sent.isoformat())
        self.assertEqual(event['message']['media']['kind'], 'photo')
        self.assertEqual(event['message']['media']['status'], 'unavailable')
        self.assertEqual(event['conversation']['chat_type'], 'supergroup')

    async def test_canonical_descriptors_are_stable(self):
        sticker = T.Document(id=5, access_hash=1, file_reference=b'x', date=None, mime_type='image/webp', size=1, dc_id=1,
                             attributes=[T.DocumentAttributeSticker('😀', T.InputStickerSetEmpty())])
        named = T.Document(id=6, access_hash=1, file_reference=b'x', date=None, mime_type='application/pdf', size=1, dc_id=1,
                           attributes=[T.DocumentAttributeFilename('report.pdf')])
        voice = T.Document(id=7, access_hash=1, file_reference=b'x', date=None, mime_type='audio/ogg', size=1, dc_id=1,
                           attributes=[T.DocumentAttributeAudio(3, voice=True)])
        self.assertEqual(canonical_text(text_message(1, 809, '', media=T.MessageMediaDocument(document=sticker))), '[Telegram sticker 😀]')
        self.assertEqual(canonical_text(text_message(1, 809, '', media=T.MessageMediaDocument(document=named))), '[Telegram document: report.pdf]')
        self.assertEqual(canonical_text(text_message(1, 809, '', media=T.MessageMediaDocument(document=voice))), '[Telegram voice message]')
        service = T.MessageService(id=2, peer_id=T.PeerChannel(123), date=None, action=T.MessageActionChatAddUser([809]))
        self.assertEqual(canonical_text(service), '[Telegram service: MessageActionChatAddUser]')
        self.assertEqual(canonical_text(text_message(1, 809, 'caption', media=T.MessageMediaDocument(document=named))), 'caption')
        preview = T.MessageMediaWebPage(webpage=T.WebPageEmpty(id=1))
        self.assertIsNone(media_descriptor(text_message(1, 809, 'see link', media=preview)))

    async def test_capture_remembers_chat_for_the_scope_editor(self):
        await self.capture(text_message(13, 809, 'hello'))
        chats = self.j.chats(self.e.account)
        self.assertEqual([(c['peer'], c['title'], c['source']) for c in chats], [(809, 'Test', 'incoming')])


class ChatListAndPlace(Base):
    async def test_owner_requested_refresh_reads_dialogs_once(self):
        calls = []
        async def dialogs(**kw):
            calls.append(kw)
            return [NS(id=GROUP, is_user=False, entity=T.Channel(id=123, title='Board games', photo=T.ChatPhotoEmpty(), date=None,
                                                                   megagroup=True, access_hash=1, participants_count=24)),
                    NS(id=809, is_user=True, entity=T.User(id=809, first_name='Mira', bot=False))]
        self.client.get_dialogs = dialogs
        self.assertFalse(await self.e.refresh_chats())
        (self.root / 'chats-refresh.json').write_text('{}')
        self.assertTrue(await self.e.refresh_chats())
        self.assertFalse(await self.e.refresh_chats())
        self.assertEqual(len(calls), 1)
        listed = {c['peer']: (c['title'], c['kind'], c['members']) for c in self.j.chats(self.e.account)}
        self.assertEqual(listed, {GROUP: ('Board games', 'supergroup', 24), 809: ('Mira', 'private', None)})
        status = json.loads((self.root / 'chats-refresh-status.json').read_text())
        self.assertEqual((status['status'], status['count']), ('done', 2))

    async def test_refresh_flood_wait_is_recorded_not_retried(self):
        from telethon.errors import FloodWaitError
        async def dialogs(**kw): raise FloodWaitError(request=None, capture=120)
        self.client.get_dialogs = dialogs
        (self.root / 'chats-refresh.json').write_text('{}')
        await self.e.refresh_chats()
        self.assertGreater(self.j.flood_remaining(self.e.account), 100)
        self.assertEqual(json.loads((self.root / 'chats-refresh-status.json').read_text())['status'], 'flood_wait')
        self.assertFalse((self.root / 'chats-refresh.json').exists())

    async def test_place_state_is_observed_for_display_only(self):
        self.host.state = 'silent'
        await self.e.observe_place(every=0)
        self.assertEqual(json.loads((self.root / 'place-state.json').read_text())['state'], 'silent')
        def broken(*a): raise RuntimeError('place not connected or unreadable')
        self.host.place = broken
        await self.e.observe_place(every=0)
        self.assertEqual(json.loads((self.root / 'place-state.json').read_text())['state'], 'unavailable')

    async def test_cancelled_place_read_writes_no_late_state(self):
        entered, release, answered = threading.Event(), threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(5)
            answered.set()
            return {'state': 'silent', 'label': 'Telegram'}
        self.host.place = slow
        reader = asyncio.create_task(self.e.observe_place(every=0), name='telegram-place-display')
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            reader.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(reader, 1)  # Shutdown does not wait for the Host to answer.
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(answered.wait, 2))
        await asyncio.sleep(0)
        self.assertFalse((self.root / 'place-state.json').exists())


class LiveAndImportIdentity(Capturing):
    """An imported copy of a message must deduplicate against its live capture."""
    def identity(self, event):
        return (event['source_event_id'], event['actor']['platform_actor_id'], event['text'])

    async def test_unedited_message_has_one_identity_live_and_imported(self):
        from userbot.history_import import history_record
        m = text_message(21, 809, 'Has anyone tried this with six players?')
        await self.capture(m)
        live = json.loads(self.j.heads()[0]['event'])
        imported = history_record(ACCOUNT, 809, None, m, self.sender)
        self.assertEqual(self.identity(live), self.identity(imported['event']))
        self.assertEqual(imported['authorship'], 'participant')
        self.assertEqual(imported['event']['message']['sent_at'], live['message']['sent_at'])
        self.assertEqual(imported['source_timestamp'], live['message']['sent_at'])

    async def test_edited_message_imports_as_its_current_revision(self):
        from userbot.history_import import history_record
        edited = datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)
        m = text_message(22, 809, 'corrected', edit_date=edited)
        await self.capture(m, kind='edit')
        live = json.loads(self.j.heads()[0]['event'])
        imported = history_record(ACCOUNT, 809, None, m, self.sender)
        self.assertEqual(self.identity(live), self.identity(imported['event']))
        self.assertIn(':edit:', imported['event']['source_event_id'])
        self.assertEqual(imported['event']['message']['edited_at'], edited.isoformat())
        self.assertEqual(imported['source_timestamp'], m.date.isoformat())  # the original send time

    async def test_own_past_message_is_platform_account_history(self):
        from userbot.history_import import history_record
        own = text_message(23, 809, 'earlier reply from this account', out=True, sender=ACCOUNT)
        imported = history_record(ACCOUNT, 809, None, own, None)
        self.assertEqual(imported['authorship'], 'platform_account')
        self.assertEqual(imported['event']['actor'], {'platform_actor_id': str(ACCOUNT)})

    async def test_media_import_never_claims_a_download(self):
        from userbot.history_import import history_record
        photo = T.Photo(id=78, access_hash=1, file_reference=b'r', date=None, sizes=[], dc_id=1)
        imported = history_record(ACCOUNT, 809, None, text_message(24, 809, '', media=T.MessageMediaPhoto(photo=photo)), None)
        self.assertEqual(imported['event']['text'], '[Telegram photo]')
        self.assertEqual(imported['event']['message']['media']['status'], 'not_downloaded')
        self.assertEqual(imported['staged_files'], [])


class UpgradeFromByteRevisions(Capturing):
    """Rows captured before 0.3 stay authoritative; a re-observation never adds a second event."""
    def legacy_row(self, source_event_id, text, revision=None, actor='809'):
        message = {'message_id': source_event_id.split(':')[4], 'username': 'masha', 'sender_name': 'Masha',
                   'sender_profile_status': 'available', 'mentioned': False, 'reply_to_message_id': None}
        if revision:
            message.update(event_kind='edit', revision=revision)
        self.j.capture({'source_event_id': source_event_id, 'provider': 'telegram', 'account_id': str(ACCOUNT),
                        'conversation_id': '809', 'thread_id': '', 'conversation_key': f'telegram:{ACCOUNT}:809:0',
                        'actor': {'platform_actor_id': actor}, 'conversation': {'title': 'Test'},
                        'message': message, 'text': text, 'facts': ['any_message', 'direct_message']})

    def photo_message(self, mid, **kw):
        photo = T.Photo(id=90 + mid, access_hash=1, file_reference=b'r', date=None, sizes=[], dc_id=1)
        return text_message(mid, 809, '', media=T.MessageMediaPhoto(photo=photo), **kw)

    async def test_pre_upgrade_media_message_is_not_a_conflict_or_a_new_event(self):
        # 0.2 stored an empty text for a staged captionless photo; 0.3 says [Telegram photo].
        self.legacy_row(f'telegram:{ACCOUNT}:809:0:31', '')
        downloads = []
        async def download(message, file): downloads.append(message.id); Path(file).write_bytes(b'x')
        self.client.download_media = download
        await self.capture(self.photo_message(31))
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)
        self.assertEqual(json.loads(self.j.db.execute('SELECT event FROM inbox').fetchone()[0])['text'], '')

    async def test_pre_upgrade_edit_is_found_by_its_byte_revision_stamp(self):
        from urllib.parse import quote
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        old_revision = str(edited) + ':' + 'a' * 64
        self.legacy_row(f'telegram:{ACCOUNT}:809:0:32:edit:' + quote(old_revision, safe=''), 'fixed text', old_revision)
        async def download(*a): raise AssertionError('a captured edit must not stage media again')
        self.client.download_media = download
        await self.capture(text_message(32, 809, 'fixed text', edit_date=edited, views=9, forwards=0), kind='edit')
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)
        # A genuine edit after the upgrade is still a new revision.
        await self.capture(text_message(32, 809, 'fixed again', edit_date=edited + timedelta(minutes=3)), kind='edit')
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 2)

    async def test_legacy_lookup_is_bounded_to_the_same_message_and_actor(self):
        from urllib.parse import quote
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        old_revision = str(edited) + ':' + 'b' * 64
        # Same stamp on another message (33 vs 330) and another actor do not match.
        self.legacy_row(f'telegram:{ACCOUNT}:809:0:330:edit:' + quote(old_revision, safe=''), 'other message', old_revision)
        self.legacy_row(f'telegram:{ACCOUNT}:809:0:33:edit:' + quote(old_revision, safe=''), 'from someone else', old_revision, actor='777')
        self.assertEqual(self.j.legacy_edits(ACCOUNT, 809, None, 33, 809, str(edited)), [])
        [(stored, payload)] = self.j.legacy_edits(ACCOUNT, 809, None, 330, 809, str(edited))
        self.assertEqual((stored['text'], payload), ('other message', None))
        for query, args in (("SELECT event FROM inbox WHERE id >= ? AND id < ?", ('a', 'b')),
                            ("SELECT payload FROM raw_updates WHERE id=?", ('a',))):
            plan = ' '.join(r[-1] for r in self.j.db.execute('EXPLAIN QUERY PLAN ' + query, args))
            self.assertIn('USING INDEX', plan)

    def legacy_edit(self, message, raw=True, payload=None):
        """An edit captured the way 0.2 did: its raw update, then a row under the byte revision."""
        from urllib.parse import quote
        payload = bytes(message) if payload is None else payload
        stamp, sha = str(message.edit_date), hashlib.sha256(payload).hexdigest()
        if raw:
            key = digest([self.e.account, 809, message.id, 'edit', stamp, sha])
            self.j.capture_raw(key, {'account_id': self.e.account, 'peer': 809, 'actor': 809,
                                     'kind': 'edit', 'private': True}, payload)
            self.j.raw_done(key)
        self.legacy_row(f'telegram:{ACCOUNT}:809:0:{message.id}:edit:' + quote(stamp + ':' + sha, safe=''),
                        message.message, stamp + ':' + sha)

    def inbox(self):
        return [(json.loads(r[0])['text'], r[1]) for r in self.j.db.execute('SELECT event, state FROM inbox ORDER BY seq')]

    async def test_same_second_edit_with_other_text_is_a_new_revision(self):
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        self.legacy_edit(text_message(40, 809, 'meet at six', edit_date=edited))
        # Edited again within the same Telegram second, heard by 0.3 after the upgrade.
        self.e.spool(incoming(text_message(40, 809, 'meet at seven', edit_date=edited), 809), 'edit')
        await self.e.hydrate_once()
        self.assertEqual(self.inbox(), [('meet at six', 'pending'), ('meet at seven', 'pending')])
        self.assertEqual(self.j.raw_heads(), [])

    async def test_reaction_on_a_legacy_edit_adds_no_second_wake(self):
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        self.legacy_edit(text_message(41, 809, 'meet at six', edit_date=edited, views=2, forwards=0))
        later = text_message(41, 809, 'meet at six', edit_date=edited, views=30, forwards=1,
                             reactions=T.MessageReactions(results=[T.ReactionCount(T.ReactionEmoji('👍'), 2)]))
        self.e.spool(incoming(later, 809), 'edit')
        self.assertEqual(len(self.j.raw_heads()), 1)  # Other bytes: a new raw update to hydrate.
        await self.e.hydrate_once()
        self.assertEqual(self.inbox(), [('meet at six', 'pending')])
        self.assertEqual(self.j.raw_heads(), [])

    async def test_same_second_entity_change_is_read_from_the_retained_raw_update(self):
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        self.legacy_edit(text_message(42, 809, 'see the rules', edit_date=edited))
        linked = text_message(42, 809, 'see the rules', edit_date=edited,
                              entities=[T.MessageEntityTextUrl(4, 9, 'https://example.org/rules')])
        await self.capture(linked, kind='edit')
        self.assertEqual(self.inbox(), [('see the rules', 'pending'), ('see the rules', 'pending')])

    async def test_without_its_raw_update_a_legacy_edit_is_compared_by_text_only(self):
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        self.legacy_edit(text_message(43, 809, 'see the rules', edit_date=edited), raw=False)
        await self.capture(text_message(43, 809, 'see the rules', edit_date=edited, views=7, forwards=0), kind='edit')
        # Indistinguishable from the stored text alone: counted as captured, not proven.
        await self.capture(text_message(43, 809, 'see the rules', edit_date=edited,
                                        entities=[T.MessageEntityBold(0, 3)]), kind='edit')
        self.assertEqual(len(self.inbox()), 1)
        await self.capture(text_message(43, 809, 'see the new rules', edit_date=edited), kind='edit')
        self.assertEqual([text for text, _ in self.inbox()], ['see the rules', 'see the new rules'])

    async def test_unreadable_legacy_raw_update_falls_back_to_the_stored_text(self):
        edited = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        self.legacy_edit(text_message(44, 809, 'meet at six', edit_date=edited), payload=b'\x01\x02\x03\x04layer')
        await self.capture(text_message(44, 809, 'meet at six', edit_date=edited, views=3, forwards=0), kind='edit')
        self.assertEqual(len(self.inbox()), 1)
        await self.capture(text_message(44, 809, 'meet at seven', edit_date=edited), kind='edit')
        self.assertEqual(len(self.inbox()), 2)

    async def test_legacy_staging_notes_and_placeholders_are_not_new_text(self):
        from userbot.participant_state import legacy_text_matches
        note = '\n[Media not staged: exceeds 32 MiB; available via Telegram message reference.]'
        self.assertTrue(legacy_text_matches('look' + note, 'look', 'MessageMediaDocument'))
        self.assertTrue(legacy_text_matches('look\n[Original Telegram media unavailable: ForbiddenError; newer media '
                                            'was not substituted. Original Telegram reference retained.]', 'look', 'MessageMediaPhoto'))
        self.assertTrue(legacy_text_matches('[Telegram service/media event: MessageMediaPhoto]', '', 'MessageMediaPhoto'))
        self.assertTrue(legacy_text_matches('', '', 'MessageMediaPhoto'))
        self.assertFalse(legacy_text_matches('[Telegram service/media event: MessageMediaPhoto]', '', 'MessageMediaDocument'))
        self.assertFalse(legacy_text_matches('look' + note, 'looks', 'MessageMediaDocument'))
        self.assertFalse(legacy_text_matches('look', 'look again', 'NoneType'))

    async def test_canonical_rows_still_conflict_on_different_text(self):
        await self.capture(text_message(34, 809, 'canonical'))
        # Canonical rows carry sent_at, so the pre-0.3 tolerance does not apply to them.
        with self.assertRaises(ValueError):
            await self.capture(text_message(34, 809, 'changed under one id'))

    async def test_pending_pre_upgrade_reaction_edit_is_retired_without_an_event(self):
        m = text_message(35, 809, 'hello')
        self.j.capture_raw('legacy-reaction', {'account_id': self.e.account, 'peer': 809, 'actor': 809,
                                               'kind': 'edit', 'private': True}, bytes(m))
        await self.e.hydrate_once()
        self.assertEqual(self.j.raw_heads(), [])
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 0)

    async def test_identical_redelivery_is_dropped_by_the_unchanged_raw_key(self):
        m = text_message(36, 809, 'same bytes')
        self.e.spool(incoming(m, 809)); self.e.spool(incoming(m, 809))
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM raw_updates').fetchone()[0], 1)


class ModerationTechnical(Base):
    """Telegram-level correctness of the admin tools. Whether to moderate is the owner's note, tested later."""
    def last(self, cls):
        return [r for r in self.client.calls if isinstance(r, cls)][-1]

    async def run_op(self, key, op, args):
        self.j.enqueue(self.e.account, key, op, args)
        await self.e.dispatch_one()
        return self.j.get(self.e.account, key)

    async def test_ban_with_expiry_keeps_unrelated_restrictions(self):
        self.client.banned_rights = T.ChatBannedRights(None, send_media=True)
        row = await self.run_op('ban', 'ban', {**ARGS['ban'], 'until': 1893456000})
        self.assertEqual(row['state'], 'sent')
        rights = self.last(F.channels.EditBannedRequest).banned_rights
        self.assertTrue(rights.view_messages and rights.send_media)
        # Telethon serializes an integer expiry as the same Unix time a datetime would carry.
        until = rights.until_date if isinstance(rights.until_date, int) else int(rights.until_date.timestamp())
        self.assertEqual(until, 1893456000)

    async def test_mute_until_zero_means_no_expiry(self):
        from datetime import datetime as dt
        self.client.banned_rights = T.ChatBannedRights(dt(2030, 1, 1, tzinfo=timezone.utc), send_polls=True)
        await self.run_op('mute', 'mute', {**ARGS['mute'], 'until': 0})
        rights = self.last(F.channels.EditBannedRequest).banned_rights
        self.assertTrue(rights.send_messages and rights.send_polls)
        self.assertIsNone(rights.until_date)

    async def test_admin_rank_override_and_non_participant_start_from_no_rights(self):
        from telethon.errors import UserNotParticipantError
        self.client.admin_rights = T.ChatAdminRights(pin_messages=True)
        await self.run_op('admin-rank', 'admin', {**ARGS['admin'], 'rank': 'Helper'})
        request = self.last(F.channels.EditAdminRequest)
        self.assertTrue(request.admin_rights.pin_messages and request.admin_rights.delete_messages)
        self.assertEqual(request.rank, 'Helper')
        original = Client.__call__
        async def absent(client, r):
            if isinstance(r, F.channels.GetParticipantRequest):
                bytes(r); client.calls.append(r); raise UserNotParticipantError(request=r)
            return await original(client, r)
        with patch.object(Client, '__call__', absent):
            await self.run_op('admin-new', 'admin', {**ARGS['admin'], 'ban_users': True})
        request = self.last(F.channels.EditAdminRequest)
        self.assertTrue(request.admin_rights.ban_users and request.admin_rights.delete_messages)
        self.assertFalse(request.admin_rights.pin_messages)
        self.assertEqual(request.rank, '')

    async def test_basic_group_restrictions_are_refused_before_any_rpc(self):
        async def basic(peer):
            return T.InputPeerUser(809, 99) if peer in ('user', 809) else T.InputPeerChat(55)
        self.client.get_input_entity = basic
        row = await self.run_op('basic-ban', 'ban', {'peer': '-55', 'user': 'user'})
        self.assertEqual(row['state'], 'failed')
        self.assertFalse(any(isinstance(r, (F.channels.GetParticipantRequest, F.channels.EditBannedRequest)) for r in self.client.calls))

    async def test_title_and_leave_use_the_basic_group_or_channel_request(self):
        async def chat_entity(peer):
            return T.Chat(id=55, title='Basic', photo=T.ChatPhotoEmpty(), participants_count=3, date=None, version=1)
        await self.run_op('title-channel', 'chat_title', ARGS['chat_title'])
        self.assertIsInstance(self.client.calls[-1], F.channels.EditTitleRequest)
        self.client.get_entity = chat_entity
        await self.run_op('title-basic', 'chat_title', {**ARGS['chat_title'], 'title': 'Basic renamed'})
        self.assertIsInstance(self.client.calls[-1], F.messages.EditChatTitleRequest)
        await self.run_op('leave-basic', 'leave', ARGS['leave'])
        self.assertIsInstance(self.client.calls[-1], F.messages.DeleteChatUserRequest)

    async def test_admin_write_is_held_while_the_place_is_silent(self):
        self.host.state = 'silent'
        row = await self.run_op('silent-ban', 'ban', ARGS['ban'])
        self.assertEqual((row['state'], json.loads(row['result'])['reason']), ('held', 'destination_inactive'))
        self.assertFalse(any(isinstance(r, F.channels.EditBannedRequest) for r in self.client.calls))

    async def test_excluded_target_user_is_refused(self):
        save_policy(self.root, {'blocked_peers': [809]})
        row = await self.run_op('excluded-user', 'mute', ARGS['mute'])
        self.assertEqual((row['state'], json.loads(row['result'])['reason']), ('failed', 'user_excluded'))
        self.assertFalse(any(isinstance(r, F.channels.EditBannedRequest) for r in self.client.calls))

    def admitted(self, op):
        return any(isinstance(r, op) for r in self.client.calls)

    async def test_selected_scope_bounds_the_chat_not_the_person_acted_on(self):
        # The selection lists chats; the person muted, invited or promoted inside one is not a chat.
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [GROUP]})
        places = []
        def place(account, peer='', topic=''):
            places.append(str(peer)); return {'place_id': 'place', 'state': 'active'}
        self.host.place = place
        for op in ('mute', 'invite', 'admin'):
            row = await self.run_op('selected-' + op, op, {**ARGS[op], 'peer': str(GROUP)})
            self.assertEqual(row['state'], 'sent', (op, row['result']))
        self.assertTrue(self.admitted(F.channels.EditBannedRequest) and self.admitted(F.channels.InviteToChannelRequest)
                        and self.admitted(F.channels.EditAdminRequest))
        self.assertIn('809', places)  # The Host is still asked about the person, as before.

    async def test_the_persons_place_still_holds_the_action(self):
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [GROUP]})
        self.host.place = lambda account, peer='', topic='': {'state': 'silent' if str(peer) == '809' else 'active'}
        row = await self.run_op('person-silent', 'mute', {**ARGS['mute'], 'peer': str(GROUP)})
        self.assertEqual((row['state'], json.loads(row['result'])['reason']), ('held', 'destination_inactive'))
        self.assertFalse(self.admitted(F.channels.EditBannedRequest))

    async def test_selecting_the_person_does_not_select_the_chat(self):
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [809]})
        row = await self.run_op('chat-unselected', 'mute', {**ARGS['mute'], 'peer': str(GROUP)})
        self.assertEqual((row['state'], json.loads(row['result'])['reason']), ('failed', 'peer_not_selected'))
        self.assertFalse(self.admitted(F.channels.GetParticipantRequest) or self.admitted(F.channels.EditBannedRequest))

    async def test_excluded_person_is_refused_in_a_selected_chat(self):
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [GROUP], 'blocked_peers': [809]})
        row = await self.run_op('person-excluded', 'ban', {**ARGS['ban'], 'peer': str(GROUP)})
        self.assertEqual((row['state'], json.loads(row['result'])['reason']), ('failed', 'user_excluded'))
        self.assertFalse(self.admitted(F.channels.GetParticipantRequest) or self.admitted(F.channels.EditBannedRequest))


class IntentReuse(Base):
    """An intent runs at most once; repeating it reports the existing receipt and queues nothing."""
    def tool(self, op, key, args):
        from userbot.participant_plugin import enqueue
        return json.loads(enqueue(NS(get_state_dir=lambda: self.root), op, key, dict(args)))

    async def settle(self):
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        return await self.e.dispatch_one()

    async def test_repeating_a_sent_intent_queues_nothing_and_says_so(self):
        first = self.tool('send_text', 'once', ARGS['send_text'])
        self.assertEqual(first['status'], 'queued')
        self.assertIn('Queued is not delivery', first['note'])
        await self.e.dispatch_one()
        again = self.tool('send_text', 'once', ARGS['send_text'])
        self.assertEqual(again['status'], 'sent')
        self.assertNotIn('Queued', again['note'])
        self.assertIn('queued nothing', again['note'])
        self.assertFalse(await self.settle())
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    async def test_repeating_a_discarded_intent_stays_cancelled(self):
        self.host.state = 'silent'
        self.tool('send_text', 'discarded', ARGS['send_text'])
        await self.e.dispatch_one()
        held = self.tool('send_text', 'discarded', ARGS['send_text'])
        self.assertEqual(held['status'], 'held')
        self.assertIn('queued nothing new', held['note'])
        self.assertEqual(self.j.discard_held(self.e.account), 1)
        self.host.state = 'active'
        again = self.tool('send_text', 'discarded', ARGS['send_text'])
        self.assertEqual(again['status'], 'cancelled')
        self.assertIn('new intent_id', again['note'])
        self.assertFalse(await self.settle())
        self.assertEqual(self.sends(), [])

    async def test_repeating_an_unknown_intent_asks_for_reconciliation(self):
        self.client.raise_error = TimeoutError()
        self.tool('send_text', 'maybe', ARGS['send_text'])
        await self.e.dispatch_one()
        self.client.raise_error = None
        again = self.tool('send_text', 'maybe', ARGS['send_text'])
        self.assertEqual(again['status'], 'unknown')
        self.assertIn('reconcile', again['note'])
        self.assertFalse(await self.settle())
        self.assertEqual(len(self.sends()), 1)

    async def test_other_arguments_under_a_used_intent_are_refused(self):
        self.tool('send_text', 'fixed', ARGS['send_text'])
        with self.assertRaisesRegex(ValueError, 'intent_id conflict'):
            self.tool('send_text', 'fixed', {**ARGS['send_text'], 'text': 'other'})


class Timeouts(Base):
    """The interpreter's own wait_for timeout; before Python 3.11 it is not the builtin TimeoutError."""

    def short_waits(self):
        import userbot.participant as module
        real = asyncio.wait_for
        bounded = NS(**{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith('_')})
        bounded.wait_for = lambda awaitable, timeout: real(awaitable, 0.05)
        return patch.object(module, 'asyncio', bounded)

    @staticmethod
    async def slow(*_args):
        await asyncio.sleep(1)

    async def test_preparation_timeout_is_retried_not_failed(self):
        self.e.ops.prepare = self.slow
        self.j.enqueue(self.e.account, 'prepare', 'send_text', ARGS['send_text'])
        with self.short_waits():
            await self.e.dispatch_one()
        row = self.j.get(self.e.account, 'prepare')
        self.assertEqual((row['state'], json.loads(row['result'])['code']), ('queued', 'preflight_unavailable'))

    async def test_execution_timeout_retries_a_read_and_keeps_a_write_unknown(self):
        self.e.ops.execute = self.slow
        self.j.enqueue(self.e.account, 'read', 'history', ARGS['history'])
        self.j.enqueue(self.e.account, 'write', 'send_text', ARGS['send_text'])
        with self.short_waits():
            await self.e.dispatch_one(); await self.e.dispatch_one()
        states = {key: (row['state'], json.loads(row['result'])['code'])
                  for key in ('read', 'write') for row in [self.j.get(self.e.account, key)]}
        self.assertEqual(states, {'read': ('queued', 'outcome_unknown'), 'write': ('unknown', 'outcome_unknown')})
        self.j.db.execute("UPDATE jobs SET due=0"); self.j.db.commit()
        self.e.ops.execute = lambda *args: asyncio.sleep(0, result={'messages': []})
        await self.e.dispatch_one(); await self.e.dispatch_one()
        self.assertEqual(self.j.get(self.e.account, 'read')['state'], 'done')
        self.assertEqual(self.j.get(self.e.account, 'write')['state'], 'unknown')  # never attempted again


class ExecutorChange(Base):
    """Accepted incoming under a mode, credential or expected-account change; real denials hold."""

    def setUp(self):
        super().setUp()
        self.calls = []
        def call(method, path, body=None):
            self.calls.append((method, path))
            return 200, {'ok': True, 'status': 'completed', 'outcome': 'silent', 'text': '', 'turn_ref': 'turn'}
        self.host.call = call

    def accept(self, refs=()):
        from userbot.presence import build_event
        event = build_event(ACCOUNT, 4242, 77, 4242, 'hello', message={'sent_at': '2026-10-26T00:00:00+00:00'})
        event['facts'] = ['any_message', 'direct_message']
        self.j.capture(event)
        if refs:
            self.j.inbox_update(self.j.heads()[0]['seq'], 'pending', list(refs))

    def inbox(self):
        row = self.j.db.execute('SELECT state, refs FROM inbox').fetchone()
        return row['state'], json.loads(row['refs'])

    async def test_mode_change_leaves_accepted_incoming_for_the_matching_companion(self):
        self.accept()
        save_policy(self.root, {'mode': 'legacy'})
        await self.e.incoming_once()
        self.assertEqual((self.inbox(), self.calls), (('pending', []), []))
        save_policy(self.root, {'mode': 'participant'})  # a matching companion rechecks everything
        await self.e.incoming_once()
        self.assertEqual((self.inbox(), self.calls), (('done', []), [('POST', '/presence/turn')]))

    async def test_credential_or_expected_account_change_keeps_existing_refs_unpolled(self):
        self.accept(refs=['turn'])
        save_policy(self.root, {'expected_account_id': '1'})
        await self.e.incoming_once()
        save_policy(self.root, {'expected_account_id': str(ACCOUNT)})
        save_config(self.root, Config(123, 'b' * 32, '+1234567890', account_id=ACCOUNT))
        await self.e.incoming_once()
        self.assertEqual((self.inbox(), self.calls), (('pending', ['turn']), []))

    async def test_exclusion_holds_and_reinclusion_does_not_revive(self):
        self.accept()
        save_policy(self.root, {'blocked_peers': [4242]})
        await self.e.incoming_once()
        save_policy(self.root, {'blocked_peers': []})
        await self.e.incoming_once()
        self.assertEqual((self.inbox(), self.calls), (('held', []), []))

    async def test_host_refusal_and_output_conflict_stay_held(self):
        self.accept()
        self.host.call = lambda *a: (409, {'ok': False, 'disposition': 'rejected', 'code': 'presence_origin_mismatch'})
        await self.e.incoming_once()
        self.assertEqual(self.inbox()[0], 'held')
        self.j.db.execute('DELETE FROM inbox'); self.j.db.commit()
        self.accept()
        self.j.enqueue(self.e.account, 'auto:' + digest('out') + ':0', 'send_text', {'peer': '4242', 'text': 'other'})
        self.host.call = lambda *a: (200, {'ok': True, 'status': 'completed', 'outcome': 'message', 'text': 'hello',
                                           'turn_ref': 'turn', 'output_ref': 'out'})
        await self.e.incoming_once()
        self.assertEqual(self.inbox()[0], 'held')


if __name__ == '__main__':
    unittest.main()
