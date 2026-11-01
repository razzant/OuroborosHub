"""Stable provider identity for one Telegram message, shared by live capture and history reads.

The Host deduplicates by source event ID, account, conversation, thread, actor ID and
text. Those facts must come from the message's own content, never from its serialized
bytes: views, forwards, reply counters and reactions change ``bytes(message)`` without
an edit, and Telegram also delivers reaction changes as edit updates. Local staging
outcomes (too large, unavailable, not downloaded) are observations of this connector,
so they live in ``message.media`` rather than in ``text``.
"""
from __future__ import annotations

from datetime import timezone

from .participant_state import digest


def _media_kind(media):
    from telethon import types as T
    if isinstance(media, T.MessageMediaPhoto):
        return 'photo', getattr(getattr(media, 'photo', None), 'id', None), {}
    if isinstance(media, T.MessageMediaDocument):
        document = getattr(media, 'document', None)
        facts = {}
        kind = 'document'
        for attribute in getattr(document, 'attributes', None) or ():
            if isinstance(attribute, T.DocumentAttributeSticker):
                kind, facts['emoji'] = 'sticker', attribute.alt or ''
            elif isinstance(attribute, T.DocumentAttributeAudio) and kind == 'document':
                kind = 'voice' if attribute.voice else 'audio'
                if attribute.title:
                    facts['title'] = attribute.title
            elif isinstance(attribute, T.DocumentAttributeVideo) and kind in ('document', 'animation'):
                kind = 'video_message' if attribute.round_message else ('animation' if kind == 'animation' else 'video')
            elif isinstance(attribute, T.DocumentAttributeAnimated) and kind in ('document', 'video'):
                kind = 'animation'
            elif isinstance(attribute, T.DocumentAttributeFilename):
                facts['name'] = attribute.file_name
        if document is not None and getattr(document, 'mime_type', None):
            facts['mime'] = document.mime_type
        if document is not None and getattr(document, 'size', None) is not None:
            facts['size'] = document.size
        return kind, getattr(document, 'id', None), facts
    if isinstance(media, T.MessageMediaWebPage):
        # A link preview is derived from the text and may load later; it is not content.
        return None, None, {}
    name = type(media).__name__
    kind = name[len('MessageMedia'):].lower() if name.startswith('MessageMedia') else name.lower()
    return kind or 'media', getattr(getattr(media, 'poll', None), 'id', None), {}


def media_descriptor(message):
    """Safe attachment facts without file references or access hashes; None for no media."""
    media = getattr(message, 'media', None)
    if media is None:
        return None
    kind, media_id, facts = _media_kind(media)
    if kind is None:
        return None
    return {'kind': kind, **({'media_id': str(media_id)} if media_id is not None else {}), **facts}


_LABELS = {'photo': 'photo', 'voice': 'voice message', 'video': 'video', 'video_message': 'video message',
           'animation': 'GIF', 'audio': 'audio'}


def canonical_text(message):
    """The provider text exactly; a stable descriptor only when Telegram sent no text."""
    text = getattr(message, 'message', '') or ''
    if text:
        return text
    media = media_descriptor(message)
    if media is not None:
        kind = media['kind']
        if kind == 'sticker':
            return ('[Telegram sticker ' + media['emoji'] + ']') if media.get('emoji') else '[Telegram sticker]'
        if kind == 'document':
            return ('[Telegram document: ' + media['name'] + ']') if media.get('name') else '[Telegram document]'
        if kind == 'audio' and media.get('title'):
            return '[Telegram audio: ' + media['title'] + ']'
        return '[Telegram ' + _LABELS.get(kind, kind) + ']'
    action = getattr(message, 'action', None)
    if action is not None:
        return '[Telegram service: ' + type(action).__name__ + ']'
    return '[Telegram message without text]'


# What an entity says beyond its type, span, URL and mentioned user, named field by field
# for the Telethon 1.45 entity constructors: a custom emoji's document, a code block's
# language, a collapsed quote, a diff's replaced text, a formatted date and its display.
# Empty values are left out, so a flag Telegram omits and one it sends unset agree, and an
# entity without such a field keeps the revision it had before these were added.
_ENTITY_FIELDS = ('document_id', 'language', 'collapsed', 'old_text', 'date', 'relative',
                  'short_time', 'long_time', 'short_date', 'long_date', 'day_of_week')


def _entities(message):
    rows = []
    for entity in getattr(message, 'entities', None) or ():
        row = [type(entity).__name__, entity.offset, entity.length,
               getattr(entity, 'url', None), getattr(entity, 'user_id', None)]
        extra = {name: iso(value) if hasattr(value, 'astimezone') else value
                 for name in _ENTITY_FIELDS for value in [getattr(entity, name, None)] if value}
        if extra:
            row.append(extra)
        rows.append(row)
    return rows


def content_revision(message):
    """Edit identity: Telegram's edit time plus a digest of what the message says."""
    media = media_descriptor(message)
    content = [getattr(message, 'message', '') or '', _entities(message),
               [media['kind'], media.get('media_id')] if media else None,
               type(message.action).__name__ if getattr(message, 'action', None) is not None else None]
    edited = getattr(message, 'edit_date', None)
    stamp = edited.astimezone(timezone.utc).isoformat() if edited is not None else ''
    return stamp + ':' + digest(content)[:32]


def iso(value):
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def actor_facts(sender):
    """Display enrichment beside the numeric actor ID; never identity, never authority."""
    if sender is None:
        return {}
    first = getattr(sender, 'first_name', None) or ''
    last = getattr(sender, 'last_name', None) or ''
    name = ' '.join(part for part in (first, last) if part) or getattr(sender, 'title', None) or ''
    facts = {'display_name': name} if name else {}
    if getattr(sender, 'username', None):
        facts['username'] = sender.username
    if hasattr(sender, 'bot'):
        facts['is_bot'] = bool(sender.bot)
    return facts


def chat_facts(entity, private=False):
    """Title and kind for the chat list and conversation facts."""
    from telethon import types as T
    if isinstance(entity, T.User):
        title = ' '.join(part for part in (entity.first_name, entity.last_name) if part) or entity.username or ''
        return title, 'bot' if entity.bot else 'private', getattr(entity, 'username', None) or '', None
    if isinstance(entity, (T.Chat, T.ChatForbidden)):
        return entity.title or '', 'group', '', getattr(entity, 'participants_count', None)
    if isinstance(entity, (T.Channel, T.ChannelForbidden)):
        kind = 'channel' if getattr(entity, 'broadcast', False) else ('forum' if getattr(entity, 'forum', False) else 'supergroup')
        return entity.title or '', kind, getattr(entity, 'username', None) or '', getattr(entity, 'participants_count', None)
    title = getattr(entity, 'title', None) or getattr(entity, 'first_name', None) or ''
    return title, 'private' if private else 'group', getattr(entity, 'username', None) or '', None
