"""Shared constants, time helpers and result shaping for the calendar skill.

Everything here is stdlib-only so the companion, the per-call child and the
offline tests share one vocabulary without importing providers.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # zoneinfo is stdlib since 3.9; tzdata may be missing on exotic hosts.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

SKILL_NAME = "calendar"
LOCAL_ACCOUNT_ID = "local"
DEFAULT_LOCAL_CALENDAR_ID = "local:personal"
DEFAULT_LOCAL_CALENDAR_NAME = "Личное"

PROVIDER_LOCAL = "local"
PROVIDER_YANDEX = "yandex"
PROVIDER_GOOGLE = "google"
PROVIDERS = (PROVIDER_LOCAL, PROVIDER_YANDEX, PROVIDER_GOOGLE)

VISIBILITY_SHOWN = "shown"
VISIBILITY_HIDDEN = "hidden"
AVAIL_BUSY = "busy"
AVAIL_FREE = "free"
AVAIL_SOFT = "soft"          # «обычно»: гибкое предпочтение, не бронь (4 A, 34 A)
PUBLISH_FULL = "full"
PUBLISH_BUSY = "busy"        # копия «Занят»: только интервал (11 A)
BUSY_COPY_TITLE = "Занят"

SCOPE_THIS = "this"
SCOPE_FOLLOWING = "following"
SCOPE_ALL = "all"
SCOPES = (SCOPE_THIS, SCOPE_FOLLOWING, SCOPE_ALL)

INTENT_PENDING = "pending"
INTENT_DONE = "done"
INTENT_FAILED = "failed"
INTENT_CONFLICT = "conflict"

MAX_EVENTS = 50
MAX_WINDOW_DAYS = 62
TOOL_RESULT_LIMIT = 15_000       # host cap for tool results (tool_capabilities.py)
DEFAULT_WORKING_HOURS = "09:00-19:00"
WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── time ────────────────────────────────────────────────────────────

def system_tz():
    """The host's local zone: an IANA zone when the OS tells us its name, else the fixed local offset."""
    if ZoneInfo is not None:
        import os
        for candidate in (os.environ.get("TZ") or "", _localtime_zone_name()):
            if candidate:
                try:
                    return ZoneInfo(candidate)
                except (ZoneInfoNotFoundError, ValueError, OSError):
                    continue
    return datetime.now().astimezone().tzinfo or timezone.utc


def _localtime_zone_name() -> str:
    """macOS/Linux keep /etc/localtime as a symlink into the zoneinfo tree."""
    try:
        import os
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    marker = "zoneinfo/"
    idx = target.find(marker)
    return target[idx + len(marker):] if idx >= 0 else ""


def get_tz(name: str = ""):
    """IANA zone by name, or the system zone when the name is empty/unknown."""
    text = str(name or "").strip()
    if text and ZoneInfo is not None:
        try:
            return ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return system_tz()


def tz_name(tz) -> str:
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    now = datetime.now(tz)
    return now.tzname() or str(tz)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Canonical storage form: ISO 8601 in UTC with explicit +00:00."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def iso_local(value: Any, tz) -> str:
    """ISO 8601 with the owner's offset — what tools and the widget return."""
    dt = parse_stored(value)
    if dt is None:
        return ""
    return dt.astimezone(tz).replace(microsecond=0).isoformat()


def parse_stored(value: Any) -> Optional[datetime]:
    """Parse a stored UTC ISO string (or pass an aware datetime through)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_input(value: Any, tz, default: Optional[datetime] = None) -> Tuple[Optional[datetime], bool]:
    """Parse user/agent input.

    Returns ``(aware_datetime, is_date_only)``. A bare ``YYYY-MM-DD`` means an
    all-day date at local midnight; a naive datetime is interpreted in ``tz``;
    an explicit offset is honoured. Unparseable input returns ``(default, False)``.
    """
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=tz)), False
    text = str(value or "").strip()
    if not text:
        return default, False
    if _DATE_RE.match(text):
        try:
            d = date.fromisoformat(text)
        except ValueError:
            return default, False
        return datetime.combine(d, time(0, 0), tzinfo=tz), True
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return default, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt, False


def day_bounds(day: date, tz) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    return start, start + timedelta(days=1)


def week_bounds(day: date, tz) -> Tuple[datetime, datetime]:
    monday = day - timedelta(days=day.weekday())
    start = datetime.combine(monday, time(0, 0), tzinfo=tz)
    return start, start + timedelta(days=7)


def parse_working_hours(text: str) -> Tuple[time, time]:
    raw = str(text or DEFAULT_WORKING_HOURS)
    try:
        a, b = raw.split("-", 1)
        start = time.fromisoformat(a.strip())
        end = time.fromisoformat(b.strip())
        if end > start:
            return start, end
    except ValueError:
        pass
    return time(9, 0), time(19, 0)


# ── identity ────────────────────────────────────────────────────────

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_uid() -> str:
    return f"{uuid.uuid4()}@ouroboros-calendar"


def calendar_id(provider: str, account_login: str, external: str) -> str:
    if provider == PROVIDER_LOCAL:
        return f"local:{external}"
    return f"{provider}:{account_login}:{external}"


def account_id(provider: str, login: str) -> str:
    return LOCAL_ACCOUNT_ID if provider == PROVIDER_LOCAL else f"{provider}:{login}"


# ── results ─────────────────────────────────────────────────────────

def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def bounded_result(obj: Dict[str, Any], limit: int = TOOL_RESULT_LIMIT, list_keys: Iterable[str] = ("events", "slots", "candidates", "items")) -> str:
    """Serialize a tool result under the host cap by trimming lists, never bytes.

    Silent truncation of a JSON string breaks the parser; trimming the longest
    list and disclosing ``truncated`` keeps the answer well-formed and honest.
    """
    text = json_dumps(obj)
    if len(text) <= limit:
        return text
    trimmed = dict(obj)
    for key in list_keys:
        items = trimmed.get(key)
        if not isinstance(items, list) or not items:
            continue
        lo, hi = 0, len(items)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            probe = {**trimmed, key: items[:mid], "truncated": True, "truncated_key": key}
            if len(json_dumps(probe)) <= limit:
                lo = mid
            else:
                hi = mid - 1
        trimmed = {**trimmed, key: items[:lo], "truncated": True, "truncated_key": key, "total_" + key: len(items)}
        text = json_dumps(trimmed)
        if len(text) <= limit:
            return text
    # Last resort: drop bulky free-text fields rather than cut the JSON.
    for key in ("message", "text", "description"):
        if key in trimmed and isinstance(trimmed[key], str):
            trimmed[key] = trimmed[key][:500] + "…"
    text = json_dumps(trimmed)
    if len(text) <= limit:
        return text
    return json_dumps({"status": "error", "message": "result too large even after trimming", "truncated": True})


def event_public(row: Dict[str, Any], tz, compact: bool = True) -> Dict[str, Any]:
    """Public projection of an event row for tools and the widget."""
    out: Dict[str, Any] = {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "start": iso_local(row.get("start_utc"), tz),
        "end": iso_local(row.get("end_utc"), tz),
        "all_day": bool(row.get("all_day")),
        "calendar_id": row.get("calendar_id"),
        "calendar_name": row.get("calendar_name") or "",
        "visibility": row.get("visibility") or VISIBILITY_SHOWN,
        "availability": row.get("availability") or AVAIL_BUSY,
        "status": row.get("status") or "confirmed",
    }
    if row.get("occurrence_start_utc"):
        out["occurrence_start"] = iso_local(row.get("occurrence_start_utc"), tz)
        out["series_id"] = row.get("series_id") or row.get("id")
    if row.get("rrule"):
        out["rrule"] = row.get("rrule")
    if row.get("link_group_id"):
        out["link_group_id"] = row.get("link_group_id")
        out["is_primary"] = bool(row.get("is_primary", 1))
    if row.get("sync_state") and row.get("sync_state") != "synced":
        out["sync_state"] = row.get("sync_state")
    if not compact:
        out.update({
            "description": row.get("description") or "",
            "location": row.get("location") or "",
            "tz": row.get("tz") or "",
            "organizer": row.get("organizer") or "",
            "attendees": _loads(row.get("attendees_json"), []),
            "my_response": row.get("my_response") or "",
            "reminders": _loads(row.get("reminders_json"), []),
            "origin": row.get("origin") or "local",
            "external_id": row.get("external_id") or "",
            "updated_at": row.get("updated_at") or "",
        })
    else:
        loc = row.get("location") or ""
        if loc:
            out["location"] = loc[:120]
    return out


def _loads(text: Any, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def label(ev: Dict[str, Any], tz) -> str:
    s = parse_stored(ev.get("start_utc"))
    e = parse_stored(ev.get("end_utc"))
    if s is None or e is None:
        return str(ev.get("title") or "")
    if ev.get("all_day"):
        when = "весь день"
    else:
        when = f"{s.astimezone(tz):%H:%M}–{e.astimezone(tz):%H:%M}"
    return f"{when} · {ev.get('title') or ''}"
