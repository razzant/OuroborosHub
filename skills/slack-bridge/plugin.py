from __future__ import annotations

import json
import pathlib
import time
import uuid
from typing import Any

from starlette.responses import JSONResponse

from .lib.host_adapter import HostContractError, normalize_binding_id
from .lib.slack_api import SlackConfigurationError, chunk_message
from .lib.store import BridgeStore


def _state_dir(api: Any) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir())


def _settings_path(api: Any) -> pathlib.Path:
    return _state_dir(api) / "settings.json"


def _load_local_settings(api: Any) -> dict[str, Any]:
    path = _settings_path(api)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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
        *,
        channel_or_user: str = "",
        text: str = "",
        thread_ts: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        target = _validate_target(channel_or_user)
        chunks = chunk_message(text)
        if not chunks:
            return {"ok": False, "error": "text is required"}
        receipt = str(request_id or uuid.uuid4().hex)
        count = BridgeStore(_state_dir(api)).enqueue_outbox(
            request_id=receipt,
            target=target,
            thread_ts=str(thread_ts or "").strip(),
            chunks=chunks,
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
        binding_id = str(_load_local_settings(api).get("binding_id") or "").strip()
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
            }
        )
        return JSONResponse(payload)

    return status


def _make_settings_save(api: Any):
    async def settings_save(request: Any) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "Expected a JSON object"}, status_code=400
            )

        current = _load_local_settings(api)
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
        for key, default, minimum, maximum in (
            ("SLACK_INBOUND_WORKERS", 4, 1, 16),
            ("SLACK_OUTBOUND_WORKERS", 2, 1, 8),
        ):
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
    api.register_tool(
        "slack_send",
        _make_slack_send(api),
        description=(
            "Durably queue a proactive Slack text message or thread reply. "
            "Use a stable channel ID (C/G/D...) or member ID (U/W...), not a display name."
        ),
        schema={
            "type": "object",
            "properties": {
                "channel_or_user": {
                    "type": "string",
                    "description": "Slack channel ID or member ID.",
                },
                "text": {"type": "string", "description": "Text to send."},
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
        "settings/save", handler=_make_settings_save(api), methods=("POST",)
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
                    "text": (
                        "### Slack presence transport\n"
                        "Live Socket Mode health and durable inbox/outbox custody. "
                        "Message contents and credentials are never shown here."
                    ),
                },
                {
                    "type": "poll",
                    "route": "status",
                    "auto_start": True,
                    "interval_ms": 3000,
                },
                {
                    "type": "group",
                    "layout": "cluster",
                    "components": [
                        {"type": "status", "label": "Socket", "path": "socket_state"},
                        {
                            "type": "status",
                            "label": "Host adapter",
                            "path": "host_adapter_state",
                        },
                        {
                            "type": "status",
                            "label": "Presence binding",
                            "path": "binding_state",
                        },
                        {
                            "type": "metric",
                            "label": "Workspace",
                            "path": "workspace_name",
                            "tone": "info",
                        },
                        {
                            "type": "metric",
                            "label": "Inbox pending",
                            "path": "inbox_pending",
                            "tone": "neutral",
                        },
                        {
                            "type": "metric",
                            "label": "Inbox delivered",
                            "path": "inbox_delivered",
                            "tone": "success",
                        },
                        {
                            "type": "metric",
                            "label": "Outbox pending",
                            "path": "outbox_pending",
                            "tone": "neutral",
                        },
                        {
                            "type": "metric",
                            "label": "Outbox delivered",
                            "path": "outbox_delivered",
                            "tone": "success",
                        },
                        {
                            "type": "metric",
                            "label": "Inbox failed",
                            "path": "inbox_failed",
                            "tone": "danger",
                        },
                        {
                            "type": "metric",
                            "label": "Outbox failed",
                            "path": "outbox_failed",
                            "tone": "danger",
                        },
                    ],
                },
                {
                    "type": "json",
                    "label": "Last delivery error",
                    "path": "last_delivery_error",
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
                        "Select an owner-created account-wide Presence Binding ID for "
                        "provider `slack`, the workspace Team ID, and conversation ID `*`. "
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
                            "help": "Owner-created binding for provider slack, this workspace Team ID, and conversation ID *.",
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
