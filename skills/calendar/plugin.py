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
import management  # noqa: E402
from routes import register_routes  # noqa: E402

TOOL_DESCRIPTIONS = {
    "cal_status": (
        "Состояние календаря: now_local/today/timezone (считай «сегодня/завтра» от них), аккаунты и календари с ролями "
        "(показывать / источник занятости / публикация «Занят»), число событий сегодня, ожидающие и конфликтные записи, "
        "правила напоминаний и канал доставки, состояние фонового процесса, next_step. Вызывай первым."
    ),
    "cal_events": (
        "События за период (по умолчанию сегодня), уже развёрнутые повторения (≤50, есть next_offset). start/end — ISO 8601 "
        "(без offset = местное время владельца). calendars — явные id/имена; алиас или имя провайдера работает только при однозначном выборе. 'all' — default плюс набор публикации, не весь аккаунт. Без calendars читаются все. query — поиск. "
        "include_hidden=true (по умолчанию) показывает и служебные события-распорядок. id — одно событие с назначениями; при конфликте читает текущую версию провайдера и возвращает local/provider и resolution_token."
    ),
    "cal_create": (
        "Создать событие владельца. calendars: '' — календарь по умолчанию; 'all'/'busy_set' — календарь по умолчанию плюс сохранённый набор "
        "публикации занятости (не все календари подряд); список id/имён — конкретные (первый получает полное содержание, остальные — копии по правилу календаря, "
        "обычно «Занят»). hidden=true — служебное событие (распорядок/заметка планировщика, в виджете по переключателю); "
        "availability: busy | free | soft (soft = «обычно», гибкое предпочтение). reminders — минуты до начала. rrule — RFC 5545 "
        "(FREQ=WEEKLY;BYDAY=MO). Разовое или постоянное поручение задаёт основание действия; confirm=true применяет предварительно выбранное действие. Приглашения участникам — send_invites."
    ),
    "cal_update": (
        "Изменить событие: перенос (start/end/duration_min), название, описание, место, скрытость, availability, "
        "напоминания (reminders: минуты до начала; [] — у этого события без напоминаний; 'default' — снова по общим правилам), участники, "
        "ответ на приглашение (response: accepted|declined|tentative), набор календарей (calendars — явная смена назначений). "
        "link_action=source|detach|follow — отдельно выбрать источник связи, отделить выбранную запись или явно вернуть копию к источнику. "
        "Правка копии не изменяет источник; источник обновляет только following-копии. Для повторяющихся: id вхождения (…@дата) + scope=this — только эта дата; scope=all — всё расписание; scope=following — начиная "
        "с этой даты. Определи охват по поручению; при неясности уточни. При конфликте сначала cal_events(id), затем обоснованный поручением/ответом владельца выбор resolution=local|provider и resolution_token из снимка без других правок. confirm=true применяет действие."
    ),
    "cal_delete": (
        "Удалить выбранное событие: только источник удаляет его following-копии; удаление копии не затрагивает источник. Для повторяющихся — scope как у cal_update; при сомнении спроси "
        "владельца: удалить одну дату или всё расписание. confirm=true обязателен."
    ),
    "cal_free": (
        "Свободные окна не короче duration_min в рабочие часы по календарям-источникам занятости (sources — иначе все с ролью «источник»). "
        "Возвращает slots и soft_overlaps (пересечения с «обычно», это предпочтение, не бронь). Выбирай окно в рамках разового или постоянного поручения; предложи варианты, когда выбор нужен владельцу."
    ),
    "cal_reminders": (
        "Правила напоминаний Уробороса (без модели): list; set_default/set_hidden/set_calendar/clear_calendar с offsets (минуты); "
        "enable_calendar/disable_calendar — режим «напоминает Уроборос» для внешнего календаря (переносит времена провайдера, глушит его дубли). "
        "Изменения — confirm=true. disable_calendar по умолчанию восстанавливает Google defaults; restore_provider=false выключает только местные уведомления. Напоминания конкретного события — через cal_update(reminders=…)."
    ),
    "cal_settings": (
        "Настройки и источники: get; set (working_hours '09:00-19:00', timezone IANA, preferences — текстовые пожелания владельца); "
        "set_calendar (visible / busy_source / publish_busy / publish_mode full|busy / default / name); new_local_calendar; reload_yandex "
        "(прежний YANDEX_CALDAV_ACCOUNTS); connect_google (ссылка для входа в браузере, public_base_url для удалённой установки/префикса); reload_google; disconnect; sync_now. "
        "Обычная форма аккаунтов находится в виджете. Изменения — confirm=true; для чтения в Advises доступны cal_status/cal_calendars."
    ),
    'cal_calendars': ('Читать подключённые календари и возможности аккаунтов; account_id/calendar_id — точные ID. refresh=true обновляет список у провайдера. '
                      'include_acl=true читает права выбранного календаря. request_id читает сохранённый результат cal_manage, не повторяя действие.'),
    'cal_manage': ('Управлять календарями целиком: create/update/delete/share/unshare. changes: name/description/timezone; sharing: type(user/group/domain/default), '
                   'value,role,rule_id,send_notifications. Результат preview без confirm; confirm=true применяет действие в рамках разового/постоянного поручения. '
                   'request_id повторяет только receipt, не запрос. uncertain требует сверки через cal_calendars/провайдера, не слепого повтора. '
                   'Доступ CalDAV настраивается на сайте сервера, если API не поддерживает операцию.'),
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
        "attendees": {"type": "array", "items": {"type": "string"}},
        "reminders": {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "string", "enum": ["default"]}]},
        "resolution": {"type": "string", "enum": ["local", "provider"]}, "resolution_token": {"type": "string"},
        'link_action': {'type': 'string', 'enum': ['source', 'detach', 'follow']},
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
        "action": {"type": "string", "enum": ["list", "set_default", "set_hidden", "set_calendar", "clear_calendar", "enable_calendar", "disable_calendar"], "default": "list"},
        "offsets": {"type": "array", "items": {"type": "integer"}}, "calendar_id": {"type": "string"},
        'restore_provider': {'type': 'boolean', 'default': True},
        "confirm": {"type": "boolean", "default": False}}},
    "cal_settings": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["get", "set", "set_calendar", "new_local_calendar", "reload_yandex", "connect_google", "reload_google", "disconnect", "sync_now"], "default": "get"},
        "calendar_id": {"type": "string"}, "visible": {"type": "boolean"}, "busy_source": {"type": "boolean"}, "publish_busy": {"type": "boolean"},
        "publish_mode": {"type": "string", "enum": ["full", "busy"]}, "default": {"type": "boolean"}, "name": {"type": "string"},
        "working_hours": {"type": "string"}, "timezone": {"type": "string"}, "preferences": {"type": "string"},
        'public_base_url': {'type': 'string'},
        "confirm": {"type": "boolean", "default": False}}},
    'cal_calendars': {'type': 'object', 'properties': {
        'account_id': {'type': 'string'}, 'calendar_id': {'type': 'string'}, 'include_acl': {'type': 'boolean', 'default': False},
        'refresh': {'type': 'boolean', 'default': False}, 'request_id': {'type': 'string'}}},
    'cal_manage': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['create', 'update', 'delete', 'share', 'unshare']},
        'account_id': {'type': 'string'}, 'calendar_id': {'type': 'string'}, 'request_id': {'type': 'string'},
        'changes': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'description': {'type': 'string'}, 'timezone': {'type': 'string'}}, 'additionalProperties': False},
        'sharing': {'type': 'object', 'properties': {
            'type': {'type': 'string', 'enum': ['user', 'group', 'domain', 'default']}, 'value': {'type': 'string'},
            'role': {'type': 'string', 'enum': ['none', 'freeBusyReader', 'reader', 'writer', 'owner']},
            'rule_id': {'type': 'string'}, 'send_notifications': {'type': 'boolean', 'default': False}}, 'additionalProperties': False},
        'confirm': {'type': 'boolean', 'default': False}}, 'required': ['action']},
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

    for name in TOOL_DESCRIPTIONS:
        module = management if name in ('cal_calendars', 'cal_manage') else tools
        api.register_tool(name=name, description=TOOL_DESCRIPTIONS[name], schema=SCHEMAS[name], handler=bind(getattr(module, name)))

    register_routes(api, make_ctx)
    api.register_ui_tab("calendar", "Календарь", icon="📅", render=WIDGET_RENDER)
    api.register_companion_process("calendar_worker")
