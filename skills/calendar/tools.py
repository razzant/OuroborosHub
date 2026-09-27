"""Agent-facing tools (8) and the shared request context used by tools and widget routes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ops
import reminders as rem
from model import (
    AVAIL_BUSY, AVAIL_FREE, AVAIL_SOFT, DEFAULT_LOCAL_CALENDAR_ID, MAX_EVENTS, MAX_WINDOW_DAYS, PROVIDER_GOOGLE,
    PROVIDER_LOCAL, PROVIDER_YANDEX, REMINDERS_DEFAULT, SCOPES, SECRET_KEYS, SCOPE_FOLLOWING, SCOPE_THIS, VISIBILITY_HIDDEN, VISIBILITY_SHOWN,
    WEEKDAY_LABELS,
    bounded_result, day_bounds, event_public, get_tz, iso_local, iso_utc, json_dumps, label, now_utc, parse_input,
    parse_stored, parse_working_hours, tz_name,
)
from providers import Providers
from store import Store

CONFIRM_MESSAGE = "Запись делается только по явной команде владельца или после его выбора: повтори вызов с confirm=true"


class Context:
    """Everything a tool or route needs: store, tz, providers built from granted secrets."""

    def __init__(self, api):
        self.api = api
        self.state_dir = api.get_state_dir()
        self.store = Store(self.state_dir)
        self.tz = get_tz(self.store.get_setting("timezone") or "")
        try:
            self.secrets = api.get_settings(list(SECRET_KEYS)) or {}
        except Exception:
            self.secrets = {}
        self.providers = Providers(self.secrets, self.state_dir)

    # ── helpers ─────────────────────────────────────────────────────

    def now(self) -> datetime:
        return now_utc().astimezone(self.tz)

    def occurrences(self, start: datetime, end: datetime, calendar_ids: Optional[Sequence[str]] = None,
                    include_hidden: bool = True) -> List[Dict[str, Any]]:
        rows = self.store.window(start, end, calendar_ids, include_hidden=include_hidden)
        return ops.expand(rows, start, end, self.store.exceptions_for, owner_tz=self.tz)

    def resolve_calendars(self, spec: Any) -> Tuple[List[str], str]:
        """Names, aliases, ids, provider words, 'all', 'busy_set', 'default' → ordered calendar ids."""
        cals = self.store.list_calendars()
        if spec in (None, "", [], "default"):
            return [self.store.default_calendar()["id"]], ""
        items = spec if isinstance(spec, list) else [p.strip() for p in str(spec).replace(";", ",").split(",") if p.strip()]
        out: List[str] = []
        for item in items:
            key = str(item).strip()
            low = key.lower()
            if low == "all":
                # 5 A: «во все календари» = календарь по умолчанию + набор публикации занятости, не все с правом записи
                out.append(self.store.default_calendar()["id"])
                out.extend(c["id"] for c in cals if c["role_publish"] and c["writable"])
                continue
            if low in ("busy_set", "занятость", "publish"):
                # same meaning as 'all' (5 A): the default calendar plus the saved busy-publication set
                out.append(self.store.default_calendar()["id"])
                out.extend(c["id"] for c in cals if c["role_publish"] and c["writable"])
                continue
            if low == "default":
                out.append(self.store.default_calendar()["id"])
                continue
            exact = [c for c in cals if c["id"] == key]
            if exact:
                out.append(exact[0]["id"])
                continue
            by_name = [c for c in cals if c["name"].lower() == low]
            if len(by_name) == 1:
                out.append(by_name[0]["id"])
                continue
            accounts = {a["id"]: a for a in self.store.list_accounts()}
            by_alias = [c for c in cals if (accounts.get(c["account_id"], {}).get("alias") or "").lower() == low]
            by_provider = [c for c in cals if c["provider"] == low]
            pool = by_alias or by_provider or [c for c in cals if low in c["name"].lower()]
            if len(pool) == 1:
                out.append(pool[0]["id"])
            elif len(pool) > 1:
                defaults = [c for c in pool if c["is_default"]] or pool
                if len(defaults) == 1:
                    out.append(defaults[0]["id"])
                else:
                    return [], (f"«{key}» подходит к нескольким календарям: " + ", ".join(f"{c['name']} ({c['id']})" for c in pool[:6])
                                + ". Уточни, какой нужен")
            else:
                return [], f"календарь «{key}» не найден; см. cal_status → calendars"
        seen, ordered = set(), []
        for cid in out:
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)
        return ordered, ""

    def parse_range(self, start: Any, end: Any, default_days: int = 1) -> Tuple[datetime, datetime]:
        today_start, _ = day_bounds(self.now().date(), self.tz)
        s, _ = parse_input(start, self.tz, today_start)
        e, _ = parse_input(end, self.tz, None)
        if e is None or e <= s:
            e = s + timedelta(days=default_days)
        if (e - s).days > MAX_WINDOW_DAYS:
            e = s + timedelta(days=MAX_WINDOW_DAYS)
        return s, e

    def companion_health(self) -> Dict[str, Any]:
        path = os.path.join(self.state_dir, "companion_health.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"state": "unknown", "message": "companion ещё не отчитывался"}
        seen = parse_stored(data.get("ts"))
        age = (now_utc() - seen).total_seconds() if seen else None
        data["age_sec"] = int(age) if age is not None else None
        data["state"] = "alive" if age is not None and age < 300 else "stale"
        return data


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "да", "on")


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
        return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    return [value]


# ── tools ───────────────────────────────────────────────────────────

def cal_status(ctx: Context, **kwargs) -> str:
    st = ctx.store
    now = ctx.now()
    accounts = st.list_accounts()
    cals = st.list_calendars()
    day_s, day_e = day_bounds(now.date(), ctx.tz)
    today = ctx.occurrences(day_s, day_e)
    counts = st.intent_counts()
    channel = st.get_setting("notify_channel_state") or {"state": "unknown"}
    next_steps: List[str] = []
    if ctx.providers.yandex_error:
        next_steps.append(ctx.providers.yandex_error)
    if not ctx.secrets.get("YANDEX_CALDAV_ACCOUNTS") and not any(a["provider"] == PROVIDER_YANDEX for a in accounts):
        next_steps.append("Яндекс не подключён: владелец кладёт YANDEX_CALDAV_ACCOUNTS в Settings → Secrets, затем cal_settings(action='reload_yandex')")
    if not ctx.secrets.get("GOOGLE_CALENDAR_CLIENT_ID"):
        next_steps.append("Google не подключён: нужны GOOGLE_CALENDAR_CLIENT_ID/SECRET и CALENDAR_TOKEN_KEY в Settings → Secrets (этап Google)")
    for a in accounts:
        if a["provider"] == PROVIDER_LOCAL:
            continue
        ctx.providers.adapter_for(a["id"])   # fills providers.errors with the concrete reason when there is none
        reason = ctx.providers.errors.get(a["id"])
        if reason:
            next_steps.append(f"{a['alias'] or a['id']}: адаптер недоступен — {reason}")
        elif a["status"] != "ok":
            next_steps.append(f"{a['alias'] or a['id']}: {a['status']} — {a.get('last_error') or ''}".strip())
    if counts.get("conflict"):
        next_steps.append(f"{counts['conflict']} операций в конфликте с внешней версией: см. cal_events(id=…) и реши, что оставить")
    if channel.get("state") in ("no_route", "unknown"):
        next_steps.append("Доставка напоминаний без модели ждёт маршрут /notify в ядре: напоминания хранятся и видны в виджете")
    elif channel.get("state") == "no_grant":
        next_steps.append("Маршрут /notify есть, но гранта нет: владелец выдаёт разрешение notify_owner скиллу calendar в Skills")
    elif channel.get("state") == "token_rejected":
        next_steps.append("Хост отверг токен скилла для фонового процесса: выключи и включи скилл (toggle), при устаревшем ревью — обнови аттестацию")
    unknown_reminders = st.unknown_reminder_count()
    if unknown_reminders:
        next_steps.append(f"У {unknown_reminders} напоминаний исход отправки неизвестен; автоматического повтора нет во избежание дублей")
    return bounded_result({
        "status": "ok",
        "timezone": tz_name(ctx.tz), "now_local": now.replace(microsecond=0).isoformat(), "today": now.date().isoformat(),
        "weekday": WEEKDAY_LABELS[now.weekday()],
        "accounts": [{"id": a["id"], "provider": a["provider"], "alias": a["alias"], "status": a["status"], "last_sync": a.get("last_sync"),
                      "last_error": (a.get("last_error") or "")[:160],
                      **({"adapter_error": ctx.providers.errors[a["id"]][:160]} if ctx.providers.errors.get(a["id"]) else {})} for a in accounts],
        "calendars": [{"id": c["id"], "name": c["name"], "account_id": c["account_id"], "provider": c["provider"], "writable": bool(c["writable"]),
                       "visible": bool(c["role_visible"]), "busy_source": bool(c["role_busy"]), "publish_busy": bool(c["role_publish"]),
                       "publish_mode": c["publish_mode"], "default": bool(c["is_default"])} for c in cals],
        "events_today": len([o for o in today if o.get("visibility") != VISIBILITY_HIDDEN]),
        "hidden_today": len([o for o in today if o.get("visibility") == VISIBILITY_HIDDEN]),
        "pending_writes": counts.get("pending", 0), "conflicts": counts.get("conflict", 0), "failed_writes": counts.get("failed", 0),
        "reminders": {"rules": rem.get_rules(st), "channel": channel, "upcoming": rem.upcoming(st, ctx.tz, limit=5),
                      "unknown_count": unknown_reminders},
        "settings": {"working_hours": st.get_setting("working_hours") or "09:00-19:00", "preferences": st.get_setting("preferences") or "",
                     "busy_publish_set": [c["id"] for c in cals if c["role_publish"]]},
        "companion": ctx.companion_health(),
        "next_step": next_steps + _companion_secret_gap(ctx),
        "hint": "Времена — ISO 8601 с offset владельца; «сегодня/завтра» считай от now_local. Скрытые (служебные) события — распорядок владельца, их видно тебе, а в виджете по переключателю.",
    })


def _companion_secret_gap(ctx: Context) -> List[str]:
    """The companion inherits granted secrets at spawn; a newly added key needs a skill restart (toggle off/on)."""
    health = ctx.companion_health()
    seen = health.get("secrets_present") or {}
    missing = [k for k in SECRET_KEYS if ctx.secrets.get(k) and seen and not seen.get(k)]
    if missing:
        return [f"Фоновый процесс ещё не видит секреты {', '.join(missing)}: выключи и включи скилл calendar в Skills (toggle), чтобы он перезапустился"]
    return []


def cal_events(ctx: Context, start: str = "", end: str = "", calendars: Any = None, query: str = "", id: str = "",
               include_hidden: bool = True, limit: int = MAX_EVENTS, offset: int = 0, **kwargs) -> str:
    st = ctx.store
    if id:
        base, occ = ops._series_id(str(id))
        row = st.get_event(base)
        if row is None or row.get("deleted_at"):
            return json_dumps({"status": "not_found", "message": f"событие {id} не найдено"})
        out = event_public(row, ctx.tz, compact=False)
        if occ:
            occ_dt = parse_stored(occ)
            live = [o for o in ops.expand([row], occ_dt - timedelta(minutes=1), occ_dt + timedelta(days=2), st.exceptions_for, owner_tz=ctx.tz)
                    if str(o.get("recurrence_id") or "") == occ or str(o.get("start_utc") or "") == occ] if occ_dt else []
            if live:
                out.update(event_public({**row, **live[0], "id": f"{row['id']}@{occ}"}, ctx.tz, compact=False))
                out["series_id"] = row["id"]
            out["occurrence_start"] = iso_local(live[0]["start_utc"], ctx.tz) if live else iso_local(occ, ctx.tz)
        out["assignments"] = [{"calendar_id": m["calendar_id"], "calendar_name": m.get("calendar_name"), "event_id": m["id"],
                               "sync_state": m.get("sync_state")} for m in st.group_masters(row.get("link_group_id") or "")] if row.get("link_group_id") else []
        out["intents"] = [{"kind": i["kind"], "state": i["state"], "attempts": i["attempts"], "result": i.get("result_json")} for i in st.intents_for_event(base)[-5:]]
        return bounded_result({"status": "ok", "event": out})
    s, e = ctx.parse_range(start, end)
    cal_ids: Optional[List[str]] = None
    if calendars:
        cal_ids, err = ctx.resolve_calendars(calendars)
        if err:
            return json_dumps({"status": "error", "message": err})
    occs = ctx.occurrences(s, e, cal_ids, include_hidden=_bool(include_hidden, True))
    if query:
        q = str(query).lower()
        occs = [o for o in occs if q in (o.get("title") or "").lower() or q in (o.get("description") or "").lower() or q in (o.get("location") or "").lower()]
    total = len(occs)
    offset = max(0, _int(offset, 0))
    limit = max(1, min(_int(limit, MAX_EVENTS), MAX_EVENTS))
    page = occs[offset:offset + limit]
    conflicts = _conflicts(page)
    return bounded_result({
        "status": "ok", "timezone": tz_name(ctx.tz), "range": {"start": iso_local(s, ctx.tz), "end": iso_local(e, ctx.tz)},
        "events": [event_public(o, ctx.tz) for o in page], "conflicts": conflicts, "total": total,
        "next_offset": offset + limit if offset + limit < total else None,
    })


def _conflicts(occs: Sequence[Dict[str, Any]]) -> List[List[str]]:
    hard = [o for o in occs if (o.get("availability") or AVAIL_BUSY) == AVAIL_BUSY and str(o.get("status") or "") != "cancelled"]
    out: List[List[str]] = []
    for i, a in enumerate(hard):
        for b in hard[i + 1:]:
            if a.get("link_group_id") and a.get("link_group_id") == b.get("link_group_id"):
                continue
            sa, ea, sb, eb = parse_stored(a["start_utc"]), parse_stored(a["end_utc"]), parse_stored(b["start_utc"]), parse_stored(b["end_utc"])
            if sa and ea and sb and eb and sa < eb and sb < ea:
                out.append([a["id"], b["id"]])
    return out[:20]


def _spec_from_args(ctx: Context, start: Any, end: Any, duration_min: Any, all_day: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    s, is_date = parse_input(start, ctx.tz)
    if s is None:
        return None, "нужно время начала в ISO 8601, например 2026-09-26T16:00 (без offset = местное время владельца)"
    all_day = _bool(all_day, is_date)
    e, _ = parse_input(end, ctx.tz)
    if all_day:
        s = s.astimezone(ctx.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if e is None or e <= s:
            e = s + timedelta(days=1)
        else:
            e = e.astimezone(ctx.tz).replace(hour=0, minute=0, second=0, microsecond=0)
            if e <= s:
                e = s + timedelta(days=1)
    elif e is None or e <= s:
        e = s + timedelta(minutes=max(5, _int(duration_min, 60)))
    return {"start_utc": iso_utc(s), "end_utc": iso_utc(e), "all_day": all_day, "tz": tz_name(ctx.tz)}, ""


def cal_create(ctx: Context, title: str = "", start: str = "", end: str = "", duration_min: int = 60, all_day: bool = False,
               calendars: Any = None, hidden: bool = False, availability: str = "", description: str = "", location: str = "",
               attendees: Any = None, send_invites: bool = False, reminders: Any = None, rrule: str = "", confirm: bool = False, **kwargs) -> str:
    if not str(title or "").strip():
        return json_dumps({"status": "error", "message": "нужно название события"})
    spec, err = _spec_from_args(ctx, start, end, duration_min, all_day)
    if err:
        return json_dumps({"status": "error", "message": err})
    cal_ids, err = ctx.resolve_calendars(calendars)
    if err:
        return json_dumps({"status": "error", "message": err})
    names = [ctx.store.get_calendar(c)["name"] for c in cal_ids]
    if not _bool(confirm):
        return bounded_result({"status": "needs_confirm", "message": CONFIRM_MESSAGE, "target_calendars": names,
                               "preview": {"title": str(title)[:300], "start": iso_local(spec["start_utc"], ctx.tz), "end": iso_local(spec["end_utc"], ctx.tz)}})
    avail = str(availability or "").lower() or (AVAIL_SOFT if _bool(hidden) and "обычно" in str(title).lower() else AVAIL_BUSY)
    if avail not in (AVAIL_BUSY, AVAIL_FREE, AVAIL_SOFT):
        avail = AVAIL_BUSY
    spec.update({"title": str(title).strip(), "calendar_ids": cal_ids, "visibility": VISIBILITY_HIDDEN if _bool(hidden) else VISIBILITY_SHOWN,
                 "availability": avail, "description": str(description or ""), "location": str(location or ""),
                 "attendees": [{"email": str(a).strip()} if not isinstance(a, dict) else a for a in _list(attendees)],
                 "send_invites": _bool(send_invites), "reminders": None if reminders is None else [_int(x, 0) for x in _list(reminders)], "rrule": str(rrule or "").strip()})
    result = ops.create_event(ctx.store, ctx.providers, spec)
    ev = result["event"]
    if ev is None:
        return bounded_result({"status": "error", "message": "ни один календарь не принял событие", "assignments": result["assignments"]})
    s, e = parse_stored(ev["start_utc"]), parse_stored(ev["end_utc"])
    overlaps = [event_public(o, ctx.tz) for o in ctx.occurrences(s, e) if o.get("id") != ev["id"] and (o.get("availability") or AVAIL_BUSY) == AVAIL_BUSY
                and o.get("link_group_id", "") != (ev.get("link_group_id") or "x")][:5]
    warning = _plan_reminders(ctx)
    statuses = {a["status"] for a in result["assignments"]}
    status = "created" if statuses <= {"done"} else ("pending" if "pending" in statuses else ("conflict" if "conflict" in statuses else "created_partially"))
    return bounded_result({"status": status, "message": _assignment_message("Создано", ev, result["assignments"], ctx.tz),
                           "event": event_public(ev, ctx.tz, compact=False), "assignments": result["assignments"], "overlaps": overlaps,
                           **({"warning": warning} if warning else {})})


def cal_update(ctx: Context, id: str = "", start: str = "", end: str = "", duration_min: Any = None, all_day: Any = None, title: str = "",
               description: Any = None, location: Any = None, hidden: Any = None, availability: str = "", calendars: Any = None,
               attendees: Any = None, reminders: Any = None, rrule: Any = None, response: str = "", scope: str = SCOPE_THIS,
               send_updates: bool = False, confirm: bool = False, **kwargs) -> str:
    if not id:
        return json_dumps({"status": "error", "message": "нужен id события (из cal_events)"})
    base, occ = ops._series_id(str(id))
    row = ctx.store.get_event(base)
    if row is None or row.get("deleted_at"):
        return json_dumps({"status": "not_found", "message": f"событие {id} не найдено"})
    scope = str(scope or SCOPE_THIS).lower()
    if scope not in SCOPES:
        return json_dumps({"status": "error", "message": "scope: this | following | all"})
    if row.get("rrule") and not occ and scope == SCOPE_THIS:
        return json_dumps({"status": "ambiguous", "message": "это повторяющееся событие: укажи id конкретного вхождения (…@дата) для одной даты, "
                                                                "scope='all' для всей серии или scope='following' с id вхождения — «начиная с этой даты»"})
    if scope == SCOPE_FOLLOWING and not occ and not row.get("master_id"):
        return json_dumps({"status": "error", "message": "scope='following' требует id вхождения (…@дата): с какой даты менять"})
    changes: Dict[str, Any] = {}
    if start or end or duration_min is not None:
        old_s, old_e = parse_stored(occ or row["start_utc"]), None
        old_dur = (parse_stored(row["end_utc"]) - parse_stored(row["start_utc"])) if row.get("start_utc") and row.get("end_utc") else timedelta(hours=1)
        new_s, is_date = parse_input(start, ctx.tz, old_s)
        new_e, _ = parse_input(end, ctx.tz, None)
        if duration_min is not None and new_e is None:
            new_e = new_s + timedelta(minutes=max(5, _int(duration_min, 60)))
        if new_e is None or new_e <= new_s:
            new_e = new_s + old_dur
        changes.update({"start_utc": iso_utc(new_s), "end_utc": iso_utc(new_e)})
        if all_day is not None or is_date:
            changes["all_day"] = _bool(all_day, is_date)
    if title not in (None, ""):
        changes["title"] = str(title)
    for key, value in (("description", description), ("location", location)):
        if value is not None:
            changes[key] = str(value)   # "" is an explicit «clear it»
    if hidden is not None:
        changes["visibility"] = VISIBILITY_HIDDEN if _bool(hidden) else VISIBILITY_SHOWN
    if availability:
        changes["availability"] = str(availability).lower()
    if attendees is not None:
        changes["attendees"] = [{"email": str(a).strip()} if not isinstance(a, dict) else a for a in _list(attendees)]
    if reminders is not None and not (isinstance(reminders, str) and not reminders.strip()):   # a blank string is «not given»
        # [] is an explicit «no reminders for this event»; 'default' returns it to the calendar/default rule
        is_default = isinstance(reminders, str) and reminders.strip().lower() == REMINDERS_DEFAULT
        changes["reminders"] = REMINDERS_DEFAULT if is_default else [_int(x, 0) for x in _list(reminders)]
    if rrule is not None:
        changes["rrule"] = str(rrule)
    if response:
        resp = str(response).lower()
        if resp not in ("accepted", "declined", "tentative"):
            return json_dumps({"status": "error", "message": "response: accepted | declined | tentative"})
        changes["my_response"] = resp
    if not changes and not calendars:
        return json_dumps({"status": "error", "message": "нечего менять"})
    if not _bool(confirm):
        return bounded_result({"status": "needs_confirm", "message": CONFIRM_MESSAGE, "event": event_public(row, ctx.tz), "changes": changes, "scope": scope})
    result = {"status": "ok", "event": None, "assignments": []}
    if changes:
        result = ops.update_event(ctx.store, ctx.providers, str(id), changes, scope=scope, send_updates=_bool(send_updates))
    if calendars:
        cal_ids, err = ctx.resolve_calendars(calendars)
        if err:
            return json_dumps({"status": "error", "message": err})
        target_row = ctx.store.get_event(result.get("split_master_id") or base) or row
        result["reassign"] = ops.reassign_event(ctx.store, ctx.providers, target_row, cal_ids)
        result["assignments"] = list(result["assignments"]) + list(result["reassign"]["added"]) \
            + [{**a, "action": "removed"} for r in result["reassign"]["removed"] for a in r["assignments"]]
    warning = _plan_reminders(ctx)
    ev = result.get("event") or ctx.store.get_event(base)
    statuses = {a["status"] for a in result["assignments"]}
    status = "updated" if statuses <= {"done"} else ("pending" if "pending" in statuses else "conflict" if "conflict" in statuses else "updated_partially")
    return bounded_result({"status": status, "message": _assignment_message("Обновлено", ev, result["assignments"], ctx.tz),
                           "event": event_public(ev, ctx.tz, compact=False) if ev else None, "assignments": result["assignments"],
                           "reassign": result.get("reassign"), **({"warning": warning} if warning else {})})


def cal_delete(ctx: Context, id: str = "", scope: str = SCOPE_THIS, send_updates: bool = False, confirm: bool = False, **kwargs) -> str:
    if not id:
        return json_dumps({"status": "error", "message": "нужен id события"})
    base, occ = ops._series_id(str(id))
    row = ctx.store.get_event(base)
    if row is None or row.get("deleted_at"):
        return json_dumps({"status": "not_found", "message": f"событие {id} не найдено"})
    scope = str(scope or SCOPE_THIS).lower()
    if row.get("rrule") and not occ and scope == SCOPE_THIS:
        return json_dumps({"status": "ambiguous", "message": "повторяющееся событие: уточни у владельца — удалить только эту дату (id вхождения), "
                                                                "всё расписание (scope='all') или начиная с даты (scope='following')"})
    if scope == SCOPE_FOLLOWING and not occ and not row.get("master_id"):
        return json_dumps({"status": "error", "message": "scope='following' требует id вхождения (…@дата): с какой даты удалять"})
    if not _bool(confirm):
        return bounded_result({"status": "needs_confirm", "message": CONFIRM_MESSAGE, "event": event_public(row, ctx.tz), "scope": scope})
    result = ops.delete_event(ctx.store, ctx.providers, str(id), scope=scope, send_updates=_bool(send_updates))
    statuses = {a["status"] for a in result["assignments"]}
    status = "deleted" if statuses <= {"done"} else ("pending" if "pending" in statuses else ("conflict" if "conflict" in statuses else "deleted_partially"))
    return bounded_result({"status": status, "message": _assignment_message("Удалено", row, result["assignments"], ctx.tz), "assignments": result["assignments"]})


def cal_free(ctx: Context, start: str = "", end: str = "", duration_min: int = 60, sources: Any = None, working_hours: str = "", max_slots: int = 6, **kwargs) -> str:
    now = ctx.now()
    s, e = ctx.parse_range(start or now.replace(microsecond=0).isoformat(), end, default_days=3)
    if s < now and not start:
        s = now
    cal_ids: Optional[List[str]]
    if sources:
        cal_ids, err = ctx.resolve_calendars(sources)
        if err:
            return json_dumps({"status": "error", "message": err})
    else:
        cal_ids = [c["id"] for c in ctx.store.list_calendars() if c["role_busy"]]
    occs = ctx.occurrences(s, e, cal_ids or None)
    hard, soft = ops.busy_intervals(occs)
    wh = parse_working_hours(working_hours or ctx.store.get_setting("working_hours") or "")
    slots = ops.free_windows(hard, s, e, timedelta(minutes=max(5, _int(duration_min, 60))), wh, ctx.tz, max_slots=max(1, min(_int(kwargs.get("max", max_slots), 6), 10)))
    soft_hits = []
    for slot_s, slot_e in slots:
        for ss, se, title in soft:
            if ss < slot_e and slot_s < se:
                soft_hits.append({"slot_start": iso_local(slot_s, ctx.tz), "usually": title, "usually_start": iso_local(ss, ctx.tz), "usually_end": iso_local(se, ctx.tz)})
    return bounded_result({
        "status": "ok", "timezone": tz_name(ctx.tz), "range": {"start": iso_local(s, ctx.tz), "end": iso_local(e, ctx.tz)},
        "duration_min": _int(duration_min, 60), "working_hours": f"{wh[0]:%H:%M}-{wh[1]:%H:%M}",
        "sources": cal_ids, "slots": [{"start": iso_local(a, ctx.tz), "end": iso_local(b, ctx.tz)} for a, b in slots],
        "soft_overlaps": soft_hits[:10],
        "next_step": "Предложи владельцу 2–4 варианта (escalate, recommended = первый без soft_overlaps), затем cal_create с confirm=true",
    })


def cal_reminders(ctx: Context, action: str = "list", offsets: Any = None, calendar_id: str = "", confirm: bool = False, **kwargs) -> str:
    st = ctx.store
    action = str(action or "list").lower()
    if action == "list":
        return bounded_result({"status": "ok", "rules": rem.get_rules(st), "modes": st.get_setting(rem.MODE_KEY) or {},
                               "channel": st.get_setting("notify_channel_state") or {"state": "unknown"},
                               "upcoming": rem.upcoming(st, ctx.tz, limit=20),
                               "hint": "Внешний календарь получает напоминания Уробороса только после enable_calendar (20 A). "
                                       "Напоминания конкретного события — cal_update(reminders=[…]); 0 = в момент начала, "
                                       "[] = у этого события без напоминаний, 'default' = снова по правилам"})
    if not _bool(confirm):
        return json_dumps({"status": "needs_confirm", "message": "Изменение правил напоминаний — по команде владельца: повтори с confirm=true"})
    mins = [_int(x, 0) for x in _list(offsets)] if offsets is not None else None
    if action == "set_default":
        rules = rem.set_rules(st, default=mins or [])
    elif action == "set_hidden":
        rules = rem.set_rules(st, hidden=mins or [])
    elif action == "set_calendar":
        cal_ids, err = ctx.resolve_calendars(calendar_id)
        if err or not cal_ids:
            return json_dumps({"status": "error", "message": err or "нужен calendar_id"})
        rules = rem.set_rules(st, calendar_id=cal_ids[0], calendar_offsets=mins or [])
    elif action == "clear_calendar":
        cal_ids, err = ctx.resolve_calendars(calendar_id)
        if err or not cal_ids:
            return json_dumps({"status": "error", "message": err or "нужен calendar_id"})
        rules = rem.set_rules(st, calendar_id=cal_ids[0], calendar_offsets=None)
    elif action in ("enable_calendar", "disable_calendar"):
        cal_ids, err = ctx.resolve_calendars(calendar_id)
        if err or not cal_ids:
            return json_dumps({"status": "error", "message": err or "нужен calendar_id"})
        report = set_reminder_mode(ctx, cal_ids[0], action == "enable_calendar")
        _plan_reminders(ctx, replan=True)
        return bounded_result({"status": "updated", **report, "rules": rem.get_rules(st), "upcoming": rem.upcoming(st, ctx.tz, limit=10)})
    else:
        return json_dumps({"status": "error", "message": "action: list | set_default | set_hidden | set_calendar | clear_calendar | enable_calendar | disable_calendar"})
    _plan_reminders(ctx, replan=True)
    return bounded_result({"status": "updated", "rules": rules, "upcoming": rem.upcoming(st, ctx.tz, limit=10)})


def set_reminder_mode(ctx: Context, calendar_id: str, on: bool) -> Dict[str, Any]:
    """20 A for one external calendar: import the provider's default reminders as the calendar rule, keep per-event
    alerts as Ouroboros reminders, then silence the provider (calendar defaults now, per-event overrides on next write)."""
    st = ctx.store
    cal = st.get_calendar(calendar_id)
    if cal is None:
        return {"error": "календарь не найден"}
    if cal["provider"] == PROVIDER_LOCAL:
        return {"calendar_id": calendar_id, "note": "локальный календарь и так напоминает через Уроборос"}
    report: Dict[str, Any] = {"calendar_id": calendar_id, "mode": "on" if on else "off", "provider_muted": False, "manual_steps": []}
    if not on:
        saved = st.get_setting(f"google_default_reminders:{calendar_id}") or []
        adapter = ctx.providers.adapter_for(cal["account_id"])
        if saved and adapter is not None and hasattr(adapter, "set_default_reminders"):
            try:
                adapter.set_default_reminders(cal, saved)
                report["provider_restored"] = saved
            except ops.ProviderError as exc:
                # neither side would remind: keep Ouroboros on and say so
                report.update({"mode": "on", "status": "failed",
                               "message": f"режим оставлен включённым: не удалось вернуть напоминания провайдера — {exc.message}"})
                return report
        else:
            report["manual_steps"].append("напоминания провайдера, снятые при включении режима, восстанови в его настройках вручную")
        rem.set_mode(st, calendar_id, False)
        return report
    modes = rem.set_mode(st, calendar_id, True)
    defaults = st.get_setting(f"google_default_reminders:{calendar_id}") or []
    minutes = sorted({int(d.get("minutes")) for d in defaults if isinstance(d, dict) and d.get("minutes") is not None})
    if minutes:
        rem.set_rules(st, calendar_id=calendar_id, calendar_offsets=minutes)
        report["imported_default"] = minutes
    adapter = ctx.providers.adapter_for(cal["account_id"])
    if adapter is not None and hasattr(adapter, "set_default_reminders"):
        try:
            adapter.set_default_reminders(cal, [])
            report["provider_muted"] = True
        except ops.ProviderError as exc:
            report["manual_steps"].append(f"не удалось отключить напоминания по умолчанию у провайдера: {exc.message}")
    else:
        report["manual_steps"].append("Яндекс: напоминания по умолчанию календаря отключаются в настройках Яндекс.Календаря вручную; напоминания событий снимутся при следующей записи")
    report["manual_steps"].append("Apple Calendar / другие приложения на устройствах имеют свои настройки уведомлений — их Уроборос не меняет")
    return report


def cal_settings(ctx: Context, action: str = "get", calendar_id: str = "", visible: Any = None, busy_source: Any = None, publish_busy: Any = None,
                 publish_mode: str = "", default: Any = None, name: str = "", working_hours: str = "", timezone: str = "", preferences: Any = None,
                 confirm: bool = False, **kwargs) -> str:
    st = ctx.store
    action = str(action or "get").lower()
    if action == "get":
        return bounded_result({"status": "ok", "timezone": tz_name(ctx.tz), "working_hours": st.get_setting("working_hours") or "09:00-19:00",
                               "preferences": st.get_setting("preferences") or "",
                               "calendars": [{"id": c["id"], "name": c["name"], "provider": c["provider"], "visible": bool(c["role_visible"]),
                                              "busy_source": bool(c["role_busy"]), "publish_busy": bool(c["role_publish"]), "publish_mode": c["publish_mode"],
                                              "default": bool(c["is_default"]), "writable": bool(c["writable"])} for c in st.list_calendars()],
                               "accounts": st.list_accounts(), "secrets_present": {k: bool(ctx.secrets.get(k)) for k in SECRET_KEYS},
                               "actions": ["set", "set_calendar", "new_local_calendar", "reload_yandex", "connect_google", "reload_google", "disconnect", "sync_now"]})
    if not _bool(confirm):
        return json_dumps({"status": "needs_confirm", "message": "Изменение настроек календаря — по команде владельца: повтори с confirm=true", "action": action})
    if action == "set":
        if working_hours:
            parse_working_hours(working_hours)
            st.set_setting("working_hours", str(working_hours))
        if timezone:
            st.set_setting("timezone", str(timezone))
        if preferences is not None:
            st.set_setting("preferences", str(preferences)[:4000])
        return json_dumps({"status": "updated", "working_hours": st.get_setting("working_hours"), "timezone": st.get_setting("timezone") or tz_name(ctx.tz),
                           "preferences": st.get_setting("preferences") or ""})
    if action == "set_calendar":
        cal_ids, err = ctx.resolve_calendars(calendar_id)
        if err or not cal_ids:
            return json_dumps({"status": "error", "message": err or "нужен calendar_id"})
        fields: Dict[str, Any] = {}
        if visible is not None:
            fields["role_visible"] = 1 if _bool(visible) else 0
        if busy_source is not None:
            fields["role_busy"] = 1 if _bool(busy_source) else 0
        if publish_busy is not None:
            fields["role_publish"] = 1 if _bool(publish_busy) else 0
        if publish_mode in ("full", "busy"):
            fields["publish_mode"] = publish_mode
        if name:
            fields["name"] = str(name)
        if default is not None and _bool(default):
            for c in st.list_calendars():
                if c["is_default"]:
                    st.update_calendar(c["id"], {"is_default": 0})
            fields["is_default"] = 1
        st.update_calendar(cal_ids[0], fields)
        return json_dumps({"status": "updated", "calendar": st.get_calendar(cal_ids[0])})
    if action == "new_local_calendar":
        if not name:
            return json_dumps({"status": "error", "message": "нужно name"})
        slug = "".join(ch for ch in name.lower().replace(" ", "_") if ch.isalnum() or ch == "_")[:24] or "cal"
        cid = f"local:{slug}"
        st.upsert_calendar({"id": cid, "account_id": "local", "provider": PROVIDER_LOCAL, "external_id": slug, "name": name, "writable": True})
        return json_dumps({"status": "created", "calendar": st.get_calendar(cid)})
    if action == "reload_yandex":
        return bounded_result(reload_yandex(ctx))
    if action == "connect_google":
        return bounded_result(connect_google(ctx))
    if action == "reload_google":
        return bounded_result(reload_google(ctx))
    if action == "disconnect":
        accounts = [a for a in st.list_accounts() if a["id"] == calendar_id or a["alias"] == calendar_id]
        if not accounts:
            return json_dumps({"status": "not_found", "message": "укажи id аккаунта (см. cal_status → accounts)"})
        acc = accounts[0]
        cancelled = st.cancel_intents_for_account(acc["id"])
        st.delete_account(acc["id"])
        forgot = False
        if acc["provider"] == PROVIDER_GOOGLE:
            import providers_google as gp
            forgot = gp.forget_account(ctx.state_dir, str(ctx.secrets.get("CALENDAR_TOKEN_KEY") or ""), acc.get("login") or acc["id"].split(":", 1)[-1])
        ctx.providers = Providers(ctx.secrets, ctx.state_dir)
        return json_dumps({"status": "updated", "cancelled_intents": cancelled, "tokens_dropped": forgot,
                           "message": f"аккаунт {acc['id']} отключён; локальные события не тронуты, незавершённых записей отменено: {cancelled};"
                                      + (" сохранённые токены Google удалены" if forgot else " секрет удаляет владелец")})
    if action == "sync_now":
        st.set_setting("sync_requested_at", iso_utc(now_utc()))
        return json_dumps({"status": "accepted", "message": "фоновая синхронизация запрошена; результат — в cal_status через минуту"})
    return json_dumps({"status": "error", "message": "неизвестное action"})


def reload_yandex(ctx: Context) -> Dict[str, Any]:
    """Read accounts from the secret, discover calendars, keep owner roles of known calendars."""
    st = ctx.store
    if ctx.providers.yandex_error:
        return {"status": "error", "message": ctx.providers.yandex_error}
    if not ctx.providers.yandex_accounts:
        return {"status": "not_connected", "message": "YANDEX_CALDAV_ACCOUNTS пуст или не выдан скиллу (грант выдаётся после появления ключа в Secrets)"}
    report = []
    for acc in ctx.providers.yandex_accounts:
        account_id = f"{PROVIDER_YANDEX}:{acc['login']}"
        st.upsert_account({"id": account_id, "provider": PROVIDER_YANDEX, "alias": acc["alias"], "login": acc["login"], "status": "ok"})
        adapter = ctx.providers.adapter_for(account_id)
        try:
            cals = adapter.list_calendars()
        except ops.ProviderError as exc:
            st.set_account_status(account_id, "auth_failed" if exc.kind == "auth" else "error", exc.message)
            report.append({"account": account_id, "status": exc.kind, "message": exc.message})
            continue
        for cal in cals:
            existing = st.get_calendar(cal["id"])
            st.upsert_calendar({**cal, "role_visible": existing["role_visible"] if existing else True,
                                "role_busy": existing["role_busy"] if existing else True,
                                "role_publish": existing["role_publish"] if existing else False,
                                "publish_mode": existing["publish_mode"] if existing else "busy"})
        st.set_account_status(account_id, "ok", "", synced=False)
        report.append({"account": account_id, "status": "ok", "calendars": [{"id": c["id"], "name": c["name"], "writable": c["writable"]} for c in cals]})
    st.set_setting("sync_requested_at", iso_utc(now_utc()))
    return {"status": "ok", "accounts": report, "next_step": "Роли календарей (показывать / источник занятости / публикация «Занят») — cal_settings(action='set_calendar', …)"}


def connect_google(ctx: Context) -> Dict[str, Any]:
    """Start the BYO OAuth flow: the owner opens the URL in the system browser; the host route finishes it."""
    import providers_google as gp
    if not ctx.secrets.get("GOOGLE_CALENDAR_CLIENT_ID"):
        return {"status": "not_connected", "message": "Нет GOOGLE_CALENDAR_CLIENT_ID в Settings → Secrets (грант выдаётся после появления ключей)"}
    if not ctx.secrets.get("CALENDAR_TOKEN_KEY"):
        return {"status": "not_connected", "message": "Нет CALENDAR_TOKEN_KEY в Settings → Secrets — им шифруются токены Google"}
    try:
        port = int((ctx.api.get_runtime_info() or {}).get("server_port") or 8765)
    except Exception:
        port = 8765
    redirect_uri = f"http://127.0.0.1:{port}/api/extensions/calendar/oauth/callback"
    try:
        started = gp.start_auth(ctx.store, ctx.secrets, redirect_uri)
    except ops.ProviderError as exc:
        return {"status": "error", "message": exc.message}
    return {"status": "ok", "auth_url": started["auth_url"], "redirect_uri": redirect_uri,
            "message": "Открой ссылку в системном браузере под нужным аккаунтом Google и разреши доступ; после возврата на 127.0.0.1 аккаунт появится в cal_status. "
                       "Если Google откажет из-за redirect_uri — сообщи владельцу: понадобится другой адрес возврата (первое живое подключение — точка проверки).",
            "next_step": "После входа: cal_settings(action='reload_google', confirm=true) при необходимости; роли календарей — set_calendar"}


def reload_google(ctx: Context) -> Dict[str, Any]:
    """Discover calendars of every Google account whose tokens are stored; keep owner roles."""
    import providers_google as gp
    st = ctx.store
    try:
        tokens = gp.load_tokens(ctx.state_dir, str(ctx.secrets.get("CALENDAR_TOKEN_KEY") or ""))
    except ops.ProviderError as exc:
        return {"status": "error", "message": exc.message}
    if not tokens:
        return {"status": "not_connected", "message": "Ни один аккаунт Google ещё не подключён: cal_settings(action='connect_google')"}
    report = []
    for email in tokens:
        account_id = f"{PROVIDER_GOOGLE}:{email}"
        st.upsert_account({"id": account_id, "provider": PROVIDER_GOOGLE, "alias": st.get_account(account_id)["alias"] if st.get_account(account_id) else email.split("@")[0],
                           "login": email, "status": "ok"})
        adapter = ctx.providers.adapter_for(account_id)
        if adapter is None:
            report.append({"account": account_id, "status": "not_connected", "message": ctx.providers.errors.get(account_id, "")})
            continue
        try:
            cals = adapter.list_calendars()
        except ops.ProviderError as exc:
            st.set_account_status(account_id, "auth_failed" if exc.kind == "auth" else "error", exc.message)
            report.append({"account": account_id, "status": exc.kind, "message": exc.message})
            continue
        for cal in cals:
            existing = st.get_calendar(cal["id"])
            st.upsert_calendar({**cal, "role_visible": existing["role_visible"] if existing else True,
                                "role_busy": existing["role_busy"] if existing else True,
                                "role_publish": existing["role_publish"] if existing else False,
                                "publish_mode": existing["publish_mode"] if existing else "busy"})
            st.set_setting(f"google_default_reminders:{cal['id']}", cal.get("default_reminders") or [])
        st.set_account_status(account_id, "ok", "", synced=False)
        report.append({"account": account_id, "status": "ok", "calendars": [{"id": c["id"], "name": c["name"], "writable": c["writable"], "primary": c.get("is_primary", False)} for c in cals]})
    st.set_setting("sync_requested_at", iso_utc(now_utc()))
    return {"status": "ok", "accounts": report}


def _plan_reminders(ctx: Context, replan: bool = False) -> str:
    """Plan the reminder queue; a failure is disclosed to the caller instead of swallowed."""
    try:
        if replan:
            ctx.store.drop_scheduled_reminders()
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e))
        return ""
    except Exception as exc:
        return f"очередь напоминаний не обновлена: {type(exc).__name__}: {exc}"


def _assignment_message(verb: str, ev: Dict[str, Any], assignments: List[Dict[str, Any]], tz) -> str:
    when = label(ev, tz) if ev else ""
    ok = [a.get("calendar_name") or a.get("calendar_id") for a in assignments if a.get("status") == "done"]
    pending = [a.get("calendar_name") or a.get("calendar_id") for a in assignments if a.get("status") == "pending"]
    bad = [f"{a.get('calendar_name') or a.get('calendar_id')}: {a.get('message') or a.get('status')}" for a in assignments if a.get("status") in ("failed", "conflict")]
    parts = [f"{verb}: {when}"]
    if ok:
        parts.append("в: " + ", ".join(str(x) for x in ok))
    if pending:
        parts.append("ожидает синхронизации: " + ", ".join(str(x) for x in pending))
    if bad:
        parts.append("не удалось: " + "; ".join(bad))
    return " · ".join(parts)
