"""DuckDuckGo search extension — free web search without any API key.

Uses the ``ddgs`` library to query DuckDuckGo and return structured
results (title, URL, snippet). Exposes:
- A tool for the agent to call searches programmatically
- An HTTP route for the widget / external callers
- A declarative widget with a search form
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from starlette.requests import Request
from starlette.responses import JSONResponse


_MAX_RESULTS_CAP = 20
_DEFAULT_RESULTS = 5


def _search(query: str, max_results: int = _DEFAULT_RESULTS) -> Dict[str, Any]:
    """Perform a DuckDuckGo text search.

    Returns a dict with either ``results`` (list) or ``error`` (str).
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return {"error": "query is empty"}
    if len(cleaned) > 400:
        return {"error": "query too long (max 400 chars)"}

    try:
        max_results_int = int(max_results)
    except (TypeError, ValueError, OverflowError):
        max_results_int = _DEFAULT_RESULTS

    max_results = min(max(1, max_results_int), _MAX_RESULTS_CAP)

    try:
        from ddgs import DDGS
    except ImportError:
        return {"error": "ddgs library not installed (pip install ddgs)"}

    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(cleaned, max_results=max_results) or [])
    except Exception as exc:
        return {"error": f"DuckDuckGo search failed: {type(exc).__name__}: {exc}"}

    results: List[Dict[str, str]] = []
    for item in raw:
        results.append({
            "title": str(item.get("title", "")),
            "url": str(item.get("href", "")),
            "snippet": str(item.get("body", "")),
        })

    return {"query": cleaned, "results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


async def _route_search(request: Request) -> JSONResponse:
    """POST /api/extensions/duckduckgo/search

    Body: {"query": "...", "max_results": 5}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    raw_query = body.get("query")
    query = raw_query.strip() if isinstance(raw_query, str) else ""
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    if len(query) > 400:
        return JSONResponse({"error": "query too long (max 400 chars)"}, status_code=400)

    raw_max_results = body.get("max_results")
    if isinstance(raw_max_results, float) and raw_max_results.is_integer():
        raw_max_results = int(raw_max_results)
    if raw_max_results is None or (isinstance(raw_max_results, str) and not raw_max_results.strip()):
        max_results = _DEFAULT_RESULTS
    elif isinstance(raw_max_results, bool) or not isinstance(raw_max_results, (str, int)):
        return JSONResponse({"error": "max_results must be an integer"}, status_code=400)
    else:
        try:
            max_results = int(raw_max_results)
        except ValueError:
            return JSONResponse({"error": "max_results must be an integer"}, status_code=400)

    payload = await asyncio.to_thread(_search, query, max_results)
    status = 200 if "error" not in payload else 502
    return JSONResponse(payload, status_code=status)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


def _tool_search(*, query: str = "", max_results: int = _DEFAULT_RESULTS) -> str:
    """Agent-callable tool. Returns JSON string with search results."""
    payload = _search(query, max_results)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(api: Any) -> None:
    """PluginAPI v1 entry point."""
    api.register_tool(
        "search",
        _tool_search,
        description=(
            "Search the web using DuckDuckGo (free, no API key needed). "
            "Returns a list of results with title, URL, and snippet."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-20, default 5).",
                },
            },
            "required": ["query"],
        },
        timeout_sec=30,
    )
    api.register_route(
        "search",
        _route_search,
        methods=("POST",),
    )
    api.register_ui_tab(
        "search",
        "DuckDuckGo Search",
        icon="search",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "form",
                    "route": "search",
                    "method": "POST",
                    "target": "search_results",
                    "fields": [
                        {
                            "name": "query",
                            "label": "Search query",
                            "type": "text",
                            "placeholder": "Search the web...",
                            "required": True,
                        },
                        {
                            "name": "max_results",
                            "label": "Max results",
                            "type": "text",
                            "placeholder": "5",
                        },
                    ],
                    "submit_label": "Search",
                },
                {
                    "type": "json",
                    "target": "search_results",
                },
            ],
        },
    )
    api.log("info", "duckduckgo: extension registered (tool, route, ui_tab)")


__all__ = ["register"]
