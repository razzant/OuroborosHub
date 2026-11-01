"""Participant intake under continuation version 2, against a loopback Host.

The actual Participant engine, Journal and Host transport (urllib over 127.0.0.1) talk to
presence_v2_host.FakeHost; Telegram is the fake client of test_participant, whose sends are
real TL requests. No session, credentials, live network or Telegram effect.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from presence_v2_host import FakeHost
from test_participant import Adapter, Client
from telethon import functions as F
from userbot.config import Config, save_config
from userbot.participant import Host, Participant, continuation_version
from userbot.participant_state import Journal, digest, save_policy
from userbot.presence import PresenceToken, build_event

ACCOUNT = 8667525238
ANN, BEN = 4242, 5353  # two private chats: two independent conversations


class Base(unittest.IsolatedAsyncioTestCase):
    versions = (1, 2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
        save_policy(self.root, {'mode': 'participant', 'outgoing': True, 'expected_account_id': str(ACCOUNT)})
        self.fake = FakeHost(self.versions)
        self.host = Host(self.fake.url, PresenceToken('synthetic'))
        self.client = Client()
        self.adapter = Adapter(self.root / 'unused.session', 123, 'a' * 32, client=self.client)
        self.j = Journal(self.root)
        self.e = Participant(self.root, self.adapter, self.host, self.j, ACCOUNT)
        self.e.continuation_version = continuation_version(self.host.identity())
        self.mid = 100

    def tearDown(self):
        self.j.close()
        self.fake.close()
        self.tmp.cleanup()

    def event(self, peer, text='hello', wake=True, thread=None, capture=True):
        self.mid += 1
        e = build_event(ACCOUNT, peer, self.mid, peer, text, topic_id=thread,
                        message={'sent_at': '2026-10-29T00:00:00+00:00'}, source_order=True)
        e['facts'] = ['any_message', 'direct_message'] if wake else ['any_message']
        if capture:
            self.j.capture(e)
        return e

    def row(self, e):
        return self.j.db.execute('SELECT * FROM inbox WHERE id=?', (e['source_event_id'],)).fetchone()

    def state(self, e):
        row = self.row(e)
        return row['state'], json.loads(row['refs'])

    def due_in(self, e):
        return self.row(e)['due'] - time.time()

    def elapse(self, *events):
        """The wait each row was given has passed."""
        with self.j.db:
            for e in events:
                self.j.db.execute('UPDATE inbox SET due=0 WHERE id=?', (e['source_event_id'],))

    def sends(self):
        return [(r.message, r.reply_to.reply_to_msg_id if r.reply_to else None)
                for r in self.client.calls if isinstance(r, F.messages.SendMessageRequest)]

    async def settle(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError('participant state not reached')
            await asyncio.sleep(0.01)

    async def dispatch_all(self):
        while await self.e.dispatch_one():
            pass

    @staticmethod
    def ids(events):
        return [e['source_event_id'] for e in events]


class Negotiation(Base):
    async def test_version_2_is_requested_only_when_listed_and_the_scalar_stays_1(self):
        identity = self.host.identity()  # still requires the scalar presence_continuation_version == 1
        self.assertEqual((identity['presence_continuation_version'], identity['presence_continuation_versions']), (1, [1, 2]))
        self.assertEqual(self.e.continuation_version, 2)
        self.assertEqual(continuation_version({'presence_continuation_version': 1}), 1)
        self.assertEqual(continuation_version({'presence_continuation_versions': [1]}), 1)
        self.assertEqual(continuation_version({'presence_continuation_versions': ['2', True]}), 1)
        self.event(ANN)
        await self.e.submit_once()
        self.assertEqual([body['continuation_version'] for body in self.fake.bodies()], [2])


class BusyWait(unittest.TestCase):
    def test_the_busy_wait_is_the_hosts_own_hint_with_no_growth(self):
        from userbot.presence import busy_delay
        self.assertEqual([busy_delay('conversation', 1.0), busy_delay('capacity', 5.0), busy_delay('capacity', 7.5)],
                         [1.0, 5.0, 7.5])
        self.assertEqual([busy_delay('conversation', None), busy_delay('capacity', None), busy_delay('', None)],
                         [1.0, 5.0, 5.0])  # no hint: the documented 1 s / 5 s; the older refusal waits like capacity


class Startup(Base):
    async def test_a_conversation_waits_for_its_early_answer_while_another_goes(self):
        self.fake.auto_intake = False  # both authors are still starting until the test opens them
        a1, a2, b1 = self.event(ANN, 'first'), self.event(ANN, 'second'), self.event(BEN, 'other')
        pump = asyncio.create_task(self.e.intake_forever(tick=0.02))
        try:
            await asyncio.to_thread(self.fake.wait, lambda: set(self.ids([a1, b1])) <= set(self.fake.posts()))
            await asyncio.sleep(0.3)  # many pump ticks: the second event of Ann's chat is not sent
            self.assertEqual(self.fake.posts(a1['conversation_key']), self.ids([a1]))
            ann = self.fake.author(a1['conversation_key'])
            self.fake.open(ann.ref)  # Ann's author accepts conversation mail: the early answer
            await self.settle(lambda: self.state(a2)[0] == 'done')
            self.assertEqual(self.state(a1), ('pending', [ann.ref]))
            self.assertEqual(self.ids(ann.context), self.ids([a2]))  # it met an author that heard it
            self.assertEqual(self.state(b1), ('pending', []))  # Ben's author has not answered yet
            self.fake.open(self.fake.author(b1['conversation_key']).ref)
            await self.settle(lambda: self.state(b1)[1] != [])
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        posted = self.fake.posts()
        self.assertEqual((sorted(posted[:2]), posted[2:]), (sorted(self.ids([a1, b1])), self.ids([a2])))
        self.assertEqual(self.e._submitting, {}, 'every submission joined at shutdown')

    async def test_an_author_ending_before_intake_answers_completed_with_its_anchor(self):
        self.fake.auto_intake = False
        a1 = self.event(ANN, 'quick question')
        task = asyncio.create_task(self.e.submit_once())
        await asyncio.to_thread(self.fake.wait, lambda: self.fake.author(a1['conversation_key']) is not None)
        ref = self.fake.author(a1['conversation_key']).ref
        self.fake.finish(ref, text='Quick answer', output_ref='out-quick', anchor=a1)
        await task
        self.assertEqual(self.state(a1), ('done', []))
        await self.dispatch_all()
        self.assertEqual(self.sends(), [('Quick answer', int(a1['message']['message_id']))])


class Busy(Base):
    async def test_a_busy_conversation_keeps_its_head_and_identity_while_others_advance(self):
        a1, a2, b1 = self.event(ANN, 'first'), self.event(ANN, 'second'), self.event(BEN, 'other')
        self.fake.busy.add(a1['conversation_key'])  # e.g. its author is still preparing or closing
        await self.e.submit_once()
        self.assertEqual(self.state(a1), ('pending', []))  # still the head; nothing ran
        self.assertAlmostEqual(self.due_in(a1), 1, delta=0.5)  # the Host's retry_after, not a long backoff
        self.assertTrue(self.state(b1)[1], 'another conversation advanced meanwhile')
        await self.e.submit_once()  # not due yet: nothing more for Ann, and her second event waits
        self.assertEqual(self.fake.posts(a1['conversation_key']), self.ids([a1]))
        self.elapse(a1)
        await self.e.submit_once()  # still busy: the same 1 s hint every time, never a growing wait
        self.assertAlmostEqual(self.due_in(a1), 1, delta=0.5)
        for _ in range(1100):  # a long busy spell: no growth, nothing to overflow
            self.e.retry_delay(self.row(a1)['seq'], {'code': 'presence_capacity_full', 'field': 'conversation', 'retry_after': 1})
        for _ in range(2):
            self.elapse(a1)
            await self.e.submit_once()
            self.assertAlmostEqual(self.due_in(a1), 1, delta=0.5)
        self.fake.busy.discard(a1['conversation_key'])
        self.elapse(a1)
        await self.e.submit_once()  # admitted now: the early answer releases her FIFO
        await self.e.submit_once()  # and her second event reaches the author as context
        self.assertEqual(self.fake.posts(a1['conversation_key']), self.ids([a1] * 5 + [a2]))
        sent = {json.dumps(body, sort_keys=True) for body in self.fake.bodies()
                if body['event']['source_event_id'] == a1['source_event_id']}
        self.assertEqual(len(sent), 1, 'the same event, byte for byte, every time')
        self.assertEqual(self.state(a2), ('done', []))
        self.assertEqual(self.ids(self.fake.author(a1['conversation_key']).context), self.ids([a2]))

    async def test_full_capacity_waits_while_live_authors_still_take_their_conversations(self):
        self.fake.capacity = 1
        a1 = self.event(ANN, 'first')
        await self.e.submit_once()  # Ann's author holds the only slot
        b1, a2 = self.event(BEN, 'new conversation'), self.event(ANN, 'background in Ann', wake=False)
        await self.e.submit_once()
        self.assertEqual(self.state(b1), ('pending', []))
        self.assertAlmostEqual(self.due_in(b1), 5, delta=0.5)
        self.assertEqual(self.state(a2), ('done', []))  # the live author took it: no slot, no allowance
        self.elapse(b1)
        await self.e.submit_once()
        self.assertAlmostEqual(self.due_in(b1), 5, delta=0.5)  # the Host's capacity hint again, unchanged
        self.fake.finish(self.state(a1)[1][0])  # the slot frees
        self.elapse(b1)
        await self.e.submit_once()
        self.assertTrue(self.state(b1)[1])

    async def test_the_older_in_flight_refusal_is_a_capacity_wait(self):
        self.fake.inflight_full = True
        a1 = self.event(ANN)
        await self.e.submit_once()
        self.assertEqual(self.state(a1), ('pending', []))
        self.assertAlmostEqual(self.due_in(a1), 5, delta=0.5)


class Flood(Base):
    async def test_a_flood_reaches_the_live_author_in_order_without_drops(self):
        a1 = self.event(ANN, 'start')
        await self.e.submit_once()
        flood = [self.event(ANN, 'm%d' % n, wake=n % 3 == 0) for n in range(20)]
        others = [self.event(BEN, 'o%d' % n) for n in range(3)]
        for _ in range(21):
            await self.e.submit_once()
        self.assertEqual(self.fake.posts(a1['conversation_key']), self.ids([a1] + flood))
        ann = self.fake.author(a1['conversation_key'])
        self.assertEqual(self.ids(ann.context), self.ids(flood), 'wake and background alike, one entry each')
        self.assertTrue(all(self.state(e) == ('done', []) for e in flood))
        self.assertEqual(self.fake.posts(others[0]['conversation_key']), self.ids(others))
        self.assertEqual(len(self.fake.authors), 2, 'no per-event author')


class Retry(Base):
    async def test_a_lost_answer_is_offered_again_unchanged_and_custody_is_taken_once(self):
        a1, a2 = self.event(ANN, 'first'), self.event(ANN, 'second')
        real, lost = self.host.transport, []
        def transport(method, path, payload, headers):
            status, body = real(method, path, payload, headers)
            if path == '/presence/turn' and not lost:
                lost.append(body)
                raise OSError('the answer was lost after the Host stored it')
            return status, body
        self.host.transport = transport
        await self.e.submit_once()
        self.assertEqual(self.state(a1), ('pending', []))
        self.assertEqual(self.fake.posts(), self.ids([a1]))
        self.elapse(a1)
        await self.e.submit_once()
        self.assertEqual(self.state(a1), ('pending', [lost[0]['turn_ref']]))
        self.assertEqual(len(self.fake.authors), 1, 'the stored envelope, never a second author')
        await self.e.submit_once()
        self.assertEqual(self.state(a2), ('done', []))


class Observation(Base):
    async def test_a_late_child_and_its_parent_each_reply_once_to_their_own_anchor(self):
        a1 = self.event(ANN, 'question')
        await self.e.submit_once()
        ref = self.state(a1)[1][0]
        a2 = self.event(ANN, 'one more detail')
        await self.e.submit_once()  # context for the live author
        self.fake.authors[ref].child = 'child-1'  # promoted at a park; the pending view names it
        child = {'ok': True, 'work_ref': 'child-1', 'task_id': 'child-1', 'child_work_ref': '', 'outputs': [],
                 'delivery_reporting_version': 1}
        self.fake.work['child-1'] = [(202, {**child, 'status': 'pending'}),
                                     (200, {**child, 'status': 'completed', 'outcome': 'message',
                                            'text': 'From the child', 'output_ref': 'out-child'})]
        self.elapse(a1)
        await self.e.observe_once()
        self.assertEqual(self.state(a1), ('pending', [ref, 'child-1']))
        self.fake.finish(ref, text='The answer', output_ref='out-author', anchor=a2)
        for _ in range(3):
            self.elapse(a1)
            await self.e.observe_once()
        self.assertEqual(self.state(a1), ('done', []))
        self.assertEqual(self.fake.polls('child-1'), ['child-1', 'child-1'], 'an ended child is not read again')
        await self.dispatch_all()
        # The author's output answers the last input it covered; the child's carries no anchor.
        self.assertEqual(sorted(self.sends()), sorted([('The answer', int(a2['message']['message_id'])),
                                                       ('From the child', int(a1['message']['message_id']))]))

    async def test_an_anchor_outside_this_conversation_or_thread_falls_back_to_the_event(self):
        a1 = self.event(ANN, 'question')
        await self.e.submit_once()
        ref = self.state(a1)[1][0]
        elsewhere = self.event(BEN, 'another chat', capture=False)
        other_thread = build_event(ACCOUNT, ANN, 777, ANN, 'topic', topic_id=9, message={'sent_at': 'x'})
        mismatched = {'source_event_id': a1['source_event_id'], 'message': {'message_id': '999'}}
        for anchor in (elsewhere, other_thread, mismatched):
            self.e.consume_outputs({'outputs': [{'output_ref': 'out-%s' % anchor['message']['message_id'],
                                                 'outcome': 'message', 'text': 'x', 'reply_anchor': {
                                                     'source_event_id': anchor['source_event_id'],
                                                     'message': anchor['message']}}]}, a1)
        targets = {json.loads(row['args'])['reply_id'] for row in self.j.db.execute('SELECT args FROM jobs')}
        self.assertEqual(targets, {int(a1['message']['message_id'])})
        self.fake.finish(ref, text='Final', output_ref='out-final')  # no anchor at all
        self.elapse(a1)
        await self.e.observe_once()
        last = self.j.get(self.e.account, 'auto:' + digest('out-final') + ':0')
        self.assertEqual(json.loads(last['args'])['reply_id'], int(a1['message']['message_id']))

    async def test_one_output_identity_is_queued_once_whatever_anchor_a_later_read_carries(self):
        a1, a2 = self.event(ANN, 'question'), self.event(ANN, 'later', capture=False)
        out = {'output_ref': 'same', 'outcome': 'message', 'text': 'Once',
               'reply_anchor': {'source_event_id': a1['source_event_id'], 'message': a1['message']}}
        self.e.consume_outputs({'outputs': [out]}, a1)
        self.e.consume_outputs({'outputs': [{**out, 'reply_anchor': {'source_event_id': a2['source_event_id'],
                                                                     'message': a2['message']}}]}, a1)
        self.e.consume_outputs({'outputs': [{k: v for k, v in out.items() if k != 'reply_anchor'}]}, a1)
        rows = self.j.db.execute('SELECT args FROM jobs').fetchall()
        self.assertEqual([json.loads(r['args'])['reply_id'] for r in rows], [int(a1['message']['message_id'])])

    async def test_a_stop_sends_nothing_and_never_submits_the_event_again(self):
        a1 = self.event(ANN, 'question')
        await self.e.submit_once()
        self.fake.finish(self.state(a1)[1][0], status='cancelled')  # the owner's Stop
        for _ in range(3):
            self.elapse(a1)
            await self.e.incoming_once()
        self.assertEqual(self.state(a1), ('done', []))
        self.assertEqual(self.fake.posts(), self.ids([a1]))
        self.assertEqual(self.j.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)


class OlderHost(Base):
    versions = None

    async def test_an_older_host_keeps_version_1_and_its_gate_wait(self):
        self.assertEqual(self.e.continuation_version, 1)
        a1 = self.event(ANN, 'first')
        task = asyncio.create_task(self.e.submit_once())
        await asyncio.to_thread(self.fake.wait, lambda: self.fake.author(a1['conversation_key']) is not None)
        await asyncio.sleep(0.1)
        self.assertFalse(task.done(), 'version 1 waits for a park or the terminal, as before')
        self.fake.finish(self.fake.author(a1['conversation_key']).ref, text='Done', output_ref='out-1')
        await task
        self.assertEqual(self.state(a1), ('done', []))
        self.assertEqual([body['continuation_version'] for body in self.fake.bodies()], [1])

    async def test_an_older_host_retry_keeps_its_previous_pacing(self):
        self.fake.inflight_full = True
        a1 = self.event(ANN)
        await self.e.submit_once()
        self.assertAlmostEqual(self.due_in(a1), 30, delta=1)


if __name__ == '__main__':
    unittest.main()
