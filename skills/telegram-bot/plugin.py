"""Ouroboros extension entry point for the generic Telegram transport."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from starlette.responses import JSONResponse

from .telegram_bot.custody import CustodyStore
from .telegram_bot.delivery import tool_origin
from .telegram_bot.formatting import prepare_text
from .telegram_bot.host import PresenceHostClient
from .telegram_bot.owner_feed import (
    OwnerFeedClient,
    SOURCE_OWNER_BOT,
    SOURCE_STANDALONE,
    owner_state_dir,
)
from .telegram_bot.runtime import BOT_NOT_RUNNING, BOT_UNCONFIRMED, TelegramTransportRuntime
from .telegram_bot.settings import load_settings, normalize_words
from .telegram_bot.status_summary import card_summary, link_state, summarize as summarize_status, summarize_owner
from .telegram_bot.tools import (
    ACCOUNT_ID_SCHEMA,
    album_items,
    discard_files,
    discard_unqueued,
    enforce_source_chat_policy,
    frozen_caption,
    make_moderation_tool,
    make_operation_tool,
    make_react_tool,
    make_receipt_tool,
    make_typing_tool,
    queue_file,
    resolve_tool_source,
    turn_place_or_bind,
    turn_target,
    upload_path,
    ALBUM_KINDS,
    MODERATION_SCHEMA,
    SEND_KINDS,
    SendRefused,
    TELEGRAM_OPERATION_SCHEMA,
    TELEGRAM_REACT_SCHEMA,
    TELEGRAM_TYPING_SCHEMA,
)


_RUNTIME: Optional[TelegramTransportRuntime] = None
_OWNER_RUNTIME: Optional[TelegramTransportRuntime] = None
_STATE_DIR: Optional[pathlib.Path] = None
_SETTINGS_SAVE = threading.Lock()


# The Widgets card is a module (widget.js): it shows when the status was read, keeps refreshing without a tick
# limit, colours by the current facts, explains the bot's reach and saves the management group ID on one surface.
_WIDGET_RENDER: Dict[str, Any] = {
    "kind": "module", "entry": "widget.js", "appearance": "host", "start": "auto", "span": 1,
}


def _status_payload() -> Dict[str, Any]:
    observed = time.time()
    counts: Dict[str, Any] = {}
    card: Dict[str, Any] = {}
    if _RUNTIME is not None or _STATE_DIR is not None:
        store = _RUNTIME.store if _RUNTIME is not None else CustodyStore.shared(_STATE_DIR / "custody.sqlite3")
        # Totals and the card's facts from one read transaction: never a send counted twice or not at all.
        counts, card = store.read_status(observed)
    runtime = _RUNTIME.status_snapshot(counts) if _RUNTIME is not None else {}
    binding_id = str(_load_local_settings().get("binding_id") or "").strip()
    # Connect offers only the observed chats of the bot getMe confirmed for the current token.
    # null, never [], while that bot or its chats cannot be read.
    # The account comes from the same runtime read as the bot's name and poll result (status_snapshot), so the
    # chats offered always belong to the bot this answer names.
    account, unavailable = ((runtime.get("confirmed_account", ""), runtime.get("confirmed_reason", ""))
                            if _RUNTIME is not None else ("", BOT_NOT_RUNNING))
    groups = None
    if account:
        try:
            groups = _RUNTIME.store.offered_groups(account)
        except (sqlite3.Error, OSError):
            unavailable = "Observed chats could not be read"
    owner = _owner_bot_facts(observed)
    return {
        "runtime_state": runtime.get("runtime_state", "not_started"),
        "bot_label": runtime.get("bot_label", "not connected"),
        "ready_wait": runtime.get("ready_wait", ""),
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
        # The bot named here is not the one getMe confirmed for the token set now (yet): a flag, so readers never
        # match the reason's words.
        "bot_unconfirmed": unavailable == BOT_UNCONFIRMED,
        **counts,
        # Additive card facts (each tested in tests/test_status_card.py). observed_at: when this body was read.
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(observed)),
        "last_error_at": runtime.get("last_error_at", ""),
        "last_error_source": runtime.get("last_error_source", ""),
        "provider_ok_at": runtime.get("provider_ok_at", ""),
        # The last completed getUpdates attempt for the token configured now ("" none yet): green needs "ok".
        "poll_result": runtime.get("poll_result", ""),
        "poll_at": runtime.get("poll_at", ""),
        "poll_error": runtime.get("poll_error", ""),
        "poll_failure": runtime.get("poll_failure", ""),
        # Where that failed attempt stopped: "identity" (getMe, recording its bot), "updates" or "record".
        "poll_stage": runtime.get("poll_stage", ""),
        "host_checked": bool(runtime.get("host_checked", False)),
        "host_checked_at": runtime.get("host_checked_at", ""),
        # Why the last capability check made no request ("" when it made one): "skill_token_unavailable".
        "host_unasked": runtime.get("host_unasked", ""),
        "places_version": runtime.get("places_version", 0),
        # null when the custody store was not read: unknown, never zero.
        "recent_problems": card.get("recent_problems"),
        "held_reasons": card.get("held_reasons") or [],
        # Waiting updates whose last attempt failed, by where it stopped; null when the store was not read.
        "handoff_retrying": card.get("handoff_retrying"),
        # Additive: the owner's own bot in groups while this Ouroboros offers it, from its own store and runtime;
        # never mixed into the standalone bot's keys above.
        **({"owner_bot": owner} if owner is not None else {}),
    }


def _owner_bot_facts(observed: float) -> Optional[Dict[str, Any]]:
    """The owner's own bot in groups for the Widgets card, under the names the standalone bot's facts use, with its
    summary (status_summary.summarize_owner); None while this Ouroboros does not offer it (telegram_group_feed).

    ``store``: ``none`` before anything was ever received or queued through it (no store file: its counts are zero
    and none is created by this read), ``read``, or ``unreadable`` (counts unknown, never zero)."""
    owner, standalone = _OWNER_RUNTIME, _RUNTIME
    if owner is None or standalone is None or getattr(standalone.submitter, "telegram_group_feed", 0) != 1:
        return None
    counts: Dict[str, Any] = {}
    card: Dict[str, Any] = {}
    store = "none"
    if owner.store_exists:
        try:
            counts, card = owner.store.read_status(observed)
            store = "read"
        except (sqlite3.Error, OSError):
            store = "unreadable"
    facts = owner.status_snapshot(counts)
    payload = {**facts, **counts, "store": store,
               "recent_problems": card.get("recent_problems"), "held_reasons": card.get("held_reasons") or [],
               "handoff_retrying": card.get("handoff_retrying")}
    return {**payload, **summarize_owner(payload)}


def _query_account_id(request: Any) -> str:
    if request is None:
        return ""
    params = getattr(request, "query_params", None)
    if params is None:
        return ""
    try:
        return str(params.get("account_id") or "").strip()
    except Exception:
        return ""


async def _status_route(_request: Any) -> JSONResponse:
    # Unchanged legacy standalone B status route (never mixes in owner-bot A).
    payload = await asyncio.to_thread(_status_payload)
    summary = summarize_status(payload)
    payload.update(
        text=summary["text"],
        tone=summary["tone"],
        attention=summary["attention"],
        link=link_state(payload),
        title="Telegram transport",
    )
    if "owner_bot" in payload:
        # The card's own line over both bots; text/tone above stay the standalone bot's (Places reads them).
        payload["card"] = card_summary(payload, payload["owner_bot"])
    return JSONResponse(payload)


async def _owner_bot_status_route(request: Any) -> JSONResponse:
    """Dedicated owner-bot status route projected for ``owner_bot`` places by new core only."""
    owner = _OWNER_RUNTIME
    payload = (await owner.owner_status_snapshot(_query_account_id(request)) if owner is not None else
               {"v": 1, "enabled": None, "reason": "not_started", "connect": None})
    return JSONResponse(payload)


def _load_local_settings() -> Dict[str, Any]:
    return load_settings(_STATE_DIR) if _STATE_DIR is not None else {}


def _make_telegram_send(
    api: Any,
    *,
    standalone_account: Callable[[], str] = lambda: "",
    owner_account: Callable[[], str] = lambda: "",
    standalone_configured: Optional[Callable[[], bool]] = None,
    owner_place_binder: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
):
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
        items: Optional[list] = None,
        account_id: str = "",
    ) -> Dict[str, Any]:
        target = str(chat_id or "").strip()
        selected_kind = str(kind or "message").strip()
        if re.fullmatch(r"-?[0-9]+", target) is None:
            return {
                "ok": False,
                "error": "chat_id must be an exact numeric Telegram id",
            }
        if selected_kind not in SEND_KINDS:
            return {"ok": False, "error": "kind must be " + ", ".join(SEND_KINDS)}
        if not isinstance(markdown, bool):
            return {"ok": False, "error": "markdown must be a boolean"}
        clean_topic = str(topic_id).strip() if topic_id not in (None, "") else ""
        try:
            root = pathlib.Path(api.get_state_dir())
            # A reply in this turn's own chat and topic keeps the bot and place connection its event came through; a
            # reply to the original message still refers to it when given another topic. Other sends are bound now.
            turn = turn_target(root, ctx, target, topic_id=clean_topic)
            if turn is None and reply_to_message_id not in (None, ""):
                turn = turn_target(root, ctx, target, message_id=reply_to_message_id)
            source = resolve_tool_source(
                root,
                account_id=account_id,
                ctx=ctx,
                standalone_account=standalone_account,
                owner_account=owner_account,
                standalone_configured=standalone_configured,
                turn=turn,
            )
            enforce_source_chat_policy(source, target)
            place_binding = turn_place_or_bind(
                source,
                turn,
                target,
                topic_id=clean_topic,
                owner_place_binder=owner_place_binder,
            )
        except SendRefused as refusal:
            return refusal.answer()
        reporting: Dict[str, Any] = {"version": -1, "origin": tool_origin(ctx)}
        if source.pin_account:
            reporting["account_id"] = source.account_id
        payload: Dict[str, Any] = {
            "kind": selected_kind,
            "chat_id": target,
            "markdown": markdown,
            "_reporting": reporting,
        }
        if source.pin_account:
            payload["account_id"] = source.account_id
        if place_binding is not None:
            payload["_place_binding"] = place_binding
        if clean_topic:
            payload["topic_id"] = clean_topic
        if reply_to_message_id not in (None, ""):
            payload["reply_to_message_id"] = str(reply_to_message_id).strip()
        state_dir = source.state_dir
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
                source_file = upload_path(selected_kind, file_path)
                payload["caption"] = str(caption or text or "")
                payload["_rendered_caption"] = frozen_caption(payload["caption"], markdown)
                # The bytes the row sends are fixed now: a copy, sent under the file's own name.
                kept = queue_file(selected_kind, source_file, state_dir)
                payload["file_path"], payload["_frozen"] = kept["path"], [kept]
        except SendRefused as refusal:
            return refusal.answer()
        receipt = str(request_id or uuid.uuid4().hex).strip()
        delivery_id, store = f"telegram-send:{receipt}", None
        try:
            store = CustodyStore.shared(source.store_path, owner_bot=source.source == SOURCE_OWNER_BOT)
            inserted = store.enqueue_outbox(delivery_id, payload)
        except BaseException:
            # The row may be committed already: a copy goes only when no row is known to name it.
            discard_unqueued(payload.get("_frozen") or [], state_dir, store, delivery_id)
            raise
        if not inserted:
            # This request_id is already queued with the bytes it had then; this call's copy is not needed.
            discard_files(payload.get("_frozen") or [], state_dir)
        return {
            "ok": True,
            "state": "queued" if inserted else "already_queued",
            "request_id": receipt,
            "chat_id": target,
            "kind": selected_kind,
            **({"account_id": source.account_id} if source.expose_account else {}),
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
    global _RUNTIME, _OWNER_RUNTIME, _STATE_DIR
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
    host_base = f"http://127.0.0.1:{host_port}"

    submitter = PresenceHostClient(
        state_dir=_STATE_DIR,
        token_provider=host_token_provider,
        host_base=host_base,
    )
    owner_submitter = PresenceHostClient(
        state_dir=_STATE_DIR,
        token_provider=host_token_provider,
        host_base=host_base,
    )
    owner_feed = OwnerFeedClient(
        host_base=host_base,
        token_provider=host_token_provider,
        transport_provider=lambda: getattr(_RUNTIME.submitter, "transport", None) if _RUNTIME is not None else None,
    )
    runtime = _RUNTIME = TelegramTransportRuntime(
        state_dir=_STATE_DIR,
        token_provider=token_provider,
        logger=api,
        submitter=submitter,
        source_mode=SOURCE_STANDALONE,
        owner_feed=owner_feed,
    )
    owner_runtime = _OWNER_RUNTIME = TelegramTransportRuntime(
        state_dir=owner_state_dir(_STATE_DIR),
        settings_dir=_STATE_DIR,
        token_provider=lambda: "",
        logger=api,
        submitter=owner_submitter,
        source_mode=SOURCE_OWNER_BOT,
        owner_feed=owner_feed,
        store_lazy=True,
        capability_source=lambda: _RUNTIME.submitter if _RUNTIME is not None else None,
    )

    async def _run_owner_safely() -> None:
        assert _OWNER_RUNTIME is not None
        try:
            await _OWNER_RUNTIME.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            api.log("warning", f"telegram-bot: owner-bot group feed exited: {type(exc).__name__}: {exc}")

    async def supervised_runtime() -> None:
        assert _RUNTIME is not None
        if _OWNER_RUNTIME is None:
            await _RUNTIME.run()
            return
        owner_task = asyncio.create_task(_run_owner_safely(), name="telegram-owner-feed-supervisor")
        try:
            await _RUNTIME.run()
        finally:
            _OWNER_RUNTIME.stop()
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)

    def unload() -> None:
        if _OWNER_RUNTIME is not None:
            _OWNER_RUNTIME.stop()
        if _RUNTIME is not None:
            _RUNTIME.stop()

    source_kwargs = {
        "standalone_account": runtime.recorded_bot,
        "owner_account": owner_runtime.recorded_bot,
        "standalone_configured": lambda: bool(token_provider()),
        "owner_place_binder": owner_submitter.bind_owner_place_sync,
    }

    api.register_supervised_task(
        "telegram_presence",
        supervised_runtime,
        restart_policy="on_failure",
        max_restarts=10,
    )
    api.register_tool(
        "telegram_send",
        _make_telegram_send(api, **source_kwargs),
        description=(
            "Durably queue a proactive Telegram send for an exact numeric chat, with optional topic and reply ids: "
            "text, or a file as photo, document, video, audio, voice (OGG/Opus, MP3 or M4A), animation (GIF or "
            "silent MP4) or album (2-10 items: photos with videos, or only audio, or only documents). Pass explicit "
            "account_id when targeting the connected owner-bot group feed. A bot uploads "
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
                "account_id": ACCOUNT_ID_SCHEMA,
                "chat_id": {"type": "string"},
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
        make_moderation_tool(api, **source_kwargs),
        description="Queue a Telegram delete/restrict/ban/unban operation with exact provider IDs. Reuse request_id when retrying; queued is not delivered.",
        schema=MODERATION_SCHEMA,
    )
    operation = make_operation_tool(
        api,
        runtime.recorded_bot,
        owner_account=owner_runtime.recorded_bot,
        standalone_configured=source_kwargs["standalone_configured"],
        owner_place_binder=owner_submitter.bind_owner_place_sync,
    )
    api.register_tool(
        "telegram_operation",
        operation,
        description=(
            "Queue an edit of this bot's own message, or set or clear this bot's reaction on any message it can "
            "reach, through the existing outbox. A reaction can be the whole social response, and it is not "
            "speech or proof of finished work; telegram_react is the same reaction with simpler arguments. "
            "Use exact numeric chat/message IDs and a new request_id; pass explicit account_id when targeting "
            "the owner-bot group feed; in a forum pass the target's known topic_id, which is never invented."
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
        make_typing_tool(api, **source_kwargs),
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
        description=(
            "Read durable delivery state, provider receipt or error for a Telegram send/moderation request. For a "
            "file send, `transfer` is this process's newest saved observation of the upload — its phase "
            "(connecting, uploading, awaiting_response), the file bytes written of the total (the last saved lower "
            "bound of completed local writes; an interrupted socket write may have emitted part of an uncounted "
            "piece). Only a saved end observation has `ended_at` and `answer`; an uncertain row can keep the last "
            "phase and count without them. Those bytes are never proof Telegram has them; the row's state and "
            "provider receipt decide delivery. `queued_at` says when the row was queued; re-read the same "
            "request_id to watch an upload move. A retained owner-bot turn reads its own bot without an account_id; "
            "other calls prefer the separate bot. Pass account_id to read a receipt that names a particular bot."
        ),
        schema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "operation": {"type": "string", "enum": ["send", "moderate", "operation"]},
                "account_id": ACCOUNT_ID_SCHEMA,
            },
            "required": ["request_id"],
        },
    )
    api.register_route("status", _status_route, methods=("GET",))
    api.register_route("settings/save", _settings_save_route, methods=("GET", "POST"))
    api.register_route("owner_bot_status", _owner_bot_status_route, methods=("GET",))
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
        render=dict(_WIDGET_RENDER),
    )
    api.on_unload(unload)
    api.log("info", "telegram-bot: durable provider transport registered")


__all__ = ["register"]
