"""The one place that changes events (plan 0.2 §2.2).

Tools and widget routes call these functions; they write the durable rows and
intents first, then attempt the external write immediately (32 A). Adapters
only know one calendar at a time; recurrence scopes, linked copies and leases
live here so Yandex and Google cannot drift apart (roast F11).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from model import (
    AVAIL_BUSY, AVAIL_FREE, AVAIL_SOFT, BUSY_COPY_TITLE, INTENT_CONFLICT, INTENT_DONE, INTENT_FAILED, INTENT_PENDING,
    PROVIDER_LOCAL, PUBLISH_BUSY, PUBLISH_FULL, SCOPE_ALL, SCOPE_FOLLOWING, SCOPE_THIS, VISIBILITY_SHOWN,
    iso_utc, new_id, new_uid, overlaps, parse_stored,
)

RETRY_BACKOFF_SEC = (30, 120, 600, 1800, 7200)
MAX_ATTEMPTS = len(RETRY_BACKOFF_SEC)
LEASE_SECONDS = 90


class ProviderError(Exception):
    """Typed adapter failure. ``kind`` ∈ network|server|auth|forbidden|not_found|conflict|http|parse|unsupported."""

    def __init__(self, kind: str, message: str, status: int = 0):
        super().__init__(message)
        self.kind, self.message, self.status = kind, message, status


def lease_owner() -> str:
    return f"child:{os.getpid()}"


# ── occurrences ─────────────────────────────────────────────────────

def _series_id(occurrence_id: str) -> Tuple[str, str]:
    """``evt_x@2026-09-25T10:00:00+00:00`` → (``evt_x``, occurrence start)."""
    if "@" in occurrence_id:
        base, occ = occurrence_id.split("@", 1)
        return base, occ
    return occurrence_id, ""


def expand(rows: Sequence[Dict[str, Any]], start: datetime, end: datetime, exceptions_for: Callable[[str], List[Dict[str, Any]]],
           owner_tz=None) -> List[Dict[str, Any]]:
    """Turn stored rows into concrete occurrences inside [start, end).

    A local «обычно» (soft) routine follows the owner's CURRENT zone (16 A), everything else keeps its own zone.
    """
    out: List[Dict[str, Any]] = []
    present = {row["id"] for row in rows if row.get("rrule") and not row.get("master_id")}
    for row in rows:
        if row.get("master_id"):
            if row["master_id"] in present or str(row.get("status") or "") == "cancelled":
                continue  # folded into the master's expansion (or a cancelled date)
            # An exception moved into this window while its master lies outside it: show it on its own.
            s, e = parse_stored(row.get("start_utc")), parse_stored(row.get("end_utc"))
            if s and e and overlaps(s, e, start, end):
                item = dict(row)
                item["occurrence_start_utc"] = row.get("recurrence_id") or row.get("start_utc")
                item["series_id"] = row["master_id"]
                out.append(item)
            continue
        if not row.get("rrule"):
            s, e = parse_stored(row.get("start_utc")), parse_stored(row.get("end_utc"))
            if s and e and overlaps(s, e, start, end):
                out.append(dict(row))
            continue
        follow_owner = owner_tz is not None and row.get("availability") == AVAIL_SOFT and row.get("origin", "local") == "local"
        out.extend(_expand_master(row, start, end, exceptions_for(row["id"]), owner_tz if follow_owner else None))
    out.sort(key=lambda r: (r.get("start_utc") or "", r.get("title") or ""))
    return out


def _expand_master(master: Dict[str, Any], start: datetime, end: datetime, exceptions: List[Dict[str, Any]], force_tz=None) -> List[Dict[str, Any]]:
    m_start, m_end = parse_stored(master.get("start_utc")), parse_stored(master.get("end_utc"))
    if m_start is None or m_end is None:
        return []
    duration = m_end - m_start
    try:
        from dateutil.rrule import rrulestr  # transitive dependency of icalendar
    except Exception:
        row = dict(master)
        row["recurrence_unsupported"] = True
        return [row] if overlaps(m_start, m_end, start, end) else []
    try:
        from model import get_tz
        tz = force_tz or get_tz(master.get("tz") or "")
        dtstart = m_start.astimezone(tz)
        if force_tz is not None:
            # keep the wall-clock time of the routine in the owner's current zone
            local_src = m_start.astimezone(get_tz(master.get("tz") or ""))
            dtstart = local_src.replace(tzinfo=tz)
        rule = rrulestr(str(master["rrule"]), dtstart=dtstart, forceset=True)
        for ex in _split_dates(master.get("exdates")):
            rule.exdate(ex.astimezone(tz))
        for rd in _split_dates(master.get("rdates")):
            rule.rdate(rd.astimezone(tz))
    except Exception:
        row = dict(master)
        row["recurrence_unsupported"] = True
        return [row] if overlaps(m_start, m_end, start, end) else []
    by_recurrence = {str(ex.get("recurrence_id") or ""): ex for ex in exceptions}
    visited = set()
    out: List[Dict[str, Any]] = []
    all_day = bool(master.get("all_day"))
    day_span = max(1, (m_end.astimezone(tz).date() - m_start.astimezone(tz).date()).days) if all_day else 0
    window_start = (start - duration).astimezone(tz)
    for occ in rule.between(window_start, end.astimezone(tz), inc=True):
        occ_utc = occ.astimezone(timezone.utc)
        key = iso_utc(occ_utc)
        visited.add(key)
        exc = by_recurrence.get(key)
        if exc is not None:
            if str(exc.get("status") or "") == "cancelled" or exc.get("deleted_at"):
                continue
            row = dict(master)
            row.update({k: v for k, v in exc.items() if v not in (None, "")})
            row["id"] = exc["id"]
        else:
            row = dict(master)
            row["id"] = f"{master['id']}@{key}"
            row["start_utc"] = key
            if all_day:
                # whole calendar days in the event's zone: a DST day is 23 or 25 hours, still one day
                occ_local = occ_utc.astimezone(tz)
                row["end_utc"] = iso_utc(datetime.combine(occ_local.date() + timedelta(days=day_span), datetime.min.time(), tzinfo=tz))
            else:
                row["end_utc"] = iso_utc(occ_utc + duration)
        row["occurrence_start_utc"] = key
        row["series_id"] = master["id"]
        s, e = parse_stored(row["start_utc"]), parse_stored(row["end_utc"])
        if s and e and overlaps(s, e, start, end):
            out.append(row)
        if len(out) > 1000:
            break
    # Exceptions moved INTO the window from an occurrence date outside it.
    for key, exc in by_recurrence.items():
        if key in visited or str(exc.get("status") or "") == "cancelled" or exc.get("deleted_at"):
            continue
        s, e = parse_stored(exc.get("start_utc")), parse_stored(exc.get("end_utc"))
        if s and e and overlaps(s, e, start, end):
            row = dict(master)
            row.update({k: v for k, v in exc.items() if v not in (None, "")})
            row["id"] = exc["id"]
            row["occurrence_start_utc"] = key
            row["series_id"] = master["id"]
            out.append(row)
    return out


def _split_dates(text: Any) -> List[datetime]:
    out: List[datetime] = []
    for part in str(text or "").split(","):
        dt = parse_stored(part.strip())
        if dt:
            out.append(dt)
    return out


# ── free/busy ───────────────────────────────────────────────────────

def busy_intervals(occurrences: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[datetime, datetime]], List[Tuple[datetime, datetime, str]]]:
    """(hard busy intervals, soft «обычно» intervals with titles); linked copies counted once."""
    hard: List[Tuple[datetime, datetime]] = []
    soft: List[Tuple[datetime, datetime, str]] = []
    seen_groups = set()
    for occ in occurrences:
        if str(occ.get("status") or "") == "cancelled":
            continue
        group = occ.get("link_group_id") or ""
        key = (group, occ.get("occurrence_start_utc") or occ.get("start_utc")) if group else None
        if key and key in seen_groups:
            continue
        if key:
            seen_groups.add(key)
        s, e = parse_stored(occ.get("start_utc")), parse_stored(occ.get("end_utc"))
        if not s or not e:
            continue
        avail = occ.get("availability") or AVAIL_BUSY
        if avail == AVAIL_FREE:
            continue
        if avail == AVAIL_SOFT:
            soft.append((s, e, str(occ.get("title") or "")))
        else:
            hard.append((s, e))
    hard.sort()
    merged: List[Tuple[datetime, datetime]] = []
    for s, e in hard:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged, soft


def free_windows(hard: List[Tuple[datetime, datetime]], start: datetime, end: datetime, duration: timedelta,
                 working_hours: Tuple[Any, Any], tz, max_slots: int = 6) -> List[Tuple[datetime, datetime]]:
    slots: List[Tuple[datetime, datetime]] = []
    day = start.astimezone(tz).date()
    last_day = end.astimezone(tz).date()
    ws, we = working_hours
    while day <= last_day and len(slots) < max_slots:
        d_start = datetime.combine(day, ws, tzinfo=tz)
        d_end = datetime.combine(day, we, tzinfo=tz)
        cursor = max(d_start, start)
        limit = min(d_end, end)
        for b_start, b_end in hard:
            if b_end <= cursor or b_start >= limit:
                continue
            if b_start - cursor >= duration:
                slots.append((cursor, b_start))
                if len(slots) >= max_slots:
                    break
            cursor = max(cursor, b_end)
        if len(slots) < max_slots and limit - cursor >= duration:
            slots.append((cursor, limit))
        day += timedelta(days=1)
    return slots[:max_slots]


# ── create / update / delete ────────────────────────────────────────

def create_event(store, providers, spec: Dict[str, Any], owner: str = "") -> Dict[str, Any]:
    """``spec``: title, start_utc, end_utc, tz, all_day, calendar_ids (ordered, first = primary — full content;
    the rest follow each calendar's ``publish_mode``, default busy), visibility, availability, description,
    location, attendees, reminders (offsets, minutes), rrule. Returns event + per-calendar assignments."""
    owner = owner or lease_owner()
    calendar_ids: List[str] = [c for c in spec.get("calendar_ids") or [] if c]
    if not calendar_ids:
        calendar_ids = [store.default_calendar()["id"]]
    link_group = new_id("lg") if len(calendar_ids) > 1 else ""
    uid = new_uid()
    assignments: List[Dict[str, Any]] = []
    primary_row: Optional[Dict[str, Any]] = None
    for index, cal_id in enumerate(calendar_ids):
        cal = store.get_calendar(cal_id)
        if cal is None or cal.get("deleted_at"):
            assignments.append({"calendar_id": cal_id, "status": "failed", "message": "календарь не найден"})
            continue
        if cal["provider"] != PROVIDER_LOCAL and not cal.get("writable", True):
            assignments.append({"calendar_id": cal_id, "calendar_name": cal["name"], "status": "failed", "message": "календарь только для чтения"})
            continue
        is_copy = index > 0
        mode = PUBLISH_FULL if not is_copy else (cal.get("publish_mode") or PUBLISH_BUSY)
        row = {
            "calendar_id": cal_id, "uid": uid, "is_primary": 0 if is_copy else 1,
            "visibility": spec.get("visibility") or VISIBILITY_SHOWN, "availability": spec.get("availability") or AVAIL_BUSY,
            "title": BUSY_COPY_TITLE if mode == PUBLISH_BUSY else spec.get("title") or "",
            "description": "" if mode == PUBLISH_BUSY else spec.get("description") or "",
            "location": "" if mode == PUBLISH_BUSY else spec.get("location") or "",
            "start_utc": spec["start_utc"], "end_utc": spec["end_utc"], "tz": spec.get("tz") or "", "all_day": bool(spec.get("all_day")),
            "rrule": spec.get("rrule") or "", "attendees_json": json.dumps([] if mode == PUBLISH_BUSY else spec.get("attendees") or [], ensure_ascii=False),
            "reminders_json": json.dumps([] if is_copy else [int(x) for x in spec.get("reminders") or []]),
            "origin": "local", "link_group_id": link_group,
            "sync_state": "synced" if cal["provider"] == PROVIDER_LOCAL else "pending",
        }
        saved = store.insert_event(row)
        if primary_row is None:
            primary_row = store.get_event(saved["id"]) or saved
        if cal["provider"] == PROVIDER_LOCAL:
            assignments.append({"calendar_id": cal_id, "calendar_name": cal["name"], "event_id": saved["id"], "status": INTENT_DONE})
            continue
        intent = store.add_intent("create", cal["account_id"], cal_id, saved["id"], {"send_updates": bool(spec.get("send_invites")) and not is_copy})
        result = execute_intent(store, providers, intent, owner)
        assignments.append({"calendar_id": cal_id, "calendar_name": cal["name"], "event_id": saved["id"], **result})
    return {"event": primary_row, "assignments": assignments, "link_group_id": link_group}


def update_event(store, providers, event_id: str, changes: Dict[str, Any], scope: str = SCOPE_THIS, owner: str = "",
                 propagate: bool = True, send_updates: bool = False) -> Dict[str, Any]:
    """Apply ``changes`` to an event, an occurrence, a whole series or «from this date on».

    ``changes`` may contain start_utc/end_utc/all_day/tz/title/description/location/
    kind/visibility/availability/attendees/reminders/rrule/my_response.
    """
    owner = owner or lease_owner()
    base_id, occ_key = _series_id(event_id)
    row = store.get_event(base_id)
    if row is None or row.get("deleted_at"):
        return {"status": "not_found", "assignments": []}
    targets: List[Dict[str, Any]] = []
    if occ_key and scope == SCOPE_THIS:
        exc_id = _ensure_exception(store, row, occ_key, changes)
        targets.append(store.get_event(exc_id))
    elif occ_key and scope == SCOPE_FOLLOWING:
        new_group = new_id("lg") if row.get("link_group_id") else ""
        targets.extend(_split_series(store, row, occ_key, changes, new_group))
    else:
        _apply_changes(store, row, changes)
        targets.append(store.get_event(row["id"]))
    # Linked copies: only time moves to busy copies; full copies get everything but attendees/reminders.
    if propagate and row.get("link_group_id"):
        sibling_changes = _sibling_changes(changes)
        for sib in store.group_members(row["link_group_id"]):
            if sib["id"] == row["id"] or not sibling_changes:
                continue
            if sib.get("title") == BUSY_COPY_TITLE or sib.get("publish_mode") == PUBLISH_BUSY:
                allowed = {k: v for k, v in sibling_changes.items() if k in ("start_utc", "end_utc", "all_day", "tz", "rrule")}
            else:
                allowed = dict(sibling_changes)
            if occ_key and scope == SCOPE_THIS:
                exc_id = _ensure_exception(store, sib, occ_key, allowed)
                targets.append(store.get_event(exc_id))
            elif occ_key and scope == SCOPE_FOLLOWING:
                targets.extend(_split_series(store, sib, occ_key, allowed, new_group))
            else:
                _apply_changes(store, sib, allowed)
                targets.append(store.get_event(sib["id"]))
    assignments = []
    rsvp_only = set(changes.keys()) == {"my_response"}
    for target in targets:
        if target is None:
            continue
        cal = store.get_calendar(target["calendar_id"])
        if cal is None or cal["provider"] == PROVIDER_LOCAL:
            assignments.append({"calendar_id": target["calendar_id"], "event_id": target["id"], "status": INTENT_DONE})
            continue
        if not cal.get("writable", True):
            assignments.append({"calendar_id": target["calendar_id"], "calendar_name": cal["name"], "event_id": target["id"], "status": INTENT_FAILED,
                                "message": "календарь только для чтения"})
            continue
        store.update_event(target["id"], {"sync_state": "pending"})
        if rsvp_only or ("my_response" in changes and target["id"] == row["id"]):
            kind = "rsvp"
        elif not target.get("external_id") and not target.get("href") and not target.get("master_id"):
            kind = "create"
        else:
            kind = "update"
        payload = {"scope": scope, "changes": changes, "send_updates": bool(send_updates)}
        if kind == "rsvp":
            payload.update({"response": changes.get("my_response"), "notify_organizer": bool(send_updates)})
        intent = store.add_intent(kind, cal["account_id"], cal["id"], target["id"], payload, scope=scope, expected_etag=target.get("etag") or "")
        result = execute_intent(store, providers, intent, owner)
        assignments.append({"calendar_id": target["calendar_id"], "calendar_name": cal["name"], "event_id": target["id"], **result})
    fresh = store.get_event(targets[0]["id"]) if targets and targets[0] else None
    return {"status": "ok", "event": fresh, "assignments": assignments}


def delete_event(store, providers, event_id: str, scope: str = SCOPE_THIS, owner: str = "", send_updates: bool = False) -> Dict[str, Any]:
    owner = owner or lease_owner()
    base_id, occ_key = _series_id(event_id)
    row = store.get_event(base_id)
    if row is None or row.get("deleted_at"):
        return {"status": "not_found", "assignments": []}
    rows = [row]
    if row.get("link_group_id"):
        rows.extend(s for s in store.group_members(row["link_group_id"]) if s["id"] != row["id"])
    assignments = []
    for target in rows:
        cal = store.get_calendar(target["calendar_id"])
        external = cal is not None and cal["provider"] != PROVIDER_LOCAL and (target.get("external_id") or target.get("href"))
        if occ_key and scope == SCOPE_THIS:
            exc_id = _ensure_exception(store, target, occ_key, {"status": "cancelled"})
            victim = store.get_event(exc_id)
        elif occ_key and scope == SCOPE_FOLLOWING:
            _truncate_series(store, target, occ_key)
            victim = store.get_event(target["id"])
        else:
            store.drop_reminders_for(target["id"])
            for exc in store.exceptions_for(target["id"]):
                store.delete_event(exc["id"])
            if external:
                # the row stays visible (sync_state=pending_delete) until the provider confirms; a conflict keeps it inspectable
                store.update_event(target["id"], {"sync_state": "pending_delete"})
            else:
                store.delete_event(target["id"])
            victim = store.get_event(target["id"])
        if cal is None or cal["provider"] == PROVIDER_LOCAL or victim is None or not external:
            assignments.append({"calendar_id": target["calendar_id"], "event_id": target["id"], "status": INTENT_DONE})
            continue
        if not cal.get("writable", True):
            assignments.append({"calendar_id": target["calendar_id"], "calendar_name": cal["name"], "event_id": victim["id"], "status": INTENT_FAILED,
                                "message": "календарь только для чтения"})
            continue
        kind = "update" if (occ_key and scope != SCOPE_ALL) else "delete"
        intent = store.add_intent(kind, cal["account_id"], cal["id"], victim["id"], {"scope": scope, "changes": {}, "send_updates": bool(send_updates)},
                                  scope=scope, expected_etag=victim.get("etag") or "")
        result = execute_intent(store, providers, intent, owner)
        assignments.append({"calendar_id": target["calendar_id"], "calendar_name": cal["name"], "event_id": victim["id"], **result})
    return {"status": "ok", "assignments": assignments}


def _apply_changes(store, row: Dict[str, Any], changes: Dict[str, Any]) -> None:
    fields: Dict[str, Any] = {}
    for key in ("start_utc", "end_utc", "all_day", "tz", "title", "description", "location", "visibility",
                "availability", "rrule", "my_response", "status", "exdates", "rdates"):
        if key in changes and changes[key] is not None:
            fields[key] = changes[key]
    if "attendees" in changes and changes["attendees"] is not None:
        fields["attendees_json"] = json.dumps(changes["attendees"], ensure_ascii=False)
    if "reminders" in changes and changes["reminders"] is not None:
        fields["reminders_json"] = json.dumps([int(x) for x in changes["reminders"]])
        store.drop_reminders_for(row["id"])
    if fields:
        store.update_event(row["id"], fields)
        if any(k in fields for k in ("start_utc", "end_utc", "status", "rrule")) and not row.get("master_id"):
            # a moved single event: its queue is replanned; for an exception the queue row stays and is re-read at delivery
            store.drop_reminders_for(row["id"])


def _sibling_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in changes.items() if k in ("start_utc", "end_utc", "all_day", "tz", "title", "description", "location", "rrule", "status")}


def _ensure_exception(store, master: Dict[str, Any], occ_key: str, changes: Dict[str, Any]) -> str:
    """One stored row per changed occurrence (RECURRENCE-ID); create it on first change."""
    for exc in store.exceptions_for(master["id"]):
        if str(exc.get("recurrence_id") or "") == occ_key:
            _apply_changes(store, exc, changes)
            return exc["id"]
    m_start, m_end = parse_stored(master["start_utc"]), parse_stored(master["end_utc"])
    occ_start = parse_stored(occ_key) or m_start
    duration = (m_end - m_start) if (m_start and m_end) else timedelta(hours=1)
    row = {k: master.get(k) for k in ("calendar_id", "uid", "visibility", "availability", "is_primary", "title", "description",
                                       "location", "tz", "all_day", "organizer", "attendees_json", "reminders_json", "origin", "link_group_id")}
    # The exception has NO provider identity of its own: Google resolves the instance id from the master + recurrence key,
    # CalDAV writes it inside the master's resource (roast/triad F01).
    row.update({"start_utc": iso_utc(occ_start), "end_utc": iso_utc(occ_start + duration), "rrule": "", "master_id": master["id"],
                "recurrence_id": occ_key, "external_id": "", "href": "", "etag": "",
                "sync_state": "synced" if not master.get("external_id") and not master.get("href") else "pending"})
    saved = store.insert_event(row)
    _apply_changes(store, saved, changes)
    return saved["id"]


def _truncate_series(store, master: Dict[str, Any], occ_key: str, keep_later_exceptions: bool = False) -> List[Dict[str, Any]]:
    """Series ends before ``occ_key``: UNTIL = occurrence start − 1s (RFC 5545, UTC). Returns the detached later exceptions."""
    occ = parse_stored(occ_key)
    if occ is None:
        return []
    until = (occ - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parts = [p for p in str(master.get("rrule") or "").split(";") if p and not p.upper().startswith(("UNTIL=", "COUNT="))]
    parts.append(f"UNTIL={until}")
    store.update_event(master["id"], {"rrule": ";".join(parts)})
    later = []
    for exc in store.exceptions_for(master["id"]):
        if str(exc.get("recurrence_id") or "") >= occ_key:
            if keep_later_exceptions:
                later.append(exc)
            else:
                store.delete_event(exc["id"])
    store.drop_reminders_for(master["id"])
    return later


def _split_series(store, master: Dict[str, Any], occ_key: str, changes: Dict[str, Any], new_group: str = "") -> List[Dict[str, Any]]:
    """«Starting from this date»: truncate the old series, start a new one carrying the changes.

    Later exceptions move to the new series (their date keeps the edit, the slot follows the new wall time);
    linked copies split together under one new link group.
    """
    later = _truncate_series(store, master, occ_key, keep_later_exceptions=True)
    m_start, m_end = parse_stored(master["start_utc"]), parse_stored(master["end_utc"])
    occ_start = parse_stored(occ_key) or m_start
    duration = (m_end - m_start) if (m_start and m_end) else timedelta(hours=1)
    new_start = parse_stored(changes.get("start_utc")) or occ_start
    new_end = parse_stored(changes.get("end_utc")) or (new_start + duration)
    rule_parts = [p for p in str(master.get("rrule") or "").split(";") if p and not p.upper().startswith(("UNTIL=", "COUNT="))]
    row = {k: master.get(k) for k in ("calendar_id", "visibility", "availability", "is_primary", "title", "description", "location", "tz",
                                       "all_day", "organizer", "attendees_json", "reminders_json", "origin")}
    row.update({"uid": new_uid(), "start_utc": iso_utc(new_start), "end_utc": iso_utc(new_end), "link_group_id": new_group,
                "rrule": changes.get("rrule") or ";".join(rule_parts), "sync_state": "synced"})
    saved = store.insert_event(row)
    _apply_changes(store, saved, {k: v for k, v in changes.items() if k not in ("start_utc", "end_utc", "rrule")})
    from model import get_tz
    tz = get_tz(master.get("tz") or "")
    shift = new_start - occ_start
    for exc in later:
        old_key = parse_stored(exc.get("recurrence_id"))
        if old_key is None:
            continue
        new_key = old_key + shift if not master.get("all_day") else old_key
        store.update_event(exc["id"], {"master_id": saved["id"], "recurrence_id": iso_utc(new_key), "uid": saved["uid"],
                                       "link_group_id": new_group, "external_id": "", "href": "", "etag": ""})
    return [store.get_event(master["id"]), store.get_event(saved["id"])]


# ── intents ─────────────────────────────────────────────────────────

def execute_intent(store, providers, intent: Dict[str, Any], owner: str) -> Dict[str, Any]:
    """Lease and run one intent now; returns the per-assignment status dict."""
    leased = store.lease_intent(intent["id"], owner, LEASE_SECONDS)
    if leased is None:
        return {"status": INTENT_PENDING, "message": "операция уже выполняется"}
    return run_leased_intent(store, providers, leased)


def run_leased_intent(store, providers, intent: Dict[str, Any]) -> Dict[str, Any]:
    event = store.get_event(intent["event_id"])
    cal = store.get_calendar(intent["calendar_id"])
    adapter = providers.adapter_for(intent["account_id"]) if providers else None
    if event is None or cal is None:
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": "event or calendar vanished"})
        return {"status": INTENT_FAILED, "message": "событие или календарь исчезли"}
    if adapter is None:
        return _defer(store, intent, "аккаунт не подключён (нет секрета или гранта)", kind="not_connected")
    try:
        payload = json.loads(intent.get("payload_json") or "{}")
    except ValueError:
        payload = {}
    if event.get("master_id") and not event.get("external_id"):
        master = store.get_event(event["master_id"])
        event = {**event, "master_external_id": (master or {}).get("external_id") or "", "master_href": (master or {}).get("href") or "",
                 "master_etag": (master or {}).get("etag") or ""}
    modes = store.get_setting("reminder_mode") or {}
    payload["mute_provider_reminders"] = bool(modes.get(str(cal["id"])))
    try:
        if intent["kind"] == "create":
            res = adapter.create(cal, event, payload)
            store.update_event(event["id"], {"external_id": res.get("external_id") or "", "href": res.get("href") or "",
                                             "etag": res.get("etag") or "", "sync_state": "synced"})
        elif intent["kind"] == "update":
            res = adapter.update(cal, event, intent.get("expected_etag") or "", payload)
            store.update_event(event["id"], {"etag": res.get("etag") or event.get("etag") or "", "sync_state": "synced",
                                             **({"external_id": res["external_id"]} if res.get("external_id") else {})})
        elif intent["kind"] == "delete":
            adapter.delete(cal, event, intent.get("expected_etag") or "", payload)
            store.delete_event(event["id"], hard=True)
        elif intent["kind"] == "rsvp":
            res = adapter.respond(cal, event, payload)
            store.update_event(event["id"], {"etag": res.get("etag") or event.get("etag") or "", "sync_state": "synced"})
        else:
            store.settle_intent(intent["id"], INTENT_FAILED, {"error": f"unknown intent kind {intent['kind']}"})
            return {"status": INTENT_FAILED, "message": "неизвестная операция"}
    except ProviderError as exc:
        if exc.kind == "conflict":
            store.update_event(event["id"], {"sync_state": "conflict"})
            store.settle_intent(intent["id"], INTENT_CONFLICT, {"error": exc.message})
            return {"status": INTENT_CONFLICT, "message": exc.message}
        if exc.kind == "not_found" and intent["kind"] == "delete":
            store.delete_event(event["id"], hard=True)
            store.settle_intent(intent["id"], INTENT_DONE, {"note": "already gone"})
            return {"status": INTENT_DONE, "message": "уже удалено на сервере"}
        if exc.kind in ("network", "server"):
            return _defer(store, intent, exc.message, kind=exc.kind)
        store.update_event(event["id"], {"sync_state": "failed"})
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": exc.message, "kind": exc.kind})
        if exc.kind == "auth":
            store.set_account_status(intent["account_id"], "auth_failed", exc.message)
        return {"status": INTENT_FAILED, "message": exc.message}
    except Exception as exc:  # adapter bug: disclose, do not loop forever
        store.update_event(event["id"], {"sync_state": "failed"})
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": f"{type(exc).__name__}: {exc}"})
        return {"status": INTENT_FAILED, "message": f"внутренняя ошибка адаптера: {type(exc).__name__}"}
    store.settle_intent(intent["id"], INTENT_DONE, {"ok": True})
    return {"status": INTENT_DONE}


def _defer(store, intent: Dict[str, Any], message: str, kind: str = "network") -> Dict[str, Any]:
    attempts = int(intent.get("attempts") or 1)
    if attempts >= MAX_ATTEMPTS:
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": message, "kind": kind, "attempts": attempts})
        store.update_event(intent["event_id"], {"sync_state": "failed"})
        return {"status": INTENT_FAILED, "message": f"{message} (попытки исчерпаны)"}
    delay = RETRY_BACKOFF_SEC[min(attempts, len(RETRY_BACKOFF_SEC)) - 1]
    store.settle_intent(intent["id"], INTENT_PENDING, {"error": message, "kind": kind}, retry_in_sec=delay)
    return {"status": INTENT_PENDING, "message": f"{message}; повтор через {delay} с"}


def retry_due_intents(store, providers, owner: str = "companion", limit: int = 20) -> List[Dict[str, Any]]:
    out = []
    for intent in store.lease_due_intents(owner, limit=limit, seconds=LEASE_SECONDS):
        out.append({"intent": intent["id"], **run_leased_intent(store, providers, intent)})
    return out


def reassign_event(store, providers, row: Dict[str, Any], calendar_ids: List[str], owner: str = "") -> Dict[str, Any]:
    """Change the set of calendars an event lives in (12 A: only on explicit command).

    Copies are added/removed per the target calendars' ``publish_mode``; the primary row stays where it is —
    moving the primary itself is a delete + create the owner asks for explicitly.
    """
    owner = owner or lease_owner()
    group = row.get("link_group_id") or ""
    members = store.group_members(group) if group else [row]
    row = next((m for m in members if m.get("is_primary")), row)   # a clicked copy still reassigns around the primary
    current = {m["calendar_id"]: m for m in members}
    added, removed = [], []
    wanted = [c for c in calendar_ids if c]
    if row["calendar_id"] not in wanted:
        wanted.insert(0, row["calendar_id"])
    if not group and len(wanted) > 1:
        group = new_id("lg")
        store.update_event(row["id"], {"link_group_id": group, "is_primary": 1})
        row = store.get_event(row["id"]) or row
    for cid in wanted:
        if cid in current:
            continue
        cal = store.get_calendar(cid)
        if cal is None or cal.get("deleted_at"):
            added.append({"calendar_id": cid, "status": INTENT_FAILED, "message": "календарь не найден"})
            continue
        if cal["provider"] != PROVIDER_LOCAL and not cal.get("writable", True):
            added.append({"calendar_id": cid, "calendar_name": cal["name"], "status": INTENT_FAILED, "message": "календарь только для чтения"})
            continue
        mode = cal.get("publish_mode") or PUBLISH_BUSY
        copy = {k: row.get(k) for k in ("uid", "visibility", "availability", "start_utc", "end_utc", "tz", "all_day", "rrule", "exdates", "rdates")}
        copy.update({"calendar_id": cid, "is_primary": 0, "link_group_id": group,
                     "title": BUSY_COPY_TITLE if mode == PUBLISH_BUSY else row.get("title") or "",
                     "description": "" if mode == PUBLISH_BUSY else row.get("description") or "",
                     "location": "" if mode == PUBLISH_BUSY else row.get("location") or "",
                     "attendees_json": "[]", "reminders_json": "[]", "origin": "local",
                     "sync_state": "synced" if cal["provider"] == PROVIDER_LOCAL else "pending"})
        saved = store.insert_event(copy)
        if cal["provider"] == PROVIDER_LOCAL:
            added.append({"calendar_id": cid, "calendar_name": cal["name"], "event_id": saved["id"], "status": INTENT_DONE})
            continue
        intent = store.add_intent("create", cal["account_id"], cid, saved["id"], {"send_updates": False})
        added.append({"calendar_id": cid, "calendar_name": cal["name"], "event_id": saved["id"], **execute_intent(store, providers, intent, owner)})
    for cid, m in current.items():
        if cid in wanted or m["id"] == row["id"]:
            continue
        res = delete_event(store, providers, m["id"], scope=SCOPE_ALL, owner=owner)
        removed.append({"calendar_id": cid, "assignments": res["assignments"]})
    return {"added": added, "removed": removed, "kept_primary": row["calendar_id"]}
