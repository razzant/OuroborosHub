"""Tests for the usage-store reader (``lens_store``) and its projection.

Standard-library ``unittest`` only. Every store is built by ``store_fixture``
(core schema 1, column for column) inside a fresh temporary directory; nothing
here opens a real install's data. Core's store protocol is replaced by
``core_store_standin`` (a synthetic stand-in, never an imported Ouroboros), so a
pass here says how the reader uses those names, not that core's helpers work.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import pathlib
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, Optional

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS))

import core_store_standin as cs  # noqa: E402
import store_fixture as sf  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lens_core = _load("context_lens_core_for_store_tests", "lens_core.py")
lens_store = _load("context_lens_store_under_test", "lens_store.py")

NOW = time.time()
HOUR = 3600.0


class StoreCase(unittest.TestCase):
    # The core the reader resolves. By default a stand-in that answers as core
    # does on this install's runtime: WAL may be shared, connections are counted.
    core_options: Dict[str, Any] = {}

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="context-lens-store-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(setattr, lens_store, "_core_override", lens_store._CORE_UNSET)
        self.core = cs.make_core(**self.core_options)
        self.use_core(self.core)

    def use_core(self, core) -> None:
        """``None`` models a process without core's ``ouroboros.store``."""
        lens_store._core_override = core

    def store(self, **kwargs) -> sf.Store:
        return sf.Store(self.root, **kwargs)

    def read(self, horizon: str = "available", anchor_ms=None, **bounds):
        anchor = anchor_ms if anchor_ms is not None else int(NOW * 1000)
        span = lens_core.HORIZON_SPANS_MS.get(horizon)
        lower = lens_core.MIN_EPOCH_MS if span is None else max(lens_core.MIN_EPOCH_MS, anchor - span)
        return lens_store.read_store(
            self.root, lower_s=lower / 1000.0, upper_s=lens_core.MAX_EPOCH_MS / 1000.0,
            band_lower_s=lens_core.MIN_EPOCH_MS / 1000.0, **bounds)

    def snapshot(self, horizon: str = "available", limit: int = 4000, **bounds):
        anchor = int(NOW * 1000)
        read = self.read(horizon, anchor, **bounds)
        return lens_core.store_snapshot(read, horizon=horizon, anchor_ms=anchor, limit=limit,
                                        limits={"max_rows": bounds.get("max_rows", lens_store.MAX_ROWS)})

    def refused(self, code: str, **kwargs) -> None:
        with self.assertRaises(lens_store.StoreUnavailable) as caught:
            self.read(**kwargs)
        self.assertEqual(caught.exception.code, code)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

class TestSelection(StoreCase):
    def test_claudexor_model_labels_remain_distinct(self) -> None:
        store = self.store()
        names = ('claudexor::codex=gpt-6-astra', 'claudexor::claude=claude-opus-5-5[1m]')
        for index, name in enumerate(names):
            store.add('model-%d' % index, epoch=NOW - 10 + index,
                      prompt_tokens=100 + index, model=name)
        payload = self.snapshot('1h')
        self.assertEqual([p['model'] for p in payload['points']], list(names))
        self.assertEqual(payload['facets']['models'], sorted(names))
        self.assertEqual(lens_core._label('<script>bad</script>'), 'other')
        self.assertEqual(lens_core._label('model\ncredential'), 'other')

    def test_points_are_the_newest_rows_inside_the_span(self) -> None:
        store = self.store()
        store.add("old", epoch=NOW - 5 * HOUR, prompt_tokens=1)
        store.add("a", epoch=NOW - 0.5 * HOUR, prompt_tokens=2, category="review")
        store.add("b", epoch=NOW - 0.2 * HOUR, prompt_tokens=3)
        payload = self.snapshot("1h")
        self.assertEqual([p["prompt_tokens"] for p in payload["points"]], [2, 3])   # oldest first
        self.assertEqual(payload["source"]["kind"], "usage_store")
        self.assertTrue(payload["source"]["current"])
        self.assertTrue(payload["horizon"]["selection_complete"])
        self.assertEqual(payload["horizon"]["partial_reasons"], [])
        self.assertEqual(payload["horizon"]["covered_from_ms"], payload["horizon"]["cutoff_ms"])
        self.assertEqual(payload["horizon"]["records_selected"], 2)

    def test_null_and_empty_categories_are_streams_of_their_own(self) -> None:
        store = self.store()
        store.add("named", epoch=NOW - 60, prompt_tokens=1, category="task")
        store.add("null", epoch=NOW - 50, prompt_tokens=2, category=None)
        store.add("empty", epoch=NOW - 40, prompt_tokens=3, category="")
        payload = self.snapshot("1h")
        self.assertEqual(sorted(p["prompt_tokens"] for p in payload["points"]), [1, 2, 3])
        self.assertEqual(payload["facets"]["categories"], ["task", "unknown"])

    def test_the_row_cap_keeps_the_exact_global_newest_prefix(self) -> None:
        store = self.store()
        rows = []
        for index in range(30):
            rows.append(("r%02d" % index, dict(epoch=NOW - 1000 + index * 7.0, prompt_tokens=index,
                                               category=("task", "review", "consolidation")[index % 3])))
        store.bulk(rows)
        read = self.read("available", max_rows=8)
        taken = [row["prompt_tokens"] for row in read["rows"]]
        self.assertEqual(taken, list(range(29, 21, -1)), "the 8 newest across every stream, newest first")
        self.assertEqual(read["facts"]["partial_reasons"], ["row_cap"])
        payload = self.snapshot("available", max_rows=8)
        self.assertFalse(payload["horizon"]["selection_complete"])
        self.assertEqual(payload["horizon"]["covered_from_ms"], min(p["t"] for p in payload["points"]))

    def test_legacy_only_categories_do_not_claim_global_completeness(self) -> None:
        store = self.store()
        store.add("physical", epoch=NOW - 60, prompt_tokens=100)
        for index, kind in enumerate(("legacy_metadata", "legacy_delta")):
            store.add("hidden-%d" % index, epoch=NOW - 10 + index, kind=kind,
                      category="legacy-only-%d" % index, prompt_tokens=999999)
            store.add("shared-%d" % index, epoch=NOW - 80 + index, kind=kind,
                      category="task", prompt_tokens=999999)
        store.add("hidden-untimed", epoch=None, kind="legacy_delta", category="legacy-only-0")
        store.add("legacy-null", epoch=NOW - 70, kind="legacy_delta", category=None)
        store.add("legacy-empty", epoch=NOW - 70, kind="legacy_delta", category="")
        conn = store.connect()
        try:
            self.assertEqual(conn.execute("SELECT key FROM summaries WHERE scope='category'").fetchall(),
                             [("task",)], "core does not summarise legacy rows by their stored category")
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 8)
        finally:
            conn.close()
        for horizon in ("1h", "available"):
            payload = self.snapshot(horizon)
            self.assertEqual([p["prompt_tokens"] for p in payload["points"]], [100])
            self.assertTrue(payload["horizon"]["selection_complete"], "all eligible physical attempts are covered")
            self.assertEqual(payload["horizon"]["selection_scope"], "enumerated_categories")
            self.assertEqual(payload["source"]["category_enumeration"], "summary_keys_plus_null_and_empty")
            self.assertEqual(payload["source"]["legacy_category_coverage"], "not_guaranteed")
            self.assertEqual(payload["horizon"]["records_selected"], 5)
            self.assertEqual(payload["counters"]["excluded"]["legacy_rows"], 4)
            self.assertEqual(payload["horizon"]["unknown_timestamp"], 0, "counts concern enumerated streams only")
            self.assertEqual(payload["source"]["newest_record_ms"], int((NOW - 60) * 1000))

        # A physical request makes a legacy-only category discoverable. Its
        # legacy rows then enter the counts; no history scan is needed.
        store.add("new-physical", epoch=NOW - 20, category="legacy-only-0", prompt_tokens=200)
        payload = self.snapshot("1h")
        self.assertEqual([p["prompt_tokens"] for p in payload["points"]], [100, 200])
        self.assertEqual(payload["counters"]["excluded"]["legacy_rows"], 5)
        self.assertEqual(payload["horizon"]["unknown_timestamp"], 1)
        self.assertEqual(payload["source"]["newest_record_ms"], int((NOW - 10) * 1000))
        self.assertEqual(payload["source"]["legacy_category_coverage"], "not_guaranteed")

    def test_legacy_only_store_is_empty_only_within_its_enumerated_scope(self) -> None:
        store = self.store()
        store.add("legacy", epoch=NOW - 10, kind="legacy_delta", category="import-only")
        payload = self.snapshot("1h")
        self.assertEqual(payload["points"], [])
        self.assertEqual(payload["horizon"]["records_selected"], 0)
        self.assertTrue(payload["horizon"]["selection_complete"])
        self.assertEqual(payload["horizon"]["selection_scope"], "enumerated_categories")
        self.assertIsNone(payload["source"]["newest_record_ms"])
        self.assertEqual(payload["source"]["legacy_category_coverage"], "not_guaranteed")

    def test_exactly_the_cap_is_still_complete(self) -> None:
        store = self.store()
        store.bulk([("r%d" % i, dict(epoch=NOW - 100 + i, prompt_tokens=i)) for i in range(5)])
        self.assertEqual(self.read("available", max_rows=5)["facts"]["partial_reasons"], [])
        self.assertEqual(self.read("available", max_rows=4)["facts"]["partial_reasons"], ["row_cap"])

    def test_an_idle_install_is_complete_and_states_its_newest_record(self) -> None:
        store = self.store()
        store.add("a", epoch=NOW - 3 * HOUR, prompt_tokens=10)
        payload = self.snapshot("1h")
        self.assertEqual(payload["points"], [])
        self.assertTrue(payload["horizon"]["selection_complete"], "nothing in the span is a complete answer")
        self.assertEqual(payload["source"]["newest_record_ms"], int((NOW - 3 * HOUR) * 1000))

    def test_the_cutoff_is_inclusive(self) -> None:
        store = self.store()
        anchor = int(NOW * 1000)
        edge = (anchor - 3600 * 1000) / 1000.0
        store.add("edge", epoch=edge, prompt_tokens=1)
        store.add("before", epoch=edge - 0.002, prompt_tokens=2)
        read = self.read("1h", anchor)
        self.assertEqual([row["prompt_tokens"] for row in read["rows"]], [1])

    def test_unusable_timestamps_are_counted_and_never_selected(self) -> None:
        store = self.store()
        store.add("ok", epoch=NOW - 60, prompt_tokens=1)
        store.add("none", epoch=None, prompt_tokens=2)
        store.add("ancient", epoch=631152000.0, prompt_tokens=3)          # 1990
        store.add("future", epoch=7258118400.0, prompt_tokens=4)          # 2200
        store.add("infinite", epoch=float("inf"), prompt_tokens=5, ts_last="not a time")
        for horizon in ("1h", "available"):
            payload = self.snapshot(horizon)
            self.assertEqual([p["prompt_tokens"] for p in payload["points"]], [1], horizon)
            self.assertEqual(payload["horizon"]["unknown_timestamp"], 4, horizon)

    def test_a_category_beyond_the_cap_is_disclosed(self) -> None:
        store = self.store()
        for index, category in enumerate(("a1", "b2", "c3", "d4")):
            store.add("r%d" % index, epoch=NOW - 60 + index, prompt_tokens=index, category=category)
        read = self.read("1h", max_categories=2)
        self.assertIn("category_cap", read["facts"]["partial_reasons"])
        self.assertEqual(sorted(row["category"] for row in read["rows"]), ["a1", "b2"])
        payload = self.snapshot("1h", max_categories=2)
        self.assertFalse(payload["horizon"]["selection_complete"])
        self.assertEqual(payload["horizon"]["selection_scope"], "enumerated_categories")

    def test_a_time_or_step_bound_stops_with_an_exact_prefix(self) -> None:
        store = self.store()
        store.bulk([("r%04d" % i, dict(epoch=NOW - 5000 + i, prompt_tokens=i,
                                       category=("task", "review")[i % 2])) for i in range(3000)])
        full = [row["prompt_tokens"] for row in self.read("available")["rows"]]
        for bounds, reason in (({"merge_step_budget": 1}, "step_budget"),
                               ({"merge_budget_sec": 0.0}, "time_budget")):
            read = self.read("available", **bounds)
            taken = [row["prompt_tokens"] for row in read["rows"]]
            self.assertIn(reason, read["facts"]["partial_reasons"])
            self.assertLess(len(taken), len(full))
            self.assertEqual(taken, full[:len(taken)], "a stopped merge is still the newest prefix")

    def test_the_hard_bound_abandons_the_read(self) -> None:
        store = self.store()
        store.bulk([("r%04d" % i, dict(epoch=NOW - 5000 + i, prompt_tokens=i)) for i in range(2000)])
        self.refused("store_slow", hard_budget_sec=0.0)

    def test_the_metadata_byte_bound_is_disclosed(self) -> None:
        store = self.store()
        store.bulk([("r%02d" % i, dict(epoch=NOW - 100 + i, prompt_tokens=i,
                                       extra={"physical_context": sf.FIT_MAX})) for i in range(20)])
        one = len(json.dumps(sf.FIT_MAX, separators=(",", ":")))
        read = self.read("available", max_extra_total=one * 5 + 10)
        self.assertIn("byte_cap", read["facts"]["partial_reasons"])
        self.assertEqual([row["prompt_tokens"] for row in read["rows"]], [19, 18, 17, 16, 15])


class TestQueryPlans(StoreCase):
    def plan(self, sql: str, args) -> str:
        store = self.store()
        conn = store.connect()
        try:
            return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args))
        finally:
            conn.close()

    def test_every_stream_is_an_index_range_in_index_order(self) -> None:
        for sql, args in (
            (lens_store.STREAM_SQL_JSON, (16384, "task", 0.0, 1e10, 10)),
            (lens_store.STREAM_SQL_TEXT, (16384, "task", 0.0, 1e10, 10)),
            (lens_store.STREAM_SQL_JSON, (16384, None, 0.0, 1e10, 10)),
            (lens_store.NEWEST_SQL, ("task", 0.0, 1e10)),
            (lens_store.UNTIMED_SQL, ("task", 10)),
            (lens_store.BELOW_BAND_SQL, ("task", 0.0, 10)),
            (lens_store.ABOVE_BAND_SQL, ("task", 1e10, 10)),
        ):
            plan = self.plan(sql, args)
            self.assertIn(lens_store.CATEGORY_INDEX, plan, sql)
            self.assertNotIn("TEMP B-TREE", plan, sql)
            self.assertNotIn("SCAN attempts", plan, sql)
            shutil.rmtree(self.root, True)
            os.makedirs(self.root)

    def test_why_there_is_no_global_order(self) -> None:
        # The one-query alternative scans and sorts the whole table.
        plan = self.plan("SELECT rowid FROM attempts ORDER BY ts_last_epoch DESC LIMIT 10", ())
        self.assertTrue("SCAN" in plan or "TEMP B-TREE" in plan, plan)

    def test_category_values_come_from_the_summary_primary_key(self) -> None:
        plan = self.plan(lens_store.CATEGORY_SQL, (65,))
        self.assertIn("summaries", plan)
        self.assertNotIn("TEMP B-TREE", plan)


class TestBoundedWork(StoreCase):
    def steps(self, total: int) -> int:
        shutil.rmtree(self.root, True)
        os.makedirs(self.root)
        store = self.store()
        store.bulk([("r%06d" % i, dict(epoch=NOW - 400000 + i * 10, prompt_tokens=i,
                                       category="c%02d" % (i % 25),
                                       extra={"physical_context": sf.FIT_MAX, "noise": "x" * 400}))
                    for i in range(total)])
        read = self.read("available", max_rows=300)
        self.assertEqual(len(read["rows"]), 300)
        self.assertEqual(read["facts"]["partial_reasons"], ["row_cap"])
        return read["facts"]["sql_steps"]

    def test_the_work_of_a_read_does_not_grow_with_history(self) -> None:
        small = self.steps(2000)
        large = self.steps(20000)
        self.assertLess(large, small * 1.5 + 20000, (small, large))


# ---------------------------------------------------------------------------
# Meaning
# ---------------------------------------------------------------------------

class TestSemantics(StoreCase):
    def test_only_a_settled_row_carries_tokens(self) -> None:
        store = self.store()
        store.add("d", epoch=NOW - 60, state="dispatched", prompt_tokens=999, cached_tokens=5)
        point = self.snapshot("1h")["points"][0]
        self.assertIsNone(point["prompt_tokens"])
        self.assertIsNone(point["cached_tokens"])

    def test_missing_is_not_zero_and_zero_is_measured(self) -> None:
        store = self.store()
        store.add("none", epoch=NOW - 60, prompt_tokens=None)
        store.add("zero", epoch=NOW - 50, prompt_tokens=0)
        payload = self.snapshot("1h")
        self.assertEqual([p["prompt_tokens"] for p in payload["points"]], [None, 0])
        self.assertEqual(payload["counters"]["measured"], 1)
        self.assertEqual(payload["counters"]["settled_without_tokens"], 1)

    def test_cache_counts_are_passed_on_and_never_added(self) -> None:
        store = self.store()
        store.add("a", epoch=NOW - 60, prompt_tokens=100000, cached_tokens=95000, cache_write_tokens=3000)
        point = self.snapshot("1h")["points"][0]
        self.assertEqual((point["prompt_tokens"], point["cached_tokens"], point["cache_write_tokens"]),
                         (100000, 95000, 3000))

    def test_modes_max_low_nano_and_unknown(self) -> None:
        store = self.store()
        store.add("max", epoch=NOW - 50, prompt_tokens=1, extra={"physical_context": sf.FIT_MAX})
        store.add("low", epoch=NOW - 40, prompt_tokens=2, extra={"physical_context": sf.FIT_LOW})
        store.add("nano", epoch=NOW - 30, prompt_tokens=3, extra={"physical_context": sf.FIT_NANO})
        store.add("none", epoch=NOW - 20, prompt_tokens=4)
        store.add("odd", epoch=NOW - 10, prompt_tokens=5,
                  extra={"physical_context": dict(sf.FIT_MAX, rendered_mode="turbo", profile="x")})
        payload = self.snapshot("1h")
        self.assertEqual([p["mode"] for p in payload["points"]], ["max", "low", "nano", None, None])
        self.assertEqual(payload["points"][2]["profile"], "task_local_nano")
        self.assertIsNone(payload["points"][4]["profile"])
        self.assertEqual(payload["facets"]["modes"], ["max", "low", "nano", "unknown"])

    def test_a_row_updated_in_place_is_one_request_across_reads(self) -> None:
        store = self.store()
        store.add("x", epoch=NOW - 90, state="reserved")
        first = self.snapshot("1h")
        self.assertEqual(first["counters"]["registered_attempts"], 1)
        self.assertEqual(first["counters"]["sent_attempts"], 0, "a reserved request was not sent")
        store.add("x", epoch=NOW - 80, state="dispatched")
        store.add("x", epoch=NOW - 70, state="settled", prompt_tokens=5000)
        second = self.snapshot("1h")
        self.assertEqual(len(second["points"]), 1)
        self.assertEqual(second["counters"]["registered_attempts"], 1)
        self.assertEqual(second["counters"]["sent_attempts"], 1)
        self.assertEqual(second["points"][0]["id"], first["points"][0]["id"], "one id for one attempt")

    def test_a_late_refinement_moves_the_point_and_never_duplicates_it(self) -> None:
        store = self.store()
        store.add("x", epoch=NOW - 2 * HOUR, prompt_tokens=7000)
        self.assertEqual(self.snapshot("1h")["points"], [])
        store.add("x", epoch=NOW - 60, prompt_tokens=7000, late_receipt=1)   # a late receipt rewrites ts_last
        payload = self.snapshot("1h")
        self.assertEqual(len(payload["points"]), 1)
        self.assertTrue(payload["points"][0]["late_receipt"])
        self.assertEqual(payload["points"][0]["t"], int((NOW - 60) * 1000))

    def test_aggregates_sessions_and_other_kinds_are_counted_never_drawn(self) -> None:
        store = self.store()
        store.add("real", epoch=NOW - 60, prompt_tokens=1000)
        store.add("group", epoch=NOW - 50, kind="usage_baseline_group", weight=40, prompt_tokens=10 ** 9)
        store.add("session", epoch=NOW - 40, kind="subscription_session", prompt_tokens=10 ** 8)
        store.add("external", epoch=NOW - 30, kind="external_unmetered", prompt_tokens=10 ** 7)
        store.add("legacy", epoch=NOW - 20, kind="legacy_delta", prompt_tokens=10 ** 6)
        store.add("future", epoch=NOW - 10, kind="future_kind", prompt_tokens=10 ** 5)
        store.add("stateless", epoch=NOW - 5, state="weird", prompt_tokens=10 ** 4)
        payload = self.snapshot("1h")
        self.assertEqual([p["prompt_tokens"] for p in payload["points"]], [1000])
        excluded = payload["counters"]["excluded"]
        self.assertEqual(excluded["baseline_rows"], 1)
        self.assertEqual(excluded["folded_attempts_weighted"], 40)
        self.assertEqual(excluded["folded_attempts"], 40)
        self.assertEqual(excluded["baselines_without_header"], 0)
        self.assertEqual(excluded["subscription_sessions"], 1)
        self.assertEqual(excluded["external_unmetered"], 1)
        self.assertEqual(excluded["legacy_rows"], 1)
        self.assertEqual(excluded["unknown_kind"], 1)
        self.assertEqual(excluded["attempts_without_state"], 1)
        self.assertEqual(payload["counters"]["registered_attempts"], 1)

    def test_hostile_values_degrade_to_missing(self) -> None:
        store = self.store()
        store.add("a", epoch=NOW - 60, prompt_tokens=2 ** 60, completion_tokens=-5, cached_tokens=1.5,
                  model="<script>", source="x" * 200)
        point = self.snapshot("1h")["points"][0]
        self.assertIsNone(point["prompt_tokens"])
        self.assertIsNone(point["completion_tokens"])
        self.assertIsNone(point["cached_tokens"])
        self.assertEqual(point["model"], "other")
        self.assertEqual(point["source"], "other")


class TestMetadata(StoreCase):
    def test_only_physical_context_is_ever_read_from_extra(self) -> None:
        store = self.store()
        secrets = {
            "physical_context": dict(sf.FIT_MAX, route_fp="ROUTE-FP-SECRET", round_id="ROUND-SECRET"),
            "credential_profile_id": "CRED-PROFILE-SECRET",
            "session_id_sha256": "SESSION-SECRET",
            "reason": "before_dispatch_failed: PROMPT TEXT SECRET",
            "transport_outcome": {"error": "ERROR-BODY-SECRET"},
            "provider_receipt_binding": {"endpoint": "https://example.invalid/SECRET"},
        }
        store.add("attempt-RAW-ID-SECRET", epoch=NOW - 60, prompt_tokens=10, extra=secrets,
                  task_id="TASK-ID-SECRET", root_task_id="ROOT-ID-SECRET", parent_task_id="PARENT-SECRET")
        read = self.read("1h")
        self.assertEqual(set(read["rows"][0]) - set(lens_store.STREAM_COLUMNS),
                         {"physical_context", "extra_status"})
        blob = json.dumps(self.snapshot("1h"))
        for secret in ("SECRET", "0.0123", "slot-secret", "consumer-secret", "cost_usd", "review_slot"):
            self.assertNotIn(secret, blob)
        self.assertIn('"mode": "max"', blob)

    def test_oversized_or_malformed_metadata_leaves_mode_unknown_and_is_counted(self) -> None:
        store = self.store()
        store.add("big", epoch=NOW - 60, prompt_tokens=1,
                  extra={"physical_context": sf.FIT_MAX, "padding": "x" * 5000})
        store.add("broken", epoch=NOW - 50, prompt_tokens=2, extra="{not json")
        store.add("list", epoch=NOW - 40, prompt_tokens=3, extra="[1, 2]")
        store.add("text", epoch=NOW - 30, prompt_tokens=4,
                  extra=json.dumps({"physical_context": json.dumps(sf.FIT_MAX)}))
        store.add("fine", epoch=NOW - 20, prompt_tokens=5, extra={"physical_context": sf.FIT_LOW})
        payload = self.snapshot("1h", max_extra_chars=4096)
        self.assertEqual([p["mode"] for p in payload["points"]], [None, None, None, None, "low"])
        self.assertEqual(payload["source"]["context_unread"], 3)

    def test_the_python_fallback_reads_the_same_facts(self) -> None:
        store = self.store()
        store.add("a", epoch=NOW - 50, prompt_tokens=1, extra={"physical_context": sf.FIT_NANO})
        store.add("b", epoch=NOW - 40, prompt_tokens=2, extra="{not json")
        store.add("c", epoch=NOW - 30, prompt_tokens=3,
                  extra=json.dumps({"physical_context": json.dumps(sf.FIT_MAX)}))
        store.add("d", epoch=NOW - 20, prompt_tokens=4)
        native = self.snapshot("1h")
        original = lens_store._json_functions
        lens_store._json_functions = lambda conn: False
        try:
            fallback = self.snapshot("1h")
        finally:
            lens_store._json_functions = original
        self.assertEqual(native["source"]["metadata_read"], "sqlite_json")
        self.assertEqual(fallback["source"]["metadata_read"], "python_text")
        self.assertEqual(native["points"], fallback["points"])
        self.assertEqual(native["source"]["context_unread"], fallback["source"]["context_unread"])


# ---------------------------------------------------------------------------
# Read-only access and refusals
# ---------------------------------------------------------------------------

class TestReadOnly(StoreCase):
    def fingerprint(self):
        state = os.path.join(self.root, "state")
        listing = sorted(os.listdir(state))
        info = os.stat(os.path.join(state, "usage.sqlite"))
        with open(os.path.join(state, "usage.sqlite"), "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return listing, info.st_size, info.st_mtime_ns, digest

    def test_reading_never_writes_or_creates_anything(self) -> None:
        store = self.store()
        store.bulk([("r%d" % i, dict(epoch=NOW - 100 + i, prompt_tokens=i)) for i in range(50)])
        before = self.fingerprint()
        for horizon in ("1h", "24h", "available"):
            self.snapshot(horizon)
        self.assertEqual(self.fingerprint(), before)

    def test_the_connection_is_read_only_query_only_and_never_immutable(self) -> None:
        store = self.store()
        seen = []
        original = lens_store.sqlite3.connect

        def recording(database, *args, **kwargs):
            seen.append((database, kwargs))
            return original(database, *args, **kwargs)

        # Through core's counted_connect (the stand-in passes core's factory on),
        # and the plain connection used when no core helper exists.
        for core, counted in ((self.core, True), (None, False)):
            seen.clear()
            lens_store.sqlite3.connect = recording
            try:
                conn = lens_store.connect(store.path, core)
            finally:
                lens_store.sqlite3.connect = original
            try:
                uri, kwargs = seen[0]
                self.assertEqual(len(seen), 1)
                self.assertTrue(uri.startswith("file:"))
                self.assertIn("mode=ro", uri)
                self.assertNotIn("immutable", uri)
                self.assertNotIn("nolock", uri)
                self.assertTrue(kwargs.get("uri"))
                self.assertLessEqual(kwargs.get("timeout"), 1.0)
                self.assertEqual("factory" in kwargs, counted)
                self.assertEqual(self.core.registry.total_open(), 1 if counted else 0)
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(sqlite3.Error):
                    conn.execute("INSERT INTO meta (key, value) VALUES ('x', '1')")
            finally:
                conn.close()
        self.assertEqual(self.core.registry.total_open(), 0)
        self.assertEqual(self.core.registry.opened, 1)
        self.assertEqual(self.core.registry.closed, 1)

    def test_the_lock_is_released_before_the_read_returns(self) -> None:
        store = self.store()
        store.add("a", epoch=NOW - 60, prompt_tokens=1)
        self.read("1h")
        writer = sqlite3.connect(store.path, timeout=0, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")       # would fail at once if a reader still held the lock
            writer.execute("ROLLBACK")
        finally:
            writer.close()

    def test_a_writer_holding_the_store_answers_busy_never_zero(self) -> None:
        store = self.store()
        store.add("a", epoch=NOW - 60, prompt_tokens=1)
        writer = sqlite3.connect(store.path, isolation_level=None)
        writer.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            self.refused("store_busy")
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            writer.execute("ROLLBACK")
            writer.close()

    def test_concurrent_reads_agree(self) -> None:
        store = self.store()
        store.bulk([("r%d" % i, dict(epoch=NOW - 100 + i, prompt_tokens=i)) for i in range(40)])
        results, errors = [], []

        def worker() -> None:
            try:
                results.append(self.snapshot("1h")["counters"]["registered_attempts"])
            except Exception as exc:                       # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, [40] * 8)


class TestRefusals(StoreCase):
    def test_the_name_tier_is_refused_before_any_row_is_read(self) -> None:
        store = self.store(application_id=sf.APPLICATION_ID_NAME, lock_tier="name")
        store.execute("DROP TABLE meta")             # decided from the header alone
        self.refused("store_name_tier")

    def test_an_unknown_application_id_is_unsupported(self) -> None:
        self.store(application_id=0x12345678)
        self.refused("store_unsupported")

    def test_schema_version_and_lock_tier_are_checked(self) -> None:
        self.store(schema_version=2)
        self.refused("store_unsupported")
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        self.store(lock_tier="name")                 # header says enforced, meta disagrees
        self.refused("store_unsupported")

    def test_an_unfinished_import_shows_nothing(self) -> None:
        self.store(import_status="running")
        self.refused("store_not_ready")

    def test_a_missing_index_or_column_is_unsupported(self) -> None:
        store = self.store()
        store.execute("DROP INDEX attempts_category_time")
        self.refused("store_unsupported")
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        store = self.store()
        store.execute("ALTER TABLE attempts DROP COLUMN late_receipt")
        self.refused("store_unsupported")

    def test_a_corrupt_file_is_unreadable_and_left_alone(self) -> None:
        os.makedirs(os.path.join(self.root, "state"))
        path = os.path.join(self.root, "state", "usage.sqlite")
        with open(path, "wb") as handle:
            handle.write(b"this is not a database" * 100)
        self.refused("store_unreadable")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"this is not a database" * 100)

    def test_symlinks_and_non_files_are_refused(self) -> None:
        elsewhere = tempfile.mkdtemp(prefix="context-lens-elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        sf.Store(elsewhere)
        os.makedirs(os.path.join(self.root, "state"))
        os.symlink(os.path.join(elsewhere, "state", "usage.sqlite"),
                   os.path.join(self.root, "state", "usage.sqlite"))
        self.refused("store_not_confined")
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        os.symlink(os.path.join(elsewhere, "state"), os.path.join(self.root, "state"))
        self.refused("store_not_confined")
        os.remove(os.path.join(self.root, "state"))
        os.makedirs(os.path.join(self.root, "state", "usage.sqlite"))
        self.refused("store_not_regular")

    def test_nothing_at_the_name_is_the_only_missing_case(self) -> None:
        with self.assertRaises(lens_store.StoreMissing):
            self.read()
        os.makedirs(os.path.join(self.root, "state"))
        with self.assertRaises(lens_store.StoreMissing):
            self.read()

    def test_a_store_replaced_during_the_read_is_discarded(self) -> None:
        self.store().add("a", epoch=NOW - 60, prompt_tokens=1)
        original = lens_store._identity
        calls = []

        def shifting(path):
            calls.append(path)
            dev, ino = original(path)
            return (dev, ino + len(calls))

        lens_store._identity = shifting
        try:
            self.refused("store_replaced")
        finally:
            lens_store._identity = original


# ---------------------------------------------------------------------------
# The journal: WAL under core's rule, the rollback journal as before
# ---------------------------------------------------------------------------

# A writer in ANOTHER process. It attaches to a built WAL store, keeps its log
# (wal_autocheckpoint=0), says "ready", waits for "go", commits ``batches`` x
# ``count`` rows, then holds its connection until its stdin says so (a line or
# EOF), or ("crash") leaves without closing, so -wal and -shm stay behind with
# committed frames.
WRITER = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
import store_fixture as sf
root, finish = sys.argv[2], sys.argv[3]
count, batches, pause = int(sys.argv[4]), int(sys.argv[5]), float(sys.argv[6])
store = sf.Store.existing(root)
conn = store.connect()
assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
conn.execute("PRAGMA wal_autocheckpoint = 0")
print("ready", flush=True)
if sys.stdin.readline().strip() == "go":
    now = time.time()
    n = 0
    for _ in range(batches):
        conn.execute("BEGIN")
        for _ in range(count):
            n += 1
            store.add("child-%06d" % n, epoch=now - 900 + n * 0.01, prompt_tokens=n, conn=conn)
        conn.execute("COMMIT")
        print("committed %d" % n, flush=True)
        if pause:
            time.sleep(pause)
    if finish == "crash":
        os._exit(0)
    sys.stdin.readline()
conn.close()
print("closed", flush=True)
'''


# Every wait on the writer process ends: one line, or its exit, within this long.
WRITER_WAIT_SEC = 60.0


class WriterLines:
    """The writer's stdout lines, pumped by a daemon thread so each wait has a deadline.

    A writer that hangs fails the test instead of hanging it; one that exits
    early reads as end of output (``""``), as ``readline()`` did.
    """

    def __init__(self, stream) -> None:
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for line in stream:
                self._lines.put(line)
        except (OSError, ValueError):          # the pipe closed under the pump
            pass
        finally:
            self._lines.put(None)

    def next(self, timeout: float = WRITER_WAIT_SEC) -> str:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("the writer process wrote no line within %.0f s" % timeout) from None
        if line is None:
            self._lines.put(None)               # every later read sees the end too
            return ""
        return line

    def join(self, timeout: float = WRITER_WAIT_SEC) -> None:
        self._thread.join(timeout)


class Pinned:
    """File digests through descriptors held open until the test ends.

    Closing ANY descriptor of a file drops every POSIX lock this process holds
    on it, so a digest taken with open()/close() would release a same-process
    connection's locks mid-test. Each file identity is opened once and closed
    at cleanup, after the test's own connections (cleanups run in reverse
    order). A -shm is never opened; only its presence is stated.
    """

    def __init__(self, case: unittest.TestCase) -> None:
        self.fds: Dict[tuple, int] = {}
        case.addCleanup(self.close)

    def _fd(self, path: str):
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return None
        key = (path, info.st_dev, info.st_ino)
        if key not in self.fds:
            self.fds[key] = os.open(path, cs.READ_FLAGS)
        return self.fds[key]

    def digest(self, path: str):
        fd = self._fd(path)
        if fd is None:
            return None
        size = os.fstat(fd).st_size
        return size, hashlib.sha256(cs.read_at(fd, size, 0) if size else b"").hexdigest()

    def header_versions(self, path: str):
        """Header bytes 18-19 (2, 2 = WAL; sqlite.org/fileformat2.html)."""
        return tuple(cs.read_at(self._fd(path), 2, 18))

    def close(self) -> None:
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()


class WalCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.pinned = Pinned(self)

    def wal_store(self, **kwargs) -> sf.Store:
        store = self.store(journal_mode="wal", **kwargs)
        self.assertEqual(self.pinned.header_versions(store.path), (2, 2))
        return store

    def files(self, store: sf.Store):
        return sorted(os.listdir(os.path.dirname(store.path)))

    def same_process_writer(self, store: sf.Store) -> sqlite3.Connection:
        """A writer in THIS process that keeps its log, and never waits on a lock."""
        writer = sqlite3.connect(store.path, isolation_level=None, timeout=0)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        return writer

    def writer_process(self, finish: str = "hold", count: int = 10, batches: int = 1,
                       pause: float = 0.0) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", WRITER, str(_TESTS), self.root, finish,
             str(count), str(batches), str(pause)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        lines = proc.writer_lines = WriterLines(proc.stdout)

        def finish_writer() -> None:
            try:
                proc.stdin.close()          # EOF: a waiting or holding writer closes and leaves
            except OSError:
                pass
            try:
                proc.wait(timeout=WRITER_WAIT_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()                 # this test's own writer, through its own handle
                proc.wait(timeout=WRITER_WAIT_SEC)
            lines.join()                    # its exit ends the output the pump reads
            proc.stdout.close()

        self.addCleanup(finish_writer)
        self.assertEqual(lines.next().strip(), "ready")
        return proc

    @staticmethod
    def go(proc: subprocess.Popen) -> None:
        proc.stdin.write("go\n")
        proc.stdin.flush()

    def committed(self, proc: subprocess.Popen, total: int) -> None:
        while True:
            line = proc.writer_lines.next().strip()
            self.assertTrue(line.startswith("committed "), line)
            if int(line.split()[1]) == total:
                return

    def release(self, proc: subprocess.Popen) -> None:
        proc.stdin.write("\n")
        proc.stdin.flush()
        self.assertEqual(proc.writer_lines.next().strip(), "closed")
        self.assertEqual(proc.wait(timeout=WRITER_WAIT_SEC), 0)


class TestWalStore(WalCase):
    """A WAL store where core says this runtime may share it (the stand-in's default)."""

    def test_committed_frames_still_in_the_log_are_read_and_left_alone(self) -> None:
        store = self.wal_store()
        store.add("checkpointed", epoch=NOW - 120, prompt_tokens=1)   # its close checkpointed it
        db = self.pinned.digest(store.path)
        writer = self.same_process_writer(store)
        for index in range(3):
            store.add("in-log-%d" % index, epoch=NOW - 60 + index, prompt_tokens=100 + index, conn=writer)
        self.assertEqual(self.pinned.digest(store.path), db, "the three rows exist only in -wal")
        log = self.pinned.digest(store.path + "-wal")
        self.assertGreater(log[0], 0)
        history = list(writer.iterdump())
        for _ in range(3):
            payload = self.snapshot("1h")
            self.assertEqual([p["prompt_tokens"] for p in payload["points"]], [1, 100, 101, 102])
        self.assertEqual((payload["source"]["journal"], payload["source"]["connection"]), ("wal", "counted"))
        self.assertTrue(payload["horizon"]["selection_complete"])
        self.assertEqual(self.pinned.digest(store.path), db, "no checkpoint, no content write")
        self.assertEqual(self.pinned.digest(store.path + "-wal"), log)
        self.assertEqual(list(writer.iterdump()), history)
        self.assertEqual(self.core.registry.total_open(), 0)
        self.assertNotIn("wal_file", self.core.registry.names(), "a sharing runtime needs no header read")
        path, tier = [details for name, details in self.core.registry.calls if name == "_journal_mode"][0]
        self.assertIsInstance(path, pathlib.Path)
        self.assertEqual((path, tier), (pathlib.Path(store.path), "enforced"))

    def test_a_writer_in_another_process_is_read_through_its_log(self) -> None:
        store = self.wal_store()
        proc = self.writer_process(count=25)
        self.go(proc)
        self.committed(proc, 25)
        self.assertIn("usage.sqlite-wal", self.files(store))
        db, log = self.pinned.digest(store.path), self.pinned.digest(store.path + "-wal")
        read = self.read("1h")
        self.assertEqual(len(read["rows"]), 25)
        self.assertEqual(read["facts"]["journal"], "wal")
        self.assertEqual((self.pinned.digest(store.path), self.pinned.digest(store.path + "-wal")), (db, log))
        self.release(proc)
        # The writer was the last connection, so its close checkpointed and
        # removed the side files: no lock or read mark of this read survived.
        self.assertEqual(self.files(store), ["usage.sqlite"])

    def test_a_crash_left_log_is_read_and_left_in_place(self) -> None:
        store = self.wal_store()
        proc = self.writer_process(finish="crash", count=12)
        self.go(proc)
        self.committed(proc, 12)
        self.assertEqual(proc.wait(timeout=60), 0)
        left = ["usage.sqlite", "usage.sqlite-shm", "usage.sqlite-wal"]
        self.assertEqual(self.files(store), left)
        db, log = self.pinned.digest(store.path), self.pinned.digest(store.path + "-wal")
        for _ in range(2):
            self.assertEqual(len(self.read("1h")["rows"]), 12)
        self.assertEqual(self.pinned.digest(store.path), db, "a read-only connection never checkpoints")
        self.assertEqual(self.pinned.digest(store.path + "-wal"), log)
        self.assertEqual(self.files(store), left)

    def test_a_wal_header_without_side_files_gains_only_coordination_files(self) -> None:
        store = self.wal_store()
        store.add("a", epoch=NOW - 60, prompt_tokens=5)
        self.assertEqual(self.files(store), ["usage.sqlite"])
        db = self.pinned.digest(store.path)
        self.assertEqual([row["prompt_tokens"] for row in self.read("1h")["rows"]], [5])
        self.assertEqual(self.pinned.digest(store.path), db)
        # SQLite's coordination files, not content: an empty log and the index.
        self.assertEqual(self.files(store), ["usage.sqlite", "usage.sqlite-shm", "usage.sqlite-wal"])
        self.assertEqual(os.path.getsize(store.path + "-wal"), 0)
        # The next read-write connection treats them as usual: its last close removes both.
        store.add("b", epoch=NOW - 30, prompt_tokens=6)
        self.assertEqual(self.files(store), ["usage.sqlite"])

    def test_one_snapshot_while_a_writer_commits_during_the_read(self) -> None:
        store = self.wal_store()
        store.add("before", epoch=NOW - 60, prompt_tokens=1)
        writer = self.same_process_writer(store)
        original = lens_store._categories
        committed = []

        def commit_meanwhile(conn, max_categories):
            found = original(conn, max_categories)
            store.add("meanwhile", epoch=NOW - 30, prompt_tokens=2, conn=writer)   # timeout=0
            committed.append(True)
            return found

        lens_store._categories = commit_meanwhile
        try:
            read = self.read("1h")
        finally:
            lens_store._categories = original
        self.assertEqual(committed, [True], "the writer committed while the read held its snapshot")
        self.assertEqual([row["prompt_tokens"] for row in read["rows"]], [1])
        self.assertEqual([row["prompt_tokens"] for row in self.read("1h")["rows"]], [2, 1])

    def test_an_open_write_transaction_is_neither_seen_nor_waited_on(self) -> None:
        store = self.wal_store()
        store.add("committed", epoch=NOW - 60, prompt_tokens=1)
        writer = self.same_process_writer(store)
        writer.execute("BEGIN IMMEDIATE")
        try:
            store.add("uncommitted", epoch=NOW - 30, prompt_tokens=2, conn=writer)
            started = time.monotonic()
            read = self.read("1h")
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            writer.execute("ROLLBACK")
        self.assertEqual([row["prompt_tokens"] for row in read["rows"]], [1])

    def test_concurrent_reads_beside_a_committing_writer_each_see_whole_commits(self) -> None:
        store = self.wal_store()
        proc = self.writer_process(count=5, batches=40, pause=0.005)
        counts, errors = [], []

        def reader() -> None:
            for _ in range(12):
                try:
                    counts.append(len(self.read("1h")["rows"]))
                except Exception as exc:                  # pragma: no cover - a failure to report
                    errors.append(repr(exc))

        threads = [threading.Thread(target=reader) for _ in range(8)]
        self.go(proc)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.committed(proc, 200)
        self.assertEqual(errors, [])
        self.assertEqual(len(counts), 96)
        self.assertTrue(all(count % 5 == 0 for count in counts), sorted(set(counts)))
        self.assertEqual(len(self.read("1h")["rows"]), 200)
        self.assertEqual(self.core.registry.total_open(), 0)
        self.assertEqual(self.core.registry.opened, self.core.registry.closed)
        self.release(proc)

    def test_every_validation_still_applies_to_a_wal_store(self) -> None:
        cases = (
            ("store_unsupported", dict(application_id=0x12345678), None),
            ("store_name_tier", dict(application_id=sf.APPLICATION_ID_NAME, lock_tier="name"),
             "DROP TABLE meta"),
            ("store_unsupported", dict(schema_version=2), None),
            ("store_unsupported", dict(lock_tier="name"), None),
            ("store_not_ready", dict(import_status="running"), None),
            ("store_unsupported", {}, "DROP INDEX attempts_category_time"),
            ("store_unsupported", {}, "ALTER TABLE attempts DROP COLUMN late_receipt"),
        )
        for code, options, damage in cases:
            with self.subTest(code=code, options=options, damage=damage):
                shutil.rmtree(os.path.join(self.root, "state"), True)
                store = self.wal_store(**options)
                if damage:
                    store.execute(damage)
                opened = self.core.registry.opened
                self.refused(code)
                self.assertEqual(self.core.registry.opened, opened + 1, "decided inside the read")
                self.assertEqual(self.core.registry.total_open(), 0)

    def test_the_counted_connection_lives_exactly_as_long_as_the_read(self) -> None:
        store = self.wal_store()
        store.add("a", epoch=NOW - 60, prompt_tokens=1)
        during = []
        original = lens_store._validate

        def observing(conn, sharing=lens_store.WAL_UNVERIFIED):
            during.append((self.core.registry.total_open(), conn.in_transaction))
            return original(conn, sharing)

        lens_store._validate = observing
        try:
            self.read("1h")
            store.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
            self.refused("store_unsupported")
        finally:
            lens_store._validate = original
        self.assertEqual(during, [(1, True), (1, True)])
        self.assertEqual((self.core.registry.opened, self.core.registry.closed), (2, 2))
        self.assertEqual(self.core.registry.total_open(), 0)
        # No read mark outlives the read: a full checkpoint finds no reader.
        writer = self.same_process_writer(store)
        self.assertEqual(writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)


class TestWalCompatibility(WalCase):
    """Where core does not say WAL may be shared, a WAL store is refused, never read around."""

    def test_an_incompatible_runtime_refuses_before_sqlite_opens_the_file(self) -> None:
        core = cs.make_core(eligible=False)
        self.use_core(core)
        store = self.wal_store()
        store.add("a", epoch=NOW - 60, prompt_tokens=1)
        files, db = self.files(store), self.pinned.digest(store.path)
        self.assertEqual(files, ["usage.sqlite"])
        self.refused("store_wal_incompatible")
        self.assertEqual(self.files(store), files, "no side file: SQLite never opened it")
        self.assertEqual(self.pinned.digest(store.path), db)
        self.assertEqual(core.registry.opened, 0)
        self.assertIn("wal_file", core.registry.names())

    def test_an_incompatible_runtime_refuses_a_live_log_and_reads_persist(self) -> None:
        core = cs.make_core(eligible=False)
        self.use_core(core)
        store = self.wal_store()
        proc = self.writer_process(count=3)
        self.go(proc)
        self.committed(proc, 3)
        self.refused("store_wal_incompatible")
        self.assertEqual(core.registry.opened, 0)
        self.assertNotIn("wal_file", core.registry.names(), "the log beside it decided first")
        self.release(proc)
        # The same runtime reads core's rollback fallback (PERSIST) as before.
        shutil.rmtree(os.path.join(self.root, "state"))
        self.store(journal_mode="persist").add("b", epoch=NOW - 60, prompt_tokens=2)
        read = self.read("1h")
        self.assertEqual([row["prompt_tokens"] for row in read["rows"]], [2])
        self.assertEqual((read["facts"]["journal"], read["facts"]["connection"]), ("rollback", "counted"))

    def test_without_core_a_wal_store_is_refused(self) -> None:
        self.use_core(None)
        store = self.wal_store()
        store.add("a", epoch=NOW - 60, prompt_tokens=1)
        db = self.pinned.digest(store.path)
        # No header read without core: SQLite opens the file, the transaction
        # sees WAL and nothing is read; SQLite has created its side files by
        # then (the residual SKILL.md states).
        self.refused("store_wal_unverified")
        self.assertEqual(self.pinned.digest(store.path), db)
        listing = self.files(store)
        self.assertEqual(listing, ["usage.sqlite", "usage.sqlite-shm", "usage.sqlite-wal"])
        # With the log beside it, refused before SQLite opens anything.
        self.refused("store_wal_unverified")
        self.assertEqual(self.files(store), listing)

    def test_a_core_missing_a_name_or_failing_its_check_is_unverified(self) -> None:
        store = self.wal_store()
        store.add("a", epoch=NOW - 60, prompt_tokens=1)
        for options in (dict(policy=False), dict(counted=False), dict(policy_error=OSError("statfs")),
                        dict(policy_error=ValueError("mountinfo"))):
            with self.subTest(options=repr(options)):
                core = cs.make_core(**options)
                self.use_core(core)
                self.refused("store_wal_unverified")
                self.assertEqual(core.registry.opened, 0, "refused from the header, before any open")
        # Without core's header read either, the transaction is the backstop.
        core = cs.make_core(policy=False, header=False)
        self.use_core(core)
        self.refused("store_wal_unverified")
        self.assertEqual((core.registry.opened, core.registry.total_open()), (1, 0))


class TestRollbackJournal(StoreCase):
    """Core's rollback fallback (PERSIST) is read exactly as 1.2.0 read it."""

    def digests(self, state: str):
        result = {}
        for name in sorted(os.listdir(state)):
            with open(os.path.join(state, name), "rb") as handle:   # no connection is open here
                result[name] = hashlib.sha256(handle.read()).hexdigest()
        return result

    def test_the_persist_journal_is_read_as_before_on_every_runtime(self) -> None:
        store = self.store(journal_mode="persist")
        for index in range(3):
            store.add("r%d" % index, epoch=NOW - 60 + index, prompt_tokens=index + 1)
        state = os.path.dirname(store.path)
        self.assertEqual(sorted(os.listdir(state)), ["usage.sqlite", "usage.sqlite-journal"],
                         "a retained rollback journal, as core's PERSIST writers leave it")
        before = self.digests(state)
        answers = []
        runtimes = (
            (cs.make_core(), "counted", False),                # shares WAL: no header read needed
            (cs.make_core(eligible=False), "counted", True),   # does not: header read first, rollback found
            (cs.make_core(counted=False), "plain", True),      # cannot tell: header read first
            (None, "plain", None),                             # no core at all
        )
        for core, connection, header_read in runtimes:
            self.use_core(core)
            payload = self.snapshot("1h")
            self.assertEqual((payload["source"]["journal"], payload["source"]["connection"]),
                             ("rollback", connection))
            answers.append(payload["points"])
            if core is not None:
                self.assertEqual(core.registry.total_open(), 0)
                self.assertEqual("wal_file" in core.registry.names(), header_read)
        self.assertEqual([p["prompt_tokens"] for p in answers[0]], [1, 2, 3])
        self.assertTrue(all(points == answers[0] for points in answers))
        self.assertEqual(self.digests(state), before, "no byte and no file changed")

    def test_a_stray_empty_log_beside_a_rollback_store(self) -> None:
        store = self.store()
        store.add("a", epoch=NOW - 60, prompt_tokens=1)
        with open(store.path + "-wal", "wb"):
            pass
        state = os.path.dirname(store.path)
        listing = sorted(os.listdir(state))
        # A sharing runtime opens it: SQLite treats an empty log as absent.
        read = self.read("1h")
        self.assertEqual(read["facts"]["journal"], "rollback")
        self.assertEqual(sorted(os.listdir(state)), listing, "no -shm: WAL was never entered")
        # Elsewhere a log beside the store is refused before anything opens it, as in 1.2.0.
        for core, code in ((cs.make_core(eligible=False), "store_wal_incompatible"), (None, "store_wal_unverified")):
            self.use_core(core)
            self.refused(code)
            self.assertEqual(sorted(os.listdir(state)), listing)
            if core is not None:
                self.assertEqual(core.registry.opened, 0)


if __name__ == "__main__":
    unittest.main()
