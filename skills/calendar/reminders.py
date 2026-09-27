"""Reminders without a model (29 A / 35 A).

Defaults live in ``settings['reminder_defaults']``; per-event offsets live in
``events.reminders_json``; the ``reminders`` table is only the delivery queue
(``notice_id``, state). The companion plans the next 24 hours, re-reads the
event before firing, posts to the host's ``POST /notify`` when the host
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
MAX_NOTICE_CHARS = 1000                     # Host Service owner notification contract
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
        end = parse_stored(occ.get("end_utc")) or (start + timedelta(hours=1))
        for offset in offsets_for(occ, rules, modes):
            fire_at = start - timedelta(minutes=offset)
            if fire_at > now + PLAN_HORIZON:
                continue
            if fire_at < now and end < now:
                continue   # already over: nothing to catch up (21 A: only what is still upcoming or running)
            if store.schedule_reminder(event_id, occ_start, offset, iso_utc(fire_at), notice_id_for(event_id, occ_start, offset)):
                scheduled += 1
    return scheduled


# ── delivery ────────────────────────────────────────────────────────

try:  # the host's credential wrapper when the companion can import it; the same contract otherwise
    from ouroboros.skill_token import SkillToken as _SkillToken  # type: ignore
except Exception:  # pragma: no cover - companion env without the core package
    class _SkillToken:
        """Refuses accidental stringification; the value is revealed only at the request-construction site."""

        def __init__(self, value: str):
            token = str(value or "").strip()
            if not token:
                raise ValueError("SkillToken cannot be empty")
            self._value = token

        def use_in_request(self) -> str:
            return self._value

        def __repr__(self) -> str:
            return "<SkillToken redacted>"

        __str__ = __repr__


class NotifyChannel:
    """Loopback Host Service ``POST /notify``; feature-detected via ``GET /identity``."""

    def __init__(self, base_url: str = "", token: str = ""):
        self.base_url = (base_url or os.environ.get("HOST_SERVICE_URL") or "").rstrip("/")
        raw = token or os.environ.get("HOST_SERVICE_TOKEN") or ""
        self._token = _SkillToken(raw) if raw else None
        self._state: Optional[str] = None   # ready | no_route | no_grant | unreachable

    def state(self, refresh: bool = False) -> str:
        if self._state and not refresh:
            return self._state
        if not self.base_url or self._token is None:
            self._state = "unreachable"
            return self._state
        try:
            data = self._request("GET", "/identity")
        except _HttpError as exc:
            # /identity needs no grant: a 403 here means the host rejected the skill token (disabled / stale review)
            self._state = "token_rejected" if exc.status == 403 else "unreachable"
            return self._state
        except Exception:
            self._state = "unreachable"
            return self._state
        self._state = "ready" if int((data or {}).get("notify_version") or 0) >= 1 else "no_route"
        return self._state

    def send(self, notice_id: str, text: str) -> Tuple[str, str]:
        """→ (state, detail): sent | no_route | no_grant | retry | unknown | failed.

        A lost or malformed response is *unknown*, not a safe retry: the host
        may have appended the notification before the connection was lost.
        """
        state = self.state()
        if state == "no_grant":
            return "no_grant", "notify_owner grant missing"
        if state in ("no_route", "token_rejected", "unreachable"):
            return "no_route", state        # kept in the queue as no_channel until the route / the token / the host comes back
        try:
            data = self._request("POST", "/notify", {"key": notice_id, "text": text})
        except _HttpError as exc:
            if exc.status == 403:
                self._state = "no_grant"
                return "no_grant", exc.body[:200]
            if exc.status in (404, 405):
                self._state = "no_route"
                return "no_route", exc.body[:200]
            if exc.status in (429, 503):
                return "retry", f"HTTP {exc.status}"
            if exc.status == 408 or (exc.status >= 500 and exc.status != 503):
                self._state = "unreachable"
                return "unknown", f"HTTP {exc.status}: host outcome unconfirmed"
            return "failed", f"HTTP {exc.status}: {exc.body[:200]}"
        except urllib.error.URLError as exc:
            self._state = "unreachable"  # leave later due reminders queued until the host recovers
            if isinstance(exc.reason, ConnectionRefusedError):
                return "no_route", "host refused connection before request delivery"
            return "unknown", f"{type(exc).__name__}: host outcome unconfirmed"
        except Exception as exc:
            self._state = "unreachable"
            return "unknown", f"{type(exc).__name__}: host outcome unconfirmed"
        if isinstance(data, dict) and data.get("ok") is True and data.get("ts") and data.get("chat_id"):
            return "sent", "host accepted (browser/Telegram delivery unconfirmed)"
        return "unknown", "host response did not confirm acceptance"

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None)
        req.add_header("X-Skill-Token", self._token.use_in_request() if self._token is not None else "")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise _HttpError(exc.code, exc.read().decode("utf-8", "replace") if exc.fp else "")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}  # malformed success response has unknown effect; send() never retries it


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
    parts = [f"⏰ {head} · {str(event.get('title') or 'Событие')[:120]} ({soon})"]
    if event.get("calendar_name"):
        parts.append(str(event["calendar_name"])[:120])
    if event.get("location"):
        parts.append(str(event["location"])[:80])
    return " · ".join(parts)


def _send_notice(store, channel: NotifyChannel, reminder_ids: List[str], key: str, text: str) -> Optional[Tuple[str, str]]:
    # A known unavailable channel cannot have accepted a POST; keep those rows
    # retryable rather than reserving them as uncertain before the no-op send.
    if isinstance(channel, NotifyChannel) and channel.state() != "ready":
        return channel.send(key, text)
    if not store.reserve_reminder_send(reminder_ids):
        return None  # a concurrent calendar edit removed this pending reminder
    return channel.send(key, text)


def deliver_due(store, channel: NotifyChannel, tz, now: Optional[datetime] = None) -> Dict[str, int]:
    """Fire what is due: fresh ones individually, a downtime backlog as one merged notice.

    Before sending, the occurrence is re-read: a cancelled/moved/finished one is skipped (21 A); an occurrence
    that vanished from its series (EXDATE, truncation) is skipped too. Rows left in ``no_channel`` are retried
    on every pass so a channel that appears later still delivers what is still relevant.
    """
    now = now or now_utc()
    due = store.due_reminders(now)
    stats = {"sent": 0, "skipped": 0, "no_channel": 0, "retry": 0, "unknown": 0, "failed": 0, "batched": 0}
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
        verdict, live = _occurrence_state(store, event, start, now, owner_tz=tz)
        if verdict:
            store.mark_reminder(rem["id"], "skipped", verdict)
            stats["skipped"] += 1
            continue
        (stale if fire_at and now - fire_at > CATCHUP_GRACE else fresh).append((rem, live))
    for rem, event in fresh:
        text = format_text(event, rem["occurrence_start_utc"], int(rem["offset_min"]), tz)
        outcome = _send_notice(store, channel, [rem["id"]], rem["notice_id"], text)
        if outcome is None:
            continue
        state, detail = outcome
        _record(store, rem["id"], state, detail, stats)
    if stale:
        header = "⏰ Пока Уроборос не работал, подошли напоминания:"
        batch: List[Tuple[Dict[str, Any], str]] = []
        size = len(header)
        for rem, event in stale:
            line = format_text(event, rem["occurrence_start_utc"], int(rem["offset_min"]), tz)
            if batch and size + 1 + len(line) > MAX_NOTICE_CHARS:
                _send_batch(store, channel, header, batch, stats)
                batch, size = [], len(header)
            batch.append((rem, line))
            size += 1 + len(line)
        if batch:
            _send_batch(store, channel, header, batch, stats)
    return stats


def _send_batch(store, channel: NotifyChannel, header: str, batch: List[Tuple[Dict[str, Any], str]], stats: Dict[str, int]) -> None:
    """Fit complete reminder lines, never mark an undisclosed item as sent."""
    text = header + "\n" + "\n".join(line for _, line in batch)
    outcome = _send_notice(store, channel, [rem["id"] for rem, _ in batch],
                           batch_notice_id([rem["notice_id"] for rem, _ in batch]), text)
    if outcome is None:
        return
    state, detail = outcome
    for rem, _ in batch:
        _record(store, rem["id"], state, detail, stats)
    stats["batched"] += len(batch)


def _occurrence_state(store, event: Dict[str, Any], start: Optional[datetime], now: datetime, owner_tz=None) -> Tuple[str, Dict[str, Any]]:
    """('', effective occurrence row) when the planned occurrence is still on; otherwise (reason to skip it, row).

    The effective row is the exception living at this slot when there is one (its own title/end), else the master."""
    if start is None:
        return "нет времени вхождения", event
    m_start, m_end = parse_stored(event.get("start_utc")), parse_stored(event.get("end_utc"))
    duration = (m_end - m_start) if (m_start and m_end and m_end > m_start) else timedelta(hours=1)
    if not event.get("rrule"):
        if m_start and m_start != start:
            return "событие перенесено; напоминание перепланировано", event
        if m_end and m_end < now:
            return "событие уже закончилось", event
        return "", event
    key = iso_utc(start)
    live_row: Dict[str, Any] = event
    end_at = start + duration
    for exc in store.exceptions_for(event["id"]):
        if str(exc.get("recurrence_id") or "") == key:
            if str(exc.get("status") or "") == "cancelled" or exc.get("deleted_at"):
                return "вхождение отменено", event
            if str(exc.get("start_utc") or "") != key:
                return "вхождение перенесено; напоминание перепланировано", event
            live_row = {**event, **{k: exc.get(k) for k in ("title", "description", "location", "end_utc") if exc.get(k) is not None}}
            end_at = parse_stored(exc.get("end_utc")) or end_at
            break
        if str(exc.get("start_utc") or "") == key:
            live_row = {**event, **{k: exc.get(k) for k in ("title", "description", "location", "end_utc") if exc.get(k) is not None}}
            end_at = parse_stored(exc.get("end_utc")) or end_at
            break  # a moved exception now living at this slot: it is the live occurrence
    else:
        try:
            import ops as _ops
            live = _ops.expand([event], start - timedelta(minutes=1), start + timedelta(minutes=1), store.exceptions_for, owner_tz=owner_tz)
        except Exception:
            live = [{"start_utc": key}]
        if not any(str(o.get("start_utc") or "") == key for o in live):
            return "вхождения больше нет в расписании", event
    if end_at < now:
        return "вхождение уже закончилось", live_row
    return "", live_row


def _record(store, reminder_id: str, state: str, detail: str, stats: Dict[str, int]) -> None:
    if state in ("sent", "duplicate"):
        store.mark_reminder(reminder_id, "sent", detail)
        stats["sent"] += 1
    elif state in ("no_route", "no_grant"):
        store.mark_reminder(reminder_id, "no_channel", detail or state)
        stats["no_channel"] += 1
    elif state == "retry":
        attempts = store.bump_reminder_attempt(reminder_id, detail)
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            store.mark_reminder(reminder_id, "failed", f"доставка не удалась после {attempts} попыток: {detail}")
            stats["failed"] += 1
        else:
            store.mark_reminder(reminder_id, "scheduled", detail)
            stats["retry"] += 1      # stays in the queue; next tick retries
    elif state == "unknown":
        store.mark_reminder(reminder_id, "unknown", detail)
        stats["unknown"] += 1  # terminal for automation, not evidence of delivery failure
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
