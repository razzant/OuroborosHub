"""Atomic provider-page checkpoints, immutable observations and reply custody."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, state_dir, *, readonly=False):
        self.root = Path(state_dir)
        self.lock = threading.RLock()
        if readonly:
            path = self.root / "transport.sqlite3"
            # A status query must not create the runtime or initialize its schema.
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                      timeout=2, check_same_thread=False) if path.is_file() else None
            if self.db:
                self.db.row_factory = sqlite3.Row
                self.db.execute("PRAGMA query_only=ON")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "transport.sqlite3", timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(key TEXT PRIMARY KEY, revision INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, value TEXT NOT NULL, observed TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL, event TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
            due REAL NOT NULL DEFAULT 0, response TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS refs(ref TEXT PRIMARY KEY, event_id TEXT NOT NULL,
            kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', due REAL NOT NULL DEFAULT 0,
            response TEXT, reporting INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS envelopes(id INTEGER PRIMARY KEY, event_id TEXT NOT NULL,
            ref TEXT NOT NULL, value TEXT NOT NULL, observed TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outputs(id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
            value TEXT NOT NULL, state TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
            intent TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', due REAL NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS parts(delivery_id TEXT NOT NULL, part INTEGER NOT NULL,
            body TEXT NOT NULL, marker TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            receipt TEXT, PRIMARY KEY(delivery_id, part));
        CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, value TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', due REAL NOT NULL DEFAULT 0, response TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, stream TEXT NOT NULL,
            spec TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', due REAL NOT NULL DEFAULT 0,
            UNIQUE(stream, spec));
        CREATE TABLE IF NOT EXISTS gaps(scope TEXT PRIMARY KEY, code TEXT NOT NULL,
            detail TEXT NOT NULL, opened TEXT NOT NULL, resolved TEXT);
        CREATE TABLE IF NOT EXISTS state_counts(table_name TEXT NOT NULL,state TEXT NOT NULL,n INTEGER NOT NULL,
            PRIMARY KEY(table_name,state));
        CREATE TABLE IF NOT EXISTS http_cache(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications(key TEXT PRIMARY KEY,repo_id INTEGER,number INTEGER,value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS notifications_target ON notifications(repo_id,number);
        """)
        if "used" not in {r["name"] for r in self.db.execute("PRAGMA table_info(http_cache)")}:
            self.db.execute("ALTER TABLE http_cache ADD COLUMN used REAL NOT NULL DEFAULT 0")
        self.db.execute("CREATE INDEX IF NOT EXISTS http_cache_used ON http_cache(used)")
        for table, default in (("parts", 1), ("outbox", 1)):
            if "report_due" not in {r["name"] for r in self.db.execute("PRAGMA table_info(" + table + ")")}:
                self.db.execute("ALTER TABLE " + table + " ADD COLUMN report_due INTEGER NOT NULL DEFAULT " + str(default))
            self.db.execute("CREATE INDEX IF NOT EXISTS " + table + "_report_due ON " + table + "(report_due,state)")
        if "body_hash" not in {r["name"] for r in self.db.execute("PRAGMA table_info(parts)")}:
            self.db.execute("ALTER TABLE parts ADD COLUMN body_hash TEXT NOT NULL DEFAULT ''")
            for row in self.db.execute("SELECT delivery_id,part,body FROM parts").fetchall():
                self.db.execute("UPDATE parts SET body_hash=? WHERE delivery_id=? AND part=?", (digest(row["body"]), row["delivery_id"], row["part"]))
        self.db.execute("CREATE INDEX IF NOT EXISTS parts_body_hash ON parts(body_hash)")
        self.db.execute("CREATE INDEX IF NOT EXISTS refs_event ON refs(event_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS outbox_event_state ON outbox(event_id,state)")
        for table in ("inbox", "refs", "outputs", "outbox", "reports", "jobs"):
            self.db.execute("CREATE INDEX IF NOT EXISTS " + table + "_state ON " + table + "(state)")
            if table != "outputs":
                self.db.execute("CREATE INDEX IF NOT EXISTS " + table + "_due ON " + table + "(state,due)")
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table + "_counts_insert",)).fetchone():
                self.db.execute("INSERT OR REPLACE INTO state_counts SELECT ?,state,COUNT(*) FROM " + table + " GROUP BY state", (table,))
                self.db.executescript("""
                CREATE TRIGGER %s_counts_insert AFTER INSERT ON %s BEGIN
                  INSERT INTO state_counts VALUES('%s',NEW.state,1) ON CONFLICT(table_name,state) DO UPDATE SET n=n+1;
                END;
                CREATE TRIGGER %s_counts_delete AFTER DELETE ON %s BEGIN
                  UPDATE state_counts SET n=n-1 WHERE table_name='%s' AND state=OLD.state;
                END;
                CREATE TRIGGER %s_counts_update AFTER UPDATE OF state ON %s WHEN OLD.state!=NEW.state BEGIN
                  UPDATE state_counts SET n=n-1 WHERE table_name='%s' AND state=OLD.state;
                  INSERT INTO state_counts VALUES('%s',NEW.state,1) ON CONFLICT(table_name,state) DO UPDATE SET n=n+1;
                END;
                """ % (table, table, table, table, table, table, table, table, table, table))
        self.db.executescript("""
        CREATE TRIGGER IF NOT EXISTS reports_repair_delete AFTER DELETE ON reports BEGIN
          UPDATE parts SET report_due=1 WHERE delivery_id=json_extract(OLD.value,'$.delivery_id');
          UPDATE outbox SET report_due=1 WHERE id=json_extract(OLD.value,'$.delivery_id');
        END;
        """)
        if not self.db.execute("SELECT 1 FROM meta WHERE key='notification_index_version'").fetchone():
            for row in self.db.execute("SELECT key,value FROM meta WHERE key LIKE 'notification:%'").fetchall():
                self.notification(self.db, row["key"], json.loads(row["value"]))
            self.db.execute("DELETE FROM meta WHERE key LIKE 'notification:%'")
            self.db.execute("INSERT INTO meta VALUES('notification_index_version','1')")
        self.db.commit()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        if self.db:
            self.db.close()

    def rows(self, sql, args=()):
        with self.lock:
            if self.db is None:
                return []
            return [dict(row) for row in self.db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def meta(self, key, default=None):
        table = "notifications" if key.startswith("notification:") else "meta"
        row = self.one("SELECT value FROM " + table + " WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        with self.transaction() as db:
            if key.startswith("notification:"):
                self.notification(db, key, value)
            else:
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dump(value)))

    @staticmethod
    def notification(db, key, value):
        match = re.search(r"/(?:issues|pulls|discussions)/(\d+)$", (value.get("subject") or {}).get("url") or "")
        db.execute("INSERT OR REPLACE INTO notifications VALUES(?,?,?,?)",
                   (key, value.get("repo_id"), int(match[1]) if match else None, dump(value)))

    def gap(self, scope, code, detail=None):
        with self.transaction() as db:
            db.execute("INSERT INTO gaps VALUES(?,?,?,?,NULL) ON CONFLICT(scope) DO UPDATE SET "
                       "code=excluded.code,detail=excluded.detail,resolved=NULL",
                       (scope, code, dump(detail or {}), now()))

    def resolve(self, scope):
        with self.transaction() as db:
            db.execute("UPDATE gaps SET resolved=? WHERE scope=?", (now(), scope))

    def event(self, event_id):
        row = self.one("SELECT event FROM inbox WHERE id=?", (event_id,))
        if not row:
            raise ValueError("unknown_source_event_id")
        return json.loads(row["event"])

    def observe(self, db, key, snapshot, event_factory, baseline=False):
        """Call within the same transaction as the consumed page checkpoint."""
        row = db.execute("SELECT * FROM snapshots WHERE key=?", (key,)).fetchone()
        fingerprint = digest(snapshot.get("comparison", snapshot))
        if row and row["fingerprint"] == fingerprint:
            db.execute("UPDATE snapshots SET observed=? WHERE key=?", (now(), key))
            return None
        revision = row["revision"] + 1 if row else 1
        event_id = "ghobs:" + digest(key) + ":" + str(revision)
        old = json.loads(row["value"]) if row else None
        db.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)",
                   (key, revision, fingerprint, dump(snapshot), now()))
        event = event_factory(event_id, revision, old)
        if event is not None:
            state = (baseline if isinstance(baseline, str) else "baseline") if baseline and row is None else "ready"
            db.execute("INSERT INTO inbox(id,event,state,created) VALUES(?,?,?,?)",
                       (event_id, dump(event), state, now()))
        return event_id

    def job(self, db, stream, spec):
        db.execute("INSERT OR IGNORE INTO jobs(stream,spec) VALUES(?,?)", (stream, dump(spec)))

    def recover(self):
        """A process death after a mutation began is unknown, never queued again."""
        with self.transaction() as db:
            db.execute("UPDATE inbox SET state='ready' WHERE state='submitting'")
            db.execute("UPDATE parts SET state='uncertain' WHERE state='sending'")
            for row in db.execute("SELECT id,detail FROM outbox WHERE state='sending'").fetchall():
                detail = {**json.loads(row["detail"]), "code": "process_interrupted_during_send"}
                db.execute("UPDATE outbox SET state='uncertain', detail=? WHERE id=?", (dump(detail), row["id"]))

    def status(self, offset=0, limit=100):
        if type(offset) is not int or offset < 0:
            raise ValueError("invalid_status_offset")
        result = {"observed_at": now(), "metadata": {r["key"]: json.loads(r["value"])
                  for r in self.rows("SELECT * FROM meta WHERE key LIKE 'auth:%' OR key LIKE 'quota:%' OR key LIKE 'scan:%' "
                                     "OR key LIKE 'bootstrap:%' OR key IN ('host_capabilities','graphql_quota','runtime')")}, "counts": {}}
        for table in ("inbox", "refs", "outputs", "outbox", "reports", "jobs"):
            result["counts"][table] = {r["state"]: r["n"] for r in self.rows(
                "SELECT state,n FROM state_counts WHERE table_name=? AND n>0", (table,))}
        result["pagination"] = {"offset": offset, "limit": limit, "next_offsets": {}}
        def page(name, sql):
            rows = self.rows(sql + " LIMIT ? OFFSET ?", (limit + 1, offset))
            result["pagination"]["next_offsets"][name] = offset + limit if len(rows) > limit else None
            return rows[:limit]
        result["gaps"] = page("gaps", "SELECT * FROM gaps WHERE resolved IS NULL ORDER BY opened,scope")
        result["oldest_waiting"] = self.one(
            "SELECT MIN(created) oldest FROM inbox WHERE state IN ('ready','blocked','submitting')")
        result["database_bytes"] = sum(p.stat().st_size for p in self.root.glob("transport.sqlite3*"))
        result["waiting_events"] = page("waiting_events", "SELECT id,state,due,created FROM inbox WHERE state IN ('ready','blocked','submitting','rejected') ORDER BY seq")
        result["work_refs"] = page("work_refs", "SELECT ref,event_id,kind,state,due FROM refs WHERE state!='terminal' ORDER BY ref")
        result["deliveries"] = [{"id": r["id"], "source_event_id": r["event_id"], "state": r["state"],
                                 "due": r["due"], "code": json.loads(r["detail"]).get("code")}
                                for r in page("deliveries", "SELECT * FROM outbox WHERE state!='delivered' ORDER BY created,id")]
        result["now_epoch"] = time.time()
        return result
