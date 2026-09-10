"""Context Lens — read-only HTTP surface for the local usage attempt ledger.

Two GET routes under this skill's own namespace, one module widget, nothing
else. No tools, no WebSocket, no network, no subprocess, no writes anywhere.

    GET /api/extensions/context-lens/data        ?limit=&refresh=&horizon=
    GET /api/extensions/context-lens/trajectory  ?task=&horizon=

The serving root is ``PluginAPI.get_runtime_info()["data_dir"]`` and the only
file opened under it is the fixed ``state/usage_attempts.jsonl`` (see
``lens_core`` for the anchors). There is no path parameter anywhere in this
module, so no route can be steered at another file.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

try:  # Loaded as a package by extension_loader (submodule_search_locations).
    from . import lens_core
except ImportError:  # Direct import (tests, tooling) — resolve the sibling file.
    import importlib.util as _util
    import pathlib as _pathlib

    _spec = _util.spec_from_file_location(
        "context_lens_lens_core", _pathlib.Path(__file__).resolve().parent / "lens_core.py"
    )
    lens_core = _util.module_from_spec(_spec)  # type: ignore[arg-type]
    assert _spec is not None and _spec.loader is not None
    _spec.loader.exec_module(lens_core)


SKILL = "context-lens"

# Owner-visible sentences for the typed failures. The browser never receives a
# filesystem path, an exception message or a ledger line.
_REASONS = {
    "no_data_dir": "The host did not report a data directory for this install.",
    "no_ledger": "No usage ledger exists yet in this install. It appears after the first model request.",
    "ledger_unreadable": "The usage ledger exists but could not be read.",
    "ledger_not_confined": "The usage ledger path does not resolve to the ledger file inside this install's data directory, so it was not read.",
    "ledger_not_regular": "The usage ledger path is not a regular file in this install, so it was not read.",
    "runtime_info_unavailable": "The host did not answer when this skill asked where this install keeps its data, so nothing was read. The reason was written to the host log.",
}

_state_lock = threading.Lock()
_window: Optional["lens_core.LedgerWindow"] = None
_data_dir = ""
# True when get_runtime_info() itself failed. Kept separate from an empty
# data_dir so the answer never claims the host reported no data directory when
# what actually happened is that the host did not answer at all.
_runtime_info_failed = False


def _get_window() -> "lens_core.LedgerWindow":
    """One shared bounded reader. Routes dispatch on threads, so this is locked."""
    global _window
    with _state_lock:
        if _runtime_info_failed:
            raise lens_core.LensUnavailable("runtime_info_unavailable")
        if _window is None:
            _window = lens_core.LedgerWindow(_data_dir)
        return _window


def _reset_window() -> None:
    global _window
    with _state_lock:
        _window = None


def _unavailable(code: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "available": False,
        "reason": code,
        "message": _REASONS.get(code, "Telemetry is unavailable."),
    }


def _query(request: Any) -> Dict[str, str]:
    params = getattr(request, "query_params", None) or {}
    try:
        return {str(key): str(value) for key, value in dict(params).items()}
    except (TypeError, ValueError):
        return {}


def _int_param(params: Dict[str, str], name: str, default: int) -> int:
    raw = params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def route_data(request: Any) -> Dict[str, Any]:
    """Window facts, exact counters, filter facets and the sanitized points.

    ``horizon`` selects how far back the answer reaches. It is normalized against
    a fixed set of tokens, so an unknown value falls back to ``available``
    instead of being reflected or trusted, and the answer always states which
    horizon it actually used, the UTC anchor it measured from and how much of
    the requested span this bounded reader could really observe.
    """
    params = _query(request)
    limit = _int_param(params, "limit", lens_core.DEFAULT_POINT_LIMIT)
    horizon = lens_core.normalize_horizon(params.get("horizon", ""))
    force_cold = params.get("refresh", "").strip().lower() in {"1", "true", "yes", "cold"}
    try:
        window = _get_window()
        window.refresh(force_cold=force_cold)
        return window.snapshot(limit=limit, horizon=horizon)
    except lens_core.LensUnavailable as exc:
        return _unavailable(exc.code)


def route_trajectory(request: Any) -> Dict[str, Any]:
    """One task's measured attempts, grouped by model and work kind.

    ``task`` must be an opaque key this skill itself minted; ``lens_core``
    validates its exact shape and answers an unrecognised key with the empty
    trajectory, so no supplied text is ever reflected back to the browser.
    """
    params = _query(request)
    task = params.get("task", "")[:64]
    horizon = lens_core.normalize_horizon(params.get("horizon", ""))
    try:
        window = _get_window()
        window.refresh()
        payload = lens_core.trajectory(window.records(), task, horizon=horizon)
    except lens_core.LensUnavailable as exc:
        return _unavailable(exc.code)
    payload["ok"] = True
    payload["available"] = True
    return payload


def register(api: Any) -> None:
    global _data_dir, _runtime_info_failed
    info = {}
    _runtime_info_failed = False
    try:
        info = api.get_runtime_info() or {}
    except Exception as exc:
        # Registration must still finish, but a swallowed failure would leave the
        # routes asserting "the host reported no data directory" — a claim about
        # the host that is simply not known to be true. Record what happened for
        # the owner, in the host's own log, naming only the exception TYPE: an
        # exception message can carry a path or a credential and this skill
        # discloses neither. `log` needs no declared permission.
        info = {}
        _runtime_info_failed = True
        try:
            api.log("warning", "context-lens: get_runtime_info() raised %s; "
                               "telemetry is unavailable for this session"
                               % type(exc).__name__)
        except Exception:
            pass
    _data_dir = str((info or {}).get("data_dir") or "")
    _reset_window()

    api.register_route("data", handler=route_data, methods=("GET",))
    api.register_route("trajectory", handler=route_trajectory, methods=("GET",))
    api.register_ui_tab(
        "lens",
        "Context Lens",
        icon="◉",
        render={
            "kind": "module",
            "entry": "widget.js",
            "start": "auto",
            # A chart card reads best across both masonry columns; the host
            # normalizes render.span to 1 or 2 (extension_surface_names.
            # _widget_span_from_render) and falls back to one column when the
            # page is too narrow for a wide card.
            "span": 2,
            # A FIXED frame height, deliberately: with `render.height` set the
            # host mounts the module frame without its auto-height bridge
            # (web/modules/widget_module.js: `autoHeight = render.height ===
            # undefined || render.height === null`), so the only ResizeObserver
            # left in the frame is this widget's own width watcher. Under
            # auto-height the host observes #root, posts a height, the card
            # resizes, the frame re-lays out and observes again — the loop WebKit
            # reports as an undelivered-notification error. The card owns its own
            # vertical scrolling instead; nothing is suppressed or swallowed.
            "height": 760,
        },
    )
    api.on_unload(_reset_window)
