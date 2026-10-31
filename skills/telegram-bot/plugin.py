"""Ouroboros extension entry point for the generic Telegram transport."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import uuid
from typing import Any, Dict, Optional

from starlette.responses import JSONResponse

from .telegram_bot.custody import CustodyStore
from .telegram_bot.delivery import tool_origin
from .telegram_bot.formatting import prepare_text, prepare_caption
from .telegram_bot.host import PresenceHostClient
from .telegram_bot.runtime import TelegramTransportRuntime
from .telegram_bot.settings import load_settings, normalize_words
from .telegram_bot.tools import (
    make_moderation_tool,
    make_operation_tool,
    make_receipt_tool,
    MODERATION_SCHEMA,
    TELEGRAM_OPERATION_SCHEMA,
)


_BOT_DIFFERENCES = (
    "**Telegram bot.** Telegram labels this account as a bot. People reach it in a private "
    "chat they opened themselves and in groups or channels it was added to; group coverage depends "
    "on privacy mode and admin rights. A bot cannot start a private chat with someone who never "
    "opened one. The Bot API cannot read earlier chat history: only messages that arrive while it "
    "runs are received. A dedicated ordinary account is the separate Telegram participant connector."
)

_RUNTIME: Optional[TelegramTransportRuntime] = None
_STATE_DIR: Optional[pathlib.Path] = None


def _widget_render() -> Dict[str, Any]:
    return {
        "kind": "declarative",
        "schema_version": 1,
        "components": [
            {"type": "markdown", "text": _BOT_DIFFERENCES},
            {
                "type": "form",
                "route": "settings/save",
                "method": "POST",
                "submit_label": "Save transport settings",
                "fields": [
                    {
                        "name": "management_group_id",
                        "label": "Management group ID",
                        "type": "text",
                        "placeholder": "Optional numeric Telegram group ID",
                        "help": "Marks this room in event facts. Work instructions stay "
                        "in the place notes; no owner permissions are "
                        "granted.",
                    },
                ],
            },
            {
                "type": "poll",
                "route": "status",
                "method": "GET",
                "target": "status",
                "interval_ms": 5000,
                "auto_start": True,
                "max_ticks": 100,
                "label": "Refresh transport status",
            },
            {
                "type": "callout",
                "path": "runtime_state",
                "tone": "info",
                "target": "status",
            },
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
                        "target": "status",
                    },
                    {
                        "type": "metric",
                        "label": "Inbox leased",
                        "path": "inbox_leased",
                        "target": "status",
                    },
                    {
                        "type": "metric",
                        "label": "Submitted",
                        "path": "inbox_submitted",
                        "target": "status",
                    },
                    {
                        "type": "metric",
                        "label": "Inbox failed",
                        "path": "inbox_failed",
                        "target": "status",
                    },
                    {
                        "type": "metric",
                        "label": "Outbox waiting",
                        "path": "outbox_waiting",
                        "target": "status",
                    },
                        {"type": "metric", "label": "Outbox held", "path": "outbox_held", "target": "status"},
                    {
                        "type": "metric",
                        "label": "Delivered",
                        "path": "outbox_delivered",
                        "target": "status",
                    },
                    {
                        "type": "metric",
                        "label": "Outbox failed",
                        "path": "outbox_failed",
                        "target": "status",
                    },
                    {
                        "type": "metric",
                        "label": "Outbox uncertain",
                        "path": "outbox_uncertain",
                        "target": "status",
                    },
                    {
                        "type": "metric",
                        "label": "Telegram offset",
                        "path": "telegram_offset",
                        "target": "status",
                    },
                ],
                "target": "status",
            },
            {
                "type": "kv",
                "fields": [
                    {"label": "Bot", "path": "bot_label"},
                    {"label": "Last provider event", "path": "last_event_at"},
                    {"label": "Last delivery", "path": "last_delivery_at"},
                    {"label": "Last error", "path": "last_error"},
                    {"label": "Reactions in Host context", "path": "reaction_context"},
                ],
                "target": "status",
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
    runtime = _RUNTIME.status_snapshot(counts) if _RUNTIME is not None else {}
    binding_id = str(_load_local_settings().get("binding_id") or "").strip()
    # Connect offers only the groups of the bot getMe confirmed for the current token.
    # null, never [], while that bot or its groups cannot be read.
    account, unavailable = _RUNTIME.confirmed_bot() if _RUNTIME is not None else ("", "Telegram bot not running")
    groups = None
    if account:
        try:
            groups = _RUNTIME.store.offered_groups(account)
        except (sqlite3.Error, OSError):
            unavailable = "Observed groups could not be read"
    return {
        "runtime_state": runtime.get("runtime_state", "not_started"),
        "bot_label": runtime.get("bot_label", "not connected"),
        **({"identity": runtime["identity"]} if "identity" in runtime else {}),
        "last_event_at": runtime.get("last_event_at", ""),
        "last_delivery_at": runtime.get("last_delivery_at", ""),
        "last_error": runtime.get("last_error", ""),
        "delivery_reporting": runtime.get("delivery_reporting", "unknown"),
        "presence_continuation_version": runtime.get("presence_continuation_version", 0),
        "reaction_context": runtime.get("reaction_context", "update_required"),
        "reaction_history": runtime.get("reaction_history", "update_required"),
        "has_presence_binding": bool(binding_id),
        "management_group_id": str(
            _load_local_settings().get("management_group_id") or ""
        ),
        "binding_state": "configured" if binding_id else "missing",
        "connect": groups,
        **({"connect_unavailable": unavailable} if groups is None else {}),
        **counts,
    }


async def _status_route(_request: Any) -> JSONResponse:
    payload = _status_payload()
    held = payload.get("outbox_held", 0)
    text, tone = ((f"{held} held", "warn") if held else
                  (payload["connect_unavailable"], "warn") if payload["connect"] is None else
                  (payload["runtime_state"], "muted"))
    payload.update(text=text, tone=tone, title="Telegram transport")
    return JSONResponse(payload)


def _load_local_settings() -> Dict[str, Any]:
    return load_settings(_STATE_DIR) if _STATE_DIR is not None else {}


def _make_telegram_send(api: Any):
    def telegram_send(
        ctx=None,
        *,
        chat_id: str = "",
        text: str = "",
        kind: str = "message",
        file_path: str = "",
        caption: str = "",
        topic_id: str = "",
        reply_to_message_id: str = "",
        request_id: str = "",
        markdown: bool = True,
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
        if not isinstance(markdown, bool):
            return {"ok": False, "error": "markdown must be a boolean"}
        payload: Dict[str, Any] = {
            "kind": selected_kind,
            "chat_id": target,
            "markdown": markdown,
            "_reporting": {"version": -1, "origin": tool_origin(ctx)},
        }
        if topic_id not in (None, ""):
            payload["topic_id"] = str(topic_id).strip()
        if reply_to_message_id not in (None, ""):
            payload["reply_to_message_id"] = str(reply_to_message_id).strip()
        if selected_kind == "message":
            if not str(text or "").strip():
                return {"ok": False, "error": "text is required for a message"}
            payload["text"] = str(text)
            payload["_rendered_chunks"] = prepare_text(str(text), markdown=markdown)
        else:
            path = pathlib.Path(str(file_path or "")).expanduser()
            if not path.is_file():
                return {"ok": False, "error": "file_path must name an existing file"}
            payload["file_path"] = str(path.resolve())
            payload["caption"] = str(caption or text or "")
            try:
                payload["_rendered_caption"] = prepare_caption(
                    payload["caption"], markdown=markdown
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
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
    if request.method == "GET":
        settings = _load_local_settings()
        return JSONResponse({
            "management_group_id": settings.get("management_group_id") or "",
            "words": "\n".join(settings.get("words", [])),
        })
    try:
        body = await request.json()
    except Exception:
        body = None
    if (
        not isinstance(body, dict)
        or set(body) - {"binding_id", "management_group_id", "words"}
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "Expected transport settings",
            },
            status_code=400,
        )
    binding_id = str(body.get("binding_id") or "").strip()
    if "binding_id" in body and re.fullmatch(r"[0-9a-f]{32}", binding_id) is None:
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
    if "words" in body:
        try:
            settings["words"] = normalize_words(body["words"])
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if "management_group_id" in body:
        group_id = str(body.get("management_group_id") or "").strip()
        if group_id and re.fullmatch(r"-[0-9]+", group_id) is None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "management_group_id must be a numeric Telegram group ID",
                },
                status_code=400,
            )
        settings["management_group_id"] = group_id
    if "binding_id" in body:
        settings["binding_id"] = binding_id
    path = _STATE_DIR / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return JSONResponse({"ok": True, **({"binding_id": binding_id} if "binding_id" in body else {})})


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
    runtime = _RUNTIME = TelegramTransportRuntime(
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
        "telegram_presence",
        supervised_runtime,
        restart_policy="on_failure",
        max_restarts=10,
    )
    api.register_tool(
        "telegram_send",
        _make_telegram_send(api),
        description=(
            "Durably queue a proactive Telegram text, photo, or document for an exact numeric "
            "chat, with optional topic and reply ids. Standard Markdown is rendered by default; "
            "set markdown=false for literal text. text/caption contain only display content. "
            "Pass request_id as its own argument, not XML or text inside the caption."
        ),
        schema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "text": {
                    "type": "string",
                    "description": "Displayed message content only; standard Markdown unless markdown=false.",
                },
                "kind": {"type": "string", "enum": ["message", "photo", "document"]},
                "file_path": {"type": "string"},
                "caption": {
                    "type": "string",
                    "description": "Displayed media caption only, up to 1024 UTF-16 units after formatting. Tool fields belong in separate arguments.",
                },
                "markdown": {
                    "type": "boolean",
                    "default": True,
                    "description": "Render standard Markdown; false preserves literal punctuation and XML.",
                },
                "topic_id": {"type": "string"},
                "reply_to_message_id": {"type": "string"},
                "request_id": {
                    "type": "string",
                    "description": "Separate stable deduplication key; reuse the same value for the same logical send.",
                },
            },
            "required": ["chat_id", "kind"],
        },
    )
    api.register_tool(
        "telegram_moderate",
        make_moderation_tool(api),
        description="Queue a Telegram delete/restrict/ban/unban operation with exact provider IDs. Reuse request_id when retrying; queued is not delivered.",
        schema=MODERATION_SCHEMA,
    )
    api.register_tool(
        "telegram_operation",
        make_operation_tool(api, runtime.recorded_bot),
        description=(
            "Queue an edit of this bot's own message, or set or clear this bot's reaction on any message it can "
            "reach, through the existing outbox. A reaction can be the whole social response, and it is not "
            "speech or proof of finished work. Use exact numeric chat/message IDs and a new request_id; "
            "in a forum pass the target's known topic_id, which is never invented."
        ),
        schema=TELEGRAM_OPERATION_SCHEMA,
    )
    api.register_tool(
        "telegram_receipt",
        make_receipt_tool(api),
        description="Read durable delivery state, provider receipt or error for a Telegram send/moderation request.",
        schema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "operation": {"type": "string", "enum": ["send", "moderate", "operation"]},
            },
            "required": ["request_id"],
        },
    )
    api.register_route("status", _status_route, methods=("GET",))
    api.register_route("settings/save", _settings_save_route, methods=("GET", "POST"))
    api.register_settings_section("transport", title="Telegram", schema={"components": [
        {"type": "form", "route": "settings/save", "method": "POST",
         "submit_label": "Save transport settings", "fields": [
             {"name": "words", "label": "Words to notice (one per line)", "type": "textarea"},
         ]},
    ]})
    api.register_ui_tab(
        "transport",
        "Telegram Transport",
        icon="message",
        render=_widget_render(),
    )
    api.on_unload(unload)
    api.log("info", "telegram-bot: durable provider transport registered")


__all__ = ["register"]
