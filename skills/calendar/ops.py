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
    PROVIDER_LOCAL, PUBLISH_BUSY, PUBLISH_FULL, SCOPES, SCOPE_ALL, SCOPE_FOLLOWING, SCOPE_THIS, VISIBILITY_SHOWN,
    get_tz, iso_utc, new_id, new_uid, overlaps, own_reminders, parse_stored, reminders_to_json,
)

RETRY_BACKOFF_SEC = (30, 120, 600, 1800, 7200)
MAX_ATTEMPTS = len(RETRY_BACKOFF_SEC)
LEASE_SECONDS = 90


class ProviderError(Exception):
    """Typed adapter failure. ``kind`` ∈ network|server|retry (transient: deferred and retried) | auth|forbidden|not_found|gone|conflict|http|parse|unsupported."""

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
        parsed = _parse_occurrence_key(occ)
        return base, iso_utc(parsed) if parsed else occ
    return occurrence_id, ""


def _parse_occurrence_key(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.microsecond == 0 else None


def resolve_occurrence(store, row: Dict[str, Any], occ_key: str) -> Tuple[Dict[str, Any], str]:
    """Resolve an addressed recurrence slot, including saved exception IDs, before a write or direct read."""
    if row.get("master_id"):
        master = store.get_event(row["master_id"])
        if master is None or master.get("deleted_at"):
            raise ValueError("occurrence has no live series")
        occ_key, row = str(row.get("recurrence_id") or ""), master
    if not occ_key:
        return row, ""
    slot = _parse_occurrence_key(occ_key)
    if slot is None or not row.get("rrule") or not row.get("id"):
        raise ValueError("invalid occurrence id")
    key = iso_utc(slot)
    if key in excluded_slots(row):
        raise ValueError("occurrence is cancelled")
    try:
        projected = _expand_master(row, slot, slot + timedelta(seconds=1), [], strict=True)
    except Exception as exc:
        raise ValueError("invalid recurrence slot") from exc
    if not any(o.get("occurrence_start_utc") == key for o in projected):
        raise ValueError("occurrence is outside the series")
    return row, key


def expand(rows: Sequence[Dict[str, Any]], start: datetime, end: datetime, exceptions_for: Callable[[str], List[Dict[str, Any]]],
           owner_tz=None, strict: bool = False, failed: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
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
        try:
            out.extend(_expand_master(row, start, end, exceptions_for(row["id"]), owner_tz if follow_owner else None, strict=strict))
        except Exception as exc:
            if failed is None:
                raise
            failed[row["id"]] = f"{type(exc).__name__}: {exc}"
    out.sort(key=lambda r: (r.get("start_utc") or "", r.get("title") or ""))
    return out


def _parser_rule(rule_text: str, tz) -> str:
    """Normalize floating UNTIL for the parser, without rewriting provider text."""
    parts = rule_text.split(";")
    for index, part in enumerate(parts):
        if not part.upper().startswith("UNTIL="):
            continue
        value = part.split("=", 1)[1].strip()
        if len(value) == 8:
            until = datetime.combine(datetime.strptime(value, "%Y%m%d").date() + timedelta(days=1),
                                     datetime.min.time(), tzinfo=tz) - timedelta(seconds=1)
        elif len(value) == 15 and value[8].upper() == "T":
            until = datetime.strptime(value.upper(), "%Y%m%dT%H%M%S").replace(tzinfo=tz)
        else:
            continue
        parts[index] = "UNTIL=" + until.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ";".join(parts)


def validate_rrule(rule: Any, start_utc: str, zone: str) -> str:
    """Validate a user rule before any write; missing parser is not validation."""
    from dateutil.rrule import rrulestr
    text = str(rule or "").strip()
    if text[:6].upper() == "RRULE:":
        text = text[6:].strip()
    if not text:
        return ""
    if any(c in text for c in ":\r\n"):
        raise ValueError("rrule: требуется одно RRULE без дополнительных строк")
    try:
        tz = get_tz(zone)
        start = parse_stored(start_utc).astimezone(tz)
        rrulestr(_parser_rule(text, tz), dtstart=start, forceset=True)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"rrule не разобрано: {exc}; пример FREQ=WEEKLY;BYDAY=MO,WE") from exc
    return text


def _expand_master(master: Dict[str, Any], start: datetime, end: datetime, exceptions: List[Dict[str, Any]],
                   force_tz=None, strict: bool = False) -> List[Dict[str, Any]]:
    m_start, m_end = parse_stored(master.get("start_utc")), parse_stored(master.get("end_utc"))
    if m_start is None or m_end is None:
        if strict:
            raise ValueError("recurring event has invalid start or end")
        return []
    duration = m_end - m_start
    try:
        from dateutil.rrule import rrulestr  # transitive dependency of icalendar
    except Exception:
        if strict:
            raise  # a partial recurrence projection must not authorize destructive queue reconciliation
        row = dict(master)
        return [row] if overlaps(m_start, m_end, start, end) else []
    try:
        from model import get_tz
        tz = force_tz or get_tz(master.get("tz") or "")
        dtstart = m_start.astimezone(tz)
        if force_tz is not None:
            # keep the wall-clock time of the routine in the owner's current zone
            local_src = m_start.astimezone(get_tz(master.get("tz") or ""))
            dtstart = local_src.replace(tzinfo=tz)
        rule = rrulestr(_parser_rule(str(master["rrule"]), tz), dtstart=dtstart, forceset=True)
        for ex in _split_dates(master.get("exdates")):
            rule.exdate(ex.astimezone(tz))
        for rd in _split_dates(master.get("rdates")):
            rule.rdate(rd.astimezone(tz))
    except Exception:
        if strict:
            raise
        row = dict(master)
        return [row] if overlaps(m_start, m_end, start, end) else []
    by_recurrence = {str(ex.get("recurrence_id") or ""): ex for ex in exceptions}
    excluded = excluded_slots(master)   # an EXDATE cancels its slot, even if a stale override still names it
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
            row.update({k: v for k, v in exc.items() if v is not None})
            if own_reminders(exc.get("reminders_json")) is None:
                row["reminders_json"] = master.get("reminders_json") or "[]"
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
            if strict:
                raise ValueError("recurrence expansion exceeded 1000 occurrences")
            break
    # Exceptions moved INTO the window from an occurrence date outside it.
    for key, exc in by_recurrence.items():
        if key in visited or key in excluded or str(exc.get("status") or "") == "cancelled" or exc.get("deleted_at"):
            continue
        s, e = parse_stored(exc.get("start_utc")), parse_stored(exc.get("end_utc"))
        if s and e and overlaps(s, e, start, end):
            row = dict(master)
            row.update({k: v for k, v in exc.items() if v is not None})
            if own_reminders(exc.get("reminders_json")) is None:
                row["reminders_json"] = master.get("reminders_json") or "[]"
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


def excluded_slots(master: Dict[str, Any]) -> set:
    """Original occurrence keys the series' EXDATE cancels (same ISO-UTC form as recurrence_id)."""
    return {iso_utc(dt) for dt in _split_dates(master.get("exdates"))}


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

def _finish_command(store, providers, stage, owner, *args, **kwargs):
    """Commit all desired rows and intents before attempting provider I/O."""
    if store._transaction_connection is not None:
        raise RuntimeError('provider dispatch requires an ordinary Store, not a transaction view')
    with store.transaction() as local:
        result = stage(local, *args, **kwargs)
    assignments = list(result.get("assignments", [])) + list(result.get("added", []))
    for removed in result.get("removed", []):
        assignments.extend(removed.get("assignments", []))
    for assignment in assignments:
        ids = assignment.pop("_intent_ids", [])
        if not ids:
            continue
        outcome = {"status": INTENT_DONE}
        for intent_id in ids:
            reply = execute_intent(store, providers, store.get_intent(intent_id), owner or lease_owner())
            if outcome["status"] == INTENT_DONE:
                outcome = reply
        assignment.update(outcome)
    if result.get("event"):
        result["event"] = store.get_event(result["event"]["id"]) or result["event"]
    return result


def create_event(store, providers, spec, owner=""):
    return _finish_command(store, providers, _stage_create_event, owner, spec)


def update_event(store, providers, event_id, changes, scope=SCOPE_THIS, owner="", propagate=True, send_updates=False):
    return _finish_command(store, providers, _stage_update_event, owner, event_id, changes,
                           scope=scope, propagate=propagate, send_updates=send_updates)


def delete_event(store, providers, event_id, scope=SCOPE_THIS, owner="", send_updates=False, cascade=True):
    return _finish_command(store, providers, _stage_delete_event, owner, event_id,
                           scope=scope, send_updates=send_updates, cascade=cascade)


def reassign_event(store, providers, row, calendar_ids, owner=""):
    return _finish_command(store, providers, _stage_reassign_event, owner, row, calendar_ids)


def _stage_create_event(store, spec: Dict[str, Any], owner: str = "") -> Dict[str, Any]:
    """``spec``: title, start_utc, end_utc, tz, all_day, calendar_ids (ordered, first = primary — full content;
    the rest follow each calendar's ``publish_mode``, default busy), visibility, availability, description,
    location, attendees, reminders (offsets, minutes), rrule. Returns event + per-calendar assignments."""
    calendar_ids: List[str] = [c for c in spec.get("calendar_ids") or [] if c]
    if not calendar_ids:
        calendar_ids = [store.default_calendar()["id"]]
    link_group = new_id("lg") if len(calendar_ids) > 1 else ""
    uid = new_uid()
    op_id = new_id("op")
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
        is_copy = primary_row is not None   # the first calendar that actually accepts the event is the primary
        mode = PUBLISH_FULL if not is_copy else cal["publish_mode"]
        row = {
            "calendar_id": cal_id, "uid": uid, "is_primary": 0 if is_copy else 1,
            "visibility": spec.get("visibility") or VISIBILITY_SHOWN, "availability": spec.get("availability") or AVAIL_BUSY,
            "title": BUSY_COPY_TITLE if mode == PUBLISH_BUSY else spec.get("title") or "",
            "description": "" if mode == PUBLISH_BUSY else spec.get("description") or "",
            "location": "" if mode == PUBLISH_BUSY else spec.get("location") or "",
            "start_utc": spec["start_utc"], "end_utc": spec["end_utc"], "tz": spec.get("tz") or "", "all_day": bool(spec.get("all_day")),
            "rrule": spec.get("rrule") or "", "attendees_json": json.dumps([] if is_copy else spec.get("attendees") or [], ensure_ascii=False),
            "reminders_json": "[]" if is_copy or spec.get("reminders") is None else reminders_to_json(spec["reminders"]),
            "origin": "local", "link_group_id": link_group,
            "sync_state": "synced" if cal["provider"] == PROVIDER_LOCAL else "pending",
        }
        saved = store.insert_event(row)
        if primary_row is None:
            primary_row = store.get_event(saved["id"]) or saved
        if cal["provider"] == PROVIDER_LOCAL:
            assignments.append({"calendar_id": cal_id, "calendar_name": cal["name"], "event_id": saved["id"], "status": INTENT_DONE})
            continue
        intent = store.add_intent("create", cal["account_id"], cal_id, saved["id"], {"send_updates": bool(spec.get("send_invites")) and not is_copy}, op_id=op_id)
        result = {"status": INTENT_PENDING, "_intent_ids": [intent["id"]]}
        assignments.append({"calendar_id": cal_id, "calendar_name": cal["name"], "event_id": saved["id"], **result})
    return {"event": primary_row, "assignments": assignments, "link_group_id": link_group}



def _stage_update_event(store, event_id: str, changes: Dict[str, Any], scope: str = SCOPE_THIS,
                 propagate: bool = True, send_updates: bool = False) -> Dict[str, Any]:
    """Apply ``changes`` to an event, an occurrence, a whole series or «from this date on».

    ``changes`` may contain start_utc/end_utc/all_day/tz/title/description/location/
    kind/visibility/availability/attendees/reminders/rrule/my_response.
    """
    if scope not in SCOPES:
        raise ValueError("scope: this | following | all")
    base_id, occ_key = _series_id(event_id)
    row = store.get_event(base_id)
    if row is None or row.get("deleted_at"):
        return {"status": "not_found", "assignments": []}
    row, occ_key = resolve_occurrence(store, row, occ_key)
    if row.get("rrule") and not occ_key and scope == SCOPE_THIS:
        raise ValueError("a recurring master needs an occurrence id or scope=all")
    if scope == SCOPE_FOLLOWING and not occ_key:
        raise ValueError("scope=following needs an occurrence id")
    changes = _shift_for_scope_all(store, row, occ_key, scope, changes)
    if changes.get("all_day"):
        zone = get_tz(changes.get("tz") or row.get("tz") or "")
        first = parse_stored(changes.get("start_utc") or occ_key or row["start_utc"]).astimezone(zone)
        last = parse_stored(changes.get("end_utc") or row["end_utc"]).astimezone(zone)
        first = first.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight = last.replace(hour=0, minute=0, second=0, microsecond=0)
        last = midnight + timedelta(days=1) if midnight != last else midnight
        changes = {**changes, "start_utc": iso_utc(first), "end_utc": iso_utc(max(last, first + timedelta(days=1)))}
    op_id = new_id("op")
    members = [row] + [m for m in _group_masters(store, row) if m["id"] != row["id"]]
    assignments: List[Dict[str, Any]] = []
    blocked = _read_only_preflight(store, members, assignments)   # refuse read-only targets BEFORE touching local rows
    if row["id"] in blocked:
        return {"status": "failed", "event": row, "assignments": assignments}
    before = {e['id']: e for master in members for e in [master] + store.exceptions_for(master['id'])}
    roots = {master['calendar_id']: master['id'] for master in members}
    # Moving topology must follow accepted writes still addressed to the old
    # resource. Their desired snapshots remain valid until they acknowledge.
    structural = scope == SCOPE_FOLLOWING or bool(_series_delta(row, changes))
    prior_by_calendar = {}
    if structural:
        for eid, old in before.items():
            prior_by_calendar.setdefault(old['calendar_id'], []).extend(
                i['id'] for i in store.intents_for_event(eid) if i['state'] == INTENT_PENDING)
    targets: List[Dict[str, Any]] = []
    if occ_key and scope == SCOPE_THIS:
        exc_id = _ensure_exception(store, row, occ_key, changes)
        targets.append(store.get_event(exc_id))
    elif occ_key and scope == SCOPE_FOLLOWING:
        new_group = new_id("lg") if row.get("link_group_id") else ""
        split_uid = new_uid()
        targets.extend(_split_series(store, row, occ_key, changes, new_group, split_uid))
    else:
        series_delta = _series_delta(row, changes)
        _apply_changes(store, row, changes)
        targets.append(store.get_event(row["id"]))
        if series_delta:
            targets.extend(_shift_series_rows(store, row, series_delta, changes))   # after the master: they resolve through it
    # Linked copies: only time moves to busy copies; full copies get everything but attendees/reminders.
    if propagate and row.get("link_group_id"):
        sibling_changes = _sibling_changes(changes)
        for sib in members[1:]:
            if sib["id"] in blocked or store.delete_requested(sib["id"]) or not sibling_changes:
                continue
            if sib.get("title") == BUSY_COPY_TITLE or sib.get("publish_mode") == PUBLISH_BUSY:
                allowed = {k: v for k, v in sibling_changes.items() if k in ("start_utc", "end_utc", "all_day", "tz", "rrule")}
            else:
                allowed = dict(sibling_changes)
            if occ_key and scope == SCOPE_THIS:
                exc_id = _ensure_exception(store, sib, occ_key, allowed)
                targets.append(store.get_event(exc_id))
            elif occ_key and scope == SCOPE_FOLLOWING:
                targets.extend(_split_series(store, sib, occ_key, allowed, new_group, split_uid))
            else:
                sib_delta = _series_delta(sib, allowed)
                _apply_changes(store, sib, allowed)
                targets.append(store.get_event(sib["id"]))
                if sib_delta:
                    targets.extend(_shift_series_rows(store, sib, sib_delta, allowed))
    rsvp_only = set(changes.keys()) == {"my_response"}
    previous_by_calendar = {}
    for target in targets:
        if target is None:
            continue
        cal = store.get_calendar(target["calendar_id"])
        if cal is None or cal["provider"] == PROVIDER_LOCAL:
            assignments.append({"calendar_id": target["calendar_id"], "event_id": target["id"], "status": INTENT_DONE})
            continue
        store.update_event(target["id"], {"sync_state": "pending"})
        write_kind = "create" if (not target.get("external_id") and not target.get("href") and not target.get("master_id")) else "update"
        birth = next((i for i in store.intents_for_event(target['id'])
                      if i['kind'] == 'create' and i['state'] in (INTENT_PENDING, INTENT_FAILED, INTENT_CONFLICT)), None)
        recover_birth = _recover_failed_create(store, birth) if birth else False
        if birth:
            write_kind = 'update'
        kinds: List[str] = []
        if "my_response" in changes and target["calendar_id"] == row["calendar_id"]:
            if not rsvp_only:
                kinds.append(write_kind)     # the other changes go out first…
            kinds.append("rsvp")             # …then the answer, as its own provider call (it waits while the write is pending)
        else:
            kinds.append(write_kind)
        result: Dict[str, Any] = {"status": INTENT_PENDING, "_intent_ids": [birth['id']] if recover_birth else []}
        for kind in kinds:
            payload = {"scope": scope, "changes": {k: v for k, v in changes.items() if k != "my_response"}, "send_updates": bool(send_updates)}
            payload['family_root_id'] = roots[cal['id']]
            previous = previous_by_calendar.get(cal['id'])
            if previous:
                payload['after_intent_id'] = previous
            else:
                if prior_by_calendar.get(cal['id']):
                    payload['prior_intent_ids'] = prior_by_calendar[cal['id']]
                if birth:
                    payload['after_intent_id'] = birth['id']
                affected = [t['id'] for t in targets if t and t['calendar_id'] == cal['id']]
                payload['restore_rows'] = [{k: v for k, v in before[eid].items() if k not in ('raw_payload','provider','account_id','calendar_name')}
                                           for eid in affected if eid in before]
                payload['created_ids'] = [eid for eid in affected if eid not in before]
            if target.get("_previous_recurrence_id"):
                payload["previous_recurrence_id"] = target["_previous_recurrence_id"]
            if kind == "rsvp":
                payload.update({"response": changes.get("my_response"), "notify_organizer": bool(send_updates)})
            current = store.get_event(target["id"]) or target
            old_target = before.get(target['id'])
            if 'exdates' in changes:
                # Remove only exclusions this command actually observed and replaces.
                # Independent occurrence cancellations can exist only in the live ICS.
                payload['previous_exdates'] = (old_target or {}).get('exdates') or ''
            if old_target and old_target.get('master_id') and old_target['master_id'] != current.get('master_id'):
                payload['relocated_from_master'] = old_target['master_id']
            if kind != 'create' and not _write_etag(store, cal, current):
                payload['baseline'] = _baseline_before(before, target)
                if payload.get('relocated_from_master'):
                    parent = store.get_event(current['master_id'])
                    payload['baseline'] = _baseline_before({parent['id']: parent}, current)
            expected = birth['expected_etag'] if recover_birth else _write_etag(store, cal, current)
            intent = store.add_intent(kind, cal["account_id"], cal["id"], target["id"], payload, scope=scope, expected_etag=expected, op_id=op_id)
            previous_by_calendar[cal['id']] = intent['id']
            result["_intent_ids"].append(intent["id"])
        assignments.append({"calendar_id": target["calendar_id"], "calendar_name": cal["name"], "event_id": target["id"], **result})
    fresh = store.get_event(targets[0]["id"]) if targets and targets[0] else None
    out = {"status": "ok", "event": fresh, "assignments": assignments}
    if occ_key and scope == SCOPE_FOLLOWING and len(targets) > 1 and targets[1]:
        out["split_master_id"] = targets[1]["id"]   # «from this date on» lives in a new series: reassignments apply to it
    return out



def _stage_delete_event(store, event_id: str, scope: str = SCOPE_THIS, send_updates: bool = False,
                 cascade: bool = True) -> Dict[str, Any]:
    """Delete an event, an occurrence or a series; ``cascade`` (28 A) takes the linked copies along,
    ``cascade=False`` removes one copy only (reassignment)."""
    if scope not in SCOPES:
        raise ValueError("scope: this | following | all")
    base_id, occ_key = _series_id(event_id)
    row = store.get_event(base_id)
    if row is None or row.get("deleted_at"):
        return {"status": "not_found", "assignments": []}
    row, occ_key = resolve_occurrence(store, row, occ_key)
    if row.get("rrule") and not occ_key and scope == SCOPE_THIS:
        raise ValueError("a recurring master needs an occurrence id or scope=all")
    if scope == SCOPE_FOLLOWING and not occ_key:
        raise ValueError("scope=following needs an occurrence id")
    rows = deletion_targets(store, row, cascade)
    op_id = new_id("op")
    assignments: List[Dict[str, Any]] = []
    blocked = _read_only_preflight(store, rows, assignments)
    if row["id"] in blocked:
        return {"status": "failed", "assignments": assignments}
    for target in rows:
        if target["id"] in blocked:
            continue
        before = {e['id']: e for e in [target] + store.exceptions_for(target['id'])}
        cal = store.get_calendar(target["calendar_id"])
        external = cal is not None and cal["provider"] != PROVIDER_LOCAL
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
        kind = "update" if (occ_key and scope != SCOPE_ALL) else "delete"
        store.update_event(victim['id'], {'sync_state': 'pending_delete' if kind=='delete' else 'pending'})
        changes = {'status': 'cancelled'} if occ_key and scope == SCOPE_THIS else {}
        payload = {"scope": scope, "changes": changes, "send_updates": bool(send_updates), "op_id": op_id,
                   'baseline': _baseline_before(before, victim), 'family_root_id': target['id'],
                   'restore_rows': [{k: v for k, v in e.items() if k not in ('raw_payload','provider','account_id','calendar_name')}
                                    for e in before.values()]}
        intent = store.add_intent(kind, cal["account_id"], cal["id"], victim["id"], payload,
                                  scope=scope, expected_etag=_write_etag(store, cal, victim))
        result = {"status": INTENT_PENDING, "_intent_ids": [intent["id"]]}
        assignments.append({"calendar_id": target["calendar_id"], "calendar_name": cal["name"], "event_id": victim["id"], **result})
    return {"status": "ok", "assignments": assignments}



def deletion_targets(store, row: Dict[str, Any], cascade: bool = True) -> List[Dict[str, Any]]:
    """Exact linked-master set affected by delete; used by preview and execution alike."""
    return [row] + ([m for m in _group_masters(store, row) if m["id"] != row["id"]] if cascade else [])


def _baseline_before(before, target):
    baseline = before.get(target['id'])
    if baseline is None and target.get('master_id') in before:
        master = before[target['master_id']]
        slot = parse_stored(target['recurrence_id'])
        baseline = {**master, 'start_utc': iso_utc(slot),
                    'end_utc': iso_utc(slot + (parse_stored(master['end_utc']) - parse_stored(master['start_utc']))),
                    'rrule': '', 'rdates': '', 'exdates': ''}
    return {k: v for k, v in (baseline or {}).items()
            if k not in ('raw_payload','provider','account_id','calendar_name')}


def _write_etag(store, calendar, event):
    if calendar['provider'] == 'yandex' and event.get('master_id'):
        return str((store.get_event(event['master_id']) or {}).get('etag') or '')
    return str(event.get('etag') or '')


def _apply_changes(store, row: Dict[str, Any], changes: Dict[str, Any]) -> None:
    fields: Dict[str, Any] = {}
    for key in ("start_utc", "end_utc", "all_day", "tz", "title", "description", "location", "visibility",
                "availability", "rrule", "my_response", "status", "exdates", "rdates"):
        if key in changes and changes[key] is not None:
            fields[key] = changes[key]
    if "attendees" in changes and changes["attendees"] is not None:
        fields["attendees_json"] = json.dumps(changes["attendees"], ensure_ascii=False)
    if "reminders" in changes and changes["reminders"] is not None:
        fields["reminders_json"] = reminders_to_json(changes["reminders"])   # [] = none for this event, 'default' = by the rules
        store.drop_reminders_for(row["id"])
        if row.get("master_id"):
            store.drop_reminders_for_occurrence(row["master_id"], str(row.get("recurrence_id") or ""))
            store.drop_reminders_for_occurrence(row["master_id"], str(row.get("start_utc") or ""))
    if fields:
        store.update_event(row["id"], fields)
        if row.get("rrule") and fields.get("rrule") == "" and not row.get("master_id"):
            from reminders import notice_id_for
            start_key = str(fields.get("start_utc") or row["start_utc"])
            for previous in store.settled_reminders_for_split(row["id"], ""):
                if previous["recurrence_id"] and previous["occurrence_start_utc"] == start_key:
                    store.carry_settled_reminder(previous, row["id"], start_key, "", previous["fire_at_utc"],
                        notice_id_for(row["id"], start_key, previous["offset_min"]))
        if any(k in fields for k in ("start_utc", "end_utc", "status", "rrule")) and not row.get("master_id"):
            # a moved single event: its queue is replanned; for an exception the queue row stays and is re-read at delivery
            store.drop_reminders_for(row["id"])


def _sibling_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in changes.items() if k in ("start_utc", "end_utc", "all_day", "tz", "title", "description", "location", "rrule", "status")}


def _group_masters(store, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The event and its linked copies — never the stored exception rows that share the link_group_id."""
    group = row.get("link_group_id") or ""
    return store.group_masters(group) if group else [row]


def _read_only_preflight(store, rows: List[Dict[str, Any]], assignments: List[Dict[str, Any]]) -> set:
    """Read-only external calendars are refused before any local mutation; returns the ids that must be skipped."""
    blocked = set()
    for target in rows:
        disputed = next((r for r in [target] + store.exceptions_for(target['id'])
                         if r.get('sync_state') == 'conflict'
                         or any(i['state'] == INTENT_CONFLICT for i in store.intents_for_event(r['id']))), None)
        if disputed:
            blocked.add(target['id'])
            assignments.append({'calendar_id': target['calendar_id'], 'event_id': disputed['id'],
                                'status': INTENT_CONFLICT, 'message': 'сначала cal_events(id=' + disputed['id'] + '), затем выбор local/provider'})
            continue
        cal = store.get_calendar(target["calendar_id"])
        if cal is not None and cal["provider"] != PROVIDER_LOCAL and not cal.get("writable", True):
            blocked.add(target["id"])
            assignments.append({"calendar_id": target["calendar_id"], "calendar_name": cal["name"], "event_id": target["id"], "status": INTENT_FAILED,
                                "message": "календарь только для чтения"})
    return blocked


def _series_delta(master: Dict[str, Any], changes: Dict[str, Any]) -> Optional[timedelta]:
    """How far a whole series moves when its master start changes (None when the start does not change)."""
    if not master.get("rrule") or not changes.get("start_utc"):
        return None
    old, new = parse_stored(master.get("start_utc")), parse_stored(changes.get("start_utc"))
    if old is None or new is None or old == new:
        return None
    return new - old


def _shift_instant(dt: datetime, delta: timedelta, all_day: bool, tz) -> datetime:
    """Timed rows move by the exact delta; all-day rows move by whole calendar days in their zone (DST-safe)."""
    if not all_day:
        return dt + delta
    days = int(round(delta.total_seconds() / 86400.0))
    local = dt.astimezone(tz).date() + timedelta(days=days)
    return datetime.combine(local, datetime.min.time(), tzinfo=tz)


def _shift_series_rows(store, master: Dict[str, Any], delta: timedelta, changes=None) -> List[Dict[str, Any]]:
    """Move relative slots, but keep explicitly supplied absolute date sets.

    Provider snapshots may replace EXDATE/RDATE while moving the anchor. Owner
    moves that omit those sets still shift the existing sets with the series.
    """
    from model import get_tz
    tz = get_tz(master.get("tz") or "")
    all_day = bool(master.get("all_day"))
    def shifted(csv: str) -> str:
        out = []
        for x in str(csv or "").split(","):
            dt = parse_stored(x) if x else None
            out.append(iso_utc(_shift_instant(dt, delta, all_day, tz)) if dt else x)
        return ",".join(v for v in out if v)
    fields = {}
    if master.get("exdates") and 'exdates' not in (changes or {}):
        fields["exdates"] = shifted(master["exdates"])
    if master.get("rdates") and 'rdates' not in (changes or {}):
        fields["rdates"] = shifted(master["rdates"])
    if fields:
        store.update_event(master["id"], fields)
    moved: List[Dict[str, Any]] = []
    for exc in store.exceptions_for(master["id"]):
        rid, s, e = parse_stored(exc.get("recurrence_id")), parse_stored(exc.get("start_utc")), parse_stored(exc.get("end_utc"))
        upd = {}
        if rid is not None:
            upd["recurrence_id"] = iso_utc(_shift_instant(rid, delta, all_day, tz))
        if s is not None and e is not None:
            upd.update({"start_utc": iso_utc(_shift_instant(s, delta, all_day, tz)), "end_utc": iso_utc(_shift_instant(e, delta, all_day, tz))})
        if upd:
            store.update_event(exc["id"], upd)
            fresh = store.get_event(exc["id"])
            if fresh is not None and (fresh.get("external_id") or fresh.get("href") or fresh.get("master_id")):
                fresh["_previous_recurrence_id"] = exc.get("recurrence_id")   # the provider still knows it under the old slot
                moved.append(fresh)
    return moved


def _shift_for_scope_all(store, row: Dict[str, Any], occ_key: str, scope: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """«All of them at 19:00», said about one occurrence: move the series by the same delta, never re-anchor DTSTART
    to that later date (that would silently drop every earlier occurrence)."""
    if not (occ_key and scope == SCOPE_ALL and changes.get("start_utc")):
        return changes
    exception = next((e for e in store.exceptions_for(row["id"]) if e.get("recurrence_id") == occ_key), None)
    effective_start = exception["start_utc"] if exception else occ_key
    occ_s, new_s = parse_stored(effective_start), parse_stored(changes.get("start_utc"))
    m_s, m_e = parse_stored(row.get("start_utc")), parse_stored(row.get("end_utc"))
    if occ_s is None or new_s is None or m_s is None:
        return changes
    new_e = parse_stored(changes.get("end_utc"))
    duration = (new_e - new_s) if new_e else ((m_e - m_s) if m_e else timedelta(hours=1))
    delta = new_s - occ_s
    return {**changes, "start_utc": iso_utc(m_s + delta), "end_utc": iso_utc(m_s + delta + duration)}


def _ensure_exception(store, master: Dict[str, Any], occ_key: str, changes: Dict[str, Any]) -> str:
    """One stored row per changed occurrence (RECURRENCE-ID); create it on first change."""
    changes = {k: v for k, v in changes.items() if k != "rrule"}   # a single occurrence has no rule of its own
    for exc in store.exceptions_for(master["id"]):
        if str(exc.get("recurrence_id") or "") == occ_key:
            _apply_changes(store, exc, changes)
            return exc["id"]
    m_start, m_end = parse_stored(master["start_utc"]), parse_stored(master["end_utc"])
    occ_start = parse_stored(occ_key) or m_start
    duration = (m_end - m_start) if (m_start and m_end) else timedelta(hours=1)
    row = {k: master.get(k) for k in ("calendar_id", "uid", "visibility", "availability", "is_primary", "title", "description",
                                       "location", "tz", "all_day", "organizer", "attendees_json", "my_response", "origin", "link_group_id")}
    row["reminders_json"] = "[]"  # no own override: later whole-series reminder edits remain effective
    # The exception has NO provider identity of its own: Google resolves the instance id from the master + recurrence key,
    # CalDAV writes it inside the master's resource (roast/triad F01).
    row.update({"start_utc": iso_utc(occ_start), "end_utc": iso_utc(occ_start + duration), "rrule": "", "master_id": master["id"],
                "recurrence_id": occ_key, "external_id": "", "href": "", "etag": "",
                "sync_state": "synced" if not master.get("external_id") and not master.get("href") else "pending"})
    saved = store.insert_event(row)
    _apply_changes(store, saved, changes)
    return saved["id"]


def _count_remaining(master: Dict[str, Any], rrule: str, cut: Optional[datetime]) -> Optional[int]:
    """For a COUNT series: how many occurrences remain from ``cut`` on (None when the rule has no COUNT)."""
    count = None
    for part in str(rrule or "").split(";"):
        if part.upper().startswith("COUNT="):
            try:
                count = int(part.split("=", 1)[1])
            except ValueError:
                return None
    if count is None or cut is None:
        return None
    m_start = parse_stored(master.get("start_utc"))
    if m_start is None:
        return None
    try:
        from dateutil.rrule import rrulestr
        from model import get_tz
        tz = get_tz(master.get("tz") or "")
        rule = rrulestr(str(rrule), dtstart=m_start.astimezone(tz), forceset=True)
        before = sum(1 for occ in rule if occ.astimezone(timezone.utc) < cut.astimezone(timezone.utc))
    except Exception:
        return None
    return count - before


def _truncate_series(store, master: Dict[str, Any], occ_key: str, keep_later_exceptions: bool = False) -> List[Dict[str, Any]]:
    """Series ends before ``occ_key``: UNTIL = occurrence start − 1s (RFC 5545, UTC). Returns the detached later exceptions."""
    occ = parse_stored(occ_key)
    if occ is None:
        return []
    if master.get("all_day"):
        from model import get_tz
        until = (occ.astimezone(get_tz(master.get("tz") or "")).date() - timedelta(days=1)).strftime("%Y%m%d")
    else:
        until = (occ - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parts = [p for p in str(master.get("rrule") or "").split(";") if p and not p.upper().startswith(("UNTIL=", "COUNT="))]
    parts.append(f"UNTIL={until}")
    # A following-delete must also remove explicit extra dates past the cut;
    # RRULE truncation alone leaves RDATE occurrences live at the source.
    fields = {"rrule": ";".join(parts)}
    for field in ("rdates", "exdates"):
        fields[field] = ",".join(x for x in str(master.get(field) or "").split(",") if x and x < occ_key)
    store.update_event(master["id"], fields)
    later = []
    for exc in store.exceptions_for(master["id"]):
        if str(exc.get("recurrence_id") or "") >= occ_key:
            if keep_later_exceptions:
                later.append(exc)
            else:
                store.delete_event(exc["id"])
    store.drop_reminders_for(master["id"])
    return later


def _split_series(store, master: Dict[str, Any], occ_key: str, changes: Dict[str, Any], new_group: str = "", split_uid: str = "") -> List[Dict[str, Any]]:
    """«Starting from this date»: truncate the old series, start a new one carrying the changes.

    Later exceptions move to the new series (their date keeps the edit, the slot follows the new wall time);
    linked copies split together under one new link group.
    """
    original_rrule = str(master.get("rrule") or "")
    later = _truncate_series(store, master, occ_key, keep_later_exceptions=True)
    m_start, m_end = parse_stored(master["start_utc"]), parse_stored(master["end_utc"])
    occ_start = parse_stored(occ_key) or m_start
    duration = (m_end - m_start) if (m_start and m_end) else timedelta(hours=1)
    cut_exception = next((exc for exc in later if str(exc.get("recurrence_id") or "") == occ_key), None)
    cut_start = parse_stored(cut_exception.get("start_utc")) if cut_exception else None
    # A moved cut has two times: its original recurrence slot and its effective
    # start. Even when a caller submits the displayed start with an end edit,
    # only a change from the effective start moves the following series.
    cut_effective = cut_start or occ_start
    requested_start = parse_stored(changes.get("start_utc")) or cut_effective
    shift = requested_start - cut_effective
    new_start = occ_start + shift
    requested_end = parse_stored(changes.get("end_utc"))
    new_duration = (requested_end - requested_start) if requested_end else duration
    new_end = new_start + new_duration
    new_cut_start = cut_effective + shift
    cut_end = parse_stored(cut_exception.get("end_utc")) if cut_exception else None
    new_cut_end = requested_end or ((cut_end + shift) if cut_end else (new_cut_start + duration))
    rule_parts = [p for p in original_rrule.split(";") if p and not p.upper().startswith(("UNTIL=", "COUNT="))]
    count_left = _count_remaining(master, original_rrule, occ_start)
    if count_left is not None:
        rule_parts.append(f"COUNT={max(1, count_left)}")   # a COUNT series keeps its remaining occurrences, it does not become endless
    # An UNTIL series also has a finite end. Keep its original bound for a
    # content-only split; shift that bound with an explicit time move.
    until_part = next((p.split("=", 1)[1] for p in original_rrule.split(";") if p.upper().startswith("UNTIL=")), "")
    # EXDATE/RDATE at/after the cut belong to the new series (shifted with it); the old one keeps only the earlier ones
    row = {k: master.get(k) for k in ("calendar_id", "visibility", "availability", "is_primary", "title", "description", "location", "tz",
                                       "all_day", "organizer", "attendees_json", "my_response", "reminders_json", "origin")}
    from model import get_tz
    split_tz = get_tz(master.get("tz") or "")
    split_all_day = bool(master.get("all_day"))
    if until_part:
        if shift:
            try:
                if len(until_part) == 8:
                    until_day = datetime.strptime(until_part, "%Y%m%d").date()
                    moved_day = until_day + timedelta(days=round(shift.total_seconds() / 86400))
                    until_part = moved_day.strftime("%Y%m%d")
                else:
                    until_dt = datetime.strptime(until_part, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                    until_part = (until_dt + shift).strftime("%Y%m%dT%H%M%SZ")
            except ValueError:
                pass  # retain a malformed legacy bound; never turn a finite rule into an endless one
        rule_parts.append(f"UNTIL={until_part}")
    def _shift_csv(csv: str) -> str:
        out = []
        for x in str(csv or "").split(","):
            if x and x >= occ_key:
                dt = parse_stored(x)
                out.append(iso_utc(_shift_instant(dt, shift, split_all_day, split_tz)) if dt is not None else x)
        return ",".join(out)
    row.update({"uid": split_uid or new_uid(), "start_utc": iso_utc(new_start), "end_utc": iso_utc(new_end), "link_group_id": new_group,
                "exdates": _shift_csv(master.get("exdates")), "rdates": _shift_csv(master.get("rdates")),
                "rrule": changes.get("rrule") or ";".join(rule_parts), "sync_state": "synced"})
    saved = store.insert_event(row)
    # A following edit replaces the series ID, not the occurrence's history.
    # Carry already sent/uncertain notices into the new identity before the
    # caller replans: an unchanged meeting must not get a second push just
    # because its title or future segment changed.
    from reminders import notice_id_for
    later_by_original = {str(exc.get("recurrence_id") or ""): exc for exc in later}
    for previous in store.settled_reminders_for_split(master["id"], occ_key):
        old_effective = parse_stored(previous["occurrence_start_utc"])
        old_original = parse_stored(previous["recurrence_id"] or previous["occurrence_start_utc"])
        old_fire = parse_stored(previous["fire_at_utc"])
        if old_effective is None or old_original is None or old_fire is None:
            continue
        if not previous["recurrence_id"] and old_effective < occ_start and not any(
                str(exc.get("start_utc") or "") == iso_utc(old_effective) for exc in later):
            continue  # an identity-less earlier occurrence is not in this segment
        candidates = {iso_utc(old_original)} if old_original >= occ_start else set()
        if not previous["recurrence_id"]:
            # Legacy rows lost original-slot identity. An exception sharing
            # the effective time may have been the one sent; conservatively
            # preserve both possible terminal identities, never resend either.
            candidates.update(str(exc.get("recurrence_id") or "") for exc in later
                              if str(exc.get("start_utc") or "") == iso_utc(old_effective))
        for old_key in candidates:
            original_dt = parse_stored(old_key)
            if original_dt is None:
                continue
            exception = later_by_original.get(old_key)
            if exception is not None and old_key != occ_key:
                # Later exceptions retain their explicit start when the master
                # moves; only their recurrence key follows the new series wall time.
                effective = iso_utc(old_effective)
                fire_at = iso_utc(old_fire)
            elif exception is not None:
                # The cut exception retains its own effective time unless it moves.
                effective = iso_utc(new_cut_start)
                fire_at = iso_utc(new_cut_start - timedelta(minutes=int(previous["offset_min"])))
            else:
                effective = iso_utc(_shift_instant(old_effective, shift, split_all_day, split_tz))
                fire_at = iso_utc(_shift_instant(old_fire, shift, split_all_day, split_tz))
            # The owner chose a fresh reminder for a genuinely moved meeting.
            # A content-only split retains sent/unknown history; a changed effective
            # start has a new notification slot, not a retry of the old send.
            if effective != iso_utc(old_effective):
                continue
            original = iso_utc(_shift_instant(original_dt, shift, split_all_day, split_tz))
            store.carry_settled_reminder(previous, saved["id"], effective, original, fire_at,
                                        notice_id_for(saved["id"], effective, previous["offset_min"], original))
    _apply_changes(store, saved, {k: v for k, v in changes.items() if k not in ("start_utc", "end_utc", "rrule")})
    moved: List[Dict[str, Any]] = []
    for exc in later:
        old_key = parse_stored(exc.get("recurrence_id"))
        if old_key is None:
            continue
        new_key = _shift_instant(old_key, shift, split_all_day, split_tz)
        upd = {"master_id": saved["id"], "recurrence_id": iso_utc(new_key), "uid": saved["uid"], "link_group_id": new_group,
               "external_id": "", "href": "", "etag": "", "sync_state": "synced" if not master.get("external_id") and not master.get("href") else "pending"}
        if str(exc.get("recurrence_id") or "") == occ_key:
            # Its recurrence key follows the segment, while its individual
            # effective time remains separate from the series anchor.
            if changes.get("start_utc") or changes.get("end_utc"):
                upd.update({"start_utc": iso_utc(new_cut_start), "end_utc": iso_utc(new_cut_end)})
            upd.update({k: v for k, v in changes.items() if k in
                        ("title", "description", "location", "visibility", "availability") and v is not None})
        store.update_event(exc["id"], upd)
        moved.append(store.get_event(exc["id"]))
    # the moved exceptions are written to the provider after the new master exists (they resolve through it)
    return [store.get_event(master["id"]), store.get_event(saved["id"]), *moved]


# ── intents ─────────────────────────────────────────────────────────

def _recover_failed_create(store, birth) -> bool:
    """A new owner edit may recover a retained, transient-unknown creation.

    Reuse the original desired snapshot and version, not the edited display row
    or an ETag imported by sync. Only the creation's verified ACK releases its
    successor. Attempts survive so a retry cannot masquerade as first revival.
    Called inside the owner's staging transaction; no provider I/O here.
    """
    if birth['state'] != INTENT_FAILED:
        return False
    payload = json.loads(birth.get('payload_json') or '{}')
    previous = json.loads(birth.get('result_json') or '{}')
    if not payload.get('desired') or previous.get('kind') not in ('network', 'server', 'retry'):
        return False
    store.settle_intent(birth['id'], INTENT_PENDING,
                        {'note': 'owner edit requests original creation recovery', 'previous': previous})
    return True


def _predecessor(store, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Ordinary event ordering uses durable insertion order, not wall-clock time."""
    writes = store.intents_for_event(intent['event_id'])
    index = next((n for n, i in enumerate(writes) if i['id'] == intent['id']), len(writes))
    older = [i for i in writes[:index] if i['state'] != 'resolved']
    if older:
        return older[-1]
    event = json.loads(intent.get('payload_json') or '{}').get('desired') or store.get_event(intent['event_id'])
    if event and event.get("master_id"):
        with store._conn() as c:
            writes = [dict(i) for i in c.execute("SELECT * FROM intents WHERE event_id=? "
                      "AND kind IN ('create','update') AND state!='resolved' "
                      "AND rowid<(SELECT rowid FROM intents WHERE id=?) ORDER BY rowid",
                      (event['master_id'], intent['id']))]
        if writes:
            return writes[-1]
    return None


def _blocked_by(store, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The predecessor when it is still pending (wait), or when it died while this intent was already queued (do not run);
    a NEW intent the owner issued after the failure is a fresh attempt and passes."""
    payload = json.loads(intent.get('payload_json') or '{}')
    dependency = payload.get('after_intent_id')
    for earlier in payload.get('prior_intent_ids', []):
        predecessor = store.get_intent(earlier)
        if predecessor is None or predecessor['state'] != INTENT_DONE:
            return predecessor or {'id': earlier, 'state': INTENT_FAILED}
    if dependency:
        predecessor = store.get_intent(dependency)
        if predecessor is None or predecessor['state'] != INTENT_DONE:
            return predecessor or {'id': dependency, 'state': INTENT_FAILED}
    event = store.get_event(intent['event_id'])
    if event and not event.get('external_id') and not event.get('href'):
        births = [i for i in store.intents_for_event(event['id']) if i['kind'] == 'create'
                  and i['state'] != 'resolved' and json.loads(i['payload_json']).get('after_intent_id')]
        if births and births[0]['id'] != intent['id'] and births[0]['state'] != INTENT_DONE:
            return births[0]
    pred = _predecessor(store, intent)
    if pred is None:
        return None
    if pred.get("state") == INTENT_PENDING:
        return pred
    if pred.get("state") in (INTENT_FAILED, INTENT_CONFLICT):
        def _op(i: Dict[str, Any]) -> str:
            try:
                return str((json.loads(i.get("payload_json") or "{}") or {}).get("op_id") or "")
            except ValueError:
                return ""
        mine, theirs = _op(intent), _op(pred)
        if mine and theirs and mine == theirs:
            return pred   # the tail of the same owner command: it shares the failure
        return None       # a new command is a fresh attempt (and the way out of a stuck event)
    return None


def _fail_after_predecessor(store, intent: Dict[str, Any], pred: Dict[str, Any]) -> Dict[str, Any]:
    message = f"предыдущая операция по событию не выполнена ({pred.get('state')}): сначала разберись с ней"
    store.settle_intent(intent["id"], INTENT_FAILED, {"error": message, "blocked_by": pred["id"]})
    store.update_event(intent["event_id"], {"sync_state": "failed"})
    return {"status": INTENT_FAILED, "message": message}


def _superseded_exception_write(store, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Deletion withdraws queued exception edits, not create/delete recovery."""
    if intent["kind"] not in ("update", "rsvp"):
        return None
    event = store.get_event(intent["event_id"])
    if event is None or not event.get("master_id"):
        return None
    master = store.get_event(event["master_id"])
    if (event.get("deleted_at") or event.get("sync_state") == "pending_delete" or store.delete_requested(event["id"])
            or master is None or master.get("deleted_at") or master.get("sync_state") == "pending_delete"
            or store.delete_requested(master["id"])):
        return event
    return None


def execute_intent(store, providers, intent: Dict[str, Any], owner: str) -> Dict[str, Any]:
    """Lease and run one intent now; returns the per-assignment status dict."""
    if _superseded_exception_write(store, intent) is None:
        pred = _blocked_by(store, intent)
        if pred is not None:
            if pred.get("state") == INTENT_PENDING:
                return {"status": INTENT_PENDING, "message": "ждёт предыдущую операцию по этому событию"}
            return _fail_after_predecessor(store, intent, pred)
    leased = store.lease_intent(intent["id"], owner, LEASE_SECONDS)
    if leased is None:
        return {"status": INTENT_PENDING, "message": "операция уже выполняется"}
    return run_leased_intent(store, providers, leased)


def created_content_matches(requested, observed):
    """Compare the serialized creation's meaning, not provider-added metadata.

    A matching lost-create readback acknowledges only that creation. It never
    licenses rewriting a resource another client has since changed.
    """
    if not requested or not observed:
        return False
    for key in ('title', 'description', 'location', 'start_utc', 'end_utc',
                'all_day', 'rrule', 'rdates', 'exdates', 'status', 'availability'):
        if (requested.get(key) or '') != (observed.get(key) or ''):
            return False
    def contains(wanted, actual):
        if isinstance(wanted, dict):
            return isinstance(actual, dict) and all(contains(v, actual.get(k)) for k, v in wanted.items())
        if isinstance(wanted, list):
            return isinstance(actual, list) and len(wanted) == len(actual) and all(contains(a,b) for a,b in zip(wanted,actual))
        return (wanted or '') == (actual or '')
    return all(contains(json.loads(requested.get(k) or '[]'), json.loads(observed.get(k) or '[]'))
               for k in ('attendees_json', 'reminders_json'))


def _matches_unversioned_baseline(payload, remote, adapter):
    """A first version pin cannot overwrite fields changed while we were offline.

    Ordinary Google exception PATCHes are sparse. Compare the fields they will
    replace, not unrelated provider additions; subsequent retries use the pin.
    """
    baseline = payload.get('baseline')
    if not baseline:
        return False
    if payload.get('response'):
        email = str(getattr(adapter, 'email', '')).lower()
        def response(row):
            people = json.loads(row.get('attendees_json') or '[]')
            own = next((p for p in people if p.get('self') or str(p.get('email', '')).lower() == email), {})
            return str(row.get('my_response') or own.get('status') or own.get('responseStatus') or 'needsAction').lower()
        return response(baseline) == response(remote)
    changes = payload.get('changes') or {}
    keys = set(changes) | {'status'}
    if any(k in keys for k in ('start_utc', 'end_utc', 'all_day', 'tz')):
        keys.update(('start_utc', 'end_utc', 'all_day'))
        keys.discard('tz')  # equivalent zone spellings do not change the instants
    for key in keys:
        field = {'attendees': 'attendees_json', 'reminders': 'reminders_json'}.get(key, key)
        if field.endswith('_json'):
            old, new = json.loads(baseline.get(field) or '[]'), json.loads(remote.get(field) or '[]')
            if old != new:
                return False
        elif field == 'availability':
            # Google has transparent/opaque, not the local soft-routine state.
            old = AVAIL_FREE if baseline.get(field) == AVAIL_FREE else AVAIL_BUSY
            new = AVAIL_FREE if remote.get(field) == AVAIL_FREE else AVAIL_BUSY
            if old != new:
                return False
        elif (baseline.get(field) or '') != (remote.get(field) or ''):
            return False
    return True


def run_leased_intent(store, providers, intent: Dict[str, Any]) -> Dict[str, Any]:
    current = store.current_leased_intent(intent)
    if current is None:
        return {'status': INTENT_PENDING, 'message': 'операция больше не принадлежит этой попытке'}
    intent = current
    if _superseded_exception_write(store, intent) is not None:
        store.settle_intent(intent["id"], INTENT_DONE, {"note": "superseded by deletion"})
        return {"status": INTENT_DONE, "message": "исключение или серия удалены"}
    event = store.get_event(intent["event_id"])
    cal = store.get_calendar(intent["calendar_id"])
    adapter = providers.adapter_for(intent["account_id"]) if providers else None
    if event is None or cal is None:
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": "event or calendar vanished"})
        return {"status": INTENT_FAILED, "message": "событие или календарь исчезли"}
    pred = _blocked_by(store, intent)
    if pred is not None:
        if pred.get("state") != INTENT_PENDING:
            return _fail_after_predecessor(store, intent, pred)
        store.park_intent(intent["id"], 60, "waiting for an earlier write")   # waiting is not an attempt
        return {"status": INTENT_PENDING, "message": "ждёт предыдущую операцию по этому событию"}
    if adapter is None:
        reason = (getattr(providers, "errors", {}) or {}).get(cal["account_id"]) or "нет секрета или гранта"
        store.park_intent(intent["id"], 300, f"аккаунт не подключён: {reason}")   # waiting for a secret/grant is not an attempt
        return {"status": INTENT_PENDING, "message": f"аккаунт не подключён: {reason}"}
    try:
        payload = json.loads(intent.get("payload_json") or "{}")
    except ValueError:
        payload = {}
    desired = payload.get('desired')
    if desired:
        identity = {k: event.get(k) for k in ('external_id','href','etag','deleted_at')}
        event = {**event, **desired, **identity}
    if payload.get('relocated_from_master') and not payload.get('_provider_external_id'):
        # An old-resource ACK cannot re-address a relocated override backwards.
        event.update(external_id='', href='', etag='')
    if event.get("master_id"):   # every exception write resolves through its master (CalDAV: the master's resource; Google: instance id)
        master = store.get_event(event["master_id"])
        event = {**event, "master_external_id": (master or {}).get("external_id") or "", "master_href": (master or {}).get("href") or "",
                 "master_etag": (master or {}).get("etag") or ""}
    modes = store.get_setting("reminder_mode") or {}
    payload["mute_provider_reminders"] = bool(modes.get(str(cal["id"])))
    expected = intent.get('expected_etag') or ''
    payload['_expected_etag'] = expected
    try:
        if cal['provider'] == 'google' and intent['kind'] != 'create' and (event.get('external_id') or event.get('master_id')):
            if payload.get('_provider_external_id'):
                event['external_id'] = payload['_provider_external_id']
            if not expected:
                if not desired and int(intent.get('attempts') or 0) > 1:
                    raise ProviderError('conflict', 'исход прежней безусловной записи неизвестен; нужна сверка')
                observed = adapter.read_current(cal, event, payload)
                remote = observed.get('event') or {}
                if not remote.get('external_id') or not observed.get('etag'):
                    raise ProviderError('conflict', 'Google не подтвердил событие и ETag; запись не отправлена')
                if not _matches_unversioned_baseline(payload, remote, adapter):
                    raise ProviderError('conflict', 'событие изменилось до первой отправки; сравни версии перед выбором')
                pinned = store.pin_provider_version(intent, remote['external_id'], observed['etag'])
                if pinned is None:
                    return {'status': INTENT_PENDING, 'message': 'lease изменилась; запись не отправлена'}
                intent = pinned
                payload.update(json.loads(intent['payload_json']))
                expected = intent['expected_etag']
                event['external_id'] = payload.get('_provider_external_id') or event.get('external_id')
            payload['_expected_etag'] = expected
        if intent["kind"] == "create":
            res = adapter.create(cal, event, payload)
            # The provider may answer after an owner delete. CAS the new identity
            # and the tombstone in one SQLite transaction, never revive the row.
            if res.get("existed"):
                store.record_created_event(event['id'], res)
                if res.get('cancelled') and (int(intent['attempts']) == 1 or expected):
                    pinned = store.pin_provider_version(intent, res.get('external_id') or '', res.get('etag') or '')
                    if pinned is None:
                        return {'status': INTENT_PENDING, 'message': 'lease изменилась; восстановление не отправлено'}
                    intent, expected = pinned, pinned.get('expected_etag') or ''
                    written = adapter.update(cal, {**event, **res}, expected,
                                             {**payload, '_revive_cancelled': True, 'send_updates': False})
                    res = {**res, 'etag': written.get('etag') or ''}
                elif not res.get('matches'):
                    raise ProviderError('conflict', 'прежнее создание найдено, но содержимое изменилось; нужна сверка')
        elif intent["kind"] == "update":
            res = adapter.update(cal, event, expected, payload)
        elif intent["kind"] == "delete":
            if not event.get("external_id") and not event.get("href"):
                creates = [i for i in store.intents_for_event(event["id"]) if i["kind"] == "create"]
                if creates and any(i["state"] != INTENT_DONE for i in creates):
                    # One definite first-attempt rejection proves no remote create.
                    # A prior transient attempt could have succeeded despite its
                    # lost response: retain the tombstone for manual reconciliation.
                    def definitely_rejected(create):
                        try:
                            kind = json.loads(create.get("result_json") or "{}").get("kind")
                        except ValueError:
                            return False
                        return (create["state"] == INTENT_FAILED and int(create.get("attempts") or 0) == 1
                                and kind in ("auth", "forbidden", "unsupported", "parse", "http"))
                    if all(definitely_rejected(create) for create in creates):
                        store.delete_event(event["id"], hard=True)
                        store.settle_intent(intent["id"], INTENT_DONE, {"note": "original create definitely rejected"})
                        return {"status": INTENT_DONE, "message": "создание было отклонено, локальная запись удалена"}
                    if any(create["state"] == INTENT_PENDING for create in creates):
                        # A create that may still be in flight is settled only by its own outcome.
                        store.park_intent(intent["id"], 60, "waiting for the create outcome")
                        return {"status": INTENT_PENDING, "message": "ждёт исхода создания у провайдера"}
                    lookup = getattr(adapter, "lookup_created", None)
                    if not callable(lookup):
                        store.settle_intent(intent["id"], INTENT_CONFLICT, {"error": "remote create outcome or identity unknown"})
                        return {"status": INTENT_CONFLICT, "message": "создание у провайдера не подтверждено; удаление требует сверки"}
                    # The create wrote an identity derived from the event's UID: ask about exactly that one
                    # instead of guessing from a lost answer (a transient error here is retried like any write).
                    found = lookup(cal, event)
                    if found is None:
                        store.delete_event(event["id"], hard=True)
                        store.settle_intent(intent["id"], INTENT_DONE, {"note": "provider confirmed the create never landed"})
                        return {"status": INTENT_DONE, "message": "у провайдера события нет, локальная запись удалена"}
                    store.record_created_event(event["id"], found)   # the tombstone stays: deletion is still requested
                    event = {**event, **found}
                    pinned = store.pin_provider_version(intent, found.get('external_id') or '', found.get('etag') or '')
                    if pinned is None:
                        return {'status': INTENT_PENDING, 'message': 'lease изменилась; удаление не отправлено'}
                    intent, expected = pinned, pinned.get('expected_etag') or ''
            adapter.delete(cal, event, expected, payload)
            store.delete_event(event["id"], hard=True)
        elif intent["kind"] == "rsvp":
            if not event.get("external_id") and not event.get("href") and not event.get("master_external_id") and not event.get("master_href"):
                return _defer(store, intent, "ответ отправится после записи события у провайдера", kind="retry")
            res = adapter.respond(cal, event, payload)
        else:
            store.settle_intent(intent["id"], INTENT_FAILED, {"error": f"unknown intent kind {intent['kind']}"})
            return {"status": INTENT_FAILED, "message": "неизвестная операция"}
    except ProviderError as exc:
        if exc.kind == "conflict":
            # Conflict stays visible to the owner; the deletion intent, not this
            # mutable display state, remains the source of deletion intent.
            store.update_event(event["id"], {"sync_state": "conflict"})
            store.settle_intent(intent["id"], INTENT_CONFLICT, {"error": exc.message})
            return {"status": INTENT_CONFLICT, "message": exc.message}
        if exc.kind in ("not_found", "gone") and intent["kind"] == "delete":
            store.delete_event(event["id"], hard=True)
            store.settle_intent(intent["id"], INTENT_DONE, {"note": "already gone"})
            return {"status": INTENT_DONE, "message": "уже удалено на сервере"}
        if exc.kind in ("network", "server", "retry"):
            return _defer(store, intent, exc.message, kind=exc.kind)
        store.update_event(event["id"], {"sync_state": "pending_delete" if intent["kind"] == "delete" else "failed"})
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": exc.message, "kind": exc.kind})
        if exc.kind == "auth":
            store.set_account_status(intent["account_id"], "auth_failed", exc.message)
        return {"status": INTENT_FAILED, "message": exc.message}
    except Exception as exc:  # adapter bug: disclose, do not loop forever
        store.update_event(event["id"], {"sync_state": "pending_delete" if intent["kind"] == "delete" else "failed"})
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": f"{type(exc).__name__}: {exc}"})
        return {"status": INTENT_FAILED, "message": f"внутренняя ошибка адаптера: {type(exc).__name__}"}
    if intent['kind'] in ('create','update','rsvp'):
        if not store.acknowledge_write(intent, event, res, expected, shared_resource=cal['provider']=='yandex'):
            return {'status': INTENT_PENDING, 'message': 'ответ провайдера не привязан к текущей попытке; нужна сверка'}
    else:
        store.settle_intent(intent["id"], INTENT_DONE, {"ok": True})
    return {"status": INTENT_DONE}


def _defer(store, intent: Dict[str, Any], message: str, kind: str = "network") -> Dict[str, Any]:
    attempts = int(intent.get("attempts") or 1)
    if kind == "retry" and attempts >= MAX_ATTEMPTS:
        store.settle_intent(intent["id"], INTENT_FAILED, {"error": message, "kind": kind, "attempts": attempts})
        store.update_event(intent["event_id"], {"sync_state": "pending_delete" if intent["kind"] == "delete" else "failed"})
        return {"status": INTENT_FAILED, "message": f"{message} (попытки исчерпаны)"}
    delay = RETRY_BACKOFF_SEC[min(max(attempts, 1), len(RETRY_BACKOFF_SEC)) - 1]
    store.settle_intent(intent["id"], INTENT_PENDING, {"error": message, "kind": kind}, retry_in_sec=delay)
    return {"status": INTENT_PENDING, "message": f"{message}; повтор через {delay} с"}


def retry_due_intents(store, providers, owner: str = "companion", limit: int = 20) -> List[Dict[str, Any]]:
    out = []
    for intent in store.lease_due_intents(owner, limit=limit, seconds=LEASE_SECONDS):
        out.append({"intent": intent["id"], **run_leased_intent(store, providers, intent)})
    return out


def _stage_reassign_event(store, row: Dict[str, Any], calendar_ids: List[str]) -> Dict[str, Any]:
    """Change the set of calendars an event lives in (12 A: only on explicit command).

    Copies are added/removed per the target calendars' ``publish_mode``; the primary row stays where it is —
    moving the primary itself is a delete + create the owner asks for explicitly.
    """
    row = store.get_event(row["id"]) or row
    if row.get("master_id"):
        row = store.get_event(row["master_id"]) or row   # an occurrence id means its whole series
    clicked_cal = store.get_calendar(row["calendar_id"])
    op_id = new_id("op")
    group = row.get("link_group_id") or ""
    members = store.group_masters(group) if group else [row]
    row = next((m for m in members if m.get("is_primary")), row)   # a clicked copy still reassigns around the primary
    own_cal = store.get_calendar(row["calendar_id"])
    for cal_ in (clicked_cal, own_cal):
        if cal_ is not None and cal_["provider"] != PROVIDER_LOCAL and not cal_.get("writable", True):
            return {"added": [], "removed": [], "kept_primary": row["calendar_id"], "failed": "календарь события только для чтения — его копии не назначаются"}
    current = {m["calendar_id"]: m for m in members}
    added, removed = [], []
    wanted = [c for c in calendar_ids if c]
    # A copy awaiting provider deletion cannot be silently counted as kept.
    # Re-adding it needs a fresh command after the old outcome is known.
    for cid in wanted:
        if cid in current and store.delete_requested(current[cid]["id"]):
            return {"added": [{"calendar_id": cid, "status": INTENT_CONFLICT,
                                "message": "удаление копии ещё не подтверждено; дождись сверки перед повторным назначением"}],
                    "removed": [], "kept_primary": row["calendar_id"], "failed": "pending_delete"}

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
        # a series copy carries the primary's exceptions (moved/cancelled dates), times only for busy copies
        exception_rows = []
        for exc in store.exceptions_for(row["id"]):
            exc_copy = {k: exc.get(k) for k in ("visibility", "availability", "start_utc", "end_utc", "tz", "all_day", "recurrence_id", "status")}
            exc_copy.update({"calendar_id": cid, "uid": saved["uid"], "master_id": saved["id"], "is_primary": 0, "link_group_id": group,
                             "title": copy["title"] if mode == PUBLISH_BUSY else exc.get("title") or copy["title"],
                             "description": "" if mode == PUBLISH_BUSY else exc.get("description") or "",
                             "location": "" if mode == PUBLISH_BUSY else exc.get("location") or "",
                             "attendees_json": "[]", "reminders_json": "[]", "origin": "local", "rrule": "",
                             "sync_state": "synced" if cal["provider"] == PROVIDER_LOCAL else "pending"})
            exception_rows.append(store.insert_event(exc_copy))
        if cal["provider"] == PROVIDER_LOCAL:
            added.append({"calendar_id": cid, "calendar_name": cal["name"], "event_id": saved["id"], "status": INTENT_DONE})
            continue
        intent = store.add_intent("create", cal["account_id"], cid, saved["id"], {"send_updates": False}, op_id=op_id)
        outcome = {"status": INTENT_PENDING, "_intent_ids": [intent["id"]]}
        for exc_row in exception_rows:
            # A copied override is an edit of the newly created master's ordinary
            # instance, not an empty PATCH. Preserve its pre-edit comparison and
            # explicit birth dependency through offline replay and resolution.
            baseline = _baseline_before({saved['id']: saved}, exc_row)
            changes = {key: exc_row[key] for key in ('title', 'description', 'location', 'availability',
                       'start_utc', 'end_utc', 'tz', 'all_day', 'status')
                       if exc_row.get(key) != baseline.get(key)}
            store.add_intent("update", cal["account_id"], cid, exc_row["id"],
                             {"scope": SCOPE_THIS, "changes": changes, "baseline": baseline,
                              "after_intent_id": intent['id'], "send_updates": False},
                             scope=SCOPE_THIS, op_id=op_id)
        added.append({"calendar_id": cid, "calendar_name": cal["name"], "event_id": saved["id"], **outcome})
    for cid, m in current.items():
        if cid in wanted or m["id"] == row["id"]:
            continue
        res = _stage_delete_event(store, m["id"], scope=SCOPE_ALL, cascade=False)   # this copy only, never the group
        removed.append({"calendar_id": cid, "calendar_name": m.get("calendar_name"), "event_id": m["id"],
                        "assignments": res.get("assignments") or [{"calendar_id": cid, "event_id": m["id"], "status": INTENT_DONE}]})
    return {"added": added, "removed": removed, "kept_primary": row["calendar_id"]}
