"""Synthetic Telegram history and an in-memory Host speaking the history wire contract v1.

The Host side follows the frozen contract of core task 3a07f5aa (history-interface.md,
218 lines): the whole request is validated before any write, a ``source_timestamp`` is
never later than the Host's receipt time and names the same instant as
``event.message.sent_at``, results come one per record in request order, ``duplicate``
versus conflict is decided by the live identity, the source message facts and the send
time (and, against earlier history, authorship), HTTP 200, 409 and 503 summarize the
results, and request-level refusals carry no results. The revised source-order contract of
core task b520 is held too: ``/identity`` advertises ``presence_source_order_version: 1``,
an optional ``event.message.source_order`` is an integer in 0..2^53-1 at ``/presence/turn``
and ``/presence/history``, and like other enrichment the first stored value stands. It
is a wire fake for offline checks, not the core: a joint run with the real Host is
separate. Live turns are answered only as far as an import's custody checks need: an
admitted turn becomes the Host's live copy of that event.
No network, credentials or Telegram session is involved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from telethon import types as T

EVENT_FIELDS = frozenset({'source_event_id', 'provider', 'account_id', 'conversation_id', 'thread_id',
                          'conversation_key', 'actor', 'conversation', 'message', 'text'})
RECORD_FIELDS = frozenset({'event', 'authorship', 'source_timestamp', 'staged_files'})
SOURCE_FACTS = ('message_id', 'event_kind', 'revision', 'edited_at', 'reply_to_message_id')


class Invalid(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


class TransportDown(OSError):
    """The request may or may not have reached the Host."""


def _instant(value):
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        return value
    return parsed.astimezone(timezone.utc).isoformat() if parsed.utcoffset() is not None else value


def source_facts(message):
    facts = {key: (_instant(message.get(key)) if key == 'edited_at' else message.get(key)) for key in SOURCE_FACTS}
    media = message.get('media')
    facts['media'] = {k: v for k, v in media.items() if k not in {'status', 'note', 'reason'}} if isinstance(media, dict) else media
    return facts


class WireHost:
    """``transport(method, path, payload, headers)`` for ``participant.Host``.

    ``faults`` is a queue consumed one per history request:
    ``('transport_after',)`` applies the request and then loses the answer;
    ``('transport_before',)`` loses the request before it lands;
    ``('unwritable_at', k, landed)`` makes record ``k`` unconfirmed (written if ``landed``)
    and leaves every later record unattempted; ``('refuse', status, code, disposition, extra)``
    answers a request-level refusal.
    """

    def __init__(self, account, *, supports=True, place_state='active', now=None):
        self.account = str(account)
        self.supports = supports
        self.orders = True  # False: a Host that records history but predates source order
        # With orders False: True refuses source_order as an unknown message key, as a stricter Host might;
        # False keeps it as display enrichment, as the core before source order does, without ordering by it.
        self.strict = False
        self.place_state = place_state  # None: not connected
        self.auth_blocked = False
        self.down = set()  # paths whose requests never reach the Host
        self.on_call = None  # called with (method, path) while a request is in flight
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.rows = {}
        self.calls = []
        self.faults = []
        self.history_bodies = []
        self.history_answers = []  # (HTTP status, body) or the transport loss, in request order
        self.sequence = 0
        self.turn_status = 'completed'  # the status of an admitted live turn
        self.turn_refusal = None  # (HTTP status, body) refusing every live turn, when set

    # ---- wire ---------------------------------------------------------------
    def transport(self, method, path, payload, headers):
        assert headers.get('x-skill-token') == 'skill-token', 'the connector token travels in the header'
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.calls.append((method, path, raw))
        body = None if raw is None else json.loads(raw)
        url = urlsplit(path)
        if url.path in self.down:
            raise TransportDown('Host unreachable')
        if self.on_call is not None:
            self.on_call(method, url.path)
        if url.path == '/identity' and method == 'GET':
            if self.auth_blocked:
                return 403, {'ok': False, 'code': 'presence_auth_blocked', 'disposition': 'blocked', 'error': 'token refused'}
            facts = {'ok': True, 'places_version': 1, 'presence_work_output_version': 1,
                     'presence_continuation_version': 1, 'presence_delivery_version': 1}
            if self.supports:
                facts['presence_history_import_version'] = 1
            if self.orders:
                facts['presence_source_order_version'] = 1
            return 200, facts
        if url.path == '/presence/place' and method == 'GET':
            query = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            if self.auth_blocked:
                return 403, {'ok': False, 'code': 'presence_auth_blocked', 'disposition': 'blocked', 'error': 'token refused'}
            if self.place_state is None or query.get('account_id') != self.account:
                return 404, {'ok': False, 'code': 'place_not_connected', 'disposition': 'blocked',
                             'error': 'Connect this place in Consciousness > Places.'}
            return 200, {'place_id': 'place-telegram', 'label': 'Telegram', 'state': self.place_state}
        if url.path == '/presence/turn' and method == 'POST':
            if self.turn_refusal is not None:
                return self.turn_refusal
            self.seed_live(body['event'])
            return 200, {'ok': True, 'status': self.turn_status, 'turn_ref': f'turn-{self.sequence}'}
        if url.path == '/presence/history' and method == 'POST':
            if not self.supports:
                return 404, {'detail': 'Not Found'}
            self.history_bodies.append(raw)
            fault = self.faults.pop(0) if self.faults else None
            if fault and fault[0] == 'transport_before':
                self.history_answers.append(('lost before landing', None))
                raise TransportDown('connection reset before the request landed')
            status, answer = self.history(body, fault)
            if fault and fault[0] == 'transport_after':
                self.history_answers.append(('lost after landing', answer))
                raise TransportDown('connection reset after the request landed')
            self.history_answers.append((status, answer))
            return status, answer
        return 404, {'ok': False, 'error': 'not found', 'code': 'not_found', 'disposition': 'rejected'}

    # ---- contract -----------------------------------------------------------
    def validate(self, payload):
        if not isinstance(payload, dict) or not {'import_id', 'records'} <= set(payload) or set(payload) - {'import_id', 'records', 'binding_id'}:
            raise Invalid('payload', 'expected import_id, records and an optional binding_id')
        import_id = payload['import_id']
        if not isinstance(import_id, str) or not import_id or import_id != import_id.strip() or len(import_id) > 128:
            raise Invalid('import_id', 'import_id must be 1-128 characters without surrounding spaces')
        if not isinstance(payload.get('binding_id', ''), str):
            raise Invalid('binding_id', 'binding_id must be a string')
        records = payload['records']
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            raise Invalid('records', 'records must hold 1-100 records')
        key = None
        for index, record in enumerate(records):
            field = f'records[{index}]'
            if not isinstance(record, dict) or not {'event', 'authorship', 'source_timestamp'} <= set(record) or set(record) - RECORD_FIELDS:
                raise Invalid(field, 'record fields')
            if record.get('staged_files', []) != []:
                raise Invalid(field + '.staged_files', 'history version 1 stages no files')
            if record['authorship'] not in ('participant', 'platform_account'):
                raise Invalid(field + '.authorship', 'authorship')
            event = record['event']
            if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
                raise Invalid(field + '.event', 'exactly the live event fields, without facts')
            for name in ('source_event_id', 'provider', 'account_id', 'conversation_id', 'thread_id', 'conversation_key', 'text'):
                if not isinstance(event[name], str):
                    raise Invalid(f'{field}.event.{name}', 'string required')
            for name in ('actor', 'conversation', 'message'):
                if not isinstance(event[name], dict):
                    raise Invalid(f'{field}.event.{name}', 'object required')
            actor = event['actor'].get('platform_actor_id')
            if not isinstance(actor, str) or not actor.strip():
                raise Invalid(f'{field}.event.actor.platform_actor_id', 'actor required')
            if 'transport_queue' in event['conversation']:
                raise Invalid(f'{field}.event.conversation.transport_queue', 'history carries no inbox observation')
            this = (event['provider'], event['account_id'], event['conversation_id'], event['thread_id'])
            if key is not None and this != key:
                raise Invalid(field + '.event', 'one request carries one conversation')
            key = this
            stamp = record['source_timestamp']
            try:
                parsed = datetime.fromisoformat(stamp[:-1] + '+00:00' if stamp.endswith('Z') else stamp)
            except (TypeError, ValueError, AttributeError):
                raise Invalid(field + '.source_timestamp', 'ISO 8601 with a time zone') from None
            if parsed.utcoffset() is None:
                raise Invalid(field + '.source_timestamp', 'ISO 8601 with a time zone')
            if parsed.astimezone(timezone.utc) > self.now():
                raise Invalid(field + '.source_timestamp', 'later than the Host receipt time')
            sent = event['message'].get('sent_at')
            if sent is not None and _instant(sent) != parsed.astimezone(timezone.utc).isoformat():
                raise Invalid(field + '.source_timestamp', 'source_timestamp and event.message.sent_at name different times')
            if 'source_order' in event['message'] and not self.orders and self.strict:
                raise Invalid(field + '.event.message.source_order', 'unknown message key')
            order = event['message'].get('source_order', 0)
            if type(order) is not int or not 0 <= order <= 2**53 - 1:
                raise Invalid(field + '.event.message.source_order', 'an integer in 0..2^53-1')
        return key

    def history(self, payload, fault):
        try:
            provider, account, conversation, thread = self.validate(payload)
        except Invalid as exc:
            return 400, {'ok': False, 'error': str(exc), 'code': 'presence_history_invalid', 'disposition': 'rejected', 'field': exc.field}
        if fault and fault[0] == 'refuse':
            _, status, code, disposition, extra = fault
            return status, {'ok': False, 'error': code, 'code': code, 'disposition': disposition, **(extra or {})}
        if self.place_state is None or account != self.account:
            return 409, {'ok': False, 'error': 'Connect this place', 'code': 'place_not_connected', 'disposition': 'blocked'}
        results = []
        for index, record in enumerate(payload['records']):
            event = record['event']
            if fault and fault[0] == 'unwritable_at' and index >= fault[1]:
                if index == fault[1] and fault[2]:
                    self._write(payload['import_id'], record)  # landed, but its confirmation is lost
                results.append({'source_event_id': event['source_event_id'], 'ok': False, 'code': 'presence_history_unwritable',
                                'disposition': 'retry', 'error': 'not confirmed; resend this record unchanged'})
                continue
            results.append(self._record(payload['import_id'], record))
        unwritten = sum(1 for r in results if r.get('code') == 'presence_history_unwritable')
        conflicts = sum(1 for r in results if r.get('code') == 'presence_event_identity_conflict')
        if unwritten:
            return 503, {'ok': False, 'code': 'presence_history_unwritable', 'disposition': 'retry',
                         'error': f'{unwritten} of {len(results)} records were not confirmed; resend them unchanged',
                         'import_id': payload['import_id'], 'results': results}
        if conflicts:
            return 409, {'ok': False, 'code': 'presence_event_identity_conflict', 'disposition': 'rejected',
                         'error': f'{conflicts} of {len(results)} records conflict with an already recorded source event',
                         'import_id': payload['import_id'], 'results': results}
        return 200, {'ok': True, 'import_id': payload['import_id'], 'results': results}

    @staticmethod
    def identity(event):
        return ['place-telegram', event['source_event_id'], event['provider'], event['account_id'], event['conversation_id'],
                event['thread_id'], event['actor']['platform_actor_id'], event['text']]

    def _write(self, import_id, record):
        event = record['event']
        self.sequence += 1
        received = self.now().isoformat()
        self.rows[event['source_event_id']] = {
            'identity': self.identity(event), 'facts': source_facts(event['message']), 'custody': 'history',
            'authorship': record['authorship'], 'ts': _instant(record['source_timestamp']), 'import_id': import_id,
            'order': event['message'].get('source_order') if self.orders else None,
            'history_id': f'chat:{self.sequence}', 'received_at': received, 'event': event}

    def _record(self, import_id, record):
        event = record['event']
        prior = self.rows.get(event['source_event_id'])
        duplicate = prior is not None
        if not duplicate:
            self._write(import_id, record)
            prior = self.rows[event['source_event_id']]
        elif prior['identity'] != self.identity(event) or prior['facts'] != source_facts(event['message']) or (
                prior['custody'] == 'history' and (prior['authorship'] != record['authorship']
                                                   or prior['ts'] != _instant(record['source_timestamp']))) or (
                # A live capture is compared by the send time it recorded, never its receipt time.
                prior['custody'] == 'live' and prior['sent'] is not None and prior['sent'] != _instant(record['source_timestamp'])):
            return {'source_event_id': event['source_event_id'], 'ok': False, 'code': 'presence_event_identity_conflict',
                    'disposition': 'rejected', 'error': 'this source event is already recorded with different facts'}
        return {'source_event_id': event['source_event_id'], 'ok': True, 'recorded': True, 'duplicate': duplicate,
                'custody': prior['custody'], 'turn_ref': 'presence-' + event['source_event_id'][-12:].replace(':', '-'),
                'history_id': prior['history_id'], 'ts': prior['ts'], 'received_at': prior['received_at']}

    def seed_live(self, event):
        """A live capture the Host already holds for this revision."""
        self.sequence += 1
        self.rows[event['source_event_id']] = {
            'identity': self.identity(event), 'facts': source_facts(event['message']), 'custody': 'live',
            'authorship': None, 'sent': _instant(event['message'].get('sent_at')), 'ts': self.now().isoformat(),
            'order': event['message'].get('source_order') if self.orders else None,
            'history_id': f'chat:{self.sequence}', 'received_at': self.now().isoformat(), 'event': event}

    def paths(self):
        return [(method, urlsplit(path).path) for method, path, _ in self.calls]


# ---- synthetic Telegram -----------------------------------------------------------
def peer_of(peer):
    if peer > 0:
        return T.PeerUser(peer)
    if peer < -1000000000000:
        return T.PeerChannel(-peer - 1000000000000)
    return T.PeerChat(-peer)


def message(mid, peer, text, date, *, sender=None, out=False, **kw):
    """A real Telethon Message as getHistory returns it."""
    from_id = None
    if out and peer < 0:
        from_id = T.PeerUser(kw.pop('account'))
    elif not out and peer < 0:
        from_id = T.PeerUser(sender or 809)
    kw.pop('account', None)
    return T.Message(id=mid, peer_id=peer_of(peer), date=date, message=text, out=out, from_id=from_id, **kw)


def service(mid, peer, date, sender=809):
    return T.MessageService(id=mid, peer_id=peer_of(peer), date=date, action=T.MessageActionChatJoinedByLink(inviter_id=1),
                            from_id=T.PeerUser(sender))


class History:
    """``client`` stand-in: getHistory over fixed messages, with every call recorded."""

    def __init__(self, entities, histories):
        self.entities, self.histories, self.calls = entities, histories, []

    async def get_entity(self, peer):
        self.calls.append(('get_entity', peer))
        if peer not in self.entities:
            raise ValueError('Could not find the input entity for %r' % (peer,))
        return self.entities[peer]

    async def get_messages(self, entity, limit=None, offset_id=0, offset_date=None, ids=None, **kw):
        peer = entity if type(entity) is int else next(p for p, e in self.entities.items() if e is entity)
        if ids is not None:  # live capture's reply-target read
            self.calls.append(('get_message', peer, ids))
            return next((m for m in self.histories.get(peer, []) if m.id == ids), None)
        self.calls.append(('get_messages', peer, {'limit': limit, 'offset_id': offset_id, 'offset_date': offset_date}))
        rows = sorted(self.histories.get(peer, []), key=lambda m: m.id, reverse=True)
        rows = [m for m in rows if (not offset_id or m.id < offset_id) and (offset_date is None or m.date < offset_date)]
        return rows[:limit]

    async def download_media(self, message, file=None):
        Path(file).write_bytes(b'synthetic media bytes')  # live capture's staging, never the import's

    def reads(self):
        return [call for call in self.calls if call[0] == 'get_messages']


class Forum(History):
    """``History`` that also answers the requests ``TelegramAdapter.topic_ids`` makes, one catalog page at a time.

    ``pages`` maps a forum to its pages of topic IDs and is read at each request, so a test
    can change the catalog between reads. ``on_page`` is called with the running number of
    catalog pages answered, after each answer.
    """

    def __init__(self, entities, histories, pages):
        super().__init__(entities, histories)
        self.pages, self.on_page, self.answered = pages, None, 0

    async def get_input_entity(self, peer):
        self.calls.append(('get_input_entity', peer))
        return peer

    async def is_user_authorized(self):
        self.calls.append(('is_user_authorized',))
        return True

    async def __call__(self, request):
        from telethon.tl.functions.messages import GetForumTopicsRequest
        assert isinstance(request, GetForumTopicsRequest), request
        pages = self.pages[request.peer]
        number = 0 if not request.offset_topic else 1 + next(n for n, page in enumerate(pages) if page[-1] == request.offset_topic)
        self.calls.append(('GetForumTopics', request.peer, number + 1))
        when = datetime(2026, 10, 1, tzinfo=timezone.utc)
        topics = [T.ForumTopic(id=tid, date=when, peer=peer_of(request.peer), title=f'Topic {tid}', icon_color=0, top_message=tid + 1000,
                               read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                               unread_reactions_count=0, unread_poll_votes_count=0, from_id=T.PeerUser(809),
                               notify_settings=T.PeerNotifySettings()) for tid in pages[number]]
        answer = T.messages.ForumTopics(count=sum(len(page) for page in pages), topics=topics, chats=[], users=[], pts=0,
                                        messages=[T.Message(id=tid + 1000, peer_id=peer_of(request.peer), date=when, message='')
                                                  for tid in pages[number]])
        self.answered += 1
        if self.on_page is not None:
            self.on_page(self.answered)
        return answer


def user(uid, first='Masha', username='masha'):
    return T.User(id=uid, first_name=first, username=username, bot=False, access_hash=1)


def group(gid, title='Board games on Fridays', forum=False):
    return T.Channel(id=-gid - 1000000000000, title=title, photo=T.ChatPhotoEmpty(), date=None, megagroup=True,
                     forum=forum, access_hash=456)
