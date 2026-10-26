"""SQLite store for the calendar skill (schema v1, plan 0.2 §1.4).

One file in the skill state dir is shared by the per-call child and the
companion; WAL + busy_timeout + short transactions keep them out of each
other's way, and intent leases (not a lock manager) coordinate one external
operation between them.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from model import (
    DEFAULT_LOCAL_CALENDAR_ID, DEFAULT_LOCAL_CALENDAR_NAME, INTENT_DONE, INTENT_FAILED, INTENT_PENDING, LOCAL_ACCOUNT_ID, iso_utc, new_id, now_utc, parse_stored,
)

SCHEMA_VERSION = 1
DB_FILENAME = "calendar.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, alias TEXT NOT NULL DEFAULT '', login TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok', last_sync TEXT, last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calendars (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, provider TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '', href TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, tz TEXT NOT NULL DEFAULT '',
    writable INTEGER NOT NULL DEFAULT 1, access_role TEXT NOT NULL DEFAULT 'owner',
    role_visible INTEGER NOT NULL DEFAULT 1, role_busy INTEGER NOT NULL DEFAULT 1, role_publish INTEGER NOT NULL DEFAULT 0,
    publish_mode TEXT NOT NULL DEFAULT 'full', sort_order INTEGER NOT NULL DEFAULT 0, is_default INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, calendar_id TEXT NOT NULL, uid TEXT NOT NULL DEFAULT '', external_id TEXT NOT NULL DEFAULT '',
    href TEXT NOT NULL DEFAULT '', etag TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'shown', availability TEXT NOT NULL DEFAULT 'busy', is_primary INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
    start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, tz TEXT NOT NULL DEFAULT '', all_day INTEGER NOT NULL DEFAULT 0,
    rrule TEXT NOT NULL DEFAULT '', rdates TEXT NOT NULL DEFAULT '', exdates TEXT NOT NULL DEFAULT '',
    recurrence_id TEXT NOT NULL DEFAULT '', master_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'confirmed', organizer TEXT NOT NULL DEFAULT '', attendees_json TEXT NOT NULL DEFAULT '[]',
    my_response TEXT NOT NULL DEFAULT '', reminders_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'local', link_group_id TEXT NOT NULL DEFAULT '', sync_state TEXT NOT NULL DEFAULT 'synced',
    raw_payload TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
CREATE INDEX IF NOT EXISTS idx_events_window ON events(calendar_id, start_utc, end_utc);
CREATE INDEX IF NOT EXISTS idx_events_uid ON events(uid);
CREATE INDEX IF NOT EXISTS idx_events_link ON events(link_group_id);
CREATE INDEX IF NOT EXISTS idx_events_master ON events(master_id);
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, account_id TEXT NOT NULL, calendar_id TEXT NOT NULL, event_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'this', payload_json TEXT NOT NULL DEFAULT '{}', expected_etag TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
    lease_until TEXT, lease_owner TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_intents_event ON intents(event_id);
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, occurrence_start_utc TEXT NOT NULL,
    recurrence_id TEXT NOT NULL DEFAULT '', offset_min INTEGER NOT NULL,
    fire_at_utc TEXT NOT NULL, notice_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'scheduled',
    sent_at TEXT, detail TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reminders_fire ON reminders(state, fire_at_utc);
CREATE INDEX IF NOT EXISTS idx_reminders_event ON reminders(event_id, occurrence_start_utc, offset_min);
CREATE TABLE IF NOT EXISTS sync_state (
    calendar_id TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '', cursor_kind TEXT NOT NULL DEFAULT '',
    window_start TEXT, window_end TEXT, last_ok_at TEXT, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
"""

EVENT_COLUMNS = (
    "id", "calendar_id", "uid", "external_id", "href", "etag", "visibility", "availability", "is_primary", "title",
    "description", "location", "start_utc", "end_utc", "tz", "all_day", "rrule", "rdates", "exdates", "recurrence_id",
    "master_id", "status", "organizer", "attendees_json", "my_response", "reminders_json", "origin", "link_group_id",
    "sync_state", "raw_payload", "created_at", "updated_at", "deleted_at",
)


def _ts() -> str:
    return iso_utc(now_utc())


class Store:
    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, DB_FILENAME)
        self._transaction_connection = None
        self._init()

    # ── connection / schema ─────────────────────────────────────────

    @contextmanager
    def _conn(self):
        if self._transaction_connection is not None:
            yield self._transaction_connection
            return
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_conn(self):
        """Join a bound command transaction, or own one short write transaction."""
        with self._conn() as c:
            if self._transaction_connection is None:
                c.execute('BEGIN IMMEDIATE')
            yield c

    @contextmanager
    def transaction(self):
        """An explicit local-only view: helpers share its connection, not this Store.

        The caller stages a whole command and exits before contacting providers.
        Other threads/processes using the ordinary Store keep independent connections.
        """
        with self._write_conn() as c:
            local = object.__new__(Store)
            local.state_dir, local.path = self.state_dir, self.path
            local._transaction_connection = c
            yield local

    def _init(self) -> None:
        with self._conn() as c:
            legacy = self._is_prototype_schema(c)
            if legacy:
                self._migrate_prototype(c)
            c.executescript(_SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(reminders)").fetchall()}
            if "attempts" not in cols:
                c.execute("ALTER TABLE reminders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "recurrence_id" not in cols:
                c.execute("ALTER TABLE reminders ADD COLUMN recurrence_id TEXT NOT NULL DEFAULT ''")
            row = c.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                c.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._ensure_local(c)
            if legacy:
                self._import_prototype_rows(c)

    @staticmethod
    def _is_prototype_schema(c: sqlite3.Connection) -> bool:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "schema_version" in tables or "events" not in tables:
            return False
        cols = {r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()}
        return "remind_min" in cols and "start_utc" not in cols

    def _migrate_prototype(self, c: sqlite3.Connection) -> None:
        """Keep the owner's prototype data: back the file up, park old tables as legacy_*."""
        backup_dir = os.path.join(self.state_dir, "backup")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        try:
            shutil.copy2(self.path, os.path.join(backup_dir, f"calendar-prototype-{stamp}.sqlite3"))
        except OSError:
            pass
        for table in ("events", "calendars", "meta"):
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists:
                c.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
        for idx in ("idx_events_start",):
            c.execute(f"DROP INDEX IF EXISTS {idx}")

    def _import_prototype_rows(self, c: sqlite3.Connection) -> None:
        """Local prototype events become events of the default local calendar.

        The Yandex cache is NOT imported: external events stay the provider's
        and are re-read after the account is connected again.
        """
        try:
            rows = c.execute("SELECT * FROM legacy_events WHERE source = 'local'").fetchall()
        except sqlite3.DatabaseError:
            return
        ts = _ts()
        for r in rows:
            r = dict(r)
            try:
                start = datetime.fromisoformat(str(r.get("start")))
                end = datetime.fromisoformat(str(r.get("end")))
            except ValueError:
                continue
            reminders = [int(r["remind_min"])] if int(r.get("remind_min") or 0) > 0 else []
            c.execute(
                "INSERT OR IGNORE INTO events (id, calendar_id, uid, title, description, location, start_utc, end_utc, tz, all_day,"
                " reminders_json, origin, raw_payload, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', '', ?, ?)",
                (
                    str(r.get("id") or new_id("evt")), DEFAULT_LOCAL_CALENDAR_ID, str(r.get("uid") or ""),
                    str(r.get("title") or ""), str(r.get("description") or ""), str(r.get("location") or ""),
                    iso_utc(start), iso_utc(end), "", 1 if r.get("all_day") else 0, json.dumps(reminders),
                    str(r.get("created") or ts), str(r.get("updated") or ts),
                ),
            )
        c.execute("INSERT OR REPLACE INTO settings (key, value_json) VALUES ('migrated_from_prototype', ?)", (json.dumps(ts),))

    def _ensure_local(self, c: sqlite3.Connection) -> None:
        ts = _ts()
        c.execute(
            "INSERT OR IGNORE INTO accounts (id, provider, alias, login, status, created_at, updated_at) VALUES (?, 'local', 'Уроборос', '', 'ok', ?, ?)",
            (LOCAL_ACCOUNT_ID, ts, ts),
        )
        c.execute(
            "INSERT OR IGNORE INTO calendars (id, account_id, provider, external_id, name, writable, role_visible, role_busy, role_publish, is_default, updated_at)"
            " VALUES (?, ?, 'local', 'personal', ?, 1, 1, 1, 0, 1, ?)",
            (DEFAULT_LOCAL_CALENDAR_ID, LOCAL_ACCOUNT_ID, DEFAULT_LOCAL_CALENDAR_NAME, ts),
        )

    # ── settings ────────────────────────────────────────────────────

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return default

    def delete_setting(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM settings WHERE key = ?", (key,))

    def set_setting(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO settings (key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                      (key, json.dumps(value, ensure_ascii=False)))

    def all_settings(self) -> Dict[str, Any]:
        with self._conn() as c:
            rows = c.execute("SELECT key, value_json FROM settings").fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except ValueError:
                pass
        return out

    # ── accounts / calendars ────────────────────────────────────────

    def upsert_account(self, account: Dict[str, Any]) -> None:
        ts = _ts()
        with self._conn() as c:
            c.execute(
                "INSERT INTO accounts (id, provider, alias, login, status, last_sync, last_error, created_at, updated_at)"
                " VALUES (:id, :provider, :alias, :login, :status, :last_sync, :last_error, :ts, :ts)"
                " ON CONFLICT(id) DO UPDATE SET alias=excluded.alias, login=excluded.login, status=excluded.status, updated_at=excluded.updated_at",
                {"id": account["id"], "provider": account["provider"], "alias": account.get("alias") or "",
                 "login": account.get("login") or "", "status": account.get("status") or "ok",
                 "last_sync": account.get("last_sync"), "last_error": account.get("last_error") or "", "ts": ts},
            )

    def set_account_status(self, account_id: str, status: str, error: str = "", synced: bool = False) -> None:
        with self._conn() as c:
            if synced:
                c.execute("UPDATE accounts SET status=?, last_error=?, last_sync=?, updated_at=? WHERE id=?",
                          (status, error, _ts(), _ts(), account_id))
            else:
                c.execute("UPDATE accounts SET status=?, last_error=?, updated_at=? WHERE id=?", (status, error, _ts(), account_id))

    def list_accounts(self, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as c:
            if provider:
                rows = c.execute("SELECT * FROM accounts WHERE provider=? ORDER BY id", (provider,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM accounts ORDER BY provider, id").fetchall()
        return [dict(r) for r in rows]

    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def delete_account(self, account_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE calendars SET deleted_at=? WHERE account_id=? AND deleted_at IS NULL", (_ts(), account_id))
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def upsert_calendar(self, cal: Dict[str, Any]) -> None:
        """Insert a calendar or refresh its provider-owned facts; owner roles are kept."""
        ts = _ts()
        with self._conn() as c:
            c.execute(
                "INSERT INTO calendars (id, account_id, provider, external_id, href, name, tz, writable, access_role,"
                " role_visible, role_busy, role_publish, publish_mode, sort_order, is_default, deleted_at, updated_at)"
                " VALUES (:id, :account_id, :provider, :external_id, :href, :name, :tz, :writable, :access_role,"
                " :role_visible, :role_busy, :role_publish, :publish_mode, :sort_order, :is_default, NULL, :ts)"
                " ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id, href=excluded.href, name=excluded.name,"
                " tz=excluded.tz, writable=excluded.writable, access_role=excluded.access_role, deleted_at=NULL, updated_at=excluded.updated_at",
                {
                    "id": cal["id"], "account_id": cal["account_id"], "provider": cal["provider"],
                    "external_id": cal.get("external_id") or "", "href": cal.get("href") or "", "name": cal.get("name") or cal["id"],
                    "tz": cal.get("tz") or "", "writable": 1 if cal.get("writable", True) else 0,
                    "access_role": cal.get("access_role") or "owner",
                    "role_visible": 1 if cal.get("role_visible", True) else 0,
                    "role_busy": 1 if cal.get("role_busy", True) else 0,
                    "role_publish": 1 if cal.get("role_publish", False) else 0,
                    "publish_mode": cal.get("publish_mode") or "full", "sort_order": int(cal.get("sort_order") or 0),
                    "is_default": 1 if cal.get("is_default") else 0, "ts": ts,
                },
            )

    def update_calendar(self, calendar_id: str, fields: Dict[str, Any]) -> None:
        allowed = {"name", "role_visible", "role_busy", "role_publish", "publish_mode", "sort_order", "is_default", "tz", "deleted_at"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        data["updated_at"] = _ts()
        sets = ", ".join(f"{k}=:{k}" for k in data)
        data["id"] = calendar_id
        with self._conn() as c:
            c.execute(f"UPDATE calendars SET {sets} WHERE id=:id", data)

    def list_calendars(self, include_deleted: bool = False, provider: Optional[str] = None, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, args = [], []
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if provider:
            clauses.append("provider=?")
            args.append(provider)
        if account_id:
            clauses.append("account_id=?")
            args.append(account_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM calendars{where} ORDER BY provider, sort_order, name", args).fetchall()
        return [dict(r) for r in rows]

    def get_calendar(self, calendar_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM calendars WHERE id=?", (calendar_id,)).fetchone()
        return dict(row) if row else None

    def default_calendar(self) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM calendars WHERE is_default=1 AND deleted_at IS NULL ORDER BY provider LIMIT 1").fetchone()
            if row is None:
                row = c.execute("SELECT * FROM calendars WHERE id=?", (DEFAULT_LOCAL_CALENDAR_ID,)).fetchone()
        return dict(row)

    # ── events ──────────────────────────────────────────────────────

    def insert_event(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        ts = _ts()
        row = {k: ev.get(k) for k in EVENT_COLUMNS}
        row.setdefault("id", None)
        row["id"] = row["id"] or new_id("evt")
        row["created_at"] = row.get("created_at") or ts
        row["updated_at"] = ts
        for key, default in (("uid", ""), ("external_id", ""), ("href", ""), ("etag", ""), ("visibility", "shown"), ("is_primary", 1),
                             ("availability", "busy"), ("title", ""), ("description", ""), ("location", ""), ("tz", ""), ("all_day", 0),
                             ("rrule", ""), ("rdates", ""), ("exdates", ""), ("recurrence_id", ""), ("master_id", ""), ("status", "confirmed"),
                             ("organizer", ""), ("attendees_json", "[]"), ("my_response", ""), ("reminders_json", "[]"), ("origin", "local"),
                             ("link_group_id", ""), ("sync_state", "synced"), ("raw_payload", "")):
            if row.get(key) is None:
                row[key] = default
        row["all_day"] = 1 if row["all_day"] else 0
        cols = ", ".join(EVENT_COLUMNS)
        marks = ", ".join(f":{k}" for k in EVENT_COLUMNS)
        with self._conn() as c:
            c.execute(f"INSERT OR REPLACE INTO events ({cols}) VALUES ({marks})", row)
        return row

    def update_event(self, event_id: str, fields: Dict[str, Any]) -> None:
        data = {k: v for k, v in fields.items() if k in EVENT_COLUMNS and k != "id"}
        if not data:
            return
        if "all_day" in data:
            data["all_day"] = 1 if data["all_day"] else 0
        data["updated_at"] = _ts()
        sets = ", ".join(
            "sync_state=CASE WHEN sync_state='pending_delete' OR"
            " (SELECT kind FROM intents WHERE event_id=:id AND state!='resolved' ORDER BY rowid DESC LIMIT 1)='delete'"
            " THEN 'pending_delete' ELSE :sync_state END"
            if k == "sync_state" and data[k] == "synced" else f"{k}=:{k}"
            for k in data
        )
        data["id"] = event_id
        with self._conn() as c:
            c.execute(f"UPDATE events SET {sets} WHERE id=:id", data)

    @staticmethod
    def _unsettled_owner_write(c: sqlite3.Connection, event_id: str, sync_state: str) -> bool:
        """An accepted owner write the provider has not settled: display state, an open intent, or a failed last intent.

        The one rule a provider observation obeys before it overwrites, retires or deletes a row. Intents are read as
        well as the state because an older ACK can already have set the display state to ``synced``."""
        if sync_state in ("pending", "pending_delete", "conflict", "failed"):
            return True
        if c.execute("SELECT 1 FROM intents WHERE event_id=? AND state IN ('pending','conflict') LIMIT 1", (event_id,)).fetchone():
            return True
        last = c.execute("SELECT state FROM intents WHERE event_id=? ORDER BY rowid DESC LIMIT 1", (event_id,)).fetchone()
        return bool(last and last[0] == "failed")

    @staticmethod
    def _series_closed(c: sqlite3.Connection, master_id: str) -> bool:
        """The series a stored override belongs to is gone, deleted, or being deleted by the owner."""
        master = c.execute("SELECT sync_state, deleted_at FROM events WHERE id=?", (master_id,)).fetchone()
        if master is None or master["deleted_at"] or master["sync_state"] == "pending_delete":
            return True
        latest = c.execute("SELECT kind FROM intents WHERE event_id=? AND state!='resolved' ORDER BY rowid DESC LIMIT 1",
                           (master_id,)).fetchone()
        return latest is not None and latest["kind"] == "delete"

    def sync_basis(self, calendar_id):
        """Local rows before the network read; a late feed cannot undo a newer ACK."""
        with self._conn() as c:
            return {r['id']: dict(r) for r in c.execute(
                "WITH roots AS (SELECT id FROM events WHERE calendar_id=? OR "
                "(link_group_id!='' AND link_group_id IN (SELECT link_group_id FROM events WHERE calendar_id=?))) "
                "SELECT * FROM events WHERE id IN roots OR master_id IN roots",
                (calendar_id, calendar_id))}

    @staticmethod
    def _same_observation(row, observed):
        return observed is None or bool(row) and all(row[k] == value for k, value in observed.items() if k in EVENT_COLUMNS)

    def insert_provider_master(self, payload):
        with self.transaction() as tx:
            if tx.find_by_external(payload['calendar_id'], payload.get('external_id') or '', payload.get('href') or '', payload.get('uid') or ''):
                return None
            return tx.insert_event(payload)

    def apply_provider_snapshot(self, event_id: str, fields: Dict[str, Any], observed=None) -> bool:
        """A sync observation cannot overwrite a still-unconfirmed owner write, nor re-attach an override to a closed series.

        The checks and the snapshot write share one transaction, including the interval where an older write ACK
        has set the display state to synced.
        """
        data = {k: v for k, v in fields.items() if k in EVENT_COLUMNS and k != "id"}
        with self._write_conn() as c:
            row = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None or not self._same_observation(row, observed) or self._unsettled_owner_write(c, event_id, row["sync_state"]):
                return False
            if row["master_id"] and self._series_closed(c, row["master_id"]):
                return False
            data.update(updated_at=_ts(), deleted_at=None)
            sets = ", ".join(f"{key}=?" for key in data)
            c.execute(f"UPDATE events SET {sets} WHERE id=?", (*data.values(), event_id))
        return True

    def provider_absence_blocker(self, event_id: str, observed: Optional[Dict[str, Any]] = None) -> str:
        """Why a provider-side absence may not remove this row *now*; '' means it may.

        Reads the row and its stored overrides on this view's connection: call it in the transaction that removes the
        row. ``observed`` binds the provider's answer to the identity it was about."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None or row["deleted_at"]:
                return "gone"
            if not self._same_observation(row, observed):
                return "rebound"
            family = [(event_id, row["sync_state"])] + [
                (r["id"], r["sync_state"]) for r in c.execute("SELECT id, sync_state FROM events WHERE master_id=? AND deleted_at IS NULL", (event_id,))]
            if any(self._unsettled_owner_write(c, eid, state) for eid, state in family):
                return "unsettled"
        return ""

    def retire_provider_absent(self, event_id: str, observed: Optional[Dict[str, Any]] = None) -> bool:
        """Provider confirmed this resource gone: soft-delete the row and its overrides unless an owner write is unsettled."""
        with self.transaction() as tx:
            if tx.provider_absence_blocker(event_id, observed):
                return False
            ts = _ts()
            with tx._conn() as c:
                c.execute("UPDATE events SET deleted_at=?, updated_at=? WHERE (id=? OR master_id=?) AND deleted_at IS NULL",
                          (ts, ts, event_id, event_id))
        return True

    def retire_synced_exception(self, exception_id: str, observed=None) -> bool:
        """Retire a provider-derived override the server no longer lists, only while it is still exactly what the provider gave us."""
        with self.transaction() as tx:
            with tx._conn() as c:
                row = c.execute("SELECT * FROM events WHERE id=? AND deleted_at IS NULL", (exception_id,)).fetchone()
                if (row is None or not row["master_id"] or not self._same_observation(row, observed)
                        or self._unsettled_owner_write(c, exception_id, row["sync_state"])):
                    return False
                ts = _ts()
                c.execute("UPDATE events SET deleted_at=?, updated_at=? WHERE id=?", (ts, ts, exception_id))
        return True

    def insert_provider_exception(self, master_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Import one provider override exactly once, only into an open series; None when the slot is taken or the series closed.

        The one-row-per-slot and series-open checks run in the transaction that inserts, so an owner command or another
        pass that committed first wins instead of leaving a twin or an orphan."""
        slot = str(payload.get("recurrence_id") or "")
        with self.transaction() as tx:
            with tx._conn() as c:
                master = c.execute('SELECT sync_state FROM events WHERE id=?', (master_id,)).fetchone()
                if (not slot or master is None or self._unsettled_owner_write(c, master_id, master['sync_state'])
                        or self._series_closed(c, master_id)
                        or c.execute("SELECT 1 FROM events WHERE master_id=? AND recurrence_id=? AND deleted_at IS NULL LIMIT 1",
                                     (master_id, slot)).fetchone()):
                    return None
            return tx.insert_event({**payload, "master_id": master_id})

    def advance_resource_etag(self, master_id: str, previous: str, current: str) -> None:
        """Carry known conditional CalDAV writes to queued sibling resource writes.

        Only a receipt of our own write may advance a matching expected version;
        importing a provider observation must never rebase an owner's intent.
        """
        if not previous or not current:
            return
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE events SET etag=? WHERE id=? AND etag=?", (current, master_id, previous))
            c.execute("UPDATE intents SET expected_etag=? WHERE state='pending' AND expected_etag=? "
                      "AND event_id IN (SELECT id FROM events WHERE id=? OR master_id=?)",
                      (current, previous, master_id, master_id))

    def record_created_event(self, event_id: str, result: Dict[str, Any]) -> None:
        """Publish the provider identity without overwriting a concurrent deletion request."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE events SET external_id=?, href=?, etag=?, updated_at=?,"
                      " sync_state=CASE WHEN sync_state='pending_delete' OR"
                      " (SELECT kind FROM intents WHERE event_id=? AND state!='resolved' ORDER BY rowid DESC LIMIT 1)='delete'"
                      " THEN 'pending_delete' ELSE 'synced' END WHERE id=?",
                      (result.get("external_id") or "", result.get("href") or "", result.get("etag") or "",
                       _ts(), event_id, event_id))

    def events_awaiting_identity(self, calendar_id: str) -> List[Dict[str, Any]]:
        """Locally created rows whose provider identity was never reported (a lost create answer)."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE calendar_id=? AND master_id='' AND external_id='' AND href=''"
                             " AND origin='local' AND deleted_at IS NULL", (calendar_id,)).fetchall()
        return [dict(r) for r in rows]

    def adopt_created_identity(self, event_id: str, identity: Dict[str, Any]) -> bool:
        """Sync found what a lost create wrote: record it only while the row still has no identity.

        Display state and a requested deletion are kept; a delete parked as a conflict because the identity was
        unknown (the only conflict an identity-less delete can reach) is queued again in the same transaction.
        """
        with self._write_conn() as c:
            cur = c.execute("UPDATE events SET external_id=?, href=?, etag=?, updated_at=? WHERE id=? AND external_id='' AND href=''",
                            (identity.get("external_id") or "", identity.get("href") or "", identity.get("etag") or "", _ts(), event_id))
            if cur.rowcount != 1:
                return False
            latest = c.execute("SELECT id, kind, state FROM intents WHERE event_id=? AND state!='resolved' ORDER BY rowid DESC LIMIT 1",
                               (event_id,)).fetchone()
            if latest is not None and latest["kind"] == "delete" and latest["state"] == "conflict":
                c.execute("UPDATE intents SET state=?, result_json=?, lease_until=NULL, lease_owner='', next_attempt_at=?, updated_at=?,"
                          " expected_etag=CASE WHEN expected_etag='' THEN ? ELSE expected_etag END WHERE id=?",
                          (INTENT_PENDING, json.dumps({"note": "provider identity found by sync"}), _ts(), _ts(), identity.get('etag') or '', latest["id"]))
        return True

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT e.*, c.name AS calendar_name, c.provider AS provider, c.account_id AS account_id"
                            " FROM events e LEFT JOIN calendars c ON c.id = e.calendar_id WHERE e.id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def delete_event(self, event_id: str, hard: bool = False) -> bool:
        with self._conn() as c:
            if hard:
                cur = c.execute("DELETE FROM events WHERE id=?", (event_id,))
            else:
                cur = c.execute("UPDATE events SET deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL", (_ts(), _ts(), event_id))
        return cur.rowcount > 0

    def confirm_pending_delete(self, event_id: str, observed=None) -> bool:
        """Provider absence closes only the selected row and its pending delete intent, atomically."""
        with self._write_conn() as c:
            row = c.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if row is None or not self._same_observation(row, observed):
                return False
            removed = c.execute("DELETE FROM events WHERE id=? AND"
                                " (sync_state='pending_delete' OR (sync_state='conflict' AND"
                                " (SELECT kind FROM intents WHERE event_id=? AND state!='resolved' ORDER BY rowid DESC LIMIT 1)='delete'))"
                                " AND NOT EXISTS (SELECT 1 FROM intents WHERE event_id=? AND kind='create' AND state!=?)",
                                (event_id, event_id, event_id, INTENT_DONE))
            if not removed.rowcount:
                return False
            c.execute("UPDATE intents SET state=?, result_json=?, lease_until=NULL, lease_owner='', next_attempt_at=NULL, updated_at=?"
                      " WHERE event_id=? AND kind='delete' AND state=?",
                      (INTENT_DONE, json.dumps({"note": "provider confirmed absent"}), _ts(), event_id, INTENT_PENDING))
        return True

    def window(self, start: datetime, end: datetime, calendar_ids: Optional[Sequence[str]] = None,
               include_hidden: bool = True, include_masters: bool = True) -> List[Dict[str, Any]]:
        """Rows overlapping [start, end) plus (optionally) recurrence masters that may expand into it."""
        clauses = ["e.deleted_at IS NULL", "c.deleted_at IS NULL",
                   "(e.master_id='' OR EXISTS (SELECT 1 FROM events m WHERE m.id=e.master_id AND m.deleted_at IS NULL))"]
        args: List[Any] = []
        if calendar_ids:
            clauses.append("e.calendar_id IN (%s)" % ",".join("?" for _ in calendar_ids))
            args.extend(calendar_ids)
        if not include_hidden:
            clauses.append("e.visibility = 'shown'")
        time_clause = "(e.start_utc < ? AND e.end_utc > ? AND e.rrule = '')"
        targs: List[Any] = [iso_utc(end), iso_utc(start)]
        if include_masters:
            # A moved exception can enter this window before its master's DTSTART. Include that
            # master so expansion overlays its reminder rules rather than treating the exception
            # as a standalone event with the wrong default offset.
            time_clause = ("(" + time_clause + " OR (e.rrule != '' AND e.start_utc < ? AND e.master_id = '')"
                           " OR (e.rrule != '' AND e.id IN (SELECT master_id FROM events"
                           " WHERE master_id != '' AND deleted_at IS NULL AND start_utc < ? AND end_utc > ?)))")
            targs.extend((iso_utc(end), iso_utc(end), iso_utc(start)))
        clauses.append(time_clause)
        args.extend(targs)
        sql = ("SELECT e.*, c.name AS calendar_name, c.provider AS provider, c.account_id AS account_id,"
               " c.role_visible, c.role_busy FROM events e JOIN calendars c ON c.id = e.calendar_id"
               " WHERE " + " AND ".join(clauses) + " ORDER BY e.start_utc")
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def exceptions_for(self, master_id: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE master_id=? AND deleted_at IS NULL ORDER BY recurrence_id", (master_id,)).fetchall()
        return [dict(r) for r in rows]

    def find_by_external(self, calendar_id: str, external_id: str = "", href: str = "", uid: str = "") -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            if external_id:
                row = c.execute("SELECT * FROM events WHERE calendar_id=? AND external_id=? AND master_id='' LIMIT 1", (calendar_id, external_id)).fetchone()
                if row:
                    return dict(row)
            if href:
                row = c.execute("SELECT * FROM events WHERE calendar_id=? AND href=? AND master_id='' LIMIT 1", (calendar_id, href)).fetchone()
                if row:
                    return dict(row)
            if uid:
                row = c.execute("SELECT * FROM events WHERE calendar_id=? AND uid=? AND master_id='' LIMIT 1", (calendar_id, uid)).fetchone()
                if row:
                    return dict(row)
        return None

    def group_members(self, link_group_id: str) -> List[Dict[str, Any]]:
        if not link_group_id:
            return []
        with self._conn() as c:
            rows = c.execute("SELECT e.*, c.name AS calendar_name, c.provider AS provider, c.account_id AS account_id, c.publish_mode AS publish_mode"
                             " FROM events e JOIN calendars c ON c.id=e.calendar_id WHERE e.link_group_id=? AND e.deleted_at IS NULL",
                             (link_group_id,)).fetchall()
        return [dict(r) for r in rows]

    def group_masters(self, link_group_id: str) -> List[Dict[str, Any]]:
        """Group members that are events in their own right (no exception rows): the targets of propagation and reassignment."""
        return [m for m in self.group_members(link_group_id) if not m.get("master_id")]

    def cancel_intents_for_account(self, account_id: str, reason: str = "account disconnected") -> int:
        with self._conn() as c:
            cur = c.execute("UPDATE intents SET state=?, result_json=?, updated_at=? WHERE account_id=? AND state IN (?, 'conflict')",
                            (INTENT_FAILED, json.dumps({"error": reason}), _ts(), account_id, INTENT_PENDING))
        return cur.rowcount

    # ── intents (durable external operations) ───────────────────────

    def get_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def current_leased_intent(self, leased):
        row = self.get_intent(leased['id'])
        if row is None or row['state'] != INTENT_PENDING:
            return None
        if any(row[k] != leased[k] for k in ('lease_owner', 'lease_until', 'attempts')):
            return None
        expiry = parse_stored(row['lease_until'])
        return row if expiry and expiry > now_utc() else None

    def pin_provider_version(self, leased, external_id, etag):
        """Bind an unversioned intent before its first write; reads perform no rebase."""
        if not external_id or not etag:
            raise ValueError('provider version needs identity and ETag')
        with self.transaction() as local:
            row = local.current_leased_intent(leased)
            if row is None or row['expected_etag']:
                return row
            payload = json.loads(row['payload_json'])
            payload['_provider_external_id'] = external_id
            row.update(expected_etag=etag, payload_json=json.dumps(payload, ensure_ascii=False))
            with local._conn() as c:
                c.execute('UPDATE intents SET expected_etag=?,payload_json=? WHERE id=?',
                          (etag, row['payload_json'], row['id']))
            return row

    def acknowledge_write(self, intent, event, result, expected, shared_resource=False):
        """Commit identity, matching successor versions and DONE from one own ACK."""
        with self.transaction() as local:
            current = local.get_intent(intent['id'])
            if current is None or current['state'] != INTENT_PENDING or any(
                    current[k] != intent[k] for k in ('lease_owner', 'lease_until', 'attempts')):
                return False
            etag = result.get('master_etag') or result.get('etag') or ''
            payload = json.loads(current['payload_json'])
            resource_id = payload.get('resource_id') or ((event.get('master_id') or event['id']) if shared_resource else event['id'])
            with local._conn() as c:
                if etag:
                    # A queued write may predate this ACK, and its row may have
                    # moved to another segment. Match the staged resource, not
                    # wall time or today's parent relationship.
                    c.execute("UPDATE intents SET expected_etag=? WHERE state='pending' AND id!=? "
                              "AND calendar_id=? AND expected_etag=? AND "
                              "COALESCE(json_extract(payload_json,'$.resource_id'),event_id)=?",
                              (etag, intent['id'], intent['calendar_id'], expected, resource_id))
                    c.execute('UPDATE events SET etag=? WHERE id=?', (etag, resource_id))
                fields = {k: result[k] for k in ('external_id','href','etag') if result.get(k)}
                fields['sync_state'] = 'synced'
                local.update_event(event['id'], fields)
                local.settle_intent(intent['id'], INTENT_DONE, {'ok': True, 'etag': etag})
                waiting = c.execute("SELECT kind FROM intents WHERE event_id=? AND state IN ('pending','conflict') ORDER BY rowid DESC LIMIT 1",
                                    (event['id'],)).fetchone()
                if waiting:
                    local.update_event(event['id'], {'sync_state': 'pending_delete' if waiting[0]=='delete' else 'pending'})
        return True

    def add_intent(self, kind: str, account_id: str, calendar_id: str, event_id: str, payload: Dict[str, Any],
                   scope: str = "this", expected_etag: str = "", op_id: str = "") -> Dict[str, Any]:
        ts = now_utc().isoformat(timespec="microseconds")   # intents of one call must order strictly (dependents wait for earlier writes)
        payload = {**(payload or {}), **({"op_id": op_id} if op_id else {})}   # one owner command = one op: its parts share the fate
        payload['desired'] = {k: v for k, v in (self.get_event(event_id) or {}).items()
                              if k in EVENT_COLUMNS and k != 'raw_payload'}
        event = payload['desired'] or {}
        calendar = self.get_calendar(calendar_id) or {}
        if calendar.get('provider') == 'yandex' and event.get('rrule') and not event.get('master_id'):
            # Every master write serializes the same complete desired family.
            # A queued cancellation is already accepted local intent, even
            # before its provider ACK; later commands must not resurrect it.
            payload['cancelled_slots'] = sorted({e['recurrence_id'] for e in self.exceptions_for(event_id)
                if e.get('status') == 'cancelled'})
        payload['resource_id'] = ((event.get('master_id') or event_id)
                                  if calendar.get('provider') == 'yandex' else event_id)
        row = {"id": new_id("int"), "kind": kind, "account_id": account_id, "calendar_id": calendar_id, "event_id": event_id,
               "scope": scope, "payload_json": json.dumps(payload, ensure_ascii=False, default=str), "expected_etag": expected_etag or "",
               "state": INTENT_PENDING, "attempts": 0, "next_attempt_at": ts, "lease_until": None, "lease_owner": "",
               "result_json": "{}", "created_at": ts, "updated_at": ts}
        with self._conn() as c:
            c.execute("INSERT INTO intents (id, kind, account_id, calendar_id, event_id, scope, payload_json, expected_etag, state, attempts,"
                      " next_attempt_at, lease_until, lease_owner, result_json, created_at, updated_at) VALUES (:id, :kind, :account_id,"
                      " :calendar_id, :event_id, :scope, :payload_json, :expected_etag, :state, :attempts, :next_attempt_at, :lease_until,"
                      " :lease_owner, :result_json, :created_at, :updated_at)", row)
        return row

    def lease_intent(self, intent_id: str, owner: str, seconds: int = 90) -> Optional[Dict[str, Any]]:
        """Claim one intent atomically; None when another actor holds a live lease or it is settled."""
        now = now_utc()
        until = iso_utc(now + timedelta(seconds=seconds))
        with self._conn() as c:
            cur = c.execute(
                "UPDATE intents SET lease_owner=?, lease_until=?, attempts=attempts+1, updated_at=?"
                " WHERE id=? AND state=? AND (lease_until IS NULL OR lease_until < ?)",
                (owner, until, iso_utc(now), intent_id, INTENT_PENDING, iso_utc(now)))
            if cur.rowcount != 1:
                return None
            row = c.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def lease_due_intents(self, owner: str, limit: int = 20, seconds: int = 90) -> List[Dict[str, Any]]:
        now = iso_utc(now_utc())
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM intents WHERE state=? AND (lease_until IS NULL OR lease_until < ?)"
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY rowid LIMIT ?",
                (INTENT_PENDING, now, now, limit)).fetchall()
        out = []
        for r in rows:
            leased = self.lease_intent(r["id"], owner, seconds)
            if leased:
                out.append(leased)
        return out

    def park_intent(self, intent_id: str, seconds: int, note: str) -> None:
        """Release a leased intent that only waits for another one: back to pending, the attempt is given back."""
        ts = _ts()
        with self._conn() as c:
            c.execute("UPDATE intents SET state=?, attempts=MAX(attempts-1, 0), lease_until=NULL, lease_owner='', next_attempt_at=?, result_json=?, updated_at=?"
                      " WHERE id=?", (INTENT_PENDING, iso_utc(now_utc() + timedelta(seconds=seconds)), json.dumps({"note": note}), ts, intent_id))

    def settle_intent(self, intent_id: str, state: str, result: Optional[Dict[str, Any]] = None, retry_in_sec: Optional[int] = None) -> None:
        ts = _ts()
        next_at = iso_utc(now_utc() + timedelta(seconds=retry_in_sec)) if retry_in_sec else None
        with self._conn() as c:
            c.execute("UPDATE intents SET state=?, result_json=?, lease_until=NULL, lease_owner='', next_attempt_at=?, updated_at=? WHERE id=?",
                      (state, json.dumps(result or {}, ensure_ascii=False, default=str), next_at, ts, intent_id))

    def intents_for_event(self, event_id: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM intents WHERE event_id=? ORDER BY rowid", (event_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_requested(self, event_id: str) -> bool:
        """Only the latest command's delete survives adapter failure; a later owner edit supersedes it."""
        with self._conn() as c:
            row = c.execute("SELECT kind FROM intents WHERE event_id=? AND state!='resolved' ORDER BY rowid DESC LIMIT 1",
                            (event_id,)).fetchone()
        return row is not None and row["kind"] == "delete"

    def open_intents(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM intents WHERE state IN (?, ?) ORDER BY rowid LIMIT ?",
                             (INTENT_PENDING, "conflict", limit)).fetchall()
        return [dict(r) for r in rows]

    def intent_counts(self, active_only: bool = False) -> Dict[str, int]:
        # The frequently polled widget needs only unsettled counts. Keep the
        # complete history available to explicit status/diagnostic readers.
        where = " WHERE state IN ('pending','conflict','failed')" if active_only else ""
        with self._conn() as c:
            rows = c.execute("SELECT state, COUNT(*) AS n FROM intents" + where + " GROUP BY state").fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    # ── reminders ───────────────────────────────────────────────────

    def schedule_reminder(self, event_id: str, occurrence_start_utc: str, offset_min: int, fire_at_utc: str,
                          notice_id: str, recurrence_id: str = "") -> bool:
        ts = _ts()
        with self._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO reminders (id, event_id, occurrence_start_utc, recurrence_id, offset_min, fire_at_utc, notice_id, state, updated_at)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?)",
                            (new_id("rem"), event_id, occurrence_start_utc, recurrence_id, int(offset_min), fire_at_utc, notice_id, ts))
        return cur.rowcount > 0

    def legacy_reminder_settled(self, event_id: str, occurrence_start_utc: str, offset_min: int) -> bool:
        """An old moved reminder may already have been accepted; prefer a miss over a duplicate on upgrade."""
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM reminders WHERE event_id=? AND occurrence_start_utc=? AND offset_min=?"
                            " AND recurrence_id='' AND state IN ('sent', 'unknown') LIMIT 1",
                            (event_id, occurrence_start_utc, offset_min)).fetchone()
        return row is not None

    def retire_legacy_waiting_series_reminders(self, except_events: Iterable[str] = ()) -> None:
        """Replace identity-less recurring queue entries before planning their replacements.

        A concurrent send reservation wins or loses this one SQLite write: when
        it wins the row is `unknown` and the planner's settled guard prevents a
        duplicate; when it loses the old row cannot be sent. Non-series rows
        retain their unchanged notice IDs.
        """
        with self._conn() as c:
            skipped = list(except_events)
            sql = ("DELETE FROM reminders WHERE recurrence_id='' AND state IN ('scheduled', 'no_channel')"
                   " AND event_id IN (SELECT id FROM events WHERE rrule != '')")
            if skipped:
                sql += " AND event_id NOT IN (" + ",".join("?" for _ in skipped) + ")"
            c.execute(sql, skipped)

    def settled_reminders_for_split(self, event_id: str, first_original: str) -> List[Dict[str, Any]]:
        """Terminal history candidates; legacy rows lack the original slot, so the caller resolves them against exceptions."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM reminders WHERE event_id=? AND state IN ('sent', 'unknown')"
                             " AND (recurrence_id='' OR recurrence_id >= ?)",
                             (event_id, first_original)).fetchall()
        return [dict(row) for row in rows]

    def carry_settled_reminder(self, previous: Dict[str, Any], new_event_id: str, effective: str,
                               original: str, fire_at: str, notice_id: str) -> None:
        """One atomic terminal copy; a split must not rearm a notice already sent or uncertain."""
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO reminders (id, event_id, occurrence_start_utc, recurrence_id,"
                      " offset_min, fire_at_utc, notice_id, state, sent_at, detail, attempts, updated_at)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (new_id("rem"), new_event_id, effective, original, previous["offset_min"], fire_at,
                       notice_id, previous["state"], previous["sent_at"], previous["detail"],
                       previous["attempts"], _ts()))

    def due_reminders(self, now: datetime, limit: Optional[int] = None, include_no_channel: bool = True) -> List[Dict[str, Any]]:
        """Snapshot every due row once; a fixed first page can starve a short upcoming event."""
        states = ("scheduled", "no_channel") if include_no_channel else ("scheduled",)
        marks = ",".join("?" for _ in states)
        with self._conn() as c:
            query = f"SELECT * FROM reminders WHERE state IN ({marks}) AND fire_at_utc <= ? ORDER BY fire_at_utc"
            if limit is not None:
                query += " LIMIT ?"
            rows = c.execute(query, (*states, iso_utc(now), *([limit] if limit is not None else []))).fetchall()
        return [dict(r) for r in rows]

    def reserve_reminder_send(self, reminder_ids: Sequence[str]) -> List[str]:
        """Mark a send's rows uncertain *before* HTTP, in one transaction; returns the ids reserved.

        A row a concurrent edit already removed, replanned or settled is left out, never re-sent."""
        if not reminder_ids:
            return []
        reserved: List[str] = []
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for reminder_id in reminder_ids:
                result = c.execute("UPDATE reminders SET state='unknown', updated_at=? WHERE id=? AND state IN ('scheduled', 'no_channel')",
                                   (_ts(), reminder_id))
                if result.rowcount == 1:
                    reserved.append(reminder_id)
            c.execute("COMMIT")
        return reserved

    def bump_reminder_attempt(self, reminder_id: str, detail: str = "") -> int:
        with self._conn() as c:
            c.execute("UPDATE reminders SET attempts=attempts+1, detail=?, updated_at=? WHERE id=?", (detail[:500], _ts(), reminder_id))
            row = c.execute("SELECT attempts FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        return int(row["attempts"]) if row else 0

    def upcoming_reminders(self, now: datetime, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT r.*, e.title FROM reminders r LEFT JOIN events e ON e.id=r.event_id"
                             " WHERE r.state IN ('scheduled', 'no_channel', 'sent', 'unknown') AND r.fire_at_utc >= ? ORDER BY r.fire_at_utc LIMIT ?",
                             (iso_utc(now - timedelta(hours=1)), limit)).fetchall()
        return [dict(r) for r in rows]

    def unknown_reminder_count(self) -> int:
        """Durable uncertain sends remain visible after they leave the upcoming window."""
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM reminders WHERE state='unknown'").fetchone()[0])

    def mark_reminder(self, reminder_id: str, state: str, detail: str = "") -> None:
        ts = _ts()
        with self._conn() as c:
            c.execute("UPDATE reminders SET state=?, detail=?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END, updated_at=? WHERE id=?",
                      (state, detail[:500], state, ts, ts, reminder_id))

    def withdraw_waiting_reminders(self, after: datetime, until: datetime, keep: Set[str], keep_events: Iterable[str] = ()) -> int:
        """Delete not-yet-attempted rows due in (after, until] whose notice the current plan no longer wants."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT id, notice_id, event_id FROM reminders WHERE state IN ('scheduled', 'no_channel') AND fire_at_utc > ? AND fire_at_utc <= ?",
                             (iso_utc(after), iso_utc(until))).fetchall()
            protected = set(keep_events)
            stale = [r["id"] for r in rows if r["notice_id"] not in keep and r["event_id"] not in protected]
            for reminder_id in stale:
                c.execute("DELETE FROM reminders WHERE id=? AND state IN ('scheduled', 'no_channel')", (reminder_id,))
            c.execute("COMMIT")
        return len(stale)

    def drop_scheduled_reminders(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM reminders WHERE state IN ('scheduled', 'no_channel')")

    def drop_reminders_for_occurrence(self, event_id: str, occurrence_start_utc: str) -> None:
        if not occurrence_start_utc:
            return
        with self._conn() as c:
            c.execute("DELETE FROM reminders WHERE event_id=? AND occurrence_start_utc=? AND state IN ('scheduled', 'no_channel')",
                      (event_id, occurrence_start_utc))

    def drop_reminders_for(self, event_id: str, only_future: bool = True) -> None:
        with self._conn() as c:
            if only_future:
                c.execute("DELETE FROM reminders WHERE event_id=? AND state IN ('scheduled', 'no_channel')", (event_id,))
            else:
                c.execute("DELETE FROM reminders WHERE event_id=?", (event_id,))

    # ── sync state ──────────────────────────────────────────────────

    def get_sync_state(self, calendar_id: str) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sync_state WHERE calendar_id=?", (calendar_id,)).fetchone()
        return dict(row) if row else {"calendar_id": calendar_id, "cursor": "", "cursor_kind": "", "last_ok_at": None, "last_error": ""}

    def set_sync_state(self, calendar_id: str, **fields: Any) -> None:
        allowed = {"cursor", "cursor_kind", "window_start", "window_end", "last_ok_at", "last_error"}
        data = {k: v for k, v in fields.items() if k in allowed}
        data["updated_at"] = _ts()
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO sync_state (calendar_id, updated_at) VALUES (?, ?)", (calendar_id, data["updated_at"]))
            sets = ", ".join(f"{k}=:{k}" for k in data)
            data["calendar_id"] = calendar_id
            c.execute(f"UPDATE sync_state SET {sets} WHERE calendar_id=:calendar_id", data)

    def counts(self) -> Dict[str, int]:
        with self._conn() as c:
            ev = c.execute("SELECT COUNT(*) FROM events WHERE deleted_at IS NULL").fetchone()[0]
            cal = c.execute("SELECT COUNT(*) FROM calendars WHERE deleted_at IS NULL").fetchone()[0]
        return {"events": int(ev), "calendars": int(cal)}
