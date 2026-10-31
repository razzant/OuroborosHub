"""Slack provider facts and the identity of its authenticated bot."""
from __future__ import annotations

import os
from typing import Any, Mapping

from .events import DECLARED_FILE_KEYS, REACTION_EVENTS, file_facts, provider_facts
from .provider_context import enrich_event
from .store import InboxItem, _iso

# reaction.target.excerpt never exceeds this; the full words stay in provider_facts.
_REACTION_EXCERPT_CHARS = 500


def slack_identity(auth):
    user_id = str(auth.get("user_id") or "")
    if not user_id:
        return {}
    return {"sends_as": {"id": user_id, "label": str(auth.get("user") or user_id), "kind": "bot"}}


def _staged_path(record: Mapping[str, Any]) -> str:
    """The one predicate for bytes the bridge staged and submits to Host.

    A nonblank path is the name the bridge wrote and is kept byte-for-byte;
    whitespace only decides whether there is a path at all.
    """
    path = str(record.get("path") or "")
    return path if path.strip() else ""


def _attachments(item: InboxItem) -> list[dict[str, Any]]:
    """Describe every declared file and what this bridge's staging observed.

    ``content_available`` means the bridge staged bytes; the Host attachment
    manifest remains authoritative for what the model can actually open.
    """
    attachments = []
    for index, file in enumerate(item.files):
        outcome = item.staged_files[index] if index < len(item.staged_files) else {}
        path = _staged_path(outcome)
        entry = {
            "file_id": str(file.get("file_id") or ""),
            "file_name": str(file.get("name") or ""),
            "mime_type": str(file.get("mimetype") or ""),
            "file_size": int(file.get("size") or 0),
            **{key: value for key, value in file_facts(file).items() if key not in DECLARED_FILE_KEYS},
            "content_available": bool(path),
        }
        if path:
            # The staged basename; Host derives its attachment label from it
            # and may shorten or normalize that label.
            entry["staged_as"] = os.path.basename(path)
        if outcome.get("stage_error"):
            entry["stage_error"] = str(outcome["stage_error"])
            details = outcome.get("stage_error_details")
            entry["stage_error_details"] = dict(details) if isinstance(details, Mapping) else {}
        attachments.append(entry)
    return attachments


def _reaction_target(item: InboxItem, lookup: Mapping[str, Any], self_user_id: str) -> dict[str, Any]:
    """What is known about the reacted message; each fact only when its source states it."""
    message = lookup.get("message") if lookup.get("status") == "available" else None
    message = message if isinstance(message, Mapping) else {}
    target: dict[str, Any] = {"message_id": item.message_ts, "resolution": "resolved" if message else "unresolved"}
    own_receipt = lookup.get("is_self") is True
    author = (str((item.structured.get("reaction") or {}).get("item_user") or "") or str(message.get("user") or "")
              or (self_user_id if own_receipt else ""))
    if author or own_receipt:
        target["author"] = {"platform_actor_id": author} if author else {}
        if own_receipt or self_user_id:
            target["author"]["is_self"] = own_receipt or author == self_user_id
    words = str(message.get("text") or "")
    if words.strip():
        target["excerpt"] = {"text": words[:_REACTION_EXCERPT_CHARS],
                             "truncated": len(words) > _REACTION_EXCERPT_CHARS}
    return target


def _with_reaction_fact(event: dict[str, Any], item: InboxItem) -> None:
    """Put the provider-neutral fact beside the raw facts, on the conversation it proves.

    ``thread_id`` is the only thread carrier: an unknown conversation stays channel-level,
    never a phantom root named after the reacted message. Own words stay out of ``text``.
    """
    raw = item.structured.get("reaction") or {}
    value = [{"type": "name", "value": str(raw.get("name") or "")}]
    lookup = (item.provider_context or {}).get("reaction_target") or {}
    target = _reaction_target(item, lookup, str(item.structured.get("self_user_id") or ""))
    thread_id = str(lookup.get("thread_ts") or "") if target["resolution"] == "resolved" else ""
    event.update(thread_id=thread_id, text="",
                 conversation_key=f"slack:{item.team_id}:{item.channel_id}:{thread_id or '0'}")
    event["actor"]["kind"] = "user"
    event["conversation"]["thread_ts"] = thread_id
    message = event["message"]
    message["reaction"] = {"schema_version": 1, "target": target,
                           "change": "add" if item.event_type == "reaction_added" else "remove",
                           ("new" if item.event_type == "reaction_added" else "old"): value}
    if lookup:
        facts = {**message["provider_facts"], "reaction": {**raw, "target_lookup": _public_lookup(lookup)}}
        message["provider_facts"] = facts
    sent_at = _sent_at(item.event_ts)
    if sent_at:
        message["sent_at"] = sent_at


def _public_lookup(lookup: Mapping[str, Any]) -> dict[str, Any]:
    """The full retained or provider target, without credentialed file URLs."""
    public = dict(lookup)
    if isinstance(public.get("message"), Mapping):
        public["message"] = provider_facts({"message": dict(public["message"])})["message"]
    return public


def _sent_at(event_ts: str) -> str:
    try:
        return _iso(float(event_ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def slack_presence_event(item: InboxItem, *, transport_queue: bool = False, places: bool = False) -> dict[str, Any]:
    """Map exact Slack facts into the frozen provider-neutral event shape.

    ``transport_queue`` adds the inbox snapshot taken before the event's first
    submission attempt (``BridgeStore.conversation_queue``) to
    ``event.conversation``; only a continuation-capable Host is sent it.
    """

    thread_id = item.thread_ts or item.message_ts
    event = {
        "source_event_id": item.provider_event_key,
        "provider": "slack",
        "account_id": item.team_id,
        "conversation_id": item.channel_id,
        "thread_id": thread_id,
        "conversation_key": f"slack:{item.ordering_key}",
        "actor": {
            "platform": "slack",
            "platform_actor_id": item.actor_user_id,
            "actor_team_id": item.actor_team_id or item.team_id,
        },
        "conversation": {
            "platform": "slack",
            "workspace_id": item.team_id,
            "enterprise_id": item.enterprise_id,
            "channel_id": item.channel_id,
            "channel_type": item.channel_type,
            "thread_ts": thread_id,
        },
        "message": {
            "message_id": item.message_ts,
            "thread_id": item.thread_ts,
            "event_id": item.event_id,
            "envelope_id": item.envelope_id,
            "event_ts": item.event_ts,
            "client_msg_id": item.client_msg_id,
            "event_type": item.event_type,
            "subtype": item.subtype,
            "attachments": _attachments(item),
            "blocks": list(item.structured.get("blocks") or []),
            "provider_facts": provider_facts(item.structured),
        },
        "text": item.text,
    }
    event = enrich_event(event, item.provider_context)
    if item.event_type in REACTION_EVENTS:
        _with_reaction_fact(event, item)
    if places:
        event["facts"] = slack_wake_facts(event)
    # A reaction's inbox lane is its target's timestamp, not the conversation it proves.
    if transport_queue and item.transport_queue is not None and item.event_type not in REACTION_EVENTS:
        event["conversation"]["transport_queue"] = dict(item.transport_queue)
    return event


def slack_wake_facts(event: Mapping[str, Any]) -> list[str]:
    """Match only provider identities/relationships, never the meaning of speech."""
    conversation, message = event["conversation"], event["message"]
    provider = message.get("provider_facts") or {}
    own = provider.get("self_user_id")
    facts = []
    if message.get("event_type") in {"message", "app_mention"} and message.get("subtype") != "message_deleted":
        facts.append("any_message")
        if conversation.get("channel_type") == "im" or conversation.get("is_im"):
            facts.append("direct_message")
        if conversation.get("channel_type") == "mpim" or conversation.get("is_mpim"):
            facts.append("group_dm")
        if own and own in provider.get("mentioned_user_ids", []):
            facts.append("mention")
        if message.get("thread_id"):
            if own and provider.get("parent_user_id") == own:
                facts.extend(["reply_to_me", "my_thread"])
            history = (conversation.get("thread_history") or {}).get("data") or {}
            if own and "my_thread" not in facts and any(row.get("user") == own for row in history.get("messages", [])):
                facts.append("my_thread")
    if event["actor"].get("is_bot") or provider.get("actor_is_bot"):
        facts.append("from_bot")
    return facts


