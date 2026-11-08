from __future__ import annotations

import json
import pathlib

from .lib import client
from starlette.responses import JSONResponse


def _state_file(api, name: str) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / name


def _load_settings(api) -> dict:
    path = _state_file(api, "settings.json")
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return stored if isinstance(stored, dict) else {}


# The keys the settings route stores (the form's fields and the documented, still-unused progress tunables).
_SETTINGS_KEYS = (
    "A2A_HOST",
    "A2A_PORT",
    "A2A_AGENT_NAME",
    "A2A_AGENT_DESCRIPTION",
    "A2A_MAX_CONCURRENT",
    "A2A_RESPONSE_TIMEOUT_SEC",
    "A2A_ALLOCATE_TIMEOUT_SEC",
    "A2A_STREAM_DEADLINE_SEC",
    "A2A_PROGRESS_POLL_SEC",
    "A2A_HEARTBEAT_SEC",
    "A2A_PROGRESS_ENRICH",
    "A2A_SERVER_PASSWORD",
)
_SECRET_KEYS = frozenset({"A2A_SERVER_PASSWORD"})


def _make_settings_save(api):
    async def _settings_save(request):
        stored = _load_settings(api)
        if str(getattr(request, "method", "POST") or "POST").upper() == "GET":
            # Hydration read for the Settings form (CREATING_SKILLS): the stored non-secret values as strings, so the
            # form shows what is saved and its Save never rewrites values the owner did not see.
            return JSONResponse({key: str(stored[key]) for key in _SETTINGS_KEYS
                                 if key not in _SECRET_KEYS and stored.get(key) is not None})
        try:
            data = await request.json()
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "message": "Invalid A2A settings payload."}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "message": "Invalid A2A settings payload."}, status_code=400)
        merged = {key: value for key, value in stored.items() if key in _SETTINGS_KEYS}
        for key in _SETTINGS_KEYS:
            if key not in data:
                continue
            if key in _SECRET_KEYS and not str(data.get(key) or ""):
                continue  # a password field hydrates empty: an untouched one keeps the saved password
            merged[key] = data.get(key)
        path = _state_file(api, "settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "message": "A2A settings saved. Toggle the skill to restart the server."})
    return _settings_save


def register(api):
    api.register_tool(
        "discover",
        handler=lambda ctx, url="": client.discover(url),
        description="Discover another A2A-compatible agent by fetching its Agent Card.",
        schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    )
    api.register_tool(
        "send",
        handler=lambda ctx, url="", message="", task_id="", context_id="": client.send(
            url, message, task_id=task_id, context_id=context_id
        ),
        description="Send a message to another A2A-compatible agent.",
        # lib/client.py::send allows httpx 120s, so the default 60s tool timeout
        # would kill a slow-but-successful peer response before it could return.
        timeout_sec=130,
        schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "message": {"type": "string"},
                "task_id": {"type": "string"},
                "context_id": {"type": "string"},
            },
            "required": ["url", "message"],
        },
    )
    api.register_tool(
        "status",
        handler=lambda ctx, url="", task_id="": client.status(url, task_id),
        description="Check a remote A2A task status.",
        schema={
            "type": "object",
            "properties": {"url": {"type": "string"}, "task_id": {"type": "string"}},
            "required": ["url", "task_id"],
        },
    )
    api.register_route("settings/save", handler=_make_settings_save(api), methods=("GET", "POST"))
    api.register_settings_section(
        "a2a",
        title="A2A Server",
        schema={
            "components": [
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "fields": [
                        {"name": "A2A_HOST", "label": "Host", "type": "text", "placeholder": "127.0.0.1"},
                        {"name": "A2A_PORT", "label": "Port", "type": "number", "placeholder": "18800"},
                        {"name": "A2A_AGENT_NAME", "label": "Agent name", "type": "text"},
                        {"name": "A2A_AGENT_DESCRIPTION", "label": "Description", "type": "textarea"},
                        {"name": "A2A_MAX_CONCURRENT", "label": "Max concurrent inbound requests", "type": "number", "placeholder": "5"},
                        {"name": "A2A_RESPONSE_TIMEOUT_SEC", "label": "Host response wait seconds (max 1740)", "type": "number", "placeholder": "600"},
                        {"name": "A2A_STREAM_DEADLINE_SEC", "label": "Total per-message deadline seconds", "type": "number", "placeholder": "3600"},
                        {"name": "A2A_HEARTBEAT_SEC", "label": "Working-status heartbeat seconds", "type": "number", "placeholder": "25"},
                        {"name": "A2A_SERVER_PASSWORD", "label": "Server password for non-loopback binds", "type": "password"},
                    ],
                    "submit_label": "Save A2A settings",
                }
            ]
        },
    )
    _token = api.get_skill_token()  # noqa: F841 -- retained for companion token provisioning
    api.register_companion_process("a2a_server")
