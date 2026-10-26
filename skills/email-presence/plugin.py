"""Email tools and configuration; the companion owns automatic intake/delivery."""
from __future__ import annotations

import json
import inspect
import time
import uuid
from functools import wraps
from pathlib import Path

from starlette.responses import JSONResponse

from .lib.client import MailClient
from .lib.delivery import tool_origin
from .lib.host_adapter import HostContractError, normalize_binding_id
from .lib.mime import reply_all_recipients
from .lib.store import EmailStore

SETTING_KEYS = ["EMAIL_IMAP_HOST", "EMAIL_IMAP_PORT", "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
                "EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_DEFAULT_FOLDER", "EMAIL_AUTH_MODE",
                "EMAIL_OAUTH_ACCESS_TOKEN", "EMAIL_OAUTH_REFRESH_TOKEN", "EMAIL_OAUTH_CLIENT_ID",
                "EMAIL_OAUTH_CLIENT_SECRET"]


def _json_tool(handler):
    """Encode only the tool boundary; preserve ctx discovery and exceptions."""
    if inspect.iscoroutinefunction(handler):
        @wraps(handler)
        async def encoded_async(*args, **kwargs):
            return json.dumps(await handler(*args, **kwargs), ensure_ascii=False)
        return encoded_async

    @wraps(handler)
    def encoded(*args, **kwargs):
        return json.dumps(handler(*args, **kwargs), ensure_ascii=False)
    return encoded


def _load(api):
    path = Path(api.get_state_dir()) / "settings.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Email settings must be a JSON object")
    return value


def register(api):
    state = Path(api.get_state_dir())
    api.register_companion_process("email_poller")

    def store():
        return EmailStore(state)

    def client():
        return MailClient(api.get_settings(SETTING_KEYS))

    def addresses(value):
        values = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        return [str(x).strip() for x in values if str(x).strip()]

    def send(ctx=None, *, to="", body="", subject="", reply_to_message_id="", references=None,
             cc=None, bcc=None, reply_all=False, html_body="", body_type="plain", attachments=None, request_id=""):
        recipients = addresses(to)
        visible_cc = addresses(cc)
        visible_bcc = addresses(bcc)
        if reply_all:
            if not reply_to_message_id:
                raise ValueError("reply_to_message_id is required for reply_all")
            original = store().message_by_id(reply_to_message_id)
            if not original:
                raise ValueError("reply_all source message is not in the durable inbox")
            derived = reply_all_recipients(original, client().settings.get("EMAIL_USER"))
            recipients = derived["to"] + recipients
            visible_cc = derived["cc"] + visible_cc
            if references is None:
                references = original.get("references") or []
        if not recipients or (not str(body).strip() and not str(html_body).strip()):
            raise ValueError("to (or reply_all source) and body are required")
        rid = request_id or uuid.uuid4().hex
        mailbox = store()
        existing = mailbox.receipt(rid) if request_id else None
        if existing:
            return {"ok": True, "request_id": rid, "deduplicated": True, "receipt": existing}
        staged = mailbox.stage_outbound_attachments(rid, attachments or [])
        inserted = mailbox.enqueue_outbox(request_id=rid, to=recipients, cc=visible_cc, bcc=visible_bcc, subject=subject,
                                          body=body, in_reply_to=reply_to_message_id, references=references or [],
                                          attachments=staged, html_body=html_body, body_type=body_type,
                                          reporting={"version": -1, "origin": tool_origin(ctx)})
        return {"ok": True, "request_id": rid, "deduplicated": not inserted, "receipt": mailbox.receipt(rid)}

    def search(**kwargs):
        kwargs.setdefault("folder", client().settings.get("EMAIL_DEFAULT_FOLDER") or "INBOX")
        return client().search(**kwargs)

    def read(**kwargs):
        kwargs.setdefault("folder", client().settings.get("EMAIL_DEFAULT_FOLDER") or "INBOX")
        message = client().read(include_attachment_data=True, **kwargs)
        if message.get("_raw_source") is not None or message.get("attachments"):
            store().stage_inbound_attachments(message)
        return message

    def draft(**kwargs):
        if kwargs.pop("reply_all", False):
            source_id = kwargs.get("reply_to_message_id")
            if not source_id:
                raise ValueError("reply_to_message_id is required for reply_all")
            original = store().message_by_id(source_id)
            if not original:
                raise ValueError("reply_all source message is not in the durable inbox")
            derived = reply_all_recipients(original, client().settings.get("EMAIL_USER"))
            kwargs["to"] = derived["to"] + addresses(kwargs.get("to"))
            kwargs["cc"] = derived["cc"] + addresses(kwargs.get("cc"))
            if kwargs.get("references") is None:
                kwargs["references"] = original.get("references") or []
        staged = store().stage_outbound_attachments("draft:" + uuid.uuid4().hex, kwargs.pop("attachments", None) or [])
        for key in ("cc", "bcc"):
            kwargs[key] = addresses(kwargs.get(key))
        kwargs["attachments"] = staged
        return client().draft(**kwargs)

    string = {"type": "string"}
    integer = {"type": "integer"}
    array = {"type": "array", "items": string}

    def tool(name, handler, description, properties, required=()):
        api.register_tool(name, _json_tool(handler), description=description,
                          schema={"type": "object", "properties": properties, "required": list(required)},
                          timeout_sec=120)

    tool("email_send", send, "Queue a text email or threaded reply. Reuse request_id to deduplicate; query email_receipt for delivery.",
         {"to": string, "body": string, "subject": string, "reply_to_message_id": string,
          "references": array, "cc": {"type": "array", "items": string},
          "bcc": {"type": "array", "items": string}, "reply_all": {"type": "boolean"},
          "html_body": string, "body_type": {"type": "string", "enum": ["plain", "html"]},
          "attachments": {"type": "array", "items": {"type": "object"}}, "request_id": string}, ("body",))
    tool("email_search", search, "Search all mail, including mail before activation. IMAP criteria tokens, e.g. ['UNSEEN'], ['FROM', '\"alice@example.org\"'], ['HEADER', 'Message-ID', '\"<id>\"']. Returns stable UID and UIDVALIDITY.",
         {"folder": string, "criteria": array, "limit": integer})
    tool("email_read", read, "Read any old or new message using UID and UIDVALIDITY from email_search. Peeks unless mark_as_read is true.",
         {"uid": integer, "uidvalidity": integer, "folder": string, "mark_as_read": {"type": "boolean"}}, ("uid", "uidvalidity"))
    tool("email_mailbox", lambda **kw: client().mailbox(**kw),
         "List/create folders, copy/move a message, or add/remove flags (Seen, Flagged, Deleted). Move requires server UID MOVE. No global expunge.",
         {"action": {"type": "string", "enum": ["list", "create", "copy", "move", "flags"]},
          "folder": string, "uid": integer, "uidvalidity": integer, "destination": string,
          "flags": array, "remove_flags": {"type": "boolean"}})
    tool("email_draft", draft,
         "Save a text draft without sending it. Choose the real draft folder from email_mailbox list.",
         {"to": string, "subject": string, "body": string, "folder": string,
          "reply_to_message_id": string, "references": array, "cc": {"type": "array", "items": string},
          "bcc": {"type": "array", "items": string}, "html_body": string,
          "reply_all": {"type": "boolean"},
          "body_type": {"type": "string", "enum": ["plain", "html"]},
          "attachments": {"type": "array", "items": {"type": "object"}}}, ("to", "subject"))
    tool("email_receipt", lambda request_id: store().receipt(request_id),
         "Get queued/completed/failed/uncertain email delivery and its stable Message-ID. Uncertain SMTP acceptance requires inspection before any new send.",
         {"request_id": string}, ("request_id",))
    tool("email_test_connection", lambda: client().test_connection(),
         "Check IMAP/SMTP TLS and authentication without sending or ingesting messages.", {})

    async def status(_request=None):
        settings = api.get_settings(SETTING_KEYS)
        payload = store().status()
        payload.update(binding_id_configured=bool(_load(api).get("binding_id")),
                       imap_host_configured=bool(settings.get("EMAIL_IMAP_HOST")),
                       smtp_host_configured=bool(settings.get("EMAIL_SMTP_HOST")),
                       poll_health=store().get("poll_health"))
        held = payload.get("outbox_held", 0)
        connected = bool((payload.get("poll_health") or {}).get("ok"))
        payload.update(text=f"{held} held" if held else "Connected" if connected else "Not connected",
                       tone="warn" if held else "ok" if connected else "muted", title="Email transport")
        return JSONResponse(payload)

    async def save(request):
        try:
            current = _load(api)
            if request.method == "GET":
                return JSONResponse({
                    "binding_id": current.get("binding_id") or "",
                    "folder": current.get("folder") or api.get_settings(["EMAIL_DEFAULT_FOLDER"]).get("EMAIL_DEFAULT_FOLDER") or "INBOX",
                    "poll_interval_sec": current.get("poll_interval_sec") or 30,
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
        except (ValueError, TypeError, HostContractError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        state.mkdir(parents=True, exist_ok=True)
        temporary = state / f"settings.tmp.{time.time_ns()}"
        temporary.write_text(json.dumps(current, indent=2) + "\n")
        temporary.replace(state / "settings.json")
        return JSONResponse({"ok": True, "message": "Saved. Toggle the skill to restart its companion."})

    api.register_route("status", status, methods=("GET",))
    api.register_route("settings/save", save, methods=("GET", "POST"))
    api.register_ui_tab("email_presence", title="Email Presence", render={
        "kind": "declarative", "schema_version": 1, "components": [
            {"type": "action", "target": "status", "route": "status", "method": "GET", "label": "Refresh delivery status", "fields": []},
            {"type": "status", "target": "status", "idle": "Refresh to inspect mailbox intake and delivery receipts.", "loading": "Reading local status…", "success": "Status loaded.", "error": "Status unavailable."},
            {"type": "json", "target": "status"}]})
    api.register_settings_section("email_presence", title="Email Presence", schema={"components": [
        {"type": "markdown", "text": "Configure granted IMAP/SMTP credentials and connect an Email place. First connection records the intake baseline; older mail remains accessible through tools."},
        {"type": "form", "route": "settings/save", "method": "POST", "submit_label": "Save email settings", "fields": [
            {"name": "folder", "label": "Intake folder", "type": "text", "placeholder": "INBOX"},
            {"name": "poll_interval_sec", "label": "Maximum idle poll interval (seconds)", "type": "number", "placeholder": "30"}]}]})
