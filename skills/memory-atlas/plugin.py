"""PluginAPI 2.1 adapter for Memory Atlas.

The host loads this file as the top module of a synthetic package whose
``__path__`` is the payload directory, so sibling modules are imported
relatively. Two kinds of route are registered, all GET and read-only:

- ``atlas/v2/<op>``: the ``memory-atlas.read/2`` contract (``atlas_routes``),
  answered by ``CoreReader`` over the host's ``api.read_memory``
  (``ouroboros.memory/1``). The manifest declares ``plugin_api: "2.1"``, so an
  older host refuses the skill instead of half-running it; a host object without
  the method still gets ``UnconnectedReader``, which refuses every read.
- ``author-kit``: the installed shared controls (``web/ui.css``) through the
  core's documented author-kit helper, read at mount like
  ``docs/examples/author_ui_kit``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping

from .atlas_reader import reader_for
from .atlas_routes import OPS, ROUTE_PREFIX, dispatch, error_body

TAB_ID = "atlas"
TITLE = "Memory Atlas"
ICON = "◈"
# Kept identical to ui_tab.render in SKILL.md (a test compares them).
RENDER = {
    "kind": "module",
    "entry_type": "module",
    "entry": "app/main.js",
    "assets": ["app/atlas.js", "app/client.js", "app/words.js", "app/markdown.js", "app/parts.js", "app/atlas.css"],
    "start": "manual",
    "appearance": "host",
    "height": 600,
    "span": 2,
}
NO_STORE = {"Cache-Control": "no-store"}


def _json(payload: Mapping[str, Any], status: int = 200):
    from starlette.responses import JSONResponse

    return JSONResponse(dict(payload), status_code=status, headers=NO_STORE)


async def author_kit(request: Any):
    """The installed ``web/ui.css`` for this mount; a failure leaves the widget readable in plain style."""
    try:
        from ouroboros.server_web import read_author_kit_assets

        assets = read_author_kit_assets(request.app.state.repo_dir)
        return _json({"css": assets["css"]})
    except Exception as exc:
        return _json({"error": {"code": "kit_unavailable", "message": type(exc).__name__}}, 503)


class MemoryAtlasPlugin:
    def __init__(self) -> None:
        self.reader: Any = None
        self._disposers: list[Any] = []

    def register(self, api: Any) -> None:
        self.reader = reader_for(api)
        for op in OPS:
            self._keep(api.register_route(ROUTE_PREFIX + op, handler=self._handler(op), methods=("GET",)))
        self._keep(api.register_route("author-kit", handler=author_kit, methods=("GET",)))
        self._keep(api.register_ui_tab(TAB_ID, title=TITLE, icon=ICON, render=dict(RENDER)))
        if callable(getattr(api, "on_unload", None)):
            api.on_unload(self.dispose)

    def _keep(self, result: Any) -> None:
        if callable(result):
            self._disposers.append(result)

    def dispose(self) -> None:
        for disposer in reversed(self._disposers):
            disposer()
        self._disposers.clear()
        self.reader = None

    def _handler(self, op: str):
        async def handle(request: Any):
            reader = self.reader
            if reader is None:
                return _json(error_body("reader_not_connected", "Memory Atlas is not registered"), 503)
            status, body = await asyncio.to_thread(dispatch, reader, op, dict(request.query_params))
            return _json(body, status)
        return handle


plugin = MemoryAtlasPlugin()


def register(api: Any) -> None:
    plugin.register(api)
