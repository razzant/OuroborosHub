"""Owner-selected earlier history: an explicit, resumable and cancellable one-off import.

Old messages enter canonical conversation history only through the Host's history-only
ingress, ``POST /presence/history``, advertised as ``presence_history_import_version: 1``
in ``/identity``. A history record wakes nothing and creates no task, unread mark or
reply. Without that capability nothing is read from Telegram, and an import never falls
back to ``/presence/turn`` or ``/chat/inject``: those treat old messages as new arrivals.
The Host must also advertise ``presence_source_order_version: 1``: it then orders a
conversation by each message's ``source_order`` as well as its send time, so messages
sent within one second keep their order. A Host without it could store them out of order,
so nothing is read or sent for it either, frozen records included.

A record carries the identity live capture gives the same message revision (one
``build_event`` and the shared normalization, with the same ``sent_at`` and
``source_order``), so the Host matches an imported and a
live copy of one revision, and a genuine later edit stays a new live event. Each page
read from Telegram is frozen in the journal before anything is sent; a retry resends
exactly those records, never a fresh read that may hold a different revision. Nothing
is read while records wait for the Host's confirmation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode

from .normalize import actor_facts, canonical_text, chat_facts, content_revision, iso, media_descriptor
from .participant_state import receiving_refusal
from .presence import build_event

MAX_PER_CHAT = 5000
MAX_WINDOW = timedelta(days=3660)
PAGE = 100  # one Telegram history page, and at most one Host request of records
READ_GAP = 3.0  # seconds between history pages; live intake and outbound work share the account
POST_GAP = 2.0  # seconds between history requests; the Host's lane allows 60 a minute
BACKOFF = (30, 60, 120, 300, 600)
# Consecutive answers outside the contract before the import stops for the owner.
INVALID_ANSWERS = 5
# Records that are final for this plan; 'pending' records are resent unchanged, and a
# 'residual' record waits for the owner's Resume: an own message for its delivery receipt,
# a message live intake still had in hand at the read for live intake to settle it.
SETTLED = ('recorded', 'duplicate', 'delivered', 'conflict', 'invalid', 'withheld', 'rejected')
HELD = ('residual', 'conflict', 'invalid', 'withheld', 'rejected')
LISTED = 50  # message IDs listed per plan; the totals are always given
# Started and not finished: listed however old, so their Pause, Resume and Cancel stay at hand.
UNFINISHED = ('running', 'paused', 'partial')
_ACTIONS = {'start': ({'saved'}, 'running', 'only a saved plan can start'),
            'pause': ({'running'}, 'paused', 'only a running import can pause'),
            'resume': ({'paused', 'partial'}, 'running', 'only a paused or partial import can resume'),
            'cancel': ({'saved', 'running', 'paused', 'partial'}, 'cancelled', 'this plan is already finished')}
_MESSAGES = {
    'start': 'Import started. Old messages are recorded with their authors and dates; they wake no one and get no replies.',
    'pause': 'Import paused. Nothing more is read or sent until you resume it.',
    'resume': 'Import resumed where it stopped.',
    'cancel': 'Import cancelled. Messages already recorded stay in the conversation history; nothing more is read or sent.'}
_WAITING = ('wait_until', 'wait_reason', 'attempts', 'invalid_answers')


def host_supports_import(capabilities):
    return type(capabilities) is dict and capabilities.get('presence_history_import_version') == 1


def host_keeps_source_order(capabilities):
    return type(capabilities) is dict and capabilities.get('presence_source_order_version') == 1


def _when(value, name):
    if type(value) is not str or len(value) > 40:
        raise ValueError(name + ' must be an ISO 8601 time with a time zone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(name + ' must be an ISO 8601 time with a time zone') from None
    if parsed.utcoffset() is None:
        raise ValueError(name + ' must be an ISO 8601 time with a time zone')
    return parsed.astimezone(timezone.utc)


def _peers(value, name):
    if type(value) is not list or len(value) > 1000:
        raise ValueError(name + ' must be a list of numeric chat IDs')
    peers = []
    for item in value:
        peer = int(item) if type(item) is str and item.lstrip('-').isdigit() else item
        if type(peer) is not int or peer == 0:
            raise ValueError(name + ' must be a list of numeric chat IDs')
        peers.append(peer)
    return sorted(set(peers))


def validate_plan(body, row, account, now=None):
    """Normalize one owner request. Receiving-scope exclusions always apply.

    Chats in ``exclude`` are never imported, whether the plan takes all receiving chats or
    names chosen ones.
    """
    if type(body) is not dict or set(body) - {'chats', 'exclude', 'from', 'to', 'max_per_chat'}:
        raise ValueError('import plan fields are chats, exclude, from, to and max_per_chat')
    now = now or datetime.now(timezone.utc)
    start, end = _when(body.get('from'), 'from'), _when(body.get('to'), 'to')
    if not start < end or end > now + timedelta(days=1) or end - start > MAX_WINDOW:
        raise ValueError('from must be before to, within ten years, and not in the future')
    cap = body.get('max_per_chat', 500)
    if type(cap) is not int or not 1 <= cap <= MAX_PER_CHAT:
        raise ValueError('max_per_chat must be 1..%d' % MAX_PER_CHAT)
    exclude = _peers(body.get('exclude', []), 'exclude')
    chats = body.get('chats', 'receiving')
    if chats != 'receiving':
        chats = _peers(chats, 'chats')
        if not chats:
            raise ValueError('choose at least one chat or all receiving chats')
        chosen = [peer for peer in chats if peer not in exclude]
        if not chosen:
            raise ValueError('every chosen chat is also left out')
        if int(account) in chosen:
            raise ValueError('Saved Messages hold this account\'s own uploads and are not imported')
        outside = [peer for peer in chosen if receiving_refusal(row, peer, account)]
        if outside:
            raise ValueError('chats outside the receiving scope cannot be imported: ' + ','.join(map(str, outside)))
    return {'chats': chats, 'exclude': exclude, 'from': start.isoformat(), 'to': end.isoformat(), 'max_per_chat': cap}


def save_plan(journal, account, spec):
    plan_id = 'imp_' + secrets.token_hex(8)
    now = time.time()
    with journal.db:
        journal.db.execute("INSERT INTO imports(id,account,spec,state,progress,created,updated) VALUES(?,?,?,?,?,?,?)",
                           (plan_id, str(account), json.dumps(spec, sort_keys=True), 'saved', '{}', now, now))
    return plan_id


def _first_run(journal, account, spec, row_policy, now):
    """Fix the chats and the upper bound when the owner presses Start.

    Messages sent after that moment are live capture's, never history. "All receiving
    chats" are the listed chats received now; chats the plan leaves out are never read,
    whether it takes all receiving chats or names chosen ones.
    """
    start, until = _when(spec['from'], 'from'), min(_when(spec['to'], 'to'), now)
    if until <= start:
        raise ValueError('nothing to import yet: the chosen dates begin after now')
    left_out = set(spec['exclude'])
    if spec['chats'] == 'receiving':
        peers = [r['peer'] for r in journal.chats(account, limit=1000) if r['peer'] not in left_out
                 and r['peer'] != int(account) and receiving_refusal(row_policy, r['peer'], account) is None]
        if not peers:
            raise ValueError('no receiving chat is listed yet; refresh the chat list on What I hear')
    else:
        peers = [peer for peer in spec['chats'] if peer not in left_out]
        if not peers:
            raise ValueError('every chosen chat is also left out')
    return {'run': 1, 'started_at': now.isoformat(), 'until': until.isoformat(),
            'chats': [{'peer': peer, 'state': 'pending', 'offset_id': 0, 'read': 0, 'skipped': {}} for peer in peers]}


def transition(journal, account, plan_id, action, row_policy, now=None):
    """Apply one owner action; a plan never starts or resumes by itself."""
    if action not in _ACTIONS:
        raise ValueError('expected start, pause, resume or cancel')
    allowed, target, refusal = _ACTIONS[action]
    with journal.db:
        journal.db.execute('BEGIN IMMEDIATE')
        row = journal.db.execute("SELECT state,spec,progress FROM imports WHERE account=? AND id=?",
                                 (str(account), str(plan_id))).fetchone()
        if row is None:
            raise ValueError('no import plan with that id for this account')
        if row['state'] not in allowed:
            raise ValueError(refusal)
        if target == 'running' and journal.db.execute(
                "SELECT 1 FROM imports WHERE account=? AND state='running' AND id!=?", (str(account), str(plan_id))).fetchone():
            raise ValueError('another import is running; pause or cancel it first')
        progress = json.loads(row['progress'])
        if action == 'start':
            progress = _first_run(journal, account, json.loads(row['spec']), row_policy, now or datetime.now(timezone.utc))
        else:
            for key in _WAITING + ('reason', 'detail'):
                progress.pop(key, None)
            if action == 'resume':
                progress['run'] = int(progress.get('run', 0)) + 1
            else:
                progress['reason'] = 'owner'
            if action == 'cancel':
                progress['finished_at'] = (now or datetime.now(timezone.utc)).isoformat()
        journal.db.execute("UPDATE imports SET state=?, progress=?, updated=? WHERE id=?",
                           (target, json.dumps(progress, sort_keys=True), time.time(), str(plan_id)))
    return _MESSAGES[action]


def interrupt_running(journal):
    """At companion start: an import that was running waits for the owner's Resume."""
    with journal.db:
        journal.db.execute('BEGIN IMMEDIATE')
        for row in journal.db.execute("SELECT id,progress FROM imports WHERE state='running'").fetchall():
            progress = json.loads(row['progress'])
            for key in _WAITING:
                progress.pop(key, None)
            progress['reason'] = 'interrupted'
            journal.db.execute("UPDATE imports SET state='paused', progress=?, updated=? WHERE id=?",
                               (json.dumps(progress, sort_keys=True), time.time(), row['id']))


def _counted(journal, plan_id):
    """One plan's records counted in a single pass, with the first listed held and released IDs."""
    counts, per_chat, released_total = {}, {}, 0
    for peer, state, number, released in journal.db.execute(
            "SELECT peer,state,count(*),count(json_extract(result,'$.released')) FROM import_records WHERE plan=? GROUP BY peer,state",
            (plan_id,)):
        counts[state] = counts.get(state, 0) + number
        per_chat.setdefault(peer, {})[state] = number
        released_total += released
    held_total = sum(counts.get(state, 0) for state in HELD)
    held, released = [], []
    if held_total:
        for peer, state, record, result in journal.db.execute(
                "SELECT peer,state,record,result FROM import_records WHERE plan=? AND state IN (%s) ORDER BY seq LIMIT ?"
                % ','.join('?' * len(HELD)), (plan_id, *HELD, LISTED)):
            result = json.loads(result)
            held.append({'chat': str(peer), 'state': state, 'reason': result.get('reason') or result.get('code', ''),
                         'receipt': result.get('receipt', ''), 'message_id': json.loads(record)['event']['message']['message_id']})
    if released_total:
        # Own messages a Resume sent because their receipt no longer stood for a delivery, in whatever state they reached.
        released = [{'chat': str(peer), 'state': state, 'released': json.loads(result)['released'],
                     'message_id': json.loads(record)['event']['message']['message_id']}
                    for peer, state, record, result in journal.db.execute(
                        "SELECT peer,state,record,result FROM import_records WHERE plan=? AND json_extract(result,'$.released') IS NOT NULL "
                        "ORDER BY seq LIMIT ?", (plan_id, LISTED))]
    return {'counts': counts, 'per_chat': per_chat, 'held': held, 'held_total': held_total,
            'released': released, 'released_total': released_total}


def plans(journal, account, limit=20, counted=None):
    """The owner's view of each plan: chats, counts by outcome and every held message ID.

    The ``limit`` newest plans are listed, newest first, and with them every older plan that
    was started and has not finished, so an import never runs, waits or holds messages out
    of the owner's sight. ``plan_count`` minus this list's length is what it leaves out:
    older plans that are saved, done or cancelled.

    ``counted`` is a dict the caller keeps between calls, such as the widget's polls. It
    holds each plan's counted records under the plan's ``updated`` stamp, which every write
    to those records moves in its own transaction, so a plan whose stamp did not move is
    not counted again. Without it every plan is counted.
    """
    rows = journal.db.execute(
        "SELECT id,spec,state,progress,created,updated FROM imports WHERE account=? AND (state IN (%s) "
        "OR id IN (SELECT id FROM imports WHERE account=? ORDER BY created DESC LIMIT ?)) ORDER BY created DESC"
        % ','.join('?' * len(UNFINISHED)), (str(account), *UNFINISHED, str(account), limit)).fetchall()
    now = time.time()
    found = []
    for r in rows:
        progress = json.loads(r['progress'])
        known = counted.get(r['id']) if counted is not None else None
        if known is None or known[0] != r['updated']:
            # The stamp was read first: a write landing meanwhile moves it, and the next call counts again.
            known = (r['updated'], _counted(journal, r['id']))
            if counted is not None:
                counted[r['id']] = known
        s = known[1]
        view = {'id': r['id'], 'spec': json.loads(r['spec']), 'state': r['state'], 'counts': dict(s['counts']),
                'held': list(s['held']), 'held_total': s['held_total'], 'held_omitted': s['held_total'] - len(s['held']),
                'released': list(s['released']), 'released_total': s['released_total'],
                'released_omitted': s['released_total'] - len(s['released']),
                'reason': progress.get('reason', ''), 'detail': progress.get('detail', {}),
                'started_at': progress.get('started_at', ''), 'until': progress.get('until', ''),
                'finished_at': progress.get('finished_at', ''),
                'created_unix': r['created'], 'updated_unix': r['updated'],
                'chats': [{'id': str(c['peer']), 'state': c['state'], 'reason': c.get('reason', ''), 'read': c.get('read', 0),
                           'truncated': bool(c.get('truncated')), 'oldest': c.get('oldest') or '',
                           'skipped': c.get('skipped', {}), 'counts': dict(s['per_chat'].get(c['peer'], {}))}
                          for c in progress.get('chats', [])]}
        if r['state'] == 'running' and progress.get('wait_until', 0) > now:
            view['waiting'] = {'reason': progress.get('wait_reason', ''), 'until_unix': progress['wait_until']}
        found.append(view)
    if counted is not None:
        for gone in set(counted) - {r['id'] for r in rows}:
            counted.pop(gone, None)
    return found


def plan_count(journal, account):
    return journal.db.execute("SELECT count(*) FROM imports WHERE account=?", (str(account),)).fetchone()[0]


def history_record(account, peer, topic, message, sender=None, conversation=None):
    """One earlier message as a history record whose event is exactly what live capture builds.

    An unedited message keeps its plain ID; an edited one is its current revision (its
    original text is unknown and not invented), with the original send time as the
    record's time. The account's own past messages are authored by that platform
    account, never by the owner or current Ouroboros. Media is described, not staged.
    """
    own = bool(getattr(message, 'out', False))
    actor_id = int(account) if own else int(getattr(message, 'sender_id', None) or peer)
    edited = getattr(message, 'edit_date', None) is not None
    media = media_descriptor(message)
    if media is not None:
        media['status'] = 'not_downloaded'
    facts = {'sent_at': iso(getattr(message, 'date', None)),
             'reply_to_message_id': getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None)}
    if edited:
        facts['edited_at'] = iso(message.edit_date)
    if media is not None:
        facts['media'] = media
    event = build_event(int(account), int(peer), message.id, actor_id, canonical_text(message), topic_id=topic,
                        event_kind='edit' if edited else 'new',
                        revision=content_revision(message) if edited else '',
                        actor=actor_facts(sender), conversation=conversation, message=facts, source_order=True)
    return {'event': event, 'authorship': 'platform_account' if own else 'participant',
            'source_timestamp': facts['sent_at'], 'staged_files': []}


_PROOF = {'recorded': 3, 'retrying': 2, 'refused': 1}


def delivery_receipts(journal, root, account, peer):
    """What this connector's own journals prove about messages it sent to ``peer``.

    Maps a provider message ID to ``recorded`` when the Host confirmed its delivery
    receipt, so the message is already in the room; ``retrying`` while a report is
    still being sent and may yet be recorded; ``refused`` when the Host refused the
    receipt or none was ever sent, so it cannot stand for a delivery in the room.
    ``None`` means a retained legacy store could not be read, so no outgoing message
    of this chat is provably new.
    """
    found = {}
    def note(key, proof):
        if _PROOF[proof] > _PROOF.get(found.get(key), 0):
            found[key] = proof
    for message_id, state in journal.db.execute(
            "SELECT json_extract(body,'$.message.message_id'),state FROM reports "
            "WHERE CAST(json_extract(body,'$.account_id') AS TEXT)=? AND CAST(json_extract(body,'$.conversation_id') AS TEXT)=?",
            (str(account), str(peer))):
        if str(message_id).isdigit():
            # A report the Host refused with 400 or 409 is held and never sent again.
            note(int(message_id), {'recorded': 'recorded', 'held': 'refused'}.get(state, 'retrying'))
    legacy = Path(root) / 'transport.sqlite3'
    if not legacy.is_file():
        return found
    try:
        db = sqlite3.connect(str(legacy), timeout=5)
        try:
            rows = db.execute("SELECT remote_message_id,reported,payload_ref FROM outgoing WHERE account_id=? AND peer_id=? "
                              "AND status='sent' AND remote_message_id IS NOT NULL", (str(account), int(peer))).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    for message_id, reported, payload_ref in rows:
        # The legacy reporter writes this receipt file only after the Host recorded it. Rows
        # retired as invalid (2) or without a negotiated receipt (3) were never reported.
        proven = reported == 1 or (Path(root) / 'outgoing' / (str(payload_ref) + '.reported')).is_file()
        note(int(message_id), 'recorded' if proven else 'refused' if reported in (2, 3) else 'retrying')
    return found


def live_pending(journal, account, peer):
    """Message IDs of ``peer`` that live capture is still receiving and has not yet captured.

    Only pending raw updates count. One held for good (written before account IDs were
    recorded) is never hydrated, so it neither keeps a message from the import nor makes
    the import wait. The second value is True when a pending raw update cannot be read,
    so which message it holds is unknown.
    """
    from telethon.extensions import BinaryReader
    ids, unreadable = set(), False
    for (payload,) in journal.db.execute(
            "SELECT payload FROM raw_updates WHERE state='pending' AND CAST(json_extract(metadata,'$.peer') AS TEXT)=? "
            "AND coalesce(CAST(json_extract(metadata,'$.account_id') AS TEXT),?)=?", (str(peer), str(account), str(account))):
        try:
            with BinaryReader(payload) as reader:
                ids.add(int(reader.tgread_object().id))
        except Exception:
            unreadable = True
    return ids, unreadable


_CUSTODY = {'held': 1, 'pending': 2, 'admitted': 3}


def live_custody(journal, sources):
    """What live intake's own rows prove about these captures of one message revision.

    ``admitted`` when the Host accepted one: its row finished, holds a Host reference, or
    kept an accepting Host answer. ``pending`` while live intake still hands one over.
    ``held`` when live intake stopped for good without the Host admitting it, for example
    because its chat was excluded or the Host refused it: nothing hands it over later.
    ``None`` when live intake holds none of them. The strongest evidence counts; each
    source is read by the inbox's unique ID, never by a scan.
    """
    found = None
    for source in sources:
        row = journal.db.execute("SELECT state,refs,response FROM inbox WHERE id=?", (source,)).fetchone()
        if row is None:
            continue
        stored = json.loads(row['response'] or '{}')
        answer = stored.get('body') if type(stored) is dict else None
        if row['state'] == 'done' or json.loads(row['refs'] or '[]') or (type(answer) is dict and answer.get('ok') is True):
            custody = 'admitted'
        else:
            custody = 'pending' if row['state'] == 'pending' else 'held'
        if _CUSTODY[custody] > _CUSTODY.get(found, 0):
            found = custody
    return found


class _Skip(Exception):
    """This chat cannot be read; it is left out with this reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Stop(Exception):
    """The owner's choices changed during an await; this step ends without reading on."""


class _Wait(Exception):
    """Nothing was frozen; the same page is read again after a pause."""

    def __init__(self, reason, seconds=None):
        super().__init__(reason)
        self.reason, self.seconds = reason, seconds


def _record_index(field):
    match = re.match(r'records\[(\d+)\]', str(field or ''))
    return int(match.group(1)) if match else None


def _delay(value):
    if type(value) in (int, float) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            return max(1.0, datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() - time.time())
        except ValueError:
            return None
    return None


class HistoryImporter:
    """Runs the owner-started import of one account, one bounded step per call.

    A step sends the oldest unconfirmed frozen records of one conversation, or, when
    none wait, checks the Host's capability and the chat's place and then reads one
    Telegram page. Live intake and outbound dispatch run beside it.
    """

    def __init__(self, engine, journal=None, account=None, clock=time.time):
        self.engine, self.clock = engine, clock
        self.journal = engine.journal if journal is None else journal
        self.account = str(engine.account if account is None else account)
        self._verified, self._verified_at = None, 0.0
        self._placed = set()
        self._next_read = self._next_post = 0.0

    @property
    def host(self):
        return self.engine.host

    def _running(self):
        row = self.journal.db.execute("SELECT id,spec,state,progress FROM imports WHERE account=? AND state='running' "
                                      "ORDER BY updated LIMIT 1", (self.account,)).fetchone()
        if row is None:
            return None
        return {'id': row['id'], 'spec': json.loads(row['spec']), 'progress': json.loads(row['progress'])}

    async def once(self):
        plan = self._running()
        if plan is None or plan['progress'].get('wait_until', 0) > self.clock():
            return False
        try:
            current = self.engine.current()
        except ValueError:
            return False  # Another account, mode or session now: this companion stops; the plan waits as it is.
        try:
            return await self._step(plan, current)
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError:
            return False  # The journal is busy; a step's writes are atomic, so the next tick repeats it.
        except Exception as exc:
            # An import fault stops this import visibly; live intake and outbound work go on.
            self._update(plan, lambda p: ('paused', {**p, 'reason': 'import_error', 'detail': {'error_type': type(exc).__name__}}))
            return True

    async def _step(self, plan, current):
        if plan['progress'].get('rechecked') != plan['progress'].get('run'):
            # Local journals only, so the rest of this step goes on with what it decided.
            if not self._recheck(plan):
                return True
        group = self._unconfirmed(plan['id'])
        if group:
            if self.clock() < self._next_post:
                return False
            self._next_post = self.clock() + POST_GAP
            # Frozen records go only to a Host that still records history in source order. One replaced
            # since the read pauses them unsent, nothing is read again, and a Resume sends the same bytes.
            if not await self._capable(plan):
                return True
            # That check may have been awaited: the owner may have paused, cancelled or narrowed the scope,
            # or the account or mode changed. The send goes on only under what holds now.
            current = self._current(plan)
            if current is None:
                return True
            return await self._send(plan, current, group)
        chat = next((c for c in plan['progress']['chats'] if c['state'] == 'pending'), None)
        if chat is None:
            held = self.journal.db.execute("SELECT count(*) FROM import_records WHERE plan=? AND state='residual'",
                                           (plan['id'],)).fetchone()[0]
            # Held-back messages keep the plan partial: only the owner's Resume decides them.
            self._update(plan, lambda p: ('partial', {**{k: v for k, v in p.items() if k not in _WAITING}, 'reason': 'held_back'})
                         if held else ('done', {**{k: v for k, v in p.items() if k not in _WAITING},
                                                'finished_at': datetime.now(timezone.utc).isoformat()}))
            return True
        peer = chat['peer']
        refusal = 'saved_messages' if peer == int(self.account) else receiving_refusal(current, peer, self.account)
        if refusal:
            self._chat(plan, peer, {'state': 'skipped', 'reason': refusal})
            return True
        if self.clock() < self._next_read or self.journal.flood_remaining(self.account):
            return False
        # Nothing is read from Telegram before the Host confirms it can record history.
        if not await self._capable(plan) or not await self._placed_chat(plan, peer):
            return True
        # Those Host reads were awaited: the owner may have paused, cancelled or narrowed the scope.
        if not self._still_allowed(plan, peer):
            return True
        self._next_read = self.clock() + READ_GAP
        try:
            frozen, update = await self._page(plan, chat)
        except _Stop:
            return True
        except _Skip as stop:
            self._chat(plan, peer, {'state': 'skipped', 'reason': stop.reason})
            return True
        except _Wait as wait:
            self._wait(plan, wait.reason, wait.seconds)
            return True
        self._freeze(plan, peer, frozen, update)
        return True

    # ---- Host --------------------------------------------------------------
    async def _capable(self, plan):
        run = (plan['id'], plan['progress'].get('run'))
        if self._verified == run and self.clock() - self._verified_at < 60:
            return True
        try:
            status, body = await asyncio.to_thread(self.host.call, 'GET', '/identity')
        except (OSError, TimeoutError, ValueError, TypeError):
            self._wait(plan, 'host_unreachable')
            return False
        if status == 403:
            self._pause(plan, 'host_auth_blocked')
            return False
        if status != 200 or type(body) is not dict or body.get('ok') is not True:
            self._wait(plan, 'host_unreachable')
            return False
        from .participant import record_host_facts
        record_host_facts(self.engine.root, body)
        if not host_supports_import(body):
            self._pause(plan, 'host_support_missing')
            return False
        if not host_keeps_source_order(body):
            # It could store messages sent within one second out of order; nothing is read for it.
            self._pause(plan, 'host_source_order_missing')
            return False
        self._verified, self._verified_at = run, self.clock()
        return True

    async def _placed_chat(self, plan, peer):
        key = (plan['id'], plan['progress'].get('run'), peer)
        if key in self._placed:
            return True
        query = urlencode({'provider': 'telegram', 'account_id': self.account, 'conversation_id': str(peer), 'thread_id': ''})
        try:
            status, body = await asyncio.to_thread(self.host.call, 'GET', '/presence/place?' + query)
        except (OSError, TimeoutError, ValueError, TypeError):
            self._wait(plan, 'host_unreachable')
            return False
        body = body if type(body) is dict else {}
        if status == 200 and body.get('state') in {'active', 'silent', 'paused'}:
            self._placed.add(key)  # Silent and Paused places record history too, without a wake.
            return True
        if status == 403:
            self._pause(plan, 'host_auth_blocked')
        elif status in (404, 409) or body.get('disposition') == 'blocked':
            self._pause(plan, body.get('code') if body.get('code') in {'place_ambiguous', 'places_unreadable'} else 'place_not_connected',
                        chat=str(peer))
        else:
            self._wait(plan, body.get('code') if body.get('code') == 'places_unavailable' else 'host_unreachable')
        return False

    def _unconfirmed(self, plan_id):
        first = self.journal.db.execute("SELECT peer,thread FROM import_records WHERE plan=? AND state='pending' ORDER BY seq LIMIT 1",
                                        (plan_id,)).fetchone()
        if first is None:
            return []
        return [dict(r) for r in self.journal.db.execute(
            "SELECT plan,seq,peer,thread,source_event_id,record FROM import_records WHERE plan=? AND state='pending' AND peer=? AND thread=? "
            "ORDER BY seq LIMIT ?", (plan_id, first['peer'], first['thread'], PAGE))]

    async def _send(self, plan, current, group):
        peer = group[0]['peer']
        if receiving_refusal(current, peer, self.account):
            # The owner stopped receiving this chat: its unconfirmed records are never sent again.
            self._settle_chat(plan, peer, 'withheld', 'outside_receiving_scope')
            return True
        records = [json.loads(row['record']) for row in group]
        try:
            status, answer = await asyncio.to_thread(self.host.call, 'POST', '/presence/history',
                                                     {'import_id': plan['id'], 'records': records})
        except (OSError, TimeoutError, ValueError, TypeError):
            self._wait(plan, 'host_unreachable')  # The outcome is unknown: the same records go again.
            return True
        answer = answer if type(answer) is dict else {}
        results = answer.get('results')
        if isinstance(results, list):
            if len(results) != len(group) or any(type(result) is not dict or result.get('source_event_id') != row['source_event_id']
                                                 for row, result in zip(group, results)):
                self._invalid_answer(plan)
                return True
            if self._settle(group, results):
                self._wait(plan, 'host_unconfirmed')
            else:
                self._update(plan, lambda p: ('running', {k: v for k, v in p.items() if k not in _WAITING}))
            return True
        code, disposition = answer.get('code'), answer.get('disposition')
        if code == 'presence_history_invalid':
            index = _record_index(answer.get('field'))
            if index is not None and index < len(group):
                self._settle_one(group[index], 'invalid', {'code': code, 'field': str(answer.get('field'))[:120],
                                                           'error': str(answer.get('error') or '')[:300]})
                return True
            self._pause(plan, code, field=str(answer.get('field') or '')[:120])
        elif code == 'presence_origin_mismatch':
            self._settle_chat(plan, peer, 'rejected', 'outside_place')
        elif disposition == 'retry':
            self._wait(plan, code if isinstance(code, str) and code else 'host_retry', _delay(answer.get('retry_after')))
        elif disposition in {'blocked', 'rejected'}:
            self._pause(plan, code if isinstance(code, str) and code else 'host_refused', chat=str(peer))
        elif status == 404:
            self._pause(plan, 'host_support_missing')
        else:
            self._invalid_answer(plan)
        return True

    def _settle(self, group, results):
        """Record each confirmed result; return how many records stay unconfirmed."""
        unconfirmed = 0
        for row, result in zip(group, results):
            kept = {k: v for k, v in result.items() if k in {'custody', 'history_id', 'turn_ref', 'ts', 'received_at', 'code', 'error'}
                    and type(v) is str}
            if result.get('ok') is True:
                self._settle_one(row, 'duplicate' if result.get('duplicate') is True else 'recorded', kept)
            elif result.get('code') == 'presence_event_identity_conflict':
                self._settle_one(row, 'conflict', kept)  # Held: never resent, never resent changed.
            else:
                unconfirmed += 1  # presence_history_unwritable: resent unchanged
        return unconfirmed

    def _settle_one(self, row, state, result):
        # The plan may have been paused or cancelled while the answer was on its way; what it says still holds.
        with self.journal.db:
            known = self.journal.db.execute("SELECT result FROM import_records WHERE plan=? AND seq=?", (row['plan'], row['seq'])).fetchone()
            if self.journal.db.execute("UPDATE import_records SET state=?, result=? WHERE plan=? AND seq=? AND state='pending'",
                                       (state, json.dumps({**json.loads(known[0] if known else '{}'), **result}, sort_keys=True),
                                        row['plan'], row['seq'])).rowcount:
                self._records_changed(row['plan'])

    def _settle_chat(self, plan, peer, state, reason):
        with self.journal.db:
            if self.journal.db.execute("UPDATE import_records SET state=?, result=? WHERE plan=? AND peer=? AND state='pending'",
                                       (state, json.dumps({'reason': reason}), plan['id'], peer)).rowcount:
                self._records_changed(plan['id'])
        self._chat(plan, peer, {'state': 'skipped', 'reason': reason}, states=('running', 'paused'))

    def _records_changed(self, plan_id):
        """Move the plan's stamp inside the transaction that changed its records (see ``plans``).

        The stamp only rises, by at least a microsecond, so two writes never share one.
        """
        self.journal.db.execute("UPDATE imports SET updated=max(updated+0.000001,?) WHERE id=?", (time.time(), plan_id))

    def _invalid_answer(self, plan):
        streak = int(plan['progress'].get('invalid_answers', 0)) + 1
        if streak >= INVALID_ANSWERS:
            self._pause(plan, 'host_answer_invalid')
        else:
            self._wait(plan, 'host_answer_invalid', extra={'invalid_answers': streak})

    # ---- Telegram ----------------------------------------------------------
    async def _page(self, plan, chat):
        """Read one page newest first and decide each message; nothing is sent here.

        After each awaited Telegram read the plan, account and scope are checked again, so
        a pause, cancel, exclusion or account change during one read stops the page before
        the next read starts. That includes every page of the forum catalog and of its
        refresh, and the reply-ancestry reads that place a message in its topic.
        """
        from telethon import errors
        from .participant import TopicRoutingUnavailable
        peer, spec = chat['peer'], plan['spec']
        start, until = _when(spec['from'], 'from'), _when(plan['progress']['until'], 'until')
        cap = spec['max_per_chat']
        # One message beyond the limit is read only to tell whether older ones remain.
        limit = min(PAGE, cap - chat['read'] + 1)
        client = self.engine.adapter.client
        def allowed():
            if not self._still_allowed(plan, peer):
                raise _Stop()
        try:
            entity = await asyncio.wait_for(client.get_entity(peer), 60)
            allowed()
            messages = list(await asyncio.wait_for(
                client.get_messages(entity, limit=limit, offset_id=chat['offset_id'], offset_date=until), 90))
            catalog = None
            if getattr(entity, 'forum', False):
                allowed()
                try:
                    catalog = await asyncio.wait_for(self.engine.adapter.topic_ids(peer, guard=allowed), 60)
                except ValueError:
                    catalog = None  # Each message then resolves as live capture would, or stays unresolved.
        except errors.FloodWaitError as exc:
            raise _Wait('telegram_flood_wait', max(1, exc.seconds)) from None
        except ValueError:
            raise _Skip('chat_unavailable') from None  # Not known to this session; a chat list refresh caches it.
        except (errors.ServerError, errors.TimedOutError, ConnectionError, TimeoutError, asyncio.TimeoutError, OSError, RuntimeError):
            raise _Wait('telegram_unavailable') from None
        except errors.RPCError as exc:
            raise _Skip('chat_unreadable:' + type(exc).__name__) from None
        title, kind, _, _ = chat_facts(entity, peer > 0)
        conversation = {'title': title, 'chat_type': kind}
        pending, unreadable = live_pending(self.journal, self.account, peer)
        if unreadable:
            raise _Wait('live_capture_settling')
        receipts = None
        frozen, skipped = [], {}
        read, offset, oldest, done, truncated = chat['read'], chat['offset_id'], chat.get('oldest'), len(messages) < limit, False
        for message in messages:
            date = getattr(message, 'date', None)
            if read >= cap and date is not None:
                truncated, done = date >= start, True
                break
            offset = getattr(message, 'id', offset)
            if date is None or date >= until:
                continue
            if date < start:
                done = True
                break
            read, oldest = read + 1, iso(date)
            reason = None
            if getattr(message, 'action', None) is not None:
                reason = 'service'  # Live capture does not receive service messages either.
            else:
                try:
                    topic = await self.engine.thread_of(entity, peer, message, catalog, allowed)
                except TopicRoutingUnavailable:
                    topic, reason = None, 'topic_unresolved'  # Never filed under a guessed topic.
                except errors.FloodWaitError as exc:
                    raise _Wait('telegram_flood_wait', max(1, exc.seconds)) from None
                except (errors.RPCError, RuntimeError, ConnectionError, TimeoutError, asyncio.TimeoutError, OSError):
                    raise _Wait('telegram_unavailable') from None
            if reason is None:
                try:
                    record = history_record(self.account, peer, topic, message, getattr(message, 'sender', None), conversation)
                except (ValueError, TypeError):
                    record, reason = None, 'invalid_source'
            if reason is None:
                event = record['event']
                if record['authorship'] == 'platform_account':
                    if receipts is None:
                        receipts = delivery_receipts(self.journal, self.engine.root, self.account, peer)
                    proof = 'unreadable' if receipts is None else receipts.get(message.id)
                    if proof == 'recorded':
                        reason = 'delivered'  # Already in the room as a receipt-backed reply.
                    elif proof:
                        # Sent by this connector, delivery unproven: held for the owner's Resume, never dropped.
                        frozen.append((event, record, 'residual', {'reason': 'receipt_unreadable'} if proof == 'unreadable'
                                       else {'reason': 'receipt_unconfirmed', 'receipt': proof}))
                        reason = 'residual'
                else:
                    custody, sources = self._live(peer, topic, message, event)
                    if custody == 'admitted':
                        reason = 'live_captured'  # The Host admitted live intake's copy: it is in the conversation.
                    elif custody == 'pending' or message.id in pending:
                        # Live intake still has it in hand: never sent beside it, held back with this copy for Resume.
                        frozen.append((event, record, 'residual', {'reason': 'live_pending', 'live': sources}))
                        reason = 'residual'
                if reason is None:
                    frozen.append((event, record, 'pending', {}))
            if reason not in (None, 'residual'):
                skipped[reason] = skipped.get(reason, 0) + 1
        update = {'offset_id': offset, 'read': read, 'oldest': oldest, 'skipped': skipped}
        if done:
            update.update(state='done', truncated=truncated)
        return frozen, update

    def _live(self, peer, topic, message, event):
        """Live intake's custody of this message revision, with the source IDs that tell it.

        The revision's own capture counts, and so does a pre-0.3 capture of the same edit.
        Only an admitted capture is in the conversation. A capture live intake let go of for
        good without the Host admitting it would otherwise never arrive, so the import carries
        the message under this run's current authorization; the Host's answer then says
        whether it was new.
        """
        sources = [event['source_event_id']]
        if 'edited_at' in event['message']:
            sources += self.engine.legacy_edit_captures(peer, topic, message, message.sender_id)
        return live_custody(self.journal, sources), sources

    def _current(self, plan):
        """The policy this run acts under now, or None once the plan no longer runs this run or the account or mode changed."""
        row = self.journal.db.execute("SELECT state,progress FROM imports WHERE id=?", (plan['id'],)).fetchone()
        if row is None or row['state'] != 'running' or json.loads(row['progress']).get('run') != plan['progress'].get('run'):
            return None
        try:
            return self.engine.current()
        except ValueError:
            return None

    def _still_allowed(self, plan, peer):
        """Whether this run may still read ``peer``: same run, same account and mode, chat received."""
        current = self._current(plan)
        return current is not None and not receiving_refusal(current, peer, self.account)

    def _recheck(self, plan):
        """Meet held-back messages with the journals again, once per Start or Resume.

        Only the journals are read: never Telegram, never a newer revision. An own message
        whose receipt the Host has since recorded, or a message live intake has since handed
        to the Host, is in the room, so it is skipped with no history write. A receipt that
        no longer stands for a delivery (refused, never sent, or gone), or a message live
        intake let go of without the Host admitting it, lets this owner-authorized run send
        the frozen record, which keeps a note of why it was held. A receipt still being
        reported, a message live intake still has in hand, or journals that cannot be read
        keep it held. Conflicts and other held records are never touched.
        """
        rows = self.journal.db.execute("SELECT seq,peer,record,result FROM import_records WHERE plan=? AND state='residual' ORDER BY seq",
                                       (plan['id'],)).fetchall()
        receipts, arriving, decisions = {}, {}, []
        for row in rows:
            record, held = json.loads(row['record']), json.loads(row['result'])
            mid = int(record['event']['message']['message_id'])
            if held.get('reason') == 'live_pending':
                if row['peer'] not in arriving:
                    arriving[row['peer']] = live_pending(self.journal, self.account, row['peer'])
                ids, unreadable = arriving[row['peer']]
                # The source IDs the read found, looked up again through the inbox's unique index.
                custody = live_custody(self.journal, held.get('live') or [record['event']['source_event_id']])
                if custody == 'admitted':
                    decisions.append(('delivered', {'reason': 'live_admitted'}, row['seq']))
                elif custody == 'pending' or mid in ids or unreadable:
                    decisions.append(('residual', held, row['seq']))
                else:
                    decisions.append(('pending', {'released': 'live_' + (custody or 'absent'), 'held_as': 'live_pending'}, row['seq']))
                continue
            if row['peer'] not in receipts:
                receipts[row['peer']] = delivery_receipts(self.journal, self.engine.root, self.account, row['peer'])
            found = receipts[row['peer']]
            proof = 'unreadable' if found is None else found.get(mid, 'absent')
            if proof == 'recorded':
                decisions.append(('delivered', {'reason': 'receipt_recorded'}, row['seq']))
            elif proof in ('refused', 'absent'):
                decisions.append(('pending', {'released': 'receipt_' + proof, 'held_as': held.get('reason', '')}, row['seq']))
            else:
                decisions.append(('residual', {'reason': 'receipt_unreadable'} if proof == 'unreadable'
                                  else {'reason': 'receipt_unconfirmed', 'receipt': proof}, row['seq']))
        with self.journal.db:
            self.journal.db.execute('BEGIN IMMEDIATE')
            current = self.journal.db.execute("SELECT state,progress FROM imports WHERE id=?", (plan['id'],)).fetchone()
            progress = json.loads(current['progress'])
            if current['state'] != 'running' or progress.get('run') != plan['progress'].get('run'):
                return False
            for state, result, seq in decisions:
                self.journal.db.execute("UPDATE import_records SET state=?, result=? WHERE plan=? AND seq=? AND state='residual'",
                                        (state, json.dumps(result, sort_keys=True), plan['id'], seq))
            progress['rechecked'] = progress.get('run')
            self.journal.db.execute("UPDATE imports SET progress=?, updated=max(updated+0.000001,?) WHERE id=?",  # records changed
                                    (json.dumps(progress, sort_keys=True), time.time(), plan['id']))
        return True

    # ---- journal -----------------------------------------------------------
    def _freeze(self, plan, peer, frozen, update):
        """Store the page's records and its cursor in one transaction, before any send."""
        with self.journal.db:
            self.journal.db.execute('BEGIN IMMEDIATE')
            row = self.journal.db.execute("SELECT state,progress FROM imports WHERE id=?", (plan['id'],)).fetchone()
            progress = json.loads(row['progress'])
            if row['state'] != 'running' or progress.get('run') != plan['progress'].get('run'):
                return False  # Paused or cancelled meanwhile: nothing of this page is kept or sent.
            seq = self.journal.db.execute("SELECT coalesce(max(seq),0) FROM import_records WHERE plan=?", (plan['id'],)).fetchone()[0]
            for event, record, state, result in frozen:
                seq += 1
                self.journal.db.execute(
                    "INSERT OR IGNORE INTO import_records(plan,seq,peer,thread,source_event_id,record,state,result) VALUES(?,?,?,?,?,?,?,?)",
                    (plan['id'], seq, peer, event['thread_id'], event['source_event_id'],
                     json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(',', ':')), state, json.dumps(result)))
            for chat in progress['chats']:
                if chat['peer'] == peer:
                    skipped = dict(chat.get('skipped', {}))
                    for reason, number in update['skipped'].items():
                        skipped[reason] = skipped.get(reason, 0) + number
                    chat.update({**update, 'skipped': skipped})
            for key in _WAITING:
                progress.pop(key, None)
            self.journal.db.execute("UPDATE imports SET progress=?, updated=max(updated+0.000001,?) WHERE id=?",  # records changed
                                    (json.dumps(progress, sort_keys=True), time.time(), plan['id']))
        return True

    def _update(self, plan, change, states=('running',)):
        """Read-modify-write the plan inside one transaction; an owner's later choice wins."""
        with self.journal.db:
            self.journal.db.execute('BEGIN IMMEDIATE')
            row = self.journal.db.execute("SELECT state,progress FROM imports WHERE id=?", (plan['id'],)).fetchone()
            if row is None or row['state'] not in states:
                return False
            state, progress = change(json.loads(row['progress']))
            self.journal.db.execute("UPDATE imports SET state=?, progress=?, updated=? WHERE id=?",
                                    (state or row['state'], json.dumps(progress, sort_keys=True), time.time(), plan['id']))
        return True

    def _chat(self, plan, peer, values, states=('running',)):
        def change(progress):
            for chat in progress['chats']:
                if chat['peer'] == peer:
                    chat.update(values)
            return None, progress
        self._update(plan, change, states)

    def _pause(self, plan, reason, **detail):
        self._update(plan, lambda p: ('paused', {**{k: v for k, v in p.items() if k not in _WAITING},
                                                 'reason': reason, 'detail': detail}))

    def _wait(self, plan, reason, seconds=None, extra=None):
        def change(progress):
            attempts = int(progress.get('attempts', 0)) + 1
            delay = seconds or BACKOFF[min(attempts, len(BACKOFF)) - 1]
            return None, {**progress, **(extra or {}), 'attempts': attempts, 'wait_reason': reason,
                          'wait_until': self.clock() + delay}
        self._update(plan, change)
