"""Named Telegram operations, not arbitrary RPC; Telethon 1.45 constructors.

The companion owns the only client. Preparation may resolve entities/upload a file;
only execute crosses the durable effect boundary. Upload preparation cannot itself
post a message, join, moderate, or create a pack. Telegram enforces actual rights.
"""
from __future__ import annotations
import inspect
import json
from pathlib import Path
from .participant_state import asset

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
PEER = {"type": "string", "description": "Numeric Telegram peer ID or public @username; not display name."}
# name: (effect, required property names, optional property names)
SPECS = {
 "dialogs": ("read", {}, {"limit": I}),
 "resolve": ("read", {"peer": PEER}, {}),
 "search": ("read", {"query": S}, {"limit": I}),
 "history": ("read", {"peer": PEER}, {"limit": I, "offset_id": I, "topic_id": I}),
 "members": ("read", {"peer": PEER}, {"limit": I, "offset": I}),
 "topics": ("read", {"peer": PEER}, {"limit": I, "offset_id": I, "offset_topic": I}),
 "sticker_pack": ("read", {"short_name": S}, {}),
 "profile": ("read", {}, {}),
 "username_check": ("read", {"username": S}, {}),
 "username_set": ("write", {"username": S}, {}),
 "profile_update": ("write", {}, {"first_name": S, "last_name": S, "about": S}),
 "avatar_set": ("write", {"asset": S}, {}),
 "send_text": ("message", {"peer": PEER, "text": S}, {"reply_id": I, "topic_id": I}),
 "send_media": ("message", {"peer": PEER, "asset": S}, {"text": S, "reply_id": I, "topic_id": I, "photo": B}),
 "send_sticker": ("message", {"peer": PEER}, {"source_peer": PEER, "message_id": I, "short_name": S, "document_id": S, "reply_id": I, "topic_id": I}),
 "forward": ("message", {"peer": PEER, "source_peer": PEER, "message_id": I}, {"topic_id": I}),
 "edit": ("write", {"peer": PEER, "message_id": I, "text": S}, {"topic_id": I}),
 "delete": ("write", {"peer": PEER, "message_id": I}, {"topic_id": I}),
 "react": ("message", {"peer": PEER, "message_id": I, "emoji": S}, {"topic_id": I}),
 "join": ("write", {"peer": PEER}, {}),
 "leave": ("write", {"peer": PEER}, {}),
 "create_chat": ("write", {"title": S}, {"about": S, "forum": B, "broadcast": B}),
 "invite": ("write", {"peer": PEER, "user": PEER}, {}),
 "ban": ("write", {"peer": PEER, "user": PEER}, {"until": I}),
 "unban": ("write", {"peer": PEER, "user": PEER}, {}),
 "mute": ("write", {"peer": PEER, "user": PEER}, {"until": I}),
 "unmute": ("write", {"peer": PEER, "user": PEER}, {}),
 "admin": ("write", {"peer": PEER, "user": PEER}, {"change_info": B, "post_messages": B, "edit_messages": B, "delete_messages": B, "ban_users": B, "invite_users": B, "pin_messages": B, "add_admins": B, "manage_topics": B, "rank": S}),
 "pin": ("write", {"peer": PEER, "message_id": I}, {"unpin": B, "notify": B, "topic_id": I}),
 "chat_title": ("write", {"peer": PEER, "title": S}, {}),
 "topic_create": ("write", {"peer": PEER, "title": S}, {}),
 "topic_edit": ("write", {"peer": PEER, "topic_id": I}, {"title": S, "closed": B, "hidden": B}),
 "pack_create": ("write", {"short_name": S, "title": S, "source_peer": PEER, "message_id": I, "emoji": S}, {}),
 "pack_add": ("write", {"short_name": S, "source_peer": PEER, "message_id": I, "emoji": S}, {}),
 "pack_remove": ("write", {"source_peer": PEER, "message_id": I}, {}),
 "pack_move": ("write", {"source_peer": PEER, "message_id": I, "position": I}, {}),
 "pack_rename": ("write", {"short_name": S, "title": S}, {}),
 "pack_install": ("write", {"short_name": S}, {"remove": B}),
 "sticker_upload": ("write", {"asset": S, "emoji": S}, {}),
}
# Writes acting on an existing message take its forum topic when topic_id is omitted;
# a reply_id does the same. The queue keeps their order until preparation reads it.
TARGET_TOPIC_OPS = frozenset({"delete", "edit", "react", "pin"})

def validate(op, args):
    if op not in SPECS or type(args) is not dict:
        raise ValueError("unknown operation")
    effect, required, optional = SPECS[op]
    if not set(required) <= set(args) or set(args) - set(required) - set(optional):
        raise ValueError("operation fields missing or unknown")
    for k, v in args.items():
        t = (required | optional)[k]["type"]
        if t == "string" and (not isinstance(v, str) or len(v.encode('utf-8')) > 16384):
            raise ValueError("invalid string field: " + k)
        if t == "integer" and type(v) is not int:
            raise ValueError("integer field required: " + k)
        if t == "boolean" and type(v) is not bool:
            raise ValueError("boolean field required: " + k)
        if k in {"peer", "source_peer", "user", "asset", "title", "short_name", "query"} and not str(v).strip():
            raise ValueError("nonempty field required: " + k)
        if k in {"message_id", "reply_id", "topic_id"} and not 0 < v < 2**31:
            raise ValueError("positive Telegram int32 required")
        if k in {"offset", "offset_id", "offset_topic", "position", "until"} and v < 0:
            raise ValueError("negative cursor/time/position")
    if not 1 <= args.get("limit", 50) <= 100:
        raise ValueError("limit must be 1..100")
    if len(args.get("text", "").encode("utf-8")) > 4096:
        raise ValueError("text max 4096 UTF-8 bytes; split deliberately")
    if op == 'send_sticker':
        source = 'source_peer' in args and 'message_id' in args
        pack = 'short_name' in args and 'document_id' in args
        if source == pack or (pack and ('source_peer' in args or 'message_id' in args)) or (source and ('short_name' in args or 'document_id' in args)):
            raise ValueError('choose source_peer/message_id OR short_name/document_id')
    if op == "send_text" and not args["text"].strip():
        raise ValueError("empty message")
    if op == "react" and len(args["emoji"].encode()) > 64:
        raise ValueError("reaction too long")
    if op == "create_chat" and args.get("forum") and args.get("broadcast"):
        raise ValueError("broadcast channels cannot be forums")
    if op == 'profile_update' and not args:
        raise ValueError('at least one profile field is required')
    if op in {'username_check','username_set'} and not args['username'].strip():
        raise ValueError('nonempty username required; Telegram checks its actual rules')
    return effect

def peer_value(value):
    return int(value) if value.lstrip('-').isdigit() else value

def public_entity(e):
    from telethon import utils
    return {"id": str(utils.get_peer_id(e)), "title": getattr(e, "title", None),
            "username": getattr(e, "username", None), "first_name": getattr(e, "first_name", None),
            "bot": bool(getattr(e, "bot", False)), "forum": bool(getattr(e, "forum", False)),
            "default_restrictions": {k:bool(getattr(e.default_banned_rights,k,False)) for k in ('view_messages','send_messages','send_media','send_docs','send_stickers','send_photos','send_videos','send_audios','send_voices')} if getattr(e,'default_banned_rights',None) else None,
            "my_admin_rights": {k:bool(getattr(e.admin_rights,k,False)) for k in ('change_info','delete_messages','ban_users','invite_users','pin_messages','add_admins','manage_topics')} if getattr(e,'admin_rights',None) else None}

def public_document(d):
    from telethon import types as T
    return {'id': str(d.id), 'mime': d.mime_type,
            'attributes': [type(x).__name__ for x in d.attributes],
            'sticker': any(isinstance(x, T.DocumentAttributeSticker) for x in d.attributes)}

def public_message(m):
    return {'document': public_document(m.document) if getattr(m, 'document', None) else None,"id": m.id, "text": getattr(m, "message", "") or "", "sender_id": getattr(m, "sender_id", None),
            "date": m.date.isoformat() if getattr(m, "date", None) else None,
            "media": type(m.media).__name__ if getattr(m, "media", None) else None,
            "reply_to_top_id": getattr(getattr(m, "reply_to", None), "reply_to_top_id", None)}

class Operations:
    def __init__(self, adapter, root, account=None):
        self.adapter, self.client, self.root, self.account = adapter, adapter.client, Path(root), account

    def in_scope(self, items):
        """Listings omit chats outside the receiving scope, as reads of them are refused."""
        from .participant_state import policy, receiving_refusal
        current = policy(self.root)
        kept = [item for item in items if receiving_refusal(current, int(item["id"]), self.account) is None]
        return kept, len(items) - len(kept)

    async def message_topic(self, peer, message):
        entity = await self.client.get_entity(peer)
        if not getattr(entity,'forum',False): return None
        catalog = await self.adapter.topic_ids(peer)
        from .topic import classify_message
        try: return classify_message(message,catalog)
        except ValueError:
            from .runtime import UserSessionBridge
            bridge=object.__new__(UserSessionBridge);bridge.adapter=self.adapter
            return await bridge._resolve_forum_target(peer,message.id,catalog,first=message)

    async def prepare(self, op, args):
        from telethon import functions as F, types as T, utils
        validate(op, args)
        a = dict(args)
        for key in ("peer", "source_peer", "user"):
            if key in a:
                value = a[key]
                if op == "join" and key == "peer" and ('t.me/+' in value or 'joinchat/' in value):
                    a["invite_hash"] = value.rstrip('/').rsplit('/', 1)[-1].lstrip('+')
                    a[key] = None
                else:
                    a[key] = await self.client.get_input_entity(peer_value(value))
                    a['resolved_'+key] = str(await self.adapter.get_me()) if isinstance(a[key],T.InputPeerSelf) else str(utils.get_peer_id(a[key]))
        a['resolved_peer'] = a.get('resolved_peer','')
        if a.get('invite_hash'):
            inspection = await self.client(F.messages.CheckChatInviteRequest(a['invite_hash']))
            chat = getattr(inspection,'chat',None)
            if chat is not None:
                a['resolved_peer'] = str(utils.get_peer_id(chat))
            else:
                from .participant_state import policy
                current = policy(self.root)
                if current['blocked_peers'] or current['scope'] == 'selected':
                    raise ValueError('invite identity unresolved: exclusions cannot be checked')
        guard = getattr(self, 'guard', None)
        if guard is not None:
            # Resolve aliases before any content/participant reads, not only
            # before the eventual mutation.
            await guard(a['resolved_peer'], a.get('topic_id',''), SPECS[op][0] != 'read')
            if a.get('source_peer') is not None:
                await guard(a['resolved_source_peer'], '', False)
            if a.get('user') is not None:
                await guard(a['resolved_user'], '', SPECS[op][0] != 'read', person=True)
        if op in TARGET_TOPIC_OPS:
            target = await self.client.get_messages(a['peer'],ids=a['message_id'])
            actual = getattr(target,'peer_id',None)
            if actual is None or str(utils.get_peer_id(actual)) != a['resolved_peer']:
                raise ValueError('message target belongs to another conversation or is unavailable')
            actual_topic = await self.message_topic(a['peer'],target)
            if args.get('topic_id') is not None and args['topic_id'] != actual_topic:
                raise ValueError('message target belongs to another topic')
            a['topic_id'] = actual_topic
        if op in {'leave','chat_title'}:
            a['entity'] = await self.client.get_entity(a['peer'])
        guard = getattr(self, 'guard', None)
        if guard is not None:
            await guard(a['resolved_peer'], a.get('topic_id',''), SPECS[op][0] != 'read')
            if a.get('source_peer') is not None:
                await guard(a['resolved_source_peer'], '', False)
            if a.get('user') is not None:
                await guard(a['resolved_user'], '', SPECS[op][0] != 'read', person=True)
        if a.get("topic_id"):
            topics = await self.adapter.topic_ids(peer_value(args["peer"]))
            if a["topic_id"] not in topics:
                raise ValueError("unknown topic")
        if a.get("reply_id"):
            target = await self.client.get_messages(a["peer"], ids=a["reply_id"])
            if target is None:
                raise ValueError("reply target missing")
            actual_topic = await self.message_topic(a['peer'],target)
            if args.get('topic_id') is not None and args['topic_id'] != actual_topic:
                raise ValueError('reply belongs to another topic')
            a['topic_id'] = actual_topic
        if "source_peer" in a:
            source = await self.client.get_messages(a["source_peer"], ids=a["message_id"])
            if source is None or getattr(source, "document", None) is None and op != "forward":
                raise ValueError("source document unavailable; use a real Telegram document message")
            a["source"] = source
            if op != "forward":
                a["document"] = utils.get_input_document(source.document)
        if op == 'send_sticker':
            if 'short_name' in a:
                pack = await self.client(F.messages.GetStickerSetRequest(T.InputStickerSetShortName(a['short_name']), 0))
                document = next((d for d in pack.documents if str(d.id) == a['document_id']), None)
            else:
                document = a['source'].document
            if document is None or not any(isinstance(x, T.DocumentAttributeSticker) for x in document.attributes):
                raise ValueError('selected document is not a Telegram sticker; select its canonical sticker-pack document')
            a['document'] = utils.get_input_document(document)
        if op in {'ban','unban','mute','unmute','admin'}:
            from telethon import errors
            try:
                current = (await self.client(F.channels.GetParticipantRequest(utils.get_input_channel(a['peer']), a['user']))).participant
            except errors.UserNotParticipantError:
                current = None  # A definite absence, never a failed read.
            if op == 'admin':
                rights = getattr(current, 'admin_rights', None) or T.ChatAdminRights()
                values = rights.to_dict(); values.pop('_', None)
                values.update({k:a[k] for k in SPECS['admin'][2] if k != 'rank' and k in a})
                a['admin_rights'] = T.ChatAdminRights(**values)
                a['rank'] = a.get('rank', getattr(current, 'rank', '') or '')
            else:
                rights = getattr(current, 'banned_rights', None) or T.ChatBannedRights(None)
                values = rights.to_dict(); values.pop('_', None)
                values['view_messages' if op in {'ban','unban'} else 'send_messages'] = op in {'ban','mute'}
                if 'until' in a: values['until_date'] = a['until'] or None
                a['banned_rights'] = T.ChatBannedRights(**values)
        if "asset" in a:
            path = asset(self.root, a["asset"])
            uploaded = await self.client.upload_file(path)
            if op == 'avatar_set':
                a['uploaded_file'] = uploaded
                return a
            if op == "send_media" and args.get("photo"):
                a["media"] = T.InputMediaUploadedPhoto(uploaded)
            else:
                suffix = path.suffix.lower()
                mime = {".png": "image/png", ".webp": "image/webp", ".webm": "video/webm", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".jpg": "image/jpeg"}.get(suffix, "application/octet-stream")
                attrs = [T.DocumentAttributeFilename(path.name)]
                if op == "sticker_upload":
                    attrs.append(T.DocumentAttributeSticker(args["emoji"], T.InputStickerSetEmpty()))
                a["media"] = T.InputMediaUploadedDocument(uploaded, mime, attrs)
        return a

    async def execute(self, op, a, random_id):
        from telethon import functions as F, types as T, utils
        c = self.client
        peer = a.get("peer")
        reply = None
        if a.get("reply_id") or a.get("topic_id"):
            reply = T.InputReplyToMessage(a.get("reply_id") or a["topic_id"], top_msg_id=a.get("topic_id") if a.get("reply_id") else None)
        limit = a.get("limit", 50)
        if op == 'profile':
            me = await c.get_me()
            full = await c(F.users.GetFullUserRequest(T.InputUserSelf()))
            return {**public_entity(me), 'last_name':getattr(me,'last_name',None),
                    'about':getattr(getattr(full,'full_user',None),'about',None),
                    'has_photo':isinstance(getattr(me,'photo',None),T.UserProfilePhoto),
                    'photo_id':str(getattr(getattr(me,'photo',None),'photo_id',''))}
        if op == 'username_check':
            r = await c(F.account.CheckUsernameRequest(a['username']))
            return {'username':a['username'],'available':r is True}
        if op == 'username_set':
            r = await c(F.account.UpdateUsernameRequest(a['username']))
            return {'ack_type':type(r).__name__,'username':getattr(r,'username',None)}
        if op == 'profile_update':
            r = await c(F.account.UpdateProfileRequest(**{k:a[k] for k in ('first_name','last_name','about') if k in a}))
            return {'ack_type':type(r).__name__,'first_name':getattr(r,'first_name',None),
                    'last_name':getattr(r,'last_name',None)}
        if op == 'avatar_set':
            r = await c(F.photos.UploadProfilePhotoRequest(file=a['uploaded_file']))
            return {'ack_type':type(r).__name__,'photo_id':str(getattr(getattr(r,'photo',None),'id',''))}
        if op == "dialogs":
            dialogs = await c.get_dialogs(limit=limit, ignore_migrated=True)
            items, omitted = self.in_scope([public_entity(x.entity) for x in dialogs])
            return {"items": items, "complete": len(dialogs) < limit, "omitted_outside_scope": omitted,
                    "note": "First page only; use resolve for known peers."}
        if op == "resolve":
            return public_entity(await c.get_entity(peer))
        if op == "search":
            r = await c(F.contacts.SearchRequest(a["query"], limit))
            items, omitted = self.in_scope([public_entity(x) for x in [*r.chats, *r.users]])
            return {"items": items, "complete": False, "omitted_outside_scope": omitted}
        if op == "history":
            kw = {"limit": limit, "offset_id": a.get("offset_id", 0)}
            if a.get("topic_id"):
                kw["reply_to"] = a["topic_id"]
            rows = await c.get_messages(peer, **kw)
            return {"items": [public_message(m) for m in rows], "next_offset_id": rows[-1].id if rows else 0, "complete": len(rows) < limit}
        if op == "members":
            r = await c(F.channels.GetParticipantsRequest(utils.get_input_channel(peer), T.ChannelParticipantsRecent(), a.get("offset", 0), limit, 0))
            return {"items": [public_entity(u) for u in r.users], "next_offset": a.get("offset", 0)+len(r.participants), "count": r.count}
        if op == "topics":
            r = await c(F.messages.GetForumTopicsRequest(peer, None, a.get("offset_id", 0), a.get("offset_topic", 0), limit))
            return {"items": [{"id": t.id, "title": getattr(t, "title", ""), "top_message": getattr(t, "top_message", 0)} for t in r.topics], "count": r.count, "complete": len(r.topics) < limit}
        pack = T.InputStickerSetShortName(a.get("short_name", ""))
        if op == "sticker_pack":
            r = await c(F.messages.GetStickerSetRequest(pack, 0))
            return {"title": r.set.title, "short_name": r.set.short_name, "documents": [public_document(d) for d in r.documents]}
        if op in {"send_text", "send_media", "send_sticker"}:
            if op == "send_text":
                req = F.messages.SendMessageRequest(peer, a["text"], random_id=random_id, reply_to=reply)
            else:
                media = a["media"] if op == "send_media" else T.InputMediaDocument(a["document"])
                req = F.messages.SendMediaRequest(peer, media, a.get("text", ""), random_id=random_id, reply_to=reply)
            r = await c(req)
            from .telegram import _sent_id
            mid = _sent_id(r, random_id)
            if mid is None:
                raise RuntimeError("acknowledgement has no matching message ID")
            return {"message_id": mid, "peer": a["resolved_peer"]}
        if op == "forward":
            r = await c(F.messages.ForwardMessagesRequest(from_peer=a["source_peer"], id=[a["message_id"]], random_id=[random_id], to_peer=peer, top_msg_id=a.get("topic_id")))
            from .telegram import _sent_id
            mid = _sent_id(r,random_id)
            if mid is None: raise RuntimeError('forward acknowledgement has no matching message ID')
            return {'message_id':mid,'peer':a['resolved_peer']}
        elif op == "edit":
            r = await c(F.messages.EditMessageRequest(peer, a["message_id"], message=a["text"]))
        elif op == "delete":
            r = await c(F.channels.DeleteMessagesRequest(utils.get_input_channel(peer),[a['message_id']])) if isinstance(peer,T.InputPeerChannel) else await c(F.messages.DeleteMessagesRequest([a['message_id']],revoke=True))
        elif op == "react":
            r = await c(F.messages.SendReactionRequest(peer, a["message_id"], reaction=[T.ReactionEmoji(a["emoji"])] if a["emoji"] else []))
        elif op == "join":
            from telethon import errors
            try:
                r = await c(F.messages.ImportChatInviteRequest(a["invite_hash"])) if a.get("invite_hash") else await c(F.channels.JoinChannelRequest(utils.get_input_channel(peer)))
            except errors.InviteRequestSentError:
                return {'membership': 'request_submitted', 'joined': False}
            return {'membership': 'joined', 'chats': [public_entity(x) for x in getattr(r,'chats',())]}
        elif op == "leave":
            e = a['entity']
            r = await c(F.messages.DeleteChatUserRequest(e.id, T.InputUserSelf())) if isinstance(e, T.Chat) else await c(F.channels.LeaveChannelRequest(utils.get_input_channel(peer)))
        elif op == "create_chat":
            r = await c(F.channels.CreateChannelRequest(a["title"], a.get("about", ""), broadcast=a.get("broadcast", False), megagroup=not a.get("broadcast", False), forum=a.get("forum", False)))
        elif op == "invite":
            r = await c(F.channels.InviteToChannelRequest(utils.get_input_channel(peer), [utils.get_input_user(a["user"])]))
            missing = [{'user_id':str(x.user_id), 'premium_would_allow_invite':bool(x.premium_would_allow_invite),
                        'premium_required_for_pm':bool(x.premium_required_for_pm)} for x in r.missing_invitees]
            requested = a['resolved_user']
            return {'ack_type':type(r).__name__, 'requested_user_id':requested,
                    'invitation_outcome':'not_invited' if any(x['user_id']==requested for x in missing) else 'accepted',
                    'missing_invitees':missing}
        elif op in {"ban", "unban", "mute", "unmute"}:
            r = await c(F.channels.EditBannedRequest(utils.get_input_channel(peer), a["user"], a['banned_rights']))
        elif op == "admin":
            r = await c(F.channels.EditAdminRequest(utils.get_input_channel(peer), utils.get_input_user(a["user"]), a['admin_rights'], a['rank']))
        elif op == "pin":
            r = await c(F.messages.UpdatePinnedMessageRequest(peer, a["message_id"], unpin=a.get("unpin", False), silent=not a.get("notify", False)))
        elif op == "chat_title":
            e = a['entity']
            r = await c(F.messages.EditChatTitleRequest(e.id, a["title"])) if isinstance(e, T.Chat) else await c(F.channels.EditTitleRequest(utils.get_input_channel(peer), a["title"]))
        elif op == "topic_create":
            r = await c(F.messages.CreateForumTopicRequest(peer, a["title"], random_id=random_id))
        elif op == "topic_edit":
            r = await c(F.messages.EditForumTopicRequest(peer, a["topic_id"], title=a.get("title"), closed=a.get("closed"), hidden=a.get("hidden")))
        elif op in {"pack_create", "pack_add"}:
            item = T.InputStickerSetItem(a["document"], a["emoji"])
            r = await c(F.stickers.CreateStickerSetRequest(T.InputUserSelf(), a["title"], a["short_name"], [item], software="Ouroboros")) if op == "pack_create" else await c(F.stickers.AddStickerToSetRequest(pack, item))
        elif op == "pack_remove":
            r = await c(F.stickers.RemoveStickerFromSetRequest(a["document"]))
        elif op == "pack_move":
            r = await c(F.stickers.ChangeStickerPositionRequest(a["document"], a["position"]))
        elif op == "pack_rename":
            r = await c(F.stickers.RenameStickerSetRequest(pack, a["title"]))
        elif op == "pack_install":
            r = await c(F.messages.UninstallStickerSetRequest(pack)) if a.get("remove") else await c(F.messages.InstallStickerSetRequest(pack, False))
        elif op == "sticker_upload":
            r = await c(F.messages.UploadMediaRequest(T.InputPeerSelf(), a["media"]))
            doc = getattr(r, "document", None)
            if doc is None:
                raise RuntimeError("no uploaded document acknowledgement")
            # Save as an actual own message so the next pack operation can refresh
            # its document reference after restart (never persist a stale file_reference).
            if getattr(self,'guard',None) is not None:
                await self.guard('', '', True)
            sent = await c(F.messages.SendMediaRequest(T.InputPeerSelf(), T.InputMediaDocument(utils.get_input_document(doc)), "", random_id=random_id))
            from .telegram import _sent_id
            mid = _sent_id(sent, random_id)
            if mid is None:
                raise RuntimeError("uploaded sticker send outcome unknown")
            return {"peer": "me", "message_id": mid, "document_id": str(doc.id)}
        else:
            raise ValueError("operation not implemented")
        # Returned RPC object proves a response, not a fake True shortcut.
        result = {"ack_type": type(r).__name__}
        if getattr(r, "chats", None):
            result["chats"] = [public_entity(x) for x in r.chats]
        if getattr(r, "set", None):
            result.update({"short_name": r.set.short_name, "title": r.set.title})
        return result
