"""One durable inbox/outbox and per-account IMAP cursor; no cognitive state."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from pathlib import Path

from .delivery import email_report

_MAX_ATTACHMENT_FILES = 10
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def _safe_filename(value, fallback="attachment"):
    name = Path(str(value or "")).name.strip()
    name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", name)
    return (name[:180] or fallback).strip(" .") or fallback


def _attachment_content(item):
    if isinstance(item, (str, Path)):
        path = Path(item)
        return path.read_bytes(), path.name, ""
    if not isinstance(item, dict):
        raise ValueError("attachment must be a path or object")
    path = str(item.get("path") or "")
    if path:
        source = Path(path)
        if not source.is_file():
            raise ValueError(f"attachment path is not a file: {path}")
        return source.read_bytes(), str(item.get("filename") or item.get("name") or source.name), str(item.get("content_type") or item.get("mimetype") or "")
    data = item.get("data", item.get("content"))
    if isinstance(data, bytes):
        raw = data
    elif isinstance(data, str):
        # Explicit base64 is preferred; plain text content remains useful for
        # small tool calls and is never interpreted as a filesystem path.
        encoded = item.get("data_base64") or item.get("content_base64")
        raw = base64.b64decode(encoded, validate=True) if encoded else data.encode()
    else:
        encoded = item.get("data_base64") or item.get("content_base64")
        raw = base64.b64decode(encoded, validate=True) if encoded else b""
    return raw, str(item.get("filename") or item.get("name") or "attachment"), str(item.get("content_type") or item.get("mimetype") or "")


def _refused_json(refused):
    result = {}
    for address, value in (refused or {}).items():
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            result[str(address)] = {"code": value[0], "message": value[1].decode("utf-8", errors="replace") if isinstance(value[1], bytes) else str(value[1])}
        else:
            result[str(address)] = str(value)
    return result


@dataclass(frozen=True)
class InboxItem:
    row_id: int
    lease_token: str
    folder: str
    uid: int
    message_id: str
    in_reply_to: str
    references: tuple[str, ...]
    sender: str
    subject: str
    body: str
    recipients: tuple[str, ...]
    host_reference: str
    attempts: int
    uidvalidity: int = 0
    context: dict = field(default_factory=dict)
    cc: tuple[str, ...] = ()
    reply_to: tuple[str, ...] = ()
    attachments: tuple[dict, ...] = ()
    staged_files: tuple[dict, ...] = ()

    @property
    def thread_key(self):
        return self.references[0] if self.references else (self.in_reply_to or self.message_id)

    @property
    def provider_event_key(self):
        return f"{self.folder}:{self.uidvalidity}:{self.uid}:{self.message_id}"


@dataclass(frozen=True)
class OutboxItem:
    row_id: int
    lease_token: str
    request_id: str
    recipients: tuple[str, ...]
    subject: str
    body: str
    in_reply_to: str
    references: tuple[str, ...]
    attempts: int
    message_id: str
    reporting: dict = field(default_factory=dict)
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    attachments: tuple[dict, ...] = ()
    html_body: str = ""
    body_type: str = "plain"

    @property
    def envelope_recipients(self):
        return [address for _, address in getaddresses(list(self.recipients)) if address]


class EmailStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "email_presence.sqlite3"
        with self._connect() as db:
            # Migrate the unpublished prototype without dropping its receipts.
            old = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='inbox'").fetchone()
            if old and "uidvalidity" not in {r[1] for r in db.execute("PRAGMA table_info(inbox)")}:
                db.execute("ALTER TABLE inbox RENAME TO prototype_inbox")
                db.execute("DROP INDEX IF EXISTS inbox_work")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL, message_id TEXT NOT NULL, in_reply_to TEXT NOT NULL,
                    references_json TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
                    body TEXT NOT NULL, recipients_json TEXT NOT NULL, thread_key TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    cc_json TEXT NOT NULL DEFAULT '[]', reply_to_json TEXT NOT NULL DEFAULT '[]',
                    attachments_json TEXT NOT NULL DEFAULT '[]', staged_files_json TEXT NOT NULL DEFAULT '[]',
                    state TEXT NOT NULL DEFAULT 'pending', host_reference TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(folder, uidvalidity, uid), UNIQUE(folder, message_id));
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    recipients_json TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    in_reply_to TEXT NOT NULL, references_json TEXT NOT NULL,
                    to_json TEXT NOT NULL DEFAULT '[]', cc_json TEXT NOT NULL DEFAULT '[]',
                    bcc_json TEXT NOT NULL DEFAULT '[]', attachments_json TEXT NOT NULL DEFAULT '[]',
                    html_body TEXT NOT NULL DEFAULT '', body_type TEXT NOT NULL DEFAULT 'plain',
                    accepted_json TEXT NOT NULL DEFAULT '[]', refused_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'pending', lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    provider_message_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            inbox_columns = {r[1] for r in db.execute("PRAGMA table_info(inbox)")}
            for name, declaration in (("context_json", "TEXT NOT NULL DEFAULT '{}'"),
                                      ("cc_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("reply_to_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("attachments_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("staged_files_json", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in inbox_columns:
                    db.execute(f"ALTER TABLE inbox ADD COLUMN {name} {declaration}")
            columns = {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
            for name, declaration in (("to_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("cc_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("bcc_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("attachments_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("html_body", "TEXT NOT NULL DEFAULT ''"),
                                      ("body_type", "TEXT NOT NULL DEFAULT 'plain'"),
                                      ("accepted_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("refused_json", "TEXT NOT NULL DEFAULT '{}'"),
                                      ("reporting_json", "TEXT NOT NULL DEFAULT '{}'"),
                                      ("reports_json", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("report_due_at", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {declaration}")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_report_due ON outbox(report_due_at)")
            if old and db.execute("SELECT name FROM sqlite_master WHERE name='prototype_inbox'").fetchone():
                columns = [r[1] for r in db.execute("PRAGMA table_info(prototype_inbox)")]
                names = ",".join(columns)
                db.execute(f"INSERT OR IGNORE INTO inbox ({names},uidvalidity) SELECT {names},0 FROM prototype_inbox")
                for r in db.execute("SELECT id,references_json,in_reply_to,message_id FROM inbox WHERE thread_key='' ").fetchall():
                    refs = json.loads(r["references_json"])
                    key = refs[0] if refs else (r["in_reply_to"] or r["message_id"])
                    db.execute("UPDATE inbox SET thread_key=? WHERE id=?", (key, r["id"]))
                db.execute("DROP TABLE prototype_inbox")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self._connect() as db:
            row = db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self._connect() as db:
            self._put(db, key, value)

    def _stage_files(self, attachments, *, bucket, identity, enforce_limits=True):
        """Copy attachment bytes into immutable skill-owned files.

        Sources are read once, written to a process-specific temporary file,
        atomically renamed, and made read-only. The returned records contain
        only stable local paths and hashes, so restart/retry never rereads a
        mutable caller file.
        """
        attachments = list(attachments or ())
        if enforce_limits and len(attachments) > _MAX_ATTACHMENT_FILES:
            raise ValueError(f"at most {_MAX_ATTACHMENT_FILES} attachments are supported")
        root = self.state_dir / "staged" / str(bucket) / hashlib.sha256(str(identity).encode()).hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        result = []
        total = 0
        for index, item in enumerate(attachments):
            raw, filename, content_type = _attachment_content(item)
            total += len(raw)
            if enforce_limits and total > _MAX_ATTACHMENT_BYTES:
                raise ValueError("attachment batch exceeds 50 MiB")
            digest = hashlib.sha256(raw).hexdigest()
            clean = _safe_filename(filename, f"attachment-{index}")
            path = root / f"{index:02d}-{digest[:16]}-{clean}"
            if path.exists():
                if path.read_bytes() != raw:
                    raise RuntimeError("immutable staged attachment hash collision")
            else:
                temporary = path.with_name(path.name + f".part.{os.getpid()}.{uuid.uuid4().hex}")
                try:
                    with temporary.open("xb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                    os.chmod(path, 0o444)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            result.append({
                "filename": clean,
                "content_type": content_type or mimetypes.guess_type(clean)[0] or "application/octet-stream",
                "size": len(raw), "sha256": digest, "path": str(path),
            })
        return result

    def _stage_raw_source(self, raw, *, identity):
        if not isinstance(raw, bytes):
            return None
        digest = hashlib.sha256(raw).hexdigest()
        root = self.state_dir / "staged" / "inbound" / hashlib.sha256(str(identity).encode()).hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"source-{digest[:16]}.eml"
        if not path.exists():
            temporary = path.with_name(path.name + f".part.{os.getpid()}.{uuid.uuid4().hex}")
            try:
                with temporary.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o444)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return {"filename": path.name, "content_type": "message/rfc822", "size": len(raw), "sha256": digest, "path": str(path), "source": str(path)}

    def stage_inbound_attachments(self, message):
        """Best-effort stage each part and the full source without wedging IMAP."""
        identity = message.get("message_id") or message.get("uid")
        staged, descriptors = [], []
        for index, item in enumerate(message.get("attachments") or ()):
            if str(item.get("content_type") or item.get("mime_type") or "").lower() == "message/rfc822":
                descriptor = {key: value for key, value in dict(item).items() if key not in {"data", "path"}}
                descriptor.update(content_available=False, stage_error="unsupported_mime_type", error="unsupported_mime_type")
                descriptors.append(descriptor)
                continue
            try:
                copied = self._stage_files([item], bucket="inbound", identity=f"{identity}:{index}")
            except Exception as exc:
                descriptor = {key: value for key, value in dict(item).items() if key not in {"data", "path"}}
                descriptor.update(content_available=False, stage_error=type(exc).__name__, error=type(exc).__name__)
                descriptors.append(descriptor)
                continue
            staged.extend(copied)
            descriptor = {key: value for key, value in copied[0].items() if key != "path"}
            descriptor["content_available"] = True
            descriptors.append(descriptor)
        source = self._stage_raw_source(message.pop("_raw_source", None), identity=identity)
        if source:
            staged.append(source)
            message["source_artifact"] = dict(source)
            for descriptor in descriptors:
                descriptor.setdefault("source", source["path"])
        message["staged_files"] = staged
        message["attachments"] = descriptors
        if any(item.get("stage_error") for item in descriptors if isinstance(item, dict)):
            message["attachment_stage_note"] = "Some attachment bytes were not staged; the full RFC822 source artifact is available."
        return staged

    def stage_outbound_attachments(self, request_id, attachments):
        return self._stage_files(attachments, bucket="outbound", identity=request_id)

    @staticmethod
    def _put(db, key, value):
        db.execute("INSERT INTO runtime_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                   (key, json.dumps(value), time.time()))

    def cursor_key(self, account, folder):
        return "cursor:" + json.dumps([account, folder])

    def prepare_cursor(self, account, folder, validity, next_uid):
        key = self.cursor_key(account, folder)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
            cursor = json.loads(row[0]) if row else None
            if cursor is None:
                cursor = {"uidvalidity": validity, "uid": next_uid - 1, "activated_at": time.time()}
            elif cursor["uidvalidity"] != validity:
                cursor.update(uidvalidity=validity, uid=0, rescan=True)
            self._put(db, key, cursor)
        return cursor

    def ingest(self, message, *, cursor_key=None, cursor=None):
        now = time.time()
        if message.get("_raw_source") is not None or (
                message.get("attachments") and not message.get("staged_files")
                and any(isinstance(item, dict) and isinstance(item.get("data"), bytes)
                        for item in message["attachments"])):
            self.stage_inbound_attachments(message)
        context = {key: message[key] for key in (
            "sender_name", "cc", "date", "reply_to", "headers", "attachments", "source_artifact", "attachment_stage_note"
        ) if key in message}
        staged = list(message.get("staged_files") or ())
        with self._connect() as db:
            cur = db.execute("""INSERT OR IGNORE INTO inbox
                (folder,uidvalidity,uid,message_id,in_reply_to,references_json,sender,subject,body,recipients_json,thread_key,context_json,cc_json,reply_to_json,attachments_json,staged_files_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    message.get("folder", "INBOX"), message.get("uidvalidity", 0), int(message["uid"]),
                    message["message_id"], message.get("in_reply_to", ""), json.dumps(message.get("references", [])),
                    message.get("sender", ""), message.get("subject", ""), message.get("body", ""),
                    json.dumps(message.get("recipients", [])), (message.get("references") or [message.get("in_reply_to") or message["message_id"]])[0],
                    json.dumps(context, ensure_ascii=False), json.dumps(message.get("cc", [])), json.dumps(message.get("reply_to", [])),
                    json.dumps(message.get("attachments", []), ensure_ascii=False), json.dumps(staged, ensure_ascii=False), now, now))
            if cursor_key:
                self._put(db, cursor_key, cursor)
            return int(cur.lastrowid or 0), bool(cur.rowcount)

    def _claim(self, table, lease_seconds):
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # SMTP DATA can have been accepted before a process disappears.
            if table == "outbox":
                for row in db.execute("SELECT * FROM outbox WHERE state='sending' AND lease_until<=?", (now,)).fetchall():
                    report = email_report(self._outbox_item(row), "uncertain", error="Process lost during SMTP acceptance")
                    self._freeze_reports(db, row["id"], [report] if report else [])
                db.execute("UPDATE outbox SET state='uncertain',last_error='Process lost during SMTP delivery; inspect Message-ID before retry',updated_at=? WHERE state='sending' AND lease_until<=?", (now, now))
            fifo = "AND NOT EXISTS (SELECT 1 FROM inbox earlier WHERE earlier.id < inbox.id AND earlier.thread_key = inbox.thread_key AND earlier.state IN ('pending','retry','processing') AND earlier.host_reference='')" if table == "inbox" else ""
            row = db.execute(f"SELECT * FROM {table} WHERE ((state IN ('pending','retry') AND available_at<=?) OR (state='processing' AND lease_until<=?)) {fifo} ORDER BY id LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            db.execute(f"UPDATE {table} SET state='processing',lease_token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?", (token, now + lease_seconds, now, row["id"]))
            result = dict(row)
            result.update(lease_token=token, attempts=row["attempts"] + 1)
            return result

    def claim_inbox(self, lease_seconds=2100):
        r = self._claim("inbox", lease_seconds)
        if not r:
            return None
        return InboxItem(r["id"], r["lease_token"], r["folder"], r["uid"], r["message_id"], r["in_reply_to"],
                         tuple(json.loads(r["references_json"])), r["sender"], r["subject"], r["body"],
                         tuple(json.loads(r["recipients_json"])), r["host_reference"], r["attempts"], r["uidvalidity"],
                         json.loads(r["context_json"]), tuple(json.loads(r["cc_json"] or "[]")),
                         tuple(json.loads(r["reply_to_json"] or "[]")), tuple(json.loads(r["attachments_json"] or "[]")),
                         tuple(json.loads(r["staged_files_json"] or "[]")))

    def set_host_reference(self, row_id, token, reference):
        with self._connect() as db:
            db.execute("UPDATE inbox SET host_reference=?,updated_at=? WHERE id=? AND lease_token=?", (reference, time.time(), row_id, token))

    def _update(self, table, row_id, token, state, error="", delay=0, reports=()):
        with self._connect() as db:
            db.execute(f"UPDATE {table} SET state=?,last_error=?,lease_token='',lease_until=0,available_at=?,updated_at=? WHERE id=? AND lease_token=?",
                       (state, error, time.time() + delay, time.time(), row_id, token))
            if table == "outbox":
                self._freeze_reports(db, row_id, reports)

    def complete_inbox(self, row_id, token):
        self._update("inbox", row_id, token, "completed")

    def retry_inbox(self, row_id, token, error, delay=5):
        self._update("inbox", row_id, token, "retry", error, delay)

    def fail_inbox(self, row_id, token, error):
        self._update("inbox", row_id, token, "failed", error)

    def enqueue_outbox(self, *, request_id, recipients=(), subject="", body="", in_reply_to="", references=(), reporting=None,
                       to=None, cc=(), bcc=(), attachments=(), html_body="", body_type="plain"):
        now = time.time()
        def dedupe(values, seen=None):
            known = set(seen or ())
            result = []
            for value in values:
                key = parseaddr(str(value))[1].casefold() or str(value).casefold()
                if key and key not in known:
                    known.add(key)
                    result.append(value)
            return result, known
        visible_to, seen = dedupe(list(recipients if to is None else to))
        visible_cc, seen = dedupe(list(cc or ()), seen)
        # To/Cc take precedence over Bcc when callers supplied the same address
        # twice. Keep the complete envelope in recipients_json for SMTP delivery.
        visible_bcc, seen = dedupe(list(bcc or ()), seen)
        envelope = visible_to + visible_cc + visible_bcc
        message_id = "<" + hashlib.sha256((str(self.path.resolve()) + ":" + request_id).encode()).hexdigest() + "@ouroboros.local>"
        with self._connect() as db:
            cur = db.execute("""INSERT OR IGNORE INTO outbox(request_id,recipients_json,subject,body,in_reply_to,references_json,provider_message_id,created_at,updated_at,reporting_json,to_json,cc_json,bcc_json,attachments_json,html_body,body_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (request_id, json.dumps(envelope), subject, body,
                                               in_reply_to, json.dumps(list(references)), message_id, now, now, json.dumps(reporting or {}),
                                               json.dumps(visible_to), json.dumps(visible_cc), json.dumps(visible_bcc), json.dumps(list(attachments), ensure_ascii=False), str(html_body or ""), str(body_type or "plain")))
            return bool(cur.rowcount)

    def claim_outbox(self, lease_seconds=300):
        r = self._claim("outbox", lease_seconds)
        if not r:
            return None
        if not r["provider_message_id"]:
            r["provider_message_id"] = f"<{uuid.uuid4().hex}@ouroboros.local>"
            with self._connect() as db:
                db.execute("UPDATE outbox SET provider_message_id=? WHERE id=?", (r["provider_message_id"], r["id"]))
        return self._outbox_item(r)

    @staticmethod
    def _outbox_item(r):
        return OutboxItem(r["id"], r["lease_token"], r["request_id"], tuple(json.loads(r["recipients_json"])),
                          r["subject"], r["body"], r["in_reply_to"], tuple(json.loads(r["references_json"])),
                          r["attempts"], r["provider_message_id"], json.loads(r["reporting_json"]),
                          tuple(json.loads(r["to_json"] or "[]")) or tuple(json.loads(r["recipients_json"])),
                          tuple(json.loads(r["cc_json"] or "[]")), tuple(json.loads(r["bcc_json"] or "[]")),
                          tuple(json.loads(r["attachments_json"] or "[]")), r["html_body"], r["body_type"])

    def set_reporting(self, item, reporting):
        with self._connect() as db:
            db.execute("UPDATE outbox SET reporting_json=? WHERE id=? AND lease_token=?", (json.dumps(reporting), item.row_id, item.lease_token))

    def _freeze_reports(self, db, row_id, reports):
        reports = [report for report in reports if report]
        if not reports:
            return
        entries = json.loads(db.execute("SELECT reports_json FROM outbox WHERE id=?", (row_id,)).fetchone()[0])
        keys = {(x["payload"]["part_id"], x["payload"]["state"]) for x in entries}
        for report in reports:
            key = report["part_id"], report["state"]
            if key not in keys:
                entries.append({"payload": report, "acked": False, "attempts": 0, "available_at": time.time(), "error": ""})
                keys.add(key)
        self._write_reports(db, row_id, entries)

    @staticmethod
    def _write_reports(db, row_id, entries):
        due = min((x["available_at"] for x in entries if not x["acked"]), default=0)
        db.execute("UPDATE outbox SET reports_json=?, report_due_at=? WHERE id=?", (json.dumps(entries), due, row_id))

    def next_delivery_report(self):
        with self._connect() as db:
            row = db.execute("SELECT id,reports_json FROM outbox WHERE report_due_at>0 AND report_due_at<=? ORDER BY report_due_at LIMIT 1", (time.time(),)).fetchone()
        if row:
            for index, entry in enumerate(json.loads(row["reports_json"])):
                if not entry["acked"] and entry["available_at"] <= time.time():
                    return row["id"], index, entry["payload"]
        return None

    def finish_delivery_report(self, row_id, index, error=""):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            entries = json.loads(db.execute("SELECT reports_json FROM outbox WHERE id=?", (row_id,)).fetchone()[0])
            entry = entries[index]
            entry["attempts"] += 1
            entry.update(acked=not error, error=error, available_at=time.time() + min(300, 2 ** min(entry["attempts"], 8)))
            self._write_reports(db, row_id, entries)

    def mark_sending(self, item):
        with self._connect() as db:
            return bool(db.execute("UPDATE outbox SET state='sending',updated_at=? WHERE id=? AND lease_token=? AND state='processing'",
                                   (time.time(), item.row_id, item.lease_token)).rowcount)

    def complete_outbox(self, row_id, token, *, reports=()):
        self._update("outbox", row_id, token, "completed", reports=reports)

    def retry_outbox(self, row_id, token, error, delay=5):
        self._update("outbox", row_id, token, "retry", error, delay)

    def uncertain_outbox(self, row_id, token, error, *, reports=()):
        self._update("outbox", row_id, token, "uncertain", error, reports=reports)

    def fail_outbox(self, row_id, token, error, *, reports=()):
        self._update("outbox", row_id, token, "failed", error, reports=reports)

    def partial_outbox(self, row_id, token, error, *, accepted=(), refused=None, reports=()):
        with self._connect() as db:
            db.execute("UPDATE outbox SET accepted_json=?,refused_json=? WHERE id=? AND lease_token=?",
                       (json.dumps(list(accepted)), json.dumps(_refused_json(refused), ensure_ascii=False), row_id, token))
        self._update("outbox", row_id, token, "partial", error, reports=reports)

    def record_recipient_outcome(self, row_id, token, *, accepted=(), refused=None):
        with self._connect() as db:
            db.execute("UPDATE outbox SET accepted_json=?,refused_json=? WHERE id=? AND lease_token=?",
                       (json.dumps(list(accepted)), json.dumps(_refused_json(refused), ensure_ascii=False), row_id, token))

    def message_by_id(self, message_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM inbox WHERE message_id=? ORDER BY id DESC LIMIT 1", (str(message_id),)).fetchone()
        if not row:
            return None
        context = json.loads(row["context_json"] or "{}")
        return {
            "message_id": row["message_id"], "sender": row["sender"], "recipients": json.loads(row["recipients_json"] or "[]"),
            "cc": json.loads(row["cc_json"] or "[]"), "reply_to": json.loads(row["reply_to_json"] or "[]"),
            "in_reply_to": row["in_reply_to"], "references": json.loads(row["references_json"] or "[]"),
            **context,
        }

    def receipt(self, request_id):
        with self._connect() as db:
            r = db.execute("SELECT request_id,state,provider_message_id,attempts,last_error,updated_at,reporting_json,reports_json,accepted_json,refused_json FROM outbox WHERE request_id=?", (request_id,)).fetchone()
            if not r:
                return None
            result = dict(r)
            result["delivery_reporting"] = json.loads(result.pop("reporting_json"))
            result["history_reports"] = json.loads(result.pop("reports_json"))
            result["accepted_recipients"] = json.loads(result.pop("accepted_json") or "[]")
            result["refused_recipients"] = json.loads(result.pop("refused_json") or "{}")
            return result

    def status(self):
        with self._connect() as db:
            counts = {table: {r[0]: r[1] for r in db.execute(f"SELECT state,COUNT(*) FROM {table} GROUP BY state")} for table in ("inbox", "outbox")}
            counts["cursors"] = [json.loads(r[0]) for r in db.execute("SELECT value FROM runtime_state WHERE key LIKE 'cursor:%'")]
            counts["receipts"] = [dict(r) for r in db.execute("SELECT request_id,state,provider_message_id,last_error FROM outbox ORDER BY id DESC LIMIT 20")]
            counts["delivery_reports_pending"] = db.execute("SELECT COUNT(*) FROM outbox WHERE report_due_at>0").fetchone()[0]
            # Retained events still observing their original attempt or child (counted in their inbox state too).
            counts["inbox_retained"] = db.execute("SELECT COUNT(*) FROM inbox WHERE host_reference LIKE 'retained:%' AND state NOT IN ('completed','failed')").fetchone()[0]
            counts["delivery_support"] = self.get("delivery_support")
        return counts
