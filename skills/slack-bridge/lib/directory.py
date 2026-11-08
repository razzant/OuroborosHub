"""Bounded directory traversal: provider paging is transport, identity choice is the mind's."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from .people import directory_key
from .provider_context import lookup_error
from .slack_api import SlackApiError, SlackConfigurationError
from .store import BridgeStore

CACHE_MAX_AGE_SEC = 300
SCAN_TIMEOUT_SEC = 45.0  # Leave room inside the registered 60-second tool envelope.
SCAN_MAX_PAGES = 50


def _exact_id(query: str, kind: str) -> str:
    """Recognize provider identifiers and Slack permalinks, never infer a person's name."""
    pattern = r"[UW][A-Z0-9]{8,}" if kind == "user" else r"[CGD][A-Z0-9]{8,}"
    query = query.strip()
    if re.fullmatch(pattern, query):
        return query
    mention = re.fullmatch(r"<[@#]([A-Z0-9]+)(?:\|[^>]+)?>", query)
    if mention and re.fullmatch(pattern, mention[1]):
        return mention[1]
    parsed = urlsplit(query.strip("<>"))
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host == "slack.com" or host.endswith(".slack.com")):
        return ""
    parts = parsed.path.strip("/").split("/")
    candidate = ""
    if len(parts) >= 2 and parts[0] == ("team" if kind == "user" else "archives"):
        candidate = parts[1]
    elif kind == "channel" and len(parts) >= 3 and parts[0] == "client":
        candidate = parts[2]
    return candidate if re.fullmatch(pattern, candidate) else ""


def _matches(entry: dict[str, Any], query: str) -> bool:
    profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
    values = [entry.get("id"), entry.get("name"), entry.get("real_name"), entry.get("display_name"),
              entry.get("url"), profile.get("display_name"), profile.get("real_name"), profile.get("email")]
    return any(query in str(value or "").casefold() for value in values)


def _result(snapshot: dict[str, Any], query: str, kind: str, *, cached: bool,
            pages: int = 0, error: dict[str, Any] | None = None) -> dict[str, Any]:
    observed = float(snapshot["observed_at"])
    complete = bool(snapshot.get("complete")) and bool(snapshot.get("from_start"))
    result = {
        "ok": error is None, "source": "users.list" if kind == "user" else "conversations.list",
        "query": query, "kind": kind,
        "candidates": [entry for entry in snapshot["entries"] if _matches(entry, query)],
        "next_cursor": snapshot.get("next_cursor") or None, "complete": complete,
        "coverage": "directory" if snapshot.get("from_start") else "from_supplied_cursor",
        "observed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
        "cache_hit": cached, "cache_age_sec": max(0, round(time.time() - observed, 3)),
        "cache_max_age_sec": CACHE_MAX_AGE_SEC, "pages_fetched": pages,
        "entries_scanned": len(snapshot["entries"]),
    }
    if error:
        result["error"] = error
    if not complete:
        result["continuation_note"] = (
            "Directory coverage is incomplete; an empty candidate list is not proof of absence. "
            "Continue with next_cursor and the same query/kind, or refresh=true to scan from the start."
            if result["next_cursor"] else
            "Directory coverage is incomplete and no continuation cursor is available. "
            "Retry with refresh=true; an empty candidate list is not proof of absence."
        )
    return result


async def resolve_directory(api: Any, slack: Any, token: str, *, query: str, kind: str = "channel",
                            cursor: str = "", limit: int = 200, refresh: bool = False) -> dict[str, Any]:
    query = str(query or "").strip()
    if not query or kind not in {"user", "channel"}:
        raise SlackConfigurationError("query is required and kind must be user or channel")
    slack._page_limit(limit)
    if cursor and refresh:
        raise SlackConfigurationError("refresh starts at the beginning; omit cursor")
    exact_id = _exact_id(query, kind)
    exact_email = kind == "user" and re.fullmatch(r"[^@\s/<>]+@[^@\s/<>]+", query)
    if exact_id or exact_email:
        if exact_id:
            source = "users.info" if kind == "user" else "conversations.info"
            value = await (slack.user_info(exact_id) if kind == "user" else slack.conversation_info(exact_id))
        else:
            source = "users.lookupByEmail"
            value = await slack.lookup_user_by_email(query)
        return {"ok": True, "source": source, "query": query, "kind": kind,
                "candidates": [value], "complete": True, "coverage": "exact_lookup", "next_cursor": None,
                "cache_hit": False, "observed_at": datetime.now(timezone.utc).isoformat(), "pages_fetched": 0}

    query = query.casefold().removeprefix("#" if kind == "channel" else "@")
    if not query:
        raise SlackConfigurationError("query must contain a name or identifier")
    store = BridgeStore(api.get_state_dir())
    key = directory_key(kind, token)
    saved = None if refresh else store.directory_snapshot(key)
    now = time.time()
    fresh = saved is not None and 0 <= now - float(saved["observed_at"]) <= CACHE_MAX_AGE_SEC
    if fresh and saved.get("complete") and saved.get("from_start") and not cursor:
        return _result(saved, query, kind, cached=True)
    resume = fresh and saved.get("next_cursor") and (not cursor or cursor == saved["next_cursor"])
    if resume:
        snapshot = saved
        cursor = snapshot["next_cursor"]
    else:
        snapshot = {"observed_at": now, "entries": [], "complete": False,
                    "from_start": not bool(cursor), "next_cursor": cursor or None}
    entries = {str(entry["id"]): entry for entry in snapshot["entries"] if entry.get("id")}
    deadline = time.monotonic() + SCAN_TIMEOUT_SEC
    pages, error = 0, None
    seen_cursors: set[str] = set()
    while pages < SCAN_MAX_PAGES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = {"code": "directory_scan_timeout"}
            break
        try:
            call = slack.list_users(cursor=cursor, limit=limit) if kind == "user" else slack.list_conversations(cursor=cursor, limit=limit)
            page = await asyncio.wait_for(call, timeout=remaining)
        except (SlackApiError, httpx.HTTPError, TimeoutError, asyncio.TimeoutError) as exc:
            error = lookup_error(exc)
            if isinstance(exc, SlackApiError) and exc.error == "invalid_cursor":
                snapshot["next_cursor"] = None
            break
        pages += 1
        for entry in page.get("members" if kind == "user" else "channels", []):
            if isinstance(entry, dict) and entry.get("id"):
                entries[str(entry["id"])] = entry
        next_cursor = str(page.get("next_cursor") or "")
        snapshot.update(next_cursor=next_cursor or None, complete=bool(page.get("complete")))
        if snapshot["complete"]:
            break
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            error = {"code": "directory_cursor_unavailable" if not next_cursor else "directory_cursor_repeated"}
            snapshot["next_cursor"] = None
            break
        seen_cursors.add(cursor)
        cursor = next_cursor
    if pages == SCAN_MAX_PAGES and not snapshot["complete"] and error is None:
        error = {"code": "directory_scan_page_bound"}
    snapshot["entries"] = list(entries.values())
    store.save_directory_snapshot(key, snapshot)
    return _result(snapshot, query, kind, cached=False, pages=pages, error=error)
