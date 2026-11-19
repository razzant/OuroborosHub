"""Ouroboros extension entry point for the generic Telegram transport."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sqlite3
import threading
import uuid
from typing import Any, Callable, Dict, Optional

from starlette.responses import JSONResponse

from .telegram_bot.custody import CustodyStore
from .telegram_bot.delivery import (OWNER_SEND_TOOL, delivery_kind, direct_send_fields, owner_condition_key, retained_sender,
                                     telegram_topic, tool_origin)
from .telegram_bot.events import is_user_id
from .telegram_bot.formatting import prepare_text
from .telegram_bot.host import PresenceHostClient
from .telegram_bot.runtime import TelegramTransportRuntime
from .telegram_bot.settings import load_settings, normalize_words
from .telegram_bot.tools import (
    album_items,
    discard_files,
    discard_unqueued,
    frozen_caption,
    make_moderation_tool,
    make_operation_tool,
    make_react_tool,
    make_receipt_tool,
    make_typing_tool,
    queue_file,
    upload_path,
    ALBUM_KINDS,
    MODERATION_SCHEMA,
    SEND_KINDS,
    SendRefused,
    TELEGRAM_OPERATION_SCHEMA,
    TELEGRAM_REACT_SCHEMA,
    TELEGRAM_TYPING_SCHEMA,
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
_SETTINGS_SAVE = threading.Lock()
_UNPINNED = object()


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
        counts = CustodyStore.shared(_STATE_DIR / "custody.sqlite3").status_snapshot()
    runtime = _RUNTIME.status_snapshot(counts) if _RUNTIME is not None else {}
    binding_id = str(_load_local_settings().get("binding_id") or "").strip()
    # Connect offers only the observed chats of the bot getMe confirmed for the current token.
    # null, never [], while that bot or its chats cannot be read.
    account, unavailable = _RUNTIME.confirmed_bot() if _RUNTIME is not None else ("", "Telegram bot not running")
    groups = None
    if account:
        try:
            groups = _RUNTIME.store.offered_groups(account)
        except (sqlite3.Error, OSError):
            unavailable = "Observed chats could not be read"
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
    # Custody counts, the token and local settings are file reads: they run beside the Host's HTTP loop.
    payload = await asyncio.to_thread(_status_payload)
    held = payload.get("outbox_held", 0)
    text, tone = ((f"{held} held", "warn") if held else
                  (payload["connect_unavailable"], "warn") if payload["connect"] is None else
                  (payload["runtime_state"], "muted"))
    payload.update(text=text, tone=tone, title="Telegram transport")
    return JSONResponse(payload)


def _load_local_settings() -> Dict[str, Any]:
    return load_settings(_STATE_DIR) if _STATE_DIR is not None else {}


def _same_send_address(first, requested, current_account):
    """The first queued bytes win. A repeat must still name their exact route and delivery purpose."""
    fields = ("kind", "send_kind", "chat_id", "topic_id", "reply_to_message_id",
              "direct_account_id", "direct_reply_user_id")
    if not isinstance(first, dict) or any(first.get(key) != requested.get(key) for key in fields):
        return False
    reporting = first.get("_reporting", {})
    origin = reporting.get("origin", {}) if isinstance(reporting, dict) else None
    # Older tool rows may lack reporting; the telegram-send namespace is itself the tool's.
    if not isinstance(origin, dict) or origin.get("kind", "tool") != "tool":
        return False
    account = requested.get("direct_account_id")
    saved_condition, new_condition = first.get("owner_confirmation"), requested.get("owner_confirmation")
    if saved_condition is not None or new_condition is not None:
        address = (account, requested["chat_id"], requested.get("topic_id", ""))
        saved_key = owner_condition_key(saved_condition, *address, tool=OWNER_SEND_TOOL)
        if saved_key is None or saved_key != owner_condition_key(new_condition, *address, tool=OWNER_SEND_TOOL):
            return False
    # Reporting can name a bot before any POST. Use the same retained sender evidence as the outbox.
    sender = retained_sender(first)
    return sender is not None and (not sender or sender == (account or current_account()))


def _make_telegram_send(api: Any, current_account: Callable[[], str] = lambda: ""):
    def telegram_send(
        ctx=None,
        *,
        chat_id: str = "",
        direct_account_id: Any = _UNPINNED,
        owner_confirmation: Optional[Dict[str, Any]] = None,
        text: str = "",
        kind: str = "message",
        file_path: str = "",
        caption: str = "",
        topic_id: str = "",
        reply_to_message_id: str = "",
        request_id: str = "",
        markdown: bool = True,
        items: Optional[list] = None,
    ) -> Dict[str, Any]:
        target = str(chat_id or "").strip()
        selected_kind = str(kind or "message").strip()
        if re.fullmatch(r"-?[0-9]+", target) is None or (target.isdecimal() and not is_user_id(chat_id)):
            return {
                "ok": False,
                "error": "chat_id must be an exact numeric Telegram id",
            }
        if selected_kind not in SEND_KINDS:
            return {"ok": False, "error": "kind must be " + ", ".join(SEND_KINDS)}
        if not isinstance(markdown, bool):
            return {"ok": False, "error": "markdown must be a boolean"}
        thread = telegram_topic(topic_id)
        if thread is None:
            return {"ok": False, "error": "topic_id must be one exact positive decimal Telegram topic ID"}
        payload: Dict[str, Any] = {
            "kind": selected_kind,
            "chat_id": target,
            "markdown": markdown,
            "_reporting": {"version": -1, "origin": tool_origin(ctx)},
        }
        if owner_confirmation is not None and (direct_account_id is _UNPINNED or owner_condition_key(
                owner_confirmation, direct_account_id, target, thread, tool=OWNER_SEND_TOOL) is None):
            return {"ok": False, "error": "The host's owner confirmation must match this tool and its exact bot, chat and topic."}
        if direct_account_id is not _UNPINNED:
            if not is_user_id(chat_id):
                return {"ok": False, "error": "A direct send requires one exact Telegram user chat_id."}
            observed = current_account()
            if not is_user_id(direct_account_id) or (observed and observed != direct_account_id):
                return {"ok": False, "error": "The configured Telegram bot does not confirm direct_account_id."}
            payload.update(direct_send_fields(selected_kind, direct_account_id, target, owner_confirmation))
        if thread:
            payload["topic_id"] = thread
        if reply_to_message_id not in (None, ""):
            payload["reply_to_message_id"] = str(reply_to_message_id).strip()
        state_dir = pathlib.Path(api.get_state_dir())
        try:
            if selected_kind == "message":
                if not str(text or "").strip():
                    return {"ok": False, "error": "text is required for a message"}
                payload["text"] = str(text)
                payload["_rendered_chunks"] = prepare_text(str(text), markdown=markdown)
            elif selected_kind == "album":
                payload["items"], payload["_rendered_items"], payload["_frozen"] = album_items(
                    items, str(caption or text or ""), markdown, state_dir)
            else:
                source = upload_path(selected_kind, file_path)
                payload["caption"] = str(caption or text or "")
                payload["_rendered_caption"] = frozen_caption(payload["caption"], markdown)
                # The bytes the row sends are fixed now: a copy, sent under the file's own name.
                kept = queue_file(selected_kind, source, state_dir)
                payload["file_path"], payload["_frozen"] = kept["path"], [kept]
        except SendRefused as refusal:
            return refusal.answer()
        receipt = str(request_id or uuid.uuid4().hex).strip()
        delivery_id, store = f"telegram-send:{receipt}", None
        try:
            store = CustodyStore.shared(state_dir / "custody.sqlite3")
            inserted = store.enqueue_outbox(delivery_id, payload)
        except BaseException:
            # The row may be committed already: a copy goes only when no row is known to name it.
            discard_unqueued(payload.get("_frozen") or [], state_dir, store, delivery_id)
            raise
        saved = payload
        if not inserted:
            # This request_id is already queued with the bytes it had then; this call's copy is not needed.
            discard_files(payload.get("_frozen") or [], state_dir)
            saved = store.outbox_payload(delivery_id)
            if not _same_send_address(saved, payload, current_account):
                return {"ok": False, "error": "This request_id does not match the saved send address, kind or condition."}
        return {
            "ok": True,
            "state": "queued" if inserted else "already_queued",
            "request_id": receipt,
            "chat_id": saved["chat_id"],
            "kind": delivery_kind(saved),
        }

    return telegram_send


def _save_local_settings(changes: Dict[str, Any]) -> None:
    """Merge the form's fields into the saved settings and replace the file atomically.

    One save at a time, as when each ran whole on the HTTP loop: none drops another's fields.
    """
    with _SETTINGS_SAVE:
        settings = {**_load_local_settings(), **changes}
        path = _STATE_DIR / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


async def _settings_save_route(request: Any) -> JSONResponse:
    # The settings file is read and written beside the Host's HTTP loop, never on it.
    if request.method == "GET":
        settings = await asyncio.to_thread(_load_local_settings)
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
    changes: Dict[str, Any] = {}
    if "words" in body:
        try:
            changes["words"] = normalize_words(body["words"])
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
        changes["management_group_id"] = group_id
    if "binding_id" in body:
        changes["binding_id"] = binding_id
    await asyncio.to_thread(_save_local_settings, changes)
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

    def current_account() -> str:
        try:
            account = runtime.recorded_bot()
        except (OSError, sqlite3.Error):
            return ""
        return account if is_user_id(account) else ""

    api.register_tool(
        "telegram_send",
        _make_telegram_send(api, current_account),
        description=(
            "Durably queue a proactive Telegram send for an exact numeric chat, with optional topic and reply ids: "
            "text, or a file as photo, document, video, audio, voice (OGG/Opus, MP3 or M4A), animation (GIF or "
            "silent MP4) or album (2-10 items: photos with videos, or only audio, or only documents). A bot uploads "
            "at most 50 MB per file and 10 MB per photo; a larger file or a caption over 1024 characters is refused, "
            "never cut. A file is copied when queued, and that copy is what Telegram receives; later changes to the "
            "file are not sent. Standard Markdown is rendered by default; set markdown=false for literal text. "
            "text/caption contain only display content. Pass request_id as its own argument, not XML or text inside "
            "the caption. "
            "An event.message.attachments record with status too_large (a bot downloads at most 20 MiB) or failed "
            "did not reach you; it keeps file_id, file_name, file_size and a factual note. You may ask the sender to "
            "share a download link in this conversation, open to no more people than already had the file, but never "
            "ask anyone to publish private or work files openly, never post or forward the file or its link outside "
            "this conversation's audience, and if the sender cannot share it that way, say so and leave it there. To "
            "fetch a link shared that way: Yandex.Disk: GET https://cloud-api.yandex.net/v1/disk/public/resources/"
            "download?public_key=<link> returns the download href; Google Drive over 100 MB: repeat with the confirm "
            "token from its warning page; Dropbox: use dl=1."
        ),
        schema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "direct_account_id": {"type": "string", "description": "Required sending bot when the host pins a private chat."},
                "text": {
                    "type": "string",
                    "description": "Displayed message content only; standard Markdown unless markdown=false.",
                },
                "kind": {"type": "string", "enum": list(SEND_KINDS)},
                "file_path": {"type": "string"},
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 10,
                    "description": "For kind=album: the files in order, each with its own optional caption.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {"type": "string", "enum": list(ALBUM_KINDS)},
                            "file_path": {"type": "string"},
                            "caption": {"type": "string"},
                        },
                        "required": ["kind", "file_path"],
                    },
                },
                "caption": {
                    "type": "string",
                    "description": "Displayed media caption only, up to 1024 UTF-16 units after formatting. Tool fields belong in separate arguments.",
                },
                "markdown": {
                    "type": "boolean",
                    "default": True,
                    "description": "Render standard Markdown; false preserves literal punctuation and XML.",
                },
                "topic_id": {"type": "string", "description": "Exact positive decimal Telegram topic ID; omit outside a topic."},
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
    operation = make_operation_tool(api, runtime.recorded_bot)
    api.register_tool(
        "telegram_operation",
        operation,
        description=(
            "Queue an edit of this bot's own message, or set or clear this bot's reaction on any message it can "
            "reach, through the existing outbox. A reaction can be the whole social response, and it is not "
            "speech or proof of finished work; telegram_react is the same reaction with simpler arguments. "
            "Use exact numeric chat/message IDs and a new request_id; "
            "in a forum pass the target's known topic_id, which is never invented."
        ),
        schema=TELEGRAM_OPERATION_SCHEMA,
    )
    api.register_tool(
        "telegram_react",
        make_react_tool(operation),
        description=(
            "Set or clear this bot's one emoji reaction on an exact Telegram message, for example to show you "
            "saw, agree or laughed. A reaction can be the whole social response; it is a gesture, not speech "
            "or proof of finished work. set replaces this bot's earlier reaction on that message. It is queued "
            "durably as a setMessageReaction operation with telegram_operation's checks, and its provider "
            "outcome is read with telegram_receipt(operation=\"operation\", request_id=...). In a forum pass "
            "the target's known topic_id, which is never invented."
        ),
        schema=TELEGRAM_REACT_SCHEMA,
    )
    api.register_tool(
        "telegram_typing",
        make_typing_tool(api),
        description=(
            "Ask for \u201ctyping\u2026\u201d in this turn's own Telegram chat while you are actually preparing a reply "
            "there. The call only records the request (it answers requested): it is not speech, nothing is queued or "
            "delivered, it has no receipt, and your reply is still yours to give. The transport shows it while this "
            "turn runs and this place may act, until your next message delivered to this chat, even one you queued "
            "before this call, the turn's end or about 2 minutes; call it again after an intermediate message if you "
            "keep preparing the reply. A turn that does not call it shows nothing. In a forum topic pass the event's "
            "topic_id."
        ),
        schema=TELEGRAM_TYPING_SCHEMA,
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
