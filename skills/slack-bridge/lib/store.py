from __future__ import annotations

import json
import pathlib
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .events import REACTION_EVENTS, ParsedEnvelope, provider_facts
from .slack_api import normalize_text_format


# An inbox row kept on a Host that cannot hold it as context-only; it is never submitted later.
REACTION_WITHHELD = "reaction_host_context_unavailable"
_REACTION_OPERATIONS = {"reaction_add": ("add", "new", "reactions.add"),
                        "reaction_remove": ("remove", "old", "reactions.remove")}


def _reaction_address(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The channel, timestamp and name SlackClient.reaction sends, for everything else that names them."""
    return {**payload, **{key: str(payload.get(key) or "").strip() for key in ("channel", "ts", "name")}}


def _reaction_lane(operation: str, payload: Mapping[str, Any]) -> str:
    """A reaction write's FIFO lane: its one message, never the channel or a thread found later.

    ``slack_api`` reactions.add/remove naming the same channel and timestamp share it, with
    their bytes and handling unchanged. A write naming no message gets ``""``: its usual lane.
    """
    if operation in _REACTION_OPERATIONS:
        address = _reaction_address(payload)
        channel, ts = address["channel"], address["ts"]
    elif operation == "generic_api" and payload.get("path") in {"reactions.add", "reactions.remove"}:
        arguments = payload.get("body") if payload.get("method") == "POST" else payload.get("params")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        channel, ts = (str(arguments.get(key) or "").strip() for key in ("channel", "timestamp"))
    else:
        return ""
    return f"reaction:{channel}:{ts}" if channel and ts else ""


def _lane_reactions_by_message(db: sqlite3.Connection) -> None:
    """Move live reaction rows an earlier version queued in their channel's lane to their message's.

    Id, lease and send marker stay, so writes to one message keep their order and an
    attempt that may have reached Slack is still never repeated. Only rows still in an
    older lane are read, and of ``slack_api`` rows only those naming a reactions method;
    one naming no message, or unreadable, keeps the lane it has.
    """
    rows = db.execute(
        """SELECT id,operation,payload_json FROM outbox WHERE state IN ('pending','leased')
           AND operation IN ('reaction_add','reaction_remove','generic_api') AND ordering_key NOT LIKE 'reaction:%'
           AND (operation<>'generic_api' OR payload_json LIKE '%"reactions.add"%'
                OR payload_json LIKE '%"reactions.remove"%')"""
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        lane = _reaction_lane(str(row["operation"]), payload) if isinstance(payload, dict) else ""
        if lane:
            db.execute("UPDATE outbox SET ordering_key=? WHERE id=?", (lane, row["id"]))


class InboxLeaseLost(RuntimeError):
    """The inbound attempt no longer owns its row; do not write with its token."""


@dataclass(frozen=True)
class InboxItem:
    row_id: int
    lease_token: str
    envelope_id: str
    event_id: str
    ordering_key: str
    team_id: str
    enterprise_id: str
    event_type: str
    subtype: str
    actor_user_id: str
    actor_team_id: str
    channel_id: str
    channel_type: str
    message_ts: str
    thread_ts: str
    event_ts: str
    client_msg_id: str
    text: str
    files: tuple[dict[str, Any], ...]
    staged_files: tuple[dict[str, Any], ...]
    host_reference: str
    attempts: int
    provider_context: dict[str, Any] | None = None
    structured: dict[str, Any] = field(default_factory=dict)
    # The ``conversation_queue`` snapshot taken before this event's first submission
    # attempt; retries reuse it, like the provider-context snapshot.
    transport_queue: dict[str, Any] | None = None
    # Pending refresh is independent of the immutable initial event observation.
    transport_queue_report: dict[str, Any] | None = None

    @property
    def reply_thread_ts(self) -> str:
        return self.thread_ts or self.message_ts

    @property
    def provider_event_key(self) -> str:
        """Stable idempotency key for a host adapter's submit call."""

        return self.event_id or self.envelope_id


@dataclass(frozen=True)
class OutboxItem:
    row_id: int
    lease_token: str
    request_id: str
    chunk_index: int
    chunk_count: int
    target: str
    thread_ts: str
    text: str
    ordering_key: str
    attempts: int
    text_format: str = "mrkdwn"
    origin: dict[str, Any] = field(default_factory=dict)
    delivery_reporting_version: int = 0
    resolved_channel: str = ""
    provider_account_id: str = ""
    kind: str = "text"
    operation: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    output_ref: str = ""

    def delivery_report(self, state: str, *, result: Mapping[str, Any] | None = None,
                        error: str = "") -> dict[str, Any] | None:
        if self.delivery_reporting_version != 1:
            return None
        result = result or {}
        message = {"provider_message_id": str(result.get("ts") or ""),
                   "requested_target": self.target, "target_resolved": bool(self.resolved_channel),
                   "chunk_count": self.chunk_count}
        if self.output_ref:
            message["output_ref"] = self.output_ref
        if error:
            message["error"] = error
        return {
            "schema_version": 1, "delivery_id": self.request_id, "part_id": str(self.chunk_index),
            "state": state, "provider": "slack", "account_id": self.provider_account_id,
            "conversation_id": str(result.get("channel") or self.resolved_channel or self.target),
            "thread_id": self.thread_ts, "text": self.text, "format": self.text_format,
            "message": message, "origin": self.origin,
        }


    def reaction_report(self, state: str, *, retained: Mapping[str, Any] | None = None, effect: str = "",
                        error: str = "") -> dict[str, Any] | None:
        """One reaction write's non-speech receipt: no words, and only the value this write named."""
        operation = _REACTION_OPERATIONS.get(self.operation)
        if operation is None or self.delivery_reporting_version != 1 or not self.provider_account_id:
            return None
        verb, field, method = operation
        reaction = {"schema_version": 1, "operation": verb,
                    field: [{"type": "name", "value": str(self.payload.get("name") or "")}],
                    "target": {"message_id": str(self.payload.get("ts") or ""),
                               "resolution": "resolved" if retained else "unresolved"},
                    **({"effect": effect} if effect else {})}
        message = {"kind": "operation", "method": method, "reaction": reaction, **({"error": error} if error else {})}
        return {
            "schema_version": 1, "delivery_id": self.request_id, "part_id": "0", "state": state,
            "provider": "slack", "account_id": self.provider_account_id,
            "conversation_id": str(self.payload.get("channel") or self.target),
            "thread_id": str(retained["thread_ts"]) if retained else "",
            "text": "", "format": "", "message": message, "origin": self.origin,
        }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _message_revision(row: Mapping[str, Any] | sqlite3.Row) -> str:
    """The provider revision shared by message and app_mention deliveries."""
    facts = json.loads(row["structured_json"])
    changed = row["subtype"] == "message_changed"
    message = facts.get("message", {}) if changed else facts
    edited = message.get("edited")
    previous_edited = facts.get("previous_message", {}).get("edited") if changed else None
    # An attachment/metadata update may retain an earlier text-edit stamp.
    # Its own event time keeps that real change separate from that old edit.
    if (isinstance(edited, Mapping) and str(edited.get("ts") or "").strip()
            and (not isinstance(previous_edited, Mapping) or previous_edited.get("ts") != edited["ts"])):
        return str(edited["ts"]).strip()
    return str(row["event_ts"] if changed else row["message_ts"])


def _queued_event(row: sqlite3.Row, text_chars: int | None) -> dict[str, Any]:
    """One received event without a durable Host reference, as transport facts."""
    text = str(row["text"])
    try:
        files = json.loads(row["files_json"] or "[]")
        structured = json.loads(row["structured_json"] or "{}")
    except json.JSONDecodeError:
        files, structured = [], {}
    event: dict[str, Any] = {
        "source_event_id": str(row["event_id"] or row["envelope_id"]),
        "event_type": str(row["event_type"]),
        "subtype": str(row["subtype"]),
        "actor": {"platform_actor_id": str(row["actor_user_id"]),
                  "actor_team_id": str(row["actor_team_id"] or row["team_id"])},
        "message_id": str(row["message_ts"]),
        "thread_id": str(row["thread_ts"]),
        "event_ts": str(row["event_ts"]),
        "received_at": _iso(row["created_at"]),
        "inbox_state": str(row["state"]),
        "text": text if text_chars is None else text[:max(0, text_chars)],
        "text_chars": len(text),
        "text_truncated": text_chars is not None and len(text) > max(0, text_chars),
        "provider_facts": provider_facts(structured) if isinstance(structured, dict) else {},
        # Declared names only: private URLs stay in the staging declaration.
        "files": [{"file_id": str(item.get("file_id") or ""), "file_name": str(item.get("name") or "")}
                  for item in files if isinstance(item, dict)],
    }
    if isinstance(structured, dict) and structured.get("change"):
        event["change"] = str(structured["change"])
    reaction = structured.get("reaction") if isinstance(structured, dict) else None
    if isinstance(reaction, dict):
        event["reaction"] = {"kind": str(reaction.get("kind") or ""), "name": str(reaction.get("name") or "")}
    return event


class BridgeStore:
    """Small durable queue shared by the extension child and companion.

    Every method opens its own SQLite connection, so short-lived plugin children
    and the long-lived companion can use the same database safely.
    """

    def __init__(self, state_dir: pathlib.Path | str):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "slack_bridge.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    envelope_id TEXT NOT NULL,
                    event_id TEXT NOT NULL DEFAULT '',
                    dedupe_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    ignored_reason TEXT NOT NULL DEFAULT '',
                    ordering_key TEXT NOT NULL DEFAULT '',
                    team_id TEXT NOT NULL DEFAULT '',
                    enterprise_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT '',
                    subtype TEXT NOT NULL DEFAULT '',
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    actor_team_id TEXT NOT NULL DEFAULT '',
                    channel_id TEXT NOT NULL DEFAULT '',
                    channel_type TEXT NOT NULL DEFAULT '',
                    message_ts TEXT NOT NULL DEFAULT '',
                    thread_ts TEXT NOT NULL DEFAULT '',
                    event_ts TEXT NOT NULL DEFAULT '',
                    client_msg_id TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    files_json TEXT NOT NULL DEFAULT '[]',
                    staged_files_json TEXT NOT NULL DEFAULT '[]',
                    raw_json TEXT NOT NULL,
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    host_reference TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inbox_work
                    ON inbox(state, available_at, id);
                CREATE INDEX IF NOT EXISTS inbox_thread
                    ON inbox(ordering_key, id, state);
                CREATE INDEX IF NOT EXISTS inbox_message
                    ON inbox(team_id, channel_id, message_ts, event_type);

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    thread_ts TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    ordering_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    provider_message_ts TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(request_id, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS outbox_work
                    ON outbox(state, available_at, id);
                CREATE INDEX IF NOT EXISTS outbox_ordering
                    ON outbox(ordering_key, id, state);

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS directory_cache (
                    cache_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    observed_at REAL NOT NULL
                );
                """
            )
            # One durable snapshot beside the existing inbox row. Serialize the
            # additive migration because plugin and companion may start together.
            db.execute("BEGIN IMMEDIATE")
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(inbox)")}
            if "provider_context_json" not in columns:
                db.execute("ALTER TABLE inbox ADD COLUMN provider_context_json TEXT NOT NULL DEFAULT ''")
            if "structured_json" not in columns:
                db.execute("ALTER TABLE inbox ADD COLUMN structured_json TEXT NOT NULL DEFAULT '{}'")
            if "transport_queue_json" not in columns:
                db.execute("ALTER TABLE inbox ADD COLUMN transport_queue_json TEXT NOT NULL DEFAULT ''")
            if "transport_queue_report_json" not in columns:
                db.execute("ALTER TABLE inbox ADD COLUMN transport_queue_report_json TEXT NOT NULL DEFAULT ''")
            outbox_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(outbox)")}
            if "text_format" not in outbox_columns:
                # Pending rows were authored for Slack's native mrkdwn. Their
                # original interpretation survives deployment and retry.
                db.execute("ALTER TABLE outbox ADD COLUMN text_format TEXT NOT NULL DEFAULT 'mrkdwn'")
            for name, declaration in (
                ("origin_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("delivery_reporting_version", "INTEGER NOT NULL DEFAULT 0"),
                ("resolved_channel", "TEXT NOT NULL DEFAULT ''"),
                ("provider_account_id", "TEXT NOT NULL DEFAULT ''"),
                ("report_payload_json", "TEXT NOT NULL DEFAULT ''"),
                ("report_state", "TEXT NOT NULL DEFAULT ''"),
                ("report_lease_token", "TEXT NOT NULL DEFAULT ''"),
                ("report_lease_until", "REAL NOT NULL DEFAULT 0"),
                ("report_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("report_available_at", "REAL NOT NULL DEFAULT 0"),
                ("report_error", "TEXT NOT NULL DEFAULT ''"),
                ("kind", "TEXT NOT NULL DEFAULT 'text'"),
                ("operation", "TEXT NOT NULL DEFAULT ''"),
                ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("result_json", "TEXT NOT NULL DEFAULT ''"),
                ("output_ref", "TEXT NOT NULL DEFAULT ''"),
                ("send_started", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in outbox_columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {declaration}")
            if "send_started" not in outbox_columns:
                # A lease from an older bridge may already have crossed the
                # physical send boundary. Its expiry cannot authorize a resend.
                db.execute("UPDATE outbox SET send_started=1 WHERE state='leased'")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_report_work ON outbox(report_state,report_available_at,id)")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_message ON outbox(resolved_channel,provider_message_ts)")
            _lane_reactions_by_message(db)
            db.commit()

    def ingest_envelope(
        self,
        raw_payload: Mapping[str, Any],
        parsed: ParsedEnvelope,
    ) -> tuple[int, bool]:
        """Commit an envelope and its dedupe identity before Socket ACK."""

        now = time.time()
        event = parsed.event
        envelope_id = parsed.envelope_id or f"missing:{uuid.uuid4().hex}"
        dedupe_key = (
            f"event:{parsed.event_id}" if parsed.event_id else f"envelope:{envelope_id}"
        )
        values: dict[str, Any] = {
            "envelope_id": envelope_id,
            "event_id": parsed.event_id,
            "dedupe_key": dedupe_key,
            "state": "pending" if parsed.accepted and event is not None else "ignored",
            "ignored_reason": "" if parsed.accepted else parsed.reason,
            "ordering_key": event.ordering_key if event else "",
            "team_id": event.team_id if event else "",
            "enterprise_id": event.enterprise_id if event else "",
            "event_type": event.event_type if event else "",
            "subtype": event.subtype if event else "",
            "actor_user_id": event.actor_user_id if event else "",
            "actor_team_id": event.actor_team_id if event else "",
            "channel_id": event.channel_id if event else "",
            "channel_type": event.channel_type if event else "",
            "message_ts": event.message_ts if event else "",
            "thread_ts": event.thread_ts if event else "",
            "event_ts": event.event_ts if event else "",
            "client_msg_id": event.client_msg_id if event else "",
            "text": event.text if event else "",
            "files_json": json.dumps(
                [item.as_dict() for item in (event.files if event else ())],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "raw_json": json.dumps(
                raw_payload, ensure_ascii=False, separators=(",", ":")
            ),
            "structured_json": json.dumps(
                event.structured if event else {}, ensure_ascii=False, separators=(",", ":")
            ),
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (parsed.accepted and event is not None and event.team_id
                    and event.event_type in {"message", "app_mention"}
                    and event.subtype != "message_deleted"):
                # Both subscriptions can describe one revision. Preserve both
                # envelopes, admitting one turn only when identity and text agree.
                other_type = "message" if event.event_type == "app_mention" else "app_mention"
                candidates = db.execute(
                    """SELECT subtype, message_ts, event_ts, structured_json FROM inbox
                       WHERE team_id=? AND channel_id=? AND message_ts=?
                         AND event_type=? AND text=? AND state!='ignored'
                         AND subtype!='message_deleted'""",
                    (event.team_id, event.channel_id, event.message_ts, other_type, event.text),
                )
                revision = _message_revision(values)
                if revision and any(_message_revision(row) == revision for row in candidates):
                    values["state"] = "ignored"
                    values["ignored_reason"] = "duplicate_message_surface"
            cursor = db.execute(
                f"INSERT OR IGNORE INTO inbox ({columns}) VALUES ({placeholders})",
                values,
            )
            inserted = cursor.rowcount == 1
            if inserted:
                row_id = int(cursor.lastrowid)
            else:
                row = db.execute(
                    "SELECT id FROM inbox WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Slack envelope dedupe lookup failed")
                row_id = int(row["id"])
            db.commit()
        return row_id, inserted

    @staticmethod
    def _claimable_sql(table: str, scope: str = "", order: str = "q.id") -> str:
        # A deferred, continuing, refused-with-work or retained turn only polls.
        # Once its durable reference exists, later conversation events may be
        # admitted, including while this row's independent polls are in flight.
        deferred = (
            "AND NOT (earlier.host_reference LIKE 'deferred:%'"
            " OR earlier.host_reference LIKE 'continuing:%'"
            " OR earlier.host_reference LIKE 'refused:%'"
            " OR earlier.host_reference LIKE 'retained:%')"
            if table == "inbox"
            else ""
        )
        return f"""
            SELECT q.id
            FROM {table} AS q
            WHERE q.available_at <= :now
              AND (q.state = 'pending' OR (q.state = 'leased' AND q.lease_until <= :now))
              {scope}
              AND NOT EXISTS (
                  SELECT 1 FROM {table} AS earlier
                  WHERE earlier.ordering_key = q.ordering_key
                    AND earlier.id < q.id
                    AND earlier.state IN ('pending', 'leased')
                    {deferred}
              )
            ORDER BY {order}
            LIMIT 1
        """

    def claim_inbox(self, *, lease_seconds: float = 60.0, submitted: bool | None = None) -> InboxItem | None:
        """Lease a claimable row, keeping each conversation's FIFO.

        ``submitted=False`` claims only rows without a stored Host reference (new
        input), oldest first; ``True`` only rows that have one (observation of
        admitted work), earliest due first, so a reference released after its poll
        never overtakes one already waiting. ``None`` claims either, the
        single-worker behavior: new input by capture time, admitted work by due time.
        """
        now = time.time()
        token = uuid.uuid4().hex
        scope, order = {
            None: ("", "CASE WHEN q.host_reference = '' THEN q.created_at ELSE q.available_at END, q.id"),
            False: ("AND q.host_reference = ''", "q.id"),
            True: ("AND q.host_reference <> ''", "q.available_at, q.id"),
        }[submitted]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(self._claimable_sql("inbox", scope, order), {"now": now}).fetchone()
            if row is None:
                db.commit()
                return None
            row_id = int(row["id"])
            updated = db.execute(
                """
                UPDATE inbox
                SET state='leased', lease_token=?, lease_until=?, attempts=attempts+1,
                    updated_at=?
                WHERE id=? AND (state='pending' OR (state='leased' AND lease_until<=?))
                """,
                (token, now + lease_seconds, now, row_id, now),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            claimed = db.execute("SELECT * FROM inbox WHERE id=?", (row_id,)).fetchone()
            db.commit()
        return self._inbox_item(claimed, token)

    def _inbox_item(self, row: sqlite3.Row, token: str) -> InboxItem:
        files = json.loads(row["files_json"] or "[]")
        staged = json.loads(row["staged_files_json"] or "[]")
        return InboxItem(
            row_id=int(row["id"]),
            lease_token=token,
            envelope_id=str(row["envelope_id"]),
            event_id=str(row["event_id"]),
            ordering_key=str(row["ordering_key"]),
            team_id=str(row["team_id"]),
            enterprise_id=str(row["enterprise_id"]),
            event_type=str(row["event_type"]),
            subtype=str(row["subtype"]),
            actor_user_id=str(row["actor_user_id"]),
            actor_team_id=str(row["actor_team_id"]),
            channel_id=str(row["channel_id"]),
            channel_type=str(row["channel_type"]),
            message_ts=str(row["message_ts"]),
            thread_ts=str(row["thread_ts"]),
            event_ts=str(row["event_ts"]),
            client_msg_id=str(row["client_msg_id"]),
            text=str(row["text"]),
            files=tuple(dict(item) for item in files if isinstance(item, dict)),
            staged_files=tuple(dict(item) for item in staged if isinstance(item, dict)),
            host_reference=str(row["host_reference"]),
            attempts=int(row["attempts"]),
            provider_context=json.loads(row["provider_context_json"]) if row["provider_context_json"] else None,
            structured=json.loads(row["structured_json"] or "{}") if row["structured_json"] else {},
            transport_queue=json.loads(row["transport_queue_json"]) if row["transport_queue_json"] else None,
            transport_queue_report=json.loads(row["transport_queue_report_json"]) if row["transport_queue_report_json"] else None,
        )

    def set_provider_context(self, row_id: int, lease_token: str, value: Mapping[str, Any]) -> None:
        self._leased_update(
            "inbox", row_id, lease_token, "provider_context_json=?",
            (json.dumps(value, ensure_ascii=False, separators=(",", ":")),),
        )

    def set_transport_queue(self, row_id: int, lease_token: str, value: Mapping[str, Any]) -> None:
        self._leased_update(
            "inbox", row_id, lease_token, "transport_queue_json=?",
            (json.dumps(value, ensure_ascii=False, separators=(",", ":")),),
        )

    def set_transport_queue_report(self, row_id: int, lease_token: str,
                                   value: Mapping[str, Any] | None) -> None:
        self._leased_update(
            "inbox", row_id, lease_token, "transport_queue_report_json=?",
            (json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else "",),
        )

    def retained_message(self, team_id: str, channel_id: str, message_ts: str) -> dict[str, Any] | None:
        """One exact message from retained records, by indexed team, channel and timestamp.

        An own receipt names its author exactly; only a posted text chunk's creation receipt
        counts, never another mutation that merely returned a ``ts``. Nothing is inferred from
        time or text, and the same timestamp in another channel or workspace does not match.
        """
        if not team_id or not channel_id or not message_ts:
            return None
        with self._connect() as db:
            own = db.execute(
                """SELECT thread_ts,text FROM outbox WHERE resolved_channel=? AND provider_message_ts=?
                   AND provider_account_id=? AND kind='text' AND state='delivered' ORDER BY id DESC LIMIT 1""",
                (channel_id, message_ts, team_id),
            ).fetchone()
            seen = None if own else db.execute(
                """SELECT actor_user_id,thread_ts,text FROM inbox WHERE team_id=? AND channel_id=? AND message_ts=?
                   AND event_type IN ('message','app_mention') AND subtype!='message_deleted'
                   ORDER BY id DESC LIMIT 1""",
                (team_id, channel_id, message_ts),
            ).fetchone()
        row = own or seen
        if row is None:
            return None
        message = {"ts": message_ts, "text": str(row["text"]),
                   **({"user": str(seen["actor_user_id"])} if seen else {})}
        return {"source": "slack-bridge outbox" if own else "slack-bridge inbox", "message": message,
                "thread_ts": str(row["thread_ts"] or message_ts), **({"is_self": True} if own else {})}

    def withhold_inbox(self, row_id: int, lease_token: str, reason: str) -> None:
        """Settle a retained observation without submitting it, now or after an update."""
        with self._connect() as db:
            updated = db.execute(
                """UPDATE inbox SET state='ignored',ignored_reason=?,lease_token='',lease_until=0,updated_at=?
                   WHERE id=? AND state='leased' AND lease_token=?""",
                (reason, time.time(), row_id, lease_token),
            )
            if updated.rowcount != 1:
                raise InboxLeaseLost("Slack inbox lease no longer belongs to this worker")

    def conversation_queue(self, item: InboxItem, *, limit: int | None = None,
                           text_chars: int | None = None, reactions: bool = True) -> dict[str, Any]:
        """This conversation's later events without an acknowledged Host reference.

        One read transaction over the existing inbox: rows of the same ordering key
        received after ``item`` that are pending or leased without a Host reference.
        Rows Host already holds (deferred/continuing references) and ignored rows are
        not queued events, nor reactions on a Host that cannot hold them (``reactions=False``). The transport sends all rows and full text, so Host can
        retain the exact observation for its scoped source reader. Optional limits
        are diagnostic views only: every cut makes completeness false. Each event
        still reaches Host later as its own submission.
        """
        where = ("FROM inbox WHERE ordering_key=? AND id>? AND state IN ('pending','leased') "
                 "AND host_reference=''")
        if not reactions:
            types = ",".join(f"'{kind}'" for kind in sorted(REACTION_EVENTS))
            where += f" AND event_type NOT IN ({types})"
        arguments = (item.ordering_key, item.row_id)
        with self._connect() as db:
            db.execute("BEGIN")
            total = int(db.execute(f"SELECT COUNT(*) {where}", arguments).fetchone()[0])
            rows = db.execute(f"SELECT * {where} ORDER BY id LIMIT ?",
                              (*arguments, -1 if limit is None else max(0, int(limit)))).fetchall()
            observed_at = _iso(time.time())
            db.commit()
        events = [_queued_event(row, text_chars) for row in rows]
        return {
            "schema_version": 1,
            "source": "slack-bridge inbox",
            "observed_at": observed_at,
            "conversation_key": f"slack:{item.ordering_key}",
            "after_source_event_id": item.provider_event_key,
            "complete": len(events) == total and not any(event["text_truncated"] for event in events),
            "pending_count": total,
            "omitted_count": total - len(events),
            "text_limit_chars": None if text_chars is None else max(0, int(text_chars)),
            "events": events,
            "note": ("Events of this Slack conversation the bridge durably received after this event "
                     "without a stored Host reference at observed_at. A leased event may already "
                     "have a submission in flight; this is not evidence of Host admission or absence. "
                     "Each retains its own event identity; correspondents' words are observations, "
                     "not owner directives."),
        }

    def workspace_name(self) -> str:
        with self._connect() as db:
            row = db.execute("SELECT value_json FROM runtime_state WHERE key='workspace_name'").fetchone()
        return str(json.loads(row["value_json"]) or "") if row else ""

    def set_staged_files(
        self,
        row_id: int,
        lease_token: str,
        files: Sequence[Mapping[str, Any]],
    ) -> None:
        self._leased_update(
            "inbox",
            row_id,
            lease_token,
            "staged_files_json=?",
            (json.dumps(list(files), ensure_ascii=False, separators=(",", ":")),),
        )

    def set_host_reference(self, row_id: int, lease_token: str, reference: str) -> None:
        self._leased_update(
            "inbox", row_id, lease_token, "host_reference=?", (str(reference),)
        )

    def renew_inbox_lease(self, row_id: int, lease_token: str, *, lease_seconds: float) -> None:
        """Budget the next finite phase only if this attempt still owns the row."""
        self._leased_update(
            "inbox", row_id, lease_token, "lease_until=?", (time.time() + lease_seconds,)
        )

    def complete_inbox(self, row_id: int, lease_token: str) -> None:
        self._terminal_update("inbox", row_id, lease_token, "delivered", "")

    def fail_inbox(self, row_id: int, lease_token: str, error: str) -> None:
        self._terminal_update("inbox", row_id, lease_token, "failed", error)

    def retry_inbox(
        self,
        row_id: int,
        lease_token: str,
        error: str,
        *,
        delay_seconds: float,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE inbox
                SET state='pending', lease_token='', lease_until=0, available_at=?,
                    last_error=?, updated_at=?
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (
                    now + max(0.0, delay_seconds),
                    str(error)[:1000],
                    now,
                    row_id,
                    lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise InboxLeaseLost("Slack inbox lease no longer belongs to this worker")

    def enqueue_outbox(
        self,
        *,
        request_id: str,
        target: str,
        thread_ts: str,
        chunks: Sequence[str],
        text_format: str = "markdown",
        origin: Mapping[str, Any] | None = None,
        delivery_reporting_version: int = 0,
        output_ref: str = "",
    ) -> int:
        """Queue one logical message once per ``request_id``, whatever its text.

        A repeated request id returns the existing row count, so neither a retried
        poll nor a provider failure recorded for that id ever queues it again.
        """
        text_format = normalize_text_format(text_format)
        request_id = str(request_id or uuid.uuid4().hex)
        target = str(target or "").strip()
        if not target:
            raise ValueError("Slack target is required")
        clean_chunks = [str(chunk) for chunk in chunks if str(chunk)]
        if not clean_chunks:
            raise ValueError("Slack message text is required")
        now = time.time()
        ordering_key = f"{target}:{str(thread_ts or '')}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = int(
                db.execute(
                    "SELECT COUNT(*) AS count FROM outbox WHERE request_id=?",
                    (request_id,),
                ).fetchone()["count"]
            )
            if existing:
                db.commit()
                return existing
            for index, chunk in enumerate(clean_chunks):
                db.execute(
                    """
                    INSERT OR IGNORE INTO outbox (
                        request_id, chunk_index, chunk_count, target, thread_ts,
                        text, ordering_key, created_at, updated_at, text_format,
                        origin_json, delivery_reporting_version, output_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        index,
                        len(clean_chunks),
                        target,
                        str(thread_ts or ""),
                        chunk,
                        ordering_key,
                        now,
                        now,
                        text_format,
                        json.dumps(dict(origin or {}), ensure_ascii=False),
                        1 if delivery_reporting_version == 1 else 0,
                        str(output_ref or ""),
                    ),
                )
            db.commit()
        return len(clean_chunks)

    def enqueue_mutation(self, *, request_id: str, operation: str, payload: Mapping[str, Any],
                         origin: Mapping[str, Any] | None = None,
                         delivery_reporting_version: int = 0) -> bool:
        request_id, operation = str(request_id or uuid.uuid4().hex), str(operation or "").strip()
        if not operation:
            raise ValueError("Slack mutation operation is required")
        now = time.time()
        if operation in _REACTION_OPERATIONS:
            payload = _reaction_address(payload)
        arguments = payload.get("body") if payload.get("method") == "POST" else payload.get("params")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        target = str(payload.get("channel") or payload.get("channel_id")
                     or arguments.get("channel") or arguments.get("channel_id") or "mutation")
        thread_ts = str(payload.get("thread_ts") or arguments.get("thread_ts") or "")
        # A reaction held for its thread's place waits only with writes to that message.
        lane = _reaction_lane(operation, payload) or f"{target}:{thread_ts}"
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO outbox
                   (request_id,chunk_index,chunk_count,target,thread_ts,text,ordering_key,kind,operation,payload_json,
                    origin_json,delivery_reporting_version,created_at,updated_at)
                   VALUES(?,0,1,?,?, '', ?, 'mutation', ?, ?, ?, ?, ?, ?)""",
                (request_id, target, thread_ts, lane, operation,
                 json.dumps(dict(payload), ensure_ascii=False),
                 json.dumps(dict(origin or {}), ensure_ascii=False),
                 1 if delivery_reporting_version == 1 else 0, now, now),
            )
            inserted = db.total_changes > 0
        return inserted

    def claim_outbox(self, *, lease_seconds: float = 60.0) -> OutboxItem | None:
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # The process may have died after Slack accepted a request but before
            # the result was stored. An expired attempt is uncertainty, never
            # evidence of no effect. Settle it before another row is claimed.
            expired = db.execute(
                "SELECT * FROM outbox WHERE state='leased' AND send_started=1 AND lease_until<=?",
                (now,),
            ).fetchall()
            for lost in expired:
                report = str(lost["report_payload_json"] or "")
                if not report and lost["kind"] == "text":
                    # Pre-marker leased rows did not prepare a report. Rebuild
                    # it from their immutable delivery identity during migration.
                    payload = self._outbox_item(lost).delivery_report("uncertain", error="send_result_unrecorded")
                    report = json.dumps(payload, ensure_ascii=False) if payload else ""
                db.execute(
                    """UPDATE outbox SET state='uncertain',lease_token='',lease_until=0,
                           send_started=0,last_error='send_result_unrecorded',updated_at=?,
                           result_json='{"uncertain":true}',report_payload_json=?,report_state=?
                       WHERE id=? AND state='leased' AND send_started=1 AND lease_until<=?""",
                    (now, report, "pending" if report else "", lost["id"], now),
                )
            # Opening the store moved earlier rows; this also moves one an older process queued since.
            _lane_reactions_by_message(db)
            row = db.execute(self._claimable_sql("outbox"), {"now": now}).fetchone()
            if row is None:
                db.commit()
                return None
            row_id = int(row["id"])
            updated = db.execute(
                """
                UPDATE outbox
                SET state='leased', lease_token=?, lease_until=?, attempts=attempts+1,
                    updated_at=?
                WHERE id=? AND (state='pending' OR (state='leased' AND lease_until<=?))
                """,
                (token, now + lease_seconds, now, row_id, now),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            claimed = db.execute(
                "SELECT * FROM outbox WHERE id=?", (row_id,)
            ).fetchone()
            db.commit()
        return self._outbox_item(claimed)

    @staticmethod
    def _outbox_item(claimed: sqlite3.Row) -> OutboxItem:
        operation, target = str(claimed["operation"] or ""), str(claimed["target"])
        payload = json.loads(claimed["payload_json"] or "{}")
        if operation in _REACTION_OPERATIONS:
            # A row queued before enqueue did this keeps its bytes; only its lane moved to its message.
            payload = _reaction_address(payload)
            target = payload["channel"] or "mutation"
        return OutboxItem(
            row_id=int(claimed["id"]),
            lease_token=str(claimed["lease_token"]),
            request_id=str(claimed["request_id"]),
            chunk_index=int(claimed["chunk_index"]),
            chunk_count=int(claimed["chunk_count"]),
            target=target,
            thread_ts=str(claimed["thread_ts"]),
            text=str(claimed["text"]),
            ordering_key=str(claimed["ordering_key"]),
            attempts=int(claimed["attempts"]),
            text_format=str(claimed["text_format"]),
            origin=json.loads(claimed["origin_json"]),
            delivery_reporting_version=int(claimed["delivery_reporting_version"]),
            resolved_channel=str(claimed["resolved_channel"]),
            provider_account_id=str(claimed["provider_account_id"]),
            kind=str(claimed["kind"] or "text"),
            operation=operation,
            payload=payload,
            output_ref=str(claimed["output_ref"] or ""),
        )

    def begin_send(self, item: OutboxItem, *, uncertain_report: Mapping[str, Any] | None = None) -> None:
        """Durably mark one provider attempt before dispatch, under its live lease.

        The prepared uncertainty report is withheld while the request is live;
        crash recovery can publish it with the original identity and format.
        A mutation that reports supplies its own.
        """
        now = time.time()
        report = item.delivery_report("uncertain", error="send_result_unrecorded") if item.kind == "text" else uncertain_report
        with self._connect() as db:
            changed = db.execute(
                """UPDATE outbox SET send_started=1,provider_account_id=?,updated_at=?,
                       report_payload_json=?,report_state=''
                   WHERE id=? AND state='leased' AND lease_token=? AND lease_until>? AND send_started=0""",
                (item.provider_account_id, now, json.dumps(report, ensure_ascii=False) if report else "",
                 item.row_id, item.lease_token, now),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Slack send requires an unexpired, unstarted outbox lease")

    def set_resolved_target(self, item: OutboxItem, channel: str, account: str) -> None:
        self._leased_update("outbox", item.row_id, item.lease_token,
                            "resolved_channel=?,provider_account_id=?", (channel, account))

    def complete_outbox(
        self,
        row_id: int,
        lease_token: str,
        *,
        provider_message_ts: str = "",
        report_payload: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE outbox
                SET state='delivered', lease_token='', lease_until=0, send_started=0, last_error='',
                    provider_message_ts=?, updated_at=?, report_payload_json=?,report_state=?,result_json=?
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (str(provider_message_ts), now,
                 json.dumps(report_payload, ensure_ascii=False) if report_payload else "",
                 "pending" if report_payload else "", json.dumps(dict(result or {}), ensure_ascii=False), row_id, lease_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Slack outbox lease no longer belongs to this worker"
                )

    def fail_outbox(self, row_id: int, lease_token: str, error: str, *,
                    state: str = "failed", report_payload: Mapping[str, Any] | None = None,
                    result: Mapping[str, Any] | None = None) -> None:
        with self._connect() as db:
            changed = db.execute(
                """UPDATE outbox SET state=?,lease_token='',lease_until=0,send_started=0,last_error=?,updated_at=?,
                       report_payload_json=?,report_state=?,result_json=? WHERE id=? AND state='leased' AND lease_token=?""",
                (state, str(error)[:1000], time.time(),
                 json.dumps(report_payload, ensure_ascii=False) if report_payload else "",
                 "pending" if report_payload else "", json.dumps(dict(result or {}), ensure_ascii=False), row_id, lease_token),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Slack outbox lease no longer belongs to this worker")

    def claim_report(self) -> dict[str, Any] | None:
        now, token = time.time(), uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id,report_payload_json,report_attempts FROM outbox
                   WHERE report_payload_json<>'' AND report_available_at<=?
                   AND (report_state='pending' OR (report_state='reporting' AND report_lease_until<=?))
                   ORDER BY id LIMIT 1""", (now, now),
            ).fetchone()
            if row is None:
                return None
            db.execute("""UPDATE outbox SET report_state='reporting',report_lease_token=?,
                          report_lease_until=?,report_attempts=report_attempts+1 WHERE id=?""",
                       (token, now + 30, row["id"]))
        return {"row_id": int(row["id"]), "lease_token": token,
                "attempts": int(row["report_attempts"]) + 1,
                "payload": json.loads(row["report_payload_json"])}

    def finish_report(self, report: Mapping[str, Any], *, error: str = "") -> None:
        delay = min(60, 2 ** min(int(report["attempts"]), 5)) if error else 0
        with self._connect() as db:
            db.execute("""UPDATE outbox SET report_state=?,report_lease_token='',report_lease_until=0,
                          report_available_at=?,report_error=? WHERE id=? AND report_lease_token=?""",
                       ("pending" if error else "acked", time.time() + delay, error[:1000],
                        report["row_id"], report["lease_token"]))

    def runtime_value(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value_json FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def directory_snapshot(self, cache_key: str) -> dict[str, Any] | None:
        """Read directory observations without exposing them on the status route."""
        with self._connect() as db:
            row = db.execute("SELECT value_json FROM directory_cache WHERE cache_key=?", (cache_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_directory_snapshot(self, cache_key: str, value: Mapping[str, Any]) -> None:
        """Cache one credential-scoped scan; an older concurrent scan cannot replace a newer one."""
        with self._connect() as db:
            db.execute("""INSERT INTO directory_cache(cache_key,value_json,observed_at) VALUES(?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET value_json=excluded.value_json,
                    observed_at=excluded.observed_at
                WHERE excluded.observed_at >= directory_cache.observed_at""",
                       (cache_key, json.dumps(dict(value), ensure_ascii=False), float(value["observed_at"])))

    def delivery_receipt(self, request_id: str) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT kind,operation,chunk_index,state,attempts,target,resolved_channel,
                          provider_message_ts,result_json,last_error,report_state,report_error
                   FROM outbox WHERE request_id=? ORDER BY chunk_index""",
                (str(request_id),),
            ).fetchall()
        if not rows:
            return {"state": "not_found", "request_id": str(request_id)}
        parts = [{"part_id": str(row["chunk_index"]), "state": "held" if str(row["last_error"]).startswith("Held:") and row["state"] == "pending" else str(row["state"]),
                  "attempts": int(row["attempts"]),
                  "target": str(row["target"]).strip() if row["operation"] in _REACTION_OPERATIONS else str(row["target"]),
                  "resolved_channel": str(row["resolved_channel"]),
                  "provider_message_ts": str(row["provider_message_ts"]),
                  "provider_result": json.loads(row["result_json"] or "{}"),
                  "error": str(row["last_error"]), "history_report_state": str(row["report_state"]),
                  "history_report_error": str(row["report_error"])} for row in rows]
        return {"request_id": str(request_id), "kind": str(rows[0]["kind"]),
                "operation": str(rows[0]["operation"]), "parts": parts}

    def retry_outbox(
        self,
        row_id: int,
        lease_token: str,
        error: str,
        *,
        delay_seconds: float,
    ) -> None:
        """Retry only after the caller establishes no effect from this attempt."""
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE outbox
                SET state='pending', lease_token='', lease_until=0, available_at=?,
                    last_error=?, updated_at=?,send_started=0,report_payload_json='',report_state=''
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (
                    now + max(0.0, delay_seconds),
                    str(error)[:1000],
                    now,
                    row_id,
                    lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Slack outbox lease no longer belongs to this worker"
                )

    def hold_outbox(self, item: OutboxItem, reason: str) -> None:
        """Keep custody and FIFO order without spending a provider attempt."""
        self._leased_update("outbox", item.row_id, item.lease_token,
                            "state='pending',lease_token='',lease_until=0,send_started=0,available_at=?,"
                            "attempts=MAX(0,attempts-1),last_error=?",
                            (time.time() + 5, reason))

    def _leased_update(
        self,
        table: str,
        row_id: int,
        lease_token: str,
        assignment: str,
        values: Iterable[Any],
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                f"UPDATE {table} SET {assignment}, updated_at=? "
                "WHERE id=? AND state='leased' AND lease_token=?",
                (*values, now, row_id, lease_token),
            )
            if updated.rowcount != 1:
                error_type = InboxLeaseLost if table == "inbox" else RuntimeError
                raise error_type(
                    f"Slack {table} lease no longer belongs to this worker"
                )

    def _terminal_update(
        self,
        table: str,
        row_id: int,
        lease_token: str,
        state: str,
        error: str,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                f"""
                UPDATE {table}
                SET state=?, lease_token='', lease_until=0, last_error=?, updated_at=?
                WHERE id=? AND state='leased' AND lease_token=?
                """,
                (state, str(error)[:1000], now, row_id, lease_token),
            )
            if updated.rowcount != 1:
                error_type = InboxLeaseLost if table == "inbox" else RuntimeError
                raise error_type(
                    f"Slack {table} lease no longer belongs to this worker"
                )

    def set_runtime(self, **values: Any) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                db.execute(
                    """
                    INSERT INTO runtime_state(key, value_json, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (str(key), json.dumps(value, ensure_ascii=False), now),
                )
            db.commit()

    def status(self) -> dict[str, Any]:
        with self._connect() as db:
            runtime_rows = db.execute(
                "SELECT key, value_json, updated_at FROM runtime_state"
            ).fetchall()
            inbox = {
                str(row["state"]): int(row["count"])
                for row in db.execute(
                    "SELECT state, COUNT(*) AS count FROM inbox GROUP BY state"
                ).fetchall()
            }
            outbox = {
                str(row["state"]): int(row["count"])
                for row in db.execute(
                    "SELECT state, COUNT(*) AS count FROM outbox GROUP BY state"
                ).fetchall()
            }
            held = db.execute("SELECT COUNT(*) FROM outbox WHERE state='pending' AND last_error LIKE 'Held:%'").fetchone()[0]
            mutations = {
                str(row["state"]): int(row["count"])
                for row in db.execute("SELECT state,COUNT(*) AS count FROM outbox WHERE kind='mutation' GROUP BY state").fetchall()
            }
            reports = {str(row["report_state"]): int(row["count"]) for row in db.execute(
                "SELECT report_state,COUNT(*) AS count FROM outbox WHERE report_state<>'' GROUP BY report_state"
            )}
            report_error = db.execute(
                "SELECT report_error FROM outbox WHERE report_error<>'' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # Retained events still observing their original attempt or child (counted in pending too).
            retained = db.execute(
                "SELECT COUNT(*) FROM inbox WHERE host_reference LIKE 'retained:%' AND state IN ('pending','leased')"
            ).fetchone()[0]
            withheld = db.execute(
                "SELECT COUNT(*) FROM inbox WHERE state='ignored' AND ignored_reason=?", (REACTION_WITHHELD,)
            ).fetchone()[0]
            last_error = db.execute(
                """
                SELECT last_error FROM (
                    SELECT last_error, updated_at FROM inbox WHERE last_error <> ''
                    UNION ALL
                    SELECT last_error, updated_at FROM outbox WHERE last_error <> ''
                ) ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        runtime: dict[str, Any] = {}
        runtime_updated_at = 0.0
        for row in runtime_rows:
            try:
                runtime[str(row["key"])] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                runtime[str(row["key"])] = None
            runtime_updated_at = max(runtime_updated_at, float(row["updated_at"]))
        return {
            "outbox_held": held,
            "socket_state": "stopped",
            "host_adapter_state": "missing_binding_id",
            "reaction_context_state": "unknown",
            "workspace_name": "",
            "workspace_id": "",
            "bot_user_id": "",
            **runtime,
            "runtime_updated_at": runtime_updated_at,
            "inbox_pending": inbox.get("pending", 0),
            "inbox_retained": int(retained),
            "inbox_leased": inbox.get("leased", 0),
            "inbox_delivered": inbox.get("delivered", 0),
            "inbox_failed": inbox.get("failed", 0),
            "inbox_ignored": inbox.get("ignored", 0),
            "reactions_withheld": int(withheld),
            "outbox_pending": outbox.get("pending", 0),
            "outbox_leased": outbox.get("leased", 0),
            "outbox_delivered": outbox.get("delivered", 0),
            "outbox_failed": outbox.get("failed", 0),
            "outbox_uncertain": outbox.get("uncertain", 0),
            "mutations_pending": mutations.get("pending", 0),
            "mutations_delivered": mutations.get("delivered", 0),
            "mutations_failed": mutations.get("failed", 0),
            "mutations_uncertain": mutations.get("uncertain", 0),
            "delivery_reports_pending": reports.get("pending", 0) + reports.get("reporting", 0),
            "delivery_reports_acked": reports.get("acked", 0),
            "last_report_error": str(report_error[0]) if report_error else "",
            "last_delivery_error": str(last_error["last_error"]) if last_error else "",
        }
