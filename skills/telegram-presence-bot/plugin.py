"""Owner setup for the separate Bot API Presence transport.

The token is a host-owned Settings secret, NOT stored in this skill's config,
GET response or route exception. The Host checks it against the owner bot.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bot.config import load, save, validate


def _reply(payload: dict, status: int = 200):
    from starlette.responses import JSONResponse
    return JSONResponse(payload, status_code=status)


def _settings_route(api: Any):
    async def route(request):
        state = Path(api.get_state_dir())
        if request.method == "GET":
            try:
                config = load(state)
            except (ValueError, OSError):
                return _reply({"ok": False, "code": "config_unavailable"}, 409)
            return _reply({"bindings": json.dumps(config["bindings"], separators=(",", ":")),
                           "download_video": config["download_video"],
                           "download_documents": config["download_documents"]})
        try:
            body = await request.json()
            if type(body) is not dict or set(body) != {"bindings", "download_video", "download_documents"}:
                raise ValueError("invalid configuration fields")
            if type(body["bindings"]) is not str or len(body["bindings"]) > 100_000:
                raise ValueError("bindings must be a bounded JSON list")
            config = validate({"schema": 1, "bindings": json.loads(body["bindings"]),
                               "download_video": body["download_video"],
                               "download_documents": body["download_documents"]})
            save(state, config)
        except (ValueError, OSError, TypeError):
            return _reply({"ok": False, "code": "configuration_rejected"}, 400)
        return _reply({"ok": True, "message": "Exact bindings and media choices saved. No Telegram request was made."})
    return route


def _status_route(api: Any):
    async def route(_request):
        state = Path(api.get_state_dir())
        try:
            config = load(state)
            from .bot.store import Store
            with Store(state / "transport.sqlite3") as store:
                counts = store.counts()
                outbox = store.outbound_counts()
        except (ValueError, OSError):
            return _reply({"ok": False, "code": "private_state_unavailable"}, 409)
        return _reply({"ok": True, "bound_topics": len(config["bindings"]), "inbox": counts,
                       "outbox": outbox,
                       "note": ("Account/login and Telegram delivery require the running companion; "
                                "a saved binding proves neither.")})
    return route


_SETTINGS_SCHEMA = {"components": [
    {"type": "markdown", "text": (
        "Separate second bot: grant its own TELEGRAM_PRESENCE_BOT_TOKEN in Secrets; "
        "never reuse the owner bot token. Incoming unbound messages stay private. "
        "Owner-created exact peer/topic Presence bindings alone permit model turns and public replies.")},
    {"type": "form", "route": "setup/config", "method": "POST", "fields": [
        {"name": "bindings", "label": "Exact chat/topic bindings (JSON)", "type": "text",
         "help": ("[{\"chat_id\":-100123,\"topic_id\":42,\"binding_id\":\"32 hex chars\"}]. "
                  "General uses null; no fallback.")},
        {"name": "download_video", "label": "Download video (up to 20 MiB each)", "type": "checkbox"},
        {"name": "download_documents", "label": "Download ordinary files (up to 20 MiB each)", "type": "checkbox"},
    ], "submit_label": "Save Presence bot scope"},
    {"type": "action", "id": "bot_status", "route": "setup/status", "method": "POST",
     "submit_label": "Check private inbox", "fields": []},
]}


def register(api: Any) -> None:
    api.register_companion_process("presence_bot")
    api.register_route("setup/config", handler=_settings_route(api), methods=("GET", "POST"))
    api.register_route("setup/status", handler=_status_route(api), methods=("GET", "POST"))
    api.register_settings_section("presence_bot", "Second Telegram Presence bot", schema=_SETTINGS_SCHEMA)
