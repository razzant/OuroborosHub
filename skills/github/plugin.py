"""PluginAPI 2.0 entrypoint. Registration starts no network or background work."""
from __future__ import annotations

import json
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ouroboros loads the entry by file; keep imports local to the reviewed payload.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gh_presence.config import SECRET_KEYS, connection_account, provider_http, read_config, write_config, writer_http
from gh_presence.http import TransportError
from gh_presence.identity import connector_identity
from gh_presence.provider import GitHub
from gh_presence.outbox import Outbox
from gh_presence.store import Store, dump


def register(api):
    state_dir = Path(api.get_runtime_info()["state_dir"])

    def status_data(offset=0):
        config = read_config(state_dir)
        store = Store(state_dir, readonly=True)
        try:
            value = store.status(offset=offset)
        finally:
            store.close()
        value["enabled"] = config["enabled"]
        value["host_contract_ready"] = value["metadata"].get("host_capabilities", {}).get("ready", False)
        value["account_id"] = account = connection_account(config) if config["expected_writer_id"] else None
        # Connect rows name a real account; until the writer is configured nothing is offered yet.
        value["connect"] = []
        if isinstance(account, str) and account:
            value["connect"] = [{"account_id": account, "conversation_id": "repo." + str(r["id"]),
                                 "label": r["full_name"]} for r in config["repositories"]]
            if config["writer_notifications"] or config["notifications_user_id"]:
                value["connect"].append({"account_id": account, "conversation_id": "",
                                         "label": "Account-wide notifications"})
        writer = value["metadata"].get("auth:writer", {})
        identity = connector_identity(config, value["metadata"])
        verified = bool(identity)
        if value["metadata"].get("host_capabilities", {}).get("presence_identity") == 1 and identity:
            value["identity"] = identity
        value["auth_mode"] = config["auth_mode"]
        value["text"] = "Writing as @" + writer["login"] if verified else "Writer identity unverified"
        if verified and writer.get("auth_mode") == "app":
            value["text"] += " (App installation " + str(writer["installation_id"]) + ")"
        value["tone"] = "warn" if value["gaps"] or not config["enabled"] else "muted"
        value["title"] = "Collection enabled" if config["enabled"] else "Collection disabled"
        heartbeat = value["metadata"].get("runtime", {}).get("at")
        if config["enabled"] and (not heartbeat or (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds() > 60):
            value.update(text="Companion heartbeat unavailable or stale", tone="warn")
        return value

    async def status(request):
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await asyncio.to_thread(status_data, int(request.query_params.get("offset", 0))))
        except (ValueError, OSError, sqlite3.Error) as exc:
            return JSONResponse({"text": "Configuration/state unavailable", "tone": "bad",
                                 "code": type(exc).__name__}, status_code=503)

    async def settings(request):
        from starlette.responses import JSONResponse
        try:
            if request.method == "GET":
                return JSONResponse({"configuration": json.dumps(await asyncio.to_thread(read_config, state_dir), indent=2)})
            value = await request.json()
            if set(value) != {"configuration"} or not isinstance(value["configuration"], str):
                raise ValueError("expected configuration JSON text")
            config = await asyncio.to_thread(write_config, state_dir, json.loads(value["configuration"]))
            return JSONResponse({"configuration": json.dumps(config, indent=2), "saved": True,
                                 "note": "Secrets refresh only on a controlled companion restart; place settings remain host-owned."})
        except (ValueError, OSError):
            # Parser errors can include input text; never return raw exceptions.
            return JSONResponse({"code": "invalid_or_unwritable_configuration"}, status_code=400)

    async def probe(request):
        """Provider probe; App mode mints a token but makes no repository change."""
        import asyncio
        from starlette.responses import JSONResponse
        def check():
            config = read_config(state_dir)
            credentials = api.get_settings(list(SECRET_KEYS))
            store = Store(state_dir)
            try:
                provider = GitHub(writer_http(config, credentials, readonly=True), store, config=config)
                provider.identity(config["expected_writer_id"])
                selected = [provider.repository(repo) for repo in config["repositories"]]
                for identity in config["watched_identities"]:
                    provider.watched_identity(identity)
                if config["writer_notifications"]:
                    provider.get("/notifications?all=true&per_page=1")
                if config["notifications_user_id"]:
                    reader = GitHub(provider_http(config, credentials.get(SECRET_KEYS[1], ""), readonly=True), store, "notifications", config=config)
                    reader.identity(config["notifications_user_id"])
                    reader.get("/notifications?all=true&per_page=1")
                return {"ok": True, "account_id": connection_account(config), "auth_mode": config["auth_mode"],
                        "repositories": [{"id": r["id"], "full_name": r["full_name"], "discussions": r.get("has_discussions", False)} for r in selected],
                        "write_permissions": "not proven by a public test write", "collection_enabled": config["enabled"],
                        "host_readiness": "not checked by this provider-only probe; see companion status"}
            finally:
                store.close()
        try:
            return JSONResponse(await asyncio.to_thread(check))
        except (ValueError, OSError, TransportError) as exc:
            return JSONResponse({"ok": False, "code": getattr(exc, "code", "configuration_or_credential_unavailable")}, status_code=400)

    def send(ctx=None, **kwargs):
        return queue(ctx, "comment", kwargs)

    def close(ctx=None, **kwargs):
        return queue(ctx, "close", kwargs)

    def queue(ctx, operation, kwargs):
        store = Store(state_dir)
        try:
            task_id = ctx.get("task_id") if isinstance(ctx, dict) else getattr(ctx, "task_id", None)
            task_id = task_id if isinstance(task_id, str) else None
            return dump(Outbox(store, read_config(state_dir)).tool(operation=operation, task_id=task_id, **kwargs))
        except (ValueError, OSError) as exc:
            return dump({"ok": False, "code": str(exc) if isinstance(exc, ValueError) else "state_unwritable"})
        finally:
            store.close()

    def inspect(account_id="", conversation_id="", delivery_id="", source_event_id="", status_offset=0):
        store = Store(state_dir, readonly=True)
        try:
            if delivery_id:
                record = store.one("SELECT event_id FROM outbox WHERE id=?", (delivery_id,))
                if not record:
                    raise ValueError("unknown_delivery_id")
                event = store.event(record["event_id"])
                result = Outbox(store, read_config(state_dir)).inspect(delivery_id)
            elif source_event_id:
                event = store.event(source_event_id)
                result = {"event": event}
            else:
                return dump(status_data(offset=status_offset))
            if account_id != event["account_id"] or conversation_id != event["conversation_id"]:
                raise ValueError("source_address_mismatch")
            return dump(result)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return dump({"ok": False, "code": str(exc)})
        finally:
            store.close()

    fields = {key: {"type": "string"} for key in
              ("account_id", "conversation_id", "thread_id", "source_event_id", "body", "request_id", "reconsider_fingerprint")}
    required = [key for key in fields if key != "reconsider_fingerprint"]
    schema = {"type": "object", "properties": fields, "required": required, "additionalProperties": False}
    api.register_tool("github_place_message", handler=send, description="Queue an authorized reply using an immutable source event's exact subthread. Returns custody, not a delivery promise.", schema=schema)
    close_schema = {**schema, "properties": {**fields, "state_reason": {"type": "string", "enum": ["not_planned", "completed"]}},
                    "required": required + ["state_reason"]}
    api.register_tool("github_place_close", handler=close, description="Queue an explained issue closure. Confirms the explanation first; uncertain explanation holds closure. Requires owner authority for this change.", schema=close_schema)
    api.register_tool("github_place_status", handler=inspect, description="Read transport status, an exact source event, or an outbox delivery with full retained evidence.",
                      schema={"type": "object", "properties": {**{key: {"type": "string"} for key in
                              ("account_id", "conversation_id", "delivery_id", "source_event_id")},
                              "status_offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False})
    api.register_route("status", handler=status, methods=("GET",))
    api.register_route("settings", handler=settings, methods=("GET", "POST"))
    api.register_route("probe", handler=probe, methods=("POST",))
    api.register_settings_section("github", title="GitHub", schema={"components": [
        {"type": "markdown", "text": "Configure a PAT writer or an App installation and explicit repository IDs; see SETUP.md. App mode verifies the App bot's numeric writer ID and mints expiring tokens. Notifications require separate classic PAT credentials. Start with collection disabled, verify identities, then connect each repository in Places. New places start Silent (on an older host, set them Silent); keep them Silent for the first trial. For public notifications outside the selected repositories, connect Account-wide notifications and add every message to that place's attention list. Private IDs require explicit selection. Credentials belong in Settings → Secrets. Windows and live App/GHES operation still require native qualification."},
        {"type": "form", "route": "settings", "method": "POST", "fields": [
            {"name": "configuration", "label": "Configuration JSON", "type": "textarea"}], "submit_label": "Save configuration"},
        {"type": "action", "route": "probe", "method": "POST", "label": "Verify identities and repositories", "target": "probe"},
        {"type": "json", "target": "probe"},
    ]})
    api.register_companion_process("github_poller")
