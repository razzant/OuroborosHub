"""Read tools: status, fields, find, ticket, knowledge base, any GET. None of them changes anything."""
from __future__ import annotations

import json
import re

from .client import FreshdeskError, download
from .service import INLINE_LIMIT, Service
from .store import now_iso
from .text import html_to_text

SEARCH_CAP_NOTE = ("Freshdesk search returns at most 300 tickets (10 pages of 30), indexes changes after a few minutes, "
                   "leaves out archived tickets, and cannot search subject or description text.")
THREAD_CHAR_BOUND = 60_000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _quote(value) -> str:
    return "'" + str(value).replace("'", "") + "'"


def ticket_url(host: str, ticket_id) -> str:
    return f"https://{host}/a/tickets/{ticket_id}"


class Reads:
    def __init__(self, service: Service):
        self.service = service

    # -- freshdesk_status -------------------------------------------------------------------------------
    def status(self, ctx=None, probe: bool = False, request_id: str = "", account_id: str = "") -> dict:
        settings = self.service.settings()
        store = self.service.store()
        try:
            out = {"ok": True, "helpdesk": settings["domain"] or None, "settings": settings,
                   "agent": _public_agent(store.meta("agent")), "agent_change": store.meta("agent_changed"),
                   "limits": store.meta("limits"), "watcher": store.meta("watcher"), "gaps": store.gaps(),
                   "skipped_tickets": store.meta("skipped_tickets"), "connection_ended": store.meta("connection_ended"),
                   "detection_note": "New replies and notes are found when Freshdesk moves the ticket's updated_at; "
                                     "a change reverted between two polls is not seen."}
            if request_id:
                out["journal"] = store.journal(request_id)
            if not settings["domain"]:
                out.update(ok=False, code="not_configured",
                           next_step="Save the helpdesk address (example.freshdesk.com) in Settings → Freshdesk, then "
                                     "save the agent's API key in the skill's protected key form.")
                return out
            if probe:
                out["probe"] = self._probe(store)
                out["agent"] = _public_agent(store.meta("agent"))
                out["limits"] = store.meta("limits")
            return out
        finally:
            store.close()

    def _probe(self, store) -> dict:
        try:
            client = self.service.client()
        except FreshdeskError as exc:
            return exc.as_dict()
        result = {"observed_at": now_iso(), "writes_tested": False}
        try:
            result["agent"] = _public_agent(self.service.agent(client, store, refresh=True))
        except FreshdeskError as exc:
            return {**result, "agent": exc.as_dict()}
        checks = {"ticket_fields": ("ticket_fields", None), "tickets": ("tickets", {"per_page": 1}),
                  "knowledge_base": ("solutions/categories", None), "canned_responses": ("canned_response_folders", None),
                  "groups": ("groups", None), "agents_list": ("agents", {"per_page": 1})}
        answers = {}
        for name, (path, params) in checks.items():
            try:
                response = client.get(path, params)
                self.service.note_limits(store, response.limits)
                answers[name] = "answers"
            except FreshdeskError as exc:
                answers[name] = "admin_only" if exc.status == 403 else exc.code
        result["endpoints"] = answers
        result["limits"] = store.meta("limits")
        return result

    # -- freshdesk_fields -------------------------------------------------------------------------------
    def fields(self, ctx=None, refresh: bool = False) -> dict:
        client = self.service.client()
        store = self.service.store()
        try:
            catalog = self.service.catalog(client, store, refresh=bool(refresh))
            return {"ok": True, "helpdesk": client.host, **catalog.describe()}
        finally:
            store.close()

    # -- freshdesk_find ---------------------------------------------------------------------------------
    def find(self, ctx=None, account_id: str = "", query: str = "", status=None, priority=None, group=None,
             agent=None, type=None, tag=None, created_after=None, created_before=None, updated_after=None,
             updated_before=None, due_before=None, custom_fields=None, requester_email=None, requester_id=None,
             company_id=None, view=None, updated_since=None, page: int = 1) -> dict:
        client = self.service.client()
        self.service.check_account(client, account_id)
        store = self.service.store()
        try:
            catalog = self.service.catalog(client, store)
            page = max(1, int(page or 1))
            listing = any(v not in (None, "") for v in (requester_email, requester_id, company_id, view, updated_since))
            if listing:
                params = {"email": requester_email, "requester_id": requester_id, "company_id": company_id,
                          "filter": view, "updated_since": updated_since, "order_by": "updated_at",
                          "order_type": "desc", "per_page": 30, "page": page}
                response = client.get("tickets", params)
                rows, total, note = response.data or [], None, ("Without updated_since Freshdesk lists only tickets "
                                                                "created in the last 30 days.")
            else:
                text = query.strip() if query else self._compile(catalog, client, store, status=status,
                                                                  priority=priority, group=group, agent=agent,
                                                                  type=type, tag=tag, created_after=created_after,
                                                                  created_before=created_before,
                                                                  updated_after=updated_after,
                                                                  updated_before=updated_before, due_before=due_before,
                                                                  custom_fields=custom_fields)
                if not text:
                    raise FreshdeskError("query_empty", "Give a filter (status, priority, group, agent, tag…), a "
                                         "requester, or a Freshdesk search query.")
                text = text if text.startswith('"') else f'"{text}"'
                response = client.get("search/tickets", {"query": text, "page": page})
                data = response.data if isinstance(response.data, dict) else {}
                rows, total, note = data.get("results") or [], data.get("total"), SEARCH_CAP_NOTE
            self.service.note_limits(store, response.limits)
            return {"ok": True, "helpdesk": client.host, "page": page, "total": total, "note": note,
                    "tickets": [self._row(catalog, client.host, t) for t in rows if isinstance(t, dict)],
                    "more": bool(response.next_url) or (total is not None and page * 30 < min(total, 300))}
        finally:
            store.close()

    def _compile(self, catalog, client, store, **filters) -> str:
        clauses = []
        for name in ("status", "priority"):
            if filters.get(name) not in (None, ""):
                clauses.append(f"{name}:{catalog.resolve(name, filters[name])[1]}")
        if filters.get("group") not in (None, ""):
            clauses.append(f"group_id:{catalog.resolve('group', filters['group'])[1]}")
        agent = filters.get("agent")
        if agent not in (None, ""):
            if str(agent).lower() in ("unassigned", "none", "null"):
                clauses.append("agent_id:null")
            elif str(agent).lower() == "me":
                clauses.append(f"agent_id:{self.service.agent(client, store)['id']}")
            else:
                clauses.append(f"agent_id:{catalog.resolve('agent', agent)[1]}")
        if filters.get("type") not in (None, ""):
            clauses.append(f"type:{_quote(catalog.resolve('type', filters['type'])[1])}")
        if filters.get("tag") not in (None, ""):
            clauses.append(f"tag:{_quote(filters['tag'])}")
        for key, field, op in (("created_after", "created_at", ">"), ("created_before", "created_at", "<"),
                               ("updated_after", "updated_at", ">"), ("updated_before", "updated_at", "<"),
                               ("due_before", "due_by", "<")):
            value = filters.get(key)
            if value not in (None, ""):
                if not _DATE.match(str(value)):
                    raise FreshdeskError("date_invalid", f"{key} takes a date as YYYY-MM-DD.")
                clauses.append(f"{field}:{op}{_quote(value)}")
        for name, value in catalog.resolve_custom(filters.get("custom_fields") or {}).items():
            if isinstance(value, bool):
                literal = "true" if value else "false"
            elif isinstance(value, (int, float)):
                literal = str(value)
            else:
                literal = _quote(value)
            clauses.append(f"{name}:{literal}")
        return " AND ".join(clauses)

    @staticmethod
    def _row(catalog, host, ticket) -> dict:
        return {"id": ticket.get("id"), "subject": ticket.get("subject"),
                "status": catalog.named(ticket.get("status"), "status"),
                "priority": catalog.named(ticket.get("priority"), "priority"),
                "group": catalog.named(ticket.get("group_id"), "group"),
                "agent": catalog.named(ticket.get("responder_id"), "agent"),
                "type": ticket.get("type"), "requester_id": ticket.get("requester_id"), "tags": ticket.get("tags") or [],
                "created_at": ticket.get("created_at"), "updated_at": ticket.get("updated_at"),
                "due_by": ticket.get("due_by"), "url": ticket_url(host, ticket.get("id"))}

    # -- freshdesk_ticket -------------------------------------------------------------------------------
    def ticket(self, ctx=None, ticket_id=None, account_id: str = "", page: int = 1, format: str = "text",
               stage_attachment_id=None) -> dict:
        if ticket_id in (None, ""):
            raise FreshdeskError("ticket_id_missing", "Name the ticket by its number.")
        client = self.service.client()
        self.service.check_account(client, account_id)
        store = self.service.store()
        try:
            catalog = self.service.catalog(client, store)
            response = client.get(f"tickets/{int(ticket_id)}", {"include": "requester,stats"})
            self.service.note_limits(store, response.limits)
            ticket = response.data if isinstance(response.data, dict) else {}
            names = self.service.agent_names(catalog)
            entries, next_page, cursor, used = [], None, max(1, int(page or 1)), 0
            conversation = client.get(f"tickets/{int(ticket_id)}/conversations", {"per_page": 100, "page": cursor})
            while True:
                self.service.note_limits(store, conversation.limits)
                for entry in conversation.data or []:
                    shaped = self._entry(entry, names, ticket, format)
                    entries.append(shaped)
                    used += len(shaped.get("text") or "")
                if not conversation.next_url:
                    break
                if used > THREAD_CHAR_BOUND:
                    next_page = cursor + 1
                    break
                cursor += 1
                conversation = client.next_page(conversation)
            out = {"ok": True, "helpdesk": client.host, "ticket": self._ticket(catalog, client.host, ticket, format),
                   "entries": entries, "complete": next_page is None}
            if next_page:
                out["next_page"] = next_page
                out["note"] = f"The thread continues: call again with page={next_page}."
            if stage_attachment_id not in (None, ""):
                out["staged"] = self._stage(ticket, attachment_id=stage_attachment_id, ticket_id=ticket_id)
            return out
        finally:
            store.close()

    def _ticket(self, catalog, host, ticket, format) -> dict:
        requester = ticket.get("requester") or {}
        description = ticket.get("description") if format == "html" else (
            ticket.get("description_text") or html_to_text(ticket.get("description") or ""))
        return {**self._row(catalog, host, ticket), "description": description,
                "requester": {"id": ticket.get("requester_id"), "name": requester.get("name"),
                              "email": requester.get("email")},
                "source": catalog.named(ticket.get("source"), "source"),
                "product": catalog.named(ticket.get("product_id"), "product"),
                "company_id": ticket.get("company_id"), "email_config_id": ticket.get("email_config_id"),
                "cc_emails": ticket.get("cc_emails") or [],
                "custom_fields": ticket.get("custom_fields") or {}, "fr_due_by": ticket.get("fr_due_by"),
                "stats": ticket.get("stats"), "attachments": [_attachment(a) for a in ticket.get("attachments") or []]}

    @staticmethod
    def _entry(entry, names, ticket, format) -> dict:
        user = str(entry.get("user_id") or "")
        is_agent = user in names
        author = {"id": user, "kind": "agent" if is_agent else "contact",
                  "name": names.get(user) or ((ticket.get("requester") or {}).get("name")
                                              if user == str(ticket.get("requester_id")) else None)}
        text = entry.get("body") if format == "html" else (entry.get("body_text") or html_to_text(entry.get("body") or ""))
        kind = {0: "reply", 2: "note", 8: "forward"}.get(entry.get("source"), "email" if entry.get("incoming") else "other")
        return {"id": entry.get("id"), "kind": kind, "private": entry.get("private"), "incoming": entry.get("incoming"),
                "author": author, "created_at": entry.get("created_at"), "edited_at": entry.get("last_edited_at"),
                "to_emails": entry.get("to_emails") or [], "cc_emails": entry.get("cc_emails") or [],
                "text": text, "attachments": [_attachment(a) for a in entry.get("attachments") or []]}

    def _stage(self, ticket, *, attachment_id, ticket_id) -> dict:
        # Links expire: read the ticket's raw attachment records again, then fetch without the key.
        client = self.service.client()
        raw = list(ticket.get("attachments") or [])
        page = client.get(f"tickets/{int(ticket_id)}/conversations", {"per_page": 100})
        while True:
            for entry in page.data or []:
                raw.extend(entry.get("attachments") or [])
            if not page.next_url:
                break
            page = client.next_page(page)
        found = next((a for a in raw if str(a.get("id")) == str(attachment_id)), None)
        if not found or not found.get("attachment_url"):
            raise FreshdeskError("attachment_not_found", f"Ticket {ticket_id} has no attachment {attachment_id}.")
        if int(found.get("file_size") or found.get("size") or 0) > MAX_ATTACHMENT_BYTES:
            raise FreshdeskError("attachment_too_large", "The attachment is larger than 20 MB.")
        data, content_type = download(found["attachment_url"], MAX_ATTACHMENT_BYTES)
        return {**self.service.job_file(f"ticket-{ticket_id}", found.get("name") or "attachment", data),
                "name": found.get("name"), "content_type": content_type}

    # -- freshdesk_knowledge ----------------------------------------------------------------------------
    def knowledge(self, ctx=None, search: str = "", article_id=None, canned=None) -> dict:
        client = self.service.client()
        store = self.service.store()
        try:
            if article_id not in (None, ""):
                response = client.get(f"solutions/articles/{int(article_id)}")
                data = response.data or {}
                result = {"article": {"id": data.get("id"), "title": data.get("title"), "status": data.get("status"),
                                      "folder_id": data.get("folder_id"), "updated_at": data.get("updated_at"),
                                      "text": data.get("description_text") or html_to_text(data.get("description") or "")}}
            elif canned not in (None, ""):
                try:
                    if str(canned) == "folders":
                        response = client.get("canned_response_folders")
                    elif str(canned).startswith("folder:"):
                        response = client.get(f"canned_response_folders/{int(str(canned)[7:])}/responses")
                    else:
                        response = client.get(f"canned_responses/{int(canned)}")
                except FreshdeskError as exc:
                    if exc.status == 403:
                        return {"ok": False, "code": "admin_only", "message": "Freshdesk lets only an administrator's "
                                "key read canned responses through the API. Search the knowledge base instead."}
                    raise
                result = {"canned": response.data}
            elif search:
                response = client.get("search/solutions", {"term": search})
                result = {"articles": [{"id": a.get("id"), "title": a.get("title"), "status": a.get("status"),
                                        "folder_id": a.get("folder_id"),
                                        "excerpt": (a.get("description_text") or html_to_text(a.get("description") or ""))[:600]}
                                       for a in (response.data or []) if isinstance(a, dict)]}
            else:
                raise FreshdeskError("knowledge_query_missing", "Give search, article_id or canned.")
            self.service.note_limits(store, response.limits)
            return {"ok": True, "helpdesk": client.host, **result}
        finally:
            store.close()

    # -- freshdesk_get ----------------------------------------------------------------------------------
    def get(self, ctx=None, path: str = "", params=None, account_id: str = "") -> dict:
        client = self.service.client()
        self.service.check_account(client, account_id)
        store = self.service.store()
        try:
            response = client.get(path, params if isinstance(params, dict) else None)
            self.service.note_limits(store, response.limits)
            text = json.dumps(response.data, ensure_ascii=False)
            out = {"ok": True, "status": response.status, "next_page_url": response.next_url or None}
            if len(text) <= INLINE_LIMIT:
                out["data"] = response.data
            else:
                out["file"] = self.service.job_file("get", "answer.json", text.encode("utf-8"))
                out["note"] = f"The answer is {len(text)} characters; it was written to the file above in full."
            return out
        finally:
            store.close()


def _attachment(record: dict) -> dict:
    return {"id": record.get("id"), "name": record.get("name"), "content_type": record.get("content_type"),
            "size": record.get("file_size") or record.get("size")}


def _public_agent(agent):
    if not agent:
        return None
    return {key: agent.get(key) for key in ("id", "name", "email", "occasional", "type", "scope", "group_ids")}
