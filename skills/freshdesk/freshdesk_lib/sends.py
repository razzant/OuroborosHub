"""Tools that change Freshdesk: words to people (reply, note, email) and other changes (update, request).

A call is journalled under its ``request_id`` before the HTTP request. The same id with the same arguments
returns the recorded outcome; with other arguments it is a conflict. A refusal that sent nothing frees the id.
When Freshdesk may have applied a call but did not answer, one readback looks for it; without proof the call
stays ``uncertain`` and is never sent again under that id: the mind reads the ticket and decides.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import NOT_SENT, RATE_LIMITED, UNKNOWN, FreshdeskError, canonical_path
from .events import parse_time
from .host import HostUnavailable
from .reads import ticket_url
from .service import Service
from .store import digest
from .text import normalized, to_html

MAX_ATTACHMENTS_BYTES = 20 * 1024 * 1024
EMAIL_OPS = ("reply", "forward", "reply_to_forward", "outbound")
# Calls that carry words go through the typed tools, whose effect is "message"; the generic writer refuses them.
SPEECH_ROUTES = (
    (("POST",), re.compile(r"^tickets/\d+/reply$"), "freshdesk_reply (or freshdesk_email op=reply to add recipients)"),
    (("POST",), re.compile(r"^tickets/\d+/notes$"), "freshdesk_note"),
    (("POST",), re.compile(r"^tickets/\d+/(forward|reply_to_forward)$"), "freshdesk_email"),
    (("POST",), re.compile(r"^tickets/outbound_email$"), "freshdesk_email op=outbound"),
    (("PUT",), re.compile(r"^conversations/\d+$"), "freshdesk_note with edit_note_id"),
    (("POST", "PUT"), re.compile(r"^tickets/\d+/summary$"), None),
)


def _task_id(ctx) -> str:
    value = ctx.get("task_id") if isinstance(ctx, dict) else getattr(ctx, "task_id", None)
    return value if isinstance(value, str) else ""


def _request_id(value) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 128:
        raise FreshdeskError("request_id_required", "Give a request_id (1–128 characters) that you keep for this exact "
                             "message; repeating it repeats nothing, it returns the recorded outcome.")
    return value


def _files(paths) -> list:
    files, total = [], 0
    for raw in paths or []:
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            raise FreshdeskError("attachment_missing", f"No file at {raw}.")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_ATTACHMENTS_BYTES:
            raise FreshdeskError("attachments_too_large", "Attachments of one call may total at most 20 MB "
                                 "(Freshdesk counts every attachment of the ticket against 20 MB).")
        files.append(("attachments[]", path.name, data, mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
    return files


def _form(body: dict) -> list:
    fields = []
    for key, value in body.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            fields.extend((f"{key}[]", item) for item in value)
        elif isinstance(value, bool):
            fields.append((key, "true" if value else "false"))
        elif isinstance(value, dict):
            fields.append((key, json.dumps(value, ensure_ascii=False)))  # e.g. structured_body, as Freshdesk documents it
        else:
            fields.append((key, value))
    return fields


def _contains_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(v, key) for v in value.values())
    if isinstance(value, list):
        return any(_contains_key(v, key) for v in value)
    return False


class Sends:
    def __init__(self, service: Service):
        self.service = service

    # -- the shared path ----------------------------------------------------------------------------------
    def _gate(self, client, ticket_id, *, require_place: bool = False) -> tuple[dict | None, str]:
        """The place check immediately before an outward call: Silent or Paused holds it, no place lets a tool run
        as an ordinary tool (``require_place``: an automatic post needs a connected place). Returns ``(refusal or
        None, place state)``."""
        host = self.service.host
        if host is None:
            if require_place:
                return {"ok": False, "sent": False, "code": "place_unknown",
                        "message": "No Ouroboros host to confirm the place; nothing was posted."}, "unknown"
            return None, "unchecked"
        ticket = str(ticket_id or "")
        try:
            place = host.place(client.host, ticket, ticket)
        except HostUnavailable as exc:
            return {"ok": False, "sent": False, "code": "place_unknown", "detail": exc.code,
                    "message": "Ouroboros could not confirm the Freshdesk place's state just before sending, so "
                               "nothing was sent. Try again shortly."}, "unknown"
        if place["state"] == "unbound" and require_place:
            return {"ok": False, "sent": False, "code": "place_not_connected",
                    "message": "The Freshdesk place is no longer connected; nothing was posted."}, "unbound"
        if place["state"] in ("active", "unbound"):
            return None, place["state"]
        label = place.get("label") or "The Freshdesk place"
        return {"ok": False, "sent": False, "code": f"place_{place['state'] or 'unknown'}",
                "message": f"{label} is {(place['state'] or 'in an unknown state').title()}; nothing was sent. "
                           "Its state is changed in Circles."}, place["state"]

    def _deliver(self, ctx, *, tool: str, request_id, account_id, ticket_id, text: str, digest_args: dict, call,
                 readback=None, words: bool = True, success: str = "accepted", origin: str = "tool",
                 gate_ticket=None, source_event_id: str = "", records: str = "entry", prepare=None,
                 editing: bool = False) -> dict:
        """``records``: what the answer's ``id`` is — ``entry`` (a reply, note or email on the ticket), ``ticket``
        (a ticket this call opened) or ``none``; own entries and tickets are never reported back as arrivals.
        ``prepare(client)``: reads the call needs, done before the place check so that check stays the last step
        before the write; its refusal sends nothing."""
        request_id = _request_id(request_id)
        if not str(account_id or "").strip():
            raise FreshdeskError("account_id_required", "Give account_id: the helpdesk address this skill is "
                                 "connected to (freshdesk_status shows it).")
        if ticket_id not in (None, "") and not str(ticket_id).strip().isdigit():
            raise FreshdeskError("ticket_id_invalid", "ticket_id is a ticket number. Nothing was sent.")
        client = self.service.client()
        self.service.check_account(client, account_id)
        store = self.service.store()
        try:
            agent = self.service.agent(client, store)
            recorded = store.claim(request_id, tool=tool, args_digest=digest(digest_args), host=client.host,
                                   agent_id=agent["id"], ticket_id=str(ticket_id or ""), text=text,
                                   origin=origin, task_id=_task_id(ctx), source_event_id=source_event_id)
            if recorded is not None:
                if recorded.get("conflict"):
                    return {"ok": False, "code": "request_id_conflict", "message": "This request_id was used for a "
                            "different call; use a new one for a new message.", "recorded": recorded}
                if recorded["state"] == "sending":
                    return {"ok": False, "code": "in_flight", "message": "This call is still being sent or its "
                            "process ended mid-call; freshdesk_status shows its outcome.", "recorded": recorded}
                return {"ok": recorded["state"] in ("delivered", "accepted"), "recorded": True, **recorded}
            if prepare is not None:
                try:
                    prepare(client)
                except FreshdeskError as exc:  # a read failed or refused: nothing was written
                    store.release(request_id)
                    return {**exc.as_dict(), "sent": False, "request_id": request_id}
            refusal, place_state = self._gate(client, gate_ticket if gate_ticket is not None else ticket_id,
                                              require_place=origin == "automatic")
            if refusal:
                store.release(request_id)
                return {**refusal, "request_id": request_id}
            words = words and place_state != "unbound"  # with no place there is no room to report words into
            attempted = datetime.now(timezone.utc)
            try:
                response = call(client, agent)
            except FreshdeskError as exc:
                if exc.effect in (NOT_SENT, RATE_LIMITED):
                    store.release(request_id)
                    return {**exc.as_dict(), "sent": False, "request_id": request_id,
                            "note": "Nothing was sent; the same request_id may be used again."}
                candidates = []
                if readback is not None:
                    try:
                        candidates = [e for e in readback(client, agent, attempted)
                                      if editing or not store.own_entry(e.get("id"))]  # an edit keeps the entry's id
                    except FreshdeskError:
                        candidates = []
                if len(candidates) == 1:
                    found = candidates[0]
                    store.finish(request_id, success, provider_id=str(found.get("id") or ""), report=words,
                                 own_key=str(found.get("id") or ""),
                                 result={"readback": True, "id": found.get("id"), "created_at": found.get("created_at")})
                    return {"ok": True, "state": success, "request_id": request_id, "entry_id": found.get("id"),
                            "note": "Freshdesk did not answer, but a readback found exactly one entry with this "
                                    "author, kind, visibility, attachments and words, made after the attempt."}
                store.finish(request_id, "uncertain", result={**exc.as_dict(), "candidates": [
                    {"id": e.get("id"), "created_at": e.get("created_at")} for e in candidates]}, report=words)
                return {"ok": False, "code": "uncertain", "request_id": request_id, "error": exc.as_dict(),
                        "candidates": [{"id": e.get("id"), "created_at": e.get("created_at")} for e in candidates],
                        "message": "Freshdesk may have applied this call but did not answer. Read the ticket before "
                                   "anything else; this request_id never sends again."}
            except Exception as exc:  # a fault of this skill mid-call: whether Freshdesk got it is unknown
                store.finish(request_id, "uncertain", result={"code": type(exc).__name__, "message": str(exc)[:300]},
                             report=words)
                return {"ok": False, "code": "uncertain", "request_id": request_id, "error": type(exc).__name__,
                        "message": "This call failed inside the skill and may or may not have reached Freshdesk. Read "
                                   "the ticket before anything else; this request_id never sends again."}
            data = response.data if isinstance(response.data, dict) else {}
            entry_id = data.get("id")
            if records == "ticket":  # the ticket's opening words: the thread names them ticket:<id>
                final_ticket = str(entry_id or "")
                own_key = provider_id = f"ticket:{entry_id}" if entry_id else ""
            else:
                final_ticket = str(ticket_id or data.get("ticket_id") or "")
                own_key = str(entry_id or "") if records == "entry" else ""
                provider_id = str(entry_id or "")
            store.finish(request_id, success, provider_id=provider_id, ticket_id=final_ticket, report=words,
                         own_key=own_key,
                         result={"status": response.status, "id": entry_id, "created_at": data.get("created_at")})
            self.service.note_limits(store, response.limits)
            out = {"ok": True, "state": success, "request_id": request_id, "id": entry_id}
            if final_ticket:
                out.update(ticket_id=int(final_ticket) if final_ticket.isdigit() else final_ticket,
                           url=ticket_url(client.host, final_ticket))
            return out
        finally:
            store.close()

    @staticmethod
    def _find_entry(client, ticket_id, agent, attempted, text, *, kind=None, private=None, attachments=0,
                    entry_id=None) -> list:
        """Readback candidates: entries of ``ticket_id`` by this agent, made after the attempt, of the same kind,
        visibility and attachment count, with the same words, whole (a shared opening is no proof). Only a single
        unclaimed candidate counts as proof."""
        wanted = normalized(text)
        since = attempted - timedelta(minutes=5)  # tolerance for clock skew between this computer and Freshdesk
        found = []
        page = client.get(f"tickets/{int(ticket_id)}/conversations", {"per_page": 100})
        while True:
            for entry in page.data or []:
                same_words = normalized(entry.get("body_text") or entry.get("body") or "") == wanted
                if entry_id is not None:
                    if str(entry.get("id")) == str(entry_id) and same_words:
                        found.append(entry)
                    continue
                when = parse_time(entry.get("created_at"))
                if when is None:
                    continue
                if (str(entry.get("user_id")) == str(agent["id"]) and when >= since and same_words
                        and (kind is None or entry.get("source") == kind)
                        and (private is None or bool(entry.get("private")) == private)
                        and len(entry.get("attachments") or []) == attachments):
                    found.append(entry)
            if not page.next_url:
                return found
            page = client.next_page(page)

    # -- freshdesk_reply ----------------------------------------------------------------------------------
    def reply(self, ctx=None, account_id: str = "", ticket_id=None, text: str = "", request_id: str = "",
              html: bool = False, attachment_paths=None, from_email: str = "") -> dict:
        _need(ticket_id, "ticket_id")
        _need(text, "text")
        body_html = text if html else to_html(text)
        files = _files(attachment_paths)
        digest_args = {"ticket": str(ticket_id), "text": text, "html": bool(html), "from": from_email,
                       "files": [(name, hashlib.sha256(data).hexdigest()) for _, name, data, _ in files]}

        def call(client, agent):
            body = {"body": body_html, "user_id": int(agent["id"]) if agent["id"].isdigit() else None,
                    "from_email": from_email or None}
            return self._post_words(client, f"tickets/{int(ticket_id)}/reply", body, files, text)

        return self._deliver(ctx, tool="freshdesk_reply", request_id=request_id, account_id=account_id,
                             ticket_id=ticket_id, text=text, digest_args=digest_args, call=call,
                             readback=lambda c, a, t: self._find_entry(c, ticket_id, a, t, text, kind=0, private=False,
                                                                     attachments=len(files)))

    # -- freshdesk_note -----------------------------------------------------------------------------------
    def note(self, ctx=None, account_id: str = "", ticket_id=None, text: str = "", request_id: str = "",
             public: bool = False, notify=None, audience: str = "ticket", edit_note_id=None, html: bool = False,
             attachment_paths=None, origin: str = "tool", source_event_id: str = "") -> dict:
        """``origin="automatic"``: a released output posted by the companion; it needs a connected place."""
        _need(ticket_id, "ticket_id")
        _need(text, "text")
        body_html = text if html else to_html(text)
        files = _files(attachment_paths)
        notify = self._agent_emails(None, [str(x).strip() for x in (notify or []) if str(x).strip()])
        # The manifest declares the notified agents as recipients only for calls with audience=notify, so a call
        # that notifies must say so; otherwise its declared destinations would leave out the people it mails.
        if notify and audience != "notify":
            raise FreshdeskError("audience_notify_required", "A note that notifies agents takes audience=\"notify\" "
                                 "together with notify; nothing was sent.")
        if audience == "notify" and not notify:
            raise FreshdeskError("notify_missing", "audience=\"notify\" needs the agents' emails in notify.")
        if audience not in ("ticket", "notify"):
            raise FreshdeskError("audience_invalid", "audience is \"ticket\" or \"notify\".")
        if notify and edit_note_id not in (None, ""):
            raise FreshdeskError("notify_on_edit", "Freshdesk notifies agents only when a note is created.")
        digest_args = {"ticket": str(ticket_id), "text": text, "html": bool(html), "public": bool(public),
                       "notify": notify, "edit": str(edit_note_id or ""),
                       "files": [(name, hashlib.sha256(data).hexdigest()) for _, name, data, _ in files]}

        editing = edit_note_id not in (None, "")

        def prepare(client):
            note = self._note_of(client, ticket_id, edit_note_id)
            if note is None:
                raise FreshdeskError("note_not_in_ticket", f"Entry {edit_note_id} is not a note of ticket {ticket_id}.")
            if bool(note.get("private")) == bool(public):  # an edit never changes who sees the note
                shown, flag = ("private", "false") if note.get("private") else ("public", "true")
                raise FreshdeskError("note_visibility_differs", f"Note {edit_note_id} is {shown} and an edit keeps "
                                     f"that; pass public={flag} to edit it, or write a new note. Nothing was changed.")

        def call(client, agent):
            if editing:
                body = {"body": body_html}
                if files:
                    return client.put(f"conversations/{int(edit_note_id)}", multipart=(_form(body), files))
                return client.put(f"conversations/{int(edit_note_id)}", body)
            body = {"body": body_html, "private": not public, "notify_emails": notify or None,
                    "user_id": int(agent["id"]) if agent["id"].isdigit() else None}
            return self._post_words(client, f"tickets/{int(ticket_id)}/notes", body, files, text)

        if editing:
            readback = lambda c, a, t: self._find_entry(c, ticket_id, a, t, text, entry_id=edit_note_id)  # noqa: E731
        else:
            readback = lambda c, a, t: self._find_entry(c, ticket_id, a, t, text, kind=2, private=not public,  # noqa: E731
                                                         attachments=len(files))
        return self._deliver(ctx, tool="freshdesk_note", request_id=request_id, account_id=account_id,
                             ticket_id=ticket_id, text=text, digest_args=digest_args, call=call, readback=readback,
                             success="delivered", origin=origin, source_event_id=source_event_id,
                             prepare=prepare if editing else None, editing=editing)

    @staticmethod
    def _agent_emails(client, notify) -> list:
        """Agents to notify, by email: an ordinary agent's key cannot look up another agent's address."""
        emails = []
        for item in notify or []:
            if "@" not in item:
                raise FreshdeskError("notify_needs_email", f"Give {item!r} as an email address: Freshdesk notifies "
                                     "agents by email, and an ordinary agent's key cannot look up another agent's address.")
            emails.append(item)
        return emails

    @staticmethod
    def _note_of(client, ticket_id, entry_id) -> dict | None:
        """The note ``entry_id`` of ticket ``ticket_id``, or None."""
        page = client.get(f"tickets/{int(ticket_id)}/conversations", {"per_page": 100})
        while True:
            for entry in page.data or []:
                if str(entry.get("id")) == str(entry_id) and entry.get("source") == 2:
                    return entry
            if not page.next_url:
                return None
            page = client.next_page(page)

    @staticmethod
    def _post_words(client, path, body, files, text):
        """POST a reply or note; a chat or app ticket that refuses an HTML body gets the same words as structured_body."""
        clean = {k: v for k, v in body.items() if v is not None}
        try:
            if files:
                return client.post(path, multipart=(_form(clean), files))
            return client.post(path, clean)
        except FreshdeskError as exc:
            mentions = " ".join(str(e) for e in exc.errors) + " " + exc.message
            if exc.status != 400 or exc.effect != NOT_SENT or "structured_body" not in mentions:
                raise
        structured = {**{k: v for k, v in clean.items() if k != "body"},
                      "structured_body": {"body_contents": [{"type": "text", "data": {"content": text}}]}}
        if files:
            return client.post(path, multipart=(_form(structured), files))
        return client.post(path, structured)

    # -- freshdesk_email ----------------------------------------------------------------------------------
    def email(self, ctx=None, account_id: str = "", op: str = "", text: str = "", request_id: str = "",
              ticket_id=None, to_emails=None, cc_emails=None, bcc_emails=None, subject: str = "",
              email_config_id=None, html: bool = False, attachment_paths=None, from_email: str = "") -> dict:
        if op not in EMAIL_OPS:
            raise FreshdeskError("op_invalid", f"op is one of {', '.join(EMAIL_OPS)}.")
        _need(text, "text")
        to_emails, cc_emails, bcc_emails = list(to_emails or []), list(cc_emails or []), list(bcc_emails or [])
        if op != "outbound":
            _need(ticket_id, "ticket_id")
        elif ticket_id not in (None, ""):
            raise FreshdeskError("ticket_id_not_for_outbound", "op=outbound opens a new ticket: leave ticket_id out. "
                                 "The answer names the new ticket.")
        if op in ("forward", "reply_to_forward", "outbound") and not to_emails:
            raise FreshdeskError("to_emails_missing", f"op={op} needs to_emails.")
        if op == "reply_to_forward" and (cc_emails or bcc_emails or from_email):
            raise FreshdeskError("reply_to_forward_option_unsupported", "Freshdesk's reply to a forward takes only "
                                 "to_emails; use op=forward to add cc or bcc. Nothing was sent.")
        if op == "reply" and to_emails:  # Freshdesk would drop them and mail only the requester
            raise FreshdeskError("to_emails_not_for_reply", "A reply goes to the ticket's requester; add others with "
                                 "cc_emails or bcc_emails, or use op=forward. Nothing was sent.")
        if op == "outbound" and len(to_emails) != 1:
            raise FreshdeskError("to_emails_invalid", "An outbound email opens a ticket for one contact: give one "
                                 "address in to_emails and the rest in cc_emails.")
        if op == "outbound" and (bcc_emails or from_email):
            raise FreshdeskError("outbound_option_unsupported", "Freshdesk's outbound email takes no bcc_emails or "
                                 "from_email: its sender is the mailbox email_config_id names. Nothing was sent.")
        if op == "outbound":
            _need(subject, "subject")
            if email_config_id in (None, ""):
                raise FreshdeskError("email_config_id_missing", "Freshdesk needs email_config_id, the mailbox that "
                                     "sends it: freshdesk_ticket shows a ticket's email_config_id, and an administrator's "
                                     "key lists them with freshdesk_get path=email_configs. Nothing was sent.")
        body_html = text if html else to_html(text)
        files = _files(attachment_paths)
        digest_args = {"op": op, "ticket": str(ticket_id or ""), "text": text, "html": bool(html), "to": to_emails,
                       "cc": cc_emails, "bcc": bcc_emails, "subject": subject, "config": str(email_config_id or ""),
                       "from": from_email, "files": [(n, hashlib.sha256(d).hexdigest()) for _, n, d, _ in files]}

        def call(client, agent):
            if op == "outbound":
                body = {"email": to_emails[0], "cc_emails": cc_emails or None, "subject": subject,
                        "description": body_html, "email_config_id": int(email_config_id) if email_config_id else None}
                path = "tickets/outbound_email"
            elif op == "reply":
                body = {"body": body_html, "cc_emails": cc_emails or None, "bcc_emails": bcc_emails or None,
                        "from_email": from_email or None}
                path = f"tickets/{int(ticket_id)}/reply"
            else:
                body = {"body": body_html, "to_emails": to_emails, "cc_emails": cc_emails or None,
                        "bcc_emails": bcc_emails or None, "from_email": from_email or None}
                path = f"tickets/{int(ticket_id)}/{op}"
            clean = {k: v for k, v in body.items() if v is not None}
            return client.post(path, multipart=(_form(clean), files)) if files else client.post(path, clean)

        kind = {"reply": 0, "forward": 8}.get(op)  # a reply to a forward: Freshdesk's examples disagree on its kind
        readback = None if op == "outbound" else (lambda c, a, t: self._find_entry(
            c, ticket_id, a, t, text, kind=kind, private=False if op == "reply" else None, attachments=len(files)))
        return self._deliver(ctx, tool="freshdesk_email", request_id=request_id, account_id=account_id,
                             ticket_id=ticket_id, text=text, digest_args=digest_args, call=call, readback=readback,
                             records="ticket" if op == "outbound" else "entry")

    # -- freshdesk_update ---------------------------------------------------------------------------------
    def update(self, ctx=None, account_id: str = "", ticket_id=None, status=None, priority=None, type=None,
               group=None, agent=None, product=None, tags_add=None, tags_remove=None, custom_fields=None,
               due_by=None, fr_due_by=None, subject=None) -> dict:
        _need(ticket_id, "ticket_id")
        if not str(account_id or "").strip():
            raise FreshdeskError("account_id_required", "Give account_id: the helpdesk address (freshdesk_status).")
        client = self.service.client()
        self.service.check_account(client, account_id)
        store = self.service.store()
        try:
            requested = {"status": status, "priority": priority, "type": type, "group": group, "agent": agent,
                         "product": product}
            body = self._resolve(client, store, requested, custom_fields)
            for key, value in (("due_by", due_by), ("fr_due_by", fr_due_by), ("subject", subject)):
                if value not in (None, ""):
                    body[key] = value
            current = None
            if tags_add or tags_remove or body.get("status") in (4, 5):
                current = client.get(f"tickets/{int(ticket_id)}").data or {}
            if tags_add or tags_remove:
                tags = [t for t in current.get("tags") or [] if t not in set(tags_remove or [])]
                body["tags"] = tags + [t for t in (tags_add or []) if t not in tags]
            if body.get("status") in (4, 5):
                catalog = self.service.catalog(client, store)
                gaps = catalog.closure_gaps(current, body)
                if gaps:
                    return {"ok": False, "code": "closure_fields_missing", "message": "Freshdesk requires these "
                            "fields before this status; nothing was written.", "fields": gaps}
            if not body:
                raise FreshdeskError("nothing_to_change", "Give at least one property to change.")
            refusal, _ = self._gate(client, ticket_id)
            if refusal:
                return refusal
            try:
                response = client.put(f"tickets/{int(ticket_id)}", body)
            except FreshdeskError as exc:
                if exc.effect != UNKNOWN:
                    raise
                after = client.get(f"tickets/{int(ticket_id)}").data or {}
                applied = all(after.get(k) == v for k, v in body.items() if k != "custom_fields") and all(
                    (after.get("custom_fields") or {}).get(k) == v for k, v in (body.get("custom_fields") or {}).items())
                return {"ok": applied, "code": "applied_after_timeout" if applied else "uncertain",
                        "message": "Freshdesk did not answer; reading the ticket back shows the change "
                                   + ("was applied." if applied else "was not (yet) applied. Read it again before retrying."),
                        "error": exc.as_dict()}
            self.service.note_limits(store, response.limits)
            catalog = self.service.catalog(client, store)
            ticket = response.data or {}
            return {"ok": True, "ticket_id": ticket.get("id"), "url": ticket_url(client.host, ticket.get("id")),
                    "status": catalog.named(ticket.get("status"), "status"),
                    "priority": catalog.named(ticket.get("priority"), "priority"),
                    "group": catalog.named(ticket.get("group_id"), "group"),
                    "agent": catalog.named(ticket.get("responder_id"), "agent"), "type": ticket.get("type"),
                    "tags": ticket.get("tags") or [], "custom_fields": ticket.get("custom_fields") or {},
                    "updated_at": ticket.get("updated_at")}
        finally:
            store.close()

    def _resolve(self, client, store, requested: dict, custom_fields) -> dict:
        for attempt in (0, 1):
            catalog = self.service.catalog(client, store, refresh=bool(attempt))
            try:
                body = {}
                for name, value in requested.items():
                    if value is None or value == "":
                        continue
                    if name == "agent" and str(value).lower() in ("unassigned", "none", "null"):
                        body["responder_id"] = None
                        continue
                    if name == "agent" and str(value).lower() == "me":
                        body["responder_id"] = int(self.service.agent(client, store)["id"])
                        continue
                    key, resolved = catalog.resolve(name, value)
                    body[key] = resolved
                if custom_fields:
                    body["custom_fields"] = catalog.resolve_custom(custom_fields)
                return body
            except FreshdeskError as exc:
                if exc.code not in ("choice_unknown", "field_unknown") or attempt:
                    raise
        return {}

    # -- freshdesk_request --------------------------------------------------------------------------------
    def request(self, ctx=None, account_id: str = "", method: str = "", path: str = "", body=None,
                request_id: str = "") -> dict:
        method = str(method or "").upper()
        if method not in ("POST", "PUT", "DELETE"):
            raise FreshdeskError("method_invalid", "method is POST, PUT or DELETE; reads use freshdesk_get.")
        clean_path = canonical_path(path)  # the same spelling the client sends
        for methods, pattern, tool in SPEECH_ROUTES:
            if method in methods and pattern.match(clean_path):
                raise FreshdeskError("words_need_typed_tool", (f"This call carries words to people; use {tool}."
                                     if tool else "Ticket summaries are not written by this skill version."))
        if clean_path == "tickets/merge" and isinstance(body, dict) and (body.get("note_in_primary") or body.get("note_in_secondary")):
            raise FreshdeskError("words_need_typed_tool", "Merge notes carry words; merge without note_in_primary/"
                                 "note_in_secondary (Freshdesk adds its own merge note) and write your note with freshdesk_note.")
        if clean_path.startswith("tickets/bulk_update") and _contains_key(body, "reply"):
            raise FreshdeskError("words_need_typed_tool", "A bulk update that replies carries words; reply per ticket with freshdesk_reply.")
        digest_args = {"method": method, "path": clean_path, "body": body}

        def call(client, agent):
            if method == "DELETE":
                return client.delete(clean_path)
            return client.post(clean_path, body) if method == "POST" else client.put(clean_path, body)

        ticket_in_path = re.match(r"^tickets/(\d+)(?:/|$)", clean_path)
        result = self._deliver(ctx, tool="freshdesk_request", request_id=request_id, account_id=account_id,
                               ticket_id="", text="", digest_args=digest_args, call=call, words=False,
                               success="delivered", gate_ticket=ticket_in_path.group(1) if ticket_in_path else "",
                               records="ticket" if method == "POST" and clean_path == "tickets" else "none")
        if result.get("code") == "uncertain":
            result["message"] = ("Freshdesk may have applied this call but did not answer. Inspect the object with "
                                 "freshdesk_get before anything else; this request_id never repeats the call.")
        return result


def _need(value, name: str) -> None:
    if value in (None, "") or (isinstance(value, str) and not value.strip()):
        raise FreshdeskError(f"{name}_missing", f"{name} is required.")
