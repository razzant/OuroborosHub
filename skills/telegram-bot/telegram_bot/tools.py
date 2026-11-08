"""Model-selected Telegram actions using the existing durable outbox."""

from __future__ import annotations

import pathlib
import re
from typing import Any

from .api import CLOUD_PHOTO_UPLOAD_LIMIT, CLOUD_UPLOAD_LIMIT, MEDIA_METHODS
from .custody import CustodyStore
from .delivery import tool_origin
from .formatting import prepare_caption


SEND_KINDS = ("message", *MEDIA_METHODS, "album")
# sendMediaGroup takes these; photos and videos mix, audio and documents group only with their own kind.
ALBUM_KINDS = ("photo", "video", "audio", "document")
# Telegram plays a voice message from OGG/Opus, MP3 or M4A (https://core.telegram.org/bots/api#sendvoice).
_VOICE_SUFFIXES = (".ogg", ".oga", ".opus", ".mp3", ".m4a")


class SendRefused(ValueError):
    """A typed refusal: nothing was queued, and ``code`` says which Bot API rule the request breaks."""

    def __init__(self, code: str, message: str, **facts: Any):
        super().__init__(message)
        self.code = code
        self.facts = facts

    def answer(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": str(self), **self.facts}


def upload_path(kind: str, file_path: Any) -> str:
    """An existing file within the cloud Bot API's upload limit for ``kind``; refused, never cut."""
    path = pathlib.Path(str(file_path or "")).expanduser()
    if not path.is_file():
        raise SendRefused("file_missing", "file_path must name an existing file")
    size, limit = path.stat().st_size, CLOUD_PHOTO_UPLOAD_LIMIT if kind == "photo" else CLOUD_UPLOAD_LIMIT
    if size > limit:
        raise SendRefused("file_too_large", f"{path.name} is {size:,} bytes; the Bot API accepts a {kind} of at most "
                          f"{limit // (1024 * 1024)} MB from a bot. Not queued.", size_bytes=size, limit_bytes=limit)
    if kind == "voice" and not path.name.lower().endswith(_VOICE_SUFFIXES):
        raise SendRefused("voice_format", "Telegram plays a voice message from OGG/Opus, MP3 or M4A; send this file "
                          "as audio or document. Not queued.")
    return str(path.resolve())


def frozen_caption(caption: str, markdown: bool) -> dict[str, str]:
    try:
        return prepare_caption(caption, markdown=markdown)
    except ValueError as exc:
        raise SendRefused("caption_too_long", str(exc)) from exc


def album_items(items: Any, caption: str, markdown: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The album as queued and as it will be sent, after Telegram's composition rules (sendMediaGroup)."""
    if not isinstance(items, list) or not 2 <= len(items) <= 10:
        raise SendRefused("album_size", "an album has 2 to 10 items. Not queued.")
    if any(not isinstance(item, dict) for item in items):
        raise SendRefused("album_item", "each album item is {kind, file_path, caption}. Not queued.")
    kinds = [str(item.get("kind") or "") for item in items]
    other = next((kind for kind in kinds if kind not in ALBUM_KINDS), None)
    if other is not None:
        raise SendRefused("album_kind", f"an album holds photo, video, audio or document items; send {other or 'that'} "
                          "on its own. Not queued.")
    if not (set(kinds) <= {"photo", "video"} or len(set(kinds)) == 1):
        raise SendRefused("album_mixed", "Telegram groups photos with videos, audio only with audio and documents only "
                          "with documents. Not queued.")
    captions = [str(item.get("caption") or "") for item in items]
    if caption and any(captions):
        raise SendRefused("album_caption", "give the album one caption, or captions on its items, not both. Not queued.")
    captions[0] = captions[0] or caption
    queued = [{"kind": kind, "file_path": upload_path(kind, item.get("file_path")), "caption": text}
              for kind, item, text in zip(kinds, items, captions)]
    return queued, [{**item, "caption": frozen_caption(item["caption"], markdown)} for item in queued]


TELEGRAM_TYPING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "chat_id": {"type": "string", "description": "This turn's own Telegram chat ID, as in the event."},
        "topic_id": {"type": "integer", "description": "This turn's forum topic, as in the event; omit outside a forum topic."},
    },
    "required": ["chat_id"],
}


def make_typing_tool(api: Any):
    """Ask the runtime to show "typing…" for the calling turn. The call records a request and contacts no one, so it
    answers ``requested``. Not speech: no outbox row, no receipt, no delivery report."""
    def telegram_typing(ctx: Any = None, *, chat_id: str, topic_id: int | None = None) -> dict[str, Any]:
        metadata = getattr(ctx, "task_metadata", None) or {}
        event = (metadata.get("presence") or {}).get("event") or {} if isinstance(metadata, dict) else {}
        turn = str(getattr(ctx, "task_id", "") or "").strip()
        own_chat, own_topic = str(event.get("conversation_id") or ""), str(event.get("thread_id") or "")
        if event.get("provider") != "telegram" or not turn or not own_chat:
            return {"ok": False, "code": "no_turn", "error": "typing shows only during a Telegram Presence turn, in "
                    "its own chat. Nothing was shown."}
        if str(chat_id or "").strip() != own_chat or (topic_id not in (None, "") and str(topic_id) != own_topic):
            where = f"chat {own_chat}" + (f", topic {own_topic}" if own_topic else "")
            return {"ok": False, "code": "other_conversation", "error": f"typing shows only in this turn's {where}. "
                    "Nothing was shown."}
        store = CustodyStore.shared(pathlib.Path(api.get_state_dir()) / "custody.sqlite3")
        source = str(event.get("source_event_id") or "")
        if not store.turn_live(source, turn):
            return {"ok": False, "code": "turn_ended", "error": "this turn is no longer live here. Nothing was shown."}
        # The forum topic of the turn's event, never a reply chain (events.py keeps that as reply_chain_id).
        store.request_typing(chat_id=own_chat, topic_id=own_topic, turn_ref=turn, source_event_id=source)
        return {"ok": True, "typing": "requested", "chat_id": own_chat, **({"topic_id": int(own_topic)} if own_topic else {}),
                "note": "Nothing was sent yet. The transport shows it while this turn runs and this place may act, until "
                        "your next message here is delivered (one already queued counts: ask again after it), the turn's "
                        "end or about 2 minutes. It is not speech and does not replace a reply."}

    return telegram_typing


MODERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["delete_message", "restrict_member", "ban_member", "unban_member"],
        },
        "chat_id": {
            "type": "string",
            "description": "Exact Telegram chat ID from the event facts or an explicitly selected target.",
        },
        "message_id": {
            "type": "integer",
            "description": "Exact message ID for delete_message.",
        },
        "user_id": {
            "type": "integer",
            "description": "Exact actor ID for member operations.",
        },
        "permissions": {
            "type": "object",
            "additionalProperties": {"type": "boolean"},
            "description": "Telegram ChatPermissions for restrict_member; true permissions can lift a restriction.",
        },
        "until_date": {
            "type": "integer",
            "description": "Optional Unix timestamp for ban/restrict expiry. Zero means indefinite; Telegram also treats durations below 30 seconds or over 366 days as indefinite.",
        },
        "revoke_messages": {
            "type": "boolean",
            "description": "Delete the banned member's messages; Telegram always does so in supergroups/channels.",
        },
        "request_id": {
            "type": "string",
            "description": "Stable caller ID; reuse for retries of the same operation.",
        },
    },
    "required": ["action", "chat_id", "request_id"],
}


TELEGRAM_OPERATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "method": {"type": "string", "enum": ["editMessageText", "setMessageReaction"]},
        "chat_id": {"type": "string", "description": "Exact numeric Telegram chat ID."},
        "message_id": {"type": "integer", "description": "Exact provider message ID."},
        "text": {"type": "string", "description": "Replacement text for editMessageText."},
        "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2"],
                       "description": "Optional text formatting for edits; omit for plain text."},
        "topic_id": {"type": "integer", "description": "Known forum topic of the target. Checked against this topic's place and used for local ordering and history; never sent to Telegram. A reaction in a forum needs it unless the message is in General."},
        "reaction": {"type": "array", "maxItems": 1, "items": {"type": "object", "additionalProperties": True}, "description": "At most one Telegram reaction: {\"type\":\"emoji\",\"emoji\":...} or {\"type\":\"custom_emoji\",\"custom_emoji_id\":...}. It replaces this bot's earlier reaction; [] removes it. Paid reactions are not available to bots."},
        "is_big": {"type": "boolean", "default": False},
        "original_delivery_id": {"type": "string", "description": "Optional prior durable send identity retained as a source reference."},
        "request_id": {"type": "string", "description": "Stable operation identity; reuse only for the same logical mutation."},
    },
    "required": ["method", "chat_id", "message_id", "request_id"],
}


def _positive_id(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def make_moderation_tool(api: Any):
    def telegram_moderate(
        *,
        action: str,
        chat_id: str,
        request_id: str,
        message_id: int = 0,
        user_id: int = 0,
        permissions: dict[str, bool] | None = None,
        until_date: int = 0,
        revoke_messages: bool = False,
    ) -> dict[str, Any]:
        try:
            target = str(chat_id).strip()
            if re.fullmatch(r"-?[0-9]+", target) is None:
                raise ValueError("chat_id must be an exact numeric Telegram id")
            if not str(request_id).strip():
                raise ValueError("request_id is required for a retryable operation")
            parameters: dict[str, Any] = {"chat_id": target}
            if action == "delete_message":
                parameters["message_id"] = _positive_id(message_id, "message_id")
            elif action in {"restrict_member", "ban_member", "unban_member"}:
                parameters["user_id"] = _positive_id(user_id, "user_id")
                if action == "restrict_member":
                    if (
                        not isinstance(permissions, dict)
                        or not permissions
                        or any(
                            type(value) is not bool for value in permissions.values()
                        )
                    ):
                        raise ValueError(
                            "permissions must be a nonempty Telegram ChatPermissions object of boolean values"
                        )
                    parameters.update(
                        permissions=dict(permissions),
                        use_independent_chat_permissions=True,
                    )
                if action in {"restrict_member", "ban_member"}:
                    if isinstance(until_date, bool) or int(until_date) < 0:
                        raise ValueError(
                            "until_date must be a nonnegative Unix timestamp"
                        )
                    parameters["until_date"] = int(until_date)
                if action == "ban_member":
                    parameters["revoke_messages"] = bool(revoke_messages)
                if action == "unban_member":
                    parameters["only_if_banned"] = True
            else:
                raise ValueError("unsupported Telegram moderation action")
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        receipt = str(request_id).strip()
        inserted = CustodyStore.shared(
            pathlib.Path(api.get_state_dir()) / "custody.sqlite3"
        ).enqueue_outbox(
            f"telegram-moderate:{receipt}",
            {
                "kind": "moderation",
                "chat_id": target,
                "action": action,
                "parameters": parameters,
            },
        )
        return {
            "ok": True,
            "state": "queued" if inserted else "already_queued",
            "request_id": receipt,
            "action": action,
        }

    return telegram_moderate


def make_receipt_tool(api: Any):
    def telegram_receipt(*, request_id: str, operation: str = "send") -> dict[str, Any]:
        if operation not in {"send", "moderate", "operation"}:
            return {"ok": False, "error": "operation must be send, moderate, or operation"}
        return CustodyStore.shared(
            pathlib.Path(api.get_state_dir()) / "custody.sqlite3"
        ).delivery_receipt(f"telegram-{operation}:{str(request_id).strip()}")

    return telegram_receipt


def _reaction_values_or_refuse(reaction: Any) -> list[dict[str, Any]]:
    """Telegram's own limits for a bot's reaction; which emoji a chat allows stays Telegram's answer."""
    if not isinstance(reaction, list) or any(not isinstance(item, dict) for item in reaction):
        raise ValueError("reaction must be a list of Telegram reaction objects")
    if len(reaction) > 1:
        raise ValueError("a Telegram bot can set at most one reaction per message; [] removes it")
    for item in reaction:
        if item.get("type") == "paid":
            raise ValueError("Telegram does not let bots set paid reactions")
        if not (item.get("type") == "emoji" and item.get("emoji")) and not (
                item.get("type") == "custom_emoji" and item.get("custom_emoji_id")):
            raise ValueError("a reaction is {type: emoji, emoji} or {type: custom_emoji, custom_emoji_id}")
    return [dict(item) for item in reaction]


def _reaction_conversation(known: dict[str, Any], chat_id: str, message_id: int,
                           topic_id: int | None) -> tuple[str, str]:
    """(topic, target resolution), or a refusal that says exactly what is missing.

    The guard saw ``topic_id`` as given, so a forum reaction without its topic would have been
    allowed by the whole chat's permission instead of that topic's. Explicit topics are only
    checked here and stay local. A positive chat id is a private chat, one conversation unless
    its retained messages have shown topics; since a private chat's records cannot show that it
    has none, an explicit topic there stays possible and unproven.
    """
    given = str(topic_id) if topic_id is not None else ""
    private = not chat_id.startswith("-")
    if known.get("unconfirmed"):
        # No record can be read as this bot's, including those that would prove a private chat's conversation.
        if given or private:
            return given, "unresolved"
        raise ValueError(f"forum_topic_unknown: this bot's identity is unconfirmed for the current token, so its "
                         f"records of chat {chat_id} cannot be read and the topic of message {message_id} is unknown; "
                         "pass the topic_id from the event if you have it, or retry once the transport status shows "
                         "the bot. Not sent.")
    if known["retained"]:
        if given != known["topic_id"]:
            where = f"topic {known['topic_id']}" if known["topic_id"] else "this chat's General conversation"
            raise ValueError(f"forum_topic_mismatch: message {message_id} is in {where}; "
                             f"pass topic_id={known['topic_id'] or 'nothing'}")
        return given, "resolved"
    if known["forum"] == 0 or (known["forum"] is None and private):
        if given and not private:
            raise ValueError("this chat has no topics; omit topic_id")
        return given, "unresolved" if given else "resolved"
    if not given:
        what = ("has shown topics and message" if private else "is a forum and message" if known["forum"] == 1
                else "may be a forum (not observed yet) and message")
        raise ValueError(f"forum_topic_unknown: chat {chat_id} {what} {message_id} is not retained here, so its "
                         "topic is unknown; pass the topic_id from the event if you have it. Not sent.")
    return given, "unresolved"


def make_operation_tool(api: Any, confirmed_account=lambda: ""):
    def telegram_operation(
        ctx: Any = None,
        *, method: str, chat_id: str, message_id: int, request_id: str,
        text: str = "", parse_mode: str = "", topic_id: int | None = None,
        reaction: list[dict[str, Any]] | None = None, is_big: bool = False,
        original_delivery_id: str = "",
    ) -> dict[str, Any]:
        try:
            if method not in {"editMessageText", "setMessageReaction"}:
                raise ValueError("unsupported Telegram operation")
            target = str(chat_id).strip()
            if re.fullmatch(r"-?[0-9]+", target) is None:
                raise ValueError("chat_id must be an exact numeric Telegram id")
            message = _positive_id(message_id, "message_id")
            receipt = str(request_id or "").strip()
            if not receipt:
                raise ValueError("request_id is required for a retryable operation")
            parameters: dict[str, Any] = {"chat_id": target, "message_id": message}
            local_topic_id = _positive_id(topic_id, "topic_id") if topic_id is not None else None
            store = CustodyStore.shared(pathlib.Path(api.get_state_dir()) / "custody.sqlite3")
            reaction_target, album = None, ""
            if method == "editMessageText":
                if not str(text or "").strip():
                    raise ValueError("text is required for editMessageText")
                parameters["text"] = str(text)
                if parse_mode:
                    parameters["parse_mode"] = str(parse_mode)
            else:
                parameters["reaction"] = _reaction_values_or_refuse(reaction)
                parameters["is_big"] = bool(is_big)
                account = confirmed_account()
                known = store.reaction_target(account, target, message) if account else {"unconfirmed": True}
                local_topic_id, resolution = _reaction_conversation(known, target, message, local_topic_id)
                reaction_target = {"message_id": str(message), "resolution": resolution}
                album = known.get("media_group_id", "")
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        origin = tool_origin(ctx)
        payload = {
            "kind": "operation", "chat_id": target, "method": method,
            "parameters": parameters, "text": str(text or ""),
            "_reporting": {"version": -1, "origin": origin},
        }
        if reaction_target:
            payload["_reaction_target"] = reaction_target
        if album:
            payload["_reaction_album"] = album
        if original_delivery_id:
            payload["original_delivery_id"] = str(original_delivery_id)
        if local_topic_id not in (None, ""):
            # Telegram's edit and reaction endpoints address the message directly;
            # keep the known topic only as a local ordering and history fact, never
            # as an unsupported Bot API argument.
            payload["topic_id"] = local_topic_id
        inserted = store.enqueue_outbox(
            f"telegram-operation:{receipt}", payload,
        )
        return {"ok": True, "state": "queued" if inserted else "already_queued",
                "request_id": receipt, "method": method, "chat_id": target,
                "message_id": message}

    return telegram_operation


TELEGRAM_REACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["set", "clear"],
                   "description": "set puts this bot's one reaction on the message, replacing its earlier one; clear removes it."},
        "chat_id": {"type": "string", "description": "Exact numeric Telegram chat ID."},
        "message_id": {"type": "integer", "description": "Exact provider message ID of the message reacted to."},
        "topic_id": {"type": "integer", "description": "Known forum topic of the target, as in the event. Checked against this topic's place and kept local; never sent to Telegram. A reaction in a forum needs it unless the message is in General."},
        "emoji": {"type": "string", "description": "For set: one emoji Telegram offers for reactions, such as 👍 or 👀."},
        "custom_emoji_id": {"type": "string", "description": "For set, instead of emoji: a custom emoji ID."},
        "is_big": {"type": "boolean", "default": False},
        "request_id": {"type": "string", "description": "Stable operation identity; reuse only for the same logical reaction. Its receipt is telegram_receipt(operation=operation)."},
    },
    "required": ["action", "chat_id", "message_id", "request_id"],
}


def make_react_tool(operation: Any):
    """``telegram_operation(method=setMessageReaction)`` with set/clear arguments: the same checks, outbox and receipt."""
    def telegram_react(
        ctx: Any = None,
        *, action: str, chat_id: str, message_id: int, request_id: str,
        topic_id: int | None = None, emoji: str = "", custom_emoji_id: str = "", is_big: bool = False,
    ) -> dict[str, Any]:
        emoji, custom = str(emoji or "").strip(), str(custom_emoji_id or "").strip()
        if action == "set":
            if bool(emoji) == bool(custom):
                return {"ok": False, "error": "set takes exactly one of emoji or custom_emoji_id"}
            reaction = [{"type": "emoji", "emoji": emoji} if emoji else {"type": "custom_emoji", "custom_emoji_id": custom}]
        elif action == "clear":
            if emoji or custom:
                return {"ok": False, "error": "clear removes this bot's reaction; omit emoji and custom_emoji_id"}
            reaction = []
        else:
            return {"ok": False, "error": "action must be set or clear"}
        return operation(ctx, method="setMessageReaction", chat_id=chat_id, message_id=message_id,
                         request_id=request_id, topic_id=topic_id, reaction=reaction, is_big=is_big)

    return telegram_react
