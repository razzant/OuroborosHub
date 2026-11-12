"""What changed on a ticket, as Presence events with observed provider facts (no judgment about relevance).

A ticket is one conversation: ``conversation_id = thread_id = <ticket id>`` (an untagged follow-up wakes the live
author only inside a thread, core ``place_intake._followup_in_scope``). Facts come from provider fields only:
``new_issue`` for a ticket created after the connection, ``assigned`` when the responder becomes (or is first seen
as) the connected agent, ``my_thread`` when that agent wrote earlier in the ticket, ``mention`` when a note's notify
list (``to_emails``) holds the agent's email, ``any_message`` for new words. A property-only change carries no words
and no facts beyond ``assigned``: it is told to a live author as background.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .reads import ticket_url
from .text import html_to_text

PROVIDER = "freshdesk"
NOTE = 2
OUTBOUND_EMAIL = 10  # a ticket an agent opened by emailing a contact: its first words are the helpdesk's
THREAD_CHAR_BOUND = 30_000
WATCHED = (("status", "status", "Status"), ("priority", "priority", "Priority"), ("type", None, "Type"),
           ("group_id", "group", "Group"), ("responder_id", "agent", "Agent"), ("product_id", "product", "Product"),
           ("tags", None, "Tags"), ("due_by", None, "Due"), ("fr_due_by", None, "First response due"))


def parse_time(value: str) -> datetime | None:
    """Freshdesk's time (documented as ``…Z`` UTC; older examples show offsets or fractions) in UTC, else None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(value: str) -> str:
    """Freshdesk's ``…Z`` as ``datetime.isoformat()`` writes it, which the host reads as a time with a zone."""
    moment = parse_time(value)
    return moment.isoformat() if moment else ""


def conversation_key(host: str, ticket_id) -> str:
    return f"{PROVIDER}:{host}:{ticket_id}:{ticket_id}"


class Context:
    """Facts shared by the events of one ticket: the helpdesk, the agent, names, and how to name a contact."""

    def __init__(self, *, host: str, agent: dict, catalog, contact, connected_at: str, identity: dict | None):
        self.host, self.agent, self.catalog, self.contact = host, agent, catalog, contact
        self.connected = parse_time(connected_at)
        self.identity = identity
        field = catalog.by_type.get("default_agent") or {}
        choices = field.get("choices") if isinstance(field.get("choices"), dict) else {}
        self.agent_names = {str(value): label for label, value in choices.items()}

    @property
    def agent_id(self) -> str:
        return str(self.agent.get("id") or "")

    def after_connection(self, value: str) -> bool:
        moment = parse_time(value)
        return bool(moment and self.connected and moment >= self.connected)

    def actor(self, user_id, *, incoming=None) -> dict:
        user = str(user_id or "")
        if not user:
            return {"platform": PROVIDER, "attribution": "unknown"}
        if user == self.agent_id and incoming is not True:  # this agent, even when Freshdesk lists it nowhere (a collaborator)
            return {"platform_actor_id": user, "display_name": self.agent.get("name") or self.agent_names.get(user) or "This agent"}
        if user in self.agent_names and incoming is not True:
            return {"platform_actor_id": user, "display_name": self.agent_names[user]}
        contact = self.contact(user) or {}
        actor = {"platform_actor_id": user, "display_name": contact.get("name") or contact.get("email") or f"Contact {user}",
                 "authenticated": False}
        if contact.get("email"):
            actor["email"] = contact["email"]
        return actor


def _label(ctx: Context, name, value):
    if name is None:
        return value
    label = ctx.catalog.label(name, value)
    return label if label is not None else value


def describe_change(ctx: Context, before: dict, after: dict) -> list[str]:
    parts = []
    for key, name, title in WATCHED:
        old, new = before.get(key), after.get(key)
        if key == "tags":
            old, new = sorted(old or []), sorted(new or [])
        if old != new:
            shown_old = ", ".join(old) if isinstance(old, list) else _label(ctx, name, old)
            shown_new = ", ".join(new) if isinstance(new, list) else _label(ctx, name, new)
            parts.append(f"{title}: {shown_old if shown_old not in (None, '', []) else '—'} → "
                         f"{shown_new if shown_new not in (None, '', []) else '—'}")
    return parts


def _attachments(entry: dict) -> list[dict]:
    return [{"name": a.get("name"), "mime_type": a.get("content_type"), "file_size": a.get("file_size") or a.get("size"),
             "status": "available", "note": f"Read it with freshdesk_ticket stage_attachment_id={a.get('id')}."}
            for a in entry.get("attachments") or []]


def _opener(ctx: Context, ticket: dict, own_ticket: bool) -> tuple[dict, str]:
    """Who wrote the ticket's first words, as an event actor and a name for the thread: this agent for a ticket this
    connector opened, the helpdesk (an agent Freshdesk does not name) for an outbound email, else the requester."""
    if own_ticket:
        name = ctx.agent.get("name") or "This agent"
        return {"platform_actor_id": ctx.agent_id, "display_name": name}, name
    if ticket.get("source") == OUTBOUND_EMAIL:
        return {"platform": PROVIDER, "attribution": "unknown"}, "The helpdesk (outbound email)"
    actor = ctx.actor(ticket.get("requester_id"), incoming=True)
    return actor, actor.get("display_name") or ""


def _conversation(ctx: Context, ticket: dict, entries: list[dict], opener: tuple[dict, str]) -> dict:
    requester = ctx.contact(str(ticket.get("requester_id") or "")) if ticket.get("requester_id") else {}
    messages, used, complete = [], 0, True
    for entry in reversed(entries):  # the newest first, so a bound keeps the most recent exchange
        text = entry.get("body_text") or html_to_text(entry.get("body") or "")
        if used + len(text) > THREAD_CHAR_BOUND and messages:
            complete = False
            break
        used += len(text)
        actor = ctx.actor(entry.get("user_id"), incoming=entry.get("incoming"))
        messages.append({"message_id": str(entry.get("id")), "actor_id": str(entry.get("user_id") or ""),
                         "display_name": actor.get("display_name"), "sent_at": iso(entry.get("created_at")),
                         "private": entry.get("private"), "text": text})
    messages.reverse()
    description = ticket.get("description_text") or html_to_text(ticket.get("description") or "")
    opening = {"message_id": f"ticket:{ticket.get('id')}", "actor_id": str(opener[0].get("platform_actor_id") or ""),
               "display_name": opener[1] or None, "sent_at": iso(ticket.get("created_at")),
               "text": f"{ticket.get('subject') or ''}\n\n{description}".strip()}
    history = {"status": "observed", "observed_at": datetime.now(timezone.utc).isoformat(),
               "data": {"messages": ([opening] if complete else []) + messages, "complete": complete}}
    if not complete:
        history["note"] = f"Older entries are left out here; freshdesk_ticket ticket_id={ticket.get('id')} reads them."
    return {"title": ticket.get("subject") or f"Ticket {ticket.get('id')}", "url": ticket_url(ctx.host, ticket.get("id")),
            "status": _label(ctx, "status", ticket.get("status")), "priority": _label(ctx, "priority", ticket.get("priority")),
            "group": _label(ctx, "group", ticket.get("group_id")), "agent": _label(ctx, "agent", ticket.get("responder_id")),
            "requester": {"id": str(ticket.get("requester_id") or ""), "name": (requester or {}).get("name"),
                          "email": (requester or {}).get("email")},
            "thread_history": history}


def _event(ctx: Context, ticket: dict, source_event_id: str, *, actor: dict, text: str, message: dict, facts: list,
           conversation: dict) -> dict:
    ticket_id = str(ticket.get("id"))
    event = {"source_event_id": source_event_id, "provider": PROVIDER, "account_id": ctx.host,
             "conversation_id": ticket_id, "thread_id": ticket_id,
             "conversation_key": conversation_key(ctx.host, ticket_id), "actor": actor, "conversation": conversation,
             "message": message, "text": text, "facts": list(dict.fromkeys(facts))}
    if ctx.identity:
        event["identity"] = ctx.identity
    return event


def ticket_events(ctx: Context, ticket: dict, previous: dict | None, entries: list[dict], known: dict[str, str],
                  own_entries: set[str]) -> list[dict]:
    """Events for one changed ticket.

    ``previous``: the stored snapshot (None on first sight); ``known``: entry id -> stored revision;
    ``own_entries``: what this connector put in Freshdesk (entry ids, ``ticket:<id>`` for tickets it opened), never
    reported back as arrivals.
    """
    ticket_id = str(ticket.get("id"))
    own_ticket = f"ticket:{ticket_id}" in own_entries
    opener = _opener(ctx, ticket, own_ticket)
    conversation = _conversation(ctx, ticket, entries, opener)
    base = f"{PROVIDER}:{ctx.host}:{ticket_id}"
    events: list[dict] = []
    responder_is_me = str(ticket.get("responder_id") or "") == ctx.agent_id and bool(ctx.agent_id)
    first_sight = previous is None
    assigned_now = responder_is_me and (first_sight or str(previous.get("responder_id") or "") != ctx.agent_id)
    pending_assigned = assigned_now and not (first_sight and own_ticket)  # opening it made it ours: not news
    if first_sight and ctx.after_connection(ticket.get("created_at")) and not own_ticket:
        description = ticket.get("description_text") or html_to_text(ticket.get("description") or "")
        facts = ["any_message", "new_issue"] + (["assigned"] if pending_assigned else [])
        pending_assigned = False
        events.append(_event(ctx, ticket, f"{base}:created", actor=opener[0],
                             text=f"{ticket.get('subject') or ''}\n\n{description}".strip(),
                             message={"message_id": f"ticket:{ticket_id}", "sent_at": iso(ticket.get("created_at")),
                                      "url": ticket_url(ctx.host, ticket_id), "source": ticket.get("source"),
                                      "incoming": ticket.get("source") != OUTBOUND_EMAIL, "source_order": 0,
                                      "attachments": _attachments(ticket)},
                             facts=facts, conversation=conversation))
    wrote_before = own_ticket
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for entry in sorted(entries, key=lambda e: (parse_time(e.get("created_at")) or epoch, int(e.get("id") or 0))):
        entry_id = str(entry.get("id"))
        revision = str(entry.get("last_edited_at") or entry.get("updated_at") or entry.get("created_at") or "")
        author = str(entry.get("user_id") or "")
        own = entry_id in own_entries
        is_new = entry_id not in known
        edited = not is_new and known.get(entry_id) != revision
        report = (is_new or edited) and not own and (edited or ctx.after_connection(entry.get("created_at")))
        if report:
            events.append(_entry_event(ctx, ticket, base, entry, revision=revision, edited=edited,
                                       wrote_before=wrote_before, assigned=pending_assigned, conversation=conversation))
            pending_assigned = False
        if author == ctx.agent_id or own:
            wrote_before = True

    if previous is not None:
        changes = describe_change(ctx, previous, ticket)
        if changes:
            facts = ["assigned"] if pending_assigned else []
            pending_assigned = False
            events.append(_event(ctx, ticket, f"{base}:change:{ticket.get('updated_at')}",
                                 actor={"platform": PROVIDER, "attribution": "unknown"}, text="",
                                 message={"message_id": f"ticket:{ticket_id}:{ticket.get('updated_at')}",
                                          "sent_at": iso(ticket.get("updated_at")), "url": ticket_url(ctx.host, ticket_id),
                                          "non_text": [{"kind": "ticket_change", "description": "; ".join(changes)}]},
                                 facts=facts, conversation=conversation))
    if pending_assigned:
        events.append(_event(ctx, ticket, f"{base}:assigned:{ticket.get('updated_at')}",
                             actor={"platform": PROVIDER, "attribution": "unknown"}, text="",
                             message={"message_id": f"ticket:{ticket_id}:{ticket.get('updated_at')}",
                                      "sent_at": iso(ticket.get("updated_at")), "url": ticket_url(ctx.host, ticket_id),
                                      "non_text": [{"kind": "ticket_change", "description":
                                                    "Assigned to this agent; first seen now, so when it was assigned is unknown."
                                                    if first_sight else "Assigned to this agent."}]},
                             facts=["assigned"], conversation=conversation))
    return events


def _entry_event(ctx: Context, ticket: dict, base: str, entry: dict, *, revision: str, edited: bool,
                 wrote_before: bool, assigned: bool, conversation: dict) -> dict:
    entry_id = str(entry.get("id"))
    author = str(entry.get("user_id") or "")
    facts = ["any_message"]
    if wrote_before and author != ctx.agent_id:
        facts.append("my_thread")
    email = str(ctx.agent.get("email") or "").lower()
    notified = {str(e).lower() for e in entry.get("to_emails") or []}
    if entry.get("source") == NOTE and email and email in notified:
        before = {str(e).lower() for e in entry.get("_previous_to_emails") or []}
        if not edited or email not in before:  # an edit counts a notification only when it is new
            facts.append("mention")
    if assigned:
        facts.append("assigned")
    message = {"message_id": entry_id, "sent_at": iso(entry.get("created_at")),
               "url": ticket_url(ctx.host, ticket.get("id")), "private": entry.get("private"),
               "incoming": entry.get("incoming"), "source": entry.get("source"),
               "to_emails": entry.get("to_emails") or [], "attachments": _attachments(entry),
               "source_order": int(entry_id) if entry_id.isdigit() else 0}
    source_event_id = f"{base}:entry:{entry_id}"
    if edited:
        message["edited_at"] = iso(entry.get("last_edited_at") or entry.get("updated_at"))
        source_event_id = f"{base}:entry:{entry_id}:edit:{revision}"
    return _event(ctx, ticket, source_event_id, actor=ctx.actor(author, incoming=entry.get("incoming")),
                  text=entry.get("body_text") or html_to_text(entry.get("body") or ""), message=message,
                  facts=facts, conversation=conversation)
