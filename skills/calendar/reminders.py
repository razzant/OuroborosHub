"""Reminders without a model (29 A / 35 A).

Defaults live in ``settings['reminder_defaults']``; per-event offsets live in
``events.reminders_json``; the ``reminders`` table is only the delivery queue
(``notice_id``, state). The companion plans the next 24 hours, re-reads the
event before firing, posts to the host's ``POST /chat/notify`` when the host
advertises ``notify_version``, and otherwise records ``no_channel`` honestly.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from model import get_tz, iso_local, iso_utc, now_utc, parse_stored

PLAN_HORIZON = timedelta(hours=24)
CATCHUP_GRACE = timedelta(minutes=10)      # a reminder older than this after downtime is batched, not sent alone
MAX_DELIVERY_ATTEMPTS = 5
NOTICE_MAX = 128
DEFAULT_RULES = {"default": [], "by_calendar": {}, "hidden": []}   # 19 A: reminders only by request or saved rule
MODE_KEY = "reminder_mode"                                          # 20 A: «напоминает Уроборос» is explicit per external calendar


# ── rules ───────────────────────────────────────────────────────────

def get_rules(store) -> Dict[str, Any]:
    rules = store.get_setting("reminder_defaults") or {}
    out = dict(DEFAULT_RULES)
    out.update({k: v for k, v in rules.items() if k in out})
    out["default"] = _offsets(out.get("default"))
    out["hidden"] = _offsets(out.get("hidden"))
    out["by_calendar"] = {str(k): _offsets(v) for k, v in (out.get("by_calendar") or {}).items()}
    return out


def set_rules(store, default: Optional[List[int]] = None, hidden: Optional[List[int]] = None,
              calendar_id: str = "", calendar_offsets: Optional[List[int]] = None) -> Dict[str, Any]:
    rules = get_rules(store)
    if default is not None:
        rules["default"] = _offsets(default)
    if hidden is not None:
        rules["hidden"] = _offsets(hidden)
    if calendar_id:
        if calendar_offsets is None:
            rules["by_calendar"].pop(calendar_id, None)
        else:
            rules["by_calendar"][calendar_id] = _offsets(calendar_offsets)
    store.set_setting("reminder_defaults", rules)
    return rules


def _offsets(value: Any) -> List[int]:
    if value is None:
        return []
    if isinstance(value, (int, float, str)):
        value = [value]
    out = set()
    for item in value:
        try:
            minutes = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= minutes <= 7 * 24 * 60:
            out.add(minutes)
    return sorted(out)


def mode_on(store, calendar_id: str) -> bool:
    modes = store.get_setting(MODE_KEY) or {}
    return bool(modes.get(str(calendar_id)))


def set_mode(store, calendar_id: str, on: bool) -> Dict[str, bool]:
    modes = store.get_setting(MODE_KEY) or {}
    if on:
        modes[str(calendar_id)] = True
    else:
        modes.pop(str(calendar_id), None)
    store.set_setting(MODE_KEY, modes)
    return modes


def offsets_for(event: Dict[str, Any], rules: Dict[str, Any], modes: Optional[Dict[str, bool]] = None) -> List[int]:
    """Per-event offsets win; otherwise the calendar rule; otherwise the default (hidden events: the hidden rule).

    An external calendar (Yandex/Google) gets Ouroboros reminders only when the owner switched
    «напоминает Уроборос» on for it; until then the provider's own alerts stay in charge (20 A).
    """
    provider = str(event.get("provider") or "local")
    if provider != "local" and not (modes or {}).get(str(event.get("calendar_id"))):
        return []
    try:
        own = json.loads(event.get("reminders_json") or "[]")
    except ValueError:
        own = []
    if own:
        return _offsets(own)
    if not event.get("is_primary", 1):
        return []
    if event.get("visibility") == "hidden":
        return list(rules.get("hidden") or [])
    return list(rules["by_calendar"].get(str(event.get("calendar_id")), rules.get("default") or []))


# ── planning ────────────────────────────────────────────────────────

def notice_id_for(event_id: str, occurrence_start_utc: str, offset_min: int) -> str:
    raw = f"cal:{event_id}:{occurrence_start_utc}:{offset_min}m"
    if len(raw) <= NOTICE_MAX:
        return raw
    return "cal:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def batch_notice_id(notice_ids: List[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(notice_ids)).encode("utf-8")).hexdigest()[:16]
    return f"cal:batch:{digest}"


def plan(store, occurrences_in: Any, now: Optional[datetime] = None) -> int:
    """Schedule reminder rows for occurrences starting within the horizon. Idempotent by notice_id."""
    now = now or now_utc()
    rules = get_rules(store)
    modes = store.get_setting(MODE_KEY) or {}
    horizon_end = now + PLAN_HORIZON + timedelta(days=7)   # offsets up to 7 days look further ahead
    scheduled = 0
    for occ in occurrences_in(now - timedelta(hours=1), horizon_end):
        if str(occ.get("status") or "") == "cancelled":
            continue
        start = parse_stored(occ.get("start_utc"))
        if start is None:
            continue
        event_id = occ.get("series_id") or occ.get("id")
        occ_start = occ.get("start_utc")   # effective start: a moved exception is a different reminder than the original slot
        for offset in offsets_for(occ, rules, modes):
            fire_at = start - timedelta(minutes=offset)
            if fire_at < now - CATCHUP_GRACE * 6 or fire_at > now + PLAN_HORIZON:
                continue
            if store.schedule_reminder(event_id, occ_start, offset, iso_utc(fire_at), notice_id_for(event_id, occ_start, offset)):
                scheduled += 1
    return scheduled


# ── delivery ────────────────────────────────────────────────────────

class NotifyChannel:
    """Loopback Host Service ``POST /chat/notify``; feature-detected via ``GET /identity``."""

    def __init__(self, base_url: str = "", token: str = ""):
        self.base_url = (base_url or os.environ.get("HOST_SERVICE_URL") or "").rstrip("/")
        self.token = token or os.environ.get("HOST_SERVICE_TOKEN") or ""
        self._state: Optional[str] = None   # ready | no_route | no_grant | unreachable

    def state(self, refresh: bool = False) -> str:
        if self._state and not refresh:
            return self._state
        if not self.base_url or not self.token:
            self._state = "unreachable"
            return self._state
        try:
            data = self._request("GET", "/identity")
        except _HttpError as exc:
            self._state = "no_grant" if exc.status == 403 else "unreachable"
            return self._state
        except Exception:
            self._state = "unreachable"
            return self._state
        self._state = "ready" if int((data or {}).get("notify_version") or 0) >= 1 else "no_route"
        return self._state

    def send(self, notice_id: str, text: str) -> Tuple[str, str]:
        """→ (state, detail): sent | duplicate | no_route | no_grant | retry | failed."""
        state = self.state()
        if state in ("no_route", "unreachable"):
            return ("no_route" if state == "no_route" else "retry"), state
        try:
            data = self._request("POST", "/chat/notify", {"notice_id": notice_id, "text": text, "markdown": False})
        except _HttpError as exc:
            if exc.status == 403:
                self._state = "no_grant"
                return "no_grant", exc.body[:200]
            if exc.status in (404, 405):
                self._state = "no_route"
                return "no_route", exc.body[:200]
            if exc.status in (429, 503, 502, 504):
                return "retry", f"HTTP {exc.status}"
            if exc.status == 409:
                return "failed", "notice_id conflict"
            return "failed", f"HTTP {exc.status}: {exc.body[:200]}"
        except Exception as exc:
            return "retry", f"{type(exc).__name__}: {exc}"
        status = str((data or {}).get("status") or "accepted")
        return ("duplicate" if status == "duplicate" else "sent"), status

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None)
        req.add_header("X-Skill-Token", self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise _HttpError(exc.code, exc.read().decode("utf-8", "replace") if exc.fp else "")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}


class _HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


def format_text(event: Dict[str, Any], occurrence_start_utc: str, offset: int, tz) -> str:
    start = parse_stored(occurrence_start_utc) or parse_stored(event.get("start_utc"))
    when = start.astimezone(tz) if start else None
    head = f"{when:%H:%M}" if when else ""
    if event.get("all_day"):
        head = f"{when:%d.%m}" if when else ""
    soon = "сейчас" if offset == 0 else f"через {offset} мин"
    parts = [f"⏰ {head} · {event.get('title') or 'Событие'} ({soon})"]
    if event.get("calendar_name"):
        parts.append(str(event["calendar_name"]))
    if event.get("location"):
        parts.append(str(event["location"])[:80])
    return " · ".join(parts)


def deliver_due(store, channel: NotifyChannel, tz, now: Optional[datetime] = None) -> Dict[str, int]:
    """Fire what is due: fresh ones individually, a downtime backlog as one merged notice.

    Before sending, the occurrence is re-read: a cancelled/moved/finished one is skipped (21 A); an occurrence
    that vanished from its series (EXDATE, truncation) is skipped too. Rows left in ``no_channel`` are retried
    on every pass so a channel that appears later still delivers what is still relevant.
    """
    now = now or now_utc()
    due = store.due_reminders(now)
    stats = {"sent": 0, "skipped": 0, "no_channel": 0, "retry": 0, "failed": 0, "batched": 0}
    fresh: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    stale: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for rem in due:
        event = store.get_event(rem["event_id"])
        start = parse_stored(rem.get("occurrence_start_utc"))
        fire_at = parse_stored(rem.get("fire_at_utc"))
        if event is None or event.get("deleted_at") or str(event.get("status") or "") == "cancelled":
            store.mark_reminder(rem["id"], "skipped", "событие отменено или удалено")
            stats["skipped"] += 1
            continue
        verdict = _occurrence_state(store, event, start, now)
        if verdict:
            store.mark_reminder(rem["id"], "skipped", verdict)
            stats["skipped"] += 1
            continue
        (stale if fire_at and now - fire_at > CATCHUP_GRACE else fresh).append((rem, event))
    for rem, event in fresh:
        text = format_text(event, rem["occurrence_start_utc"], int(rem["offset_min"]), tz)
        state, detail = channel.send(rem["notice_id"], text)
        _record(store, rem["id"], state, detail, stats)
    if stale:
        lines = [format_text(ev, r["occurrence_start_utc"], int(r["offset_min"]), tz) for r, ev in stale]
        text = "⏰ Пока Уроборос не работал, подошли напоминания:\n" + "\n".join(lines[:10])
        if len(lines) > 10:
            text += f"\n… и ещё {len(lines) - 10}"
        state, detail = channel.send(batch_notice_id([r["notice_id"] for r, _ in stale]), text)
        for rem, _ in stale:
            _record(store, rem["id"], state, detail, stats)
        stats["batched"] += len(stale)
    return stats


def _occurrence_state(store, event: Dict[str, Any], start: Optional[datetime], now: datetime) -> str:
    """'' when the planned occurrence is still on; otherwise the reason to skip it."""
    if start is None:
        return "нет времени вхождения"
    m_start, m_end = parse_stored(event.get("start_utc")), parse_stored(event.get("end_utc"))
    duration = (m_end - m_start) if (m_start and m_end and m_end > m_start) else timedelta(hours=1)
    if not event.get("rrule"):
        if m_start and m_start != start:
            return "событие перенесено; напоминание перепланировано"
        if m_end and m_end < now:
            return "событие уже закончилось"
        return ""
    key = iso_utc(start)
    for exc in store.exceptions_for(event["id"]):
        if str(exc.get("recurrence_id") or "") == key:
            if str(exc.get("status") or "") == "cancelled" or exc.get("deleted_at"):
                return "вхождение отменено"
            if str(exc.get("start_utc") or "") != key:
                return "вхождение перенесено; напоминание перепланировано"
            break
        if str(exc.get("start_utc") or "") == key:
            break  # a moved exception now living at this slot: it is the live occurrence
    else:
        try:
            import ops as _ops
            live = _ops.expand([event], start - timedelta(minutes=1), start + timedelta(minutes=1), store.exceptions_for)
        except Exception:
            live = [{"start_utc": key}]
        if not any(str(o.get("start_utc") or "") == key for o in live):
            return "вхождения больше нет в расписании"
    if start + duration < now:
        return "вхождение уже закончилось"
    return ""


def _record(store, reminder_id: str, state: str, detail: str, stats: Dict[str, int]) -> None:
    if state in ("sent", "duplicate"):
        store.mark_reminder(reminder_id, "sent", detail)
        stats["sent"] += 1
    elif state in ("no_route", "no_grant"):
        store.mark_reminder(reminder_id, "no_channel", state)
        stats["no_channel"] += 1
    elif state == "retry":
        attempts = store.bump_reminder_attempt(reminder_id, detail)
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            store.mark_reminder(reminder_id, "failed", f"доставка не удалась после {attempts} попыток: {detail}")
            stats["failed"] += 1
        else:
            stats["retry"] += 1      # stays in the queue; next tick retries
    else:
        store.mark_reminder(reminder_id, "failed", detail)
        stats["failed"] += 1


def upcoming(store, tz, now: Optional[datetime] = None, limit: int = 20) -> List[Dict[str, Any]]:
    now = now or now_utc()
    out = []
    for rem in store.upcoming_reminders(now, limit=limit):
        out.append({"event_id": rem["event_id"], "title": rem.get("title") or "", "fire_at": iso_local(rem["fire_at_utc"], tz),
                    "occurrence_start": iso_local(rem["occurrence_start_utc"], tz), "offset_min": rem["offset_min"], "state": rem["state"]})
    return out
