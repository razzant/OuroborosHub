"""Neutral Telegram update parsing with exact provider provenance."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from typing import Any, Dict, Optional, Sequence, Tuple


# Chats a bot can be part of. A private chat is one person who started the bot.
_PLACE_CHAT_TYPES = frozenset({"private", "group", "supergroup", "channel"})
# getFile serves at most this much; a larger file can never be downloaded by a bot.
BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TOO_LARGE_NOTE = ("Not staged: larger than the Bot API's 20 MiB download limit. "
                  "The Telegram message reference is retained.")


def too_large_marks(size: int) -> Dict[str, str]:
    """Telegram never serves a bot a file above the download limit; retrying it would hold its conversation."""
    return {"status": "too_large", "note": TOO_LARGE_NOTE} if size > BOT_API_DOWNLOAD_LIMIT else {}
# The Host classifies a staged file by its bytes, but an MP3 without an ID3 tag or an
# M4A under a generic MP4 brand is only recognized with its usual suffix.
_AUDIO_SUFFIXES = {
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/webm": ".webm",
}

@dataclass(frozen=True)
class TelegramAttachment:
    kind: str
    file_id: str
    file_unique_id: str
    file_name: str
    mime_type: str
    file_size: int
    # duration_sec is voice and audio only; status and note mark a file above the Bot API
    # download limit. All three are omitted from the dict when absent, so an ordinary photo
    # or document keeps its exact earlier shape.
    duration_sec: Optional[int] = None
    status: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items()
                if value is not None or key not in {"duration_sec", "status", "note"}}


@dataclass(frozen=True)
class TelegramEvent:
    source_event_id: str
    provider: str
    account_id: str
    conversation_id: str
    thread_id: str
    conversation_key: str
    actor: Dict[str, Any]
    conversation: Dict[str, Any]
    message: Dict[str, Any]
    text: str
    facts: Optional[list[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.facts is None:
            result.pop("facts")
        return result


def parse_telegram_update(
    update: Dict[str, Any], *, bot_account_id: str, management_group_id: str = "",
    places: bool = False, bot_username: str = "", words: Sequence[str] = (),
) -> Optional[TelegramEvent]:
    """Return one factual external-message event or ``None`` for unsupported updates."""
    if not isinstance(update, dict):
        return None
    update_id = _integer(update.get("update_id"))
    event_kind = "message"
    message = update.get("message")
    if not isinstance(message, dict) and isinstance(update.get("edited_message"), dict):
        message = update["edited_message"]
        event_kind = "edited_message"
    if places and not isinstance(message, dict):
        for kind in ("channel_post", "edited_channel_post"):
            if isinstance(update.get(kind), dict):
                message, event_kind = update[kind], kind
                break
    reaction = update.get("message_reaction")
    if not isinstance(reaction, dict):
        reaction = update.get("message_reaction_count")
    if not isinstance(message, dict) and isinstance(reaction, dict):
        return _parse_reaction_update(
            update,
            reaction,
            bot_account_id=bot_account_id,
            management_group_id=management_group_id,
            places=places,
        )
    if update_id is None or not isinstance(message, dict):
        return None

    actor = (message.get("sender_chat") or message.get("from")) if places else message.get("from")
    chat = message.get("chat")
    message_id = _integer(message.get("message_id"))
    if not isinstance(actor, dict) or not isinstance(chat, dict) or message_id is None:
        return None
    if (not places and actor.get("is_bot")) or (places and str(actor.get("id")) == bot_account_id):
        return None
    if places and chat.get("type") not in _PLACE_CHAT_TYPES:
        return None

    actor_id = _integer(actor.get("id"))
    chat_id = _integer(chat.get("id"))
    if actor_id is None or chat_id is None:
        return None

    # Telegram also sets message_thread_id on replies outside topics (ordinary
    # groups, forum General), where it names a reply chain.  Only a message that
    # Telegram marks as a topic message opens a separate conversation.
    raw_thread_id = _integer(message.get("message_thread_id"))
    topic_id = raw_thread_id if message.get("is_topic_message") is True else None
    bot_id = str(bot_account_id or "unknown").strip() or "unknown"
    topic_key = str(topic_id) if topic_id is not None else "0"
    conversation_key = f"telegram:{bot_id}:{chat_id}:{topic_key}"
    text = str(message.get("text") or message.get("caption") or "")

    actor_fact = {
        "platform": "telegram",
        "platform_actor_id": str(actor_id),
        "username": str(actor.get("username") or ""),
        "first_name": str(actor.get("first_name") or ""),
        "last_name": str(actor.get("last_name") or ""),
        "language_code": str(actor.get("language_code") or ""),
    }
    if places:
        actor_fact["is_bot"] = bool(actor.get("is_bot"))
        if message.get("sender_chat"):
            actor_fact["sender_chat"] = deepcopy(message["sender_chat"])
    conversation_fact = {
        "platform": "telegram",
        "bot_account_id": bot_id,
        "chat_id": str(chat_id),
        "chat_type": str(chat.get("type") or "unknown"),
        "title": str(chat.get("title") or ""),
        "username": str(chat.get("username") or ""),
        "topic_id": topic_id,
        **({"is_forum": True} if chat.get("is_forum") is True else {}),
    }
    if str(chat_id) == str(management_group_id).strip() and chat.get("type") in {
        "group",
        "supergroup",
    }:
        conversation_fact["configured_room"] = "management_group"
    attachments = _attachments(message)
    message_fact = {
        "event_kind": event_kind,
        "message_id": message_id,
        "sent_at_epoch": _integer(message.get("date")),
        **_time_facts(_integer(message.get("date")), _integer(message.get("edit_date"))),
        "reply_to_message_id": _reply_message_id(message),
        "attachments": [item.to_dict() for item in attachments],
    }
    if raw_thread_id is not None and topic_id is None:
        message_fact["reply_chain_id"] = raw_thread_id
    for key in ("entities", "caption_entities", "quote", "forward_origin", "media_group_id"):
        if key in message:
            message_fact[key] = deepcopy(message[key])
    reply = message.get("reply_to_message")
    if isinstance(reply, dict):
        message_fact["reply_to_message"] = _reply_context(reply)
    if not text.strip() and not attachments:
        return None

    return TelegramEvent(
        source_event_id=f"telegram:{bot_id}:{update_id}",
        provider="telegram",
        account_id=bot_id,
        conversation_id=str(chat_id),
        thread_id=str(topic_id) if topic_id is not None else "",
        conversation_key=conversation_key,
        actor=actor_fact,
        conversation=conversation_fact,
        message=message_fact,
        text=text,
        facts=telegram_wake_facts(message, bot_id, bot_username, words) if places else None,
    )


def telegram_wake_facts(message: Dict[str, Any], bot_id: str, username: str, words: Sequence[str] = ()) -> list[str]:
    """Provider entities establish mentions; literal owner words are a separate signal."""
    facts = ["any_message"]
    if (message.get("chat") or {}).get("type") == "private":
        # A private bot chat exists only after that person opened it with the bot.
        facts.append("direct_message")
    if not message.get("sender_chat") and (message.get("from") or {}).get("is_bot"):
        facts.append("from_bot")
    reply = message.get("reply_to_message") or {}
    if str((reply.get("from") or {}).get("id") or "") == bot_id:
        facts.append("reply_to_me")
    # Telegram entity offsets are UTF-16.
    for text_key, entities_key in (("text", "entities"), ("caption", "caption_entities")):
        wire = str(message.get(text_key) or "").encode("utf-16-le")
        for entity in message.get(entities_key) or []:
            if entity.get("type") == "text_mention" and str((entity.get("user") or {}).get("id") or "") == bot_id:
                if "mention" not in facts:
                    facts.append("mention")
            if entity.get("type") == "mention" and username:
                start, length = entity.get("offset"), entity.get("length")
                if type(start) is int and type(length) is int and start >= 0 and length > 0:
                    token = wire[2 * start:2 * (start + length)].decode("utf-16-le", errors="replace")
                    if token.casefold() == "@" + username.lstrip("@").casefold() and "mention" not in facts:
                        facts.append("mention")
    if words:
        text = str(message.get("text") or message.get("caption") or "").casefold()
        if any(re.search(r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)", text) for word in words if word):
            facts.append("signal.words")
    return facts


def _parse_reaction_update(
    update: Dict[str, Any],
    reaction: Dict[str, Any],
    *,
    bot_account_id: str,
    management_group_id: str = "",
    places: bool = False,
) -> Optional[TelegramEvent]:
    """Turn Telegram's reaction update into a neutral provider fact.

    Reaction updates intentionally carry no synthetic text. ``user`` is an individual,
    ``actor_chat`` a chat speaking as itself, and ``message_reaction_count`` an anonymous
    aggregate with no individual at all. Neither update names the target's topic, text or
    author: the store adds those only from an exact retained record (``CustodyStore``).
    """
    counted = "reactions" in reaction
    user = reaction.get("user") if isinstance(reaction.get("user"), dict) else None
    actor_chat = reaction.get("actor_chat") if isinstance(reaction.get("actor_chat"), dict) else None
    source = user or actor_chat or {}
    chat = reaction.get("chat") or {}
    message_id = _integer(reaction.get("message_id"))
    chat_id = _integer(chat.get("id")) if isinstance(chat, dict) else None
    actor_id = _integer(source.get("id"))
    update_id = _integer(update.get("update_id"))
    if update_id is None or message_id is None or chat_id is None or (actor_id is None and not counted):
        return None
    if (not places and source.get("is_bot")) or (places and str(actor_id) == bot_account_id):
        return None
    if places and chat.get("type") not in _PLACE_CHAT_TYPES:
        return None
    if counted:
        values = {"change": "count", "counts": [
            {**_reaction_values([row.get("type")])[0], "count": row["total_count"]}
            for row in (reaction["reactions"] if isinstance(reaction["reactions"], list) else [])
            if isinstance(row, dict) and _reaction_values([row.get("type")])
            and type(row.get("total_count")) is int and row["total_count"] >= 0]}
        raw = {"reaction_counts": deepcopy(reaction["reactions"])}
    else:
        old, new = _reaction_values(reaction.get("old_reaction")), _reaction_values(reaction.get("new_reaction"))
        if sorted(map(str, old)) == sorted(map(str, new)):
            return None  # names no change: nothing here for anyone to read
        values = ({"change": "add", "new": new} if not old else {"change": "remove", "old": old} if not new
                  else {"change": "replace", "old": old, "new": new})
        raw = {"old_reaction": deepcopy(reaction.get("old_reaction") or []),
               "new_reaction": deepcopy(reaction.get("new_reaction") or [])}
    bot_id = str(bot_account_id or "unknown").strip() or "unknown"
    conversation_key = f"telegram:{bot_id}:{chat_id}:0"
    conversation = {
        "platform": "telegram",
        "bot_account_id": bot_id,
        "chat_id": str(chat_id),
        "chat_type": str(chat.get("type") or "unknown"),
        "title": str(chat.get("title") or ""),
        "username": str(chat.get("username") or ""),
        "topic_id": None,
        **({"is_forum": True} if chat.get("is_forum") is True else {}),
    }
    if str(chat_id) == str(management_group_id).strip() and chat.get("type") in {"group", "supergroup"}:
        conversation["configured_room"] = "management_group"
    # Outside a forum the chat is the conversation. In a forum only a retained record proves the topic.
    proven = chat.get("is_forum") is not True and chat.get("type") in _PLACE_CHAT_TYPES
    sent_at_epoch = _integer(reaction.get("date"))
    return TelegramEvent(
        source_event_id=f"telegram:{bot_id}:{update_id}",
        provider="telegram",
        account_id=bot_id,
        conversation_id=str(chat_id),
        thread_id="",
        conversation_key=conversation_key,
        actor=_reaction_actor(user, actor_chat, places=places),
        conversation=conversation,
        message={
            "event_kind": "message_reaction_count" if counted else "message_reaction",
            "message_id": message_id,
            "sent_at_epoch": sent_at_epoch,
            **_time_facts(sent_at_epoch),
            **raw,
            "reaction": {"schema_version": 1, **values, "target": {
                "message_id": str(message_id), "resolution": "resolved" if proven else "unresolved"}},
        },
        text="",
        facts=(["from_bot"] if source.get("is_bot") else []) if places else None,
    )


def _reaction_actor(user: Optional[Dict[str, Any]], actor_chat: Optional[Dict[str, Any]], *, places: bool) -> Dict[str, Any]:
    """An individual, a chat acting as itself, or nobody in particular; never a made-up person."""
    if user:
        return {
            "kind": "user", "platform": "telegram", "platform_actor_id": str(user["id"]),
            "username": str(user.get("username") or ""), "first_name": str(user.get("first_name") or ""),
            "last_name": str(user.get("last_name") or ""),
            **({"is_bot": bool(user.get("is_bot"))} if places else {}),
        }
    if actor_chat:
        return {"kind": "chat", "platform": "telegram", "platform_actor_id": str(actor_chat["id"]),
                "display_name": str(actor_chat.get("title") or ""), "username": str(actor_chat.get("username") or ""),
                "authenticated": False}
    return {"kind": "anonymous", "platform": "telegram", "authenticated": False}


def _reaction_values(raw: Any) -> list[Dict[str, str]]:
    """Telegram ReactionType objects as provider values, with no emoji mapping or allowlist."""
    values = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"]:
            field = {"emoji": "emoji", "custom_emoji": "custom_emoji_id"}.get(item["type"], "")
            values.append({"type": item["type"], "value": str(item.get(field) or "")})
    return values


def _iso_time(epoch: int) -> str:
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _time_facts(sent_epoch: Optional[int], edited_epoch: Optional[int] = None) -> Dict[str, str]:
    """``message.sent_at`` and, for an edit, ``edited_at`` from Telegram's own dates; an absent or unreadable one is
    omitted, never replaced by a receipt time."""
    times = (("sent_at", sent_epoch), ("edited_at", edited_epoch))
    return {key: value for key, epoch in times if epoch and (value := _iso_time(epoch))}


def _attachments(message: Dict[str, Any]) -> Tuple[TelegramAttachment, ...]:
    result = []
    photos = message.get("photo")
    if isinstance(photos, list):
        choices = [
            item for item in photos if isinstance(item, dict) and item.get("file_id")
        ]
        if choices:
            photo = choices[-1]
            result.append(
                TelegramAttachment(
                    kind="photo",
                    file_id=str(photo.get("file_id")),
                    file_unique_id=str(photo.get("file_unique_id") or ""),
                    file_name=f"photo-{photo.get('file_unique_id') or photo.get('file_id')}.jpg",
                    mime_type="image/jpeg",
                    file_size=_integer(photo.get("file_size")) or 0,
                    **too_large_marks(_integer(photo.get("file_size")) or 0),
                )
            )
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        result.append(
            TelegramAttachment(
                kind="document",
                file_id=str(document.get("file_id")),
                file_unique_id=str(document.get("file_unique_id") or ""),
                file_name=str(document.get("file_name") or "document"),
                mime_type=str(document.get("mime_type") or "application/octet-stream"),
                file_size=_integer(document.get("file_size")) or 0,
                **too_large_marks(_integer(document.get("file_size")) or 0),
            )
        )
    for kind in ("voice", "audio"):
        item = message.get(kind)
        if isinstance(item, dict) and item.get("file_id"):
            result.append(_sound(kind, item))
    return tuple(result)


def _sound(kind: str, item: Dict[str, Any]) -> TelegramAttachment:
    """A voice message or audio file as an ordinary attachment: its bytes, never words."""
    unique = str(item.get("file_unique_id") or "")
    mime = str(item.get("mime_type") or ("audio/ogg" if kind == "voice" else "application/octet-stream"))
    suffix = _AUDIO_SUFFIXES.get(mime.split(";", 1)[0].strip().lower(), "")
    name = str(item.get("file_name") or "") if kind == "audio" else ""
    if not name:
        name = f"{kind}-{unique or item.get('file_id')}"
    if suffix and not name.lower().endswith(suffix) and not (suffix == ".ogg" and name.lower().endswith((".oga", ".opus"))):
        name += suffix
    size = _integer(item.get("file_size")) or 0
    duration = _integer(item.get("duration"))
    return TelegramAttachment(
        kind=kind,
        file_id=str(item.get("file_id")),
        file_unique_id=unique,
        file_name=name,
        mime_type=mime,
        file_size=size,
        duration_sec=duration if duration is not None and duration >= 0 else None,
        **too_large_marks(size),
    )


def _reply_message_id(message: Dict[str, Any]) -> Optional[int]:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return None
    return _integer(reply.get("message_id"))


def _reply_context(reply: Dict[str, Any]) -> Dict[str, Any]:
    """Retain the source message already supplied by Telegram, without a chain."""
    fields = (
        "message_id",
        "message_thread_id",
        "date",
        "from",
        "sender_chat",
        "chat",
        "text",
        "caption",
        "entities",
        "caption_entities",
        "forward_origin",
    )
    snapshot = {key: deepcopy(reply[key]) for key in fields if key in reply}
    attachments = _attachments(reply)
    if attachments:
        snapshot["attachments"] = [item.to_dict() for item in attachments]
    return snapshot


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
