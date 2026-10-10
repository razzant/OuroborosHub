"""Tests for the usage-store reader (``lens_store``) and its projection.

Standard-library ``unittest`` only. Every store is built by ``store_fixture``
(core schema 1, column for column) inside a fresh temporary directory; nothing
here opens a real install's data.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

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
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="context-lens-store-")
        self.addCleanup(shutil.rmtree, self.root, True)

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

        lens_store.sqlite3.connect = recording
        try:
            conn = lens_store.connect(store.path)
        finally:
            lens_store.sqlite3.connect = original
        try:
            uri, kwargs = seen[0]
            self.assertTrue(uri.startswith("file:"))
            self.assertIn("mode=ro", uri)
            self.assertNotIn("immutable", uri)
            self.assertNotIn("nolock", uri)
            self.assertTrue(kwargs.get("uri"))
            self.assertLessEqual(kwargs.get("timeout"), 1.0)
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.Error):
                conn.execute("INSERT INTO meta (key, value) VALUES ('x', '1')")
        finally:
            conn.close()

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

    def test_a_wal_store_is_refused(self) -> None:
        store = self.store(journal_mode="wal")
        self.refused("store_unsupported")
        os.remove(store.path + "-wal")
        if os.path.exists(store.path + "-shm"):
            os.remove(store.path + "-shm")
        # Without the side file the mode is only visible to SQLite itself.
        self.refused("store_unsupported")

    def test_a_live_wal_file_is_refused_without_opening_the_store(self) -> None:
        store = self.store()
        with open(store.path + "-wal", "wb"):
            pass
        before = sorted(os.listdir(os.path.dirname(store.path)))
        self.refused("store_unsupported")
        self.assertEqual(sorted(os.listdir(os.path.dirname(store.path))), before)

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


if __name__ == "__main__":
    unittest.main()
