"""Model-selected Telegram actions using the existing durable outbox."""

from __future__ import annotations

import pathlib
import re
from typing import Any

from .custody import CustodyStore
from .delivery import tool_origin


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
        inserted = CustodyStore(
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
        return CustodyStore(
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
            store = CustodyStore(pathlib.Path(api.get_state_dir()) / "custody.sqlite3")
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
