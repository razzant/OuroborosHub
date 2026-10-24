"""Private v2 policy and atomic durable work; legacy databases/sessions stay untouched."""
from __future__ import annotations
import hashlib
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from .config import _private_dir, _read_json, _write_json, _config_lock

DEFAULT_POLICY = {"mode": "legacy", "outgoing": False, "owner_id": "",
                  "expected_account_id": "", "blocked_peers": [], "interval": 3}

def policy(root):
    row = _read_json(_private_dir(root) / "participant.json")
    if row is None:
        return dict(DEFAULT_POLICY)
    if set(row) != set(DEFAULT_POLICY) or row["mode"] not in {"legacy", "participant"}:
        raise ValueError("participant policy invalid")
    if type(row["outgoing"]) is not bool or type(row["interval"]) is not int or not 1 <= row["interval"] <= 60:
        raise ValueError("participant controls invalid")
    for k in ("owner_id", "expected_account_id"):
        if not isinstance(row[k], str) or (row[k] and (not row[k].isascii() or not row[k].isdigit() or int(row[k]) <= 0)):
            raise ValueError("account identity must be a numeric ID")
    if not isinstance(row["blocked_peers"], list) or any(type(x) is not int or x == 0 for x in row["blocked_peers"]):
        raise ValueError("blocked peers invalid")
    return row

def save_policy(root, values):
    root = _private_dir(root)
    with _config_lock(root, 5):
        row = policy(root)
        if set(values) - set(row):
            raise ValueError("unknown participant policy field")
        row.update(values)
        # Validate through the same pure conditions without writing a bad preimage.
        if row["mode"] not in {"legacy", "participant"} or type(row["outgoing"]) is not bool:
            raise ValueError("invalid mode or outgoing control")
        if type(row["interval"]) is not int or not 1 <= row["interval"] <= 60:
            raise ValueError("interval must be 1..60 seconds")
        for key in ("owner_id", "expected_account_id"):
            if not isinstance(row[key], str) or (row[key] and (not row[key].isascii() or not row[key].isdigit() or int(row[key]) <= 0)):
                raise ValueError("numeric account ID required")
        if type(row["blocked_peers"]) is not list or len(row["blocked_peers"]) > 1000 or any(type(x) is not int or not x for x in row["blocked_peers"]):
            raise ValueError("blocked peer IDs invalid")
        _write_json(root, "participant.json", row)
        return row

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

def asset(root, name):
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]{1,8}", name):
        raise ValueError("asset must be an uploaded immutable asset handle")
    base = _private_dir(root) / "assets"
    path = base / name
    if base.is_symlink() or path.is_symlink() or not path.is_file() or path.resolve().parent != base.resolve():
        raise ValueError("asset unavailable")
    if hashlib.sha256(path.read_bytes()).hexdigest() != name.split(".")[0]:
        raise ValueError("asset bytes changed")
    return path

class Journal:
    """One SQLite transaction claims work; post-attempt death means unknown, never resend.

    Each companion process opens this under its sole session lock. Tool connections
    only enqueue/read. Reads can be retried; write boundaries cannot. Reports are
    independent rows: a failed host receipt cannot requeue a Telegram effect.
    """
    def __init__(self, root, recover=False):
        self.root = _private_dir(root)
        path = self.root / "participant.sqlite3"
        if path.is_symlink():
            raise ValueError("journal path redirected")
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY, account TEXT NOT NULL, id TEXT NOT NULL,
          op TEXT NOT NULL, args TEXT NOT NULL, hash TEXT NOT NULL, state TEXT NOT NULL,
          result TEXT, due REAL NOT NULL DEFAULT 0, random_id INTEGER NOT NULL, origin TEXT NOT NULL DEFAULT '{}', UNIQUE(account,id));
        CREATE TABLE IF NOT EXISTS limits (account TEXT PRIMARY KEY, until REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
          event TEXT NOT NULL, files TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
          refs TEXT NOT NULL DEFAULT '[]', due REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS raw_updates (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
          metadata TEXT NOT NULL, payload BLOB NOT NULL, state TEXT NOT NULL DEFAULT 'pending', due REAL NOT NULL DEFAULT 0, code TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, code TEXT NOT NULL DEFAULT '');
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(reports)')}
        for name, declaration in [('due','REAL NOT NULL DEFAULT 0'),('attempts','INTEGER NOT NULL DEFAULT 0'),('code',"TEXT NOT NULL DEFAULT ''")]:
            if name not in columns: self.db.execute('ALTER TABLE reports ADD COLUMN '+name+' '+declaration)
        raw_columns={r[1] for r in self.db.execute('PRAGMA table_info(raw_updates)')}
        for name,declaration in [('due','REAL NOT NULL DEFAULT 0'),('code',"TEXT NOT NULL DEFAULT ''")]:
            if name not in raw_columns: self.db.execute('ALTER TABLE raw_updates ADD COLUMN '+name+' '+declaration)
        if 'origin' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT '{}'")
        self.db.commit()
        if recover:
            self.db.execute("UPDATE jobs SET state='unknown' WHERE state='attempted'")
            self.db.execute("UPDATE jobs SET state='queued' WHERE state='preparing'")
            self.db.commit()
    def close(self):
        self.db.close()
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.close()
    def enqueue(self, account, key, op, args, origin=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", key):
            raise ValueError("stable intent_id required")
        encoded = json.dumps(args, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        h = digest([op, args])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(account,id,op,args,hash,state,random_id,origin) VALUES(?,?,?,?,?,'queued',?,?)",
                            (str(account), key, op, encoded, h, secrets.randbits(63), json.dumps(origin or {'kind':'tool'})))
            row = self.get(account, key)
            if row["hash"] != h:
                raise ValueError("intent_id conflict: existing operation/bytes differ")
        return row
    def get(self, account, key):
        row = self.db.execute("SELECT * FROM jobs WHERE account=? AND id=?", (str(account), key)).fetchone()
        return dict(row) if row else None
    def flood(self, account, seconds):
        with self.db:
            self.db.execute('INSERT INTO limits VALUES(?,?) ON CONFLICT(account) DO UPDATE SET until=max(until,excluded.until)',(str(account),time.time()+max(1,seconds)))
    def flood_remaining(self, account):
        held=self.db.execute('SELECT until FROM limits WHERE account=?',(str(account),)).fetchone()
        return max(0,held[0]-time.time()) if held else 0
    def claim(self, account):
        if self.flood_remaining(account): return None
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM jobs WHERE account=? AND state='queued' AND due<=? ORDER BY seq LIMIT 1", (str(account), time.time())).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='preparing' WHERE seq=?", (row["seq"],))
        return dict(row) if row else None
    def set(self, seq, state, result=None, delay=0):
        with self.db:
            self.db.execute("UPDATE jobs SET state=?, result=?, due=? WHERE seq=?", (state, json.dumps(result, ensure_ascii=True), time.time()+delay, seq))
    def capture_raw(self, key, metadata, payload):
        with self.db:
            old = self.db.execute('SELECT payload FROM raw_updates WHERE id=?',(key,)).fetchone()
            if old:
                if old[0] != payload: raise ValueError('raw update identity conflict')
                return
            if self.db.execute("SELECT count(*) FROM raw_updates WHERE state='pending'").fetchone()[0] >= 1000:
                raise RuntimeError('raw ingress backpressure')
            self.db.execute('INSERT INTO raw_updates(id,metadata,payload) VALUES(?,?,?)',(key,json.dumps(metadata),payload))
    def raw_heads(self):
        return self.db.execute("SELECT * FROM raw_updates WHERE seq IN (SELECT min(seq) FROM raw_updates WHERE state='pending' GROUP BY json_extract(metadata,'$.peer')) ORDER BY seq LIMIT 30").fetchall()
    def raw_defer(self,key,delay,code):
        with self.db:
            self.db.execute('UPDATE raw_updates SET due=?,code=? WHERE id=?',(time.time()+delay,code,key))
    def raw_done(self, key):
        with self.db:
            self.db.execute("UPDATE raw_updates SET state='done' WHERE id=?",(key,))
    def capture(self, event, files=(), raw_id=None):
        data = json.dumps(event, sort_keys=True, ensure_ascii=True)
        with self.db:
            old = self.db.execute("SELECT event FROM inbox WHERE id=?", (event["source_event_id"],)).fetchone()
            if old:
                if old[0] != data:
                    raise ValueError("source event identity conflict")
                return
            count = self.db.execute("SELECT count(*) FROM inbox WHERE state='pending'").fetchone()[0]
            if count >= 1000:
                raise RuntimeError("ingress backpressure; reconcile history before restarting")
            self.db.execute("INSERT INTO inbox(id,event,files) VALUES(?,?,?)", (event["source_event_id"], data, json.dumps(list(files))))
            if raw_id is not None:
                self.db.execute("UPDATE raw_updates SET state='done' WHERE id=?",(raw_id,))
    def heads(self):
        return self.db.execute("SELECT * FROM inbox WHERE state='pending' AND due<=? ORDER BY seq LIMIT 30", (time.time(),)).fetchall()
    def inbox_update(self, seq, state, refs, delay=0):
        with self.db:
            self.db.execute("UPDATE inbox SET state=?,refs=?,due=? WHERE seq=?", (state, json.dumps(refs), time.time()+delay, seq))
    def _insert_report(self, key, body):
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=True)
        self.db.execute("INSERT OR IGNORE INTO reports(id,body) VALUES(?,?)", (key, encoded))
        if self.db.execute("SELECT body FROM reports WHERE id=?", (key,)).fetchone()[0] != encoded:
            raise ValueError("receipt identity conflict")
    def report(self, key, body):
        with self.db: self._insert_report(key,body)
    def settle_with_report(self, seq, result, key, body):
        with self.db:
            self.db.execute("UPDATE jobs SET state='sent',result=? WHERE seq=?",(json.dumps(result),seq))
            self._insert_report(key,body)
    def report_defer(self, key, code, permanent=False):
        row=self.db.execute('SELECT attempts FROM reports WHERE id=?',(key,)).fetchone()
        attempts=row[0]+1
        with self.db:
            self.db.execute('UPDATE reports SET state=?,due=?,attempts=?,code=? WHERE id=?',
                ('held' if permanent else 'pending',time.time()+min(3600,30*2**min(attempts-1,7)),attempts,code,key))
    def counts(self):
        jobs={r[0]:r[1] for r in self.db.execute('SELECT state,count(*) FROM jobs GROUP BY state')}
        reports={r[0]:r[1] for r in self.db.execute('SELECT state,count(*) FROM reports GROUP BY state')}
        return {'jobs':jobs,'reports':reports,'inbound_retrying':self.db.execute("SELECT count(*) FROM raw_updates WHERE state='pending' AND code!=''").fetchone()[0], 'report_errors':self.db.execute("SELECT count(*) FROM reports WHERE code!=''").fetchone()[0]}
    def pending_reports(self):
        return self.db.execute("SELECT * FROM reports WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 5",(time.time(),)).fetchall()
    def report_done(self, key):
        with self.db:
            self.db.execute("UPDATE reports SET state='recorded' WHERE id=?", (key,))
