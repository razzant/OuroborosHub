"""Perplexity Research extension — LLM-grounded web search via OpenRouter.

Uses OpenRouter's ``openrouter:web_search`` server tool to perform
real-time web searches through any supported model. Returns a
synthesized answer with citations/annotations.

Requires: OPENROUTER_API_KEY in settings (granted by owner).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse


_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
_DEFAULT_MAX_RESULTS = 5
_TIMEOUT_SEC = 60
_MAX_TOKENS = 2000


def _research(query: str, api_key: str, max_results: int = _DEFAULT_MAX_RESULTS) -> Dict[str, Any]:
    """Perform a grounded web research query via OpenRouter.

    Returns a dict with ``answer``, ``citations``, and ``model``,
    or an ``error`` field on failure.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return {"error": "query is empty"}
    if len(cleaned) > 2000:
        return {"error": "query too long (max 2000 chars)"}
    if not api_key:
        return {"error": "OPENROUTER_API_KEY not configured — set it in Settings and grant it to this skill"}

    max_results = min(max(1, int(max_results or _DEFAULT_MAX_RESULTS)), 25)

    payload = {
        "model": _DEFAULT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a research assistant. Answer the user's question "
                    "using web search results. Be concise but thorough. "
                    "Always cite your sources."
                ),
            },
            {"role": "user", "content": cleaned},
        ],
        "tools": [
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "max_results": max_results,
                    "search_context_size": "medium",
                },
            }
        ],
        "max_tokens": _MAX_TOKENS,
    }

    req = urllib.request.Request(
        _OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ouroboros.local",
            "X-Title": "Ouroboros Perplexity Skill",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            raw = resp.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return {"error": f"OpenRouter HTTP {exc.code}: {body or exc.reason}"}
    except urllib.error.URLError as exc:
        return {"error": f"network error: {exc.reason!r}"}
    except TimeoutError:
        return {"error": f"OpenRouter timed out after {_TIMEOUT_SEC}s"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        data = json.loads(raw)
    except ValueError:
        return {"error": "OpenRouter returned non-JSON response"}

    # Extract the answer
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")

    if not content:
        return {"error": "model returned empty response"}

    # Extract citations from annotations
    annotations = message.get("annotations", [])
    citations = []
    for ann in annotations:
        if ann.get("type") == "url_citation":
            # OpenRouter nests the source under "url_citation"; a flat annotation is read as it is.
            cite = ann.get("url_citation") if isinstance(ann.get("url_citation"), dict) else ann
            citations.append({
                "title": cite.get("title", ""),
                "url": cite.get("url", ""),
            })

    # Usage info
    usage = data.get("usage", {})

    return {
        "query": cleaned,
        "answer": content,
        "citations": citations,
        "model": data.get("model", _DEFAULT_MODEL),
        "tokens_used": usage.get("total_tokens", 0),
    }


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------

_plugin_api_ref: Any = None


async def _route_research(request: Request) -> JSONResponse:
    """POST /api/extensions/perplexity/research

    Body: {"query": "...", "max_results": 5}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    value = body.get("max_results", _DEFAULT_MAX_RESULTS)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
        return JSONResponse({"error": "max_results must be an integer"}, status_code=400)
    try:
        max_results = int(value or _DEFAULT_MAX_RESULTS)
    except ValueError:
        return JSONResponse({"error": "max_results must be an integer"}, status_code=400)
    query = str(body.get("query", "")).strip()

    api_key = _get_api_key()

    payload = await asyncio.to_thread(_research, query, api_key, max_results)
    status = 200 if "error" not in payload else 502
    return JSONResponse(payload, status_code=status)


def _get_api_key() -> str:
    """Retrieve the OpenRouter API key through the PluginAPI grant path."""
    if _plugin_api_ref is not None:
        settings = _plugin_api_ref.get_settings(["OPENROUTER_API_KEY"])
        return settings.get("OPENROUTER_API_KEY", "")
    return ""


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


def _tool_research(*, query: str = "", max_results: int = _DEFAULT_MAX_RESULTS) -> str:
    """Agent-callable tool. Performs web research with citations."""
    api_key = _get_api_key()
    payload = _research(query, api_key, max_results)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(api: Any) -> None:
    """PluginAPI v1 entry point."""
    global _plugin_api_ref
    _plugin_api_ref = api

    api.register_tool(
        "research",
        _tool_research,
        description=(
            "Deep web research using OpenRouter's web search. "
            "Returns a synthesized answer with source citations. "
            "Requires OPENROUTER_API_KEY granted to this skill."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Research question or search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max search results for grounding (1-25, default 5).",
                },
            },
            "required": ["query"],
        },
        timeout_sec=90,
    )
    api.register_route(
        "research",
        _route_research,
        methods=("POST",),
    )
    api.register_ui_tab(
        "research",
        "Perplexity Research",
        icon="search",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "form",
                    "route": "research",
                    "method": "POST",
                    "target": "research_results",
                    "fields": [
                        {
                            "name": "query",
                            "label": "Research question",
                            "type": "text",
                            "placeholder": "Ask anything \u2014 get a grounded answer with sources...",
                            "required": True,
                        },
                        {
                            "name": "max_results",
                            "label": "Max search results",
                            "type": "text",
                            "placeholder": "5",
                        },
                    ],
                    "submit_label": "Research",
                },
                {
                    "type": "markdown",
                    "target": "research_results",
                    "path": "answer",
                },
                {
                    "type": "json",
                    "target": "research_results",
                    "path": "citations",
                },
            ],
        },
    )

    def _cleanup() -> None:
        global _plugin_api_ref
        _plugin_api_ref = None

    api.on_unload(_cleanup)
    api.log("info", "perplexity: extension registered (tool, route, ui_tab)")


__all__ = ["register"]
