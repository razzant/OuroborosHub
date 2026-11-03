"""Retained legacy intake under continuation version 2, against a loopback Host.

The actual UserSessionBridge, DurableStore and PresenceClient (urllib over 127.0.0.1) talk
to presence_v2_host.FakeHost; Telegram is the fake client of test_participant, whose sends
are real TL requests. One test runs the companion's own legacy loop. No session,
credentials, live network or Telegram effect.
"""
import asyncio
import contextlib
import hashlib
import json
import os
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
from presence_v2_host import FakeHost
from test_participant import Adapter, Client
from telethon import functions as F
from userbot.config import Binding, Config, save_config
from userbot.participant import CurrentAuthorization, Host
from userbot.participant_state import save_policy
from userbot.presence import PresenceClient, PresenceToken
from userbot.runtime import UserSessionBridge, _source_filename
from userbot.store import DurableStore

ACCOUNT, ACTOR = 8667525238, 809
GROUP, MIRA = -1000000000123, 4242  # two enrolled chats: two independent conversations
BINDINGS = {GROUP: 'b' * 32, MIRA: 'c' * 32}
TOKEN = PresenceToken('synthetic')


def sid(peer, mid):
    return 'telegram:%d:%d:0:%d' % (ACCOUNT, peer, mid)


def key(peer):
    return 'telegram:%d:%d:0' % (ACCOUNT, peer)


def incoming(peer, mid, text='synthetic text'):
    return NS(chat_id=peer, sender_id=ACTOR, raw_text=text,
              message=NS(id=mid, out=False, mentioned=False, reply_to=None, action=None))


async def until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


class Base(unittest.IsolatedAsyncioTestCase):
    versions = (1, 2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cfg = Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT, allowed_peer_ids=(GROUP, MIRA),
                          bindings=tuple(Binding(peer, None, BINDINGS[peer]) for peer in sorted(BINDINGS)))
        save_config(self.root, self.cfg)
        save_policy(self.root, {'mode': 'legacy', 'outgoing': True})
        self.fake = FakeHost(self.versions)
        self.client = Client()
        self.adapter = Adapter(self.root / 'unused.session', 123, 'a' * 32, client=self.client)
        async def no_forum(_peer):
            raise ValueError('not a forum')
        self.adapter.topic_ids = no_forum
        self.store = DurableStore(self.root / 'transport.sqlite3', recover_attempted=True)
        self.bridge = self.make_bridge()

    def make_bridge(self):
        presence = self.presence(BINDINGS[GROUP])
        return UserSessionBridge(self.root, self.cfg, self.adapter, self.store, presence,
                                 presence_factory=self.presence,
                                 authorization=CurrentAuthorization(self.root, Host(self.fake.url, TOKEN), ACCOUNT, 'legacy'),
                                 continuation_version=presence.continuation_capability())

    def presence(self, binding):
        return PresenceClient(self.fake.url, TOKEN, binding)

    def tearDown(self):
        self.store.close()
        self.fake.close()
        self.tmp.cleanup()

    async def rounds(self, count=1, limit=10):
        for _ in range(count):
            await self.bridge.process_incoming(limit=limit)

    def raw_state(self, peer, mid):
        row = self.store.db.execute('SELECT status, custody FROM raw_ingress WHERE filename=?',
                                    (_source_filename(ACCOUNT, peer, mid, 'new', ''),)).fetchone()
        return row['status'], row['custody']

    def stored(self, peer, mid):
        path = self.root / 'incoming' / _source_filename(ACCOUNT, peer, mid, 'new', '')
        return json.loads(path.read_text()) if path.exists() else None

    def sends(self):
        return [(r.message, r.reply_to.reply_to_msg_id if r.reply_to else None)
                for r in self.client.calls if isinstance(r, F.messages.SendMessageRequest)]

    def health(self):
        path = self.root / 'inbound-health.json'
        return json.loads(path.read_text())['peers'] if path.exists() else {}


class Negotiation(Base):
    async def test_version_2_only_when_listed_and_the_request_shape_otherwise_unchanged(self):
        self.assertEqual(self.bridge.continuation_version, 2)
        self.bridge.spool_update(incoming(GROUP, 10))
        await self.rounds()
        self.assertEqual(self.fake.bodies()[0]['continuation_version'], 2)
        with self.assertRaises(ValueError):
            UserSessionBridge(self.root, self.cfg, self.adapter, self.store, self.presence(BINDINGS[GROUP]),
                              continuation_version=1)


class Custody(Base):
    async def test_the_early_answer_releases_the_chat_and_the_reply_follows_its_anchor(self):
        for mid in (10, 11):
            self.bridge.spool_update(incoming(GROUP, mid, 'message %d' % mid))
        await self.rounds()
        author = self.fake.author(key(GROUP))
        self.assertEqual(self.fake.posts(), [sid(GROUP, 10), sid(GROUP, 11)])  # one pass: the chat moved on
        self.assertEqual([e['source_event_id'] for e in author.context], [sid(GROUP, 11)])
        self.assertEqual(self.raw_state(GROUP, 10), ('pending', 1))
        self.assertEqual(self.stored(GROUP, 10)['refs'], [author.ref])
        self.assertEqual(self.raw_state(GROUP, 11), ('done', 0))
        self.fake.finish(author.ref, text='Answer to both', output_ref='out-1', anchor=author.context[0])
        await self.rounds()
        self.assertEqual(self.raw_state(GROUP, 10), ('done', 1))
        self.assertTrue(await self.bridge.send_one())
        self.assertEqual(self.sends(), [('Answer to both', 11)])
        self.assertEqual([row['status'] for row in self.store.db.execute('SELECT status FROM inbox_delivery')], ['acked', 'acked'])

    async def test_a_stop_between_the_stored_references_and_the_custody_mark_is_completed_once(self):
        self.bridge.spool_update(incoming(GROUP, 10))
        self.bridge.spool_update(incoming(GROUP, 11))
        mark = self.store.mark_custody
        def stopped(*args):
            self.store.mark_custody = mark
            raise OSError('synthetic stop after the file named its references')
        self.store.mark_custody = stopped
        await self.rounds(limit=1)
        self.assertEqual(self.raw_state(GROUP, 10), ('pending', 0))
        self.bridge._blocked_until.clear()
        await self.rounds()
        self.assertEqual(self.raw_state(GROUP, 10), ('pending', 1))
        self.assertEqual(self.fake.posts(), [sid(GROUP, 10), sid(GROUP, 11)], 'never submitted twice')


class Retry(Base):
    async def test_a_lost_answer_is_offered_again_unchanged_and_custody_is_taken_once(self):
        self.bridge.spool_update(incoming(GROUP, 10))
        self.bridge.spool_update(incoming(GROUP, 11))
        presence = self.bridge.presence
        real, lost = presence._transport, []
        def transport(method, path, payload, headers):
            status, body = real(method, path, payload, headers)
            if path == '/presence/turn' and not lost:
                lost.append(body)
                raise ConnectionResetError('the answer was lost after the Host stored it')
            return status, body
        presence._transport = transport
        await self.rounds()
        self.assertEqual((self.fake.posts(), self.raw_state(GROUP, 10)), ([sid(GROUP, 10)], ('pending', 0)))
        await self.rounds()  # the same event again: the stored envelope, one author
        self.assertEqual(self.stored(GROUP, 10)['refs'], [lost[0]['turn_ref']])
        self.assertEqual(len(self.fake.authors), 1)
        self.assertEqual(self.fake.posts(), [sid(GROUP, 10), sid(GROUP, 10), sid(GROUP, 11)])
        self.assertEqual(self.fake.bodies()[0], self.fake.bodies()[1])


class Busy(Base):
    async def test_a_busy_chat_keeps_its_head_and_identity_while_another_advances(self):
        self.fake.busy.add(key(GROUP))
        self.bridge.spool_update(incoming(GROUP, 10))
        self.bridge.spool_update(incoming(GROUP, 11))
        self.bridge.spool_update(incoming(MIRA, 20))
        await self.rounds()
        self.assertEqual(self.fake.posts(), [sid(GROUP, 10), sid(MIRA, 20)])
        self.assertAlmostEqual(self.bridge._busy[GROUP] - time.monotonic(), 1, delta=0.5)
        self.assertEqual(self.health(), {}, 'busy is not an ingress failure')
        await self.rounds()  # not due: nothing for the group, its second event waits
        self.assertEqual(self.fake.posts(key(GROUP)), [sid(GROUP, 10)])
        for _ in range(3):
            self.bridge._busy[GROUP] = 0.0  # its wait has passed
            await self.rounds()
            self.assertAlmostEqual(self.bridge._busy[GROUP] - time.monotonic(), 1, delta=0.5)  # the hint, no growth
        self.fake.busy.discard(key(GROUP))
        self.bridge._busy[GROUP] = 0.0
        await self.rounds()
        self.assertEqual(self.fake.posts(key(GROUP)), [sid(GROUP, 10)] * 5 + [sid(GROUP, 11)])
        bodies = {json.dumps(body, sort_keys=True) for body in self.fake.bodies()
                  if body['event']['source_event_id'] == sid(GROUP, 10)}
        self.assertEqual(len(bodies), 1, 'the same event, byte for byte, every time')
        self.assertNotIn(GROUP, self.bridge._busy)
        self.assertEqual(self.raw_state(GROUP, 11), ('done', 0))

    async def test_full_capacity_waits_while_the_live_author_takes_its_chat(self):
        self.fake.capacity = 1
        self.bridge.spool_update(incoming(GROUP, 10))
        await self.rounds()
        self.bridge.spool_update(incoming(MIRA, 20))
        self.bridge.spool_update(incoming(GROUP, 11))
        await self.rounds()
        self.assertAlmostEqual(self.bridge._busy[MIRA] - time.monotonic(), 5, delta=0.5)
        self.assertEqual(self.raw_state(GROUP, 11), ('done', 0))  # context for the running author
        self.fake.finish(self.fake.author(key(GROUP)).ref)
        self.bridge._busy[MIRA] = 0.0
        await self.rounds()
        self.assertEqual(self.raw_state(MIRA, 20), ('pending', 1))


class Flood(Base):
    async def test_a_flood_reaches_the_live_author_in_order_without_drops(self):
        self.bridge.spool_update(incoming(GROUP, 10))
        await self.rounds()
        for mid in range(11, 31):
            self.bridge.spool_update(incoming(GROUP, mid, 'flood %d' % mid))
        self.bridge.spool_update(incoming(MIRA, 50))
        await self.rounds(limit=100)
        self.assertEqual(self.fake.posts(key(GROUP)), [sid(GROUP, mid) for mid in range(10, 31)])
        self.assertEqual([e['source_event_id'] for e in self.fake.author(key(GROUP)).context],
                         [sid(GROUP, mid) for mid in range(11, 31)])
        self.assertEqual(self.fake.posts(key(MIRA)), [sid(MIRA, 50)])
        self.assertEqual(self.store.pending_raw_count(str(ACCOUNT)), 2)  # the two authors' own events


class Observation(Base):
    async def test_a_late_child_and_its_parent_are_read_apart_and_each_replies_once(self):
        self.bridge.spool_update(incoming(GROUP, 10, 'question'))
        await self.rounds()
        author = self.fake.author(key(GROUP))
        author.child = 'child-1'  # promoted at a park; the pending view names it
        child = {'ok': True, 'work_ref': 'child-1', 'task_id': 'child-1', 'child_work_ref': '', 'outputs': [],
                 'delivery_reporting_version': 1}
        self.fake.work['child-1'] = [(202, {**child, 'status': 'pending'}),
                                     (200, {**child, 'status': 'completed', 'outcome': 'message',
                                            'text': 'From the child', 'output_ref': 'out-child'})]
        await self.rounds()
        self.assertEqual(self.stored(GROUP, 10)['refs'], [author.ref, 'child-1'])
        await self.rounds()  # both still run
        self.assertEqual(self.stored(GROUP, 10)['refs'], [author.ref, 'child-1'])
        await self.rounds()  # the child ends first; its parent still runs
        self.assertEqual(self.stored(GROUP, 10)['refs'], [author.ref])
        self.fake.finish(author.ref, text='From the author', output_ref='out-author', anchor=author.event)
        await self.rounds(2)
        self.assertEqual(self.raw_state(GROUP, 10), ('done', 1))
        self.assertEqual(self.fake.polls('child-1'), ['child-1', 'child-1'], 'an ended child is not read again')
        while await self.bridge.send_one():
            pass
        self.assertEqual(sorted(self.sends()), [('From the author', 10), ('From the child', 10)])

    async def test_a_released_output_keeps_the_hosts_reporting_version_and_is_sent_and_reported_once(self):
        self.bridge.spool_update(incoming(GROUP, 10, 'question'))
        self.bridge.spool_update(incoming(GROUP, 11, 'detail'))
        await self.rounds()
        author = self.fake.author(key(GROUP))
        detail = author.context[0]
        # A live author releases an identity-bearing output anchored on the detail, and promotes a child.
        author.outputs = [{'output_ref': 'released-1', 'outcome': 'deferred', 'text': 'Working on it.',
                           'reply_anchor': {'source_event_id': detail['source_event_id'], 'message': detail['message']}}]
        author.child = 'child-1'
        child = {'ok': True, 'work_ref': 'child-1', 'task_id': 'child-1', 'child_work_ref': '', 'outputs': [],
                 'delivery_reporting_version': 1}
        self.fake.work['child-1'] = [(202, {**child, 'status': 'pending'}),
                                     (200, {**child, 'status': 'completed', 'outcome': 'message',
                                            'text': 'The child finished.', 'output_ref': 'out-child'})]
        await self.rounds(3)  # the released output is read on every poll of the live author
        self.fake.finish(author.ref, text='Final answer.', output_ref='out-final', anchor=detail)
        await self.rounds(2)
        self.assertEqual(self.raw_state(GROUP, 10), ('done', 1))
        payloads = {payload['output_ref']: payload for _, payload in self.outbox()}
        self.assertEqual(set(payloads), {'released-1', 'out-child', 'out-final'})
        self.assertEqual({ref: p['delivery_reporting_version'] for ref, p in payloads.items()},
                         {'released-1': 1, 'out-child': 1, 'out-final': 1})
        while await self.bridge.send_one():
            pass
        self.assertEqual(sorted(self.sends()), [('Final answer.', 11), ('The child finished.', 10), ('Working on it.', 11)])
        self.assertEqual(await self.bridge.report_sent(limit=5), 3)
        source = sid(GROUP, 10)
        prefix = 'auto_' + hashlib.sha256(source.encode()).hexdigest()
        reports = [body for body in self.fake.bodies('/presence/delivery')]
        self.assertEqual(sorted(body['delivery_id'] for body in reports),
                         sorted(prefix + '_' + hashlib.sha256(ref.encode()).hexdigest()[:32]
                                for ref in ('released-1', 'out-child', 'out-final')))
        self.assertEqual(await self.bridge.report_sent(limit=5), 0)  # each receipt once; reporting never re-sends

    def outbox(self):
        rows = self.store.db.execute('SELECT intent_id, payload_ref FROM outgoing ORDER BY rowid').fetchall()
        return [(row['intent_id'], json.loads((self.root / 'outgoing' / (row['payload_ref'] + '.json')).read_text()))
                for row in rows]

    async def test_an_anchor_in_another_chat_falls_back_to_the_event(self):
        self.bridge.spool_update(incoming(GROUP, 10))
        await self.rounds()
        author = self.fake.author(key(GROUP))
        elsewhere = {'source_event_id': sid(MIRA, 99), 'message': {'message_id': '99'}}
        self.fake.finish(author.ref, text='Stays here', output_ref='out-1', anchor=elsewhere)
        await self.rounds()
        self.assertTrue(await self.bridge.send_one())
        self.assertEqual(self.sends(), [('Stays here', 10)])

    async def test_a_stop_sends_nothing_and_never_submits_the_event_again(self):
        self.bridge.spool_update(incoming(GROUP, 10))
        await self.rounds()
        self.fake.finish(self.fake.author(key(GROUP)).ref, status='cancelled')
        await self.rounds(3)
        self.assertEqual(self.raw_state(GROUP, 10), ('held', 1))
        self.assertTrue(any(p.suffix == '.held' for p in (self.root / 'incoming').iterdir()))
        self.assertEqual(self.fake.posts(), [sid(GROUP, 10)])
        self.assertFalse(await self.bridge.send_one())


class Fairness(Base):
    """A bounded observation batch rotates through custody instead of rereading its oldest events."""

    async def test_a_later_completed_child_is_read_while_older_children_stay_pending(self):
        pending = {'ok': True, 'task_id': '', 'child_work_ref': '', 'outputs': [], 'status': 'pending',
                   'delivery_reporting_version': 1}
        for k in range(4):  # four serial group parents, each ended deferring to a child that keeps running
            self.bridge.spool_update(incoming(GROUP, 10 + k, 'parent %d' % k))
            await self.rounds(limit=10)
            ref = self.fake.author(key(GROUP)).ref
            self.fake.work['child-%d' % k] = [(202, {**pending, 'work_ref': 'child-%d' % k})]
            self.fake.finish(ref, outcome='deferred', child='child-%d' % k)
            await self.rounds(limit=10)
            self.assertEqual(self.stored(GROUP, 10 + k)['refs'], ['child-%d' % k])
        self.bridge.spool_update(incoming(MIRA, 20, 'later question'))
        await self.rounds(limit=10)
        mira = self.fake.author(key(MIRA))
        self.fake.work['mira-child'] = [(200, {'ok': True, 'work_ref': 'mira-child', 'task_id': 'mira-child',
                                               'child_work_ref': '', 'outputs': [], 'status': 'completed',
                                               'outcome': 'message', 'text': 'Later answer',
                                               'output_ref': 'out-mira', 'delivery_reporting_version': 1})]
        self.fake.finish(mira.ref, outcome='deferred', child='mira-child')
        self.assertEqual(self.store.pending_raw_count(str(ACCOUNT)), 5)
        reads = []
        for _ in range(6):  # five events in custody, at most three read per pass
            before = len(self.fake.polls())
            await self.bridge.observe_custody(limit=3)
            reads.append(self.fake.polls()[before:])
        self.assertTrue(all(len(batch) <= 3 for batch in reads), reads)
        self.assertEqual(self.fake.polls('mira-child'), ['mira-child'], 'the later child was read once, to its end')
        self.assertEqual(self.raw_state(MIRA, 20), ('done', 1))
        for k in range(4):  # every older child is still read, at least once in each two passes
            for first, second in zip(reads, reads[1:]):
                self.assertTrue(any(read.startswith('child-%d' % k) for read in first + second), (k, reads))
            self.assertEqual(self.raw_state(GROUP, 10 + k), ('pending', 1))
        self.assertTrue(await self.bridge.send_one())
        self.assertEqual(self.sends(), [('Later answer', 20)])

    async def test_the_store_batch_rotates_and_keeps_its_bound(self):
        for mid in range(1, 151):
            self.bridge.spool_update(incoming(GROUP if mid % 2 else MIRA, mid))
        account = str(ACCOUNT)
        self.assertEqual(self.store.custody_high(account), 0, 'nothing in custody yet')
        seqs = [row['seq'] for row in self.store.db.execute('SELECT seq FROM raw_ingress ORDER BY seq')]
        for row in self.store.db.execute('SELECT filename FROM raw_ingress').fetchall():
            self.store.mark_custody(account, row['filename'])
        self.assertEqual(self.store.custody_high(account), seqs[-1])
        def read(**bounds):
            return [seq for seq, _f, _o in self.store.custody_raw(account, **bounds)]
        self.assertEqual(read(), seqs[:100], 'the default call is unchanged: the oldest 100')
        self.assertEqual(read(after_seq=seqs[99]), seqs[100:])
        self.assertEqual(read(after_seq=seqs[9], through_seq=seqs[19]), seqs[10:20])
        self.assertEqual(read(limit=7, through_seq=seqs[2]), seqs[:3])
        self.assertEqual(read(through_seq=0), [])
        self.store.settle_raw(account, self.store.custody_raw(account, limit=1)[0][1])
        self.assertEqual(len(read(limit=10000)), 149, 'each pending event once, nothing settled')
        for bounds in ({'after_seq': -1}, {'after_seq': True}, {'through_seq': '3'}, {'through_seq': -1}):
            with self.assertRaises(ValueError):
                self.store.custody_raw(account, **bounds)

    async def test_each_pass_cycles_through_custody_within_its_bound(self):
        """The runtime cycle itself: every event once per cycle, never twice in a pass."""
        for mid in range(1, 8):
            self.bridge.spool_update(incoming(GROUP if mid % 2 else MIRA, mid))
        account = str(ACCOUNT)
        rows = self.store.db.execute('SELECT seq, filename FROM raw_ingress ORDER BY seq').fetchall()
        for row in rows:
            self.store.mark_custody(account, row['filename'])
        seqs, seen = [row['seq'] for row in rows], []
        async def observed(filename, _original):
            seen[-1].append(next(row['seq'] for row in rows if row['filename'] == filename))
            return 0
        self.bridge._observe_custody = observed
        for _ in range(5):
            seen.append([])
            await self.bridge.observe_custody(limit=3)
        self.assertEqual(seen, [seqs[0:3], seqs[3:6], [seqs[6], seqs[0], seqs[1]], seqs[2:5], [seqs[5], seqs[6], seqs[0]]])
        seen.append([])
        self.store.settle_raw(account, rows[0]['filename'])
        await self.bridge.observe_custody(limit=10)  # larger than custody: each event once
        self.assertEqual(seen[-1], seqs[1:])


class ArrivalFairness(Base):
    """Old events stay pending while new custody keeps arriving: the batch still cycles through both."""
    ARRIVING = (-1000000000201, -1000000000202, -1000000000203)  # three more enrolled chats

    def setUp(self):
        super().setUp()
        bindings = {**BINDINGS, **{peer: c * 32 for peer, c in zip(self.ARRIVING, 'ef9')}}
        self.cfg = Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT, allowed_peer_ids=tuple(sorted(bindings)),
                          bindings=tuple(Binding(peer, None, bindings[peer]) for peer in sorted(bindings)))
        save_config(self.root, self.cfg)
        self.bridge = self.make_bridge()

    async def old_parents(self, count):
        """``count`` serial group events in custody, each ended deferring to a child that keeps running."""
        refs = {}
        for k in range(count):
            self.bridge.spool_update(incoming(GROUP, 1000 + k, 'parent %d' % k))
            await self.bridge.process_heads(limit=10)
            ref = self.fake.author(key(GROUP)).ref
            self.fake.work['child-%d' % k] = [(202, {'ok': True, 'task_id': '', 'child_work_ref': '', 'outputs': [],
                                                     'status': 'pending', 'work_ref': 'child-%d' % k,
                                                     'delivery_reporting_version': 1})]
            self.fake.finish(ref, outcome='deferred', child='child-%d' % k)
            refs[ref], refs['child-%d' % k] = k, k  # either read is attention to old event k
        self.assertEqual(self.store.pending_raw_count(str(ACCOUNT)), count)
        return refs

    def read_old(self, batch, old):
        return {old[ref.split('?')[0]] for ref in batch if ref.split('?')[0] in old}

    async def test_steady_arrivals_never_starve_older_pending_events(self):
        old = await self.old_parents(4)
        reads, arrived = [], []
        for n in range(15):
            if n:  # before each later pass, one completing event in each of three more chats
                for peer in self.ARRIVING:
                    self.bridge.spool_update(incoming(peer, 100 + n))
                    arrived.append((peer, 100 + n))
                await self.bridge.process_heads(limit=10)
                for peer in self.ARRIVING:
                    author = self.fake.author(key(peer))
                    self.fake.finish(author.ref, text='answer %d' % n, output_ref='out-%d-%d' % (peer, n),
                                     anchor=author.event)
            before = len(self.fake.polls())
            await self.bridge.observe_custody(limit=3)
            reads.append(self.fake.polls()[before:])
        self.assertTrue(all(len(batch) <= 3 for batch in reads), reads)
        attention = [self.read_old(batch, old) for batch in reads]
        for k in range(4):
            self.assertTrue(any(k in seen for seen in attention[-5:]),
                            'old event %d is still read in the last five passes: %r' % (k, attention))
            self.assertEqual(self.raw_state(GROUP, 1000 + k), ('pending', 1))
        settled = [pair for pair in arrived[:21] if self.raw_state(*pair)[0] == 'done']
        self.assertEqual(settled, arrived[:21], 'the first seven passes\' arrivals all reached their end')
        self.assertLess(self.store.pending_raw_count(str(ACCOUNT)), 40)

    async def test_a_later_child_is_read_behind_a_hundred_older_pending_children_at_the_default_batch(self):
        old = await self.old_parents(100)
        self.bridge.spool_update(incoming(MIRA, 20, 'later question'))
        await self.bridge.process_heads(limit=10)
        mira = self.fake.author(key(MIRA))
        self.fake.work['mira-child'] = [(200, {'ok': True, 'work_ref': 'mira-child', 'task_id': 'mira-child',
                                               'child_work_ref': '', 'outputs': [], 'status': 'completed',
                                               'outcome': 'message', 'text': 'Later answer',
                                               'output_ref': 'out-mira', 'delivery_reporting_version': 1})]
        self.fake.finish(mira.ref, outcome='deferred', child='mira-child')
        reads = []
        for _ in range(4):
            before = len(self.fake.polls())
            await self.bridge.observe_custody()  # the companion's own batch: 100
            reads.append(self.fake.polls()[before:])
        self.assertTrue(all(len(batch) <= 100 for batch in reads), [len(batch) for batch in reads])
        self.assertEqual(self.fake.polls('mira-child'), ['mira-child'])
        self.assertEqual(self.raw_state(MIRA, 20), ('done', 1))
        attention = [self.read_old(batch, old) for batch in reads]
        for first, second in zip(attention, attention[1:]):
            self.assertEqual(first | second, set(range(100)), 'every older event is read in each two passes')
        self.assertTrue(await self.bridge.send_one())
        self.assertEqual(self.sends(), [('Later answer', 20)])


class Upgrade(Base):
    """Work a version-0 companion admitted, then the companion restarted against a version-2 Host."""
    versions = None

    async def admit_under_version_0(self, *, stop_before_queueing=False):
        """GROUP 10 is answered with a reply and a child, as version 0 stores it: work_ref, no refs."""
        self.assertEqual(self.bridge.continuation_version, 0)
        self.bridge.spool_update(incoming(GROUP, 10, 'question'))
        queue = self.bridge._queue_pending
        def stopped(raw, *args):
            if raw.get('pending_replies'):
                raise OSError('synthetic stop after the file stored the child and its replies')
            return queue(raw, *args)
        if stop_before_queueing:
            self.bridge._queue_pending = stopped
        task = asyncio.create_task(self.bridge.process_incoming(limit=1))
        await asyncio.to_thread(self.fake.wait, lambda: self.fake.author(key(GROUP)) is not None)
        self.fake.finish(self.fake.author(key(GROUP)).ref, text='Parent says', output_ref='out-parent',
                         child='child-1')
        await task
        child = {'ok': True, 'work_ref': 'child-1', 'task_id': 'child-1', 'child_work_ref': '', 'outputs': [],
                 'delivery_reporting_version': 1}
        self.fake.work['child-1'] = [(202, {**child, 'status': 'pending'})]
        self.done_child = (200, {**child, 'status': 'completed', 'outcome': 'message',
                                 'text': 'From the child', 'output_ref': 'out-child'})
        stored = self.stored(GROUP, 10)
        self.assertEqual((stored['work_ref'], stored['selected_binding'], 'refs' in stored),
                         ('child-1', BINDINGS[GROUP], False))
        self.assertEqual(self.raw_state(GROUP, 10), ('pending', 0))
        self.assertEqual(bool(stored.get('pending_replies')), stop_before_queueing)
        return stored

    def restart(self, versions):
        self.store.close()
        self.fake.versions = None if versions is None else list(versions)
        self.store = DurableStore(self.root / 'transport.sqlite3', recover_attempted=True)
        self.bridge = self.make_bridge()

    def inbox(self, event_id):
        return dict(self.store.db.execute('SELECT * FROM inbox_delivery WHERE event_id=?', (event_id,)).fetchone())

    def outputs(self):
        return [json.loads((self.root / 'outgoing' / (row['payload_ref'] + '.json')).read_text())['output_ref']
                for row in self.store.db.execute('SELECT payload_ref FROM outgoing ORDER BY rowid')]

    async def test_admitted_version_0_work_moves_to_custody_once_and_releases_its_chat(self):
        before = await self.admit_under_version_0(stop_before_queueing=True)
        inbox = self.inbox(sid(GROUP, 10))
        self.restart((1, 2))
        self.assertEqual(self.bridge.continuation_version, 2)
        marks = []
        mark = self.store.mark_custody
        self.store.mark_custody = lambda *args: (marks.append(args), mark(*args))
        self.bridge.spool_update(incoming(GROUP, 11, 'follow-up'))
        await self.rounds(3)
        self.assertEqual(self.fake.posts(key(GROUP)), [sid(GROUP, 10), sid(GROUP, 11)],
                         'the follow-up is submitted; the admitted event is never submitted again')
        self.assertEqual(self.raw_state(GROUP, 10), ('pending', 1))
        after = self.stored(GROUP, 10)
        self.assertEqual(after['refs'], ['child-1'])
        self.assertEqual({k: v for k, v in after.items() if k != 'refs'},
                         {k: v for k, v in before.items() if k != 'pending_replies'},
                         'only the references are added; the stored reply was queued')
        self.assertEqual(self.inbox(sid(GROUP, 10)), inbox, 'the reserved Host event keeps its exact bytes')
        self.assertEqual([args[1] for args in marks].count(_source_filename(ACCOUNT, GROUP, 10, 'new', '')), 1,
                         'migrated once')
        self.assertEqual(self.outputs(), ['out-parent'])
        self.assertGreaterEqual(len(self.fake.polls('child-1')), 1, 'observed apart, in custody')
        self.fake.work['child-1'] = [self.done_child]
        await self.rounds(2)
        self.assertEqual(self.raw_state(GROUP, 10), ('done', 1))
        self.assertEqual(self.outputs(), ['out-parent', 'out-child'])
        self.assertEqual(self.fake.posts(key(GROUP)), [sid(GROUP, 10), sid(GROUP, 11)])
        while await self.bridge.send_one():
            pass
        self.assertEqual(self.sends(), [('Parent says', 10), ('From the child', 10)])

    async def test_an_older_host_still_observes_admitted_work_at_its_chats_head(self):
        await self.admit_under_version_0()
        self.restart(None)
        self.assertEqual(self.bridge.continuation_version, 0)
        self.bridge.spool_update(incoming(GROUP, 11, 'follow-up'))
        await self.rounds(3)
        self.assertEqual(self.fake.posts(key(GROUP)), [sid(GROUP, 10)], 'version 0 keeps the follow-up behind it')
        self.assertEqual(len(self.fake.polls('child-1')), 3)
        self.assertEqual(self.raw_state(GROUP, 10), ('pending', 0))
        self.assertNotIn('refs', self.stored(GROUP, 10))
        self.fake.work['child-1'] = [self.done_child]
        await self.rounds(limit=1)  # one head: the follow-up's version-0 turn would wait for its terminal
        self.assertEqual(self.raw_state(GROUP, 10), ('done', 0))
        self.assertEqual(self.outputs(), ['out-parent', 'out-child'])

    async def test_a_binding_changed_across_the_upgrade_holds_the_event_unmigrated(self):
        await self.admit_under_version_0(stop_before_queueing=True)
        self.cfg = Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT, allowed_peer_ids=(GROUP, MIRA),
                          bindings=(Binding(GROUP, None, 'd' * 32), Binding(MIRA, None, BINDINGS[MIRA])))
        save_config(self.root, self.cfg)
        self.restart((1, 2))
        await self.rounds(2)
        self.assertEqual(self.raw_state(GROUP, 10), ('held', 0))
        held = self.root / 'incoming' / _source_filename(ACCOUNT, GROUP, 10, 'new', '')
        self.assertNotIn('refs', json.loads(held.with_suffix('.held').read_text()))
        self.assertEqual(self.fake.polls('child-1'), [], 'a prior binding\'s work is not read')
        self.assertEqual(self.outputs(), [], 'nor its stored reply queued under the new binding')
        self.assertEqual(self.fake.posts(), [sid(GROUP, 10)])


class OlderHost(Base):
    versions = None

    async def test_an_older_host_gets_the_version_0_request_and_its_terminal_wait(self):
        self.assertEqual(self.bridge.continuation_version, 0)
        self.bridge.spool_update(incoming(GROUP, 10))
        self.bridge.spool_update(incoming(GROUP, 11))
        task = asyncio.create_task(self.bridge.process_incoming(limit=1))
        await asyncio.to_thread(self.fake.wait, lambda: self.fake.author(key(GROUP)) is not None)
        await asyncio.sleep(0.1)
        self.assertFalse(task.done(), 'version 0 still waits for the terminal')
        self.fake.finish(self.fake.author(key(GROUP)).ref, text='Done', output_ref='out-1')
        await task
        self.assertEqual(set(self.fake.bodies()[0]), {'binding_id', 'event', 'delivery_reporting_version'})
        self.assertEqual(self.raw_state(GROUP, 10), ('done', 0))


class Companion(Base):
    """The companion's own legacy loop (scripts/companion.py) over the loopback Host."""

    @contextlib.asynccontextmanager
    async def companion(self):
        """Run _serve_legacy as the Host supervisor would; yields the capture callback and the client."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('companion_v2_test', ROOT / 'scripts/companion.py')
        companion = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(companion)

        class Live(Client):
            def __init__(self):
                super().__init__()
                self.handlers = []
            async def connect(self): pass
            async def disconnect(self): pass
            def add_event_handler(self, handler, _builder): self.handlers.append(handler)
            async def catch_up(self): pass
        live = Live()
        adapter = Adapter(self.root / 'unused.session', 123, 'a' * 32, client=live)
        adapter.topic_ids = self.adapter.topic_ids
        env = {'HOST_SERVICE_URL': self.fake.url, 'HOST_SERVICE_TOKEN': 'synthetic'}
        with patch.dict(os.environ, env), patch.object(companion, 'TelegramAdapter', lambda *a, **kw: adapter):
            serving = asyncio.create_task(companion._serve_legacy(self.root))
            try:
                self.assertTrue(await until(lambda: bool(live.handlers) or serving.done()), 'companion did not start')
                if serving.done():
                    serving.result()
                async def capture(peer, mid, text='synthetic text'):
                    await live.handlers[0](incoming(peer, mid, text))
                    return sid(peer, mid)
                yield capture, live
            finally:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith('telegram-legacy-') and not t.done()],
                         'every intake and observation worker joined at the stop')

    def context(self, peer):
        author = self.fake.author(key(peer))
        return [e['source_event_id'] for e in author.context] if author else []

    async def test_outbound_replies_progress_while_a_host_submission_is_held(self):
        # The test's own store handle stays open beside the companion's, as a plugin tool's would.
        self.fake.hold[sid(GROUP, 10)] = gate = threading.Event()
        async with self.companion() as (capture, live):
            await capture(GROUP, 10, 'held at the Host')
            await asyncio.to_thread(self.fake.wait, lambda: sid(GROUP, 10) in self.fake.posts())
            # Only now, with that submission held at the Host, a reply becomes due.
            self.bridge._queue_reply({'peer_id': MIRA, 'message_id': 77}, None, BINDINGS[MIRA], 'Earlier reply',
                                     1, sid(MIRA, 77), 'turn-0', output_ref='out-earlier')
            self.assertTrue(await until(lambda: any(isinstance(r, F.messages.SendMessageRequest) for r in live.calls), 15),
                            'outbound waited behind the held Host submission')
            self.assertFalse(gate.is_set())
            self.assertEqual([r.message for r in live.calls if isinstance(r, F.messages.SendMessageRequest)],
                             ['Earlier reply'])
            gate.set()
            await asyncio.to_thread(self.fake.wait, lambda: self.fake.author(key(GROUP)) is not None)
        capability = json.loads((self.root / 'presence-capability.json').read_text())
        self.assertEqual(capability['continuation_version'], 2)

    async def test_a_slow_custody_read_never_holds_the_chats_next_event(self):
        self.fake.hold_work['turn-1'] = gate = threading.Event()  # the first author's work view answers slowly
        async with self.companion() as (capture, _live):
            await capture(GROUP, 10, 'first')
            await asyncio.to_thread(self.fake.wait, lambda: self.fake.polls('turn-1'))  # its custody read is in flight
            await capture(GROUP, 11, 'second, while that read is held')
            reached = await until(lambda: sid(GROUP, 11) in self.context(GROUP), 15)
            held = not gate.is_set()
            gate.set()
        self.assertTrue(reached and held, "the chat's next event waited behind a slow custody read")
        self.assertEqual(self.fake.posts(key(GROUP)), [sid(GROUP, 10), sid(GROUP, 11)])

    async def test_a_held_start_of_one_chat_never_holds_another_chats_live_author(self):
        async with self.companion() as (capture, _live):
            await capture(MIRA, 20, 'Mira starts')
            await asyncio.to_thread(self.fake.wait, lambda: self.fake.author(key(MIRA)) is not None
                                    and self.fake.answers.get(sid(MIRA, 20)) is not None)
            self.fake.hold[sid(GROUP, 10)] = gate = threading.Event()  # the group's start waits at the Host
            await capture(GROUP, 10, 'the group starts')
            await asyncio.to_thread(self.fake.wait, lambda: sid(GROUP, 10) in self.fake.posts())
            await capture(MIRA, 21, 'Mira again')
            reached = await until(lambda: sid(MIRA, 21) in self.context(MIRA), 15)
            held = not gate.is_set()
            gate.set()
            await asyncio.to_thread(self.fake.wait, lambda: self.fake.author(key(GROUP)) is not None)
        self.assertTrue(reached and held, "another chat's follow-up waited behind the held start")
        self.assertEqual(self.fake.posts(key(MIRA)), [sid(MIRA, 20), sid(MIRA, 21)])


if __name__ == '__main__':
    unittest.main()
