"""One durable inbox/outbox and per-account IMAP cursor; no cognitive state."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from email.utils import parseaddr
from pathlib import Path

from .delivery import email_report
from .mime import conversation_root, mailbox_addresses, prior_imap_capture, recipient_groups

_MAX_ATTACHMENT_FILES = 10
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
# Rule A for an outgoing Gmail mail whose submission began and whose outcome is unknown: its mailbox is searched for it
# no sooner than this after the answer was lost, since Gmail search can lag a moment behind a send (GmailClient
# find_submitted); a search that fails is asked again after a pause doubling from a minute to an hour.
RECONCILE_AFTER = 300
_RECONCILE_LONGEST = 3600
# One sentence the owner is told through the host (owner_notice), kept here until the host confirms it.
_NOTICE = "owner_notice:"
# One notice of each kind at most per queued mail: `waiting` while its mailbox is signed out, `held` while a Place keeps
# rule A from writing its Drafts, `drafted` or `unresolved` once rule A settled it.
_NOTICE_KINDS = ("waiting", "held", "drafted", "unresolved")
_SUBJECT_IN_NOTICE = 150  # characters of a subject quoted in a notice
_NOTICE_LONGEST = 400  # the host's limit for one notice (POST /notify)
_QUEUED = ("pending", "retry", "processing")  # a mail that has not begun to go out
_UNKNOWN = ("uncertain", "partial")  # a mail whose sending began and whose outcome is unknown


def _delivery_state(state, error):
    return "held" if state == "retry" and str(error).startswith("Held:") else state


def _unsettled(state, reporting_json):
    """A mail of unknown outcome that rule A has not settled yet (no `reconciled` record)."""
    return state in _UNKNOWN and "reconciled" not in json.loads(reporting_json or "{}")


def _held_otherwise(error, hold_kind):
    """Its last try was held by a Place or another check of Mail, which its mailbox's sign-in does not lift. A hold for
    access (the sign-in itself) or for Sending off (read from the mailbox as it is now) is not counted here."""
    return str(error).startswith("Held:") and hold_kind not in ("access", "sending_off")


def _shown_whole(request_id):
    return (isinstance(request_id, str) and bool(request_id) and request_id.isprintable()
            and not any(ch.isspace() for ch in request_id))


def _physical_source(message):
    """The provider message a read came from: its Gmail/Exchange ref, else its IMAP folder, UIDVALIDITY and UID.
    None when a stored read does not name one (e.g. one kept by an earlier version without those fields)."""
    ref = message.get("message_ref")
    if isinstance(ref, str) and ref:
        return ["ref", ref]
    where = ref if isinstance(ref, dict) else message
    if where.get("uid") is not None and where.get("uidvalidity") is not None:
        return ["imap", str(where.get("folder") or ""), str(where["uidvalidity"]), str(where["uid"])]
    return ["provider", str(message["provider_id"])] if message.get("provider_id") else None


def _read_source_key(message_id, source):
    data = json.dumps([str(message_id or ""), source], separators=(",", ":"), ensure_ascii=True)
    return "read_source:" + hashlib.sha256(data.encode("ascii")).hexdigest()


def _notice(template, subject, address, request_id=""):
    """One notice sentence naming a mail by its subject (on one line, shortened as needed) and by its request_id, which
    "send" and "delete" take, within the host's limit. A request_id that cannot be shown whole on that line is left out
    rather than cut: the mailbox's status lists the mail that waits (EmailStore.status `waiting`)."""
    ref = f" (request_id {request_id})" if _shown_whole(request_id) else ""
    if ref and len(template.format(subject="", address=address, ref=ref)) + 20 > _NOTICE_LONGEST:
        ref = ""
    words = " ".join(str(subject or "").split()) or "(no subject)"
    room = max(20, min(_SUBJECT_IN_NOTICE, _NOTICE_LONGEST - len(template.format(subject="", address=address, ref=ref))))
    quoted = words if len(words) <= room else words[:room - 1].rstrip() + "…"
    text = template.format(subject=quoted, address=address, ref=ref)
    return text if len(text) <= _NOTICE_LONGEST else text[:_NOTICE_LONGEST - 1] + "…"


def drafted_notice(subject, address, request_id=""):
    return _notice('The email "{subject}"{ref} is in Drafts of {address}: check it and send it.',
                   subject, address, request_id)


def unresolved_notice(subject, address, request_id=""):
    return _notice('The email "{subject}"{ref} may already have gone out from {address}, and it could not be put in '
                   'Drafts: check Sent.', subject, address, request_id)


def draft_held_notice(subject, address, request_id=""):
    return _notice('The email "{subject}"{ref} may already have gone out from {address}; it is put in Drafts there once '
                   "a Place or this skill's access in Ouroboros allows writing Drafts: check Sent, or ask to delete it.",
                   subject, address, request_id)


def waiting_notice(subject, address, *, unknown=False, hold="", request_id=""):
    """`hold`: what holds the mail besides its mailbox's sign-in: "" (nothing: it goes by itself once signed in),
    "sending_off" (Sending is off for that mailbox) or "held" (its last try was held by a Place or a check of Mail)."""
    if unknown:
        template = ('The email "{subject}"{ref} may already have gone out from {address}: once that mailbox is signed in '
                    'to Mail again it is checked in Sent, never sent twice, and put in Drafts if it is not there.')
    elif hold == "sending_off":
        template = ('The email "{subject}"{ref} waits for {address} to be signed in to Mail again, and Sending is off '
                    'for that mailbox: it goes out once both allow it; you can also ask to send it from another mailbox '
                    'or to delete it.')
    elif hold:
        template = ('The email "{subject}"{ref} waits for {address} to be signed in to Mail again and was also held when '
                    'last tried (its receipt says why): it goes out once that allows it; you can also ask to send it '
                    'from another mailbox or to delete it.')
    else:
        template = ('The email "{subject}"{ref} waits for {address} to be signed in to Mail again and then goes by '
                    'itself; you can also ask to send it from another mailbox or to delete it.')
    return _notice(template, subject, address, request_id)


class RequestIdConflict(ValueError):
    """A request id already names a different outgoing message."""


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
        return conversation_root(references=self.references, in_reply_to=self.in_reply_to, message_id=self.message_id)

    @property
    def provider_event_key(self):
        return self.context.get("provider_event_key") or f"{self.folder}:{self.uidvalidity}:{self.uid}:{self.message_id}"


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
        recipients = [parseaddr(value)[1] for value in mailbox_addresses(self.recipients)]
        if not recipients:
            raise ValueError("Mail delivery needs at least one valid envelope recipient")
        return recipients


class EmailStore:
    """One mailbox's durable queues over one kept SQLite connection.

    The connection opens on first use and stays open until ``close`` (a later call opens
    it again); callers take turns on it under the store's lock, whatever thread they run
    on. A loop with nothing to claim only reads: it takes no write lock and writes
    nothing. Every commit is synchronous=FULL: a ``sending`` mark must survive a power
    loss, or a message could go out twice from the owner's mailbox.
    """

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.state_dir / "email_presence.sqlite3"
        self._lock = threading.Lock()
        self._db = None
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # Existing mail and its owner's access policy remain intact.
        else:
            os.close(descriptor)
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
                                      ("report_due_at", "REAL NOT NULL DEFAULT 0"),
                                      # What held it last (hold_outbox), kept apart from the accepted row it describes.
                                      ("hold_kind", "TEXT NOT NULL DEFAULT ''")):
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

    def __del__(self):
        # Short-lived tool callers may drop the store without an explicit stop.
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()

    def close(self):
        """Close the kept connection once its current user is done."""
        with self._lock:
            db, self._db = self._db, None
            if db is not None:
                db.close()

    def _open(self):
        db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _connect(self):
        """The kept connection for one caller at a time: committed on success, rolled back on error."""
        with self._lock:
            if self._db is None:
                self._db = self._open()
            db = self._db
            try:
                with db:
                    yield db
            finally:
                if db.in_transaction:
                    # Its commit and rollback both failed. Closing discards the transaction, as it
                    # did for a per-call connection; the next call opens a fresh one.
                    self._db = None
                    db.close()

    def get(self, key):
        with self._connect() as db:
            return self._get(db, key)

    @staticmethod
    def _get(db, key):
        row = db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self._connect() as db:
            self._put(db, key, value)

    def put_first(self, key, value):
        """Keep ``value`` under ``key`` unless a record is already kept there, in one statement: the first record stays,
        whichever process writes next. True when this call kept it."""
        with self._connect() as db:
            return db.execute("INSERT INTO runtime_state VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                              (key, json.dumps(value), time.time())).rowcount == 1

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

    def _stage_raw_source(self, raw, *, identity, bucket="inbound"):
        if not isinstance(raw, bytes):
            return None
        digest = hashlib.sha256(raw).hexdigest()
        root = self.state_dir / "staged" / bucket / hashlib.sha256(str(identity).encode()).hexdigest()
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
            if (item.get("content_available") is False and not item.get("path") and
                    not any(isinstance(item.get(k), (bytes, str)) for k in ("data", "content", "data_base64", "content_base64"))):
                # A provider may describe an attachment without yielding its
                # bytes. An explicitly supplied b"" below is a real empty file.
                descriptors.append(dict(item))
                continue
            if str(item.get("content_type") or item.get("mime_type") or "").lower().startswith(("message/", "multipart/")):
                # Containers (attached mail, delivery/read reports) have no single payload to stage.
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
            for key in ("within", "container", "container_index", "depth", "mime_part"):
                if key in item:  # the nearest container that carried it, and where the part sits in the raw MIME
                    descriptor[key] = item[key]
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
            message["attachment_stage_note"] = ("Some attachment bytes were not staged; the full RFC822 source artifact is available."
                                                if source else "Some attachment bytes were not staged; no raw RFC822 source was supplied.")
        return staged

    def stage_outbound_attachments(self, request_id, attachments):
        return self._stage_files(attachments, bucket="outbound", identity=request_id)

    def stage_draft(self, request_id, letter):
        """An existing Gmail draft queued by its draft_id, as it was read then (GmailClient.draft_letter): its exact source,
        which rule A puts back in that mailbox's Drafts if its send's outcome is unknown and the draft is gone, and its
        files, so its receipt gives the whole mail to send from another mailbox. Both are immutable files kept with the
        mailbox's other staged mail, as large as the draft (its files twice: in the source and on their own); they go
        with the mailbox's state. Returns ``(source, files)``."""
        files = [item for item in letter.get("attachments") or () if isinstance(item, dict)
                 and isinstance(item.get("data"), bytes)]  # a carried message stays in the source only
        staged = self._stage_files(files, bucket="outbound", identity=request_id, enforce_limits=False)
        source = self._stage_raw_source(letter["_raw_source"], identity="draft:" + str(request_id), bucket="outbound")
        return {key: source[key] for key in ("path", "sha256", "size")}, staged

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

    def captured_before_id_repair(self, message):
        """UIDVALIDITY changed: require the same folder, old captured ID and original bytes."""
        witness = prior_imap_capture(message.get("_raw_source"), message["message_id"])
        if witness is None:
            return False
        old_id, digest = witness
        # Existing UNIQUE(folder, message_id) locates one candidate. Historical
        # tool reads never entered this table and cannot suppress new intake.
        with self._connect() as db:
            return db.execute("""SELECT 1 FROM inbox WHERE folder=? AND message_id=?
                AND json_extract(context_json,'$.source_artifact.sha256')=?""",
                (message["folder"], old_id, digest)).fetchone() is not None

    def has_native_event(self, folder, event_key):
        """The exact opaque event is already in this folder's durable inbox."""
        with self._connect() as db:
            return db.execute("""SELECT 1 FROM inbox WHERE uidvalidity=-1 AND folder=?
                              AND json_extract(context_json,'$.provider_event_key')=? LIMIT 1""",
                              (folder, event_key)).fetchone() is not None

    def ingest(self, message, *, cursor_key=None, cursor=None):
        now = time.time()
        if message.get("_raw_source") is not None or (
                message.get("attachments") and not message.get("staged_files")
                and any(isinstance(item, dict) and isinstance(item.get("data"), bytes)
                        for item in message["attachments"])):
            self.stage_inbound_attachments(message)
        context = {key: message[key] for key in (
            "sender_name", "cc", "date", "reply_to", "headers", "address_names", "thread_headers", "raw_address_headers", "attachments", "source_artifact", "attachment_stage_note",
            "provider_id", "provider_thread_id", "provider_event_key", "message_ref", "labels", "source_format", "source_note"
        ) if key in message}
        staged = list(message.get("staged_files") or ())
        with self._connect() as db:
            uid = message.get("uid")
            validity = message.get("uidvalidity", 0)
            if uid is None:
                # Native opaque IDs cannot be truncated to an IMAP UID. Use
                # the next local inbox address only when actually capturing;
                # retain the exact provider key so replay remains idempotent.
                if not context.get("provider_event_key"):
                    raise ValueError("A native event key or a numeric UID is required")
                db.execute("BEGIN IMMEDIATE")
                validity = -1  # Local address space, never an IMAP generation.
                db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS inbox_native_event
                           ON inbox(folder,json_extract(context_json,'$.provider_event_key'))
                           WHERE uidvalidity=-1""")
                uid = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM inbox").fetchone()[0]
            cur = db.execute("""INSERT OR IGNORE INTO inbox
                (folder,uidvalidity,uid,message_id,in_reply_to,references_json,sender,subject,body,recipients_json,thread_key,context_json,cc_json,reply_to_json,attachments_json,staged_files_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    message.get("folder", "INBOX"), validity, int(uid),
                    message["message_id"], message.get("in_reply_to", ""), json.dumps(message.get("references", [])),
                    message.get("sender", ""), message.get("subject", ""), message.get("body", ""),
                    json.dumps(message.get("recipients", [])), conversation_root(references=message.get("references"), in_reply_to=message.get("in_reply_to"), message_id=message["message_id"]),
                    json.dumps(context, ensure_ascii=False), json.dumps(message.get("cc", [])), json.dumps(message.get("reply_to", [])),
                    json.dumps(message.get("attachments", []), ensure_ascii=False), json.dumps(staged, ensure_ascii=False), now, now))
            if cursor_key:
                self._put(db, cursor_key, cursor)
            # lastrowid is the connection's newest insert: an ignored duplicate has none of its own.
            return (int(cur.lastrowid or 0) if cur.rowcount else 0), bool(cur.rowcount)

    def _claim(self, table, lease_seconds, scope="", order="id"):
        now = time.time()
        token = uuid.uuid4().hex
        fifo = "AND NOT EXISTS (SELECT 1 FROM inbox earlier WHERE earlier.id < inbox.id AND earlier.thread_key = inbox.thread_key AND earlier.state IN ('pending','retry','processing') AND earlier.host_reference='')" if table == "inbox" else ""
        claimable = f"((state IN ('pending','retry') AND available_at<=?) OR (state='processing' AND lease_until<=?)) {scope} {fifo}"
        with self._connect() as db:
            # Plain reads first: with no lost send to settle and nothing claimable, no write lock is taken.
            if not ((table == "outbox" and db.execute(
                    "SELECT 1 FROM outbox WHERE state='sending' AND lease_until<=? LIMIT 1", (now,)).fetchone())
                    or db.execute(f"SELECT 1 FROM {table} WHERE {claimable} LIMIT 1", (now, now)).fetchone()):
                return None
            db.execute("BEGIN IMMEDIATE")
            if table == "outbox":
                self._settle_lost_sends(db, now)
            row = db.execute(f"SELECT * FROM {table} WHERE {claimable} ORDER BY {order} LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            db.execute(f"UPDATE {table} SET state='processing',lease_token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?", (token, now + lease_seconds, now, row["id"]))
            result = dict(row)
            result.update(lease_token=token, attempts=row["attempts"] + 1)
            return result

    def _settle_lost_sends(self, db, now):
        """SMTP DATA or a provider's send can have been accepted before a process disappears: a mail whose `sending` lease
        ran out is of unknown outcome, never sent again. Run where its worker claims mail and, for a mailbox no worker
        serves (signed out), where its notices are kept (note_waiting)."""
        for row in db.execute("SELECT * FROM outbox WHERE state='sending' AND lease_until<=?", (now,)).fetchall():
            report = email_report(self._outbox_item(row), "uncertain", error="Process lost during provider acceptance")
            self._freeze_reports(db, row["id"], [report] if report else [])
            self._retire(db, row["request_id"])
        db.execute("UPDATE outbox SET state='uncertain',last_error='Process lost during mail delivery; inspect Message-ID before retry',updated_at=? WHERE state='sending' AND lease_until<=?", (now, now))

    def claim_inbox(self, lease_seconds=2100, submitted=None):
        """Lease claimable mail, keeping each thread's FIFO.

        ``submitted=False`` claims only mail without a stored Host reference (new
        input), oldest first; ``True`` only mail that has one (observation of
        admitted work), earliest due first, so a reference released after its poll
        never overtakes one already waiting; ``None`` new mail first, then either.
        """
        scope, order = {None: ("", "host_reference<>'', CASE WHEN host_reference<>'' THEN available_at END, id"),
                        False: ("AND host_reference=''", "id"),
                        True: ("AND host_reference<>''", "available_at, id")}[submitted]
        r = self._claim("inbox", lease_seconds, scope, order)
        if not r:
            return None
        return self._inbox_item(r)

    @staticmethod
    def _inbox_item(r):
        return InboxItem(r["id"], r["lease_token"], r["folder"], r["uid"], r["message_id"], r["in_reply_to"],
                         tuple(json.loads(r["references_json"])), r["sender"], r["subject"], r["body"],
                         tuple(json.loads(r["recipients_json"])), r["host_reference"], r["attempts"], r["uidvalidity"],
                         json.loads(r["context_json"]), tuple(json.loads(r["cc_json"] or "[]")),
                         tuple(json.loads(r["reply_to_json"] or "[]")), tuple(json.loads(r["attachments_json"] or "[]")),
                         tuple(json.loads(r["staged_files_json"] or "[]")))

    def inbox_event(self, folder, uidvalidity, uid):
        """The captured message at this folder/UIDVALIDITY/UID (its unique key), or None (read-only)."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM inbox WHERE folder=? AND uidvalidity=? AND uid=?",
                             (folder, uidvalidity, uid)).fetchone()
        return self._inbox_item({**dict(row), "lease_token": ""}) if row else None

    def set_host_reference(self, row_id, token, reference):
        with self._connect() as db:
            db.execute("UPDATE inbox SET host_reference=?,updated_at=? WHERE id=? AND lease_token=?", (reference, time.time(), row_id, token))

    def _update(self, table, row_id, token, state, error="", delay=0, reports=(), reporting=None):
        with self._connect() as db:
            changed = db.execute(f"UPDATE {table} SET state=?,last_error=?,lease_token='',lease_until=0,available_at=?,updated_at=? WHERE id=? AND lease_token=?",
                                 (state, error, time.time() + delay, time.time(), row_id, token)).rowcount
            if not changed:
                return False
            if table == "outbox":
                if reporting is not None:
                    db.execute("UPDATE outbox SET reporting_json=? WHERE id=?", (json.dumps(reporting), row_id))
                self._freeze_reports(db, row_id, reports)
                if state != "retry":  # it went, failed or began to go: a line saying that it waits no longer holds
                    self._retire(db, db.execute("SELECT request_id FROM outbox WHERE id=?", (row_id,)).fetchone()[0])
        return True

    def complete_inbox(self, row_id, token):
        return self._update("inbox", row_id, token, "completed")

    def retry_inbox(self, row_id, token, error, delay=5):
        self._update("inbox", row_id, token, "retry", error, delay)

    def fail_inbox(self, row_id, token, error):
        return self._update("inbox", row_id, token, "failed", error)

    def active_host_references(self):
        with self._connect() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT host_reference FROM inbox "
                    "WHERE state IN ('pending','retry','processing') AND host_reference<>''")]

    def queued_message(self, request_id):
        """The original outgoing snapshot; replay does not need its provider source again."""
        with self._connect() as db:
            row = db.execute('SELECT * FROM outbox WHERE request_id=?', (str(request_id),)).fetchone()
        return self._outbox_item(row) if row is not None else None

    def enqueue_outbox(self, *, request_id, recipients=(), subject="", body="", in_reply_to="", references=(), reporting=None,
                       to=None, cc=(), bcc=(), attachments=(), html_body="", body_type="plain", hold_reason="",
                       message_id=""):
        """Queue one outgoing mail under `request_id`; `message_id` is the Message-ID it goes out with when that is not
        Mail's own (a mail sent from an existing draft keeps the draft's)."""
        now = time.time()
        visible_to, visible_cc, visible_bcc = recipient_groups(recipients if to is None else to, cc, bcc)
        envelope = visible_to + visible_cc + visible_bcc
        if not envelope:
            raise ValueError("Mail delivery needs at least one valid envelope recipient")
        message_id = message_id or self.message_id_for(request_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM outbox WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                candidate = {"subject": subject, "body": body, "in_reply_to": in_reply_to,
                             "html_body": str(html_body or ""), "body_type": str(body_type or "plain")}
                groups = {"to_json": visible_to, "cc_json": visible_cc, "bcc_json": visible_bcc,
                          "references_json": list(references), "attachments_json": list(attachments)}
                if (any(existing[key] != value for key, value in candidate.items())
                        or any(json.loads(existing[key]) != value for key, value in groups.items())):
                    raise RequestIdConflict("This request_id already names a different email; use a new request_id for a new send")
                previous = json.loads(existing["reporting_json"])
                if previous.get("sender") and previous["sender"] != (reporting or {}).get("sender"):
                    raise RequestIdConflict("This request_id already names a different sender")
                return False
            cur = db.execute("""INSERT OR IGNORE INTO outbox(request_id,recipients_json,subject,body,in_reply_to,references_json,provider_message_id,created_at,updated_at,reporting_json,to_json,cc_json,bcc_json,attachments_json,html_body,body_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (request_id, json.dumps(envelope), subject, body,
                                               in_reply_to, json.dumps(list(references)), message_id, now, now, json.dumps(reporting or {}),
                                               json.dumps(visible_to), json.dumps(visible_cc), json.dumps(visible_bcc), json.dumps(list(attachments), ensure_ascii=False), str(html_body or ""), str(body_type or "plain")))
            if cur.rowcount and hold_reason:
                db.execute("UPDATE outbox SET state='retry',last_error=?,available_at=? WHERE id=?",
                           ("Held: " + str(hold_reason).removeprefix("Held: "), now + 5, cur.lastrowid))
            return bool(cur.rowcount)

    def message_id_for(self, request_id):
        return "<" + hashlib.sha256((str(self.path.resolve()) + ":" + str(request_id)).encode()).hexdigest() + "@ouroboros.local>"

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
    def _groups(r):
        """To, Cc and Bcc as queued. A row from before they were kept apart names every recipient in To; a mail with only
        Cc or Bcc recipients keeps them there, never in To."""
        groups = tuple(tuple(json.loads(r[name] or "[]")) for name in ("to_json", "cc_json", "bcc_json"))
        return groups if any(groups) else (tuple(json.loads(r["recipients_json"])), (), ())

    @classmethod
    def _outbox_item(cls, r):
        return OutboxItem(r["id"], r["lease_token"], r["request_id"], tuple(json.loads(r["recipients_json"])),
                          r["subject"], r["body"], r["in_reply_to"], tuple(json.loads(r["references_json"])),
                          r["attempts"], r["provider_message_id"], json.loads(r["reporting_json"]),
                          *cls._groups(r),
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

    def complete_outbox(self, row_id, token, *, reports=(), reporting=None):
        self._update("outbox", row_id, token, "completed", reports=reports, reporting=reporting)

    def retry_outbox(self, row_id, token, error, delay=5):
        self._update("outbox", row_id, token, "retry", error, delay)

    def hold_outbox(self, item, reason, *, delay=5, kind="policy"):
        """Hold a leased mail unsent; it is asked about again after `delay` seconds. `kind` records what holds it:
        `access` (its mailbox's sign-in cannot serve it now), `sending_off`, or `policy` (a Place or a check of Mail),
        so a line about its signed-out mailbox says whether the sign-in alone holds it (note_waiting). It is kept in its
        own column: a hold never rewrites the accepted row (its envelope and reporting stay as they were queued)."""
        with self._connect() as db:
            db.execute("UPDATE outbox SET state='retry',last_error=?,lease_token='',lease_until=0,"
                       "available_at=?,updated_at=?,attempts=MAX(0,attempts-1),hold_kind=? WHERE id=? AND lease_token=?",
                       (str(reason), time.time() + delay, time.time(), str(kind), item.row_id, item.lease_token))

    def uncertain_outbox(self, row_id, token, error, *, reports=()):
        # Never sent again by a pass; its mailbox is searched for it once Gmail search can show it (claim_uncertain).
        self._update("outbox", row_id, token, "uncertain", error, RECONCILE_AFTER, reports=reports)

    def claim_uncertain(self, lease_seconds=300):
        """Lease the next mail whose submission began and whose outcome is unknown (`uncertain`, or `partial` where some
        recipients were accepted) and that rule A has not settled yet (no `reconciled` record), once its pause is over.
        Its state stays as it is: no pass ever sends it again. None when there is nothing due (read only)."""
        now = time.time()
        due = ("state IN ('uncertain','partial') AND lease_until<=? AND available_at<=? "
               "AND json_extract(reporting_json,'$.reconciled') IS NULL")
        with self._connect() as db:
            if not db.execute(f"SELECT 1 FROM outbox WHERE {due} LIMIT 1", (now, now)).fetchone():
                return None
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT * FROM outbox WHERE {due} ORDER BY id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE outbox SET lease_token=?,lease_until=? WHERE id=?", (token, now + lease_seconds, row["id"]))
        return self._outbox_item({**dict(row), "lease_token": token})

    def settle_uncertain(self, item, outcome, *, receipt=None, draft=None, error="", notice=""):
        """Record what rule A found or did for a leased unknown-outcome mail, once, with the owner's notice in the same
        write: `sent` (found in the mailbox: completed, with its provider receipt), `drafted` (a draft of it is in the
        mailbox), `unresolved` (neither found nor saved) or `discarded` (the owner dropped it). False when the lease
        was lost meanwhile."""
        reporting = {**item.reporting, "reconciled": {"outcome": outcome, "at": time.time(),
                                                      **({"error": error} if error else {})}}
        if draft:
            reporting["draft"] = dict(draft)
        if receipt:
            reporting["provider_receipt"] = dict(receipt)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE outbox SET reporting_json=?,state=CASE WHEN ? THEN 'completed' ELSE state END,"
                "lease_token='',lease_until=0,updated_at=? WHERE id=? AND lease_token=?",
                (json.dumps(reporting), outcome == "sent", time.time(), item.row_id, item.lease_token)).rowcount
            if not changed:
                return False
            if outcome == "sent":
                self._freeze_reports(db, item.row_id, [email_report(replace(item, reporting=reporting), "accepted")])
            self._retire(db, item.request_id)  # it is settled: a line that it waits no longer holds
            if notice:
                self._note(db, item.request_id, outcome, notice)
        return True

    def defer_uncertain(self, item, note, *, notice=""):
        """Ask about a leased unknown-outcome mail again later: its mailbox could not be searched or written now. `notice`,
        when a Place keeps rule A from writing that mailbox's Drafts, is the owner's line about it (kept once, `held`)."""
        tries = int(item.reporting.get("reconcile_tries") or 0) + 1
        reporting = {**item.reporting, "reconcile_tries": tries, "reconcile_note": str(note)}
        with self._connect() as db:
            changed = db.execute("UPDATE outbox SET reporting_json=?,lease_token='',lease_until=0,available_at=?,updated_at=? "
                                 "WHERE id=? AND lease_token=?",
                                 (json.dumps(reporting), time.time() + min(_RECONCILE_LONGEST, 60 * 2 ** min(tries - 1, 6)),
                                  time.time(), item.row_id, item.lease_token)).rowcount
            if changed and notice:
                self._note(db, item.request_id, "held", notice)

    def discard_queued(self, request_id):
        """The owner's "delete" for a mail that has not gone out, in one write: one still queued or held, or taken by a
        worker that stopped before its sending began (its lease ran out), is cancelled; its receipt stays, with the mail,
        so replaying the same request_id never queues it again and the mail can still be sent from another mailbox. One
        whose outcome is unknown and not yet settled is never drafted. A mail a worker is on this moment, sent, failed or
        already settled is left as it is. The result is its receipt, with the mail, and whether it was dropped."""
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE request_id=?", (str(request_id),)).fetchone()
            if row is None:
                return None, False
            item = self._outbox_item(row)
            leased = bool(row["lease_token"]) and row["lease_until"] > now
            if row["state"] in _QUEUED and not leased:
                # A stopped worker's lease is cleared with it: that worker can no longer mark it sending (mark_sending).
                db.execute("UPDATE outbox SET state='cancelled',last_error='Deleted by the owner before it was sent',"
                           "lease_token='',lease_until=0,updated_at=? WHERE id=?", (now, row["id"]))
                self._freeze_reports(db, row["id"], [email_report(item, "failed", error="Deleted by the owner")])
                dropped = True
            elif _unsettled(row["state"], row["reporting_json"]) and not leased:
                reporting = {**item.reporting, "reconciled": {"outcome": "discarded", "at": now}}
                db.execute("UPDATE outbox SET reporting_json=?,updated_at=? WHERE id=?",
                           (json.dumps(reporting), now, row["id"]))
                dropped = True
            else:
                dropped = False
            if dropped:
                self._retire(db, row["request_id"])
        return self.receipt(request_id, with_letter=True), dropped

    def release_queued(self, request_id, link=None):
        """The owner's "send" for a mail still queued or held (or taken by a worker that stopped before its sending began):
        it is due now, under `link` when given (the access its mailbox's current confirmed sign-in gives it). Its holds are
        judged again as it goes; False when it is not such a mail (a worker is on it this moment, sent, failed, cancelled
        or of unknown outcome)."""
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE request_id=?", (str(request_id),)).fetchone()
            if row is None or row["state"] not in _QUEUED or (row["lease_token"] and row["lease_until"] > now):
                return False
            reporting = json.loads(row["reporting_json"])
            if link:
                reporting["gmail_connection"] = dict(link)
            db.execute("UPDATE outbox SET reporting_json=?,available_at=?,updated_at=? WHERE id=?",
                       (json.dumps(reporting), now, now, row["id"]))
        return True

    @staticmethod
    def _note(db, request_id, kind, text):
        """Keep one notice for the owner about one queued mail: the first kept for that mail and kind stays."""
        now = time.time()
        notice = {"request_id": str(request_id), "kind": kind, "text": str(text), "state": "pending", "attempts": 0,
                  "available_at": now, "created_at": now}
        db.execute("INSERT INTO runtime_state VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                   (f"{_NOTICE}{request_id}:{kind}", json.dumps(notice, ensure_ascii=False), now))

    @staticmethod
    def _retire(db, request_id, kinds=("waiting", "held")):
        """End the notices of these kinds about one mail that the host has not shown the owner: written in the same write
        that settles, sends, starts sending or drops that mail, since they no longer hold. One already shown stays."""
        db.executemany("DELETE FROM runtime_state WHERE key=? AND json_extract(value,'$.state')<>'sent'",
                       [(f"{_NOTICE}{request_id}:{kind}",) for kind in kinds])

    def note_waiting(self, address, *, sending_off=False):
        """While this mailbox is signed out of Mail: one notice for each mail of it that has not gone out (queued or held)
        and each whose outcome is unknown and not yet settled, naming the mailbox and what else holds the mail
        (`sending_off`: Sending is off for this mailbox now). It waits; nothing is sent or drafted elsewhere. A send
        whose worker stopped during it is first known to be of unknown outcome, as a worker's claim would find it.
        Returns how many were kept now."""
        kept = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._settle_lost_sends(db, time.time())
            rows = db.execute("SELECT request_id,subject,state,last_error,hold_kind,reporting_json FROM outbox WHERE "
                              "state IN ('pending','retry','processing','uncertain','partial')").fetchall()
            for row in rows:
                unknown = row["state"] in _UNKNOWN
                if unknown and not _unsettled(row["state"], row["reporting_json"]):
                    continue
                key = f"{_NOTICE}{row['request_id']}:waiting"
                if self._get(db, key) is not None:
                    continue
                hold = ("" if unknown else "sending_off" if sending_off else
                        "held" if _held_otherwise(row["last_error"], row["hold_kind"]) else "")
                self._note(db, row["request_id"], "waiting", waiting_notice(row["subject"], address, unknown=unknown,
                                                                            hold=hold, request_id=row["request_id"]))
                kept += 1
        return kept

    @staticmethod
    def _holds(db, notice, signed_out):
        """Whether a notice not yet shown still says what is true: a `waiting` one while its mail has not gone out (or is
        of unknown outcome, unsettled) and its mailbox is signed out; a `held` one while its mail is unsettled. A
        `drafted` or `unresolved` one reports what rule A did, which stays true."""
        if notice.get("kind") not in ("waiting", "held"):
            return True
        row = db.execute("SELECT state,reporting_json FROM outbox WHERE request_id=?",
                         (str(notice.get("request_id")),)).fetchone()
        if row is None:
            return False
        unsettled = _unsettled(row["state"], row["reporting_json"])
        return unsettled if notice["kind"] == "held" else signed_out and (row["state"] in _QUEUED or unsettled)

    def due_notices(self, *, signed_out=False):
        """The owner's notices to post now: (key, notice), oldest first. `signed_out`: this mailbox is signed out now.
        A notice that no longer holds (_holds) is ended unposted, under the write lock; otherwise this only reads."""
        now = time.time()
        with self._connect() as db:
            pending = self._pending_notices(db)
            if any(not self._holds(db, notice, signed_out) for _, notice in pending):
                db.execute("BEGIN IMMEDIATE")
                pending = self._pending_notices(db)  # as they are now, under the write lock
                ended = {key for key, notice in pending if not self._holds(db, notice, signed_out)}
                db.executemany("DELETE FROM runtime_state WHERE key=?", [(key,) for key in ended])
                pending = [(key, notice) for key, notice in pending if key not in ended]
        return [(key, notice) for key, notice in pending if notice["available_at"] <= now]

    @staticmethod
    def _pending_notices(db):
        rows = db.execute("SELECT key,value FROM runtime_state WHERE key LIKE ? ORDER BY updated_at,key",
                          (_NOTICE + "%",)).fetchall()
        notices = [(row["key"], json.loads(row["value"])) for row in rows]
        return [(key, notice) for key, notice in notices if notice.get("state") == "pending"]

    def finish_notice(self, key, outcome, delay=0, note=""):
        """`sent` once the host wrote it; `invalid` when the host refused its text, which posting again cannot change
        (it stays in the status); otherwise it stays pending and is posted again after `delay` seconds."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            notice = self._get(db, key)
            if notice is None:
                return
            notice.update(attempts=int(notice.get("attempts") or 0) + 1, note=str(note))
            if outcome in ("sent", "invalid"):
                notice.update(state=outcome, **({"sent_at": time.time()} if outcome == "sent" else {}))
            else:
                notice["available_at"] = time.time() + delay
            self._put(db, key, notice)

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

    def reply_sources(self, message_id):
        from .mime import canonical_message_id
        token = canonical_message_id(message_id)
        with self._connect() as db:
            # Existing keys remain unchanged, including comment-bearing IDs and old read caches.
            ids = [r[0] for r in db.execute('SELECT message_id FROM inbox')
                   if canonical_message_id(r[0]) == token]
            messages = []
            for identity in dict.fromkeys(ids):
                for row in db.execute('SELECT * FROM inbox WHERE message_id=? ORDER BY id DESC', (identity,)):
                    context = json.loads(row['context_json'] or '{}')
                    messages.append({**context, 'message_id': row['message_id'], 'sender': row['sender'],
                        'folder': row['folder'], 'uid': row['uid'], 'uidvalidity': row['uidvalidity'],
                        'recipients': json.loads(row['recipients_json'] or '[]'), 'cc': json.loads(row['cc_json'] or '[]'),
                        'reply_to': json.loads(row['reply_to_json'] or '[]'), 'in_reply_to': row['in_reply_to'],
                        'references': json.loads(row['references_json'] or '[]')})
            messages.extend(json.loads(row[0]) for row in db.execute(
                "SELECT value FROM runtime_state WHERE key LIKE 'read_message:%' OR key LIKE 'read_source:%'")
                if canonical_message_id(json.loads(row[0]).get('message_id')) == token)
        return messages

    def message_by_id(self, message_id):
        from .mime import unique_reply_source
        return unique_reply_source(self.reply_sources(message_id), message_id)

    def retained_outbox_hold(self, item):
        """An accepted damaged envelope stays intact. A new parser is not consent to repair and send it."""
        from .mime import reply_chain
        from .retained import legacy_ews_source
        # New rows were built from verified source syntax before acceptance. Only legacy accepted rows need
        # retrospective source inspection; ordinary delivery/anchor lookup must not scan the whole inbox.
        if not item.in_reply_to or item.reporting.get('reply_source_syntax') == 1:
            return ''
        for source in self.reply_sources(item.in_reply_to):
            if legacy_ews_source(source):
                return 'Held: Legacy Exchange reply source lost address syntax; accepted envelope needs reconciliation'
            before = list(source.get('references') or ([source['in_reply_to']] if source.get('in_reply_to') else []))
            after = reply_chain(source)
            if before != after and list(item.references) == before:
                return 'Held: Accepted reply used comment-derived ancestry; original headers and queue are preserved'
        return ''

    def historical_message_roots(self, selection):
        """Stored scopes of this physical message whose old reference projection differs from raw syntax."""
        from .mime import reply_chain
        wanted = _physical_source(selection)
        if wanted is None:
            return []
        with self._connect() as db:
            rows = db.execute('SELECT * FROM inbox WHERE folder=? AND uidvalidity=? AND uid=?',
                              (selection.get('folder', 'INBOX'), selection.get('uidvalidity'), selection.get('uid'))).fetchall()
            if selection.get('message_ref'):
                rows = db.execute("SELECT * FROM inbox WHERE json_extract(context_json,'$.message_ref')=?",
                                  (selection['message_ref'],)).fetchall()
            sources = [{**json.loads(row['context_json'] or '{}'), 'message_id': row['message_id'],
                        'references': json.loads(row['references_json']), 'in_reply_to': row['in_reply_to']}
                       for row in rows]
            sources.extend(m for row in db.execute(
                "SELECT value FROM runtime_state WHERE key LIKE 'read_message:%' OR key LIKE 'read_source:%'")
                for m in [json.loads(row[0])] if _physical_source(m) == wanted)
        from .retained import UndecidableThreadSyntax
        roots = []
        for source in sources:
            old = conversation_root(references=source.get('references'), in_reply_to=source.get('in_reply_to'),
                                    message_id=source.get('message_id'))
            try:
                actual = reply_chain(source)
            except UndecidableThreadSyntax:
                actual = None  # unknown ancestry: the stored scope keeps its own check
            if old and actual != list(source.get('references') or
                                      ([source['in_reply_to']] if source.get('in_reply_to') else [])):
                roots.append(old)
        return list(dict.fromkeys(roots))

    def remember_read(self, message):
        """Keep reply headers for an explicitly read old mail without admitting it to Presence.

        ``read_message:<Message-ID>`` keeps the latest read, as before. When the read it replaces came from another
        physical message (provider ref, or IMAP folder/UIDVALIDITY/UID) with a different reply authority, that read
        is kept too, under ``read_source:<digest>``: message_by_id then still sees both and refuses the ambiguous ID
        instead of silently answering whichever was read last. A later read of a kept source refreshes its entry."""
        from .mime import reply_authority
        keys = ("message_id", "sender", "sender_name", "headers", "address_names", "thread_headers", "raw_address_headers", "source_format", "recipients", "cc", "reply_to", "in_reply_to", "references",
                "provider_id", "provider_thread_id", "message_ref", "folder", "uid", "uidvalidity", "source_artifact")
        record = {key: message[key] for key in keys if key in message}
        key = "read_message:" + str(message["message_id"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous, source = self._get(db, key), _physical_source(record)
            if isinstance(previous, dict):
                earlier = _physical_source(previous)
                from .retained import legacy_ews_source
                from .mime import conversation_root
                root = lambda m: conversation_root(references=m.get('references'), in_reply_to=m.get('in_reply_to'),
                                                   message_id=m.get('message_id'))
                if earlier == source and (legacy_ews_source(previous) or root(previous) != root(record)
                                          or previous.get('references') != record.get('references')):
                    # Keep the old interpretation as evidence even after a fresh read of the same provider item.
                    # Its independent key cannot be refreshed away by the next read_source update below.
                    self._put(db, _read_source_key(previous.get('message_id'), ['historical', earlier,
                        hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()]), previous)
                if source and earlier and earlier != source and reply_authority(previous) != reply_authority(record):
                    self._put(db, _read_source_key(previous.get("message_id"), earlier), previous)
            own = _read_source_key(record.get("message_id"), source) if source else ""
            if own and self._get(db, own) is not None:
                self._put(db, own, record)
            self._put(db, key, record)

    def receipt(self, request_id, *, with_letter=False):
        """The delivery receipt of `request_id`; `with_letter` adds, while it has not gone out, the mail as queued."""
        with self._connect() as db:
            r = db.execute("SELECT request_id,state,provider_message_id,attempts,last_error,updated_at,reporting_json,reports_json,accepted_json,refused_json FROM outbox WHERE request_id=?", (request_id,)).fetchone()
            if not r:
                return None
            result = dict(r)
            result["state"] = _delivery_state(result["state"], result["last_error"])
            result["delivery_reporting"] = json.loads(result.pop("reporting_json"))
            result["delivery_reporting"].pop("reply_all_fingerprint", None)
            result["account_id"] = result["delivery_reporting"].get("account_id", "")
            result["sender"] = result["delivery_reporting"].get("sender", "")
            result["provider_receipt"] = result["delivery_reporting"].get("provider_receipt")
            result["history_reports"] = json.loads(result.pop("reports_json"))
            result["accepted_recipients"] = json.loads(result.pop("accepted_json") or "[]")
            result["refused_recipients"] = json.loads(result.pop("refused_json") or "{}")
            settled = result["delivery_reporting"].get("reconciled")
            if settled:
                # Rule A's outcome for a mail whose submission began: found sent, drafted, unresolved or discarded.
                result["unknown_outcome"] = settled.get("outcome")
            if (result["delivery_reporting"].get("draft") or {}).get("id"):
                result["draft_id"] = result["delivery_reporting"]["draft"]["id"]  # in this mailbox's Gmail Drafts
            notices = [json.loads(row[0]) for row in db.execute(
                f"SELECT value FROM runtime_state WHERE key IN ({','.join('?' * len(_NOTICE_KINDS))})",
                tuple(f"{_NOTICE}{request_id}:{kind}" for kind in _NOTICE_KINDS))]
            if notices:
                result["owner_notices"] = [{key: notice.get(key) for key in ("kind", "text", "state")}
                                           for notice in notices]
            if with_letter and r["state"] in (*_QUEUED, *_UNKNOWN, "cancelled"):
                # The mail as queued, also once the owner dropped it here, so it can be sent from another mailbox.
                row = db.execute("SELECT recipients_json,to_json,cc_json,bcc_json,subject,body,html_body,body_type,"
                                 "in_reply_to,references_json,attachments_json FROM outbox WHERE request_id=?",
                                 (request_id,)).fetchone()
                to, cc, bcc = (list(group) for group in self._groups(row))
                result["letter"] = {"to": to, "cc": cc, "bcc": bcc, "subject": row["subject"], "body": row["body"],
                                    "html_body": row["html_body"], "body_type": row["body_type"],
                                    "in_reply_to": row["in_reply_to"], "references": json.loads(row["references_json"]),
                                    "attachments": [{"filename": item.get("filename"), "path": item.get("path")}
                                                    for item in json.loads(row["attachments_json"] or "[]")
                                                    if isinstance(item, dict)]}
            return result

    def status(self):
        with self._connect() as db:
            counts = {table: {r[0]: r[1] for r in db.execute(f"SELECT state,COUNT(*) FROM {table} GROUP BY state")} for table in ("inbox", "outbox")}
            counts["cursors"] = [json.loads(r[0]) for r in db.execute("SELECT value FROM runtime_state WHERE key LIKE 'cursor:%'")]
            counts["receipts"] = [{**dict(r), "state": _delivery_state(r["state"], r["last_error"])} for r in db.execute(
                "SELECT request_id,state,provider_message_id,last_error FROM outbox ORDER BY id DESC LIMIT 20")]
            counts["delivery_reports_pending"] = db.execute("SELECT COUNT(*) FROM outbox WHERE report_due_at>0").fetchone()[0]
            counts["outbox_held"] = db.execute("SELECT COUNT(*) FROM outbox WHERE state='retry' AND last_error LIKE 'Held:%'").fetchone()[0]
            # What the owner is to be told and the host has not confirmed yet (owner_notice): shown here meanwhile.
            notices = [json.loads(row[0]) for row in db.execute(
                "SELECT value FROM runtime_state WHERE key LIKE ? ORDER BY updated_at", (_NOTICE + "%",))]
            counts["owner_notices"] = [{key: notice.get(key) for key in ("request_id", "kind", "text", "attempts", "created_at")}
                                       for notice in notices if notice.get("state") != "sent"][-20:]
            # Every mail that has not gone out and is not settled, by the request_id a line names it with ("send" and
            # "delete" take it) and its subject, however many receipts came since.
            counts["waiting"] = [{"request_id": r["request_id"], "subject": r["subject"],
                                  "state": _delivery_state(r["state"], r["last_error"])}
                                 for r in db.execute("SELECT request_id,subject,state,last_error,reporting_json FROM outbox "
                                                     "WHERE state IN ('pending','retry','processing','sending',"
                                                     "'uncertain','partial') ORDER BY id")
                                 if r["state"] not in _UNKNOWN or _unsettled(r["state"], r["reporting_json"])]
            # Retained events still observing their original attempt or child (counted in their inbox state too).
            counts["inbox_retained"] = db.execute("SELECT COUNT(*) FROM inbox WHERE host_reference LIKE 'retained:%' AND state NOT IN ('completed','failed')").fetchone()[0]
            counts["delivery_support"] = self._get(db, "delivery_support")
        return counts
