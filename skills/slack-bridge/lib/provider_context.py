"""Provider observations enrich a message; they never identify a person for it."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import httpx

from .events import REACTION_EVENTS, provider_facts
from .people import NAME_MAX_AGE_SEC, known_names, person_name, remember_names
from .slack_api import SlackApiError

# One event's people (its mentions and its thread's authors) are read with users.info a few at a time, within one
# provider-I/O budget; a person still unread when it ends keeps no name.
_NAME_READS = 4
_NAME_BUDGET_SEC = 10.0


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
                          retained_target: Mapping[str, Any] | None = None, *,
                          names: Any = None) -> dict[str, Any]:
    """``names`` is the bridge store whose credential-scoped observations name this event's people first."""
    lookups = [
        observe("users.info", lambda: slack.user_info(item.actor_user_id)),
        observe("conversations.info", lambda: slack.conversation_info(item.channel_id)),
    ]
    reaction = item.event_type in REACTION_EVENTS
    has_thread = not reaction and bool(item.conversation_thread)
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
        _flat_chat_target(snapshot, item)
    people = _named_people(item, snapshot)
    if people:
        snapshot["people"] = await capture_people(slack, people, names=names,
                                                  seen={item.actor_user_id: snapshot["user"]})
    return snapshot


def _named_people(item: Any, snapshot: Mapping[str, Any]) -> list[str]:
    """The Slack user ids this event names: its message's explicit mentions, then its captured thread's authors."""
    ids = [str(user_id) for user_id in (item.structured or {}).get("mentioned_user_ids") or []]
    data = (snapshot.get("thread") or {}).get("data")
    rows = data.get("messages") if isinstance(data, Mapping) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and isinstance(row.get("user"), str) and row["user"].strip():
            ids.append(row["user"].strip())
    return list(dict.fromkeys(user_id for user_id in ids if user_id))


async def capture_people(slack: Any, user_ids: list[str], *, names: Any = None,
                         seen: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Name each person, reading Slack at most once per id: this capture's own users.info lookup of them (``seen``),
    else a name observed under this credential within ``NAME_MAX_AGE_SEC``, else one users.info read. A failed read
    keeps an older observation when there is one (``refresh_error`` says why it is not newer); otherwise the name
    stays None beside the error. Every name is dated and names its source; users.info reads are kept for later
    events (``people.remember_names``)."""
    now = datetime.now(timezone.utc)
    token = str(getattr(slack, "bot_token", "") or "")
    cached = known_names(names, token, user_ids) if names is not None and token else {}
    observed = {user_id: dict(observation) for user_id, observation in (seen or {}).items() if user_id in user_ids}
    current = {user_id for user_id, entry in cached.items()
               if user_id not in observed and 0 <= now.timestamp() - entry["observed_at"] <= NAME_MAX_AGE_SEC}
    observed.update(await _read_users(slack, [user_id for user_id in user_ids
                                              if user_id not in observed and user_id not in current], now))
    people: dict[str, dict[str, Any]] = {}
    read: dict[str, dict[str, Any]] = {}
    for user_id in user_ids:
        observation = observed.get(user_id)
        kept = {**cached[user_id], "observed_at": _iso(cached[user_id]["observed_at"])} if user_id in cached else None
        if observation is None:
            people[user_id] = kept
        elif observation.get("status") == "available":
            people[user_id] = read[user_id] = {"name": person_name(observation.get("data")), "source": "users.info",
                                               "observed_at": observation["observed_at"]}
        elif kept is not None:
            people[user_id] = {**kept, "refresh_error": observation.get("error") or {}}
        else:
            people[user_id] = {"name": None, "source": "users.info",
                               "observed_at": observation.get("observed_at") or now.isoformat(),
                               "error": observation.get("error") or {}}
    if names is not None and token and read:
        remember_names(names, token, {user_id: {**entry, "observed_at": _epoch(entry["observed_at"])}
                                      for user_id, entry in read.items()})
    return {"observed_at": now.isoformat(), "users": people}


async def _read_users(slack: Any, user_ids: list[str], now: datetime) -> dict[str, dict[str, Any]]:
    """One users.info read per id, a few at a time, within the shared budget; an unfinished read is a timeout.
    Once Slack limits the rate, reads still waiting are not sent and carry that refusal: later events' author
    lookups share the method's limit."""
    reads = asyncio.Semaphore(_NAME_READS)
    limited: list[dict[str, Any]] = []

    async def read(user_id: str) -> dict[str, Any]:
        async with reads:
            if limited:
                return {**limited[0], "observed_at": datetime.now(timezone.utc).isoformat()}
            observation = await observe("users.info", lambda: slack.user_info(user_id))
            error = observation.get("error") or {}
            if error.get("http_status") == 429 or error.get("code") == "ratelimited":
                limited.append(observation)
            return observation

    tasks = {user_id: asyncio.ensure_future(read(user_id)) for user_id in user_ids}
    try:
        if tasks:
            await asyncio.wait(tasks.values(), timeout=_NAME_BUDGET_SEC)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    timeout = {"status": "unavailable", "source": "users.info", "observed_at": now.isoformat(),
               "error": {"code": "lookup_timeout"}}
    return {user_id: dict(timeout) if task.cancelled() else task.result() for user_id, task in tasks.items()}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _flat_chat_target(snapshot: dict[str, Any], item: Any) -> None:
    """A top-level message of a direct or group chat belongs to the chat's flow. Reaction events
    carry no channel type, so the conversation observation says which kind this is; unknown stays."""
    info = snapshot["conversation"].get("data") if snapshot["conversation"].get("status") == "available" else None
    target = snapshot["reaction_target"]
    if isinstance(info, dict) and (info.get("is_im") or info.get("is_mpim")) and target.get("thread_ts") == item.message_ts:
        target["thread_ts"] = ""


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
    arguments = {"channel_id": item.channel_id, "thread_ts": item.conversation_thread, "limit": 200}
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
    people = snapshot.get("people")
    if isinstance(people, Mapping):
        _with_people(event, people.get("users") if isinstance(people.get("users"), Mapping) else {})
    return event


def _with_people(event: dict[str, Any], users: Mapping[str, Any]) -> None:
    """Name the people this event names as ``{id, name, is_self}``: the message's explicit mentions
    (``message.mentions``, in ``mentioned_user_ids`` order) and the captured thread's authors
    (``thread_history.authors``, in first-message order). ``name`` is None when the capture observed none;
    ``is_self`` is true only for this bot's own known id. The provider facts stay as they are."""
    facts = event["message"].get("provider_facts") or {}
    own = str(facts.get("self_user_id") or "")

    def person(user_id: str) -> dict[str, Any]:
        entry = users.get(user_id) if isinstance(users.get(user_id), Mapping) else {}
        name = entry.get("name")
        return {"id": user_id, "name": name if isinstance(name, str) and name.strip() else None,
                "is_self": bool(own) and user_id == own}

    mentioned = [str(user_id) for user_id in facts.get("mentioned_user_ids") or [] if str(user_id)]
    if mentioned:
        event["message"]["mentions"] = [person(user_id) for user_id in mentioned]
    thread = event["conversation"].get("thread_history")
    data = thread.get("data") if isinstance(thread, dict) else None
    rows = data.get("messages") if isinstance(data, Mapping) else None
    authors = [row["user"].strip() for row in rows if isinstance(row, Mapping)
               and isinstance(row.get("user"), str) and row["user"].strip()] if isinstance(rows, list) else []
    if authors:
        thread["authors"] = [person(user_id) for user_id in dict.fromkeys(authors)]
