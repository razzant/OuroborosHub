"""Context Lens — read-only HTTP surface over this install's usage record.

Two GET routes under this skill's own namespace, one module widget, nothing
else. No tools, no WebSocket, no network, no subprocess, no writes anywhere.

    GET /api/extensions/context-lens/data        ?horizon=&limit=&refresh=
    GET /api/extensions/context-lens/trajectory  ?task=&snapshot=&horizon=

The serving root is ``PluginAPI.get_runtime_info()["data_dir"]``. The source is
the usage store ``state/usage.sqlite``, read by ``lens_store`` strictly
read-only; only when NO file exists at that name is the retired journal
``state/usage_attempts.jsonl`` read instead, and the answer says it is
historical. A store that exists but cannot be read is reported as such — it is
never papered over with older data. There is no path parameter anywhere in this
module, so no route can be steered at another file.
"""

from __future__ import annotations

import collections
import secrets
import threading
from typing import Any, Dict, Optional

try:  # Loaded as a package by extension_loader (submodule_search_locations).
    from . import lens_core, lens_store
except ImportError:  # Direct import (tests, tooling) — resolve the sibling files.
    import importlib.util as _util
    import pathlib as _pathlib

    def _sibling(name: str, filename: str):
        spec = _util.spec_from_file_location(
            name, _pathlib.Path(__file__).resolve().parent / filename)
        module = _util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        return module

    lens_core = _sibling("context_lens_lens_core", "lens_core.py")
    lens_store = _sibling("context_lens_lens_store", "lens_store.py")


SKILL = "context-lens"

# Owner-visible sentences for the typed failures. The browser never receives a
# filesystem path, an exception message or a stored row.
_REASONS = {
    "no_data_dir": "The host did not report a data directory for this install.",
    "no_ledger": "This install has no usage record yet. It appears after the first model request.",
    "ledger_unreadable": "The retired usage journal exists but could not be read.",
    "ledger_platform_unsupported": "This platform cannot open the retired journal relative to a verified directory, so it was not read.",
    "ledger_not_confined": "The retired usage journal path does not resolve inside this install's data directory, so it was not read.",
    "ledger_not_regular": "The retired usage journal path is not a regular file in this install, so it was not read.",
    "store_busy": "The usage store is busy with a write right now. Try again in a moment.",
    "store_slow": "Reading the usage store took longer than this widget allows, so the read was abandoned.",
    "store_unreadable": "The usage store exists but could not be read.",
    "store_unsupported": "The usage store has a format this version of Context Lens does not read.",
    "store_not_ready": "The usage store exists but its one-time import has not completed, so nothing is shown.",
    "store_name_tier": "This install keeps its usage store on a filesystem without kernel file locks. Reading it safely needs the host's money lock, which this read-only widget cannot take, so nothing is read.",
    "store_not_confined": "This reader requires a real data directory, state directory and store file; a checked path is symlinked or invalid, so it was not read.",
    "store_not_regular": "The usage store path is not a regular file in this install, so it was not read.",
    "store_replaced": "The usage store was replaced while it was being read, so that read was discarded. Try again.",
    "snapshot_expired": "That snapshot is no longer held. Reload the overview and ask again.",
    "runtime_info_unavailable": "The host did not answer when this skill asked where this install keeps its data, so nothing was read. The reason was written to the host log.",
}

# How many recent snapshots the trajectory route can still answer from. Each
# holds only the points that snapshot sent, so the memory bound is
# MAX_SNAPSHOTS * lens_core.MAX_POINT_LIMIT sanitized points.
MAX_SNAPSHOTS = 2

_state_lock = threading.Lock()
_window: Optional["lens_core.LedgerWindow"] = None
_snapshots: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
_data_dir = ""
# True when get_runtime_info() itself failed. Kept separate from an empty
# data_dir so the answer never claims the host reported no data directory when
# what actually happened is that the host did not answer at all.
_runtime_info_failed = False


def _get_window() -> "lens_core.LedgerWindow":
    """One shared bounded journal reader. Routes dispatch on threads, so this is locked."""
    global _window
    with _state_lock:
        if _window is None:
            _window = lens_core.LedgerWindow(_data_dir)
        return _window


def _reset() -> None:
    global _window
    with _state_lock:
        _window = None
        _snapshots.clear()


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


def _read(horizon: str, limit: int, force_cold: bool) -> Dict[str, Any]:
    """One snapshot payload from whichever source this install has.

    Raises ``lens_core.LensUnavailable`` for every typed failure.
    """
    if _runtime_info_failed:
        raise lens_core.LensUnavailable("runtime_info_unavailable")
    if not _data_dir.strip():
        raise lens_core.LensUnavailable("no_data_dir")
    anchor = lens_core.now_ms()
    span = lens_core.HORIZON_SPANS_MS.get(horizon)
    lower_ms = lens_core.MIN_EPOCH_MS if span is None else max(lens_core.MIN_EPOCH_MS, anchor - span)
    try:
        read = lens_store.read_store(
            _data_dir,
            lower_s=lower_ms / 1000.0,
            upper_s=lens_core.MAX_EPOCH_MS / 1000.0,
            band_lower_s=lens_core.MIN_EPOCH_MS / 1000.0,
        )
    except lens_store.StoreMissing:
        # Nothing at the store's name: the journal is the only record there is.
        window = _get_window()
        window.refresh(force_cold=force_cold)
        payload = window.snapshot(limit=limit, horizon=horizon, anchor_ms=anchor)
    except lens_store.StoreUnavailable as exc:
        raise lens_core.LensUnavailable(exc.code) from None
    else:
        payload = lens_core.store_snapshot(
            read, horizon=horizon, anchor_ms=anchor, limit=limit,
            limits={"max_rows": lens_store.MAX_ROWS, "max_points": lens_core.MAX_POINT_LIMIT,
                    "max_categories": lens_store.MAX_CATEGORIES,
                    "merge_budget_ms": int(lens_store.MERGE_BUDGET_SEC * 1000)})
    snapshot_id = "s-" + secrets.token_hex(6)
    payload["snapshot"] = {"id": snapshot_id}
    with _state_lock:
        _snapshots[snapshot_id] = {
            "points": payload["points"],
            "horizon": payload["horizon"],
            "points_omitted": payload["points_omitted"],
        }
        while len(_snapshots) > MAX_SNAPSHOTS:
            _snapshots.popitem(last=False)
    return payload


def route_data(request: Any) -> Dict[str, Any]:
    """Source facts, the selection, exact counters, filter facets and the points.

    ``horizon`` is normalized against a fixed set of tokens, so an unknown value
    falls back to ``available`` instead of being reflected or trusted. Every
    answer carries its own snapshot id, its anchor, what it selected and whether
    that selection is complete.
    """
    params = _query(request)
    limit = _int_param(params, "limit", lens_core.DEFAULT_POINT_LIMIT)
    horizon = lens_core.normalize_horizon(params.get("horizon", ""))
    force_cold = params.get("refresh", "").strip().lower() in {"1", "true", "yes", "cold"}
    try:
        return _read(horizon, limit, force_cold)
    except lens_core.LensUnavailable as exc:
        return _unavailable(exc.code)


def route_trajectory(request: Any) -> Dict[str, Any]:
    """One task's measured requests from ONE snapshot (compatibility route).

    With ``snapshot`` the answer comes from that snapshot's own points, or is
    ``snapshot_expired`` when it is no longer held — never silently from a newer
    read. Without it, a fresh read is taken and its own snapshot id and anchor
    are stated. ``task`` must be an opaque key this skill minted; anything else
    answers with the empty shape and is never reflected.
    """
    params = _query(request)
    task = params.get("task", "")[:64]
    wanted = params.get("snapshot", "").strip()[:32]
    if wanted:
        with _state_lock:
            held = _snapshots.get(wanted)
        if held is None:
            if _runtime_info_failed:
                return _unavailable("runtime_info_unavailable")
            return dict(_unavailable("snapshot_expired"), available=True)
        snapshot_id = wanted
    else:
        horizon = lens_core.normalize_horizon(params.get("horizon", ""))
        try:
            payload = _read(horizon, lens_core.MAX_POINT_LIMIT, False)
        except lens_core.LensUnavailable as exc:
            return _unavailable(exc.code)
        snapshot_id = payload["snapshot"]["id"]
        held = {"points": payload["points"], "horizon": payload["horizon"],
                "points_omitted": payload["points_omitted"]}
    answer = lens_core.trajectory(held["points"], task, snapshot=snapshot_id,
                                  horizon=held["horizon"], points_omitted=held["points_omitted"])
    answer["ok"] = True
    answer["available"] = True
    return answer


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
    _reset()

    api.register_route("data", handler=route_data, methods=("GET",))
    api.register_route("trajectory", handler=route_trajectory, methods=("GET",))
    api.register_ui_tab(
        "lens",
        "Context Lens",
        icon="◉",
        render={
            "kind": "module",
            "entry": "widget.js",
            # Opts into the host's resolved Light/Dark palette through the
            # optional OuroborosWidget.onTheme bridge (first shipped in 1.1.3).
            "appearance": "host",
            "start": "auto",
            # A chart card reads best across both masonry columns; the host
            # normalizes render.span to 1 or 2 and falls back to one column
            # when the page is too narrow.
            "span": 2,
            # No `height`: the card follows its content through the host's
            # auto-height bridge. The widget keeps that loop-free: the chart box
            # has a fixed CSS height, nothing sizes itself from the viewport, and
            # its own ResizeObserver only schedules a canvas repaint, which
            # changes no layout.
        },
    )
    api.on_unload(_reset)
