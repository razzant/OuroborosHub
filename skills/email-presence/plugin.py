"""Mail tools and owner-facing account configuration; the companion owns delivery."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from starlette.responses import JSONResponse

from .lib.accounts import AccountError, SETTING_KEYS
from .lib.client import MailClient
from .lib.facts import mailbox_identity
from .lib import google_auth
from .lib.host_adapter import HostContractError, normalize_binding_id
from .lib.service import MailService, json_tool, register_tools

_json_tool = json_tool  # Retain the existing extension text-boundary helper.
from .lib.settings import SENDING_OFF, load_settings, normalize_words


def _load(api):
    return load_settings(api.get_state_dir())


def register(api):
    state = Path(api.get_state_dir())
    service = MailService(api, mail_client=MailClient)
    api.register_companion_process("email_poller")
    register_tools(api, service)

    def account_status(account):
        payload = service.queue_status(account)
        health = payload.get("poll_health")
        payload.update(account_id=account["id"], address=account["address"], label=account["label"],
                       provider=account["provider"], connected=account["connected"], poll_health=health,
                       runtime_error=account.get("runtime_error", ""))
        sending = account.get("sending", "on")
        held = payload.get("outbox_held", 0)
        payload["readiness"] = {
            "sending": sending, "connected": account["connected"],
            "text": "Disconnected; history retained" if not account["connected"] else
                    SENDING_OFF if sending == "off" else "Sending on",
        }
        payload["identity"] = mailbox_identity(account["address"], senders=account["senders"],
                                                sender=account.get("sender") or account["address"])
        text = "Disconnected" if not account["connected"] else (
            "Worker stopped: " + account["runtime_error"] if account.get("runtime_error") else
            f"{held} held" if held else "Tools account; no automatic intake" if (health or {}).get("intake") == "no_place"
            else "Authenticated intake" if (health or {}).get("ok") else "Configured; test connection")
        payload.update(text=text, tone="warn" if held or not account["connected"] or account.get("runtime_error") else "ok" if (health or {}).get("ok") else "muted",
                       title="Email transport")
        return payload

    async def status(request=None):
        def build():
            records = service.accounts.list()
            selected = str(request.query_params.get("account_id") or "") if request is not None else ""
            if selected:
                payload = account_status(service.accounts.get(selected))
            elif len(records) == 1:
                payload = account_status(records[0])
            else:
                payload = {"accounts": [account_status(record) for record in records], "text": f"{len(records)} accounts",
                           "title": "Email accounts", "tone": "muted"}
            payload["labels"] = {"direct_message": "Mail addressed to you"}
            payload["connect"] = [{"account_id": record["id"], "conversation_id": "", "label": record["label"]}
                                  for record in records if record["connected"]]
            return payload
        try:
            return JSONResponse(await asyncio.to_thread(build))
        except AccountError as exc:
            return JSONResponse({"ok": False, "code": "account_not_found", "error": str(exc)}, status_code=404)

    async def account_list(_request):
        def build():
            result = service.list_accounts()
            settings = api.get_settings(SETTING_KEYS)
            result["google_client_configured"] = bool(settings.get("GOOGLE_OAUTH_CLIENT_ID") or settings.get("EMAIL_OAUTH_CLIENT_ID"))
            result["credential_storage"] = "Credentials are stored in this skill's private local state (file mode 0600). Disconnect keeps mail history."
            return result
        return JSONResponse(await asyncio.to_thread(build))

    async def mutate_account(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            action = request.url.path.rsplit("/", 1)[-1]
            def perform():
                accounts = service.accounts
                if action == "save":
                    return accounts.public(accounts.save_imap(body))
                account_id = str(body.get("account_id") or "")
                if action == "preferences":
                    return accounts.public(accounts.update_preferences(account_id, body))
                if action == "disconnect":
                    return accounts.public(accounts.disconnect(account_id))
                if action == "test":
                    return service.test_connection(account_id=account_id)
                if action == "refresh-senders":
                    with service.target(account_id) as (account, _store, client):
                        if account["provider"] != "gmail":
                            raise AccountError("Edit configured sender addresses for this IMAP account")
                        values = client.settings_read(resource="sendAs")
                        senders = list(dict.fromkeys([account["address"]] + [
                            row["sendAsEmail"] for row in values.get("sendAs", [])
                            if row.get("sendAsEmail") and row.get("verificationStatus") == "accepted"]))
                        return accounts.public(accounts.update_senders(account_id, senders, account["generation"]))
                if action == "resume-legacy":
                    legacy = accounts._legacy_account()
                    if not legacy or legacy["id"] != account_id:
                        raise AccountError("The original legacy mailbox is no longer configured in Settings")
                    with accounts._db(write=True) as db:
                        db.execute("UPDATE accounts SET connected=1,generation=? WHERE id=? AND json_extract(record,'$.uses_env')=1",
                                   (str(time.time_ns()), account_id))
                    return accounts.public(accounts.get(account_id))
                raise ValueError("Unknown account action")
            result = await asyncio.to_thread(perform)
            return JSONResponse({"ok": True, "account": result})
        except (ValueError, TypeError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    async def oauth_begin(request):
        try:
            body = await request.json()
            origin = str(body.get("origin") or "").rstrip("/")
            if not origin:
                info = api.get_runtime_info()
                origin = f"http://127.0.0.1:{int(info['server_port'])}"
            result = await asyncio.to_thread(google_auth.begin, service.accounts, api.get_settings(SETTING_KEYS),
                                            origin=origin, login_hint=str(body.get("login_hint") or ""),
                                            permanent_delete=body.get("permanent_delete") is True)
            return JSONResponse(result)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    async def oauth_complete(request):
        try:
            body = await request.json()
            result = await asyncio.to_thread(google_auth.finish, service.accounts, state=str(body.get("state") or ""),
                                            code=str(body.get("code") or ""), error=str(body.get("error") or ""))
            return JSONResponse(result)
        except (ValueError, TypeError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    async def save(request):
        """Legacy settings stay at the original route and state path."""
        try:
            current = _load(api)
            if request.method == "GET":
                return JSONResponse({
                    "binding_id": current.get("binding_id") or "",
                    "folder": current.get("folder") or api.get_settings(["EMAIL_DEFAULT_FOLDER"]).get("EMAIL_DEFAULT_FOLDER") or "INBOX",
                    "poll_interval_sec": current.get("poll_interval_sec") or 30,
                    "sending": current.get("sending", "on"),
                    "words": "\n".join(current.get("words", [])),
                })
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
            if "binding_id" in body:
                current["binding_id"] = normalize_binding_id(body["binding_id"])
            if body.get("poll_interval_sec") is not None and str(body["poll_interval_sec"]).strip():
                current["poll_interval_sec"] = max(5, min(3600, int(body["poll_interval_sec"])))
            if "folder" in body:
                current["folder"] = str(body["folder"]).strip() or "INBOX"
            if "sending" in body:
                if body["sending"] not in ("on", "off"):
                    raise ValueError("Sending must be on or off")
                current["sending"] = body["sending"]
            if "words" in body:
                current["words"] = normalize_words(body["words"])
        except (ValueError, TypeError, HostContractError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        state.mkdir(parents=True, exist_ok=True)
        temporary = state / f"settings.tmp.{time.time_ns()}"
        temporary.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        temporary.replace(state / "settings.json")
        return JSONResponse({"ok": True, "message": "Saved. The companion reloads account settings automatically."})

    api.register_route("status", status, methods=("GET",))
    api.register_route("accounts", account_list, methods=("GET",))
    for action in ("save", "preferences", "disconnect", "test", "refresh-senders", "resume-legacy"):
        api.register_route("accounts/" + action, mutate_account, methods=("POST",))
    api.register_route("oauth/begin", oauth_begin, methods=("POST",))
    api.register_route("oauth/complete", oauth_complete, methods=("POST",))
    api.register_route("settings/save", save, methods=("GET", "POST"))
    api.register_ui_tab("email_presence", title="Email accounts", render={
        "kind": "module", "entry": "ui/accounts.js", "entry_type": "module",
        "assets": ["ui/accounts.css"], "appearance": "host", "start": "auto", "height": 620})
    api.register_settings_section("email_presence", title="Email accounts", schema={"components": [
        {"type": "markdown", "text": "Use the Email accounts widget to add Google, IMAP or Proton Bridge accounts and choose sender addresses. Google needs your installation's granted GOOGLE_OAUTH_CLIENT_ID (and CLIENT_SECRET when required). These fields below keep the original EMAIL_* mailbox configuration."},
        {"type": "form", "route": "settings/save", "method": "POST", "submit_label": "Save legacy mailbox settings", "fields": [
            {"name": "folder", "label": "Intake folder", "type": "text", "placeholder": "INBOX"},
            {"name": "sending", "label": "Sending", "type": "select", "default": "on",
             "options": [{"value": "on", "label": "On"}, {"value": "off", "label": "Off"}]},
            {"name": "words", "label": "Words to notice (one per line)", "type": "textarea"},
            {"name": "poll_interval_sec", "label": "Maximum idle poll interval (seconds)", "type": "number", "placeholder": "30"}]}]})
