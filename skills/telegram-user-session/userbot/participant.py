"""Account-wide Telegram participant using current Places, not legacy allowlist profiles."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from .config import load_config, _private_dir, _write_json
from .participant_state import Journal, policy, digest, receiving_refusal, legacy_text_matches
from .participant_ops import Operations, SPECS, validate
from .presence import (_urllib_transport, EARLY_CONTINUATION, PresenceToken, anchored_message_id, build_event,
                       busy_delay, lists_early_continuation, retry_seconds)
from .normalize import actor_facts, canonical_text, chat_facts, content_revision, iso, media_descriptor

@contextmanager
def session_lock(root):
    """Kernel-released lifetime lock. A stale word/file never denies a new owner."""
    root = _private_dir(root)
    path = root / 'participant-session.lock'
    if path.is_symlink():
        raise ValueError('session lock redirected')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_BINARY', 0), 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        os.lseek(fd, 0, 0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            if os.name == 'nt':
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

# Host facts worth showing the owner; any other identity field stays private.
HOST_FACTS = ('places_version', 'presence_work_output_version', 'presence_continuation_version',
              'presence_delivery_version', 'presence_main_contact_version', 'presence_history_import_version',
              'presence_source_order_version')

class Host:
    def __init__(self, url, token, transport=None):
        self.transport = transport or _urllib_transport(url, timeout=30)
        self.token = token
    def call(self, method, path, body=None):
        return self.transport(method, path, body, {'x-skill-token': self.token.use_in_request()})
    def identity(self):
        status, body = self.call('GET', '/identity')
        if status != 200 or body.get('ok') is not True or any(body.get(k) != 1 for k in ('places_version','presence_work_output_version','presence_continuation_version','presence_delivery_version')):
            raise RuntimeError('current authenticated Host Places unavailable')
        return body
    def place(self, account, peer='', topic=''):
        status, body = self.call('GET', '/presence/place?' + urlencode({'provider':'telegram', 'account_id':str(account), 'conversation_id':str(peer), 'thread_id':str(topic or '')}))
        if status != 200 or body.get('state') not in {'active','silent','paused'}:
            raise RuntimeError('place not connected or unreadable')
        return body

def continuation_version(identity):
    """2 when this Host lists early custody answers, else 1: the scalar it keeps at 1 either way."""
    return EARLY_CONTINUATION if lists_early_continuation(identity) else 1

class CurrentAuthorization:
    """One current local/Host authorization owner for both transport modes."""
    def __init__(self, root, host, account, mode):
        self.root, self.host, self.account, self.mode = Path(root), host, str(account), mode
        config = load_config(self.root)
        self.credentials = (config.api_id, config.api_hash, config.phone) if config else None
    def current(self, peer='', write=False, *, topic=None, expected_binding=None, person=False):
        p = policy(self.root)
        c = load_config(self.root)
        if p['mode'] != self.mode or c is None or str(c.account_id) != self.account:
            raise ValueError('account or mode changed')
        if self.credentials != (c.api_id, c.api_hash, c.phone):
            raise ValueError('session configuration changed')
        if p['expected_account_id'] and p['expected_account_id'] != self.account:
            raise ValueError('authenticated account does not match expected identity')
        if peer and str(peer).lstrip('-').isdigit():
            # Legacy mode keeps its own allowlist as the scope; exclusions apply to both.
            # A person acted on inside a chat (the user of invite, ban, mute or admin) is
            # not a chat: the selection lists chats, so only an exclusion applies to them.
            chats = self.mode == 'participant' and not person
            refusal = receiving_refusal(p if chats else {**p, 'scope': 'all'}, peer, self.account)
            if refusal and person:
                raise ValueError('target user blocked by current configuration')
            if refusal == 'peer_excluded':
                raise ValueError('peer blocked by current configuration')
            if refusal:
                raise ValueError('peer outside the selected receiving scope')
        if self.mode == 'legacy' and (int(peer) not in c.allowed_peer_ids or not expected_binding
                or c.binding_for(int(peer), topic) != expected_binding):
            raise ValueError('legacy enrollment changed')
        if write and not p['outgoing']:
            raise ValueError('outgoing actions paused locally')
        return p
    async def authorize(self, peer='', topic='', write=False, *, expected_binding=None, person=False):
        self.current(peer, write, topic=topic or None, expected_binding=expected_binding, person=person)
        place = await asyncio.to_thread(self.host.place, self.account, peer, topic)
        self.current(peer, write, topic=topic or None, expected_binding=expected_binding, person=person)
        if write and place['state'] != 'active':
            raise ValueError('destination is Silent or Paused')
        return place

class TopicRoutingUnavailable(ValueError):
    """Captured Telegram source lacks a currently provable forum destination."""

# Only these fixed validation sentences become public reason codes; arbitrary
# provider text may contain private request details.
PREFLIGHT_REASONS = {'outgoing actions paused locally':'outgoing_paused',
    'destination is Silent or Paused':'destination_inactive',
    'account or mode changed':'account_changed',
    'authenticated account changed':'account_changed',
    'peer blocked by current configuration':'peer_excluded',
    'peer outside the selected receiving scope':'peer_not_selected',
    'target user blocked by current configuration':'user_excluded',
    'unknown topic':'unknown_topic',
    'selected document is not a Telegram sticker; select its canonical sticker-pack document':'not_a_sticker'}
# A closed place or local switch holds unattempted work; it is not a refusal of the work.
HOLD_REASONS = frozenset({'outgoing_paused', 'destination_inactive'})
# The owner changed the mode, credentials or expected account under this running companion.
# Its accepted incoming stays pending for the companion that matches; never a denial.
EXECUTOR_CHANGED = frozenset({'account or mode changed', 'session configuration changed',
                              'authenticated account does not match expected identity'})
_CHANGED_MEDIA = 'retained snapshot removed or changed'


class Participant:
    def __init__(self, root, adapter, host, journal, account):
        self.root, self.adapter, self.host, self.journal, self.account = Path(root), adapter, host, journal, str(account)
        self.ops = Operations(adapter, root, self.account)
        self.ops.guard = self.authorize
        self.capabilities = {}
        self.authorization = CurrentAuthorization(root, host, account, 'participant')
        self.next_send = 0.0
        # Peers hydrate side by side; one peer's slow download holds only that peer.
        self.hydration_limit = 3
        self._hydrating = {}
        self._place_observed = 0.0
        # The continuation version negotiated at startup (serve): 2 when the Host lists it.
        self.continuation_version = 1
        # Conversations are offered to the Host side by side, each in its own order.
        self.intake_limit = 3
        self._submitting = {}
        # References of an event already read to their end; a live parent may keep naming one.
        self._ended = {}
    def current(self, peer='', write=False):
        return self.authorization.current(peer, write)
    async def authorize(self, peer='', topic='', write=False, *, person=False):
        return await self.authorization.authorize(peer, topic, write, person=person)
    async def dispatch_one(self):
        from telethon import errors
        row = self.journal.claim(self.account)
        if not row:
            return False
        args = json.loads(row['args'])
        effect = validate(row['op'], args)
        write = effect != 'read'
        try:
            await self.authorize(args.get('peer',''), args.get('topic_id',''), write)
            if await self.adapter.get_me() != int(self.account):
                raise ValueError('authenticated account changed')
            prepared = await asyncio.wait_for(self.ops.prepare(row['op'], args), 60)
            # Resolve aliases BEFORE the final destination check; a @name must
            # not evade a numeric muted/blocked place. Source reads are checked too.
            if prepared.get('source_peer') is not None:
                await self.authorize(prepared['resolved_source_peer'], '', False)
            if prepared.get('user') is not None:
                await self.authorize(prepared['resolved_user'], '', write, person=True)
            await self.authorize(prepared.get('resolved_peer',''), prepared.get('topic_id',''), write)
            self.current(prepared.get('resolved_peer',''), write)
        except errors.FloodWaitError as exc:
            self.journal.set(row['seq'], 'queued', {'code':'flood_wait','reason':type(exc).__name__,'retry_after_seconds':exc.seconds}, max(1, exc.seconds))
            self.journal.flood(self.account, exc.seconds, type(exc).__name__)
            return True
        except (TimeoutError, asyncio.TimeoutError, OSError, RuntimeError):
            # Python 3.10's wait_for raises asyncio.TimeoutError, not the builtin.
            self.journal.set(row['seq'], 'queued', {'code':'preflight_unavailable'}, 30)
            return True
        except Exception as exc:
            reason = PREFLIGHT_REASONS.get(str(exc)) if type(exc) is ValueError else None
            if reason in HOLD_REASONS:
                self.journal.hold(row, reason)
                return True
            self.journal.set(row['seq'], 'failed', {'code':'preflight_refused', 'type':type(exc).__name__, 'reason':reason})
            return True
        remaining = self.journal.flood_remaining(self.account)
        if remaining:
            self.journal.set(row['seq'], 'queued', {'code':'flood_wait','retry_after_seconds':remaining}, remaining)
            return True
        self.journal.set(row['seq'], 'attempted')
        try:
            result = await asyncio.wait_for(self.ops.execute(row['op'], prepared, row['random_id']), 90)
        except asyncio.CancelledError:
            self.journal.set(row['seq'], 'unknown' if write else 'queued', {'code':'interrupted'})
            raise
        except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
            # A typed Telegram refusal proves no accepted operation; preserve ID
            # and pace the account, never bypass the provider's flood controls.
            self.journal.set(row['seq'], 'queued', {'code':'flood_wait','reason':type(exc).__name__,'retry_after_seconds':exc.seconds}, max(1, exc.seconds))
            self.journal.flood(self.account, exc.seconds, type(exc).__name__)
            self.next_send = asyncio.get_running_loop().time() + max(1, exc.seconds)
            return True
        except (errors.ServerError, errors.TimeoutError, errors.TimedOutError, errors.RandomIdDuplicateError, TimeoutError, asyncio.TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
            self.journal.set(row['seq'], 'unknown' if write else 'queued', {'code':'outcome_unknown', 'type':type(exc).__name__}, 30)
            return True
        except errors.RPCError as exc:
            self.journal.set(row['seq'], 'failed', {'code':'telegram_refused', 'type':type(exc).__name__,
                'rpc_code':exc.code, 'rpc_message':exc.message,
                'request_type':type(exc.request).__name__ if exc.request is not None else None})
            return True
        except Exception as exc:
            self.journal.set(row['seq'], 'unknown' if write else 'failed', {'code':'execution_error', 'type':type(exc).__name__})
            return True
        if row['op'] in {'send_text','send_media','send_sticker','forward'}:
            report = self.delivery_body(row, {**args,'topic_id':prepared.get('topic_id')}, result, prepared['resolved_peer'])
            self.journal.settle_with_report(row['seq'],result,row['id'],report)
        else:
            self.journal.set(row['seq'], 'sent' if write else 'done', result)
        self.next_send = asyncio.get_running_loop().time() + policy(self.root)['interval']
        return True
    def delivery_body(self, row, args, result, peer):
        origin = json.loads(row.get('origin') or '{"kind":"tool"}')
        body = {'schema_version':1, 'delivery_id': row['id'], 'part_id':'0', 'state':'delivered',
                'provider':'telegram', 'account_id':self.account, 'conversation_id':str(peer),
                'thread_id':str(args.get('topic_id') or ''), 'text':args.get('text',''),
                'format':'text' if row['op']=='send_text' else 'media',
                'message':{'message_id':str(result['message_id'])}, 'origin':origin}
        return body
    def spool(self, event, kind='new'):
        """Capture immutable TL bytes before the first provider/Host await."""
        m = event.message
        if m.out or not event.chat_id or not event.sender_id:
            return
        if kind == 'edit' and getattr(m, 'edit_date', None) is None:
            # Reactions and counters also arrive as edit updates; without an edit
            # time Telegram reports no new revision of what the message says.
            return
        p = self.current()
        if receiving_refusal(p, event.chat_id, self.account):
            return  # Outside the receiving scope: never stored, not a capture fault.
        if len((event.raw_text or '').encode('utf-8')) > 60000:
            raise RuntimeError('raw ingress too large')
        payload = bytes(m)
        # Transport dedup only; the provider identity is derived from content later.
        key = digest([self.account,event.chat_id,m.id,kind,str(getattr(m,'edit_date',None)),hashlib.sha256(payload).hexdigest()])
        self.journal.capture_raw(key,{'account_id':self.account,'peer':event.chat_id,'actor':event.sender_id,'kind':kind,'private':bool(event.is_private)},payload)
    async def _hydrate_row(self, row):
        from telethon.extensions import BinaryReader
        from types import SimpleNamespace
        from telethon import errors
        meta=json.loads(row['metadata'])
        if receiving_refusal(self.current(), meta['peer'], self.account):
            self.journal.raw_done(row['id']); return
        with BinaryReader(row['payload']) as reader:
            m=reader.tgread_object()
        async def get_sender(): return await self.adapter.client.get_entity(meta['actor'])
        async def get_chat(): return await self.adapter.client.get_entity(meta['peer'])
        event=SimpleNamespace(message=m,chat_id=meta['peer'],sender_id=meta['actor'],raw_text=getattr(m,'message','') or '',is_private=meta['private'],get_sender=get_sender,get_chat=get_chat)
        try:
            await asyncio.wait_for(self.capture(event,meta['kind'],raw_id=row['id']),90)
        except errors.FloodWaitError as exc:
            self.journal.raw_defer(row['id'],max(1,exc.seconds),'flood_wait')
            self.journal.flood(self.account,exc.seconds)
            return
        except TopicRoutingUnavailable:
            # Retain this peer's exact raw head; other peers and tools keep moving.
            self.journal.raw_defer(row['id'],60,'topic_routing_unavailable')
            return
        except (ConnectionError,TimeoutError,asyncio.TimeoutError,errors.RPCError) as exc:
            import traceback
            frames='/'.join(frame.name for frame in traceback.extract_tb(exc.__traceback__)[-5:])
            self.journal.raw_defer(row['id'],30,'provider_read_retry:'+type(exc).__name__+':'+frames)
            return
        self.journal.raw_done(row['id'])
    async def hydrate_once(self):
        """Hydrate each due peer head once: peers side by side, each peer in its own order."""
        if self.journal.flood_remaining(self.account): return
        rows = [row for row in self.journal.raw_heads(self.account) if row['due'] <= time.time()]
        gate = asyncio.Semaphore(self.hydration_limit)
        async def one(row):
            async with gate:
                if not self.journal.flood_remaining(self.account):
                    await self._hydrate_row(row)
        for outcome in await asyncio.gather(*(one(row) for row in rows), return_exceptions=True):
            if isinstance(outcome, BaseException):
                raise outcome
    def hydrate_pump(self):
        """Start due peer heads without waiting for another peer's slow provider read.

        A peer's unresolved head still holds that peer's later messages; it never
        holds a different peer, the dispatcher or Host intake. Unexpected failures
        of a finished worker surface here.
        """
        for key, task in list(self._hydrating.items()):
            if task.done():
                del self._hydrating[key]
                task.result()
        if self.journal.flood_remaining(self.account):
            return
        for row in self.journal.raw_heads(self.account):
            if len(self._hydrating) >= self.hydration_limit:
                break
            key = str(json.loads(row['metadata']).get('peer'))
            if key in self._hydrating or row['due'] > time.time():
                continue
            self._hydrating[key] = asyncio.create_task(self._hydrate_row(row), name='telegram-media-hydration:' + key)
    async def stop_hydration(self):
        tasks = list(self._hydrating.values())
        self._hydrating.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    async def hydrate_forever(self, tick=0.5):
        """Hydration supervisor, separate from Host intake and outbound dispatch."""
        try:
            while True:
                self.hydrate_pump()
                await asyncio.sleep(tick)
        finally:
            await self.stop_hydration()
    async def capture(self, event, kind='new', raw_id=None):
        m = event.message
        if m.out or not event.chat_id or not event.sender_id:
            return
        if kind == 'edit' and getattr(m, 'edit_date', None) is None:
            return
        p = self.current(event.chat_id)
        if len((event.raw_text or '').encode('utf-8')) > 60000:
            raise RuntimeError('ingress text exceeds capture bound')
        # All supported provider events are reported; host wakes decide attention.
        try:
            sender = await event.get_sender()
        except ValueError:
            # A valid public sender ID need not have an access hash in this
            # session's entity cache. Display-profile enrichment is optional;
            # keep the authenticated numeric actor and leave profile/bot unknown.
            sender = None
        chat = await event.get_chat()
        topic = await self.thread_of(chat, event.chat_id, m)
        if kind == 'edit' and self.legacy_edit_seen(event.chat_id, topic, m, event.sender_id):
            return  # Already captured before 0.3 under its byte-based revision; never a second wake.
        facts = ['any_message']
        if getattr(event,'is_private',False): facts.append('direct_message')
        if getattr(m,'mentioned',False): facts.append('mention')
        if getattr(sender,'bot',False): facts.append('from_bot')
        reply_id = getattr(getattr(m,'reply_to',None),'reply_to_msg_id',None)
        if reply_id:
            target = await self.adapter.client.get_messages(event.chat_id, ids=reply_id)
            if target is not None and getattr(target,'sender_id',None) == int(self.account):
                facts.append('reply_to_me')
        media = media_descriptor(m)
        files = []
        if media is not None and getattr(m,'file',None) is not None:
            size = getattr(m.file, 'size', 0) or 0
            if size > 32*1024*1024:
                media.update(status='too_large', note='Not staged: larger than 32 MiB. The Telegram message reference is retained.')
            else:
                directory = self.root / 'incoming-media'
                directory.mkdir(exist_ok=True)
                if directory.is_symlink(): raise ValueError('media directory redirected')
                name = digest([self.account,event.chat_id,m.id,kind,str(getattr(m,'edit_date',None)),getattr(getattr(m,'document',None),'id',None),hashlib.sha256(bytes(m.media)).hexdigest()]) + (m.file.ext or '.bin')
                path = directory / name
                if path.is_symlink(): raise ValueError('media path redirected')
                if not path.exists():
                    temporary=path.with_name('.'+name+'.download')
                    if temporary.is_symlink(): raise ValueError('temporary media path redirected')
                    try:
                        missing = await self.download_snapshot(event.chat_id,m,temporary)
                        if missing == _CHANGED_MEDIA:
                            media.update(status='changed', note='Original Telegram media was removed or replaced; newer media was not substituted. The Telegram message reference is retained.')
                        elif missing:
                            media.update(status='unavailable', reason=missing, note='Original Telegram media unavailable ('+missing+'). The Telegram message reference is retained.')
                        elif temporary.is_file(): os.replace(temporary,path)
                    finally: temporary.unlink(missing_ok=True)
                if path.is_file():
                    files.append(str(path))
                    media['status'] = 'staged'
                elif 'status' not in media:
                    media.update(status='unavailable', reason='no_bytes', note='Telegram returned no media bytes. The Telegram message reference is retained.')
        elif media is not None:
            media['status'] = 'not_downloadable'
        revision = content_revision(m) if kind=='edit' else ''
        title, chat_kind, chat_username, members = chat_facts(chat, bool(getattr(event,'is_private',False)))
        message = {'username':getattr(sender,'username',None), 'sender_name':getattr(sender,'first_name',''),
                   'sender_profile_status':'available' if sender is not None else 'unavailable',
                   'mentioned':bool(m.mentioned), 'sent_at':iso(getattr(m,'date',None)),
                   'reply_to_message_id':getattr(getattr(m,'reply_to',None),'reply_to_msg_id',None)}
        if getattr(m,'edit_date',None) is not None:
            message['edited_at'] = iso(m.edit_date)
        if media is not None:
            message['media'] = media
        e = build_event(int(self.account),event.chat_id,m.id,event.sender_id,canonical_text(m),topic_id=topic,event_kind=kind,revision=revision,
              actor=actor_facts(sender), conversation={'title': title, 'chat_type': chat_kind}, message=message, source_order=True)
        e['facts'] = facts
        self.journal.capture(e, files, raw_id=raw_id)
        self.journal.remember_chat(self.account, event.chat_id, title, chat_kind, 'incoming', chat_username, members)
    def legacy_edit_seen(self, peer, topic, message, actor):
        """Whether a pre-0.3 capture of this edit time already says what this edit says."""
        return bool(self.legacy_edit_captures(peer, topic, message, actor))

    def legacy_edit_captures(self, peer, topic, message, actor):
        """Source IDs of the pre-0.3 captures of this edit time that say what this edit says.

        The evidence is that one message's edit rows. A retained raw update is compared
        by content, so other text, entities or media edited within the same Telegram
        second are a new revision while a reaction or redelivery is not. Without one,
        only the stored text remains: an entity- or media-only change within that
        second is then indistinguishable and counts as already captured.
        """
        from telethon.errors import TypeNotFoundError
        from telethon.extensions import BinaryReader
        revision = content_revision(message)
        kind = type(getattr(message, 'media', None) or getattr(message, 'action', None)).__name__
        found = []
        for stored, payload in self.journal.legacy_edits(self.account, peer, topic, message.id, actor, str(message.edit_date)):
            earlier = None
            if payload is not None:
                try:
                    with BinaryReader(payload) as reader:
                        earlier = reader.tgread_object()
                except (TypeNotFoundError, BufferError):
                    pass  # This Telethon cannot read those bytes; the stored text remains.
            if earlier is not None:
                if content_revision(earlier) == revision:
                    found.append(stored['source_event_id'])
            elif legacy_text_matches(str(stored.get('text') or ''), getattr(message, 'message', '') or '', kind):
                found.append(stored['source_event_id'])
        return found
    async def download_snapshot(self, peer, message, temporary):
        """Refresh expiring references, never a changed immutable media snapshot."""
        from telethon import errors, utils
        current = message
        for attempt in range(2):
            try:
                await self.adapter.client.download_media(current,file=str(temporary))
                return None
            except (errors.FileReferenceExpiredError,errors.FilerefUpgradeNeededError):
                if attempt: raise  # Refreshed reference still transiently unavailable.
                fresh=await self.adapter.client.get_messages(peer,ids=message.id)
                original=getattr(message,'document',None) or getattr(message,'photo',None)
                media=getattr(fresh,'document',None) or getattr(fresh,'photo',None)
                if (fresh is None or fresh.id!=message.id or original is None
                        or str(utils.get_peer_id(fresh.peer_id))!=str(peer)
                        or type(media) is not type(original) or media.id!=original.id):
                    return _CHANGED_MEDIA
                temporary.unlink(missing_ok=True)
                current=fresh
            except (errors.BadRequestError,errors.ForbiddenError) as exc:
                # A provider-declared unsupported/inaccessible download is an
                # explicit missing attachment, not a forever-blocked peer.
                return type(exc).__name__

    async def thread_of(self, chat, peer, message, catalog=None, guard=None):
        """The thread a message belongs to, the same for live capture and history import.

        A forum message gets its forum topic; elsewhere a message thread's top names it.
        A history page passes the forum catalog it already read; live capture reads one.
        ``guard``, when given, runs before each Telegram read and may raise to stop it.
        """
        if getattr(chat,'forum',False):
            return await self.forum_topic(peer, message, catalog, guard)
        return getattr(getattr(message,'reply_to',None), 'reply_to_top_id', None)

    async def forum_topic(self, peer, message, catalog=None, guard=None):
        """Refresh once, preserving an explicit top over any reply ancestry."""
        from .topic import classify_message
        from .runtime import UserSessionBridge
        check = guard or (lambda: None)
        # A guard also runs between the catalog's own pages; without one the adapter is called as before.
        topics = (lambda: self.adapter.topic_ids(peer)) if guard is None else (lambda: self.adapter.topic_ids(peer, guard=guard))
        try:
            if catalog is None:
                check()
                catalog = await topics()
            try:
                return classify_message(message, catalog)
            except ValueError:
                check()
                catalog = await topics()
                try:
                    return classify_message(message, catalog)
                except ValueError:
                    header = getattr(message, 'reply_to', None)
                    if (getattr(header, 'reply_to_top_id', None) is not None
                            or getattr(message, 'action', None) is not None):
                        raise
                    reply_id = getattr(header, 'reply_to_msg_id', None)
                    if type(reply_id) is not int or reply_id <= 0:
                        raise
                    resolver = object.__new__(UserSessionBridge)
                    resolver.adapter = self.adapter
                    return await resolver._resolve_forum_target(peer, reply_id, catalog, guard=guard)
        except ValueError as exc:
            raise TopicRoutingUnavailable('forum destination unresolved; raw event retained') from exc

    def consume_outputs(self, body, event):
        if str(event.get('account_id','')) != self.account:
            raise ValueError('retained event belongs to another account')
        candidates = list(body.get('outputs') or [])
        if body.get('output_ref'):
            candidates.append(body)
        # A version-2 output names the last input it covers; the reply stays in this event's
        # chat and topic, and an output without an anchor answers the event itself.
        conversation = 'telegram:%s:%s:%s' % (event['account_id'], event['conversation_id'], event.get('thread_id') or '0')
        for out in candidates:
            if out.get('outcome') not in {'message','deferred'} or not out.get('output_ref') or not out.get('text','').strip():
                continue
            # Output identity, not text or producer work id, deduplicates speech.
            text = out['text']
            reply = anchored_message_id(out.get('reply_anchor'), conversation) or int(event['message']['message_id'])
            # Split UTF-8 safely into independently recorded physical parts.
            chunks, current = [], ''
            for char in text:
                if len((current+char).encode()) > 4096:
                    chunks.append(current); current=''
                current += char
            if current: chunks.append(current)
            for n, chunk in enumerate(chunks):
                key = 'auto:'+digest(out['output_ref'])+':'+str(n)
                queued = self.journal.get(self.account, key)
                if queued is not None and json.loads(queued['args']).get('text') == chunk:
                    continue  # Queued once under this output identity; the target it was given stands.
                args = {'peer':event['conversation_id'], 'text':chunk, 'reply_id':reply}
                if event['thread_id']: args['topic_id']=int(event['thread_id'])
                self.journal.enqueue(self.account, key, 'send_text', args,
                    origin={'kind':'automatic','source_event_id':event['source_event_id'], 'task_id':str(body.get('task_id') or body.get('turn_ref') or '')})
    def retry_delay(self, seq, body):
        """Seconds until the same event is offered again; it stays its conversation's head."""
        after = retry_seconds(body.get('retry_after'))
        if self.continuation_version == EARLY_CONTINUATION and body.get('code') == 'presence_capacity_full':
            # Busy: nothing ran. Again after the Host's own wait, never the long backoff of a failure.
            return busy_delay(str(body.get('field') or ''), after)
        return after or 30
    async def submit(self, row):
        """Offer one conversation's oldest event the Host has not taken yet.

        A reference in the answer is custody: the event's observation moves to observe_once
        and its conversation's next event may be offered. Version 2 answers ``continuing``
        as soon as the author takes the conversation's later events, so that next event meets
        an author that hears it. A retry, including a busy conversation or capacity, keeps
        the event the head; the same bytes go again after the delay.
        """
        e = json.loads(row['event'])
        refs = json.loads(row['refs'])
        if str(e.get('account_id','')) != self.account:
            self.journal.inbox_update(row['seq'],'held',refs)
            return
        try:
            self.current(e['conversation_id'])
            code, body = await asyncio.to_thread(self.host.call,'POST','/presence/turn',{'event':e, 'staged_files':json.loads(row['files']), 'delivery_reporting_version':1, 'continuation_version':self.continuation_version})
            self.journal.observe_inbox(row['seq'],code,body)
            if body.get('disposition') == 'retry':
                self.journal.inbox_update(row['seq'],'pending',[],self.retry_delay(row['seq'], body))
                return  # Same immutable POST after the Host's certified reset.
            if body.get('disposition') == 'retained' and body.get('turn_ref'):
                refs.append(body['turn_ref'])
            elif body.get('ok') is not True:
                if body.get('disposition') in {'blocked','rejected'}:
                    self.journal.inbox_update(row['seq'],'held',refs); return
                raise RuntimeError('host intake retry')
            self.consume_outputs(body,e)
            for key in ('continuation_ref','work_ref','child_work_ref'):
                if body.get(key) and body[key] not in refs: refs.append(body[key])
            if body.get('status') not in {'completed','failed','cancelled'} and body.get('turn_ref') and body['turn_ref'] not in refs:
                refs.append(body['turn_ref'])
            self.journal.inbox_update(row['seq'],'pending' if refs else 'done',refs,3)
        except (RuntimeError, OSError, TimeoutError):
            self.journal.inbox_update(row['seq'],'pending',refs,30)
        except ValueError as exc:
            if type(exc) is ValueError and str(exc) in EXECUTOR_CHANGED:
                return  # Untouched: no post here; a matching companion rechecks it.
            # Exclusion, selection, a Host refusal or an output identity conflict.
            self.journal.inbox_update(row['seq'],'held',refs)
    def intake_pump(self):
        """Start each due conversation head without waiting for another conversation's answer.

        One submission per conversation at a time keeps its order; finished submissions'
        unexpected failures surface here.
        """
        for key, task in list(self._submitting.items()):
            if task.done():
                del self._submitting[key]
                task.result()
        for row in self.journal.submissions(self.account):
            if len(self._submitting) >= self.intake_limit:
                break
            key = json.loads(row['event']).get('conversation_key') or row['id']
            if key not in self._submitting:
                self._submitting[key] = asyncio.create_task(self.submit(row), name='telegram-host-submission')
    async def stop_intake(self):
        tasks = list(self._submitting.values())
        self._submitting.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    async def intake_forever(self, tick=0.5):
        """Host intake supervisor, separate from reference observation and outbound dispatch."""
        try:
            while True:
                self.intake_pump()
                await asyncio.sleep(tick)
        finally:
            await self.stop_intake()
    async def submit_once(self):
        """One intake pass: every due conversation head, side by side."""
        gate = asyncio.Semaphore(self.intake_limit)
        async def one(row):
            async with gate:
                await self.submit(row)
        for outcome in await asyncio.gather(*(one(row) for row in self.journal.submissions(self.account)), return_exceptions=True):
            if isinstance(outcome, BaseException):
                raise outcome
    async def observe_once(self):
        """Read the references of events in Host custody; settle each event once none is live.

        Every output the Host selected is queued once by its output identity; a child is
        read beside its parent until its own end. Nothing here submits an event again.
        """
        for row in self.journal.observations(self.account):
            e = json.loads(row['event'])
            refs = json.loads(row['refs'])
            ended = self._ended.setdefault(row['seq'], set())
            try:
                self.current(e['conversation_id'])
                live = []
                for ref in list(refs):
                    code, body = await asyncio.to_thread(self.host.call,'GET','/presence/work/'+quote(ref,safe=''))
                    self.journal.observe_inbox(row['seq'],code,body)
                    if body.get('ok') is not True:
                        if body.get('disposition') in {'blocked','rejected'}:
                            raise ValueError('host work reference refused; exact response retained')
                        raise RuntimeError('host work unavailable')
                    self.consume_outputs(body,e)
                    if body.get('status') not in {'completed','failed','cancelled','interrupted'}: live.append(ref)
                    else: ended.add(ref)
                    child = body.get('child_work_ref')
                    if child and child not in refs+live and child not in ended: live.append(child)
                refs = list(dict.fromkeys(live))
                self.journal.inbox_update(row['seq'],'pending' if refs else 'done',refs,3)
                if not refs:
                    self._ended.pop(row['seq'], None)
            except (RuntimeError, OSError, TimeoutError):
                self.journal.inbox_update(row['seq'],'pending',refs,30)
            except ValueError as exc:
                if type(exc) is ValueError and str(exc) in EXECUTOR_CHANGED:
                    return  # Untouched: no poll here; a matching companion rechecks it.
                # A Host refusal or an output identity conflict.
                self.journal.inbox_update(row['seq'],'held',refs)
                self._ended.pop(row['seq'], None)
    async def incoming_once(self):
        """One intake pass, then one observation pass; the companion runs them as separate workers."""
        await self.submit_once()
        await self.observe_once()
    async def reports_once(self):
        for row in self.journal.pending_reports():
            try:
                code, body = await asyncio.to_thread(self.host.call, 'POST','/presence/delivery',json.loads(row['body']))
                if code==200 and body.get('recorded') is True:
                    self.journal.report_done(row['id'])
                else:
                    self.journal.report_defer(row['id'], 'host_'+str(code), permanent=code in (400,409))
            except (OSError,ValueError,TimeoutError):
                self.journal.report_defer(row['id'],'transport_unavailable')
    async def refresh_chats(self):
        """Read the dialog list once when the owner asked, for the receiving-scope editor.

        The list is owner-facing metadata (titles and kinds), including excluded
        chats so they can be included again. It is not shown to the model.
        """
        request = self.root / 'chats-refresh.json'
        if not request.is_file() or request.is_symlink() or self.journal.flood_remaining(self.account):
            return False
        claimed = self.root / 'chats-refresh.inflight'
        os.replace(request, claimed)
        from telethon import errors
        status = {'at': datetime.now(timezone.utc).isoformat()}
        try:
            dialogs = await asyncio.wait_for(self.adapter.client.get_dialogs(limit=200, ignore_migrated=True), 90)
            for dialog in dialogs:
                title, kind, username, members = chat_facts(dialog.entity, bool(getattr(dialog, 'is_user', False)))
                self.journal.remember_chat(self.account, dialog.id, title, kind, 'dialogs', username, members)
            status.update(status='done', count=len(dialogs), complete=len(dialogs) < 200)
        except errors.FloodWaitError as exc:
            self.journal.flood(self.account, exc.seconds, type(exc).__name__)
            status.update(status='flood_wait', retry_after_seconds=exc.seconds)
        except (ConnectionError, TimeoutError, asyncio.TimeoutError, errors.RPCError) as exc:
            status.update(status='failed', reason=type(exc).__name__)
        finally:
            claimed.unlink(missing_ok=True)
        _write_json(self.root, 'chats-refresh-status.json', status)
        return True
    async def observe_place(self, every=30):
        """Owner display of the strictest Telegram place state; the Host stays the authority."""
        now = time.time()
        if now - self._place_observed < every:
            return
        self._place_observed = now
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            place = await asyncio.to_thread(self.host.place, self.account, '', '')
            row = {'state': place['state'], 'label': str(place.get('label') or 'Telegram')[:80], 'at': stamp}
        except (RuntimeError, OSError, ValueError, TimeoutError):
            row = {'state': 'unavailable', 'at': stamp}
        _write_json(self.root, 'place-state.json', row)

def record_host_facts(root, identity):
    _write_json(Path(root), 'host-capabilities.json',
                {'at': datetime.now(timezone.utc).isoformat(),
                 **{key: identity[key] for key in HOST_FACTS if type(identity.get(key)) is int}})

async def record_profile(root, adapter):
    """Own profile for the widget: no phone, no session facts."""
    me = await adapter.client.get_me()
    if me is not None:
        _write_json(Path(root), 'account-profile.json',
                    {'id': str(me.id), 'username': getattr(me, 'username', None) or '',
                     'first_name': getattr(me, 'first_name', None) or '', 'last_name': getattr(me, 'last_name', None) or ''})

async def serve(root, host_url, token, adapter_factory, login, verify, health):
    from telethon import events
    cfg = load_config(root)
    while cfg is None:
        await asyncio.sleep(2); cfg=load_config(root)
    host = Host(host_url, token)
    identity = await asyncio.to_thread(host.identity)
    with session_lock(root):
        # Recorded only once this companion owns the session: a start the lock refused beside a
        # running companion must not stand as the latest start (see the import route).
        if isinstance(identity, dict):
            record_host_facts(root, identity)
        adapter=adapter_factory(Path(root)/'mtproto.session',cfg.api_id,cfg.api_hash)
        await adapter.connect()
        try:
            while not await login(root,cfg,adapter):
                health(root,'login_required'); await asyncio.sleep(2)
            account = await adapter.get_me()
            cfg=verify(root,cfg,account)
            try:
                await record_profile(root, adapter)
            except Exception:
                pass  # Display only; the verified numeric account is authoritative.
            # Under the session lock no other companion can be reading the chat list.
            (Path(root)/'chats-refresh.inflight').unlink(missing_ok=True)
            with Journal(root,recover=True) as journal:
                from .history_import import HistoryImporter, interrupt_running
                # A restart interrupts a running import; the owner resumes it explicitly.
                interrupt_running(journal)
                engine=Participant(root,adapter,host,journal,account)
                # Version 2 only when the Host read at startup lists it; a v1 turn waits for a park or its end.
                engine.continuation_version=continuation_version(identity)
                importer=HistoryImporter(engine,journal,account)
                engine.current()
                faults=[]
                async def capture(e,kind):
                    try: engine.spool(e,kind)
                    except asyncio.CancelledError: raise
                    except Exception as exc:
                        faults.append(type(exc).__name__)
                        health(root,'ingress_failed')
                async def new(e): await capture(e,'new')
                async def edit(e): await capture(e,'edit')
                adapter.client.add_event_handler(new,events.NewMessage(incoming=True))
                adapter.client.add_event_handler(edit,events.MessageEdited(incoming=True))
                from telethon import errors
                while True:
                    try:
                        remaining=journal.flood_remaining(account)
                        if remaining: await asyncio.sleep(remaining)
                        await adapter.client.catch_up()
                        break
                    except errors.FloodWaitError as exc:
                        journal.flood(account,exc.seconds)
                        health(root,'provider_retry')
                        await asyncio.sleep(max(1,exc.seconds))
                    except (ConnectionError,TimeoutError,errors.RPCError):
                        health(root,'provider_retry');await asyncio.sleep(30)
                # Each worker is tracked: a finished one surfaces its failure here,
                # and shutdown cancels and joins every one before disconnecting.
                workers = {}
                def keep(name, start):
                    task = workers.get(name)
                    if task is not None and task.done():
                        del workers[name]
                        task.result()
                        task = None
                    if task is None:
                        workers[name] = asyncio.create_task(start(), name=name)
                try:
                    while True:
                        if faults: raise RuntimeError('ingress unavailable; reconcile before restart')
                        engine.current()
                        if (Path(root)/'login-request.json').exists() or (Path(root)/'auth-handoff.json').exists():
                            await login(root,cfg,adapter)
                        health(root,'running')
                        # Hydration has its own supervisor; one peer's media read holds no other peer.
                        keep('telegram-media-hydration', engine.hydrate_forever)
                        # A Host turn can await a model that needs our outbound tools.
                        # Owned intake and observation workers never block those tools,
                        # and reading a running author's references never holds the next
                        # event of its conversation, nor one conversation another.
                        keep('telegram-host-intake', engine.intake_forever)
                        keep('telegram-host-observe', engine.observe_once)
                        keep('telegram-chat-list', engine.refresh_chats)
                        # The place chip is display only; a slow Host read must not hold tools.
                        keep('telegram-place-display', engine.observe_place)
                        # An owner-started history import takes one bounded step at a time.
                        keep('telegram-history-import', importer.once)
                        if asyncio.get_running_loop().time() >= engine.next_send:
                            await engine.dispatch_one()
                        await engine.reports_once()
                        await asyncio.sleep(1)
                finally:
                    running = list(workers.values())
                    for task in running: task.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
                    await engine.stop_hydration()
                    await engine.stop_intake()
        finally:
            await adapter.disconnect()
