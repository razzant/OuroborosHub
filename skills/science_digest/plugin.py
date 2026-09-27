"""Public Telegram preview intake; the Ouroboros agent owns editorial decisions.

There is deliberately no model client, Telegram login, timer, or delivery client.
SQLite holds only owner preferences and attempted edition identities. A preview
fetch is read-only and never advances a delivery cursor.
"""
from __future__ import annotations

import hashlib
import html
import http.client
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
POST_ID = re.compile(r"([A-Za-z][A-Za-z0-9_]{4,31})/([1-9][0-9]{0,15})\Z")
EDITION = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
MAX_CHANNELS = 12
MAX_INTERESTS = 4000
MAX_RESPONSE_BYTES = 768 * 1024
MAX_POST_TEXT = 4096
MAX_POSTS_PER_CHANNEL = 20
MAX_OUTPUT_CHARS = 14000
REQUEST_TIMEOUT = 6
_USER_AGENT = "Ouroboros-ScienceDigest/0.1 (public preview)"
_STATE_DIR: Path | None = None


def _state() -> Path:
    if _STATE_DIR is None:
        raise RuntimeError("science_digest state directory not bound")
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / "digest.sqlite3"


@contextmanager
def _connect():
    conn = sqlite3.connect(_state(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS channels (name TEXT PRIMARY KEY COLLATE NOCASE);"
            "CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, day TEXT NOT NULL, post_ids TEXT NOT NULL, "
            "content_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "CREATE TABLE IF NOT EXISTS seen (channel TEXT PRIMARY KEY, last_id INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS observed (channel TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(channel,id));"
            "CREATE TABLE IF NOT EXISTS gaps (channel TEXT PRIMARY KEY, lost_count INTEGER NOT NULL);"
        )
        with conn:
            yield conn
    finally:
        conn.close()


def _channel_name(value: str) -> str:
    raw = str(value or "").strip().lstrip("@")
    if raw.startswith("https://"):
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme != "https" or parsed.hostname != "t.me" or parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("only public https://t.me/<username> channels are supported")
        parts = parsed.path.strip("/").split("/")
        raw = parts[-1] if len(parts) == 1 or (len(parts) == 2 and parts[0] == "s") else ""
    if not USERNAME.fullmatch(raw):
        raise ValueError("expected a public Telegram username (5–32 ASCII letters/digits/underscores)")
    return raw.lower()


def _interests(text: str | None = None) -> str:
    if text is not None:
        cleaned = text.strip()
        if not cleaned or len(json.dumps(cleaned, ensure_ascii=False)) > MAX_INTERESTS:
            return json.dumps({"ok": False, "error": "interests must be 1–4000 encoded characters"})
        with _connect() as conn:
            conn.execute("INSERT INTO preferences (key,value) VALUES ('interests',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cleaned,))
    with _connect() as conn:
        row = conn.execute("SELECT value FROM preferences WHERE key='interests'").fetchone()
    return json.dumps({"ok": True, "interests": row[0] if row else "",
                       "guidance": "On owner request, schedule_followup in this chat with 5-field cron. One owner destination per installation. Objective must name science_digest and read CURRENT skill state, not paste interests/channels. List/disable old digest schedules before replacement; restore old if creation fails. Record returned IDs as attempt, not delivery. Disable schedule separately on opt-out."}, ensure_ascii=False)


def _channels(action: str = "list", name: str = "") -> str:
    if action not in ("add", "remove", "list"):
        return json.dumps({"ok": False, "error": "action must be add, remove or list"})
    try:
        with _connect() as conn:
            if action == "add":
                channel = _channel_name(name)
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
                if count >= MAX_CHANNELS and not conn.execute("SELECT 1 FROM channels WHERE name=?", (channel,)).fetchone():
                    raise ValueError(f"channel limit reached ({MAX_CHANNELS})")
                conn.execute("INSERT OR IGNORE INTO channels(name) VALUES (?)", (channel,))
            elif action == "remove":
                channel = _channel_name(name)
                conn.execute("DELETE FROM channels WHERE name=?", (channel,))
            rows = conn.execute("SELECT name FROM channels ORDER BY name").fetchall()
        return json.dumps({"ok": True, "channels": [row[0] for row in rows]}, ensure_ascii=False)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(request.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect())


class _Posts(HTMLParser):
    """Extract post text and links without executing or interpreting page scripts."""

    def __init__(self, channel: str) -> None:
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.depth = 0
        self.post: dict[str, Any] | None = None
        self.post_depth = 0
        self.text_depth = 0
        self.time_depth = 0
        self.skip_depth = 0
        self.posts: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self.post is None and tag == "div" and "tgme_widget_message" in (values.get("class") or "").split():
            marker = values.get("data-post") or ""
            match = POST_ID.fullmatch(marker)
            if match and match.group(1).lower() == self.channel:
                self.post = {"id": marker.lower(), "url": f"https://t.me/{self.channel}/{match.group(2)}",
                             "date": "", "text": "", "links": []}
                self.post_depth = self.depth + 1
        if self.post is not None:
            classes = (values.get("class") or "").split()
            if tag in ("script", "style"):
                self.skip_depth += 1
            if tag == "div" and "tgme_widget_message_text" in classes:
                self.text_depth = self.depth + 1
            if tag == "time" and self.post.get("date") == "":
                self.post["date"] = values.get("datetime") or ""
                self.time_depth = self.depth + 1
            if tag == "a" and self.text_depth and not self.skip_depth:
                href = html.unescape(values.get("href") or "")
                try:
                    parsed = urllib.parse.urlsplit(href)
                    valid_link = parsed.scheme in ("http", "https") and bool(parsed.hostname)
                except ValueError:
                    valid_link = False
                if valid_link and len(href) <= 300:
                    links = self.post["links"]
                    if href not in links and len(links) < 3:
                        links.append(href)
            if tag == "br" and self.text_depth and not self.skip_depth:
                self.post["text"] += "\n"
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            return
        if self.post is not None:
            if self.skip_depth and tag in ("script", "style"):
                self.skip_depth -= 1
            if self.text_depth == self.depth and tag == "div":
                self.text_depth = 0
            if self.time_depth == self.depth and tag == "time":
                self.time_depth = 0
            if self.post_depth == self.depth and tag == "div":
                text = " ".join(self.post["text"].split())
                self.post["text"] = text[:MAX_POST_TEXT]
                self.post["text_truncated"] = len(text) > MAX_POST_TEXT
                self.post["media_only"] = not bool(text)
                self.posts.append(self.post)
                self.post = None
                self.text_depth = 0
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.post is not None and self.text_depth and not self.skip_depth:
            self.post["text"] += data


def _fetch_channel(channel: str) -> tuple[list[dict[str, Any]], str]:
    url = f"https://t.me/s/{channel}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html"})
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname != "t.me":
                return [], "unexpected host in response"
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                return [], "response is not HTML"
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        return [], f"preview unavailable: {type(exc).__name__}"
    if len(raw) > MAX_RESPONSE_BYTES:
        return [], "preview exceeds byte limit"
    parser = _Posts(channel)
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, AssertionError):
        return [], "preview HTML could not be parsed"
    if not parser.posts:
        return [], "no readable posts: preview may be empty, changed, or access-limited"
    return parser.posts[-MAX_POSTS_PER_CHANNEL:], ""


def _fetch_posts(limit: int = 10, include_attempted: bool = False, channel: str = "") -> str:
    try:
        per_channel = int(limit)
        if per_channel < 1 or per_channel > MAX_POSTS_PER_CHANNEL:
            raise ValueError("limit must be 1–20 per channel")
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "limit must be 1–20 per channel"})
    with _connect() as conn:
        channels = [row[0] for row in conn.execute("SELECT name FROM channels ORDER BY name")]
        interests = conn.execute("SELECT value FROM preferences WHERE key='interests'").fetchone()
        seen = {row[0]: row[1] for row in conn.execute("SELECT channel,last_id FROM seen")}
    if not channels:
        return json.dumps({"ok": False, "error": "no channels configured; ask owner for public channel usernames"})
    if channel:
        try:
            selected = _channel_name(channel)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if selected not in channels:
            return json.dumps({"ok": False, "error": "channel is not configured"})
        channels = [selected]
    attempts: list[dict[str, Any]] = []
    with _connect() as conn:
        for row in conn.execute("SELECT key,day,post_ids,recorded_at FROM attempts ORDER BY recorded_at DESC,key DESC LIMIT 7"):
            attempts.append({"key": row[0], "day": row[1], "post_count": len(json.loads(row[2])),
                             "recorded_at": row[3], "status": "attempted_not_delivered"})
    results = []
    for channel in channels:
        posts, error = _fetch_channel(channel)
        watermark = seen.get(channel, 0)
        oldest = int(posts[0]["id"].split("/")[1]) if posts else 0
        lost_now = 0
        if posts and not error:
            with _connect() as conn:
                visible_ids = {int(post["id"].split("/")[1]) for post in posts}
                missing = [row[0] for row in conn.execute(
                    "SELECT id FROM observed WHERE channel=? AND id>?", (channel, watermark)
                ) if row[0] not in visible_ids]
                lost_now = len(missing)
                if lost_now:
                    # The public preview no longer exposes these observed but
                    # unattempted IDs. Keep the loss count, not an impossible
                    # requirement to acknowledge posts we can no longer read.
                    conn.executemany("DELETE FROM observed WHERE channel=? AND id=?",
                                     ((channel, identifier) for identifier in missing))
                    conn.execute("INSERT INTO gaps(channel,lost_count) VALUES (?,?) ON CONFLICT(channel) "
                                 "DO UPDATE SET lost_count=lost_count+excluded.lost_count", (channel, lost_now))
                gap = conn.execute("SELECT lost_count FROM gaps WHERE channel=?", (channel,)).fetchone()
        else:
            with _connect() as conn:
                gap = conn.execute("SELECT lost_count FROM gaps WHERE channel=?", (channel,)).fetchone()
        new_posts = [post for post in posts if include_attempted or int(post["id"].split("/")[1]) > watermark]
        if include_attempted:
            for post in new_posts:
                post["already_attempted"] = int(post["id"].split("/")[1]) <= watermark
        results.append({"channel": channel, "coverage": "unavailable" if error else "recent_page_only",
                        "error": error, "posts": new_posts[:per_channel], "omitted_posts": max(0, len(new_posts) - per_channel),
                        "first_omitted_id": new_posts[per_channel]["id"] if len(new_posts) > per_channel else "",
                        "possible_gap": bool(lost_now or (watermark and oldest > watermark)),
                        "lost_unattempted_now": lost_now,
                        "historical_lost_unattempted": gap[0] if gap else 0,
                        "last_attempted_id": watermark})
    result = {"ok": any(x["coverage"] == "recent_page_only" for x in results),
              "interests": interests[0] if interests else "", "channels": results,
              "recent_attempts": attempts, "archive_complete": False,
              "source_text_is_untrusted": True,
              "workflow": "Agent judges relevance, verifies important claims, records fully inspected IDs as ATTEMPT, then answers in task chat. For output_deferred call fetch_posts(channel=name). If no verified news, say so with coverage gaps. Never infer delivery.",
              "output_limited": False}
    # Bound the WHOLE return, not just post bodies. Keep oldest unattempted IDs
    # first. If even one post per channel will not fit, defer whole channels
    # explicitly; the agent can fetch each by name without losing text.
    while len(json.dumps(result, ensure_ascii=False)) > MAX_OUTPUT_CHARS:
        largest = max(results, key=lambda item: len(json.dumps(item["posts"], ensure_ascii=False)))
        if not largest["posts"]:
            break
        if len(largest["posts"]) > 1:
            removed = largest["posts"].pop()  # newest remains for the next fetch
            largest["omitted_posts"] += 1
            largest["first_omitted_id"] = removed["id"]
            continue
        removed = largest["posts"].pop()
        largest["omitted_posts"] += 1
        largest["first_omitted_id"] = removed["id"]
        largest["coverage"] = "output_deferred"
        largest["error"] = "call fetch_posts(channel=...) for this source"
    if len(json.dumps(result, ensure_ascii=False)) > MAX_OUTPUT_CHARS:
        return json.dumps({"ok": False, "error": "output budget cannot represent all configured sources",
                           "channel_count": len(results), "coverage": "unavailable"})
    result["ok"] = any(x["coverage"] == "recent_page_only" for x in results)
    result["output_limited"] = any(x["omitted_posts"] or any(
        post.get("text_truncated") for post in x["posts"]
    ) for x in results)
    # Only returned IDs are recordable. A guessed future ID must not poison the
    # durable watermark. This observation is NOT a delivery or cursor advance.
    with _connect() as conn:
        for item in results:
            for post in item["posts"]:
                conn.execute("INSERT OR IGNORE INTO observed(channel,id) VALUES (?,?)",
                             (item["channel"], int(post["id"].split("/")[1])))
            conn.execute("DELETE FROM observed WHERE channel=? AND id NOT IN "
                         "(SELECT id FROM observed WHERE channel=? ORDER BY id DESC LIMIT 240)",
                         (item["channel"], item["channel"]))
    return json.dumps(result, ensure_ascii=False)


def _record_attempt(day: str, post_ids: list[str], edition: str = "daily") -> str:
    try:
        normalized_day = date.fromisoformat(day)
        if normalized_day.isoformat() != day:
            raise ValueError("date must be YYYY-MM-DD")
        if not isinstance(edition, str) or not EDITION.fullmatch(edition):
            raise ValueError("edition must be 1–80 ASCII letters/digits/_.:-; default daily")
        if not isinstance(post_ids, list) or len(post_ids) > MAX_CHANNELS * MAX_POSTS_PER_CHANNEL or any(not isinstance(x, str) or not POST_ID.fullmatch(x) for x in post_ids):
            raise ValueError("post_ids must be at most 240 channel/id strings")
        key = f"{day}:{edition}"
        unique = sorted(set(x.lower() for x in post_ids))
        digest = hashlib.sha256(json.dumps(unique, separators=(",", ":")).encode()).hexdigest()
        with _connect() as conn:
            chosen: dict[str, set[int]] = {}
            for post_id in unique:
                channel, numeric_id = post_id.split("/")
                chosen.setdefault(channel, set()).add(int(numeric_id))
                if not conn.execute("SELECT 1 FROM channels WHERE name=?", (channel,)).fetchone() or not conn.execute(
                    "SELECT 1 FROM observed WHERE channel=? AND id=?", (channel, int(numeric_id))
                ).fetchone():
                    raise ValueError("post_ids must come from a configured channel's returned fetch_posts result")
            for channel, ids in chosen.items():
                watermark = conn.execute("SELECT last_id FROM seen WHERE channel=?", (channel,)).fetchone()
                prior = watermark[0] if watermark else 0
                unaccounted = conn.execute(
                    "SELECT id FROM observed WHERE channel=? AND id>? AND id<=?",
                    (channel, prior, max(ids)),
                ).fetchall()
                if any(row[0] not in ids for row in unaccounted):
                    raise ValueError("post_ids must include every earlier returned post before advancing a channel")
            inserted = conn.execute("INSERT OR IGNORE INTO attempts(key,day,post_ids,content_sha256) VALUES (?,?,?,?)",
                                    (key, day, json.dumps(unique), digest)).rowcount
            if inserted:
                for post_id in unique:
                    channel, numeric_id = post_id.split("/")
                    conn.execute("INSERT INTO seen(channel,last_id) VALUES (?,?) ON CONFLICT(channel) "
                                 "DO UPDATE SET last_id=MAX(last_id,excluded.last_id)", (channel, int(numeric_id)))
            row = conn.execute("SELECT key,day,post_ids,content_sha256,recorded_at FROM attempts WHERE key=?", (key,)).fetchone()
        return json.dumps({"ok": True, "key": row[0], "day": row[1], "post_ids": json.loads(row[2]),
                           "content_sha256": row[3], "recorded_at": row[4],
                           "new_attempt": bool(inserted), "same_selection": row[3] == digest,
                           "status": "attempted_not_delivered" if inserted else "already_attempted_do_not_auto_publish"})
    except (TypeError, ValueError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def register(api: Any) -> None:
    global _STATE_DIR
    _STATE_DIR = Path(api.get_state_dir())
    api.register_tool("interests", _interests,
                      description="Save or read owner free-text interests. Omit text to read. A scheduled agent task must read current state, judge relevance, then answer in its originating chat; use schedule_followup/manage_schedules for owner-selected cadence, never a new daemon.",
                      schema={"type": "object", "properties": {"text": {"type": "string"}}})
    api.register_tool("channels", _channels,
                      description="List, add or remove public Telegram channels for science digest. No private invites or login.",
                      schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "add", "remove"]},
                                                               "name": {"type": "string"}}})
    api.register_tool("fetch_posts", _fetch_posts,
                      description="Fetch bounded recent public posts since last attempt; if output_deferred re-fetch with channel=name before recording. Inspect coverage/gaps/untrusted text. include_attempted=true only for an explicitly requested revision. Agent checks primary sources and writes a cited dated answer in its task chat.",
                      schema={"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20},
                                                               "include_attempted": {"type": "boolean"},
                                                               "channel": {"type": "string"}}},
                      timeout_sec=120)
    api.register_tool("record_attempt", _record_attempt,
                      description="Before final digest reply, record ALL observed post IDs as an ATTEMPT, never delivery. day+edition (default daily) is the idempotency key; choose an edition per extra same-day run. Inspect task/chat outcome before reissuing.",
                      schema={"type": "object", "properties": {"day": {"type": "string"},
                                                               "edition": {"type": "string"},
                                                               "post_ids": {"type": "array", "items": {"type": "string"}}},
                              "required": ["day", "post_ids"]})
