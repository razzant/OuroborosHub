"""A loopback stand-in for Freshdesk's REST v2, with invented data on reserved example domains.

It implements only what the skill calls, with the documented shapes (developers.freshdesk.com/api):
agents/me, ticket_fields, tickets (list with updated_since/order_by/include/Link paging, view, create,
update, delete, restore, merge, reply, notes, forward, outbound email), conversations, search (a small
subset of the query grammar), solutions, contacts, companies and admin-only refusals. Faults can be queued
per request: a 429 with Retry-After, a server error, a write that is applied and then left unanswered
(the connection drops), or an answer slower than the client's timeout.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "fixture-api-key"
ME = 9001
CHAT = 7  # a webchat ticket: replies and notes take structured_body, never body


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def form_fields(raw: bytes, content_type: str) -> dict:
    """The plain fields of a multipart body (``name[]`` as lists) and its files as ``_files``."""
    match = re.search(r"boundary=([^;]+)", content_type)
    if not match:
        return {}
    fields: dict = {"_files": []}
    for part in raw.split(b"--" + match.group(1).strip().encode()):
        head, _, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head)
        if not name:
            continue
        value = value[:-2] if value.endswith(b"\r\n") else value
        filename = re.search(rb'filename="([^"]*)"', head)
        key = name.group(1).decode("utf-8")
        if filename:
            fields["_files"].append({"name": filename.group(1).decode("utf-8"), "size": len(value)})
        elif key.endswith("[]"):
            fields.setdefault(key[:-2], []).append(value.decode("utf-8"))
        else:
            fields[key] = value.decode("utf-8")
    return fields


def parse_iso(value: str) -> datetime:
    """``…Z``, with or without fractions of a second."""
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value).astimezone(timezone.utc)


TICKET_FIELDS = [
    {"id": 1, "name": "requester", "label": "Search a requester", "type": "default_requester", "default": True},
    {"id": 2, "name": "subject", "label": "Subject", "type": "default_subject", "default": True},
    {"id": 3, "name": "ticket_type", "label": "Type", "type": "default_ticket_type", "default": True,
     "required_for_closure": False, "choices": ["Question", "Incident", "Problem", "Refund"]},
    {"id": 4, "name": "source", "label": "Source", "type": "default_source", "default": True,
     "choices": {"Email": 1, "Portal": 2, "Phone": 3, "Chat": 7, "Feedback Widget": 9, "Outbound Email": 10}},
    {"id": 5, "name": "status", "label": "Status", "type": "default_status", "default": True,
     "required_for_closure": False,
     "choices": {"2": ["Open", "Being Processed"], "3": ["Pending", "Awaiting your Reply"],
                 "4": ["Resolved", "This ticket has been Resolved"], "5": ["Closed", "This ticket has been Closed"],
                 "6": ["Waiting on Customer", "Awaiting your Reply"], "7": ["Waiting on Third Party", "Being Processed"]}},
    {"id": 6, "name": "priority", "label": "Priority", "type": "default_priority", "default": True,
     "choices": {"Low": 1, "Medium": 2, "High": 3, "Urgent": 4}},
    {"id": 7, "name": "group", "label": "Group", "type": "default_group", "default": True,
     "choices": {"Billing": 11, "Technical": 12}},
    {"id": 8, "name": "agent", "label": "Agent", "type": "default_agent", "default": True,
     "choices": {"Desk Agent": ME, "Team Lead": 9002}},
    {"id": 9, "name": "product", "label": "Product", "type": "default_product", "default": True,
     "choices": {"Example App": 21}},
    {"id": 10, "name": "description", "label": "Description", "type": "default_description", "default": True},
    {"id": 20, "name": "cf_refund_reason", "label": "Refund reason", "type": "custom_dropdown", "default": False,
     "required_for_closure": True, "choices": ["Duplicate charge", "Not as described", "Other"]},
    {"id": 21, "name": "cf_order_number", "label": "Order number", "type": "custom_text", "default": False,
     "required_for_closure": False},
    {"id": 22, "name": "cf_category", "label": "Category", "type": "nested_field", "default": False,
     "required_for_closure": False, "choices": [{"Hardware": [{"Laptop": ["Screen", "Battery"]}, {"Printer": []}]}],
     "nested_ticket_fields": [{"id": 5, "name": "cf_subcategory", "label": "Subcategory", "level": 2, "ticket_field_id": 22},
                              {"id": 6, "name": "cf_item", "label": "Item", "level": 3, "ticket_field_id": 22}]},
]


class FakeFreshdesk:
    """In-memory helpdesk. ``admin`` decides whether admin-only endpoints answer."""

    def __init__(self, *, admin: bool = False, start: datetime | None = None):
        self.lock = threading.RLock()
        self.admin = admin
        self.clock = start or datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
        self.tickets: dict[int, dict] = {}
        self.conversations: dict[int, list[dict]] = {}
        self.deleted: set[int] = set()
        self.contacts = {501: {"id": 501, "name": "Customer One", "email": "customer.one@example.com", "company_id": 601},
                         502: {"id": 502, "name": "Customer Two", "email": "customer.two@example.org", "company_id": None}}
        self.companies = {601: {"id": 601, "name": "Example Shop", "domains": ["example.com"]}}
        self.agents = {ME: {"id": ME, "contact": {"name": "Desk Agent", "email": "desk.agent@example.net"}},
                       9002: {"id": 9002, "contact": {"name": "Team Lead", "email": "team.lead@example.net"}}}
        self.articles = {701: {"id": 701, "title": "How refunds work", "status": 2, "folder_id": 801,
                               "description": "<p>Refunds reach the card in 5 to 10 business days.</p>",
                               "description_text": "Refunds reach the card in 5 to 10 business days."}}
        self.next_ticket, self.next_entry = 1001, 50001
        self.faults: list[dict] = []
        self.requests: list[dict] = []
        self.rate_total, self.rate_used = 100, 0
        self.server = None
        self.thread = None

    # -- helpers used by tests ----------------------------------------------------------------------
    def tick(self, seconds: int = 1) -> datetime:
        """Advance the helpdesk clock: at least ``seconds``, and never behind the real time of this test."""
        with self.lock:
            self.clock = max(self.clock + timedelta(seconds=seconds),
                             datetime.now(timezone.utc).replace(microsecond=0))
            return self.clock

    def settle(self, margin: float = 1.1) -> None:
        """Wait until real time is past the helpdesk clock (several changes in one second run it ahead)."""
        while datetime.now(timezone.utc) < self.clock + timedelta(seconds=margin):
            time.sleep(0.1)

    def iso_now(self) -> str:
        """Advance the clock one step and return it, for tests that change a ticket by hand."""
        return iso(self.tick())

    def add_ticket(self, *, subject: str, description: str, requester_id: int = 501, status: int = 2,
                   priority: int = 1, responder_id=None, group_id=None, source: int = 1, tags=(), cc_emails=()) -> dict:
        with self.lock:
            moment = self.tick()
            ticket_id = self.next_ticket
            self.next_ticket += 1
            ticket = {"id": ticket_id, "subject": subject, "description": f"<div>{description}</div>",
                      "description_text": description, "requester_id": requester_id, "status": status,
                      "priority": priority, "responder_id": responder_id, "group_id": group_id, "source": source,
                      "type": None, "product_id": None, "company_id": self.contacts.get(requester_id, {}).get("company_id"),
                      "tags": list(tags), "cc_emails": list(cc_emails), "fwd_emails": [], "reply_cc_emails": list(cc_emails),
                      "custom_fields": {"cf_refund_reason": None, "cf_order_number": None},
                      "created_at": iso(moment), "updated_at": iso(moment), "due_by": iso(moment + timedelta(days=1)),
                      "fr_due_by": iso(moment + timedelta(hours=4)), "is_escalated": False, "attachments": [],
                      "email_config_id": 4001}
            self.tickets[ticket_id] = ticket
            self.conversations[ticket_id] = []
            return ticket

    def add_entry(self, ticket_id: int, *, body: str, user_id: int, incoming: bool, private: bool = False,
                  source: int = 0, to_emails=(), attachments=(), bump: bool = True) -> dict:
        with self.lock:
            moment = self.tick()
            entry = {"id": self.next_entry, "body": f"<div>{body}</div>", "body_text": body, "incoming": incoming,
                     "private": private, "user_id": user_id, "support_email": None, "source": source,
                     "ticket_id": ticket_id, "to_emails": list(to_emails), "from_email": None, "cc_emails": [],
                     "bcc_emails": [], "attachments": list(attachments), "created_at": iso(moment), "updated_at": iso(moment)}
            self.next_entry += 1
            self.conversations[ticket_id].append(entry)
            if bump:
                self.tickets[ticket_id]["updated_at"] = iso(moment)
            return entry

    def fault(self, method: str, path_regex: str, kind: str, **extra) -> None:
        """Queue one fault for the next matching request: ``rate``, ``server``, ``drop_after_apply``, ``slow``."""
        with self.lock:
            self.faults.append({"method": method, "path": re.compile(path_regex), "kind": kind, **extra})

    # -- server ---------------------------------------------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                return

            def do_GET(self):
                fake._serve(self, "GET")

            def do_POST(self):
                fake._serve(self, "POST")

            def do_PUT(self):
                fake._serve(self, "PUT")

            def do_DELETE(self):
                fake._serve(self, "DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def _serve(self, handler, method: str) -> None:
        parts = urllib.parse.urlsplit(handler.path)
        query = urllib.parse.parse_qs(parts.query)
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = {}
        if raw and "json" in (handler.headers.get("Content-Type") or ""):
            body = json.loads(raw.decode("utf-8"))
        elif raw:
            body = {**form_fields(raw, handler.headers.get("Content-Type") or ""), "_multipart": raw}
        with self.lock:
            self.requests.append({"method": method, "path": parts.path, "query": query, "body": body})
            self.rate_used += 1
            fault = next((f for f in self.faults if f["method"] == method and f["path"].search(parts.path)), None)
            if fault:
                self.faults.remove(fault)
        expected = "Basic " + base64.b64encode(f"{KEY}:X".encode()).decode()
        if handler.headers.get("Authorization") != expected:
            return self._answer(handler, 401, {"code": "invalid_credentials", "message": "You have to be logged in."})
        if fault and fault["kind"] == "rate":
            return self._answer(handler, 429, {"message": "Rate limit exceeded"},
                                {"Retry-After": str(fault.get("retry_after", 2))})
        if fault and fault["kind"] == "server":
            return self._answer(handler, 500, {"message": "Internal error"})
        if fault and fault["kind"] == "slow":
            time.sleep(fault.get("seconds", 3))
        try:
            status, payload, headers = self._route(method, parts.path, query, body)
        except KeyError:
            status, payload, headers = 404, {"message": "Not found"}, {}
        if fault and fault["kind"] == "drop_after_apply":
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            handler.close_connection = True
            return None
        return self._answer(handler, status, payload, headers)

    def _answer(self, handler, status, payload, headers=None):
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("X-RateLimit-Total", str(self.rate_total))
        handler.send_header("X-RateLimit-Remaining", str(max(0, self.rate_total - self.rate_used)))
        handler.send_header("X-RateLimit-Used-CurrentRequest", "1")
        handler.send_header("X-RateLimit-Account-Total", str(self.rate_total))
        handler.send_header("X-RateLimit-Account-Remaining", str(max(0, self.rate_total - self.rate_used)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        if data:
            handler.wfile.write(data)
        return None

    # -- routes ----------------------------------------------------------------------------------------
    def _route(self, method, path, query, body):
        with self.lock:
            p = path[len("/api/v2/"):] if path.startswith("/api/v2/") else path
            one = lambda name: (query.get(name) or [None])[0]  # noqa: E731
            if method == "GET" and p == "agents/me":
                return 200, {**self.agents[ME], "occasional": False, "type": "support_agent", "ticket_scope": 1,
                             "group_ids": [11], "role_ids": [3], "available": True}, {}
            if method == "GET" and p in ("ticket_fields",):
                return 200, TICKET_FIELDS, {}
            if p in ("groups", "agents", "roles", "canned_response_folders") and not self.admin:
                return 403, {"code": "access_denied", "message": "You are not authorized to perform this action."}, {}
            if method == "GET" and p == "canned_response_folders":
                return 200, [{"id": 901, "name": "Refunds", "responses_count": 1}], {}
            if method == "GET" and p == "tickets":
                return self._list_tickets(query, path)
            if method == "POST" and p == "tickets":
                return 201, self._create(body), {}
            if method == "POST" and p == "tickets/outbound_email":
                if not body.get("email_config_id"):  # mandatory per Freshdesk's reference
                    return 400, {"description": "Validation failed", "errors": [
                        {"field": "email_config_id", "message": "It should not be blank", "code": "missing_field"}]}, {}
                ticket = self._create({**body, "status": body.get("status", 5), "source": 10})
                ticket["responder_id"] = ME
                return 201, ticket, {}
            if method == "PUT" and p == "tickets/merge":
                for ticket_id in body.get("ticket_ids", []):
                    self.tickets[int(ticket_id)]["status"] = 5
                return 204, None, {}
            edit = re.fullmatch(r"conversations/(\d+)", p)
            if method == "PUT" and edit:
                entry = next((e for rows in self.conversations.values() for e in rows if e["id"] == int(edit.group(1))), None)
                if entry is None:
                    return 404, {"message": "Not found"}, {}
                text = re.sub(r"<[^>]+>", "", str(body.get("body") or ""))
                moment = iso(self.tick())
                added = [{"id": 71000 + n, "name": f["name"], "size": f["size"], "content_type": "text/plain"}
                         for n, f in enumerate(body.get("_files") or [])]
                entry.update(body=f"<div>{text}</div>", body_text=text, updated_at=moment, last_edited_at=moment,
                             last_edited_user_id=ME, attachments=list(entry.get("attachments") or []) + added)
                self.tickets[entry["ticket_id"]]["updated_at"] = moment
                return 200, dict(entry), {}
            match = re.fullmatch(r"tickets/(\d+)(?:/(\w+))?", p)
            if match:
                return self._ticket_route(method, int(match.group(1)), match.group(2), body, query, path)
            if method == "GET" and p == "search/tickets":
                return self._search(one("query") or "")
            if method == "GET" and p == "search/solutions":
                term = (one("term") or "").lower()
                return 200, [a for a in self.articles.values() if term in (a["title"] + a["description_text"]).lower()], {}
            match = re.fullmatch(r"solutions/articles/(\d+)", p)
            if method == "GET" and match:
                return 200, self.articles[int(match.group(1))], {}
            match = re.fullmatch(r"contacts/(\d+)", p)
            if method == "GET" and match:
                return 200, self.contacts[int(match.group(1))], {}
            if method == "GET" and p == "contacts":
                email = (one("email") or "").lower()
                return 200, [c for c in self.contacts.values() if not email or c["email"] == email], {}
            match = re.fullmatch(r"companies/(\d+)", p)
            if method == "GET" and match:
                return 200, self.companies[int(match.group(1))], {}
            raise KeyError(p)

    def _create(self, body):
        requester = next((c["id"] for c in self.contacts.values() if c["email"] == body.get("email")), None)
        if requester is None and body.get("email"):
            requester = max(self.contacts) + 1
            self.contacts[requester] = {"id": requester, "name": body.get("name") or body["email"],
                                        "email": body["email"], "company_id": None}
        ticket = self.add_ticket(subject=body.get("subject", ""), description=body.get("description", ""),
                                 requester_id=body.get("requester_id", requester), status=body.get("status", 2),
                                 priority=body.get("priority", 1), responder_id=body.get("responder_id"),
                                 group_id=body.get("group_id"), source=body.get("source", 2), tags=body.get("tags", ()))
        return ticket

    def _list_tickets(self, query, path):
        one = lambda name: (query.get(name) or [None])[0]  # noqa: E731
        per_page = int(one("per_page") or 30)
        if per_page > 100:
            return 400, {"description": "Validation failed", "errors": [{"field": "per_page", "code": "invalid_value"}]}, {}
        page = int(one("page") or 1)
        since = one("updated_since")
        rows = [t for t in self.tickets.values() if t["id"] not in self.deleted]
        if since:
            rows = [t for t in rows if parse_iso(t["updated_at"]) >= parse_iso(since)]
        else:
            rows = [t for t in rows if parse_iso(t["created_at"]) >= self.clock - timedelta(days=30)]
        key = one("order_by") or "created_at"
        rows.sort(key=lambda t: (t[key], t["id"]), reverse=(one("order_type") or "desc") == "desc")
        chunk = rows[(page - 1) * per_page: page * per_page]
        include = one("include") or ""
        out = []
        for ticket in chunk:
            row = {k: v for k, v in ticket.items() if k not in ("description", "description_text")}
            if "description" in include:
                row["description"], row["description_text"] = ticket["description"], ticket["description_text"]
            out.append(row)
        headers = {}
        if len(rows) > page * per_page:
            following = urllib.parse.urlencode({**{k: v[0] for k, v in query.items()}, "page": page + 1})
            headers["Link"] = f'<http://127.0.0.1:{self.server.server_address[1]}{path}?{following}>; rel="next"'
        return 200, out, headers

    def _ticket_route(self, method, ticket_id, action, body, query, path):
        if ticket_id not in self.tickets or (ticket_id in self.deleted and action != "restore"):
            return 404, {"message": "Ticket not found"}, {}
        ticket = self.tickets[ticket_id]
        if method == "GET" and action is None:
            return 200, dict(ticket), {}
        if method == "PUT" and action is None:
            status = body.get("status")
            if status is not None and str(status) not in {"2", "3", "4", "5", "6", "7"}:
                return 400, {"description": "Validation failed",
                             "errors": [{"field": "status", "message": "It should be one of these values", "code": "invalid_value"}]}, {}
            if status in (4, 5) and not {**ticket["custom_fields"], **body.get("custom_fields", {})}.get("cf_refund_reason"):
                return 400, {"description": "Validation failed",
                             "errors": [{"field": "cf_refund_reason", "message": "It should not be blank as this is a mandatory field", "code": "invalid_value"}]}, {}
            for key, value in body.items():
                if key == "custom_fields":
                    ticket["custom_fields"].update(value)
                elif key == "tags":
                    ticket["tags"] = list(value)
                else:
                    ticket[key] = value
            ticket["updated_at"] = iso(self.tick())
            return 200, dict(ticket), {}
        if method == "DELETE" and action is None:
            self.deleted.add(ticket_id)
            return 204, None, {}
        if method == "PUT" and action == "restore":
            self.deleted.discard(ticket_id)
            return 204, None, {}
        if method == "GET" and action == "conversations":
            one = lambda name: (query.get(name) or [None])[0]  # noqa: E731
            per_page, page = int(one("per_page") or 30), int(one("page") or 1)
            if per_page > 100:
                return 400, {"description": "Validation failed", "errors": [{"field": "per_page", "code": "invalid_value"}]}, {}
            rows = self.conversations[ticket_id]
            headers = {}
            if len(rows) > page * per_page:
                following = urllib.parse.urlencode({"per_page": per_page, "page": page + 1})
                headers["Link"] = f'<http://127.0.0.1:{self.server.server_address[1]}{path}?{following}>; rel="next"'
            return 200, [dict(r) for r in rows[(page - 1) * per_page: page * per_page]], headers
        if method == "POST" and action in ("reply", "notes", "forward"):
            if ticket["source"] == CHAT and action != "forward" and "body" in body:
                return 400, {"description": "Validation failed", "errors": [{
                    "field": "body", "code": "invalid_value",
                    "message": "Use structured_body for a webchat or mobile SDK ticket"}]}, {}
            text = re.sub(r"<[^>]+>", "", str(body.get("body") or ""))
            if "structured_body" in body:
                structured = body["structured_body"]
                structured = json.loads(structured) if isinstance(structured, str) else structured
                text = " ".join(part["data"]["content"] for part in structured["body_contents"])
            files = [{"id": 70000 + n, "name": f["name"], "size": f["size"], "content_type": "text/plain"}
                     for n, f in enumerate(body.get("_files") or [])]
            user = int(body.get("user_id", ME))
            if action == "notes":
                private = body.get("private", True)
                entry = self.add_entry(ticket_id, body=text, user_id=user, incoming=False,
                                       private=private if isinstance(private, bool) else private == "true", source=2,
                                       to_emails=body.get("notify_emails", ()), attachments=files)
            elif action == "forward":
                entry = self.add_entry(ticket_id, body=text, user_id=user, incoming=False,
                                       private=True, source=8, to_emails=body.get("to_emails", ()), attachments=files)
            else:
                requester = self.contacts.get(ticket["requester_id"], {})
                entry = self.add_entry(ticket_id, body=text, user_id=user, incoming=False,
                                       private=False, source=0, to_emails=[requester.get("email")], attachments=files)
            return 201, dict(entry), {}
        raise KeyError(action)

    def _search(self, query):
        query = query.strip()
        if not (query.startswith('"') and query.endswith('"')):
            return 400, {"description": "Validation failed", "errors": [{"field": "query", "code": "invalid_value"}]}, {}
        clauses = [c.strip() for c in query.strip('"').split(" AND ")]
        rows = [t for t in self.tickets.values() if t["id"] not in self.deleted]
        for clause in clauses:
            name, _, value = clause.partition(":")
            value = value.strip("'")
            key = {"agent_id": "responder_id"}.get(name, name)
            if name == "tag":
                rows = [t for t in rows if value in t["tags"]]
            elif value == "null":
                rows = [t for t in rows if t.get(key) is None]
            else:
                rows = [t for t in rows if str(t.get(key)) == value]
        return 200, {"results": [dict(t) for t in rows[:30]], "total": len(rows)}, {}
