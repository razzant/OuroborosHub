"""Neutral Telegram update parsing with exact provider provenance."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class TelegramAttachment:
    kind: str
    file_id: str
    file_unique_id: str
    file_name: str
    mime_type: str
    file_size: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
    places: bool = False, bot_username: str = "",
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
    if places and chat.get("type") not in {"group", "supergroup", "channel"}:
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
        facts=telegram_wake_facts(message, bot_id, bot_username) if places else None,
    )


def telegram_wake_facts(message: Dict[str, Any], bot_id: str, username: str) -> list[str]:
    """Telegram entity offsets are UTF-16; only explicit entities establish mentions."""
    facts = ["any_message"]
    if not message.get("sender_chat") and (message.get("from") or {}).get("is_bot"):
        facts.append("from_bot")
    reply = message.get("reply_to_message") or {}
    if str((reply.get("from") or {}).get("id") or "") == bot_id:
        facts.append("reply_to_me")
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

    Reaction updates intentionally carry no synthetic text.  The Presence turn
    receives the structured reaction payload and can decide whether it matters.
    """
    actor = reaction.get("user") or reaction.get("actor_chat") or {}
    chat = reaction.get("chat") or {}
    message_id = _integer(reaction.get("message_id"))
    chat_id = _integer(chat.get("id")) if isinstance(chat, dict) else None
    actor_id = _integer(actor.get("id")) if isinstance(actor, dict) else None
    update_id = _integer(update.get("update_id"))
    if update_id is None or message_id is None or chat_id is None or actor_id is None:
        return None
    if (not places and actor.get("is_bot")) or (places and str(actor_id) == bot_account_id):
        return None
    if places and chat.get("type") not in {"group", "supergroup", "channel"}:
        return None
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
    }
    if str(chat_id) == str(management_group_id).strip() and chat.get("type") in {"group", "supergroup"}:
        conversation["configured_room"] = "management_group"
    return TelegramEvent(
        source_event_id=f"telegram:{bot_id}:{update_id}",
        provider="telegram",
        account_id=bot_id,
        conversation_id=str(chat_id),
        thread_id="",
        conversation_key=conversation_key,
        actor={
            "platform": "telegram",
            "platform_actor_id": str(actor_id),
            "username": str(actor.get("username") or "") if isinstance(actor, dict) else "",
            "first_name": str(actor.get("first_name") or "") if isinstance(actor, dict) else "",
            "last_name": str(actor.get("last_name") or "") if isinstance(actor, dict) else "",
        },
        conversation=conversation,
        message={
            "event_kind": "message_reaction",
            "message_id": message_id,
            "old_reaction": deepcopy(reaction.get("old_reaction") or []),
            "new_reaction": deepcopy(reaction.get("new_reaction") or []),
        },
        text="",
        facts=(["from_bot"] if actor.get("is_bot") else []) if places else None,
    )


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
            )
        )
    return tuple(result)


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
