"""Agent-facing tools (8) and the shared request context used by tools and widget routes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ops
import links
import conflicts as conflict_ops
import reminders as rem
from model import (
    AVAIL_BUSY, AVAIL_FREE, AVAIL_SOFT, MAX_EVENTS, MAX_WINDOW_DAYS, PROVIDER_GOOGLE,
    PROVIDER_LOCAL, REMINDERS_DEFAULT, SCOPES, SECRET_KEYS, SCOPE_FOLLOWING, SCOPE_THIS, VISIBILITY_HIDDEN, VISIBILITY_SHOWN,
    WEEKDAY_LABELS,
    bounded_result, day_bounds, event_public, get_tz, iso_local, iso_utc, json_dumps, label, new_id, now_utc, parse_input,
    parse_stored, parse_working_hours, tz_name,
)
from providers import Providers
from accounts import connect_google, reload_google, reload_yandex, _public_base
from store import Store

CONFIRM_MESSAGE = "Это предварительный просмотр. Если действие входит в разовое или постоянное поручение, примени его с confirm=true; при неясности уточни у владельца."


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
                    include_hidden: bool = True, strict: bool = False, failed=None) -> List[Dict[str, Any]]:
        rows = self.store.window(start, end, calendar_ids, include_hidden=include_hidden)
        return ops.expand(rows, start, end, self.store.exceptions_for, owner_tz=self.tz, strict=strict, failed=failed)

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
    account_details = {}
    if ctx.providers.yandex_error:
        next_steps.append(ctx.providers.yandex_error)
    import providers_google as gp
    for a in accounts:
        if a["provider"] == PROVIDER_LOCAL:
            account_details[a['id']] = {'capabilities': {'create': True, 'update': True, 'delete': True, 'sharing': False}}
            continue
        adapter = ctx.providers.adapter_for(a["id"])   # fills providers.errors with the concrete reason when there is none
        account_details[a['id']] = {'capabilities': adapter.calendar_capabilities() if adapter and hasattr(adapter, 'calendar_capabilities') else None,
                                    'server_url': getattr(adapter, 'server_url', '') if adapter else ''}
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
                      'login': a.get('login') or '', **account_details.get(a['id'], {}),
                      "last_error": (a.get("last_error") or "")[:160],
                      **({"adapter_error": ctx.providers.errors[a["id"]][:160]} if ctx.providers.errors.get(a["id"]) else {})} for a in accounts],
        "calendars": [{"id": c["id"], "name": c["name"], "account_id": c["account_id"], "provider": c["provider"], "writable": bool(c["writable"]),
                       "visible": bool(c["role_visible"]), "busy_source": bool(c["role_busy"]), "publish_busy": bool(c["role_publish"]),
                       "publish_mode": c["publish_mode"], "default": bool(c["is_default"])} for c in cals],
        "events_today": len([o for o in today if o.get("visibility") != VISIBILITY_HIDDEN]),
        "hidden_today": len([o for o in today if o.get("visibility") == VISIBILITY_HIDDEN]),
        "pending_writes": counts.get("pending", 0), "conflicts": counts.get("conflict", 0), "failed_writes": counts.get("failed", 0),
        'uncertain_writes': counts.get('uncertain', 0),
        'calendar_operations': [{'request_id': row['id'], 'state': row['state'], 'account_id': row['account_id'],
                                 'calendar_id': row['calendar_id'], 'result': json.loads(row['result_json'])}
                                for row in st.calendar_operations()],
        "reminders": {"rules": rem.get_rules(st), 'modes': st.get_setting(rem.MODE_KEY) or {}, "channel": channel, "upcoming": rem.upcoming(st, ctx.tz, limit=5),
                      "unknown_count": unknown_reminders},
        "settings": {"working_hours": st.get_setting("working_hours") or "09:00-19:00", "preferences": st.get_setting("preferences") or "",
                     'public_base_url': st.get_setting('public_base_url') or '', 'full_preferences': 'cal_settings(action=get)',
                     "busy_publish_set": [c["id"] for c in cals if c["role_publish"]]},
        'connection_options': {'google_client_configured': bool(gp.client_credentials(ctx.secrets)[0]), 'caldav_form': True},
        "companion": ctx.companion_health(),
        "link_issues": st.link_issues(),
        "next_step": next_steps + _companion_secret_gap(ctx),
        "hint": "Времена — ISO 8601 с offset владельца; «сегодня/завтра» считай от now_local. Скрытые (служебные) события — распорядок владельца, их видно тебе, а в виджете по переключателю.",
    })


def _companion_secret_gap(ctx: Context) -> List[str]:
    """The companion inherits granted secrets at spawn; a newly added key needs a skill restart (toggle off/on)."""
    health = ctx.companion_health()
    seen = health.get("secrets_present") or {}
    missing = [k for k in ('YANDEX_CALDAV_ACCOUNTS', 'CALENDAR_TOKEN_KEY') if ctx.secrets.get(k) and seen and not seen.get(k)]
    if missing:
        return [f"Фоновый процесс ещё не видит секреты {', '.join(missing)}: выключи и включи скилл calendar в Skills (toggle), чтобы он перезапустился"]
    return []


def cal_events(ctx: Context, start: str = "", end: str = "", calendars: Any = None, query: str = "", id: str = "",
               include_hidden: bool = True, limit: int = MAX_EVENTS, offset: int = 0, **kwargs) -> str:
    st = ctx.store
    if id:
        base, occ = ops._series_id(str(id))
        row = st.get_event(base)
        if row is None or (row.get('deleted_at') and row.get('sync_state') != 'conflict' and row.get('link_state') != links.MISSING):
            return json_dumps({"status": "not_found", "message": f"событие {id} не найдено"})
        try:
            master, occ = ops.resolve_occurrence(st, row, occ)
        except ValueError:
            if not occ and row.get('master_id') and row.get('sync_state') in ('conflict', 'failed'):
                # A failed structural command may leave an old occurrence key.
                # Its stable stored ID still names the comparison the owner needs.
                master, occ = row, ''
            else:
                return json_dumps({"status": "not_found", "message": f"событие {id} не найдено"})
        out = event_public(row, ctx.tz, compact=False)
        if row.get('link_group_id'):
            out['link'] = links.inspect(st, master)
        if occ:
            exception = next((e for e in st.exceptions_for(master["id"]) if e.get("recurrence_id") == occ), None)
            effective = ops.routine_time(master, exception["start_utc"] if exception else occ, ctx.tz)
            live = [o for o in ops.expand([master], effective - timedelta(minutes=1), effective + timedelta(days=2),
                                          st.exceptions_for, owner_tz=ctx.tz)
                    if o.get("occurrence_start_utc") == occ] if effective else []
            if live:
                out.update(event_public({**master, **live[0], "id": f"{master['id']}@{occ}"}, ctx.tz, compact=False))
                out["series_id"] = master["id"]
            out["occurrence_start"] = iso_local(live[0]["start_utc"], ctx.tz) if live else iso_local(occ, ctx.tz)
        out["assignments"] = [{"calendar_id": m["calendar_id"], "calendar_name": m.get("calendar_name"), "event_id": m["id"],
                               "sync_state": m.get("sync_state")} for m in st.group_masters(row.get("link_group_id") or "")] if row.get("link_group_id") else []
        out["intents"] = [{"kind": i["kind"], "state": i["state"], "attempts": i["attempts"], "result": i.get("result_json")} for i in st.intents_for_event(base)[-5:]]
        inspection = conflict_ops.inspect(ctx, str(id))
        if inspection.get('status') != 'not_conflicted':
            out['conflict'] = inspection
        result = {'status': 'ok', 'event': out}
        if len(json_dumps(result)) > 14000 and out.get('conflict'):
            # A shortened comparison must not carry an approval token.
            out['conflict'].pop('resolution_token', None)
            out['conflict']['comparison_incomplete'] = True
        return bounded_result(result)
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
    try:
        rrule = ops.validate_rrule(rrule, spec["start_utc"], spec["tz"])
    except ValueError as exc:
        return json_dumps({"status": "error", "message": str(exc)})
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
               send_updates: bool = False, confirm: bool = False, resolution: str = '', resolution_token: str = '', link_action: str = '', **kwargs) -> str:
    if link_action:
        row = ctx.store.get_event(ops._series_id(str(id))[0])
        if row is None:
            return json_dumps({'status': 'not_found'})
        if any((start, end, title, calendars, response, resolution, resolution_token)) or any(
                value is not None for value in (duration_min, all_day, description, location, hidden, attendees, reminders, rrule)):
            return json_dumps({'status': 'error', 'message': 'link_action применяется отдельно от изменения содержания'})
        if not _bool(confirm):
            return bounded_result({'status': 'needs_confirm', 'message': CONFIRM_MESSAGE, 'event': event_public(row, ctx.tz),
                                   'link_action': link_action, 'scope': scope, 'link': links.inspect(ctx.store, row)})
        result = ops.link_event(ctx.store, ctx.providers, str(id), str(link_action), scope=scope)
        warning = _plan_reminders(ctx)
        return bounded_result({**result, **({'warning': warning} if warning else {})})
    if resolution or resolution_token:
        if not id:
            return json_dumps({'status':'error', 'message':'нужен id выбранной копии'})
        if any((start, end, title, availability, calendars, response, send_updates, scope != SCOPE_THIS)) or any(
                v is not None for v in (duration_min, all_day, description, location, hidden, attendees, reminders, rrule)) or kwargs:
            return json_dumps({'status':'error', 'message':'resolution применяется отдельно, без новых изменений'})
        if not _bool(confirm):
            return json_dumps({'status':'needs_confirm', 'message':CONFIRM_MESSAGE})
        result = conflict_ops.resolve(ctx, str(id), str(resolution), str(resolution_token))
        warning = _plan_reminders(ctx) if result.get('status') in ('resolved','pending') else ''
        return bounded_result({**result, **({'warning':warning} if warning else {})})
    if not id:
        return json_dumps({"status": "error", "message": "нужен id события (из cal_events)"})
    base, occ = ops._series_id(str(id))
    row = ctx.store.get_event(base)
    if row is None or row.get("deleted_at"):
        return json_dumps({"status": "not_found", "message": f"событие {id} не найдено"})
    try:
        ops.resolve_occurrence(ctx.store, row, occ)
    except ValueError as exc:
        return json_dumps({"status": "error", "message": str(exc)})
    scope = str(scope or SCOPE_THIS).lower()
    if scope not in SCOPES:
        return json_dumps({"status": "error", "message": "scope: this | following | all"})
    if row.get("rrule") and not occ and scope == SCOPE_THIS:
        return json_dumps({"status": "ambiguous", "message": "это повторяющееся событие: укажи id конкретного вхождения (…@дата) для одной даты, "
                                                                "scope='all' для всей серии или scope='following' с id вхождения — «начиная с этой даты»"})
    if scope == SCOPE_FOLLOWING and not occ and not row.get("master_id"):
        return json_dumps({"status": "error", "message": "scope='following' требует id вхождения (…@дата): с какой даты менять"})
    changes: Dict[str, Any] = {}
    if start or end or duration_min is not None or all_day is not None:
        exception = next((e for e in ctx.store.exceptions_for(row["id"]) if e.get("recurrence_id") == occ), None) if occ else None
        effective = exception or row
        old_s = ops.routine_time(row, effective["start_utc"] if exception else (occ or row["start_utc"]), ctx.tz)
        old_dur = (parse_stored(effective["end_utc"]) - parse_stored(effective["start_utc"])) if effective.get("start_utc") and effective.get("end_utc") else timedelta(hours=1)
        new_s, is_date = parse_input(start, ctx.tz, old_s)
        new_e, _ = parse_input(end, ctx.tz, None)
        if duration_min is not None and new_e is None:
            new_e = new_s + timedelta(minutes=max(5, _int(duration_min, 60)))
        if new_e is None or new_e <= new_s:
            new_e = new_s + old_dur
        if start or scope != SCOPE_FOLLOWING:
            changes["start_utc"] = iso_utc(new_s)
        changes["end_utc"] = iso_utc(new_e)
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
        try:
            changes["rrule"] = ops.validate_rrule(rrule, changes.get("start_utc") or row["start_utc"], row.get("tz") or "")
        except ValueError as exc:
            return json_dumps({"status": "error", "message": str(exc)})
    if response:
        resp = str(response).lower()
        if resp not in ("accepted", "declined", "tentative"):
            return json_dumps({"status": "error", "message": "response: accepted | declined | tentative"})
        changes["my_response"] = resp
    if not changes and not calendars:
        return json_dumps({"status": "error", "message": "нечего менять"})
    cal_ids = None
    if calendars:
        cal_ids, err = ctx.resolve_calendars(calendars)
        if err:
            return json_dumps({"status": "error", "message": err})
    if not _bool(confirm):
        return bounded_result({"status": "needs_confirm", "message": CONFIRM_MESSAGE, "event": event_public(row, ctx.tz), "changes": changes, "scope": scope})
    result = {"status": "ok", "event": None, "assignments": []}
    if changes:
        result = ops.update_event(ctx.store, ctx.providers, str(id), changes, scope=scope, send_updates=_bool(send_updates), owner_tz=ctx.tz)
    if cal_ids is not None:
        target_row = ctx.store.get_event(result.get("split_master_id") or base) or row
        result["reassign"] = ops.reassign_event(ctx.store, ctx.providers, target_row, cal_ids)
        result["assignments"] = list(result["assignments"]) + list(result["reassign"]["added"]) \
            + [{**a, "action": "removed"} for r in result["reassign"]["removed"] for a in r["assignments"]]
    warning = _plan_reminders(ctx)
    ev = result.get("event") or ctx.store.get_event(base)
    statuses = {a["status"] for a in result["assignments"]}
    if (result.get('reassign') or {}).get('failed'):
        statuses.add('failed')
    status = "updated" if statuses <= {"done"} else ("pending" if "pending" in statuses else "conflict" if "conflict" in statuses else "updated_partially" if 'done' in statuses else 'failed')
    return bounded_result({"status": status, "message": _assignment_message("Не изменено" if status == 'failed' else "Обновлено", ev, result["assignments"], ctx.tz),
                           "event": event_public(ev, ctx.tz, compact=False) if ev else None, "assignments": result["assignments"],
                           "reassign": result.get("reassign"), 'link': result.get('link'), **({"warning": warning} if warning else {})})


def cal_delete(ctx: Context, id: str = "", scope: str = SCOPE_THIS, send_updates: bool = False, confirm: bool = False, **kwargs) -> str:
    if not id:
        return json_dumps({"status": "error", "message": "нужен id события"})
    base, occ = ops._series_id(str(id))
    row = ctx.store.get_event(base)
    if row is None or row.get("deleted_at"):
        return json_dumps({"status": "not_found", "message": f"событие {id} не найдено"})
    try:
        ops.resolve_occurrence(ctx.store, row, occ)
    except ValueError as exc:
        return json_dumps({"status": "error", "message": str(exc)})
    scope = str(scope or SCOPE_THIS).lower()
    if scope not in SCOPES:
        return json_dumps({"status": "error", "message": "scope: this | following | all"})
    if row.get("rrule") and not occ and scope == SCOPE_THIS:
        return json_dumps({"status": "ambiguous", "message": "повторяющееся событие: уточни у владельца — удалить только эту дату (id вхождения), "
                                                                "всё расписание (scope='all') или начиная с даты (scope='following')"})
    if scope == SCOPE_FOLLOWING and not occ and not row.get("master_id"):
        return json_dumps({"status": "error", "message": "scope='following' требует id вхождения (…@дата): с какой даты удалять"})
    if not _bool(confirm):
        target = ctx.store.get_event(row["master_id"]) if row.get("master_id") else row
        affected = [{"event_id": m["id"], "calendar_id": m["calendar_id"]}
                    for m in ops.deletion_targets(ctx.store, target or row)]
        return bounded_result({"status": "needs_confirm", "message": CONFIRM_MESSAGE, "event": event_public(row, ctx.tz),
                               "scope": scope, "affected": affected})
    result = ops.delete_event(ctx.store, ctx.providers, str(id), scope=scope, send_updates=_bool(send_updates))
    statuses = {a["status"] for a in result["assignments"]}
    status = "deleted" if statuses <= {"done"} else ("pending" if "pending" in statuses else "conflict" if "conflict" in statuses else "deleted_partially" if 'done' in statuses else 'failed')
    return bounded_result({"status": status, "message": _assignment_message("Не удалено" if status == 'failed' else "Удалено", row, result["assignments"], ctx.tz), "assignments": result["assignments"]})


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
    failed = {}
    occs = ctx.occurrences(s, e, cal_ids, strict=True, failed=failed) if cal_ids else []
    with ctx.store._conn() as c:
        disputed = [r['id'] for r in c.execute("SELECT id,calendar_id FROM events WHERE sync_state='conflict'")
                    if r['calendar_id'] in cal_ids]
    incomplete = bool(failed or disputed)
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
        'availability_complete': not incomplete, 'conflict_event_ids': disputed, 'unexpanded_series': failed,
        "sources": cal_ids, "slots": [{"start": iso_local(a, ctx.tz), "end": iso_local(b, ctx.tz)} for a, b in slots],
        "soft_overlaps": soft_hits[:10],
        'next_step': ('Окна предварительные: сначала проверь конфликтные события через cal_events(id) и ошибки повторений; '
                      'не считай эти окна подтверждённо свободными' if incomplete else
                      'Предложи владельцу 2–4 варианта, затем cal_create с confirm=true'),
    })


def cal_reminders(ctx: Context, action: str = "list", offsets: Any = None, calendar_id: str = "", confirm: bool = False,
                  restore_provider: bool = True, **kwargs) -> str:
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
        report = set_reminder_mode(ctx, cal_ids[0], action == "enable_calendar", restore_provider=_bool(restore_provider, True))
        _plan_reminders(ctx)
        return bounded_result({"status": "updated", **report, "rules": rem.get_rules(st), "upcoming": rem.upcoming(st, ctx.tz, limit=10)})
    else:
        return json_dumps({"status": "error", "message": "action: list | set_default | set_hidden | set_calendar | clear_calendar | enable_calendar | disable_calendar"})
    _plan_reminders(ctx)
    return bounded_result({"status": "updated", "rules": rules, "upcoming": rem.upcoming(st, ctx.tz, limit=10)})


def set_reminder_mode(ctx: Context, calendar_id: str, on: bool, *, restore_provider: bool = True) -> Dict[str, Any]:
    """20 A for one external calendar: import the provider's default reminders as the calendar rule, keep per-event
    alerts as Ouroboros reminders, then silence the provider (calendar defaults now, per-event overrides on next write)."""
    st = ctx.store
    cal = st.get_calendar(calendar_id)
    if cal is None:
        return {"error": "календарь не найден"}
    if cal["provider"] == PROVIDER_LOCAL:
        return {"calendar_id": calendar_id, "note": "локальный календарь и так напоминает через Уроборос"}
    report: Dict[str, Any] = {"calendar_id": calendar_id, "mode": "on" if on else "off", "provider_muted": False, "manual_steps": []}
    backup_key = f"google_pre_mute_reminders:{calendar_id}"
    backup = st.get_setting(backup_key)
    adapter = ctx.providers.adapter_for(cal["account_id"])
    if not on:
        if cal['provider'] == PROVIDER_GOOGLE and restore_provider and (backup is None or adapter is None):
            return {**report, 'mode': 'on', 'status': 'failed',
                    'message': 'Исходные напоминания или аккаунт недоступны. Режим оставлен включённым; '
                               'restore_provider=false выключает только местные уведомления, не обещая восстановление Google.'}
        if restore_provider and backup is not None and adapter is not None and hasattr(adapter, "set_default_reminders"):
            try:
                adapter.set_default_reminders(cal, backup)
                st.set_setting(f"google_default_reminders:{calendar_id}", backup)
                report["provider_restored"] = backup
                st.delete_setting(backup_key)
            except ops.ProviderError as exc:
                # neither side would remind: keep Ouroboros on and say so
                report.update({"mode": "on", "status": "failed",
                               "message": f"режим оставлен включённым: не удалось вернуть напоминания провайдера — {exc.message}"})
                return report
        elif restore_provider:
            report["manual_steps"].append("напоминания провайдера, снятые при включении режима, восстанови в его настройках вручную")
        rem.set_mode(st, calendar_id, False)
        return report
    was_on = bool((st.get_setting(rem.MODE_KEY) or {}).get(calendar_id))
    defaults = st.get_setting(f"google_default_reminders:{calendar_id}")
    if cal['provider'] == PROVIDER_GOOGLE and backup is None and not was_on:
        if defaults is None:
            if adapter is None:
                return {**report, 'mode': 'off', 'status': 'failed', 'message': 'Google не подключён; напоминания не изменены'}
            try:
                current = next(c for c in adapter.list_calendars() if c['id'] == calendar_id)
                defaults = current.get('default_reminders') or []
            except (StopIteration, ops.ProviderError) as exc:
                return {**report, 'mode': 'off', 'status': 'failed', 'message': f'Не удалось прочитать исходные напоминания: {exc}'}
        st.set_setting(backup_key, defaults)
        backup = defaults
    rem.set_mode(st, calendar_id, True)
    defaults = backup if backup is not None else (defaults or [])
    minutes = sorted({int(d.get("minutes")) for d in defaults if isinstance(d, dict) and d.get("minutes") is not None})
    if minutes and not was_on:
        rem.set_rules(st, calendar_id=calendar_id, calendar_offsets=minutes)
        report["imported_default"] = minutes
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
                 confirm: bool = False, public_base_url: str = "", **kwargs) -> str:
    st = ctx.store
    action = str(action or "get").lower()
    if action == "get":
        return json_dumps({"status": "ok", "timezone": tz_name(ctx.tz), "working_hours": st.get_setting("working_hours") or "09:00-19:00",
                               "preferences": st.get_setting("preferences") or "", 'public_base_url': st.get_setting('public_base_url') or '',
                               "calendars": [{"id": c["id"], "name": c["name"], "provider": c["provider"], "visible": bool(c["role_visible"]),
                                              "busy_source": bool(c["role_busy"]), "publish_busy": bool(c["role_publish"]), "publish_mode": c["publish_mode"],
                                              "default": bool(c["is_default"]), "writable": bool(c["writable"])} for c in st.list_calendars()],
                               "accounts": st.list_accounts(), "secrets_present": {k: bool(ctx.secrets.get(k)) for k in SECRET_KEYS},
                               "actions": ["set", "set_calendar", "new_local_calendar", "reload_yandex", "connect_google", "reload_google", "disconnect", "sync_now"]})
    if not _bool(confirm):
        return json_dumps({"status": "needs_confirm", "message": CONFIRM_MESSAGE, "action": action})
    if action == "set":
        fields = {}
        if working_hours:
            try:
                first, last = str(working_hours).split('-', 1)
                beginning, finish = datetime.strptime(first, '%H:%M').time(), datetime.strptime(last, '%H:%M').time()
                if beginning >= finish:
                    raise ValueError('end before start')
                fields['working_hours'] = beginning.strftime('%H:%M') + '-' + finish.strftime('%H:%M')
            except ValueError:
                return json_dumps({'status': 'error', 'message': 'working_hours: HH:MM-HH:MM в пределах одного дня'})
        if timezone:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                return json_dumps({'status': 'error', 'message': 'Нужна существующая временная зона IANA, например Europe/Moscow'})
            fields['timezone'] = str(timezone)
        if public_base_url:
            try:
                fields['public_base_url'] = _public_base(public_base_url)
            except ValueError as exc:
                return json_dumps({'status': 'error', 'message': str(exc)})
        if preferences is not None:
            fields['preferences'] = str(preferences)
        with st.transaction() as view:
            for key, value in fields.items():
                view.set_setting(key, value)
        ctx.tz = get_tz(st.get_setting('timezone') or '')
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
        if st.get_calendar(cid) is not None:
            cid = 'local:' + new_id(slug)
        st.upsert_calendar({"id": cid, "account_id": "local", "provider": PROVIDER_LOCAL, "external_id": slug, "name": name, "writable": True})
        return json_dumps({"status": "created", "calendar": st.get_calendar(cid)})
    if action == "reload_yandex":
        return bounded_result(reload_yandex(ctx))
    if action == "connect_google":
        return bounded_result(connect_google(ctx, public_base_url=public_base_url))
    if action == "reload_google":
        return bounded_result(reload_google(ctx))
    if action == "disconnect":
        accounts = [a for a in st.list_accounts() if a["id"] == calendar_id or a["alias"] == calendar_id]
        if not accounts:
            return json_dumps({"status": "not_found", "message": "укажи id аккаунта (см. cal_status → accounts)"})
        acc = accounts[0]
        if acc['provider'] == PROVIDER_LOCAL:
            return json_dumps({'status': 'error', 'message': 'Локальный календарь не является внешним подключением; события и настройки сохранены'})
        cancelled = st.cancel_intents_for_account(acc["id"])
        st.delete_account(acc["id"])
        forgot = False
        if acc["provider"] == PROVIDER_GOOGLE:
            import providers_google as gp
            forgot = gp.forget_account(ctx.state_dir, str(ctx.secrets.get("CALENDAR_TOKEN_KEY") or ""), acc["id"].split(":", 1)[-1])
        else:
            from credentials import _mutate_tokens
            def forget(vault):
                return vault.pop(acc['id'], None) is not None
            forgot = _mutate_tokens(ctx.state_dir, str(ctx.secrets.get('CALENDAR_TOKEN_KEY') or ''), forget)
        ctx.providers = Providers(ctx.secrets, ctx.state_dir)
        return json_dumps({"status": "updated", "cancelled_intents": cancelled, "tokens_dropped": forgot,
                           "message": f"аккаунт {acc['id']} отключён; локальные события не тронуты, незавершённых записей отменено: {cancelled};"
                                      + (" сохранённые токены Google удалены" if forgot else " секрет удаляет владелец")})
    if action == "sync_now":
        st.set_setting("sync_requested_at", iso_utc(now_utc()))
        return json_dumps({"status": "accepted", "message": "фоновая синхронизация запрошена; результат — в cal_status через минуту"})
    return json_dumps({"status": "error", "message": "неизвестное action"})


def _plan_reminders(ctx: Context) -> str:
    """Reconcile waiting reminders through the planner; do not clear them before reading occurrences."""
    held: Dict[str, str] = {}
    try:
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e, strict=True, failed=held), held=held)
    except Exception as exc:
        return f"очередь напоминаний не обновлена: {type(exc).__name__}: {exc}"
    return ("очередь напоминаний не обновлена для серий: " + "; ".join(f"{key}: {value}" for key, value in held.items())) if held else ""


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
