"""The polling walk: which tickets changed, and their events into the durable inbox.

The cutoff between history and events is the connection time by Freshdesk's own clock (its ``Date`` header);
tickets and entries written before it are history, never events. A walk lists tickets updated since the
watermark (minus an overlap), oldest first, and anchors each next request by time, not by page offset, so a ticket
updated during the walk cannot slip between pages. Changed tickets are stored as dirty work before the watermark
moves; their conversations are read and turned into events across restarts. Requests are paced by a share of the
account's per-minute limit that Freshdesk reports in its headers.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .client import FreshdeskError
from .events import Context, parse_time, ticket_events
from .status import identity
from .store import dump

OVERLAP_SECONDS = 120
PAGE_SIZE = 100
PAGE_CAP = 300            # Freshdesk returns at most 300 pages of a list
LIST_CREDITS = 3          # one list page with include=description
UNKNOWN_LIMIT_PER_MINUTE = 10


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Budget:
    """Credits this cycle may spend: ``share`` % of the per-minute limit Freshdesk reports, scaled to the cycle."""

    def __init__(self, store, *, share: int, poll_seconds: int):
        limits = store.meta("limits") or {}
        per_minute = limits.get("account_total") or limits.get("total")
        sub = limits.get("sublimit_total")
        try:
            per_minute = float(per_minute) if per_minute else 0.0
            sub = float(sub) if sub else 0.0
        except (TypeError, ValueError):
            per_minute = sub = 0.0
        scale = max(poll_seconds, 1) / 60.0
        if per_minute:
            allowed = per_minute * share / 100.0 * scale
            if sub:
                allowed = min(allowed, sub * share / 100.0 * scale * LIST_CREDITS)
        else:
            allowed = UNKNOWN_LIMIT_PER_MINUTE * scale
        self.left = max(LIST_CREDITS + 1, int(allowed))
        self.spent = 0

    def take(self, credits: int = 1) -> bool:
        if self.left < credits:
            return False
        self.left -= credits
        self.spent += credits
        return True


def baseline(client, store) -> str:
    """The connection time by Freshdesk's clock, set once; tickets and entries before it are history."""
    connected = store.meta("connected_at")
    if connected:
        return connected
    response = client.get("tickets", {"per_page": 1, "order_by": "updated_at", "order_type": "desc"})
    moment = None
    try:
        moment = parsedate_to_datetime(response.headers.get("date", ""))
    except (TypeError, ValueError):
        moment = None
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    connected = _z(moment)
    store.set_meta("connected_at", connected)
    store.set_meta("watermark", _z(moment - timedelta(seconds=OVERLAP_SECONDS)))
    return connected


def walk(client, store, budget: Budget) -> dict:
    """List changed tickets into ``dirty``; advance the watermark only when the walk is complete."""
    watermark = parse_time(store.meta("watermark") or "") or datetime.now(timezone.utc)
    cursor = store.meta("walk") or {"since": _z(watermark - timedelta(seconds=OVERLAP_SECONDS)), "page": 1,
                                    "newest": store.meta("watermark")}
    found = 0
    while budget.take(LIST_CREDITS):
        response = client.get("tickets", {"updated_since": cursor["since"], "order_by": "updated_at",
                                           "order_type": "asc", "per_page": PAGE_SIZE, "include": "description",
                                           "page": cursor["page"]})
        store.set_meta("limits", {**response.limits, "observed_at": _z(datetime.now(timezone.utc))})
        rows = [row for row in (response.data or []) if isinstance(row, dict) and row.get("id") is not None]
        with store.transaction() as db:
            for row in rows:
                ticket_id, updated = str(row["id"]), str(row.get("updated_at") or "")
                seen = db.execute("SELECT updated_at FROM tickets WHERE id=?", (ticket_id,)).fetchone()
                queued = db.execute("SELECT updated_at FROM dirty WHERE ticket_id=?", (ticket_id,)).fetchone()
                if (seen and seen["updated_at"] == updated) or (queued and queued["updated_at"] == updated):
                    continue
                db.execute("INSERT INTO dirty(ticket_id, updated_at, queued, snapshot) VALUES(?,?,?,?) "
                           "ON CONFLICT(ticket_id) DO UPDATE SET updated_at=excluded.updated_at, "
                           "queued=excluded.queued, snapshot=excluded.snapshot",
                           (ticket_id, updated, time.time(), dump(row)))
                found += 1
        stamps = sorted(str(row.get("updated_at") or "") for row in rows if row.get("updated_at"))
        if stamps and (not cursor.get("newest") or stamps[-1] > cursor["newest"]):
            cursor["newest"] = stamps[-1]
        if len(rows) < PAGE_SIZE:
            if cursor.get("newest"):
                store.set_meta("watermark", cursor["newest"])
            store.set_meta("walk", None)
            store.set_meta("last_walk", _z(datetime.now(timezone.utc)))
            store.resolve("walk")
            return {"complete": True, "changed": found}
        if stamps[0] == stamps[-1]:
            cursor["page"] += 1  # a whole page shares one time: only then page by number
            if cursor["page"] > PAGE_CAP:
                store.gap("walk", "provider_page_cap", {"since": cursor["since"]})
                cursor = {"since": stamps[-1], "page": 1, "newest": cursor.get("newest")}
        else:
            cursor = {"since": _z(parse_time(stamps[-1]) - timedelta(seconds=1)), "page": 1,
                      "newest": cursor.get("newest")}
        store.set_meta("walk", cursor)
    return {"complete": False, "changed": found}


def _conversation(client, store, budget: Budget, ticket_id: str) -> list | None:
    entries, page = [], None
    while True:
        if not budget.take(1):
            return None
        page = client.get(f"tickets/{int(ticket_id)}/conversations", {"per_page": 100}) if page is None \
            else client.next_page(page)
        entries.extend(e for e in (page.data or []) if isinstance(e, dict))
        if not page.next_url:
            return entries


def process(service, client, store, budget: Budget, *, capabilities: dict | None = None, limit: int = 500) -> int:
    """Turn dirty tickets into inbox events; a ticket stays dirty until its whole thread was read."""
    connected = store.meta("connected_at") or ""
    agent = service.agent(client, store)
    catalog = service.catalog(client, store)
    claim = identity(agent) if (capabilities or {}).get("presence_identity") == 1 else None

    def contact(user_id):
        if not budget.take(1) and not store.one("SELECT 1 FROM contacts WHERE id=?", (str(user_id),)):
            return {}
        try:
            return service.contact(client, store, user_id)
        except FreshdeskError:
            return {}

    ctx = Context(host=client.host, agent=agent, catalog=catalog, contact=contact, connected_at=connected,
                  identity=claim)
    done = 0
    for row in store.rows("SELECT * FROM dirty ORDER BY updated_at, ticket_id LIMIT ?", (limit,)):
        ticket = json.loads(row["snapshot"])
        entries = _conversation(client, store, budget, row["ticket_id"])
        if entries is None:
            break
        stored = store.one("SELECT snapshot FROM tickets WHERE id=?", (row["ticket_id"],))
        previous = json.loads(stored["snapshot"]) if stored else None
        known, previous_to = {}, {}
        for item in store.rows("SELECT id, revision, to_emails FROM entries WHERE ticket_id=?", (row["ticket_id"],)):
            known[item["id"]] = item["revision"]
            previous_to[item["id"]] = json.loads(item["to_emails"] or "[]")
        for entry in entries:
            entry["_previous_to_emails"] = previous_to.get(str(entry.get("id")), [])
        own = {r["entry_id"] for r in store.rows("SELECT entry_id FROM sent_entries WHERE ticket_id=?", (row["ticket_id"],))}
        events = ticket_events(ctx, ticket, previous, entries, known, own)
        with store.transaction() as db:
            seq = (db.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM inbox").fetchone()["s"]) + 1
            for event in events:
                db.execute("INSERT OR IGNORE INTO inbox(source_event_id, ticket_id, seq, event) VALUES(?,?,?,?)",
                           (event["source_event_id"], row["ticket_id"], seq, dump(event)))
                seq += 1
            db.execute("INSERT INTO tickets(id, updated_at, snapshot, first_seen) VALUES(?,?,?,?) ON CONFLICT(id) DO "
                       "UPDATE SET updated_at=excluded.updated_at, snapshot=excluded.snapshot",
                       (row["ticket_id"], row["updated_at"], dump({k: v for k, v in ticket.items()
                                                                     if k not in ("description", "description_text")}),
                        _z(datetime.now(timezone.utc))))
            for entry in entries:
                revision = str(entry.get("last_edited_at") or entry.get("updated_at") or entry.get("created_at") or "")
                db.execute("INSERT INTO entries(id, ticket_id, revision, user_id, created_at, to_emails) "
                           "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, "
                           "to_emails=excluded.to_emails",
                           (str(entry.get("id")), row["ticket_id"], revision, str(entry.get("user_id") or ""),
                            str(entry.get("created_at") or ""), dump(entry.get("to_emails") or [])))
            db.execute("DELETE FROM dirty WHERE ticket_id=? AND updated_at=?", (row["ticket_id"], row["updated_at"]))
        done += 1
    return done
