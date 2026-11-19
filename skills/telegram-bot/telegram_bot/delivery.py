"""Exact provider-send observations for the existing outbox."""

from pathlib import Path

from .events import _reaction_values, is_user_id


DIRECT_SEND = "direct_send"
OWNER_SEND = "owner_dm_send"
DIRECT_KINDS = (DIRECT_SEND, OWNER_SEND)
# The canonical Host producer name in owner confirmations, not a caller-supplied field.
OWNER_SEND_TOOL = "ext_14_r_telegram-bot_telegram_send"


def telegram_topic(value):
    """The exact outbound topic, empty when absent; None means invalid."""
    if value is None or value == "":
        return ""
    return value if is_user_id(value) else None


def matches_owner_destination(confirmation, account_id, chat_id, thread_id):
    return (isinstance(confirmation, dict) and confirmation.get("provider") == "telegram"
            and confirmation.get("account_id") == account_id
            and confirmation.get("conversation_id") == chat_id
            and confirmation.get("thread_id") == thread_id)


def owner_condition_key(condition, account_id, chat_id, thread_id, *, tool):
    """Identify the saved proof and delivery Place; a newer observation alone is not a new destination."""
    if (not matches_owner_destination(condition, account_id, chat_id, thread_id)
            or tool not in (OWNER_SEND_TOOL, "place_reply") or condition.get("tool") != tool):
        return None
    outbound = condition.get("delivery_place")
    floor = condition.get("observed_revision")
    confirmed_at = condition.get("confirmed_at")
    if (not matches_owner_destination(outbound, account_id, chat_id, thread_id)
            or type(outbound.get("version")) is not int or outbound["version"] != 1
            or type(floor) is not int or not isinstance(confirmed_at, str) or not confirmed_at):
        return None
    for place in (condition, outbound):
        place_id, revision = place.get("place_id"), place.get("connected_revision")
        if (place.get("connector") != "telegram-bot" or not isinstance(place_id, str) or not place_id
                or type(revision) is not int or not 0 <= revision <= floor):
            return None
    # Comparison only. The original opaque condition, including its floor, goes back to the Host at delivery.
    return {key: value for key, value in condition.items() if key != "observed_revision"}


def direct_send_fields(kind, account_id, user_id, owner_confirmation=None):
    # This kind retains the pin; the outbox's live states keep it out of older claimers.
    fields = {"kind": DIRECT_SEND, "send_kind": kind, "direct_account_id": account_id, "direct_reply_user_id": user_id}
    if owner_confirmation is not None:
        fields.update(kind=OWNER_SEND, owner_confirmation=owner_confirmation)
    return fields


def matches_direct_recipient(payload, account_id, user_id):
    return (payload.get("kind") in DIRECT_KINDS and is_user_id(account_id) and is_user_id(user_id)
            and payload.get("direct_account_id") == account_id
            and payload.get("chat_id") == user_id == payload.get("direct_reply_user_id"))


def delivery_kind(payload):
    kind = payload.get("kind") or "message"
    return payload.get("send_kind") if kind in DIRECT_KINDS else kind


def names_message(answer):
    """Telegram's answer to a send names the message it created: a positive integer ``message_id``.

    The one test of a confirmed message, for the outbox's decisions, its receipts and its reports; nothing else in the
    answer is checked. An answer that fails it stays in the row as stored, and confirms nothing.
    """
    message_id = answer.get("message_id") if isinstance(answer, dict) else None
    return type(message_id) is int and message_id > 0


def stored_answers(payload):
    """The answers a send row stored (text chunks in ``_sent_messages``, a file's or an album's in ``_sent_media``),
    as (confirmed messages, other answers kept as stored); ``None`` when the row stored no answer."""
    for key in ("_sent_messages", "_sent_media"):
        if key in payload:
            stored = payload[key] if isinstance(payload[key], list) else [payload[key]]
            return ([answer for answer in stored if names_message(answer)],
                    [answer for answer in stored if not names_message(answer)])
    return None


def retained_sender(payload):
    """The sender of a partly sent row, or None if its confirmed parts do not establish one."""
    if "_sending_account_id" in payload:
        account = payload["_sending_account_id"]
        return account if is_user_id(account) else None
    if payload.get("kind") in DIRECT_KINDS:
        return ""  # the independently retained direct_account_id is checked on every attempt
    confirmed, _ = stored_answers(payload) or ([], [])
    if not confirmed:
        return ""
    accounts = set()
    for message in confirmed:
        sender = message.get("from")
        if not isinstance(sender, dict) or sender.get("is_bot") is not True or type(sender.get("id")) is not int:
            return None
        account = str(sender["id"])
        if not is_user_id(account):
            return None
        accounts.add(account)
    return next(iter(accounts)) if len(accounts) == 1 else None


def tool_origin(ctx):
    origin = {"kind": "tool"}
    if getattr(ctx, "task_id", None):
        origin["task_id"] = str(ctx.task_id)
    metadata = getattr(ctx, "task_metadata", None) or {}
    event = (metadata.get("presence") or {}).get("event") or {}
    if event.get("source_event_id"):
        origin["source_event_id"] = str(event["source_event_id"])
    return origin


# A Host without presence_explicit_completion could count the report as speech; the provider receipt stays local.
REACTION_HISTORY_UNAVAILABLE = "Held: Ouroboros records this reaction after an update."
# An earlier version's report that does not state its message and values; it never reaches Host history.
REACTION_REPORT_UNREADABLE = "Held: Ouroboros will not record this reaction."


def _reaction_fact(values, target, state):
    """A set says what was asked for. Telegram does not say what it replaced, so nothing here does."""
    return {"schema_version": 1, "operation": "set", "new": values, "target": target,
            **({"effect": "applied"} if state in {"delivered", "accepted"} else {})}


def _reaction_receipt(payload, state):
    parameters = payload.get("parameters") or {}
    return _reaction_fact(_reaction_values(parameters.get("reaction")), payload.get("_reaction_target") or {
        "message_id": str(parameters.get("message_id") or ""), "resolution": "unresolved"}, state)


def _album(payload):
    """Telegram moves a reaction on an album member to the album's first undeleted one and answers only True.

    So the fact keeps the requested message, and which member took it stays unknown. Only a retained
    record names the album; other targets say nothing either way.
    """
    group = payload.get("_reaction_album")
    return {"album": {"media_group_id": group, "effective_message_id": "unknown"}} if group else {}


def reaction_receipt(payload):
    """The local provider receipt of a set Telegram confirmed."""
    return {"kind": "operation", "method": payload["method"], "result": payload["_operation_result"],
            "effect": "applied", **_album(payload)}


def is_reaction_report(report):
    """A frozen report of a reaction, in today's typed shape or an earlier version's list."""
    message = report.get("message")
    return isinstance(message, dict) and (message.get("method") == "setMessageReaction"
                                          or (message.get("kind") == "operation" and "reaction" in message))


def typed_reaction_report(report):
    """What a Host that reads typed actions may receive, from this report's own retained facts.

    An earlier version froze ``reaction`` as Telegram's list. Its message and values are exact, and
    its topic was never recorded, so the target stays unresolved. ``None`` when the facts do not say.
    The stored report is not changed.
    """
    message = report["message"]
    reaction, target = message.get("reaction"), message.get("target_message_id")
    if isinstance(reaction, dict):
        return report if reaction.get("schema_version") == 1 else None
    if not isinstance(reaction, list) or type(target) not in (int, str) or not str(target).strip():
        return None
    values = _reaction_values(reaction)
    if len(values) != len(reaction):
        return None
    fact = _reaction_fact(values, {"message_id": str(target), "resolution": "unresolved"}, report.get("state"))
    return {**report, "message": {**message, "reaction": fact}}


def delivery_report(delivery_id, payload, *, part_id, state, receipt=None, text="", fmt="", error=""):
    reporting = payload.get("_reporting") or {}
    if reporting.get("version") != 1:
        return None
    reaction = payload.get("kind") == "operation" and payload.get("method") == "setMessageReaction"
    if reaction and reporting.get("explicit_completion") != 1:
        # A Host without it may count this receipt as speech and drop the later answer. The
        # provider receipt stays local; edits and sends keep reporting as they always have.
        return None
    receipt = receipt or {}
    wire = receipt.get("_delivery") or {}
    message = {"kind": delivery_kind(payload)}
    for key in ("message_id", "date", "entities", "caption_entities", "photo", "document", "video", "audio", "voice",
                "animation", "media_group_id"):
        if key in receipt:
            message[key] = receipt[key]
    if payload.get("file_path"):
        message["file_name"] = Path(payload["file_path"]).name
    if payload.get("reply_to_message_id"):
        message["reply_to_message_id"] = payload["reply_to_message_id"]
    if payload.get("kind") == "operation":
        message["method"] = str(payload.get("method") or "")
        message["target_message_id"] = (payload.get("parameters") or {}).get("message_id")
        if payload.get("method") == "editMessageText":
            message["replacement_text"] = str(payload.get("text") or "")
        if reaction:
            message["reaction"] = _reaction_receipt(payload, state)
            message.update(_album(payload))
        if payload.get("original_delivery_id"):
            message["original_delivery_id"] = str(payload["original_delivery_id"])
    if error:
        # Only stored answers that name their message count; an earlier version's empty answer stays in the row.
        message.update(error=error, confirmed_parts=len((stored_answers(payload) or ([], []))[0]))
    return {
        "schema_version": 1, "delivery_id": delivery_id, "part_id": str(part_id),
        "state": state, "provider": "telegram", "account_id": str(reporting.get("account_id") or ""),
        "conversation_id": str(payload["chat_id"]), "thread_id": str(payload.get("topic_id") or ""),
        "text": "" if payload.get("kind") == "operation" else wire.get("text", text),
        "format": "" if payload.get("kind") == "operation" else wire.get("format", fmt),
        "message": message, "origin": reporting.get("origin") or {"kind": "automatic"},
    }
