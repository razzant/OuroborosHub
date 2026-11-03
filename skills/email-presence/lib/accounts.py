"""Skill-owned mailbox configuration and credential lifetime, separate from mail history.

The registry is a private SQLite file. Disconnect erases credentials and invalidates
in-flight refreshes, but retains the account identity and its inbox/outbox directory.
Legacy EMAIL_* settings remain a live, single-account source without copying secrets.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

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


class AccountError(ValueError):
    """An explicit account selection or connection is unavailable."""


class AccountStore:
    def __init__(self, state_dir, legacy=None):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "accounts.sqlite3"
        self.legacy = dict(legacy or {})

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
        return {key: account[key] for key in ("id", "provider", "address", "label", "senders", "sender", "senders_status", "senders_checked_at", "connected", "runtime_error",
                                             "sending", "folder", "poll_interval_sec", "words") if key in account} | {
            "legacy": bool(account.get("legacy")),
            "environment_config": bool(account.get("uses_env")),
            "scopes": list(account.get("scopes") or []),
            "scope_status": "known" if account.get("scopes") is not None else "unknown",
            "oauth_client_id": (account.get("oauth") or {}).get("client_id", ""),
            "connection": {key: value for key, value in config.items() if key not in _SECRET_KEYS},
            "password_configured": bool(config.get("EMAIL_PASSWORD")),
            "refresh_token_configured": bool((account.get("tokens") or {}).get("refresh_token") or
                                              config.get("EMAIL_OAUTH_REFRESH_TOKEN")),
        }

    def save(self, record, *, connected=True):
        record = dict(record)
        account_id = str(record.pop("id"))
        record.pop("generation", None)
        record.pop("connected", None)
        record.pop("_persisted", None)
        if record.get("uses_env"):
            record["config"] = {}  # Environment grants remain their only credential owner.
        generation = uuid.uuid4().hex
        with self._db(write=True) as db:
            db.execute("INSERT INTO accounts VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "record=excluded.record,generation=excluded.generation,connected=excluded.connected,updated_at=excluded.updated_at",
                       (account_id, json.dumps(record, ensure_ascii=False), generation, int(connected), time.time()))
        return self.get(account_id)

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
        raw_senders = (values.get("senders") if "senders" in values else (old or {}).get("senders")) or [address]
        senders = raw_senders.splitlines() if isinstance(raw_senders, str) else list(raw_senders)
        senders = list(dict.fromkeys([address] + [str(x).strip() for x in senders if str(x).strip()]))
        if any(c in sender for sender in senders for c in "\r\n\0"):
            raise AccountError("Sender addresses cannot contain line breaks")
        record = {"id": identity, "provider": "imap", "address": address,
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
        return self.save(record)

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

    def refresh_tokens(self, account_id, generation, tokens):
        """A late refresh never revives an account disconnected or reconnected meanwhile."""
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row or not row["connected"] or row["generation"] != generation:
                return False
            record = json.loads(row["record"])
            record["tokens"] = {**record.get("tokens", {}), **tokens}
            db.execute("UPDATE accounts SET record=?,updated_at=? WHERE id=?",
                       (json.dumps(record), time.time(), account_id))
            return True

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
