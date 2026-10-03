"""2ГИС extension — геоданные: поиск, геокодирование, маршруты, изохроны, карты.

Точка входа register(). Транспорт и разбор ответов лежат в client.py; здесь —
регистрация инструментов, чтение ключей, виджет настроек и показ карты в чате.

Навык повторяет набор инструментов официального MCP-сервера 2ГИС
(github.com/2gis/2gis-mcp), но как нативный Python-extension: без Node.js и сборки.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import time
from typing import Any, Dict, Optional

from starlette.responses import JSONResponse

from .client import (
    TwoGisClient,
    TwoGisError,
    web_map_url,
    web_route_url,
)

_api = None

# Ключи, которые принимает виджет/настройки. Всё прочее из запроса игнорируется.
_SETTINGS_KEYS = ("TWOGIS_MAP_API_KEY", "TWOGIS_ROUTING_API_KEY")

# Карту показываем встроенно в чате, если PNG не больше этого размера; крупнее —
# отдаём путём к файлу (base64 сильно раздувает кадр передачи).
_MAX_INLINE_PNG = 12 * 1024 * 1024


# ── Настройки ─────────────────────────────────────────────────────────────────

def _settings_path(api) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / "settings.json"


def _load_settings(api) -> dict:
    """Прочитать настройки навыка. Нет файла или битый JSON → пустой словарь."""
    path = _settings_path(api)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — повреждённые настройки откатываются к пустым
        return {}


def _client(api) -> TwoGisClient:
    """Собрать клиент из настроек навыка (с откатом на переменные окружения)."""
    settings = _load_settings(api)
    maps_key = (
        settings.get("TWOGIS_MAP_API_KEY")
        or os.environ.get("TWOGIS_MAP_API_KEY")
        or ""
    )
    routing_key = (
        settings.get("TWOGIS_ROUTING_API_KEY")
        or os.environ.get("TWOGIS_ROUTING_API_KEY")
        or maps_key
    )
    return TwoGisClient(maps_key, routing_key)


def _make_settings_save(api):
    """Роут сохранения настроек. Пишет только известные ключи."""
    async def _save(request):
        data = await request.json()
        current = _load_settings(api)
        for key in _SETTINGS_KEYS:
            if key in data:
                current[key] = str(data[key] or "").strip()
        path = _settings_path(api)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "message": "Ключи 2ГИС сохранены."})
    return _save


# ── Показ карты в чате ────────────────────────────────────────────────────────

def _save_png(api, data: bytes) -> pathlib.Path:
    """Сохранить PNG в приватном каталоге навыка и вернуть путь."""
    maps_dir = pathlib.Path(api.get_state_dir()) / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    path = maps_dir / f"map_{int(time.time())}_{os.getpid()}.png"
    path.write_bytes(data)
    return path


def _queue_link(ctx, url: str, label: str, title: str = "") -> bool:
    """Поставить кликабельную ссылку в чат (кнопка send_links). False — не вышло."""
    if not url:
        return False
    try:
        if ctx is None:
            return False
        pending = getattr(ctx, "pending_events", None)
        chat_id = getattr(ctx, "current_chat_id", None)
        if pending is None or not chat_id:
            return False
        meta = getattr(ctx, "task_metadata", {})
        meta = meta if isinstance(meta, dict) else {}
        pending.append({
            "type": "send_links",
            "chat_id": chat_id,
            "title": title or "",
            "actions": [{"label": label, "url": url}],
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            "parent_task_id": str(meta.get("parent_task_id") or ""),
            "root_task_id": str(meta.get("root_task_id") or ""),
        })
        return True
    except Exception:  # noqa: BLE001 — ссылка не должна ломать сам вызов
        return False


def _deliver_map(ctx, saved: pathlib.Path, caption: str = "", web_url: str = "") -> dict:
    """Показать карту встроенно в чате; иначе вернуть путь к файлу.

    Два непересекающихся состояния: при успешном инлайне путей не отдаём вовсе
    (модель не напечатает то, чего не получила); иначе — только путь, иначе
    пользователю нечем найти уже созданный файл.

    Рядом с картинкой ставим кликабельную ссылку на веб-карту 2ГИС: снимок
    статичен, а ссылка открывает интерактивную карту в деталях.
    """
    link_sent = _queue_link(ctx, web_url, "Открыть на карте 2ГИС", title="2ГИС")
    try:
        if ctx is not None:
            pending = getattr(ctx, "pending_events", None)
            chat_id = getattr(ctx, "current_chat_id", None)
            size = saved.stat().st_size if saved.is_file() else 0
            if pending is not None and chat_id and 0 < size <= _MAX_INLINE_PNG:
                meta = getattr(ctx, "task_metadata", {})
                meta = meta if isinstance(meta, dict) else {}
                pending.append({
                    "type": "send_photo",
                    "chat_id": chat_id,
                    "task_id": str(getattr(ctx, "task_id", "") or ""),
                    "parent_task_id": str(meta.get("parent_task_id") or ""),
                    "root_task_id": str(meta.get("root_task_id") or ""),
                    "mime": "image/png",
                    "caption": caption or "",
                    "image_base64": base64.b64encode(saved.read_bytes()).decode("ascii"),
                })
                return {
                    "inline_delivered": True,
                    "map_url": web_url,
                    "link_sent": link_sent,
                    "shown": ("Карта уже показана пользователю в чате"
                              + (", ссылка на интерактивную карту 2ГИС отправлена кнопкой." if link_sent else ".")
                              + " Отвечать не нужно: ни подтверждающей фразы, ни путей, ни base64, ни "
                              "«FINAL ANSWER». send_photo не вызывай — карта уже доставлена."),
                }
    except Exception:  # noqa: BLE001 — инлайн не должен ломать сам вызов
        pass
    return {
        "inline_delivered": False,
        "file_path": str(saved),
        "map_url": web_url,
        "link_sent": link_sent,
        "show_this": ("Показать карту в чате не удалось, но файл сохранён. Скажи об этом "
                      "одной строкой и укажи путь: «Файл: <значение поля file_path>» — "
                      "его видно во вкладке «Файлы». send_photo не вызывай."),
    }


# ── Обработчики инструментов ──────────────────────────────────────────────────

def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _err(message: str) -> str:
    return f"Ошибка: {message}"


def tool_check(ctx, **_ignored: Any) -> str:
    """Префлайт: какие ключи заданы и отвечают ли Catalog и Routing API."""
    client = _client(_api)
    report: Dict[str, Any] = {
        "maps_key_set": bool(client.maps_key),
        "routing_key_set": bool(client.routing_key),
        "catalog_api": "unknown",
        "routing_api": "unknown",
    }
    try:
        client.geocode("Москва")
        report["catalog_api"] = "ok"
    except TwoGisError as exc:
        report["catalog_api"] = f"error: {exc}"
    try:
        client.isochrone(55.7558, 37.6173, "car", [300])
        report["routing_api"] = "ok"
    except TwoGisError as exc:
        report["routing_api"] = f"error: {exc}"
    return _ok(report)


def tool_geocode(ctx, query: str = "", **_ignored: Any) -> str:
    if not query:
        return _err("укажи адрес в параметре query")
    try:
        results = _client(_api).geocode(query)
    except TwoGisError as exc:
        return _err(str(exc))
    if not results:
        return _ok({"query": query, "found": 0, "results": [],
                    "note": "Адрес не найден. Добавь город/регион или проверь написание."})
    payload = {"query": query, "found": len(results), "results": results}
    first = results[0]
    if first.get("lat") is not None and first.get("lon") is not None:
        payload["map_url"] = web_map_url(float(first["lat"]), float(first["lon"]), 16)
    return _ok(payload)


def tool_reverse_geocode(ctx, lat: Optional[float] = None, lon: Optional[float] = None,
                         radius: Optional[int] = None, **_ignored: Any) -> str:
    if lat is None or lon is None:
        return _err("укажи координаты lat и lon")
    try:
        result = _client(_api).reverse_geocode(float(lat), float(lon), radius)
    except (TwoGisError, ValueError, TypeError) as exc:
        return _err(str(exc))
    return _ok(result)


def tool_search_poi(ctx, query: str = "", lat: Optional[float] = None,
                    lon: Optional[float] = None, radius: int = 500,
                    page: int = 1, **_ignored: Any) -> str:
    if not query:
        return _err("укажи поисковый запрос в параметре query")
    if lat is None or lon is None:
        return _err("укажи центр поиска: lat и lon")
    try:
        results = _client(_api).search_poi(query, float(lat), float(lon),
                                           int(radius or 500), int(page or 1))
    except (TwoGisError, ValueError, TypeError) as exc:
        return _err(str(exc))
    payload = {"query": query, "radius_m": int(radius or 500),
               "found": len(results), "results": results,
               "map_url": web_map_url(float(lat), float(lon), 14)}
    return _ok(payload)


def tool_route(ctx, start: Any = None, end: Any = None, profile: str = "car",
               via: Optional[list] = None, route_mode: str = "fastest",
               traffic_mode: str = "jam", filters: Optional[list] = None,
               **_ignored: Any) -> str:
    if start is None or end is None:
        return _err("укажи start и end (координаты {lat, lng} или адрес)")
    try:
        result = _client(_api).route(start, end, profile, via=via,
                                     route_mode=route_mode, traffic_mode=traffic_mode,
                                     filters=filters)
    except (TwoGisError, ValueError, TypeError) as exc:
        return _err(str(exc))
    # Deep-link на маршрут: точки в порядке старт → промежуточные → финиш.
    route_points: list = [result.get("start")] + list(via or []) + [result.get("end")]
    web_url = web_route_url(route_points)
    if web_url:
        result["route_url"] = web_url
        _queue_link(ctx, web_url, "Открыть маршрут в 2ГИС", title="Маршрут 2ГИС")
    return _ok(result)


def tool_isochrone(ctx, lat: Optional[float] = None, lon: Optional[float] = None,
                   profile: str = "car", intervals: Optional[list] = None,
                   reverse: Optional[bool] = None,
                   start_time: Optional[str] = None, **_ignored: Any) -> str:
    if lat is None or lon is None:
        return _err("укажи координаты lat и lon")
    if not intervals:
        return _err("укажи intervals — список секунд (например [900])")
    try:
        result = _client(_api).isochrone(float(lat), float(lon), profile,
                                         intervals, reverse=reverse,
                                         start_time=start_time)
    except (TwoGisError, ValueError, TypeError) as exc:
        return _err(str(exc))
    return _ok(result)


def tool_map(ctx, lat: Optional[float] = None, lon: Optional[float] = None,
             zoom: int = 14, markers: Optional[list] = None,
             lines: Optional[list] = None, polygons: Optional[list] = None,
             width: int = 800, height: int = 600, **_ignored: Any) -> str:
    if lat is None or lon is None:
        return _err("укажи центр карты: lat и lon")
    try:
        png = _client(_api).map_view(float(lat), float(lon), int(zoom or 14),
                                     markers=markers, lines=lines, polygons=polygons,
                                     width=int(width or 800), height=int(height or 600))
    except (TwoGisError, ValueError, TypeError) as exc:
        return _err(str(exc))
    try:
        saved = _save_png(_api, png)
    except Exception as exc:  # noqa: BLE001 — сбой записи не должен терять карту
        return _err(f"карта получена, но не сохранена: {exc}")
    web_url = web_map_url(float(lat), float(lon), int(zoom or 14))
    return _ok(_deliver_map(ctx, saved, caption="Карта 2ГИС", web_url=web_url))


# ── Регистрация ───────────────────────────────────────────────────────────────

_TOOLS = (
    ("check", tool_check,
     "Проверка подключения к API 2ГИС: какие ключи заданы и жив ли сервис.",
     {"type": "object", "properties": {}}),
    ("geocode", tool_geocode,
     "Адрес → координаты (прямое геокодирование). Поддерживает российские "
     "адреса и названия организаций.",
     {"type": "object", "properties": {
         "query": {"type": "string", "description": "Адрес или название места"},
     }, "required": ["query"]}),
    ("reverse_geocode", tool_reverse_geocode,
     "Координаты → ближайший адрес.",
     {"type": "object", "properties": {
         "lat": {"type": "number", "description": "Широта"},
         "lon": {"type": "number", "description": "Долгота"},
         "radius": {"type": "integer", "description": "Радиус поиска, метры (0–2000)"},
     }, "required": ["lat", "lon"]}),
    ("search_poi", tool_search_poi,
     "Поиск мест и организаций по запросу рядом с точкой.",
     {"type": "object", "properties": {
         "query": {"type": "string", "description": "Что искать (например «аптека», «кофейня»)"},
         "lat": {"type": "number", "description": "Широта центра поиска"},
         "lon": {"type": "number", "description": "Долгота центра поиска"},
         "radius": {"type": "integer", "description": "Радиус поиска в метрах (по умолчанию 500)"},
         "page": {"type": "integer", "description": "Страница результатов (по умолчанию 1)"},
     }, "required": ["query", "lat", "lon"]}),
    ("route", tool_route,
     "Маршрут между точками: расстояние, время в пути, маневры и альтернативы. "
     "Возвращает route_url — ссылку на интерактивный маршрут в 2ГИС (ставится кнопкой в чат).",
     {"type": "object", "properties": {
         "start": {"description": "Начало: {\"lat\":55.75,\"lng\":37.62}, \"55.75,37.62\" или адрес"},
         "end": {"description": "Конец: координаты, строка или адрес"},
         "profile": {"type": "string",
                     "enum": ["car", "taxi", "pedestrian", "bicycle", "scooter", "motorcycle", "truck"],
                     "description": "Вид транспорта"},
         "via": {"type": "array", "items": {}, "description": "Промежуточные точки"},
         "route_mode": {"type": "string", "enum": ["fastest", "shortest"]},
         "traffic_mode": {"type": "string", "enum": ["jam", "statistics"]},
         "filters": {"type": "array", "items": {"type": "string"},
                     "description": "Исключить: toll_road, ferry, dirt_road, highway, ban_car_road, ban_stairway"},
     }, "required": ["start", "end", "profile"]}),
    ("isochrone", tool_isochrone,
     "Изохроны: зона, достижимая от точки за заданное время (WKT MULTIPOLYGON).",
     {"type": "object", "properties": {
         "lat": {"type": "number"}, "lon": {"type": "number"},
         "profile": {"type": "string",
                     "enum": ["car", "pedestrian", "bicycle", "public_transport"]},
         "intervals": {"type": "array", "items": {"type": "number"},
                       "description": "Интервалы в секундах (до 5 значений, каждое ≤ 3600)"},
         "reverse": {"type": "boolean",
                     "description": "false — достижимо ОТ точки; true — откуда можно ДОЕХАТЬ"},
         "start_time": {"type": "string", "description": "Время отправления, RFC 3339"},
     }, "required": ["lat", "lon", "profile", "intervals"]}),
    ("map", tool_map,
     "Статическая карта PNG с маркерами, линиями и полигонами. Ключ не требуется. "
     "Вместе с картинкой ставит кликабельную ссылку (map_url) на интерактивную карту 2ГИС.",
     {"type": "object", "properties": {
         "lat": {"type": "number", "description": "Широта центра"},
         "lon": {"type": "number", "description": "Долгота центра"},
         "zoom": {"type": "integer", "description": "Зум 1–18 (по умолчанию 14)"},
         "markers": {"type": "array", "items": {"type": "object", "properties": {
             "lat": {"type": "number"}, "lon": {"type": "number"},
             "color": {"type": "string",
                       "enum": ["blue", "red", "green", "orange", "yellow",
                                "purple", "pink", "grey", "black"]},
         }, "required": ["lat", "lon"]}, "description": "Маркеры"},
         "lines": {"type": "array", "items": {"type": "object", "properties": {
             "points": {"type": "array", "items": {"type": "object"}},
             "color": {"type": "string", "description": "Hex RRGGBB"},
             "width": {"type": "integer"},
         }}, "description": "Линии (например, маршрут)"},
         "polygons": {"type": "array", "items": {"type": "object", "properties": {
             "points": {"type": "array", "items": {"type": "object"}},
             "strokeColor": {"type": "string"}, "strokeWidth": {"type": "integer"},
             "fillColor": {"type": "string", "description": "Hex RRGGBB или RRGGBBAA"},
         }}, "description": "Полигоны (зоны, области)"},
         "width": {"type": "integer", "description": "Ширина PNG 120–1280 (по умолчанию 800)"},
         "height": {"type": "integer", "description": "Высота PNG 90–1280 (по умолчанию 600)"},
     }, "required": ["lat", "lon"]}),
)


def register(api) -> None:
    global _api
    _api = api

    for name, handler, description, schema in _TOOLS:
        api.register_tool(name, handler=handler, description=description, schema=schema)

    api.register_route("settings/save", handler=_make_settings_save(api), methods=("POST",))
    api.register_settings_section(
        "2gis",
        title="2ГИС",
        schema={
            "components": [
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "fields": [
                        {
                            "name": "TWOGIS_MAP_API_KEY",
                            "label": "Ключ: карты и поиск",
                            "type": "password",
                            "required": True,
                            "placeholder": "Вставьте ключ 2ГИС",
                            "help": ("Ключ с доступом к Static API, Geocoder API и Places API. "
                                     "Получить можно в Менеджере Платформы 2ГИС: "
                                     "platform.2gis.ru (есть бесплатный демо-ключ на 30 дней)."),
                            "description": "Ключ для карт и поиска (Static, Geocoder, Places).",
                        },
                        {
                            "name": "TWOGIS_ROUTING_API_KEY",
                            "label": "Ключ: навигация",
                            "type": "password",
                            "required": False,
                            "placeholder": "Вставьте ключ (или повторите основной, если он один)",
                            "help": ("Ключ с доступом к Routing API и Isochrone API. "
                                     "Если у вас один ключ на все сервисы — вставьте его "
                                     "и сюда, и в поле выше. После сохранения поле снова "
                                     "станет пустым: ключ хранится скрыто."),
                            "description": "Ключ для маршрутов и изохрон (Routing, Isochrone).",
                        },
                    ],
                    "submit_label": "Сохранить",
                }
            ]
        },
    )
    api.log("info", "2gis: registered 7 tools + settings section")
