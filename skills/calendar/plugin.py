"""Календарь Уробороса — локальный календарь + Яндекс Календарь (CalDAV), tools и декларативный виджет.

v0.2: источники `local` (SQLite в state dir) и `yandex` (caldav.yandex.ru, пароль приложения).
Все события живут в одной таблице с полем `source`; виджет и tools работают поверх неё.
Только stdlib: urllib, xml.etree, sqlite3, zoneinfo.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from starlette.responses import JSONResponse

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

LOCAL_CALENDAR = "local:personal"
LOCAL_CALENDAR_NAME = "Личное"
WORK_START = time(9, 0)
WORK_END = time(19, 0)
MAX_EVENTS = 50
MAX_WINDOW_DAYS = 62
SYNC_TTL_SEC = 120
SYNC_PAST_DAYS = 7
SYNC_FUTURE_DAYS = 60
YANDEX_BASE = "https://caldav.yandex.ru"
HTTP_TIMEOUT = 15
USER_AGENT = "Ouroboros-Calendar/0.2"

WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/"}


# ── время ─────────────────────────────────────────────────────────


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _now() -> datetime:
    return datetime.now(_local_tz())


def _parse_dt(value: Any, default: Optional[datetime] = None) -> Optional[datetime]:
    """ISO 8601 → aware datetime в локальной зоне; naive трактуется как местное время."""
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.combine(date.fromisoformat(text), time(0, 0))
            except ValueError:
                return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt.astimezone(_local_tz())


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _day_bounds(day: date):
    tz = _local_tz()
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    return start, start + timedelta(days=1)


def _week_bounds(today: date):
    monday = today - timedelta(days=today.weekday())
    start, _ = _day_bounds(monday)
    return start, start + timedelta(days=7), monday


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── iCalendar ─────────────────────────────────────────────────────


def _ical_unescape(text: str) -> str:
    return (text.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def _ical_escape(text: str) -> str:
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw:
            continue
        if raw[0] in " \t" and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_prop(line: str) -> Tuple[str, Dict[str, str], str]:
    """NAME;PARAM=VAL;PARAM2=VAL2:VALUE → (NAME, {PARAM: VAL}, VALUE)."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return line.upper(), {}, ""
    parts = head.split(";")
    name = parts[0].upper()
    params: Dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, value


def _parse_ical_dt(value: str, params: Dict[str, str]) -> Tuple[Optional[datetime], bool]:
    """Возвращает (aware datetime в локальной зоне, all_day)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
        try:
            d = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        except ValueError:
            return None, True
        return datetime.combine(d, time(0, 0), tzinfo=_local_tz()), True
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)$", value)
    if not m:
        return None, False
    y, mo, d, hh, mm, ss, z = m.groups()
    naive = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0))
    if z:
        dt = naive.replace(tzinfo=timezone.utc)
    else:
        tzid = params.get("TZID")
        tz = None
        if tzid and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tzid)
            except Exception:
                tz = None
        dt = naive.replace(tzinfo=tz or _local_tz())
    return dt.astimezone(_local_tz()), False


def _parse_duration(value: str) -> timedelta:
    m = re.match(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", value.strip())
    if not m:
        return timedelta(hours=1)
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def parse_vevents(ics_text: str) -> List[Dict[str, Any]]:
    """Все VEVENT из VCALENDAR (включая override'ы серии с RECURRENCE-ID)."""
    events: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    depth_alarm = 0
    for line in _unfold(ics_text):
        name, params, value = _split_prop(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {"uid": "", "title": "", "location": "", "description": "", "start": None, "end": None,
                   "all_day": False, "duration": None, "rrule": "", "recurrence_id": "", "remind_min": 0, "status": ""}
            continue
        if name == "BEGIN" and value.upper() == "VALARM":
            depth_alarm += 1
            continue
        if name == "END" and value.upper() == "VALARM":
            depth_alarm = max(0, depth_alarm - 1)
            continue
        if name == "END" and value.upper() == "VEVENT":
            if cur is not None:
                if cur["start"] is not None:
                    if cur["end"] is None:
                        if cur["duration"] is not None:
                            cur["end"] = cur["start"] + cur["duration"]
                        elif cur["all_day"]:
                            cur["end"] = cur["start"] + timedelta(days=1)
                        else:
                            cur["end"] = cur["start"] + timedelta(hours=1)
                    events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        if depth_alarm:
            if name == "TRIGGER" and value.strip().startswith("-P"):
                td = _parse_duration(value)
                cur["remind_min"] = max(cur["remind_min"], int(abs(td.total_seconds()) // 60))
            continue
        if name == "UID":
            cur["uid"] = value.strip()
        elif name == "SUMMARY":
            cur["title"] = _ical_unescape(value)
        elif name == "LOCATION":
            cur["location"] = _ical_unescape(value)
        elif name == "DESCRIPTION":
            cur["description"] = _ical_unescape(value)
        elif name == "DTSTART":
            cur["start"], cur["all_day"] = _parse_ical_dt(value, params)
        elif name == "DTEND":
            cur["end"], _ = _parse_ical_dt(value, params)
        elif name == "DURATION":
            cur["duration"] = _parse_duration(value)
        elif name == "RRULE":
            cur["rrule"] = value.strip()
        elif name == "RECURRENCE-ID":
            rid, _ = _parse_ical_dt(value, params)
            cur["recurrence_id"] = _utc_stamp(rid) if rid else value.strip()
        elif name == "STATUS":
            cur["status"] = value.strip().upper()
    return events


def build_vcalendar(uid: str, title: str, start: datetime, end: datetime, all_day: bool,
                    location: str = "", description: str = "", remind_min: int = 0) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Ouroboros//Calendar 0.2//RU", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{_utc_stamp(_now())}",
    ]
    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{start.date().strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{end.date().strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART:{_utc_stamp(start)}")
        lines.append(f"DTEND:{_utc_stamp(end)}")
    lines.append(f"SUMMARY:{_ical_escape(title)}")
    if location:
        lines.append(f"LOCATION:{_ical_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_ical_escape(description)}")
    if remind_min and remind_min > 0:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ical_escape(title)}",
                  f"TRIGGER:-PT{int(remind_min)}M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def rewrite_vevent_times(ics_text: str, new_start: datetime, new_end: datetime, all_day: bool) -> str:
    """Заменяет DTSTART/DTEND у мастер-VEVENT (без RECURRENCE-ID), остальное не трогает."""
    out: List[str] = []
    in_master = False
    in_event = False
    seen_master = False
    lines = _unfold(ics_text)
    # определяем, какой VEVENT мастер: первый без RECURRENCE-ID
    blocks: List[Tuple[int, int, bool]] = []
    start_idx = None
    has_rid = False
    for i, line in enumerate(lines):
        name, _, value = _split_prop(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            start_idx, has_rid = i, False
        elif name == "RECURRENCE-ID":
            has_rid = True
        elif name == "END" and value.upper() == "VEVENT" and start_idx is not None:
            blocks.append((start_idx, i, has_rid))
            start_idx = None
    master = next((b for b in blocks if not b[2]), blocks[0] if blocks else None)
    if not master:
        return ics_text
    ms, me, _ = master
    for i, line in enumerate(lines):
        name, _, _ = _split_prop(line)
        if ms < i < me and name in ("DTSTART", "DTEND", "DURATION"):
            if name == "DTSTART":
                out.append(f"DTSTART;VALUE=DATE:{new_start.date().strftime('%Y%m%d')}" if all_day else f"DTSTART:{_utc_stamp(new_start)}")
            elif name == "DTEND":
                out.append(f"DTEND;VALUE=DATE:{new_end.date().strftime('%Y%m%d')}" if all_day else f"DTEND:{_utc_stamp(new_end)}")
            continue
        out.append(line)
    # если DTEND не было (был DURATION) — добавим DTEND после DTSTART
    if not any(_split_prop(l)[0] == "DTEND" for l in out[ms:me]):
        for j in range(ms, len(out)):
            if _split_prop(out[j])[0] == "DTSTART":
                out.insert(j + 1, f"DTEND;VALUE=DATE:{new_end.date().strftime('%Y%m%d')}" if all_day else f"DTEND:{_utc_stamp(new_end)}")
                break
    return "\r\n".join(out) + "\r\n"


# ── хранилище ─────────────────────────────────────────────────────


class Store:
    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "calendar.sqlite3")
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    calendar TEXT NOT NULL,
                    title TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    all_day INTEGER NOT NULL DEFAULT 0,
                    location TEXT,
                    description TEXT,
                    remind_min INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                )"""
            )
            cols = {r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()}
            for col, ddl in (
                ("source", "TEXT NOT NULL DEFAULT 'local'"),
                ("calendar_name", "TEXT NOT NULL DEFAULT 'Личное'"),
                ("uid", "TEXT"),
                ("href", "TEXT"),
                ("etag", "TEXT"),
                ("recurring", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_start ON events(start)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS calendars (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
                    href TEXT, writable INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL
                )"""
            )
            c.execute(
                "INSERT OR IGNORE INTO calendars (id, source, name, href, writable, updated) VALUES (?, 'local', ?, NULL, 1, ?)",
                (LOCAL_CALENDAR, LOCAL_CALENDAR_NAME, _iso(_now())),
            )

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    # meta
    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # calendars
    def calendars(self, source: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as c:
            if source:
                rows = c.execute("SELECT * FROM calendars WHERE source = ? ORDER BY id", (source,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM calendars ORDER BY source, id").fetchall()
        return [dict(r) for r in rows]

    def replace_calendars(self, source: str, items: List[Dict[str, Any]]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM calendars WHERE source = ?", (source,))
            for it in items:
                c.execute(
                    "INSERT INTO calendars (id, source, name, href, writable, updated) VALUES (?, ?, ?, ?, ?, ?)",
                    (it["id"], source, it["name"], it.get("href"), 1 if it.get("writable", True) else 0, _iso(_now())),
                )

    # events
    def insert(self, ev: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO events (id, source, calendar, calendar_name, uid, href, etag, recurring, title, start, end, all_day,"
                " location, description, remind_min, created, updated)"
                " VALUES (:id, :source, :calendar, :calendar_name, :uid, :href, :etag, :recurring, :title, :start, :end, :all_day,"
                " :location, :description, :remind_min, :created, :updated)",
                ev,
            )

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def find_by_title(self, needle: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE lower(title) LIKE ? ORDER BY start LIMIT 20",
                (f"%{needle.lower()}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def update(self, event_id: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated"] = _iso(_now())
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        fields["id"] = event_id
        with self._conn() as c:
            c.execute(f"UPDATE events SET {sets} WHERE id = :id", fields)

    def delete(self, event_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return cur.rowcount > 0

    def replace_source_window(self, source: str, start: datetime, end: datetime, rows: List[Dict[str, Any]]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM events WHERE source = ? AND start < ? AND end > ?", (source, _iso(end), _iso(start)))
            for ev in rows:
                c.execute(
                    "INSERT OR REPLACE INTO events (id, source, calendar, calendar_name, uid, href, etag, recurring, title, start, end, all_day,"
                    " location, description, remind_min, created, updated)"
                    " VALUES (:id, :source, :calendar, :calendar_name, :uid, :href, :etag, :recurring, :title, :start, :end, :all_day,"
                    " :location, :description, :remind_min, :created, :updated)",
                    ev,
                )

    def window(self, start: datetime, end: datetime, calendars: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        with self._conn() as c:
            if calendars:
                marks = ",".join("?" for _ in calendars)
                rows = c.execute(
                    f"SELECT * FROM events WHERE start < ? AND end > ? AND calendar IN ({marks}) ORDER BY start",
                    (_iso(end), _iso(start), *calendars),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events WHERE start < ? AND end > ? ORDER BY start",
                    (_iso(end), _iso(start)),
                ).fetchall()
        return [dict(r) for r in rows]


_store: Optional[Store] = None


def _db(api) -> Store:
    global _store
    if _store is None:
        _store = Store(api.get_state_dir())
    return _store


# ── Яндекс CalDAV ─────────────────────────────────────────────────


class CalDavError(Exception):
    def __init__(self, kind: str, message: str, status: int = 0):
        super().__init__(message)
        self.kind, self.message, self.status = kind, message, status


class YandexCalDav:
    """Тонкий CalDAV-клиент под quirks Яндекса: без discovery, principal/home по e-mail."""

    def __init__(self, email: str, app_password: str):
        self.email = email.strip()
        self.password = app_password
        self.home = f"{YANDEX_BASE}/calendars/{urllib.parse.quote(self.email, safe='')}/"
        token = base64.b64encode(f"{self.email}:{self.password}".encode("utf-8")).decode("ascii")
        self._auth = f"Basic {token}"

    def _request(self, method: str, url: str, body: Optional[str] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], str]:
        if not url.startswith(YANDEX_BASE):
            raise CalDavError("network", f"Адрес вне caldav.yandex.ru: {url}")
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", "replace")
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, text
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code == 401:
                raise CalDavError("auth_failed", "Яндекс не принял логин или пароль приложения (401). Если пароль создан недавно — он может активироваться до 2–3 часов.", 401)
            if exc.code == 403:
                raise CalDavError("forbidden", "Яндекс отказал в доступе (403).", 403)
            if exc.code == 404:
                raise CalDavError("not_found", "Ресурс не найден на сервере Яндекса (404).", 404)
            if exc.code == 412:
                raise CalDavError("conflict", "Событие изменилось на сервере (412): перечитай и повтори.", 412)
            if exc.code in (502, 503, 504, 507):
                raise CalDavError("server", f"Сервер Яндекса временно не отвечает ({exc.code}).", exc.code)
            raise CalDavError("http", f"HTTP {exc.code} от Яндекса: {text[:200]}", exc.code)
        except urllib.error.URLError as exc:
            raise CalDavError("network", f"Нет связи с caldav.yandex.ru: {exc.reason}")
        except TimeoutError:
            raise CalDavError("network", "Таймаут при обращении к caldav.yandex.ru")

    def list_calendars(self) -> List[Dict[str, Any]]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:displayname/><d:resourcetype/><d:current-user-privilege-set/></d:prop></d:propfind>'
        )
        status, _, text = self._request("PROPFIND", self.home, body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        out: List[Dict[str, Any]] = []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise CalDavError("parse", "Не удалось разобрать ответ PROPFIND от Яндекса")
        for resp in root.findall("d:response", NS):
            href = (resp.findtext("d:href", default="", namespaces=NS) or "").strip()
            if not href:
                continue
            is_cal = any(p.find("d:resourcetype/c:calendar", NS) is not None for p in resp.findall("d:propstat/d:prop", NS))
            if not is_cal:
                continue
            name_part = href.rstrip("/").rsplit("/", 1)[-1]
            if not name_part.startswith("events-"):
                continue
            display = ""
            for p in resp.findall("d:propstat/d:prop", NS):
                dn = p.findtext("d:displayname", default="", namespaces=NS)
                if dn:
                    display = dn.strip()
            writable = True
            privs = resp.find("d:propstat/d:prop/d:current-user-privilege-set", NS)
            if privs is not None:
                writable = any(pr.find("d:write", NS) is not None or pr.find("d:write-content", NS) is not None for pr in privs.findall("d:privilege", NS))
            full = href if href.startswith("http") else YANDEX_BASE + href
            out.append({"collection": name_part, "name": display or name_part, "href": full, "writable": writable})
        return out

    def fetch_events(self, cal_href: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range start="{_utc_stamp(start)}" end="{_utc_stamp(end)}"/>'
            '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>'
        )
        _, _, text = self._request("REPORT", cal_href, body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        out: List[Dict[str, Any]] = []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise CalDavError("parse", "Не удалось разобрать ответ REPORT от Яндекса")
        for resp in root.findall("d:response", NS):
            href = (resp.findtext("d:href", default="", namespaces=NS) or "").strip()
            etag = ""
            data = ""
            for p in resp.findall("d:propstat/d:prop", NS):
                etag = (p.findtext("d:getetag", default="", namespaces=NS) or etag).strip()
                data = p.findtext("c:calendar-data", default="", namespaces=NS) or data
            if not data:
                continue
            full = href if href.startswith("http") else YANDEX_BASE + href
            for ev in parse_vevents(data):
                ev["href"], ev["etag"] = full, etag
                out.append(ev)
        return out

    def get_ics(self, href: str) -> Tuple[str, str]:
        _, headers, text = self._request("GET", href)
        return text, headers.get("etag", "")

    def put_new(self, cal_href: str, uid: str, ics: str) -> Tuple[str, str]:
        href = cal_href.rstrip("/") + "/" + urllib.parse.quote(uid, safe="") + ".ics"
        _, headers, _ = self._request("PUT", href, ics, {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        return href, headers.get("etag", "")

    def put_update(self, href: str, ics: str, etag: str = "") -> str:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        _, resp_headers, _ = self._request("PUT", href, ics, headers)
        return resp_headers.get("etag", "")

    def delete(self, href: str, etag: str = "") -> None:
        headers = {"If-Match": etag} if etag else {}
        self._request("DELETE", href, None, headers)


def _yandex_client(api) -> Optional[YandexCalDav]:
    try:
        settings = api.get_settings(["YANDEX_CALDAV_USER", "YANDEX_CALDAV_APP_PASSWORD"])
    except Exception:
        return None
    user = str(settings.get("YANDEX_CALDAV_USER") or "").strip()
    pwd = str(settings.get("YANDEX_CALDAV_APP_PASSWORD") or "")
    if not user or not pwd:
        return None
    return YandexCalDav(user, pwd)


def _yandex_cal_id(email: str, collection: str) -> str:
    return f"yandex:{email}:{collection}"


def _sync_yandex(api, force: bool = False) -> Dict[str, Any]:
    """Подтягивает календари и события Яндекса в общую базу. Никогда не бросает."""
    db = _db(api)
    client = _yandex_client(api)
    state: Dict[str, Any] = {"source": "yandex", "state": "not_configured", "last_sync": db.get_meta("yandex.last_sync", ""),
                             "last_error": db.get_meta("yandex.last_error", ""), "calendars": 0, "account": ""}
    if client is None:
        state["next_step"] = ("Создай пароль приложения типа «Календарь» на id.yandex.ru/security/app-passwords и добавь в Settings → Secrets "
                              "ключи YANDEX_CALDAV_USER (login@yandex.ru) и YANDEX_CALDAV_APP_PASSWORD, затем выдай скиллу грант на них.")
        return state
    state["account"] = client.email
    last = db.get_meta("yandex.last_sync", "")
    last_dt = _parse_dt(last) if last else None
    if not force and last_dt and (_now() - last_dt).total_seconds() < SYNC_TTL_SEC and not db.get_meta("yandex.last_error", ""):
        state["state"] = "connected"
        state["calendars"] = len(db.calendars("yandex"))
        return state
    try:
        cals = client.list_calendars()
        items = [{"id": _yandex_cal_id(client.email, c["collection"]), "name": c["name"], "href": c["href"], "writable": c["writable"]} for c in cals]
        db.replace_calendars("yandex", items)
        now = _now()
        w_start = _day_bounds((now - timedelta(days=SYNC_PAST_DAYS)).date())[0]
        w_end = _day_bounds((now + timedelta(days=SYNC_FUTURE_DAYS)).date())[1]
        rows: List[Dict[str, Any]] = []
        for c in cals:
            cal_id = _yandex_cal_id(client.email, c["collection"])
            for ev in client.fetch_events(c["href"], w_start, w_end):
                if not ev.get("start") or ev.get("status") == "CANCELLED":
                    continue
                s, e = ev["start"], ev["end"]
                if e <= w_start or s >= w_end:
                    continue  # Яндекс отдаёт лишнее — фильтруем сами
                rid = ev.get("recurrence_id") or ""
                ev_id = f"{cal_id}:{ev['uid']}" + (f":{rid}" if rid else "")
                rows.append({
                    "id": ev_id, "source": "yandex", "calendar": cal_id, "calendar_name": c["name"],
                    "uid": ev["uid"], "href": ev["href"], "etag": ev["etag"], "recurring": 1 if ev.get("rrule") else 0,
                    "title": ev["title"] or "(без названия)", "start": _iso(s), "end": _iso(e),
                    "all_day": 1 if ev["all_day"] else 0, "location": ev.get("location") or "",
                    "description": (ev.get("description") or "")[:2000], "remind_min": int(ev.get("remind_min") or 0),
                    "created": _iso(now), "updated": _iso(now),
                })
        db.replace_source_window("yandex", w_start, w_end, rows)
        db.set_meta("yandex.last_sync", _iso(now))
        db.set_meta("yandex.last_error", "")
        state.update({"state": "connected", "last_sync": _iso(now), "last_error": "", "calendars": len(cals), "events": len(rows)})
    except CalDavError as exc:
        db.set_meta("yandex.last_error", f"{exc.kind}: {exc.message}")
        state.update({"state": "auth_failed" if exc.kind == "auth_failed" else "error", "last_error": f"{exc.kind}: {exc.message}"})
        if exc.kind == "auth_failed":
            state["next_step"] = "Проверь YANDEX_CALDAV_USER (полный e-mail) и пароль приложения; новый пароль может заработать через 2–3 часа."
    except Exception as exc:  # noqa: BLE001
        db.set_meta("yandex.last_error", f"unexpected: {exc}")
        state.update({"state": "error", "last_error": f"unexpected: {exc}"})
    return state


# ── доменная логика ───────────────────────────────────────────────


def _event_out(row: Dict[str, Any], compact: bool = True) -> Dict[str, Any]:
    out = {
        "id": row["id"],
        "source": row.get("source") or "local",
        "calendar": row["calendar"],
        "calendar_name": row.get("calendar_name") or LOCAL_CALENDAR_NAME,
        "title": row["title"],
        "start": row["start"],
        "end": row["end"],
        "all_day": bool(row["all_day"]),
        "location": row.get("location") or "",
        "remind_before_min": int(row.get("remind_min") or 0),
        "recurring": bool(row.get("recurring")),
    }
    if not compact:
        out["description"] = row.get("description") or ""
        out["updated"] = row.get("updated")
    return out


def _overlaps(events: List[Dict[str, Any]], start: datetime, end: datetime, exclude: str = "") -> List[str]:
    hits = []
    for ev in events:
        if ev["id"] == exclude or ev["all_day"]:
            continue
        s, e = _parse_dt(ev["start"]), _parse_dt(ev["end"])
        if s < end and e > start:
            hits.append(ev["id"])
    return hits


def _conflicts(events: List[Dict[str, Any]]) -> List[List[str]]:
    pairs = []
    timed = [e for e in events if not e["all_day"]]
    for i, a in enumerate(timed):
        sa, ea = _parse_dt(a["start"]), _parse_dt(a["end"])
        for b in timed[i + 1:]:
            if a.get("uid") and a.get("uid") == b.get("uid"):
                continue  # копии одного события в разных календарях — не конфликт
            sb, eb = _parse_dt(b["start"]), _parse_dt(b["end"])
            if sa < eb and ea > sb:
                pairs.append([a["id"], b["id"]])
    return pairs


def _dedupe(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Одно событие (один UID) в нескольких календарях → одна карточка с also_in."""
    seen: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for ev in events:
        key = f"{ev.get('uid') or ev['id']}|{ev['start']}"
        if key in seen:
            seen[key].setdefault("also_in", []).append(ev.get("calendar_name") or ev["calendar"])
            continue
        ev = dict(ev)
        seen[key] = ev
        out.append(ev)
    return out


def _free_slots(events: List[Dict[str, Any]], start: datetime, end: datetime, duration_min: int,
                work_start: time = WORK_START, work_end: time = WORK_END, max_slots: int = 6) -> List[Dict[str, Any]]:
    tz = _local_tz()
    busy = sorted(((_parse_dt(e["start"]), _parse_dt(e["end"])) for e in events if not e["all_day"]), key=lambda p: p[0])
    slots: List[Dict[str, Any]] = []
    day = start.date()
    now = _now()
    while day <= end.date() and len(slots) < max_slots:
        cursor = max(datetime.combine(day, work_start, tzinfo=tz), start, now)
        day_end = min(datetime.combine(day, work_end, tzinfo=tz), end)
        for bs, be in busy:
            if be <= cursor or bs >= day_end:
                continue
            if (bs - cursor).total_seconds() >= duration_min * 60:
                slots.append({"start": _iso(cursor), "end": _iso(bs), "minutes": int((bs - cursor).total_seconds() // 60)})
                if len(slots) >= max_slots:
                    break
            cursor = max(cursor, be)
        if len(slots) < max_slots and (day_end - cursor).total_seconds() >= duration_min * 60:
            slots.append({"start": _iso(cursor), "end": _iso(day_end), "minutes": int((day_end - cursor).total_seconds() // 60)})
        day += timedelta(days=1)
    return slots[:max_slots]


def _label(ev: Dict[str, Any]) -> str:
    rec = " ↻" if ev.get("recurring") else ""
    if ev["all_day"]:
        return f"весь день · {ev['title']}{rec}"
    return f"{_hm(_parse_dt(ev['start']))}–{_hm(_parse_dt(ev['end']))} · {ev['title']}{rec}"


def _source_label(ev: Dict[str, Any]) -> str:
    src = ev.get("source") or "local"
    name = ev.get("calendar_name") or LOCAL_CALENDAR_NAME
    prefix = "Яндекс · " if src == "yandex" else ""
    return f"{prefix}{name}"


def _calendar_item(ev: Dict[str, Any], with_date: bool = False) -> Dict[str, Any]:
    s = _parse_dt(ev["start"])
    label = _label(ev)
    if with_date:
        label = f"{WEEKDAY_LABELS[s.weekday()]} {s.day:02d}.{s.month:02d} · {label}"
    row = _source_label(ev)
    if ev.get("also_in"):
        row += " · также в: " + ", ".join(ev["also_in"])
    if ev.get("location"):
        row += f" · {ev['location']}"
    if int(ev.get("remind_min") or 0):
        row += f" · напомнить за {int(ev['remind_min'])} мин"
    return {"label": label, "row": row}


def _agenda_payload(api) -> Dict[str, Any]:
    db = _db(api)
    ystate = _sync_yandex(api)
    now = _now()
    today = now.date()
    d_start, d_end = _day_bounds(today)
    w_start, w_end, monday = _week_bounds(today)
    today_events = _dedupe(db.window(d_start, d_end))
    week_events = _dedupe(db.window(w_start, w_end))
    conflicts = _conflicts(week_events)
    conflict_ids = {i for pair in conflicts for i in pair}

    def item(ev, with_date=False):
        it = _calendar_item(ev, with_date)
        if ev["id"] in conflict_ids:
            it["label"] = "⚠ " + it["label"]
        return it

    free_today = _free_slots(today_events, max(d_start, now), d_end, 15, max_slots=20)
    free_hours = round(sum(s["minutes"] for s in free_today) / 60, 1)
    cards = []
    for ev in week_events:
        s = _parse_dt(ev["start"])
        mark = "[Я] " if ev.get("source") == "yandex" else ""
        cards.append({"id": ev["id"], "label": f"{mark}{_label(ev)}", "column": f"d{(s.date() - monday).days}"})
    week_label = f"{monday.day:02d}.{monday.month:02d}–{(monday + timedelta(days=6)).day:02d}.{(monday + timedelta(days=6)).month:02d}"
    sources_text = f"«{LOCAL_CALENDAR_NAME}»"
    if ystate["state"] == "connected":
        sources_text += f" + Яндекс ({ystate.get('calendars', 0)} кал., синк {ystate['last_sync'][11:16] if ystate.get('last_sync') else '—'})"
    elif ystate["state"] == "not_configured":
        sources_text += " · Яндекс не подключён"
    else:
        sources_text += f" · Яндекс: ошибка ({ystate.get('last_error', '')[:60]})"
    notice = (
        f"{sources_text} · сегодня {WEEKDAY_LABELS[today.weekday()]} {today.day:02d}.{today.month:02d} · "
        f"неделя {week_label} · {now.strftime('%H:%M')}"
    )
    if conflicts:
        notice += f" · ⚠ пересечений: {len(conflicts)}"
    return {
        "status": "ok",
        "notice": notice,
        "sources": {"local": {"state": "connected"}, "yandex": {k: v for k, v in ystate.items() if k != "source"}},
        "metrics": {
            "today_count": len(today_events),
            "free_hours": free_hours,
            "week_count": len(week_events),
            "conflicts": len(conflicts),
        },
        "items_today": [item(e) for e in today_events],
        "items_week": [item(e, True) for e in week_events],
        "cards": cards,
        "free_slots": _free_slots(week_events, max(w_start, now), w_end, 30, max_slots=8),
        "week": {"monday": monday.isoformat(), "label": week_label},
    }


def _resolve_calendars(api, text: str) -> Tuple[List[Dict[str, Any]], str]:
    """«local» / «yandex» / «all» / id / имя → список календарей для записи. Возвращает (calendars, error)."""
    db = _db(api)
    cals = db.calendars()
    key = (text or "").strip().lower()
    if key in ("", "default", "local", "личное", "личный", "мой", "уроборос", "ouroboros"):
        return [c for c in cals if c["id"] == LOCAL_CALENDAR], ""
    if key in ("all", "все", "везде", "во все", "во всех"):
        return [c for c in cals if c["writable"]], ""
    if key in ("yandex", "яндекс", "ya"):
        ycals = [c for c in cals if c["source"] == "yandex" and c["writable"]]
        if not ycals:
            st = _sync_yandex(api, force=True)
            ycals = [c for c in db.calendars("yandex") if c["writable"]]
            if not ycals:
                return [], st.get("next_step") or st.get("last_error") or "Яндекс не подключён"
        return ycals[:1], ""
    for c in cals:
        if c["id"].lower() == key or c["name"].lower() == key:
            return [c], ""
    for c in cals:
        if key in c["name"].lower():
            return [c], ""
    return [], f"Календарь «{text}» не найден. Доступные: " + ", ".join(f"{c['name']} ({c['id']})" for c in cals)


def _create_in(api, cal: Dict[str, Any], *, uid: str, title: str, start: datetime, end: datetime, all_day: bool,
               location: str, description: str, remind_min: int) -> Dict[str, Any]:
    db = _db(api)
    now_iso = _iso(_now())
    base = {
        "source": cal["source"], "calendar": cal["id"], "calendar_name": cal["name"], "uid": uid, "recurring": 0,
        "title": title.strip(), "start": _iso(start), "end": _iso(end), "all_day": 1 if all_day else 0,
        "location": location or "", "description": description or "", "remind_min": int(remind_min or 0),
        "created": now_iso, "updated": now_iso, "href": None, "etag": None,
    }
    if cal["source"] == "yandex":
        client = _yandex_client(api)
        if client is None:
            return {"status": "not_connected", "message": "Яндекс не подключён"}
        ics = build_vcalendar(uid, title, start, end, all_day, location, description, remind_min)
        try:
            href, etag = client.put_new(cal["href"], uid, ics)
            # read-back: read-only календари Яндекса молча «принимают» PUT
            try:
                text, etag2 = client.get_ics(href)
                if "BEGIN:VEVENT" not in text:
                    return {"status": "not_saved", "message": f"Яндекс не сохранил событие в «{cal['name']}» (возможно, календарь только для чтения)"}
                etag = etag2 or etag
            except CalDavError as exc:
                if exc.kind == "not_found":
                    return {"status": "not_saved", "message": f"Яндекс не сохранил событие в «{cal['name']}» (календарь только для чтения?)"}
        except CalDavError as exc:
            return {"status": "error", "message": f"Яндекс: {exc.message}"}
        base.update({"id": f"{cal['id']}:{uid}", "href": href, "etag": etag})
    else:
        base["id"] = f"{LOCAL_CALENDAR}:{uid}"
    db.insert(base)
    return {"status": "created", "event": _event_out(db.get(base["id"]), compact=False)}


def _create(api, *, calendars: List[Dict[str, Any]], title: str, start: datetime, end: Optional[datetime] = None,
            duration_min: int = 60, all_day: bool = False, location: str = "", description: str = "", remind_min: int = 0) -> Dict[str, Any]:
    db = _db(api)
    if all_day:
        s, _ = _day_bounds(start.date())
        e = s + timedelta(days=1)
    else:
        s = start
        e = end or (s + timedelta(minutes=max(5, int(duration_min or 60))))
        if e <= s:
            e = s + timedelta(minutes=60)
    overlaps = _overlaps(db.window(s - timedelta(days=1), e + timedelta(days=1)), s, e)
    uid = f"ouro-{uuid.uuid4().hex[:16]}@ouroboros"
    created, failed = [], []
    for cal in calendars:
        r = _create_in(api, cal, uid=uid, title=title, start=s, end=e, all_day=all_day, location=location,
                       description=description, remind_min=remind_min)
        (created if r.get("status") == "created" else failed).append({**r, "calendar": cal["name"]})
    return {"created": created, "failed": failed, "overlaps": overlaps}


def _move_row(api, row: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    """Применяет изменения к одной записи (локальной или Яндекса)."""
    db = _db(api)
    if row.get("source") == "yandex":
        client = _yandex_client(api)
        if client is None or not row.get("href"):
            return {"status": "not_connected", "message": "Яндекс не подключён"}
        try:
            text, etag = client.get_ics(row["href"])
            new_s = _parse_dt(fields.get("start", row["start"]))
            new_e = _parse_dt(fields.get("end", row["end"]))
            all_day = bool(row["all_day"])
            if "start" in fields or "end" in fields:
                text = rewrite_vevent_times(text, new_s, new_e, all_day)
            for prop, key in (("SUMMARY", "title"), ("LOCATION", "location"), ("DESCRIPTION", "description")):
                if key in fields:
                    text = _rewrite_text_prop(text, prop, fields[key])
            new_etag = client.put_update(row["href"], text, etag or row.get("etag") or "")
            fields = dict(fields)
            fields["etag"] = new_etag or etag
        except CalDavError as exc:
            return {"status": "conflict" if exc.kind == "conflict" else "error", "message": f"Яндекс: {exc.message}"}
    db.update(row["id"], fields)
    return {"status": "moved", "event": _event_out(db.get(row["id"]), compact=False)}


def _rewrite_text_prop(ics_text: str, prop: str, value: str) -> str:
    lines = _unfold(ics_text)
    out, done, in_event, rid = [], False, False, False
    for line in lines:
        name, _, val = _split_prop(line)
        if name == "BEGIN" and val.upper() == "VEVENT":
            in_event, rid = True, False
        elif name == "RECURRENCE-ID":
            rid = True
        elif name == "END" and val.upper() == "VEVENT":
            if in_event and not rid and not done:
                out.append(f"{prop}:{_ical_escape(value)}")
                done = True
            in_event = False
        if in_event and not rid and name == prop:
            if not done:
                out.append(f"{prop}:{_ical_escape(value)}")
                done = True
            continue
        out.append(line)
    return "\r\n".join(out) + "\r\n"


def _delete_row(api, row: Dict[str, Any]) -> Dict[str, Any]:
    db = _db(api)
    if row.get("source") == "yandex":
        client = _yandex_client(api)
        if client is None or not row.get("href"):
            return {"status": "not_connected", "message": "Яндекс не подключён"}
        try:
            client.delete(row["href"], row.get("etag") or "")
        except CalDavError as exc:
            if exc.kind != "not_found":
                return {"status": "error", "message": f"Яндекс: {exc.message}"}
    db.delete(row["id"])
    return {"status": "deleted", "deleted_id": row["id"]}


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# ── регистрация ───────────────────────────────────────────────────


def register(api):
    # ---------- tools ----------

    def cal_status(sync: bool = False, **kwargs) -> str:
        db = _db(api)
        ystate = _sync_yandex(api, force=bool(sync))
        now = _now()
        d_start, d_end = _day_bounds(now.date())
        return _json({
            "status": "ok",
            "timezone": str(_local_tz()),
            "now_local": _iso(now),
            "today": now.date().isoformat(),
            "weekday": WEEKDAY_LABELS[now.weekday()],
            "calendars": [{"id": c["id"], "name": c["name"], "source": c["source"], "writable": bool(c["writable"])} for c in db.calendars()],
            "default_calendar": LOCAL_CALENDAR,
            "sources": {
                "local": {"state": "connected", "calendars": 1},
                "yandex": {k: v for k, v in ystate.items() if k != "source"},
            },
            "events_today": len(_dedupe(db.window(d_start, d_end))),
            "working_hours": f"{WORK_START.strftime('%H:%M')}-{WORK_END.strftime('%H:%M')}",
            "hint": "Для записи в Яндекс укажи calendar='yandex' (или имя календаря); 'all' — во все календари; по умолчанию — «Личное».",
        })

    api.register_tool(
        name="cal_status",
        description="Состояние календаря: таймзона, now_local и today (считай «сегодня/завтра/в четверг» от них), календари всех источников (Личное + Яндекс), статус подключения Яндекса и next_step, если он не подключён. Вызывай первым. sync=true — принудительно обновить Яндекс.",
        schema={"type": "object", "properties": {"sync": {"type": "boolean", "default": False}}},
        handler=cal_status,
    )

    def cal_events(start: str = "", end: str = "", query: str = "", id: str = "", calendar: str = "", limit: int = MAX_EVENTS, **kwargs) -> str:
        db = _db(api)
        _sync_yandex(api)
        if id:
            row = db.get(id)
            if not row:
                return _json({"status": "not_found", "message": f"Событие {id} не найдено"})
            return _json({"status": "ok", "event": _event_out(row, compact=False)})
        now = _now()
        s = _parse_dt(start, _day_bounds(now.date())[0])
        e = _parse_dt(end, s + timedelta(days=1))
        if e <= s:
            e = s + timedelta(days=1)
        if (e - s).days > MAX_WINDOW_DAYS:
            e = s + timedelta(days=MAX_WINDOW_DAYS)
        cal_ids = None
        if calendar:
            cals, err = _resolve_calendars(api, calendar)
            if err:
                return _json({"status": "error", "message": err})
            cal_ids = [c["id"] for c in cals]
        rows = _dedupe(db.window(s, e, cal_ids))
        if query:
            q = query.lower()
            rows = [r for r in rows if q in r["title"].lower() or q in (r.get("description") or "").lower()]
        limit = max(1, min(int(limit or MAX_EVENTS), MAX_EVENTS))
        truncated = len(rows) > limit
        rows = rows[:limit]
        return _json({
            "status": "ok",
            "timezone": str(_local_tz()),
            "range": {"start": _iso(s), "end": _iso(e)},
            "events": [{**_event_out(r), "also_in": r.get("also_in", [])} for r in rows],
            "conflicts": _conflicts(rows),
            "truncated": truncated,
        })

    api.register_tool(
        name="cal_events",
        description="События всех календарей (Личное + Яндекс) за период (по умолчанию сегодня). start/end — ISO 8601 (без offset = местное время). query — поиск по названию. calendar — ограничить одним календарём ('yandex', 'local', имя или id). id — одно событие целиком. Возвращает events[], conflicts[] (пересечения), also_in у копий.",
        schema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Начало периода ISO 8601, по умолчанию сегодня 00:00"},
                "end": {"type": "string", "description": "Конец периода ISO 8601, по умолчанию start + 1 день"},
                "query": {"type": "string", "description": "Подстрока для поиска по названию"},
                "calendar": {"type": "string", "description": "yandex | local | имя или id календаря"},
                "id": {"type": "string", "description": "Вернуть одно событие по id"},
                "limit": {"type": "integer", "description": "Максимум событий (≤50)", "default": 50},
            },
        },
        handler=cal_events,
    )

    def cal_create(title: str = "", start: str = "", end: str = "", duration_min: int = 60, all_day: bool = False,
                   calendar: str = "", location: str = "", description: str = "", remind_before_min: int = 0,
                   confirm: bool = False, **kwargs) -> str:
        if not title.strip():
            return _json({"status": "error", "message": "Нужно название события"})
        s = _parse_dt(start)
        if s is None:
            return _json({"status": "error", "message": "Нужно время начала в ISO 8601, например 2026-09-22T16:00"})
        cals, err = _resolve_calendars(api, calendar)
        if err or not cals:
            return _json({"status": "not_connected" if "не подключён" in (err or "") else "error", "message": err or "Календарь не найден"})
        if not confirm:
            return _json({"status": "needs_confirm", "message": "Запись делается только по явной команде владельца: повтори вызов с confirm=true",
                          "target_calendars": [c["name"] for c in cals]})
        e = _parse_dt(end) if end else None
        result = _create(api, calendars=cals, title=title, start=s, end=e, duration_min=int(duration_min or 60), all_day=bool(all_day),
                         location=location, description=description, remind_min=int(remind_before_min or 0))
        if not result["created"]:
            return _json({"status": "error", "message": "Не удалось создать: " + "; ".join(f"{f['calendar']}: {f.get('message', f.get('status'))}" for f in result["failed"])})
        ev = result["created"][0]["event"]
        where = ", ".join(c["calendar"] for c in result["created"])
        when = ev["start"][:10] + (" весь день" if ev["all_day"] else f" {ev['start'][11:16]}–{ev['end'][11:16]}")
        msg = f"Создано: {ev['title']} · {when} · в: {where}"
        if result["failed"]:
            msg += " · не удалось: " + "; ".join(f"{f['calendar']}: {f.get('message', f.get('status'))}" for f in result["failed"])
        if result["overlaps"]:
            msg += f" · ⚠ пересекается с {len(result['overlaps'])} событием(ями)"
        out = {"status": "created", "message": msg, "event": ev, "created_in": [c["calendar"] for c in result["created"]],
               "failed": result["failed"], "overlaps": result["overlaps"]}
        if ev["remind_before_min"]:
            remind_at = _parse_dt(ev["start"]) - timedelta(minutes=ev["remind_before_min"])
            out["reminder_at"] = _iso(remind_at)
            out["next_step"] = "Поставь schedule_followup(run_at=reminder_at, objective='Напомни владельцу: <название> в <время>')"
        return _json(out)

    api.register_tool(
        name="cal_create",
        description="Создать событие. calendar: '' или 'local' — «Личное» (по умолчанию); 'yandex' — основной календарь Яндекса; 'all' — во все календари сразу (одно событие копией); имя или id — конкретный. Только по явной команде владельца или после его выбора в escalate — confirm=true. start ISO 8601 (без offset = местное). Возвращает event, created_in, overlaps, reminder_at.",
        schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название"},
                "start": {"type": "string", "description": "Начало ISO 8601, например 2026-09-22T16:00"},
                "end": {"type": "string", "description": "Конец ISO 8601 (или используй duration_min)"},
                "duration_min": {"type": "integer", "description": "Длительность в минутах, по умолчанию 60", "default": 60},
                "all_day": {"type": "boolean", "default": False},
                "calendar": {"type": "string", "description": "local | yandex | all | имя или id календаря", "default": ""},
                "location": {"type": "string", "description": "Место или ссылка"},
                "description": {"type": "string", "description": "Заметка"},
                "remind_before_min": {"type": "integer", "description": "Напомнить за N минут (0 = без напоминания)", "default": 0},
                "confirm": {"type": "boolean", "description": "Владелец явно попросил создать событие", "default": False},
            },
            "required": ["title", "start", "confirm"],
        },
        handler=cal_create,
    )

    def _resolve(db: Store, id: str, query: str):
        if id:
            row = db.get(id)
            return ([row] if row else [])
        if query:
            return db.find_by_title(query)
        return []

    def _siblings(db: Store, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Копии того же события (один UID) в других календарях."""
        if not row.get("uid"):
            return [row]
        rows = db.window(_parse_dt(row["start"]) - timedelta(days=1), _parse_dt(row["end"]) + timedelta(days=1))
        same = [r for r in rows if r.get("uid") == row["uid"]]
        return same or [row]

    def cal_move(id: str = "", query: str = "", start: str = "", end: str = "", title: str = "", location: str = "",
                 description: str = "", remind_before_min: Optional[int] = None, confirm: bool = False, **kwargs) -> str:
        db = _db(api)
        _sync_yandex(api)
        rows = _resolve(db, id, query)
        if not rows:
            return _json({"status": "not_found", "message": "Событие не найдено: уточни id или название через cal_events"})
        rows = _dedupe(rows)
        if len(rows) > 1:
            return _json({"status": "ambiguous", "message": "Найдено несколько событий, уточни какое", "candidates": [_event_out(r) for r in rows]})
        if not confirm:
            return _json({"status": "needs_confirm", "message": "Перенос/правка только по явной команде владельца: повтори с confirm=true", "event": _event_out(rows[0])})
        row = rows[0]
        fields: Dict[str, Any] = {}
        old_s, old_e = _parse_dt(row["start"]), _parse_dt(row["end"])
        if start:
            new_s = _parse_dt(start)
            new_e = _parse_dt(end) if end else new_s + (old_e - old_s)
            if new_e <= new_s:
                new_e = new_s + (old_e - old_s)
            fields["start"], fields["end"] = _iso(new_s), _iso(new_e)
        elif end:
            new_e = _parse_dt(end)
            if new_e > old_s:
                fields["end"] = _iso(new_e)
        if title:
            fields["title"] = title.strip()
        if location:
            fields["location"] = location
        if description:
            fields["description"] = description
        if remind_before_min is not None:
            fields["remind_min"] = int(remind_before_min)
        if not fields:
            return _json({"status": "error", "message": "Нечего менять: укажи start/end/title/location/description/remind_before_min"})
        results = [(_move_row(api, r, dict(fields)), r) for r in _siblings(db, row)]
        ok = [r for r, _ in results if r.get("status") == "moved"]
        bad = [(r, src) for r, src in results if r.get("status") != "moved"]
        if not ok:
            return _json({"status": bad[0][0].get("status", "error"), "message": bad[0][0].get("message", "Не удалось изменить")})
        ev = ok[0]["event"]
        overlaps = _overlaps(db.window(_parse_dt(ev["start"]) - timedelta(days=1), _parse_dt(ev["end"]) + timedelta(days=1)),
                             _parse_dt(ev["start"]), _parse_dt(ev["end"]), exclude=ev["id"])
        msg = f"Обновлено: {ev['title']} {ev['start'][:16]}–{ev['end'][11:16]} ({', '.join(r['event']['calendar_name'] for r in ok)})"
        if bad:
            msg += " · не удалось: " + "; ".join(f"{src.get('calendar_name')}: {r.get('message')}" for r, src in bad)
        return _json({"status": "moved", "message": msg, "event": ev, "overlaps": overlaps})

    api.register_tool(
        name="cal_move",
        description="Перенести или изменить событие (по id или по названию через query), включая события Яндекса и копии во всех календарях. Только по явной команде владельца — confirm=true. Можно поменять start/end (длительность сохраняется), title, location, description, remind_before_min.",
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "id события"},
                "query": {"type": "string", "description": "Часть названия, если id неизвестен"},
                "start": {"type": "string", "description": "Новое начало ISO 8601"},
                "end": {"type": "string", "description": "Новый конец ISO 8601"},
                "title": {"type": "string"},
                "location": {"type": "string"},
                "description": {"type": "string"},
                "remind_before_min": {"type": "integer", "description": "Напомнить за N минут"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["confirm"],
        },
        handler=cal_move,
    )

    def cal_delete(id: str = "", query: str = "", confirm: bool = False, **kwargs) -> str:
        db = _db(api)
        rows = _resolve(db, id, query)
        if not rows:
            return _json({"status": "not_found", "message": "Событие не найдено"})
        rows = _dedupe(rows)
        if len(rows) > 1:
            return _json({"status": "ambiguous", "message": "Найдено несколько событий, уточни какое", "candidates": [_event_out(r) for r in rows]})
        if not confirm:
            return _json({"status": "needs_confirm", "message": "Удаление только по явной команде владельца: повтори с confirm=true", "event": _event_out(rows[0])})
        row = rows[0]
        results = [(_delete_row(api, r), r) for r in _siblings(db, row)]
        ok = [src for r, src in results if r.get("status") == "deleted"]
        bad = [(r, src) for r, src in results if r.get("status") != "deleted"]
        if not ok:
            return _json({"status": bad[0][0].get("status", "error"), "message": bad[0][0].get("message", "Не удалось удалить")})
        msg = f"Удалено: {row['title']} {row['start'][:16]} ({', '.join(s.get('calendar_name') or '' for s in ok)})"
        if bad:
            msg += " · не удалось: " + "; ".join(f"{src.get('calendar_name')}: {r.get('message')}" for r, src in bad)
        return _json({"status": "deleted", "message": msg, "deleted_ids": [s["id"] for s in ok]})

    api.register_tool(
        name="cal_delete",
        description="Удалить событие (по id или названию через query), включая копии в других календарях. Только по явной команде владельца — confirm=true.",
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "query": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["confirm"],
        },
        handler=cal_delete,
    )

    def cal_free(start: str = "", end: str = "", duration_min: int = 60, working_hours: str = "", max: int = 6, **kwargs) -> str:
        db = _db(api)
        _sync_yandex(api)
        now = _now()
        s = _parse_dt(start, now)
        e = _parse_dt(end, s + timedelta(days=3))
        if e <= s:
            e = s + timedelta(days=1)
        ws, we = WORK_START, WORK_END
        if working_hours and "-" in working_hours:
            try:
                a, b = working_hours.split("-", 1)
                ws, we = time.fromisoformat(a.strip()), time.fromisoformat(b.strip())
            except ValueError:
                pass
        events = _dedupe(db.window(s, e))
        slots = _free_slots(events, s, e, int(duration_min or 60), ws, we, max_slots=max(1, min(int(max or 6), 10)))
        return _json({
            "status": "ok",
            "timezone": str(_local_tz()),
            "range": {"start": _iso(s), "end": _iso(e)},
            "duration_min": int(duration_min or 60),
            "working_hours": f"{ws.strftime('%H:%M')}-{we.strftime('%H:%M')}",
            "slots": slots,
            "busy_sources": sorted({(ev.get("source") or "local") for ev in events}),
            "next_step": "Предложи владельцу 2–4 варианта через escalate (recommended = первый), затем cal_create с confirm=true",
        })

    api.register_tool(
        name="cal_free",
        description="Свободные окна не короче duration_min минут в рабочие часы (по умолчанию 09:00-19:00) по занятости всех календарей за период (по умолчанию ближайшие 3 дня). Затем предложи варианты владельцу через escalate.",
        schema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Начало периода ISO 8601, по умолчанию сейчас"},
                "end": {"type": "string", "description": "Конец периода, по умолчанию +3 дня"},
                "duration_min": {"type": "integer", "default": 60},
                "working_hours": {"type": "string", "description": "Например 10:00-18:00"},
                "max": {"type": "integer", "description": "Максимум окон (≤10)", "default": 6},
            },
        },
        handler=cal_free,
    )

    def cal_brief(date: str = "today", **kwargs) -> str:
        db = _db(api)
        _sync_yandex(api)
        now = _now()
        if date in ("", "today"):
            day = now.date()
        elif date == "tomorrow":
            day = now.date() + timedelta(days=1)
        else:
            parsed = _parse_dt(date)
            day = parsed.date() if parsed else now.date()
        s, e = _day_bounds(day)
        events = _dedupe(db.window(s, e))
        conflicts = _conflicts(events)
        conflict_ids = {i for pair in conflicts for i in pair}
        head = f"**{WEEKDAY_LABELS[day.weekday()]}, {day.day:02d}.{day.month:02d}**"
        lines = [f"{head} — {len(events)} событий" if events else f"{head} — событий нет"]
        for ev in events:
            mark = " ⚠" if ev["id"] in conflict_ids else ""
            loc = f" · {ev['location']}" if ev.get("location") else ""
            rem = f" · напоминание за {ev['remind_min']} мин" if int(ev.get("remind_min") or 0) else ""
            lines.append(f"- {_label(ev)} · {_source_label(ev)}{loc}{rem}{mark}")
        free = _free_slots(events, max(s, now) if day == now.date() else s, e, 30, max_slots=4)
        if free:
            lines.append("")
            lines.append("Свободные окна: " + ", ".join(f"{f['start'][11:16]}–{f['end'][11:16]}" for f in free))
        if conflicts:
            lines.append(f"⚠ Пересечений: {len(conflicts)}")
        first = events[0] if events else None
        return _json({
            "status": "ok",
            "date": day.isoformat(),
            "text": "\n".join(lines),
            "stats": {"events": len(events), "conflicts": len(conflicts), "first_event": _label(first) if first else None, "free_slots": free},
        })

    api.register_tool(
        name="cal_brief",
        description="Детерминированный брифинг на день (today | tomorrow | YYYY-MM-DD) по всем календарям: события с источником, свободные окна, пересечения. Готовый markdown в поле text — верни владельцу как есть.",
        schema={"type": "object", "properties": {"date": {"type": "string", "description": "today, tomorrow или дата YYYY-MM-DD", "default": "today"}}},
        handler=cal_brief,
    )

    # ---------- routes для виджета ----------

    async def route_agenda(request):
        return JSONResponse(_agenda_payload(api))

    async def route_create(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        title = str(body.get("title") or "").strip()
        if not title:
            return JSONResponse({"status": "error", "message": "Введите название"}, status_code=400)
        now = _now()
        day_text = str(body.get("date") or "").strip()
        time_text = str(body.get("time") or "").strip()
        try:
            day = date.fromisoformat(day_text) if day_text else now.date()
        except ValueError:
            return JSONResponse({"status": "error", "message": "Дата в формате 2026-09-22"}, status_code=400)
        all_day = not time_text
        if time_text:
            try:
                hm = time.fromisoformat(time_text if len(time_text) > 5 else time_text + ":00")
            except ValueError:
                return JSONResponse({"status": "error", "message": "Время в формате 16:00"}, status_code=400)
            start = datetime.combine(day, hm, tzinfo=_local_tz())
        else:
            start = datetime.combine(day, time(0, 0), tzinfo=_local_tz())
        try:
            duration = int(float(body.get("duration_min") or 60))
        except (TypeError, ValueError):
            duration = 60
        try:
            remind = int(float(body.get("remind_min") or 0))
        except (TypeError, ValueError):
            remind = 0
        cals, err = _resolve_calendars(api, str(body.get("calendar") or ""))
        if err or not cals:
            return JSONResponse({"status": "error", "message": err or "Календарь не найден"}, status_code=400)
        result = _create(api, calendars=cals, title=title, start=start, duration_min=duration, all_day=all_day, remind_min=remind)
        if not result["created"]:
            return JSONResponse({"status": "error", "message": "Не удалось: " + "; ".join(f"{f['calendar']}: {f.get('message')}" for f in result["failed"])}, status_code=502)
        ev = result["created"][0]["event"]
        when = "весь день" if all_day else f"{ev['start'][11:16]}–{ev['end'][11:16]}"
        msg = f"Создано: {ev['title']} · {ev['start'][:10]} · {when} · {', '.join(c['calendar'] for c in result['created'])}"
        if result["overlaps"]:
            msg += f" · ⚠ пересекается с {len(result['overlaps'])}"
        return JSONResponse({"status": "created", "message": msg, "event": ev})

    async def route_move_card(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        card_id = str((body or {}).get("card_id") or "")
        column_id = str((body or {}).get("column_id") or "")
        db = _db(api)
        row = db.get(card_id)
        if not row or not column_id.startswith("d") or not column_id[1:].isdigit():
            return JSONResponse({"status": "error", "message": "Неизвестная карточка или колонка"}, status_code=400)
        offset = int(column_id[1:])
        if not 0 <= offset <= 6:
            return JSONResponse({"status": "error", "message": "Колонка вне недели"}, status_code=400)
        _, _, monday = _week_bounds(_now().date())
        target_day = monday + timedelta(days=offset)
        old_s, old_e = _parse_dt(row["start"]), _parse_dt(row["end"])
        new_s = datetime.combine(target_day, old_s.timetz())
        new_e = new_s + (old_e - old_s)
        errors = []
        for r in _siblings(db, row):
            res = _move_row(api, r, {"start": _iso(new_s), "end": _iso(new_e)})
            if res.get("status") != "moved":
                errors.append(f"{r.get('calendar_name')}: {res.get('message')}")
        if errors:
            return JSONResponse({"status": "error", "message": "Не удалось перенести: " + "; ".join(errors)}, status_code=502)
        payload = _agenda_payload(api)
        payload["message"] = f"Перенесено: {row['title']} → {WEEKDAY_LABELS[offset]} {target_day.day:02d}.{target_day.month:02d}"
        return JSONResponse(payload)

    api.register_route("agenda", route_agenda, methods=("GET",))
    api.register_route("create", route_create, methods=("POST",))
    api.register_route("move_card", route_move_card, methods=("POST",))

    # ---------- виджет ----------

    api.register_ui_tab("agenda", "Календарь", icon="📅", render={
        "kind": "declarative",
        "start": "auto",
        "schema_version": 1,
        "span": 2,
        "components": [
            {"type": "poll", "route": "agenda", "method": "GET", "target": "result",
             "interval_ms": 30000, "max_ticks": 100, "auto_start": True},
            {"type": "callout", "target": "result", "path": "notice", "tone": "info"},
            {"type": "group", "layout": "cluster", "components": [
                {"type": "metric", "label": "Событий сегодня", "target": "result", "path": "metrics.today_count"},
                {"type": "metric", "label": "Свободно часов сегодня", "target": "result", "path": "metrics.free_hours", "precision": 1},
                {"type": "metric", "label": "На неделе", "target": "result", "path": "metrics.week_count"},
                {"type": "metric", "label": "Пересечений", "target": "result", "path": "metrics.conflicts", "tone": "warning"},
            ]},
            {"type": "tabs", "target": "result", "tabs": [
                {"label": "Сегодня", "components": [
                    {"type": "calendar", "target": "result", "path": "items_today"},
                ]},
                {"label": "Неделя", "components": [
                    {"type": "kanban", "target": "result", "path": "cards",
                     "on_move": {"route": "move_card", "method": "POST"},
                     "columns": [{"id": f"d{i}", "label": WEEKDAY_LABELS[i]} for i in range(7)]},
                ]},
                {"label": "Список недели", "components": [
                    {"type": "calendar", "target": "result", "path": "items_week"},
                ]},
                {"label": "Свободные окна", "components": [
                    {"type": "table", "target": "result", "path": "free_slots", "columns": [
                        {"label": "Начало", "path": "start"},
                        {"label": "Конец", "path": "end"},
                        {"label": "Минут", "path": "minutes", "presentation": "number"},
                    ]},
                ]},
            ]},
            {"type": "form", "route": "create", "method": "POST", "target": "create_result",
             "submit_label": "Создать", "columns": 4, "fields": [
                {"name": "title", "label": "Название", "type": "text", "required": True, "span": 2},
                {"name": "date", "label": "Дата", "type": "text", "placeholder": "2026-09-22 (пусто = сегодня)"},
                {"name": "time", "label": "Время", "type": "text", "placeholder": "16:00"},
                {"name": "calendar", "label": "Календарь", "type": "select", "default": "local",
                 "options": [{"value": "local", "label": "Личное"}, {"value": "yandex", "label": "Яндекс"}, {"value": "all", "label": "Все"}]},
                {"name": "duration_min", "label": "Длительность", "type": "number", "default": 60, "min": 5, "max": 1440, "step": 5},
                {"name": "remind_min", "label": "Напомнить за (мин)", "type": "number", "default": 0, "min": 0, "max": 1440, "step": 5},
            ]},
            {"type": "kv", "target": "create_result", "fields": [
                {"label": "Результат", "path": "message"},
            ]},
        ],
    })
