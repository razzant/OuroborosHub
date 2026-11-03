"""Provider observations enrich a message; they never identify a person for it."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import httpx

from .events import REACTION_EVENTS, provider_facts
from .slack_api import SlackApiError


def lookup_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, SlackApiError):
        result: dict[str, Any] = {
            "code": exc.error, "http_status": exc.status_code,
            "retry_after": exc.retry_after,
        }
        for key in ("needed", "provided"):
            if key in exc.details:
                result[key] = exc.details[key]
        if exc.error in {"missing_scope", "not_allowed_token_type", "not_in_channel", "channel_not_found", "http_403"}:
            result["hint"] = (
                "Check this bot's granted scopes and conversation membership. "
                "Slack may restrict thread/history access for this bot token; an error is not an empty result."
            )
        return result
    return {"code": "lookup_timeout" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "transport_error",
            "exception_type": type(exc).__name__}


async def observe(source: str, call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        data = await asyncio.wait_for(call(), timeout=10.0)
        return {"status": "available", "source": source, "observed_at": observed_at, "data": data}
    except (SlackApiError, httpx.HTTPError, TimeoutError, asyncio.TimeoutError) as exc:
        return {"status": "unavailable", "source": source, "observed_at": observed_at, "error": lookup_error(exc)}


async def capture_context(slack: Any, item: Any, workspace_name: str,
                          retained_target: Mapping[str, Any] | None = None) -> dict[str, Any]:
    lookups = [
        observe("users.info", lambda: slack.user_info(item.actor_user_id)),
        observe("conversations.info", lambda: slack.conversation_info(item.channel_id)),
    ]
    reaction = item.event_type in REACTION_EVENTS
    has_thread = not reaction and bool(item.thread_ts or item.event_type in {"message", "app_mention"})
    if has_thread:
        lookups.append(capture_thread(slack, item))
    if reaction:
        lookups.append(capture_reaction_target(slack, item, retained_target))
    results = await asyncio.gather(*lookups)
    snapshot = {"user": results[0], "conversation": results[1],
                "workspace": {"name": workspace_name, "source": "auth.test"}}
    if has_thread:
        snapshot["thread"] = results[2]
    if reaction:
        snapshot["reaction_target"] = results[2]
    return snapshot


async def capture_reaction_target(slack: Any, item: Any, retained: Mapping[str, Any] | None) -> dict[str, Any]:
    """Prove which conversation a reacted message belongs to, or say that it is unknown.

    An exact retained record costs no request. Otherwise one conversations.replies read
    narrowed to the target's timestamp: Slack accepts a reply's ts and returns the thread's
    parent beside it, so the page holds at most those two. No retry and no history scan;
    only the exact message counts as proof. Refused, empty or foreign pages stay unknown.
    """
    if retained is not None:
        return {"status": "available", "observed_at": datetime.now(timezone.utc).isoformat(), **retained}

    async def read() -> dict[str, Any]:
        page = await slack.read_messages(item.channel_id, thread_ts=item.message_ts, oldest=item.message_ts,
                                         latest=item.message_ts, inclusive=True, limit=2)
        message = next((row for row in page["messages"]
                        if isinstance(row, Mapping) and row.get("ts") == item.message_ts), None)
        if message is None:
            raise SlackApiError("reacted_message_not_returned")
        return {"message": dict(message)}

    observation = await observe("conversations.replies", read)
    message = (observation.pop("data", None) or {}).get("message")
    if message is not None:
        # A reply names its proved root; a top-level message is its own conversation.
        observation.update(message=message, thread_ts=str(message.get("thread_ts") or message["ts"]))
    return observation


async def capture_thread(slack: Any, item: Any) -> dict[str, Any]:
    """Observe the currently accessible thread, not only the triggering event's past.

    Pages share the existing provider-I/O budget. The dated result is context,
    never additional inbox work or an atomic snapshot of the conversation.
    """
    arguments = {"channel_id": item.channel_id, "thread_ts": item.thread_ts or item.message_ts, "limit": 200}
    data: dict[str, Any] = {**arguments, "messages": [], "complete": False,
                           "next_cursor": None, "pages_read": 0}

    async def read() -> dict[str, Any]:
        cursors: set[str] = set()
        while True:
            cursor = data["next_cursor"] or ""
            if cursor in cursors:
                raise SlackApiError("repeated_history_cursor")
            cursors.add(cursor)
            page = await slack.read_messages(**arguments, cursor=cursor)
            data["messages"].extend(page["messages"])
            data.update(complete=page["complete"], next_cursor=page["next_cursor"],
                        pages_read=data["pages_read"] + 1)
            if page.get("continuation_note"):
                data["continuation_note"] = page["continuation_note"]
            if data["complete"] or not data["next_cursor"]:
                return data

    observation = await observe("conversations.replies", read)
    observation["completed_at"] = datetime.now(timezone.utc).isoformat()
    observation["scope"] = "current_accessible_thread"
    if not data["complete"]:
        if data["pages_read"]:
            observation.update(status="partial", data=data)
        if data["pages_read"] and not data["next_cursor"]:
            observation["continuation"] = {
                "tool": "slack_thread", "requires": "explicit_timestamp_range", "filters": arguments,
            }
        else:
            observation["continuation"] = {
                "tool": "slack_thread", "arguments": {**arguments, "cursor": data["next_cursor"] or ""},
            }
    return observation


def enrich_event(event: dict[str, Any], snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if snapshot is None:
        # Old rows already submitted to Host retain their original event view.
        return event
    for section, target, marker in (("user", "actor", "profile_lookup"),
                                    ("conversation", "conversation", "info_lookup")):
        observation = snapshot.get(section) or {}
        event[target][marker] = {key: value for key, value in observation.items() if key != "data"}
        data = observation.get("data")
        if observation.get("status") != "available" or not isinstance(data, dict):
            continue
        if section == "user":
            profile = data.get("profile")
            if isinstance(profile, dict):
                event["actor"]["profile"] = profile
                if "display_name" in profile:
                    event["actor"]["display_name"] = profile["display_name"]
            if "name" in data:
                event["actor"]["username"] = data["name"]
            for key in ("real_name", "tz", "tz_label", "tz_offset", "locale", "deleted", "is_bot", "is_app_user"):
                if key in data:
                    event["actor"][key] = data[key]
        else:
            for key in ("name", "name_normalized", "topic", "purpose", "is_channel", "is_group",
                        "is_im", "is_mpim", "is_private", "is_shared", "is_archived", "locale", "user"):
                if key in data:
                    event["conversation"][key] = data[key]
    workspace_name = (snapshot.get("workspace") or {}).get("name")
    if workspace_name:
        event["conversation"]["workspace_name"] = workspace_name
        event["conversation"]["workspace_name_source"] = "auth.test"
    if "thread" in snapshot:
        thread = dict(snapshot["thread"])
        data = thread.get("data")
        if isinstance(data, dict):
            # Preserve speech and identity, applying the live-message projection
            # to top-level files. Their private download URLs stay in state.
            thread["data"] = {**data, "messages": [
                provider_facts({"message": message})["message"]
                for message in data.get("messages", [])
            ]}
        event["conversation"]["thread_history"] = thread
    return event
