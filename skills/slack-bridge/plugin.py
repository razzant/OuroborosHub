from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import time
import uuid
from typing import Any

from starlette.responses import JSONResponse

from .lib.host_adapter import HostContractError, normalize_binding_id
from .lib.local_settings import LocalSettingsError, load_local_settings, settings_path
from .lib.slack_api import (
    SlackApiError,
    SlackClient,
    SlackConfigurationError,
    TEXT_FORMATS,
    chunk_message,
    normalize_text_format,
)
from .lib.store import BridgeStore
from .lib.read_tools import register_read_tools
from .lib.tool_results import register_json_tool


def _state_dir(api: Any) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir())


def _settings_path(api: Any) -> pathlib.Path:
    return settings_path(_state_dir(api))


def _load_local_settings(api: Any) -> dict[str, Any]:
    """Absent settings are ``{}``; unreadable settings raise LocalSettingsError."""

    return load_local_settings(_state_dir(api))


def _save_local_settings(api: Any, value: dict[str, Any]) -> None:
    path = _settings_path(api)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{time.time_ns()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_target(target: str) -> str:
    clean = str(target or "").strip()
    if not clean:
        raise SlackConfigurationError("channel_or_user is required")
    if clean.startswith(("#", "@")):
        raise SlackConfigurationError(
            "Use a stable Slack channel ID or member ID instead of a display name"
        )
    return clean


def _make_slack_send(api: Any):
    def slack_send(
        ctx: Any = None,
        *,
        channel_or_user: str = "",
        text: str = "",
        thread_ts: str = "",
        request_id: str = "",
        text_format: str = "markdown",
    ) -> dict[str, Any]:
        target = _validate_target(channel_or_user)
        text_format = normalize_text_format(text_format)
        chunks = chunk_message(text)
        if not chunks:
            return {"ok": False, "error": "text is required"}
        receipt = str(request_id or uuid.uuid4().hex)
        origin = {"kind": "tool"}
        task_id = str(getattr(ctx, "task_id", "") or "")
        if task_id:
            origin["task_id"] = task_id
        metadata = getattr(ctx, "task_metadata", {})
        presence = metadata.get("presence", {}) if isinstance(metadata, dict) else {}
        event = presence.get("event", {}) if isinstance(presence, dict) else {}
        source_event_id = event.get("source_event_id") if isinstance(event, dict) else None
        if source_event_id:
            origin["source_event_id"] = str(source_event_id)
        store = BridgeStore(_state_dir(api))
        count = store.enqueue_outbox(
            request_id=receipt,
            target=target,
            thread_ts=str(thread_ts or "").strip(),
            chunks=chunks,
            text_format=text_format,
            origin=origin,
            delivery_reporting_version=store.runtime_value("presence_delivery_version", 0),
        )
        return {
            "ok": True,
            "state": "queued",
            "request_id": receipt,
            "chunks_queued": count,
            "target": target,
            "thread_ts": str(thread_ts or "").strip(),
        }

    return slack_send


def _make_status(api: Any):
    async def status(_request: Any = None) -> JSONResponse:
        store = BridgeStore(_state_dir(api))
        payload = store.status()
        protected = api.get_settings(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
        settings_error = ""
        try:
            binding_id = str(_load_local_settings(api).get("binding_id") or "").strip()
        except LocalSettingsError as exc:
            # A file nobody can read is not a missing binding. Say so, so the
            # widget cannot be read as "the owner never configured one".
            valid_binding_id, binding_state = "", "unreadable"
            settings_error = str(exc)
        else:
            try:
                valid_binding_id = normalize_binding_id(binding_id) if binding_id else ""
                binding_state = "configured" if valid_binding_id else "missing"
            except HostContractError:
                valid_binding_id = ""
                binding_state = "invalid"
        payload.update(
            {
                "has_bot_token": bool(protected.get("SLACK_BOT_TOKEN")),
                "has_app_token": bool(protected.get("SLACK_APP_TOKEN")),
                "has_presence_binding": bool(valid_binding_id),
                "binding_state": binding_state,
                "local_settings_error": settings_error,
            }
        )
        return JSONResponse(payload)

    return status


def _safe_name(value: str, fallback: str = "file") -> str:
    leaf = pathlib.PurePath(str(value or "")).name
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", leaf).strip(" .")
    return clean[:180] or fallback


def _tool_origin(ctx: Any) -> dict[str, str]:
    origin = {"kind": "tool"}
    if getattr(ctx, "task_id", None):
        origin["task_id"] = str(ctx.task_id)
    metadata = getattr(ctx, "task_metadata", {})
    event = metadata.get("presence", {}).get("event", {}) if isinstance(metadata, dict) else {}
    if isinstance(event, dict) and event.get("source_event_id"):
        origin["source_event_id"] = str(event["source_event_id"])
    return origin


def _enqueue_mutation(api: Any, operation: str, payload: dict[str, Any], request_id: str = "", ctx: Any = None) -> dict[str, Any]:
    request_id = str(request_id or uuid.uuid4().hex)
    store = BridgeStore(_state_dir(api))
    inserted = store.enqueue_mutation(
        request_id=request_id, operation=operation, payload=payload,
        origin=_tool_origin(ctx), delivery_reporting_version=store.runtime_value("presence_delivery_version", 0),
    )
    return {"ok": True, "state": "queued", "request_id": request_id,
            "operation": operation, "deduplicated": not inserted,
            "uncertainty": "Provider result is recorded after the companion runs; a lost response is reported as uncertain."}


def _make_file_upload(api: Any):
    def upload(
        ctx: Any = None,
        *,
        file_path: str = "",
        channel_id: str = "",
        thread_ts: str = "",
        title: str = "",
        initial_comment: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        source = pathlib.Path(str(file_path or "")).expanduser()
        if not source.is_file():
            return {"ok": False, "error": "file_path must point to a regular file"}
        if source.stat().st_size <= 0:
            return {"ok": False, "error": "file must not be empty"}
        if source.stat().st_size > 50 * 1024 * 1024:
            return {"ok": False, "error": "file exceeds 50 MiB staging limit"}
        receipt = str(request_id or uuid.uuid4().hex)
        staged_root = _state_dir(api) / "outbound" / "files"
        staged_root.mkdir(parents=True, exist_ok=True)
        # Request IDs are opaque outbox keys, never filesystem components.
        # A fresh full UUID also prevents retries from overwriting bytes
        # already referenced by the first durable outbox row.
        staged = staged_root / f"{uuid.uuid4().hex}-{_safe_name(source.name)}"
        temporary = staged.with_name(staged.name + f".part.{os.getpid()}")
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, staged)
        except OSError as exc:
            if temporary.exists():
                temporary.unlink()
            return {"ok": False, "error": f"could not stage immutable file bytes: {exc}"}
        payload = {"path": str(staged), "filename": source.name, "title": title,
                   "channel": str(channel_id or ""), "thread_ts": str(thread_ts or ""),
                   "initial_comment": initial_comment}
        result = _enqueue_mutation(api, "upload_file", payload, receipt, ctx)
        if result["deduplicated"]:
            staged.unlink(missing_ok=True)
        return result

    return upload


def _make_file_download(api: Any):
    async def download(*, file_id: str = "") -> dict[str, Any]:
        try:
            settings = api.get_settings(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
            from .lib.slack_api import SlackClient
            async with SlackClient(settings.get("SLACK_BOT_TOKEN", ""), settings.get("SLACK_APP_TOKEN", "")) as slack:
                destination = _state_dir(api) / "downloads"
                staged = await slack.download_file(file_id, destination=destination)
                return {"ok": True, "source": "files.info+url_private", "file": staged.as_dict()}
        except Exception as exc:
            return {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}

    return download


def _make_generic_api(api: Any):
    async def generic_api(
        ctx: Any = None,
        *, method: str = "GET", path: str = "", effect: str = "write", params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None, request_id: str = "",
        result_kind: str = "auto",
    ) -> dict[str, Any]:
        try:
            selected, endpoint = SlackClient.normalize_method_path(method, path)
            if effect not in {"read", "write"}:
                raise SlackConfigurationError("effect must be read or write")
            if result_kind not in {"auto", "operation", "message"}:
                raise SlackConfigurationError("result_kind must be auto, operation, or message")
            if "token" in (params or {}) or "token" in (body or {}):
                raise SlackConfigurationError("generic Slack API payload must not include token")
            if effect == "read":
                settings = api.get_settings(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
                async with SlackClient(
                    settings.get("SLACK_BOT_TOKEN", ""), settings.get("SLACK_APP_TOKEN", ""),
                    require_app_token=False,
                ) as slack:
                    result = await slack.generic_request(method=selected, path=endpoint,
                                                         params=params or {}, body=body or {}, effect="read")
                return {"ok": True, "state": "read", "source": endpoint, "response": result}
            payload = {"method": selected, "path": endpoint, "effect": "write",
                       "result_kind": result_kind, "params": params or {}, "body": body or {}}
            return _enqueue_mutation(api, "generic_api", payload, request_id, ctx)
        except (SlackConfigurationError, SlackApiError) as exc:
            return {"ok": False, "error": {"code": getattr(exc, "error", "configuration_or_argument"), "message": str(exc)}}

    return generic_api


def _make_action(api: Any, operation: str):
    def action(ctx: Any = None, *, request_id: str = "", **payload: Any) -> dict[str, Any]:
        return _enqueue_mutation(api, operation, payload, request_id, ctx)
    return action


def _register_mutation_tools(api: Any) -> None:
    block_schema = {
        "type": "object",
        "properties": {"type": {"type": "string"}},
        "required": ["type"],
        # Block Kit variants carry different fields; keep their provider payloads open.
        "additionalProperties": True,
    }
    upload_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "file_path": {"type": "string", "description": "Path to bytes readable by this task; bytes are copied into skill state before enqueue."},
            "channel_id": {"type": "string"}, "thread_ts": {"type": "string"},
            "title": {"type": "string"}, "initial_comment": {"type": "string"},
            "request_id": {"type": "string"},
        }, "required": ["file_path"],
    }
    register_json_tool(api, "slack_file_upload", _make_file_upload(api), description="Stage immutable bytes and queue Slack External Upload API delivery.", schema=upload_schema, timeout_sec=30)
    register_json_tool(api, "slack_file_download", _make_file_download(api), description="Download one Slack file by exact provider file ID into the skill state artifact directory.", schema={"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"], "additionalProperties": False}, timeout_sec=60)
    register_json_tool(
        api, "slack_receipt", lambda *, request_id: BridgeStore(_state_dir(api)).delivery_receipt(request_id),
        description="Read durable provider and Host-report states for one queued Slack request ID, including uncertain outcomes.",
        schema={"type": "object", "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"], "additionalProperties": False}, timeout_sec=30,
    )
    register_json_tool(
        api,
        "slack_api",
        _make_generic_api(api),
        description=(
            "Call one actual Slack Web API method with the existing bot credential. "
            "Set effect=read for a provider read; effect=write uses the durable mutation outbox "
            "regardless of the HTTP GET/POST transport method. "
            "Writes retain provider receipts or uncertainty. Never include token in params/body."
        ),
        schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
                "effect": {"type": "string", "enum": ["read", "write"], "default": "write",
                           "description": "Provider effect selected by the model; HTTP verb does not indicate whether a Slack method mutates state."},
                "result_kind": {"type": "string", "enum": ["auto", "operation", "message"], "default": "auto",
                                "description": "Select message for a write that creates speech, operation for other effects. Auto reports confirmed chat.postMessage speech."},
                "path": {"type": "string", "description": "Slack Web API method such as conversations.list or chat.postMessage."},
                "params": {"type": "object", "additionalProperties": True},
                "body": {"type": "object", "additionalProperties": True},
                "request_id": {"type": "string", "description": "Stable dedupe key required for retryable POST writes."},
            },
            "required": ["path"],
        },
        timeout_sec=60,
    )
    action_specs = [
        ("slack_message_edit", "update_message", {"channel": {"type": "string"}, "ts": {"type": "string"}, "text": {"type": "string"}, "blocks": {"type": "array", "items": block_schema}, "text_format": {"type": "string", "enum": list(TEXT_FORMATS)}, "request_id": {"type": "string"}}, ["channel", "ts"]),
        ("slack_message_delete", "delete_message", {"channel": {"type": "string"}, "ts": {"type": "string"}, "request_id": {"type": "string"}}, ["channel", "ts"]),
        ("slack_reaction_add", "reaction_add", {"channel": {"type": "string"}, "ts": {"type": "string"}, "name": {"type": "string"}, "request_id": {"type": "string"}}, ["channel", "ts", "name"]),
        ("slack_reaction_remove", "reaction_remove", {"channel": {"type": "string"}, "ts": {"type": "string"}, "name": {"type": "string"}, "request_id": {"type": "string"}}, ["channel", "ts", "name"]),
        ("slack_pin_add", "pin_add", {"channel": {"type": "string"}, "ts": {"type": "string"}, "request_id": {"type": "string"}}, ["channel", "ts"]),
        ("slack_pin_remove", "pin_remove", {"channel": {"type": "string"}, "ts": {"type": "string"}, "request_id": {"type": "string"}}, ["channel", "ts"]),
        ("slack_bookmark_add", "bookmark_add", {"channel": {"type": "string"}, "title": {"type": "string"}, "link": {"type": "string"}, "emoji": {"type": "string"}, "request_id": {"type": "string"}}, ["channel", "title", "link"]),
        ("slack_bookmark_remove", "bookmark_remove", {"channel": {"type": "string"}, "bookmark_id": {"type": "string"}, "request_id": {"type": "string"}}, ["channel", "bookmark_id"]),
    ]
    for name, operation, properties, required in action_specs:
        register_json_tool(api, name, _make_action(api, operation), description=f"Queue Slack {operation.replace('_', ' ')} and preserve its provider receipt.", schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False}, timeout_sec=30)
    register_json_tool(
        api,
        "slack_join",
        _make_action(api, "join_conversation"),
        description=(
            "Queue an explicit join of one public Slack conversation by exact ID. "
            "Joining is a durable provider mutation: it returns a request_id whose "
            "provider receipt, failure or uncertainty is read with slack_receipt. "
            "It is never an automatic fallback for a failed history read."
        ),
        schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "channel_id": {"type": "string", "description": "Exact Slack conversation ID to join."},
                "request_id": {"type": "string", "description": "Optional stable dedupe key for safe retries."},
            },
            "required": ["channel_id"],
        },
        timeout_sec=30,
    )


_WORKER_SETTINGS = (
    ("SLACK_INBOUND_WORKERS", 4, 1, 16),
    ("SLACK_OUTBOUND_WORKERS", 2, 1, 8),
)


def _make_settings_save(api: Any):
    async def settings_save(request: Any) -> JSONResponse:
        # The host hydrates the form with GET (and admits HEAD beside it):
        # only POST saves, so reading the form never writes the file.
        saving = request.method.upper() == "POST"
        body: Any = {}
        if saving:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "error": "Expected a JSON object"}, status_code=400
                )

        try:
            current = _load_local_settings(api)
        except LocalSettingsError as exc:
            # Saving would rewrite the whole object, silently discarding values
            # this reader could not parse. Refuse instead of overwriting them.
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        f"{exc}. Existing settings were left unchanged; repair or "
                        "remove the file before saving."
                    ),
                },
                status_code=409,
            )
        if not saving:
            # Exactly the form's fields, as strings; the number fields have
            # placeholders, not defaults, so unset counts show the effective one.
            values = {"binding_id": str(current.get("binding_id") or "")}
            for key, default, _minimum, _maximum in _WORKER_SETTINGS:
                values[key] = str(current.get(key) or default)
            return JSONResponse(values)
        if "binding_id" in body:
            binding_id = str(body.get("binding_id") or "").strip()
            if binding_id:
                try:
                    binding_id = normalize_binding_id(binding_id)
                except HostContractError as exc:
                    return JSONResponse(
                        {"ok": False, "error": str(exc)}, status_code=400
                    )
            current["binding_id"] = binding_id
        for key, default, minimum, maximum in _WORKER_SETTINGS:
            if key not in body:
                continue
            try:
                value = int(body[key])
            except (TypeError, ValueError):
                return JSONResponse(
                    {"ok": False, "error": f"{key} must be an integer"},
                    status_code=400,
                )
            current[key] = max(minimum, min(maximum, value or default))

        _save_local_settings(api, current)
        return JSONResponse(
            {
                "ok": True,
                "message": "Slack Bridge settings saved. Toggle the skill to restart its companion.",
            }
        )

    return settings_save


def register(api: Any) -> None:
    api.register_companion_process("slack_socket_mode")
    register_read_tools(api)
    _register_mutation_tools(api)
    register_json_tool(
        api,
        "slack_send",
        _make_slack_send(api),
        description=(
            "Durably queue a proactive Slack text message or thread reply. "
            "Use a stable channel ID (C/G/D...) or member ID (U/W...), not a display name. "
            "Standard Markdown is the default; choose mrkdwn for Slack-native syntax or plain for literal markup."
        ),
        schema={
            "type": "object",
            "properties": {
                "channel_or_user": {
                    "type": "string",
                    "description": "Slack channel ID or member ID.",
                },
                "text": {"type": "string", "description": "Text to send."},
                "text_format": {
                    "type": "string", "enum": list(TEXT_FORMATS), "default": "markdown",
                    "description": "markdown: standard Markdown via Slack markdown_text; mrkdwn: Slack-native syntax; plain: literal markup.",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Optional Slack thread timestamp.",
                },
                "request_id": {
                    "type": "string",
                    "description": "Optional caller dedupe key for safe retries.",
                },
            },
            "required": ["channel_or_user", "text"],
        },
        timeout_sec=30,
    )
    api.register_route("status", handler=_make_status(api), methods=("GET",))
    api.register_route(
        "settings/save", handler=_make_settings_save(api), methods=("GET", "POST")
    )

    api.register_ui_tab(
        "slack_presence",
        title="Slack Bridge",
        icon="message",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "markdown",
                    "text": "### Slack presence transport\n"
                    "Live Socket Mode health and durable inbox/outbox custody. Message "
                    "contents and credentials are never shown here.",
                },
                {
                    "type": "poll",
                    "route": "status",
                    "auto_start": True,
                    "interval_ms": 3000,
                    "target": "status",
                    "max_ticks": 100,
                    "label": "Refresh transport status",
                },
                {
                    "type": "group",
                    "layout": "cluster",
                    "components": [
                        {
                            "type": "metric",
                            "label": "Socket",
                            "path": "socket_state",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Host adapter",
                            "path": "host_adapter_state",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Presence binding",
                            "path": "binding_state",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Workspace",
                            "path": "workspace_name",
                            "tone": "info",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Inbox pending",
                            "path": "inbox_pending",
                            "tone": "neutral",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Inbox delivered",
                            "path": "inbox_delivered",
                            "tone": "success",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Outbox pending",
                            "path": "outbox_pending",
                            "tone": "neutral",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Outbox delivered",
                            "path": "outbox_delivered",
                            "tone": "success",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Inbox failed",
                            "path": "inbox_failed",
                            "tone": "danger",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Outbox failed",
                            "path": "outbox_failed",
                            "tone": "danger",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Mutations pending",
                            "path": "mutations_pending",
                            "tone": "neutral",
                            "target": "status",
                        },
                        {
                            "type": "metric",
                            "label": "Mutations uncertain",
                            "path": "mutations_uncertain",
                            "tone": "danger",
                            "target": "status",
                        },
                    ],
                    "target": "status",
                },
                {
                    "type": "json",
                    "label": "Last delivery error",
                    "path": "last_delivery_error",
                    "target": "status",
                },
            ],
        },
    )

    api.register_settings_section(
        "slack_presence",
        title="Slack Bridge",
        schema={
            "components": [
                {
                    "type": "markdown",
                    "text": (
                        "Set and grant `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in Secrets. "
                        "Select an owner-created Presence Binding ID for "
                        "provider `slack`, the workspace Team ID, and an exact channel conversation ID or `*`. "
                        "The bridge receives messages from every DM, MPDM, public channel, "
                        "and private channel that the installed app can actually see."
                    ),
                },
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "submit_label": "Save Slack settings",
                    "fields": [
                        {
                            "name": "binding_id",
                            "label": "Presence Binding ID",
                            "type": "text",
                            "placeholder": "32 lowercase hexadecimal characters",
                            "help": "Owner-created binding for provider slack, this workspace Team ID, and an exact channel conversation ID or *.",
                        },
                        {
                            "name": "SLACK_INBOUND_WORKERS",
                            "label": "Inbound workers",
                            "type": "number",
                            "placeholder": "4",
                        },
                        {
                            "name": "SLACK_OUTBOUND_WORKERS",
                            "label": "Outbound workers",
                            "type": "number",
                            "placeholder": "2",
                        },
                    ],
                },
            ]
        },
    )
