"""PluginAPI 2.0 adapter for Memory Atlas.

The host loads this file as the top module of a synthetic package whose
``__path__`` is the payload directory, so sibling modules are imported
relatively. An absolute ``import memory_reader`` would depend on the payload
directory being on ``sys.path``, which the loader does not do.
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse

from .memory_reader import AtlasError, MemoryReader, error_response

ROUTES = ("catalog", "document", "history", "history/event", "search",
          "graph", "dialogue")


class MemoryAtlasPlugin:
    def __init__(self) -> None:
        self.reader: MemoryReader | None = None
        self._disposers: list[Any] = []

    def register(self, api: Any) -> None:
        info = api.get_runtime_info()
        data_dir = info.get("data_dir") if isinstance(info, Mapping) else None
        self.reader = MemoryReader(data_dir)
        for name in ROUTES:
            result = api.register_route(name, handler=self._handler(name), methods=("GET",))
            if callable(result):
                self._disposers.append(result)
        result = api.register_ui_tab(
            "atlas", title="Memory Atlas", icon="◈",
            render={"kind": "module", "entry": "widget.js", "start": "manual",
                    "height": 580, "span": 2},
        )
        if callable(result):
            self._disposers.append(result)
        if callable(getattr(api, "on_unload", None)):
            api.on_unload(self.dispose)

    def dispose(self) -> None:
        for dispose in reversed(self._disposers):
            dispose()
        self._disposers.clear()
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def _handler(self, route: str):
        async def handle(request: Request) -> JSONResponse:
            try:
                payload = await asyncio.to_thread(
                    self.dispatch, route, dict(request.query_params))
                return JSONResponse(payload, headers={"Cache-Control": "no-store"})
            except Exception as exc:
                status, payload = error_response(exc)
                return JSONResponse(payload, status_code=status,
                                    headers={"Cache-Control": "no-store"})
        return handle

    def dispatch(self, route: str, query: Mapping[str, Any]) -> dict[str, Any]:
        if self.reader is None:
            raise RuntimeError("plugin not registered")
        allowed = {
            "catalog": {"cursor", "limit"},
            "document": {"id", "cursor", "limit", "revision"},
            "history": {"id", "cursor", "limit", "revision"},
            "history/event": {"id", "event_id", "cursor", "limit", "revision"},
            "search": {"q", "cursor", "limit", "case_sensitive", "revision"},
            "graph": {"focus", "limit", "revision"},
            "dialogue": {"cursor", "limit", "revision"},
        }
        if route not in allowed:
            raise AtlasError(404, "route_not_found", "route not found")
        unknown = set(query) - allowed[route]
        if unknown:
            raise AtlasError(400, "unknown_parameter",
                             "unknown parameter: " + sorted(unknown)[0])
        q = dict(query)
        if "limit" in q:
            try:
                q["limit"] = int(q["limit"])
            except (TypeError, ValueError) as exc:
                raise AtlasError(400, "invalid_limit", "limit must be an integer") from exc
        for key in ("case_sensitive",):
            if key in q:
                if q[key] not in ("true", "false", True, False):
                    raise AtlasError(400, "invalid_boolean", f"{key} must be true or false")
                q[key] = q[key] in ("true", True)
        required = {"document": ("id",), "history": ("id",),
                    "history/event": ("id", "event_id"), "search": ("q",),
                    "graph": ("focus",)}.get(route, ())
        for key in required:
            if not q.get(key):
                raise AtlasError(400, "missing_parameter", f"missing required parameter: {key}")
        if route in ("document", "history", "history/event"):
            q["source_id"] = q.pop("id")
        if route == "search":
            q["query"] = q.pop("q")
        return getattr(self.reader, route.replace("/", "_"))(**q)


plugin = MemoryAtlasPlugin()


def register(api: Any) -> None:
    plugin.register(api)
