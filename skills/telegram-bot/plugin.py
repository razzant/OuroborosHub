"""Ouroboros extension entry point for the generic Telegram transport."""

from __future__ import annotations

import json
import os
import pathlib
import re
import uuid
from typing import Any, Dict, Optional

from starlette.responses import JSONResponse

from .telegram_bot.custody import CustodyStore
from .telegram_bot.host import PresenceHostClient
from .telegram_bot.runtime import TelegramTransportRuntime


_RUNTIME: Optional[TelegramTransportRuntime] = None
_STATE_DIR: Optional[pathlib.Path] = None


def _widget_render() -> Dict[str, Any]:
    return {
        "kind": "declarative",
        "schema_version": 1,
        "components": [
            {
                "type": "form",
                "route": "settings/save",
                "method": "POST",
                "submit_label": "Save Presence binding",
                "fields": [
                    {
                        "name": "binding_id",
                        "label": "Presence Binding ID",
                        "type": "text",
                        "placeholder": "32-character binding id",
                        "help": (
                            "Single owner-created account-wide binding "
                            "(conversation_id=*) for this Telegram transport."
                        ),
                    }
                ],
            },
            {
                "type": "poll",
                "route": "status",
                "method": "GET",
                "interval_sec": 5,
                "components": [
                    {"type": "callout", "path": "runtime_state", "tone": "info"},
                    {
                        "type": "group",
                        "title": "Provider custody",
                        "layout": "grid",
                        "columns": 4,
                        "components": [
                            {
                                "type": "metric",
                                "label": "Inbox waiting",
                                "path": "inbox_waiting",
                            },
                            {
                                "type": "metric",
                                "label": "Inbox leased",
                                "path": "inbox_leased",
                            },
                            {
                                "type": "metric",
                                "label": "Submitted",
                                "path": "inbox_submitted",
                            },
                            {
                                "type": "metric",
                                "label": "Inbox failed",
                                "path": "inbox_failed",
                            },
                            {
                                "type": "metric",
                                "label": "Outbox waiting",
                                "path": "outbox_waiting",
                            },
                            {
                                "type": "metric",
                                "label": "Delivered",
                                "path": "outbox_delivered",
                            },
                            {
                                "type": "metric",
                                "label": "Outbox failed",
                                "path": "outbox_failed",
                            },
                            {
                                "type": "metric",
                                "label": "Telegram offset",
                                "path": "telegram_offset",
                            },
                        ],
                    },
                    {
                        "type": "kv",
                        "fields": [
                            {"label": "Bot", "path": "bot_label"},
                            {"label": "Last provider event", "path": "last_event_at"},
                            {"label": "Last delivery", "path": "last_delivery_at"},
                            {"label": "Last error", "path": "last_error"},
                        ],
                    },
                ],
            },
        ],
    }


def _status_payload() -> Dict[str, Any]:
    if _RUNTIME is not None:
        counts = _RUNTIME.store.status_snapshot()
    elif _STATE_DIR is None:
        counts: Dict[str, Any] = {}
    else:
        counts = CustodyStore(_STATE_DIR / "custody.sqlite3").status_snapshot()
    runtime = _RUNTIME.status_snapshot() if _RUNTIME is not None else {}
    binding_id = str(_load_local_settings().get("binding_id") or "").strip()
    return {
        "runtime_state": runtime.get("runtime_state", "not_started"),
        "bot_label": runtime.get("bot_label", "not connected"),
        "last_event_at": runtime.get("last_event_at", ""),
        "last_delivery_at": runtime.get("last_delivery_at", ""),
        "last_error": runtime.get("last_error", ""),
        "has_presence_binding": bool(binding_id),
        "binding_state": "configured" if binding_id else "missing",
        **counts,
    }


async def _status_route(_request: Any) -> JSONResponse:
    return JSONResponse(_status_payload())


def _load_local_settings() -> Dict[str, Any]:
    if _STATE_DIR is None:
        return {}
    path = _STATE_DIR / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _make_telegram_send(api: Any):
    def telegram_send(
        *,
        chat_id: str = "",
        text: str = "",
        kind: str = "message",
        file_path: str = "",
        caption: str = "",
        topic_id: str = "",
        reply_to_message_id: str = "",
        request_id: str = "",
    ) -> Dict[str, Any]:
        target = str(chat_id or "").strip()
        selected_kind = str(kind or "message").strip()
        if re.fullmatch(r"-?[0-9]+", target) is None:
            return {
                "ok": False,
                "error": "chat_id must be an exact numeric Telegram id",
            }
        if selected_kind not in {"message", "photo", "document"}:
            return {"ok": False, "error": "kind must be message, photo, or document"}
        payload: Dict[str, Any] = {"kind": selected_kind, "chat_id": target}
        if topic_id not in (None, ""):
            payload["topic_id"] = str(topic_id).strip()
        if reply_to_message_id not in (None, ""):
            payload["reply_to_message_id"] = str(reply_to_message_id).strip()
        if selected_kind == "message":
            if not str(text or "").strip():
                return {"ok": False, "error": "text is required for a message"}
            payload["text"] = str(text)
        else:
            path = pathlib.Path(str(file_path or "")).expanduser()
            if not path.is_file():
                return {"ok": False, "error": "file_path must name an existing file"}
            payload["file_path"] = str(path.resolve())
            payload["caption"] = str(caption or text or "")
        receipt = str(request_id or uuid.uuid4().hex).strip()
        inserted = CustodyStore(
            pathlib.Path(api.get_state_dir()) / "custody.sqlite3"
        ).enqueue_outbox(
            f"telegram-send:{receipt}",
            payload,
        )
        return {
            "ok": True,
            "state": "queued" if inserted else "already_queued",
            "request_id": receipt,
            "chat_id": target,
            "kind": selected_kind,
        }

    return telegram_send


async def _settings_save_route(request: Any) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or set(body) != {"binding_id"}:
        return JSONResponse(
            {"ok": False, "error": "Expected only binding_id"},
            status_code=400,
        )
    binding_id = str(body.get("binding_id") or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", binding_id) is None:
        return JSONResponse(
            {"ok": False, "error": "binding_id must be 32 lowercase hex characters"},
            status_code=400,
        )
    if _STATE_DIR is None:
        return JSONResponse(
            {"ok": False, "error": "Telegram transport state is unavailable"},
            status_code=503,
        )
    settings = _load_local_settings()
    settings["binding_id"] = binding_id
    path = _STATE_DIR / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return JSONResponse({"ok": True, "binding_id": binding_id})


def register(api: Any) -> None:
    """Register the bounded provider runtime and its read-only status Widget."""
    global _RUNTIME, _STATE_DIR
    _STATE_DIR = pathlib.Path(api.get_state_dir())
    _STATE_DIR.mkdir(parents=True, exist_ok=True)

    def token_provider() -> str:
        try:
            values = api.get_settings(["TELEGRAM_PUBLIC_BOT_TOKEN"]) or {}
        except Exception:
            return ""
        return str(values.get("TELEGRAM_PUBLIC_BOT_TOKEN") or "").strip()

    def host_token_provider() -> str:
        return str(api.get_skill_token().use_in_request() or "").strip()

    try:
        host_port = int(os.environ.get("OUROBOROS_HOST_SERVICE_PORT", "8767"))
    except ValueError:
        host_port = 8767
    submitter = PresenceHostClient(
        state_dir=_STATE_DIR,
        token_provider=host_token_provider,
        host_base=f"http://127.0.0.1:{host_port}",
    )
    _RUNTIME = TelegramTransportRuntime(
        state_dir=_STATE_DIR,
        token_provider=token_provider,
        logger=api,
        submitter=submitter,
    )

    async def supervised_runtime() -> None:
        assert _RUNTIME is not None
        await _RUNTIME.run()

    def unload() -> None:
        if _RUNTIME is not None:
            _RUNTIME.stop()

    api.register_supervised_task(
        "telegram_presence_transport",
        supervised_runtime,
        restart_policy="on_failure",
        max_restarts=10,
    )
    api.register_tool(
        "telegram_send",
        _make_telegram_send(api),
        description=(
            "Durably queue a proactive Telegram text, photo, or document for an exact numeric "
            "chat, with optional topic and reply ids."
        ),
        schema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": ["message", "photo", "document"]},
                "file_path": {"type": "string"},
                "caption": {"type": "string"},
                "topic_id": {"type": "string"},
                "reply_to_message_id": {"type": "string"},
                "request_id": {"type": "string"},
            },
            "required": ["chat_id", "kind"],
        },
    )
    api.register_route("status", _status_route, methods=("GET",))
    api.register_route("settings/save", _settings_save_route, methods=("POST",))
    api.register_ui_tab(
        "transport",
        "Telegram Transport",
        icon="message",
        render=_widget_render(),
    )
    api.on_unload(unload)
    api.log("info", "telegram-bot: durable provider transport registered")


__all__ = ["register"]
