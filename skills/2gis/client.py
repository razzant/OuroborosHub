"""Транспортный слой 2ГИС: Catalog, Routing, Isochrone и Static Maps.

Здесь только HTTP и разбор ответов — никакой работы с файлами агента и каталогами
(это в plugin.py/delivery.py). Хосты 2ГИС зафиксированы константами: ни ключ, ни
тело запроса не могут быть перенаправлены на чужой сервер.

Зависимостей нет — только стандартная библиотека (urllib), поэтому навыку не нужен
ни Node.js, ни pip-пакеты.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── Хосты API 2ГИС (зафиксированы намеренно: это граница доверия) ──────────────
CATALOG_BASE = "https://catalog.api.2gis.com"
ROUTING_BASE = "https://routing.api.2gis.com"
STATIC_BASE = "https://static.maps.2gis.com"

# Таймаут одного HTTP-запроса. Держим заметно ниже манифестного timeout_sec,
# чтобы сетевой таймаут истёк с понятной ошибкой, а не хост прибил вызов. Часть
# инструментов делает НЕСКОЛЬКО последовательных запросов (route с адресами =
# 2 геокодирования + маршрут), поэтому таймаут запроса задан с запасом на
# суммарный бюджет инструмента (см. per-tool timeout_sec в plugin.py).
_REQUEST_TIMEOUT = 15

# Catalog API отдаёт не больше 10 страниц; дальше смысла листать нет.
_MAX_PAGE = 10

# Транспорт для Routing API (профиль навыка → transport API).
_ROUTE_TRANSPORT = {
    "car": "driving",
    "taxi": "taxi",
    "pedestrian": "walking",
    "bicycle": "bicycle",
    "scooter": "scooter",
    "motorcycle": "motorcycle",
    "truck": "truck",
}
# Транспорт для Isochrone API (набор уже, чем у Routing).
_ISO_TRANSPORT = {
    "car": "driving",
    "pedestrian": "walking",
    "bicycle": "bicycle",
    "public_transport": "public_transport",
}
# Цвета маркеров Static API: человекочитаемое имя → код 2ГИС.
_MARKER_COLORS = {
    "blue": "be", "red": "rd", "green": "gn", "orange": "oe", "yellow": "yw",
    "purple": "pe", "pink": "pk", "grey": "gy", "black": "bk",
}
_ROUTE_FILTERS = {
    "dirt_road", "toll_road", "ferry", "highway", "ban_car_road", "ban_stairway",
}


class TwoGisError(RuntimeError):
    """Понятная человеку ошибка 2ГИС: нет ключа, отказ API, сеть, битый ответ."""


# ── Низкоуровневый HTTP ───────────────────────────────────────────────────────

def _fetch(url: str, *, method: str = "GET", body: Optional[dict] = None,
           timeout: int = _REQUEST_TIMEOUT) -> bytes:
    """Выполнить один HTTP-запрос и вернуть тело ответа. Ошибки — в TwoGisError."""
    data = None
    headers = {"Accept": "application/json", "User-Agent": "ouroboros-2gis-skill/1.0"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 — диагностика не должна падать сама
            detail = ""
        raise TwoGisError(
            f"HTTP {exc.code} от {url.split('?')[0]}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TwoGisError(f"Сеть недоступна: {exc.reason}") from exc
    except TimeoutError as exc:
        raise TwoGisError("Таймаут запроса к 2ГИС") from exc


def _fetch_json(url: str, **kwargs: Any) -> dict:
    raw = _fetch(url, **kwargs)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TwoGisError(f"Ответ 2ГИС не является JSON: {raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise TwoGisError("Неожиданный формат ответа 2ГИС (ожидался объект)")
    return data


def _catalog_url(path: str, params: Dict[str, Any]) -> str:
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None and v != ""}
    )
    return f"{CATALOG_BASE}{path}?{query}"


def _check_catalog(data: dict) -> None:
    """Catalog API кладёт код в meta.code; отличаем отказ API от пустой выдачи."""
    meta = data.get("meta") or {}
    code = meta.get("code")
    if code is not None and int(code) != 200:
        message = (meta.get("error") or {}).get("message") or "неизвестная ошибка"
        raise TwoGisError(f"Catalog API {code}: {message}")


def _address_of(item: dict) -> Optional[str]:
    """Достать адрес из элемента Catalog API в любой из его форм.

    API отдаёт адрес то строкой (`address_name`), то объектом (`address` с
    полем `address_name`/`name`). Понимаем обе формы, чтобы поле не молчало.
    """
    direct = item.get("address_name")
    if direct:
        return str(direct)
    full = item.get("full_name")
    if full:
        return str(full)
    address = item.get("address")
    if isinstance(address, dict):
        for key in ("address_name", "name", "full_name"):
            if address.get(key):
                return str(address[key])
    elif address:
        return str(address)
    return item.get("name")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Расстояние между двумя точками в метрах (для поля distance)."""
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return int(2 * radius * math.asin(math.sqrt(a)))


# ── Клиент ────────────────────────────────────────────────────────────────────

class TwoGisClient:
    """Тонкий клиент поверх четырёх API 2ГИС.

    Держит два ключа (карты/поиск и навигация), как рекомендует сама 2ГИС.
    Если задан только один ключ — он используется для обеих групп запросов.
    """

    def __init__(self, maps_key: str = "", routing_key: str = "") -> None:
        self.maps_key = (maps_key or "").strip()
        self.routing_key = (routing_key or "").strip()

    # — вспомогательное —

    def _require_maps_key(self) -> str:
        if not self.maps_key:
            raise TwoGisError(
                "Не задан ключ 2ГИС для карт и поиска. Открой «Настройки» → "
                "«Расширенные» → «2ГИС» и вставь ключ в поле «Ключ: карты и поиск»."
            )
        return self.maps_key

    def _require_routing_key(self) -> str:
        if not self.routing_key:
            raise TwoGisError(
                "Не задан ключ 2ГИС для навигации. Открой «Настройки» → "
                "«Расширенные» → «2ГИС» и вставь ключ в поле «Ключ: навигация» "
                "(если ключ один на все сервисы — вставь его в оба поля)."
            )
        return self.routing_key

    # — геокодирование —

    def geocode(self, query: str) -> List[dict]:
        """Адрес или название → список координат с уверенностью."""
        key = self._require_maps_key()
        data = _fetch_json(_catalog_url("/3.0/items/geocode", {
            "q": query,
            "fields": "items.point,items.address_name",
            "key": key,
        }))
        _check_catalog(data)
        items = ((data.get("result") or {}).get("items")) or []
        out: List[dict] = []
        for item in items:
            point = item.get("point") or {}
            lat, lon = point.get("lat"), point.get("lon")
            if lat is None or lon is None:
                continue
            out.append({
                "address": _address_of(item),
                "lat": lat,
                "lon": lon,
                "confidence": item.get("confidence", 1),
            })
        return out

    def reverse_geocode(self, lat: float, lon: float, radius: Optional[int] = None) -> dict:
        """Координаты → ближайший адрес."""
        key = self._require_maps_key()
        params: Dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "fields": "items.point,items.address_name",
            "key": key,
        }
        if radius is not None:
            params["radius"] = int(radius)
        data = _fetch_json(_catalog_url("/3.0/items/geocode", params))
        _check_catalog(data)
        items = ((data.get("result") or {}).get("items")) or []
        if not items:
            raise TwoGisError(f"По координатам {lat}, {lon} адрес не найден")
        item = items[0]
        point = item.get("point") or {}
        return {
            "address": _address_of(item),
            "lat": point.get("lat", lat),
            "lon": point.get("lon", lon),
            "confidence": item.get("confidence", 1),
        }

    def search_poi(self, query: str, lat: float, lon: float, radius: int,
                   page: int = 1) -> List[dict]:
        """Поиск мест и организаций рядом с точкой."""
        key = self._require_maps_key()
        page = max(1, min(int(page), _MAX_PAGE))
        params: Dict[str, Any] = {
            "q": query,
            # Запрашиваем оба поля адреса: Catalog API отдаёт адрес то строкой
            # (address_name), то объектом (address). Парсер ниже понимает обе формы.
            "fields": "items.point,items.address_name,items.address,items.rubrics",
            "point": f"{lon},{lat}",
            "radius": int(radius),
            "key": key,
        }
        if page > 1:
            params["page"] = page
        data = _fetch_json(_catalog_url("/3.0/items", params))
        _check_catalog(data)
        items = ((data.get("result") or {}).get("items")) or []
        out: List[dict] = []
        for item in items:
            point = item.get("point") or {}
            item_lat, item_lon = point.get("lat"), point.get("lon")
            if item_lat is None or item_lon is None:
                continue
            out.append({
                "name": item.get("name"),
                "address": _address_of(item),
                "lat": item_lat,
                "lon": item_lon,
                "rubrics": [r.get("name") for r in (item.get("rubrics") or []) if r.get("name")],
                "distance_m": _haversine_m(lat, lon, item_lat, item_lon),
            })
        out.sort(key=lambda entry: entry["distance_m"])
        return out

    # — маршруты —

    def _resolve_point(self, point: Any) -> Tuple[float, float]:
        """Разобрать точку из разных форматов, которые генерируют модели.

        Поддерживает: объект {lat, lng|lon}, JSON-строку '{"lat":..,"lng":..}',
        строку "55.75,37.62" и текстовый адрес (геокодируется).
        """
        if isinstance(point, dict):
            lat, lon = point.get("lat"), point.get("lng", point.get("lon"))
            if lat is not None and lon is not None:
                return float(lat), float(lon)
        if isinstance(point, str):
            text = point.strip()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    lat, lon = parsed.get("lat"), parsed.get("lng", parsed.get("lon"))
                    if lat is not None and lon is not None:
                        return float(lat), float(lon)
            except ValueError:
                pass
            parts = [p.strip() for p in text.split(",")]
            if len(parts) == 2:
                try:
                    return float(parts[0]), float(parts[1])
                except ValueError:
                    pass
            matches = self.geocode(text)
            if not matches:
                raise TwoGisError(f"Не удалось геокодировать адрес: {text}")
            return float(matches[0]["lat"]), float(matches[0]["lon"])
        raise TwoGisError(f"Некорректный формат точки: {point!r}")

    def route(self, start: Any, end: Any, profile: str,
              via: Optional[Sequence[Any]] = None, route_mode: str = "fastest",
              traffic_mode: str = "jam",
              filters: Optional[Sequence[str]] = None) -> dict:
        """Маршрут между точками с маневрами и альтернативами."""
        key = self._require_routing_key()
        transport = _ROUTE_TRANSPORT.get(str(profile).lower())
        if not transport:
            raise TwoGisError(
                f"Неизвестный профиль '{profile}'. Допустимо: "
                + ", ".join(sorted(_ROUTE_TRANSPORT))
            )
        if route_mode not in ("fastest", "shortest"):
            raise TwoGisError("route_mode должен быть 'fastest' или 'shortest'")
        if traffic_mode not in ("jam", "statistics"):
            raise TwoGisError("traffic_mode должен быть 'jam' или 'statistics'")

        # Для пешеходного маршрута точки входа/выхода — типа walking (обход препятствий).
        point_type = "walking" if transport == "walking" else "stop"
        start_lat, start_lon = self._resolve_point(start)
        end_lat, end_lon = self._resolve_point(end)

        points: List[dict] = [{"type": point_type, "lat": start_lat, "lon": start_lon}]
        for via_point in (via or []):
            v_lat, v_lon = self._resolve_point(via_point)
            points.append({"type": "pref", "lat": v_lat, "lon": v_lon})
        points.append({"type": point_type, "lat": end_lat, "lon": end_lon})

        body: Dict[str, Any] = {
            "points": points,
            "transport": transport,
            "route_mode": route_mode,
            "traffic_mode": traffic_mode,
            "output": "detailed",
            "locale": "ru",
        }
        if filters:
            cleaned = [f for f in filters if f in _ROUTE_FILTERS]
            if cleaned:
                body["filters"] = cleaned

        data = _fetch_json(
            f"{ROUTING_BASE}/routing/7.0.0/global?key={urllib.parse.quote(key)}",
            method="POST", body=body,
        )
        if data.get("status") not in ("OK", "partial_success") or not data.get("result"):
            raise TwoGisError(
                f"Routing API: {data.get('status') or 'нет результата'} — "
                f"{json.dumps(data, ensure_ascii=False)[:300]}"
            )

        routes = [self._normalize_route(r) for r in data["result"]]
        primary = routes[0]
        if len(routes) > 1:
            primary["alternatives"] = routes[1:]
        # Разрешённые координаты концов — чтобы вызывающий мог собрать deep-link
        # на маршрут (сам ответ Routing API их не повторяет).
        primary["start"] = {"lat": start_lat, "lon": start_lon}
        primary["end"] = {"lat": end_lat, "lon": end_lon}
        return primary

    @staticmethod
    def _normalize_route(raw: dict) -> dict:
        maneuvers = []
        for maneuver in (raw.get("maneuvers") or []):
            path = maneuver.get("outcoming_path") or {}
            maneuvers.append({
                "type": maneuver.get("type"),
                "comment": maneuver.get("comment") or maneuver.get("outcoming_path_comment") or "",
                "distance_m": path.get("distance", 0),
                "duration_s": path.get("duration", 0),
                "streets": path.get("names") or [],
            })
        ui_distance = raw.get("ui_total_distance") or {}
        return {
            "distance_m": raw.get("total_distance"),
            "duration_s": raw.get("total_duration"),
            "distance_text": (
                f"{ui_distance.get('value')} {ui_distance.get('unit')}"
                if ui_distance.get("value") else f"{raw.get('total_distance')} м"
            ),
            "duration_text": raw.get("ui_total_duration")
            or f"{round((raw.get('total_duration') or 0) / 60)} мин",
            "maneuvers": maneuvers,
        }

    def isochrone(self, lat: float, lon: float, profile: str,
                  intervals: Sequence[float], reverse: Optional[bool] = None,
                  start_time: Optional[str] = None) -> dict:
        """Зона достижимости от точки за заданное время, в формате WKT."""
        key = self._require_routing_key()
        transport = _ISO_TRANSPORT.get(str(profile).lower())
        if not transport:
            raise TwoGisError(
                f"Неизвестный профиль '{profile}'. Допустимо: "
                + ", ".join(sorted(_ISO_TRANSPORT))
            )
        durations = [int(v) for v in intervals]
        if not durations or len(durations) > 5:
            raise TwoGisError("intervals: нужно от 1 до 5 значений (в секундах)")
        if any(v <= 0 or v > 3600 for v in durations):
            raise TwoGisError("каждый интервал должен быть от 1 до 3600 секунд")
        if profile == "public_transport" and reverse:
            raise TwoGisError("reverse не поддерживается для профиля public_transport")

        body: Dict[str, Any] = {
            "start": {"lat": lat, "lon": lon},
            "durations": durations,
            "transport": transport,
            "format": "wkt",
        }
        if reverse is not None:
            body["reverse"] = bool(reverse)
        if start_time:
            body["start_time"] = str(start_time)

        data = _fetch_json(
            f"{ROUTING_BASE}/isochrone/2.0.0?key={urllib.parse.quote(key)}",
            method="POST", body=body,
        )
        status = data.get("status")
        if status not in ("OK", "partial_success"):
            raise TwoGisError(
                f"Isochrone API: {status or 'нет результата'} — "
                f"{json.dumps(data, ensure_ascii=False)[:300]}"
            )
        return {
            "transport": data.get("transport") or transport,
            "status": status,
            "isochrones": [
                {
                    "time_s": iso.get("duration"),
                    "geometry_wkt": iso.get("geometry"),
                    "status": iso.get("status"),
                }
                for iso in (data.get("isochrones") or [])
            ],
        }

    # — статическая карта —

    def map_view(self, lat: float, lon: float, zoom: int = 14,
                 markers: Optional[Sequence[dict]] = None,
                 lines: Optional[Sequence[dict]] = None,
                 polygons: Optional[Sequence[dict]] = None,
                 width: int = 800, height: int = 600) -> bytes:
        """Статическая карта PNG. Static API v1.0 ключа не требует."""
        width = max(120, min(int(width), 1280))
        height = max(90, min(int(height), 1280))
        zoom = max(1, min(int(zoom), 18))

        url = f"{STATIC_BASE}/1.0?s={width}x{height}&z={zoom}&c={lat},{lon}"

        for marker in (markers or []):
            m_lat, m_lon = marker.get("lat"), marker.get("lon")
            if m_lat is None or m_lon is None:
                continue
            color = _MARKER_COLORS.get(str(marker.get("color") or "blue").lower(), "be")
            url += f"&pt={m_lat},{m_lon}~k:c~c:{color}"

        for line in (lines or []):
            coords = self._flatten_points(line.get("points"))
            if len(coords) < 2:
                continue
            url += "&ls=" + ",".join(coords)
            if line.get("width"):
                url += f"~w:{int(line['width'])}"
            if line.get("color"):
                url += f"~c:{self._hex(line['color'])}"

        for polygon in (polygons or []):
            coords = self._flatten_points(polygon.get("points"))
            if len(coords) < 3:
                continue
            url += "&pn=" + ",".join(coords)
            if polygon.get("strokeWidth"):
                url += f"~w:{int(polygon['strokeWidth'])}"
            if polygon.get("strokeColor"):
                url += f"~c:{self._hex(polygon['strokeColor'])}"
            if polygon.get("fillColor"):
                url += f"~f:{self._hex(polygon['fillColor'])}"

        png = _fetch(url)
        # Static API отдаёт PNG напрямую; если пришёл не PNG — это ошибка сервиса,
        # и сохранять такие байты как картинку нельзя (иначе битый файл уйдёт в чат).
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise TwoGisError(
                "Static Maps API вернул не PNG (вероятно, ошибка сервиса). "
                f"Первые байты: {png[:16]!r}"
            )
        return png

    @staticmethod
    def _flatten_points(points: Optional[Sequence[dict]]) -> List[str]:
        """[[{lat,lon}, …]] → ["lat,lon", …] (Static API ждёт плоский список)."""
        out: List[str] = []
        for point in (points or []):
            if not isinstance(point, dict):
                continue
            p_lat, p_lon = point.get("lat"), point.get("lon")
            if p_lat is None or p_lon is None:
                continue
            out.append(f"{p_lat},{p_lon}")
        return out

    @staticmethod
    def _hex(value: Any) -> str:
        """Нормализовать hex-цвет к виду RRGGBB или RRGGBBAA (без решётки)."""
        text = str(value).strip().lstrip("#")
        return text if all(c in "0123456789abcdefABCDEF" for c in text) and len(text) in (6, 8) else ""


# ── Веб-ссылки на карту 2ГИС (deep-link) ──────────────────────────────────────
# Статический PNG — это снимок без адреса. Чтобы пользователь мог открыть карту
# «в деталях» (зум, сдвиг, клик по объектам), строим ссылку на веб-версию 2ГИС.
# Формат проверен живыми запросами: https://2gis.ru/?m=<lon>,<lat>/<zoom> и
# https://2gis.ru/directions/points/<lon>,<lat>|<lon>,<lat>. Долгота идёт ПЕРВОЙ.

WEB_BASE = "https://2gis.ru"


def web_map_url(lat: float, lon: float, zoom: int = 14) -> str:
    """Ссылка на карту 2ГИС, центрированную на точке (m=lon,lat/zoom)."""
    z = max(1, min(int(zoom), 18))
    return f"{WEB_BASE}/?m={lon},{lat}/{z}"


def web_route_url(points: Sequence[Any]) -> str:
    """Ссылка на маршрут в 2ГИС по списку точек.

    Точка — {lat,lon}/{lat,lng}, кортеж/список (lat, lon) или строка "lat,lon".
    В URL координаты идут как lon,lat, точки соединяются '|' (как %7C).
    Меньше двух валидных точек → пустая строка (ссылку не показываем).
    """
    parts: List[str] = []
    for point in points or []:
        pair = _coerce_lat_lon(point)
        if pair is None:
            continue
        lat, lon = pair
        parts.append(f"{lon},{lat}")
    if len(parts) < 2:
        return ""
    return f"{WEB_BASE}/directions/points/{'%7C'.join(parts)}"


def _coerce_lat_lon(point: Any) -> Optional[Tuple[float, float]]:
    """Точка любого формата → (lat, lon) или None. Не бросает исключений."""
    if isinstance(point, dict):
        lat, lon = point.get("lat"), point.get("lon", point.get("lng"))
    elif isinstance(point, (list, tuple)) and len(point) == 2:
        lat, lon = point[0], point[1]
    elif isinstance(point, str):
        parts = [p.strip() for p in point.split(",")]
        if len(parts) != 2:
            return None
        lat, lon = parts[0], parts[1]
    else:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None
