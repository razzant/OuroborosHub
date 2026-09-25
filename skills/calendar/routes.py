"""HTTP routes for the module widget. Same ``ops``/``tools`` code as the agent, JSON in and out."""

from __future__ import annotations

import html
import json
from datetime import timedelta
from typing import Any, Dict

from starlette.responses import JSONResponse

import ops
import tools
from model import (
    AVAIL_BUSY, AVAIL_SOFT, VISIBILITY_HIDDEN, VISIBILITY_SHOWN, WEEKDAY_LABELS, day_bounds, event_public, iso_local, iso_utc,
    parse_input, parse_stored, tz_name, week_bounds,
)


async def _body(request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"status": "error", "message": message}, status_code=status)


def _aggregate(assignments) -> str:
    statuses = {a.get("status") for a in assignments or []}
    if not statuses or statuses <= {"done"}:
        return "ok"
    if "conflict" in statuses:
        return "conflict"
    if "pending" in statuses:
        return "pending"
    return "partial" if "done" in statuses else "failed"


def agenda_payload(ctx: tools.Context, view: str, anchor: str, calendar_ids: Any, show_hidden: bool) -> Dict[str, Any]:
    """One payload for Day/Week: events, calendars with roles, hidden-busy hatching, «обычно» bands, sync state."""
    now = ctx.now()
    day, _ = parse_input(anchor, ctx.tz, now)
    day = day.astimezone(ctx.tz).date()
    if view == "week":
        start, end = week_bounds(day, ctx.tz)
    else:
        start, end = day_bounds(day, ctx.tz)
    cals = ctx.store.list_calendars()
    visible_ids = [c["id"] for c in cals if c["role_visible"]]
    if calendar_ids:
        visible_ids = [c for c in visible_ids if c in calendar_ids]   # ["-"] (every chip unticked) leaves nothing
    occs = ctx.occurrences(start, end, visible_ids, include_hidden=True) if visible_ids else []
    events, hatches, usual = [], [], []
    for occ in occs:
        if str(occ.get("status") or "") == "cancelled":
            continue
        avail = occ.get("availability") or AVAIL_BUSY
        pub = event_public(occ, ctx.tz)
        pub["provider"] = occ.get("provider")
        if occ.get("visibility") == VISIBILITY_HIDDEN and not show_hidden:
            if avail == AVAIL_BUSY:
                hatches.append({"start": pub["start"], "end": pub["end"], "all_day": pub["all_day"]})
            continue
        if avail == AVAIL_SOFT:
            usual.append({**pub, "usual": True})
            continue
        events.append(pub)
    pending = ctx.store.intent_counts()
    return {
        "status": "ok", "view": view, "anchor": day.isoformat(), "timezone": tz_name(ctx.tz), "now": now.replace(microsecond=0).isoformat(),
        "range": {"start": iso_local(start, ctx.tz), "end": iso_local(end, ctx.tz)},
        "days": [{"date": (start.astimezone(ctx.tz).date() + timedelta(days=i)).isoformat(),
                  "weekday": WEEKDAY_LABELS[(start.astimezone(ctx.tz).date() + timedelta(days=i)).weekday()]}
                 for i in range((end.astimezone(ctx.tz).date() - start.astimezone(ctx.tz).date()).days)],
        "calendars": [{"id": c["id"], "name": c["name"], "provider": c["provider"], "writable": bool(c["writable"]), "visible": bool(c["role_visible"]),
                       "default": bool(c["is_default"])} for c in cals],
        "events": events, "hidden_busy": hatches, "usual": usual if show_hidden else [],
        "show_hidden": show_hidden, "working_hours": ctx.store.get_setting("working_hours") or "09:00-19:00",
        "sync": {"pending": pending.get("pending", 0), "conflict": pending.get("conflict", 0), "companion": ctx.companion_health().get("state")},
        "accounts": [{"id": a["id"], "alias": a["alias"], "provider": a["provider"], "status": a["status"], "last_sync": a.get("last_sync")} for a in ctx.store.list_accounts()],
    }


def register_routes(api, make_ctx) -> None:
    async def agenda(request):
        ctx = make_ctx()
        q = request.query_params
        raw_cals = str(q.get("calendars") or "")
        cal_ids = ["-"] if raw_cals == "-" else [c for c in raw_cals.split(",") if c]   # "-" = the owner unticked every calendar
        show_hidden = str(q.get("show_hidden") or "").lower() in ("1", "true")
        view = "week" if str(q.get("view") or "day") == "week" else "day"
        return JSONResponse(agenda_payload(ctx, view, str(q.get("date") or ""), cal_ids, show_hidden))

    async def status(request):
        ctx = make_ctx()
        return JSONResponse(json.loads(tools.cal_status(ctx)))

    async def event_create(request):
        ctx = make_ctx()
        body = await _body(request)
        title = str(body.get("title") or "").strip()
        if not title:
            return _err("Введите название")
        spec, err = tools._spec_from_args(ctx, body.get("start"), body.get("end"), body.get("duration_min") or 60, body.get("all_day"))
        if err:
            return _err(err)
        cal_ids, err = ctx.resolve_calendars(body.get("calendars") or body.get("calendar_id"))
        if err:
            return _err(err)
        spec.update({"title": title, "calendar_ids": cal_ids, "visibility": VISIBILITY_HIDDEN if body.get("hidden") else VISIBILITY_SHOWN,
                     "availability": body.get("availability") or AVAIL_BUSY, "description": str(body.get("description") or ""),
                     "location": str(body.get("location") or ""),
                     "attendees": [{"email": str(a).strip()} if not isinstance(a, dict) else a for a in (body.get("attendees") or []) if str(a).strip()],
                     "send_invites": bool(body.get("send_updates")),
                     "reminders": [int(x) for x in (body.get("reminders") or []) if str(x).strip() != ""], "rrule": str(body.get("rrule") or "")})
        result = ops.create_event(ctx.store, ctx.providers, spec)
        warning = tools._plan_reminders(ctx)
        if result["event"] is None:
            return _err("ни один календарь не принял событие: " + "; ".join(a.get("message") or a.get("status") for a in result["assignments"]))
        return JSONResponse({"status": _aggregate(result["assignments"]), "event": event_public(result["event"], ctx.tz, compact=False),
                             "assignments": result["assignments"], **({"warning": warning} if warning else {})})

    async def event_update(request):
        ctx = make_ctx()
        body = await _body(request)
        event_id = str(body.get("id") or "")
        if not event_id:
            return _err("нет id")
        changes: Dict[str, Any] = {}
        if body.get("start") or body.get("end"):
            base, occ = ops._series_id(event_id)
            row = ctx.store.get_event(base)
            if row is None:
                return _err("событие не найдено", 404)
            old_s = parse_stored(occ or row["start_utc"])
            dur = parse_stored(row["end_utc"]) - parse_stored(row["start_utc"])
            s, is_date = parse_input(body.get("start"), ctx.tz, old_s)
            e, _ = parse_input(body.get("end"), ctx.tz, None)
            if e is None or e <= s:
                e = s + dur
            changes.update({"start_utc": iso_utc(s), "end_utc": iso_utc(e)})
            if "all_day" in body:
                changes["all_day"] = bool(body.get("all_day"))
        for key in ("title", "description", "location", "availability", "rrule"):
            if key in body and body[key] is not None:
                changes[key] = str(body[key])
        if "hidden" in body:
            changes["visibility"] = VISIBILITY_HIDDEN if body.get("hidden") else VISIBILITY_SHOWN
        if body.get("reminders") is not None and body.get("reminders_edited", True):
            changes["reminders"] = [int(x) for x in (body.get("reminders") or []) if str(x).strip() != ""]
        if "attendees" in body and body.get("attendees") is not None:
            changes["attendees"] = [{"email": str(a).strip()} if not isinstance(a, dict) else a for a in body.get("attendees") or [] if str(a).strip()]
        scope = str(body.get("scope") or "this")
        result = {"status": "ok", "event": None, "assignments": []}
        if changes:
            result = ops.update_event(ctx.store, ctx.providers, event_id, changes, scope=scope, send_updates=bool(body.get("send_updates")))
            if result.get("status") == "not_found":
                return _err("событие не найдено", 404)
        if body.get("calendars"):
            cal_ids, err = ctx.resolve_calendars(body.get("calendars"))
            if err:
                return _err(err)
            base, _ = ops._series_id(event_id)
            row = ctx.store.get_event(result.get("split_master_id") or base)
            if row is None:
                return _err("событие не найдено", 404)
            result["reassign"] = ops.reassign_event(ctx.store, ctx.providers, row, cal_ids)
            result["assignments"] = list(result["assignments"]) + list(result["reassign"]["added"]) \
                + [{**a, "action": "removed"} for r in result["reassign"]["removed"] for a in r["assignments"]]
        warning = tools._plan_reminders(ctx)
        base, _ = ops._series_id(event_id)
        ev = result.get("event") or ctx.store.get_event(base)
        return JSONResponse({"status": _aggregate(result["assignments"]), "event": event_public(ev, ctx.tz, compact=False) if ev else None,
                             "assignments": result["assignments"], "reassign": result.get("reassign"), **({"warning": warning} if warning else {})})

    async def event_delete(request):
        ctx = make_ctx()
        body = await _body(request)
        event_id = str(body.get("id") or "")
        if not event_id:
            return _err("нет id")
        result = ops.delete_event(ctx.store, ctx.providers, event_id, scope=str(body.get("scope") or "this"), send_updates=bool(body.get("send_updates")))
        if result.get("status") == "not_found":
            return _err("событие не найдено", 404)
        return JSONResponse({"status": _aggregate(result["assignments"]), "assignments": result["assignments"]})

    async def event_get(request):
        ctx = make_ctx()
        event_id = str(request.query_params.get("id") or "")
        if not event_id:
            return _err("нет id")
        data = json.loads(tools.cal_events(ctx, id=event_id))
        if data.get("status") != "ok":
            return JSONResponse(data, status_code=404)
        return JSONResponse(data)

    async def reminders_get(request):
        ctx = make_ctx()
        return JSONResponse(json.loads(tools.cal_reminders(ctx, action="list")))

    async def reminders_save(request):
        ctx = make_ctx()
        body = await _body(request)
        body["confirm"] = True
        return JSONResponse(json.loads(tools.cal_reminders(ctx, **body)))

    async def oauth_callback(request):
        """Google redirects the owner's browser here (loopback passes without a session)."""
        from starlette.responses import HTMLResponse
        import providers_google as gp
        ctx = make_ctx()
        q = request.query_params
        code, state, error = str(q.get("code") or ""), str(q.get("state") or ""), str(q.get("error") or "")
        if error or not code or not state:
            return HTMLResponse(f"<h3>Google не завершил вход</h3><p>{html.escape(error or 'нет кода авторизации')}. Закрой вкладку и попробуй снова из чата.</p>", status_code=400)
        try:
            done = gp.finish_auth(ctx.store, ctx.secrets, ctx.state_dir, code, state)
            ctx.providers = tools.Providers(ctx.secrets, ctx.state_dir)
            report = tools.reload_google(ctx)
        except ops.ProviderError as exc:
            return HTMLResponse(f"<h3>Ошибка подключения Google</h3><p>{html.escape(exc.message)}</p>", status_code=502)
        except Exception as exc:  # a malformed token response must still end as a page, not a bare 500
            return HTMLResponse(f"<h3>Ошибка подключения Google</h3><p>{html.escape(f'{type(exc).__name__}: {exc}')}</p>", status_code=502)
        cals = [c["name"] for a in report.get("accounts", []) if a.get("account") == done["account_id"] for c in a.get("calendars", [])]
        return HTMLResponse("<h3>Google подключён: " + html.escape(done["email"]) + "</h3><p>Календари: " + html.escape(", ".join(cals))
                            + "</p><p>Вкладку можно закрыть; вернись в чат Уробороса.</p>")

    async def sync_now(request):
        ctx = make_ctx()
        ctx.store.set_setting("sync_requested_at", iso_utc(ctx.now()))
        return JSONResponse({"status": "accepted"})

    api.register_route("agenda", agenda, methods=("GET",))
    api.register_route("status", status, methods=("GET",))
    api.register_route("event", event_create, methods=("POST",))
    api.register_route("event/get", event_get, methods=("GET",))
    api.register_route("event/update", event_update, methods=("POST",))
    api.register_route("event/delete", event_delete, methods=("POST",))
    api.register_route("reminders", reminders_get, methods=("GET",))
    api.register_route("reminders/save", reminders_save, methods=("POST",))
    api.register_route("sync", sync_now, methods=("POST",))
    api.register_route("oauth/callback", oauth_callback, methods=("GET",))
