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
                  "expected_account_id": "", "blocked_peers": [], "interval": 3,
                  "scope": "all", "selected_peers": []}
# Policies written before receiving scope existed omit these keys: all chats, none selected.
_ADDED_IN_0_3 = {"scope", "selected_peers"}

def _checked(row):
    """One validation for reading and saving, so a bad preimage is never written."""
    if row["mode"] not in {"legacy", "participant"} or type(row["outgoing"]) is not bool:
        raise ValueError("invalid mode or outgoing control")
    if type(row["interval"]) is not int or not 1 <= row["interval"] <= 60:
        raise ValueError("interval must be 1..60 seconds")
    for key in ("owner_id", "expected_account_id"):
        if not isinstance(row[key], str) or (row[key] and (not row[key].isascii() or not row[key].isdigit() or int(row[key]) <= 0)):
            raise ValueError("numeric account ID required")
    if row["scope"] not in {"all", "selected"}:
        raise ValueError("receiving scope must be all or selected")
    for key in ("blocked_peers", "selected_peers"):
        if type(row[key]) is not list or len(row[key]) > 1000 or any(type(x) is not int or not x for x in row[key]):
            raise ValueError("peer IDs invalid")
    return row

def policy(root):
    row = _read_json(_private_dir(root) / "participant.json")
    if row is None:
        return dict(DEFAULT_POLICY)
    if set(row) - set(DEFAULT_POLICY) or set(DEFAULT_POLICY) - _ADDED_IN_0_3 - set(row):
        raise ValueError("participant policy invalid")
    return _checked({**{k: DEFAULT_POLICY[k] for k in _ADDED_IN_0_3}, **row})

def save_policy(root, values):
    root = _private_dir(root)
    with _config_lock(root, 5):
        row = policy(root)
        if set(values) - set(row):
            raise ValueError("unknown participant policy field")
        row.update(values)
        _write_json(root, "participant.json", _checked(row))
        return row

def receiving_refusal(row, peer, account=None):
    """None when this numeric peer is inside the receiving scope, otherwise a fixed reason.

    The same scope bounds what is captured, read and written. Saved Messages (the
    account itself) stays reachable so own uploads and sticker packs keep working.
    """
    peer = int(peer)
    if account is not None and peer == int(account):
        return None
    if peer in row["blocked_peers"]:
        return "peer_excluded"
    if row["scope"] == "selected" and peer not in row["selected_peers"]:
        return "peer_not_selected"
    return None

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

def event_identity(event):
    """The facts the Host's presence_event_identity compares, from one captured event."""
    return [event.get("source_event_id"), event.get("account_id"), event.get("conversation_id"),
            event.get("thread_id"), str((event.get("actor") or {}).get("platform_actor_id") or ""), event.get("text")]

def predates_canonical_text(event):
    """Captured before 0.3: text could carry staging notes and an edit revision used message bytes."""
    return "sent_at" not in (event.get("message") or {})

# Local staging outcomes that 0.2 appended to the provider text.
_LEGACY_NOTES = ("\n[Media not staged: ", "\n[Original Telegram media unavailable: ")

def legacy_text_matches(stored, text, kind):
    """Whether a pre-0.3 capture's text says ``text``, the provider text of a message.

    0.2 appended staging notes to that text, and wrote ``[Telegram service/media
    event: <kind>]`` when it had neither text nor a staged file; ``kind`` is the
    type name of the message's media or service action.
    """
    if stored == text or any(stored.startswith(text + note) for note in _LEGACY_NOTES):
        return True
    return not text and stored == "[Telegram service/media event: " + kind + "]"

def same_source(stored, event):
    """The same provider message: equal identity facts, or a pre-0.3 capture of the same IDs and actor.

    A Telegram message ID is unique in its chat and edits get their own source ID, so
    a pre-0.3 row under the same ID differs only by the old text composition.
    """
    first, later = event_identity(stored), event_identity(event)
    return first == later or (predates_canonical_text(stored) and first[:-1] == later[:-1])

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
        try:
            with _config_lock(self.root, 5):
                self._initialize(recover)
        except BaseException:
            self.db.close()
            raise
    def _initialize(self, recover):
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
        CREATE TABLE IF NOT EXISTS chats (account TEXT NOT NULL, peer INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '', members INTEGER, source TEXT NOT NULL,
          seen REAL NOT NULL, PRIMARY KEY(account,peer));
        CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, account TEXT NOT NULL, spec TEXT NOT NULL,
          state TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS import_records (plan TEXT NOT NULL, seq INTEGER NOT NULL, peer INTEGER NOT NULL,
          thread TEXT NOT NULL, source_event_id TEXT NOT NULL, record TEXT NOT NULL, state TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(plan, source_event_id));
        CREATE INDEX IF NOT EXISTS import_records_order ON import_records(plan, state, seq);
        ''')
        # Serialize inspect-and-ALTER across companion, tools and status opens.
        self.db.execute('BEGIN IMMEDIATE')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(reports)')}
        for name, declaration in [('due','REAL NOT NULL DEFAULT 0'),('attempts','INTEGER NOT NULL DEFAULT 0'),('code',"TEXT NOT NULL DEFAULT ''")]:
            if name not in columns: self.db.execute('ALTER TABLE reports ADD COLUMN '+name+' '+declaration)
        raw_columns={r[1] for r in self.db.execute('PRAGMA table_info(raw_updates)')}
        for name,declaration in [('due','REAL NOT NULL DEFAULT 0'),('code',"TEXT NOT NULL DEFAULT ''")]:
            if name not in raw_columns: self.db.execute('ALTER TABLE raw_updates ADD COLUMN '+name+' '+declaration)
        if 'origin' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT '{}'")
        if 'response' not in {r[1] for r in self.db.execute('PRAGMA table_info(inbox)')}:
            self.db.execute("ALTER TABLE inbox ADD COLUMN response TEXT NOT NULL DEFAULT '{}'")
        if 'reason' not in {r[1] for r in self.db.execute('PRAGMA table_info(limits)')}:
            self.db.execute("ALTER TABLE limits ADD COLUMN reason TEXT NOT NULL DEFAULT 'Telegram wait'")
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
    def flood(self, account, seconds, reason='FloodWait'):
        with self.db:
            self.db.execute('INSERT INTO limits(account,until,reason) VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET reason=CASE WHEN excluded.until>=until THEN excluded.reason ELSE reason END, until=max(until,excluded.until)',(str(account),time.time()+max(1,seconds),reason))
    def cooldown(self, account):
        row=self.db.execute('SELECT until,reason FROM limits WHERE account=?',(str(account),)).fetchone()
        now=time.time()
        return {'remaining_seconds':max(0,row[0]-now) if row else 0,
                'until_unix':row[0] if row and row[0]>now else None,
                'reason':row[1] if row and row[0]>now else None,
                'observed_at_unix':now}
    def flood_remaining(self, account):
        held=self.db.execute('SELECT until FROM limits WHERE account=?',(str(account),)).fetchone()
        return max(0,held[0]-time.time()) if held else 0
    def claim(self, account):
        """The next due operation; writes to one destination keep their admission order.

        A held or backed-off write still comes before later writes to the same peer and
        topic, so a follow-up never overtakes it; other destinations do not wait for it.
        A write without topic_id that replies to or acts on a message gets that message's
        topic only in preparation, so until then it keeps its order with every write to
        its peer. Topic 1 is General, the same as no topic. Reads are never held and never wait.
        """
        from .participant_ops import SPECS, TARGET_TOPIC_OPS
        if self.flood_remaining(account): return None
        reads = sorted(op for op, (effect, _, _) in SPECS.items() if effect == 'read')
        targets = sorted(TARGET_TOPIC_OPS)
        listed = ','.join('?' * len(reads))
        inferred = ("(json_extract({0}.args,'$.topic_id') IS NULL AND (json_extract({0}.args,'$.reply_id') IS NOT NULL "
                    "OR {0}.op IN (" + ','.join('?' * len(targets)) + ")))")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # A held row was never attempted; new work for an Active place goes first.
            row = self.db.execute("SELECT * FROM jobs AS j WHERE account=? AND state IN ('queued','held') AND due<=? "
                                  f"AND (op IN ({listed}) OR NOT EXISTS (SELECT 1 FROM jobs AS earlier WHERE earlier.account=j.account "
                                  f"AND earlier.seq<j.seq AND earlier.state IN ('queued','held','preparing') AND earlier.op NOT IN ({listed}) "
                                  "AND json_extract(earlier.args,'$.peer') IS json_extract(j.args,'$.peer') "
                                  "AND (nullif(json_extract(earlier.args,'$.topic_id'),1) IS nullif(json_extract(j.args,'$.topic_id'),1) "
                                  f"OR {inferred.format('earlier')} OR {inferred.format('j')}))) "
                                  "ORDER BY state='held', seq LIMIT 1",
                                  (str(account), time.time(), *reads, *reads, *targets, *targets)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='preparing' WHERE seq=?", (row["seq"],))
        return dict(row) if row else None
    def set(self, seq, state, result=None, delay=0):
        with self.db:
            self.db.execute("UPDATE jobs SET state=?, result=?, due=? WHERE seq=?", (state, json.dumps(result, ensure_ascii=True), time.time()+delay, seq))
    def hold(self, row, reason):
        """Keep an unattempted operation for the place or local switch to reopen.

        Nothing is sent while held; the same row resumes when Active and outgoing
        allow it. A recheck backs off to one minute and keeps the first hold time.
        """
        previous = json.loads(row["result"]) if row.get("result") else None
        prior = previous if row.get("state") == "held" and type(previous) is dict else {}
        checks = int(prior.get("checks", 0)) + 1
        self.set(row["seq"], "held", {"code": "held", "reason": reason, "checks": checks,
                                      "held_since_unix": prior.get("held_since_unix", time.time())},
                 min(60, 5 * 2 ** min(checks - 1, 4)))
    def discard_held(self, account):
        """Owner choice: held rows were never attempted, so cancelling cannot hide an effect."""
        with self.db:
            return self.db.execute("UPDATE jobs SET state='cancelled', result=? WHERE account=? AND state='held'",
                                   (json.dumps({"code": "discarded_by_owner"}), str(account))).rowcount
    def remember_chat(self, account, peer, title, kind, source, username='', members=None):
        with self.db:
            self.db.execute("INSERT INTO chats(account,peer,title,kind,username,members,source,seen) VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(account,peer) DO UPDATE SET title=excluded.title, kind=excluded.kind, "
                            "username=excluded.username, members=coalesce(excluded.members,members), "
                            "source=CASE WHEN source='dialogs' THEN source ELSE excluded.source END, seen=excluded.seen",
                            (str(account), int(peer), str(title or '')[:256], kind, str(username or '')[:64], members, source, time.time()))
    def chats(self, account, limit=500):
        return [dict(r) for r in self.db.execute("SELECT peer,title,kind,username,members,source,seen FROM chats WHERE account=? ORDER BY seen DESC LIMIT ?", (str(account), limit))]
    def capture_raw(self, key, metadata, payload):
        with self.db:
            old = self.db.execute('SELECT payload FROM raw_updates WHERE id=?',(key,)).fetchone()
            if old:
                if old[0] != payload: raise ValueError('raw update identity conflict')
                return
            if self.db.execute("SELECT count(*) FROM raw_updates WHERE state='pending'").fetchone()[0] >= 1000:
                raise RuntimeError('raw ingress backpressure')
            self.db.execute('INSERT INTO raw_updates(id,metadata,payload) VALUES(?,?,?)',(key,json.dumps(metadata),payload))
    def raw_heads(self, account=None):
        scope = ''
        params = []
        if account is not None:
            with self.db:
                self.db.execute("UPDATE raw_updates SET state='held',code='capture_account_unknown' WHERE state='pending' AND json_extract(metadata,'$.account_id') IS NULL")
            scope = " AND CAST(json_extract(metadata,'$.account_id') AS TEXT)=?"
            params.append(str(account))
        return self.db.execute("SELECT * FROM raw_updates WHERE seq IN (SELECT min(seq) FROM raw_updates WHERE state='pending'"+scope+" GROUP BY json_extract(metadata,'$.account_id'),json_extract(metadata,'$.peer')) AND due<=? ORDER BY seq LIMIT 30", (*params,time.time())).fetchall()
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
                # A later observation of the same source (a renamed sender, a reaction
                # delivered as an edit update) keeps the first capture. Different
                # identity facts under one source ID remain a visible conflict.
                if old[0] != data and not same_source(json.loads(old[0]), event):
                    raise ValueError("source event identity conflict")
                if raw_id is not None:
                    self.db.execute("UPDATE raw_updates SET state='done' WHERE id=?",(raw_id,))
                return
            count = self.db.execute("SELECT count(*) FROM inbox WHERE state='pending'").fetchone()[0]
            if count >= 1000:
                raise RuntimeError("ingress backpressure; reconcile history before restarting")
            self.db.execute("INSERT INTO inbox(id,event,files) VALUES(?,?,?)", (event["source_event_id"], data, json.dumps(list(files))))
            if raw_id is not None:
                self.db.execute("UPDATE raw_updates SET state='done' WHERE id=?",(raw_id,))
    def legacy_edits(self, account, peer, topic, message_id, actor, edit_stamp):
        """Pre-0.3 captures of this message by this actor at this edit time, with their raw bytes.

        Bounded to the edit rows of this one message through the inbox ID index; no
        history-wide scan. A pre-0.3 revision is ``str(edit_date) + ':' +
        sha256(message bytes)``, so with the message IDs it names the transport key of
        the raw update that capture was hydrated from, read by primary key. The bytes
        are ``None`` when no raw update with exactly those bytes is retained.
        """
        base = f"telegram:{account}:{peer}:{topic or 0}:{message_id}:edit:"
        found = []
        for (raw,) in self.db.execute("SELECT event FROM inbox WHERE id >= ? AND id < ?", (base, base + "\uffff")):
            stored = json.loads(raw)
            revision = str((stored.get("message") or {}).get("revision") or "")
            if not (predates_canonical_text(stored) and revision.startswith(edit_stamp + ":")
                    and str((stored.get("actor") or {}).get("platform_actor_id") or "") == str(actor)):
                continue
            sha = revision[len(edit_stamp) + 1:]
            key = digest([str(account), int(peer), int(message_id), "edit", edit_stamp, sha])
            row = self.db.execute("SELECT payload FROM raw_updates WHERE id=?", (key,)).fetchone()
            found.append((stored, row[0] if row and hashlib.sha256(row[0]).hexdigest() == sha else None))
        return found
    def heads(self, account=None):
        scope = " AND CAST(json_extract(event,'$.account_id') AS TEXT)=?" if account is not None else ''
        params = (str(account),time.time()) if account is not None else (time.time(),)
        return self.db.execute("SELECT * FROM inbox WHERE state='pending'"+scope+" AND due<=? AND NOT EXISTS (SELECT 1 FROM inbox AS earlier WHERE earlier.state='pending' AND earlier.seq<inbox.seq AND earlier.refs='[]' AND json_extract(earlier.event,'$.conversation_key')=json_extract(inbox.event,'$.conversation_key') AND json_extract(earlier.event,'$.account_id')=json_extract(inbox.event,'$.account_id')) ORDER BY seq LIMIT 30", params).fetchall()
    def submissions(self, account):
        """Each conversation's oldest event the Host has not taken yet, when it is due.

        An event the Host holds (it has references) no longer keeps its conversation's later
        events waiting; one it has not taken, including one it answered busy, still does.
        """
        return self.db.execute("SELECT * FROM inbox WHERE state='pending' AND refs='[]' AND CAST(json_extract(event,'$.account_id') AS TEXT)=? AND due<=? AND NOT EXISTS (SELECT 1 FROM inbox AS earlier WHERE earlier.state='pending' AND earlier.refs='[]' AND earlier.seq<inbox.seq AND json_extract(earlier.event,'$.conversation_key')=json_extract(inbox.event,'$.conversation_key') AND json_extract(earlier.event,'$.account_id')=json_extract(inbox.event,'$.account_id')) ORDER BY seq LIMIT 30", (str(account), time.time())).fetchall()
    def observations(self, account):
        """Events the Host holds whose references are due to be read, least recently read first."""
        return self.db.execute("SELECT * FROM inbox WHERE state='pending' AND refs!='[]' AND CAST(json_extract(event,'$.account_id') AS TEXT)=? AND due<=? ORDER BY due,seq LIMIT 30", (str(account), time.time())).fetchall()
    def observe_inbox(self, seq, status, body):
        with self.db:
            self.db.execute('UPDATE inbox SET response=? WHERE seq=?',(json.dumps({'http_status':status,'body':body},ensure_ascii=True),seq))
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
        return {'jobs':jobs,'reports':reports,'inbox':{r[0]:r[1] for r in self.db.execute('SELECT state,count(*) FROM inbox GROUP BY state')},'inbound_retrying':self.db.execute("SELECT count(*) FROM raw_updates WHERE state='pending' AND code!=''").fetchone()[0], 'report_errors':self.db.execute("SELECT count(*) FROM reports WHERE code!=''").fetchone()[0]}
    def pending_reports(self):
        return self.db.execute("SELECT * FROM reports WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 5",(time.time(),)).fetchall()
    def report_done(self, key):
        with self.db:
            self.db.execute("UPDATE reports SET state='recorded' WHERE id=?", (key,))
