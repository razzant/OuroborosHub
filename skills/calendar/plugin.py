"""Ouroboros calendar skill — extension entry point (PluginAPI 2.0).

Runs as a short-lived child per call because the manifest declares isolated
dependencies; the long-lived work (sync, retries, reminders) lives in the
``calendar_worker`` companion (``scripts/worker.py``). ``register()`` is cheap
and idempotent: schema migration is guarded by ``schema_version``.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import tools  # noqa: E402
from routes import register_routes  # noqa: E402

TOOL_DESCRIPTIONS = {
    "cal_status": (
        "Состояние календаря: now_local/today/timezone (считай «сегодня/завтра» от них), аккаунты и календари с ролями "
        "(показывать / источник занятости / публикация «Занят»), число событий сегодня, ожидающие и конфликтные записи, "
        "правила напоминаний и канал доставки, состояние фонового процесса, next_step. Вызывай первым."
    ),
    "cal_events": (
        "События за период (по умолчанию сегодня), уже развёрнутые повторения (≤50, есть next_offset). start/end — ISO 8601 "
        "(без offset = местное время владельца). calendars — id/имена/алиасы/'yandex'/'google'/'local'. query — поиск. "
        "include_hidden=true (по умолчанию) показывает и служебные события-распорядок. id — одно событие целиком с назначениями."
    ),
    "cal_create": (
        "Создать событие владельца. calendars: '' — календарь по умолчанию; 'all' — во все доступные для записи; 'busy_set' — набор "
        "публикации занятости; список id/имён — конкретные (первый получает полное содержание, остальные — копии по правилу календаря, "
        "обычно «Занят»). hidden=true — служебное событие (распорядок/заметка планировщика, в виджете по переключателю); "
        "availability: busy | free | soft (soft = «обычно», гибкое предпочтение). reminders — минуты до начала. rrule — RFC 5545 "
        "(FREQ=WEEKLY;BYDAY=MO). Только по явной команде владельца или после его выбора — confirm=true. Приглашения участникам — send_invites."
    ),
    "cal_update": (
        "Изменить событие: перенос (start/end/duration_min), название, описание, место, скрытость, availability, напоминания, участники, "
        "ответ на приглашение (response: accepted|declined|tentative), набор календарей (calendars — явная смена назначений). "
        "Для повторяющихся: id вхождения (…@дата) + scope=this — только эта дата; scope=all — всё расписание; scope=following — начиная "
        "с этой даты. Если владелец не уточнил охват — спроси его. confirm=true обязателен."
    ),
    "cal_delete": (
        "Удалить событие (и его связанные копии в других календарях). Для повторяющихся — scope как у cal_update; при сомнении спроси "
        "владельца: удалить одну дату или всё расписание. confirm=true обязателен."
    ),
    "cal_free": (
        "Свободные окна не короче duration_min в рабочие часы по календарям-источникам занятости (sources — иначе все с ролью «источник»). "
        "Возвращает slots и soft_overlaps (пересечения с «обычно», это предпочтение, не бронь). Потом предложи владельцу варианты."
    ),
    "cal_reminders": (
        "Правила напоминаний Уробороса (без модели): list; set_default/set_hidden/set_calendar/clear_calendar с offsets (минуты). "
        "Изменения — confirm=true. Напоминания конкретного события — через cal_update(reminders=…)."
    ),
    "cal_settings": (
        "Настройки и источники: get; set (working_hours '09:00-19:00', timezone IANA, preferences — текстовые пожелания владельца); "
        "set_calendar (visible / busy_source / publish_busy / publish_mode full|busy / default / name); new_local_calendar; reload_yandex "
        "(перечитать аккаунты из YANDEX_CALDAV_ACCOUNTS); connect_google; disconnect; sync_now. Изменения — confirm=true."
    ),
}

SCHEMAS = {
    "cal_status": {"type": "object", "properties": {}},
    "cal_events": {"type": "object", "properties": {
        "start": {"type": "string"}, "end": {"type": "string"}, "calendars": {"type": "string"}, "query": {"type": "string"},
        "id": {"type": "string"}, "include_hidden": {"type": "boolean", "default": True},
        "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}},
    "cal_create": {"type": "object", "properties": {
        "title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "duration_min": {"type": "integer", "default": 60},
        "all_day": {"type": "boolean", "default": False}, "calendars": {"type": "string"}, "hidden": {"type": "boolean", "default": False},
        "availability": {"type": "string", "enum": ["busy", "free", "soft"]}, "description": {"type": "string"}, "location": {"type": "string"},
        "attendees": {"type": "array", "items": {"type": "string"}}, "send_invites": {"type": "boolean", "default": False},
        "reminders": {"type": "array", "items": {"type": "integer"}}, "rrule": {"type": "string"},
        "confirm": {"type": "boolean", "default": False}}, "required": ["title", "start", "confirm"]},
    "cal_update": {"type": "object", "properties": {
        "id": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "duration_min": {"type": "integer"},
        "all_day": {"type": "boolean"}, "title": {"type": "string"}, "description": {"type": "string"}, "location": {"type": "string"},
        "hidden": {"type": "boolean"}, "availability": {"type": "string", "enum": ["busy", "free", "soft"]}, "calendars": {"type": "string"},
        "attendees": {"type": "array", "items": {"type": "string"}}, "reminders": {"type": "array", "items": {"type": "integer"}},
        "rrule": {"type": "string"}, "response": {"type": "string", "enum": ["accepted", "declined", "tentative"]},
        "scope": {"type": "string", "enum": ["this", "following", "all"], "default": "this"},
        "send_updates": {"type": "boolean", "default": False}, "confirm": {"type": "boolean", "default": False}}, "required": ["id", "confirm"]},
    "cal_delete": {"type": "object", "properties": {
        "id": {"type": "string"}, "scope": {"type": "string", "enum": ["this", "following", "all"], "default": "this"},
        "send_updates": {"type": "boolean", "default": False}, "confirm": {"type": "boolean", "default": False}}, "required": ["id", "confirm"]},
    "cal_free": {"type": "object", "properties": {
        "start": {"type": "string"}, "end": {"type": "string"}, "duration_min": {"type": "integer", "default": 60},
        "sources": {"type": "string"}, "working_hours": {"type": "string"}, "max_slots": {"type": "integer", "default": 6}}},
    "cal_reminders": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "set_default", "set_hidden", "set_calendar", "clear_calendar"], "default": "list"},
        "offsets": {"type": "array", "items": {"type": "integer"}}, "calendar_id": {"type": "string"},
        "confirm": {"type": "boolean", "default": False}}},
    "cal_settings": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["get", "set", "set_calendar", "new_local_calendar", "reload_yandex", "connect_google", "disconnect", "sync_now"], "default": "get"},
        "calendar_id": {"type": "string"}, "visible": {"type": "boolean"}, "busy_source": {"type": "boolean"}, "publish_busy": {"type": "boolean"},
        "publish_mode": {"type": "string", "enum": ["full", "busy"]}, "default": {"type": "boolean"}, "name": {"type": "string"},
        "working_hours": {"type": "string"}, "timezone": {"type": "string"}, "preferences": {"type": "string"},
        "confirm": {"type": "boolean", "default": False}}},
}

WIDGET_RENDER = {"kind": "module", "entry": "widget.js", "start": "auto", "appearance": "host", "span": 2}


def register(api):
    def make_ctx():
        return tools.Context(api)

    def bind(fn):
        def handler(**kwargs):
            ctx = make_ctx()
            try:
                return fn(ctx, **kwargs)
            except Exception as exc:  # never raise into the agent; disclose
                return tools.json_dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
        handler.__name__ = fn.__name__
        return handler

    for name in ("cal_status", "cal_events", "cal_create", "cal_update", "cal_delete", "cal_free", "cal_reminders", "cal_settings"):
        api.register_tool(name=name, description=TOOL_DESCRIPTIONS[name], schema=SCHEMAS[name], handler=bind(getattr(tools, name)))

    register_routes(api, make_ctx)
    api.register_ui_tab("calendar", "Календарь", icon="📅", render=WIDGET_RENDER)
    api.register_companion_process("calendar_worker")
