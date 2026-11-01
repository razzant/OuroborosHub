"""Selected earlier history through the history-only ingress. Offline fakes only.

A synthetic Telegram history and an in-memory Host speaking the frozen wire contract v1
stand in for the provider and the core. Nothing here proves the real Host: that joint
run happens once the core history ingress is integrated.
"""
import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from history_fakes import WireHost, History, Forum, message, service, user, group, source_facts
from userbot.config import Config, save_config, _write_json
from userbot import participant
from userbot.participant import Participant, Host, record_host_facts
from userbot.participant_state import Journal, save_policy, policy, digest
from userbot.presence import PresenceToken, build_event
from userbot.telegram import TelegramAdapter
from userbot.store import DurableStore
from userbot import history_import as H
from telethon import types as T

ACCOUNT = 8667525238
GROUP = -1000000000123
FORUM = -1000000000456
OTHER = -1000000000789
MIRA = 4242
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Adapter(TelegramAdapter):
    catalogs = {}

    async def topic_ids(self, peer, guard=None):
        self.client.calls.append(('topic_ids', peer))  # a forum catalog read is a Telegram read too
        return set(self.catalogs[peer])


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
        save_policy(self.root, {'mode': 'participant', 'outgoing': False})
        self.wire = WireHost(ACCOUNT, now=lambda: NOW)
        self.host = Host('http://127.0.0.1:9', PresenceToken('skill-token'), transport=self.wire.transport)
        self.entities = {GROUP: group(GROUP), MIRA: user(MIRA, 'Mira', 'mira'), OTHER: group(OTHER, 'Other'),
                         FORUM: group(FORUM, 'Workshop', forum=True)}
        self.histories = {}
        self.telegram = History(self.entities, self.histories)
        self.adapter = Adapter(self.root / 'unused.session', 123, 'a' * 32, client=self.telegram)
        self.j = Journal(self.root)
        self.engine = Participant(self.root, self.adapter, self.host, self.j, ACCOUNT)
        self.clock = Clock()
        self.importer = H.HistoryImporter(self.engine, clock=self.clock)
        for peer, title, kind in ((GROUP, 'Board games on Fridays', 'supergroup'), (MIRA, 'Mira', 'private'),
                                  (OTHER, 'Other', 'supergroup'), (ACCOUNT, 'Saved Messages', 'private')):
            self.j.remember_chat(ACCOUNT, peer, title, kind, 'dialogs')

    def tearDown(self):
        self.j.close(); self.tmp.cleanup()

    def plan(self, chats=(GROUP,), start=NOW - 30 * DAY, end=NOW, cap=500, exclude=()):
        spec = H.validate_plan({'chats': chats if chats == 'receiving' else [str(c) for c in chats],
                                'exclude': [str(c) for c in exclude], 'from': start.isoformat(), 'to': end.isoformat(),
                                'max_per_chat': cap}, policy(self.root), ACCOUNT, now=NOW)
        return H.save_plan(self.j, ACCOUNT, spec)

    def act(self, plan_id, action, now=NOW):
        return H.transition(self.j, ACCOUNT, plan_id, action, policy(self.root), now=now)

    def view(self, plan_id):
        return next(p for p in H.plans(self.j, ACCOUNT) if p['id'] == plan_id)

    def state(self, plan_id):
        return self.j.db.execute('SELECT state FROM imports WHERE id=?', (plan_id,)).fetchone()[0]

    async def drive(self, plan_id, steps=200):
        for _ in range(steps):
            if self.state(plan_id) != 'running':
                break
            self.clock.t += 700  # past every pacing gap and backoff
            await self.importer.once()
        return self.state(plan_id)

    def posts(self):
        return [json.loads(raw) for raw in self.wire.history_bodies]

    def posted_ids(self):
        return [r['event']['source_event_id'] for body in self.posts() for r in body['records']]

    def stored(self):
        return {sid: row for sid, row in self.wire.rows.items() if row['custody'] == 'history'}

    def records(self, plan_id):
        return {r['source_event_id']: r['state'] for r in self.j.db.execute(
            'SELECT source_event_id,state FROM import_records WHERE plan=?', (plan_id,))}

    def receipt(self, key, peer, mid, recorded):
        self.j.report(key, {'schema_version': 1, 'delivery_id': key, 'part_id': '0', 'state': 'delivered', 'provider': 'telegram',
                            'account_id': str(ACCOUNT), 'conversation_id': str(peer), 'thread_id': '', 'text': 'same words',
                            'format': 'text', 'message': {'message_id': str(mid)}, 'origin': {'kind': 'automatic'}})
        if recorded:
            self.j.report_done(key)

    def legacy(self, rows):
        path = self.root / 'transport.sqlite3'
        path.unlink(missing_ok=True)
        with DurableStore(path):
            pass
        db = sqlite3.connect(path)
        for n, (mid, reported, ref) in enumerate(rows):
            db.execute("INSERT INTO outgoing(account_id,intent_id,peer_id,payload_ref,random_id,status,remote_message_id,reported) "
                       "VALUES(?,?,?,?,?,'sent',?,?)", (str(ACCOUNT), f'auto_{n}', MIRA, ref, n + 1, mid, reported))
        db.commit(); db.close()

    def live(self, msg, peer, sender):
        """Live capture of the same message through the participant's own capture path."""
        event = NS(message=msg, chat_id=peer, sender_id=sender, raw_text=msg.message, is_private=peer > 0)
        event.get_sender = lambda: asyncio.sleep(0, result=user(sender))
        event.get_chat = lambda: self.telegram.get_entity(peer)
        return event


def sid(peer, mid, thread=0):
    return f'telegram:{ACCOUNT}:{peer}:{thread}:{mid}'


class Routes:
    """The skill's registered routes over the same private state, called as the Host calls them."""

    def __init__(self, root):
        import importlib.util
        from types import ModuleType
        name = 'history_import_routes_test'
        package = ModuleType(name); package.__path__ = [str(ROOT)]
        sys.modules[name] = package
        spec = importlib.util.spec_from_file_location(name + '.plugin', ROOT / 'plugin.py')
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        self.routes = {}
        plugin.register(NS(get_state_dir=lambda: str(root), register_route=lambda path, handler, methods: self.routes.__setitem__(path, handler),
                           register_tool=lambda *a, **kw: None, register_companion_process=lambda *a: None,
                           register_settings_section=lambda *a, **kw: None, register_ui_tab=lambda *a, **kw: None))

    async def __call__(self, path, body=None):
        async def payload():
            return body
        answer = await self.routes[path](NS(method='GET' if body is None else 'POST', json=payload, query_params={}))
        return answer.status_code, json.loads(answer.body)


class CapabilityAndAuthority(Base):
    async def test_without_host_capability_nothing_is_read_and_nothing_falls_back(self):
        self.wire.supports = False
        self.histories[GROUP] = [message(1, GROUP, 'old', NOW - 2 * DAY)]
        plan = self.plan(); self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'paused')
        self.assertEqual(self.view(plan)['reason'], 'host_support_missing')
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.wire.paths(), [('GET', '/identity')])
        # A resume re-checks first; still nothing is read, and nothing goes to /presence/turn or /chat/inject.
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'paused')
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.wire.paths(), [('GET', '/identity')] * 2)

    async def test_refused_or_unreachable_host_stops_before_any_telegram_read(self):
        self.histories[GROUP] = [message(1, GROUP, 'old', NOW - 2 * DAY)]
        plan = self.plan(); self.act(plan, 'start')
        self.wire.down.add('/identity')
        self.clock.t += 700
        await self.importer.once()
        self.assertEqual(self.state(plan), 'running')
        self.assertEqual(self.view(plan)['chats'][0]['state'], 'pending')
        self.assertEqual(self.telegram.calls, [])
        self.wire.down.clear(); self.wire.auth_blocked = True
        self.assertEqual(await self.drive(plan), 'paused')
        self.assertEqual(self.view(plan)['reason'], 'host_auth_blocked')
        self.assertEqual(self.telegram.calls, [])

    async def test_place_not_connected_pauses_before_reading_and_silent_still_records(self):
        self.histories[GROUP] = [message(1, GROUP, 'old', NOW - 2 * DAY)]
        self.wire.place_state = None
        plan = self.plan(); self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'paused')
        self.assertEqual((self.view(plan)['reason'], self.view(plan)['detail']), ('place_not_connected', {'chat': str(GROUP)}))
        self.assertEqual(self.telegram.calls, [])
        # Silent and Paused places record history; they only never wake.
        self.wire.place_state = 'silent'
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(list(self.stored()), [sid(GROUP, 1)])

    async def test_another_account_never_runs_or_sees_the_plan(self):
        self.histories[GROUP] = [message(1, GROUP, 'old', NOW - 2 * DAY)]
        plan = self.plan(); self.act(plan, 'start')
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=999))
        self.clock.t += 700
        self.assertFalse(await self.importer.once())  # this companion's account is no longer current
        other = Participant(self.root, self.adapter, self.host, self.j, 999)
        self.assertFalse(await H.HistoryImporter(other, clock=self.clock).once())
        self.assertEqual((self.state(plan), self.wire.calls, self.telegram.calls), ('running', [], []))
        self.assertEqual(H.plans(self.j, 999), [])
        with self.assertRaisesRegex(ValueError, 'no import plan'):
            H.transition(self.j, 999, plan, 'pause', policy(self.root))


class SelectionWindowAndCaps(Base):
    async def test_chosen_chats_window_edges_and_clamp_at_start(self):
        start, end = NOW - 10 * DAY, NOW + 12 * timedelta(hours=1)
        self.histories[GROUP] = [
            message(1, GROUP, 'one second too early', start - timedelta(seconds=1)),
            message(2, GROUP, 'exactly at from', start),
            message(3, GROUP, 'inside', NOW - DAY, sender=811),
            message(4, GROUP, 'after Start is live, never history', NOW + timedelta(minutes=5)),
        ]
        self.histories[MIRA] = [message(7, MIRA, 'hi from Mira', NOW - 2 * DAY)]
        plan = self.plan(chats=(GROUP,), start=start, end=end)
        self.act(plan, 'start', now=NOW)
        self.assertEqual(self.view(plan)['until'], NOW.isoformat())  # "to" clamps to the moment of Start
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(sorted(self.stored()), [sid(GROUP, 2), sid(GROUP, 3)])
        first = self.telegram.reads()[0]
        self.assertEqual(first[2]['offset_date'], NOW)
        record = next(r for b in self.posts() for r in b['records'] if r['event']['source_event_id'] == sid(GROUP, 3))
        self.assertEqual((record['authorship'], record['source_timestamp'], record['staged_files']),
                         ('participant', (NOW - DAY).isoformat(), []))
        self.assertEqual(record['event']['actor']['platform_actor_id'], '811')
        self.assertEqual(record['event']['conversation'], {'title': 'Board games on Fridays', 'chat_type': 'supergroup'})
        self.assertNotIn('facts', record['event'])
        self.assertNotIn(MIRA, [c[1] for c in self.telegram.calls])  # not chosen, never read

    async def test_all_receiving_chats_minus_plan_and_scope_exclusions_and_saved_messages(self):
        save_policy(self.root, {'blocked_peers': [OTHER]})
        for peer in (GROUP, MIRA, OTHER, ACCOUNT):
            self.histories[peer] = [message(5, peer, 'x', NOW - DAY)]
        plan = self.plan(chats='receiving', exclude=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual([c['id'] for c in self.view(plan)['chats']], [str(GROUP)])
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual({c[1] for c in self.telegram.calls}, {GROUP})
        with self.assertRaisesRegex(ValueError, 'Saved Messages'):
            self.plan(chats=(ACCOUNT,))
        with self.assertRaisesRegex(ValueError, 'outside the receiving scope'):
            self.plan(chats=(OTHER,))

    async def test_chats_left_out_are_never_imported_when_chats_are_chosen_either(self):
        for peer in (GROUP, MIRA):
            self.histories[peer] = [message(5, peer, 'x', NOW - DAY)]
        plan = self.plan(chats=(GROUP, MIRA), exclude=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual([c['id'] for c in self.view(plan)['chats']], [str(GROUP)])
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual({c[1] for c in self.telegram.calls}, {GROUP})
        self.assertEqual(self.posted_ids(), [sid(GROUP, 5)])
        with self.assertRaisesRegex(ValueError, 'every chosen chat is also left out'):
            self.plan(chats=(MIRA,), exclude=(MIRA,))

    async def test_cap_reports_whether_older_messages_were_left_unread(self):
        self.histories[GROUP] = [message(i, GROUP, f'm{i}', NOW - (6 - i) * timedelta(hours=1)) for i in range(1, 6)]
        self.histories[MIRA] = [message(i, MIRA, f'm{i}', NOW - (6 - i) * timedelta(hours=1)) for i in range(1, 4)]
        plan = self.plan(chats=(GROUP, MIRA), cap=3)
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        chats = {c['id']: c for c in self.view(plan)['chats']}
        self.assertEqual((chats[str(GROUP)]['read'], chats[str(GROUP)]['truncated']), (3, True))
        self.assertEqual((chats[str(MIRA)]['read'], chats[str(MIRA)]['truncated']), (3, False))
        self.assertEqual(len(self.stored()), 6)
        self.assertEqual(sorted(self.stored())[:3], [sid(GROUP, 3), sid(GROUP, 4), sid(GROUP, 5)])  # the newest three
        # One message beyond the limit is read only to tell whether older ones remain.
        self.assertEqual([c[2]['limit'] for c in self.telegram.reads()], [4, 4])

    async def test_empty_window_finishes_without_any_host_write(self):
        self.histories[GROUP] = [message(1, GROUP, 'too old', NOW - 60 * DAY)]
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual((self.wire.history_bodies, self.view(plan)['counts']), ([], {}))
        self.assertEqual(self.view(plan)['chats'][0]['read'], 0)

    async def test_long_history_pages_newest_first_with_a_frozen_cursor(self):
        self.histories[GROUP] = [message(i, GROUP, f'm{i}', NOW - DAY + i * timedelta(seconds=10)) for i in range(1, 251)]
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        offsets = [c[2]['offset_id'] for c in self.telegram.reads()]
        self.assertEqual(offsets, [0, 151, 51])
        self.assertEqual([len(b['records']) for b in self.posts()], [100, 100, 50])
        self.assertEqual(len(self.stored()), 250)

    async def test_start_and_resume_are_explicit_and_exclusive(self):
        first, second = self.plan(), self.plan(chats=(MIRA,))
        with self.assertRaisesRegex(ValueError, 'only a paused or partial import can resume'):
            self.act(first, 'resume')
        self.act(first, 'start')
        with self.assertRaisesRegex(ValueError, 'another import is running'):
            self.act(second, 'start')
        self.act(first, 'pause')
        self.act(second, 'start')
        with self.assertRaisesRegex(ValueError, 'another import is running'):
            self.act(first, 'resume')
        self.act(second, 'cancel')
        with self.assertRaisesRegex(ValueError, 'already finished'):
            self.act(second, 'cancel')
        late = self.plan(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=20))
        with self.assertRaisesRegex(ValueError, 'nothing to import yet'):
            self.act(late, 'start')


class ScopeAndRetries(Base):
    async def test_scope_change_withholds_unsent_records_and_skips_the_chat(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        self.histories[MIRA] = [message(9, MIRA, 'still received', NOW - DAY)]
        plan = self.plan(chats=(GROUP, MIRA))
        self.act(plan, 'start')
        self.clock.t += 700
        await self.importer.once()  # capability, place, one page frozen; nothing sent yet
        self.assertEqual(set(self.records(plan).values()), {'pending'})
        save_policy(self.root, {'blocked_peers': [GROUP]})
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(set(self.records(plan)[sid(GROUP, i)] for i in range(1, 4)), {'withheld'})
        self.assertEqual(self.posted_ids(), [sid(MIRA, 9)])
        chats = {c['id']: c for c in self.view(plan)['chats']}
        self.assertEqual((chats[str(GROUP)]['state'], chats[str(GROUP)]['reason']), ('skipped', 'outside_receiving_scope'))
        # Selected scope: a chat that is no longer selected is skipped before any read.
        save_policy(self.root, {'blocked_peers': [], 'scope': 'selected', 'selected_peers': [GROUP, MIRA]})
        again = self.plan(chats=(GROUP, MIRA))
        save_policy(self.root, {'selected_peers': []})
        self.act(again, 'start')
        reads = len(self.telegram.calls)
        self.assertEqual(await self.drive(again), 'done')
        self.assertEqual(len(self.telegram.calls), reads)
        self.assertEqual({c['reason'] for c in self.view(again)['chats']}, {'peer_not_selected'})

    async def test_partial_503_resends_only_the_unconfirmed_suffix_byte_for_byte(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i} ✓', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 6)]
        self.wire.faults.append(('unwritable_at', 2, True))
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        first, second = self.posts()
        self.assertEqual(second['records'], first['records'][2:])
        self.assertEqual(self.wire.history_bodies[1],
                         json.dumps({'import_id': plan, 'records': first['records'][2:]}, ensure_ascii=False).encode())
        results = self.records(plan)
        self.assertEqual(set(results.values()), {'recorded', 'duplicate'})
        # The landed-but-unconfirmed write answers duplicate on the unchanged resend.
        self.assertEqual(results[first['records'][2]['event']['source_event_id']], 'duplicate')
        self.assertEqual(len(self.telegram.reads()), 1)

    async def test_unknown_transport_outcome_resends_identical_bytes_without_rereading(self):
        original = message(3, GROUP, 'first text', NOW - DAY)
        self.histories[GROUP] = [message(2, GROUP, 'other', NOW - 2 * DAY), original]
        self.wire.faults.append(('transport_after',))
        plan = self.plan()
        self.act(plan, 'start')
        self.clock.t += 700
        await self.importer.once()  # freeze
        self.clock.t += 700
        await self.importer.once()  # landed, answer lost
        self.assertEqual(self.view(plan)['counts'], {'pending': 2})
        # Telegram now holds an edited revision; the retry still carries the frozen records.
        self.histories[GROUP][1] = message(3, GROUP, 'edited later', NOW - DAY, edit_date=NOW - timedelta(hours=1))
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(len(self.wire.history_bodies), 2)
        self.assertEqual(self.wire.history_bodies[0], self.wire.history_bodies[1])
        self.assertEqual(self.view(plan)['counts'], {'duplicate': 2})
        self.assertEqual(len(self.telegram.reads()), 1)  # the page was never read again
        self.assertNotIn(':edit:', ' '.join(self.wire.rows))  # the later edit stays a live event

    async def test_no_page_is_read_while_records_wait_for_the_host(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(seconds=10)) for i in range(1, 151)]
        self.wire.faults += [('transport_before',), ('refuse', 503, 'presence_history_unavailable', 'retry', None)]
        plan = self.plan()
        self.act(plan, 'start')
        for _ in range(3):  # freeze, lost request, refused retry
            self.clock.t += 700; await self.importer.once()
            self.assertEqual(len(self.telegram.reads()), 1)
        self.clock.t += 700; await self.importer.once()  # confirmed at last
        self.assertEqual(len(self.telegram.reads()), 1)
        self.clock.t += 700; await self.importer.once()  # only now the next page
        self.assertEqual(len(self.telegram.reads()), 2)
        self.assertEqual(len(set(self.wire.history_bodies[:3])), 1)
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(len(self.stored()), 150)

    async def test_conflict_is_held_once_and_the_rest_is_confirmed(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        live = H.history_record(ACCOUNT, GROUP, None, message(2, GROUP, 'g2 said differently', NOW - DAY + timedelta(minutes=2)))
        self.wire.seed_live(live['event'])
        plan = self.plan()
        self.act(plan, 'start')
        self.clock.t += 700; await self.importer.once()
        self.clock.t += 700; await self.importer.once()
        status = [s for s in self.wire.calls if s[1] == '/presence/history']
        self.assertEqual(len(status), 1)
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.records(plan), {sid(GROUP, 1): 'recorded', sid(GROUP, 2): 'conflict', sid(GROUP, 3): 'recorded'})
        self.assertEqual(self.posted_ids().count(sid(GROUP, 2)), 1)
        self.assertEqual(self.view(plan)['held'], [{'chat': str(GROUP), 'state': 'conflict', 'reason': 'presence_event_identity_conflict',
                                                     'receipt': '', 'message_id': '2'}])
        self.assertEqual(self.wire.rows[sid(GROUP, 2)]['custody'], 'live')  # never rewritten

    async def test_request_refusals_wait_pause_or_hold_exactly_as_typed(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        self.histories[MIRA] = [message(8, MIRA, 'm', NOW - DAY)]
        self.wire.faults += [('refuse', 429, 'presence_rate_limited', 'retry', {'retry_after': 7}),
                             ('refuse', 400, 'presence_history_invalid', 'rejected', {'field': 'records[1].source_timestamp'}),
                             ('refuse', 409, 'place_not_connected', 'blocked', None)]
        plan = self.plan(chats=(GROUP, MIRA))
        self.act(plan, 'start')
        self.clock.t += 700; await self.importer.once()
        self.clock.t += 700; await self.importer.once()
        waiting = self.view(plan)
        self.assertEqual(self.j.db.execute('SELECT progress FROM imports').fetchone()[0].count('presence_rate_limited'), 1)
        self.assertEqual(json.loads(self.j.db.execute('SELECT progress FROM imports').fetchone()[0])['wait_until'], self.clock.t + 7)
        self.assertEqual(waiting['counts'], {'pending': 3})
        self.clock.t += 8; await self.importer.once()  # invalid record 1 is held, the others go again
        self.assertEqual(self.records(plan)[sid(GROUP, 2)], 'invalid')
        self.clock.t += 700; await self.importer.once()  # a place problem needs the owner
        self.assertEqual((self.state(plan), self.view(plan)['reason']), ('paused', 'place_not_connected'))
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        bodies = self.posts()
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[2]['records'], [bodies[0]['records'][0], bodies[0]['records'][2]])
        self.assertEqual(bodies[3], bodies[2])
        self.assertEqual(self.records(plan), {sid(GROUP, 1): 'recorded', sid(GROUP, 2): 'invalid', sid(GROUP, 3): 'recorded',
                                              sid(MIRA, 8): 'recorded'})

    async def test_origin_mismatch_rejects_that_chat_only(self):
        self.histories[GROUP] = [message(1, GROUP, 'g', NOW - DAY)]
        self.histories[MIRA] = [message(8, MIRA, 'm', NOW - DAY)]
        self.wire.faults.append(('refuse', 403, 'presence_origin_mismatch', 'rejected', None))
        plan = self.plan(chats=(GROUP, MIRA))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.records(plan), {sid(GROUP, 1): 'rejected', sid(MIRA, 8): 'recorded'})
        self.assertEqual({c['id']: c['reason'] for c in self.view(plan)['chats']}, {str(GROUP): 'outside_place', str(MIRA): ''})

    async def test_restart_pauses_and_resume_sends_the_frozen_page_before_any_read(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        plan = self.plan()
        self.act(plan, 'start')
        self.clock.t += 700; await self.importer.once()
        reads = len(self.telegram.reads())
        H.interrupt_running(self.j)  # what the next companion start does
        self.assertEqual((self.state(plan), self.view(plan)['reason']), ('paused', 'interrupted'))
        restarted = H.HistoryImporter(self.engine, clock=self.clock)
        self.clock.t += 700
        self.assertFalse(await restarted.once())  # never resumes by itself
        self.act(plan, 'resume')
        calls = len(self.wire.calls)
        self.clock.t += 700; await restarted.once()
        # The restarted companion asks the Host again before sending the frozen page, and never Telegram.
        self.assertEqual(self.wire.paths()[calls:], [('GET', '/identity'), ('POST', '/presence/history')])
        self.assertEqual(len(self.telegram.reads()), reads)
        self.importer = restarted
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(len(self.stored()), 3)

    async def test_cancel_stops_reading_and_sending_and_keeps_counts(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        plan = self.plan()
        self.act(plan, 'start')
        self.clock.t += 700; await self.importer.once()
        self.act(plan, 'cancel')
        self.clock.t += 700
        self.assertFalse(await self.importer.once())
        self.assertEqual((self.state(plan), self.wire.history_bodies), ('cancelled', []))
        self.assertEqual(self.view(plan)['counts'], {'pending': 3})  # shown as not confirmed, never as recorded

    async def test_pause_during_a_read_keeps_nothing_of_that_page(self):
        self.histories[GROUP] = [message(1, GROUP, 'g', NOW - DAY)]
        plan = self.plan()
        self.act(plan, 'start')
        original = self.telegram.get_messages
        async def pausing(*args, **kw):
            self.act(plan, 'pause')  # the owner presses Pause while Telegram answers
            return await original(*args, **kw)
        self.telegram.get_messages = pausing
        self.clock.t += 700; await self.importer.once()
        self.assertEqual((self.state(plan), self.records(plan)), ('paused', {}))
        self.assertEqual(self.view(plan)['chats'][0]['read'], 0)
        self.telegram.get_messages = original
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(list(self.stored()), [sid(GROUP, 1)])


class IdentityAndCustody(Base):
    async def test_imported_identity_and_source_facts_equal_live_capture(self):
        photo = T.Photo(id=78, access_hash=1, file_reference=b'r', date=None, sizes=[], dc_id=1)
        plain = message(21, GROUP, 'Has anyone tried this with six players?', NOW - DAY, sender=809,
                        reply_to=T.MessageReplyHeader(reply_to_msg_id=20))
        edited = message(22, GROUP, 'corrected', NOW - DAY, sender=809, edit_date=NOW - timedelta(hours=2))
        media = message(23, GROUP, '', NOW - DAY, sender=809, media=T.MessageMediaPhoto(photo=photo))
        self.histories[GROUP] = [message(20, GROUP, 'root', NOW - 2 * DAY), plain, edited, media]
        imported = {}
        for m in (plain, edited, media):
            imported[m.id] = H.history_record(ACCOUNT, GROUP, await self.engine.thread_of(self.entities[GROUP], GROUP, m), m,
                                              conversation={'title': 'Board games on Fridays', 'chat_type': 'supergroup'})
            await self.engine.capture(self.live(m, GROUP, 809), 'edit' if m.edit_date else 'new')
        live = {json.loads(r['event'])['message']['message_id']: json.loads(r['event']) for r in self.j.db.execute('SELECT event FROM inbox')}
        for mid, record in imported.items():
            event = live[str(mid)]
            self.assertEqual(WireHost.identity(record['event']), WireHost.identity(event))
            self.assertEqual(source_facts(record['event']['message']), source_facts(event['message']))
            self.assertEqual(event['message']['sent_at'], record['source_timestamp'])  # the send time a live row is compared by
            # One order fact on both doors: Telegram's id of the message, an edit's included.
            self.assertEqual((event['message']['source_order'], record['event']['message']['source_order']), (mid, mid))
        self.assertIn(':edit:', imported[22]['event']['source_event_id'])
        self.assertEqual(imported[22]['source_timestamp'], (NOW - DAY).isoformat())  # original send time, even edited
        self.assertEqual(imported[23]['event']['message']['media']['status'], 'not_downloaded')
        # Live intake hands its copies to the Host, which admits them: the import leaves them to it.
        for _ in range(3):
            await self.engine.incoming_once()
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.posted_ids(), [sid(GROUP, 20)])
        self.assertEqual(self.view(plan)['chats'][0]['skipped'], {'live_captured': 3})
        # A genuine later edit is a new revision, so a new live event, never this history.
        later = message(22, GROUP, 'corrected twice', NOW - DAY, sender=809, edit_date=NOW - timedelta(hours=1))
        again = H.history_record(ACCOUNT, GROUP, None, later)
        self.assertNotEqual(again['event']['source_event_id'], imported[22]['event']['source_event_id'])

    async def test_host_live_copy_answers_duplicate_with_live_custody(self):
        m = message(31, GROUP, 'captured live before this journal existed', NOW - DAY)
        self.histories[GROUP] = [m]
        self.wire.seed_live(H.history_record(ACCOUNT, GROUP, None, m)['event'])
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        row = json.loads(self.j.db.execute('SELECT result FROM import_records').fetchone()[0])
        self.assertEqual((self.records(plan)[sid(GROUP, 31)], row['custody']), ('duplicate', 'live'))
        self.assertEqual(self.stored(), {})

    async def test_a_second_plan_reimports_as_duplicates_whatever_the_display_facts(self):
        self.histories[GROUP] = [message(1, GROUP, 'one', NOW - DAY), message(2, GROUP, 'two', NOW - DAY)]
        first = self.plan()
        self.act(first, 'start')
        self.assertEqual(await self.drive(first), 'done')
        self.entities[GROUP].title = 'Board games, renamed'  # display enrichment only
        second = self.plan()
        self.act(second, 'start')
        self.assertEqual(await self.drive(second), 'done')
        self.assertEqual(set(self.records(second).values()), {'duplicate'})
        later = [r['event']['conversation']['title'] for r in self.posts()[-1]['records']]
        self.assertEqual(later, ['Board games, renamed'] * 2)
        self.assertEqual({row['import_id'] for row in self.stored().values()}, {first})  # the first record stands

    async def test_messages_live_capture_still_holds_are_held_back_never_sent_beside_it(self):
        held = message(41, GROUP, 'arrived a moment ago', NOW - timedelta(minutes=1))
        self.histories[GROUP] = [message(40, GROUP, 'old', NOW - DAY), held]
        self.j.capture_raw(digest(['x', GROUP, 41]), {'account_id': str(ACCOUNT), 'peer': GROUP, 'actor': 809, 'kind': 'new', 'private': False}, bytes(held))
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')  # never Done while live intake has it in hand
        self.assertEqual(self.posted_ids(), [sid(GROUP, 40)])
        view = self.view(plan)
        self.assertEqual(([(h['message_id'], h['reason']) for h in view['held']], view['chats'][0]['skipped']), ([('41', 'live_pending')], {}))

    async def test_service_messages_and_unresolved_forum_topics_are_not_guessed(self):
        Adapter.catalogs = {FORUM: {1, 40}}
        general = message(50, FORUM, 'general', NOW - DAY)
        topical = message(51, FORUM, 'in topic', NOW - DAY, reply_to=T.MessageReplyHeader(forum_topic=True, reply_to_msg_id=40, reply_to_top_id=40))
        unknown = message(52, FORUM, 'unknown topic', NOW - DAY, reply_to=T.MessageReplyHeader(forum_topic=True, reply_to_msg_id=77, reply_to_top_id=77))
        self.histories[FORUM] = [general, topical, unknown, service(53, FORUM, NOW - DAY)]
        plan = self.plan(chats=(FORUM,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        bodies = self.posts()
        self.assertEqual([{r['event']['thread_id'] for r in b['records']} for b in bodies], [{'40'}, {''}])
        self.assertEqual(sorted(self.posted_ids()), [sid(FORUM, 50), sid(FORUM, 51, 40)])
        self.assertEqual(self.view(plan)['chats'][0]['skipped'], {'service': 1, 'topic_unresolved': 1})
        # The same messages captured live land in the same threads under the same IDs.
        await self.engine.capture(self.live(topical, FORUM, 809), 'new')
        await self.engine.capture(self.live(general, FORUM, 809), 'new')
        self.assertEqual(sorted(json.loads(r['event'])['source_event_id'] for r in self.j.db.execute('SELECT event FROM inbox')),
                         sorted(self.posted_ids()))


class OwnMessages(Base):
    async def test_only_proven_receipts_skip_and_unproven_ones_are_listed_not_lost(self):
        own = lambda mid: message(mid, MIRA, 'same words', NOW - DAY + mid * timedelta(minutes=1), out=True)
        self.histories[MIRA] = [own(30), own(31), own(32), own(33), own(34), own(35), message(36, MIRA, 'from Mira', NOW - DAY)]
        self.receipt('auto:a', MIRA, 30, recorded=True)   # Host confirmed: already in the room
        self.receipt('auto:b', MIRA, 31, recorded=False)  # acknowledgement unproven
        self.legacy([(33, 1, 'p33'), (34, 0, 'p34'), (35, 0, 'p35')])
        (self.root / 'outgoing').mkdir()
        (self.root / 'outgoing' / 'p35.reported').write_text('{}')  # written only after the Host recorded it
        plan = self.plan(chats=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')  # held own messages wait for the owner's Resume
        # Same text as a delivered reply but another message ID: imported, never matched by text.
        self.assertEqual(sorted(self.posted_ids()), [sid(MIRA, 32), sid(MIRA, 36)])
        own32 = next(r for b in self.posts() for r in b['records'] if r['event']['message']['message_id'] == '32')
        self.assertEqual((own32['authorship'], own32['event']['actor']['platform_actor_id']), ('platform_account', str(ACCOUNT)))
        view = self.view(plan)
        self.assertEqual(view['chats'][0]['skipped'], {'delivered': 3})
        self.assertEqual(sorted((h['message_id'], h['state'], h['reason'], h['receipt']) for h in view['held']),
                         [('31', 'residual', 'receipt_unconfirmed', 'retrying'), ('34', 'residual', 'receipt_unconfirmed', 'retrying')])
        self.assertEqual((view['reason'], view['held_total'], view['held_omitted']), ('held_back', 2, 0))

    async def test_unreadable_legacy_receipts_keep_every_own_message_as_residual(self):
        (self.root / 'transport.sqlite3').write_bytes(b'not a database')
        self.histories[MIRA] = [message(30, MIRA, 'mine', NOW - DAY, out=True), message(31, MIRA, 'theirs', NOW - DAY)]
        plan = self.plan(chats=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')
        self.assertEqual(self.posted_ids(), [sid(MIRA, 31)])
        self.assertEqual([(h['message_id'], h['reason']) for h in self.view(plan)['held']], [('30', 'receipt_unreadable')])


class ReceiptRepair(Base):
    """An own message whose delivery receipt is unproven holds the plan partial until the owner resumes it."""

    def own(self, mid, text='Sure, I will bring them.'):
        return message(mid, MIRA, text, NOW - DAY + mid * timedelta(minutes=1), out=True)

    async def test_unconfirmed_receipt_stays_held_until_a_resume_finds_it_recorded(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        self.histories[MIRA] = [message(30, MIRA, 'Can you bring the rules?', NOW - DAY), self.own(31)]
        self.wire.seed_live(H.history_record(ACCOUNT, GROUP, None, message(2, GROUP, 'g2 said otherwise', NOW - DAY + 2 * timedelta(minutes=1)))['event'])
        self.receipt('auto:31', MIRA, 31, recorded=False)
        plan = self.plan(chats=(GROUP, MIRA))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')
        view = self.view(plan)
        self.assertEqual((view['reason'], view['counts']), ('held_back', {'recorded': 3, 'conflict': 1, 'residual': 1}))
        self.assertEqual(sorted((h['message_id'], h['state']) for h in view['held']), [('2', 'conflict'), ('31', 'residual')])
        posts, reads = len(self.wire.history_bodies), len(self.telegram.reads())
        # A Resume while the receipt is still being reported changes nothing and writes nothing.
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'partial')
        self.assertEqual((len(self.wire.history_bodies), len(self.telegram.reads())), (posts, reads))
        # Repair: the Host records the receipt; the next Resume skips the proven message without a write.
        self.j.report_done('auto:31')
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual((len(self.wire.history_bodies), len(self.telegram.reads())), (posts, reads))
        self.assertEqual(self.records(plan)[sid(MIRA, 31)], 'delivered')
        self.assertNotIn(sid(MIRA, 31), self.posted_ids())
        # The old conflict stays held: it was sent once and is never retried.
        self.assertEqual((self.records(plan)[sid(GROUP, 2)], self.posted_ids().count(sid(GROUP, 2))), ('conflict', 1))
        self.assertEqual([(h['message_id'], h['state']) for h in self.view(plan)['held']], [('2', 'conflict')])

    async def test_refused_receipt_releases_the_frozen_record_once_on_resume(self):
        self.histories[MIRA] = [self.own(31)]
        self.receipt('auto:31', MIRA, 31, recorded=False)
        plan = self.plan(chats=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')
        frozen = self.j.db.execute('SELECT record FROM import_records WHERE plan=?', (plan,)).fetchone()[0]
        # The Host refuses the receipt for good, and Telegram now holds an edited revision.
        self.j.report_defer('auto:31', 'host_409', permanent=True)
        self.histories[MIRA][0] = message(31, MIRA, 'edited later', NOW - DAY + 31 * timedelta(minutes=1), out=True, edit_date=NOW - timedelta(hours=1))
        self.assertEqual(self.view(plan)['held'][0]['receipt'], 'retrying')
        reads = len(self.telegram.reads())
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(len(self.telegram.reads()), reads)  # nothing re-read from Telegram
        self.assertEqual(self.posted_ids(), [sid(MIRA, 31)])  # exactly one write
        sent = self.posts()[0]['records'][0]
        self.assertEqual(sent, json.loads(frozen))  # the frozen record, unchanged
        self.assertEqual(self.wire.rows[sid(MIRA, 31)]['event']['text'], 'Sure, I will bring them.')
        view = self.view(plan)
        self.assertEqual(view['released'], [{'chat': str(MIRA), 'state': 'recorded', 'released': 'receipt_refused', 'message_id': '31'}])
        result = json.loads(self.j.db.execute('SELECT result FROM import_records WHERE plan=?', (plan,)).fetchone()[0])
        self.assertEqual((result['released'], result['held_as'], result['custody']), ('receipt_refused', 'receipt_unconfirmed', 'history'))
        with self.assertRaisesRegex(ValueError, 'only a paused or partial import can resume'):
            self.act(plan, 'resume')

    async def test_unreadable_receipts_stay_held_until_readable_and_then_follow_the_proof(self):
        (self.root / 'transport.sqlite3').write_bytes(b'not a database')
        self.histories[MIRA] = [self.own(30), self.own(32)]
        plan = self.plan(chats=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'partial')
        self.assertEqual({h['reason'] for h in self.view(plan)['held']}, {'receipt_unreadable'})
        self.legacy([(30, 1, 'p30'), (32, 3, 'p32')])  # 30 recorded; 32 never reported (no receipt was negotiated)
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.records(plan), {sid(MIRA, 30): 'delivered', sid(MIRA, 32): 'recorded'})
        self.assertEqual(self.posted_ids(), [sid(MIRA, 32)])

    async def test_held_ids_beyond_the_listed_ones_are_counted(self):
        self.histories[MIRA] = [self.own(mid, f'reply {mid}') for mid in range(100, 160)]
        for mid in range(100, 160):
            self.receipt(f'auto:{mid}', MIRA, mid, recorded=False)
        plan = self.plan(chats=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')
        view = self.view(plan)
        self.assertEqual((len(view['held']), view['held_total'], view['held_omitted'], view['counts']), (50, 60, 10, {'residual': 60}))

    async def test_owner_changes_during_host_checks_stop_before_any_telegram_read(self):
        self.histories[GROUP] = [message(1, GROUP, 'g', NOW - DAY)]
        def cancel(method, path):
            if path == '/presence/place':
                with Journal(self.root) as other:  # the widget route, on its own connection
                    H.transition(other, ACCOUNT, plan, 'cancel', policy(self.root))
        def exclude(method, path):
            if path == '/presence/place':
                save_policy(self.root, {'blocked_peers': [GROUP]})
        def switch(method, path):
            if path == '/identity':
                save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=999))
        for change, after in ((cancel, 'cancelled'), (exclude, 'running'), (switch, 'running')):
            with self.subTest(change=change.__name__):
                plan = self.plan()
                self.act(plan, 'start')
                self.importer = H.HistoryImporter(self.engine, clock=self.clock)
                self.telegram.calls.clear()
                self.wire.on_call = change
                try:
                    self.clock.t += 700
                    await self.importer.once()
                    observed = (list(self.telegram.calls), self.state(plan))
                finally:  # each case starts from the same account, scope and no running import
                    self.wire.on_call = None
                    save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
                    save_policy(self.root, {'blocked_peers': []})
                    if self.state(plan) == 'running':
                        self.act(plan, 'cancel')
                self.assertEqual(observed, ([], after))

    async def test_owner_changes_during_the_host_check_before_a_send_govern_that_send(self):
        """Frozen records are sent after a Host check; what the owner changes while it runs stops the send.

        Pause, Cancel, another account or legacy mode: nothing is posted and the records stay pending.
        An exclusion: nothing is posted and the chat's pending records are withheld, as when the exclusion
        is seen before the check. Nothing changed: exactly the frozen records are posted.
        """
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        def owner(plan_id, action):
            with Journal(self.root) as other:  # the widget route, on its own connection
                H.transition(other, ACCOUNT, plan_id, action, policy(self.root))
        changes = {'unchanged': lambda plan_id: None,
                   'pause': lambda plan_id: owner(plan_id, 'pause'), 'cancel': lambda plan_id: owner(plan_id, 'cancel'),
                   'exclude': lambda plan_id: save_policy(self.root, {'blocked_peers': [GROUP]}),
                   'account': lambda plan_id: save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=999)),
                   'legacy': lambda plan_id: save_policy(self.root, {'mode': 'legacy'})}
        expected = {'pause': ('paused', {'pending'}, 0), 'cancel': ('cancelled', {'pending'}, 0), 'exclude': ('running', {'withheld'}, 0),
                    'account': ('running', {'pending'}, 0), 'legacy': ('running', {'pending'}, 0), 'unchanged': ('running', {'recorded'}, 1)}
        for name, change in changes.items():
            with self.subTest(change=name):
                plan = self.plan()
                self.act(plan, 'start')
                self.importer = H.HistoryImporter(self.engine, clock=self.clock)
                self.clock.t += 700; await self.importer.once()  # the page is read and frozen
                frozen = [json.loads(raw) for (raw,) in self.j.db.execute('SELECT record FROM import_records WHERE plan=? ORDER BY seq', (plan,))]
                posts, reads = len(self.wire.history_bodies), len(self.telegram.reads())
                self.wire.on_call = lambda method, path: change(plan) if path == '/identity' else None
                try:
                    self.clock.t += 700; await self.importer.once()  # the cached check has expired: the Host is asked again
                    observed = (self.state(plan), set(self.records(plan).values()), len(self.wire.history_bodies) - posts)
                    sent = [json.loads(body)['records'] for body in self.wire.history_bodies[posts:]]
                finally:  # each case starts from the same account, mode, scope and no running import
                    self.wire.on_call = None
                    save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
                    save_policy(self.root, {'blocked_peers': [], 'mode': 'participant'})
                    if self.state(plan) in ('running', 'paused'):
                        self.act(plan, 'cancel')
                self.assertEqual(observed, expected[name])
                self.assertEqual((sent, len(self.telegram.reads())), ([frozen] if name == 'unchanged' else [], reads))
                if name == 'exclude':
                    self.assertEqual([(c['state'], c['reason']) for c in self.view(plan)['chats']], [('skipped', 'outside_receiving_scope')])

    async def test_fake_holds_the_send_time_rules_of_the_contract(self):
        record = H.history_record(ACCOUNT, GROUP, None, message(1, GROUP, 'g', NOW - DAY))
        late = json.loads(json.dumps(record)); late['source_timestamp'] = (NOW + timedelta(seconds=1)).isoformat()
        late['event']['message']['sent_at'] = late['source_timestamp']
        other = json.loads(json.dumps(record)); other['source_timestamp'] = (NOW - 2 * DAY).isoformat()
        for bad in (late, other):
            status, answer = self.wire.history({'import_id': 'imp_x', 'records': [bad]}, None)
            self.assertEqual((status, answer['code'], answer['field']), (400, 'presence_history_invalid', 'records[0].source_timestamp'))
        self.assertEqual(self.wire.history({'import_id': 'imp_x', 'records': [record]}, None)[0], 200)


class ReadsFollowCurrentAuthority(Base):
    """Every Telegram read of a page starts only while the plan, account and scope still allow it.

    Forum message 62 answers 61 without naming its topic, so placing it takes the page's
    catalog read, one catalog refresh and one reply-ancestry read: each is a read of its own.
    """

    def setUp(self):
        super().setUp()
        Adapter.catalogs = {FORUM: {1, 40}}
        self.thanks = message(62, FORUM, 'Thanks', NOW - DAY + timedelta(minutes=1),
                              reply_to=T.MessageReplyHeader(forum_topic=True, reply_to_msg_id=61))
        self.histories[FORUM] = [message(61, FORUM, 'Rules are pinned', NOW - DAY, reply_to=T.MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=40, reply_to_top_id=40)), self.thanks]

    def kinds(self):
        return [call[0] for call in self.telegram.calls]

    async def test_unchanged_authority_imports_topics_found_by_refresh_and_ancestry(self):
        plan = self.plan(chats=(FORUM,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.kinds(), ['get_entity', 'get_messages', 'topic_ids', 'topic_ids', 'get_message'])
        self.assertEqual(sorted(self.posted_ids()), [sid(FORUM, 61, 40), sid(FORUM, 62, 40)])
        self.assertEqual(self.view(plan)['chats'][0]['skipped'], {})
        # Live capture places the same message in the same topic under the same ID.
        await self.engine.capture(self.live(self.thanks, FORUM, 809), 'new')
        self.assertEqual([r[0] for r in self.j.db.execute('SELECT id FROM inbox')], [sid(FORUM, 62, 40)])

    async def step_with_change(self, at, change):
        """One importer step of a new plan; ``change`` happens while Telegram answers the read ``at``."""
        plan = self.plan(chats=(FORUM,))
        self.act(plan, 'start')
        importer = H.HistoryImporter(self.engine, clock=self.clock)
        page, catalog, catalogs = self.telegram.get_messages, self.adapter.topic_ids, []
        async def reading(*args, **kw):
            answer = await page(*args, **kw)
            if at == 'page' and kw.get('ids') is None:
                change(plan)
            return answer
        async def cataloguing(peer, guard=None):
            answer = await catalog(peer, guard=guard)
            catalogs.append(peer)
            if at == 'refresh' and len(catalogs) == 2:
                change(plan)
            return answer
        self.telegram.get_messages, self.adapter.topic_ids = reading, cataloguing
        self.telegram.calls.clear()
        try:
            self.clock.t += 700
            await importer.once()
            return self.kinds(), self.state(plan), self.records(plan)
        finally:  # each case starts from the same account, scope and no running import
            del self.telegram.get_messages, self.adapter.topic_ids
            save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
            save_policy(self.root, {'blocked_peers': []})
            if self.state(plan) in ('running', 'paused'):
                self.act(plan, 'cancel')

    async def test_owner_changes_during_a_page_stop_the_next_telegram_read(self):
        def pause(plan):
            with Journal(self.root) as other:  # the widget route, on its own connection
                H.transition(other, ACCOUNT, plan, 'pause', policy(self.root))
        def exclude(plan):
            save_policy(self.root, {'blocked_peers': [FORUM]})
        def switch(plan):
            save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=999))
        page = ['get_entity', 'get_messages']
        for at, before in (('page', page), ('refresh', page + ['topic_ids', 'topic_ids'])):
            for change, after in ((pause, 'paused'), (exclude, 'running'), (switch, 'running')):
                with self.subTest(at=at, change=change.__name__):
                    # Nothing more is read after the change, and nothing of that page is kept.
                    self.assertEqual(await self.step_with_change(at, change), (before, after, {}))
        self.assertEqual(self.wire.history_bodies, [])


class LiveCustody(Base):
    """Live intake keeps a message only when the Host admitted it or live intake still hands it over."""

    def inbox(self):
        return {r['id']: r['state'] for r in self.j.db.execute('SELECT id,state FROM inbox')}

    async def test_capture_held_by_an_exclusion_is_imported_once_the_chat_is_received_again(self):
        held = message(70, GROUP, 'See you at ten', NOW - DAY)
        self.histories[GROUP] = [held]
        await self.engine.capture(self.live(held, GROUP, 809), 'new')
        save_policy(self.root, {'blocked_peers': [GROUP]})  # excluded before live intake handed it to the Host
        await self.engine.incoming_once()
        self.assertEqual(self.inbox(), {sid(GROUP, 70): 'held'})  # held for good: nothing hands it over later
        save_policy(self.root, {'blocked_peers': []})  # received again, and the owner imports those dates
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual((self.posted_ids(), self.records(plan)), ([sid(GROUP, 70)], {sid(GROUP, 70): 'recorded'}))
        self.assertEqual(self.view(plan)['chats'][0]['skipped'], {})
        self.assertNotIn(('POST', '/presence/turn'), self.wire.paths())
        self.assertEqual(self.wire.rows[sid(GROUP, 70)]['custody'], 'history')

    async def test_capture_the_host_refused_is_imported(self):
        refused = message(71, GROUP, 'Board night moved to Saturday', NOW - DAY)
        self.histories[GROUP] = [refused]
        self.wire.turn_refusal = (409, {'ok': False, 'code': 'place_not_connected', 'disposition': 'blocked',
                                        'error': 'Connect this place in Consciousness > Places.'})
        await self.engine.capture(self.live(refused, GROUP, 809), 'new')
        await self.engine.incoming_once()
        self.assertEqual(self.inbox(), {sid(GROUP, 71): 'held'})
        self.wire.turn_refusal = None  # the place is connected now
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.records(plan), {sid(GROUP, 71): 'recorded'})
        self.assertEqual(self.wire.rows[sid(GROUP, 71)]['custody'], 'history')

    async def test_admitted_or_still_arriving_captures_stay_with_live_intake(self):
        finished = message(72, GROUP, 'Admitted and finished', NOW - DAY)
        polled = message(73, MIRA, 'Admitted, then its work was refused', NOW - DAY)
        arriving = message(74, GROUP, 'Not handed over yet', NOW - DAY + timedelta(minutes=1))
        self.histories[GROUP], self.histories[MIRA] = [finished, arriving], [polled]
        await self.engine.capture(self.live(finished, GROUP, 809), 'new')
        await self.engine.incoming_once()  # admitted: the Host holds its live copy
        self.wire.turn_status = 'running'
        await self.engine.capture(self.live(polled, MIRA, MIRA), 'new')
        await self.engine.incoming_once()  # admitted, its work still running
        with self.j.db:
            self.j.db.execute('UPDATE inbox SET due=0')  # the three seconds before the next poll have passed
        await self.engine.incoming_once()  # the Host refuses the work reference: held, though admitted
        await self.engine.capture(self.live(arriving, GROUP, 809), 'new')  # not handed over yet
        self.assertEqual(self.inbox(), {sid(GROUP, 72): 'done', sid(MIRA, 73): 'held', sid(GROUP, 74): 'pending'})
        plan = self.plan(chats=(GROUP, MIRA))
        self.act(plan, 'start')
        # 74 is still live intake's: held back with its copy, never sent beside it and never counted as present.
        self.assertEqual(await self.drive(plan), 'partial')
        self.assertEqual(self.wire.history_bodies, [])
        self.assertEqual({c['id']: c['skipped'] for c in self.view(plan)['chats']},
                         {str(GROUP): {'live_captured': 1}, str(MIRA): {'live_captured': 1}})
        self.assertEqual([(h['message_id'], h['reason']) for h in self.view(plan)['held']], [('74', 'live_pending')])

    def legacy_edit(self, m):
        """An edit of a message in Mira's chat as 0.2 captured it, under its byte revision; returns its source ID."""
        revision = str(m.edit_date) + ':' + hashlib.sha256(bytes(m)).hexdigest()
        source = f'telegram:{ACCOUNT}:{MIRA}:0:{m.id}:edit:' + quote(revision, safe='')
        self.j.capture({'source_event_id': source,
                        'provider': 'telegram', 'account_id': str(ACCOUNT), 'conversation_id': str(MIRA), 'thread_id': '',
                        'conversation_key': f'telegram:{ACCOUNT}:{MIRA}:0', 'actor': {'platform_actor_id': str(MIRA)},
                        'conversation': {'title': 'Mira'}, 'text': m.message, 'facts': ['any_message', 'direct_message'],
                        'message': {'message_id': str(m.id), 'event_kind': 'edit', 'revision': revision}})
        return source

    async def test_a_pending_pre_upgrade_edit_capture_holds_the_message_until_live_intake_settles_it(self):
        m = message(82, MIRA, 'Meet at seven', NOW - DAY, edit_date=NOW - timedelta(hours=2))
        self.histories[MIRA] = [m]
        legacy = self.legacy_edit(m)  # pending: live intake has not handed it over yet
        plan = self.plan(chats=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')
        current = H.history_record(ACCOUNT, MIRA, None, m)['event']['source_event_id']
        result = json.loads(self.j.db.execute('SELECT result FROM import_records WHERE plan=?', (plan,)).fetchone()[0])
        self.assertEqual(result, {'reason': 'live_pending', 'live': [current, legacy]})  # rechecked by these IDs
        await self.engine.incoming_once()  # live intake hands the 0.2 capture to the Host, which admits it
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual((self.records(plan), self.wire.history_bodies), ({current: 'delivered'}, []))

    async def test_a_pre_upgrade_edit_capture_counts_only_when_admitted(self):
        edited = NOW - timedelta(hours=2)
        held = message(80, MIRA, 'Meet at six', NOW - DAY, edit_date=edited)
        admitted = message(81, MIRA, 'Bring the rules', NOW - DAY + timedelta(minutes=1), edit_date=edited)
        self.histories[MIRA] = [held, admitted]
        self.legacy_edit(held)
        self.legacy_edit(admitted)
        save_policy(self.root, {'blocked_peers': [MIRA]})
        await self.engine.incoming_once()  # 80 is held: its chat was excluded before live intake handed it over
        save_policy(self.root, {'blocked_peers': []})
        await self.engine.incoming_once()  # 81 is admitted
        self.assertEqual(sorted(self.inbox().values()), ['done', 'held'])
        plan = self.plan(chats=(MIRA,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        # The held edit is imported as its current revision, the ID live capture would give it now.
        self.assertEqual(self.posted_ids(), [H.history_record(ACCOUNT, MIRA, None, held)['event']['source_event_id']])
        self.assertEqual(self.view(plan)['chats'][0]['skipped'], {'live_captured': 1})

    async def test_admission_evidence_is_read_from_the_inbox_row_of_that_source(self):
        def row(mid, state, refs=(), answer=None):
            event = H.history_record(ACCOUNT, GROUP, None, message(mid, GROUP, f'm{mid}', NOW - DAY))['event']
            self.j.capture(event)
            seq = self.j.db.execute('SELECT seq FROM inbox WHERE id=?', (event['source_event_id'],)).fetchone()[0]
            if answer is not None:
                self.j.observe_inbox(seq, *answer)
            self.j.inbox_update(seq, state, list(refs))
            return event['source_event_id']
        cases = {row(1, 'done'): 'admitted',
                 row(2, 'held', ['turn-1']): 'admitted',  # accepted, then its work reference was refused
                 row(3, 'held', [], (200, {'ok': True, 'status': 'completed'})): 'admitted',  # accepted, then its outputs conflicted
                 row(4, 'pending', [], (200, {'ok': True, 'status': 'completed'})): 'admitted',
                 row(5, 'pending'): 'pending',
                 row(6, 'pending', [], (503, {'ok': False, 'disposition': 'retry'})): 'pending',
                 row(7, 'held'): 'held',  # its chat was excluded before it was handed over
                 row(8, 'held', [], (409, {'ok': False, 'disposition': 'blocked', 'code': 'place_not_connected'})): 'held'}
        for source, custody in cases.items():
            self.assertEqual(H.live_custody(self.j, [source]), custody, source)
        self.assertIsNone(H.live_custody(self.j, [sid(GROUP, 99)]))
        # Of several captures of one revision, the strongest evidence counts.
        self.assertEqual(H.live_custody(self.j, [sid(GROUP, 7), sid(GROUP, 5)]), 'pending')
        self.assertEqual(H.live_custody(self.j, [sid(GROUP, 7), sid(GROUP, 99), sid(GROUP, 1)]), 'admitted')


class CatalogPagesFollowCurrentAuthority(Base):
    """The real adapter reads a forum catalog page by page, and each page waits for the same check.

    The forum's first catalog has two pages, with topic 43 only on the second. Message 65 is
    in topic 44, which the catalog lists only from its refresh on, again on the second page.
    """

    def setUp(self):
        super().setUp()
        self.forum = Forum(self.entities, self.histories, {FORUM: [[41, 42], [43]]})
        adapter = TelegramAdapter(self.root / 'unused.session', 123, 'a' * 32, client=self.forum)
        self.engine = Participant(self.root, adapter, self.host, self.j, ACCOUNT)
        self.importer = H.HistoryImporter(self.engine, clock=self.clock)
        topic = lambda mid, tid, minutes: message(mid, FORUM, f'in topic {tid}', NOW - DAY + timedelta(minutes=minutes),
                                                  reply_to=T.MessageReplyHeader(forum_topic=True, reply_to_msg_id=tid, reply_to_top_id=tid))
        self.histories[FORUM] = [topic(63, 41, 1), topic(64, 43, 2), topic(65, 44, 3)]

    def pages(self, change=None, at=None, plan=None):
        """Topic 44 appears once the first catalog read is complete; ``change`` happens after page ``at``."""
        self.forum.pages[FORUM], self.forum.answered = [[41, 42], [43]], 0
        def answered(n):
            if n == 2:
                self.forum.pages[FORUM] = [[41, 42], [43, 44]]
            if n == at:
                change(plan)
        self.forum.on_page = answered

    def catalog_pages(self):
        return [call[2] for call in self.forum.calls if call[0] == 'GetForumTopics']

    async def test_unchanged_authority_reads_every_page_of_each_catalog(self):
        self.pages()
        plan = self.plan(chats=(FORUM,))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.catalog_pages(), [1, 2, 1, 2])  # the page's catalog, then the refresh message 65 needs
        self.assertEqual(sorted(self.posted_ids()), [sid(FORUM, 63, 41), sid(FORUM, 64, 43), sid(FORUM, 65, 44)])

    async def test_owner_changes_after_a_catalog_page_stop_the_next_page(self):
        def pause(plan):
            with Journal(self.root) as other:  # the widget route, on its own connection
                H.transition(other, ACCOUNT, plan, 'pause', policy(self.root))
        def exclude(plan):
            save_policy(self.root, {'blocked_peers': [FORUM]})
        def switch(plan):
            save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=999))
        # After page 1 of the page's own catalog, and after page 1 of the refresh.
        for at, before in ((1, [1]), (3, [1, 2, 1])):
            for change, after in ((pause, 'paused'), (exclude, 'running'), (switch, 'running')):
                with self.subTest(at=at, change=change.__name__):
                    plan = self.plan(chats=(FORUM,))
                    self.act(plan, 'start')
                    self.pages(change, at, plan)
                    self.forum.calls.clear()
                    try:
                        self.clock.t += 700
                        await H.HistoryImporter(self.engine, clock=self.clock).once()
                        observed = (self.catalog_pages(), self.state(plan), self.records(plan))
                    finally:  # each case starts from the same account, scope and no running import
                        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
                        save_policy(self.root, {'blocked_peers': []})
                        if self.state(plan) in ('running', 'paused'):
                            self.act(plan, 'cancel')
                    self.assertEqual(observed, (before, after, {}))
        self.assertEqual(self.wire.history_bodies, [])


class LiveIntakeStillHasIt(Base):
    """A message live intake still has in hand when its chat is read is held back with that copy.

    Only the owner's Resume decides it, from live intake's own records: admitted means it is
    in the conversation; still in hand keeps it held; let go without admission sends the copy
    saved at the read, under the run's current checks. Telegram is never read again for it.
    """

    def inbox(self):
        return {r['id']: r['state'] for r in self.j.db.execute('SELECT id,state FROM inbox')}

    async def test_resume_follows_live_intake_and_sends_the_saved_copy_once_it_lets_go(self):
        admitted = message(75, GROUP, 'Arrived live, admitted after the read', NOW - DAY)
        let_go = message(76, MIRA, 'Arrived live, held for good after the read', NOW - DAY)
        self.histories[GROUP], self.histories[MIRA] = [message(74, GROUP, 'Older', NOW - 2 * DAY), admitted], [let_go]
        await self.engine.capture(self.live(admitted, GROUP, 809), 'new')
        await self.engine.capture(self.live(let_go, MIRA, MIRA), 'new')
        plan = self.plan(chats=(GROUP, MIRA))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')  # never Done, never "already in the conversation"
        self.assertEqual(self.posted_ids(), [sid(GROUP, 74)])
        view = self.view(plan)
        self.assertEqual(sorted((h['message_id'], h['reason']) for h in view['held']), [('75', 'live_pending'), ('76', 'live_pending')])
        self.assertEqual(([c['skipped'] for c in view['chats']], view['counts']), ([{}, {}], {'recorded': 1, 'residual': 2}))
        frozen = self.j.db.execute('SELECT record FROM import_records WHERE plan=? AND source_event_id=?', (plan, sid(MIRA, 76))).fetchone()[0]
        posts, reads = len(self.wire.history_bodies), len(self.telegram.reads())
        # A Resume while live intake still has both keeps them held and writes nothing.
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'partial')
        self.assertEqual((len(self.wire.history_bodies), len(self.telegram.reads())), (posts, reads))
        # Live intake hands 75 to the Host, which admits it; 76's chat is excluded before intake reaches it.
        save_policy(self.root, {'blocked_peers': [MIRA]})
        await self.engine.incoming_once()
        save_policy(self.root, {'blocked_peers': []})
        self.assertEqual(self.inbox(), {sid(GROUP, 75): 'done', sid(MIRA, 76): 'held'})
        # Telegram meanwhile holds an edited revision of 76; the Resume sends the copy saved at the read.
        self.histories[MIRA][0] = message(76, MIRA, 'Edited after the read', NOW - DAY, edit_date=NOW - timedelta(hours=1))
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(len(self.telegram.reads()), reads)  # Telegram was not read again
        self.assertEqual(self.posted_ids(), [sid(GROUP, 74), sid(MIRA, 76)])  # 75 never: the Host has live intake's copy
        self.assertEqual(self.posts()[-1]['records'], [json.loads(frozen)])
        self.assertEqual(self.wire.rows[sid(MIRA, 76)]['event']['text'], 'Arrived live, held for good after the read')
        self.assertEqual(self.records(plan), {sid(GROUP, 74): 'recorded', sid(GROUP, 75): 'delivered', sid(MIRA, 76): 'recorded'})
        results = {r['source_event_id']: json.loads(r['result'])
                   for r in self.j.db.execute('SELECT source_event_id,result FROM import_records WHERE plan=?', (plan,))}
        self.assertEqual(results[sid(GROUP, 75)], {'reason': 'live_admitted'})
        self.assertEqual((results[sid(MIRA, 76)]['released'], results[sid(MIRA, 76)]['held_as']), ('live_held', 'live_pending'))
        self.assertEqual(self.view(plan)['released'], [{'chat': str(MIRA), 'state': 'recorded', 'released': 'live_held', 'message_id': '76'}])

    async def test_raw_updates_held_for_good_neither_hide_a_message_nor_stall_the_import(self):
        readable = message(90, GROUP, 'Captured before account IDs were recorded', NOW - DAY)
        unreadable = message(91, GROUP, 'Its retained bytes cannot be read', NOW - DAY + timedelta(minutes=1))
        arriving = message(92, GROUP, 'Still being received', NOW - DAY + timedelta(minutes=2))
        self.histories[GROUP] = [readable, unreadable, arriving]
        # Rows written before account IDs were recorded are held for good; nothing hydrates them.
        self.j.capture_raw('legacy-90', {'peer': GROUP, 'actor': 809, 'kind': 'new', 'private': False}, bytes(readable))
        self.j.capture_raw('legacy-91', {'peer': GROUP, 'actor': 809, 'kind': 'new', 'private': False}, b'not a Telegram update')
        self.assertEqual(self.j.raw_heads(ACCOUNT), [])
        self.j.capture_raw('live-92', {'account_id': str(ACCOUNT), 'peer': GROUP, 'actor': 809, 'kind': 'new', 'private': False},
                           bytes(arriving))
        self.assertEqual({r['id']: (r['state'], r['code']) for r in self.j.db.execute('SELECT id,state,code FROM raw_updates')},
                         {'legacy-90': ('held', 'capture_account_unknown'), 'legacy-91': ('held', 'capture_account_unknown'),
                          'live-92': ('pending', '')})
        plan = self.plan()
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'partial')  # 92 is still live intake's; nothing waits on 90 or 91
        self.assertEqual(sorted(self.posted_ids()), [sid(GROUP, 90), sid(GROUP, 91)])
        view = self.view(plan)
        self.assertEqual(([(h['message_id'], h['reason']) for h in view['held']], view['chats'][0]['skipped']), ([('92', 'live_pending')], {}))
        # Hydration then drops 92 without a capture, as it does for a chat excluded at that moment; Resume sends its saved copy.
        self.j.raw_done('live-92')
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.records(plan)[sid(GROUP, 92)], 'recorded')
        self.assertEqual(self.view(plan)['released'][0]['released'], 'live_absent')


class WireExamples(Base):
    async def test_every_request_is_contract_valid_and_examples_are_kept(self):
        """Requests the importer sends pass the contract's validation; examples go to the evidence folder."""
        photo = T.Photo(id=78, access_hash=1, file_reference=b'r', date=None, sizes=[], dc_id=1)
        self.histories[GROUP] = [message(1, GROUP, 'See you at ten', NOW - DAY, sender=333),
                                 message(2, GROUP, '', NOW - DAY, sender=333, media=T.MessageMediaPhoto(photo=photo)),
                                 message(3, GROUP, 'fixed', NOW - DAY, sender=333, edit_date=NOW - timedelta(hours=3))]
        self.histories[MIRA] = [message(4, MIRA, 'my earlier reply', NOW - DAY, out=True)]
        self.wire.seed_live(H.history_record(ACCOUNT, GROUP, None, message(1, GROUP, 'See you at 10', NOW - DAY, sender=333))['event'])
        self.wire.faults.append(('unwritable_at', 1, False))
        plan = self.plan(chats=(GROUP, MIRA))
        self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        answers = []
        for raw in self.wire.history_bodies:
            body = json.loads(raw)
            self.wire.validate(body)
            answers.append(body)
        self.assertEqual([len(b['records']) for b in answers], [3, 2, 1])
        self.assertEqual([status for status, _ in self.wire.history_answers], [503, 409, 200])
        evidence = os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR')
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            (Path(evidence) / 'history-wire-examples.json').write_text(json.dumps(
                {'note': 'Synthetic messages; requests exactly as the importer sent them to the in-memory contract-v1 Host fake, '
                         'with its answers. A wire fake, not the core Host.',
                 'exchanges': [{'request': request, 'http_status': status, 'answer': answer}
                               for request, (status, answer) in zip(answers, self.wire.history_answers)],
                 'records': self.records(plan), 'plan_view': self.view(plan)},
                ensure_ascii=False, indent=1, sort_keys=True))


class SourceOrder(Base):
    """Messages sent within one Telegram second keep their order: the Host orders them by ``message.source_order``."""

    async def test_messages_sent_within_one_second_carry_their_telegram_order(self):
        second = NOW - DAY
        self.histories[GROUP] = [message(300 + i, GROUP, f'album {i}', second, sender=811) for i in range(130)]
        plan = self.plan(); self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'done')
        posts = self.posts()
        self.assertEqual([len(body['records']) for body in posts], [100, 30])  # read and sent newest first
        facts = [r['event']['message'] for body in posts for r in body['records']]
        self.assertEqual([m['source_order'] for m in facts], list(range(429, 299, -1)))
        self.assertEqual(({m['sent_at'] for m in facts}, {m['source_order'] - int(m['message_id']) for m in facts}),
                         ({second.isoformat()}, {0}))
        # Within the one second the Host's rows read in send order, whatever order the pages arrived in.
        rows = sorted(self.stored().values(), key=lambda row: (row['ts'], row['order']))
        self.assertEqual([row['order'] for row in rows], list(range(300, 430)))

    async def test_a_host_without_source_order_reads_nothing_and_says_why(self):
        self.wire.orders = False  # it records history but predates source order
        self.histories[GROUP] = [message(1, GROUP, 'old', NOW - 2 * DAY)]
        plan = self.plan(); self.act(plan, 'start')
        self.assertEqual(await self.drive(plan), 'paused')
        self.assertEqual(self.view(plan)['reason'], 'host_source_order_missing')
        self.assertEqual((self.telegram.calls, self.wire.paths()), ([], [('GET', '/identity')]))
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'paused')
        self.assertEqual((self.telegram.calls, self.wire.history_bodies), ([], []))
        self.wire.orders = True  # the Host is updated
        self.act(plan, 'resume')
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.stored()[sid(GROUP, 1)]['order'], 1)
        cached = json.loads((self.root / 'host-capabilities.json').read_text())
        self.assertEqual((cached['presence_history_import_version'], cached['presence_source_order_version']), (1, 1))

    async def test_frozen_records_wait_while_the_host_no_longer_keeps_order(self):
        """The Host is replaced between a page's read and its send, and the companion is not restarted.

        The core before source order keeps source_order as display enrichment, so it would record the page
        without its order; a stricter Host would refuse it. Either way the records stay pending and unsent,
        the plan pauses with that reason (a Host without history at all says that instead), nothing is read
        from Telegram again, and once the Host keeps order a Resume sends exactly the frozen records.
        """
        self.histories[GROUP] = [message(i, GROUP, f'same second {i}', NOW - DAY) for i in range(1, 4)]
        plan = self.plan(); self.act(plan, 'start')
        self.clock.t += 700; await self.importer.once()  # read and frozen under a Host that keeps order
        frozen = [json.loads(raw) for (raw,) in self.j.db.execute('SELECT record FROM import_records WHERE plan=? ORDER BY seq', (plan,))]
        reads = len(self.telegram.reads())
        for supports, strict, reason in ((True, False, 'host_source_order_missing'), (True, True, 'host_source_order_missing'),
                                         (False, False, 'host_support_missing')):
            self.wire.supports, self.wire.orders, self.wire.strict = supports, False, strict
            self.clock.t += 700; await self.importer.once()
            self.assertEqual((self.state(plan), self.view(plan)['reason']), ('paused', reason), (supports, strict))
            self.assertEqual((self.wire.history_bodies, set(self.records(plan).values())), ([], {'pending'}))
            self.act(plan, 'resume')
        self.wire.supports = self.wire.orders = True
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual((self.posts()[0]['records'], len(self.telegram.reads())), (frozen, reads))
        self.assertEqual([row['order'] for row in sorted(self.stored().values(), key=lambda row: (row['ts'], row['order']))], [1, 2, 3])

    async def test_records_frozen_without_source_order_are_resent_unchanged(self):
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 3)]
        plan = self.plan(); self.act(plan, 'start')
        self.clock.t += 700; await self.importer.once()  # the page is read and frozen
        with self.j.db:  # as an earlier version froze it
            for seq, raw in self.j.db.execute('SELECT seq,record FROM import_records WHERE plan=?', (plan,)).fetchall():
                record = json.loads(raw); del record['event']['message']['source_order']
                self.j.db.execute('UPDATE import_records SET record=? WHERE plan=? AND seq=?',
                                  (json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(',', ':')), plan, seq))
        frozen = [json.loads(raw) for (raw,) in self.j.db.execute('SELECT record FROM import_records WHERE plan=? ORDER BY seq', (plan,))]
        self.assertEqual(await self.drive(plan), 'done')
        self.assertEqual(self.posts()[0]['records'], frozen)  # exactly what was frozen; no order is added or invented
        self.assertEqual({row['order'] for row in self.stored().values()}, {None})

    def test_only_participant_capture_and_history_name_the_order(self):
        self.assertNotIn('source_order', build_event(ACCOUNT, GROUP, 7, 811, 'x')['message'])  # retained legacy intake
        self.assertEqual(build_event(ACCOUNT, GROUP, 7, 811, 'x', message={'source_order': 99}, source_order=True)['message']['source_order'], 7)
        self.assertNotIn('source_order', build_event(ACCOUNT, GROUP, 7, 811, 'x', message={'source_order': 99})['message'])
        record = H.history_record(ACCOUNT, GROUP, None, message(7, GROUP, 'x', NOW - DAY))
        record['event']['message']['source_order'] = -1  # the fake holds the contract's range
        with self.assertRaisesRegex(ValueError, r'0\.\.2\^53-1'):
            self.wire.validate({'import_id': 'i', 'records': [record]})


class OwnerControlsThroughTheRoute(Base):
    """The registered import route and the real importer on one journal, as the widget and the companion share it."""

    def setUp(self):
        super().setUp()
        self.route = Routes(self.root)

    def started(self, mode):
        """What a companion's start leaves behind: the record of its Host check, which each mode writes for itself."""
        if mode == 'participant':
            record_host_facts(self.root, self.host.identity())  # participant.serve, once it holds the session lock
            mine, other = self.root / 'host-capabilities.json', self.root / 'presence-capability.json'
        else:
            _write_json(self.root, 'presence-capability.json', {'delivery_reporting_version': 1, 'status': 'available'})  # _serve_legacy
            mine, other = self.root / 'presence-capability.json', self.root / 'host-capabilities.json'
        # A start comes after the previous companion's last record, even where file times are coarse.
        written = mine.stat().st_mtime_ns
        if other.exists() and other.stat().st_mtime_ns >= written:
            os.utime(other, ns=(written - 10**9, written - 10**9))

    async def listing(self):
        code, body = await self.route('participant/import')
        self.assertEqual(code, 200, body)
        return body

    async def owner(self, action, plan_id=None, **plan):
        return await self.route('participant/import', {'action': action, **({'id': plan_id} if plan_id else plan)})

    async def mode(self, value):
        code, body = await self.route('participant/policy', {'mode': value})
        self.assertEqual(code, 200, body)

    def legacy_companion(self):
        """A live legacy companion in its own process: the real session lock held as companion._serve holds it,
        and under it the start record _serve_legacy writes. It runs until the returned stop is called."""
        script = textwrap.dedent('''
            import sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from userbot.config import _write_json
            from userbot.participant import session_lock
            root = Path(sys.argv[2])
            with session_lock(root):
                _write_json(root, 'presence-capability.json', {'delivery_reporting_version': 1, 'status': 'available'})
                print('serving', flush=True)
                sys.stdin.read()
        ''')
        companion = subprocess.Popen([sys.executable, '-c', script, str(ROOT), str(self.root)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def stop():
            if companion.poll() is None:
                companion.stdin.close()
                try:
                    companion.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    companion.kill(); companion.wait()
            companion.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(companion.stdout.readline().strip(), 'serving')
        return stop

    async def participant_start(self):
        """participant.serve as companion._serve runs it in Free participant mode, over the Host fake and a
        synthetic adapter, so nothing reaches Telegram. It returns once the companion reports itself running."""
        class Running(Exception):
            pass
        class Client:
            def add_event_handler(self, *args): pass
            async def catch_up(self): pass
            async def get_me(self): return NS(id=ACCOUNT, username='ouro_demo', first_name='Ouroboros', last_name='AI')
        class Adapter:
            client = Client()
            async def connect(self): pass
            async def get_me(self): return ACCOUNT
            async def disconnect(self): pass
        def adapter(*args):
            self.adapters.append(Adapter())
            return self.adapters[-1]
        async def login(*args):
            return True
        def health(root, category):
            if category == 'running':
                raise Running()
        real = participant.Host
        with mock.patch.object(participant, 'Host', lambda url, token: real(url, token, transport=self.wire.transport)):
            try:
                await participant.serve(self.root, 'http://127.0.0.1:9', PresenceToken('skill-token'), adapter, login,
                                        lambda root, cfg, account: cfg, health)
            except Running:
                pass

    async def test_a_participant_start_refused_by_a_live_legacy_companion_records_nothing(self):
        """Only the companion that owns the session lock records the Host check of its start.

        A legacy companion holds the real session lock and has recorded its start. A participant start
        that this lock refuses leaves the participant record as it was, so the route keeps Start and
        Resume closed. Once the legacy companion has gone, a participant start records its Host check,
        pauses what ran before it, and Start and Resume work again.
        """
        self.adapters = []
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        self.started('participant')  # an earlier companion in Free participant mode
        left, paused, saved = self.plan(chats=(OTHER,)), self.plan(), self.plan(chats=(MIRA,))
        self.act(paused, 'start'); self.act(paused, 'pause'); self.act(left, 'start')
        # The skill went back to legacy and its companion now runs; the owner has chosen Free participant again.
        stop_legacy = self.legacy_companion()
        record, legacy = self.root / 'host-capabilities.json', self.root / 'presence-capability.json'
        if record.stat().st_mtime_ns >= legacy.stat().st_mtime_ns:  # file times may be coarse
            os.utime(record, ns=(legacy.stat().st_mtime_ns - 10**9,) * 2)
        before = (record.read_bytes(), record.stat().st_mtime_ns)
        body = await self.listing()
        self.assertEqual((body['available'], body['reason']), (False, 'restart_required'))
        # A participant start beside it: the lock refuses it before any Telegram connection, and it records nothing.
        with self.assertRaises(BlockingIOError):
            await self.participant_start()
        self.assertEqual(self.adapters, [])
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), before)
        body = await self.listing()
        self.assertEqual((body['available'], body['reason']), (False, 'restart_required'))
        for action, plan_id, state in (('start', saved, 'saved'), ('resume', paused, 'paused')):
            code, answer = await self.owner(action, plan_id)
            self.assertEqual((code, answer['code'], self.state(plan_id)), (409, 'restart_required', state), action)
        # The legacy companion stops and the kernel releases its lock: now a participant start owns the session.
        stop_legacy()
        await self.participant_start()
        self.assertEqual(len(self.adapters), 1)
        self.assertGreater(record.stat().st_mtime_ns, legacy.stat().st_mtime_ns)
        self.assertEqual(json.loads(record.read_text())['presence_source_order_version'], 1)
        self.assertEqual((self.state(left), self.view(left)['reason']), ('paused', 'interrupted'))
        body = await self.listing()
        self.assertEqual((body['available'], body['reason']), (True, ''))
        self.assertEqual((await self.owner('resume', paused))[0], 200)
        self.assertEqual(await self.drive(paused), 'done')
        self.assertEqual(sorted(self.stored()), sorted(sid(GROUP, i) for i in range(1, 4)))
        self.assertEqual((await self.owner('start', saved))[0], 200)

    async def test_an_import_older_than_twenty_newer_plans_keeps_its_controls(self):
        self.started('participant')
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        self.histories[MIRA] = [message(5, MIRA, 'my earlier reply', NOW - DAY, out=True)]
        self.receipt('auto:5', MIRA, 5, recorded=False)  # still being reported: the message is held back
        done = self.plan(chats=(OTHER,)); self.act(done, 'start')
        self.assertEqual(await self.drive(done), 'done')
        partial = self.plan(chats=(MIRA,)); self.act(partial, 'start')
        self.assertEqual(await self.drive(partial), 'partial')
        paused = self.plan(chats=(OTHER,)); self.act(paused, 'start'); self.act(paused, 'pause')
        running = self.plan(); self.act(running, 'start')
        newer = [self.plan(chats=(OTHER,)) for _ in range(20)]
        body = await self.listing()
        # The 20 newest, then every older plan an owner action still waits on; only the finished one is left out.
        self.assertEqual([p['id'] for p in body['plans']], newer[::-1] + [running, paused, partial])
        self.assertEqual(([p['state'] for p in body['plans'][20:]], body['older_plans']), (['running', 'paused', 'partial'], 1))
        # The worker goes on with the old plan, and the listing shows it doing so.
        self.clock.t += 700; await self.importer.once()
        self.assertEqual([c[0] for c in self.telegram.calls[-2:]], ['get_entity', 'get_messages'])
        shown = next(p for p in (await self.listing())['plans'] if p['id'] == running)
        self.assertEqual((shown['state'], shown['chats'][0]['read'], shown['counts']), ('running', 3, {'pending': 3}))
        code, body = await self.owner('start', newer[-1])
        self.assertEqual((code, body['message']), (400, 'another import is running; pause or cancel it first'))
        # Pause and Resume through the route reach the plan the refusal points to.
        self.assertEqual((await self.owner('pause', running))[0], 200)
        calls = len(self.telegram.calls) + len(self.wire.calls)
        self.clock.t += 700
        self.assertFalse(await self.importer.once())
        self.assertEqual(len(self.telegram.calls) + len(self.wire.calls), calls)
        self.assertEqual([p['state'] for p in (await self.listing())['plans'] if p['id'] == running], ['paused'])
        self.assertEqual((await self.owner('resume', running))[0], 200)
        self.clock.t += 700; await self.importer.once()  # the frozen page goes to the Host
        self.assertEqual(self.records(running), {sid(GROUP, i): 'recorded' for i in range(1, 4)})
        # Cancel ends it; finished and older than the newest 20, it leaves the list and is counted instead.
        self.assertEqual((await self.owner('cancel', running))[0], 200)
        calls = len(self.telegram.calls) + len(self.wire.calls)
        self.clock.t += 700
        self.assertFalse(await self.importer.once())
        self.assertEqual(len(self.telegram.calls) + len(self.wire.calls), calls)
        body = await self.listing()
        self.assertEqual(([p['id'] for p in body['plans']][20:], body['older_plans']), ([paused, partial], 2))
        # Nothing runs now, so the newest plan starts, and the paused one resumes after it.
        self.assertEqual((await self.owner('start', newer[-1]))[0], 200)
        self.assertEqual(await self.drive(newer[-1]), 'done')
        self.assertEqual((await self.owner('resume', paused))[0], 200)
        self.assertEqual(await self.drive(paused), 'done')
        body = await self.listing()
        self.assertEqual(([p['id'] for p in body['plans']][20:], body['older_plans']), ([partial], 3))

    async def test_start_and_resume_wait_for_a_companion_started_in_participant_mode(self):
        """Only a participant-mode companion runs imports, and a saved mode starts only with the next companion."""
        self.histories[GROUP] = [message(i, GROUP, f'g{i}', NOW - DAY + i * timedelta(minutes=1)) for i in range(1, 4)]
        self.started('participant')
        paused, running, saved = self.plan(), self.plan(chats=(MIRA,)), self.plan(chats=(OTHER,))
        self.assertEqual((await self.owner('start', paused))[0], 200)
        self.clock.t += 700; await self.importer.once()
        self.assertEqual((await self.owner('pause', paused))[0], 200)
        self.assertEqual((await self.owner('start', running))[0], 200)
        # The owner saves legacy mode: this participant companion stops at its next authority check and reads nothing.
        await self.mode('legacy')
        calls = len(self.telegram.calls) + len(self.wire.calls)
        self.clock.t += 700
        self.assertFalse(await self.importer.once())
        self.assertEqual(len(self.telegram.calls) + len(self.wire.calls), calls)
        self.started('legacy')  # its restart dispatches to legacy, which has no importer
        body = await self.listing()
        # The Host support recorded by the earlier participant start is kept as a fact, but nothing can run.
        self.assertEqual((body['available'], body['host_supports_import'], body.get('mode'), body['reason']),
                         (False, True, 'legacy', 'participant_mode_required'))
        self.assertEqual({p['id']: p['state'] for p in body['plans']}, {paused: 'paused', running: 'running', saved: 'saved'})
        for action, plan_id, state in (('start', saved, 'saved'), ('resume', paused, 'paused')):
            code, answer = await self.owner(action, plan_id)
            self.assertEqual((code, answer['code']), (409, 'participant_mode_required'), action)
            self.assertIn('turned off and on', answer['message'])
            self.assertEqual(self.state(plan_id), state)
        code, answer = await self.owner('plan', chats=[str(GROUP)], exclude=[], **{'from': (NOW - 30 * DAY).isoformat(), 'to': NOW.isoformat()})
        self.assertEqual(code, 200, answer)
        self.assertIn('until the companion runs in Free participant mode', answer['message'])
        later = answer['id']
        self.assertEqual((await self.owner('pause', running))[0], 200)
        self.assertEqual((await self.owner('cancel', saved))[0], 200)
        self.assertEqual((self.state(running), self.state(saved)), ('paused', 'cancelled'))
        # Free participant saved again: the companion that runs is still the legacy one until the skill restarts.
        await self.mode('participant')
        body = await self.listing()
        self.assertEqual((body['available'], body['mode'], body['reason']), (False, 'participant', 'restart_required'))
        for action, plan_id in (('start', later), ('resume', paused)):
            code, answer = await self.owner(action, plan_id)
            self.assertEqual((code, answer['code']), (409, 'restart_required'), action)
        self.assertEqual((self.state(later), self.state(paused)), ('saved', 'paused'))
        self.assertEqual((self.telegram.calls[-1][0], len(self.telegram.calls) + len(self.wire.calls)), ('get_messages', calls))
        # The skill is turned off and on: a participant companion starts and records its own Host check.
        self.started('participant')
        H.interrupt_running(self.j)
        self.importer = H.HistoryImporter(Participant(self.root, self.adapter, self.host, self.j, ACCOUNT), clock=self.clock)
        body = await self.listing()
        self.assertEqual((body['available'], body['reason']), (True, ''))
        self.assertEqual((await self.owner('resume', paused))[0], 200)
        self.assertEqual(await self.drive(paused), 'done')
        self.assertEqual(sorted(self.stored()), sorted(sid(GROUP, i) for i in range(1, 4)))
        self.assertEqual((await self.owner('start', later))[0], 200)
        self.assertEqual(await self.drive(later), 'done')


if __name__ == '__main__':
    unittest.main()
