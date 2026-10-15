"""Durable Bot API inbox, offset and non-replaying outbox.

The next getUpdates(offset) confirms older Telegram updates: advance offset only
in the SAME transaction as capture. Mark an outbound intent attempted BEFORE
its HTTP call; Bot API has no MTProto random_id for safe automatic resend.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

MAX_PENDING = 1000


class InboxFull(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path, *, recover_attempted: bool = False):
        path = Path(path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("private store path unavailable")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS inbound (
            update_id INTEGER PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','held','done')),
            binding_id TEXT NOT NULL DEFAULT '', work_ref TEXT NOT NULL DEFAULT '',
            transcript TEXT NOT NULL DEFAULT '')""")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS outbound (
            source_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER,
            reply_id INTEGER NOT NULL, binding_id TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','attempted','unknown','sent')),
            remote_id INTEGER, reported INTEGER NOT NULL DEFAULT 0)""")
        if recover_attempted:
            # Only the exclusive companion after prior process teardown owns
            # recovery. A passive status route must never settle a live send.
            with self.db:
                self.db.execute("UPDATE outbound SET status='unknown' WHERE status='attempted'")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def offset(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def skip_unbound_text(self, update: dict) -> None:
        """A non-media, non-bound update has no private payload to retain.

        The decision is made before this transaction from the exact current
        mapping; the offset alone records that the update was deliberately
        excluded, so unrelated chatter cannot fill the media inbox quota.
        """
        uid = update.get("update_id") if type(update) is dict else None
        if type(uid) is not int or uid < 0:
            raise ValueError("Bot API update lacks numeric identity")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""INSERT INTO meta(key,value) VALUES ('offset',?)
                ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)""", (uid + 1,))

    def bind_account(self, account_id: int) -> None:
        """An existing inbox cannot be reused under a rotated bot identity."""
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("authenticated bot identity unavailable")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT value FROM meta WHERE key='account_id'").fetchone()
            if row and row[0] != account_id:
                raise ValueError("bot account changed; inspect private inbox before reset")
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('account_id',?)", (account_id,))

    def capture(self, update: dict, *, binding_id: str | None = None) -> str:
        # Freeze the exact owner mapping in the same transaction as this update.
        # "unbound" is a private capture, never authority gained by a later edit.
        if binding_id is not None and (type(binding_id) is not str or
                re.fullmatch(r"[0-9a-fA-F]{32}", binding_id) is None):
            raise ValueError("invalid capture binding")
        frozen = binding_id if binding_id is not None else "unbound"
        uid = update.get("update_id") if type(update) is dict else None
        if type(uid) is not int or uid < 0:
            raise ValueError("Bot API update lacks numeric identity")
        body = json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(body.encode()) > 1_000_000:
            raise InboxFull("incoming update exceeds private limit")
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT digest,status FROM inbound WHERE update_id=?", (uid,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise ValueError("update identity changed")
                return old["status"]
            count = self.db.execute("SELECT count(*) FROM inbound WHERE status IN ('pending','held')").fetchone()[0]
            if count >= MAX_PENDING:
                raise InboxFull("private inbox reached 1000 pending/held updates")
            self.db.execute("""INSERT INTO inbound(update_id,digest,body,status,binding_id)
                VALUES (?,?,?,'pending',?)""", (uid, digest, body, frozen))
            self.db.execute("""INSERT INTO meta(key,value) VALUES ('offset',?)
                ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)""", (uid + 1,))
        return "pending"

    def pending(self, limit: int = 100) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM inbound WHERE status='pending' ORDER BY update_id LIMIT ?", (limit,))]

    def set_work(self, uid: int, binding: str, work_ref: str) -> None:
        if not binding or not work_ref:
            raise ValueError("binding and work reference required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT binding_id,work_ref FROM inbound WHERE update_id=? AND status='pending'",
                                  (uid,)).fetchone()
            if not row or row["binding_id"] != binding or row["work_ref"] not in ("", work_ref):
                raise ValueError("Presence work identity changed")
            self.db.execute("UPDATE inbound SET binding_id=?,work_ref=? WHERE update_id=?",
                            (binding, work_ref, uid))

    def freeze_transcript(self, uid: int, value: dict) -> dict:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT transcript FROM inbound WHERE update_id=? AND status='pending'",
                                  (uid,)).fetchone()
            if not row:
                raise ValueError("inbound event unavailable")
            if row["transcript"]:
                return json.loads(row["transcript"])
            self.db.execute("UPDATE inbound SET transcript=? WHERE update_id=?", (encoded, uid))
            return value

    def freeze_binding(self, uid: int, binding: str) -> None:
        """Bind the first Host attempt BEFORE POST, not after its response."""
        if not binding:
            raise ValueError("exact Presence binding required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT binding_id FROM inbound WHERE update_id=? AND status='pending'",
                                  (uid,)).fetchone()
            if not row or row["binding_id"] != binding:
                raise ValueError("Presence binding changed")
            self.db.execute("UPDATE inbound SET binding_id=? WHERE update_id=?", (binding, uid))

    def settle(self, uid: int, *, held: bool = False) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""UPDATE inbound SET status=?,body=CASE WHEN ? THEN body ELSE '' END
                WHERE update_id=? AND status='pending'""", ("held" if held else "done", int(held), uid))

    def hold_reply_too_long(self, uid: int, text: str) -> None:
        """Keep the source update and a bounded reason; never send a partial reply."""
        if not isinstance(text, str) or len(text.encode("utf-8")) <= 4096:
            raise ValueError("overlong reply required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT transcript FROM inbound WHERE update_id=? AND status='pending'",
                                  (uid,)).fetchone()
            if not row:
                raise ValueError("inbound event unavailable")
            retained = json.loads(row["transcript"]) if row["transcript"] else {}
            if not isinstance(retained, dict):
                raise ValueError("transcript receipt unavailable")
            retained.update({"hold_reason": "reply_too_long", "reply_bytes": len(text.encode("utf-8")),
                             "reply_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
            self.db.execute("UPDATE inbound SET status='held',transcript=? WHERE update_id=?",
                            (json.dumps(retained, sort_keys=True, ensure_ascii=False), uid))

    def hold_frozen_transcript(self, uid: int, reason: str) -> None:
        """Keep the original decision and record why a legacy turn cannot replay."""
        if reason != "legacy_parameterized_audio_unresolved":
            raise ValueError("unsupported held transcript reason")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT transcript FROM inbound WHERE update_id=? AND status='pending'",
                                  (uid,)).fetchone()
            transcript = json.loads(row[0]) if row and row[0] else None
            if not isinstance(transcript, dict) or transcript.get("status") != "audio_unsupported":
                raise ValueError("frozen transcription unavailable")
            transcript["hold_reason"] = reason
            self.db.execute("UPDATE inbound SET status='held',transcript=? WHERE update_id=? AND status='pending'",
                            (json.dumps(transcript, sort_keys=True), uid))

    def queue_reply(self, source: str, chat: int, topic: int | None,
                    reply_id: int, binding: str, text: str) -> None:
        if not source or not binding or not text.strip() or len(text.encode()) > 4096:
            raise ValueError("automatic reply invalid or too long")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            prior = self.db.execute("SELECT * FROM outbound WHERE source_id=?", (source,)).fetchone()
            if prior:
                if any(prior[key] != expected for key, expected in (
                        ("chat_id", chat), ("topic_id", topic), ("reply_id", reply_id),
                        ("binding_id", binding), ("text", text))):
                    raise ValueError("automatic reply identity changed")
                return
            self.db.execute("""INSERT INTO outbound
                (source_id,chat_id,topic_id,reply_id,binding_id,text,status)
                VALUES (?,?,?,?,?,?,'queued')""", (source, chat, topic, reply_id, binding, text))

    def claim_reply(self) -> dict | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM outbound WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE outbound SET status='attempted' WHERE source_id=?", (row["source_id"],))
            return dict(row)

    def sent(self, source: str, remote_id: int) -> None:
        if type(remote_id) is not int or remote_id <= 0:
            raise ValueError("Telegram did not return a concrete message id")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE outbound SET status='sent',remote_id=? WHERE source_id=? AND status='attempted'",
                            (remote_id, source))

    def unknown(self, source: str) -> None:
        with self.db:
            self.db.execute("UPDATE outbound SET status='unknown' WHERE source_id=? AND status='attempted'",
                            (source,))

    def pending_reports(self, limit: int = 10) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM outbound WHERE status='sent' AND reported=0 ORDER BY rowid LIMIT ?", (limit,))]

    def reported(self, source: str) -> None:
        with self.db:
            self.db.execute("UPDATE outbound SET reported=1 WHERE source_id=? AND status='sent'", (source,))

    def counts(self) -> dict:
        return {row["status"]: row["total"] for row in self.db.execute(
            "SELECT status,count(*) AS total FROM inbound GROUP BY status")}

    def outbound_counts(self) -> dict:
        return {row["status"]: row["total"] for row in self.db.execute(
            "SELECT status,count(*) AS total FROM outbound GROUP BY status")}
