"""Place tools: the owner handoff to Main and native Telegram actions in a place chat.

Authority comes from the Host, never from the model: the calling task id names
its Host record, whose retained Presence source must be a message this bridge
committed. A delegated child never acts here. Every refusal is a structured
``{"ok": false}`` result, so the dispatcher records it as a failure.

``hand_to_main`` passes the owner's own retained words, verbatim, into ordinary Main
owner routing (``/chat/inject``), exactly as the classic bridge would have; only the
place focus itself may call it, the sender must be the bridge's pinned owner and
the account the Host's place records confirm NOW. A later message counts only when
the Host delivered it to this focus as context mail. Forwarded text, quoted
replies and replies to unmapped old bot messages are never handed off.

``telegram_message`` sends (queued in the place outbox, which rechecks the place
before every part), edits a bot message of this chat, reacts to a retained message
of this chat, or reads an earlier action's receipt. A request id is one action's
identity across every action kind: the same full request rejoins, anything else
under it (another kind, other words, another target) refuses.
"""
from __future__ import annotations

import json
import time
from typing import Awaitable, Callable

from .telegram_api import TelegramRequestRejected, TelegramTransportError, _u16len, markdown_to_telegram_html
from .telegram_places import _rows, _tx, bot_identity, enqueue, forward_origin, place_lookup, report
from .telegram_state import _data_dir, _message_ref

HANDOFF_DESCRIPTION = (
    "Pass the owner's own Telegram message, verbatim, to ordinary Main owner routing, which can steer "
    "existing Projects and tasks with the owner's authority. Use it when the owner, writing in this Telegram "
    "place, asks for something that belongs to owner work outside this place. You choose; you supply no words. "
    "Default: this turn's own message; source_event_id may name a later owner message the Host delivered to "
    "this focus. Refused for anyone but the owner the place currently confirms, for forwarded messages, replies "
    "to unmapped old bot messages and delegated work. Main answers in this chat; afterwards finish this turn "
    "silently unless you add something of your own.")
HANDOFF_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "source_event_id": {"type": "string", "description": "Optional: telegram:<chat>:<message> of a later owner "
                        "message delivered to this focus; empty means this turn's own message."}}}
MESSAGE_DESCRIPTION = (
    "Act in this Telegram place's own chat: send a message (queued; read its receipt later), edit a message "
    "this bot sent here, react to a retained message here, or read an earlier action by request_id. Use a "
    "stable request_id per intended action and reuse it after an uncertain call; it never resends.")
MESSAGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action", "request_id"], "properties": {
    "action": {"type": "string", "enum": ["send", "edit", "react", "read"]},
    "request_id": {"type": "string", "description": "1..100 characters; the action's identity."},
    "text": {"type": "string", "description": "Markdown for send/edit."},
    "message_id": {"type": "integer", "minimum": 0, "description": "Edit/react target, or send's reply target."},
    "emoji": {"type": "string", "description": "React: one emoji; empty removes the reaction."}}}


class ActionRefused(ValueError):
    """Nothing was sent; the reason is for the mind."""


def refusal(code: str, exc: Exception) -> str:
    return json.dumps({"ok": False, "code": code, "error": str(exc)}, ensure_ascii=False)


def _source(api, ctx, *, focus_only: bool) -> tuple[dict, dict, dict]:
    """The calling task's Host record and its retained Telegram source (the Host's own store)."""
    from ouroboros.task_results import load_task_result

    task_id = str(getattr(ctx, "task_id", "") or "")
    record = load_task_result(_data_dir(api), task_id) if task_id else None
    meta = (record or {}).get("metadata") or {}
    presence = meta.get("presence") if isinstance(meta.get("presence"), dict) else {}
    event = presence.get("event") if isinstance(presence.get("event"), dict) else {}
    lineage = ((record or {}).get("task_contract") or {}).get("lineage") or {}
    if (not record or presence.get("transport_skill") != "telegram" or not event.get("source_event_id")
            or record.get("parent_task_id") or meta.get("parent_task_id") or lineage.get("parent_task_id")
            or str(meta.get("delegation_role") or "") == "subagent"):
        raise ActionRefused("only a root task started from this Telegram place can act here")
    if focus_only and meta.get("wake_cause") != "focus":
        raise ActionRefused("only the Telegram place focus itself can hand its owner's words to Main")
    rows = _rows(api, "SELECT * FROM inbox WHERE source_event_id=?", (str(event["source_event_id"]),))
    if not rows or rows[0]["conversation_key"] != event.get("conversation_key"):
        raise ActionRefused("this task's source is not a message retained by the Telegram bridge")
    return record, presence, rows[0]


def _confirmed_now(api, presence: dict, actor: str, account_id: str) -> None:
    """The Host's place records as they stand now; a revoked confirmation refuses immediately."""
    from ouroboros.places import PlaceError, get_place, is_owner_account, places_path

    root = _data_dir(api)
    if not places_path(root).exists():
        raise ActionRefused("there are no place records")
    try:
        place = get_place(root, str(presence.get("place_id") or ""))
    except PlaceError as exc:
        raise ActionRefused(f"this Telegram place is not available ({exc.code})") from None
    if not is_owner_account(place, actor, account_id):
        raise ActionRefused("the sender is not an owner account this place currently confirms")


def _offered_to(api, task_id: str, binding_id: str, row: dict) -> bool:
    """Whether the Host handed this later message to the calling focus as context mail."""
    from ouroboros.owner_mailbox import mail_read_state, mailbox_holds
    from ouroboros.presence_runner import PresenceTurnEvent, presence_event_identity

    frozen = json.loads(row["event_json"] or "{}")
    if not frozen:
        return False
    event = PresenceTurnEvent(**{key: frozen[key] for key in (
        "source_event_id", "provider", "account_id", "conversation_id", "thread_id", "conversation_key",
        "actor", "conversation", "message", "text")})
    msg_id = "place-event:" + presence_event_identity(binding_id, event)
    root = _data_dir(api)
    return mailbox_holds(root, task_id, msg_id) or mail_read_state(root, task_id, msg_id) is True


async def hand_to_main(api, ctx, source_event_id: str, *, pinned_owner: str,
                       relay: Callable[[int, dict], Awaitable[str]]) -> str:
    """Hand one retained owner message to ``relay`` (the classic owner inject), once per message."""
    record, presence, own = _source(api, ctx, focus_only=True)
    event = presence["event"]
    actor = str((event.get("actor") or {}).get("platform_actor_id") or "")
    if not pinned_owner or actor != pinned_owner:
        raise ActionRefused("the sender is not the bridge's pinned owner")
    target, requested = own, str(source_event_id or "").strip()
    if requested and requested != own["source_event_id"]:
        later = _rows(api, "SELECT * FROM inbox WHERE source_event_id=? AND chat_id=? AND update_id>?",
                      (requested, own["chat_id"], own["update_id"]))
        if not later or not _offered_to(api, str(record.get("id") or ctx.task_id),
                                        str(presence.get("binding_id") or ""), later[0]):
            raise ActionRefused("source_event_id must name a later message the Host delivered to this focus")
        target = later[0]
    message = json.loads(target["message_json"])
    chat = message.get("chat") or {}
    if (str((message.get("from") or {}).get("id") or "") != pinned_owner or str(chat.get("id") or "") != pinned_owner
            or chat.get("type") != "private" or not target["owner"]):
        raise ActionRefused("only the owner's own private message can go to Main")
    # The handed-off message's own sender, confirmed now; never borrowed from the focus's first message.
    _confirmed_now(api, presence, str((message.get("from") or {}).get("id") or ""), str(event.get("account_id") or ""))
    if forward_origin(message) or message.get("via_bot") or message.get("is_automatic_forward"):
        raise ActionRefused("a forwarded message is not the owner's own words; ask the owner to write the request")
    reference = ((json.loads(target["event_json"] or "{}").get("message") or {}).get("reply_to") or {}).get("reference") or {}
    if reference.get("state") == "unknown":
        raise ActionRefused("the message replies to an old bot message this bridge no longer maps; ask the owner "
                            "to send the request again as a new message so Main is not given a guessed reference")
    client_message_id = await relay(target["update_id"], message)
    with _tx(api) as conn:
        conn.execute("INSERT OR REPLACE INTO handoffs(update_id,task_id,client_message_id,at) VALUES (?,?,?,?)",
                     (target["update_id"], str(ctx.task_id), client_message_id, time.time()))
    return json.dumps({"ok": True, "status": "handed_to_main", "source_event_id": target["source_event_id"],
                       "client_message_id": client_message_id,
                       "note": "Main received the owner's exact words; a repeat of this call rejoins the same "
                               "message. Main answers in this Telegram chat."}, ensure_ascii=False)


def _action_row(api, delivery_id: str) -> dict:
    rows = _rows(api, "SELECT * FROM actions WHERE delivery_id=?", (delivery_id,))
    return rows[0] if rows else {}


def claim(conn, delivery_id: str, payload: dict, state: str) -> dict:
    """Bind a request id to one full request inside the caller's ``_tx``; a different request refuses."""
    row = conn.execute("SELECT payload_json, state, receipt_json FROM actions WHERE delivery_id=?",
                       (delivery_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO actions(delivery_id,payload_json,state,at) VALUES (?,?,?,?)",
                     (delivery_id, json.dumps(payload, sort_keys=True), state, time.time()))
        return {}
    if json.loads(row["payload_json"]) != json.loads(json.dumps(payload, sort_keys=True)):
        raise ActionRefused("this request_id already names a different request; nothing was changed")
    return dict(row)


def _retained(api, chat_id: int, message_id: int, *, bot_only: bool) -> bool:
    record = _message_ref(api, chat_id, message_id)
    if bot_only:
        return record.get("kind") in {"place_reply", "place_tool"}
    return bool(record) or bool(_rows(api, "SELECT 1 FROM inbox WHERE chat_id=? AND source_event_id=?",
                                      (chat_id, f"telegram:{chat_id}:{message_id}")))


def _confirmed_edit(result: dict, chat_id: int, message_id: int) -> bool:
    """Telegram answers an edit with the edited Message; only that exact message proves it."""
    edited = result.get("result") if isinstance(result, dict) else None
    return (isinstance(edited, dict) and int(edited.get("message_id") or 0) == int(message_id)
            and int((edited.get("chat") or {}).get("id") or chat_id) == int(chat_id))


async def telegram_message(api, ctx, *, action: str, request_id: str, text: str, message_id: int, emoji: str,
                           client_factory) -> str:
    record, presence, own = _source(api, ctx, focus_only=False)
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
        raise ActionRefused("use a stable request_id of 1..100 characters; reuse it after an uncertain call")
    chat, task_id = int(own["chat_id"]), str(record.get("id") or ctx.task_id)
    delivery_id = f"tool:{task_id}:{request_id}"
    origin = {"kind": "tool", "task_id": task_id, "source_event_id": own["source_event_id"]}
    reporting = int(presence.get("delivery_reporting_version") == 1)
    if action == "read":
        claimed = _action_row(api, delivery_id)
        if not claimed:
            raise ActionRefused("no action was recorded under this request_id")
        sent = _rows(api, "SELECT state, receipts_json FROM outbox WHERE delivery_id=?", (delivery_id,))
        row = sent[0] if sent else claimed
        return json.dumps({"ok": True, "delivery_id": delivery_id, "action": json.loads(claimed["payload_json"])["action"],
                           "state": row["state"],
                           "receipts": json.loads(row.get("receipts_json") or row.get("receipt_json") or "[]")})
    if action not in {"send", "edit", "react"}:
        raise ActionRefused("action must be send, edit, react or read")
    payload = {"action": action, "chat_id": chat, "message_id": int(message_id or 0),
               "text": text if action != "react" else "", "emoji": emoji if action == "react" else ""}
    previous = _action_row(api, delivery_id)
    if previous:  # identity first: an earlier request under this id answers, or a different one refuses
        with _tx(api) as conn:
            claim(conn, delivery_id, payload, "")
        if action == "send":
            return json.dumps({"ok": True, "status": "already_queued", "delivery_id": delivery_id})
        return json.dumps({"ok": previous["state"] == "delivered", "state": previous["state"], "delivery_id": delivery_id,
                           "note": "recorded earlier; never repeated"})
    if action == "send":
        if not str(text).strip():
            raise ActionRefused("a message needs nonempty text")
        if message_id and not _retained(api, chat, message_id, bot_only=False):
            raise ActionRefused("reply only to a message retained in this chat")
        with _tx(api) as conn:  # the request and its queued words commit together
            previous = claim(conn, delivery_id, payload, "queued")
            if not previous:
                enqueue(api, delivery_id, update_id=own["update_id"], chat_id=chat, reply_to=int(message_id or 0),
                        task_id=task_id, source_event_id=own["source_event_id"], text=text, reporting=reporting,
                        origin="tool", kind="place_tool", conn=conn)
        return json.dumps({"ok": True, "status": "already_queued" if previous else "queued", "delivery_id": delivery_id,
                           "note": "Queued, not yet delivered: the place is rechecked before every part. "
                                   "Read the receipt with action=read and the same request_id."})
    if not _retained(api, chat, int(message_id or 0), bot_only=action == "edit"):
        raise ActionRefused("edit only this bot's messages in this chat; react only to messages retained here")
    if action == "edit" and (not str(text).strip() or _u16len(text) > 4096):
        raise ActionRefused("an edit needs nonempty text that fits one Telegram message")
    lane, place = await place_lookup(api, chat)
    if lane != "place" or place.get("state") != "active":
        raise ActionRefused(f"the place is {place.get('state') or lane}; nothing was changed")
    bot = str(bot_identity(api).get("id") or "")
    method = "editMessageText" if action == "edit" else "setMessageReaction"
    with _tx(api) as conn:  # write-ahead: a crash after this point reads as uncertain, never repeated
        if claim(conn, delivery_id, payload, "sending"):
            return json.dumps({"ok": False, "code": "TELEGRAM_ACTION_IN_FLIGHT", "delivery_id": delivery_id,
                               "error": "another call holds this request; read it later"})
        if reporting:  # what a restart reports if this call never returns
            conn.execute("UPDATE actions SET report_json=? WHERE delivery_id=?", (json.dumps({
                "chat_id": chat, "account_id": bot, "origin": origin,
                "message": {"method": method, "target_message_id": int(message_id)}}), delivery_id))
    client, state, error = client_factory(), "delivered", ""
    try:
        if action == "edit":
            result = await client.call("editMessageText", data={"chat_id": str(chat), "message_id": int(message_id),
                                                                "text": markdown_to_telegram_html(text), "parse_mode": "HTML"})
            if not _confirmed_edit(result, chat, int(message_id)):
                state, error = "uncertain", "edit_not_confirmed_for_this_message"
        else:
            result = await client.call("setMessageReaction", data={"chat_id": str(chat), "message_id": int(message_id),
                                       "reaction": json.dumps([{"type": "emoji", "emoji": emoji}] if emoji else [])})
            if result.get("result") is not True:
                state, error = "uncertain", "reaction_not_confirmed"
    except TelegramRequestRejected as exc:
        state, error = "failed", str(exc)
    except TelegramTransportError as exc:
        state, error = ("uncertain" if exc.maybe_sent else "failed"), str(exc)
    receipt = {"method": method, "target_message_id": int(message_id), **({"error": error[:300]} if error else {}),
               **({"replacement_text": text} if action == "edit" else {"reaction": emoji})}
    with _tx(api) as conn:  # the outcome and its report commit together
        conn.execute("UPDATE actions SET state=?, receipt_json=? WHERE delivery_id=?",
                     (state, json.dumps([receipt]), delivery_id))
        if reporting:
            report(conn, delivery_id=delivery_id, part_id="0", state=state, chat_id=chat, account_id=bot,
                   text="", fmt="", message=receipt, origin=origin)
    if state != "delivered":
        return json.dumps({"ok": False, "code": f"TELEGRAM_{action.upper()}_{state.upper()}", "error": error,
                           "delivery_id": delivery_id, "note": "never repeated automatically"})
    return json.dumps({"ok": True, "state": state, "delivery_id": delivery_id})

def register(api, *, client_factory, relay: Callable[..., Awaitable[str]], owner: Callable[[], str]) -> None:
    """The place runner and the four tools, over the plugin's one classic relay.

    ``relay(api, client, message, update_id, text, caption, lang)`` is the classic owner
    inject; it returns the client message id, or "" when nothing was relayed.
    """
    from .telegram_places import Runner

    classic = register_tools(api, client_factory=client_factory, relay=relay, owner=owner)
    api.register_supervised_task("places", lambda: Runner(api, client_factory=client_factory, classic=classic).run(),
                                 restart_policy="on_failure", max_restarts=10)


def register_tools(api, *, client_factory, relay: Callable[..., Awaitable[str]], owner: Callable[[], str]):
    """The four tools; returns the classic relay the runner uses for unconnected chats."""
    from . import telegram_news
    from .telegram_i18n import language

    async def classic(update_id: int, message: dict) -> str:
        return await relay(api, client_factory(), message, update_id,
                           str(message.get("text") or message.get("caption") or "").strip(),
                           str(message.get("caption") or "").strip(), language())

    async def handoff(ctx, source_event_id: str = "") -> str:
        async def carry(update_id: int, message: dict) -> str:
            sent = await classic(update_id, message)
            if not sent:
                raise ActionRefused("the message has no words or file the classic relay can carry")
            return sent
        try:
            return await hand_to_main(api, ctx, source_event_id, relay=carry, pinned_owner=owner())
        except ActionRefused as exc:
            return refusal("HANDOFF_REFUSED", exc)
        except Exception as exc:  # the inject may have landed: its client id makes a repeat rejoin it
            return refusal("HANDOFF_UNCONFIRMED", RuntimeError(
                f"{type(exc).__name__}: {exc}. Main may still have the message; repeat this exact call to "
                "rejoin it, never retype the owner's words"))

    async def message(ctx, action: str, request_id: str, text: str = "", message_id: int = 0, emoji: str = "") -> str:
        try:
            return await telegram_message(api, ctx, action=action, request_id=request_id, text=text,
                                          message_id=message_id, emoji=emoji, client_factory=client_factory)
        except ActionRefused as exc:
            return refusal("TELEGRAM_ACTION_REFUSED", exc)

    def news_read(ctx, after: int = 0, limit: int = 10) -> str:
        try:
            return telegram_news.read(api, ctx, after=after, limit=limit)
        except (ValueError, OSError) as exc:
            return refusal("TELEGRAM_NEWS_REFUSED", exc)

    async def news_publish(ctx, action: str, source_ids: list, text: str = "", request_id: str = "",
                           reason: str = "") -> str:
        try:
            return await telegram_news.publish(api, ctx, action=action, source_ids=source_ids, text=text,
                                               request_id=request_id, reason=reason)
        except (ValueError, OSError) as exc:
            return refusal("TELEGRAM_NEWS_REFUSED", exc)

    api.register_tool("hand_to_main", handoff, description=HANDOFF_DESCRIPTION, schema=HANDOFF_SCHEMA, timeout_sec=60)
    api.register_tool("telegram_message", message, description=MESSAGE_DESCRIPTION, schema=MESSAGE_SCHEMA,
                      timeout_sec=60)
    api.register_tool("telegram_news_read", news_read, description=telegram_news.READ_DESCRIPTION,
                      schema=telegram_news.READ_SCHEMA, timeout_sec=30)
    api.register_tool("telegram_news_publish", news_publish, description=telegram_news.PUBLISH_DESCRIPTION,
                      schema=telegram_news.PUBLISH_SCHEMA, timeout_sec=60)
    return classic
