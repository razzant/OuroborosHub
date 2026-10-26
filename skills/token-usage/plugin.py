"""Token Observatory plugin_api 2.0 entry point.

Only this module integrates with the host. Ingestion/accounting are stdlib-only.
The host adapter is intentionally explicit and fails visibly on an unknown API.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse


def _sibling(name):
    """Load own modules even when the extension loader does not alter sys.path."""
    location = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("_token_observatory_" + name, location)
    if spec is None or spec.loader is None:
        raise RuntimeError("Token Observatory module is unavailable: " + name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_accounting = _sibling("accounting")
QueryError, export_snapshot, query_snapshot = _accounting.QueryError, _accounting.export_snapshot, _accounting.query_snapshot
UsageIndex = _sibling("ingestion").UsageIndex

PREF_DEFAULTS = {"period": "all", "model": "", "route": "", "project": "", "task": "",
                 "scope": "all", "view": "auto", "custom_start": "", "custom_end": ""}
MAX_PREFERENCES_BYTES = 8192


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) - set(PREF_DEFAULTS):
        raise QueryError("Preferences must contain only supported display fields")
    result = dict(PREF_DEFAULTS)
    for key, item in value.items():
        if not isinstance(item, str) or len(item) > 256:
            raise QueryError("Display preferences must be short strings")
        result[key] = item
    if result["period"] not in ("1h", "24h", "7d", "30d", "all", "custom"):
        raise QueryError("Unsupported display period")
    if result["scope"] not in ("all", "own", "descendants"):
        raise QueryError("Unsupported task scope")
    if result["view"] not in ("auto", "physical", "sessions"):
        raise QueryError("Unsupported chart view")
    for key in ("custom_start", "custom_end"):
        item = result[key]
        if item:
            try:
                if len(item) != 10 or datetime.strptime(item, "%Y-%m-%d").strftime("%Y-%m-%d") != item:
                    raise ValueError()
            except ValueError:
                raise QueryError("Calendar preferences must use YYYY-MM-DD") from None
    if result["custom_start"] and result["custom_end"] and result["custom_start"] > result["custom_end"]:
        raise QueryError("Calendar start must be on or before end")
    return result


class Preferences:
    def __init__(self, state_dir):
        # The API's skill-owned state directory is the only runtime write target.
        self.directory = Path(state_dir) / "token-observatory"
        self.path = self.directory / "display-preferences.json"
        self.lock = threading.RLock()

    def _check_directory(self, create=False):
        for parent in reversed((self.directory, *self.directory.parents)):
            if parent.exists() or parent.is_symlink():
                if parent.is_symlink() or not parent.is_dir():
                    raise ValueError("Preference directory must not contain symlinks or non-directories")
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)

    def read(self):
        with self.lock:
            self._check_directory()
            if not self.path.exists() and not self.path.is_symlink():
                return dict(PREF_DEFAULTS)
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise ValueError("Display preferences must be a regular file")
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PREFERENCES_BYTES:
                    raise ValueError("Invalid or oversized display preferences")
                raw = source.read(MAX_PREFERENCES_BYTES + 1)
            return validate_preferences(json.loads(raw))

    def write(self, value):
        value = validate_preferences(value)
        with self.lock:
            self._check_directory(create=True)
            if self.path.is_symlink():
                raise ValueError("Preference file must not be a symlink")
            # Exclusive creation prevents following a planted temporary symlink.
            temporary = self.directory / (".preferences-" + os.urandom(12).hex() + ".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as target:
                    target.write(json.dumps(value, sort_keys=True).encode("utf-8"))
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return value


class Observatory:
    """One bounded background reader, with numeric query work off the ASGI loop."""
    def __init__(self, data_dir, state_dir, refresh_seconds=4.0):
        self._stop = threading.Event()
        self.index = UsageIndex(Path(data_dir), stop_event=self._stop)
        self.preferences = Preferences(state_dir)
        self.refresh_seconds = refresh_seconds
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._failure = None
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="token-observatory-query")

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Token Observatory is stopped")
            if self._thread is None:
                self._thread = threading.Thread(target=self._refresh_loop, name="token-observatory-index", daemon=True)
                self._thread.start()

    def _refresh_loop(self):
        while not self._stop.is_set():
            try:
                self.index.refresh()
                self._failure = None
            except Exception as exc:
                # Unexpected failures are visible; no stale success response.
                # Do not expose file paths, source line contents or host internals.
                self._failure = "Index refresh failed (" + type(exc).__name__ + "). Retry after correcting the source or restart the widget."
            self._ready.set()
            self._stop.wait(self.refresh_seconds)

    def _snapshot(self):
        if not self._ready.is_set():
            return {"rows": [], "snapshot_id": "catching-up", "coverage": {
                "status": "loading", "history_complete": False,
                "issues": ["Reading all retained numeric history. Initial catch-up is running in a background worker."]}}
        if self._failure:
            return {"rows": [], "snapshot_id": "refresh-error", "coverage": {
                "status": "error", "history_complete": False, "issues": [self._failure]}}
        return self.index.snapshot()

    async def query(self, params, *, export=False):
        self.start()
        loop = asyncio.get_running_loop()
        operation = export_snapshot if export else query_snapshot
        return await loop.run_in_executor(self._pool, lambda: operation(self._snapshot(), params))

    async def query_response(self, params, *, export=False):
        """Keep potentially large JSON serialization on the query workers too."""
        payload = await self.query(params, export=export)
        return await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: _response(payload, download=export))

    async def get_preferences(self):
        return await asyncio.get_running_loop().run_in_executor(self._pool, self.preferences.read)

    async def put_preferences(self, value):
        return await asyncio.get_running_loop().run_in_executor(self._pool, self.preferences.write, value)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        # No join on the ASGI loop. The bounded current source pass may finish.


def _response(payload, status=200, *, download=False):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="token-observatory-snapshot.json"'
    return JSONResponse(payload, status_code=status, headers=headers)


def _request_params(request: Request):
    pairs = list(request.query_params.multi_items())
    if len({key for key, _ in pairs}) != len(pairs):
        raise QueryError("Duplicate query parameters are not supported")
    return dict(pairs)


def usage_chip(payload):
    """Format the existing 24-hour physical-call summary; unknown stays unknown."""
    summary = payload["summary"]
    coverage = payload.get("coverage") or {}
    if coverage.get("status") in ("loading", "error"):
        return {"text": "—", "tone": "muted", "title": "\n".join(coverage.get("issues") or ["Usage unavailable"])}
    tokens = summary["reported_tokens"]
    cost = summary["metrics"]["cost_confirmed_usd"]["value"]
    calls = summary["physical_calls"]
    if tokens is None:
        token_text = "unknown"
    else:
        token_text = str(tokens)
        for threshold, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "k")):
            if tokens >= threshold:
                token_text = f"{tokens / threshold:.1f}{suffix}"
                break
    cost_text = "unknown" if cost is None else f"${cost:.2f}"
    title = (f"24h physical calls: {calls}\nReported tokens: {tokens if tokens is not None else 'unknown'}"
             f"\nConfirmed cost: {cost_text}"
             f"\nFully reported calls: {summary['fully_reported_rows']}/{calls}"
             f"\nCalls with unknown cost: {summary['unknown_cost_rows']}")
    if coverage.get("issues"):
        title += "\n" + "\n".join(coverage["issues"])
    return {"text": f"{cost_text} · 24h" if cost is not None else f"24h · {token_text} tok",
            "tone": "muted", "title": title}


def register(api):
    """Register own relative routes; the host owns prefixing and widget lifecycle."""
    runtime = api.get_runtime_info()
    if not isinstance(runtime, dict) or not all(isinstance(runtime.get(key), str) and runtime[key]
                                                for key in ("data_dir", "state_dir")):
        raise RuntimeError("get_runtime_info() must expose data_dir and skill-owned state_dir")
    service = Observatory(runtime["data_dir"], runtime["state_dir"])

    async def data(request: Request):
        try:
            return await service.query_response(_request_params(request))
        except QueryError as exc:
            return _response({"error": str(exc)}, 400)
        except Exception as exc:
            return _response({"error": "Dashboard query failed (" + type(exc).__name__ + "). Retry or restart the widget."}, 500)

    async def chip(_request: Request):
        try:
            return _response(usage_chip(await service.query({"period": "24h", "page_size": "1"})))
        except Exception as exc:
            return _response({"text": "—", "tone": "muted", "title": f"Usage unavailable ({type(exc).__name__})"})

    async def export(request: Request):
        try:
            return await service.query_response(_request_params(request), export=True)
        except QueryError as exc:
            return _response({"error": str(exc)}, 400)
        except Exception as exc:
            return _response({"error": "Snapshot export failed (" + type(exc).__name__ + ")."}, 500)

    async def preferences(request: Request):
        if request.method == "GET":
            try:
                return _response(await service.get_preferences())
            except Exception as exc:
                return _response({"error": "Display preferences could not be read (" + type(exc).__name__ + ")."}, 500)
        if request.method != "PUT":
            return _response({"error": "Method not allowed"}, 405)
        try:
            header = request.headers.get("content-length")
            if header is not None and (int(header) < 0 or int(header) > MAX_PREFERENCES_BYTES):
                raise QueryError("Display preferences are too large")
            chunks, total = [], 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_PREFERENCES_BYTES:
                    raise QueryError("Display preferences are too large")
                chunks.append(chunk)
            value = json.loads(b"".join(chunks))
            return _response(await service.put_preferences(value))
        except (QueryError, ValueError, UnicodeError) as exc:
            return _response({"error": str(exc) if isinstance(exc, QueryError) else "Invalid display preference document"}, 400)
        except Exception as exc:
            return _response({"error": "Display preferences could not be saved (" + type(exc).__name__ + ")."}, 500)

    try:
        api.register_route("data", data, methods=["GET"])
        api.register_route("chip", chip, methods=["GET"])
        api.register_route("export", export, methods=["GET"])
        api.register_route("preferences", preferences, methods=["GET", "PUT"])
        api.register_ui_tab("observatory", "Token Observatory", icon="◉", render={
            "kind": "module", "entry": "widget.js", "appearance": "host", "height": 560, "span": 2, "start": "manual"})
        api.on_unload(service.close)
    except Exception:
        service.close()
        raise
    return service
