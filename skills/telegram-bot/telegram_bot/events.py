"""Neutral Telegram update parsing with exact provider provenance."""

from __future__ import annotations

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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_telegram_update(
    update: Dict[str, Any], *, bot_account_id: str
) -> Optional[TelegramEvent]:
    """Return one factual external-message event or ``None`` for unsupported updates."""
    if not isinstance(update, dict):
        return None
    update_id = _integer(update.get("update_id"))
    message = update.get("message")
    if update_id is None or not isinstance(message, dict):
        return None

    actor = message.get("from")
    chat = message.get("chat")
    message_id = _integer(message.get("message_id"))
    if not isinstance(actor, dict) or not isinstance(chat, dict) or message_id is None:
        return None
    if actor.get("is_bot"):
        return None

    actor_id = _integer(actor.get("id"))
    chat_id = _integer(chat.get("id"))
    if actor_id is None or chat_id is None:
        return None

    topic_id = _integer(message.get("message_thread_id"))
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
    conversation_fact = {
        "platform": "telegram",
        "bot_account_id": bot_id,
        "chat_id": str(chat_id),
        "chat_type": str(chat.get("type") or "unknown"),
        "title": str(chat.get("title") or ""),
        "username": str(chat.get("username") or ""),
        "topic_id": topic_id,
    }
    attachments = _attachments(message)
    message_fact = {
        "message_id": message_id,
        "sent_at_epoch": _integer(message.get("date")),
        "reply_to_message_id": _reply_message_id(message),
        "attachments": [item.to_dict() for item in attachments],
    }
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


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
