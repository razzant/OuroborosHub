"""PluginAPI 2.4 entry point. Registration starts no network work; every tool reads its settings when called."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from freshdesk_lib.client import FreshdeskError  # noqa: E402
from freshdesk_lib.host import HostLink  # noqa: E402
from freshdesk_lib.reads import Reads  # noqa: E402
from freshdesk_lib.sends import Sends  # noqa: E402
from freshdesk_lib.service import KEY_SETTING, Service, read_settings, write_settings  # noqa: E402
from freshdesk_lib.status import place_status  # noqa: E402

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
STRINGS = {"type": "array", "items": {"type": "string"}}
NAME_OR_ID = {"type": ["string", "integer"]}
ACCOUNT = {"type": "string", "description": "The helpdesk address this skill is connected to (freshdesk_status shows it)."}
REQUEST = {"type": "string", "description": "Your stable id for this exact call (1-128 chars). Repeating it returns "
                                            "the recorded outcome and never sends twice."}
TICKET = {"type": "integer", "description": "Ticket number."}
FILES = {"type": "array", "items": {"type": "string"}, "description": "Local file paths to attach (20 MB in total)."}


def _schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def register(api):
    state_dir = Path(api.get_state_dir())

    def key() -> str:
        try:
            return str((api.get_settings([KEY_SETTING]) or {}).get(KEY_SETTING) or "")
        except Exception:  # an ungranted key reads as missing; the tool then says what to grant
            return ""

    host = HostLink(lambda: str(api.get_skill_token().use_in_request() or ""))
    service = Service(state_dir, key, host=host)
    reads, sends = Reads(service), Sends(service)

    def tool(function):
        def handler(ctx=None, **kwargs):
            try:
                result = function(ctx, **kwargs)
            except FreshdeskError as exc:
                result = exc.as_dict()
            except TypeError as exc:
                result = {"ok": False, "code": "arguments_invalid", "message": str(exc)[:300]}
            except (ValueError, OSError) as exc:
                result = {"ok": False, "code": type(exc).__name__, "message": str(exc)[:300]}
            return json.dumps(result, ensure_ascii=False, default=str)
        return handler

    api.register_tool("freshdesk_status", handler=tool(reads.status), description=(
        "The connected Freshdesk helpdesk: its agent (name, scope), the last rate-limit facts, the watcher's state and "
        "gaps, and a journal entry by request_id. probe=true also tests the connection and which endpoints this key "
        "may use; it never tests writes."), schema=_schema({"probe": B, "request_id": S, "account_id": S}))
    api.register_tool("freshdesk_fields", handler=tool(reads.fields), description=(
        "This helpdesk's names for ticket properties: statuses (custom ones too), priorities, sources, types, groups, "
        "agents, products and custom fields with their choices and which are required before Resolved/Closed."),
        schema=_schema({"refresh": B}))
    api.register_tool("freshdesk_find", handler=tool(reads.find), description=(
        "Find tickets. By names (status, priority, group, agent incl. 'me'/'unassigned', type, tag, dates YYYY-MM-DD, "
        "custom fields) it searches; by requester, company, view or updated_since it lists. Search returns at most 300 "
        "tickets, lags a few minutes and cannot search subject or description text."),
        schema=_schema({"account_id": S, "query": S, "status": NAME_OR_ID, "priority": NAME_OR_ID,
                        "group": NAME_OR_ID, "agent": NAME_OR_ID, "type": S, "tag": S, "created_after": S,
                        "created_before": S, "updated_after": S, "updated_before": S, "due_before": S,
                        "custom_fields": {"type": "object"}, "requester_email": S, "requester_id": I,
                        "company_id": I, "view": {"type": "string", "enum": ["new_and_my_open", "watching", "spam", "deleted"]},
                        "updated_since": S, "page": I}))
    api.register_tool("freshdesk_ticket", handler=tool(reads.ticket), description=(
        "One ticket with its whole thread: properties by name, requester, every reply and note with its author. Long "
        "threads come in pages (next_page). stage_attachment_id downloads one attachment to a local file."),
        schema=_schema({"ticket_id": TICKET, "account_id": S, "page": I,
                        "format": {"type": "string", "enum": ["text", "html"]}, "stage_attachment_id": NAME_OR_ID},
                       ["ticket_id"]))
    api.register_tool("freshdesk_knowledge", handler=tool(reads.knowledge), description=(
        "The helpdesk's knowledge base: search articles or read one. canned='folders', 'folder:<id>' or a response id "
        "reads canned responses where this key may (Freshdesk allows that only to administrators' keys)."),
        schema=_schema({"search": S, "article_id": I, "canned": S}))
    api.register_tool("freshdesk_get", handler=tool(reads.get), description=(
        "Any other read of Freshdesk's API v2 (for example contacts/<id>, companies/<id>, tickets/<id>/time_entries, "
        "tickets/<id>/satisfaction_ratings). Large answers are written whole to a file."),
        schema=_schema({"path": S, "params": {"type": "object"}, "account_id": S}, ["path"]))
    api.register_tool("freshdesk_reply", handler=tool(sends.reply), description=(
        "Reply to the ticket's requester: Freshdesk emails it at once. Customers hear only what this tool and "
        "freshdesk_email send; a final answer of longer work on a ticket is posted as a private note instead. To add "
        "recipients use freshdesk_email op=reply."),
        schema=_schema({"account_id": ACCOUNT, "ticket_id": TICKET, "text": S, "request_id": REQUEST, "html": B,
                        "attachment_paths": FILES, "from_email": S},
                       ["account_id", "ticket_id", "text", "request_id"]))
    api.register_tool("freshdesk_note", handler=tool(sends.note), description=(
        "Write a note on a ticket: private (team only) by default, public=true shows it to the requester. To mail "
        "agents about it, give their emails in notify with audience='notify'. edit_note_id edits a note of this ticket."),
        schema=_schema({"account_id": ACCOUNT, "ticket_id": TICKET, "text": S, "request_id": REQUEST, "public": B,
                        "notify": STRINGS, "audience": {"type": "string", "enum": ["ticket", "notify"]},
                        "edit_note_id": I, "html": B, "attachment_paths": FILES},
                       ["account_id", "ticket_id", "text", "request_id"]))
    api.register_tool("freshdesk_email", handler=tool(sends.email), description=(
        "Email with explicit recipients: op=reply (a reply that adds cc/bcc), forward, reply_to_forward, or outbound "
        "(a new ticket that emails one contact; Freshdesk opens it closed)."),
        schema=_schema({"account_id": ACCOUNT, "op": {"type": "string", "enum": ["reply", "forward", "reply_to_forward",
                                                                                   "outbound"]},
                        "text": S, "request_id": REQUEST, "ticket_id": TICKET, "to_emails": STRINGS,
                        "cc_emails": STRINGS, "bcc_emails": STRINGS, "subject": S, "email_config_id": I, "html": B,
                        "attachment_paths": FILES, "from_email": S},
                       ["account_id", "op", "text", "request_id"]))
    api.register_tool("freshdesk_update", handler=tool(sends.update), description=(
        "Change ticket properties by name or id: status, priority, type, group, agent ('me', 'unassigned'), product, "
        "tags_add/tags_remove, custom_fields, due dates, subject. Before Resolved/Closed it checks the fields Freshdesk "
        "requires and writes nothing if one is empty."),
        schema=_schema({"account_id": ACCOUNT, "ticket_id": TICKET, "status": NAME_OR_ID, "priority": NAME_OR_ID,
                        "type": S, "group": NAME_OR_ID, "agent": NAME_OR_ID, "product": NAME_OR_ID,
                        "tags_add": STRINGS, "tags_remove": STRINGS, "custom_fields": {"type": "object"},
                        "due_by": S, "fr_due_by": S, "subject": S}, ["account_id", "ticket_id"]))
    api.register_tool("freshdesk_request", handler=tool(sends.request), description=(
        "Any other change through Freshdesk's API v2 (POST, PUT, DELETE): create a ticket, merge, delete or restore, "
        "watchers, time entries, contacts, companies, article drafts. Calls that carry words to people are refused "
        "with the tool to use. A created ticket may trigger the helpdesk's own notification emails."),
        schema=_schema({"account_id": ACCOUNT, "method": {"type": "string", "enum": ["POST", "PUT", "DELETE"]},
                        "path": S, "body": {"type": "object"}, "request_id": REQUEST},
                       ["account_id", "method", "path", "request_id"]))

    def refresh_watcher() -> None:
        """A key saved in the protected form reaches the running watcher: respawn it only when its key differs."""
        try:
            state = api.get_settings_state([KEY_SETTING])
            if state.get("status") == "ok":
                value = (state.get("values") or {}).get(KEY_SETTING, "")
                api.refresh_companion_process("freshdesk_watcher", {KEY_SETTING: value}, only_if_differs=True)
        except Exception:  # an older host or a closed skill: the next status read tries again
            return

    async def status(request):
        from starlette.responses import JSONResponse
        try:
            await asyncio.to_thread(refresh_watcher)
            value = await asyncio.to_thread(place_status, service, request.query_params.get("account_id", ""))
            return JSONResponse(value)
        except (ValueError, OSError) as exc:
            return JSONResponse({"text": "State unavailable", "tone": "bad", "code": type(exc).__name__}, status_code=503)

    async def settings(request):
        from starlette.responses import JSONResponse
        try:
            if request.method == "GET":
                current = await asyncio.to_thread(read_settings, state_dir)
                return JSONResponse({key: str(value) for key, value in current.items()})
            values = await request.json()
            saved = await asyncio.to_thread(write_settings, state_dir, values if isinstance(values, dict) else {})
            return JSONResponse({**{key: str(value) for key, value in saved.items()}, "saved": True})
        except FreshdeskError as exc:
            return JSONResponse(exc.as_dict(), status_code=400)
        except (ValueError, OSError):
            return JSONResponse({"code": "settings_unwritable"}, status_code=400)

    async def probe(request):
        from starlette.responses import JSONResponse
        try:
            await asyncio.to_thread(refresh_watcher)
            return JSONResponse(json.loads(json.dumps(await asyncio.to_thread(reads.status, None, True), default=str)))
        except FreshdeskError as exc:
            return JSONResponse(exc.as_dict(), status_code=400)

    api.register_companion_process("freshdesk_watcher")
    api.register_route("status", handler=status, methods=("GET",))
    api.register_route("settings", handler=settings, methods=("GET", "POST"))
    api.register_route("probe", handler=probe, methods=("POST",))
    api.register_settings_section("freshdesk", title="Freshdesk", schema={"components": [
        {"type": "markdown", "text": (
            "Connect one Freshdesk helpdesk. Save its address here, then the agent's API key in the skill's protected "
            "key form (ask Ouroboros to set up Freshdesk). Give Ouroboros its own agent: its role and ticket scope "
            "decide what it can do and see. Check connection reads the agent and tests which endpoints the key may "
            "use; it sends nothing.")},
        {"type": "form", "route": "settings", "method": "POST", "submit_label": "Save", "fields": [
            {"name": "domain", "label": "Helpdesk address (example.freshdesk.com)", "type": "text"},
            {"name": "poll_seconds", "label": "Check for changes every (seconds, 30-600)", "type": "text"},
            {"name": "budget_share", "label": "Share of the account's API limit for checking (%)", "type": "text"}]},
        {"type": "action", "route": "probe", "method": "POST", "label": "Check connection", "target": "probe"},
        {"type": "json", "target": "probe"},
    ]})
