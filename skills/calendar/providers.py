"""Provider adapters: Yandex CalDAV (live) and the resolver both processes share.

Each adapter knows ONE calendar per call and exposes exactly:
``list_calendars() -> [calendar dict]``, ``fetch(calendar, cursor) -> (events, cursor, kind)``,
``create(calendar, event, payload) -> {external_id, href, etag}``,
``update(calendar, event, expected_etag, payload) -> {etag}``,
``delete(calendar, event, expected_etag)``, ``respond(calendar, event, payload) -> {etag}``.
Recurrence scopes, linked copies and leases are ``ops.py``'s job, never the adapter's.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from model import (
    AVAIL_BUSY, PROVIDER_GOOGLE, PROVIDER_LOCAL, PROVIDER_YANDEX, account_id as make_account_id, calendar_id as make_calendar_id,
    get_tz, iso_utc, now_utc, parse_stored,
)
from ops import ProviderError

YANDEX_BASE = "https://caldav.yandex.ru"
HTTP_TIMEOUT = 20
USER_AGENT = "Ouroboros-Calendar/1.0"
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/"}
SYNC_WINDOW_PAST_DAYS = 30
SYNC_WINDOW_FUTURE_DAYS = 400


# ── secrets → accounts ──────────────────────────────────────────────

def parse_yandex_accounts(raw: Any) -> Tuple[List[Dict[str, str]], str]:
    """``YANDEX_CALDAV_ACCOUNTS`` is a JSON list of {login, app_password, alias?} (30 A)."""
    text = str(raw or "").strip()
    if not text:
        return [], ""
    try:
        data = json.loads(text)
    except ValueError:
        return [], "YANDEX_CALDAV_ACCOUNTS должен быть JSON-списком вида [{\"login\": \"…@yandex.ru\", \"app_password\": \"…\", \"alias\": \"личный\"}]"
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return [], "YANDEX_CALDAV_ACCOUNTS: ожидается список"
    out: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        login = str(item.get("login") or item.get("email") or item.get("user") or "").strip()
        pwd = str(item.get("app_password") or item.get("password") or "")
        if not login or not pwd:
            continue
        alias = str(item.get("alias") or login.split("@")[0]).strip()
        out.append({"login": login, "app_password": pwd, "alias": alias})
    if not out:
        return [], "YANDEX_CALDAV_ACCOUNTS: ни одной записи с login и app_password"
    return out, ""


class Providers:
    """Resolve an adapter for an account id from the secrets both processes were given."""

    def __init__(self, secrets: Dict[str, Any], state_dir: str):
        self.secrets = dict(secrets or {})
        self.state_dir = state_dir
        self.yandex_accounts, self.yandex_error = parse_yandex_accounts(self.secrets.get("YANDEX_CALDAV_ACCOUNTS"))
        self._cache: Dict[str, Any] = {}

    def adapter_for(self, account_id: str):
        if account_id in self._cache:
            return self._cache[account_id]
        adapter = None
        if account_id.startswith(PROVIDER_YANDEX + ":"):
            login = account_id.split(":", 1)[1]
            for acc in self.yandex_accounts:
                if acc["login"] == login:
                    adapter = YandexAdapter(login, acc["app_password"])
                    break
        elif account_id.startswith(PROVIDER_GOOGLE + ":"):
            try:
                from providers_google import GoogleAdapter  # phase 1
                adapter = GoogleAdapter.for_account(self.secrets, self.state_dir, account_id.split(":", 1)[1])
            except Exception:
                adapter = None
        self._cache[account_id] = adapter
        return adapter


# ── iCalendar helpers (icalendar library) ───────────────────────────

def _ical():
    try:
        import icalendar  # noqa: F401
        return icalendar
    except Exception as exc:  # dependency missing in this process
        raise ProviderError("unsupported", f"библиотека icalendar недоступна: {exc}")


def _as_utc(value: Any, tz_hint) -> Tuple[str, bool]:
    """icalendar decoded value → (stored UTC iso, all_day)."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=tz_hint)
        return iso_utc(dt), False
    if isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time(), tzinfo=tz_hint)
        return iso_utc(dt), True
    raise ProviderError("parse", f"неизвестный тип даты {type(value).__name__}")


def _dt_list(prop: Any) -> List[str]:
    """EXDATE/RDATE (single or list of vDDDLists) → sorted ISO-UTC strings."""
    items = prop if isinstance(prop, list) else [prop]
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        dts = getattr(item, "dts", None) or []
        for d in dts:
            value = getattr(d, "dt", None)
            if isinstance(value, datetime):
                out.append(iso_utc(value if value.tzinfo else value.replace(tzinfo=timezone.utc)))
            elif isinstance(value, date):
                out.append(iso_utc(datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)))
    return sorted(set(out))


def ics_to_rows(ics_text: str, calendar_id: str, href: str, etag: str, default_tz) -> List[Dict[str, Any]]:
    """One .ics resource → master row (+ exception rows with recurrence_id). Unknown fields ride in raw_payload."""
    icalendar = _ical()
    try:
        cal = icalendar.Calendar.from_ical(ics_text)
    except Exception as exc:
        raise ProviderError("parse", f"не удалось разобрать VCALENDAR: {exc}")
    rows: List[Dict[str, Any]] = []
    master: Optional[Dict[str, Any]] = None
    exceptions: List[Dict[str, Any]] = []
    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID") or "")
        dtstart = comp.decoded("DTSTART", None)
        if dtstart is None:
            continue
        tz_hint = getattr(dtstart, "tzinfo", None) or default_tz
        start_utc, all_day = _as_utc(dtstart, tz_hint)
        dtend = comp.decoded("DTEND", None)
        if dtend is None:
            dur = comp.decoded("DURATION", None)
            if isinstance(dur, timedelta):
                dtend = dtstart + dur
            else:
                dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))
        end_utc, _ = _as_utc(dtend, tz_hint)
        tz_name = getattr(getattr(dtstart, "tzinfo", None), "key", "") or ""
        rrule = comp.get("RRULE")
        rrule_text = rrule.to_ical().decode() if rrule is not None else ""
        rec_id = comp.decoded("RECURRENCE-ID", None)
        rec_key = _as_utc(rec_id, tz_hint)[0] if rec_id is not None else ""
        attendees = []
        for att in _listify(comp.get("ATTENDEE")):
            params = getattr(att, "params", {}) or {}
            attendees.append({"email": str(att).replace("mailto:", "").replace("MAILTO:", ""), "name": str(params.get("CN") or ""),
                              "status": str(params.get("PARTSTAT") or "").lower(), "role": str(params.get("ROLE") or "").lower()})
        organizer = str(comp.get("ORGANIZER") or "").replace("mailto:", "").replace("MAILTO:", "")
        reminders: List[int] = []
        for alarm in comp.walk("VALARM"):
            trig = alarm.decoded("TRIGGER", None)
            if isinstance(trig, timedelta):
                reminders.append(int(-trig.total_seconds() // 60))
        status = str(comp.get("STATUS") or "CONFIRMED").lower()
        row = {
            "calendar_id": calendar_id, "uid": uid, "external_id": uid, "href": href, "etag": etag,
            "title": str(comp.get("SUMMARY") or ""), "description": str(comp.get("DESCRIPTION") or ""),
            "location": str(comp.get("LOCATION") or ""), "start_utc": start_utc, "end_utc": end_utc, "tz": tz_name,
            "all_day": all_day, "rrule": rrule_text, "exdates": ",".join(_dt_list(comp.get("EXDATE"))) if comp.get("EXDATE") else "",
            "rdates": ",".join(_dt_list(comp.get("RDATE"))) if comp.get("RDATE") else "",
            "recurrence_id": rec_key, "status": "cancelled" if status == "cancelled" else "confirmed",
            "organizer": organizer, "attendees_json": json.dumps(attendees, ensure_ascii=False),
            "reminders_json": json.dumps(sorted(set(reminders))), "origin": "external", "availability": AVAIL_BUSY,
            "sync_state": "synced", "raw_payload": ics_text if rec_key == "" else "",
        }
        if rec_key:
            exceptions.append(row)
        else:
            master = row
    if master is not None:
        rows.append(master)
    rows.extend(exceptions)
    return rows


def _listify(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def row_to_ics(event: Dict[str, Any], prodid: str = "-//Ouroboros//calendar//RU") -> str:
    """Build VCALENDAR from a row; when the row carries the provider's original text, edit it in place."""
    icalendar = _ical()
    start = parse_stored(event.get("start_utc"))
    end = parse_stored(event.get("end_utc"))
    if start is None or end is None:
        raise ProviderError("parse", "у события нет времени начала/конца")
    tz = get_tz(event.get("tz") or "")
    all_day = bool(event.get("all_day"))
    cal = None
    vevent = None
    raw = str(event.get("raw_payload") or "")
    if raw:
        try:
            cal = icalendar.Calendar.from_ical(raw)
            for comp in cal.walk("VEVENT"):
                if comp.get("RECURRENCE-ID") is None:
                    vevent = comp
                    break
        except Exception:
            cal, vevent = None, None
    if cal is None or vevent is None:
        cal = icalendar.Calendar()
        cal.add("PRODID", prodid)
        cal.add("VERSION", "2.0")
        vevent = icalendar.Event()
        cal.add_component(vevent)
    for key in ("DTSTART", "DTEND", "DURATION", "SUMMARY", "DESCRIPTION", "LOCATION", "RRULE", "STATUS", "LAST-MODIFIED", "DTSTAMP", "SEQUENCE", "EXDATE"):
        if key in vevent:
            del vevent[key]
    uid = str(event.get("uid") or event.get("external_id") or "")
    if "UID" not in vevent and uid:
        vevent.add("UID", uid)
    now = now_utc()
    if all_day:
        vevent.add("DTSTART", start.astimezone(tz).date())
        vevent.add("DTEND", end.astimezone(tz).date())
    elif event.get("rrule"):
        vevent.add("DTSTART", start.astimezone(tz))
        vevent.add("DTEND", end.astimezone(tz))
    else:
        vevent.add("DTSTART", start.astimezone(timezone.utc))
        vevent.add("DTEND", end.astimezone(timezone.utc))
    vevent.add("SUMMARY", str(event.get("title") or ""))
    if event.get("description"):
        vevent.add("DESCRIPTION", str(event["description"]))
    if event.get("location"):
        vevent.add("LOCATION", str(event["location"]))
    if event.get("rrule"):
        vevent.add("RRULE", icalendar.vRecur.from_ical(str(event["rrule"])))
    if event.get("exdates"):
        ex = [parse_stored(x) for x in str(event["exdates"]).split(",") if x]
        ex = [x.astimezone(tz) for x in ex if x]
        if ex:
            vevent.add("EXDATE", ex)
    vevent.add("STATUS", "CANCELLED" if str(event.get("status") or "") == "cancelled" else "CONFIRMED")
    vevent.add("DTSTAMP", now)
    vevent.add("LAST-MODIFIED", now)
    try:
        seq = int(vevent.get("SEQUENCE", 0)) + 1
    except (TypeError, ValueError):
        seq = 1
    vevent.add("SEQUENCE", seq)
    for alarm in list(vevent.walk("VALARM")):
        if alarm is not vevent:
            vevent.subcomponents.remove(alarm)
    try:
        offsets = json.loads(event.get("reminders_json") or "[]")
    except ValueError:
        offsets = []
    for minutes in offsets:
        alarm = icalendar.Alarm()
        alarm.add("ACTION", "DISPLAY")
        alarm.add("DESCRIPTION", str(event.get("title") or ""))
        alarm.add("TRIGGER", timedelta(minutes=-int(minutes)))
        vevent.add_component(alarm)
    if hasattr(cal, "add_missing_timezones"):
        try:
            cal.add_missing_timezones()
        except Exception:
            pass
    return cal.to_ical().decode("utf-8")


# ── Yandex CalDAV ───────────────────────────────────────────────────

class YandexAdapter:
    """Thin CalDAV client for caldav.yandex.ru (principal/home derived from the login, no discovery)."""

    provider = PROVIDER_YANDEX

    def __init__(self, login: str, app_password: str):
        self.login = login.strip()
        self.account_id = make_account_id(PROVIDER_YANDEX, self.login)
        self.home = f"{YANDEX_BASE}/calendars/{urllib.parse.quote(self.login, safe='')}/"
        token = base64.b64encode(f"{self.login}:{app_password}".encode("utf-8")).decode("ascii")
        self._auth = f"Basic {token}"

    # transport
    def _request(self, method: str, url: str, body: Optional[str] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], str]:
        if not url.startswith(YANDEX_BASE):
            raise ProviderError("network", f"адрес вне caldav.yandex.ru: {url}")
        req = urllib.request.Request(url, data=body.encode("utf-8") if body is not None else None, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code == 401:
                raise ProviderError("auth", "Яндекс не принял логин или пароль приложения (401); новый пароль может активироваться до 2–3 часов", 401)
            if exc.code == 403:
                raise ProviderError("forbidden", "Яндекс отказал в доступе (403)", 403)
            if exc.code == 404:
                raise ProviderError("not_found", "ресурс не найден на сервере Яндекса (404)", 404)
            if exc.code == 412:
                raise ProviderError("conflict", "событие изменилось на сервере Яндекса (412)", 412)
            if exc.code in (405, 501):
                raise ProviderError("unsupported", f"метод не поддерживается сервером Яндекса ({exc.code})", exc.code)
            if exc.code in (500, 502, 503, 504, 507):
                raise ProviderError("server", f"сервер Яндекса временно не отвечает ({exc.code})", exc.code)
            raise ProviderError("http", f"HTTP {exc.code} от Яндекса: {text[:200]}", exc.code)
        except urllib.error.URLError as exc:
            raise ProviderError("network", f"нет связи с caldav.yandex.ru: {exc.reason}")
        except TimeoutError:
            raise ProviderError("network", "таймаут при обращении к caldav.yandex.ru")

    @staticmethod
    def _xml(text: str, what: str) -> ET.Element:
        try:
            return ET.fromstring(text)
        except ET.ParseError:
            raise ProviderError("parse", f"не удалось разобрать ответ {what} от Яндекса")

    # capabilities
    def probe(self) -> Dict[str, Any]:
        """Facts the sync loop adapts to: sync-collection / ctag support, write privileges."""
        body = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
                '<d:prop><d:supported-report-set/><d:sync-token/><cs:getctag/><d:current-user-privilege-set/></d:prop></d:propfind>')
        _, _, text = self._request("PROPFIND", self.home, body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        root = self._xml(text, "PROPFIND")
        facts = {"sync_collection": False, "ctag": False, "checked_at": iso_utc(now_utc())}
        for resp in root.findall("d:response", NS):
            for prop in resp.findall("d:propstat/d:prop", NS):
                if prop.find("d:sync-token", NS) is not None and (prop.findtext("d:sync-token", default="", namespaces=NS) or "").strip():
                    facts["sync_collection"] = True
                if prop.find("cs:getctag", NS) is not None and (prop.findtext("cs:getctag", default="", namespaces=NS) or "").strip():
                    facts["ctag"] = True
                reports = prop.find("d:supported-report-set", NS)
                if reports is not None and any(r.find("d:report/d:sync-collection", NS) is not None for r in reports.findall("d:supported-report", NS)):
                    facts["sync_collection"] = True
        return facts

    # calendars
    def list_calendars(self) -> List[Dict[str, Any]]:
        body = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/">'
                '<d:prop><d:displayname/><d:resourcetype/><d:current-user-privilege-set/><cs:getctag/><d:sync-token/></d:prop></d:propfind>')
        _, _, text = self._request("PROPFIND", self.home, body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        root = self._xml(text, "PROPFIND")
        out: List[Dict[str, Any]] = []
        for resp in root.findall("d:response", NS):
            href = (resp.findtext("d:href", default="", namespaces=NS) or "").strip()
            if not href:
                continue
            props = resp.findall("d:propstat/d:prop", NS)
            if not any(p.find("d:resourcetype/c:calendar", NS) is not None for p in props):
                continue
            collection = href.rstrip("/").rsplit("/", 1)[-1]
            if not collection.startswith("events-"):
                continue
            display, writable, ctag, sync_token = "", False, "", ""
            for p in props:
                display = (p.findtext("d:displayname", default="", namespaces=NS) or display).strip()
                ctag = (p.findtext("cs:getctag", default="", namespaces=NS) or ctag).strip()
                sync_token = (p.findtext("d:sync-token", default="", namespaces=NS) or sync_token).strip()
                privs = p.find("d:current-user-privilege-set", NS)
                if privs is not None:
                    writable = writable or any(pr.find("d:write", NS) is not None or pr.find("d:write-content", NS) is not None
                                               for pr in privs.findall("d:privilege", NS))
            full = href if href.startswith("http") else YANDEX_BASE + href
            out.append({"id": make_calendar_id(PROVIDER_YANDEX, self.login, collection), "account_id": self.account_id,
                        "provider": PROVIDER_YANDEX, "external_id": collection, "href": full, "name": display or collection,
                        "writable": writable, "access_role": "owner" if writable else "reader", "ctag": ctag, "sync_token": sync_token})
        return out

    # events
    def fetch(self, calendar: Dict[str, Any], cursor: str = "", tz=None) -> Tuple[List[Dict[str, Any]], str, str, List[str]]:
        """Window read (calendar-query). Returns (rows, cursor, cursor_kind, hrefs_seen)."""
        tz = tz or timezone.utc
        start = now_utc() - timedelta(days=SYNC_WINDOW_PAST_DAYS)
        end = now_utc() + timedelta(days=SYNC_WINDOW_FUTURE_DAYS)
        body = ('<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                f'<c:time-range start="{start:%Y%m%dT%H%M%SZ}" end="{end:%Y%m%dT%H%M%SZ}"/>'
                '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
        _, _, text = self._request("REPORT", calendar["href"], body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        root = self._xml(text, "REPORT")
        rows: List[Dict[str, Any]] = []
        hrefs: List[str] = []
        for resp in root.findall("d:response", NS):
            href = (resp.findtext("d:href", default="", namespaces=NS) or "").strip()
            etag, data = "", ""
            for p in resp.findall("d:propstat/d:prop", NS):
                etag = (p.findtext("d:getetag", default="", namespaces=NS) or etag).strip()
                data = p.findtext("c:calendar-data", default="", namespaces=NS) or data
            if not data:
                continue
            full = href if href.startswith("http") else YANDEX_BASE + href
            hrefs.append(full)
            try:
                rows.extend(ics_to_rows(data, calendar["id"], full, etag, tz))
            except ProviderError:
                continue
        # cursor = ctag when the server exposes it; the window itself is the fallback cursor kind.
        return rows, "", "window", hrefs

    def get(self, href: str) -> Tuple[str, str]:
        _, headers, text = self._request("GET", href)
        return text, headers.get("etag", "")

    def exists(self, href: str) -> bool:
        try:
            self._request("HEAD", href)
            return True
        except ProviderError as exc:
            if exc.kind == "not_found":
                return False
            raise

    def create(self, calendar: Dict[str, Any], event: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        uid = str(event.get("uid") or "")
        if not uid:
            raise ProviderError("parse", "у события нет uid")
        href = calendar["href"].rstrip("/") + "/" + urllib.parse.quote(uid, safe="") + ".ics"
        ics = row_to_ics({**event, "raw_payload": ""})
        try:
            _, headers, _ = self._request("PUT", href, ics, {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        except ProviderError as exc:
            if exc.kind == "conflict":
                # A retry after a lost response: the resource already exists — read it back instead of duplicating.
                _, etag = self.get(href)
                return {"external_id": uid, "href": href, "etag": etag}
            raise
        etag = headers.get("etag", "")
        if not etag:
            try:
                _, etag = self.get(href)
            except ProviderError:
                etag = ""
        return {"external_id": uid, "href": href, "etag": etag}

    def update(self, calendar: Dict[str, Any], event: Dict[str, Any], expected_etag: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        href = str(event.get("href") or "")
        if not href:
            return self.create(calendar, event, payload)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if expected_etag:
            headers["If-Match"] = expected_etag
        _, resp_headers, _ = self._request("PUT", href, row_to_ics(event), headers)
        etag = resp_headers.get("etag", "")
        if not etag:
            try:
                _, etag = self.get(href)
            except ProviderError:
                etag = ""
        return {"etag": etag}

    def delete(self, calendar: Dict[str, Any], event: Dict[str, Any], expected_etag: str) -> None:
        href = str(event.get("href") or "")
        if not href:
            return
        self._request("DELETE", href, None, {"If-Match": expected_etag} if expected_etag else {})

    def respond(self, calendar: Dict[str, Any], event: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        raise ProviderError("unsupported", "ответ на приглашение в Яндексе будет включён после проверки поведения сервера (этап 1)")
