"""GigaSearch extension for Ouroboros."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse


_SETTINGS = ("GIGASEARCH_API_URL", "GIGASEARCH_API_KEY")
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20
_TIMEOUT_SEC = 30
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "status": "error", "error": message}


def _pick(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return ""


def _result_list(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    empty = None
    for key in ("results", "items", "documents", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            nested = value
        elif isinstance(value, dict):
            nested = _result_list(value)
        else:
            continue
        if nested:
            return nested
        if nested is not None:
            empty = []
    return empty


def _normalise_result(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    url = str(_pick(item, "url", "link", "href", "source_url")).strip()
    if not url:
        return None
    result = {
        "title": str(_pick(item, "title", "name", "source_title")).strip(),
        "url": url,
        "snippet": str(
            _pick(item, "snippet", "text", "content", "description", "passage")
        ).strip(),
    }
    published = _pick(item, "published_at", "published", "published_date", "date")
    updated = _pick(item, "updated_at", "updated", "modified_at")
    if published:
        result["published_at"] = str(published)
    if updated:
        result["updated_at"] = str(updated)
    return result


def _normalise_response(query: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, (dict, list)):
        return _error("GigaSearch returned an unsupported JSON payload")

    if isinstance(payload, dict):
        service_error = _pick(payload, "error", "errors")
        failed = str(payload.get("status") or "").lower() in {"error", "failed"}
        if service_error or failed:
            message = service_error or _pick(payload, "message", "detail")
            return _error(f"GigaSearch service error: {message or 'unspecified error'}")

    raw_results = _result_list(payload)
    results = [result for item in raw_results or [] if (result := _normalise_result(item))]
    if raw_results is not None and len(results) != len(raw_results):
        return _error("GigaSearch returned an invalid result record; expected an object with a URL")
    answer = ""
    if isinstance(payload, dict):
        answer = str(_pick(payload, "answer", "summary")).strip()

    # Explicit references belong to the summary, independently of search hits.
    sources = None
    source_payload = payload
    while isinstance(source_payload, dict):
        for key in ("sources", "citations"):
            references = source_payload.get(key)
            if references is None:
                continue
            if isinstance(references, dict):
                references = _result_list(references)
            if not isinstance(references, list):
                return _error(f"GigaSearch returned invalid {key}; expected a list")
            if sources is None:
                sources = []
            for reference in references:
                item = _normalise_result(reference)
                if item is None:
                    return _error(f"GigaSearch returned an invalid {key} record; expected an object with a URL")
                sources.append(item)
        source_payload = source_payload.get("data")

    if raw_results is None:
        if sources is not None:
            results = sources
        elif not answer:
            return _error("GigaSearch returned an unsupported response structure")
    if not results and not answer and not sources:
        return {"ok": True, "status": "empty", "query": query, "results": [], "count": 0}

    response: dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "query": query,
        "results": results,
        "count": len(results),
    }
    if sources is not None:
        response["sources"] = [{"title": item["title"], "url": item["url"]} for item in sources]
    if answer:
        response.update(
            {
                "summary": answer,
                "summary_kind": "model_generated",
            }
        )
        if sources is None:
            response["sources"] = [{"title": item["title"], "url": item["url"]} for item in results]
    return response


def _search(query: str, limit: int, api_url: str, api_key: str) -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        return _error("query is empty")
    if len(query) > 2000:
        return _error("query is too long (max 2000 characters)")

    try:
        limit = min(max(1, int(limit)), _MAX_LIMIT)
    except (TypeError, ValueError):
        return _error("limit must be an integer")

    api_url = str(api_url or "").strip()
    api_key = str(api_key or "").strip()
    parsed = urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _error("GIGASEARCH_API_URL must be a valid HTTP(S) URL")
    if not api_key:
        return _error("GIGASEARCH_API_KEY is not configured")

    request = urllib.request.Request(
        api_url,
        data=json.dumps({"query": query, "limit": limit}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Ouroboros-GigaSearch/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return _error(f"GigaSearch HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return _error(f"GigaSearch network error: {exc.reason}")
    except TimeoutError:
        return _error(f"GigaSearch timed out after {_TIMEOUT_SEC}s")
    except Exception as exc:
        return _error(f"GigaSearch request failed: {type(exc).__name__}: {exc}")

    if len(raw) > _MAX_RESPONSE_BYTES:
        return _error("GigaSearch response exceeds 4 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _error("GigaSearch returned invalid JSON")
    return _normalise_response(query, payload)


def register(api: Any) -> None:
    """Register the preferred web-search tool through PluginAPI 2.0."""

    def tool_search(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        values = dict(args or {})
        values.update(kwargs)
        settings = api.get_settings(list(_SETTINGS)) or {}
        payload = _search(
            values.get("query", ""),
            values.get("limit", _DEFAULT_LIMIT),
            settings.get("GIGASEARCH_API_URL", ""),
            settings.get("GIGASEARCH_API_KEY", ""),
        )
        return json.dumps(payload, ensure_ascii=False)

    api.register_tool(
        name="gigasearch_search",
        handler=tool_search,
        description=(
            "Основной веб-поиск этой установки. Для обычного поиска внешней информации сначала используй GigaSearch. "
            "Возвращает заголовок, URL, фрагмент и доступные даты; пустая выдача и ошибка различаются. "
            "Выбери альтернативу при явном указании пользователя, недоступности сервиса или необходимости другой возможности; известный URL открывай напрямую."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Число результатов от 1 до 20 (по умолчанию 5).",
                    "default": _DEFAULT_LIMIT,
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                },
            },
            "required": ["query"],
        },
        timeout_sec=_TIMEOUT_SEC,
    )
    api.log("info", "gigasearch: registered preferred web-search tool")


__all__ = ["register"]
