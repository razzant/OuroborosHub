"""Skill-owned mailbox configuration and credential lifetime, separate from mail history.

The registry is a private SQLite file. Disconnect erases credentials and invalidates
in-flight refreshes, but retains the account identity and its inbox/outbox directory.
Legacy EMAIL_* settings remain a live, single-account source without copying secrets.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from email.errors import HeaderParseError
from email.headerregistry import Address
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .settings import load_settings, normalize_words

SETTING_KEYS = [
    "EMAIL_IMAP_HOST", "EMAIL_IMAP_PORT", "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
    "EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_SMTP_USER", "EMAIL_SMTP_PASSWORD", "EMAIL_DEFAULT_FOLDER", "EMAIL_AUTH_MODE",
    "EMAIL_OAUTH_ACCESS_TOKEN", "EMAIL_OAUTH_REFRESH_TOKEN", "EMAIL_OAUTH_CLIENT_ID",
    "EMAIL_OAUTH_CLIENT_SECRET", "EMAIL_IMAP_SECURITY", "EMAIL_SMTP_SECURITY",
    "EMAIL_TLS_CA", "EMAIL_TLS_FINGERPRINT", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
]
_SECRET_KEYS = {"EMAIL_PASSWORD", "EMAIL_SMTP_PASSWORD", "EMAIL_OAUTH_ACCESS_TOKEN", "EMAIL_OAUTH_REFRESH_TOKEN",
                "EMAIL_OAUTH_CLIENT_SECRET"}
_CONNECTION_KEYS = set(SETTING_KEYS) - {"GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"}
_ANY_GENERATION = object()
if os.name == "nt":  # one byte of the account's lock file, through the CRT
    import msvcrt

    def _try_lock(handle):
        handle.seek(0)  # the locked byte starts here; a failure of the file itself is never contention
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):  # the CRT's words for a byte another handle holds
                return False
            raise
        return True

    def _unlock(handle):
        # Closing the handle releases the byte as well, so a failed unlock is raised, never hidden.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(handle):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # EWOULDBLOCK: another open file holds it; any other failure is raised
            return False
        return True

    def _unlock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _content(record):
    """A record's stored fields, without the row's own columns."""
    return {key: value for key, value in record.items() if key not in ("id", "generation", "connected", "_persisted")}


def bearer_mark(access):
    """A short fingerprint of one access token, kept beside the permissions Google named for it (`scope_of`)."""
    return hashlib.sha256(str(access).encode()).hexdigest()[:16]


def _offered(record, late):
    """Whether a transfer awaiting its answer was offered from this saved grant: the original fields, or the owner's
    later local sign-in `late`. Its recovery names exactly those, so no local refresh starts on them or writes them."""
    claim = record.get("gmail_transfer") or {}
    # "not_started" is gmail_host._NOT_STARTED: a transfer the host proved it never started.
    pending = claim and not claim.get("withdrawn") and claim.get("refusal") != "not_started"
    return bool(pending and (claim.get("generation") == late if late else not claim.get("renewal")))


def _refresh_place(row, generation, *, legacy_owner, source, late, connection):
    """(record, the saved grant a local refresh of `source` may mark or write), or (None, None): see refresh_tokens."""
    if not row or not row["connected"]:
        return None, None
    record = json.loads(row["record"])
    if (record.get("gmail_host") or record.get("gmail_transfer")) and not legacy_owner:
        return None, None
    place = record.get("gmail_late_consent") if late else record
    if not isinstance(place, dict) or (late and place.get("generation") != late) or _offered(record, late):
        return None, None
    if connection is None:
        same = row["generation"] == generation
    else:
        same = bool(connection) and (record.get("gmail_connection") or {}).get("id") == connection
    if not same or (source and (place.get("tokens") or {}).get("refresh_token") != source):
        return None, None
    return record, place


def public_message_ref(account_id, native_ref):
    """Use the same account-bound locator in tool results and incoming Place events."""
    payload = json.dumps({"account_id": account_id, "ref": native_ref}, separators=(",", ":")).encode()
    return "mail:" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


class AccountError(ValueError):
    """An explicit account selection or connection is unavailable."""


def _ews_endpoint(value):
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        if (parts.scheme != "https" or not host or parts.username is not None or
                parts.query or parts.fragment or "\\" in url or
                any(char.isspace() or ord(char) < 32 for char in url)):
            raise ValueError
    except ValueError:
        raise AccountError("Enter an HTTPS EWS service URL without credentials, a query or a fragment") from None
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        authority += f":{port}"
    return urlunsplit(("https", authority, parts.path or "/", "", ""))


class AccountStore:
    def __init__(self, state_dir, legacy=None, *, sign_in=None):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "accounts.sqlite3"
        self.legacy = dict(legacy or {})
        self.sign_in = sign_in

    @contextmanager
    def _db(self, *, write=False):
        if write:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
            db = sqlite3.connect(self.path, timeout=15)
        else:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id TEXT PRIMARY KEY, record TEXT NOT NULL, generation TEXT NOT NULL,
                        connected INTEGER NOT NULL, updated_at REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS oauth_pending (
                        state TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL);
                """)
            with db:
                yield db
        finally:
            db.close()

    def _legacy_account(self):
        address = str(self.legacy.get("EMAIL_USER") or "").strip()
        if not address:
            return None
        settings = load_settings(self.state_dir)
        return {"id": address, "provider": "imap", "address": address, "label": address,
                "senders": [address], "sender": address, "connected": True, "legacy": True, "uses_env": True,
                "generation": "legacy",
                "config": {key: value for key, value in self.legacy.items() if key in _CONNECTION_KEYS},
                "sending": settings.get("sending", "on"), "words": settings.get("words", []),
                "folder": settings.get("folder") or self.legacy.get("EMAIL_DEFAULT_FOLDER") or "INBOX",
                "poll_interval_sec": settings.get("poll_interval_sec", 30)}

    def list(self):
        records = []
        if self.path.exists():
            with self._db() as db:
                records = [self._record(row) for row in db.execute("SELECT * FROM accounts ORDER BY updated_at,id")]
        legacy = self._legacy_account()
        if legacy and not any(row["id"] == legacy["id"] for row in records):
            # The first observed environment account owns the old mail directory.
            # Repointing EMAIL_USER later must not reuse that mailbox's UIDs/outbox.
            if any(row.get("legacy") for row in records):
                legacy["legacy"] = False
            records.insert(0, legacy)
        return records

    def _record(self, row):
        record = {**json.loads(row["record"]), "id": row["id"], "generation": row["generation"],
                  "connected": bool(row["connected"]), "_persisted": True}
        if record.get("uses_env"):
            current = self._legacy_account()
            if current and current["id"] == record["id"]:
                for key in ("config", "folder", "sending", "words", "poll_interval_sec"):
                    record[key] = current[key]
            else:
                record.update(config={}, connected=False)
        return record

    def get(self, account_id):
        # The host guard already compared this exact ID. Never resolve aliases,
        # spelling variants or an unknown ID to another credential afterwards.
        for account in self.list():
            if account["id"] == account_id:
                return account
        raise AccountError("Unknown account_id; choose an exact id from email_accounts")

    def resolve(self, account_id=""):
        if account_id:
            account = self.get(account_id)
        else:
            connected = [row for row in self.list() if row["connected"]]
            if len(connected) != 1:
                raise AccountError("Choose account_id from email_accounts" if connected else "Connect a mailbox in Email accounts first")
            account = connected[0]
        if not account["connected"]:
            if account.get("gmail_host") or account.get("gmail_transfer"):
                # Email accounts names this Ouroboros's place: the sign-in card, or a new local sign-in on an older one.
                raise AccountError("Check this Google account's sign-in in Email accounts")
            raise AccountError("This account is disconnected; reconnect it in Email accounts")
        if account.get("uses_env") and not account.get("_persisted"):
            account = self.save(account)
        return account

    def mail_dir(self, account):
        if account.get("legacy"):
            return self.state_dir
        return self.state_dir / "accounts" / hashlib.sha256(account["id"].encode()).hexdigest()

    @staticmethod
    def public(account):
        config = account.get("config") or {}
        parked = account.get("gmail_host") or account.get("gmail_transfer")
        return {key: account[key] for key in ("id", "provider", "address", "label", "senders", "sender", "senders_status", "senders_checked_at", "connected", "runtime_error",
                                             "sending", "folder", "poll_interval_sec", "words", "gmail_auth_state",
                                             "oauth_client_label", "auth_owner") if key in account} | {
            "legacy": bool(account.get("legacy")),
            "environment_config": bool(account.get("uses_env")),
            "scopes": list(account.get("scopes") or []),
            "scope_status": "known" if account.get("scopes") is not None else "unknown",
            "oauth_client_id": "" if parked else (account.get("oauth") or {}).get("client_id", ""),
            "connection": {key: value for key, value in config.items() if key not in _SECRET_KEYS},
            "password_configured": bool(config.get("EMAIL_PASSWORD")),
            "refresh_token_configured": not parked and bool((account.get("tokens") or {}).get("refresh_token") or
                                                             config.get("EMAIL_OAUTH_REFRESH_TOKEN")),
        }

    def save(self, record, *, connected=True, expected_generation=_ANY_GENERATION, expected=None):
        """None as an explicit generation means this ID must not yet exist. `expected`, the stored record this one was
        built from, must still be stored: a write made in place since (a renewed token, a queued mail's connection)
        is never replaced by that older copy."""
        record = dict(record)
        account_id = str(record.pop("id"))
        record.pop("generation", None)
        record.pop("connected", None)
        record.pop("_persisted", None)
        if record.get("uses_env"):
            record["config"] = {}  # Environment grants remain their only credential owner.
        generation = uuid.uuid4().hex
        with self._db(write=True) as db:
            if expected_generation is not _ANY_GENERATION:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT generation,record FROM accounts WHERE id=?", (account_id,)).fetchone()
                if (current["generation"] if current is not None else None) != expected_generation:
                    return None
                if (expected is not None and current is not None
                        and _content(json.loads(current["record"])) != _content(expected)):
                    return None
            db.execute("INSERT INTO accounts VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "record=excluded.record,generation=excluded.generation,connected=excluded.connected,updated_at=excluded.updated_at",
                       (account_id, json.dumps(record, ensure_ascii=False), generation, int(connected), time.time()))
        return self.get(account_id)

    def _compare_account(self, account_id, build):
        """Apply a conditional transition to the current row and return exactly what this transaction stored."""
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None:
                return None
            current = {**json.loads(row["record"]), "id": account_id, "generation": row["generation"],
                       "connected": bool(row["connected"])}
            record = build(current)
            if record is None:
                return None
            saved, connected = _content(record), bool(record["connected"])
            generation = row["generation"]
            if saved != _content(current) or connected != current["connected"]:
                generation = uuid.uuid4().hex
                db.execute("UPDATE accounts SET record=?,generation=?,connected=?,updated_at=? WHERE id=?",
                           (json.dumps(saved, ensure_ascii=False), generation, int(connected), time.time(), account_id))
        return {**saved, "id": account_id, "generation": generation, "connected": connected, "_persisted": True}

    def reserve(self, account, build, *, connected):
        """Reserve an offer only while the row it was prepared from is still current."""
        def prepare(current):
            if current["generation"] != account.get("generation"):
                return None
            record = build(current)
            return {**record, "connected": bool(connected)} if record is not None else None

        return self._compare_account(account["id"], prepare)

    def settle_gmail_transfer(self, account_id, generation, build):
        """Settle the exact reserved offer, not a previous snapshot of the mailbox's preferences."""
        def prepare(current):
            claim = current.get("gmail_transfer") or {}
            if current.get("provider") != "gmail" or not generation or claim.get("generation") != generation:
                return None
            return build(current)

        return self._compare_account(account_id, prepare)

    def save_gmail_consent(self, consent, *, local=False, new_connection=False, borrowed=False):
        """File a completed legacy callback without overwriting a transfer or host binding.

        On an older host (`local`) that consent also connects a mailbox that moved: no sign-in card exists there to do
        it. `new_connection` (every explicitly passed consent) starts another connection id, so mail queued earlier is
        never sent automatically through this sign-in. A `borrowed` refresh token, the saved one Google kept, is taken
        from the record at this write, never from a value read before it."""
        for _ in range(3):
            try:
                old = self.get(consent["id"])
            except AccountError:
                old = {}
            if old and old.get("provider") != "gmail":
                raise AccountError("The Google mailbox ID is already in use")
            moved = bool(old.get("gmail_host") or old.get("gmail_transfer"))
            if borrowed:
                # The saved grant's own refresh token as stored now, so a renewal since this consent began keeps its
                # rotation. One that moved, ended or changed client meanwhile is not borrowed.
                kept = (old.get("tokens") or {}).get("refresh_token")
                previous_oauth = old.get("oauth") or {}
                if moved or not kept or previous_oauth.get("client_id") != consent["oauth"]["client_id"]:
                    raise AccountError("Account settings changed during sign-in; check the Email accounts widget")
                consent = {**consent, "tokens": {**consent["tokens"], "refresh_token": kept},
                           "oauth": {**consent["oauth"], "withdrawals_seen": previous_oauth.get("withdrawals_seen")}}
            if moved:
                # This is fresh consent, never a copy of the already-transferred grant.
                record = {**old, "gmail_late_consent": {
                    "generation": uuid.uuid4().hex,
                    **{key: consent[key] for key in ("oauth", "tokens", "scopes")},
                }}
                connected = old["connected"] or local
            else:
                record = {**consent, **{key: old[key] for key in
                    ("label", "folder", "sending", "words", "poll_interval_sec", "gmail_connection") if key in old}}
                if old.get("sender") in record["senders"]:
                    record["sender"] = old["sender"]
                connected = True
            link = old.get("gmail_connection")
            if new_connection or not (isinstance(link, dict) and link.get("id")):
                # A new consent, a recorded break, or no record of this mailbox's earlier access: this sign-in starts its
                # own connection; mail queued before keeps its own record (or none) and is not sent through it.
                record["gmail_connection"] = {"id": uuid.uuid4().hex}
            saved = self.save(record, connected=connected, expected_generation=old.get("generation"),
                              expected=old or None)
            if saved is not None:
                return saved
        raise AccountError("Account settings changed during sign-in; check the Email accounts widget")

    def save_imap(self, values):
        address = str(values.get("address") or "").strip()
        username = str(values.get("username") or address).strip()
        host = str(values.get("imap_host") or "").strip()
        smtp_host = str(values.get("smtp_host") or "").strip()
        if not address or not host or not smtp_host or any(c in address + host + smtp_host for c in "\r\n\0"):
            raise AccountError("Address, IMAP host and SMTP host are required without line breaks")
        identity = "imap:" + hashlib.sha256(json.dumps([host.lower(), username]).encode()).hexdigest()[:32]
        old = None
        if values.get("account_id"):
            old = self.get(str(values["account_id"]))
            if old.get("uses_env"):
                raise AccountError("Legacy credentials are managed in Settings; add a separate account or keep that configuration")
            if old["provider"] != "imap" or old["config"]["EMAIL_USER"] != username or old["config"]["EMAIL_IMAP_HOST"] != host:
                raise AccountError("A different mailbox needs its own account; do not replace an existing account identity")
            identity = old["id"]
        else:
            try:
                old = self.get(identity)
            except AccountError:
                pass
        config = dict((old or {}).get("config") or {})
        mapping = {"imap_host": "EMAIL_IMAP_HOST", "smtp_host": "EMAIL_SMTP_HOST",
                   "imap_security": "EMAIL_IMAP_SECURITY", "smtp_security": "EMAIL_SMTP_SECURITY",
                   "tls_ca": "EMAIL_TLS_CA", "tls_fingerprint": "EMAIL_TLS_FINGERPRINT"}
        for field, key in mapping.items():
            if field in values:
                config[key] = str(values[field] or "").strip()
        for field, key, default in (("imap_port", "EMAIL_IMAP_PORT", 993), ("smtp_port", "EMAIL_SMTP_PORT", 587)):
            config[key] = int(values.get(field) or config.get(key) or default)
            if not 1 <= config[key] <= 65535:
                raise AccountError("Ports must be between 1 and 65535")
        config.update(EMAIL_USER=username, EMAIL_AUTH_MODE="password")
        if "smtp_username" in values:
            config["EMAIL_SMTP_USER"] = str(values["smtp_username"] or username)
        if values.get("smtp_password"):
            config["EMAIL_SMTP_PASSWORD"] = str(values["smtp_password"])
        if values.get("password"):
            config["EMAIL_PASSWORD"] = str(values["password"])
        if not config.get("EMAIL_PASSWORD"):
            raise AccountError("An app password or Bridge password is required")
        for key, default in (("EMAIL_IMAP_SECURITY", "ssl"), ("EMAIL_SMTP_SECURITY", "starttls")):
            config[key] = config.get(key) or default
            if config[key] not in {"ssl", "starttls"}:
                raise AccountError("TLS mode must be ssl or starttls")
        return self.save({"id": identity, **self._password_record("imap", address, config, values, old)})

    def save_ews(self, values):
        endpoint = _ews_endpoint(values.get("endpoint"))
        address = str(values.get("address") or "").strip()
        try:
            mailbox = Address(addr_spec=address)
            if not mailbox.username or not mailbox.domain:
                raise ValueError
        except (ValueError, HeaderParseError):
            raise AccountError("Enter the Exchange mailbox email address") from None
        identity = "ews:" + hashlib.sha256(json.dumps([endpoint, address.casefold()]).encode()).hexdigest()[:32]
        edit_id = values.get("account_id")
        if edit_id is not None and edit_id != identity:
            raise AccountError("A different mailbox or EWS endpoint needs its own account")

        def connection(old):
            previous_login = (old or {}).get("config", {}).get("EMAIL_USER")
            username = str(values.get("username", previous_login if edit_id else None) or address).strip()
            if not username or any(c in username for c in "\r\n\0"):
                raise AccountError("Enter an Exchange login without line breaks")
            password = values.get("password")
            # A retained password belongs to this login on this endpoint, never a new destination.
            if not password and edit_id and old and previous_login == username:
                password = old["config"].get("EMAIL_PASSWORD")
            if not isinstance(password, str) or not password:
                raise AccountError("Enter the Exchange password for this login")
            config = {"EWS_ENDPOINT": endpoint, "EMAIL_USER": username, "EMAIL_PASSWORD": password}
            return self._password_record("ews", (old or {}).get("address", address), config, values, old)

        try:
            self.get(identity)
        except AccountError:
            if edit_id:
                raise
            return self.save({"id": identity, **connection(None)})
        # Read the current credentials and preferences under the same lock as disconnect.
        return self._change(identity, lambda row: row.update(connection(row)), connected=True)

    @staticmethod
    def _password_record(provider, address, config, values, old):
        raw_senders = (values.get("senders") if "senders" in values else (old or {}).get("senders")) or [address]
        senders = raw_senders.splitlines() if isinstance(raw_senders, str) else list(raw_senders)
        senders = list(dict.fromkeys([address] + [str(x).strip() for x in senders if str(x).strip()]))
        if any(c in sender for sender in senders for c in "\r\n\0"):
            raise AccountError("Sender addresses cannot contain line breaks")
        record = {"provider": provider, "address": address,
                  "label": str(values.get("label") or (old or {}).get("label") or address),
                  "senders": senders, "sender": str(values.get("sender") or (old or {}).get("sender") or address),
                  "config": config, "sending": values.get("sending", (old or {}).get("sending", "on")),
                  "folder": str(values.get("folder") or (old or {}).get("folder") or "INBOX"),
                  "words": normalize_words(values.get("words", (old or {}).get("words", [])) or []),
                  "poll_interval_sec": max(5, min(3600, int(values.get("poll_interval_sec") or (old or {}).get("poll_interval_sec") or 30)))}
        if record["sending"] not in {"on", "off"}:
            raise AccountError("Sending must be on or off")
        if record["sender"] not in senders:
            raise AccountError("Default sender must be one of this account's configured addresses")
        return record

    def _change(self, account_id, mutate, *, connected=None):
        # Preferences and disconnect serialize with refresh, so editing a label
        # cannot overwrite a token another request just refreshed.
        current = self.get(account_id)
        if current.get("uses_env") and not current.get("_persisted"):
            self.save(current)
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None:
                raise AccountError("Unknown account_id")
            record = json.loads(row["record"])
            mutate(record)
            db.execute("UPDATE accounts SET record=?,generation=?,connected=?,updated_at=? WHERE id=?",
                       (json.dumps(record, ensure_ascii=False), uuid.uuid4().hex,
                        row["connected"] if connected is None else int(connected), time.time(), account_id))
        return self.get(account_id)

    def disconnect(self, account_id):
        def clear(record):
            record["config"] = {key: value for key, value in record.get("config", {}).items() if key not in _SECRET_KEYS}
            record.pop("tokens", None)
            record.pop("oauth", None)
            if record.get("provider") == "gmail":
                record.pop("gmail_legacy_copy", None)
                record.pop("gmail_late_consent", None)
                record["gmail_connection"] = {"ended": True}  # mail queued before is never sent through a later sign-in
                if isinstance(record.get("gmail_transfer"), dict):
                    record["gmail_transfer"]["withdrawn"] = True  # a late host answer never reconnects it by itself
        return self._change(account_id, clear, connected=False)

    def update_preferences(self, account_id, values):
        def change(record):
            if record.get("uses_env"):
                raise AccountError("Use the existing settings form for the legacy mailbox")
            for key in ("label", "folder", "sending", "sender"):
                if key in values:
                    record[key] = str(values[key])
            if record.get("sending") not in {"on", "off"}:
                raise AccountError("Sending must be on or off")
            if record.get("sender", record["address"]) not in record["senders"]:
                raise AccountError("Choose a configured sender for this account")
            if "words" in values:
                record["words"] = normalize_words(values["words"])
            if "poll_interval_sec" in values:
                record["poll_interval_sec"] = max(5, min(3600, int(values["poll_interval_sec"])))
        return self._change(account_id, change)

    def update_senders(self, account_id, senders, generation):
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row or not row["connected"] or row["generation"] != generation:
                raise AccountError("Account changed while sender addresses were being read; refresh the current account")
            record = json.loads(row["record"])
            record["senders"] = list(dict.fromkeys([record["address"]] + list(senders)))
            record.update(senders_status="verified", senders_checked_at=time.time())
            if record.get("sender") not in record["senders"]:
                record["sender"] = record["address"]
            db.execute("UPDATE accounts SET record=?,generation=?,updated_at=? WHERE id=?",
                       (json.dumps(record), uuid.uuid4().hex, time.time(), account_id))
        return self.get(account_id)

    @contextmanager
    def account_lock(self, account_id, *, wait=0.0):
        """This account's own lock, held by one local renewal or one transfer reservation of it at a time and by no
        other mailbox. The OS releases it when its holder ends; a stopped holder keeps it, so no competitor renews or
        reserves beside it. Yields False when another holder kept it for `wait` seconds; an OS failure is raised."""
        folder = self.state_dir / "locks"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / (hashlib.sha256(account_id.encode()).hexdigest() + ".lock"), "a+b") as handle:
            deadline = time.monotonic() + wait
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
            try:
                yield True
            finally:
                _unlock(handle)

    def refresh_tokens(self, account_id, generation, tokens, *, legacy_owner=False, source="", late="", connection=None):
        """Store a local refresh made under account_lock, only for the original sign-in.

        `connection` is captured before the refresh: preferences may change the row generation, but a new consent
        gets a different connection. Without a captured connection, require the original row generation. Also match
        `source` (the refresh token used) and `late` (the later local sign-in's generation, if one was used). An open
        transfer's own fields stay untouched, as do disconnects. A bearer's scope stays with that exact bearer."""
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            record, place = _refresh_place(row, generation, legacy_owner=legacy_owner, source=source, late=late,
                                           connection=connection)
            if place is None:
                return False
            kept = {key: value for key, value in (place.get("tokens") or {}).items()
                    if not (key in ("scope", "scope_of") and "access_token" in tokens)}
            place["tokens"] = {**kept, **tokens}
            db.execute("UPDATE accounts SET record=?,updated_at=? WHERE id=?",
                       (json.dumps(record), time.time(), account_id))
            return True

    def expire_access(self, account_id, access, *, late=""):
        """A local bearer Gmail refused is not presented again: its next use refreshes the grant."""
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM accounts WHERE id=?", (account_id,)).fetchone()
            record = json.loads(row["record"]) if row else {}
            place = record.get("gmail_late_consent") if late else record
            if not isinstance(place, dict) or (late and place.get("generation") != late):
                return
            tokens = place.get("tokens")
            if isinstance(tokens, dict) and access and tokens.get("access_token") == access:
                tokens["expires_at"] = 0
                db.execute("UPDATE accounts SET record=? WHERE id=?", (json.dumps(record), account_id))

    def gmail_connection(self, account_id, *, expected_generation=_ANY_GENERATION):
        """Name this Gmail mailbox's uninterrupted connection, recording an id if it has none.

        No worker restart. A disconnect ends it and an explicit consent starts another. If a caller has already
        selected a saved grant, it gives that row's generation and never obtains the id of a newer sign-in instead."""
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record,connected,generation FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row or not row["connected"]:
                raise AccountError("This account is disconnected; reconnect it in Email accounts")
            if expected_generation is not _ANY_GENERATION and row["generation"] != expected_generation:
                raise AccountError("Account changed while its access was being named; use the current connection")
            record = json.loads(row["record"])
            link = record.get("gmail_connection")
            if isinstance(link, dict) and isinstance(link.get("id"), str) and link["id"]:
                return link["id"]
            # Only the access serving it from now on is recorded: mail queued before has no record and stays held.
            record["gmail_connection"] = {"id": uuid.uuid4().hex}
            db.execute("UPDATE accounts SET record=? WHERE id=?", (json.dumps(record), account_id))
            return record["gmail_connection"]["id"]

    def runtime_error(self, account_id, error):
        """Startup failure is a local observation, never a new credential generation."""
        if not self.path.exists():
            return
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row:
                record = json.loads(row["record"])
                record["runtime_error"] = str(error)
                db.execute("UPDATE accounts SET record=? WHERE id=?", (json.dumps(record), account_id))

    def begin_oauth(self, state, value):
        with self._db(write=True) as db:
            db.execute("DELETE FROM oauth_pending WHERE created_at<?", (time.time() - 1800,))
            db.execute("INSERT INTO oauth_pending VALUES(?,?,?)", (state, json.dumps(value), time.time()))

    def consume_oauth(self, state):
        if not self.path.exists():
            raise AccountError("Unknown or expired authorization; start Google connection again")
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value,created_at FROM oauth_pending WHERE state=?", (state,)).fetchone()
            db.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
        if not row or time.time() - row["created_at"] > 1800:
            raise AccountError("Unknown or expired authorization; start Google connection again")
        return json.loads(row["value"])
