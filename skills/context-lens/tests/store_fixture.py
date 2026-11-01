"""A usage-store fixture for the tests: core schema 1, written into a temp dir.

The DDL below mirrors ``ouroboros/usage_store.py`` ``_SCHEMA`` (schema version 1)
column for column, so the reader under test meets the real table shapes, the
real ``attempts_category_time`` index and the real ``meta`` encoding (JSON
values). Nothing here imports Ouroboros, and nothing here touches a real
install: every store is created under a fresh temporary directory.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from typing import Any, Dict, Optional

APPLICATION_ID_ENFORCED = 0x4F555345
APPLICATION_ID_NAME = 0x4F55534E

SCHEMA = """
CREATE TABLE attempts (
  attempt_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, seq INTEGER NOT NULL, seq_first INTEGER NOT NULL,
  kind TEXT, state TEXT, task_id TEXT, root_task_id TEXT, parent_task_id TEXT, billing_group_id TEXT,
  model TEXT, provider TEXT, category TEXT, source TEXT,
  ts_reserved TEXT, ts_dispatched TEXT, ts_final TEXT, ts_last TEXT, ts_last_epoch REAL,
  cost_usd TEXT, reservation_upper_bound_usd TEXT, cost_final INTEGER, pricing_known INTEGER,
  settle_reason TEXT, late_receipt INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER, cache_write_tokens INTEGER,
  weight INTEGER NOT NULL DEFAULT 1,
  root_limit_usd TEXT, has_root_limit INTEGER NOT NULL DEFAULT 0,
  billing_group_limit_usd TEXT, has_group_limit INTEGER NOT NULL DEFAULT 0,
  billing_group_limit_source TEXT, billing_group_limit_revision TEXT,
  review_skill TEXT, review_wave_id TEXT, review_slot_id TEXT,
  subscription_route TEXT, subscription_reset_at TEXT,
  local_answer_owner_pid INTEGER, local_answer_consumer_id TEXT, owner_birth TEXT, task_attempt INTEGER,
  prompt_cache_ttl TEXT, extra TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX attempts_root ON attempts(root_task_id);
CREATE INDEX attempts_group ON attempts(billing_group_id);
CREATE INDEX attempts_task ON attempts(task_id);
CREATE INDEX attempts_open ON attempts(state) WHERE (state IN ('reserved','dispatched','unresolved')
  OR (state='settled' AND settle_reason='abandoned'));
CREATE INDEX attempts_category_time ON attempts(category, ts_last_epoch);
CREATE INDEX attempts_review ON attempts(review_skill, review_wave_id);
CREATE INDEX attempts_route ON attempts(subscription_route);
CREATE TABLE summaries (
  scope TEXT NOT NULL, key TEXT NOT NULL, rows INTEGER NOT NULL,
  settled TEXT NOT NULL, confirmed TEXT NOT NULL, estimated TEXT NOT NULL, reserved TEXT NOT NULL,
  unresolved TEXT NOT NULL, accounted_num REAL NOT NULL,
  attempt_counts TEXT NOT NULL, unknown_unmetered INTEGER NOT NULL, priced_rows INTEGER NOT NULL,
  tracked_nonfinal_rows INTEGER NOT NULL, accounting_open_rows INTEGER NOT NULL, non_final_rows INTEGER NOT NULL,
  subscription_sessions INTEGER NOT NULL, subscription_windows TEXT NOT NULL, processing TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL, prompt_tokens_present INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL, completion_tokens_present INTEGER NOT NULL,
  cached_tokens INTEGER NOT NULL, cached_tokens_present INTEGER NOT NULL,
  cache_write_tokens INTEGER NOT NULL, cache_write_tokens_present INTEGER NOT NULL,
  physical_calls INTEGER NOT NULL, prompt_cache_ttls TEXT NOT NULL, caps TEXT NOT NULL,
  PRIMARY KEY (scope, key)
) WITHOUT ROWID;
CREATE INDEX summaries_accounted ON summaries(scope, accounted_num);
CREATE TABLE bindings (scope TEXT NOT NULL, key TEXT NOT NULL, binding TEXT NOT NULL,
  PRIMARY KEY (scope, key)) WITHOUT ROWID;
CREATE TABLE dirty_owners (owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE one_shots (attempt_id TEXT PRIMARY KEY, kind TEXT, task_id TEXT, root_task_id TEXT,
  subscription_route TEXT, source TEXT, category TEXT, identity TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
"""

FIT_MAX = {
    "profile": "owner_max", "rendered_mode": "max",
    "measurement_basis": "fresh_route_usage", "route_fp": "a" * 64,
    "round_id": "round-1", "target_total_tokens": 180000,
    "capacity_total_tokens": 200000, "context_target_miss": False,
    "automatic_pass_used": False, "measurement_density": 1.02,
}
FIT_LOW = dict(FIT_MAX, profile="owner_low", rendered_mode="low",
               target_total_tokens=40000, capacity_total_tokens=60000)
FIT_NANO = dict(FIT_MAX, profile="task_local_nano", rendered_mode="nano",
                target_total_tokens=12000, capacity_total_tokens=16000)


def iso(epoch_s: float) -> str:
    return _dt.datetime.fromtimestamp(epoch_s, _dt.timezone.utc).isoformat()


class Store:
    """Builds ``<root>/state/usage.sqlite`` exactly the way core lays it out."""

    def __init__(self, root: str, *, application_id: int = APPLICATION_ID_ENFORCED,
                 schema_version: Any = 1, lock_tier: Any = "enforced",
                 import_status: Any = "completed", journal_mode: str = "delete") -> None:
        self.root = root
        state = os.path.join(root, "state")
        os.makedirs(state, exist_ok=True)
        self.path = os.path.join(state, "usage.sqlite")
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA application_id = %d" % application_id)
            if journal_mode != "delete":
                conn.execute("PRAGMA journal_mode = %s" % journal_mode)
            meta = {
                "schema_version": schema_version,
                "lock_tier": lock_tier,
                "publication_marker": [0, 0],
                "import": {"status": import_status, "schema_version": schema_version,
                           "lock_tier": lock_tier, "header": None},
            }
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                             [(key, json.dumps(value)) for key, value in meta.items()])
        finally:
            conn.close()
        self.seq = 0

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None)

    def add(
        self,
        attempt_id: str,
        *,
        epoch: Optional[float],
        kind: Optional[str] = "attempt",
        state: Optional[str] = "settled",
        category: Optional[str] = "task",
        model: Optional[str] = "vendor/model-a",
        provider: Optional[str] = "openrouter",
        source: Optional[str] = "agent.task",
        task_id: Optional[str] = "task-1",
        root_task_id: Optional[str] = "task-1",
        parent_task_id: Optional[str] = "",
        prompt_tokens: Any = None,
        completion_tokens: Any = None,
        cached_tokens: Any = None,
        cache_write_tokens: Any = None,
        weight: int = 1,
        late_receipt: int = 0,
        extra: Any = None,
        ts_last: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """INSERT or REPLACE the ONE current row of ``attempt_id`` (core UPDATEs in place)."""
        self.seq += 1
        payload = extra if isinstance(extra, str) else json.dumps(extra if extra is not None else {})
        stamp = ts_last if ts_last is not None else (iso(epoch) if isinstance(epoch, (int, float)) else None)
        own = conn is None
        conn = conn or self.connect()
        try:
            existing = conn.execute("SELECT rowid, revision, seq_first FROM attempts WHERE attempt_id=?",
                                    (attempt_id,)).fetchone()
            values = dict(
                attempt_id=attempt_id, revision=(existing[1] + 1) if existing else 1, seq=self.seq,
                seq_first=existing[2] if existing else self.seq, kind=kind, state=state, task_id=task_id,
                root_task_id=root_task_id, parent_task_id=parent_task_id, model=model, provider=provider,
                category=category, source=source, ts_last=stamp, ts_last_epoch=epoch,
                ts_final=stamp if state in ("settled", "unresolved", "released") else None,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens,
                weight=weight, late_receipt=late_receipt, extra=payload,
                cost_usd="0.0123", review_slot_id="slot-secret-1",
                local_answer_consumer_id="consumer-secret",
            )
            columns = ",".join(values)
            marks = ",".join("?" * len(values))
            if existing:
                assignments = ",".join("%s=?" % key for key in values)
                conn.execute("UPDATE attempts SET %s WHERE attempt_id=?" % assignments,
                             tuple(values.values()) + (attempt_id,))
            else:
                conn.execute("INSERT INTO attempts (%s) VALUES (%s)" % (columns, marks),
                             tuple(values.values()))
            # Core _summary_keys files legacy imports as unattributed, even
            # when the attempts row keeps a named category. Do not invent a
            # category summary that would hide the reader's enumeration limit.
            # Only category membership is needed here, not accounting totals
            # or the other summary scopes.
            if kind not in ("legacy_metadata", "legacy_delta") and isinstance(category, str) and category:
                conn.execute(
                    "INSERT OR IGNORE INTO summaries VALUES (?, ?, 1, '0','0','0','0','0', 0.0, '{}', 0, 0, "
                    "0, 0, 0, 0, '{}', '{}', 0, 0, 0, 0, 0, 0, 0, 0, 0, '{}', '[]')",
                    ("category", category))
        finally:
            if own:
                conn.close()

    def bulk(self, rows) -> None:
        """Many rows in one transaction (large synthetic fixtures)."""
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            for attempt_id, kwargs in rows:
                self.add(attempt_id, conn=conn, **kwargs)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def set_meta(self, key: str, value: Any) -> None:
        conn = self.connect()
        try:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        finally:
            conn.close()

    def execute(self, sql: str, args=()) -> None:
        conn = self.connect()
        try:
            conn.execute(sql, args)
        finally:
            conn.close()
