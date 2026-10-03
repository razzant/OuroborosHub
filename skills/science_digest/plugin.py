"""Public Telegram preview and curated official-feed intake; the Ouroboros agent
owns editorial decisions.

There is deliberately no model client, Telegram login, timer, or delivery client.
SQLite holds only owner preferences and attempted edition identities. A fetch is
read-only and never advances a delivery cursor.
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import http.client
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.parsers.expat
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
POST_ID = re.compile(r"([A-Za-z][A-Za-z0-9_]{4,31})/([1-9][0-9]{0,15})\Z")
FEED_ID = re.compile(r"(rss:[a-z][a-z0-9_]{2,31})/([0-9a-f]{20})\Z")
EDITION = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
# Curated official feeds only. Owners pick a key; the skill never fetches a
# user-supplied URL. Each URL was confirmed on the publisher's own site. The
# historical "rss:" prefix names every curated feed, whatever its XML format.
FEEDS = {
    "openai_news": "https://openai.com/news/rss.xml",
    "deepmind": "https://deepmind.google/blog/rss.xml",
    "sglang_releases": "https://github.com/sgl-project/sglang/releases.atom",
}
# A curated feed that changes format is an explicit failure, not a guess.
ATOM_FEEDS = frozenset({"sglang_releases"})
ATOM_NS = "http://www.w3.org/2005/Atom"
# GitHub's release feed always holds the latest ten releases with full notes
# (each ~0.2 MiB); read all of it rather than one release.
FEED_MAX_BYTES = {"sglang_releases": 2 * 1024 * 1024}
MAX_CHANNELS = 12
MAX_INTERESTS = 4000
MAX_RESPONSE_BYTES = 768 * 1024
# Feeds are newest first; a prefix holds months of items, and a shorter read
# keeps slow full-archive downloads inside the request timeout.
MAX_FEED_BYTES = 256 * 1024
MAX_POST_TEXT = 4096
MAX_POSTS_PER_CHANNEL = 20
MAX_OUTPUT_CHARS = 14000
MAX_FEED_ITEMS = 2000
MAX_FEED_STATE = 5000
FEED_LOOKBACK_DAYS = 7
# Publishers reorder items within a day (OpenAI does); a wider disorder means
# an unread feed tail could hold a newer item.
FEED_ORDER_SLACK = timedelta(days=1)
REQUEST_TIMEOUT = 6
# Whole-body wall clock per source, so a slow trickle cannot hold the tool.
READ_DEADLINE = 20
# Sources are fetched concurrently: 12 sequential slow sources could outlast
# the 120 s tool timeout and lose every source's result.
MAX_PARALLEL_SOURCES = 6
READABLE = ("recent_page_only", "feed_window")
_USER_AGENT = "Ouroboros-ScienceDigest/0.3 (public preview; curated feeds)"
_STATE_DIR: Path | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
            "CREATE TABLE IF NOT EXISTS observed (channel TEXT NOT NULL, id INTEGER NOT NULL, "
            "lost_reported INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(channel,id));"
            "CREATE TABLE IF NOT EXISTS telegram_reads (channel TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS gaps (channel TEXT PRIMARY KEY, lost_count INTEGER NOT NULL);"
            # Feed identities are hashes, so feeds keep a per-item attempted set
            # instead of the Telegram numeric watermark.
            "CREATE TABLE IF NOT EXISTS feeds (name TEXT PRIMARY KEY, since TEXT NOT NULL, "
            "read_generation INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS feed_items (source TEXT NOT NULL, key TEXT NOT NULL, published TEXT NOT NULL, "
            "attempted INTEGER NOT NULL DEFAULT 0, returned INTEGER NOT NULL DEFAULT 0, "
            "lost_reported INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source,key));"
        )
        # A previous installed revision has observed(channel,id) without this
        # column. Migrate in a write transaction so simultaneous first reads
        # cannot both attempt ALTER TABLE.
        conn.execute("BEGIN IMMEDIATE")
        if "lost_reported" not in {row[1] for row in conn.execute("PRAGMA table_info(observed)")}:
            conn.execute("ALTER TABLE observed ADD COLUMN lost_reported INTEGER NOT NULL DEFAULT 0")
        conn.commit()
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


def _source_name(value: str) -> str:
    """Normalize a Telegram channel or a curated feed key/exact URL."""
    raw = str(value or "").strip()
    lowered = raw.lower()
    for name, url in FEEDS.items():
        if lowered in (f"rss:{name}", url):
            return f"rss:{name}"
    if lowered.startswith("rss:") or (lowered.startswith("https://") and urllib.parse.urlsplit(lowered).hostname != "t.me"):
        raise ValueError("feeds are limited to the curated official list: " + ", ".join(f"rss:{name}" for name in FEEDS))
    return _channel_name(raw)


def _setup_needed(conn: sqlite3.Connection) -> list[str]:
    """What a fresh, neutral installation still needs from its owner."""
    missing = []
    if not conn.execute("SELECT 1 FROM preferences WHERE key='interests' AND value<>''").fetchone():
        missing.append("interests")
    if not conn.execute("SELECT 1 FROM channels").fetchone():
        missing.append("sources")
    return missing


_ONBOARDING = ("Ask the owner, in their own words, for what is missing. Do not assume topics, "
               "sources, language, timezone or a schedule, and do not reuse anyone else's setup.")


def _interests(text: str | None = None) -> str:
    blank = text is not None and not str(text).strip()
    if text is not None and not blank:
        cleaned = str(text).strip()
        if len(json.dumps(cleaned, ensure_ascii=False)) > MAX_INTERESTS:
            return json.dumps({"ok": False, "error": "interests must be 1–4000 encoded characters"})
        with _connect() as conn:
            conn.execute("INSERT INTO preferences (key,value) VALUES ('interests',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cleaned,))
    with _connect() as conn:
        row = conn.execute("SELECT value FROM preferences WHERE key='interests'").fetchone()
        missing = _setup_needed(conn)
    result = {"ok": True, "interests": row[0] if row else "", "setup_needed": missing,
              "guidance": "On owner request, schedule_followup(relation=independent) in this chat with the owner's 5-field cron and timezone. One owner destination per installation. Objective must name science_digest and read CURRENT skill state, not paste interests/channels. List/disable old digest schedules before replacement; restore old if creation fails. Record returned IDs as attempt, not delivery. Disable schedule separately on opt-out."}
    if missing:
        result["onboarding"] = _ONBOARDING
    if blank:
        result["note"] = "blank text only reads; interests unchanged"
    return json.dumps(result, ensure_ascii=False)


def _channels(action: str = "list", name: str = "") -> str:
    if action not in ("add", "remove", "list"):
        return json.dumps({"ok": False, "error": "action must be add, remove or list"})
    try:
        with _connect() as conn:
            if action == "add":
                channel = _source_name(name)
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
                if count >= MAX_CHANNELS and not conn.execute("SELECT 1 FROM channels WHERE name=?", (channel,)).fetchone():
                    raise ValueError(f"source limit reached ({MAX_CHANNELS} channels and feeds)")
                added = conn.execute("INSERT OR IGNORE INTO channels(name) VALUES (?)", (channel,)).rowcount
                if added and channel.startswith("rss:"):
                    # A new subscription reads a short recent window, not the
                    # publisher's whole archive; the window never moves back.
                    since = (_utcnow() - timedelta(days=FEED_LOOKBACK_DAYS)).isoformat(timespec="seconds")
                    conn.execute("INSERT INTO feeds(name,since) VALUES (?,?) ON CONFLICT(name) "
                                 "DO UPDATE SET since=MAX(since,excluded.since)", (channel, since))
            elif action == "remove":
                stored = str(name or "").strip().lower()
                # A feed later dropped from the curated list must stay removable.
                known = stored.startswith("rss:") and conn.execute("SELECT 1 FROM channels WHERE name=?", (stored,)).fetchone()
                channel = stored if known else _source_name(name)
                conn.execute("DELETE FROM channels WHERE name=?", (channel,))
                if channel.startswith("rss:"):
                    # The window and attempted identities stay so a later
                    # re-add cannot replay what was already attempted.
                    conn.execute("DELETE FROM feed_items WHERE source=? AND attempted=0", (channel,))
            rows = conn.execute("SELECT name FROM channels ORDER BY name").fetchall()
            missing = _setup_needed(conn)
        result = {"ok": True, "channels": [row[0] for row in rows],
                  "curated_feeds": {f"rss:{name}": url for name, url in FEEDS.items()},
                  "setup_needed": missing}
        if missing:
            result["onboarding"] = _ONBOARDING
        return json.dumps(result, ensure_ascii=False)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(request.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect())


def _http_failure(exc: urllib.error.HTTPError) -> str:
    """Name the status (rate limit, block, refused redirect) without its body."""
    try:
        exc.close()
    except Exception:
        pass
    code = exc.code if isinstance(exc.code, int) else 0
    return f"HTTP {code}" + (" redirect refused" if 300 <= code < 400 else "")


class _BodyError(Exception):
    """A response body that cannot be decoded as it declares."""


def _read_bounded(response: Any, limit: int) -> tuple[bytes, bool]:
    """Read at most limit+1 decoded bytes. False means READ_DEADLINE cut the
    body short. A CDN may gzip a response nobody asked to compress (the
    DeepMind feed does, intermittently); inflate it under the same bound."""
    encoding = str(response.headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding not in ("identity", "gzip", "x-gzip"):
        raise _BodyError(f"unsupported content encoding {encoding[:20]!r}")
    inflate = None if encoding == "identity" else zlib.decompressobj(16 + zlib.MAX_WBITS)
    deadline = time.monotonic() + READ_DEADLINE
    # read1 returns after one socket read, so the deadline is checked between
    # reads instead of after a long blocking read(n).
    read = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        if time.monotonic() >= deadline:
            return b"".join(chunks), False
        data = read(min(64 * 1024, limit + 1 - size))
        if not data:
            if inflate is not None and not inflate.eof:
                raise _BodyError("truncated gzip body")
            break
        if inflate is not None:
            try:
                # max_length bounds the output, so a gzip bomb cannot expand.
                data = inflate.decompress(data, limit + 1 - size)
            except zlib.error:
                raise _BodyError("corrupt gzip body") from None
        chunks.append(data)
        size += len(data)
    return b"".join(chunks), True


def _http_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        valid = False
    return value if valid and len(value) <= 300 else ""


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
        self.reply_depth = 0
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
            # A reply header quotes ANOTHER post; never read it as this post's text.
            if tag == "a" and not self.reply_depth and "tgme_widget_message_reply" in classes:
                self.reply_depth = self.depth + 1
            if tag == "div" and "tgme_widget_message_text" in classes and not self.reply_depth:
                self.post["content_recognized"] = True
                self.text_depth = self.depth + 1
            if tag == "time" and self.post.get("date") == "":
                self.post["date"] = values.get("datetime") or ""
                self.time_depth = self.depth + 1
            if tag == "a" and self.text_depth and not self.skip_depth:
                href = _http_url(html.unescape(values.get("href") or ""))
                if href:
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
            if self.reply_depth == self.depth and tag == "a":
                self.reply_depth = 0
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
                self.reply_depth = 0
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
            raw, complete = _read_bounded(response, MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        return [], f"preview unavailable: {_http_failure(exc)}"
    except _BodyError as exc:
        return [], f"preview unavailable: {exc}"
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        return [], f"preview unavailable: {type(exc).__name__}"
    # A partial page would make every unread newer post look deleted.
    if not complete:
        return [], "preview unavailable: read deadline exceeded"
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
    # Recognized media alone cannot prove absence of an unread caption under
    # changed markup. Refuse this source batch instead of advancing its cursor.
    if any(not post.get("content_recognized") or not post["text"] for post in parser.posts):
        return [], "unrecognized post content: text unavailable or preview structure changed"
    for post in parser.posts:
        post.pop("content_recognized", None)
    # Keep every identity found in the bounded response. A later limit applies
    # to the agent-facing page, never to visibility/loss accounting. Oldest
    # first by ID, whatever the page order; a repeated ID keeps its first copy.
    unique: dict[int, dict[str, Any]] = {}
    for post in parser.posts:
        unique.setdefault(int(post["id"].split("/")[1]), post)
    return [unique[key] for key in sorted(unique)], ""


class _FeedRefused(Exception):
    pass


class _FeedFull(Exception):
    pass


class _Text(HTMLParser):
    """Plain text of an entry summary; markup is dropped, never interpreted."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def _plain_text(value: str) -> str:
    parser = _Text()
    try:
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    except (ValueError, AssertionError):
        value = re.sub(r"<[^>]*>", " ", value)
    return " ".join(value.split())


def _rss_date(value: str) -> str:
    try:
        parsed = email.utils.parsedate_to_datetime(value.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError, OverflowError, AttributeError):
        return ""


def _atom_date(value: str) -> str:
    """RFC 3339 → the same UTC form as _rss_date. Python 3.10 rejects a bare Z."""
    raw = value.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return ""


def _shift(stamp: str, delta: timedelta) -> str:
    try:
        return (datetime.fromisoformat(stamp) + delta).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return stamp


def _date_ordered(items: list[dict[str, Any]]) -> bool:
    """True when dated items run newest first, allowing same-day reordering."""
    floor = ""
    for item in items:
        if floor and item["date"] > _shift(floor, FEED_ORDER_SLACK):
            return False
        floor = min(floor, item["date"]) if floor else item["date"]
    return True


class _Feed:
    """Collect RSS 2.0 items or Atom entries with expat. DTDs and entity
    declarations are refused, never expanded; only item fields are kept."""

    FIELDS = ("title", "link", "guid", "pubDate", "description")
    ATOM_FIELDS = ("id", "title", "updated", "published", "summary", "content")

    def __init__(self, atom: bool = False) -> None:
        self.atom = atom
        self.item_path = ["feed", "entry"] if atom else ["rss", "channel", "item"]
        self.path: list[str] = []
        self.fields: dict[str, str] | None = None
        self.types: dict[str, str] = {}
        self.clipped = False
        self.parsed = 0
        self.items: list[dict[str, Any]] = []
        self.keys: dict[str, int] = {}
        self.duplicate = 0
        self.unidentified = 0
        self.parser = xml.parsers.expat.ParserCreate()
        self.parser.buffer_text = True
        self.parser.StartDoctypeDeclHandler = self._refuse
        self.parser.EntityDeclHandler = self._refuse
        self.parser.StartElementHandler = self._start
        self.parser.EndElementHandler = self._end
        self.parser.CharacterDataHandler = self._data

    def _refuse(self, *_args: Any) -> None:
        raise _FeedRefused("feed declares a DTD or entity; refused")

    def _start(self, tag: str, attrs: dict[str, str]) -> None:
        if not self.path:
            # Namespace processing stays off, as for RSS: Atom must be the
            # unprefixed default-namespace document the curated publisher serves.
            if self.atom and (tag != "feed" or attrs.get("xmlns") != ATOM_NS):
                raise _FeedRefused("not an Atom feed")
            if not self.atom and tag != "rss":
                raise _FeedRefused("not an RSS 2.0 feed")
        self.path.append(tag)
        if self.path == self.item_path:
            if self.parsed >= MAX_FEED_ITEMS:
                raise _FeedFull()
            self.fields, self.clipped, self.types = {}, False, {}
        elif self.atom and self.fields is not None and len(self.path) == 3:
            if tag == "link":
                # The entry's page is its first rel=alternate (default) link.
                if "link" not in self.fields and attrs.get("rel", "alternate") == "alternate":
                    self.fields["link"] = attrs.get("href") or ""
            elif tag in self.ATOM_FIELDS:
                self.types[tag] = attrs.get("type") or "text"

    def _data(self, data: str) -> None:
        if self.fields is None:
            return
        if self.atom:
            # Atom text constructs may hold nested XHTML; keep all their text.
            field = self.path[2] if len(self.path) >= 3 and self.path[2] in self.ATOM_FIELDS else ""
        else:
            field = self.path[3] if len(self.path) == 4 and self.path[3] in self.FIELDS else ""
        if field:
            value = self.fields.get(field, "")
            if len(value) > 2 * MAX_POST_TEXT:
                self.clipped = True
            else:
                self.fields[field] = value + data

    def _end(self, _tag: str) -> None:
        if self.fields is not None and self.path == self.item_path:
            self._item(self.fields)
            self.fields = None
            self.parsed += 1
        self.path.pop()

    def _atom_text(self, fields: dict[str, str], name: str) -> str:
        value = fields.get(name, "")
        escaped_html = self.types.get(name) in ("html", "text/html")
        return _plain_text(value) if escaped_html else " ".join(value.split())

    def _item(self, fields: dict[str, str]) -> None:
        if self.atom:
            guid = fields.get("id", "").strip()
            stamp = _atom_date(fields.get("published", "")) or _atom_date(fields.get("updated", ""))
            title = self._atom_text(fields, "title")
            text = self._atom_text(fields, "summary") or self._atom_text(fields, "content")
        else:
            guid = fields.get("guid", "").strip()
            stamp = _rss_date(fields.get("pubDate", ""))
            title = " ".join(fields.get("title", "").split())
            text = _plain_text(fields.get("description", ""))
        link = fields.get("link", "").strip()
        identity = guid or link
        if not identity or len(identity) > 2048:
            self.unidentified += 1
            return
        # Publisher identity, not position or date: reorders, retitles and
        # re-dated entries keep the same key.
        key = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:20]
        item = {"key": key, "url": _http_url(link) or _http_url(guid),
                "date": stamp,
                "title": title[:300],
                "text": text[:MAX_POST_TEXT],
                "text_truncated": self.clipped or len(text) > MAX_POST_TEXT}
        if key in self.keys:
            self.duplicate += 1
            position = self.keys[key]
            # A stale/undated duplicate must not hide a fresher entry with
            # the same publisher identity. Keep one record, newest dated copy.
            if item["date"] > self.items[position]["date"]:
                self.items[position] = item
        else:
            self.keys[key] = len(self.items)
            self.items.append(item)


def _fetch_feed(name: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    url = FEEDS[name]
    atom = name in ATOM_FEEDS
    limit = FEED_MAX_BYTES.get(name, MAX_FEED_BYTES)
    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": ("application/atom+xml" if atom else "application/rss+xml")
                  + ", application/xml;q=0.9, text/xml;q=0.8"})
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            if response.geturl() != url:
                return [], {}, "unexpected URL in response"
            if "xml" not in response.headers.get("Content-Type", "").lower():
                return [], {}, "response is not XML"
            raw, complete = _read_bounded(response, limit)
    except urllib.error.HTTPError as exc:
        return [], {}, f"feed unavailable: {_http_failure(exc)}"
    except _BodyError as exc:
        return [], {}, f"feed unavailable: {exc}"
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        return [], {}, f"feed unavailable: {type(exc).__name__}"
    # Official feeds may carry their whole archive, newest first. Past the
    # byte bound or read deadline we keep the complete items of the prefix
    # and say so; truncation never counts an unread item as lost.
    truncated = len(raw) > limit or not complete
    feed = _Feed(atom)
    try:
        feed.parser.Parse(raw[:limit], not truncated)
    except _FeedFull:
        truncated = True
    except _FeedRefused as exc:
        return [], {}, str(exc)
    except (xml.parsers.expat.ExpatError, LookupError, ValueError):
        return [], {}, "feed XML could not be parsed"
    facts = {"feed_truncated": truncated, "duplicate_items": feed.duplicate, "unidentified_items": feed.unidentified}
    if not feed.items:
        return [], facts, "no readable feed items" + (" within the byte limit" if truncated else "")
    return feed.items, facts, ""


def _telegram_source(channel: str, per_channel: int, include_attempted: bool, watermark: int) -> dict[str, Any]:
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO telegram_reads(channel) VALUES (?)", (channel,))
        conn.execute("UPDATE telegram_reads SET generation=generation+1 WHERE channel=?", (channel,))
        generation = conn.execute("SELECT generation FROM telegram_reads WHERE channel=?", (channel,)).fetchone()[0]
    posts, error = _fetch_channel(channel)
    oldest = min((int(post["id"].split("/")[1]) for post in posts), default=0)
    lost_now = 0
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute("SELECT generation FROM telegram_reads WHERE channel=?", (channel,)).fetchone()[0]
        current = conn.execute("SELECT last_id FROM seen WHERE channel=?", (channel,)).fetchone()
        watermark = current[0] if current else 0
        if posts and not error and generation == latest:
            visible_ids = {int(post["id"].split("/")[1]) for post in posts}
            conn.executemany("UPDATE observed SET lost_reported=0 WHERE channel=? AND id=?",
                             ((channel, identifier) for identifier in visible_ids))
            missing = [row[0] for row in conn.execute(
                "SELECT id FROM observed WHERE channel=? AND id>? AND lost_reported=0", (channel, watermark)
            ) if row[0] not in visible_ids]
            lost_now = len(missing)
            if lost_now:
                # An absent item is a coverage gap, not revoked authority to
                # acknowledge an ID the agent actually saw on a prior fetch.
                conn.executemany("UPDATE observed SET lost_reported=1 WHERE channel=? AND id=?",
                                 ((channel, identifier) for identifier in missing))
                conn.execute("INSERT INTO gaps(channel,lost_count) VALUES (?,?) ON CONFLICT(channel) "
                             "DO UPDATE SET lost_count=lost_count+excluded.lost_count", (channel, lost_now))
        gap = conn.execute("SELECT lost_count FROM gaps WHERE channel=?", (channel,)).fetchone()
    # A superseded network read must not insert older IDs into the current
    # acknowledgement ordering after a newer fetch has already returned.
    if generation != latest:
        return {"channel": channel, "kind": "telegram", "coverage": "unavailable",
                "error": "stale preview; refetch this channel", "posts": [], "omitted_posts": 0,
                "first_omitted_id": "", "possible_gap": True,
                "lost_unattempted_now": 0, "historical_lost_unattempted": gap[0] if gap else 0,
                "last_attempted_id": watermark, "stale_read": True, "_generation": generation}
    new_posts = [post for post in posts if include_attempted or int(post["id"].split("/")[1]) > watermark]
    if include_attempted:
        for post in new_posts:
            post["already_attempted"] = int(post["id"].split("/")[1]) <= watermark
    return {"channel": channel, "kind": "telegram", "coverage": "unavailable" if error else "recent_page_only",
            "error": error, "posts": new_posts[:per_channel], "omitted_posts": max(0, len(new_posts) - per_channel),
            "first_omitted_id": new_posts[per_channel]["id"] if len(new_posts) > per_channel else "",
            # The page continues the attempted run only when its oldest post is
            # the very next ID; a contiguous next page is not a gap.
            "possible_gap": bool(generation != latest or lost_now or (watermark and oldest > watermark + 1)),
            "lost_unattempted_now": lost_now,
            "historical_lost_unattempted": gap[0] if gap else 0,
            "last_attempted_id": watermark, "stale_read": generation != latest,
            "_generation": generation}


def _feed_source(source: str, per_channel: int, include_attempted: bool) -> dict[str, Any]:
    name = source.split(":", 1)[1]
    with _connect() as conn:
        row = conn.execute("SELECT since FROM feeds WHERE name=?", (source,)).fetchone()
        since = row[0] if row else (_utcnow() - timedelta(days=FEED_LOOKBACK_DAYS)).isoformat(timespec="seconds")
        if not row:
            conn.execute("INSERT OR IGNORE INTO feeds(name,since) VALUES (?,?)", (source, since))
        # Claim a per-source read order BEFORE network I/O. An older request
        # completing after a newer one may return its own items, but its stale
        # absence can never delete the newer request's pending identities.
        conn.execute("UPDATE feeds SET read_generation=read_generation+1 WHERE name=?", (source,))
        generation = conn.execute("SELECT read_generation FROM feeds WHERE name=?", (source,)).fetchone()[0]
    items, facts, error = _fetch_feed(name) if name in FEEDS else ([], {}, "feed is no longer curated; remove it")
    lost_now = 0
    with _connect() as conn:
        # Serialize the generation recheck with loss cleanup. Without this
        # write lease a newer read could start between the SELECT and DELETE.
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute("SELECT read_generation FROM feeds WHERE name=?", (source,)).fetchone()[0]
        attempted = {key: published for key, published in conn.execute(
            "SELECT key,published FROM feed_items WHERE source=? AND attempted=1", (source,))}
        unattempted = dict(conn.execute(
            "SELECT key,lost_reported FROM feed_items WHERE source=? AND attempted=0", (source,)).fetchall())
        pending = set(unattempted)
        dated = [item for item in items if item["date"]]
        if items and not dated:
            error = "feed items lack parseable publication dates"
        # An existing unattempted item remains eligible when its publisher
        # corrects its date to before the subscription's initial window.
        window = [item for item in dated if item["date"] >= since or item["key"] in pending]
        unseen = {item["key"] for item in window if item["key"] not in attempted}
        present = {item["key"] for item in items}
        undated_pending = bool(pending & (present - {item["key"] for item in dated}))
        # Reappearance ends one absence episode; a later disappearance can be
        # counted again. Retain the row so an ID actually returned to the agent
        # remains acknowledgeable even if another fetch completes meanwhile.
        if generation == latest and not error:
            conn.executemany("UPDATE feed_items SET lost_reported=0 WHERE source=? AND key=?",
                             ((source, key) for key in present))
        if generation == latest and not error and not facts.get("feed_truncated"):
            # Only a complete readable feed proves an observed unattempted
            # item disappeared. A byte/item-limited prefix may simply omit it.
            missing = [row[0] for row in conn.execute(
                "SELECT key FROM feed_items WHERE source=? AND attempted=0 AND lost_reported=0",
                (source,)) if row[0] not in present]
            lost_now = len(missing)
            if lost_now:
                conn.executemany("UPDATE feed_items SET lost_reported=1 WHERE source=? AND key=?",
                                 ((source, key) for key in missing))
                conn.execute("INSERT INTO gaps(channel,lost_count) VALUES (?,?) ON CONFLICT(channel) "
                             "DO UPDATE SET lost_count=lost_count+excluded.lost_count", (source, lost_now))
        if not error:
            # Include eligible IDs omitted by the response limit. They are
            # tracked for gap reporting but are NOT recordable until returned.
            conn.executemany("INSERT OR IGNORE INTO feed_items(source,key,published) VALUES (?,?,?)",
                             ((source, item["key"], item["date"]) for item in window if item["key"] in unseen))
        gap = conn.execute("SELECT lost_count FROM gaps WHERE channel=?", (source,)).fetchone()
    attempted_through = max(attempted.values(), default="")
    oldest = min((item["date"] for item in dated), default="")
    truncated = bool(facts.get("feed_truncated"))
    reference = max(since, attempted_through)
    truncation_gap = False
    if truncated:
        # A date-ordered prefix that reaches past the covered window cannot
        # hide an unread in-window item; same-day reordering may, so the
        # prefix must reach one slack further. Otherwise a long publisher
        # archive (always cut at the byte bound) would flag every read.
        reference = _shift(reference, -FEED_ORDER_SLACK)
        hidden_pending = any(not unattempted[key] for key in pending - present)
        truncation_gap = hidden_pending or not _date_ordered(dated)
    # No numeric watermark: every unattempted identity inside the subscription
    # window stays eligible, oldest first, whatever order the feed uses.
    pool = sorted((item for item in window if include_attempted or item["key"] in unseen),
                  key=lambda item: (item["date"], item["key"]))
    if include_attempted:
        pool = pool[-MAX_POSTS_PER_CHANNEL:]
    posts = []
    for item in [] if error else pool:
        post = {"id": f"{source}/{item['key']}", "url": item["url"], "date": item["date"], "title": item["title"],
                "text": item["text"], "text_truncated": item["text_truncated"]}
        if include_attempted:
            post["already_attempted"] = item["key"] in attempted
        posts.append(post)
    return {"channel": source, "kind": "rss", "coverage": "unavailable" if error else "feed_window",
            "error": error, "posts": posts[:per_channel], "omitted_posts": max(0, len(posts) - per_channel),
            "first_omitted_id": posts[per_channel]["id"] if len(posts) > per_channel else "",
            "possible_gap": bool(not error and (generation != latest or truncation_gap or
                                                lost_now or undated_pending or
                                                (oldest and oldest > reference))),
            "lost_unattempted_now": lost_now,
            "historical_lost_unattempted": gap[0] if gap else 0,
            "window_since": since, "attempted_through": attempted_through,
            "feed_truncated": truncated, "stale_read": generation != latest,
            "skipped_items": {"duplicate": facts.get("duplicate_items", 0), "undated": len(items) - len(dated),
                              "unidentified": facts.get("unidentified_items", 0)}}


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
        missing = _setup_needed(conn)
    if not channels:
        return json.dumps({"ok": False, "error": "no sources configured; ask owner for public channel usernames or curated feeds",
                           "setup_needed": missing, "onboarding": _ONBOARDING}, ensure_ascii=False)
    if channel:
        try:
            selected = _source_name(channel)
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

    def read(name: str) -> dict[str, Any]:
        if name.startswith("rss:"):
            return _feed_source(name, per_channel, bool(include_attempted))
        return _telegram_source(name, per_channel, include_attempted, seen.get(name, 0))

    if len(channels) == 1:
        results = [read(channels[0])]
    else:
        # Network waits overlap. Each source keeps its own read generation and
        # transactions; results keep the configured order.
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_SOURCES, len(channels)),
                                thread_name_prefix="science-digest") as pool:
            results = list(pool.map(read, channels))
    result = {"ok": any(x["coverage"] in READABLE for x in results),
              "interests": interests[0] if interests else "", "channels": results,
              "recent_attempts": attempts, "archive_complete": False,
              "source_text_is_untrusted": True,
              "workflow": "Agent judges relevance, verifies important claims, records fully inspected IDs as ATTEMPT, then answers in task chat. For output_deferred call fetch_posts(channel=name). If no verified news, say so with coverage gaps. Never infer delivery.",
              "output_limited": False}
    if missing:
        # Sources exist but no interests: ask instead of inventing a profile.
        result["setup_needed"] = missing
        result["onboarding"] = _ONBOARDING
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
    result["ok"] = any(x["coverage"] in READABLE for x in results)
    result["output_limited"] = any(x["omitted_posts"] or any(
        post.get("text_truncated") for post in x["posts"]
    ) for x in results)
    # Final response and returned-ID eligibility are one write transaction.
    # A newer fetch can start after _telegram_source checked its generation;
    # its write cannot interleave with this recheck and publication.
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in results:
            if item["kind"] != "telegram":
                continue
            generation = item.pop("_generation")
            current = conn.execute("SELECT generation FROM telegram_reads WHERE channel=?",
                                   (item["channel"],)).fetchone()
            if not current or current[0] != generation:
                item.update(coverage="unavailable", error="stale preview; refetch this channel",
                            posts=[], omitted_posts=0, first_omitted_id="", possible_gap=True,
                            lost_unattempted_now=0, stale_read=True)
        result["ok"] = any(x["coverage"] in READABLE for x in results)
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized) > MAX_OUTPUT_CHARS:
            return json.dumps({"ok": False, "error": "output budget cannot represent all configured sources",
                               "channel_count": len(results), "coverage": "unavailable"})
        # Only IDs in this exact returned JSON become eligible to record.
        for item in results:
            if item["kind"] == "rss":
                conn.executemany("UPDATE feed_items SET returned=1 WHERE source=? AND key=?",
                                 ((item["channel"], post["id"].rsplit("/", 1)[1]) for post in item["posts"]))
                continue
            for post in item["posts"]:
                conn.execute("INSERT OR IGNORE INTO observed(channel,id) VALUES (?,?)",
                             (item["channel"], int(post["id"].split("/")[1])))
            conn.execute("DELETE FROM observed WHERE channel=? AND id NOT IN "
                         "(SELECT id FROM observed WHERE channel=? ORDER BY id DESC LIMIT 240)",
                         (item["channel"], item["channel"]))
    return serialized


def _bound_feed_state(conn: sqlite3.Connection, source: str) -> None:
    """Drop attempted identities that fell below the eligibility window. Past
    the row cap the window moves forward with them, keeping state bounded
    without re-offering what was already attempted."""
    row = conn.execute("SELECT since FROM feeds WHERE name=?", (source,)).fetchone()
    if not row:
        return
    since = row[0]
    cutoff = conn.execute("SELECT published FROM feed_items WHERE source=? AND attempted=1 "
                          "ORDER BY published DESC LIMIT 1 OFFSET ?", (source, MAX_FEED_STATE - 1)).fetchone()
    if cutoff and cutoff[0] > since:
        since = cutoff[0]
        conn.execute("UPDATE feeds SET since=? WHERE name=?", (since, source))
    conn.execute("DELETE FROM feed_items WHERE source=? AND attempted=1 AND published<?", (source, since))


def _record_attempt(day: str, post_ids: list[str], edition: str = "daily") -> str:
    try:
        normalized_day = date.fromisoformat(day)
        if normalized_day.isoformat() != day:
            raise ValueError("date must be YYYY-MM-DD")
        if edition == "":
            edition = "daily"
        if not isinstance(edition, str) or not EDITION.fullmatch(edition):
            raise ValueError("edition must be 1–80 ASCII letters/digits/_.:-; default daily")
        if not isinstance(post_ids, list) or len(post_ids) > MAX_CHANNELS * MAX_POSTS_PER_CHANNEL or any(
                not isinstance(x, str) or not (POST_ID.fullmatch(x) or FEED_ID.fullmatch(x.lower())) for x in post_ids):
            raise ValueError("post_ids must be at most 240 channel/id or rss:feed/key strings")
        key = f"{day}:{edition}"
        unique = sorted(set(x.lower() for x in post_ids))
        digest = hashlib.sha256(json.dumps(unique, separators=(",", ":")).encode()).hexdigest()
        with _connect() as conn:
            # Validate and publish the receipt under one write lease. A fetch
            # cannot change marker state between checking IDs and the attempt.
            conn.execute("BEGIN IMMEDIATE")
            prior_receipt = conn.execute(
                "SELECT key,day,post_ids,content_sha256,recorded_at FROM attempts WHERE key=?", (key,)
            ).fetchone()
            if prior_receipt:
                return json.dumps({"ok": True, "key": prior_receipt[0], "day": prior_receipt[1],
                                   "post_ids": json.loads(prior_receipt[2]), "content_sha256": prior_receipt[3],
                                   "recorded_at": prior_receipt[4], "new_attempt": False,
                                   "same_selection": prior_receipt[3] == digest,
                                   "status": "already_attempted_do_not_auto_publish"})
            chosen: dict[str, set[int]] = {}
            feed_keys: list[tuple[str, str]] = []
            for post_id in unique:
                feed = FEED_ID.fullmatch(post_id)
                channel, identifier = feed.groups() if feed else post_id.split("/")
                observed = conn.execute(
                    "SELECT 1 FROM feed_items WHERE source=? AND key=? AND returned=1" if feed else
                    "SELECT 1 FROM observed WHERE channel=? AND id=?", (channel, identifier if feed else int(identifier)))
                if not conn.execute("SELECT 1 FROM channels WHERE name=?", (channel,)).fetchone() or not observed.fetchone():
                    raise ValueError("post_ids must come from a configured channel's returned fetch_posts result")
                if feed:
                    # Feed items are independent identities: recording one never
                    # implies anything about another, so no ordering rule applies.
                    feed_keys.append((channel, identifier))
                else:
                    chosen.setdefault(channel, set()).add(int(identifier))
            for channel, ids in chosen.items():
                watermark = conn.execute("SELECT last_id FROM seen WHERE channel=?", (channel,)).fetchone()
                prior = watermark[0] if watermark else 0
                unaccounted = conn.execute(
                    "SELECT id FROM observed WHERE channel=? AND id>? AND id<=? AND lost_reported=0",
                    (channel, prior, max(ids)),
                ).fetchall()
                if any(row[0] not in ids for row in unaccounted):
                    raise ValueError("post_ids must include every earlier returned post before advancing a channel")
            inserted = conn.execute("INSERT OR IGNORE INTO attempts(key,day,post_ids,content_sha256) VALUES (?,?,?,?)",
                                    (key, day, json.dumps(unique), digest)).rowcount
            if inserted:
                for channel, ids in chosen.items():
                    conn.execute("INSERT INTO seen(channel,last_id) VALUES (?,?) ON CONFLICT(channel) "
                                 "DO UPDATE SET last_id=MAX(last_id,excluded.last_id)", (channel, max(ids)))
                updated = conn.executemany("UPDATE feed_items SET attempted=1 WHERE source=? AND key=?", feed_keys).rowcount
                if updated != len(feed_keys):
                    raise ValueError("feed item changed before attempt could be recorded")
                for source in sorted({source for source, _ in feed_keys}):
                    _bound_feed_state(conn, source)
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
                      description="Save or read the owner's own free-text interests (omit or leave text blank to read). setup_needed lists what to ask the owner; never assume a profile. A scheduled agent task must read current state, judge relevance, then answer in its originating chat; use schedule_followup/manage_schedules for owner-selected cadence, never a new daemon.",
                      schema={"type": "object", "properties": {"text": {"type": "string"}}})
    api.register_tool("channels", _channels,
                      description="List, add or remove digest sources: public Telegram channels (@name or https://t.me/name) or curated official feeds (rss:<key>, see curated_feeds). No private invites, login or arbitrary URLs.",
                      schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "add", "remove"]},
                                                               "name": {"type": "string"}}})
    api.register_tool("fetch_posts", _fetch_posts,
                      description="Fetch bounded unattempted items from every configured channel and feed; if output_deferred re-fetch with channel=name before recording. Inspect per-source coverage/gaps/untrusted text. include_attempted=true only for an explicitly requested revision. Agent checks primary sources and writes a cited dated answer in its task chat.",
                      schema={"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20},
                                                               "include_attempted": {"type": "boolean"},
                                                               "channel": {"type": "string"}}},
                      timeout_sec=120)
    api.register_tool("record_attempt", _record_attempt,
                      description="Before final digest reply, record ALL inspected returned post/feed item IDs as an ATTEMPT, never delivery. day+edition (default daily) is the idempotency key; choose an edition per extra same-day run. Inspect task/chat outcome before reissuing.",
                      schema={"type": "object", "properties": {"day": {"type": "string"},
                                                               "edition": {"type": "string"},
                                                               "post_ids": {"type": "array", "items": {"type": "string"}}},
                              "required": ["day", "post_ids"]})
