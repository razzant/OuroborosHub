"""The skill's own SQLite state under its state directory: send journal, watcher state and gaps.

Tool calls (in Ouroboros's processes) and the companion share one database in WAL mode. The journal is the
custody of every outward call: a ``request_id`` is claimed before the HTTP call, the outcome is written after
it, and the companion reports each outcome to the host once. Nothing here decides what to say or when.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal (
    request_id TEXT PRIMARY KEY, tool TEXT NOT NULL, digest TEXT NOT NULL, host TEXT NOT NULL,
    agent_id TEXT NOT NULL, ticket_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
    provider_id TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', format TEXT NOT NULL DEFAULT 'html',
    origin TEXT NOT NULL DEFAULT 'tool', task_id TEXT NOT NULL DEFAULT '', source_event_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '{}',
    attempted_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', report_state TEXT NOT NULL DEFAULT 'pending',
    report_due REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS journal_report ON journal(report_state, report_due);
CREATE INDEX IF NOT EXISTS journal_ticket ON journal(ticket_id);
CREATE TABLE IF NOT EXISTS sent_entries (entry_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, request_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, snapshot TEXT NOT NULL,
    first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, revision TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', to_emails TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS entries_ticket ON entries(ticket_id);
CREATE TABLE IF NOT EXISTS dirty (ticket_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, queued REAL NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS inbox (source_event_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, seq INTEGER NOT NULL,
    event TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready', response TEXT NOT NULL DEFAULT '{}',
    due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, connection TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS inbox_ready ON inbox(state, ticket_id, seq);
CREATE TABLE IF NOT EXISTS refs (ref TEXT PRIMARY KEY, source_event_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
    kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', due REAL NOT NULL DEFAULT 0,
    binding TEXT NOT NULL DEFAULT '', response TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS outputs (output_ref TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, text TEXT NOT NULL,
    source_event_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'queued', request_id TEXT NOT NULL DEFAULT '', binding TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '', due REAL NOT NULL DEFAULT 0, hold TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, value TEXT NOT NULL, fetched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS gaps (scope TEXT PRIMARY KEY, code TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL);
"""

FINAL_STATES = ("delivered", "accepted", "failed", "uncertain", "not_sent")


def _parse(value: str) -> datetime:
    text = str(value or "").strip()
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)  # unknown: treated as not before the connection
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, state_dir, *, readonly: bool = False):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "freshdesk.sqlite3"
        self.db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def rows(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    # -- meta and gaps --------------------------------------------------------------------------------
    def meta(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, dump(value)))

    def gap(self, scope: str, code: str, detail: dict | None = None) -> None:
        self.db.execute("INSERT INTO gaps(scope,code,detail,at) VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                        "code=excluded.code, detail=excluded.detail, at=excluded.at",
                        (scope, code, dump(detail or {}), now_iso()))

    def resolve(self, scope: str) -> None:
        self.db.execute("DELETE FROM gaps WHERE scope=?", (scope,))

    def gaps(self) -> list[dict]:
        return [{"scope": r["scope"], "code": r["code"], "detail": json.loads(r["detail"]), "at": r["at"]}
                for r in self.rows("SELECT * FROM gaps ORDER BY at DESC LIMIT 100")]

    # -- journal ----------------------------------------------------------------------------------------
    def claim(self, request_id: str, *, tool: str, args_digest: str, host: str, agent_id: str, ticket_id: str,
              text: str = "", text_format: str = "html", origin: str = "tool", task_id: str = "",
              source_event_id: str = "") -> dict | None:
        """Claim ``request_id`` for a call. ``None``: claimed now, go ahead. Otherwise the recorded row: the same
        call answered before (or still in flight), or a ``conflict`` when the id was used for different arguments."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM journal WHERE request_id=?", (request_id,)).fetchone()
            if row is not None:
                recorded = self.journal_row(row)
                if row["digest"] != args_digest:
                    return {**recorded, "conflict": True}
                return recorded
            db.execute("INSERT INTO journal(request_id,tool,digest,host,agent_id,ticket_id,state,text,format,origin,"
                       "task_id,source_event_id,attempted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (request_id, tool, args_digest, host, agent_id, str(ticket_id or ""), "sending", text,
                        text_format, origin, task_id, source_event_id, now_iso()))
        return None

    def finish(self, request_id: str, state: str, *, provider_id: str = "", result: dict | None = None,
               ticket_id: str = "", report: bool = True, own_key: str = "") -> dict:
        """Record an outcome. ``report``: words people read, reported to the host once per state. ``own_key``: what
        this call put in Freshdesk, never reported back as an arrival: a conversation entry id, or ``ticket:<id>``
        for a ticket this connector opened."""
        assert state in FINAL_STATES, state
        with self.transaction() as db:
            db.execute("UPDATE journal SET state=?, provider_id=?, result=?, finished_at=?, "
                       "ticket_id=CASE WHEN ?<>'' THEN ? ELSE ticket_id END, report_state=?, report_due=0 "
                       "WHERE request_id=?",
                       (state, str(provider_id or ""), dump(result or {}), now_iso(), str(ticket_id or ""),
                        str(ticket_id or ""), "pending" if report and state != "not_sent" else "none", request_id))
            # A receipt names its conversation: a call that ended with no ticket (an outbound email whose answer
            # was lost) has none to report into.
            db.execute("UPDATE journal SET report_state='none' WHERE request_id=? AND ticket_id=''", (request_id,))
            if own_key and state in ("delivered", "accepted"):
                row = db.execute("SELECT ticket_id FROM journal WHERE request_id=?", (request_id,)).fetchone()
                db.execute("INSERT OR IGNORE INTO sent_entries(entry_id,ticket_id,request_id) VALUES(?,?,?)",
                           (str(own_key), row["ticket_id"] if row else "", request_id))
        return self.journal(request_id)

    def release(self, request_id: str) -> None:
        """Forget a claim that never reached Freshdesk (a refusal before the HTTP call), so the id can be reused."""
        self.db.execute("DELETE FROM journal WHERE request_id=? AND state='sending'", (request_id,))

    def journal(self, request_id: str) -> dict | None:
        row = self.one("SELECT * FROM journal WHERE request_id=?", (request_id,))
        return self.journal_row(row) if row else None

    @staticmethod
    def journal_row(row: sqlite3.Row) -> dict:
        return {"request_id": row["request_id"], "tool": row["tool"], "state": row["state"],
                "ticket_id": row["ticket_id"], "provider_id": row["provider_id"], "agent_id": row["agent_id"],
                "attempted_at": row["attempted_at"], "finished_at": row["finished_at"],
                "result": json.loads(row["result"] or "{}"), "report_state": row["report_state"]}

    def interrupted_sends(self) -> list[dict]:
        """Claims left ``sending`` by a process that ended mid-call: their outcome is unknown."""
        return [self.journal_row(r) for r in self.rows("SELECT * FROM journal WHERE state='sending'")]

    def fold_own_change(self, ticket_id, sent: dict, *, answer: dict, tags_add=(), tags_remove=()) -> None:
        """A property change this skill made, so it never comes back as news.

        ``sent``: only the properties this call wrote (as Freshdesk's answer states them), so a colleague's change in
        the same minute is still news; tags added or removed apply to each snapshot's own list. A ticket the watcher
        knows has them folded into its stored and queued snapshot. A ticket it has never read, opened before the
        connection, is recorded as seen now (the whole answer) when this call assigned it: taking an old ticket does
        not wake as "first seen assigned". The update time is left alone, so the next walk still reads the thread and a customer reply that
        came just before is not lost."""
        ticket_id = str(ticket_id)
        added, removed = list(tags_add or []), set(tags_remove or [])
        with self.transaction() as db:
            known = False
            for table, key in (("tickets", "id"), ("dirty", "ticket_id")):
                row = db.execute(f"SELECT snapshot FROM {table} WHERE {key}=?", (ticket_id,)).fetchone()
                if row is None:
                    continue
                known = known or table == "tickets"
                snapshot = {**json.loads(row["snapshot"] or "{}"), **sent}
                if added or removed:
                    kept = [tag for tag in snapshot.get("tags") or [] if tag not in removed]
                    snapshot["tags"] = kept + [tag for tag in added if tag not in kept]
                db.execute(f"UPDATE {table} SET snapshot=? WHERE {key}=?", (dump(snapshot), ticket_id))
            connected = db.execute("SELECT value FROM meta WHERE key='connected_at'").fetchone()
            cutoff = json.loads(connected["value"]) if connected else ""
            opened = str((answer or {}).get("created_at") or "")
            # Only a call that assigned the ticket makes it seen: another write leaves it unread, so a colleague's
            # assignment in the same minute still wakes as "first seen assigned".
            took = "responder_id" in sent
            if not known and took and answer and (not cutoff or (opened and _parse(opened) < _parse(cutoff))):
                seen = {k: v for k, v in answer.items() if k not in ("description", "description_text")}
                db.execute("INSERT OR IGNORE INTO tickets(id, updated_at, snapshot, first_seen) VALUES(?,?,?,?)",
                           (ticket_id, "", dump(seen), now_iso()))

    def mark_own(self, key: str, ticket_id: str, request_id: str) -> None:
        """One more key for what a call wrote (an edit as the listing shows it), never reported back as news."""
        if key:
            self.db.execute("INSERT OR IGNORE INTO sent_entries(entry_id,ticket_id,request_id) VALUES(?,?,?)",
                            (str(key), str(ticket_id or ""), request_id))

    def own_entry(self, entry_id) -> bool:
        return self.one("SELECT 1 FROM sent_entries WHERE entry_id=?", (str(entry_id),)) is not None

    def pending_reports(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM journal WHERE report_state='pending' AND state IN "
                         "('delivered','accepted','failed','uncertain') AND report_due<=? ORDER BY finished_at LIMIT ?",
                         (time.time(), limit))

    def mark_report(self, request_id: str, state: str, *, retry_in: float = 0) -> None:
        self.db.execute("UPDATE journal SET report_state=?, report_due=? WHERE request_id=?",
                        (state, time.time() + retry_in, request_id))
